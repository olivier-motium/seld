"""Small dependency-free MCP stdio server for Codex."""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any, Final

from continuity_kernel import __version__
from continuity_kernel.config import resolve_vault
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED
from continuity_kernel.direction import direction_aim, direction_dict
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError
from continuity_kernel.operations import (
    OperationBinding,
    OperationLedger,
    capture_operation_binding,
)
from continuity_kernel.portfolio import (
    portfolio_dict,
    portfolio_inspection_dict,
    portfolio_item,
)
from continuity_kernel.records import record_dict
from continuity_kernel.vault import Vault, doctor_dict

PROTOCOL_VERSION: Final = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS: Final = frozenset({PROTOCOL_VERSION})
MAX_REQUEST_BYTES: Final = 1024 * 1024
OPERATION_TOOL_NAMES: Final = frozenset(
    {
        "gsv_operation_accept",
        "gsv_operation_archive_closed",
        "gsv_operation_list",
        "gsv_operation_reject",
    }
)
GUIDED_REVIEW_PROFILE: Final = "guided-review"
GUIDED_REVIEW_TOOL_NAMES: Final = frozenset(
    {
        "gsv_status",
        "gsv_context",
        "gsv_task_list",
        "gsv_task_show",
        "gsv_task_create",
        "gsv_task_update",
        "gsv_direction_show",
        "gsv_portfolio_show",
        "gsv_portfolio_inspect",
        "gsv_portfolio_set",
        "gsv_entity_list",
        "gsv_entity_show",
        "gsv_entity_create",
        "gsv_entity_update",
        "gsv_thread_list",
        "gsv_thread_show",
        "gsv_thread_create",
        "gsv_thread_update",
        "gsv_operation_list",
        "gsv_operation_accept",
        "gsv_operation_reject",
    }
)


@dataclass
class _OperationSession:
    """Lazy, process-lifetime binding for the optional operation tool family."""

    binding: OperationBinding | None = None


def serve(
    vault: Vault | None = None,
    *,
    profile: str | None = None,
    event_id: str | None = None,
) -> int:
    """Serve line-delimited MCP JSON-RPC until stdin closes."""

    bound_event_id = _profile_event_id(profile, event_id)
    bound = vault or Vault(resolve_vault())
    operation_session = _OperationSession() if CONTROL_STORE_SUPPORTED else None
    for raw_line in _bounded_lines(sys.stdin.buffer):
        if raw_line is None:
            _write(_error(None, -32600, "JSON-RPC request exceeds its size bound"))
            continue
        if not raw_line.strip():
            continue
        request_id: object = None
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValidationError("JSON-RPC message must be an object")
            request_id = message.get("id")
            response = _handle(
                message,
                vault=bound,
                operation_session=operation_session,
                profile=profile,
                event_id=bound_event_id,
            )
            if response is not None:
                _write(response)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write(_error(request_id, -32700, "invalid JSON"))
        except ContinuityError as exc:
            _write(_error(request_id, -32000, str(exc)))
        except Exception as exc:  # pragma: no cover - final protocol safety net
            _write(_error(request_id, -32603, f"internal error: {type(exc).__name__}"))
    return 0


def _bounded_lines(stream: IO[bytes]) -> Iterator[bytes | None]:
    while True:
        raw = stream.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return
        if len(raw) <= MAX_REQUEST_BYTES:
            yield raw
            continue
        while raw and not raw.endswith(b"\n"):
            raw = stream.readline(MAX_REQUEST_BYTES + 1)
        yield None


def _handle(
    message: dict[str, Any],
    *,
    vault: Vault | None = None,
    operation_binding: OperationBinding | None = None,
    operation_session: _OperationSession | None = None,
    profile: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any] | None:
    bound_event_id = _profile_event_id(profile, event_id)
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        params = message.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        )
        return _result(
            request_id,
            {
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": (
                    "GSV is a private local vault. Read exact records before writes, "
                    "use compare-and-swap revisions, and never store secrets or raw provider "
                    "payloads."
                ),
                "protocolVersion": protocol_version,
                "serverInfo": {"name": "gsv", "version": __version__},
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": _advertised_tools(profile=profile)})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "tools/call requires a tool name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "tool arguments must be an object")
        try:
            payload = _call(
                params["name"],
                arguments,
                vault=vault,
                operation_binding=operation_binding,
                operation_session=operation_session,
                profile=profile,
                event_id=bound_event_id,
            )
            return _result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                payload, ensure_ascii=False, indent=2, sort_keys=True
                            ),
                        }
                    ],
                    "isError": False,
                    "structuredContent": payload,
                },
            )
        except ContinuityError as exc:
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def _call(
    name: str,
    values: dict[str, Any],
    *,
    vault: Vault | None = None,
    operation_binding: OperationBinding | None = None,
    operation_session: _OperationSession | None = None,
    profile: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    allowed = _profile_tool_names(profile)
    if allowed is not None and name not in allowed:
        raise ValidationError(f"unknown tool: {name}")
    if name in {"gsv_operation_accept", "gsv_operation_reject"} and event_id is not None:
        requested_event_id = _string(values, "event_id")
        if requested_event_id != event_id:
            raise ValidationError("operation event is outside this guided-review MCP binding")
    if name in OPERATION_TOOL_NAMES and not CONTROL_STORE_SUPPORTED:
        raise ValidationError(f"unknown tool: {name}")
    vault = vault or Vault(resolve_vault())
    if name in OPERATION_TOOL_NAMES:
        if operation_session is not None:
            if operation_session.binding is None:
                operation_session.binding = capture_operation_binding(vault.root)
            operation_binding = operation_session.binding
        elif operation_binding is None:
            operation_binding = capture_operation_binding(vault.root)
    if name == "gsv_status":
        return vault.status()
    if name == "gsv_context":
        return {
            "context": vault.context_pack(max_characters=_integer(values, "max_characters", 48_000))
        }
    if name == "gsv_doctor":
        return doctor_dict(vault.doctor(repair=False))
    if name == "gsv_task_list":
        status = _optional_string(values, "status")
        return {"tasks": [record_dict(item) for item in vault.list_tasks(status=status)]}
    if name == "gsv_task_show":
        return record_dict(vault.get_task(_string(values, "id")))
    if name == "gsv_task_create":
        return record_dict(
            vault.create_task(
                identifier=_string(values, "id"),
                title=_string(values, "title"),
                outcome=_string(values, "outcome"),
                status=_optional_string(values, "status") or "captured",
                next_actor=_optional_string(values, "next_actor"),
                next_action=_optional_string(values, "next_action"),
                waiting_on=_optional_string(values, "waiting_on"),
                rank=_optional_integer(values, "rank"),
                active_thread_id=_optional_string(values, "active_thread_id"),
                refs=_strings(values, "refs"),
            )
        )
    if name == "gsv_task_update":
        return record_dict(
            vault.update_task(
                _string(values, "id"),
                expected_revision=_string(values, "expected_revision"),
                title=_optional_string(values, "title"),
                outcome=_optional_string(values, "outcome"),
                status=_optional_string(values, "status"),
                next_actor=_optional_string(values, "next_actor"),
                next_action=_optional_string(values, "next_action"),
                waiting_on=_optional_string(values, "waiting_on"),
                rank=_optional_integer(values, "rank"),
                active_thread_id=_optional_string(values, "active_thread_id"),
                clear_next_actor=_boolean(values, "clear_next_actor"),
                clear_next_action=_boolean(values, "clear_next_action"),
                clear_waiting_on=_boolean(values, "clear_waiting_on"),
                clear_rank=_boolean(values, "clear_rank"),
                clear_active_thread_id=_boolean(values, "clear_active_thread_id"),
                add_refs=_strings(values, "add_refs"),
                remove_refs=_strings(values, "remove_refs"),
            )
        )
    if name == "gsv_direction_show":
        return direction_dict(vault.get_direction())
    if name == "gsv_direction_set":
        raw_aims = values.get("aims")
        if not isinstance(raw_aims, list):
            raise ValidationError("aims must be an array")
        aims = []
        for raw in raw_aims:
            if not isinstance(raw, dict):
                raise ValidationError("each Direction aim must be an object")
            aims.append(
                direction_aim(
                    identifier=_string(raw, "id"),
                    title=_string(raw, "title"),
                    desired_state=_string(raw, "desired_state"),
                )
            )
        return direction_dict(
            vault.set_direction(
                expected_revision=_string(values, "expected_revision"),
                status=_string(values, "status"),
                current_chapter=_string(values, "current_chapter"),
                aims=tuple(aims),
            )
        )
    if name == "gsv_portfolio_show":
        return portfolio_dict(vault.get_portfolio())
    if name == "gsv_portfolio_inspect":
        return portfolio_inspection_dict(vault.inspect_portfolio())
    if name == "gsv_portfolio_migrate_review_session":
        return record_dict(
            vault.migrate_legacy_review_session(
                _string(values, "session_id"),
                expected_session_revision=_string(values, "expected_session_revision"),
                expected_review_thread_revision=_string(values, "expected_review_thread_revision"),
                thread_title=_optional_string(values, "thread_title"),
                thread_purpose=_optional_string(values, "thread_purpose"),
                thread_summary=_optional_string(values, "thread_summary"),
            )
        )
    if name == "gsv_portfolio_set":
        raw_items = values.get("items")
        if not isinstance(raw_items, list):
            raise ValidationError("items must be an array")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValidationError("each Portfolio item must be an object")
            items.append(
                portfolio_item(
                    task_id_value=_string(raw, "task_id"),
                    task_revision=_string(raw, "task_revision"),
                    stance=_string(raw, "stance"),
                    reason=_string(raw, "reason"),
                    work_thread_id=_optional_string(raw, "work_thread_id"),
                    work_thread_revision=_optional_string(raw, "work_thread_revision"),
                    direction_aim_ids=_strings(raw, "direction_aim_ids"),
                    unaligned_reason=_optional_string(raw, "unaligned_reason"),
                )
            )
        return portfolio_dict(
            vault.set_portfolio(
                expected_revision=_string(values, "expected_revision"),
                summary=_string(values, "summary"),
                items=tuple(items),
                direction_revision=_optional_string(values, "direction_revision"),
            )
        )
    if name == "gsv_entity_list":
        return {"entities": [record_dict(item) for item in vault.list_entities()]}
    if name == "gsv_entity_show":
        return record_dict(vault.get_entity(_string(values, "id")))
    if name == "gsv_entity_create":
        return record_dict(
            vault.create_entity(
                identifier=_string(values, "id"),
                title=_string(values, "title"),
                entity_type=_string(values, "entity_type"),
                summary=_string(values, "summary"),
                aliases=_strings(values, "aliases"),
                refs=_strings(values, "refs"),
            )
        )
    if name == "gsv_entity_update":
        return record_dict(
            vault.update_entity(
                _string(values, "id"),
                expected_revision=_string(values, "expected_revision"),
                title=_optional_string(values, "title"),
                summary=_optional_string(values, "summary"),
                aliases=_optional_strings(values, "aliases"),
                add_refs=_strings(values, "add_refs"),
                remove_refs=_strings(values, "remove_refs"),
            )
        )
    if name == "gsv_thread_list":
        status = _optional_string(values, "status")
        return {"threads": [record_dict(item) for item in vault.list_threads(status=status)]}
    if name == "gsv_thread_show":
        return record_dict(vault.get_thread(_string(values, "id")))
    if name == "gsv_thread_create":
        return record_dict(
            vault.create_thread(
                identifier=_string(values, "id"),
                title=_string(values, "title"),
                purpose=_string(values, "purpose"),
                summary=_string(values, "summary"),
                status=_optional_string(values, "status") or "active",
                next_move=_optional_string(values, "next_move"),
                focus_task_id=_optional_string(values, "focus_task_id"),
                task_ids=_strings(values, "task_ids"),
                entity_ids=_strings(values, "entity_ids"),
                refs=_strings(values, "refs"),
            )
        )
    if name == "gsv_thread_update":
        return record_dict(
            vault.update_thread(
                _string(values, "id"),
                expected_revision=_string(values, "expected_revision"),
                title=_optional_string(values, "title"),
                purpose=_optional_string(values, "purpose"),
                summary=_optional_string(values, "summary"),
                status=_optional_string(values, "status"),
                next_move=_optional_string(values, "next_move"),
                clear_next_move=_boolean(values, "clear_next_move"),
                focus_task_id=_optional_string(values, "focus_task_id"),
                clear_focus_task=_boolean(values, "clear_focus_task"),
                task_ids=_optional_strings(values, "task_ids"),
                entity_ids=_optional_strings(values, "entity_ids"),
                add_refs=_strings(values, "add_refs"),
                remove_refs=_strings(values, "remove_refs"),
            )
        )
    if name == "gsv_document_show":
        return vault.read_document(_string(values, "name"))
    if name == "gsv_document_update":
        return vault.write_document(
            _string(values, "name"),
            _string(values, "content"),
            expected_revision=_string(values, "expected_revision"),
        )
    if name == "gsv_backup_create":
        return vault.create_backup()
    if name == "gsv_operation_list":
        assert operation_binding is not None
        snapshot = OperationLedger(vault.root).snapshot(
            expected_vault_id=operation_binding.vault_id,
            expected_root_identity=operation_binding.root_identity,
        )
        return _scoped_operation_snapshot(snapshot.to_dict(), event_id)
    if name in {"gsv_operation_accept", "gsv_operation_reject"}:
        assert operation_binding is not None
        expected_vault_id = _bound_operation_vault_id(values, operation_binding)
        snapshot = OperationLedger(vault.root).decide(
            event_id=_string(values, "event_id"),
            decision="accepted" if name == "gsv_operation_accept" else "rejected",
            actor_ref=_string(values, "actor_ref"),
            reason_code=_string(values, "reason_code"),
            expected_queue_revision=_string(values, "expected_queue_revision"),
            expected_disposition_revision=_string(values, "expected_disposition_revision"),
            expected_vault_id=expected_vault_id,
            expected_root_identity=operation_binding.root_identity,
            result_ref=_optional_string(values, "result_ref"),
        )
        return _scoped_operation_snapshot(snapshot.to_dict(), event_id)
    if name == "gsv_operation_archive_closed":
        assert operation_binding is not None
        expected_vault_id = _bound_operation_vault_id(values, operation_binding)
        return OperationLedger(vault.root).archive_closed(
            expected_queue_revision=_string(values, "expected_queue_revision"),
            expected_disposition_revision=_string(values, "expected_disposition_revision"),
            expected_vault_id=expected_vault_id,
            expected_root_identity=operation_binding.root_identity,
        )
    raise ValidationError(f"unknown tool: {name}")


def _bound_operation_vault_id(
    values: dict[str, Any],
    binding: OperationBinding,
) -> str:
    """Reject caller-supplied identity changes inside one live MCP session."""

    supplied = _string(values, "expected_vault_id")
    if supplied != binding.vault_id:
        raise ConflictError(
            "operation vault binding changed; start a fresh MCP session before retrying"
        )
    return binding.vault_id


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    read_only: bool,
) -> dict[str, Any]:
    return {
        "annotations": {
            "destructiveHint": False,
            "idempotentHint": read_only,
            "openWorldHint": False,
            "readOnlyHint": read_only,
        },
        "description": description,
        "inputSchema": {
            "additionalProperties": False,
            "properties": properties,
            "required": list(required),
            "type": "object",
        },
        "name": name,
    }


TEXT = {"type": "string"}
TEXTS = {"items": {"type": "string"}, "type": "array"}
BOOLEAN = {"type": "boolean"}
TASK_ACTIVE_THREAD_ID = {
    "description": (
        "Opaque active Codex hand identifier. In guided review this must be the raw Codex thread "
        "UUID, never a GSV WorkThread ID such as thread:life-portfolio-review; omit it until the "
        "Codex UUID is known."
    ),
    "type": "string",
}
TASK_REFS = {
    "description": (
        "Task navigation or evidence references. Never use a codex-thread:* reference as a "
        "substitute for active_thread_id or for ownership by a GSV WorkThread."
    ),
    "items": {"type": "string"},
    "type": "array",
}
TASK_REMOVE_REFS = {
    "description": (
        "Exact task references to remove. In guided review remove every codex-thread:* shadow "
        "reference; the real Codex UUID belongs only in active_thread_id."
    ),
    "items": {"type": "string"},
    "type": "array",
}

TOOLS: Final = [
    _tool("gsv_status", "Read vault identity, counts, and digest.", {}, read_only=True),
    _tool(
        "gsv_context",
        "Read the bounded current context pack at the start of substantive work.",
        {"max_characters": {"maximum": 256000, "minimum": 4000, "type": "integer"}},
        read_only=True,
    ),
    _tool(
        "gsv_doctor",
        "Validate vault structure and references without mutation.",
        {},
        read_only=True,
    ),
    _tool("gsv_task_list", "List durable tasks.", {"status": TEXT}, read_only=True),
    _tool(
        "gsv_task_show",
        "Read one exact task and its revision.",
        {"id": TEXT},
        ("id",),
        read_only=True,
    ),
    _tool(
        "gsv_task_create",
        "Create one explicit durable outcome. Do not infer task meaning from source text.",
        {
            "id": TEXT,
            "active_thread_id": TASK_ACTIVE_THREAD_ID,
            "next_action": TEXT,
            "next_actor": {"enum": ["agent", "human", "external"], "type": "string"},
            "outcome": TEXT,
            "refs": TASK_REFS,
            "rank": {"minimum": 0, "type": "integer"},
            "status": TEXT,
            "title": TEXT,
            "waiting_on": TEXT,
        },
        ("id", "title", "outcome"),
        read_only=False,
    ),
    _tool(
        "gsv_task_update",
        "Update an exact task using its latest compare-and-swap revision.",
        {
            "add_refs": TASK_REFS,
            "active_thread_id": TASK_ACTIVE_THREAD_ID,
            "clear_active_thread_id": BOOLEAN,
            "clear_next_action": BOOLEAN,
            "clear_next_actor": BOOLEAN,
            "clear_waiting_on": BOOLEAN,
            "clear_rank": BOOLEAN,
            "expected_revision": TEXT,
            "id": TEXT,
            "next_action": TEXT,
            "next_actor": {"enum": ["agent", "human", "external"], "type": "string"},
            "outcome": TEXT,
            "remove_refs": TASK_REMOVE_REFS,
            "rank": {"minimum": 0, "type": "integer"},
            "status": TEXT,
            "title": TEXT,
            "waiting_on": TEXT,
        },
        ("id", "expected_revision"),
        read_only=False,
    ),
    _tool(
        "gsv_direction_show",
        "Read the exact current whole-life Direction and stable authored aims.",
        {},
        read_only=True,
    ),
    _tool(
        "gsv_direction_set",
        "CAS-author the complete Direction from explicit stable aims; never derive aims from text.",
        {
            "aims": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "desired_state": TEXT,
                        "id": TEXT,
                        "title": TEXT,
                    },
                    "required": ["id", "title", "desired_state"],
                    "type": "object",
                },
                "type": "array",
            },
            "current_chapter": TEXT,
            "expected_revision": TEXT,
            "status": {"enum": ["provisional", "confirmed"], "type": "string"},
        },
        ("expected_revision", "status", "current_chapter", "aims"),
        read_only=False,
    ),
    _tool(
        "gsv_portfolio_show",
        "Read the complete authored Portfolio and its exact revision.",
        {},
        read_only=True,
    ),
    _tool(
        "gsv_portfolio_inspect",
        "Inspect exact Direction, Portfolio, review coverage, and new or changed open work.",
        {},
        read_only=True,
    ),
    _tool(
        "gsv_portfolio_migrate_review_session",
        (
            "CAS-bind one pre-focus review Task and its existing Codex hand to the canonical "
            "review WorkThread. Supply authored WorkThread prose only when its expected "
            "revision is absent."
        ),
        {
            "expected_review_thread_revision": TEXT,
            "expected_session_revision": TEXT,
            "session_id": TEXT,
            "thread_purpose": TEXT,
            "thread_summary": TEXT,
            "thread_title": TEXT,
        },
        (
            "session_id",
            "expected_session_revision",
            "expected_review_thread_revision",
        ),
        read_only=False,
    ),
    _tool(
        "gsv_portfolio_set",
        (
            "Author the complete open Portfolio against exact task and optional WorkThread "
            "revisions. Order and stances are authored judgment, never inferred."
        ),
        {
            "expected_revision": TEXT,
            "items": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "reason": TEXT,
                        "stance": {
                            "enum": [
                                "needs-human",
                                "agent-can-carry",
                                "keep-in-view",
                                "reconsider",
                            ],
                            "type": "string",
                        },
                        "task_id": TEXT,
                        "task_revision": TEXT,
                        "work_thread_id": TEXT,
                        "work_thread_revision": TEXT,
                        "direction_aim_ids": TEXTS,
                        "unaligned_reason": TEXT,
                    },
                    "required": ["task_id", "task_revision", "stance", "reason"],
                    "type": "object",
                },
                "type": "array",
            },
            "direction_revision": TEXT,
            "summary": TEXT,
        },
        ("expected_revision", "summary", "items"),
        read_only=False,
    ),
    _tool("gsv_entity_list", "List canonical entities.", {}, read_only=True),
    _tool(
        "gsv_entity_show",
        "Read one exact canonical entity.",
        {"id": TEXT},
        ("id",),
        read_only=True,
    ),
    _tool(
        "gsv_entity_create",
        "Create a canonical entity only after deliberate identity resolution.",
        {
            "aliases": TEXTS,
            "entity_type": TEXT,
            "id": TEXT,
            "refs": TEXTS,
            "summary": TEXT,
            "title": TEXT,
        },
        ("id", "title", "entity_type", "summary"),
        read_only=False,
    ),
    _tool(
        "gsv_entity_update",
        "Update an exact canonical entity using its latest compare-and-swap revision.",
        {
            "add_refs": TEXTS,
            "aliases": TEXTS,
            "expected_revision": TEXT,
            "id": TEXT,
            "remove_refs": TEXTS,
            "summary": TEXT,
            "title": TEXT,
        },
        ("id", "expected_revision"),
        read_only=False,
    ),
    _tool("gsv_thread_list", "List durable work threads.", {"status": TEXT}, read_only=True),
    _tool(
        "gsv_thread_show",
        "Read one exact work thread.",
        {"id": TEXT},
        ("id",),
        read_only=True,
    ),
    _tool(
        "gsv_thread_create",
        "Create an explicitly authored work thread with exact relationships.",
        {
            "entity_ids": TEXTS,
            "focus_task_id": TEXT,
            "id": TEXT,
            "next_move": TEXT,
            "purpose": TEXT,
            "refs": TEXTS,
            "status": TEXT,
            "summary": TEXT,
            "task_ids": TEXTS,
            "title": TEXT,
        },
        ("id", "title", "purpose", "summary"),
        read_only=False,
    ),
    _tool(
        "gsv_thread_update",
        "Update an exact work thread using its latest compare-and-swap revision.",
        {
            "add_refs": TEXTS,
            "clear_next_move": BOOLEAN,
            "clear_focus_task": BOOLEAN,
            "entity_ids": TEXTS,
            "expected_revision": TEXT,
            "id": TEXT,
            "focus_task_id": TEXT,
            "next_move": TEXT,
            "purpose": TEXT,
            "remove_refs": TEXTS,
            "status": TEXT,
            "summary": TEXT,
            "task_ids": TEXTS,
            "title": TEXT,
        },
        ("id", "expected_revision"),
        read_only=False,
    ),
    _tool(
        "gsv_document_show",
        "Read MIND.md or NOW.md and its revision.",
        {"name": {"enum": ["MIND.md", "NOW.md"], "type": "string"}},
        ("name",),
        read_only=True,
    ),
    _tool(
        "gsv_document_update",
        "Update MIND.md or NOW.md using its latest compare-and-swap revision.",
        {
            "content": TEXT,
            "expected_revision": TEXT,
            "name": {"enum": ["MIND.md", "NOW.md"], "type": "string"},
        },
        ("name", "content", "expected_revision"),
        read_only=False,
    ),
    _tool(
        "gsv_backup_create",
        "Create and verify a portable local vault backup.",
        {},
        read_only=False,
    ),
    _tool(
        "gsv_operation_list",
        "Read pending Bridge intents and their durable accept/reject dispositions.",
        {},
        read_only=True,
    ),
    _tool(
        "gsv_operation_accept",
        (
            "Acknowledge one Bridge intent for later review. This does not approve or execute "
            "the intent, authorize an external effect, or mutate semantic canon."
        ),
        {
            "actor_ref": TEXT,
            "event_id": TEXT,
            "expected_disposition_revision": TEXT,
            "expected_queue_revision": TEXT,
            "expected_vault_id": TEXT,
            "reason_code": TEXT,
            "result_ref": TEXT,
        },
        (
            "event_id",
            "expected_queue_revision",
            "expected_disposition_revision",
            "expected_vault_id",
            "actor_ref",
            "reason_code",
        ),
        read_only=False,
    ),
    _tool(
        "gsv_operation_reject",
        "Reject one Bridge intent durably without executing it or mutating semantic canon.",
        {
            "actor_ref": TEXT,
            "event_id": TEXT,
            "expected_disposition_revision": TEXT,
            "expected_queue_revision": TEXT,
            "expected_vault_id": TEXT,
            "reason_code": TEXT,
            "result_ref": TEXT,
        },
        (
            "event_id",
            "expected_queue_revision",
            "expected_disposition_revision",
            "expected_vault_id",
            "actor_ref",
            "reason_code",
        ),
        read_only=False,
    ),
    _tool(
        "gsv_operation_archive_closed",
        (
            "Archive a fully dispositioned live queue generation to recover bounded capacity; "
            "this never executes an intent or changes semantic canon."
        ),
        {
            "expected_disposition_revision": TEXT,
            "expected_queue_revision": TEXT,
            "expected_vault_id": TEXT,
        },
        ("expected_queue_revision", "expected_disposition_revision", "expected_vault_id"),
        read_only=False,
    ),
]


def _advertised_tools(*, profile: str | None = None) -> list[dict[str, Any]]:
    """Apply an explicit profile and hide an unavailable secure operation lane."""

    allowed = _profile_tool_names(profile)
    tools = TOOLS if allowed is None else [tool for tool in TOOLS if tool["name"] in allowed]
    if not CONTROL_STORE_SUPPORTED:
        tools = [tool for tool in tools if tool["name"] not in OPERATION_TOOL_NAMES]
    return list(tools)


def _profile_tool_names(profile: str | None) -> frozenset[str] | None:
    if profile is None:
        return None
    if profile == GUIDED_REVIEW_PROFILE:
        return GUIDED_REVIEW_TOOL_NAMES
    raise ValidationError(f"unknown MCP profile: {profile}")


def _profile_event_id(profile: str | None, event_id: str | None) -> str | None:
    _profile_tool_names(profile)
    if profile is None:
        if event_id is not None:
            raise ValidationError("an MCP event binding requires an explicit profile")
        return None
    if event_id is None:
        raise ValidationError("guided-review MCP profile requires an exact event ID")
    try:
        parsed = str(uuid.UUID(event_id))
    except (AttributeError, ValueError) as exc:
        raise ValidationError("guided-review MCP event ID must be a canonical UUID") from exc
    if parsed != event_id:
        raise ValidationError("guided-review MCP event ID must be a canonical UUID")
    return event_id


def _scoped_operation_snapshot(
    payload: dict[str, Any],
    event_id: str | None,
) -> dict[str, Any]:
    if event_id is None:
        return payload
    scoped = dict(payload)
    scoped["pending"] = [event for event in payload["pending"] if event.get("event_id") == event_id]
    scoped["decided"] = [
        item for item in payload["decided"] if item.get("event", {}).get("event_id") == event_id
    ]
    archived = []
    for generation in payload["archived"]:
        decided = [
            item
            for item in generation.get("decided", [])
            if item.get("event", {}).get("event_id") == event_id
        ]
        if decided:
            archived.append({**generation, "decided": decided})
    scoped["archived"] = archived
    return scoped


def _result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": request_id, "jsonrpc": "2.0", "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}, "id": request_id, "jsonrpc": "2.0"}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _string(values: dict[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _optional_string(values: dict[str, Any], name: str) -> str | None:
    value = values.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _strings(values: dict[str, Any], name: str) -> tuple[str, ...]:
    value = values.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{name} must be a string list")
    return tuple(value)


def _optional_strings(values: dict[str, Any], name: str) -> tuple[str, ...] | None:
    if name not in values:
        return None
    return _strings(values, name)


def _boolean(values: dict[str, Any], name: str) -> bool:
    value = values.get(name, False)
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _integer(values: dict[str, Any], name: str, default: int) -> int:
    value = values.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    return value


def _optional_integer(values: dict[str, Any], name: str) -> int | None:
    if name not in values:
        return None
    value = values[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    return value
