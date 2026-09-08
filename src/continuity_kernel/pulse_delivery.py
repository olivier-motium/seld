"""Restart-safe delivery boundaries for the event-driven resident Pulse.

This module contains operational receipts only.  Source-report meaning remains
in canonical Markdown, while the Pulse task remains the sole integration
authority.  In particular, an accepted Codex queue request proves only that a
wake was queued; it never proves that Pulse consumed or integrated the report.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from continuity_kernel.atomic import (
    PINNED_PATH_ROOT_SUPPORTED,
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    atomic_write,
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
from continuity_kernel.pulse_reports import PulseReport, PulseReportStore
from continuity_kernel.records import (
    RESIDENT_PULSE_REF,
    RESIDENT_PULSE_TASK_ID,
    TERMINAL_TASK_STATUSES,
    format_time,
    stored_time,
)
from continuity_kernel.vault import Vault

DELIVERY_FORMAT_VERSION: Final = 1
MAX_DELIVERY_STATE_BYTES: Final = 512 * 1024
MAX_WAKE_REPORTS: Final = 20
MAX_OUTSTANDING_WAKES: Final = 2
MAX_WAKE_REQUESTS: Final = 1_000
MAX_INTEGRATIONS: Final = 2_000
MAX_CURATION_OUTBOX: Final = 2_000
MAX_SUMMARY_BYTES: Final = 1_200
MAX_RECEIPT: Final = 1_000
MAX_RESULT_REFS: Final = 20
FALLBACK_AFTER: Final = timedelta(minutes=30)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CHIEF_REF = re.compile(
    r"^codex-chief-of-staff:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_REPORT_REF = re.compile(
    r"^source-report:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})@([0-9a-f]{64})$"
)
_NATIVE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}@[0-9a-f]{64}$")

WakeState = Literal["prepared", "queued", "uncertain"]
WakeQueueState = WakeState | Literal["noop"]
CurationState = Literal["prepared", "queued", "completed", "noop", "blocked", "uncertain"]
_WAKE_STATES: Final = frozenset({"prepared", "queued", "uncertain"})
_CURATION_STATES: Final = frozenset(
    {"prepared", "queued", "completed", "noop", "blocked", "uncertain"}
)
QueueRunner = Callable[[Sequence[str]], int]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class NativePulseBinding:
    task_id: str
    task_revision: str
    pulse_thread_id: str
    chief_thread_id: str


@dataclass(frozen=True)
class WakeRequest:
    identifier: str
    event_key: str
    report_ids: tuple[str, ...]
    report_refs: tuple[str, ...]
    pulse_thread_id: str
    state: WakeState
    prepared_at: str
    updated_at: str
    revision: str

    @property
    def fallback_at(self) -> str:
        return format_time(_parsed_time(self.prepared_at) + FALLBACK_AFTER)


@dataclass(frozen=True)
class WakeQueueResult:
    request_id: str | None
    state: WakeQueueState
    report_refs: tuple[str, ...]
    pulse_thread_id: str
    fallback_at: str | None


@dataclass(frozen=True)
class DianeOutbox:
    identifier: str
    event_key: str
    source_gsv_revision: str
    result_refs: tuple[str, ...]
    target_desktop_work_id: str | None
    curation_followup: bool
    summary: str
    observed_at: str
    accepted_at: str
    state: CurationState
    receipt: str | None
    updated_at: str
    revision: str


@dataclass(frozen=True)
class CurationList:
    outbox: tuple[DianeOutbox, ...]
    remaining: int


@dataclass(frozen=True)
class PulseIntegrationResult:
    report: PulseReport
    report_ref: str
    outbox_id: str | None
    integration_state: Literal["integrated"]


@dataclass(frozen=True)
class PulseDeliveryStatus:
    binding: NativePulseBinding | None
    binding_error: str | None
    wake_requests: tuple[WakeRequest, ...]
    fallback_request_ids: tuple[str, ...]
    pending_relevance: int
    pending_delivery: int
    pending_investigation: int
    pending_curation: int


@dataclass(frozen=True)
class _Integration:
    identifier: str
    report_id: str
    expected_revision: str
    pulse_thread_id: str
    result_refs: tuple[str, ...]
    primary_result_ref: str
    target_desktop_work_id: str | None
    curation_followup: bool
    base_integration_id: str | None
    summary: str
    interface_change: bool
    accepted_at: str
    state: Literal["prepared", "integrated"]
    delivered_report_ref: str | None
    outbox_id: str | None


@dataclass(frozen=True)
class _DeliveryState:
    wake_requests: tuple[WakeRequest, ...] = ()
    integrations: tuple[_Integration, ...] = ()
    outbox: tuple[DianeOutbox, ...] = ()


@dataclass(frozen=True)
class _WakeReservation:
    request: WakeRequest | None
    created: bool
    report_ids: tuple[str, ...]
    report_refs: tuple[str, ...]


class PulseDelivery:
    """Keep Pulse queuing, semantic integration, and UI curation distinct."""

    def __init__(
        self,
        vault: Vault,
        *,
        queue_runner: QueueRunner | None = None,
        now: Clock | None = None,
    ) -> None:
        self.vault = vault
        self.reports = PulseReportStore(vault.root)
        self._queue_runner = queue_runner or _run_codex_queue
        self._now = now or (lambda: datetime.now(UTC))
        self.vault_root = vault.root
        self.root = self.vault_root / ".gsv/pulse-delivery"

    @property
    def lock_path(self) -> Path:
        return self.vault_root / ".gsv/locks/pulse-delivery.lock"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def queue_wake(self, report_ids: Sequence[str]) -> WakeQueueResult:
        """Persist one queue receipt before asking Codex to wake the exact Pulse task."""

        binding = self._binding()
        ids = _report_ids(report_ids)
        reports = tuple(self.reports.show(identifier) for identifier in ids)
        for report in reports:
            if (
                report.decision is None
                or report.decision.decision != "wake"
                or report.delivery is not None
            ):
                raise ValidationError(
                    "Pulse wake requires reports with an undelivered wake decision"
                )
        refs = tuple(report.report_ref for report in reports)
        pending_ids = frozenset(
            report.identifier
            for report in self.reports.list_pending(stage="delivery", limit=1_000).reports
        )
        reservation = self._reserve_wake(
            report_ids=ids,
            report_refs=refs,
            pulse_thread_id=binding.pulse_thread_id,
            pending_report_ids=pending_ids,
        )
        if not reservation.created:
            if reservation.request is None:
                return _noop_wake_result(refs, binding.pulse_thread_id)
            request = reservation.request
            if request.state == "prepared":
                request = self._set_wake_state(request.identifier, "uncertain")
            return _wake_result(request, now=self._now())

        prepared = reservation.request
        assert prepared is not None
        request = prepared
        ids = reservation.report_ids
        refs = reservation.report_refs

        try:
            for ref in refs:
                self.vault.append_canonical_signal(record_ref=ref, change_type="observation")
        except Exception:
            return _wake_result(
                self._set_wake_state(request.identifier, "uncertain"), now=self._now()
            )

        message = _wake_message(ids)
        command = ("codex", "queue", "--thread", binding.pulse_thread_id, "--message", message)
        try:
            code = self._queue_runner(command)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            code = -1
        return _wake_result(
            self._set_wake_state(request.identifier, "queued" if code == 0 else "uncertain"),
            now=self._now(),
        )

    def integrate(
        self,
        report_id: str,
        *,
        expected_revision: str,
        pulse_thread_id: str,
        result_refs: Sequence[str],
        summary: str,
        interface_change: bool,
        target_desktop_work_id: str | None = None,
    ) -> PulseIntegrationResult:
        """Persist a completed Pulse integration, then optionally queue UI curation.

        This accepts only canonical results that exist at the readback boundary.
        A prepared integration receipt lets an identical retry finish after a
        crash between report delivery and operational-state publication.
        """

        identifier = _uuid(report_id, "pulse report ID")
        expected = _revision(expected_revision, "expected pulse report revision")
        binding = self._binding(required_thread_id=pulse_thread_id)
        report = self.reports.show(identifier)
        clean_summary = _summary(summary)
        if not isinstance(interface_change, bool):
            raise ValidationError("Pulse interface_change must be boolean")
        target = _optional_uuid(target_desktop_work_id, "target desktop work ID")
        if target is not None and not interface_change:
            raise ValidationError("Pulse target desktop work requires an interface change")

        state = self._snapshot()
        existing = _find_integration(state, report_id=identifier, expected_revision=expected)
        if existing is None and report.revision != expected:
            raise ConflictError("pulse report changed; reload it before integrating")

        supplied_refs = _native_refs(result_refs, "Pulse integration result reference")
        if existing is None:
            followup_base = self._followup_base(
                state,
                report=report,
                expected_revision=expected,
                interface_change=interface_change,
            )
            canonical_refs = self._resolve_results(
                report=report,
                result_refs=supplied_refs,
                interface_change=interface_change,
                target_desktop_work_id=target,
            )
            if followup_base is not None:
                _validate_followup_results(canonical_refs, followup_base)
            integration, _ = self._prepare_integration(
                report=report,
                expected_revision=expected,
                binding=binding,
                result_refs=canonical_refs,
                summary=clean_summary,
                interface_change=interface_change,
                target_desktop_work_id=target,
                followup_base=followup_base,
            )
        else:
            _match_integration(
                existing,
                pulse_thread_id=binding.pulse_thread_id,
                result_refs=supplied_refs,
                summary=clean_summary,
                interface_change=interface_change,
                target_desktop_work_id=target,
            )
            integration = existing
            if integration.state == "integrated":
                _require_integrated_report(report, integration)
                return PulseIntegrationResult(
                    report=report,
                    report_ref=report.report_ref,
                    outbox_id=integration.outbox_id,
                    integration_state="integrated",
                )
            if report.revision == expected:
                self._resolve_results(
                    report=report,
                    result_refs=integration.result_refs,
                    interface_change=integration.interface_change,
                    target_desktop_work_id=integration.target_desktop_work_id,
                )
            else:
                _require_recoverable_delivery(report, integration)
                for ref in integration.result_refs:
                    if ref != integration.primary_result_ref or not ref.startswith(
                        "source-report:"
                    ):
                        self.vault.resolve_canonical_result_ref(ref)

        current = self.reports.show(identifier)
        if integration.curation_followup:
            _require_recoverable_delivery(current, integration)
            delivered = current
        elif current.revision == expected:
            delivered = self.reports.record_delivery(
                identifier,
                expected_revision=expected,
                result_ref=integration.primary_result_ref,
            )
        else:
            _require_recoverable_delivery(current, integration)
            delivered = current
        finalized = self._finalize_integration(integration.identifier, delivered)
        return PulseIntegrationResult(
            report=delivered,
            report_ref=delivered.report_ref,
            outbox_id=finalized.outbox_id,
            integration_state="integrated",
        )

    def _followup_base(
        self,
        state: _DeliveryState,
        *,
        report: PulseReport,
        expected_revision: str,
        interface_change: bool,
    ) -> _Integration | None:
        if report.delivery is None:
            return None
        if not interface_change:
            raise ValidationError(
                "a delivered Pulse report can only add an interface curation follow-up"
            )
        if report.revision != expected_revision:
            raise ConflictError(
                "curation follow-up requires the current delivered pulse report revision"
            )
        base = next(
            (
                item
                for item in state.integrations
                if item.report_id == report.identifier
                and not item.curation_followup
                and item.state == "integrated"
                and item.delivered_report_ref == report.report_ref
            ),
            None,
        )
        if base is None:
            raise ConflictError(
                "curation follow-up requires the preceding integrated Pulse receipt"
            )
        return base

    def pending_curation(self, *, limit: int = 100) -> CurationList:
        """Return nonterminal UI work for daemon readback, without performing delivery."""

        _limit(limit, "curation limit")
        pending = tuple(
            item
            for item in self._snapshot().outbox
            if item.state in {"prepared", "queued", "blocked", "uncertain"}
        )
        return CurationList(outbox=pending[:limit], remaining=len(pending) - len(pending[:limit]))

    def record_curation(
        self,
        outbox_id: str,
        *,
        expected_revision: str,
        state: CurationState,
        receipt: str | None,
        observed_at: datetime | None = None,
    ) -> DianeOutbox:
        """CAS-record a daemon result without changing the Pulse integration."""

        identifier = _uuid(outbox_id, "Diane outbox ID")
        expected = _revision(expected_revision, "expected Diane outbox revision")
        desired_state = _recorded_curation_state(state)
        clean_receipt = _optional_text(receipt, "Diane curation receipt", MAX_RECEIPT)
        timestamp = format_time(observed_at or self._now())
        with self._transaction() as store:
            current, before = self._read_state(store)
            outbox = _find_outbox(current, identifier)
            if outbox is None:
                raise NotFoundError(f"Diane outbox does not exist: {identifier}")
            if outbox.revision != expected:
                raise ConflictError("Diane outbox changed; reload before recording curation")
            if outbox.state in {"completed", "noop"} and desired_state != outbox.state:
                raise ConflictError("Diane outbox already has a terminal curation result")
            if outbox.state == desired_state and outbox.receipt == clean_receipt:
                return outbox
            replacement = replace(
                outbox,
                state=desired_state,
                receipt=clean_receipt,
                updated_at=timestamp,
                revision="",
            )
            after = replace(
                current,
                outbox=tuple(
                    replacement if item.identifier == identifier else item
                    for item in current.outbox
                ),
            )
            self._write_state(store, before=before, state=after)
            return _with_outbox_revision(replacement)

    def status(self) -> PulseDeliveryStatus:
        """Report delivery mechanics without claiming that queued Pulse work ran."""

        state = self._snapshot()
        try:
            binding = self._binding()
            binding_error = None
        except (ConflictError, NotFoundError, ValidationError) as exc:
            binding = None
            binding_error = str(exc)
        now = self._now()
        return PulseDeliveryStatus(
            binding=binding,
            binding_error=binding_error,
            wake_requests=state.wake_requests,
            fallback_request_ids=tuple(
                item.identifier
                for item in state.wake_requests
                if item.state != "queued" and _parsed_time(item.prepared_at) + FALLBACK_AFTER <= now
            ),
            pending_relevance=_pending_count(self.reports, "relevance"),
            pending_delivery=_pending_count(self.reports, "delivery"),
            pending_investigation=_pending_count(self.reports, "investigation"),
            pending_curation=sum(
                item.state in {"prepared", "queued", "blocked", "uncertain"}
                for item in state.outbox
            ),
        )

    def _binding(self, *, required_thread_id: str | None = None) -> NativePulseBinding:
        task = self.vault.get_task(RESIDENT_PULSE_TASK_ID)
        if task.status in TERMINAL_TASK_STATUSES:
            raise ValidationError("resident Pulse task is terminal")
        if task.refs.count(RESIDENT_PULSE_REF) != 1:
            raise ValidationError("resident Pulse task must have exactly one system-role reference")
        chief = tuple(ref for ref in task.refs if _CHIEF_REF.fullmatch(ref) is not None)
        if len(chief) != 1:
            raise ValidationError(
                "resident Pulse task must have exactly one Chief of Staff reference"
            )
        pulse_thread = _uuid(task.active_thread_id, "resident Pulse active thread ID")
        chief_match = _CHIEF_REF.fullmatch(chief[0])
        assert chief_match is not None
        chief_thread = _uuid(chief_match.group(1), "Chief of Staff thread ID")
        if chief_thread == pulse_thread:
            raise ValidationError("resident Pulse and Chief of Staff threads must differ")
        if (
            required_thread_id is not None
            and _uuid(required_thread_id, "Pulse thread ID") != pulse_thread
        ):
            raise ConflictError("Pulse thread does not match the active resident Pulse binding")
        return NativePulseBinding(
            task_id=task.identifier,
            task_revision=task.revision,
            pulse_thread_id=pulse_thread,
            chief_thread_id=chief_thread,
        )

    def _reserve_wake(
        self,
        *,
        report_ids: tuple[str, ...],
        report_refs: tuple[str, ...],
        pulse_thread_id: str,
        pending_report_ids: frozenset[str],
    ) -> _WakeReservation:
        now = format_time(self._now())
        with self._transaction() as store:
            state, before = self._read_state(store)
            covered_ids = {
                identifier
                for item in state.wake_requests
                if item.pulse_thread_id == pulse_thread_id
                for identifier in item.report_ids
            }
            selected = tuple(
                (identifier, ref)
                for identifier, ref in zip(report_ids, report_refs, strict=True)
                if identifier not in covered_ids
            )
            if not selected:
                existing = next(
                    (
                        item
                        for item in state.wake_requests
                        if item.pulse_thread_id == pulse_thread_id
                        and set(report_ids).issubset(item.report_ids)
                    ),
                    None,
                )
                return _WakeReservation(existing, False, (), ())
            outstanding = sum(
                item.pulse_thread_id == pulse_thread_id
                and item.state == "queued"
                and any(identifier in pending_report_ids for identifier in item.report_ids)
                for item in state.wake_requests
            )
            if outstanding >= MAX_OUTSTANDING_WAKES:
                # Keep one follow-up available while Pulse works. Its bounded
                # pending-report read includes new arrivals; a separate turn
                # for every arrival would only lengthen the same serial queue.
                # These IDs remain unreserved and are reconsidered next tick.
                return _WakeReservation(None, False, (), ())
            if len(state.wake_requests) >= MAX_WAKE_REQUESTS:
                raise ValidationError("Pulse wake receipt store reached its bounded limit")
            selected_ids = tuple(identifier for identifier, _ in selected)
            selected_refs = tuple(ref for _, ref in selected)
            event_key = "pulse-wake:" + sha256_bytes(
                (pulse_thread_id + "\0" + "\0".join(selected_refs)).encode("utf-8")
            )
            request = WakeRequest(
                identifier=str(uuid.uuid4()),
                event_key=event_key,
                report_ids=selected_ids,
                report_refs=selected_refs,
                pulse_thread_id=pulse_thread_id,
                state="prepared",
                prepared_at=now,
                updated_at=now,
                revision="",
            )
            after = replace(state, wake_requests=(*state.wake_requests, request))
            self._write_state(store, before=before, state=after)
            return _WakeReservation(_with_wake_revision(request), True, selected_ids, selected_refs)

    def _set_wake_state(self, request_id: str, desired: WakeState) -> WakeRequest:
        desired = _wake_state(desired)
        with self._transaction() as store:
            state, before = self._read_state(store)
            current = _find_wake(state, request_id)
            if current is None:
                raise NotFoundError(f"Pulse wake request does not exist: {request_id}")
            if current.state == desired:
                return current
            if current.state == "queued":
                return current
            replacement = replace(
                current,
                state=desired,
                updated_at=format_time(self._now()),
                revision="",
            )
            after = replace(
                state,
                wake_requests=tuple(
                    replacement if item.identifier == request_id else item
                    for item in state.wake_requests
                ),
            )
            self._write_state(store, before=before, state=after)
            return _with_wake_revision(replacement)

    def _prepare_integration(
        self,
        *,
        report: PulseReport,
        expected_revision: str,
        binding: NativePulseBinding,
        result_refs: tuple[str, ...],
        summary: str,
        interface_change: bool,
        target_desktop_work_id: str | None,
        followup_base: _Integration | None,
    ) -> tuple[_Integration, bool]:
        curation_followup = followup_base is not None
        key = sha256_bytes(
            (
                report.identifier
                + "\0"
                + expected_revision
                + "\0"
                + binding.pulse_thread_id
                + "\0"
                + "\0".join(result_refs)
                + "\0"
                + summary
                + "\0"
                + str(interface_change)
                + "\0"
                + (target_desktop_work_id or "")
                + "\0"
                + str(curation_followup)
                + "\0"
                + (followup_base.identifier if followup_base is not None else "")
            ).encode("utf-8")
        )
        accepted_at = format_time(self._now())
        with self._transaction() as store:
            state, before = self._read_state(store)
            prior = _find_integration(
                state, report_id=report.identifier, expected_revision=expected_revision
            )
            if prior is not None:
                _match_integration(
                    prior,
                    pulse_thread_id=binding.pulse_thread_id,
                    result_refs=result_refs,
                    summary=summary,
                    interface_change=interface_change,
                    target_desktop_work_id=target_desktop_work_id,
                )
                return prior, False
            if len(state.integrations) >= MAX_INTEGRATIONS:
                raise ValidationError("Pulse integration receipt store reached its bounded limit")
            outbox_id = (
                str(uuid.uuid5(uuid.NAMESPACE_URL, "seld-diane:" + key))
                if interface_change
                else None
            )
            integration = _Integration(
                identifier=key,
                report_id=report.identifier,
                expected_revision=expected_revision,
                pulse_thread_id=binding.pulse_thread_id,
                result_refs=result_refs,
                primary_result_ref=(
                    followup_base.primary_result_ref
                    if followup_base is not None
                    else result_refs[0]
                ),
                target_desktop_work_id=target_desktop_work_id,
                curation_followup=curation_followup,
                base_integration_id=(
                    followup_base.identifier if followup_base is not None else None
                ),
                summary=summary,
                interface_change=interface_change,
                accepted_at=accepted_at,
                state="prepared",
                delivered_report_ref=None,
                outbox_id=outbox_id,
            )
            outbox = state.outbox
            if interface_change:
                assert outbox_id is not None
                if len(outbox) >= MAX_CURATION_OUTBOX:
                    raise ValidationError("Diane outbox reached its bounded limit")
                outbox = (*outbox, _new_outbox(outbox_id, integration, report))
            after = replace(state, integrations=(*state.integrations, integration), outbox=outbox)
            self._write_state(store, before=before, state=after)
            return integration, True

    def _finalize_integration(self, integration_id: str, report: PulseReport) -> _Integration:
        with self._transaction() as store:
            state, before = self._read_state(store)
            integration = next(
                (item for item in state.integrations if item.identifier == integration_id), None
            )
            if integration is None:
                raise NotFoundError("Pulse integration receipt disappeared")
            _require_recoverable_delivery(report, integration)
            if integration.state == "integrated":
                return integration
            completed = replace(
                integration,
                state="integrated",
                delivered_report_ref=report.report_ref,
            )
            outbox = state.outbox
            if completed.outbox_id is not None:
                current = _find_outbox(state, completed.outbox_id)
                if current is None:
                    raise PersistenceError("Pulse integration lost its Diane outbox receipt")
                if current.state == "prepared":
                    queued = replace(
                        current,
                        state="queued",
                        updated_at=format_time(self._now()),
                        revision="",
                    )
                    outbox = tuple(
                        queued if item.identifier == queued.identifier else item
                        for item in state.outbox
                    )
            after = replace(
                state,
                integrations=tuple(
                    completed if item.identifier == integration_id else item
                    for item in state.integrations
                ),
                outbox=outbox,
            )
            self._write_state(store, before=before, state=after)
            return completed

    def _resolve_results(
        self,
        *,
        report: PulseReport,
        result_refs: tuple[str, ...],
        interface_change: bool,
        target_desktop_work_id: str | None,
    ) -> tuple[str, ...]:
        canonical = tuple(self.vault.resolve_canonical_result_ref(value) for value in result_refs)
        if len(set(canonical)) != len(canonical):
            raise ValidationError("Pulse integration result references must be unique")
        own = report.report_ref
        if own in canonical and (interface_change or canonical != (own,)):
            raise ValidationError(
                "a source report may be its own result only for explicit no-change integration"
            )
        if target_desktop_work_id is not None:
            task_refs = tuple(value for value in canonical if value.startswith("task:"))
            if not any(
                self.vault.get_task(_task_identifier(value)).active_thread_id
                == target_desktop_work_id
                for value in task_refs
            ):
                raise ValidationError(
                    "Pulse target desktop work must match a supplied canonical task's active thread"
                )
        return canonical

    @contextmanager
    def _transaction(self) -> Iterator[PinnedPathRoot | None]:
        store: PinnedPathRoot | None = None
        try:
            if not PINNED_PATH_ROOT_SUPPORTED:
                self.root.mkdir(parents=True, exist_ok=True)
                with exclusive_lock(self.lock_path):
                    yield None
                return
            store = PinnedPathRoot(self.vault_root)
            with (
                store.watch_directory(".gsv", create=True),
                store.watch_directory(".gsv/locks", create=True),
                store.bind_directory(".gsv/pulse-delivery", create=True),
                store.exclusive_file_lock(".gsv/locks/pulse-delivery.lock"),
            ):
                yield store
        finally:
            if store is not None:
                store.close()

    def _snapshot(self) -> _DeliveryState:
        with self._transaction() as store:
            state, _ = self._read_state(store)
            return state

    def _read_state(self, store: PinnedPathRoot | None) -> tuple[_DeliveryState, bytes | None]:
        if store is not None:
            encoded = store.read_regular_file(
                ".gsv/pulse-delivery/state.json",
                label="Pulse delivery state",
                max_bytes=MAX_DELIVERY_STATE_BYTES,
                missing_ok=True,
                retain=True,
            )
        elif os.path.lexists(self.state_path):
            encoded = read_regular_file(
                self.state_path,
                label="Pulse delivery state",
                max_bytes=MAX_DELIVERY_STATE_BYTES,
            )
        else:
            encoded = None
        return _parse_state(encoded), encoded

    def _write_state(
        self,
        store: PinnedPathRoot | None,
        *,
        before: bytes | None,
        state: _DeliveryState,
    ) -> None:
        encoded = _render_state(state)
        if store is None:
            current = (
                read_regular_file(
                    self.state_path,
                    label="Pulse delivery state",
                    max_bytes=MAX_DELIVERY_STATE_BYTES,
                )
                if os.path.lexists(self.state_path)
                else None
            )
            if current != before:
                raise ConflictError("Pulse delivery state changed before publication")
            atomic_write(self.state_path, encoded)
            return
        try:
            store.exchange_regular_file_if_exact(
                ".gsv/pulse-delivery/state.json",
                expected=before,
                replacement=encoded,
                label="Pulse delivery state",
                max_bytes=MAX_DELIVERY_STATE_BYTES,
            )
        except DurablePublishError as exc:
            if exc.outcome is PublishOutcome.UNPUBLISHED:
                raise PersistenceError("Pulse delivery state was not published") from exc
            if exc.outcome is PublishOutcome.COMMITTED:
                raise MutationCommittedError(
                    "Pulse delivery state committed, but cleanup is unconfirmed"
                ) from exc
            raise DegradedIntegrityError(
                "Pulse delivery state has an unknown publication outcome; run gsv doctor"
            ) from exc


def pulse_delivery_dict(
    value: PulseDeliveryStatus
    | WakeQueueResult
    | PulseIntegrationResult
    | CurationList
    | DianeOutbox,
) -> dict[str, Any]:
    return asdict(value)


def _run_codex_queue(command: Sequence[str]) -> int:
    completed = subprocess.run(
        tuple(command),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    return int(completed.returncode)


def _wake_message(report_ids: tuple[str, ...]) -> str:
    return (
        "Seld event-driven Pulse: derived source reports await your judgment. Preserve your "
        "exact resident-pulse binding and normal authority and notification gates. First "
        "read installed gsv pulse status. When event_runtime.live is true, its "
        "monitored_sources belong to the watcher: do not acquire or acknowledge those "
        "sources yourself, including WhatsApp. Read gsv pulse reports --stage delivery. "
        "This is a bounded event wake: prioritize the listed reports, then batch other "
        "pending delivery reports already available, up to 20 reports total. Skip any "
        "listed report already delivered. Read only the canonical records needed to "
        "judge that batch. Leave unrelated acquisition and whole-portfolio "
        "maintenance to the existing 30-minute safeguard. "
        "Judge these IDs against current canon, apply justified changes, and read results "
        "back. Use gsv pulse integrate --help for the exact arguments; integrate each "
        "fresh report revision with your existing Pulse task UUID and actual canonical "
        "result refs. Supported result kinds are task, thread, entity, direction:current, "
        "portfolio:current, and source-report, each pinned to its fresh revision. "
        "document:NOW.md is not a supported result ref. Treat dated backfill claims as "
        "historical unless current evidence establishes an open consequence. "
        "Set --interface-change for useful accepted interface changes, "
        "which the native bridge routes to the existing Diane. When the affected task "
        "has an active_thread_id for an existing desktop work item, include that exact "
        "UUID as --target-desktop-work-id and the task's fresh result ref. "
        "For a justified no-change "
        "disposition use the source-report ref and omit --interface-change. Do not "
        "rewrite NOW or tasks merely to record a review, an unverified historical "
        "claim, or a changed timestamp with no supported current consequence. Queue "
        "acceptance is not semantic completion. Do not independently message Diane "
        "again for that integration.\n" + "\n".join(report_ids)
    )


def _wake_result(request: WakeRequest, *, now: datetime) -> WakeQueueResult:
    fallback_due = (
        request.state != "queued" and _parsed_time(request.prepared_at) + FALLBACK_AFTER <= now
    )
    return WakeQueueResult(
        request_id=request.identifier,
        state=request.state,
        report_refs=request.report_refs,
        pulse_thread_id=request.pulse_thread_id,
        fallback_at=request.fallback_at if fallback_due else None,
    )


def _noop_wake_result(report_refs: tuple[str, ...], pulse_thread_id: str) -> WakeQueueResult:
    return WakeQueueResult(
        request_id=None,
        state="noop",
        report_refs=report_refs,
        pulse_thread_id=pulse_thread_id,
        fallback_at=None,
    )


def _new_outbox(identifier: str, integration: _Integration, report: PulseReport) -> DianeOutbox:
    assert integration.outbox_id == identifier
    outbox = DianeOutbox(
        identifier=identifier,
        event_key="diane-curation:"
        + sha256_bytes((integration.identifier + "\0" + report.report_ref).encode("utf-8")),
        source_gsv_revision=report.coverage_revision,
        result_refs=integration.result_refs,
        target_desktop_work_id=integration.target_desktop_work_id,
        curation_followup=integration.curation_followup,
        summary=integration.summary,
        observed_at=report.observed_at,
        accepted_at=integration.accepted_at,
        state="prepared",
        receipt=None,
        updated_at=integration.accepted_at,
        revision="",
    )
    return _with_outbox_revision(outbox)


def _render_state(state: _DeliveryState) -> bytes:
    _validate_state(state)
    payload = {
        "integrations": [_integration_dict(item) for item in state.integrations],
        "outbox": [_outbox_dict(item) for item in state.outbox],
        "version": DELIVERY_FORMAT_VERSION,
        "wakeRequests": [_wake_dict(item) for item in state.wake_requests],
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_DELIVERY_STATE_BYTES:
        raise ValidationError("Pulse delivery state exceeds its size bound")
    return encoded


def _parse_state(encoded: bytes | None) -> _DeliveryState:
    if encoded is None:
        return _DeliveryState()
    if len(encoded) > MAX_DELIVERY_STATE_BYTES:
        raise ValidationError("Pulse delivery state exceeds its size bound")
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Pulse delivery state is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "integrations",
        "outbox",
        "version",
        "wakeRequests",
    }:
        raise ValidationError("Pulse delivery state has an unsupported shape")
    if payload.get("version") != DELIVERY_FORMAT_VERSION:
        raise ValidationError("Pulse delivery state has an unsupported version")
    state = _DeliveryState(
        wake_requests=tuple(
            _parse_wake(item) for item in _objects(payload.get("wakeRequests"), "wake requests")
        ),
        integrations=tuple(
            _parse_integration(item)
            for item in _objects(payload.get("integrations"), "integrations")
        ),
        outbox=tuple(_parse_outbox(item) for item in _objects(payload.get("outbox"), "outbox")),
    )
    _validate_state(state)
    if _render_state(state) != encoded:
        raise ValidationError("Pulse delivery state is not in canonical form")
    return state


def _validate_state(state: _DeliveryState) -> None:
    if len(state.wake_requests) > MAX_WAKE_REQUESTS:
        raise ValidationError("Pulse wake receipt store reached its bounded limit")
    if len(state.integrations) > MAX_INTEGRATIONS or len(state.outbox) > MAX_CURATION_OUTBOX:
        raise ValidationError("Pulse delivery state reached its bounded limit")
    if len({item.identifier for item in state.wake_requests}) != len(state.wake_requests):
        raise ValidationError("Pulse delivery has duplicate wake request IDs")
    if len({item.event_key for item in state.wake_requests}) != len(state.wake_requests):
        raise ValidationError("Pulse delivery has duplicate wake event keys")
    if len({item.identifier for item in state.integrations}) != len(state.integrations):
        raise ValidationError("Pulse delivery has duplicate integration IDs")
    if len({(item.report_id, item.expected_revision) for item in state.integrations}) != len(
        state.integrations
    ):
        raise ValidationError("Pulse delivery has duplicate report integration receipts")
    if len({item.identifier for item in state.outbox}) != len(state.outbox):
        raise ValidationError("Pulse delivery has duplicate Diane outbox IDs")
    integration_outbox = {
        item.outbox_id for item in state.integrations if item.outbox_id is not None
    }
    if integration_outbox != {item.identifier for item in state.outbox}:
        raise ValidationError("Pulse delivery outbox does not match its integration receipts")
    integrations = {item.identifier: item for item in state.integrations}
    outbox = {item.identifier: item for item in state.outbox}
    for item in state.integrations:
        if item.curation_followup:
            assert item.base_integration_id is not None
            base = integrations.get(item.base_integration_id)
            if base is None or base.curation_followup or base.state != "integrated":
                raise ValidationError("Pulse curation follow-up has no integrated base receipt")
            if base.report_id != item.report_id or base.delivered_report_ref is None:
                raise ValidationError(
                    "Pulse curation follow-up base receipt does not match its report"
                )
            expected_ref = f"source-report:{item.report_id}@{item.expected_revision}"
            if base.delivered_report_ref != expected_ref:
                raise ValidationError(
                    "Pulse curation follow-up must use its delivered report revision"
                )
            _validate_followup_results(item.result_refs, base)
            if item.primary_result_ref != base.primary_result_ref:
                raise ValidationError("Pulse curation follow-up must preserve its original result")
        if item.outbox_id is not None:
            linked = outbox[item.outbox_id]
            if (
                linked.result_refs != item.result_refs
                or linked.target_desktop_work_id != item.target_desktop_work_id
                or linked.curation_followup != item.curation_followup
            ):
                raise ValidationError("Diane outbox does not match its integration content")


def _wake_dict(value: WakeRequest) -> dict[str, object]:
    _validate_wake(value)
    return {
        "eventKey": value.event_key,
        "id": value.identifier,
        "preparedAt": value.prepared_at,
        "pulseThreadId": value.pulse_thread_id,
        "reportIds": list(value.report_ids),
        "reportRefs": list(value.report_refs),
        "state": value.state,
        "updatedAt": value.updated_at,
    }


def _parse_wake(value: dict[str, object]) -> WakeRequest:
    if set(value) != {
        "eventKey",
        "id",
        "preparedAt",
        "pulseThreadId",
        "reportIds",
        "reportRefs",
        "state",
        "updatedAt",
    }:
        raise ValidationError("Pulse wake receipt has an unsupported shape")
    wake = WakeRequest(
        identifier=_uuid(value.get("id"), "Pulse wake request ID"),
        event_key=_event_key(value.get("eventKey"), "pulse-wake"),
        report_ids=_report_ids(_strings(value.get("reportIds"), "Pulse wake report IDs")),
        report_refs=_report_refs(_strings(value.get("reportRefs"), "Pulse wake report references")),
        pulse_thread_id=_uuid(value.get("pulseThreadId"), "Pulse wake thread ID"),
        state=_wake_state(value.get("state")),
        prepared_at=_time(value.get("preparedAt"), "Pulse wake prepared time"),
        updated_at=_time(value.get("updatedAt"), "Pulse wake update time"),
        revision="",
    )
    return _with_wake_revision(wake)


def _integration_dict(value: _Integration) -> dict[str, object]:
    _validate_integration(value)
    result: dict[str, object] = {
        "acceptedAt": value.accepted_at,
        "deliveredReportRef": value.delivered_report_ref,
        "expectedRevision": value.expected_revision,
        "id": value.identifier,
        "interfaceChange": value.interface_change,
        "outboxId": value.outbox_id,
        "primaryResultRef": value.primary_result_ref,
        "pulseThreadId": value.pulse_thread_id,
        "reportId": value.report_id,
        "resultRefs": list(value.result_refs),
        "state": value.state,
        "summary": value.summary,
    }
    if value.target_desktop_work_id is not None:
        result["targetDesktopWorkId"] = value.target_desktop_work_id
    if value.curation_followup:
        result["curationFollowup"] = True
        assert value.base_integration_id is not None
        result["baseIntegrationId"] = value.base_integration_id
    return result


def _parse_integration(value: dict[str, object]) -> _Integration:
    expected = {
        "acceptedAt",
        "deliveredReportRef",
        "expectedRevision",
        "id",
        "interfaceChange",
        "outboxId",
        "primaryResultRef",
        "pulseThreadId",
        "reportId",
        "resultRefs",
        "state",
        "summary",
    }
    optional = {"targetDesktopWorkId", "curationFollowup", "baseIntegrationId"}
    if not expected.issubset(value) or set(value) - expected - optional:
        raise ValidationError("Pulse integration receipt has an unsupported shape")
    refs = _native_refs(
        _strings(value.get("resultRefs"), "Pulse integration result references"),
        "Pulse integration result reference",
    )
    result = _Integration(
        identifier=_revision(value.get("id"), "Pulse integration ID"),
        report_id=_uuid(value.get("reportId"), "Pulse integration report ID"),
        expected_revision=_revision(
            value.get("expectedRevision"), "Pulse integration expected revision"
        ),
        pulse_thread_id=_uuid(value.get("pulseThreadId"), "Pulse integration thread ID"),
        result_refs=refs,
        primary_result_ref=_native_ref(
            value.get("primaryResultRef"), "Pulse integration primary result reference"
        ),
        target_desktop_work_id=_optional_uuid(
            value.get("targetDesktopWorkId"), "Pulse integration target desktop work ID"
        ),
        curation_followup=_optional_true(
            value.get("curationFollowup"), "Pulse integration curation follow-up"
        ),
        base_integration_id=_optional_revision(
            value.get("baseIntegrationId"), "Pulse integration base ID"
        ),
        summary=_summary(value.get("summary")),
        interface_change=_bool(value.get("interfaceChange"), "Pulse integration interface change"),
        accepted_at=_time(value.get("acceptedAt"), "Pulse integration accepted time"),
        state=_integration_state(value.get("state")),
        delivered_report_ref=_optional_native_ref(
            value.get("deliveredReportRef"), "Pulse delivered report reference"
        ),
        outbox_id=_optional_uuid(value.get("outboxId"), "Diane outbox ID"),
    )
    _validate_integration(result)
    return result


def _outbox_dict(value: DianeOutbox) -> dict[str, object]:
    _validate_outbox(value)
    result: dict[str, object] = {
        "acceptedAt": value.accepted_at,
        "eventKey": value.event_key,
        "id": value.identifier,
        "observedAt": value.observed_at,
        "receipt": value.receipt,
        "resultRefs": list(value.result_refs),
        "sourceGSVRevision": value.source_gsv_revision,
        "state": value.state,
        "summary": value.summary,
        "updatedAt": value.updated_at,
    }
    if value.target_desktop_work_id is not None:
        result["targetDesktopWorkId"] = value.target_desktop_work_id
    if value.curation_followup:
        result["curationFollowup"] = True
    return result


def _parse_outbox(value: dict[str, object]) -> DianeOutbox:
    expected = {
        "acceptedAt",
        "eventKey",
        "id",
        "observedAt",
        "receipt",
        "resultRefs",
        "sourceGSVRevision",
        "state",
        "summary",
        "updatedAt",
    }
    optional = {"targetDesktopWorkId", "curationFollowup"}
    if not expected.issubset(value) or set(value) - expected - optional:
        raise ValidationError("Diane outbox has an unsupported shape")
    outbox = DianeOutbox(
        identifier=_uuid(value.get("id"), "Diane outbox ID"),
        event_key=_event_key(value.get("eventKey"), "diane-curation"),
        source_gsv_revision=_revision(value.get("sourceGSVRevision"), "Diane source GSV revision"),
        result_refs=_native_refs(
            _strings(value.get("resultRefs"), "Diane result references"), "Diane result reference"
        ),
        target_desktop_work_id=_optional_uuid(
            value.get("targetDesktopWorkId"), "Diane target desktop work ID"
        ),
        curation_followup=_optional_true(value.get("curationFollowup"), "Diane curation follow-up"),
        summary=_summary(value.get("summary")),
        observed_at=_time(value.get("observedAt"), "Diane observed time"),
        accepted_at=_time(value.get("acceptedAt"), "Diane accepted time"),
        state=_curation_state(value.get("state")),
        receipt=_optional_text(value.get("receipt"), "Diane curation receipt", MAX_RECEIPT),
        updated_at=_time(value.get("updatedAt"), "Diane update time"),
        revision="",
    )
    return _with_outbox_revision(outbox)


def _validate_wake(value: WakeRequest) -> None:
    _uuid(value.identifier, "Pulse wake request ID")
    _event_key(value.event_key, "pulse-wake")
    _report_ids(value.report_ids)
    if len(value.report_refs) != len(value.report_ids):
        raise ValidationError("Pulse wake receipt report IDs and references differ")
    refs = _report_refs(value.report_refs)
    if (
        tuple(
            match.group(1)
            for match in (_REPORT_REF.fullmatch(item) for item in refs)
            if match is not None
        )
        != value.report_ids
    ):
        raise ValidationError("Pulse wake receipt report references do not match report IDs")
    _uuid(value.pulse_thread_id, "Pulse wake thread ID")
    _wake_state(value.state)
    _time(value.prepared_at, "Pulse wake prepared time")
    _time(value.updated_at, "Pulse wake update time")


def _validate_integration(value: _Integration) -> None:
    _revision(value.identifier, "Pulse integration ID")
    _uuid(value.report_id, "Pulse integration report ID")
    _revision(value.expected_revision, "Pulse integration expected revision")
    _uuid(value.pulse_thread_id, "Pulse integration thread ID")
    refs = _native_refs(value.result_refs, "Pulse integration result reference")
    _optional_uuid(value.target_desktop_work_id, "Pulse integration target desktop work ID")
    if value.target_desktop_work_id is not None and not value.interface_change:
        raise ValidationError("Pulse target desktop work requires an interface change")
    if value.curation_followup:
        if not value.interface_change or value.base_integration_id is None:
            raise ValidationError("Pulse curation follow-up is invalid")
        _revision(value.base_integration_id, "Pulse integration base ID")
    elif value.base_integration_id is not None:
        raise ValidationError("ordinary Pulse integration cannot have a follow-up base")
    if not refs or (not value.curation_followup and value.primary_result_ref != refs[0]):
        raise ValidationError("Pulse integration primary result reference is invalid")
    _native_ref(value.primary_result_ref, "Pulse integration primary result reference")
    _summary(value.summary)
    _time(value.accepted_at, "Pulse integration accepted time")
    if value.state not in {"prepared", "integrated"}:
        raise ValidationError("Pulse integration state is invalid")
    if value.interface_change != (value.outbox_id is not None):
        raise ValidationError("Pulse integration outbox binding is invalid")
    if value.outbox_id is not None:
        _uuid(value.outbox_id, "Diane outbox ID")
    if value.state == "integrated":
        if value.delivered_report_ref is None:
            raise ValidationError("integrated Pulse receipt requires a delivered report reference")
        _report_refs((value.delivered_report_ref,))
    elif value.delivered_report_ref is not None:
        raise ValidationError("prepared Pulse receipt cannot claim a delivered report")


def _validate_outbox(value: DianeOutbox) -> None:
    _uuid(value.identifier, "Diane outbox ID")
    _event_key(value.event_key, "diane-curation")
    _revision(value.source_gsv_revision, "Diane source GSV revision")
    _native_refs(value.result_refs, "Diane result reference")
    _optional_uuid(value.target_desktop_work_id, "Diane target desktop work ID")
    if not isinstance(value.curation_followup, bool):
        raise ValidationError("Diane curation follow-up must be boolean")
    _summary(value.summary)
    _time(value.observed_at, "Diane observed time")
    _time(value.accepted_at, "Diane accepted time")
    _curation_state(value.state)
    _optional_text(value.receipt, "Diane curation receipt", MAX_RECEIPT)
    _time(value.updated_at, "Diane update time")


def _with_wake_revision(value: WakeRequest) -> WakeRequest:
    return replace(value, revision=sha256_bytes(_canonical(_wake_dict(value))))


def _with_outbox_revision(value: DianeOutbox) -> DianeOutbox:
    return replace(value, revision=sha256_bytes(_canonical(_outbox_dict(value))))


def _find_integration(
    state: _DeliveryState, *, report_id: str, expected_revision: str
) -> _Integration | None:
    return next(
        (
            item
            for item in state.integrations
            if item.report_id == report_id and item.expected_revision == expected_revision
        ),
        None,
    )


def _find_wake(state: _DeliveryState, identifier: str) -> WakeRequest | None:
    return next((item for item in state.wake_requests if item.identifier == identifier), None)


def _find_outbox(state: _DeliveryState, identifier: str) -> DianeOutbox | None:
    return next((item for item in state.outbox if item.identifier == identifier), None)


def _validate_followup_results(result_refs: tuple[str, ...], base: _Integration) -> None:
    base_identifiers = {_result_identifier(value) for value in base.result_refs}
    if not {_result_identifier(value) for value in result_refs}.issubset(base_identifiers):
        raise ValidationError(
            "Pulse curation follow-up results must refer only to the original integrated records"
        )


def _result_identifier(value: str) -> str:
    return value.rsplit("@", 1)[0]


def _task_identifier(value: str) -> str:
    identifier = value.removeprefix("task:").rsplit("@", 1)[0]
    if not identifier or not value.startswith("task:"):
        raise ValidationError("Pulse task result reference is invalid")
    return identifier


def _match_integration(
    value: _Integration,
    *,
    pulse_thread_id: str,
    result_refs: tuple[str, ...],
    summary: str,
    interface_change: bool,
    target_desktop_work_id: str | None,
) -> None:
    if (
        value.pulse_thread_id != pulse_thread_id
        or value.result_refs != result_refs
        or value.summary != summary
        or value.interface_change != interface_change
        or value.target_desktop_work_id != target_desktop_work_id
    ):
        raise ConflictError("Pulse integration receipt already has different content")


def _require_recoverable_delivery(report: PulseReport, integration: _Integration) -> None:
    if report.delivery is None or report.delivery.result_ref != integration.primary_result_ref:
        raise ConflictError("pulse report has a different delivery result")


def _require_integrated_report(report: PulseReport, integration: _Integration) -> None:
    _require_recoverable_delivery(report, integration)
    if integration.delivered_report_ref != report.report_ref:
        raise ConflictError("Pulse integration receipt has a stale delivered report reference")


def _objects(value: object, label: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValidationError(f"Pulse delivery {label} must be an array of objects")
    return tuple(value)


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"{label} must be a list of strings")
    return tuple(value)


def _report_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values or len(values) > MAX_WAKE_REPORTS:
        raise ValidationError("Pulse wake requires one to twenty report IDs")
    clean = tuple(_uuid(value, "pulse report ID") for value in values)
    if len(set(clean)) != len(clean):
        raise ValidationError("Pulse wake report IDs must be unique")
    return tuple(sorted(clean))


def _report_refs(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values or len(values) > MAX_WAKE_REPORTS:
        raise ValidationError("Pulse report references are invalid")
    clean = tuple(_native_ref(value, "Pulse report reference") for value in values)
    if any(_REPORT_REF.fullmatch(value) is None for value in clean) or len(set(clean)) != len(
        clean
    ):
        raise ValidationError("Pulse report references are invalid")
    return clean


def _native_refs(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values or len(values) > MAX_RESULT_REFS:
        raise ValidationError(f"{label}s must contain one to twenty references")
    clean = tuple(_native_ref(value, label) for value in values)
    if len(set(clean)) != len(clean):
        raise ValidationError(f"{label}s must be unique")
    return clean


def _native_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or _NATIVE_REF.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a canonical revision reference")
    return value


def _event_key(value: object, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or value != f"{prefix}:{value.removeprefix(prefix + ':')}"
        or not value.startswith(prefix + ":")
    ):
        raise ValidationError("Pulse delivery event key is invalid")
    if _SHA256.fullmatch(value.removeprefix(prefix + ":")) is None:
        raise ValidationError("Pulse delivery event key must contain a SHA-256 digest")
    return value


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a canonical UUID")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValidationError(f"{label} must be a canonical UUID") from exc
    if canonical != value:
        raise ValidationError(f"{label} must be a canonical UUID")
    return value


def _optional_uuid(value: object, label: str) -> str | None:
    return None if value is None else _uuid(value, label)


def _optional_revision(value: object, label: str) -> str | None:
    return None if value is None else _revision(value, label)


def _optional_true(value: object, label: str) -> bool:
    if value is None:
        return False
    if value is not True:
        raise ValidationError(f"{label} must be true when present")
    return True


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _time(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    return stored_time(value, label)


def _parsed_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValidationError(f"{label} must be bounded derived text")
    clean = " ".join(value.split())
    if not clean or len(clean) > maximum:
        raise ValidationError(f"{label} must be one non-empty bounded line")
    return clean


def _summary(value: object) -> str:
    clean = _text(value, "Pulse integration summary", MAX_SUMMARY_BYTES)
    if len(clean.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise ValidationError("Pulse integration summary must be at most 1200 UTF-8 bytes")
    return clean


def _optional_text(value: object, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, label, maximum)


def _optional_native_ref(value: object, label: str) -> str | None:
    return None if value is None else _native_ref(value, label)


def _wake_state(value: object) -> WakeState:
    if value not in _WAKE_STATES:
        raise ValidationError("Pulse wake state is invalid")
    return str(value)  # type: ignore[return-value]


def _curation_state(value: object) -> CurationState:
    if value not in _CURATION_STATES:
        raise ValidationError("Diane curation state is invalid")
    return str(value)  # type: ignore[return-value]


def _recorded_curation_state(value: object) -> CurationState:
    state = _curation_state(value)
    if state == "prepared":
        raise ValidationError("Diane curation result cannot return an outbox to prepared")
    return state


def _integration_state(value: object) -> Literal["prepared", "integrated"]:
    if value not in {"prepared", "integrated"}:
        raise ValidationError("Pulse integration state is invalid")
    return str(value)  # type: ignore[return-value]


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} must be boolean")
    return value


def _limit(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000:
        raise ValidationError(f"{label} must be between 1 and 1000")


def _pending_count(
    reports: PulseReportStore, stage: Literal["relevance", "delivery", "investigation"]
) -> int:
    page = reports.list_pending(stage=stage, limit=1_000)
    return len(page.reports) + page.remaining


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
