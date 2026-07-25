"""Narrow append-only control surface for Bridge-authored user intent.

The Bridge may record an explicit choice, approval, correction, or undo request.
Those events are *requests* for the deterministic core or a Codex hand to
interpret.  They never mutate semantic canon directly.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from continuity_kernel.atomic import (
    PINNED_PATH_ROOT_SUPPORTED,
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    sha256_bytes,
)
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    ValidationError,
)
from continuity_kernel.records import format_time, stored_time
from continuity_kernel.vault_identity import parse_vault_manifest

CONTROL_SCHEMA_VERSION: Final = 1
MAX_QUEUE_BYTES: Final = 1024 * 1024
MAX_CONTROL_TEXT_BYTES: Final = 4096
MAX_EVENTS: Final = 2_000
MAX_GENERATIONS: Final = 1_024
MAX_VAULT_MANIFEST_BYTES: Final = 64 * 1024
EMPTY_REVISION: Final = sha256_bytes(b"")
CONTROL_STORE_SUPPORTED: Final = PINNED_PATH_ROOT_SUPPORTED
_MARKER_RELATIVE: Final = ".gsv/control/initialized"
_MARKER_PREPARING: Final = b'{"schema_version":1,"state":"preparing"}\n'
_MARKER_INITIALIZED: Final = b'{"schema_version":1,"state":"initialized"}\n'
_MAX_MARKER_BYTES: Final = 128
_SHA256_REVISION: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_SUBJECT_REFERENCE: Final = re.compile(
    r"^(?P<namespace>[a-z][a-z0-9-]{0,31}):(?P<identifier>[A-Za-z0-9][A-Za-z0-9._/-]{0,479})$"
)
_HEADER_KEYS: Final = frozenset(
    {
        "disposition_head_revision",
        "event_count",
        "events_digest",
        "generation",
        "opened_at",
        "previous_revision",
        "record_type",
        "schema_version",
    }
)


class ControlKind(StrEnum):
    """The only intent shapes Bridge is allowed to append."""

    SETUP_CHOICE = "setup_choice"
    APPROVAL = "approval"
    CORRECTION = "correction"
    UNDO_REQUEST = "undo_request"


class ControlStorageError(ValidationError):
    """The private control store is unavailable or violates its stored contract."""


@contextmanager
def control_store(vault_root: Path) -> Iterator[PinnedPathRoot]:
    """Open the secure pinned store or classify platform absence as unavailable."""

    try:
        store = PinnedPathRoot(vault_root)
    except ValidationError as exc:
        raise ControlStorageError(f"private control storage is unavailable: {exc}") from exc
    try:
        yield store
    finally:
        store.close()


@contextmanager
def locked_control_store(
    vault_root: Path,
    *,
    expected_vault_id: str | None = None,
    expected_root_identity: tuple[int, int] | None = None,
) -> Iterator[PinnedPathRoot]:
    """Hold the vault-wide writer lock for one prevalidated pinned vault."""

    with control_store(vault_root) as store:
        if expected_root_identity is not None:
            _validate_root_binding(vault_root, expected_root_identity)
        if expected_vault_id is not None:
            _validate_vault_binding(store, expected_vault_id)
        # Backups normally preserve this directory. Recreate it defensively so
        # a partial manual copy cannot leak a raw ENOENT through the CLI.
        if not store.directory_exists(".gsv/locks"):
            store.ensure_directory(".gsv/locks")
        with (
            store.exclusive_file_lock(".gsv/locks/global.lock"),
            store.exclusive_root_lock(),
        ):
            if expected_root_identity is not None:
                _validate_root_binding(vault_root, expected_root_identity)
            if expected_vault_id is not None:
                _validate_vault_binding(store, expected_vault_id)
            yield store


@dataclass(frozen=True)
class ControlEvent:
    schema_version: int
    event_id: str
    kind: ControlKind
    subject: str
    choice: str
    target_revision: str | None
    created_at: str
    source: str = "bridge"


@dataclass(frozen=True)
class ControlSnapshot:
    events: tuple[ControlEvent, ...]
    generation: int
    previous_revision: str | None
    disposition_head_revision: str | None
    revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [event_dict(event) for event in self.events],
            "generation": self.generation,
            "previous_revision": self.previous_revision,
            "disposition_head_revision": self.disposition_head_revision,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class _QueueHeader:
    generation: int
    previous_revision: str | None
    disposition_head_revision: str | None
    opened_at: str
    event_count: int
    events_digest: str


class ControlQueue:
    """CAS-protected append-only queue inside one exact vault."""

    def __init__(self, vault_root: Path | str):
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.path = self.vault_root / ".gsv/control/queue.jsonl"

    def snapshot(self) -> ControlSnapshot:
        with control_store(self.vault_root) as store:
            if not store.directory_exists(".gsv/control"):
                return _empty_snapshot()
            with store.bind_directory(".gsv/control"):
                return self._snapshot_with_store(store, allow_missing=True)

    def _snapshot_with_store(
        self,
        store: PinnedPathRoot,
        *,
        allow_missing: bool,
        full_lineage: bool = False,
    ) -> ControlSnapshot:
        marker = self._marker_state(store)
        stored = self._read_optional_bytes(store)
        if stored is None:
            if marker == "initialized":
                raise ControlStorageError("control queue disappeared after initialization")
            if allow_missing:
                return _empty_snapshot()
            raise ControlStorageError("control queue has not been initialized")
        try:
            header, events = _parse_queue_document(stored)
        except ValidationError as exc:
            raise ControlStorageError(f"stored control queue is invalid: {exc}") from exc
        generation = header.generation if header is not None else 0
        snapshot = ControlSnapshot(
            events=events,
            generation=generation,
            previous_revision=(header.previous_revision if header is not None else None),
            disposition_head_revision=(
                header.disposition_head_revision if header is not None else None
            ),
            revision=sha256_bytes(stored),
        )
        # Ordinary reads surface only the immediately previous generation.
        # Keep Bridge polling O(1); callers performing a mutation explicitly
        # request the complete archive chain before changing durable state.
        self._validate_lineage(store, header, max_depth=None if full_lineage else 1)
        return snapshot

    def append(
        self,
        *,
        kind: object,
        subject: object,
        choice: object,
        expected_revision: object,
        target_revision: object = None,
        expected_vault_id: object = None,
        expected_root_identity: tuple[int, int] | None = None,
        observed_at: datetime | None = None,
    ) -> ControlSnapshot:
        clean_kind = _control_kind(kind)
        clean_subject = _subject_reference(subject, clean_kind)
        clean_choice = _bounded_text(choice, "choice", max_bytes=MAX_CONTROL_TEXT_BYTES)
        clean_target = _optional_revision(target_revision)
        clean_expected = _revision(expected_revision, "expected_revision")
        clean_vault_id = (
            _uuid(expected_vault_id, "expected vault ID") if expected_vault_id is not None else None
        )
        with locked_control_store(
            self.vault_root,
            expected_vault_id=clean_vault_id,
            expected_root_identity=expected_root_identity,
        ) as store:
            store.ensure_directory(".gsv/control")
            with store.bind_directory(".gsv/control"):
                return self._append_with_store(
                    store,
                    kind=clean_kind,
                    subject=clean_subject,
                    choice=clean_choice,
                    target_revision=clean_target,
                    expected_revision=clean_expected,
                    observed_at=observed_at,
                )

    def _append_with_store(
        self,
        store: PinnedPathRoot,
        *,
        kind: ControlKind,
        subject: str,
        choice: str,
        target_revision: str | None,
        expected_revision: str,
        observed_at: datetime | None,
    ) -> ControlSnapshot:
        marker = self._marker_state(store)
        stored = self._read_optional_bytes(store)
        if stored is None and marker == "initialized":
            raise ControlStorageError("control queue disappeared after initialization")
        before = stored or b""
        before_revision = sha256_bytes(before)
        try:
            header, events = _parse_queue_document(before)
        except ValidationError as exc:
            raise ControlStorageError(f"stored control queue is invalid: {exc}") from exc
        self._validate_lineage(store, header)
        generation = header.generation if header is not None else 0
        if before_revision != expected_revision:
            raise ConflictError(
                "control queue changed; reload it and append against the latest revision"
            )
        if stored is None and marker is None:
            self._write_preparing_marker(store)
            marker = "preparing"
        if len(events) >= MAX_EVENTS:
            raise ValidationError("control queue reached its bounded event limit")
        event_time = observed_at or datetime.now(UTC)
        event = ControlEvent(
            schema_version=CONTROL_SCHEMA_VERSION,
            event_id=str(uuid.uuid4()),
            kind=kind,
            subject=subject,
            choice=choice,
            target_revision=target_revision,
            created_at=format_time(event_time),
        )
        after = _encode_queue(
            generation=generation,
            previous_revision=header.previous_revision if header is not None else None,
            disposition_head_revision=(
                header.disposition_head_revision if header is not None else None
            ),
            opened_at=(header.opened_at if header is not None else format_time(event_time)),
            events=(*events, event),
        )
        if len(after) > MAX_QUEUE_BYTES:
            raise ValidationError("control queue exceeds its size bound")
        self._write_verified(
            store,
            ".gsv/control/queue.jsonl",
            after,
            "control queue",
            expected_current=stored,
        )
        if marker != "initialized":
            self._finalize_marker(
                store,
                expected_queue=after,
                expected_current=(_MARKER_PREPARING if marker == "preparing" else None),
            )
        return ControlSnapshot(
            events=(*events, event),
            generation=generation,
            previous_revision=(header.previous_revision if header is not None else None),
            disposition_head_revision=(
                header.disposition_head_revision if header is not None else None
            ),
            revision=sha256_bytes(after),
        )

    def _rotate_closed_with_store(
        self,
        store: PinnedPathRoot,
        *,
        expected_revision: str,
        closed_event_ids: frozenset[str],
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Rotate while the caller holds ``locked_control_store``."""

        if not store.directory_exists(".gsv/control"):
            raise ControlStorageError("control queue directory is missing")
        with store.bind_directory(".gsv/control"):
            return self._rotate_closed_in_bound_store(
                store,
                expected_revision=expected_revision,
                closed_event_ids=closed_event_ids,
                observed_at=observed_at,
            )

    def _rotate_closed_in_bound_store(
        self,
        store: PinnedPathRoot,
        *,
        expected_revision: str,
        closed_event_ids: frozenset[str],
        observed_at: datetime | None,
    ) -> dict[str, Any]:
        clean_expected = _revision(expected_revision, "expected_revision")
        marker = self._marker_state(store)
        before = self._read_bytes(store, allow_missing=False)
        before_revision = sha256_bytes(before)
        if before_revision != clean_expected:
            raise ConflictError("control queue changed; reload it before archiving closed events")
        try:
            header, events = _parse_queue_document(before)
        except ValidationError as exc:
            raise ControlStorageError(f"stored control queue is invalid: {exc}") from exc
        if header is None:
            raise ControlStorageError("initialized control queue has no generation header")
        self._validate_lineage(store, header)
        generation = header.generation
        if generation >= MAX_GENERATIONS:
            raise ValidationError(
                "control queue reached its audited generation limit; preserve this vault and "
                "start a new vault until authenticated compaction is supported"
            )
        event_ids = frozenset(event.event_id for event in events)
        if not events:
            raise ValidationError("control queue has no events to archive")
        if event_ids != closed_event_ids:
            raise ConflictError("control queue still contains undispositioned events")
        store.ensure_directory(".gsv/control/archive")
        archive_dir = self.path.parent / "archive"
        archive = archive_dir / f"queue-{generation}-{before_revision}.jsonl"
        archive_relative = archive.relative_to(self.vault_root)
        existing = store.read_regular_file(
            archive_relative,
            label="control queue archive",
            max_bytes=MAX_QUEUE_BYTES,
            missing_ok=True,
        )
        if existing is not None:
            if existing != before:
                raise ValidationError("control queue archive path already contains other data")
        else:
            self._write_verified(
                store,
                archive_relative,
                before,
                "control queue archive",
                expected_current=None,
            )
        rotated_at = format_time(observed_at or datetime.now(UTC))
        after = _encode_queue(
            generation=generation + 1,
            previous_revision=before_revision,
            disposition_head_revision=None,
            opened_at=rotated_at,
            events=(),
        )
        self._write_verified(
            store,
            ".gsv/control/queue.jsonl",
            after,
            "control queue",
            expected_current=before,
        )
        if marker != "initialized":
            self._finalize_marker(
                store,
                expected_queue=after,
                expected_current=(_MARKER_PREPARING if marker == "preparing" else None),
            )
        return {
            "archive": archive.relative_to(self.vault_root).as_posix(),
            "archived_events": len(events),
            "generation": generation + 1,
            "previous_revision": before_revision,
            "revision": sha256_bytes(after),
            "rotated_at": rotated_at,
        }

    def _bind_disposition_head_with_store(
        self,
        store: PinnedPathRoot,
        *,
        expected_revision: str,
        expected_generation: int,
        expected_head_revision: str | None,
        new_head_revision: str,
    ) -> ControlSnapshot:
        """Bind one exact operation head while the caller holds the control lock."""

        if not store.directory_exists(".gsv/control"):
            raise ControlStorageError("control queue directory is missing")
        with store.bind_directory(".gsv/control"):
            return self._bind_disposition_head_in_bound_store(
                store,
                expected_revision=expected_revision,
                expected_generation=expected_generation,
                expected_head_revision=expected_head_revision,
                new_head_revision=new_head_revision,
            )

    def _bind_disposition_head_in_bound_store(
        self,
        store: PinnedPathRoot,
        *,
        expected_revision: str,
        expected_generation: int,
        expected_head_revision: str | None,
        new_head_revision: str,
    ) -> ControlSnapshot:
        clean_expected = _revision(expected_revision, "expected_revision")
        clean_expected_head = _optional_revision(expected_head_revision)
        clean_new_head = _revision(new_head_revision, "new_head_revision")
        before = self._read_bytes(store, allow_missing=False)
        before_revision = sha256_bytes(before)
        if before_revision != clean_expected:
            raise ConflictError("control queue changed before disposition head binding")
        try:
            header, events = _parse_queue_document(before)
        except ValidationError as exc:
            raise ControlStorageError(f"stored control queue is invalid: {exc}") from exc
        if header is None or header.generation != expected_generation:
            raise ControlStorageError("control queue generation changed before head binding")
        self._validate_lineage(store, header)
        if header.disposition_head_revision != clean_expected_head:
            raise ConflictError("control queue disposition head binding changed")
        after = _encode_queue(
            generation=header.generation,
            previous_revision=header.previous_revision,
            disposition_head_revision=clean_new_head,
            opened_at=header.opened_at,
            events=events,
        )
        self._write_verified(
            store,
            ".gsv/control/queue.jsonl",
            after,
            "control queue",
            expected_current=before,
        )
        return ControlSnapshot(
            events=events,
            generation=header.generation,
            previous_revision=header.previous_revision,
            disposition_head_revision=clean_new_head,
            revision=sha256_bytes(after),
        )

    def _read_bytes(self, store: PinnedPathRoot, *, allow_missing: bool) -> bytes:
        stored = self._read_optional_bytes(store)
        if stored is None:
            if allow_missing:
                return b""
            raise ControlStorageError("control queue disappeared after initialization")
        return stored

    def _read_optional_bytes(self, store: PinnedPathRoot) -> bytes | None:
        try:
            stored = store.read_regular_file(
                ".gsv/control/queue.jsonl",
                label="control queue",
                max_bytes=MAX_QUEUE_BYTES,
                missing_ok=True,
            )
        except ValidationError as exc:
            raise ControlStorageError(str(exc)) from exc
        if stored is None:
            return None
        if not stored:
            raise ControlStorageError("stored control queue is empty")
        return stored

    def _marker_state(self, store: PinnedPathRoot) -> str | None:
        try:
            stored = store.read_regular_file(
                _MARKER_RELATIVE,
                label="control queue initialization marker",
                max_bytes=_MAX_MARKER_BYTES,
                missing_ok=True,
            )
        except ValidationError as exc:
            raise ControlStorageError(str(exc)) from exc
        if stored is None:
            return None
        if stored == _MARKER_PREPARING:
            return "preparing"
        if stored == _MARKER_INITIALIZED:
            return "initialized"
        raise ControlStorageError("control queue initialization marker is invalid")

    def _write_preparing_marker(self, store: PinnedPathRoot) -> None:
        try:
            store.compare_and_swap_regular_file(
                _MARKER_RELATIVE,
                expected=None,
                replacement=_MARKER_PREPARING,
                label="control queue initialization marker",
                max_bytes=_MAX_MARKER_BYTES,
            )
        except DurablePublishError as exc:
            if exc.outcome is not PublishOutcome.COMMITTED:
                raise
            visible = self._read_marker_bytes(store)
            if visible != _MARKER_PREPARING:
                raise DegradedIntegrityError(
                    "control queue preparation marker has an unknown visible state"
                ) from exc
            # A later successful queue publication fsyncs this same directory.
        except OSError as exc:
            raise ControlStorageError("control queue preparation marker was not published") from exc

    def _finalize_marker(
        self,
        store: PinnedPathRoot,
        *,
        expected_queue: bytes,
        expected_current: bytes | None,
    ) -> None:
        try:
            self._write_verified(
                store,
                _MARKER_RELATIVE,
                _MARKER_INITIALIZED,
                "control queue initialization marker",
                expected_current=expected_current,
            )
        except Exception as exc:
            visible = self._read_optional_bytes(store)
            if visible != expected_queue:
                raise DegradedIntegrityError(
                    "control queue initialization failed after an unknown queue mutation"
                ) from exc
            raise MutationCommittedError(
                "control queue mutation was committed, but initialization durability could "
                "not be confirmed"
            ) from exc

    def _read_marker_bytes(self, store: PinnedPathRoot) -> bytes | None:
        try:
            return store.read_regular_file(
                _MARKER_RELATIVE,
                label="control queue initialization marker",
                max_bytes=_MAX_MARKER_BYTES,
                missing_ok=True,
            )
        except ValidationError as exc:
            raise ControlStorageError(str(exc)) from exc

    def _write_verified(
        self,
        store: PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        label: str,
        *,
        expected_current: bytes | None,
    ) -> None:
        try:
            store.compare_and_swap_regular_file(
                relative,
                expected=expected_current,
                replacement=content,
                label=label,
                max_bytes=MAX_QUEUE_BYTES,
            )
        except DurablePublishError as exc:
            if exc.outcome is PublishOutcome.UNPUBLISHED:
                raise ControlStorageError(f"{label} was not published") from exc
            try:
                visible = store.read_regular_file(
                    relative,
                    label=label,
                    max_bytes=MAX_QUEUE_BYTES,
                    missing_ok=True,
                )
            except ValidationError as read_exc:
                raise DegradedIntegrityError(
                    f"{label} publication has an unknown visible state"
                ) from read_exc
            if visible == content:
                raise MutationCommittedError(
                    f"{label} is visible, but directory durability is unconfirmed"
                ) from exc
            raise DegradedIntegrityError(
                f"{label} publication has an unknown visible state"
            ) from exc
        except OSError as exc:
            raise ControlStorageError(f"{label} was not published") from exc

    def _validate_lineage(
        self,
        store: PinnedPathRoot,
        header: _QueueHeader | None,
        *,
        max_depth: int | None = None,
    ) -> None:
        if header is None:
            return
        current = header
        seen: set[str] = set()
        depth = 0
        while current.generation > 0:
            if max_depth is not None and depth >= max_depth:
                break
            assert current.previous_revision is not None
            if current.previous_revision in seen:
                raise ControlStorageError("control queue archive lineage contains a cycle")
            seen.add(current.previous_revision)
            archive = (
                f".gsv/control/archive/queue-{current.generation - 1}-"
                f"{current.previous_revision}.jsonl"
            )
            try:
                previous = store.read_regular_file(
                    archive,
                    label="previous control queue generation",
                    max_bytes=MAX_QUEUE_BYTES,
                    missing_ok=False,
                )
            except ValidationError as exc:
                raise ControlStorageError(
                    "control queue generation has no valid archived predecessor"
                ) from exc
            assert previous is not None
            if sha256_bytes(previous) != current.previous_revision:
                raise ControlStorageError("control queue predecessor archive hash does not match")
            try:
                previous_header, previous_events = _parse_queue_document(previous)
            except ValidationError as exc:
                raise ControlStorageError("control queue predecessor archive is invalid") from exc
            if (
                previous_header is None
                or previous_header.generation != current.generation - 1
                or not previous_events
            ):
                raise ControlStorageError("control queue predecessor archive has invalid lineage")
            current = previous_header
            depth += 1


def event_dict(event: ControlEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload["kind"] = event.kind.value
    return payload


def _empty_snapshot() -> ControlSnapshot:
    return ControlSnapshot(
        events=(),
        generation=0,
        previous_revision=None,
        disposition_head_revision=None,
        revision=EMPTY_REVISION,
    )


def _vault_id_from_store(store: PinnedPathRoot) -> str:
    """Read the canonical logical vault identity through an already-pinned root."""

    try:
        encoded = store.read_regular_file(
            ".gsv/manifest.json",
            label="vault manifest",
            max_bytes=MAX_VAULT_MANIFEST_BYTES,
            missing_ok=False,
        )
        assert encoded is not None
        payload = parse_vault_manifest(encoded)
    except ValidationError as exc:
        raise ControlStorageError("control queue vault identity is unavailable") from exc
    return str(payload["vault_id"])


def _validate_vault_binding(store: PinnedPathRoot, expected_vault_id: str) -> str:
    """Bind an authenticated writer to the logical vault it started with."""

    actual_vault_id = _vault_id_from_store(store)
    if actual_vault_id != expected_vault_id:
        raise ControlStorageError("control queue vault identity changed")
    return actual_vault_id


def _validate_root_binding(root: Path, expected: tuple[int, int]) -> None:
    """Reject a control bearer after its startup vault directory is replaced."""

    try:
        metadata = os.lstat(root)
    except OSError as exc:
        raise ControlStorageError("control queue vault root is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (int(metadata.st_dev), int(metadata.st_ino)) != expected
    ):
        raise ControlStorageError("control queue vault root changed")


def _parse_queue_document(
    stored: bytes,
) -> tuple[_QueueHeader | None, tuple[ControlEvent, ...]]:
    if not stored:
        return None, ()
    if not stored.endswith(b"\n"):
        raise ValidationError("control queue ends with a partial event")
    lines = stored.splitlines(keepends=True)
    try:
        header_payload = json.loads(lines[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("control queue generation header is invalid JSON") from exc
    if not isinstance(header_payload, dict) or set(header_payload) != _HEADER_KEYS:
        raise ValidationError("control queue generation header has an unsupported shape")
    if header_payload.get("record_type") != "generation":
        raise ValidationError("control queue generation header has an unsupported record type")
    if header_payload.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise ValidationError("control queue generation header has an unsupported version")
    generation = header_payload.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or generation > MAX_GENERATIONS
    ):
        raise ValidationError("control queue generation is invalid")
    previous = header_payload.get("previous_revision")
    if generation == 0:
        if previous is not None:
            raise ValidationError("initial control queue generation cannot name a predecessor")
    else:
        previous = _revision(previous, "previous_revision")
    disposition_head_revision = header_payload.get("disposition_head_revision")
    if disposition_head_revision is not None:
        disposition_head_revision = _revision(
            disposition_head_revision,
            "disposition_head_revision",
        )
    opened_at = header_payload.get("opened_at")
    if not isinstance(opened_at, str):
        raise ValidationError("control queue generation timestamp is invalid")
    try:
        opened_at = stored_time(opened_at, "control queue generation timestamp")
    except ValidationError as exc:
        raise ValidationError("control queue generation timestamp is invalid") from exc
    event_count = header_payload.get("event_count")
    if (
        not isinstance(event_count, int)
        or isinstance(event_count, bool)
        or event_count < 0
        or event_count > MAX_EVENTS
    ):
        raise ValidationError("control queue event count exceeds its bounded limit")
    events_digest = _revision(header_payload.get("events_digest"), "events_digest")
    event_bytes = b"".join(lines[1:])
    if sha256_bytes(event_bytes) != events_digest:
        raise ValidationError("control queue event digest does not match its stored events")

    header = _QueueHeader(
        generation=generation,
        previous_revision=previous,
        disposition_head_revision=disposition_head_revision,
        opened_at=opened_at,
        event_count=event_count,
        events_digest=events_digest,
    )
    events: list[ControlEvent] = []
    for number, raw in enumerate(lines[1:], start=1):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"control queue event {number} is invalid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "choice",
            "created_at",
            "event_id",
            "kind",
            "schema_version",
            "source",
            "subject",
            "target_revision",
        }:
            raise ValidationError(f"control queue event {number} has an unsupported shape")
        if payload["schema_version"] != CONTROL_SCHEMA_VERSION:
            raise ValidationError(f"control queue event {number} has an unsupported version")
        if payload["source"] != "bridge":
            raise ValidationError(f"control queue event {number} has an unsupported source")
        event_id = payload["event_id"]
        try:
            if not isinstance(event_id, str) or str(uuid.UUID(event_id)) != event_id:
                raise ValueError
        except ValueError as exc:
            raise ValidationError(f"control queue event {number} has an invalid ID") from exc
        created_at = payload["created_at"]
        if not isinstance(created_at, str):
            raise ValidationError(f"control queue event {number} has an invalid timestamp")
        try:
            created_at = stored_time(created_at, "control queue event timestamp")
        except ValidationError as exc:
            raise ValidationError(f"control queue event {number} has an invalid timestamp") from exc
        kind = _control_kind(payload["kind"])
        events.append(
            ControlEvent(
                schema_version=CONTROL_SCHEMA_VERSION,
                event_id=event_id,
                kind=kind,
                subject=_subject_reference(payload["subject"], kind),
                choice=_bounded_text(payload["choice"], "choice", max_bytes=MAX_CONTROL_TEXT_BYTES),
                target_revision=_optional_revision(payload["target_revision"]),
                created_at=created_at,
            )
        )
        if len(events) > MAX_EVENTS:
            raise ValidationError("control queue exceeds its bounded event count")
    if len(events) != event_count:
        raise ValidationError("control queue event count does not match its header")
    if len({event.event_id for event in events}) != len(events):
        raise ValidationError("control queue contains duplicate event IDs")
    return header, tuple(events)


def _encode_queue(
    *,
    generation: int,
    previous_revision: str | None,
    disposition_head_revision: str | None,
    opened_at: str,
    events: tuple[ControlEvent, ...],
) -> bytes:
    if len(events) > MAX_EVENTS:
        raise ValidationError("control queue exceeds its bounded event count")
    event_bytes = b"".join(_event_line(event) for event in events)
    header = {
        "disposition_head_revision": disposition_head_revision,
        "event_count": len(events),
        "events_digest": sha256_bytes(event_bytes),
        "generation": generation,
        "opened_at": opened_at,
        "previous_revision": previous_revision,
        "record_type": "generation",
        "schema_version": CONTROL_SCHEMA_VERSION,
    }
    encoded = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    return encoded + event_bytes


def _event_line(event: ControlEvent) -> bytes:
    return (
        json.dumps(
            event_dict(event),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _control_kind(value: object) -> ControlKind:
    if not isinstance(value, str):
        raise ValidationError("kind must be a string")
    try:
        return ControlKind(value)
    except ValueError as exc:
        raise ValidationError("kind is not an allowed Bridge control event") from exc


def _subject_reference(value: object, kind: ControlKind) -> str:
    subject = _bounded_text(value, "subject", max_bytes=512)
    matched = _SUBJECT_REFERENCE.fullmatch(subject)
    if matched is None:
        raise ValidationError("subject must be a bounded namespaced reference")
    namespace = matched.group("namespace")
    allowed = {
        ControlKind.SETUP_CHOICE: frozenset({"source"}),
        ControlKind.APPROVAL: frozenset({"operation"}),
        ControlKind.CORRECTION: frozenset({"mind", "record"}),
        ControlKind.UNDO_REQUEST: frozenset({"operation"}),
    }[kind]
    if namespace not in allowed:
        expected = " or ".join(f"{prefix}:" for prefix in sorted(allowed))
        raise ValidationError(f"{kind.value} subject must use {expected}")
    return subject


def _bounded_text(value: object, name: str, *, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned or len(cleaned.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{name} is empty, unsafe, or exceeds its size bound")
    return cleaned


def _revision(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_REVISION.fullmatch(value) is None:
        raise ValidationError(f"{name} must be a SHA-256 revision")
    return value.lower()


def _optional_revision(value: object) -> str | None:
    if value is None:
        return None
    return _revision(value, "target_revision")


def _uuid(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a UUID")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValidationError(f"{name} must be a UUID") from exc
    if normalized != value:
        raise ValidationError(f"{name} must be a canonical UUID")
    return value
