"""Task-shaped, read-only Slack commands over one portable Seld connection."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from continuity_kernel.connector_auth import ConnectionHealth
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_identifiers import ConnectionId, SecretName, parse_connection_id
from continuity_kernel.connector_runtime import ConnectorRuntime, default_connector_adapters
from continuity_kernel.connector_sources import read_connector_source
from continuity_kernel.errors import NotFoundError, SetupError, ValidationError
from continuity_kernel.vault import Vault

MAX_SEARCH_PAGES: Final = 10
MAX_SEARCH_RESULTS: Final = 200
MAX_CONTEXT_SIDE: Final = 50
MAX_SNIPPET_CHARS: Final = 4_000
MAX_REFERENCE_ENTRIES: Final = 128
REFERENCE_TTL: Final = timedelta(hours=6)
_REFERENCE_NAME: Final = SecretName("slack-reference-map")
_REFERENCE = re.compile(r"^slack:v1:(?P<token>[A-Za-z0-9_-]{16,64})$")


RuntimeFactory = Callable[[], ConnectorRuntime]


class SlackTaskReader:
    """Expose human-scale Slack reads without a private checkout or legacy session."""

    def __init__(
        self,
        vault: Vault,
        *,
        connection_id: str | None = None,
        auth_manager: ConnectorAuthManager | None = None,
        runtime_factory: RuntimeFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.vault = vault
        self.auth = auth_manager or ConnectorAuthManager(vault)
        self.connection_id = self._select_connection(connection_id)
        self._runtime_factory = runtime_factory or (
            lambda: ConnectorRuntime(vault, adapters=default_connector_adapters())
        )
        self._now = now or (lambda: datetime.now(UTC))

    def status(self) -> dict[str, object]:
        response = self._call("identity.get", {})
        identity = _mapping(response.get("result"), "Slack identity result")
        if identity.get("ok") is not True:
            raise SetupError("Slack identity verification failed")
        metadata = self.auth.verified_connection_metadata(self.connection_id)
        return {
            "account_label": metadata.account.label,
            "access": self.auth.access_tier(self.connection_id).value,
            "available": True,
            "identity_verified": True,
            "mode": "read_only",
            "provider": "slack",
            "user_name": _optional_text(identity.get("user")),
            "workspace_name": _optional_text(identity.get("team")),
        }

    @staticmethod
    def capabilities() -> dict[str, object]:
        return {
            "identity": "one exact verified portable Seld Slack connection",
            "mode": "read_only",
            "operations": {
                "slack-poll": "record one bounded current source read",
                "slack-inbox": "group bounded recent search results by conversation",
                "slack-context": "expand one short-lived opaque Slack reference",
                "slack-search": "run one deliberate bounded Slack search",
                "slack-status": "verify the live Slack user and workspace",
            },
            "defaults": {
                "inbox_days": 30,
                "inbox_conversations": 12,
                "inbox_messages_per_conversation": 2,
                "search_pages": 1,
            },
            "known_omissions": [
                "polling is a bounded current projection, not an exhaustive history mirror",
                "search and inbox remain bounded when Slack reports more results",
                "opaque context references expire after six hours",
            ],
            "prohibited": [
                "post",
                "react",
                "join",
                "mark read",
                "upload",
                "edit",
                "delete",
            ],
        }

    def poll(self, *, limit: int = 25) -> dict[str, object]:
        delivery = read_connector_source(
            self.vault,
            connection_id=str(self.connection_id),
            source_id="slack",
            limit=limit,
            timeout_seconds=45.0,
        )
        record = _mapping(delivery.get("record"), "Slack source record")
        result = _required_text(record.get("result"), "Slack source result")
        source_revision = _required_text(delivery.get("sourceRevision"), "Slack source revision")
        self.vault.record_source_observation(
            expected_revision=source_revision,
            source_id="slack",
            actor_ref="system-role:slack-task-reader",
            result=result,
            covered_through=_optional_text(record.get("coveredThrough")),
            completeness=_optional_text(record.get("completeness")),
            account_binding=_optional_text(record.get("accountBinding")),
            tool_binding=_optional_text(record.get("toolBinding")),
            evidence_refs=tuple(_text_list(record.get("evidenceRefs"))),
            error_code=_optional_text(record.get("errorCode")),
        )
        items = delivery.get("items")
        if not isinstance(items, list):
            items = []
        return {
            "coverage": {
                "checkpoint_safe": result in {"success", "explicit_empty"},
                "known_omissions": self.capabilities()["known_omissions"],
                "returned": len(items),
                "scope": "current_visible_conversations",
                "status": result,
                "truncated_reason": (
                    "bounded_current_projection"
                    if record.get("completeness") == "partial"
                    else None
                ),
            },
            "items": items,
            "observed_at": _optional_text(record.get("coveredThrough")),
            "status": result,
        }

    def search(
        self,
        query: str,
        *,
        max_pages: int = 1,
        max_results: int = 100,
        snippet_chars: int = 320,
    ) -> dict[str, object]:
        clean_query = _query(query)
        _bounded_int(max_pages, "Slack search page bound", 1, MAX_SEARCH_PAGES)
        _bounded_int(max_results, "Slack search result bound", 1, MAX_SEARCH_RESULTS)
        _bounded_int(snippet_chars, "Slack snippet bound", 40, MAX_SNIPPET_CHARS)
        messages: list[dict[str, object]] = []
        total_known: int | None = None
        page_count: int | None = None
        pages_read = 0
        for page in range(1, max_pages + 1):
            remaining = max_results - len(messages)
            if remaining <= 0:
                break
            response = self._call(
                "search.messages",
                {
                    "count": min(100, remaining),
                    "page": page,
                    "query": clean_query,
                    "sort": "timestamp",
                    "sort_direction": "descending",
                },
            )
            result = _mapping(response.get("result"), "Slack search result")
            message_result = _mapping(result.get("messages"), "Slack search messages")
            matches = message_result.get("matches")
            if not isinstance(matches, list):
                raise ValidationError("Slack search returned malformed matches")
            pagination = message_result.get("pagination")
            if isinstance(pagination, Mapping):
                total_known = _optional_int(pagination.get("total_count"))
                page_count = _optional_int(pagination.get("page_count"))
            pages_read += 1
            messages.extend(self._render_matches(matches, snippet_chars=snippet_chars)[:remaining])
            if page_count is not None and page >= page_count:
                break
            if not matches:
                break
        complete = page_count is not None and pages_read >= page_count
        if total_known is not None and len(messages) >= total_known:
            complete = True
        return {
            "coverage": {
                "checkpoint_safe": True,
                "known_omissions": [],
                "pages_read": pages_read,
                "returned": len(messages),
                "scope": "search",
                "status": "complete" if complete else "partial",
                "total_known": total_known,
                "truncated_reason": None if complete else "search_bound",
            },
            "messages": messages,
            "observed_at": self._now().isoformat().replace("+00:00", "Z"),
            "query": clean_query,
        }

    def inbox(
        self,
        *,
        since: str | None = None,
        max_pages: int = 2,
        max_results: int = 200,
        max_conversations: int = 12,
        messages_per_conversation: int = 2,
        snippet_chars: int = 240,
    ) -> dict[str, object]:
        _bounded_int(max_conversations, "Slack conversation bound", 1, 50)
        _bounded_int(messages_per_conversation, "Slack message bound", 1, 10)
        start = _since_date(since, now=self._now())
        searched = self.search(
            f"after:{start}",
            max_pages=max_pages,
            max_results=max_results,
            snippet_chars=snippet_chars,
        )
        raw_messages = searched["messages"]
        assert isinstance(raw_messages, list)
        grouped: dict[str, dict[str, object]] = {}
        for raw in raw_messages:
            message = _mapping(raw, "Slack inbox message")
            label = _optional_text(message.get("conversation")) or "Unknown conversation"
            group = grouped.setdefault(label, {"conversation": label, "messages": []})
            group_messages = cast(list[dict[str, object]], group["messages"])
            if len(group_messages) < messages_per_conversation:
                group_messages.append(dict(message))
        groups = list(grouped.values())[:max_conversations]
        coverage = dict(cast(dict[str, object], searched["coverage"]))
        if len(grouped) > max_conversations:
            coverage["status"] = "partial"
            coverage["truncated_reason"] = "conversation_bound"
        return {
            "coverage": coverage,
            "groups": groups,
            "observed_at": searched["observed_at"],
            "since": start,
        }

    def context(
        self,
        reference: str,
        *,
        before: int = 5,
        after: int = 5,
        include_thread: bool = True,
        snippet_chars: int = 1_000,
    ) -> dict[str, object]:
        _bounded_int(before, "Slack context-before bound", 0, MAX_CONTEXT_SIDE)
        _bounded_int(after, "Slack context-after bound", 0, MAX_CONTEXT_SIDE)
        _bounded_int(snippet_chars, "Slack snippet bound", 40, MAX_SNIPPET_CHARS)
        binding = self._reference_store().resolve(reference)
        channel = _required_text(binding.get("channel"), "Slack reference channel")
        timestamp = _required_text(binding.get("ts"), "Slack reference timestamp")
        thread_timestamp = _optional_text(binding.get("thread_ts")) or timestamp
        messages: list[dict[str, object]] = []
        pages_read = 0
        if before:
            response = self._call(
                "messages.list",
                {"channel": channel, "latest": timestamp, "limit": before + 1},
            )
            messages.extend(
                self._render_history(response, channel=channel, snippet_chars=snippet_chars)
            )
            pages_read += 1
        if after:
            response = self._call(
                "messages.list",
                {"channel": channel, "oldest": timestamp, "limit": after + 1},
            )
            messages.extend(
                self._render_history(response, channel=channel, snippet_chars=snippet_chars)
            )
            pages_read += 1
        if include_thread:
            response = self._call(
                "threads.list",
                {"channel": channel, "thread_ts": thread_timestamp, "limit": 100},
            )
            messages.extend(
                self._render_history(response, channel=channel, snippet_chars=snippet_chars)
            )
            pages_read += 1
        unique = {
            (
                str(item.get("at") or ""),
                str(item.get("sender") or ""),
                str(item.get("text") or ""),
            ): item
            for item in messages
        }
        ordered = sorted(unique.values(), key=lambda item: str(item.get("at") or ""))
        return {
            "coverage": {
                "checkpoint_safe": True,
                "known_omissions": [],
                "pages_read": pages_read,
                "returned": len(ordered),
                "scope": "message_context",
                "status": "complete",
                "truncated_reason": None,
            },
            "focus_ref": reference,
            "messages": ordered,
            "observed_at": self._now().isoformat().replace("+00:00", "Z"),
        }

    def _select_connection(self, requested: str | None) -> ConnectionId:
        snapshot = self.vault.get_connection_snapshot()
        if requested is not None:
            clean = parse_connection_id(requested)
            selected = snapshot.connection(clean)
            if selected is None:
                raise NotFoundError("Slack connection was not found")
            if selected.provider != "slack" or "slack" not in selected.source_ids:
                raise ValidationError("connection does not authorize Slack")
            return clean
        candidates = [
            item
            for item in snapshot.connections
            if item.provider == "slack"
            and "slack" in item.source_ids
            and item.health in {ConnectionHealth.READY, ConnectionHealth.DEGRADED}
        ]
        if not candidates:
            raise SetupError("No ready Slack connection. Run `gsv connectors connect slack`.")
        if len(candidates) > 1:
            raise ValidationError("multiple Slack connections exist; pass --connection-id")
        return candidates[0].connection_id

    def _call(self, operation: str, input_value: Mapping[str, object]) -> dict[str, object]:
        runtime = self._runtime_factory()
        try:
            return runtime.call_tool(
                "gsv_slack_read",
                {
                    "connection_id": str(self.connection_id),
                    "input": dict(input_value),
                    "operation": operation,
                },
            )
        finally:
            runtime.close()

    def _render_matches(
        self, matches: list[object], *, snippet_chars: int
    ) -> list[dict[str, object]]:
        pending: list[tuple[dict[str, object], dict[str, str | None]]] = []
        for raw in matches:
            match = _mapping(raw, "Slack search match")
            channel = _mapping(match.get("channel"), "Slack search channel")
            channel_id = _required_text(channel.get("id"), "Slack channel")
            timestamp = _required_text(match.get("ts"), "Slack message timestamp")
            thread_timestamp = _thread_timestamp(match)
            pending.append(
                (
                    {
                        "at": _slack_timestamp_iso(timestamp),
                        "conversation": _optional_text(channel.get("name")),
                        "sender": _optional_text(match.get("username")),
                        "text": _snippet(match.get("text"), snippet_chars),
                    },
                    {"channel": channel_id, "thread_ts": thread_timestamp, "ts": timestamp},
                )
            )
        references = self._reference_store().issue([binding for _, binding in pending])
        return [
            {**message, "ref": reference}
            for (message, _), reference in zip(pending, references, strict=True)
        ]

    def _render_history(
        self,
        response: Mapping[str, object],
        *,
        channel: str,
        snippet_chars: int,
    ) -> list[dict[str, object]]:
        result = _mapping(response.get("result"), "Slack history result")
        raw_messages = result.get("messages")
        if not isinstance(raw_messages, list):
            raise ValidationError("Slack history returned malformed messages")
        pending: list[tuple[dict[str, object], dict[str, str | None]]] = []
        for raw in raw_messages:
            message = _mapping(raw, "Slack history message")
            timestamp = _required_text(message.get("ts"), "Slack message timestamp")
            pending.append(
                (
                    {
                        "at": _slack_timestamp_iso(timestamp),
                        "sender": _optional_text(message.get("username")),
                        "text": _snippet(message.get("text"), snippet_chars),
                    },
                    {
                        "channel": channel,
                        "thread_ts": _thread_timestamp(message),
                        "ts": timestamp,
                    },
                )
            )
        references = self._reference_store().issue([binding for _, binding in pending])
        return [
            {**message, "ref": reference}
            for (message, _), reference in zip(pending, references, strict=True)
        ]

    def _reference_store(self) -> _SlackReferenceStore:
        return _SlackReferenceStore(
            self.auth,
            self.connection_id,
            now=self._now,
        )


class _SlackReferenceStore:
    """Keep raw routing identifiers only inside vault-scoped OS secret custody."""

    def __init__(
        self,
        manager: ConnectorAuthManager,
        connection_id: ConnectionId,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self.manager = manager
        self.connection_id = connection_id
        self.now = now

    def issue(self, bindings: list[dict[str, str | None]]) -> list[str]:
        state = self._load()
        entries = self._current_entries(state)
        issued_at = self.now().astimezone(UTC).isoformat()
        references: list[str] = []
        for binding in bindings:
            token = secrets.token_urlsafe(18)
            entries[token] = {**binding, "issued_at": issued_at}
            references.append(f"slack:v1:{token}")
        ordered = sorted(
            entries.items(),
            key=lambda item: _required_text(item[1].get("issued_at"), "Slack reference time"),
            reverse=True,
        )[:MAX_REFERENCE_ENTRIES]
        self.manager.secret_store.set_secret(
            self.connection_id,
            _REFERENCE_NAME,
            json.dumps(dict(ordered), separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )
        return references

    def resolve(self, reference: str) -> dict[str, object]:
        matched = _REFERENCE.fullmatch(reference)
        if matched is None:
            raise ValidationError("Slack reference is invalid")
        entries = self._current_entries(self._load())
        binding = entries.get(matched.group("token"))
        if binding is None:
            raise NotFoundError("Slack reference expired; run `gsv slack-inbox` again")
        return dict(binding)

    def _load(self) -> dict[str, object]:
        encoded = self.manager.secret_store.get_secret(self.connection_id, _REFERENCE_NAME)
        if encoded is None:
            return {}
        try:
            value = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("stored Slack references are invalid") from exc
        if not isinstance(value, dict) or len(value) > MAX_REFERENCE_ENTRIES:
            raise ValidationError("stored Slack references are invalid")
        return cast(dict[str, object], value)

    def _current_entries(self, state: dict[str, object]) -> dict[str, dict[str, object]]:
        now = self.now().astimezone(UTC)
        current: dict[str, dict[str, object]] = {}
        for token, raw in state.items():
            if not isinstance(token, str) or _REFERENCE.fullmatch(f"slack:v1:{token}") is None:
                raise ValidationError("stored Slack references are invalid")
            entry = _mapping(raw, "stored Slack reference")
            issued = _parse_time(entry.get("issued_at"))
            if now - issued <= REFERENCE_TTL:
                current[token] = dict(entry)
        return current


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} is malformed")
    return cast(Mapping[str, object], value)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is missing")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return cast(list[str], value)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _bounded_int(value: int, label: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{label} must be between {minimum} and {maximum}")


def _query(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
        raise ValidationError("Slack search query is empty or too large")
    return value.strip()


def _since_date(value: str | None, *, now: datetime) -> str:
    if value is None:
        return (now.astimezone(UTC).date() - timedelta(days=30)).isoformat()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValidationError("Slack since date must be YYYY-MM-DD") from exc
    return parsed.isoformat()


def _snippet(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text[:limit]


def _thread_timestamp(message: Mapping[str, object]) -> str | None:
    direct = _optional_text(message.get("thread_ts"))
    if direct is not None:
        return direct
    db_message = message.get("db_message")
    if isinstance(db_message, Mapping):
        return _optional_text(db_message.get("thread_ts"))
    return None


def _slack_timestamp_iso(value: str) -> str:
    try:
        seconds = float(value)
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError) as exc:
        raise ValidationError("Slack message timestamp is invalid") from exc


def _parse_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(_required_text(value, "Slack reference time"))
    except ValueError as exc:
        raise ValidationError("stored Slack reference time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("stored Slack reference time is invalid")
    return parsed.astimezone(UTC)
