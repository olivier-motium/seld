"""Durable accept/reject dispositions for Bridge control intents."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from continuity_kernel.atomic import (
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    sha256_bytes,
)
from continuity_kernel.control_queue import (
    EMPTY_REVISION,
    MAX_EVENTS,
    MAX_QUEUE_BYTES,
    ControlEvent,
    ControlQueue,
    ControlSnapshot,
    ControlStorageError,
    _parse_queue_document,
    _validate_root_binding,
    _validate_vault_binding,
    _vault_id_from_store,
    control_store,
    event_dict,
    locked_control_store,
)
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    ValidationError,
)
from continuity_kernel.records import format_time, stored_time

DISPOSITION_SCHEMA_VERSION: Final = 1
MAX_DISPOSITION_BYTES: Final = 4 * MAX_QUEUE_BYTES
MAX_DISPOSITIONS: Final = MAX_EVENTS
EMPTY_DISPOSITION_REVISION: Final = EMPTY_REVISION
_MAX_DISPOSITION_MARKER_BYTES: Final = 1024
_SAFE_ACTOR = re.compile(r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFE_RESULT_REF = re.compile(r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9._:/-]*$")
MAX_RESULT_REF_BYTES: Final = 512


class DispositionDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ControlDisposition:
    schema_version: int
    disposition_id: str
    event_id: str
    event_digest: str
    decision: DispositionDecision
    actor_ref: str
    reason_code: str
    result_ref: str | None
    acknowledged_at: str


@dataclass(frozen=True)
class ArchivedOperationGeneration:
    """One bounded closed generation, linked to the current queue by revision."""

    queue_revision: str
    queue_generation: int
    previous_queue_revision: str | None
    disposition_revision: str
    decided: tuple[tuple[ControlEvent, ControlDisposition], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decided": [
                {"disposition": disposition_dict(disposition), "event": event_dict(event)}
                for event, disposition in self.decided
            ],
            "disposition_revision": self.disposition_revision,
            "previous_queue_revision": self.previous_queue_revision,
            "queue_generation": self.queue_generation,
            "queue_revision": self.queue_revision,
        }


@dataclass(frozen=True)
class OperationSnapshot:
    vault_id: str
    queue_revision: str
    queue_generation: int
    previous_queue_revision: str | None
    disposition_revision: str
    pending: tuple[ControlEvent, ...]
    decided: tuple[tuple[ControlEvent, ControlDisposition], ...]
    archived: tuple[ArchivedOperationGeneration, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "archived": [generation.to_dict() for generation in self.archived],
            "decided": [
                {"disposition": disposition_dict(disposition), "event": event_dict(event)}
                for event, disposition in self.decided
            ],
            "disposition_revision": self.disposition_revision,
            "pending": [event_dict(event) for event in self.pending],
            "previous_queue_revision": self.previous_queue_revision,
            "queue_generation": self.queue_generation,
            "queue_revision": self.queue_revision,
            "vault_id": self.vault_id,
        }


@dataclass(frozen=True)
class OperationBinding:
    """Logical and physical vault identity captured for one operation process."""

    vault_id: str
    root_identity: tuple[int, int]


def capture_operation_binding(vault_root: Path | str) -> OperationBinding:
    """Capture one exact vault identity without creating operation state."""

    root = Path(vault_root).expanduser().resolve()
    with control_store(root) as store, store.exclusive_root_lock():
        try:
            metadata = os.lstat(root)
        except OSError as exc:
            raise ControlStorageError("operation vault root is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ControlStorageError("operation vault root is not a stable directory")
        root_identity = (int(metadata.st_dev), int(metadata.st_ino))
        _validate_root_binding(root, root_identity)
        return OperationBinding(
            vault_id=_vault_id_from_store(store),
            root_identity=root_identity,
        )


@dataclass(frozen=True)
class _DispositionState:
    revision: str
    byte_count: int
    disposition_count: int


@dataclass(frozen=True)
class _DispositionHead:
    generation: int
    state: str
    previous_head_revision: str | None
    previous_head_parent_revision: str | None = None
    current: _DispositionState | None = None
    previous: _DispositionState | None = None
    proposed: _DispositionState | None = None
    proposed_head_revision: str | None = None


class OperationLedger:
    """Acknowledge each live Bridge event exactly once without executing it."""

    def __init__(self, vault_root: Path | str):
        self.vault_root = Path(vault_root).expanduser().resolve()
        self.queue = ControlQueue(self.vault_root)

    @property
    def path(self) -> Path:
        """Return the disposition log for the queue's current generation."""

        return self._path_for_generation(self.queue.snapshot().generation)

    def snapshot(
        self,
        *,
        expected_vault_id: str | None = None,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> OperationSnapshot:
        clean_vault_id = (
            _uuid(expected_vault_id, "expected vault ID") if expected_vault_id is not None else None
        )
        with control_store(self.vault_root) as store, store.exclusive_root_lock():
            if expected_root_identity is not None:
                _validate_root_binding(self.vault_root, expected_root_identity)
            actual_vault_id = _vault_id_from_store(store)
            if clean_vault_id is not None:
                _validate_vault_binding(store, clean_vault_id)
            if not store.directory_exists(".gsv/control"):
                return self._snapshot_with_store(store, vault_id=actual_vault_id)
            with store.bind_directory(".gsv/control"):
                return self._snapshot_with_store(store, vault_id=actual_vault_id)

    def decide(
        self,
        *,
        event_id: object,
        decision: object,
        actor_ref: object,
        reason_code: object,
        expected_queue_revision: object,
        expected_disposition_revision: object,
        expected_vault_id: object = None,
        expected_root_identity: tuple[int, int] | None = None,
        result_ref: object = None,
        observed_at: datetime | None = None,
    ) -> OperationSnapshot:
        clean_event_id = _uuid(event_id, "event ID")
        clean_decision = _decision(decision)
        clean_actor = _actor(actor_ref)
        clean_reason = _reason(reason_code)
        clean_queue_revision = _revision(expected_queue_revision, "expected queue revision")
        clean_disposition_revision = _revision(
            expected_disposition_revision, "expected disposition revision"
        )
        clean_result = _optional_result_ref(result_ref)
        clean_vault_id = (
            _uuid(expected_vault_id, "expected vault ID") if expected_vault_id is not None else None
        )
        if clean_vault_id is not None or expected_root_identity is not None:
            self._preflight_binding(
                expected_vault_id=clean_vault_id,
                expected_root_identity=expected_root_identity,
            )
        with locked_control_store(
            self.vault_root,
            expected_vault_id=clean_vault_id,
            expected_root_identity=expected_root_identity,
        ) as store:
            if expected_root_identity is not None:
                _validate_root_binding(self.vault_root, expected_root_identity)
            actual_vault_id = _vault_id_from_store(store)
            if clean_vault_id is not None:
                _validate_vault_binding(store, clean_vault_id)
            if not store.directory_exists(".gsv/control"):
                raise ControlStorageError("control queue directory is missing")
            with store.bind_directory(".gsv/control"):
                return self._decide_in_bound_store(
                    store,
                    event_id=clean_event_id,
                    decision=clean_decision,
                    actor_ref=clean_actor,
                    reason_code=clean_reason,
                    queue_revision=clean_queue_revision,
                    disposition_revision=clean_disposition_revision,
                    vault_id=actual_vault_id,
                    result_ref=clean_result,
                    observed_at=observed_at,
                )

    def _decide_in_bound_store(
        self,
        store: PinnedPathRoot,
        *,
        event_id: str,
        decision: DispositionDecision,
        actor_ref: str,
        reason_code: str,
        queue_revision: str,
        disposition_revision: str,
        vault_id: str,
        result_ref: str | None,
        observed_at: datetime | None,
    ) -> OperationSnapshot:
        queue = self.queue._snapshot_with_store(
            store,
            allow_missing=False,
            full_lineage=True,
        )
        self._read_archived_generations(store, queue, full_lineage=True)
        before = self._view_current_dispositions(store, queue)
        if queue.revision != queue_revision:
            raise ConflictError("control queue changed; reload operations before disposition")
        events = {event.event_id: event for event in queue.events}
        event = events.get(event_id)
        if event is None:
            raise ValidationError("control event is not present in the live queue")
        if sha256_bytes(before) != disposition_revision:
            raise ConflictError("operation dispositions changed; reload before disposition")
        dispositions = _parse_dispositions(before)
        if any(item.event_id == event_id for item in dispositions):
            raise ConflictError("control event already has a durable disposition")
        if len(dispositions) >= MAX_DISPOSITIONS:
            raise ValidationError("operation disposition log reached its bounded limit")
        disposition = ControlDisposition(
            schema_version=DISPOSITION_SCHEMA_VERSION,
            disposition_id=str(uuid.uuid4()),
            event_id=event_id,
            event_digest=_event_digest(event),
            decision=decision,
            actor_ref=actor_ref,
            reason_code=reason_code,
            result_ref=result_ref,
            acknowledged_at=format_time(observed_at or datetime.now(UTC)),
        )
        line = (
            json.dumps(
                disposition_dict(disposition),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        after = before + line
        if len(after) > MAX_DISPOSITION_BYTES:
            raise ValidationError("operation disposition log exceeds its size bound")
        recovered_queue, recovered_before = self._recover_current_dispositions(store, queue)
        if recovered_before != before:
            raise DegradedIntegrityError(
                "operation disposition recovery changed the observed logical state"
            )
        self._write_dispositions(
            store,
            recovered_queue,
            before=before,
            after=after,
        )
        return self._snapshot_with_store(store, vault_id=vault_id)

    def archive_closed(
        self,
        *,
        expected_queue_revision: object,
        expected_disposition_revision: object,
        expected_vault_id: object = None,
        expected_root_identity: tuple[int, int] | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        queue_revision = _revision(expected_queue_revision, "expected queue revision")
        expected_disposition_revision = _revision(
            expected_disposition_revision, "expected disposition revision"
        )
        clean_vault_id = (
            _uuid(expected_vault_id, "expected vault ID") if expected_vault_id is not None else None
        )
        if clean_vault_id is not None or expected_root_identity is not None:
            self._preflight_binding(
                expected_vault_id=clean_vault_id,
                expected_root_identity=expected_root_identity,
            )
        with locked_control_store(
            self.vault_root,
            expected_vault_id=clean_vault_id,
            expected_root_identity=expected_root_identity,
        ) as store:
            if expected_root_identity is not None:
                _validate_root_binding(self.vault_root, expected_root_identity)
            actual_vault_id = _vault_id_from_store(store)
            if clean_vault_id is not None:
                _validate_vault_binding(store, clean_vault_id)
            if not store.directory_exists(".gsv/control"):
                raise ControlStorageError("control queue directory is missing")
            with store.bind_directory(".gsv/control"):
                queue = self.queue._snapshot_with_store(
                    store,
                    allow_missing=False,
                    full_lineage=True,
                )
                encoded = self._view_current_dispositions(store, queue)
                observed_disposition_revision, pending, decided = _link_dispositions(
                    queue.events,
                    encoded,
                )
                snapshot = OperationSnapshot(
                    vault_id=actual_vault_id,
                    queue_revision=queue.revision,
                    queue_generation=queue.generation,
                    previous_queue_revision=queue.previous_revision,
                    disposition_revision=observed_disposition_revision,
                    pending=pending,
                    decided=decided,
                    archived=self._read_archived_generations(
                        store,
                        queue,
                        full_lineage=True,
                    ),
                )
                if snapshot.queue_revision != queue_revision:
                    raise ConflictError("control queue changed; reload before archive recovery")
                if snapshot.disposition_revision != expected_disposition_revision:
                    raise ConflictError(
                        "operation dispositions changed; reload before archive recovery"
                    )
                if snapshot.pending:
                    raise ConflictError("control queue still contains pending events")
                recovered_queue, recovered_encoded = self._recover_current_dispositions(
                    store,
                    queue,
                )
                if recovered_encoded != encoded:
                    raise DegradedIntegrityError(
                        "operation disposition recovery changed the observed logical state"
                    )
                closed = frozenset(event.event_id for event, _ in snapshot.decided)
                disposition_archive = self._path_for_generation(snapshot.queue_generation)
                result = self.queue._rotate_closed_with_store(
                    store,
                    expected_revision=recovered_queue.revision,
                    closed_event_ids=closed,
                    observed_at=observed_at,
                )
                rotated = self.queue._snapshot_with_store(
                    store,
                    allow_missing=False,
                )
                try:
                    anchored, _ = self._recover_current_dispositions(store, rotated)
                except Exception as exc:
                    raise MutationCommittedError(
                        "control queue was archived, but its successor disposition head could "
                        "not be anchored"
                    ) from exc
                return {
                    **result,
                    "disposition_archive": disposition_archive.relative_to(
                        self.vault_root
                    ).as_posix(),
                    "disposition_revision": observed_disposition_revision,
                    "revision": anchored.revision,
                    "vault_id": actual_vault_id,
                }

    def _preflight_binding(
        self,
        *,
        expected_vault_id: str | None,
        expected_root_identity: tuple[int, int] | None,
    ) -> None:
        """Reject a stale logical or physical vault before creating lock state."""

        with control_store(self.vault_root) as store, store.exclusive_root_lock():
            if expected_root_identity is not None:
                _validate_root_binding(self.vault_root, expected_root_identity)
            if expected_vault_id is not None:
                _validate_vault_binding(store, expected_vault_id)

    def _snapshot_with_store(
        self,
        store: PinnedPathRoot,
        *,
        vault_id: str | None = None,
        full_lineage: bool = False,
    ) -> OperationSnapshot:
        queue = self.queue._snapshot_with_store(
            store,
            allow_missing=True,
            full_lineage=full_lineage,
        )
        encoded = self._view_current_dispositions(store, queue)
        disposition_revision, pending, decided = _link_dispositions(queue.events, encoded)
        archived = self._read_archived_generations(store, queue, full_lineage=full_lineage)
        return OperationSnapshot(
            vault_id=vault_id or _vault_id_from_store(store),
            queue_revision=queue.revision,
            queue_generation=queue.generation,
            previous_queue_revision=queue.previous_revision,
            disposition_revision=disposition_revision,
            pending=pending,
            decided=decided,
            archived=archived,
        )

    def _read_archived_generations(
        self,
        store: PinnedPathRoot,
        queue: ControlSnapshot,
        *,
        full_lineage: bool,
    ) -> tuple[ArchivedOperationGeneration, ...]:
        if queue.generation == 0:
            return ()
        expected_generation = queue.generation - 1
        previous_revision = queue.previous_revision
        visible: list[ArchivedOperationGeneration] = []
        while expected_generation >= 0:
            if previous_revision is None:
                raise ControlStorageError("control queue generation has no predecessor revision")
            archive_relative = Path(
                f".gsv/control/archive/queue-{expected_generation}-{previous_revision}.jsonl"
            )
            try:
                archive = store.read_regular_file(
                    archive_relative,
                    label="previous control queue generation",
                    max_bytes=MAX_QUEUE_BYTES,
                    missing_ok=False,
                )
            except ValidationError as exc:
                raise ControlStorageError(
                    "control queue generation has no valid archived predecessor"
                ) from exc
            assert archive is not None
            if sha256_bytes(archive) != previous_revision:
                raise ControlStorageError("control queue predecessor archive hash does not match")
            try:
                header, events = _parse_queue_document(archive)
            except ValidationError as exc:
                raise ControlStorageError("control queue predecessor archive is invalid") from exc
            if header is None or header.generation != expected_generation or not events:
                raise ControlStorageError("control queue predecessor archive has invalid lineage")
            dispositions = self._read_archived_dispositions(
                store,
                expected_generation,
                expected_head_revision=header.disposition_head_revision,
            )
            disposition_revision, pending, decided = _link_dispositions(events, dispositions)
            if pending:
                raise ControlStorageError(
                    "archived control queue generation is missing durable dispositions"
                )
            if not visible:
                visible.append(
                    ArchivedOperationGeneration(
                        queue_revision=previous_revision,
                        queue_generation=expected_generation,
                        previous_queue_revision=header.previous_revision,
                        disposition_revision=disposition_revision,
                        decided=decided,
                    )
                )
            if not full_lineage:
                break
            previous_revision = header.previous_revision
            expected_generation -= 1
        return tuple(visible)

    def _path_for_generation(self, generation: int) -> Path:
        return self.vault_root / f".gsv/control/dispositions-{generation:016d}.jsonl"

    def _marker_path_for_generation(self, generation: int) -> Path:
        return self.vault_root / f".gsv/control/dispositions-{generation:016d}.head.jsonl"

    def _read_disposition_files(
        self,
        store: PinnedPathRoot,
        generation: int,
    ) -> tuple[bytes | None, _DispositionHead | None]:
        relative = self._path_for_generation(generation).relative_to(self.vault_root)
        try:
            encoded = store.read_regular_file(
                relative,
                label="operation dispositions",
                max_bytes=MAX_DISPOSITION_BYTES,
                missing_ok=True,
            )
        except ValidationError as exc:
            raise ControlStorageError(str(exc)) from exc
        head = self._read_disposition_head(store, generation)
        return encoded, head

    def _view_current_dispositions(
        self,
        store: PinnedPathRoot,
        queue: ControlSnapshot,
    ) -> bytes:
        """Return the logical live disposition bytes without repairing or anchoring them."""

        encoded, head = self._read_disposition_files(store, queue.generation)
        if queue.revision == EMPTY_REVISION:
            if encoded is not None or head is not None:
                raise ControlStorageError(
                    "operation disposition state exists without a control queue"
                )
            return b""
        anchor = queue.disposition_head_revision
        if anchor is None:
            if encoded is not None:
                raise ControlStorageError(
                    "operation disposition log has no independently anchored head"
                )
            empty = _disposition_state(b"")
            if head is not None and (
                head.state != "initialized"
                or head.current != empty
                or head.previous_head_revision is not None
            ):
                raise ControlStorageError(
                    "unanchored operation disposition head is not the empty generation head"
                )
            return b""
        if head is None:
            raise ControlStorageError(
                "operation disposition head disappeared after queue anchoring"
            )
        content = encoded or b""
        actual = _disposition_state(content)
        if head.state == "initialized":
            assert head.current is not None
            if actual != head.current:
                if encoded is None and head.current.byte_count:
                    raise ControlStorageError(
                        "operation disposition log disappeared after initialization"
                    )
                raise ControlStorageError(
                    "operation disposition log does not match its cryptographic head"
                )
            head_revision = _disposition_head_revision(head)
            if head_revision == anchor:
                return content
            if head.previous_head_revision != anchor:
                raise ControlStorageError(
                    "operation disposition head does not descend from its queue anchor"
                )
            self._read_archived_generations(store, queue, full_lineage=True)
            return content

        assert head.previous is not None and head.proposed is not None
        if head.previous_head_revision != anchor:
            raise ControlStorageError(
                "operation disposition preparation does not match its queue anchor"
            )
        self._read_archived_generations(store, queue, full_lineage=True)
        if actual == head.previous:
            restored = _initialized_disposition_head(
                queue.generation,
                actual,
                previous_head_revision=head.previous_head_parent_revision,
            )
            if _disposition_head_revision(restored) != anchor:
                raise ControlStorageError(
                    "operation disposition preparation cannot restore its anchored head"
                )
            return content
        if actual == head.proposed:
            recovered = _initialized_disposition_head(
                queue.generation,
                actual,
                previous_head_revision=anchor,
            )
            if _disposition_head_revision(recovered) != head.proposed_head_revision:
                raise ControlStorageError(
                    "operation disposition preparation does not bind its proposed head"
                )
            return content
        raise ControlStorageError(
            "operation disposition recovery found a log outside its prepared states"
        )

    def _recover_current_dispositions(
        self,
        store: PinnedPathRoot,
        queue: ControlSnapshot,
    ) -> tuple[ControlSnapshot, bytes]:
        """Repair and anchor a previously validated logical view during an explicit mutation."""

        content = self._view_current_dispositions(store, queue)
        if queue.revision == EMPTY_REVISION:
            return queue, content
        encoded, head = self._read_disposition_files(store, queue.generation)
        anchor = queue.disposition_head_revision
        if anchor is None:
            empty = _disposition_state(b"")
            if head is None:
                head = _initialized_disposition_head(
                    queue.generation,
                    empty,
                    previous_head_revision=None,
                )
                self._write_disposition_head(store, head, expected=None)
            anchored = self._bind_current_head(store, queue, head)
            return anchored, content

        assert head is not None
        actual = _disposition_state(encoded or b"")
        if head.state == "initialized":
            if _disposition_head_revision(head) == anchor:
                return queue, content
            anchored = self._bind_current_head(store, queue, head)
            return anchored, content

        assert head.previous is not None and head.proposed is not None
        if actual == head.previous:
            restored = _initialized_disposition_head(
                queue.generation,
                actual,
                previous_head_revision=head.previous_head_parent_revision,
            )
            self._write_disposition_head(store, restored, expected=head)
            return queue, content
        recovered = _initialized_disposition_head(
            queue.generation,
            actual,
            previous_head_revision=anchor,
        )
        self._write_disposition_head(store, recovered, expected=head)
        anchored = self._bind_current_head(store, queue, recovered)
        return anchored, content

    def _read_archived_dispositions(
        self,
        store: PinnedPathRoot,
        generation: int,
        *,
        expected_head_revision: str | None,
    ) -> bytes:
        if expected_head_revision is None:
            raise ControlStorageError(
                "archived control queue generation has no disposition head anchor"
            )
        encoded, head = self._read_disposition_files(store, generation)
        if head is None:
            raise ControlStorageError(
                "archived operation disposition head disappeared after queue anchoring"
            )
        if head.state != "initialized":
            raise ControlStorageError("archived operation disposition head is not initialized")
        if _disposition_head_revision(head) != expected_head_revision:
            raise ControlStorageError(
                "archived operation disposition head does not match its queue anchor"
            )
        content = encoded or b""
        actual = _disposition_state(content)
        assert head.current is not None
        if actual != head.current:
            if encoded is None and head.current.byte_count:
                raise ControlStorageError(
                    "operation disposition log disappeared after initialization"
                )
            raise ControlStorageError(
                "operation disposition log does not match its cryptographic head"
            )
        return content

    def _bind_current_head(
        self,
        store: PinnedPathRoot,
        queue: ControlSnapshot,
        head: _DispositionHead,
    ) -> ControlSnapshot:
        return self.queue._bind_disposition_head_with_store(
            store,
            expected_revision=queue.revision,
            expected_generation=queue.generation,
            expected_head_revision=queue.disposition_head_revision,
            new_head_revision=_disposition_head_revision(head),
        )

    def _write_dispositions(
        self,
        store: PinnedPathRoot,
        queue: ControlSnapshot,
        *,
        before: bytes,
        after: bytes,
    ) -> None:
        relative = self._path_for_generation(queue.generation).relative_to(self.vault_root)
        current_queue, visible_before = self._recover_current_dispositions(store, queue)
        if current_queue != queue:
            raise ControlStorageError(
                "operation disposition queue anchor changed while preparing its mutation"
            )
        if visible_before != before:
            raise ControlStorageError(
                "operation disposition log changed while preparing its mutation"
            )
        previous = _disposition_state(before)
        proposed = _disposition_state(after)
        if (
            not after.startswith(before)
            or proposed.disposition_count != previous.disposition_count + 1
        ):
            raise ControlStorageError("operation disposition mutation is not one complete append")
        current_encoded, current_head = self._read_disposition_files(store, queue.generation)
        if (current_encoded or b"") != before:
            raise ControlStorageError(
                "operation disposition log changed before its prepared mutation"
            )
        if (
            current_head is None
            or current_head.state != "initialized"
            or _disposition_head_revision(current_head) != queue.disposition_head_revision
        ):
            raise ControlStorageError("operation disposition mutation has no current anchored head")
        current_head_revision = _disposition_head_revision(current_head)
        proposed_head = _initialized_disposition_head(
            queue.generation,
            proposed,
            previous_head_revision=current_head_revision,
        )
        preparing_head = _preparing_disposition_head(
            queue.generation,
            previous,
            proposed,
            previous_head_revision=current_head_revision,
            previous_head_parent_revision=current_head.previous_head_revision,
            proposed_head_revision=_disposition_head_revision(proposed_head),
        )
        self._write_preparing_disposition_head(
            store,
            preparing_head,
            expected=current_head,
        )
        publication_error: DurablePublishError | None = None
        try:
            store.compare_and_swap_regular_file(
                relative,
                expected=current_encoded,
                replacement=after,
                label="operation dispositions",
                max_bytes=MAX_DISPOSITION_BYTES,
            )
        except DurablePublishError as exc:
            if exc.outcome is PublishOutcome.UNPUBLISHED:
                raise ControlStorageError("operation disposition was not published") from exc
            try:
                visible = store.read_regular_file(
                    relative,
                    label="operation dispositions",
                    max_bytes=MAX_DISPOSITION_BYTES,
                    missing_ok=True,
                )
            except ValidationError as read_exc:
                raise DegradedIntegrityError(
                    "operation disposition publication has an unknown visible state"
                ) from read_exc
            if visible != after:
                raise DegradedIntegrityError(
                    "operation disposition publication has an unknown visible state"
                ) from exc
            publication_error = exc
        except OSError as exc:
            raise ControlStorageError("operation disposition was not published") from exc
        self._finalize_disposition_head(
            store,
            queue.generation,
            expected_dispositions=after,
            expected_state=proposed,
            expected_head=proposed_head,
            expected_current=preparing_head,
        )
        try:
            self._bind_current_head(store, queue, proposed_head)
        except MutationCommittedError:
            raise
        except Exception as exc:
            raise DegradedIntegrityError(
                "operation disposition is visible, but its queue anchor is incomplete"
            ) from exc
        if publication_error is not None:
            raise MutationCommittedError(
                "operation disposition is visible, but directory durability is unconfirmed"
            ) from publication_error

    def _read_disposition_head(
        self,
        store: PinnedPathRoot,
        generation: int,
    ) -> _DispositionHead | None:
        relative = self._marker_path_for_generation(generation).relative_to(self.vault_root)
        try:
            encoded = store.read_regular_file(
                relative,
                label="operation disposition head",
                max_bytes=_MAX_DISPOSITION_MARKER_BYTES,
                missing_ok=True,
            )
        except ValidationError as exc:
            raise ControlStorageError(str(exc)) from exc
        if encoded is None:
            return None
        try:
            head = _parse_disposition_head(encoded)
        except ValidationError as exc:
            raise ControlStorageError(f"operation disposition head is invalid: {exc}") from exc
        if head.generation != generation:
            raise ControlStorageError("operation disposition head names another generation")
        return head

    def _write_preparing_disposition_head(
        self,
        store: PinnedPathRoot,
        head: _DispositionHead,
        *,
        expected: _DispositionHead,
    ) -> None:
        # The final initialized head and disposition publication will fsync
        # the same directory before this mutation can report success.
        with suppress(MutationCommittedError):
            self._write_disposition_head(store, head, expected=expected)

    def _finalize_disposition_head(
        self,
        store: PinnedPathRoot,
        generation: int,
        *,
        expected_dispositions: bytes,
        expected_state: _DispositionState,
        expected_head: _DispositionHead,
        expected_current: _DispositionHead,
    ) -> None:
        try:
            self._write_disposition_head(store, expected_head, expected=expected_current)
            return
        except Exception as exc:
            relative = self._path_for_generation(generation).relative_to(self.vault_root)
            try:
                visible = store.read_regular_file(
                    relative,
                    label="operation dispositions",
                    max_bytes=MAX_DISPOSITION_BYTES,
                    missing_ok=True,
                )
                head = self._read_disposition_head(store, generation)
            except ValidationError as read_exc:
                raise DegradedIntegrityError(
                    "operation disposition initialization has an unknown visible state"
                ) from read_exc
            if visible != expected_dispositions:
                raise DegradedIntegrityError(
                    "operation disposition initialization has an unknown visible state"
                ) from exc
            if (
                head is not None
                and head.state == "initialized"
                and head.current == expected_state
                and head == expected_head
            ):
                raise MutationCommittedError(
                    "operation disposition is visible, but initialization durability is unconfirmed"
                ) from exc
            raise DegradedIntegrityError(
                "operation disposition is visible, but initialization is incomplete"
            ) from exc

    def _write_disposition_head(
        self,
        store: PinnedPathRoot,
        head: _DispositionHead,
        *,
        expected: _DispositionHead | None,
    ) -> None:
        relative = self._marker_path_for_generation(head.generation).relative_to(self.vault_root)
        content = _encode_disposition_head(head)
        try:
            store.compare_and_swap_regular_file(
                relative,
                expected=(_encode_disposition_head(expected) if expected is not None else None),
                replacement=content,
                label="operation disposition head",
                max_bytes=_MAX_DISPOSITION_MARKER_BYTES,
            )
        except DurablePublishError as exc:
            if exc.outcome is PublishOutcome.UNPUBLISHED:
                raise ControlStorageError("operation disposition head was not published") from exc
            try:
                visible = store.read_regular_file(
                    relative,
                    label="operation disposition head",
                    max_bytes=_MAX_DISPOSITION_MARKER_BYTES,
                    missing_ok=True,
                )
            except ValidationError as read_exc:
                raise DegradedIntegrityError(
                    "operation disposition head has an unknown visible state"
                ) from read_exc
            if visible == content:
                raise MutationCommittedError(
                    "operation disposition head is visible, but directory durability is unconfirmed"
                ) from exc
            raise DegradedIntegrityError(
                "operation disposition head has an unknown visible state"
            ) from exc
        except OSError as exc:
            raise ControlStorageError("operation disposition head was not published") from exc


def _disposition_state(encoded: bytes) -> _DispositionState:
    return _DispositionState(
        revision=sha256_bytes(encoded),
        byte_count=len(encoded),
        disposition_count=len(_parse_dispositions(encoded)),
    )


def _initialized_disposition_head(
    generation: int,
    state: _DispositionState,
    *,
    previous_head_revision: str | None,
) -> _DispositionHead:
    return _DispositionHead(
        generation=generation,
        state="initialized",
        previous_head_revision=previous_head_revision,
        current=state,
    )


def _preparing_disposition_head(
    generation: int,
    previous: _DispositionState,
    proposed: _DispositionState,
    *,
    previous_head_revision: str,
    previous_head_parent_revision: str | None,
    proposed_head_revision: str,
) -> _DispositionHead:
    return _DispositionHead(
        generation=generation,
        state="preparing",
        previous_head_revision=previous_head_revision,
        previous_head_parent_revision=previous_head_parent_revision,
        previous=previous,
        proposed=proposed,
        proposed_head_revision=proposed_head_revision,
    )


def _disposition_head_revision(head: _DispositionHead) -> str:
    return sha256_bytes(_encode_disposition_head(head))


def _encode_disposition_head(head: _DispositionHead) -> bytes:
    payload: dict[str, Any] = {
        "generation": head.generation,
        "previous_head_revision": head.previous_head_revision,
        "schema_version": DISPOSITION_SCHEMA_VERSION,
        "state": head.state,
    }
    if head.state == "initialized":
        if head.current is None:  # pragma: no cover - internal construction contract
            raise ValueError("initialized disposition head requires a current state")
        payload.update(_state_payload(head.current, prefix=""))
    elif head.state == "preparing":
        if head.previous is None or head.proposed is None:  # pragma: no cover
            raise ValueError("preparing disposition head requires both states")
        if head.previous_head_revision is None or head.proposed_head_revision is None:
            raise ValueError("preparing disposition head requires head revisions")
        payload["previous_head_parent_revision"] = head.previous_head_parent_revision
        payload["proposed_head_revision"] = head.proposed_head_revision
        payload.update(_state_payload(head.previous, prefix="previous_"))
        payload.update(_state_payload(head.proposed, prefix="proposed_"))
    else:  # pragma: no cover - internal construction contract
        raise ValueError("unsupported disposition head state")
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"


def _state_payload(state: _DispositionState, *, prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}byte_count": state.byte_count,
        f"{prefix}disposition_count": state.disposition_count,
        f"{prefix}revision": state.revision,
    }


def _parse_disposition_head(encoded: bytes) -> _DispositionHead:
    if not encoded.endswith(b"\n") or encoded.count(b"\n") != 1:
        raise ValidationError("head must contain one complete JSON record")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("head is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("head has an unsupported shape")
    if payload.get("schema_version") != DISPOSITION_SCHEMA_VERSION:
        raise ValidationError("head has an unsupported version")
    generation = payload.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValidationError("head generation is invalid")
    state = payload.get("state")
    if state == "initialized":
        expected = {
            "byte_count",
            "disposition_count",
            "generation",
            "previous_head_revision",
            "revision",
            "schema_version",
            "state",
        }
        if set(payload) != expected:
            raise ValidationError("initialized head has an unsupported shape")
        head = _initialized_disposition_head(
            generation,
            _state_from_payload(payload, prefix=""),
            previous_head_revision=_optional_head_revision(
                payload["previous_head_revision"],
                "previous head revision",
            ),
        )
        assert head.current is not None
        if (head.current.disposition_count == 0) != (head.previous_head_revision is None):
            raise ValidationError("initialized head ancestry is inconsistent")
    elif state == "preparing":
        expected = {
            "generation",
            "previous_byte_count",
            "previous_disposition_count",
            "previous_head_parent_revision",
            "previous_head_revision",
            "previous_revision",
            "proposed_byte_count",
            "proposed_disposition_count",
            "proposed_head_revision",
            "proposed_revision",
            "schema_version",
            "state",
        }
        if set(payload) != expected:
            raise ValidationError("preparing head has an unsupported shape")
        previous = _state_from_payload(payload, prefix="previous_")
        proposed = _state_from_payload(payload, prefix="proposed_")
        previous_head_revision = _revision(
            payload["previous_head_revision"],
            "previous head revision",
        )
        previous_head_parent_revision = _optional_head_revision(
            payload["previous_head_parent_revision"],
            "previous head parent revision",
        )
        proposed_head_revision = _revision(
            payload["proposed_head_revision"],
            "proposed head revision",
        )
        if (
            proposed.disposition_count != previous.disposition_count + 1
            or proposed.byte_count <= previous.byte_count
            or proposed.revision == previous.revision
        ):
            raise ValidationError("preparing head does not describe one append")
        if (previous.disposition_count == 0) != (previous_head_parent_revision is None):
            raise ValidationError("preparing head ancestry is inconsistent")
        restored = _initialized_disposition_head(
            generation,
            previous,
            previous_head_revision=previous_head_parent_revision,
        )
        proposed_head = _initialized_disposition_head(
            generation,
            proposed,
            previous_head_revision=previous_head_revision,
        )
        if _disposition_head_revision(restored) != previous_head_revision:
            raise ValidationError("preparing head does not bind its previous head")
        if _disposition_head_revision(proposed_head) != proposed_head_revision:
            raise ValidationError("preparing head does not bind its proposed head")
        head = _preparing_disposition_head(
            generation,
            previous,
            proposed,
            previous_head_revision=previous_head_revision,
            previous_head_parent_revision=previous_head_parent_revision,
            proposed_head_revision=proposed_head_revision,
        )
    else:
        raise ValidationError("head has an unsupported state")
    if _encode_disposition_head(head) != encoded:
        raise ValidationError("head is not canonically encoded")
    return head


def _optional_head_revision(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _revision(value, label)


def _state_from_payload(payload: dict[str, Any], *, prefix: str) -> _DispositionState:
    byte_count = payload[f"{prefix}byte_count"]
    disposition_count = payload[f"{prefix}disposition_count"]
    if (
        not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
        or byte_count > MAX_DISPOSITION_BYTES
    ):
        raise ValidationError("head byte count is invalid")
    if (
        not isinstance(disposition_count, int)
        or isinstance(disposition_count, bool)
        or disposition_count < 0
        or disposition_count > MAX_DISPOSITIONS
    ):
        raise ValidationError("head disposition count is invalid")
    revision = _revision(payload[f"{prefix}revision"], "head revision")
    if disposition_count == 0:
        if byte_count != 0 or revision != EMPTY_DISPOSITION_REVISION:
            raise ValidationError("empty disposition head state is inconsistent")
    elif byte_count == 0:
        raise ValidationError("nonempty disposition head state is inconsistent")
    return _DispositionState(
        revision=revision,
        byte_count=byte_count,
        disposition_count=disposition_count,
    )


def _link_dispositions(
    events: tuple[ControlEvent, ...],
    encoded: bytes,
) -> tuple[
    str,
    tuple[ControlEvent, ...],
    tuple[tuple[ControlEvent, ControlDisposition], ...],
]:
    dispositions = _parse_dispositions(encoded)
    events_by_id = {event.event_id: event for event in events}
    for disposition in dispositions:
        event = events_by_id.get(disposition.event_id)
        if event is None:
            raise ControlStorageError(
                "operation disposition references an event outside its queue generation"
            )
        if disposition.event_digest != _event_digest(event):
            raise ControlStorageError("operation disposition does not match its control event")
    by_event = {item.event_id: item for item in dispositions}
    pending = tuple(event for event in events if event.event_id not in by_event)
    decided = tuple(
        (event, by_event[event.event_id]) for event in events if event.event_id in by_event
    )
    return sha256_bytes(encoded), pending, decided


def disposition_dict(disposition: ControlDisposition) -> dict[str, Any]:
    payload = asdict(disposition)
    payload["decision"] = disposition.decision.value
    return payload


def _parse_dispositions(encoded: bytes) -> tuple[ControlDisposition, ...]:
    if not encoded:
        return ()
    if not encoded.endswith(b"\n"):
        raise ValidationError("operation disposition log ends with a partial event")
    dispositions: list[ControlDisposition] = []
    expected = {
        "acknowledged_at",
        "actor_ref",
        "decision",
        "disposition_id",
        "event_digest",
        "event_id",
        "reason_code",
        "result_ref",
        "schema_version",
    }
    for number, raw in enumerate(encoded.splitlines(), start=1):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"operation disposition {number} is invalid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValidationError(f"operation disposition {number} has an unsupported shape")
        if payload["schema_version"] != DISPOSITION_SCHEMA_VERSION:
            raise ValidationError(f"operation disposition {number} has an unsupported version")
        acknowledged_at = payload["acknowledged_at"]
        if not isinstance(acknowledged_at, str):
            raise ValidationError(f"operation disposition {number} has an invalid timestamp")
        try:
            acknowledged_at = stored_time(acknowledged_at, "operation disposition timestamp")
        except ValidationError as exc:
            raise ValidationError(
                f"operation disposition {number} has an invalid timestamp"
            ) from exc
        dispositions.append(
            ControlDisposition(
                schema_version=DISPOSITION_SCHEMA_VERSION,
                disposition_id=_uuid(payload["disposition_id"], "disposition ID"),
                event_id=_uuid(payload["event_id"], "event ID"),
                event_digest=_revision(payload["event_digest"], "event digest"),
                decision=_decision(payload["decision"]),
                actor_ref=_actor(payload["actor_ref"]),
                reason_code=_reason(payload["reason_code"]),
                result_ref=_optional_result_ref(payload["result_ref"]),
                acknowledged_at=acknowledged_at,
            )
        )
    if len({item.disposition_id for item in dispositions}) != len(dispositions):
        raise ValidationError("operation disposition log contains duplicate disposition IDs")
    if len({item.event_id for item in dispositions}) != len(dispositions):
        raise ValidationError("operation disposition log contains duplicate event dispositions")
    if len(dispositions) > MAX_DISPOSITIONS:
        raise ValidationError("operation disposition log exceeds its bounded count")
    return tuple(dispositions)


def _event_digest(event: ControlEvent) -> str:
    return sha256_bytes(
        json.dumps(event_dict(event), separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _decision(value: object) -> DispositionDecision:
    if not isinstance(value, str):
        raise ValidationError("disposition decision must be a string")
    try:
        return DispositionDecision(value)
    except ValueError as exc:
        raise ValidationError("disposition decision must be accepted or rejected") from exc


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc
    if normalized != value:
        raise ValidationError(f"{label} must be a canonical UUID")
    return value


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValidationError(f"{label} must be a SHA-256 revision")
    return value.lower()


def _actor(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ACTOR.fullmatch(value):
        raise ValidationError("actor_ref must be a bounded namespaced reference")
    return value


def _reason(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_REASON.fullmatch(value):
        raise ValidationError("reason_code must be a bounded machine code")
    return value


def _optional_result_ref(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > MAX_RESULT_REF_BYTES
        or not _SAFE_RESULT_REF.fullmatch(value)
    ):
        raise ValidationError("result_ref must be a bounded opaque namespaced reference")
    return value
