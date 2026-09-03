"""Privacy-minimized, read-only access to a standalone wacli store."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from continuity_kernel.atomic import read_regular_file
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.sqlite_snapshot import (
    SQLiteFileIdentity as FileIdentity,
)
from continuity_kernel.sqlite_snapshot import (
    pinned_sqlite_snapshot,
)

DEFAULT_RUNTIME = Path("/opt/homebrew/bin/wacli")
DEFAULT_SERVICE_LABEL = "ai.seld.wacli-sync"
SERVICE_LABEL_ENV = "GSV_WHATSAPP_SERVICE_LABEL"
SOURCE_ID = "whatsapp"

__all__ = [
    "DEFAULT_RUNTIME",
    "DEFAULT_SERVICE_LABEL",
    "SERVICE_LABEL_ENV",
    "SOURCE_ID",
    "Runner",
    "WhatsAppAck",
    "WhatsAppDelta",
    "WhatsAppMessage",
    "WhatsAppStatus",
    "account_fingerprint",
    "create_whatsapp_ack_token",
    "default_store_root",
    "inspect_whatsapp",
    "read_whatsapp_delta",
    "resolve_service_label",
    "verify_whatsapp_ack_token",
    "verify_whatsapp_checkpoint",
]

MAX_DELTA = 100
MAX_BODY_CHARS = 4_000
MAX_DELTA_BODY_CHARS = 48_000
MAX_LABEL_CHARS = 240
MAX_HEARTBEAT_AGE_SECONDS = 86_400
MAX_CURSOR_CHARS = 8_192
FINGERPRINT_CHARS = 20
MAX_TIMESTAMP_CHARS = 40
MAX_ACCOUNT_ID_CHARS = 512
MAX_CLOCK_SKEW_SECONDS = 300
CURSOR_VERSION = 2
ACK_TOKEN_VERSION = 1
MAX_ACK_TOKEN_CHARS = 16_384
SHA256_HEX_CHARS = 64
PROJECTION_NONCE_CHARS = 32
_SERVICE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$")


class Runner(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class WhatsAppStatus:
    observed_at: str
    runtime_present: bool
    database_present: bool
    sync_process_running: bool | None
    sync_service_running: bool
    sync_heartbeat_at: str | None
    sync_heartbeat_age_seconds: float | None
    read_mode: str | None
    schema: str | None
    generation: str | None
    messages: int | None
    chats: int | None
    max_rowid: int | None
    newest_message_at: str | None
    account_fingerprint: str | None
    available: bool
    error: str | None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["cursor"] = self.cursor()
        return payload

    def cursor(self, *, rowid: int | None = None) -> str | None:
        """Return an aggregate-only cursor; never include provider identifiers."""

        if not self.available:
            return None
        if any(
            value is None
            for value in (self.schema, self.generation, self.messages, self.chats, self.max_rowid)
        ):
            return None
        assert self.schema is not None and self.generation is not None
        assert self.messages is not None and self.chats is not None and self.max_rowid is not None
        sequence = self.max_rowid if rowid is None else rowid
        return _encode_cursor(
            _Cursor(
                version=CURSOR_VERSION,
                schema=self.schema,
                generation=self.generation,
                messages=self.messages,
                chats=self.chats,
                rowid=sequence,
                newest=self.newest_message_at,
            )
        )


@dataclass(frozen=True)
class WhatsAppMessage:
    rowid: int
    at: str
    from_me: bool
    chat_name: str | None
    sender_name: str | None
    body: str | None
    body_truncated: bool
    media_type: str | None


@dataclass(frozen=True)
class WhatsAppDelta:
    observed_at: str
    covered_through: str
    complete: bool
    cursor: str
    messages: tuple[WhatsAppMessage, ...]
    account_fingerprint: str
    store_reconciled: bool = False
    drift: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("account_fingerprint")
        return {**payload, "messages": [asdict(message) for message in self.messages]}


@dataclass(frozen=True)
class WhatsAppAck:
    """Aggregate-only proof that one transient delta may advance the cursor."""

    previous_digest: str
    project_digest: str
    from_rowid: int
    cursor: str
    observed_at: str
    items_observed: int
    complete: bool
    projection_nonce: str
    projection_digest: str
    store_reconciled: bool


@dataclass(frozen=True)
class _StatusEvidence:
    runtime_present: bool
    database_present: bool
    service_running: bool
    heartbeat_at: datetime | None
    heartbeat_age: float | None
    heartbeat_error: str | None
    newest: str | None
    account_error: str | None
    query_error: str | None


@dataclass(frozen=True)
class _Cursor:
    version: int
    schema: str
    generation: str
    messages: int
    chats: int
    rowid: int
    newest: str | None


def default_store_root() -> Path:
    return Path.home() / ".wacli"


def account_fingerprint(linked_identity: str) -> str:
    """Return one stable digest for a wacli-linked account without retaining its JID."""

    if not isinstance(linked_identity, str):
        raise ValidationError("standalone WhatsApp account identity is invalid")
    value = linked_identity.strip().casefold()
    if not value or len(value) > MAX_ACCOUNT_ID_CHARS or "\x00" in value or "@" not in value:
        raise ValidationError("standalone WhatsApp account identity is invalid")
    local, domain = value.rsplit("@", 1)
    if not local or not domain:
        raise ValidationError("standalone WhatsApp account identity is invalid")
    device_separator = local.rfind(":")
    if device_separator > 0 and local[device_separator + 1 :].isdigit():
        local = local[:device_separator]
    canonical = f"{local}@{domain}"
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def resolve_service_label(explicit: str | None = None) -> str:
    value = (
        explicit
        if explicit is not None
        else os.environ.get(SERVICE_LABEL_ENV, DEFAULT_SERVICE_LABEL)
    )
    if not isinstance(value, str) or _SERVICE_LABEL.fullmatch(value) is None:
        raise ValidationError("standalone WhatsApp service label is invalid")
    return value


def inspect_whatsapp(
    *,
    store_root: Path | None = None,
    runtime: Path = DEFAULT_RUNTIME,
    service_label: str = DEFAULT_SERVICE_LABEL,
    observed_at: datetime | None = None,
    runner: Runner | None = None,
) -> WhatsAppStatus:
    """Inspect standalone sync and scalar corpus health without reading message content."""

    now = _aware_utc(observed_at)
    root = (store_root or default_store_root()).expanduser().resolve()
    database = root / "wacli.db"
    heartbeat = root / "HEARTBEAT"
    runtime_present = runtime.expanduser().is_file() and os.access(runtime.expanduser(), os.X_OK)
    account_digest, account_error = (
        _runtime_account_fingerprint(
            runtime=runtime,
            store_root=root,
            runner=runner,
        )
        if runtime_present
        else (None, None)
    )
    database_present = _is_regular_file(database)
    process_running = _process_running(runner=runner)
    service_running = _service_running(service_label, runtime=runtime, runner=runner)
    heartbeat_at, heartbeat_age, heartbeat_error = _heartbeat(heartbeat, now=now)
    read_mode: str | None = None
    schema: str | None = None
    generation: str | None = None
    messages: int | None = None
    chats: int | None = None
    max_rowid: int | None = None
    newest: str | None = None
    query_error: str | None = None

    if database_present:
        writer_possible = process_running is not False or service_running
        sidecar_present = any(
            database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm")
        )
        if not writer_possible and sidecar_present:
            read_mode = "deferred"
            query_error = "inactive SQLite sidecars prevent passive WhatsApp inspection"
        else:
            read_mode = "ro" if writer_possible else "immutable"
            try:
                schema, generation, messages, chats, max_rowid, newest = _aggregates(database)
            except ContinuityError as exc:
                query_error = str(exc)

    error = _status_error(
        _StatusEvidence(
            runtime_present=runtime_present,
            database_present=database_present,
            service_running=service_running,
            heartbeat_at=heartbeat_at,
            heartbeat_age=heartbeat_age,
            heartbeat_error=heartbeat_error,
            newest=newest,
            account_error=account_error,
            query_error=query_error,
        )
    )
    return WhatsAppStatus(
        observed_at=_iso(now),
        runtime_present=runtime_present,
        database_present=database_present,
        sync_process_running=process_running,
        sync_service_running=service_running,
        sync_heartbeat_at=_iso(heartbeat_at) if heartbeat_at else None,
        sync_heartbeat_age_seconds=heartbeat_age,
        read_mode=read_mode,
        schema=schema,
        generation=generation,
        messages=messages,
        chats=chats,
        max_rowid=max_rowid,
        newest_message_at=newest,
        account_fingerprint=account_digest,
        available=error is None,
        error=error,
    )


def verify_whatsapp_checkpoint(
    *,
    cursor: str,
    store_root: Path | None = None,
    runtime: Path = DEFAULT_RUNTIME,
    service_label: str = DEFAULT_SERVICE_LABEL,
    observed_at: datetime | None = None,
    runner: Runner | None = None,
) -> tuple[str, str]:
    """Verify an aggregate-only prior cursor against the exact live store prefix."""

    prior = _decode_cursor(cursor)
    status = inspect_whatsapp(
        store_root=store_root,
        runtime=runtime,
        service_label=service_label,
        observed_at=observed_at,
        runner=runner,
    )
    if not status.available:
        raise ContinuityError(status.error or "standalone WhatsApp source is unavailable")
    assert status.account_fingerprint is not None
    database = (store_root or default_store_root()).expanduser().resolve() / "wacli.db"
    candidate, _reconciled = _reconcile_cursor(prior, status, database)
    assert status.schema is not None and status.generation is not None
    prefix_messages, prefix_rowid, prefix_newest = _prefix_aggregates(
        database,
        through_rowid=candidate.rowid,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )
    newest_matches = (candidate.newest is None and prefix_newest is None) or (
        candidate.newest is not None
        and prefix_newest is not None
        and _parse_iso(candidate.newest) == _parse_iso(prefix_newest)
    )
    if (
        prefix_messages != candidate.messages
        or prefix_rowid != candidate.rowid
        or not newest_matches
    ):
        raise ContinuityError("standalone WhatsApp prior checkpoint prefix is not present exactly")
    verified = _encode_cursor(candidate)
    return verified, status.observed_at


def read_whatsapp_delta(
    *,
    cursor: str,
    store_root: Path | None = None,
    limit: int = MAX_DELTA,
    observed_at: datetime | None = None,
    runtime: Path = DEFAULT_RUNTIME,
    service_label: str = DEFAULT_SERVICE_LABEL,
    runner: Runner | None = None,
    reconcile_store_replacement: bool = False,
) -> WhatsAppDelta:
    """Read a bounded delta while structurally excluding JIDs, IDs, keys, and paths."""

    if not 1 <= limit <= MAX_DELTA:
        raise ValidationError(f"WhatsApp delta limit must be between 1 and {MAX_DELTA}")
    previous = _decode_cursor(cursor)
    status = inspect_whatsapp(
        store_root=store_root,
        runtime=runtime,
        service_label=service_label,
        observed_at=observed_at,
        runner=runner,
    )
    if not status.available:
        raise ContinuityError(status.error or "standalone WhatsApp source is unavailable")
    assert status.account_fingerprint is not None
    drift = _cursor_drift(previous, status)
    database = (store_root or default_store_root()).expanduser().resolve() / "wacli.db"
    store_reconciled = False
    if reconcile_store_replacement or _schema_migrated_in_place(previous, status, database):
        previous, store_reconciled = _reconcile_cursor(previous, status, database)
    else:
        _validate_cursor(previous, status)
    assert status.schema is not None and status.generation is not None
    assert status.max_rowid is not None
    if status.max_rowid == previous.rowid:
        next_cursor = _cursor_at_prefix(database, status, previous.rowid)
        return WhatsAppDelta(
            observed_at=status.observed_at,
            covered_through=_decode_cursor(next_cursor).newest or status.observed_at,
            complete=True,
            cursor=next_cursor,
            messages=(),
            account_fingerprint=status.account_fingerprint,
            store_reconciled=store_reconciled,
            drift=drift,
        )
    rows = _delta_rows(
        database,
        after_rowid=previous.rowid,
        limit=limit + 1,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )
    messages, last_rowid, budget_complete = _bounded_messages(
        rows[:limit], starting_rowid=previous.rowid
    )
    complete = len(rows) <= limit and budget_complete
    next_cursor = _cursor_at_prefix(database, status, last_rowid)
    return WhatsAppDelta(
        observed_at=status.observed_at,
        covered_through=_decode_cursor(next_cursor).newest or status.observed_at,
        complete=complete,
        cursor=next_cursor,
        messages=messages,
        account_fingerprint=status.account_fingerprint,
        store_reconciled=store_reconciled,
        drift=drift,
    )


def replay_whatsapp_delta(
    *,
    cursor: str,
    target_cursor: str,
    complete: bool,
    store_root: Path | None = None,
    limit: int = MAX_DELTA,
    observed_at: datetime | None = None,
    runtime: Path = DEFAULT_RUNTIME,
    service_label: str = DEFAULT_SERVICE_LABEL,
    runner: Runner | None = None,
) -> WhatsAppDelta:
    """Re-read one prepared historical prefix while leaving later rows for the next poll."""

    if not 1 <= limit <= MAX_DELTA:
        raise ValidationError(f"WhatsApp delta limit must be between 1 and {MAX_DELTA}")
    previous = _decode_cursor(cursor)
    target = _decode_cursor(target_cursor)
    status = inspect_whatsapp(
        store_root=store_root,
        runtime=runtime,
        service_label=service_label,
        observed_at=observed_at,
        runner=runner,
    )
    if not status.available:
        raise ContinuityError(status.error or "standalone WhatsApp source is unavailable")
    assert status.account_fingerprint is not None
    _validate_cursor(previous, status)
    assert status.schema is not None and status.generation is not None
    assert status.max_rowid is not None
    if (
        target.schema != status.schema
        or target.generation != status.generation
        or target.rowid < previous.rowid
        or target.rowid > status.max_rowid
    ):
        raise ContinuityError("standalone WhatsApp prepared delivery prefix is unavailable")

    database = (store_root or default_store_root()).expanduser().resolve() / "wacli.db"
    prefix_messages, prefix_rowid, prefix_newest = _prefix_aggregates(
        database,
        through_rowid=target.rowid,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )
    newest_matches = (target.newest is None and prefix_newest is None) or (
        target.newest is not None
        and prefix_newest is not None
        and _parse_iso(target.newest) == _parse_iso(prefix_newest)
    )
    if prefix_messages != target.messages or prefix_rowid != target.rowid or not newest_matches:
        raise ContinuityError("standalone WhatsApp prepared delivery prefix changed")

    rows = _delta_rows(
        database,
        after_rowid=previous.rowid,
        through_rowid=target.rowid,
        limit=limit + 1,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )
    messages, last_rowid, budget_complete = _bounded_messages(rows, starting_rowid=previous.rowid)
    if len(rows) > limit or last_rowid != target.rowid or not budget_complete:
        raise ContinuityError("standalone WhatsApp prepared delivery exceeds its original bound")
    return WhatsAppDelta(
        observed_at=status.observed_at,
        covered_through=target.newest or status.observed_at,
        complete=complete,
        cursor=target_cursor,
        messages=messages,
        account_fingerprint=status.account_fingerprint,
        drift=_cursor_drift(previous, status),
    )


def create_whatsapp_ack_token(
    *, project_root: Path, previous_cursor: str, delta: WhatsAppDelta
) -> str:
    """Create an opaque, content-free acknowledgement for one non-empty delta."""

    if not delta.messages:
        raise ValidationError("standalone WhatsApp delivery acknowledgement is unnecessary")
    _decode_cursor(previous_cursor)
    target = _decode_cursor(delta.cursor)
    previous = _decode_cursor(previous_cursor)
    if target.rowid <= previous.rowid:
        raise ValidationError("standalone WhatsApp delivery checkpoint did not advance")
    try:
        _parse_iso(delta.observed_at)
    except ContinuityError as exc:
        raise ValidationError("standalone WhatsApp delivery token is invalid") from exc
    payload: dict[str, object] = {
        "complete": delta.complete,
        "cursor": delta.cursor,
        "from_rowid": previous.rowid,
        "items_observed": len(delta.messages),
        "observed_at": delta.observed_at,
        "previous_digest": _cursor_digest(previous_cursor),
        "project_digest": _project_digest(project_root),
        "projection_nonce": (nonce := secrets.token_hex(16)),
        "projection_digest": _projection_digest(delta.messages, nonce=nonce),
        "store_reconciled": delta.store_reconciled,
        "version": ACK_TOKEN_VERSION,
    }
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    envelope = {
        "digest": hashlib.sha256(("seld-whatsapp-ack-v1\0" + canonical).encode()).hexdigest(),
        "payload": payload,
    }
    raw = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_whatsapp_ack_token(
    *,
    token: str,
    project_root: Path,
    current_cursor: str,
    store_root: Path | None = None,
    runtime: Path = DEFAULT_RUNTIME,
    service_label: str = DEFAULT_SERVICE_LABEL,
    observed_at: datetime | None = None,
    runner: Runner | None = None,
) -> tuple[WhatsAppAck, bool]:
    """Verify a prepared delivery against current state and its aggregate store prefix.

    The boolean is true when the durable cursor already covers the prepared
    checkpoint, making a repeated acknowledgement a no-op.
    """

    acknowledgement = _decode_ack_token(token)
    if acknowledgement.project_digest != _project_digest(project_root):
        raise ValidationError("standalone WhatsApp delivery token belongs to another state root")
    current = _decode_cursor(current_cursor)
    target = _decode_cursor(acknowledgement.cursor)
    if _cursor_covers(current, target):
        return acknowledgement, True
    if _cursor_digest(current_cursor) != acknowledgement.previous_digest:
        raise ContinuityError(
            "standalone WhatsApp delivery checkpoint changed before acknowledgement"
        )
    if acknowledgement.from_rowid != current.rowid or target.rowid <= current.rowid:
        raise ContinuityError("standalone WhatsApp delivery checkpoint is stale")
    same_store = target.schema == current.schema and target.generation == current.generation
    legacy_rebase = (
        acknowledgement.store_reconciled
        and current.version == 1
        and target.version == CURSOR_VERSION
        and target.schema == current.schema
    )
    if not same_store and not legacy_rebase:
        raise ContinuityError("standalone WhatsApp delivery store changed before acknowledgement")

    status = inspect_whatsapp(
        store_root=store_root,
        runtime=runtime,
        service_label=service_label,
        observed_at=observed_at,
        runner=runner,
    )
    if not status.available:
        raise ContinuityError(status.error or "standalone WhatsApp source is unavailable")
    if target.schema != status.schema or target.generation != status.generation:
        raise ContinuityError("standalone WhatsApp delivery store changed before acknowledgement")
    database = (store_root or default_store_root()).expanduser().resolve() / "wacli.db"
    if legacy_rebase:
        prior_messages, prior_rowid, prior_newest = _prefix_aggregates(
            database,
            through_rowid=current.rowid,
            expected_schema=target.schema,
            expected_generation=target.generation,
        )
        prior_newest_matches = (current.newest is None and prior_newest is None) or (
            current.newest is not None
            and prior_newest is not None
            and _parse_iso(current.newest) == _parse_iso(prior_newest)
        )
        if (
            prior_messages != current.messages
            or prior_rowid != current.rowid
            or not prior_newest_matches
        ):
            raise ContinuityError(
                "standalone WhatsApp delivery prefix changed before acknowledgement"
            )
    prefix_messages, prefix_rowid, prefix_newest = _prefix_aggregates(
        database,
        through_rowid=target.rowid,
        expected_schema=target.schema,
        expected_generation=target.generation,
    )
    newest_matches = (target.newest is None and prefix_newest is None) or (
        target.newest is not None
        and prefix_newest is not None
        and _parse_iso(target.newest) == _parse_iso(prefix_newest)
    )
    if prefix_messages != target.messages or prefix_rowid != target.rowid or not newest_matches:
        raise ContinuityError("standalone WhatsApp delivery prefix changed before acknowledgement")
    rows = _delta_rows(
        database,
        after_rowid=acknowledgement.from_rowid,
        through_rowid=target.rowid,
        limit=MAX_DELTA + 1,
        expected_schema=target.schema,
        expected_generation=target.generation,
    )
    delivered, last_rowid, budget_complete = _bounded_messages(
        rows, starting_rowid=acknowledgement.from_rowid
    )
    if (
        last_rowid != target.rowid
        or not budget_complete
        or len(delivered) != acknowledgement.items_observed
        or _projection_digest(delivered, nonce=acknowledgement.projection_nonce)
        != acknowledgement.projection_digest
    ):
        raise ContinuityError(
            "standalone WhatsApp delivered content changed before acknowledgement"
        )
    return acknowledgement, False


def _aggregates(database: Path) -> tuple[str, str, int, int, int, str | None]:
    with _connect(database) as (connection, before):
        try:
            connection.execute("BEGIN")
            message_columns = _columns(connection, "messages")
            chat_columns = _columns(connection, "chats")
            required = {"rowid", "ts", "from_me", "text", "display_text"}
            if not required <= message_columns.keys() or not chat_columns:
                raise ContinuityError("standalone WhatsApp schema is missing required metadata")
            schema = _schema_fingerprint(message_columns, chat_columns)
            message_row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(rowid), 0), MAX(ts) FROM messages"
            ).fetchone()
            chat_row = connection.execute("SELECT COUNT(*) FROM chats").fetchone()
        except sqlite3.Error as exc:
            raise ContinuityError("standalone WhatsApp aggregate query failed") from exc
    if message_row is None or chat_row is None:
        raise ContinuityError("standalone WhatsApp aggregate query returned no result")
    messages = int(message_row[0])
    max_rowid = int(message_row[1])
    chats = int(chat_row[0])
    if min(messages, max_rowid, chats) < 0:
        raise ContinuityError("standalone WhatsApp aggregate metadata is invalid")
    newest = _epoch_iso(message_row[2])
    generation = _generation(before, schema)
    return schema, generation, messages, chats, max_rowid, newest


def _delta_rows(
    database: Path,
    *,
    after_rowid: int,
    limit: int,
    expected_schema: str,
    expected_generation: str,
    through_rowid: int | None = None,
) -> list[sqlite3.Row]:
    with _connect(database) as (connection, before):
        try:
            connection.execute("BEGIN")
            columns = _columns(connection, "messages")
            chat_columns = _columns(connection, "chats")
            required = {"rowid", "ts", "from_me", "text", "display_text"}
            if not required <= columns.keys():
                raise ContinuityError("standalone WhatsApp schema is missing required delta fields")
            schema = _schema_fingerprint(columns, chat_columns)
            generation = _generation(before, schema)
            if schema != expected_schema or generation != expected_generation:
                raise ContinuityError("standalone WhatsApp store changed; cursor preserved")
            visible_filters = [
                f"COALESCE({name}, 0) = 0"
                for name in ("revoked", "deleted_for_me")
                if name in columns
            ]
            visible = " AND ".join(visible_filters) if visible_filters else "1"
            content = {
                name: (
                    f"CASE WHEN {visible} THEN substr({name}, 1, {MAX_BODY_CHARS + 1}) "
                    f"ELSE NULL END AS {name}"
                )
                for name in ("text", "display_text")
            }
            optionals = {
                name: (
                    f"CASE WHEN {visible} THEN substr({name}, 1, "
                    f"{(MAX_BODY_CHARS if name == 'media_caption' else MAX_LABEL_CHARS) + 1}) "
                    f"ELSE NULL END AS {name}"
                    if name in columns
                    else f"NULL AS {name}"
                )
                for name in ("chat_name", "sender_name", "media_caption", "media_type")
            }
            upper_bound = " AND rowid <= ?" if through_rowid is not None else ""
            query = (
                "SELECT rowid, ts, from_me, "
                + ", ".join((*content.values(), *optionals.values()))
                + f", CASE WHEN {visible} THEN 1 ELSE 0 END AS visible"
                + " FROM messages WHERE rowid > ?"
                + upper_bound
                + " ORDER BY rowid ASC LIMIT ?"
            )
            parameters = (
                (after_rowid, through_rowid, limit)
                if through_rowid is not None
                else (after_rowid, limit)
            )
            rows = connection.execute(query, parameters).fetchall()
        except sqlite3.Error as exc:
            raise ContinuityError("standalone WhatsApp delta query failed") from exc
    return rows


@contextmanager
def _connect(database: Path) -> Iterator[tuple[sqlite3.Connection, FileIdentity]]:
    try:
        with pinned_sqlite_snapshot(database, label="standalone WhatsApp store") as (
            snapshot,
            identity,
            snapshot_immutable,
        ):
            suffix = "?mode=ro&immutable=1" if snapshot_immutable else "?mode=ro"
            uri = f"{snapshot.as_uri()}{suffix}"
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 2000")
                yield connection, identity
            finally:
                connection.close()
    except ValidationError as exc:
        raise ContinuityError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise ContinuityError("standalone WhatsApp store is unreadable") from exc


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    if table not in {"messages", "chats"}:
        raise ContinuityError("standalone WhatsApp schema request is invalid")
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]): str(row[2]).upper() for row in rows}


def _schema_fingerprint(messages: dict[str, str], chats: dict[str, str]) -> str:
    encoded = json.dumps(
        {"chats": sorted(chats.items()), "messages": sorted(messages.items())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def _generation(identity: FileIdentity, schema: str) -> str:
    """Fingerprint one store file without depending on APFS mount numbering."""

    _device, inode, _size, _modified_ns, birthtime = identity
    return hashlib.sha256(f"v2:{inode}:{birthtime}:{schema}".encode()).hexdigest()[:20]


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _process_running(*, runner: Runner | None) -> bool | None:
    run = runner or subprocess.run
    try:
        result = run(
            ["/usr/bin/pgrep", "-x", "wacli"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    return False if result.returncode == 1 else None


def _service_running(label: str, *, runtime: Path, runner: Runner | None) -> bool:
    label = resolve_service_label(label)
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        return False
    run = runner or subprocess.run
    try:
        result = run(
            ["/bin/launchctl", "print", f"gui/{getuid()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    lines = {line.strip() for line in result.stdout.splitlines()}
    return {
        "state = running",
        f"program = {runtime.expanduser()}",
        "sync",
        "--follow",
    } <= lines


def _runtime_account_fingerprint(
    *,
    runtime: Path,
    store_root: Path,
    runner: Runner | None,
) -> tuple[str | None, str | None]:
    run = runner or subprocess.run
    try:
        result = run(
            [
                runtime.expanduser(),
                "--read-only",
                "--json",
                "--store",
                str(store_root),
                "doctor",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "standalone WhatsApp account identity is unavailable"
    if result.returncode != 0:
        return None, "standalone WhatsApp account identity is unavailable"
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None, "standalone WhatsApp account identity is unavailable"
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None, "standalone WhatsApp account identity is unavailable"
    data = payload.get("data") if isinstance(payload, dict) else None
    linked_identity = data.get("linked_jid") if isinstance(data, dict) else None
    if not isinstance(linked_identity, str):
        return None, "standalone WhatsApp account identity is unavailable"
    try:
        return account_fingerprint(linked_identity), None
    except ValidationError:
        return None, "standalone WhatsApp account identity is unavailable"


def _heartbeat(path: Path, *, now: datetime) -> tuple[datetime | None, float | None, str | None]:
    if not os.path.lexists(path):
        return None, None, "standalone sync heartbeat is absent"
    try:
        encoded = read_regular_file(
            path,
            label="standalone sync heartbeat",
            max_bytes=256,
        )
        value = encoded.decode("utf-8").strip()
    except (UnicodeError, ValidationError):
        return None, None, "standalone sync heartbeat is unreadable"
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, None, "standalone sync heartbeat is invalid"
    if observed.tzinfo is None or observed.utcoffset() is None:
        return None, None, "standalone sync heartbeat is invalid"
    utc = observed.astimezone(UTC)
    if (utc - now).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
        return None, None, "standalone sync heartbeat is invalid"
    age = max(0.0, (now - utc).total_seconds())
    return utc, age, None


def _status_error(evidence: _StatusEvidence) -> str | None:
    if not evidence.runtime_present:
        return "standalone wacli runtime is unavailable"
    if not evidence.database_present:
        return "standalone WhatsApp store is unavailable"
    if evidence.query_error:
        return evidence.query_error
    if evidence.account_error:
        return evidence.account_error
    if not evidence.service_running:
        suffix = f"; corpus newest message is {evidence.newest}" if evidence.newest else ""
        return "standalone sync service is not running" + suffix
    if evidence.heartbeat_at is None:
        return evidence.heartbeat_error or "standalone sync heartbeat is unavailable"
    if evidence.heartbeat_age is None or evidence.heartbeat_age > MAX_HEARTBEAT_AGE_SECONDS:
        return "standalone sync heartbeat is stale"
    return None


def _decode_cursor(cursor: str) -> _Cursor:
    if len(cursor) > MAX_CURSOR_CHARS:
        raise ValidationError("standalone WhatsApp cursor is invalid")
    try:
        payload = json.loads(cursor)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("standalone WhatsApp cursor is invalid") from exc
    required = {"version", "schema", "generation", "messages", "chats", "rowid", "newest"}
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or type(payload.get("version")) is not int
        or payload.get("version") not in {1, CURSOR_VERSION}
    ):
        raise ValidationError("standalone WhatsApp cursor is invalid")
    for name in ("messages", "chats", "rowid"):
        if type(payload[name]) is not int or payload[name] < 0:
            raise ValidationError("standalone WhatsApp cursor is invalid")
    for name in ("schema", "generation"):
        value = payload[name]
        if (
            not isinstance(value, str)
            or len(value) != FINGERPRINT_CHARS
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValidationError("standalone WhatsApp cursor is invalid")
    if payload["newest"] is not None and (
        not isinstance(payload["newest"], str) or len(payload["newest"]) > MAX_TIMESTAMP_CHARS
    ):
        raise ValidationError("standalone WhatsApp cursor is invalid")
    return _Cursor(
        version=payload["version"],
        schema=payload["schema"],
        generation=payload["generation"],
        messages=payload["messages"],
        chats=payload["chats"],
        rowid=payload["rowid"],
        newest=payload["newest"],
    )


def _encode_cursor(cursor: _Cursor) -> str:
    return json.dumps(
        {
            "chats": cursor.chats,
            "generation": cursor.generation,
            "messages": cursor.messages,
            "newest": cursor.newest,
            "rowid": cursor.rowid,
            "schema": cursor.schema,
            "version": cursor.version,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_ack_token(token: str) -> WhatsAppAck:
    if not isinstance(token, str) or not token or len(token) > MAX_ACK_TOKEN_CHARS:
        raise ValidationError("standalone WhatsApp delivery token is invalid")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        envelope = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("standalone WhatsApp delivery token is invalid") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"digest", "payload"}:
        raise ValidationError("standalone WhatsApp delivery token is invalid")
    payload = envelope.get("payload")
    expected = {
        "complete",
        "cursor",
        "from_rowid",
        "items_observed",
        "observed_at",
        "previous_digest",
        "project_digest",
        "projection_digest",
        "projection_nonce",
        "store_reconciled",
        "version",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValidationError("standalone WhatsApp delivery token is invalid")
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = envelope.get("digest")
    actual = hashlib.sha256(("seld-whatsapp-ack-v1\0" + canonical).encode()).hexdigest()
    if not isinstance(digest, str) or digest != actual:
        raise ValidationError("standalone WhatsApp delivery token is invalid")
    if payload.get("version") != ACK_TOKEN_VERSION:
        raise ValidationError("standalone WhatsApp delivery token is invalid")
    cursor = payload.get("cursor")
    observed_at = payload.get("observed_at")
    previous_digest = payload.get("previous_digest")
    project_digest = payload.get("project_digest")
    from_rowid = payload.get("from_rowid")
    items_observed = payload.get("items_observed")
    complete = payload.get("complete")
    projection_nonce = payload.get("projection_nonce")
    projection_digest = payload.get("projection_digest")
    store_reconciled = payload.get("store_reconciled")
    if (
        not isinstance(cursor, str)
        or not isinstance(observed_at, str)
        or not isinstance(previous_digest, str)
        or len(previous_digest) != SHA256_HEX_CHARS
        or any(character not in "0123456789abcdef" for character in previous_digest)
        or not isinstance(project_digest, str)
        or len(project_digest) != SHA256_HEX_CHARS
        or any(character not in "0123456789abcdef" for character in project_digest)
        or type(from_rowid) is not int
        or from_rowid < 0
        or type(items_observed) is not int
        or not 1 <= items_observed <= MAX_DELTA
        or type(complete) is not bool
        or not isinstance(projection_nonce, str)
        or len(projection_nonce) != PROJECTION_NONCE_CHARS
        or any(character not in "0123456789abcdef" for character in projection_nonce)
        or not isinstance(projection_digest, str)
        or len(projection_digest) != SHA256_HEX_CHARS
        or any(character not in "0123456789abcdef" for character in projection_digest)
        or type(store_reconciled) is not bool
    ):
        raise ValidationError("standalone WhatsApp delivery token is invalid")
    try:
        _decode_cursor(cursor)
        _parse_iso(observed_at)
    except ContinuityError as exc:
        raise ValidationError("standalone WhatsApp delivery token is invalid") from exc
    return WhatsAppAck(
        previous_digest=previous_digest,
        project_digest=project_digest,
        from_rowid=from_rowid,
        cursor=cursor,
        observed_at=observed_at,
        items_observed=items_observed,
        complete=complete,
        projection_nonce=projection_nonce,
        projection_digest=projection_digest,
        store_reconciled=store_reconciled,
    )


def _cursor_digest(cursor: str) -> str:
    return hashlib.sha256(cursor.encode()).hexdigest()


def _project_digest(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.expanduser().resolve()).encode()).hexdigest()


def _projection_digest(messages: tuple[WhatsAppMessage, ...], *, nonce: str) -> str:
    projection = json.dumps(
        [asdict(message) for message in messages],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(
        (f"seld-whatsapp-projection-v1\0{nonce}\0" + projection).encode()
    ).hexdigest()


def _cursor_covers(current: _Cursor, target: _Cursor) -> bool:
    return (
        current.version == CURSOR_VERSION
        and current.schema == target.schema
        and current.generation == target.generation
        and current.rowid >= target.rowid
    )


def _validate_cursor(cursor: _Cursor, status: WhatsAppStatus) -> None:
    _validate_cursor_aggregates(cursor, status)
    if cursor.schema != status.schema:
        raise ContinuityError("standalone WhatsApp schema changed; cursor preserved")
    if cursor.generation != status.generation:
        raise ContinuityError("standalone WhatsApp store changed; cursor preserved")


def _validate_cursor_aggregates(cursor: _Cursor, status: WhatsAppStatus) -> None:
    if cursor.schema != status.schema:
        raise ContinuityError("standalone WhatsApp schema changed; cursor preserved")
    _validate_row_high_water(cursor, status)


def _validate_row_high_water(cursor: _Cursor, status: WhatsAppStatus) -> None:
    assert status.messages is not None and status.chats is not None and status.max_rowid is not None
    # Counts and timestamps describe mutable inventory, not delivery order.
    # Deletion, expiry, revocation, or correction may legitimately move them
    # backward. The append-only SQLite row high-water remains the fail-closed
    # continuity boundary; inventory drift is returned to Sol as evidence.
    if status.max_rowid < cursor.rowid:
        raise ContinuityError("standalone WhatsApp row cursor regressed; cursor preserved")


def _cursor_drift(cursor: _Cursor, status: WhatsAppStatus) -> tuple[str, ...]:
    assert status.messages is not None and status.chats is not None
    drift: list[str] = []
    if status.messages < cursor.messages:
        drift.append("message inventory decreased")
    if status.chats < cursor.chats:
        drift.append("chat inventory decreased")
    if cursor.newest is not None and (
        status.newest_message_at is None
        or _parse_iso(status.newest_message_at) < _parse_iso(cursor.newest)
    ):
        drift.append("newest message time moved backward")
    return tuple(drift)


def _same_store_under_prior_schema(cursor: _Cursor, database: Path) -> bool:
    """Whether the live file is the one the cursor was taken from, schema aside.

    A generation binds the file identity and the schema fingerprint together, so a
    schema that moved makes the generation move with it. Recomputing the generation
    from the live file under the cursor's own schema separates the two: equal means
    the same inode and birth time, so the store was migrated in place by a newer
    wacli; different means the file itself was replaced.
    """

    with _connect(database) as (_connection, identity):
        return _generation(identity, cursor.schema) == cursor.generation


def _schema_migrated_in_place(cursor: _Cursor, status: WhatsAppStatus, database: Path) -> bool:
    """A current cursor whose schema moved while its store file did not."""

    return (
        cursor.version == CURSOR_VERSION
        and status.schema is not None
        and cursor.schema != status.schema
        and _same_store_under_prior_schema(cursor, database)
    )


def _reconcile_cursor(
    cursor: _Cursor, status: WhatsAppStatus, database: Path
) -> tuple[_Cursor, bool]:
    """Rebase a cursor only when the old aggregate prefix still exists exactly.

    Two cursors qualify: a legacy version-1 cursor, and a current cursor whose schema
    fingerprint moved while the store file stayed the same, which is what an in-place
    column migration by a newer wacli leaves behind (29 August 2026: three columns
    were added to ``messages`` and every read refused as "schema changed" until this
    path existed). A current cursor whose file identity moved is a replaced store and
    is refused here exactly as before.
    """

    if cursor.version == CURSOR_VERSION:
        if cursor.schema == status.schema:
            _validate_cursor_aggregates(cursor, status)
            if cursor.generation != status.generation:
                raise ContinuityError("standalone WhatsApp store changed; cursor preserved")
            return cursor, False
        if not _same_store_under_prior_schema(cursor, database):
            raise ContinuityError("standalone WhatsApp store changed; cursor preserved")
        _validate_row_high_water(cursor, status)
    else:
        _validate_cursor_aggregates(cursor, status)
    assert status.schema is not None and status.generation is not None
    prefix_messages, prefix_rowid, prefix_newest = _prefix_aggregates(
        database,
        through_rowid=cursor.rowid,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )
    newest_matches = (cursor.newest is None and prefix_newest is None) or (
        cursor.newest is not None
        and prefix_newest is not None
        and _parse_iso(cursor.newest) == _parse_iso(prefix_newest)
    )
    if prefix_messages != cursor.messages or prefix_rowid != cursor.rowid or not newest_matches:
        raise ContinuityError(
            "standalone WhatsApp store continuity could not be verified; cursor preserved"
        )
    return (
        _Cursor(
            version=CURSOR_VERSION,
            schema=status.schema,
            generation=status.generation,
            messages=cursor.messages,
            chats=cursor.chats,
            rowid=cursor.rowid,
            newest=cursor.newest,
        ),
        True,
    )


def _prefix_aggregates(
    database: Path,
    *,
    through_rowid: int,
    expected_schema: str,
    expected_generation: str,
) -> tuple[int, int, str | None]:
    """Read only count, row position, and time for a historical prefix."""

    with _connect(database) as (connection, before):
        try:
            connection.execute("BEGIN")
            message_columns = _columns(connection, "messages")
            chat_columns = _columns(connection, "chats")
            schema = _schema_fingerprint(message_columns, chat_columns)
            generation = _generation(before, schema)
            if schema != expected_schema or generation != expected_generation:
                raise ContinuityError("standalone WhatsApp store changed; cursor preserved")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(rowid), 0), MAX(ts) FROM messages WHERE rowid <= ?",
                (through_rowid,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ContinuityError("standalone WhatsApp continuity query failed") from exc
    if row is None:
        raise ContinuityError("standalone WhatsApp continuity query returned no result")
    return int(row[0]), int(row[1]), _epoch_iso(row[2])


def _cursor_at_prefix(database: Path, status: WhatsAppStatus, last_rowid: int) -> str:
    assert status.schema is not None and status.generation is not None and status.chats is not None
    messages, prefix_rowid, newest = _prefix_aggregates(
        database,
        through_rowid=last_rowid,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )
    if prefix_rowid != last_rowid:
        raise ContinuityError("standalone WhatsApp store changed; cursor preserved")
    return _encode_cursor(
        _Cursor(
            version=CURSOR_VERSION,
            schema=status.schema,
            generation=status.generation,
            messages=messages,
            chats=status.chats,
            rowid=last_rowid,
            newest=newest,
        )
    )


def _bounded_messages(
    rows: list[sqlite3.Row], *, starting_rowid: int
) -> tuple[tuple[WhatsAppMessage, ...], int, bool]:
    messages: list[WhatsAppMessage] = []
    last_rowid = starting_rowid
    remaining = MAX_DELTA_BODY_CHARS
    for row in rows:
        if not bool(row["visible"]):
            last_rowid = int(row["rowid"])
            continue
        raw_body = _body(row)
        if raw_body and remaining <= 0:
            return tuple(messages), last_rowid, False
        allowance = min(MAX_BODY_CHARS, remaining)
        message = _message(row, body=raw_body, body_allowance=allowance)
        messages.append(message)
        remaining -= len(message.body or "")
        last_rowid = message.rowid
    return tuple(messages), last_rowid, True


def _body(row: sqlite3.Row) -> str | None:
    return next(
        (
            str(row[name])
            for name in ("display_text", "text", "media_caption")
            if row[name] is not None and str(row[name]).strip()
        ),
        None,
    )


def _message(row: sqlite3.Row, *, body: str | None, body_allowance: int) -> WhatsAppMessage:
    truncated = body is not None and len(body) > body_allowance
    bounded_body = body[:body_allowance] if body is not None else None
    return WhatsAppMessage(
        rowid=int(row["rowid"]),
        at=_epoch_iso(row["ts"]) or "1970-01-01T00:00:00Z",
        from_me=bool(row["from_me"]),
        chat_name=_optional_text(row["chat_name"]),
        sender_name=_optional_text(row["sender_name"]),
        body=bounded_body,
        body_truncated=truncated,
        media_type=_optional_text(row["media_type"]),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[: MAX_LABEL_CHARS - 1] + "…" if len(text) > MAX_LABEL_CHARS else text or None


def _epoch_iso(value: object) -> str | None:
    if value is None:
        return None
    try:
        return _iso(datetime.fromtimestamp(int(str(value)), tz=UTC))
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ContinuityError("standalone WhatsApp timestamp is invalid") from exc


def _aware_utc(value: datetime | None) -> datetime:
    observed = value or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValidationError("WhatsApp observation time must be timezone-aware")
    return observed.astimezone(UTC)


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("standalone WhatsApp cursor is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("standalone WhatsApp cursor is invalid")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
