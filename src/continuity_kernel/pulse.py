"""Deterministic admission and single-flight state for one bounded Pulse wake.

This module is deliberately content-blind.  It freezes identifiers and due
envelopes, accounts for a daily cognition budget, and leases one cognitive
episode.  It never calls a model, provider, network, browser, or Computer Use
surface; those capabilities are structurally outside the mechanical profile.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from continuity_kernel.atomic import atomic_write, exclusive_lock, read_regular_file, sha256_bytes
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ConflictError, ValidationError

STATE_FORMAT_VERSION: Final = 1
STATE_MAX_BYTES: Final = 64 * 1024
MECHANICAL_CPU_BUDGET_SECONDS: Final = 5
COGNITIVE_WALL_CLOCK_BUDGET_SECONDS: Final = 8 * 60
LEASE_STALE_AFTER_SECONDS: Final = 16 * 60
ACTIVE_SOURCE_MAX_STALENESS_SECONDS: Final = 6 * 60 * 60
WHOLE_MIND_MAX_STALENESS_SECONDS: Final = 24 * 60 * 60
_SOURCE_IDENTIFIER: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPAQUE_REFERENCE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9._/-]{0,991}$")
_OPAQUE_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

PULSE_ALLOWED_CAPABILITIES: Final = frozenset(
    {
        "clock_read",
        "local_state_read",
        "local_state_write",
        "receipt_emit",
    }
)
PULSE_FORBIDDEN_CAPABILITIES: Final = frozenset(
    {
        "browser",
        "computer_use",
        "external_action",
        "model_inference",
        "network",
        "provider_read",
        "provider_write",
        "screen_capture",
    }
)


class PulseMode(StrEnum):
    THINKING = "thinking"
    WATCHING_NOT_THINKING = "watching_not_thinking"


class AdmissionReason(StrEnum):
    MANUAL_REQUEST = "manual_request"
    PENDING_EVIDENCE = "pending_evidence"
    SOURCE_DUE = "source_due"
    RECHECK_DUE = "recheck_due"
    ACTIVE_SOURCE_MAX_STALENESS = "active_source_max_staleness"
    WHOLE_MIND_MAX_STALENESS = "whole_mind_max_staleness"


class SuppressionReason(StrEnum):
    NO_COGNITIVE_REASON = "no_cognitive_reason"
    SINGLE_FLIGHT_ACTIVE = "single_flight_active"
    DAILY_BUDGET_EXHAUSTED = "daily_budget_exhausted"


class MechanicalEvent(StrEnum):
    STALE_LEASE_RECLAIMED = "stale_lease_reclaimed"
    SLEEP_CATCH_UP_COALESCED = "sleep_catch_up_coalesced"
    WATCHING_RECEIPT_THROTTLED = "watching_receipt_throttled"


@dataclass(frozen=True)
class CapabilityProfile:
    """An explicit capability boundary for deterministic Pulse admission."""

    name: str
    allowed: frozenset[str]
    forbidden: frozenset[str]

    def authorize(self, requested: frozenset[str]) -> None:
        forbidden = requested & self.forbidden
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValidationError(f"Pulse capability profile forbids: {names}")
        unknown = requested - self.allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValidationError(f"Pulse capability profile does not allow: {names}")


MECHANICAL_PULSE_PROFILE: Final = CapabilityProfile(
    name="mechanical_local_only",
    allowed=PULSE_ALLOWED_CAPABILITIES,
    forbidden=PULSE_FORBIDDEN_CAPABILITIES,
)


@dataclass(frozen=True)
class PulsePolicy:
    """Mechanical limits applied before any cognitive episode can be admitted."""

    interval_seconds: int = 10 * 60
    mechanical_cpu_budget_seconds: int = MECHANICAL_CPU_BUDGET_SECONDS
    cognitive_wall_clock_budget_seconds: int = COGNITIVE_WALL_CLOCK_BUDGET_SECONDS
    lease_stale_after_seconds: int = LEASE_STALE_AFTER_SECONDS
    active_source_max_staleness_seconds: int = ACTIVE_SOURCE_MAX_STALENESS_SECONDS
    whole_mind_max_staleness_seconds: int = WHOLE_MIND_MAX_STALENESS_SECONDS
    daily_cognitive_budget: int = 72
    watching_receipt_interval_seconds: int = 60 * 60
    max_evidence_items: int = 64
    max_due_items: int = 64

    def __post_init__(self) -> None:
        if self.interval_seconds < 60:
            raise ValidationError("Pulse interval must be at least 60 seconds")
        if self.mechanical_cpu_budget_seconds != MECHANICAL_CPU_BUDGET_SECONDS:
            raise ValidationError("Pulse mechanical CPU budget is fixed at 5 seconds")
        if (
            not 1
            <= self.cognitive_wall_clock_budget_seconds
            <= min(
                self.interval_seconds,
                COGNITIVE_WALL_CLOCK_BUDGET_SECONDS,
            )
        ):
            raise ValidationError("Pulse cognitive wall-clock budget cannot exceed eight minutes")
        if self.lease_stale_after_seconds != LEASE_STALE_AFTER_SECONDS:
            raise ValidationError("Pulse stale-lease reclaim is fixed at 16 minutes")
        if self.active_source_max_staleness_seconds != ACTIVE_SOURCE_MAX_STALENESS_SECONDS:
            raise ValidationError("Pulse active-source maximum staleness is fixed at 6 hours")
        if self.whole_mind_max_staleness_seconds != WHOLE_MIND_MAX_STALENESS_SECONDS:
            raise ValidationError("Pulse whole-Mind maximum staleness is fixed at 24 hours")
        if self.daily_cognitive_budget < 1:
            raise ValidationError("Pulse daily cognition budget must be positive")
        if self.watching_receipt_interval_seconds < self.interval_seconds:
            raise ValidationError("watching receipt throttle cannot be shorter than the interval")
        if not 1 <= self.max_evidence_items <= 512:
            raise ValidationError("Pulse evidence bound is invalid")
        if not 1 <= self.max_due_items <= 512:
            raise ValidationError("Pulse due-item bound is invalid")


@dataclass(frozen=True)
class EvidenceEnvelope:
    """One content-free reference eligible for the start-of-wake frozen set."""

    identifier: str
    source: str
    observed_at: str
    reference: str | None = None


@dataclass(frozen=True)
class PulseRequest:
    evidence: tuple[EvidenceEnvelope, ...] = ()
    due_sources: tuple[str, ...] = ()
    due_rechecks: tuple[str, ...] = ()
    active_source_stale_due: tuple[str, ...] = ()
    whole_mind_stale_due: bool = False
    manual: bool = False
    requested_capabilities: frozenset[str] = PULSE_ALLOWED_CAPABILITIES


@dataclass(frozen=True)
class FrozenEvidenceBatch:
    batch_id: str
    frozen_at: str
    evidence: tuple[EvidenceEnvelope, ...]
    due_sources: tuple[str, ...]
    due_rechecks: tuple[str, ...]
    active_source_stale_due: tuple[str, ...]
    whole_mind_stale_due: bool


@dataclass(frozen=True)
class PulseReceipt:
    format_version: int
    observed_at: str
    mode: PulseMode
    cognitive_admitted: bool
    admission_reasons: tuple[AdmissionReason, ...]
    suppression_reasons: tuple[SuppressionReason, ...]
    mechanical_events: tuple[MechanicalEvent, ...]
    mechanical_cpu_budget_seconds: int
    mechanical_cpu_seconds_used: float
    within_mechanical_cpu_budget: bool
    cognitive_wall_clock_budget_seconds: int
    lease_stale_after_seconds: int
    active_source_max_staleness_seconds: int
    whole_mind_max_staleness_seconds: int
    lease_id: str | None
    lease_expires_at: str | None
    blocking_lease_expires_at: str | None
    evidence_batch: FrozenEvidenceBatch | None
    catch_up_intervals: int
    catch_up_coalesced: bool
    daily_budget_day: str
    daily_budget_limit: int
    daily_budget_used: int
    daily_budget_remaining: int
    receipt_emitted: bool
    throttled_until: str | None
    capability_profile: str
    state_revision: str


@dataclass(frozen=True)
class PulseCompletionReceipt:
    format_version: int
    lease_id: str
    batch_id: str
    outcome: Literal["completed", "abandoned"]
    completed_at: str
    elapsed_seconds: int
    cognitive_wall_clock_budget_seconds: int
    within_cognitive_wall_clock_budget: bool
    state_revision: str


@dataclass(frozen=True)
class PulseSnapshot:
    format_version: int
    last_heartbeat_at: str | None
    last_cognitive_at: str | None
    daily_budget_day: str | None
    daily_budget_used: int
    active_lease_id: str | None
    active_lease_expires_at: str | None
    state_revision: str | None


@dataclass(frozen=True)
class _LeaseState:
    lease_id: str
    acquired_at: str
    expires_at: str
    cognitive_wall_clock_budget_seconds: int
    batch: FrozenEvidenceBatch
    manual_requested: bool = False


@dataclass(frozen=True)
class _PulseState:
    generation: int = 0
    last_heartbeat_at: str | None = None
    last_cognitive_at: str | None = None
    daily_budget_day: str | None = None
    daily_budget_used: int = 0
    active_lease: _LeaseState | None = None
    pending_batch: FrozenEvidenceBatch | None = None
    pending_manual: bool = False
    last_watching_receipt_at: str | None = None
    last_watching_signature: str | None = None


class PulseController:
    """Own one cross-process Pulse admission state directory."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        policy: PulsePolicy | None = None,
        cpu_clock: Callable[[], float] = time.process_time,
    ):
        self.root = (root or (data_dir() / "pulse")).expanduser().resolve()
        self.policy = policy or PulsePolicy()
        self.cpu_clock = cpu_clock
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"

    def heartbeat(
        self,
        request: PulseRequest | None = None,
        *,
        now: datetime | None = None,
    ) -> PulseReceipt:
        """Run one local mechanical tick and possibly lease one cognitive wake."""

        cpu_started = self.cpu_clock()
        request = request or PulseRequest()
        MECHANICAL_PULSE_PROFILE.authorize(request.requested_capabilities)
        observed = _as_utc(now or datetime.now(UTC))
        normalized_evidence = _normalize_evidence(request.evidence, self.policy.max_evidence_items)
        due_sources = _source_values(request.due_sources, "due source", self.policy.max_due_items)
        due_rechecks = _reference_values(
            request.due_rechecks,
            "due recheck",
            self.policy.max_due_items,
        )
        active_source_stale_due = _source_values(
            request.active_source_stale_due,
            "active source stale due",
            self.policy.max_due_items,
        )
        if not isinstance(request.whole_mind_stale_due, bool):
            raise ValidationError("whole-Mind stale-due signal must be boolean")
        if not isinstance(request.manual, bool):
            raise ValidationError("manual Pulse signal must be boolean")

        with exclusive_lock(self.lock_path):
            state, _ = self._read_state()
            _reject_clock_regression(state, observed)
            budget_day = observed.date().isoformat()
            budget_used = state.daily_budget_used if state.daily_budget_day == budget_day else 0
            active = state.active_lease
            pending_batch = state.pending_batch
            pending_manual = state.pending_manual
            events: list[MechanicalEvent] = []
            if active is not None and observed >= _parse_time(active.expires_at, "lease expiry"):
                pending_batch = _coalesce_batch(
                    observed=observed,
                    pending=pending_batch,
                    evidence=active.batch.evidence,
                    due_sources=active.batch.due_sources,
                    due_rechecks=active.batch.due_rechecks,
                    active_source_stale_due=active.batch.active_source_stale_due,
                    whole_mind_stale_due=active.batch.whole_mind_stale_due,
                    policy=self.policy,
                )
                pending_manual = pending_manual or active.manual_requested
                active = None
                events.append(MechanicalEvent.STALE_LEASE_RECLAIMED)

            effective_batch = _coalesce_batch(
                observed=observed,
                pending=pending_batch,
                evidence=normalized_evidence,
                due_sources=due_sources,
                due_rechecks=due_rechecks,
                active_source_stale_due=active_source_stale_due,
                whole_mind_stale_due=request.whole_mind_stale_due,
                policy=self.policy,
            )
            effective_manual = pending_manual or request.manual
            effective_evidence = effective_batch.evidence if effective_batch is not None else ()
            effective_due_sources = (
                effective_batch.due_sources if effective_batch is not None else ()
            )
            effective_due_rechecks = (
                effective_batch.due_rechecks if effective_batch is not None else ()
            )
            effective_stale_due = (
                effective_batch.active_source_stale_due if effective_batch is not None else ()
            )
            effective_whole_mind_due = (
                effective_batch.whole_mind_stale_due if effective_batch is not None else False
            )

            catch_up_intervals = _catch_up_intervals(
                state.last_heartbeat_at,
                observed,
                self.policy.interval_seconds,
            )
            if catch_up_intervals:
                events.append(MechanicalEvent.SLEEP_CATCH_UP_COALESCED)
            admission_reasons = _admission_reasons(
                manual=effective_manual,
                whole_mind_stale_due=effective_whole_mind_due,
                evidence=effective_evidence,
                due_sources=effective_due_sources,
                due_rechecks=effective_due_rechecks,
                active_source_stale_due=effective_stale_due,
            )
            suppressions: list[SuppressionReason] = []
            if active is not None:
                suppressions.append(SuppressionReason.SINGLE_FLIGHT_ACTIVE)
            elif not admission_reasons:
                suppressions.append(SuppressionReason.NO_COGNITIVE_REASON)
            elif budget_used >= self.policy.daily_cognitive_budget:
                suppressions.append(SuppressionReason.DAILY_BUDGET_EXHAUSTED)

            admitted = not suppressions
            batch: FrozenEvidenceBatch | None = None
            lease: _LeaseState | None = active
            receipt_emitted = True
            throttled_until: str | None = None
            last_watching_at = state.last_watching_receipt_at
            last_watching_signature = state.last_watching_signature
            next_pending_batch = effective_batch
            next_pending_manual = effective_manual

            if admitted:
                batch = effective_batch or _freeze_batch(
                    observed=observed,
                    evidence=(),
                    due_sources=(),
                    due_rechecks=(),
                    active_source_stale_due=(),
                    whole_mind_stale_due=False,
                )
                lease_id = secrets.token_urlsafe(24)
                expires = observed + timedelta(seconds=self.policy.lease_stale_after_seconds)
                lease = _LeaseState(
                    lease_id=lease_id,
                    acquired_at=_format_time(observed),
                    expires_at=_format_time(expires),
                    cognitive_wall_clock_budget_seconds=(
                        self.policy.cognitive_wall_clock_budget_seconds
                    ),
                    batch=batch,
                    manual_requested=effective_manual,
                )
                next_pending_batch = None
                next_pending_manual = False
                budget_used += 1
            else:
                signature = _watching_signature(admission_reasons, suppressions, events)
                last_watching = (
                    _parse_time(last_watching_at, "last watching receipt")
                    if last_watching_at is not None
                    else None
                )
                throttle_end = (
                    last_watching + timedelta(seconds=self.policy.watching_receipt_interval_seconds)
                    if last_watching is not None
                    else None
                )
                receipt_emitted = (
                    last_watching is None
                    or signature != last_watching_signature
                    or throttle_end is None
                    or observed >= throttle_end
                )
                if receipt_emitted:
                    last_watching_at = _format_time(observed)
                    last_watching_signature = signature
                else:
                    events.append(MechanicalEvent.WATCHING_RECEIPT_THROTTLED)
                    if throttle_end is None:  # pragma: no cover - guarded by receipt_emitted
                        raise ValidationError("Pulse watching throttle has no deadline")
                    throttled_until = _format_time(throttle_end)

            next_state = _PulseState(
                generation=state.generation + 1,
                last_heartbeat_at=_format_time(observed),
                last_cognitive_at=state.last_cognitive_at,
                daily_budget_day=budget_day,
                daily_budget_used=budget_used,
                active_lease=lease,
                pending_batch=next_pending_batch,
                pending_manual=next_pending_manual,
                last_watching_receipt_at=last_watching_at,
                last_watching_signature=last_watching_signature,
            )
            encoded = _encode_state(next_state)
            atomic_write(self.state_path, encoded)

        cpu_seconds_used = max(0.0, self.cpu_clock() - cpu_started)
        return PulseReceipt(
            format_version=STATE_FORMAT_VERSION,
            observed_at=_format_time(observed),
            mode=PulseMode.THINKING if admitted else PulseMode.WATCHING_NOT_THINKING,
            cognitive_admitted=admitted,
            admission_reasons=tuple(admission_reasons),
            suppression_reasons=tuple(suppressions),
            mechanical_events=tuple(events),
            mechanical_cpu_budget_seconds=self.policy.mechanical_cpu_budget_seconds,
            mechanical_cpu_seconds_used=cpu_seconds_used,
            within_mechanical_cpu_budget=(
                cpu_seconds_used <= self.policy.mechanical_cpu_budget_seconds
            ),
            cognitive_wall_clock_budget_seconds=(self.policy.cognitive_wall_clock_budget_seconds),
            lease_stale_after_seconds=self.policy.lease_stale_after_seconds,
            active_source_max_staleness_seconds=(self.policy.active_source_max_staleness_seconds),
            whole_mind_max_staleness_seconds=self.policy.whole_mind_max_staleness_seconds,
            lease_id=lease.lease_id if admitted and lease is not None else None,
            lease_expires_at=lease.expires_at if admitted and lease is not None else None,
            blocking_lease_expires_at=(
                active.expires_at
                if not admitted
                and active is not None
                and SuppressionReason.SINGLE_FLIGHT_ACTIVE in suppressions
                else None
            ),
            evidence_batch=batch,
            catch_up_intervals=catch_up_intervals,
            catch_up_coalesced=catch_up_intervals > 0,
            daily_budget_day=budget_day,
            daily_budget_limit=self.policy.daily_cognitive_budget,
            daily_budget_used=budget_used,
            daily_budget_remaining=max(0, self.policy.daily_cognitive_budget - budget_used),
            receipt_emitted=receipt_emitted,
            throttled_until=throttled_until,
            capability_profile=MECHANICAL_PULSE_PROFILE.name,
            state_revision=sha256_bytes(encoded),
        )

    def complete(
        self,
        lease_id: str,
        *,
        outcome: Literal["completed", "abandoned"] = "completed",
        now: datetime | None = None,
    ) -> PulseCompletionReceipt:
        """Release exactly one live lease and report its cognitive wall-clock use."""

        lease_id = _bounded_text(lease_id, "lease ID", 256)
        if outcome not in {"completed", "abandoned"}:
            raise ValidationError("Pulse completion outcome is invalid")
        observed = _as_utc(now or datetime.now(UTC))
        with exclusive_lock(self.lock_path):
            state, _ = self._read_state()
            lease = state.active_lease
            if lease is None or lease.lease_id != lease_id:
                raise ConflictError("Pulse lease is no longer active")
            _reject_clock_regression(state, observed)
            acquired = _parse_time(lease.acquired_at, "lease acquisition")
            if observed < acquired:
                raise ConflictError("Pulse completion precedes its lease acquisition")
            expires = _parse_time(lease.expires_at, "lease expiry")
            if observed >= expires:
                expired_state = _PulseState(
                    generation=state.generation + 1,
                    last_heartbeat_at=state.last_heartbeat_at,
                    last_cognitive_at=state.last_cognitive_at,
                    daily_budget_day=state.daily_budget_day,
                    daily_budget_used=state.daily_budget_used,
                    active_lease=None,
                    pending_batch=_coalesce_batch(
                        observed=observed,
                        pending=state.pending_batch,
                        evidence=lease.batch.evidence,
                        due_sources=lease.batch.due_sources,
                        due_rechecks=lease.batch.due_rechecks,
                        active_source_stale_due=lease.batch.active_source_stale_due,
                        whole_mind_stale_due=lease.batch.whole_mind_stale_due,
                        policy=self.policy,
                    ),
                    pending_manual=state.pending_manual or lease.manual_requested,
                    last_watching_receipt_at=state.last_watching_receipt_at,
                    last_watching_signature=state.last_watching_signature,
                )
                atomic_write(self.state_path, _encode_state(expired_state))
                raise ConflictError("Pulse lease expired before completion")
            elapsed = int((observed - acquired).total_seconds())
            effective_outcome: Literal["completed", "abandoned"] = outcome
            if elapsed > lease.cognitive_wall_clock_budget_seconds:
                effective_outcome = "abandoned"
            pending_batch = state.pending_batch
            pending_manual = state.pending_manual
            if effective_outcome == "abandoned":
                pending_batch = _coalesce_batch(
                    observed=observed,
                    pending=pending_batch,
                    evidence=lease.batch.evidence,
                    due_sources=lease.batch.due_sources,
                    due_rechecks=lease.batch.due_rechecks,
                    active_source_stale_due=lease.batch.active_source_stale_due,
                    whole_mind_stale_due=lease.batch.whole_mind_stale_due,
                    policy=self.policy,
                )
                pending_manual = pending_manual or lease.manual_requested
            next_state = _PulseState(
                generation=state.generation + 1,
                last_heartbeat_at=state.last_heartbeat_at,
                last_cognitive_at=(
                    _format_time(observed)
                    if effective_outcome == "completed"
                    else state.last_cognitive_at
                ),
                daily_budget_day=state.daily_budget_day,
                daily_budget_used=state.daily_budget_used,
                active_lease=None,
                pending_batch=pending_batch,
                pending_manual=pending_manual,
                last_watching_receipt_at=state.last_watching_receipt_at,
                last_watching_signature=state.last_watching_signature,
            )
            encoded = _encode_state(next_state)
            atomic_write(self.state_path, encoded)
        return PulseCompletionReceipt(
            format_version=STATE_FORMAT_VERSION,
            lease_id=lease.lease_id,
            batch_id=lease.batch.batch_id,
            outcome=effective_outcome,
            completed_at=_format_time(observed),
            elapsed_seconds=elapsed,
            cognitive_wall_clock_budget_seconds=lease.cognitive_wall_clock_budget_seconds,
            within_cognitive_wall_clock_budget=(
                elapsed <= lease.cognitive_wall_clock_budget_seconds
            ),
            state_revision=sha256_bytes(encoded),
        )

    def snapshot(self) -> PulseSnapshot:
        """Read bounded local state without changing admission or budget counters."""

        with exclusive_lock(self.lock_path):
            state, revision = self._read_state()
        active = state.active_lease
        return PulseSnapshot(
            format_version=STATE_FORMAT_VERSION,
            last_heartbeat_at=state.last_heartbeat_at,
            last_cognitive_at=state.last_cognitive_at,
            daily_budget_day=state.daily_budget_day,
            daily_budget_used=state.daily_budget_used,
            active_lease_id=active.lease_id if active else None,
            active_lease_expires_at=active.expires_at if active else None,
            state_revision=revision,
        )

    def _read_state(self) -> tuple[_PulseState, str | None]:
        if not os.path.lexists(self.state_path):
            return _PulseState(), None
        encoded = read_regular_file(
            self.state_path,
            label="Pulse state",
            max_bytes=STATE_MAX_BYTES,
        )
        return _parse_state(encoded), sha256_bytes(encoded)


def _admission_reasons(
    *,
    manual: bool,
    whole_mind_stale_due: bool,
    evidence: tuple[EvidenceEnvelope, ...],
    due_sources: tuple[str, ...],
    due_rechecks: tuple[str, ...],
    active_source_stale_due: tuple[str, ...],
) -> list[AdmissionReason]:
    reasons: list[AdmissionReason] = []
    if manual:
        reasons.append(AdmissionReason.MANUAL_REQUEST)
    if evidence:
        reasons.append(AdmissionReason.PENDING_EVIDENCE)
    if due_sources:
        reasons.append(AdmissionReason.SOURCE_DUE)
    if due_rechecks:
        reasons.append(AdmissionReason.RECHECK_DUE)
    if active_source_stale_due:
        reasons.append(AdmissionReason.ACTIVE_SOURCE_MAX_STALENESS)
    if whole_mind_stale_due:
        reasons.append(AdmissionReason.WHOLE_MIND_MAX_STALENESS)
    return reasons


def _catch_up_intervals(last_value: str | None, now: datetime, interval_seconds: int) -> int:
    if last_value is None:
        return 0
    last = _parse_time(last_value, "last heartbeat")
    elapsed = max(0, int((now - last).total_seconds()))
    return max(0, elapsed // interval_seconds - 1)


def _reject_clock_regression(state: _PulseState, observed: datetime) -> None:
    """Reject a wall clock earlier than any persisted monotonic admission boundary."""

    if state.last_heartbeat_at is not None:
        last_heartbeat = _parse_time(state.last_heartbeat_at, "last heartbeat")
        if observed < last_heartbeat:
            raise ConflictError("Pulse clock regressed behind the persisted heartbeat")
    if state.last_cognitive_at is not None:
        last_cognitive = _parse_time(state.last_cognitive_at, "last cognitive completion")
        if observed < last_cognitive:
            raise ConflictError("Pulse clock regressed behind the last cognitive completion")
    if state.active_lease is not None:
        acquired = _parse_time(state.active_lease.acquired_at, "lease acquisition")
        if observed < acquired:
            raise ConflictError("Pulse clock regressed behind the active lease acquisition")
    if state.daily_budget_day is not None and observed.date().isoformat() < state.daily_budget_day:
        raise ConflictError("Pulse clock cannot move the daily budget backward")


def _freeze_batch(
    *,
    observed: datetime,
    evidence: tuple[EvidenceEnvelope, ...],
    due_sources: tuple[str, ...],
    due_rechecks: tuple[str, ...],
    active_source_stale_due: tuple[str, ...],
    whole_mind_stale_due: bool,
) -> FrozenEvidenceBatch:
    frozen_at = _format_time(observed)
    payload = {
        "active_source_stale_due": list(active_source_stale_due),
        "due_rechecks": list(due_rechecks),
        "due_sources": list(due_sources),
        "evidence": [asdict(item) for item in evidence],
        "frozen_at": frozen_at,
        "whole_mind_stale_due": whole_mind_stale_due,
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return FrozenEvidenceBatch(
        batch_id=sha256_bytes(canonical),
        frozen_at=frozen_at,
        evidence=evidence,
        due_sources=due_sources,
        due_rechecks=due_rechecks,
        active_source_stale_due=active_source_stale_due,
        whole_mind_stale_due=whole_mind_stale_due,
    )


def _coalesce_batch(
    *,
    observed: datetime,
    pending: FrozenEvidenceBatch | None,
    evidence: tuple[EvidenceEnvelope, ...],
    due_sources: tuple[str, ...],
    due_rechecks: tuple[str, ...],
    active_source_stale_due: tuple[str, ...],
    whole_mind_stale_due: bool,
    policy: PulsePolicy,
) -> FrozenEvidenceBatch | None:
    """Merge one pending wake without replaying intervals or dropping evidence."""

    prior_evidence = pending.evidence if pending is not None else ()
    by_identifier: dict[str, EvidenceEnvelope] = {}
    for item in (*prior_evidence, *evidence):
        prior = by_identifier.get(item.identifier)
        if prior is not None and prior != item:
            raise ValidationError(f"conflicting evidence ID: {item.identifier}")
        by_identifier[item.identifier] = item
    if len(by_identifier) > policy.max_evidence_items:
        raise ValidationError(
            f"Pulse pending evidence exceeds its bound of {policy.max_evidence_items}"
        )

    def merged_values(
        previous: tuple[str, ...],
        current: tuple[str, ...],
        label: str,
    ) -> tuple[str, ...]:
        merged = tuple(sorted(set(previous) | set(current)))
        if len(merged) > policy.max_due_items:
            raise ValidationError(
                f"Pulse pending {label} values exceed their bound of {policy.max_due_items}"
            )
        return merged

    merged_evidence = tuple(
        sorted(by_identifier.values(), key=lambda item: (item.source, item.identifier))
    )
    merged_sources = merged_values(
        pending.due_sources if pending is not None else (), due_sources, "due source"
    )
    merged_rechecks = merged_values(
        pending.due_rechecks if pending is not None else (), due_rechecks, "due recheck"
    )
    merged_stale = merged_values(
        pending.active_source_stale_due if pending is not None else (),
        active_source_stale_due,
        "active source stale due",
    )
    merged_whole = whole_mind_stale_due or (
        pending.whole_mind_stale_due if pending is not None else False
    )
    if not (merged_evidence or merged_sources or merged_rechecks or merged_stale or merged_whole):
        return None
    return _freeze_batch(
        observed=observed,
        evidence=merged_evidence,
        due_sources=merged_sources,
        due_rechecks=merged_rechecks,
        active_source_stale_due=merged_stale,
        whole_mind_stale_due=merged_whole,
    )


def _normalize_evidence(
    values: tuple[EvidenceEnvelope, ...],
    maximum: int,
) -> tuple[EvidenceEnvelope, ...]:
    if len(values) > maximum:
        raise ValidationError(f"Pulse evidence exceeds its bound of {maximum}")
    by_identifier: dict[str, EvidenceEnvelope] = {}
    for value in values:
        normalized = EvidenceEnvelope(
            identifier=_opaque_reference(value.identifier, "evidence ID", 256),
            source=_source_identifier(value.source, "evidence source"),
            observed_at=_format_time(_parse_time(value.observed_at, "evidence observation")),
            reference=(
                _opaque_reference(value.reference, "evidence reference", 1024)
                if value.reference is not None
                else None
            ),
        )
        prior = by_identifier.get(normalized.identifier)
        if prior is not None and prior != normalized:
            raise ValidationError(f"conflicting evidence ID: {normalized.identifier}")
        by_identifier[normalized.identifier] = normalized
    return tuple(sorted(by_identifier.values(), key=lambda item: (item.source, item.identifier)))


def _source_values(values: tuple[str, ...], label: str, maximum: int) -> tuple[str, ...]:
    if len(values) > maximum:
        raise ValidationError(f"Pulse {label} values exceed their bound of {maximum}")
    return tuple(sorted({_source_identifier(value, label) for value in values}))


def _reference_values(values: tuple[str, ...], label: str, maximum: int) -> tuple[str, ...]:
    if len(values) > maximum:
        raise ValidationError(f"Pulse {label} values exceed their bound of {maximum}")
    return tuple(sorted({_opaque_reference(value, label, 256) for value in values}))


def _source_identifier(value: str, label: str) -> str:
    normalized = _bounded_text(value, label, 64)
    if _SOURCE_IDENTIFIER.fullmatch(normalized) is None:
        raise ValidationError(f"{label} must be an opaque source identifier")
    return normalized


def _opaque_reference(value: str, label: str, maximum: int) -> str:
    normalized = _bounded_text(value, label, maximum)
    if _OPAQUE_DIGEST.fullmatch(normalized) is not None:
        return normalized
    if _OPAQUE_REFERENCE.fullmatch(normalized) is None:
        raise ValidationError(f"{label} must be an opaque namespaced reference")
    return f"sha256:{sha256_bytes(normalized.encode('utf-8'))}"


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    normalized = value.strip()
    if not normalized or "\x00" in normalized or "\n" in normalized or "\r" in normalized:
        raise ValidationError(f"{label} is invalid")
    if len(normalized.encode("utf-8")) > maximum:
        raise ValidationError(f"{label} exceeds its size bound")
    return normalized


def _watching_signature(
    admissions: list[AdmissionReason],
    suppressions: list[SuppressionReason],
    events: list[MechanicalEvent],
) -> str:
    payload = {
        "admissions": [item.value for item in admissions],
        "events": [item.value for item in events],
        "suppressions": [item.value for item in suppressions],
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True).encode("utf-8"))


def _encode_state(state: _PulseState) -> bytes:
    payload = {
        "active_lease": _lease_payload(state.active_lease),
        "daily_budget_day": state.daily_budget_day,
        "daily_budget_used": state.daily_budget_used,
        "format_version": STATE_FORMAT_VERSION,
        "generation": state.generation,
        "last_cognitive_at": state.last_cognitive_at,
        "last_heartbeat_at": state.last_heartbeat_at,
        "last_watching_receipt_at": state.last_watching_receipt_at,
        "last_watching_signature": state.last_watching_signature,
        "pending_batch": _batch_payload(state.pending_batch),
        "pending_manual": state.pending_manual,
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > STATE_MAX_BYTES:  # pragma: no cover - policy bounds make this defensive
        raise ValidationError("Pulse state exceeds its size bound")
    return encoded


def _lease_payload(lease: _LeaseState | None) -> dict[str, object] | None:
    if lease is None:
        return None
    return {
        "acquired_at": lease.acquired_at,
        "batch": _batch_payload(lease.batch),
        "expires_at": lease.expires_at,
        "lease_id": lease.lease_id,
        "cognitive_wall_clock_budget_seconds": lease.cognitive_wall_clock_budget_seconds,
        "manual_requested": lease.manual_requested,
    }


def _batch_payload(batch: FrozenEvidenceBatch | None) -> dict[str, object] | None:
    if batch is None:
        return None
    return {
        "active_source_stale_due": list(batch.active_source_stale_due),
        "batch_id": batch.batch_id,
        "due_rechecks": list(batch.due_rechecks),
        "due_sources": list(batch.due_sources),
        "evidence": [asdict(item) for item in batch.evidence],
        "frozen_at": batch.frozen_at,
        "whole_mind_stale_due": batch.whole_mind_stale_due,
    }


def _parse_state(encoded: bytes) -> _PulseState:
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid Pulse state") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != STATE_FORMAT_VERSION:
        raise ValidationError("unsupported Pulse state version")
    generation = _nonnegative_integer(payload.get("generation"), "Pulse generation")
    daily_budget_used = _nonnegative_integer(
        payload.get("daily_budget_used"),
        "Pulse daily budget",
    )
    daily_budget_day = _optional_text(payload.get("daily_budget_day"), "Pulse budget day", 32)
    if daily_budget_day is not None:
        try:
            datetime.strptime(daily_budget_day, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("Pulse budget day is invalid") from exc
    timestamps = {
        name: _optional_timestamp(payload.get(name), name)
        for name in (
            "last_cognitive_at",
            "last_heartbeat_at",
            "last_watching_receipt_at",
        )
    }
    signature = _optional_text(
        payload.get("last_watching_signature"),
        "watching signature",
        128,
    )
    active_payload = payload.get("active_lease")
    active = _parse_lease(active_payload) if active_payload is not None else None
    pending_payload = payload.get("pending_batch")
    pending = _parse_batch(pending_payload) if pending_payload is not None else None
    pending_manual = payload.get("pending_manual", False)
    if not isinstance(pending_manual, bool):
        raise ValidationError("Pulse pending-manual signal is invalid")
    return _PulseState(
        generation=generation,
        last_heartbeat_at=timestamps["last_heartbeat_at"],
        last_cognitive_at=timestamps["last_cognitive_at"],
        daily_budget_day=daily_budget_day,
        daily_budget_used=daily_budget_used,
        active_lease=active,
        pending_batch=pending,
        pending_manual=pending_manual,
        last_watching_receipt_at=timestamps["last_watching_receipt_at"],
        last_watching_signature=signature,
    )


def _parse_lease(value: object) -> _LeaseState:
    if not isinstance(value, dict):
        raise ValidationError("Pulse lease is invalid")
    lease_id = _object_text(value.get("lease_id"), "lease ID", 256)
    acquired_at = _required_timestamp(value.get("acquired_at"), "lease acquisition")
    expires_at = _required_timestamp(value.get("expires_at"), "lease expiry")
    lease_duration = _parse_time(expires_at, "lease expiry") - _parse_time(
        acquired_at,
        "lease acquisition",
    )
    if lease_duration != timedelta(seconds=LEASE_STALE_AFTER_SECONDS):
        raise ValidationError("Pulse lease expiry is invalid")
    cognitive_wall_clock_budget = _positive_integer(
        value.get("cognitive_wall_clock_budget_seconds"),
        "lease cognitive wall-clock budget",
    )
    if cognitive_wall_clock_budget > COGNITIVE_WALL_CLOCK_BUDGET_SECONDS:
        raise ValidationError("lease cognitive wall-clock budget exceeds eight minutes")
    batch = _parse_batch(value.get("batch"))
    manual_requested = value.get("manual_requested", False)
    if not isinstance(manual_requested, bool):
        raise ValidationError("Pulse lease manual-request signal is invalid")
    return _LeaseState(
        lease_id=lease_id,
        acquired_at=acquired_at,
        expires_at=expires_at,
        cognitive_wall_clock_budget_seconds=cognitive_wall_clock_budget,
        batch=batch,
        manual_requested=manual_requested,
    )


def _parse_batch(value: object) -> FrozenEvidenceBatch:
    if not isinstance(value, dict):
        raise ValidationError("Pulse evidence batch is invalid")
    batch_id = _object_text(value.get("batch_id"), "batch ID", 128)
    frozen_at = _required_timestamp(value.get("frozen_at"), "batch freeze time")
    raw_evidence = value.get("evidence")
    if not isinstance(raw_evidence, list):
        raise ValidationError("Pulse evidence batch items are invalid")
    evidence_values: list[EvidenceEnvelope] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            raise ValidationError("Pulse evidence batch item is invalid")
        reference = item.get("reference")
        evidence_values.append(
            EvidenceEnvelope(
                identifier=_object_text(item.get("identifier"), "evidence ID", 256),
                source=_object_text(item.get("source"), "evidence source", 128),
                observed_at=_required_timestamp(
                    item.get("observed_at"),
                    "evidence observation",
                ),
                reference=(
                    _object_text(reference, "evidence reference", 1024)
                    if reference is not None
                    else None
                ),
            )
        )
    evidence = _normalize_evidence(tuple(evidence_values), 512)
    due_sources = _stored_string_tuple(value.get("due_sources"), "due source", source=True)
    due_rechecks = _stored_string_tuple(value.get("due_rechecks"), "due recheck", source=False)
    active_source_stale_due = _stored_string_tuple(
        value.get("active_source_stale_due"),
        "active source stale due",
        source=True,
    )
    whole_mind_stale_due = value.get("whole_mind_stale_due")
    if not isinstance(whole_mind_stale_due, bool):
        raise ValidationError("Pulse whole-Mind stale-due signal is invalid")
    candidate = _freeze_batch(
        observed=_parse_time(frozen_at, "batch freeze time"),
        evidence=evidence,
        due_sources=due_sources,
        due_rechecks=due_rechecks,
        active_source_stale_due=active_source_stale_due,
        whole_mind_stale_due=whole_mind_stale_due,
    )
    if candidate.batch_id != batch_id:
        raise ValidationError("Pulse evidence batch digest does not match its contents")
    return candidate


def _stored_string_tuple(value: object, label: str, *, source: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"Pulse {label} values are invalid")
    values = tuple(value)
    if source:
        return _source_values(values, label, 512)
    return _reference_values(values, label, 512)


def _nonnegative_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{label} is invalid")
    return value


def _positive_integer(value: object, label: str) -> int:
    result = _nonnegative_integer(value, label)
    if result == 0:
        raise ValidationError(f"{label} is invalid")
    return result


def _optional_text(value: object, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    return _bounded_text(value, label, maximum)


def _object_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    return _bounded_text(value, label, maximum)


def _optional_timestamp(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_timestamp(value, label)


def _required_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a timestamp")
    return _format_time(_parse_time(value, label))


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValidationError("Pulse time must include a timezone")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
