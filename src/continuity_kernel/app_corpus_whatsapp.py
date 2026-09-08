"""Read-only WhatsApp documents for the host-local app corpus.

The adapter reads only wacli's materialized message store.  It deliberately
does not open the paired session database, which contains WhatsApp credentials.
An account fingerprint is supplied at connection time and carried in the
opaque checkpoint so a stored page cannot silently move to another account.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import quote

from continuity_kernel.app_corpus import AppCorpusDocument, AppCorpusSyncResult
from continuity_kernel.app_corpus_text import MAX_INPUT_BYTES, ExtractionResult, extract_text
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.sqlite_snapshot import SQLiteFileIdentity, pinned_sqlite_snapshot
from continuity_kernel.whatsapp import DEFAULT_RUNTIME, default_store_root

PROVIDER: Final = "whatsapp"
CHECKPOINT_VERSION: Final = 2
LEGACY_CHECKPOINT_VERSION: Final = 1
MAX_CHECKPOINT_CHARS: Final = 4_096
MAX_IDENTIFIER_CHARS: Final = 2_048
MAX_LABEL_CHARS: Final = 512
RESCAN_INTERVAL: Final = timedelta(days=1)
LEGACY_INITIAL_RESCAN_WINDOW: Final = timedelta(hours=1)
_ACCOUNT_FINGERPRINT = "sha256:"
_VOICE_MEDIA_TYPES: Final = frozenset({"audio", "ptt", "voice"})
_RELAY_VOICE_MARKER: Final = (
    "[transcribed voice note (fallback transcript: tone and emphasis may be lost)]"
)
_RELAY_FROM: Final = "service-whatsapp-chief-relay"
_RELAY_EVENT_KEY = re.compile(r"^whatsapp(?:-envoy)?-inbound-([0-9a-f]{32})$")
_RELAY_ENVELOPE_DIRS: Final = frozenset({"inbox", "processed"})
_RELAY_TARGET_PREFIXES: Final = ("envoy", "chief-of-staff", "orchestrator-fabric")
_MAX_RELAY_ENVELOPES: Final = 10_000
_MAX_RELAY_ENVELOPE_BYTES: Final = 64 * 1024

__all__ = ["PROVIDER", "WhatsAppAppCorpusAdapter", "whatsapp_app_capabilities"]


@dataclass(frozen=True)
class _Checkpoint:
    account: str
    generation: str
    phase: str
    rescan_due_at: str | None
    rescan_rowid: int
    rowid: int
    schema: str
    columns: tuple[tuple[str, str], ...] | None = None
    prefix_messages: int | None = None
    prefix_newest: str | None = None
    continuity_gap: bool = False


@dataclass(frozen=True)
class _VoiceTranscript:
    body: str
    event_digest: str


class WhatsAppAppCorpusAdapter:
    """Normalize one explicitly pinned local wacli account into message documents."""

    def __init__(
        self,
        *,
        account_fingerprint: str,
        store_root: Path | None = None,
        voice_transcript_root: Path | None = None,
        recheck_existing: bool = False,
    ) -> None:
        self.account_fingerprint = _validate_account_fingerprint(account_fingerprint)
        if not isinstance(recheck_existing, bool):
            raise ValidationError("WhatsApp existing-message recheck must be a boolean")
        self.store_root = (store_root or default_store_root()).expanduser().resolve()
        self.voice_transcript_root = (
            voice_transcript_root.expanduser()
            if voice_transcript_root is not None
            else Path.home() / ".workbench" / "mail"
        )
        self.recheck_existing = recheck_existing
        self._relay_voice_stamp: tuple[tuple[str, int, int, int], ...] | None = None
        self._relay_voice_envelopes: Mapping[str, _VoiceTranscript] = {}

    def sync(
        self,
        connection_id: str,
        *,
        checkpoint: str | None = None,
        limit: int = 100,
    ) -> AppCorpusSyncResult:
        """Return one bounded page without invoking wacli or opening its session database."""

        _validate_connection_id(connection_id)
        _validate_limit(limit)
        observed_at = _now()
        try:
            prior = _decode_checkpoint(checkpoint, account=self.account_fingerprint)
        except ValidationError:
            return _result(
                checkpoint=checkpoint,
                scanned=0,
                complete=False,
                observed_at=observed_at,
                status="refused",
                detail="WhatsApp retrieval checkpoint is not valid for this linked account",
            )

        try:
            with _connect(self.store_root / "wacli.db") as (connection, identity):
                columns = _columns(connection, "messages")
                schema = _schema_fingerprint(columns)
                _validate_message_schema(columns)
                generation = _generation(identity, schema)
                current = prior or _Checkpoint(
                    account=self.account_fingerprint,
                    generation=generation,
                    phase="initial",
                    rescan_due_at=None,
                    rescan_rowid=0,
                    rowid=0,
                    schema=schema,
                )
                try:
                    current = _reconcile_checkpoint(
                        current,
                        connection=connection,
                        identity=identity,
                        columns=columns,
                        schema=schema,
                        generation=generation,
                    )
                except ContinuityError:
                    return _result(
                        checkpoint=checkpoint,
                        scanned=0,
                        complete=False,
                        observed_at=observed_at,
                        status="refused",
                        detail="WhatsApp local store changed; reconnect before retrieval continues",
                    )
                result = self._sync_snapshot(
                    connection,
                    connection_id=connection_id,
                    current=current,
                    columns=columns,
                    limit=limit,
                    observed_at=observed_at,
                )
                if current.continuity_gap:
                    result = replace(
                        result,
                        freshness=_freshness(
                            "partial",
                            observed_at,
                            "Local WhatsApp rows were removed; retrieval continues "
                            "from the local store. "
                            "Previously indexed messages are retained without inferred deletions",
                        ),
                    )
                return result
        except (ContinuityError, ValidationError, sqlite3.Error):
            return _result(
                checkpoint=checkpoint,
                scanned=0,
                complete=False,
                observed_at=observed_at,
                status="error",
                detail="WhatsApp local message store is unavailable",
            )

    def _sync_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        connection_id: str,
        current: _Checkpoint,
        columns: Mapping[str, str],
        limit: int,
        observed_at: str,
    ) -> AppCorpusSyncResult:
        max_rowid = _max_rowid(connection)
        if current.phase in {"initial", "rescan", "rescan-v2"} and current.rowid > max_rowid:
            return _result(
                checkpoint=_encode_checkpoint(current),
                scanned=0,
                complete=False,
                observed_at=observed_at,
                status="refused",
                detail="WhatsApp local store lost previously observed message rows",
            )

        if _is_legacy_initial_rescan(current, observed_at=observed_at):
            return _initial_complete_result(current, observed_at=observed_at)

        # An explicit recheck uses the normal persisted cursor.  It starts only
        # from a settled history, so later page calls keep their existing
        # rescan position even if a caller accidentally retains the flag.
        if self.recheck_existing and current.phase == "steady":
            current = replace(current, phase="rescan-v2", rescan_rowid=0)

        # New rows are always read before the bounded old-row rescan. The rescan
        # keeps old in-place edits and explicit provider tombstones observable.
        if current.rowid < max_rowid:
            rows = _rows_after(
                connection,
                columns=columns,
                after_rowid=current.rowid,
                through_rowid=None,
                limit=limit + 1,
            )
            return self._new_rows_result(
                rows,
                current=current,
                connection_id=connection_id,
                connection=connection,
                limit=limit,
                observed_at=observed_at,
            )

        if current.phase == "initial":
            return _initial_complete_result(current, observed_at=observed_at)

        due = _rescan_due(current, observed_at=observed_at)
        if current.phase in {"rescan", "rescan-v2"} or due:
            if current.phase not in {"rescan", "rescan-v2"}:
                current = replace(
                    current,
                    phase="rescan-v2",
                    rescan_rowid=0,
                )
            rows = _rows_after(
                connection,
                columns=columns,
                after_rowid=current.rescan_rowid,
                through_rowid=current.rowid,
                limit=limit + 1,
            )
            return self._rescan_result(
                rows,
                current=current,
                connection_id=connection_id,
                limit=limit,
                observed_at=observed_at,
            )

        return _result(
            checkpoint=_encode_checkpoint(current),
            scanned=0,
            complete=True,
            observed_at=observed_at,
            status="complete",
            detail="WhatsApp message history is current; old rows are checked in rolling batches",
        )

    def _new_rows_result(
        self,
        rows: list[sqlite3.Row],
        *,
        current: _Checkpoint,
        connection_id: str,
        connection: sqlite3.Connection,
        limit: int,
        observed_at: str,
    ) -> AppCorpusSyncResult:
        page = rows[:limit]
        if not page:
            raise ContinuityError("WhatsApp store changed during message read")
        next_rowid = int(page[-1]["rowid"])
        has_more = len(rows) > limit
        next_phase = current.phase
        next_rescan_rowid = current.rescan_rowid
        next_due = current.rescan_due_at
        if not has_more and current.phase == "initial":
            next_phase = "steady"
            next_rescan_rowid = 0
            next_due = _iso(_parse_iso(observed_at) + RESCAN_INTERVAL)
        next_checkpoint = _with_prefix(
            replace(
                current,
                phase=next_phase,
                rescan_due_at=next_due,
                rescan_rowid=next_rescan_rowid,
                rowid=next_rowid,
            ),
            connection=connection,
        )
        documents = _documents(
            page,
            connection_id=connection_id,
            account=current.account,
            observed_at=observed_at,
            voice_transcripts=self._voice_transcripts(page),
        )
        incomplete = has_more or next_phase in {"rescan", "rescan-v2"}
        return AppCorpusSyncResult(
            documents=documents,
            checkpoint=_encode_checkpoint(next_checkpoint),
            scanned=len(page),
            complete=not incomplete,
            freshness=_freshness(
                "partial" if incomplete else "complete",
                observed_at,
                "WhatsApp message history is still being read"
                if incomplete
                else "WhatsApp message history is current",
            ),
        )

    def _rescan_result(
        self,
        rows: list[sqlite3.Row],
        *,
        current: _Checkpoint,
        connection_id: str,
        limit: int,
        observed_at: str,
    ) -> AppCorpusSyncResult:
        page = rows[:limit]
        if page:
            next_rescan_rowid = int(page[-1]["rowid"])
            has_more = len(rows) > limit
        else:
            next_rescan_rowid = current.rowid
            has_more = False
        if has_more:
            next_checkpoint = replace(
                current,
                phase="rescan-v2",
                rescan_rowid=next_rescan_rowid,
            )
            complete = False
        else:
            next_checkpoint = replace(
                current,
                phase="steady",
                rescan_due_at=_iso(_parse_iso(observed_at) + RESCAN_INTERVAL),
                rescan_rowid=0,
            )
            complete = True
        return AppCorpusSyncResult(
            documents=_documents(
                page,
                connection_id=connection_id,
                account=current.account,
                observed_at=observed_at,
                voice_transcripts=self._voice_transcripts(page),
            ),
            checkpoint=_encode_checkpoint(next_checkpoint),
            scanned=len(page),
            complete=complete,
            freshness=_freshness(
                "complete" if complete else "partial",
                observed_at,
                "WhatsApp message history is current; old edits are checked in rolling batches"
                if complete
                else "WhatsApp edits and explicit deletions are under rolling review",
            ),
        )

    def _voice_transcripts(self, rows: list[sqlite3.Row]) -> Mapping[int, _VoiceTranscript]:
        """Return only relay transcripts bound to audio rows in this exact page."""

        audio_rows = [
            row
            for row in rows
            if not bool(row["deleted"]) and _is_voice_media_type(row["media_type"])
        ]
        if not audio_rows:
            return {}
        stamp = _relay_mail_stamp(self.voice_transcript_root)
        if stamp is None:
            return {}
        if stamp != self._relay_voice_stamp:
            self._relay_voice_envelopes = _relay_voice_envelopes(self.voice_transcript_root)
            self._relay_voice_stamp = stamp
        matched: dict[int, _VoiceTranscript] = {}
        for row in audio_rows:
            at = _epoch_iso(row["ts"])
            for transcript in self._relay_voice_envelopes.values():
                digest = hashlib.sha256(
                    f"{int(row['rowid'])}:{at}:{transcript.body}".encode()
                ).hexdigest()[:32]
                if digest == transcript.event_digest:
                    matched[int(row["rowid"])] = transcript
                    break
        return matched


def whatsapp_app_capabilities(
    *,
    runtime: Path = DEFAULT_RUNTIME,
    store_root: Path | None = None,
) -> tuple[dict[str, object], ...]:
    """Describe the installed native message surface without supplying an executor.

    The command templates come from the local wacli help surface. They are
    deliberately separate from ``gsv apps call``: this module does not turn a
    provider write into an app-corpus operation.
    """

    executable = str(runtime.expanduser().resolve())
    store = str((store_root or default_store_root()).expanduser().resolve())
    read_prefix = [executable, "--store", store, "--read-only", "--json"]
    write_prefix = [executable, "--store", store, "--json"]
    return (
        _capability(
            name="whatsapp.messages.list",
            mode="read",
            effect="read_only",
            command=[*read_prefix, "messages", "list", "--limit", "<limit>"],
            required=(),
            properties={
                "after": "string",
                "before": "string",
                "chat": "string",
                "from_me": "boolean",
                "from_them": "boolean",
                "limit": "integer",
                "sender": "string",
            },
            reference="wacli messages list --help",
            detail="Read bounded locally synchronized messages.",
        ),
        _capability(
            name="whatsapp.messages.search",
            mode="read",
            effect="read_only",
            command=[*read_prefix, "messages", "search", "<query>", "--limit", "<limit>"],
            required=("query",),
            properties={
                "after": "string",
                "before": "string",
                "chat": "string",
                "has_media": "boolean",
                "limit": "integer",
                "query": "string",
                "type": "string",
            },
            reference="wacli messages search --help",
            detail="Search the local FTS5/LIKE message index.",
        ),
        _capability(
            name="whatsapp.messages.show",
            mode="read",
            effect="read_only",
            command=[*read_prefix, "messages", "show", "--chat", "<chat>", "--id", "<id>"],
            required=("chat", "id"),
            properties={"chat": "string", "id": "string"},
            reference="wacli messages show --help",
            detail="Read one exact locally synchronized message.",
        ),
        _capability(
            name="whatsapp.messages.context",
            mode="read",
            effect="read_only",
            command=[
                *read_prefix,
                "messages",
                "context",
                "--chat",
                "<chat>",
                "--id",
                "<id>",
                "--before",
                "<before>",
                "--after",
                "<after>",
            ],
            required=("chat", "id"),
            properties={
                "after": "integer",
                "before": "integer",
                "chat": "string",
                "id": "string",
            },
            reference="wacli messages context --help",
            detail="Read bounded local context around one exact message.",
        ),
        _capability(
            name="whatsapp.messages.export",
            mode="read",
            effect="read_only",
            command=[*read_prefix, "messages", "export", "--limit", "<limit>"],
            required=(),
            properties={
                "after": "string",
                "before": "string",
                "chat": "string",
                "limit": "integer",
            },
            reference="wacli messages export --help",
            detail=(
                "Export bounded local message JSON to standard output; no --output path is exposed."
            ),
        ),
        _capability(
            name="whatsapp.messages.send_text",
            mode="create",
            effect="external_message",
            command=[*write_prefix, "send", "text", "--to", "<to>", "--message", "<message>"],
            required=("message", "to"),
            properties={
                "message": "string",
                "no_preview": "boolean",
                "reply_to": "string",
                "reply_to_sender": "string",
                "to": "string",
            },
            reference="wacli send text --help",
            detail="Send one text message to an explicit recipient.",
        ),
        _capability(
            name="whatsapp.messages.edit_sent_text",
            mode="update",
            effect="external_message",
            command=[
                *write_prefix,
                "messages",
                "edit",
                "--chat",
                "<chat>",
                "--id",
                "<id>",
                "--message",
                "<message>",
            ],
            required=("chat", "id", "message"),
            properties={"chat": "string", "id": "string", "message": "string"},
            reference="wacli messages edit --help",
            detail="Edit one of this account's recent sent text messages.",
        ),
        _capability(
            name="whatsapp.messages.delete",
            mode="delete",
            effect="external_delete",
            command=[*write_prefix, "messages", "delete", "--chat", "<chat>", "--id", "<id>"],
            required=("chat", "id"),
            properties={
                "chat": "string",
                "delete_media": "boolean",
                "for_me": "boolean",
                "id": "string",
            },
            reference="wacli messages delete --help",
            detail="Delete one message for everyone or, with for_me, for this account only.",
        ),
        _capability(
            name="whatsapp.messages.revoke",
            mode="delete",
            effect="external_delete",
            command=[*write_prefix, "messages", "revoke", "--chat", "<chat>", "--id", "<id>"],
            required=("chat", "id"),
            properties={"chat": "string", "id": "string"},
            reference="wacli messages revoke --help",
            detail="Delete one of this account's sent messages for everyone.",
        ),
        _capability(
            name="whatsapp.messages.purge_tombstone",
            mode="delete",
            effect="local_delete",
            command=[
                *write_prefix,
                "messages",
                "purge",
                "--chat",
                "<chat>",
                "--id",
                "<id>",
                "--confirm",
            ],
            required=("chat", "id"),
            properties={"chat": "string", "id": "string"},
            reference="wacli messages purge --help",
            detail="Permanently erase only the local payload of an already tombstoned message.",
        ),
    )


def _capability(
    *,
    name: str,
    mode: str,
    effect: str,
    command: list[str],
    required: tuple[str, ...],
    properties: Mapping[str, str],
    reference: str,
    detail: str,
) -> dict[str, object]:
    write = effect != "read_only"
    return {
        "approval_required": write,
        "apps_call_supported": False,
        "command": command,
        "detail": detail,
        "effect": effect,
        "executor": "native_wacli_cli",
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                property_name: {"type": kind} for property_name, kind in properties.items()
            },
            "required": list(required),
            "type": "object",
        },
        "mode": mode,
        "name": name,
        "provider": PROVIDER,
        "reference": reference,
    }


def _initial_complete_result(
    current: _Checkpoint,
    *,
    observed_at: str,
) -> AppCorpusSyncResult:
    """Finish the initial rowid pass and defer the first duplicate review."""

    steady = replace(
        current,
        phase="steady",
        rescan_due_at=_iso(_parse_iso(observed_at) + RESCAN_INTERVAL),
        rescan_rowid=0,
    )
    return _result(
        checkpoint=_encode_checkpoint(steady),
        scanned=0,
        complete=True,
        observed_at=observed_at,
        status="complete",
        detail="WhatsApp message history is current; old rows are checked in rolling batches",
    )


def _is_voice_media_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    return normalized in _VOICE_MEDIA_TYPES or normalized.startswith("audio/")


def _is_media_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return bool(value.strip()) and value.strip().casefold() not in {"none", "text"}


def _relay_mail_stamp(root: Path) -> tuple[tuple[str, int, int, int], ...] | None:
    directories = _relay_envelope_directories(root)
    if directories is None:
        return None
    return tuple(
        (name, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size)
        for name, _path, metadata in directories
    )


def _relay_voice_envelopes(root: Path) -> Mapping[str, _VoiceTranscript]:
    directories = _relay_envelope_directories(root)
    if directories is None:
        return {}
    transcripts: dict[str, _VoiceTranscript] = {}
    rejected: set[str] = set()
    checked = 0
    for _name, directory, _metadata in directories:
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if checked >= _MAX_RELAY_ENVELOPES:
                return {}
            checked += 1
            transcript = _read_relay_voice_envelope(entry)
            if transcript is None:
                continue
            prior = transcripts.get(transcript.event_digest)
            if prior is None:
                if transcript.event_digest not in rejected:
                    transcripts[transcript.event_digest] = transcript
            elif prior.body != transcript.body:
                rejected.add(transcript.event_digest)
                transcripts.pop(transcript.event_digest, None)
    return transcripts


def _relay_envelope_directories(
    root: Path,
) -> tuple[tuple[str, Path, os.stat_result], ...] | None:
    if _directory_metadata(root) is None:
        return None
    directories: list[tuple[str, Path, os.stat_result]] = []
    try:
        mailboxes = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return None
    for mailbox in mailboxes:
        if _directory_metadata(mailbox) is None or not mailbox.name.startswith(
            _RELAY_TARGET_PREFIXES
        ):
            continue
        try:
            candidates = sorted(mailbox.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for candidate in candidates:
            if candidate.name not in _RELAY_ENVELOPE_DIRS:
                continue
            metadata = _directory_metadata(candidate)
            if metadata is not None:
                directories.append((f"{mailbox.name}/{candidate.name}", candidate, metadata))
    return tuple(directories)


def _directory_metadata(path: Path) -> os.stat_result | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    return metadata if stat.S_ISDIR(metadata.st_mode) else None


def _read_relay_voice_envelope(path: Path) -> _VoiceTranscript | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > _MAX_RELAY_ENVELOPE_BYTES:
            return None
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
            raw = handle.read(_MAX_RELAY_ENVELOPE_BYTES + 1)
        current = os.fstat(descriptor)
        if (
            len(raw) > _MAX_RELAY_ENVELOPE_BYTES
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
        ):
            return None
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _parse_relay_voice_envelope(text)


def _parse_relay_voice_envelope(text: str) -> _VoiceTranscript | None:
    if "\x00" in text or not text.startswith("---\n"):
        return None
    header, separator, body = text[4:].partition("\n---\n")
    if not separator:
        return None
    fields: dict[str, str] = {}
    for line in header.splitlines():
        key, delimiter, value = line.partition(":")
        if not delimiter or not re.fullmatch(r"[a-z][a-z-]{0,63}", key):
            continue
        if key in fields:
            return None
        fields[key] = value.strip().strip("\"'")
    if fields.get("from") != _RELAY_FROM or fields.get("kind") != "request":
        return None
    event_key = fields.get("event-key")
    match = _RELAY_EVENT_KEY.fullmatch(event_key or "")
    if match is None:
        return None
    transcript = body.lstrip("\r\n").rstrip("\r\n")
    if not transcript.startswith(_RELAY_VOICE_MARKER):
        return None
    if not transcript.removeprefix(_RELAY_VOICE_MARKER).strip():
        return None
    return _VoiceTranscript(body=transcript, event_digest=match.group(1))


def _documents(
    rows: list[sqlite3.Row],
    *,
    connection_id: str,
    account: str,
    observed_at: str,
    voice_transcripts: Mapping[int, _VoiceTranscript],
) -> tuple[AppCorpusDocument, ...]:
    return tuple(
        _document(
            row,
            connection_id=connection_id,
            account=account,
            observed_at=observed_at,
            voice_transcript=voice_transcripts.get(int(row["rowid"])),
        )
        for row in rows
    )


def _document(
    row: sqlite3.Row,
    *,
    connection_id: str,
    account: str,
    observed_at: str,
    voice_transcript: _VoiceTranscript | None,
) -> AppCorpusDocument:
    chat_jid = _provider_identifier(row["chat_jid"])
    message_id = _provider_identifier(row["msg_id"])
    at = _epoch_iso(row["ts"])
    edited_at = _optional_epoch_iso(row["edited_ts"])
    deleted = bool(row["deleted"])
    conversation = _label(row["chat_name"]) or "WhatsApp chat"
    media_type = _label(row["media_type"])
    text = "" if deleted else _message_text(row)
    if not deleted and voice_transcript is not None:
        text = "\n\n".join(value for value in (text, voice_transcript.body) if value)
    revision_at = edited_at or at
    object_digest = hashlib.sha256(f"{account}\0{chat_jid}\0{message_id}".encode()).hexdigest()
    source_ref = (
        "whatsapp://local/message/"
        f"{quote(chat_jid, safe='')}/{quote(message_id, safe='')}?at={quote(at, safe='')}"
    )
    metadata: dict[str, object] = {
        "conversation": conversation,
        "direction": "outbound" if bool(row["from_me"]) else "inbound",
        "sent_at": at,
    }
    if media_type is not None:
        metadata["media_type"] = media_type
    row_columns = row.keys()  # sqlite3.Row membership checks values, not column names.
    filename = _label(row["filename"]) if "filename" in row_columns else None
    if filename and not deleted:
        metadata["attachment_name"] = filename
        text = "\n\n".join(value for value in (text, f"Attachment: {filename}") if value)
    if not deleted and _is_voice_media_type(media_type):
        if voice_transcript is not None:
            metadata["media_extraction_status"] = "local_transcript"
            metadata["transcript_source"] = "local_relay"
        else:
            # A caption or message line can be useful context, but it is not a
            # transcription of the attached audio.
            metadata["extraction_status"] = "gap"
            metadata["media_extraction_status"] = "transcript_unavailable"
    elif not deleted and _is_media_type(media_type):
        # The local message store supplies only message text and captions.  It
        # does not extract the attachment itself, so never label that text as
        # complete media content.
        metadata["extraction_status"] = "partial" if text else "gap"
        metadata["media_extraction_status"] = "not_extracted"
        extracted = _local_attachment_text(row)
        if extracted is not None:
            metadata["extraction_status"] = extracted.status
            metadata["media_extraction_status"] = (
                "local_text"
                if extracted.status == "ok"
                else "partial_text"
                if extracted.text
                else "not_extracted"
            )
            if extracted.reason:
                metadata["extraction_reason"] = extracted.reason
            if extracted.text:
                text = "\n\n".join(value for value in (text, extracted.text) if value)
    revision = f"whatsapp-message:{revision_at}"
    if voice_transcript is not None and not deleted:
        revision += f":relay-transcript:{voice_transcript.event_digest}"
    if metadata.get("media_extraction_status") in {"local_text", "partial_text"}:
        revision += ":attachment-text:" + hashlib.sha256(text.encode()).hexdigest()
    return AppCorpusDocument(
        connection_id=connection_id,
        provider=PROVIDER,
        object_id=f"whatsapp-message:{object_digest}",
        revision=revision,
        fetched_at=observed_at,
        source_ref=source_ref,
        title=f"WhatsApp | {conversation} | {at}",
        text=text,
        metadata=metadata,
        freshness=_freshness("complete", observed_at, "Read from the local WhatsApp store"),
        deleted=deleted,
    )


def _message_text(row: sqlite3.Row) -> str:
    values: list[str] = []
    for name in ("text", "display_text", "media_caption"):
        value = row[name]
        if value is None:
            continue
        if not isinstance(value, str) or "\x00" in value:
            raise ContinuityError("WhatsApp message text is invalid")
        if value and value not in values:
            values.append(value)
    return "\n\n".join(values)


def _local_attachment_text(row: sqlite3.Row) -> ExtractionResult | None:
    """Extract only a bounded snapshot matching the provider's plaintext digest.

    wacli may record downloads outside its store. A path alone is insufficient:
    authenticate the bytes against the message before passing them to a parser.
    Never download, open the paired session database, or follow a file symlink.
    """
    keys = row.keys()
    path = row["local_path"] if "local_path" in keys else None
    digest = row["file_sha256"] if "file_sha256" in keys else None
    if not isinstance(path, str) or not path:
        return None
    if not isinstance(digest, bytes) or len(digest) != 32:
        return ExtractionResult("", "gap", "attachment digest unavailable", ())
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT_BYTES:
                return ExtractionResult("", "gap", "attachment is not a bounded regular file", ())
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES or hashlib.sha256(data).digest() != digest:
            return ExtractionResult("", "gap", "attachment digest mismatch", ())
        filename = row["filename"] if "filename" in keys else None
        suffix = Path(filename or path).suffix[:16]
        mime = row["mime_type"] if "mime_type" in keys else None
        with tempfile.TemporaryDirectory(prefix="gsv-wa-text-") as temporary:
            snapshot = Path(temporary) / ("attachment" + suffix)
            snapshot.write_bytes(data)
            return extract_text(snapshot, mime=mime.split(";", 1)[0] if mime else None)
    except (OSError, ValueError):
        return ExtractionResult("", "gap", "local attachment unavailable", ())


def _rows_after(
    connection: sqlite3.Connection,
    *,
    columns: Mapping[str, str],
    after_rowid: int,
    through_rowid: int | None,
    limit: int,
) -> list[sqlite3.Row]:
    if after_rowid < 0 or (through_rowid is not None and through_rowid < after_rowid):
        raise ContinuityError("WhatsApp retrieval cursor is invalid")
    selected = {
        name: name if name in columns else f"NULL AS {name}"
        for name in (
            "chat_name",
            "text",
            "display_text",
            "media_caption",
            "media_type",
            "edited_ts",
            "local_path",
            "file_sha256",
            "filename",
            "mime_type",
        )
    }
    deleted_terms = [
        f"COALESCE({name}, 0) <> 0"
        for name in ("revoked", "deleted_for_me", "deleted_at", "payload_purged_at")
        if name in columns
    ]
    deleted = " OR ".join(deleted_terms) if deleted_terms else "0"
    where = "rowid > ?"
    parameters: list[int] = [after_rowid]
    if through_rowid is not None:
        where += " AND rowid <= ?"
        parameters.append(through_rowid)
    parameters.append(limit)
    query = (
        "SELECT rowid, chat_jid, msg_id, ts, from_me, "
        + ", ".join(selected.values())
        + f", CASE WHEN {deleted} THEN 1 ELSE 0 END AS deleted "
        + f"FROM messages WHERE {where} ORDER BY rowid ASC LIMIT ?"
    )
    try:
        return connection.execute(query, tuple(parameters)).fetchall()
    except sqlite3.Error as exc:
        raise ContinuityError("WhatsApp message query failed") from exc


def _max_rowid(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()
    except sqlite3.Error as exc:
        raise ContinuityError("WhatsApp message aggregate query failed") from exc
    if row is None or not isinstance(row[0], int) or row[0] < 0:
        raise ContinuityError("WhatsApp message aggregate is invalid")
    return row[0]


@contextmanager
def _connect(database: Path) -> Iterator[tuple[sqlite3.Connection, SQLiteFileIdentity]]:
    try:
        with pinned_sqlite_snapshot(database, label="WhatsApp local message store") as (
            snapshot,
            identity,
            snapshot_immutable,
        ):
            suffix = "?mode=ro&immutable=1" if snapshot_immutable else "?mode=ro"
            connection = sqlite3.connect(f"{snapshot.as_uri()}{suffix}", uri=True, timeout=2.0)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA busy_timeout = 2000")
                connection.execute("BEGIN")
                yield connection, identity
            finally:
                connection.close()
    except ValidationError as exc:
        raise ContinuityError("WhatsApp local message store is unavailable") from exc
    except sqlite3.Error as exc:
        raise ContinuityError("WhatsApp local message store is unavailable") from exc


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    if table != "messages":
        raise ContinuityError("WhatsApp schema request is invalid")
    try:
        rows = connection.execute("PRAGMA table_info(messages)").fetchall()
    except sqlite3.Error as exc:
        raise ContinuityError("WhatsApp schema read failed") from exc
    return {str(row[1]): str(row[2]).upper() for row in rows}


def _validate_message_schema(columns: Mapping[str, str]) -> None:
    required = {"rowid", "chat_jid", "msg_id", "ts", "from_me"}
    if not required <= columns.keys():
        raise ContinuityError("WhatsApp schema is missing required message fields")


def _schema_fingerprint(columns: Mapping[str, str]) -> str:
    encoded = json.dumps(sorted(columns.items()), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _generation(identity: SQLiteFileIdentity, schema: str) -> str:
    _device, inode, _size, _modified_ns, birthtime = identity
    return hashlib.sha256(f"v1:{inode}:{birthtime}:{schema}".encode()).hexdigest()[:20]


def _schema_columns(columns: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(columns.items()))


def _same_store_under_prior_schema(checkpoint: _Checkpoint, identity: SQLiteFileIdentity) -> bool:
    """Check the file identity separately from its schema fingerprint."""

    return _generation(identity, checkpoint.schema) == checkpoint.generation


def _reconcile_checkpoint(
    checkpoint: _Checkpoint,
    *,
    connection: sqlite3.Connection,
    identity: SQLiteFileIdentity,
    columns: Mapping[str, str],
    schema: str,
    generation: str,
) -> _Checkpoint:
    """Keep a row cursor across a verified, additive in-place migration.

    A checkpoint generation binds an inode and birth time to the schema.  A
    wacli ``ALTER TABLE`` therefore moves the generation even though the file
    and the append-only row cursor remain valid.  Recompute the old generation
    from the live file to separate that safe case from a replaced database.
    """

    current_columns = _schema_columns(columns)
    if checkpoint.schema == schema:
        if checkpoint.generation != generation:
            raise ContinuityError("WhatsApp local store changed")
        if checkpoint.columns is not None and checkpoint.columns != current_columns:
            raise ContinuityError("WhatsApp local store changed")
        # Local retention may remove the high-water row without replacing the
        # pinned database. Replay remaining rows, but do not infer tombstones.
        if (
            checkpoint.rowid
            and connection.execute(
                "SELECT 1 FROM messages WHERE rowid = ?", (checkpoint.rowid,)
            ).fetchone()
            is None
        ):
            checkpoint = replace(
                checkpoint,
                rowid=0,
                phase="initial",
                rescan_rowid=0,
                rescan_due_at=None,
                continuity_gap=True,
            )
        return _with_prefix(replace(checkpoint, columns=current_columns), connection=connection)

    if not _same_store_under_prior_schema(checkpoint, identity):
        raise ContinuityError("WhatsApp local store changed")
    if checkpoint.columns is None:
        if not _legacy_additive_schema(checkpoint.schema, columns):
            raise ContinuityError("WhatsApp local store schema changed")
    elif not _is_additive_schema(checkpoint.columns, current_columns):
        raise ContinuityError("WhatsApp local store schema changed")
    else:
        _validate_prefix(checkpoint, connection=connection)

    # This also rejects a cursor whose high-water row vanished during the
    # migration.  It leaves the rowid untouched, so the next page has no replay.
    return _with_prefix(
        replace(
            checkpoint,
            columns=current_columns,
            generation=generation,
            schema=schema,
        ),
        connection=connection,
    )


def _is_additive_schema(
    previous: tuple[tuple[str, str], ...], current: tuple[tuple[str, str], ...]
) -> bool:
    before = dict(previous)
    after = dict(current)
    return len(after) > len(before) and all(
        after.get(name) == kind for name, kind in before.items()
    )


def _legacy_additive_schema(previous_schema: str, current: Mapping[str, str]) -> bool:
    """Recognize only wacli's documented 29-Aug column migration for v1 cursors.

    Version-one checkpoints stored only a digest, so they cannot describe an
    arbitrary prior schema.  They may still cross this known additive migration;
    all other schema changes stay refused.  Version two records the schema shape
    and supports any additive migration whose old columns are unchanged.
    """

    additions = {
        "deleted_at": "INTEGER",
        "deletion_reason": "TEXT",
        "payload_purged_at": "INTEGER",
    }
    present = tuple(name for name, kind in additions.items() if current.get(name) == kind)
    for mask in range(1, 1 << len(present)):
        removed = {present[index] for index in range(len(present)) if mask & (1 << index)}
        candidate = {name: kind for name, kind in current.items() if name not in removed}
        if _schema_fingerprint(candidate) == previous_schema:
            return True
    return False


def _prefix_aggregates(
    connection: sqlite3.Connection, *, through_rowid: int
) -> tuple[int, str | None]:
    try:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0), MAX(ts) FROM messages WHERE rowid <= ?",
            (through_rowid,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ContinuityError("WhatsApp local message aggregate query failed") from exc
    if row is None or int(row[1]) != through_rowid:
        raise ContinuityError("WhatsApp local store lost previously observed message rows")
    return int(row[0]), _epoch_iso(row[2]) if row[2] is not None else None


def _with_prefix(checkpoint: _Checkpoint, *, connection: sqlite3.Connection) -> _Checkpoint:
    messages, newest = _prefix_aggregates(connection, through_rowid=checkpoint.rowid)
    return replace(checkpoint, prefix_messages=messages, prefix_newest=newest)


def _validate_prefix(checkpoint: _Checkpoint, *, connection: sqlite3.Connection) -> None:
    if checkpoint.prefix_messages is None:
        raise ContinuityError("WhatsApp retrieval checkpoint is incomplete")
    messages, newest = _prefix_aggregates(connection, through_rowid=checkpoint.rowid)
    if messages != checkpoint.prefix_messages or newest != checkpoint.prefix_newest:
        raise ContinuityError("WhatsApp local store continuity could not be verified")


def _encode_checkpoint(value: _Checkpoint) -> str:
    if value.columns is None or value.prefix_messages is None:
        raise ValidationError("WhatsApp retrieval checkpoint is incomplete")
    return json.dumps(
        {
            "account": value.account,
            "columns": [list(column) for column in value.columns],
            "generation": value.generation,
            "phase": value.phase,
            "prefix_messages": value.prefix_messages,
            "prefix_newest": value.prefix_newest,
            "rescan_due_at": value.rescan_due_at,
            "rescan_rowid": value.rescan_rowid,
            "rowid": value.rowid,
            "schema": value.schema,
            "version": CHECKPOINT_VERSION,
            **({"continuity_gap": True} if value.continuity_gap else {}),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_checkpoint(value: str | None, *, account: str) -> _Checkpoint | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_CHECKPOINT_CHARS:
        raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError("WhatsApp retrieval checkpoint is invalid") from exc
    legacy_required = {
        "account",
        "generation",
        "phase",
        "rescan_due_at",
        "rescan_rowid",
        "rowid",
        "schema",
        "version",
    }
    required = legacy_required | {"columns", "prefix_messages", "prefix_newest"}
    if not isinstance(payload, dict):
        raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    version = payload.get("version")
    if version == LEGACY_CHECKPOINT_VERSION and set(payload) == legacy_required:
        columns: tuple[tuple[str, str], ...] | None = None
        prefix_messages: int | None = None
        prefix_newest: str | None = None
    elif version == CHECKPOINT_VERSION and set(payload) in (
        required,
        required | {"continuity_gap"},
    ):
        columns = _decode_schema_columns(payload.get("columns"))
        prefix_messages = payload.get("prefix_messages")
        if (
            not isinstance(prefix_messages, int)
            or isinstance(prefix_messages, bool)
            or prefix_messages < 0
        ):
            raise ValidationError("WhatsApp retrieval checkpoint is invalid")
        prefix_newest = payload.get("prefix_newest")
        if prefix_newest is not None:
            _parse_iso(prefix_newest)
    else:
        raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    if not isinstance(payload.get("continuity_gap", False), bool):
        raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    if payload.get("account") != account:
        raise ValidationError("WhatsApp retrieval checkpoint belongs to another account")
    for name in ("schema", "generation"):
        token = payload.get(name)
        if (
            not isinstance(token, str)
            or len(token) != 20
            or any(c not in "0123456789abcdef" for c in token)
        ):
            raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    phase = payload.get("phase")
    if phase not in {"initial", "rescan", "rescan-v2", "steady"}:
        raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    for name in ("rowid", "rescan_rowid"):
        number = payload.get(name)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    due = payload.get("rescan_due_at")
    if due is not None:
        _parse_iso(due)
    return _Checkpoint(
        account=account,
        generation=str(payload["generation"]),
        phase=str(phase),
        rescan_due_at=due,
        rescan_rowid=int(payload["rescan_rowid"]),
        rowid=int(payload["rowid"]),
        schema=str(payload["schema"]),
        columns=columns,
        prefix_messages=prefix_messages,
        prefix_newest=prefix_newest,
        continuity_gap=payload.get("continuity_gap", False),
    )


def _decode_schema_columns(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > 128:
        raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    decoded: list[tuple[str, str]] = []
    for column in value:
        if (
            not isinstance(column, list)
            or len(column) != 2
            or not isinstance(column[0], str)
            or not isinstance(column[1], str)
            or not column[0]
            or len(column[0]) > 255
            or len(column[1]) > 255
        ):
            raise ValidationError("WhatsApp retrieval checkpoint is invalid")
        decoded.append((column[0], column[1]))
    if decoded != sorted(decoded) or len(set(decoded)) != len(decoded):
        raise ValidationError("WhatsApp retrieval checkpoint is invalid")
    return tuple(decoded)


def _rescan_due(value: _Checkpoint, *, observed_at: str) -> bool:
    if value.rescan_due_at is None:
        return False
    return _parse_iso(value.rescan_due_at) <= _parse_iso(observed_at)


def _is_legacy_initial_rescan(value: _Checkpoint, *, observed_at: str) -> bool:
    """End the old immediate duplicate pass created by checkpoint version one."""

    if value.phase != "rescan" or value.rescan_due_at is None:
        return False
    elapsed = _parse_iso(observed_at) - _parse_iso(value.rescan_due_at)
    return timedelta() <= elapsed <= LEGACY_INITIAL_RESCAN_WINDOW


def _result(
    *,
    checkpoint: str | None,
    scanned: int,
    complete: bool,
    observed_at: str,
    status: str,
    detail: str,
) -> AppCorpusSyncResult:
    return AppCorpusSyncResult(
        documents=(),
        checkpoint=checkpoint,
        scanned=scanned,
        complete=complete,
        freshness=_freshness(status, observed_at, detail),
    )


def _freshness(status: str, observed_at: str, detail: str) -> dict[str, str]:
    return {"detail": detail, "status": status, "updated_at": observed_at}


def _validate_account_fingerprint(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(_ACCOUNT_FINGERPRINT)
        or len(value) != len(_ACCOUNT_FINGERPRINT) + 64
        or any(
            character not in "0123456789abcdef" for character in value[len(_ACCOUNT_FINGERPRINT) :]
        )
    ):
        raise ValidationError("WhatsApp account fingerprint is invalid")
    return value


def _validate_connection_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValidationError("app corpus connection ID must be a non-empty string")


def _validate_limit(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1_000:
        raise ValidationError("WhatsApp retrieval limit must be between 1 and 1000")


def _provider_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value) > MAX_IDENTIFIER_CHARS
    ):
        raise ContinuityError("WhatsApp message identity is invalid")
    return value


def _label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise ContinuityError("WhatsApp message label is invalid")
    compact = " ".join(value.split())
    return compact[:MAX_LABEL_CHARS] if compact else None


def _epoch_iso(value: object) -> str:
    try:
        return _iso(datetime.fromtimestamp(int(value), tz=UTC))
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ContinuityError("WhatsApp message timestamp is invalid") from exc


def _optional_epoch_iso(value: object) -> str | None:
    if value is None:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise ContinuityError("WhatsApp edit timestamp is invalid") from exc
    return _epoch_iso(timestamp) if timestamp > 0 else None


def _now() -> str:
    return _iso(datetime.now(UTC))


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("WhatsApp retrieval timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("WhatsApp retrieval timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("WhatsApp retrieval timestamp is invalid")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
