"""Privacy-minimized, read-only access to the local Messages database."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.sqlite_snapshot import (
    SQLiteFileIdentity as FileIdentity,
)
from continuity_kernel.sqlite_snapshot import (
    pinned_sqlite_snapshot,
)

SOURCE_ID = "apple_messages"

__all__ = [
    "SOURCE_ID",
    "AppleMessage",
    "AppleMessagesAck",
    "AppleMessagesDelta",
    "AppleMessagesStatus",
    "create_apple_messages_ack_token",
    "default_store_root",
    "inspect_apple_messages",
    "read_apple_messages_delta",
    "verify_apple_messages_ack_token",
    "verify_apple_messages_checkpoint",
]

MAX_DELTA = 100
MAX_BODY_CHARS = 4_000
MAX_DELTA_BODY_CHARS = 48_000
MAX_ATTRIBUTED_BODY_BYTES = MAX_BODY_CHARS * 4 + 512
MAX_CURSOR_CHARS = 8_192
FINGERPRINT_CHARS = 20
MAX_TIMESTAMP_CHARS = 40
MAX_CLOCK_SKEW_SECONDS = 300
CURSOR_VERSION = 2
ACK_TOKEN_VERSION = 1
MAX_ACK_TOKEN_CHARS = 16_384
SHA256_HEX_CHARS = 64
PROJECTION_NONCE_CHARS = 32
MIN_ATTRIBUTED_CHUNK_BYTES = 2
APPLE_NANOSECOND_THRESHOLD = 10_000_000_000


@dataclass(frozen=True)
class AppleMessagesStatus:
    observed_at: str
    database_present: bool
    read_mode: str | None
    schema: str | None
    generation: str | None
    messages: int | None
    chats: int | None
    max_rowid: int | None
    newest_message_at: str | None
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
            value is None for value in (self.schema, self.generation, self.messages, self.max_rowid)
        ):
            return None
        assert self.schema is not None and self.generation is not None
        assert self.messages is not None and self.max_rowid is not None
        sequence = self.max_rowid if rowid is None else rowid
        return _encode_cursor(
            _Cursor(
                version=CURSOR_VERSION,
                schema=self.schema,
                generation=self.generation,
                messages=self.messages,
                chats=0,
                rowid=sequence,
                newest=self.newest_message_at,
            )
        )


@dataclass(frozen=True)
class AppleMessage:
    rowid: int
    at: str
    from_me: bool
    body: str | None
    body_truncated: bool
    body_unavailable: bool
    has_attachment: bool


@dataclass(frozen=True)
class AppleMessagesDelta:
    observed_at: str
    covered_through: str
    complete: bool
    cursor: str
    messages: tuple[AppleMessage, ...]
    store_reconciled: bool = False
    drift: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "messages": [asdict(message) for message in self.messages]}


@dataclass(frozen=True)
class AppleMessagesAck:
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
class _Cursor:
    version: int
    schema: str
    generation: str
    messages: int
    chats: int
    rowid: int
    newest: str | None


def default_store_root() -> Path:
    return Path.home() / "Library/Messages"


def inspect_apple_messages(
    *,
    store_root: Path | None = None,
    observed_at: datetime | None = None,
) -> AppleMessagesStatus:
    """Inspect scalar store health without selecting message content."""

    now = _aware_utc(observed_at)
    root = (store_root or default_store_root()).expanduser().resolve()
    database = root / "chat.db"
    database_present = _is_regular_file(database)
    read_mode: str | None = None
    schema: str | None = None
    generation: str | None = None
    messages: int | None = None
    max_rowid: int | None = None
    newest: str | None = None
    query_error: str | None = None

    if database_present:
        read_mode = "ro"
        try:
            schema, generation, messages, _chats, max_rowid, newest = _aggregates(database)
        except ContinuityError as exc:
            query_error = str(exc)
    error = "Apple Messages store is unavailable" if not database_present else query_error
    return AppleMessagesStatus(
        observed_at=_iso(now),
        database_present=database_present,
        read_mode=read_mode,
        schema=schema,
        generation=generation,
        messages=messages,
        chats=0 if messages is not None else None,
        max_rowid=max_rowid,
        newest_message_at=newest,
        available=error is None,
        error=error,
    )


def verify_apple_messages_checkpoint(
    *,
    cursor: str,
    store_root: Path | None = None,
    observed_at: datetime | None = None,
) -> tuple[str, str]:
    """Verify an aggregate-only prior cursor against the exact live store prefix."""

    prior = _decode_cursor(cursor)
    status = inspect_apple_messages(store_root=store_root, observed_at=observed_at)
    if not status.available:
        raise ContinuityError(status.error or "Apple Messages source is unavailable")
    database = (store_root or default_store_root()).expanduser().resolve() / "chat.db"
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
        raise ContinuityError("Apple Messages prior checkpoint prefix is not present exactly")
    verified = _encode_cursor(candidate)
    return verified, status.observed_at


def read_apple_messages_delta(
    *,
    cursor: str,
    store_root: Path | None = None,
    limit: int = MAX_DELTA,
    observed_at: datetime | None = None,
    reconcile_store_replacement: bool = False,
) -> AppleMessagesDelta:
    """Read a bounded delta while structurally excluding JIDs, IDs, keys, and paths."""

    if not 1 <= limit <= MAX_DELTA:
        raise ValidationError(f"Apple Messages delta limit must be between 1 and {MAX_DELTA}")
    previous = _decode_cursor(cursor)
    status = inspect_apple_messages(
        store_root=store_root,
        observed_at=observed_at,
    )
    if not status.available:
        raise ContinuityError(status.error or "Apple Messages source is unavailable")
    drift = _cursor_drift(previous, status)
    database = (store_root or default_store_root()).expanduser().resolve() / "chat.db"
    store_reconciled = False
    if reconcile_store_replacement:
        previous, store_reconciled = _reconcile_cursor(previous, status, database)
    else:
        _validate_cursor(previous, status)
    assert status.schema is not None and status.generation is not None
    assert status.max_rowid is not None
    if status.max_rowid == previous.rowid:
        next_cursor = _cursor_at_prefix(database, status, previous.rowid)
        return AppleMessagesDelta(
            observed_at=status.observed_at,
            covered_through=_decode_cursor(next_cursor).newest or status.observed_at,
            complete=True,
            cursor=next_cursor,
            messages=(),
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
    return AppleMessagesDelta(
        observed_at=status.observed_at,
        covered_through=_decode_cursor(next_cursor).newest or status.observed_at,
        complete=complete,
        cursor=next_cursor,
        messages=messages,
        store_reconciled=store_reconciled,
        drift=drift,
    )


def replay_apple_messages_delta(
    *,
    cursor: str,
    target_cursor: str,
    complete: bool,
    store_root: Path | None = None,
    limit: int = MAX_DELTA,
    observed_at: datetime | None = None,
) -> AppleMessagesDelta:
    """Re-read one prepared historical prefix while leaving later rows for the next poll."""

    if not 1 <= limit <= MAX_DELTA:
        raise ValidationError(f"Apple Messages delta limit must be between 1 and {MAX_DELTA}")
    previous = _decode_cursor(cursor)
    target = _decode_cursor(target_cursor)
    status = inspect_apple_messages(store_root=store_root, observed_at=observed_at)
    if not status.available:
        raise ContinuityError(status.error or "Apple Messages source is unavailable")
    _validate_cursor(previous, status)
    assert status.schema is not None and status.generation is not None
    assert status.max_rowid is not None
    if (
        target.schema != status.schema
        or target.generation != status.generation
        or target.rowid < previous.rowid
        or target.rowid > status.max_rowid
    ):
        raise ContinuityError("Apple Messages prepared delivery prefix is unavailable")

    database = (store_root or default_store_root()).expanduser().resolve() / "chat.db"
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
        raise ContinuityError("Apple Messages prepared delivery prefix changed")

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
        raise ContinuityError("Apple Messages prepared delivery exceeds its original bound")
    return AppleMessagesDelta(
        observed_at=status.observed_at,
        covered_through=target.newest or status.observed_at,
        complete=complete,
        cursor=target_cursor,
        messages=messages,
        drift=_cursor_drift(previous, status),
    )


def create_apple_messages_ack_token(
    *, project_root: Path, previous_cursor: str, delta: AppleMessagesDelta
) -> str:
    """Create an opaque, content-free acknowledgement for one non-empty delta."""

    if not delta.messages:
        raise ValidationError("Apple Messages delivery acknowledgement is unnecessary")
    _decode_cursor(previous_cursor)
    target = _decode_cursor(delta.cursor)
    previous = _decode_cursor(previous_cursor)
    if target.rowid <= previous.rowid:
        raise ValidationError("Apple Messages delivery checkpoint did not advance")
    try:
        _parse_iso(delta.observed_at)
    except ContinuityError as exc:
        raise ValidationError("Apple Messages delivery token is invalid") from exc
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
        "digest": hashlib.sha256(("seld-apple-messages-ack-v1\0" + canonical).encode()).hexdigest(),
        "payload": payload,
    }
    raw = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_apple_messages_ack_token(
    *,
    token: str,
    project_root: Path,
    current_cursor: str,
    store_root: Path | None = None,
    observed_at: datetime | None = None,
) -> tuple[AppleMessagesAck, bool]:
    """Verify a prepared delivery against current state and its aggregate store prefix.

    The boolean is true when the durable cursor already covers the prepared
    checkpoint, making a repeated acknowledgement a no-op.
    """

    acknowledgement = _decode_ack_token(token)
    if acknowledgement.project_digest != _project_digest(project_root):
        raise ValidationError("Apple Messages delivery token belongs to another state root")
    current = _decode_cursor(current_cursor)
    target = _decode_cursor(acknowledgement.cursor)
    if _cursor_covers(current, target):
        return acknowledgement, True
    if _cursor_digest(current_cursor) != acknowledgement.previous_digest:
        raise ContinuityError("Apple Messages delivery checkpoint changed before acknowledgement")
    if acknowledgement.from_rowid != current.rowid or target.rowid <= current.rowid:
        raise ContinuityError("Apple Messages delivery checkpoint is stale")
    same_store = target.schema == current.schema and target.generation == current.generation
    legacy_rebase = (
        acknowledgement.store_reconciled
        and current.version == 1
        and target.version == CURSOR_VERSION
        and target.schema == current.schema
    )
    if not same_store and not legacy_rebase:
        raise ContinuityError("Apple Messages delivery store changed before acknowledgement")

    status = inspect_apple_messages(
        store_root=store_root,
        observed_at=observed_at,
    )
    if not status.available:
        raise ContinuityError(status.error or "Apple Messages source is unavailable")
    if target.schema != status.schema or target.generation != status.generation:
        raise ContinuityError("Apple Messages delivery store changed before acknowledgement")
    database = (store_root or default_store_root()).expanduser().resolve() / "chat.db"
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
            raise ContinuityError("Apple Messages delivery prefix changed before acknowledgement")
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
        raise ContinuityError("Apple Messages delivery prefix changed before acknowledgement")
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
        raise ContinuityError("Apple Messages delivered content changed before acknowledgement")
    return acknowledgement, False


def _aggregates(database: Path) -> tuple[str, str, int, int, int, str | None]:
    with _connect(database) as (connection, before):
        try:
            connection.execute("BEGIN")
            message_columns = _columns(connection, "message")
            required = {"date", "is_from_me", "text"}
            if not required <= message_columns.keys():
                raise ContinuityError("Apple Messages schema is missing required metadata")
            schema = _schema_fingerprint(message_columns, {})
            message_row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(ROWID), 0), MAX(date) FROM message"
            ).fetchone()
        except sqlite3.Error as exc:
            raise ContinuityError("Apple Messages aggregate query failed") from exc
    if message_row is None:
        raise ContinuityError("Apple Messages aggregate query returned no result")
    messages = int(message_row[0])
    max_rowid = int(message_row[1])
    chats = 0
    if min(messages, max_rowid) < 0:
        raise ContinuityError("Apple Messages aggregate metadata is invalid")
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
            columns = _columns(connection, "message")
            required = {"date", "is_from_me", "text"}
            if not required <= columns.keys():
                raise ContinuityError("Apple Messages schema is missing required delta fields")
            schema = _schema_fingerprint(columns, {})
            generation = _generation(before, schema)
            if schema != expected_schema or generation != expected_generation:
                raise ContinuityError("Apple Messages store changed; cursor preserved")
            visible = "COALESCE(item_type, 0) = 0 AND COALESCE(associated_message_type, 0) = 0"
            attributed = (
                f"substr(attributedBody, 1, {MAX_ATTRIBUTED_BODY_BYTES + 1})"
                if "attributedBody" in columns
                else "NULL"
            )
            attachment = "cache_has_attachments" if "cache_has_attachments" in columns else "0"
            upper_bound = " AND rowid <= ?" if through_rowid is not None else ""
            query = (
                "SELECT ROWID AS rowid, date AS ts, is_from_me AS from_me, "
                + f"CASE WHEN {visible} THEN substr(text, 1, {MAX_BODY_CHARS + 1}) "
                + "ELSE NULL END AS text, "
                + f"CASE WHEN {visible} THEN {attributed} ELSE NULL END AS attributed_body, "
                + f"CASE WHEN {visible} THEN COALESCE({attachment}, 0) ELSE 0 END AS has_attachment"
                + f", CASE WHEN {visible} THEN 1 ELSE 0 END AS visible"
                + " FROM message WHERE ROWID > ?"
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
            raise ContinuityError("Apple Messages delta query failed") from exc
    return rows


@contextmanager
def _connect(database: Path) -> Iterator[tuple[sqlite3.Connection, FileIdentity]]:
    try:
        with pinned_sqlite_snapshot(database, label="Apple Messages store") as (
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
        detail = str(exc).lower()
        if "authorization denied" in detail or "permission denied" in detail:
            raise ContinuityError("Apple Messages store requires Full Disk Access") from exc
        raise ContinuityError("Apple Messages store is unreadable") from exc


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    if table != "message":
        raise ContinuityError("Apple Messages schema request is invalid")
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


def _decode_cursor(cursor: str) -> _Cursor:
    if len(cursor) > MAX_CURSOR_CHARS:
        raise ValidationError("Apple Messages cursor is invalid")
    try:
        payload = json.loads(cursor)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("Apple Messages cursor is invalid") from exc
    required = {"version", "schema", "generation", "messages", "chats", "rowid", "newest"}
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or type(payload.get("version")) is not int
        or payload.get("version") not in {1, CURSOR_VERSION}
    ):
        raise ValidationError("Apple Messages cursor is invalid")
    for name in ("messages", "chats", "rowid"):
        if type(payload[name]) is not int or payload[name] < 0:
            raise ValidationError("Apple Messages cursor is invalid")
    for name in ("schema", "generation"):
        value = payload[name]
        if (
            not isinstance(value, str)
            or len(value) != FINGERPRINT_CHARS
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValidationError("Apple Messages cursor is invalid")
    if payload["newest"] is not None and (
        not isinstance(payload["newest"], str) or len(payload["newest"]) > MAX_TIMESTAMP_CHARS
    ):
        raise ValidationError("Apple Messages cursor is invalid")
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


def _decode_ack_token(token: str) -> AppleMessagesAck:
    if not isinstance(token, str) or not token or len(token) > MAX_ACK_TOKEN_CHARS:
        raise ValidationError("Apple Messages delivery token is invalid")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        envelope = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("Apple Messages delivery token is invalid") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"digest", "payload"}:
        raise ValidationError("Apple Messages delivery token is invalid")
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
        raise ValidationError("Apple Messages delivery token is invalid")
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = envelope.get("digest")
    actual = hashlib.sha256(("seld-apple-messages-ack-v1\0" + canonical).encode()).hexdigest()
    if not isinstance(digest, str) or digest != actual:
        raise ValidationError("Apple Messages delivery token is invalid")
    if payload.get("version") != ACK_TOKEN_VERSION:
        raise ValidationError("Apple Messages delivery token is invalid")
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
        raise ValidationError("Apple Messages delivery token is invalid")
    try:
        _decode_cursor(cursor)
        _parse_iso(observed_at)
    except ContinuityError as exc:
        raise ValidationError("Apple Messages delivery token is invalid") from exc
    return AppleMessagesAck(
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


def _projection_digest(messages: tuple[AppleMessage, ...], *, nonce: str) -> str:
    projection = json.dumps(
        [asdict(message) for message in messages],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(
        (f"seld-apple-messages-projection-v1\0{nonce}\0" + projection).encode()
    ).hexdigest()


def _cursor_covers(current: _Cursor, target: _Cursor) -> bool:
    return (
        current.version == CURSOR_VERSION
        and current.schema == target.schema
        and current.generation == target.generation
        and current.rowid >= target.rowid
    )


def _validate_cursor(cursor: _Cursor, status: AppleMessagesStatus) -> None:
    _validate_cursor_aggregates(cursor, status)
    if cursor.schema != status.schema:
        raise ContinuityError("Apple Messages schema changed; cursor preserved")
    if cursor.generation != status.generation:
        raise ContinuityError("Apple Messages store changed; cursor preserved")


def _validate_cursor_aggregates(cursor: _Cursor, status: AppleMessagesStatus) -> None:
    if cursor.schema != status.schema:
        raise ContinuityError("Apple Messages schema changed; cursor preserved")
    assert status.messages is not None and status.chats is not None and status.max_rowid is not None
    # Counts and timestamps describe mutable inventory, not delivery order.
    # Deletion, expiry, revocation, or correction may legitimately move them
    # backward. The append-only SQLite row high-water remains the fail-closed
    # continuity boundary; inventory drift is returned to Sol as evidence.
    if status.max_rowid < cursor.rowid:
        raise ContinuityError("Apple Messages row cursor regressed; cursor preserved")


def _cursor_drift(cursor: _Cursor, status: AppleMessagesStatus) -> tuple[str, ...]:
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


def _reconcile_cursor(
    cursor: _Cursor, status: AppleMessagesStatus, database: Path
) -> tuple[_Cursor, bool]:
    """Rebase a cursor only when the old aggregate prefix still exists exactly."""

    _validate_cursor_aggregates(cursor, status)
    if cursor.version == CURSOR_VERSION:
        if cursor.generation != status.generation:
            raise ContinuityError("Apple Messages store changed; cursor preserved")
        return cursor, False
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
            "Apple Messages store continuity could not be verified; cursor preserved"
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
            message_columns = _columns(connection, "message")
            schema = _schema_fingerprint(message_columns, {})
            generation = _generation(before, schema)
            if schema != expected_schema or generation != expected_generation:
                raise ContinuityError("Apple Messages store changed; cursor preserved")
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(ROWID), 0), MAX(date) FROM message WHERE ROWID <= ?",
                (through_rowid,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ContinuityError("Apple Messages continuity query failed") from exc
    if row is None:
        raise ContinuityError("Apple Messages continuity query returned no result")
    return int(row[0]), int(row[1]), _epoch_iso(row[2])


def _cursor_at_prefix(database: Path, status: AppleMessagesStatus, last_rowid: int) -> str:
    assert status.schema is not None and status.generation is not None and status.chats is not None
    messages, prefix_rowid, newest = _prefix_aggregates(
        database,
        through_rowid=last_rowid,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )
    if prefix_rowid != last_rowid:
        raise ContinuityError("Apple Messages store changed; cursor preserved")
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
) -> tuple[tuple[AppleMessage, ...], int, bool]:
    messages: list[AppleMessage] = []
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
    if row["text"] is not None and str(row["text"]).strip():
        return str(row["text"])
    return _extract_attributed_body(row["attributed_body"])


def _message(row: sqlite3.Row, *, body: str | None, body_allowance: int) -> AppleMessage:
    truncated = body is not None and len(body) > body_allowance
    bounded_body = body[:body_allowance] if body is not None else None
    return AppleMessage(
        rowid=int(row["rowid"]),
        at=_epoch_iso(row["ts"]) or "1970-01-01T00:00:00Z",
        from_me=bool(row["from_me"]),
        body=bounded_body,
        body_truncated=truncated,
        body_unavailable=body is None and row["attributed_body"] is not None,
        has_attachment=bool(row["has_attachment"]),
    )


def _extract_attributed_body(value: object) -> str | None:
    """Best-effort bounded extraction without deserializing attacker-controlled objects."""
    if not isinstance(value, bytes) or not value:
        return None
    marker = b"NSString"
    start = value.find(marker)
    if start < 0:
        return None
    tail = value[start + len(marker) : start + len(marker) + MAX_BODY_CHARS * 4 + 256]
    candidates = []
    for chunk in tail.split(b"\x00"):
        if len(chunk) < MIN_ATTRIBUTED_CHUNK_BYTES:
            continue
        try:
            text = chunk.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if text and all(character.isprintable() or character.isspace() for character in text):
            candidates.append(text)
    return max(candidates, key=len)[: MAX_BODY_CHARS + 1] if candidates else None


def _epoch_iso(value: object) -> str | None:
    if value is None:
        return None
    try:
        raw = int(str(value))
        seconds = raw / 1_000_000_000 if abs(raw) > APPLE_NANOSECOND_THRESHOLD else raw
        return _iso(datetime.fromtimestamp(seconds + 978_307_200, tz=UTC))
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ContinuityError("Apple Messages timestamp is invalid") from exc


def _aware_utc(value: datetime | None) -> datetime:
    observed = value or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValidationError("Apple Messages observation time must be timezone-aware")
    return observed.astimezone(UTC)


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("Apple Messages cursor is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("Apple Messages cursor is invalid")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
