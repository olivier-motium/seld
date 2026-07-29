"""Bounded append-only delivery queue for facts awaiting resident AI integration.

Signals carry evidence, never task disposition or authority.  The kernel keeps
delivery exact and acknowledgement crash-safe; the AI decides what a signal
means and whether any canonical record should change before acknowledging it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from continuity_kernel.atomic import (
    PINNED_PATH_ROOT_SUPPORTED,
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    atomic_write,
    durable_unlink,
    exclusive_lock,
    read_regular_file,
    sha256_bytes,
)
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.records import format_time, stored_time

MAX_SIGNAL_EVENT_BYTES: Final = 32 * 1024
MAX_SIGNAL_QUEUE_BYTES: Final = 64 * 1024 * 1024
MAX_SIGNAL_HISTORY_BYTES: Final = 512 * 1024 * 1024
MAX_SIGNAL_HISTORY_FILES: Final = 10_000
MAX_SIGNAL_OPERATIONS: Final = 32
MAX_SIGNAL_RESULTS: Final = 10_000
MAX_SIGNAL_ACK_BATCH: Final = 2_000
MAX_SIGNAL_RESULT_REFS: Final = 20
MAX_SIGNAL_TOKEN: Final = 200
MAX_COMPACTION_MARKER_BYTES: Final = 16 * 1024
SIGNAL_COMPACTION_FORMAT_VERSION: Final = 2
SETTLED_EVENT_KEY_FORMAT_VERSION: Final = 1
SETTLED_EVENT_INDEX_FORMAT_VERSION: Final = 2
SETTLED_EVENT_KEYS_RELATIVE: Final = ".gsv/signals/event-keys.jsonl"
EMPTY_SIGNAL_REVISION: Final = sha256_bytes(b"\0")
SAFE_SIGNAL_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
SAFE_DURABLE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}@[0-9a-f]{64}$")
SIGNAL_ARCHIVE_NAME = re.compile(r"^(?:inputs|acks)-[0-9A-Za-z]+-[0-9a-f]{32}\.jsonl$")
SIGNAL_COMPACTION_STAGE_NAMES: Final = frozenset(
    {
        "archive_acknowledgements.jsonl",
        "archive_inputs.jsonl",
        "live_acknowledgements.jsonl",
        "live_inputs.jsonl",
        "settled_event_keys.jsonl",
    }
)
CANONICAL_CHANGE_TYPES: Final = frozenset({"correction", "failure", "observation", "outcome"})


@dataclass(frozen=True)
class ResidentSignal:
    input_id: str
    observed_at: str
    kind: str
    ref: str | None
    event_key: str | None
    envelope: dict[str, object]


@dataclass(frozen=True)
class SignalAppendRequest:
    """One validated signal candidate for an atomic mailbox append."""

    kind: str
    envelope: Mapping[str, object]
    ref: str | None = None
    event_key: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class SignalAcknowledgement:
    acknowledgement_id: str
    input_id: str
    acknowledged_at: str
    consumer: str
    disposition: str | None = None
    result_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SettledEventKey:
    """Content-free replay receipt for one compacted, acknowledged signal."""

    format_version: int
    event_key_digest: str
    signal_digest: str
    input_id: str
    observed_at: str


@dataclass(frozen=True)
class SignalCompaction:
    archived_signals: int
    archived_acknowledgements: int
    live_signals: int
    live_acknowledgements: int
    archive_inputs_path: str | None
    archive_acknowledgements_path: str | None


@dataclass(frozen=True)
class SignalQueueView:
    signals: tuple[ResidentSignal, ...]
    acknowledged_ids: tuple[str, ...]
    remaining: int
    revision: str
    next_cursor: str | None


@dataclass(frozen=True)
class SignalQueueStatus:
    inputs: int
    acknowledged: int
    pending: int
    revision: str


class ResidentSignalStore:
    """Read and acknowledge the exact queue owned by one Seld vault."""

    def __init__(self, vault_root: Path | str):
        expanded = Path(vault_root).expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        # Do not resolve the root again here. Vault rejects a symlink root and
        # PinnedPathRoot must inspect that exact lexical entry with O_NOFOLLOW;
        # resolving here would silently retarget a later root substitution.
        self.vault_root = Path(os.path.abspath(expanded))
        self.root = self.vault_root / ".gsv/signals"

    @property
    def lock_path(self) -> Path:
        return self.vault_root / ".gsv/locks/resident-signals.lock"

    @property
    def inputs_path(self) -> Path:
        return self.root / "inputs.jsonl"

    @property
    def acknowledgements_path(self) -> Path:
        return self.root / "acks.jsonl"

    @property
    def settled_event_keys_path(self) -> Path:
        return self.vault_root / SETTLED_EVENT_KEYS_RELATIVE

    @property
    def compaction_marker_path(self) -> Path:
        return self.root / "compaction.json"

    @contextmanager
    def _transaction(self) -> Iterator[PinnedPathRoot | None]:
        """Serialize one mailbox operation beneath a single pinned signals binding."""

        if not PINNED_PATH_ROOT_SUPPORTED:
            self.root.mkdir(parents=True, exist_ok=True)
            with exclusive_lock(self.lock_path):
                yield None
            return

        store = PinnedPathRoot(self.vault_root)
        try:
            try:
                with (
                    store.watch_directory(".gsv", create=True),
                    store.watch_directory(".gsv/locks", create=True),
                    store.bind_directory(".gsv/signals", create=True),
                    store.exclusive_file_lock(".gsv/locks/resident-signals.lock"),
                ):
                    yield store
            except DurablePublishError as exc:
                if exc.outcome is PublishOutcome.UNPUBLISHED:
                    raise PersistenceError(
                        "resident signal storage change was not published"
                    ) from exc
                if exc.outcome is PublishOutcome.COMMITTED:
                    raise MutationCommittedError(
                        "resident signal storage change committed, but cleanup is unconfirmed"
                    ) from exc
                raise DegradedIntegrityError(
                    "resident signal storage has an unknown publication state; run gsv doctor"
                ) from exc
        finally:
            store.close()

    @contextmanager
    def coherent_snapshot(self) -> Iterator[None]:
        """Hold a recovered, validated mailbox stable for a whole-vault snapshot."""

        with self._transaction() as store:
            inputs_state, acknowledgements_state, settled_state = self._read_state(store)
            signals = _parse_signals(inputs_state or b"")
            acknowledgements = _parse_acknowledgements(acknowledgements_state or b"")
            settled = _parse_settled_event_keys(settled_state or b"")
            _validate_acknowledgement_targets(signals, acknowledgements)
            _validate_live_settled_separation(signals, settled)
            yield

    def list(
        self,
        *,
        include_acknowledged: bool = False,
        limit: int = 500,
        cursor: str | None = None,
    ) -> SignalQueueView:
        if not 1 <= limit <= MAX_SIGNAL_RESULTS:
            raise ValidationError("signal limit must be between 1 and 10000")
        with self._transaction() as store:
            inputs_state, acknowledgements_state, settled_state = self._read_state(store)
            inputs_bytes = inputs_state or b""
            acknowledgements_bytes = acknowledgements_state or b""
            signals = _parse_signals(inputs_bytes)
            acknowledgements = _parse_acknowledgements(acknowledgements_bytes)
            settled = _parse_settled_event_keys(settled_state or b"")
            revision = _queue_revision(inputs_bytes, acknowledgements_bytes)
            _validate_acknowledgement_targets(signals, acknowledgements)
            _validate_live_settled_separation(signals, settled)
        acknowledged = {item.input_id for item in acknowledgements}
        visible = tuple(
            signals
            if include_acknowledged
            else tuple(item for item in signals if item.input_id not in acknowledged)
        )
        mode = "all" if include_acknowledged else "pending"
        offset = _cursor_offset(cursor, expected_revision=revision, expected_mode=mode)
        if offset > len(visible):
            raise ValidationError("resident signal cursor is outside the current queue")
        selected = visible[offset : offset + limit]
        next_offset = offset + len(selected)
        return SignalQueueView(
            signals=selected,
            acknowledged_ids=tuple(sorted(acknowledged)),
            remaining=max(0, len(visible) - next_offset),
            revision=revision,
            next_cursor=(
                f"{revision}:{mode}:{next_offset}" if next_offset < len(visible) else None
            ),
        )

    def get(self, input_id: str) -> ResidentSignal:
        clean_id = _uuid(input_id, "signal ID")
        with self._transaction() as store:
            inputs_state, acknowledgements_state, settled_state = self._read_state(store)
            inputs_bytes = inputs_state or b""
            signals = _parse_signals(inputs_bytes)
            acknowledgements = _parse_acknowledgements(acknowledgements_state or b"")
            settled = _parse_settled_event_keys(settled_state or b"")
            _validate_acknowledgement_targets(signals, acknowledgements)
            _validate_live_settled_separation(signals, settled)
        match = next((item for item in signals if item.input_id == clean_id), None)
        if match is None:
            raise NotFoundError(f"resident signal does not exist: {clean_id}")
        return match

    def status(self, *, verify_archive_history: bool = False) -> SignalQueueStatus:
        """Validate the complete live queue and return content-free counts."""
        with self._transaction() as store:
            inputs_state, acknowledgements_state, settled_state = self._read_state(
                store,
                verify_archive_history=verify_archive_history,
            )
            inputs_bytes = inputs_state or b""
            acknowledgements_bytes = acknowledgements_state or b""
            signals = _parse_signals(inputs_bytes)
            acknowledgements = _parse_acknowledgements(acknowledgements_bytes)
            settled = _parse_settled_event_keys(settled_state or b"")
            _validate_acknowledgement_targets(signals, acknowledgements)
            _validate_live_settled_separation(signals, settled)
        return SignalQueueStatus(
            inputs=len(signals),
            acknowledged=len(acknowledgements),
            pending=len(signals) - len(acknowledgements),
            revision=_queue_revision(inputs_bytes, acknowledgements_bytes),
        )

    def append(
        self,
        *,
        kind: str,
        envelope: Mapping[str, object],
        ref: str | None = None,
        event_key: str | None = None,
        observed_at: datetime | None = None,
    ) -> ResidentSignal:
        """Append one evidence envelope, idempotently when it has an event key."""

        signal, _created = self.append_result(
            kind=kind,
            envelope=envelope,
            ref=ref,
            event_key=event_key,
            observed_at=observed_at,
        )
        return signal

    def append_result(
        self,
        *,
        kind: str,
        envelope: Mapping[str, object],
        ref: str | None = None,
        event_key: str | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[ResidentSignal, bool]:
        """Append one envelope and report whether this call created the signal."""

        return self.append_many_results(
            (
                SignalAppendRequest(
                    kind=kind,
                    envelope=envelope,
                    ref=ref,
                    event_key=event_key,
                    observed_at=observed_at,
                ),
            )
        )[0]

    def append_many_results(
        self,
        requests: Sequence[SignalAppendRequest],
    ) -> tuple[tuple[ResidentSignal, bool], ...]:
        """Append a bounded group after one coherent queue validation and publication."""

        if len(requests) > MAX_SIGNAL_RESULTS:
            raise ValidationError(f"append at most {MAX_SIGNAL_RESULTS} resident signals at once")
        prepared: list[tuple[str, str | None, str | None, dict[str, object], str]] = []
        for request in requests:
            if not isinstance(request, SignalAppendRequest):
                raise ValidationError("resident signal append request is invalid")
            prepared.append(
                (
                    _token(request.kind, "signal kind"),
                    _optional_line(request.ref, 1_000, "signal reference"),
                    _optional_line(request.event_key, 1_000, "signal event key"),
                    _json_object(request.envelope),
                    format_time(request.observed_at or datetime.now(UTC)),
                )
            )
        if not prepared:
            return ()

        with self._transaction() as store:
            inputs_state, acknowledgements_state, settled_state = self._read_state(store)
            inputs_bytes = inputs_state or b""
            acknowledgements_bytes = acknowledgements_state or b""
            signals = _parse_signals(inputs_bytes)
            settled = _parse_settled_event_keys(settled_state or b"")
            _validate_acknowledgement_targets(
                signals, _parse_acknowledgements(acknowledgements_bytes)
            )
            _validate_live_settled_separation(signals, settled)
            by_event_key = {item.event_key: item for item in signals if item.event_key is not None}
            settled_by_digest = {item.event_key_digest: item for item in settled}
            results: list[tuple[ResidentSignal, bool]] = []
            additions: list[bytes] = []
            replacement_size = len(inputs_bytes)
            for clean_kind, clean_ref, clean_event_key, clean_envelope, signal_time in prepared:
                existing = (
                    by_event_key.get(clean_event_key) if clean_event_key is not None else None
                )
                if existing is not None:
                    if (
                        existing.kind != clean_kind
                        or existing.ref != clean_ref
                        or existing.envelope != clean_envelope
                    ):
                        raise ConflictError(
                            "resident signal event key already has a different envelope"
                        )
                    results.append((existing, False))
                    continue
                settled_match = (
                    settled_by_digest.get(_event_key_digest(clean_event_key))
                    if clean_event_key is not None
                    else None
                )
                if settled_match is not None:
                    candidate = ResidentSignal(
                        input_id=settled_match.input_id,
                        observed_at=settled_match.observed_at,
                        kind=clean_kind,
                        ref=clean_ref,
                        event_key=clean_event_key,
                        envelope=clean_envelope,
                    )
                    if _signal_digest(candidate) != settled_match.signal_digest:
                        raise ConflictError(
                            "resident signal event key already has a different envelope"
                        )
                    results.append((candidate, False))
                    continue
                signal = ResidentSignal(
                    input_id=str(uuid.uuid4()),
                    observed_at=signal_time,
                    kind=clean_kind,
                    ref=clean_ref,
                    event_key=clean_event_key,
                    envelope=clean_envelope,
                )
                encoded = _json_line(asdict(signal), label="resident signal input")
                replacement_size += len(encoded)
                if replacement_size > MAX_SIGNAL_QUEUE_BYTES:
                    raise ValidationError(
                        "resident signal input queue is full; compact acknowledged signals first"
                    )
                additions.append(encoded)
                results.append((signal, True))
                if clean_event_key is not None:
                    by_event_key[clean_event_key] = signal

            replacement = inputs_bytes + b"".join(additions)
            if additions:
                self._replace_exact(
                    store,
                    self.inputs_path,
                    expected=inputs_state,
                    replacement=replacement,
                    label="resident signal input queue",
                )
            return tuple(results)

    def append_canonical_change(
        self,
        *,
        record_ref: str,
        change_type: str,
        observed_at: datetime | None = None,
    ) -> ResidentSignal:
        """Append one content-free pointer for later resident interpretation."""

        clean_ref = _durable_ref(record_ref, "canonical record reference")
        if change_type not in CANONICAL_CHANGE_TYPES:
            raise ValidationError("canonical change type is invalid")
        event_key = "canonical-change:" + sha256_bytes(f"{clean_ref}\0{change_type}".encode())
        return self.append(
            kind="canonical-change",
            ref=clean_ref,
            event_key=event_key,
            envelope={"change_type": change_type},
            observed_at=observed_at,
        )

    def acknowledge(
        self,
        input_ids: Sequence[str],
        *,
        expected_revision: str,
        consumer: str,
        disposition: str | None = None,
        result_refs: Sequence[str] = (),
        precommit_guard: Callable[[ResidentSignal], None] | None = None,
        acknowledged_at: datetime | None = None,
    ) -> tuple[SignalAcknowledgement, ...]:
        ordered = tuple(dict.fromkeys(_uuid(value, "signal ID") for value in input_ids))
        if not ordered:
            raise ValidationError("acknowledge at least one resident signal")
        if len(ordered) > MAX_SIGNAL_ACK_BATCH:
            raise ValidationError(
                f"acknowledge at most {MAX_SIGNAL_ACK_BATCH} resident signals at once"
            )
        clean_consumer = _token(consumer, "signal consumer")
        clean_disposition = _optional_disposition(disposition)
        clean_result_refs = _result_refs(result_refs, strict=True)
        if clean_disposition is None and clean_result_refs:
            raise ValidationError("legacy signal acknowledgements cannot carry result references")
        if clean_disposition is not None and not clean_result_refs:
            raise ValidationError("signal acknowledgement requires a durable disposition reference")
        with self._transaction() as store:
            inputs_state, acknowledgements_state, settled_state = self._read_state(store)
            inputs_bytes = inputs_state or b""
            acknowledgements_bytes = acknowledgements_state or b""
            revision = _queue_revision(inputs_bytes, acknowledgements_bytes)
            if revision != expected_revision:
                raise ConflictError("resident signal queue changed; reload before acknowledging")
            signals = _parse_signals(inputs_bytes)
            settled = _parse_settled_event_keys(settled_state or b"")
            _validate_live_settled_separation(signals, settled)
            by_id = {item.input_id: item for item in signals}
            known = set(by_id)
            missing = next((value for value in ordered if value not in known), None)
            if missing is not None:
                raise NotFoundError(f"resident signal does not exist: {missing}")
            previous = {
                item.input_id: item for item in _parse_acknowledgements(acknowledgements_bytes)
            }
            _validate_acknowledgement_targets(signals, tuple(previous.values()))
            conflict = next(
                (
                    value
                    for value in ordered
                    if value in previous and previous[value].consumer != clean_consumer
                ),
                None,
            )
            if conflict is not None:
                raise ConflictError(
                    f"resident signal was already acknowledged by {previous[conflict].consumer}"
                )
            timestamp = format_time(acknowledged_at or datetime.now(UTC))
            results: list[SignalAcknowledgement] = []
            pending_writes: list[bytes] = []
            for value in ordered:
                existing = previous.get(value)
                if existing is not None:
                    if clean_disposition is not None and (
                        existing.disposition != clean_disposition
                        or existing.result_refs != clean_result_refs
                    ):
                        raise ConflictError("resident signal already has a different disposition")
                    results.append(existing)
                    continue
                if precommit_guard is not None:
                    precommit_guard(by_id[value])
                acknowledgement = SignalAcknowledgement(
                    acknowledgement_id=str(uuid.uuid4()),
                    input_id=value,
                    acknowledged_at=timestamp,
                    consumer=clean_consumer,
                    disposition=clean_disposition,
                    result_refs=clean_result_refs,
                )
                encoded = _json_line(
                    _acknowledgement_storage_dict(acknowledgement),
                    label="resident signal acknowledgement",
                )
                pending_writes.append(encoded)
                results.append(acknowledgement)
            if len(acknowledgements_bytes) + sum(map(len, pending_writes)) > (
                MAX_SIGNAL_QUEUE_BYTES
            ):
                raise ValidationError(
                    "resident signal acknowledgement queue is full; compact it first"
                )
            if pending_writes:
                self._replace_exact(
                    store,
                    self.acknowledgements_path,
                    expected=acknowledgements_state,
                    replacement=acknowledgements_bytes + b"".join(pending_writes),
                    label="resident signal acknowledgement queue",
                )
            return tuple(results)

    def compact(
        self,
        *,
        retain_recent: int = 1_000,
        observed_at: datetime | None = None,
    ) -> SignalCompaction:
        """Archive settled evidence while preserving every byte as queue history."""

        if retain_recent < 0:
            raise ValidationError("retain at least zero recent resident signals")
        with self._transaction() as store:
            (
                inputs_state,
                acknowledgements_state,
                settled_state,
                archive_files,
            ) = self._read_state_with_archive(
                store,
                verify_archive_history=True,
            )
            inputs_bytes = inputs_state or b""
            acknowledgements_bytes = acknowledgements_state or b""
            signals = _parse_signals(inputs_bytes)
            acknowledgements = _parse_acknowledgements(acknowledgements_bytes)
            settled = _parse_settled_event_keys(settled_state or b"")
            _validate_acknowledgement_targets(signals, acknowledgements)
            _validate_live_settled_separation(signals, settled)
            acknowledged = {item.input_id for item in acknowledgements}
            keep_from = max(0, len(signals) - retain_recent)
            archived_signals = tuple(
                item
                for index, item in enumerate(signals)
                if index < keep_from and item.input_id in acknowledged
            )
            if not archived_signals:
                return SignalCompaction(
                    0,
                    0,
                    len(signals),
                    len(acknowledgements),
                    None,
                    None,
                )
            moved = {item.input_id for item in archived_signals}
            archived_acknowledgement_records = tuple(
                item for item in acknowledgements if item.input_id in moved
            )
            live_signals = tuple(item for item in signals if item.input_id not in moved)
            live_acknowledgements = tuple(
                item for item in acknowledgements if item.input_id not in moved
            )
            settled_replacement = _merge_settled_event_keys(settled, archived_signals)
            settled_bytes = _encode_settled_event_keys(settled_replacement)
            if len(settled_bytes) > MAX_SIGNAL_QUEUE_BYTES:
                raise ValidationError(
                    "resident signal settled event-key ledger is full; compaction was not started"
                )
            archived_inputs_bytes = _encode_jsonl(archived_signals)
            archived_acknowledgements_bytes = _encode_jsonl(archived_acknowledgement_records)
            stamp = format_time(observed_at or datetime.now(UTC))
            portable_stamp = re.sub(r"[^0-9A-Za-z]", "", stamp)
            archive_token = uuid.uuid4().hex
            archive_root = self.root / "archive"
            archive_inputs = archive_root / f"inputs-{portable_stamp}-{archive_token}.jsonl"
            archive_acknowledgements_path = (
                archive_root / f"acks-{portable_stamp}-{archive_token}.jsonl"
            )
            self._require_archive_capacity(
                store,
                {
                    archive_inputs.relative_to(self.vault_root).as_posix(): (archived_inputs_bytes),
                    archive_acknowledgements_path.relative_to(self.vault_root).as_posix(): (
                        archived_acknowledgements_bytes
                    ),
                },
                operation="compaction",
                verified_archive_files=archive_files,
            )
            operation_root = self.root / "operations" / archive_token
            if store is not None:
                for relative in (".gsv/signals/archive", ".gsv/signals/operations"):
                    if not store.directory_exists(relative):
                        store.ensure_directory(relative)
                operation_relative = operation_root.relative_to(self.vault_root)
                if store.directory_exists(operation_relative):
                    raise ConflictError("resident signal compaction token already exists")
                store.ensure_directory(operation_relative)
            else:
                if os.path.lexists(operation_root):
                    raise ConflictError("resident signal compaction token already exists")
                operation_root.mkdir(parents=True)
                if operation_root.is_symlink() or not operation_root.is_dir():
                    raise ValidationError("resident signal compaction storage is invalid")
            staged = {
                "archive_acknowledgements": archived_acknowledgements_bytes,
                "archive_inputs": archived_inputs_bytes,
                "live_acknowledgements": _encode_jsonl(live_acknowledgements),
                "live_inputs": _encode_jsonl(live_signals),
                "settled_event_keys": settled_bytes,
            }
            for name, encoded in staged.items():
                self._replace_exact(
                    store,
                    operation_root / f"{name}.jsonl",
                    expected=None,
                    replacement=encoded,
                    label="resident signal compaction stage",
                )
            marker = {
                "archive_acknowledgements": archive_acknowledgements_path.relative_to(
                    self.vault_root
                ).as_posix(),
                "archive_inputs": archive_inputs.relative_to(self.vault_root).as_posix(),
                "digests": {name: sha256_bytes(encoded) for name, encoded in staged.items()},
                "expected_digests": {
                    "live_acknowledgements": (
                        None
                        if acknowledgements_state is None
                        else sha256_bytes(acknowledgements_state)
                    ),
                    "live_inputs": (None if inputs_state is None else sha256_bytes(inputs_state)),
                    "settled_event_keys": (
                        None if settled_state is None else sha256_bytes(settled_state)
                    ),
                },
                "format_version": SIGNAL_COMPACTION_FORMAT_VERSION,
                "phase": "prepared",
                "token": archive_token,
            }
            marker_bytes = _bounded_json(marker, label="resident signal compaction marker")
            self._replace_exact(
                store,
                self.compaction_marker_path,
                expected=None,
                replacement=marker_bytes,
                label="resident signal compaction marker",
                max_bytes=MAX_COMPACTION_MARKER_BYTES,
            )
            self._recover_compaction(store)
            return SignalCompaction(
                len(archived_signals),
                len(archived_acknowledgement_records),
                len(live_signals),
                len(live_acknowledgements),
                archive_inputs.relative_to(self.vault_root).as_posix(),
                archive_acknowledgements_path.relative_to(self.vault_root).as_posix(),
            )

    def _read_state(
        self,
        store: PinnedPathRoot | None,
        *,
        verify_archive_history: bool = False,
    ) -> tuple[bytes | None, bytes | None, bytes | None]:
        inputs, acknowledgements, settled, _archives = self._read_state_with_archive(
            store,
            verify_archive_history=verify_archive_history,
        )
        return inputs, acknowledgements, settled

    def _read_state_with_archive(
        self,
        store: PinnedPathRoot | None,
        *,
        verify_archive_history: bool = False,
    ) -> tuple[bytes | None, bytes | None, bytes | None, dict[str, bytes]]:
        self._recover_compaction(store)
        inputs_state = self._read_optional_state(
            store,
            self.inputs_path,
            label="resident signal input queue",
            max_bytes=MAX_SIGNAL_QUEUE_BYTES,
        )
        acknowledgements_state = self._read_optional_state(
            store,
            self.acknowledgements_path,
            label="resident signal acknowledgement queue",
            max_bytes=MAX_SIGNAL_QUEUE_BYTES,
        )
        settled_state = self._read_optional_state(
            store,
            self.settled_event_keys_path,
            label="resident signal settled event-key ledger",
            max_bytes=MAX_SIGNAL_QUEUE_BYTES,
        )
        settled_state, archive_files = self._reconcile_archive_index(
            store,
            inputs_state=inputs_state,
            acknowledgements_state=acknowledgements_state,
            settled_state=settled_state,
            verify_archive_history=verify_archive_history,
        )
        self._cleanup_committed_compaction_residue(
            store,
            inputs_state=inputs_state,
            acknowledgements_state=acknowledgements_state,
            settled_state=settled_state,
            archive_files=archive_files,
        )
        return inputs_state, acknowledgements_state, settled_state, archive_files

    def _recover_compaction(self, store: PinnedPathRoot | None) -> None:
        encoded = self._read_optional_state(
            store,
            self.compaction_marker_path,
            label="resident signal compaction marker",
            max_bytes=MAX_COMPACTION_MARKER_BYTES,
        )
        if encoded is None:
            return
        marker = _parse_compaction_marker(encoded, vault_root=self.vault_root)
        token = marker["token"]
        assert isinstance(token, str)
        operation_root = self.root / "operations" / token
        archive_targets: tuple[tuple[str, Path, bool], ...] = (
            ("archive_inputs", self.vault_root / str(marker["archive_inputs"]), False),
            (
                "archive_acknowledgements",
                self.vault_root / str(marker["archive_acknowledgements"]),
                False,
            ),
        )
        live_targets: tuple[tuple[str, Path, bool], ...] = (
            ("live_inputs", self.inputs_path, True),
            ("live_acknowledgements", self.acknowledgements_path, True),
        )
        settled_targets: tuple[tuple[str, Path, bool], ...] = (
            (("settled_event_keys", self.settled_event_keys_path, True),)
            if marker["format_version"] == SIGNAL_COMPACTION_FORMAT_VERSION
            else ()
        )
        targets = (*archive_targets, *settled_targets, *live_targets)
        digests = marker["digests"]
        assert isinstance(digests, dict)
        expected_digests = marker.get("expected_digests")
        if expected_digests is not None:
            assert isinstance(expected_digests, dict)
        staged_targets: dict[str, bytes] = {}
        for name, _target, _replace in targets:
            staged = self._read_optional_state(
                store,
                operation_root / f"{name}.jsonl",
                label="resident signal compaction stage",
                max_bytes=MAX_SIGNAL_QUEUE_BYTES,
            )
            expected = digests[name]
            if staged is None or not isinstance(expected, str) or sha256_bytes(staged) != expected:
                raise ValidationError("resident signal compaction stage changed")
            staged_targets[name] = staged
        archive_staged = {
            target.relative_to(self.vault_root).as_posix(): staged_targets[name]
            for name, target, _replace in archive_targets
        }
        self._cleanup_owned_archive_temps(store, archive_staged)
        final_archives = self._require_archive_capacity(
            store,
            archive_staged,
            operation="compaction recovery",
        )
        intended_files = dict(final_archives)
        intended_files[".gsv/signals/inputs.jsonl"] = staged_targets["live_inputs"]
        intended_files[".gsv/signals/acks.jsonl"] = staged_targets["live_acknowledgements"]
        if "settled_event_keys" in staged_targets:
            intended_files[SETTLED_EVENT_KEYS_RELATIVE] = staged_targets["settled_event_keys"]
        build_settled_event_index(intended_files)
        if marker["format_version"] == 1:
            if expected_digests is None:
                raise ConflictError(
                    "legacy resident signal compaction lacks exact live prestate; "
                    "the live queue was preserved"
                )
            self._upgrade_legacy_compaction_index(
                store,
                operation_root=operation_root,
                marker=marker,
            )
        for name, target, replace in targets:
            staged = staged_targets[name]
            expected = digests[name]
            assert isinstance(expected, str)
            existing = self._read_optional_state(
                store,
                target,
                label="resident signal compaction target",
                max_bytes=MAX_SIGNAL_QUEUE_BYTES,
            )
            if existing is not None and sha256_bytes(existing) == expected:
                continue
            if existing is not None and not replace:
                raise ConflictError("resident signal archive path changed outside Seld")
            if replace:
                if expected_digests is None:
                    raise ConflictError(
                        "legacy resident signal compaction lacks exact live prestate; "
                        "the live queue was preserved"
                    )
                prior_digest = expected_digests[name]
                if (prior_digest is None and existing is not None) or (
                    isinstance(prior_digest, str)
                    and (existing is None or sha256_bytes(existing) != prior_digest)
                ):
                    subject = (
                        "settled event-key ledger" if name == "settled_event_keys" else "live queue"
                    )
                    raise ConflictError(
                        f"resident signal {subject} changed during compaction recovery"
                    )
            self._replace_exact(
                store,
                target,
                expected=existing,
                replacement=staged,
                label="resident signal compaction target",
            )
        self._cleanup_owned_archive_temps(store, archive_staged)
        self._unlink_private_exact(
            store,
            self.compaction_marker_path,
            expected=encoded,
            label="resident signal compaction marker",
            max_bytes=MAX_COMPACTION_MARKER_BYTES,
        )
        try:
            stage_names = (
                "archive_inputs",
                "archive_acknowledgements",
                *(
                    ("settled_event_keys",)
                    if marker["format_version"] == SIGNAL_COMPACTION_FORMAT_VERSION
                    else ()
                ),
                "live_inputs",
                "live_acknowledgements",
            )
            for name in stage_names:
                staged_path = operation_root / f"{name}.jsonl"
                staged = self._read_optional_state(
                    store,
                    staged_path,
                    label="resident signal compaction stage",
                    max_bytes=MAX_SIGNAL_QUEUE_BYTES,
                )
                if staged is not None:
                    self._unlink_private_exact(
                        store,
                        staged_path,
                        expected=staged,
                        label="resident signal compaction stage",
                    )
            if store is None:
                with suppress(OSError):
                    operation_root.rmdir()
            else:
                store.remove_retained_empty_directory(
                    operation_root.relative_to(self.vault_root),
                    label="resident signal compaction operation",
                )
        except MutationCommittedError:
            raise
        except Exception as exc:
            raise MutationCommittedError(
                "resident signal compaction committed, but operation cleanup is incomplete; "
                "run gsv doctor"
            ) from exc

    def _upgrade_legacy_compaction_index(
        self,
        store: PinnedPathRoot | None,
        *,
        operation_root: Path,
        marker: dict[str, object],
    ) -> None:
        """Add the replay ledger before a version-1 compaction removes live inputs."""

        digests = marker["digests"]
        expected_digests = marker["expected_digests"]
        assert isinstance(digests, dict)
        assert isinstance(expected_digests, dict)
        staged: dict[str, bytes] = {}
        for name in (
            "archive_inputs",
            "archive_acknowledgements",
            "live_inputs",
            "live_acknowledgements",
        ):
            encoded = self._read_optional_state(
                store,
                operation_root / f"{name}.jsonl",
                label="resident signal compaction stage",
                max_bytes=MAX_SIGNAL_QUEUE_BYTES,
            )
            expected = digests[name]
            if (
                encoded is None
                or not isinstance(expected, str)
                or sha256_bytes(encoded) != expected
            ):
                raise ValidationError("resident signal compaction stage changed")
            staged[name] = encoded
        for name, path in (
            ("live_inputs", self.inputs_path),
            ("live_acknowledgements", self.acknowledgements_path),
        ):
            current = self._read_optional_state(
                store,
                path,
                label="resident signal compaction target",
                max_bytes=MAX_SIGNAL_QUEUE_BYTES,
            )
            current_digest = None if current is None else sha256_bytes(current)
            if current_digest not in {expected_digests[name], digests[name]}:
                raise ConflictError("resident signal live queue changed during compaction recovery")
        current_index = self._read_optional_state(
            store,
            self.settled_event_keys_path,
            label="resident signal settled event-key ledger",
            max_bytes=MAX_SIGNAL_QUEUE_BYTES,
        )
        portable = self._read_archive_history_files(store)
        archive_paths = {
            "archive_inputs": str(marker["archive_inputs"]),
            "archive_acknowledgements": str(marker["archive_acknowledgements"]),
        }
        prior = {
            path: content
            for path, content in portable.items()
            if path not in set(archive_paths.values())
        }
        prior[".gsv/signals/inputs.jsonl"] = staged["live_inputs"]
        prior[".gsv/signals/acks.jsonl"] = staged["live_acknowledgements"]
        if current_index is not None:
            prior[SETTLED_EVENT_KEYS_RELATIVE] = current_index
        build_settled_event_index(prior)
        for name, archive_path in archive_paths.items():
            existing_archive = portable.get(archive_path)
            if existing_archive is not None and existing_archive != staged[name]:
                raise ConflictError("resident signal archive path changed outside Seld")
            portable[archive_path] = staged[name]
        portable[".gsv/signals/inputs.jsonl"] = staged["live_inputs"]
        portable[".gsv/signals/acks.jsonl"] = staged["live_acknowledgements"]
        replacement_bytes = build_settled_event_index(portable)
        if current_index != replacement_bytes:
            self._replace_exact(
                store,
                self.settled_event_keys_path,
                expected=current_index,
                replacement=replacement_bytes,
                label="resident signal settled event-key ledger",
            )

    def _reconcile_archive_index(
        self,
        store: PinnedPathRoot | None,
        *,
        inputs_state: bytes | None,
        acknowledgements_state: bytes | None,
        settled_state: bytes | None,
        verify_archive_history: bool,
    ) -> tuple[bytes | None, dict[str, bytes]]:
        """Derive replay truth from the archives and upgrade completed v1 history."""

        if settled_state is not None:
            _parse_settled_event_keys(settled_state)
        operations_present = bool(
            self._directory_entry_names(
                store,
                Path(".gsv/signals/operations"),
                max_entries=MAX_SIGNAL_OPERATIONS,
            )
        )
        if (
            settled_state is not None
            and _settled_index_has_current_header(settled_state)
            and not verify_archive_history
            and not operations_present
        ):
            return settled_state, {}
        archive_files = self._read_archive_history_files(store)
        if settled_state is None and not archive_files:
            return None, {}
        files = dict(archive_files)
        files[".gsv/signals/inputs.jsonl"] = inputs_state or b""
        files[".gsv/signals/acks.jsonl"] = acknowledgements_state or b""
        if settled_state is not None:
            files[SETTLED_EVENT_KEYS_RELATIVE] = settled_state
        canonical = build_settled_event_index(files)
        if settled_state is None or not _settled_index_has_current_header(settled_state):
            self._replace_exact(
                store,
                self.settled_event_keys_path,
                expected=settled_state,
                replacement=canonical,
                label="resident signal settled event-key ledger",
            )
            return canonical, archive_files
        if settled_state != canonical:
            raise ValidationError(
                "resident signal settled event-key ledger differs from its archives"
            )
        return settled_state, archive_files

    def _read_archive_history_files(
        self,
        store: PinnedPathRoot | None,
    ) -> dict[str, bytes]:
        relative_root = Path(".gsv/signals/archive")
        names = self._directory_entry_names(
            store,
            relative_root,
            max_entries=MAX_SIGNAL_HISTORY_FILES,
        )
        archive_names = tuple(
            name for name in names if not _is_owned_signal_archive_temp_name(name)
        )
        if any(SIGNAL_ARCHIVE_NAME.fullmatch(name) is None for name in archive_names):
            raise ValidationError("resident signal archive contains an invalid entry")
        files: dict[str, bytes] = {}
        total = 0
        for name in archive_names:
            relative = (relative_root / name).as_posix()
            encoded = self._read_optional_state(
                store,
                self.vault_root / relative,
                label="resident signal archive",
                max_bytes=MAX_SIGNAL_QUEUE_BYTES,
                retain=False,
            )
            if encoded is None:
                raise ValidationError("resident signal archive changed while it was read")
            total += len(encoded)
            if total > MAX_SIGNAL_HISTORY_BYTES:
                raise ValidationError("resident signal archive exceeds its total size bound")
            files[relative] = encoded
        if names != self._directory_entry_names(
            store,
            relative_root,
            max_entries=MAX_SIGNAL_HISTORY_FILES,
        ):
            raise ConflictError("resident signal archive changed while it was read")
        for relative, expected in files.items():
            current = self._read_optional_state(
                store,
                self.vault_root / relative,
                label="resident signal archive",
                max_bytes=MAX_SIGNAL_QUEUE_BYTES,
                retain=False,
            )
            if current != expected:
                raise ConflictError("resident signal archive changed while it was read")
        if names != self._directory_entry_names(
            store,
            relative_root,
            max_entries=MAX_SIGNAL_HISTORY_FILES,
        ):
            raise ConflictError("resident signal archive changed while it was read")
        return files

    def _require_archive_capacity(
        self,
        store: PinnedPathRoot | None,
        additions: Mapping[str, bytes],
        *,
        operation: str,
        verified_archive_files: Mapping[str, bytes] | None = None,
    ) -> dict[str, bytes]:
        """Prove a compaction's final archive remains within durable bounds."""

        relative_root = Path(".gsv/signals/archive")
        names = self._directory_entry_names(
            store,
            relative_root,
            max_entries=MAX_SIGNAL_HISTORY_FILES,
        )
        current = (
            dict(verified_archive_files)
            if verified_archive_files is not None
            else self._read_archive_history_files(store)
        )
        if names != self._directory_entry_names(
            store,
            relative_root,
            max_entries=MAX_SIGNAL_HISTORY_FILES,
        ):
            raise ConflictError(f"resident signal archive changed before {operation}")
        missing: dict[str, bytes] = {}
        for relative, encoded in additions.items():
            existing = current.get(relative)
            if existing is None:
                missing[relative] = encoded
            elif existing != encoded:
                raise ConflictError("resident signal archive path changed outside Seld")
        if len(names) + len(missing) > MAX_SIGNAL_HISTORY_FILES:
            raise ValidationError(
                f"resident signal archive file bound would be exceeded; {operation} was not started"
            )
        if sum(map(len, current.values())) + sum(map(len, missing.values())) > (
            MAX_SIGNAL_HISTORY_BYTES
        ):
            raise ValidationError(
                f"resident signal archive size bound would be exceeded; {operation} was not started"
            )
        current.update(additions)
        return current

    def _cleanup_owned_archive_temps(
        self,
        store: PinnedPathRoot | None,
        expected_targets: Mapping[str, bytes],
    ) -> None:
        """Remove only writer stages bound to this proven compaction's targets."""

        relative_root = Path(".gsv/signals/archive")
        names = self._directory_entry_names(
            store,
            relative_root,
            max_entries=MAX_SIGNAL_HISTORY_FILES + len(expected_targets),
        )
        expected_names = {Path(path).name for path in expected_targets}
        for name in names:
            target_name = _owned_signal_archive_temp_target(name)
            if target_name is None or target_name not in expected_names:
                continue
            path = self.vault_root / relative_root / name
            encoded = self._read_optional_state(
                store,
                path,
                label="resident signal archive publication stage",
                max_bytes=MAX_SIGNAL_QUEUE_BYTES,
            )
            if encoded is None:
                continue
            self._unlink_private_exact(
                store,
                path,
                expected=encoded,
                label="resident signal archive publication stage",
            )

    def _cleanup_committed_compaction_residue(
        self,
        store: PinnedPathRoot | None,
        *,
        inputs_state: bytes | None,
        acknowledgements_state: bytes | None,
        settled_state: bytes | None,
        archive_files: Mapping[str, bytes],
    ) -> None:
        """Remove only markerless stages whose canonical targets are already coherent."""

        operations_relative = Path(".gsv/signals/operations")
        tokens = self._directory_entry_names(
            store,
            operations_relative,
            max_entries=MAX_SIGNAL_OPERATIONS,
        )
        for token in tokens:
            if re.fullmatch(r"[0-9a-f]{32}", token) is None:
                raise ValidationError("resident signal compaction operation name is invalid")
            operation_relative = operations_relative / token
            stage_names = self._directory_entry_names(
                store,
                operation_relative,
                max_entries=6,
            )
            visible_stage_names = tuple(
                name for name in stage_names if name in SIGNAL_COMPACTION_STAGE_NAMES
            )
            temp_stage_names = tuple(
                name
                for name in stage_names
                if _atomic_writer_temp_target(name) in SIGNAL_COMPACTION_STAGE_NAMES
            )
            if len(visible_stage_names) + len(temp_stage_names) != len(stage_names):
                raise ValidationError("resident signal compaction operation is invalid")
            archive_matches = {
                path: content
                for path, content in archive_files.items()
                if path.endswith(f"-{token}.jsonl")
            }
            if not archive_matches:
                for name in (*visible_stage_names, *temp_stage_names):
                    path = self.vault_root / operation_relative / name
                    staged = self._read_optional_state(
                        store,
                        path,
                        label="resident signal compaction stage",
                        max_bytes=MAX_SIGNAL_QUEUE_BYTES,
                    )
                    if staged is None:
                        raise ConflictError(
                            "resident signal compaction operation changed during cleanup"
                        )
                    self._unlink_private_exact(
                        store,
                        path,
                        expected=staged,
                        label="resident signal compaction stage",
                    )
                self._remove_empty_operation(store, operation_relative)
                continue
            if len(archive_matches) != 2:
                raise ValidationError(
                    "resident signal compaction operation is not proven committed"
                )
            targets: dict[str, bytes | None] = {
                "archive_acknowledgements.jsonl": next(
                    (
                        content
                        for path, content in archive_matches.items()
                        if Path(path).name.startswith("acks-")
                    ),
                    None,
                ),
                "archive_inputs.jsonl": next(
                    (
                        content
                        for path, content in archive_matches.items()
                        if Path(path).name.startswith("inputs-")
                    ),
                    None,
                ),
                "live_acknowledgements.jsonl": acknowledgements_state or b"",
                "live_inputs.jsonl": inputs_state or b"",
                "settled_event_keys.jsonl": settled_state,
            }
            for name in visible_stage_names:
                expected = targets[name]
                if expected is None:
                    raise ValidationError(
                        "resident signal compaction operation is not proven committed"
                    )
                path = self.vault_root / operation_relative / name
                staged = self._read_optional_state(
                    store,
                    path,
                    label="resident signal compaction stage",
                    max_bytes=MAX_SIGNAL_QUEUE_BYTES,
                )
                if staged is None or staged != expected:
                    raise ValidationError(
                        "resident signal compaction operation differs from canonical storage"
                    )
            if stage_names != self._directory_entry_names(
                store,
                operation_relative,
                max_entries=6,
            ):
                raise ConflictError(
                    "resident signal compaction operation changed while it was read"
                )
            for name in (*visible_stage_names, *temp_stage_names):
                path = self.vault_root / operation_relative / name
                staged = self._read_optional_state(
                    store,
                    path,
                    label="resident signal compaction stage",
                    max_bytes=MAX_SIGNAL_QUEUE_BYTES,
                )
                if staged is not None:
                    self._unlink_private_exact(
                        store,
                        path,
                        expected=staged,
                        label="resident signal compaction stage",
                    )
            self._remove_empty_operation(store, operation_relative)
        remaining = self._directory_entry_names(
            store,
            operations_relative,
            max_entries=MAX_SIGNAL_OPERATIONS,
        )
        if remaining:
            raise ConflictError("resident signal compaction operations changed during cleanup")

    def _remove_empty_operation(
        self,
        store: PinnedPathRoot | None,
        operation_relative: Path,
    ) -> None:
        operation_root = self.vault_root / operation_relative
        if store is None:
            try:
                operation_root.rmdir()
            except OSError as exc:
                raise ValidationError(
                    "resident signal compaction operation could not be removed"
                ) from exc
            return
        store.retain_directory(operation_relative)
        store.remove_retained_empty_directory(
            operation_relative,
            label="resident signal compaction operation",
        )

    def _directory_entry_names(
        self,
        store: PinnedPathRoot | None,
        relative: Path,
        *,
        max_entries: int,
        suffix: str | None = None,
    ) -> tuple[str, ...]:
        if store is not None:
            if not store.directory_exists(relative):
                return ()
            store.retain_directory(relative)
            pinned_names = store.list_directory_entry_names(
                relative,
                max_entries=max_entries,
                suffix=suffix,
            )
            store.retain_directory(relative)
            return pinned_names
        directory = self.vault_root / relative
        if not os.path.lexists(directory):
            return ()
        metadata = os.lstat(directory)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError("resident signal directory storage is invalid")
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        try:
            with os.scandir(directory) as entries:
                names: list[str] = []
                for seen, entry in enumerate(entries, 1):
                    if seen > max_entries:
                        raise ValidationError("resident signal directory exceeds its entry bound")
                    if suffix is None or entry.name.endswith(suffix):
                        names.append(entry.name)
        except OSError as exc:
            raise ValidationError("resident signal directory storage is unavailable") from exc
        try:
            current = os.lstat(directory)
        except OSError as exc:
            raise ValidationError("resident signal directory changed while it was listed") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (int(current.st_dev), int(current.st_ino)) != identity
        ):
            raise ValidationError("resident signal directory changed while it was listed")
        return tuple(sorted(names))

    def _replace_exact(
        self,
        store: PinnedPathRoot | None,
        path: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int = MAX_SIGNAL_QUEUE_BYTES,
    ) -> None:
        if store is not None:
            store.exchange_regular_file_if_exact(
                path.relative_to(self.vault_root),
                expected=expected,
                replacement=replacement,
                label=label,
                max_bytes=max_bytes,
            )
            return
        current = self._read_optional_state(
            None,
            path,
            label=label,
            max_bytes=max_bytes,
        )
        if current != expected:
            raise ConflictError(f"{label} changed before publication")
        atomic_write(path, replacement)

    def _unlink_private_exact(
        self,
        store: PinnedPathRoot | None,
        path: Path,
        *,
        expected: bytes,
        label: str,
        max_bytes: int = MAX_SIGNAL_QUEUE_BYTES,
    ) -> None:
        if store is not None:
            store.unlink_private_regular_file_if_exact(
                path.relative_to(self.vault_root),
                expected=expected,
                label=label,
                max_bytes=max_bytes,
            )
            return
        current = self._read_optional_state(
            None,
            path,
            label=label,
            max_bytes=max_bytes,
        )
        if current != expected:
            raise ConflictError(f"{label} changed before removal")
        durable_unlink(path)

    def _read_optional_state(
        self,
        store: PinnedPathRoot | None,
        path: Path,
        *,
        label: str,
        max_bytes: int,
        retain: bool = True,
    ) -> bytes | None:
        if store is not None:
            return store.read_regular_file(
                path.relative_to(self.vault_root),
                label=label,
                max_bytes=max_bytes,
                missing_ok=True,
                retain=retain,
            )
        if not os.path.lexists(path):
            return None
        return read_regular_file(path, label=label, max_bytes=max_bytes)


def signal_dict(
    value: ResidentSignal | SignalAcknowledgement | SignalCompaction | SignalQueueStatus,
) -> dict[str, Any]:
    return asdict(value)


def signal_view_dict(value: SignalQueueView) -> dict[str, Any]:
    return {
        "acknowledged_ids": list(value.acknowledged_ids),
        "next_cursor": value.next_cursor,
        "remaining": value.remaining,
        "revision": value.revision,
        "signals": [signal_dict(item) for item in value.signals],
    }


def build_settled_event_index(files: Mapping[str, bytes]) -> bytes:
    """Validate a portable signal history and derive its compacted-key replay ledger."""

    active_inputs = b""
    active_acknowledgements = b""
    supplied_index: bytes | None = None
    archives: dict[str, dict[str, bytes]] = {}
    archive_pattern = re.compile(
        r"^\.gsv/signals/archive/(?P<kind>inputs|acks)-"
        r"(?P<stamp>[0-9A-Za-z]+)-(?P<token>[0-9a-f]{32})\.jsonl$"
    )
    for path, encoded in files.items():
        if not isinstance(path, str) or not isinstance(encoded, bytes):
            raise ValidationError("resident signal file set is invalid")
        if path == ".gsv/signals/inputs.jsonl":
            active_inputs = encoded
            continue
        if path == ".gsv/signals/acks.jsonl":
            active_acknowledgements = encoded
            continue
        if path == SETTLED_EVENT_KEYS_RELATIVE:
            supplied_index = encoded
            continue
        match = archive_pattern.fullmatch(path)
        if match is None:
            raise ValidationError(f"resident signal file path is unsupported: {path}")
        key = f"{match.group('stamp')}-{match.group('token')}"
        group = archives.setdefault(key, {})
        kind = match.group("kind")
        if kind in group:
            raise ValidationError("resident signal archive contains duplicate paths")
        group[kind] = encoded

    active_signals = _parse_signals(active_inputs)
    active_acks = _parse_acknowledgements(active_acknowledgements)
    _validate_acknowledgement_targets(active_signals, active_acks)
    all_signals = list(active_signals)
    all_acknowledgements = list(active_acks)
    archived_signals: list[ResidentSignal] = []
    for key in sorted(archives):
        group = archives[key]
        if set(group) != {"inputs", "acks"}:
            raise ValidationError("resident signal archive must contain an exact inputs/acks pair")
        signals = _parse_signals(group["inputs"])
        acknowledgements = _parse_acknowledgements(group["acks"])
        _validate_acknowledgement_targets(signals, acknowledgements)
        if {item.input_id for item in signals} != {item.input_id for item in acknowledgements}:
            raise ValidationError("resident signal archive contains unsettled inputs")
        archived_signals.extend(signals)
        all_signals.extend(signals)
        all_acknowledgements.extend(acknowledgements)

    derived = _merge_settled_event_keys((), tuple(archived_signals))
    _validate_live_settled_separation(active_signals, derived)
    _validate_global_signal_history(tuple(all_signals), tuple(all_acknowledgements))
    canonical = _encode_settled_event_keys(derived)
    if len(canonical) > MAX_SIGNAL_QUEUE_BYTES:
        raise ValidationError("resident signal settled event-key ledger is too large")
    if supplied_index is not None and _parse_settled_event_keys(supplied_index) != derived:
        raise ValidationError("resident signal settled event-key ledger differs from its archives")
    return canonical


def _is_owned_signal_archive_temp_name(name: str) -> bool:
    """Recognize only the atomic writer's exact archive-temp namespace."""

    return _owned_signal_archive_temp_target(name) is not None


def _owned_signal_archive_temp_target(name: str) -> str | None:
    target_name = _atomic_writer_temp_target(name)
    if target_name is not None and SIGNAL_ARCHIVE_NAME.fullmatch(target_name) is not None:
        return target_name
    return None


def _atomic_writer_temp_target(name: str) -> str | None:
    if not name.startswith("."):
        return None
    target_name = ""
    token_is_valid = False
    if ".seld-stage-" in name:
        target_name, token = name[1:].rsplit(".seld-stage-", 1)
        token_is_valid = re.fullmatch(r"[0-9a-f]{32}", token) is not None
    elif ".tmp-" in name:
        target_name, token = name[1:].rsplit(".tmp-", 1)
        token_is_valid = re.fullmatch(r"[0-9a-f]{32}|[a-z0-9_]{8}", token) is not None
    if token_is_valid:
        return target_name
    return None


def settled_event_key_count(encoded: bytes) -> int:
    """Return the validated content-free replay-receipt count."""

    return len(_parse_settled_event_keys(encoded))


def _parse_signals(encoded: bytes) -> tuple[ResidentSignal, ...]:
    values = _parse_jsonl(encoded, "resident signal input")
    results: list[ResidentSignal] = []
    event_keys: dict[str, ResidentSignal] = {}
    identifiers: set[str] = set()
    for value in values:
        if set(value) != {"input_id", "observed_at", "kind", "ref", "event_key", "envelope"}:
            raise ValidationError("resident signal input has an unsupported shape")
        envelope = value.get("envelope")
        if not isinstance(envelope, dict):
            raise ValidationError("resident signal envelope must be an object")
        clean_envelope = _json_object(envelope)
        signal = ResidentSignal(
            input_id=_uuid(value.get("input_id"), "signal ID"),
            observed_at=_time(value.get("observed_at"), "signal observation time"),
            kind=_token(value.get("kind"), "signal kind"),
            ref=_optional_line(value.get("ref"), 1_000, "signal reference"),
            event_key=_optional_line(value.get("event_key"), 1_000, "signal event key"),
            envelope=clean_envelope,
        )
        if signal.input_id in identifiers:
            raise ValidationError("resident signal IDs must be unique")
        identifiers.add(signal.input_id)
        if signal.event_key is not None:
            prior = event_keys.get(signal.event_key)
            if prior is not None and prior != signal:
                raise ValidationError("resident signal event keys must be unique")
            event_keys[signal.event_key] = signal
        results.append(signal)
    return tuple(results)


def _parse_settled_event_keys(encoded: bytes) -> tuple[_SettledEventKey, ...]:
    values = _parse_jsonl(encoded, "resident signal settled event-key")
    header: dict[str, object] | None = None
    header_receipt_count: int | None = None
    receipt_values = values
    if values and set(values[0]) == {
        "format_version",
        "kind",
        "receipt_count",
        "receipts_sha256",
    }:
        header = values[0]
        receipt_values = values[1:]
        if (
            type(header["format_version"]) is not int
            or header["format_version"] != SETTLED_EVENT_INDEX_FORMAT_VERSION
            or header["kind"] != "settled-event-key-index"
        ):
            raise ValidationError(
                "resident signal settled event-key index has an unsupported version"
            )
        raw_receipt_count = header["receipt_count"]
        if (
            not isinstance(raw_receipt_count, int)
            or isinstance(raw_receipt_count, bool)
            or raw_receipt_count < 0
        ):
            raise ValidationError("resident signal settled event-key index count is invalid")
        header_receipt_count = raw_receipt_count
        _sha256(header["receipts_sha256"], "settled event-key index digest")
    results: list[_SettledEventKey] = []
    event_key_digests: set[str] = set()
    input_ids: set[str] = set()
    prior_digest: str | None = None
    for value in receipt_values:
        if set(value) != {
            "event_key_digest",
            "format_version",
            "input_id",
            "observed_at",
            "signal_digest",
        }:
            raise ValidationError(
                "resident signal settled event-key receipt has an unsupported shape"
            )
        if (
            type(value["format_version"]) is not int
            or value["format_version"] != SETTLED_EVENT_KEY_FORMAT_VERSION
        ):
            raise ValidationError(
                "resident signal settled event-key receipt has an unsupported version"
            )
        event_key_digest = _sha256(value["event_key_digest"], "settled event-key digest")
        signal_digest = _sha256(value["signal_digest"], "settled signal digest")
        item = _SettledEventKey(
            format_version=SETTLED_EVENT_KEY_FORMAT_VERSION,
            event_key_digest=event_key_digest,
            signal_digest=signal_digest,
            input_id=_uuid(value["input_id"], "settled signal ID"),
            observed_at=_time(value["observed_at"], "settled signal observation time"),
        )
        if prior_digest is not None and event_key_digest <= prior_digest:
            raise ValidationError(
                "resident signal settled event-key receipts must be uniquely sorted"
            )
        if event_key_digest in event_key_digests or item.input_id in input_ids:
            raise ValidationError("resident signal settled event-key receipts must be unique")
        prior_digest = event_key_digest
        event_key_digests.add(event_key_digest)
        input_ids.add(item.input_id)
        results.append(item)
    if header is not None:
        assert header_receipt_count is not None
        receipt_bytes = _encode_settled_event_key_rows(tuple(results))
        if header_receipt_count != len(results) or str(header["receipts_sha256"]) != sha256_bytes(
            receipt_bytes
        ):
            raise ValidationError(
                "resident signal settled event-key index does not match its receipts"
            )
        if encoded != _encode_settled_event_keys(tuple(results)):
            raise ValidationError("resident signal settled event-key index is not canonical")
    return tuple(results)


def _settled_index_has_current_header(encoded: bytes) -> bool:
    first = encoded.splitlines()[:1]
    if not first:
        return False
    try:
        value = json.loads(first[0])
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and set(value) == {
        "format_version",
        "kind",
        "receipt_count",
        "receipts_sha256",
    }


def _parse_acknowledgements(encoded: bytes) -> tuple[SignalAcknowledgement, ...]:
    values = _parse_jsonl(encoded, "resident signal acknowledgement")
    results: list[SignalAcknowledgement] = []
    by_input: dict[str, SignalAcknowledgement] = {}
    acknowledgement_ids: set[str] = set()
    for value in values:
        legacy_keys = {
            "acknowledgement_id",
            "input_id",
            "acknowledged_at",
            "consumer",
        }
        current_keys = legacy_keys | {"disposition", "result_refs"}
        if set(value) not in (legacy_keys, current_keys):
            raise ValidationError("resident signal acknowledgement has an unsupported shape")
        disposition = _optional_disposition(value.get("disposition"))
        raw_refs = value.get("result_refs", ())
        if not isinstance(raw_refs, (list, tuple)):
            raise ValidationError("signal acknowledgement result references must be a list")
        result_refs = _result_refs(raw_refs, strict=False)
        if disposition is None and result_refs:
            raise ValidationError("legacy signal acknowledgement has result references")
        if disposition is not None and not result_refs:
            raise ValidationError("signal acknowledgement requires a durable disposition reference")
        acknowledgement = SignalAcknowledgement(
            acknowledgement_id=_uuid(value.get("acknowledgement_id"), "signal acknowledgement ID"),
            input_id=_uuid(value.get("input_id"), "signal ID"),
            acknowledged_at=_time(value.get("acknowledged_at"), "signal acknowledgement time"),
            consumer=_token(value.get("consumer"), "signal consumer"),
            disposition=disposition,
            result_refs=result_refs,
        )
        if acknowledgement.acknowledgement_id in acknowledgement_ids:
            raise ValidationError("resident signal acknowledgement IDs must be unique")
        acknowledgement_ids.add(acknowledgement.acknowledgement_id)
        prior = by_input.get(acknowledgement.input_id)
        if prior is not None:
            raise ValidationError("resident signal has duplicate acknowledgements")
        by_input[acknowledgement.input_id] = acknowledgement
        results.append(acknowledgement)
    return tuple(results)


def _validate_acknowledgement_targets(
    signals: tuple[ResidentSignal, ...],
    acknowledgements: tuple[SignalAcknowledgement, ...],
) -> None:
    known = {item.input_id for item in signals}
    unknown = next(
        (item.input_id for item in acknowledgements if item.input_id not in known),
        None,
    )
    if unknown is not None:
        raise ValidationError(
            f"resident signal acknowledgement references an unknown signal: {unknown}"
        )


def _validate_global_signal_history(
    signals: tuple[ResidentSignal, ...],
    acknowledgements: tuple[SignalAcknowledgement, ...],
) -> None:
    input_ids: set[str] = set()
    event_keys: set[str] = set()
    for signal in signals:
        if signal.input_id in input_ids:
            raise ValidationError("resident signal history contains duplicate signal IDs")
        input_ids.add(signal.input_id)
        if signal.event_key is not None:
            if signal.event_key in event_keys:
                raise ValidationError("resident signal history contains duplicate event keys")
            event_keys.add(signal.event_key)
    acknowledgement_ids: set[str] = set()
    acknowledged_inputs: set[str] = set()
    for acknowledgement in acknowledgements:
        if acknowledgement.acknowledgement_id in acknowledgement_ids:
            raise ValidationError("resident signal history contains duplicate acknowledgement IDs")
        if acknowledgement.input_id in acknowledged_inputs:
            raise ValidationError("resident signal history contains duplicate acknowledgements")
        if acknowledgement.input_id not in input_ids:
            raise ValidationError(
                "resident signal history acknowledgement references an unknown signal"
            )
        acknowledgement_ids.add(acknowledgement.acknowledgement_id)
        acknowledged_inputs.add(acknowledgement.input_id)


def _merge_settled_event_keys(
    existing: tuple[_SettledEventKey, ...],
    archived_signals: tuple[ResidentSignal, ...],
) -> tuple[_SettledEventKey, ...]:
    by_digest = {item.event_key_digest: item for item in existing}
    by_input = {item.input_id: item for item in existing}
    for signal in archived_signals:
        if signal.event_key is None:
            continue
        item = _SettledEventKey(
            format_version=SETTLED_EVENT_KEY_FORMAT_VERSION,
            event_key_digest=_event_key_digest(signal.event_key),
            signal_digest=_signal_digest(signal),
            input_id=signal.input_id,
            observed_at=signal.observed_at,
        )
        prior_key = by_digest.get(item.event_key_digest)
        prior_input = by_input.get(item.input_id)
        if (prior_key is not None and prior_key != item) or (
            prior_input is not None and prior_input != item
        ):
            raise ValidationError("resident signal history contains conflicting settled event keys")
        by_digest[item.event_key_digest] = item
        by_input[item.input_id] = item
    return tuple(sorted(by_digest.values(), key=lambda item: item.event_key_digest))


def _validate_live_settled_separation(
    signals: tuple[ResidentSignal, ...],
    settled: tuple[_SettledEventKey, ...],
) -> None:
    settled_digests = {item.event_key_digest for item in settled}
    overlap = next(
        (
            signal.event_key
            for signal in signals
            if signal.event_key is not None
            and _event_key_digest(signal.event_key) in settled_digests
        ),
        None,
    )
    if overlap is not None:
        raise ValidationError("resident signal event key appears in both live and settled storage")


def _event_key_digest(event_key: str) -> str:
    return sha256_bytes(event_key.encode("utf-8"))


def _signal_digest(signal: ResidentSignal) -> str:
    identity = {
        "envelope": signal.envelope,
        "event_key": signal.event_key,
        "kind": signal.kind,
        "ref": signal.ref,
    }
    return sha256_bytes(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _cursor_offset(
    cursor: str | None,
    *,
    expected_revision: str,
    expected_mode: str,
) -> int:
    if cursor is None:
        return 0
    parts = cursor.split(":") if isinstance(cursor, str) else []
    if len(parts) != 3 or parts[0] != expected_revision or parts[1] != expected_mode:
        raise ConflictError("resident signal queue changed; restart listing from the first page")
    try:
        offset = int(parts[2])
    except ValueError as exc:
        raise ValidationError("resident signal cursor is invalid") from exc
    if offset < 0 or str(offset) != parts[2]:
        raise ValidationError("resident signal cursor is invalid")
    return offset


def _parse_jsonl(encoded: bytes, label: str) -> tuple[dict[str, object], ...]:
    if len(encoded) > MAX_SIGNAL_QUEUE_BYTES:
        raise ValidationError(f"{label} queue is too large")
    values: list[dict[str, object]] = []
    for line_number, line in enumerate(encoded.splitlines(), 1):
        if len(line) > MAX_SIGNAL_EVENT_BYTES:
            raise ValidationError(f"{label} line {line_number} is too large")
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"{label} line {line_number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"{label} line {line_number} must be an object")
        values.append(value)
    return tuple(values)


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    if any(not isinstance(key, str) for key in value):
        raise ValidationError("resident signal envelope keys must be strings")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValidationError("resident signal envelope must be bounded JSON") from exc
    if not isinstance(decoded, dict) or len(encoded.encode("utf-8")) > MAX_SIGNAL_EVENT_BYTES // 2:
        raise ValidationError("resident signal envelope must be a bounded object")
    return decoded


def _json_line(value: dict[str, Any], *, label: str) -> bytes:
    encoded = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_SIGNAL_EVENT_BYTES:
        raise ValidationError(f"{label} is too large")
    return encoded


def _bounded_json(value: dict[str, object], *, label: str) -> bytes:
    try:
        encoded = (
            json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - internal fixed shapes only.
        raise ValidationError(f"{label} is invalid") from exc
    if len(encoded) > MAX_COMPACTION_MARKER_BYTES:
        raise ValidationError(f"{label} is too large")
    return encoded


def _parse_compaction_marker(encoded: bytes, *, vault_root: Path) -> dict[str, object]:
    try:
        value = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("resident signal compaction marker is invalid") from exc
    keys = {
        "archive_acknowledgements",
        "archive_inputs",
        "digests",
        "format_version",
        "phase",
        "token",
    }
    if not isinstance(value, dict) or set(value) not in (keys, keys | {"expected_digests"}):
        raise ValidationError("resident signal compaction marker has an unsupported shape")
    format_version = value["format_version"]
    if type(format_version) is not int or format_version not in {
        1,
        SIGNAL_COMPACTION_FORMAT_VERSION,
    }:
        raise ValidationError("resident signal compaction marker has an unsupported version")
    if value["phase"] != "prepared":
        raise ValidationError("resident signal compaction phase is invalid")
    token = value["token"]
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ValidationError("resident signal compaction token is invalid")
    for key, prefix in (
        ("archive_inputs", "inputs"),
        ("archive_acknowledgements", "acks"),
    ):
        relative = value[key]
        pattern = rf"\.gsv/signals/archive/{prefix}-[0-9A-Za-z]+-{token}\.jsonl"
        if not isinstance(relative, str) or re.fullmatch(pattern, relative) is None:
            raise ValidationError("resident signal compaction archive path is invalid")
        target = (vault_root / relative).resolve(strict=False)
        try:
            target.relative_to((vault_root / ".gsv/signals/archive").resolve(strict=False))
        except ValueError as exc:  # pragma: no cover - exact grammar already excludes traversal.
            raise ValidationError(
                "resident signal compaction archive path escapes the vault"
            ) from exc
    digests = value["digests"]
    digest_keys = {
        "archive_acknowledgements",
        "archive_inputs",
        "live_acknowledgements",
        "live_inputs",
    }
    if format_version == SIGNAL_COMPACTION_FORMAT_VERSION:
        digest_keys.add("settled_event_keys")
    if not isinstance(digests, dict) or set(digests) != digest_keys:
        raise ValidationError("resident signal compaction digests are invalid")
    if any(
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in digests.values()
    ):
        raise ValidationError("resident signal compaction digest is invalid")
    expected_digests = value.get("expected_digests")
    if expected_digests is not None:
        expected_keys = {"live_acknowledgements", "live_inputs"}
        if format_version == SIGNAL_COMPACTION_FORMAT_VERSION:
            expected_keys.add("settled_event_keys")
        if not isinstance(expected_digests, dict) or set(expected_digests) != expected_keys:
            raise ValidationError("resident signal compaction expected digests are invalid")
        if any(
            digest is not None
            and (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None)
            for digest in expected_digests.values()
        ):
            raise ValidationError("resident signal compaction expected digest is invalid")
    return value


def _encode_jsonl(
    values: tuple[ResidentSignal, ...] | tuple[SignalAcknowledgement, ...],
) -> bytes:
    return b"".join(
        _json_line(
            (
                asdict(value)
                if isinstance(value, ResidentSignal)
                else _acknowledgement_storage_dict(value)
            ),
            label=(
                "resident signal input"
                if isinstance(value, ResidentSignal)
                else "resident signal acknowledgement"
            ),
        )
        for value in values
    )


def _encode_settled_event_keys(values: tuple[_SettledEventKey, ...]) -> bytes:
    receipts = _encode_settled_event_key_rows(values)
    header = _json_line(
        {
            "format_version": SETTLED_EVENT_INDEX_FORMAT_VERSION,
            "kind": "settled-event-key-index",
            "receipt_count": len(values),
            "receipts_sha256": sha256_bytes(receipts),
        },
        label="resident signal settled event-key index",
    )
    return header + receipts


def _encode_settled_event_key_rows(values: tuple[_SettledEventKey, ...]) -> bytes:
    return b"".join(
        _json_line(asdict(value), label="resident signal settled event-key receipt")
        for value in values
    )


def _queue_revision(inputs: bytes, acknowledgements: bytes) -> str:
    return sha256_bytes(inputs + b"\0" + acknowledgements)


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc
    if canonical != value.casefold():
        raise ValidationError(f"{label} must be canonical")
    return canonical


def _sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _token(value: object, label: str) -> str:
    if not isinstance(value, str) or SAFE_SIGNAL_TOKEN.fullmatch(value) is None:
        raise ValidationError(f"{label} is invalid")
    return value


def _optional_disposition(value: object) -> str | None:
    if value is None:
        return None
    if value not in {"accepted", "rejected"}:
        raise ValidationError("signal disposition must be accepted or rejected")
    return str(value)


def _result_refs(values: Sequence[object], *, strict: bool) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValidationError("signal result references must be a sequence")
    if len(values) > MAX_SIGNAL_RESULT_REFS:
        raise ValidationError(
            f"signal acknowledgement accepts at most {MAX_SIGNAL_RESULT_REFS} result references"
        )
    if strict:
        return tuple(_durable_ref(value, "signal result reference") for value in values)
    cleaned = tuple(_optional_line(value, 1_000, "signal result reference") for value in values)
    if any(value is None for value in cleaned):
        raise ValidationError("signal result reference is invalid")
    return tuple(value for value in cleaned if value is not None)


def _acknowledgement_storage_dict(value: SignalAcknowledgement) -> dict[str, Any]:
    payload = asdict(value)
    payload["result_refs"] = list(value.result_refs)
    if value.disposition is None:
        payload.pop("disposition")
        payload.pop("result_refs")
    return payload


def _optional_line(value: object, maximum: int, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text or null")
    clean = " ".join(value.split())
    if not clean or len(clean) > maximum:
        raise ValidationError(f"{label} is invalid")
    return clean


def _durable_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or SAFE_DURABLE_REF.fullmatch(value) is None:
        raise ValidationError(f"{label} must be an opaque record revision reference")
    return value


def _time(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    return stored_time(value, label)
