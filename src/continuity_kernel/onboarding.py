"""Deterministic, local-first onboarding and connector-readiness records.

The portable onboarding session is authoritative Markdown inside the vault.
Machine-specific capability evidence is a separate JSON receipt under the
application data directory.  Neither record accepts provider bodies or model
narrative as readiness proof.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeVar

from continuity_kernel.atomic import (
    PinnedPathRoot,
    atomic_write,
    exclusive_lock,
    read_regular_file,
    sha256_bytes,
)
from continuity_kernel.errors import ConflictError, NotFoundError, ValidationError
from continuity_kernel.records import format_time, next_timestamp, parse_time, stored_time
from continuity_kernel.source_recipes import get_recipe

ONBOARDING_FORMAT_VERSION: Final = 1
HOST_READINESS_FORMAT_VERSION: Final = 1
MAX_SESSION_BYTES: Final = 128 * 1024
MAX_HOST_RECEIPT_BYTES: Final = 256 * 1024
MAX_SOURCES: Final = 32
DEFAULT_LEASE_TTL: Final = timedelta(minutes=30)
MIN_LEASE_TTL: Final = timedelta(seconds=5)
MAX_LEASE_TTL: Final = timedelta(hours=24)
DEFAULT_ATTESTATION_MAX_AGE: Final = timedelta(minutes=15)
HOST_PROOF_MAX_AGE: Final = timedelta(hours=24)
MAX_FUTURE_SKEW: Final = timedelta(minutes=1)

_SESSION_META = re.compile(r"^<!-- gsv-onboarding:(\{.*\}) -->$")
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SAFE_OWNER_REF = re.compile(r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TOOL_CALL_ID = re.compile(r"^call:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{1,255}$")
_SAFE_AUTOMATION_REF = re.compile(r"^automation:[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class OnboardingPhase(StrEnum):
    CODEX_SUBSTRATE = "codex_substrate"
    PRIVACY_AND_CONTEXT_CAPTURE = "privacy_and_context_capture"
    SOURCE_SELECTION = "source_selection"
    ENABLEMENT_WAIT = "enablement_wait"
    FRESH_TASK_VERIFICATION = "fresh_task_verification"
    CONTEXT_SYNTHESIS = "context_synthesis"
    INITIAL_ORIENTATION = "initial_orientation"
    CONTINUITY_AND_AUTONOMY_PROOF = "continuity_and_autonomy_proof"
    DONE = "done"


class CompletionState(StrEnum):
    IN_PROGRESS = "in_progress"
    WAITING_USER = "waiting_user"
    FRESH_TASK_REQUIRED = "fresh_task_required"
    BLOCKED = "blocked"
    OPERATIONAL_WITH_GAPS = "operational_with_gaps"
    FULLY_CONNECTED = "fully_connected"
    NEEDS_REVALIDATION = "needs_revalidation"


class SourceState(StrEnum):
    NOT_SELECTED = "not_selected"
    PLUGIN_MISSING = "plugin_missing"
    AUTH_REQUIRED = "auth_required"
    FRESH_TASK_REQUIRED = "fresh_task_required"
    TOOL_ABSENT = "tool_absent"
    IDENTITY_PENDING = "identity_pending"
    IDENTITY_MISMATCH = "identity_mismatch"
    READ_UNVERIFIED = "read_unverified"
    CANARY_FAILED = "canary_failed"
    READY = "ready"
    STALE = "stale"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    UNSUPPORTED_TOOL_SHAPE = "unsupported_tool_shape"
    UNAVAILABLE = "unavailable"
    DECLINED = "declined"


class HostCapabilityState(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    WAITING_USER = "waiting-user"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    STALE = "stale"


class PermissionState(StrEnum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    DENIED = "denied"
    WAITING_USER = "waiting_user"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    UNSUPPORTED = "unsupported"


class ScheduledWakeState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    SCHEDULED = "scheduled"
    VERIFIED = "verified"
    FAILED = "failed"
    STALE = "stale"


class ContextSection(StrEnum):
    PURPOSE = "purpose"
    LIFE_MAP = "life_map"
    CURRENT_OUTCOMES = "current_outcomes"
    RECURRING_ENTITIES = "recurring_entities"
    WORKING_PREFERENCES = "working_preferences"
    PRIVACY_ACTION_BOUNDARIES = "privacy_action_boundaries"


class ContextState(StrEnum):
    UNASKED = "unasked"
    DRAFTED = "drafted"
    CONFIRMED = "confirmed"
    DECLINED = "declined"


class NextActor(StrEnum):
    AGENT = "agent"
    USER = "user"
    WORLD = "world"


class AttestationEvent(StrEnum):
    TOOL_RESULT = "tool_result"
    TOOL_ABSENT = "tool_absent"


class AttestationDecisionReason(StrEnum):
    APPLIED = "applied"
    PRIOR_READY_PRESERVED = "prior_ready_preserved"


@dataclass(frozen=True)
class ContextCheckpoint:
    section: ContextSection
    state: ContextState


@dataclass(frozen=True)
class SessionLease:
    owner_ref: str
    lease_id: str
    acquired_at: str
    expires_at: str
    generation: int
    predecessor_lease_id: str | None = None


@dataclass(frozen=True)
class OnboardingSession:
    format_version: int
    onboarding_id: str
    phase: OnboardingPhase
    completion: CompletionState
    selected_sources: tuple[str, ...]
    source_selection_confirmed: bool
    context: tuple[ContextCheckpoint, ...]
    next_actor: NextActor | None
    next_action_code: str | None
    lease: SessionLease | None
    created_at: str
    updated_at: str
    revision: str


@dataclass(frozen=True)
class SourceReadiness:
    source_id: str
    state: SourceState
    expected_account_fingerprint: str | None = None
    observed_account_fingerprint: str | None = None
    tool_name: str | None = None
    tool_shape_fingerprint: str | None = None
    stable_ref: str | None = None
    result_digest: str | None = None
    last_verified_at: str | None = None
    last_observed_at: str | None = None
    last_error_code: str | None = None


@dataclass(frozen=True)
class CapabilityFingerprint:
    capability_id: str
    fingerprint: str
    observed_at: str


@dataclass(frozen=True)
class PermissionEvidence:
    permission_id: str
    state: PermissionState
    evidence_ref: str
    observed_at: str


@dataclass(frozen=True)
class ScheduledWakeEvidence:
    state: ScheduledWakeState
    automation_ref: str | None
    task_id: str | None
    scheduled_for: str | None
    observed_at: str
    result_digest: str | None


@dataclass(frozen=True)
class HostReadinessReceipt:
    format_version: int
    vault_id: str
    host_id: str
    app_version: str
    core_version: str
    plugin_version: str
    capability_fingerprints: tuple[CapabilityFingerprint, ...]
    permission_evidence: tuple[PermissionEvidence, ...]
    scheduled_wake_evidence: ScheduledWakeEvidence
    codex_state: HostCapabilityState
    bridge_state: HostCapabilityState
    pulse_state: HostCapabilityState
    computer_use_state: HostCapabilityState
    context_synthesis_proved: bool
    initial_orientation_proved: bool
    fresh_task_context_proved: bool
    sources: tuple[SourceReadiness, ...]
    created_at: str
    updated_at: str
    revision: str


@dataclass(frozen=True)
class ToolAttestation:
    """A bounded model-submitted claim that must match a trusted host receipt."""

    format_version: int
    event: AttestationEvent
    task_id: str
    source_id: str
    observed_at: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_shape_fingerprint: str | None = None
    account_fingerprint: str | None = None
    stable_ref: str | None = None
    result_digest: str | None = None
    read_complete: bool | None = None
    records_observed: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ToolAttestation:
        _exact_keys(value, _ATTESTATION_KEYS, "tool attestation")
        result = cls(
            format_version=_integer(value, "format_version"),
            event=_enum(AttestationEvent, value.get("event"), "attestation event"),
            task_id=_required_string(value, "task_id"),
            source_id=_required_string(value, "source_id"),
            observed_at=_required_string(value, "observed_at"),
            tool_call_id=_optional_string(value, "tool_call_id"),
            tool_name=_optional_string(value, "tool_name"),
            tool_shape_fingerprint=_optional_string(value, "tool_shape_fingerprint"),
            account_fingerprint=_optional_string(value, "account_fingerprint"),
            stable_ref=_optional_string(value, "stable_ref"),
            result_digest=_optional_string(value, "result_digest"),
            read_complete=_optional_boolean(value, "read_complete"),
            records_observed=_optional_integer(value, "records_observed"),
        )
        _validate_tool_attestation_shape(result)
        return result

    def as_mapping(self) -> dict[str, Any]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "event": self.event.value,
            "format_version": self.format_version,
            "observed_at": self.observed_at,
            "read_complete": self.read_complete,
            "records_observed": self.records_observed,
            "result_digest": self.result_digest,
            "source_id": self.source_id,
            "stable_ref": self.stable_ref,
            "task_id": self.task_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_shape_fingerprint": self.tool_shape_fingerprint,
        }


@dataclass(frozen=True)
class TrustedToolReceipt:
    """Host-observed, content-free facts for one completed app tool call."""

    call_id: str
    task_id: str
    source_id: str
    tool_name: str
    tool_shape_fingerprint: str
    account_fingerprint: str
    stable_ref: str
    result_digest: str
    read_complete: bool
    records_observed: int
    observed_at: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TrustedToolReceipt:
        _exact_keys(value, _TRUSTED_RECEIPT_KEYS, "trusted tool receipt")
        result = cls(
            call_id=_required_string(value, "call_id"),
            task_id=_required_string(value, "task_id"),
            source_id=_required_string(value, "source_id"),
            tool_name=_required_string(value, "tool_name"),
            tool_shape_fingerprint=_required_string(value, "tool_shape_fingerprint"),
            account_fingerprint=_required_string(value, "account_fingerprint"),
            stable_ref=_required_string(value, "stable_ref"),
            result_digest=_required_string(value, "result_digest"),
            read_complete=_boolean(value, "read_complete"),
            records_observed=_integer(value, "records_observed"),
            observed_at=_required_string(value, "observed_at"),
        )
        _validate_trusted_receipt(result)
        return result


@dataclass(frozen=True)
class AttestationDecision:
    applied: bool
    reason: AttestationDecisionReason
    readiness: SourceReadiness


_ATTESTATION_KEYS: Final = frozenset(
    {
        "account_fingerprint",
        "event",
        "format_version",
        "observed_at",
        "read_complete",
        "records_observed",
        "result_digest",
        "source_id",
        "stable_ref",
        "task_id",
        "tool_call_id",
        "tool_name",
        "tool_shape_fingerprint",
    }
)
_TRUSTED_RECEIPT_KEYS: Final = frozenset(
    {
        "account_fingerprint",
        "call_id",
        "observed_at",
        "read_complete",
        "records_observed",
        "result_digest",
        "source_id",
        "stable_ref",
        "task_id",
        "tool_name",
        "tool_shape_fingerprint",
    }
)


def new_onboarding_session(
    *,
    selected_sources: tuple[str, ...] = (),
    source_selection_confirmed: bool = False,
    observed_at: datetime | None = None,
    onboarding_id: str | None = None,
) -> OnboardingSession:
    now = format_time(observed_at or datetime.now(UTC))
    requested_sources = _source_ids(selected_sources)
    selected = tuple(sorted({"gsv", *requested_sources}))
    session = OnboardingSession(
        format_version=ONBOARDING_FORMAT_VERSION,
        onboarding_id=_uuid(onboarding_id or str(uuid.uuid4()), "onboarding ID"),
        phase=OnboardingPhase.CODEX_SUBSTRATE,
        completion=CompletionState.IN_PROGRESS,
        selected_sources=selected,
        source_selection_confirmed=source_selection_confirmed,
        context=tuple(
            ContextCheckpoint(section=section, state=ContextState.UNASKED)
            for section in ContextSection
        ),
        next_actor=NextActor.AGENT,
        next_action_code="verify-codex-substrate",
        lease=None,
        created_at=now,
        updated_at=now,
        revision="",
    )
    return parse_onboarding_session(render_onboarding_session(session))


def new_host_readiness_receipt(
    *,
    vault_id: str,
    host_id: str,
    app_version: str,
    core_version: str,
    plugin_version: str,
    capability_fingerprints: tuple[CapabilityFingerprint, ...] = (),
    permission_evidence: tuple[PermissionEvidence, ...] = (),
    scheduled_wake_evidence: ScheduledWakeEvidence | None = None,
    sources: tuple[SourceReadiness, ...] = (),
    observed_at: datetime | None = None,
) -> HostReadinessReceipt:
    now = format_time(observed_at or datetime.now(UTC))
    wake = scheduled_wake_evidence or ScheduledWakeEvidence(
        state=ScheduledWakeState.NOT_CONFIGURED,
        automation_ref=None,
        task_id=None,
        scheduled_for=None,
        observed_at=now,
        result_digest=None,
    )
    receipt = HostReadinessReceipt(
        format_version=HOST_READINESS_FORMAT_VERSION,
        vault_id=_uuid(vault_id, "vault ID"),
        host_id=_uuid(host_id, "host ID"),
        app_version=_version(app_version, "app version"),
        core_version=_version(core_version, "core version"),
        plugin_version=_version(plugin_version, "plugin version"),
        capability_fingerprints=tuple(
            sorted(
                (_validated_capability_fingerprint(item) for item in capability_fingerprints),
                key=lambda item: item.capability_id,
            )
        ),
        permission_evidence=tuple(
            sorted(
                (_validated_permission_evidence(item) for item in permission_evidence),
                key=lambda item: item.permission_id,
            )
        ),
        scheduled_wake_evidence=_validated_scheduled_wake(wake),
        codex_state=HostCapabilityState.UNKNOWN,
        bridge_state=HostCapabilityState.UNKNOWN,
        pulse_state=HostCapabilityState.UNKNOWN,
        computer_use_state=HostCapabilityState.UNKNOWN,
        context_synthesis_proved=False,
        initial_orientation_proved=False,
        fresh_task_context_proved=False,
        sources=tuple(
            sorted((_validated_source(item) for item in sources), key=lambda x: x.source_id)
        ),
        created_at=now,
        updated_at=now,
        revision="",
    )
    return parse_host_readiness_receipt(render_host_readiness_receipt(receipt))


def render_onboarding_session(session: OnboardingSession) -> str:
    _validate_session(session)
    metadata = {
        "completion": session.completion.value,
        "context": [
            {"section": item.section.value, "state": item.state.value} for item in session.context
        ],
        "created_at": session.created_at,
        "format_version": session.format_version,
        "kind": "onboarding-session",
        "lease": _lease_mapping(session.lease),
        "next_action_code": session.next_action_code,
        "next_actor": session.next_actor.value if session.next_actor else None,
        "onboarding_id": session.onboarding_id,
        "phase": session.phase.value,
        "selected_sources": list(session.selected_sources),
        "source_selection_confirmed": session.source_selection_confirmed,
        "updated_at": session.updated_at,
    }
    encoded_meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    selected = [f"- `{item}`" for item in session.selected_sources] or ["- None selected."]
    context = [f"- `{item.section.value}`: {item.state.value}" for item in session.context]
    lease = (
        [
            f"- Owner: `{session.lease.owner_ref}`",
            f"- Lease: `{session.lease.lease_id}`",
            f"- Expires: `{session.lease.expires_at}`",
            f"- Generation: {session.lease.generation}",
        ]
        if session.lease
        else ["- No active ChatGPT task."]
    )
    next_action = (
        f"{session.next_actor.value}:`{session.next_action_code}`"
        if session.next_actor and session.next_action_code
        else "None."
    )
    markdown = "\n".join(
        [
            f"<!-- gsv-onboarding:{encoded_meta} -->",
            "",
            "# Seld onboarding",
            "",
            "## Selected sources",
            *selected,
            "",
            "## Context coverage",
            *context,
            "",
            "## Active hand",
            *lease,
            "",
            "## Next action",
            next_action,
            "",
        ]
    )
    if len(markdown.encode("utf-8")) > MAX_SESSION_BYTES:
        raise ValidationError("onboarding session exceeds its size bound")
    return markdown


def parse_onboarding_session(markdown: str) -> OnboardingSession:
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_SESSION_BYTES:
        raise ValidationError("onboarding session exceeds its size bound")
    first = markdown.splitlines()[0] if markdown.splitlines() else ""
    match = _SESSION_META.fullmatch(first)
    if not match:
        raise ValidationError("onboarding session metadata header is missing")
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError("onboarding session metadata is invalid JSON") from exc
    value = _mapping(raw, "onboarding session")
    _exact_keys(value, _SESSION_KEYS, "onboarding session")
    if value.get("kind") != "onboarding-session":
        raise ValidationError("expected an onboarding-session record")
    context_raw = _list(value, "context")
    context: list[ContextCheckpoint] = []
    for item in context_raw:
        entry = _mapping(item, "context checkpoint")
        _exact_keys(entry, frozenset({"section", "state"}), "context checkpoint")
        context.append(
            ContextCheckpoint(
                section=_enum(ContextSection, entry.get("section"), "context section"),
                state=_enum(ContextState, entry.get("state"), "context state"),
            )
        )
    session = OnboardingSession(
        format_version=_integer(value, "format_version"),
        onboarding_id=_required_string(value, "onboarding_id"),
        phase=_enum(OnboardingPhase, value.get("phase"), "onboarding phase"),
        completion=_enum(CompletionState, value.get("completion"), "completion state"),
        selected_sources=tuple(_string_list(value, "selected_sources")),
        source_selection_confirmed=_boolean(value, "source_selection_confirmed"),
        context=tuple(context),
        next_actor=_optional_enum(NextActor, value.get("next_actor"), "next actor"),
        next_action_code=_optional_string(value, "next_action_code"),
        lease=_lease_from_value(value.get("lease")),
        created_at=_required_string(value, "created_at"),
        updated_at=_required_string(value, "updated_at"),
        revision=sha256_bytes(encoded),
    )
    _validate_session(session)
    if render_onboarding_session(replace(session, revision="")) != markdown:
        raise ValidationError("onboarding session contains non-canonical or free-form content")
    return session


_SESSION_KEYS: Final = frozenset(
    {
        "completion",
        "context",
        "created_at",
        "format_version",
        "kind",
        "lease",
        "next_action_code",
        "next_actor",
        "onboarding_id",
        "phase",
        "selected_sources",
        "source_selection_confirmed",
        "updated_at",
    }
)


def render_host_readiness_receipt(receipt: HostReadinessReceipt) -> bytes:
    _validate_host_receipt(receipt)
    payload = {
        "app_version": receipt.app_version,
        "bridge_state": receipt.bridge_state.value,
        "capability_fingerprints": [
            _capability_fingerprint_mapping(item) for item in receipt.capability_fingerprints
        ],
        "codex_state": receipt.codex_state.value,
        "computer_use_state": receipt.computer_use_state.value,
        "context_synthesis_proved": receipt.context_synthesis_proved,
        "core_version": receipt.core_version,
        "created_at": receipt.created_at,
        "format_version": receipt.format_version,
        "fresh_task_context_proved": receipt.fresh_task_context_proved,
        "host_id": receipt.host_id,
        "initial_orientation_proved": receipt.initial_orientation_proved,
        "permission_evidence": [
            _permission_evidence_mapping(item) for item in receipt.permission_evidence
        ],
        "plugin_version": receipt.plugin_version,
        "pulse_state": receipt.pulse_state.value,
        "scheduled_wake_evidence": _scheduled_wake_mapping(receipt.scheduled_wake_evidence),
        "sources": [_source_mapping(item) for item in receipt.sources],
        "updated_at": receipt.updated_at,
        "vault_id": receipt.vault_id,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(encoded) > MAX_HOST_RECEIPT_BYTES:
        raise ValidationError("host readiness receipt exceeds its size bound")
    return encoded


def parse_host_readiness_receipt(encoded: bytes) -> HostReadinessReceipt:
    if len(encoded) > MAX_HOST_RECEIPT_BYTES:
        raise ValidationError("host readiness receipt exceeds its size bound")
    try:
        raw = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("host readiness receipt is invalid JSON") from exc
    value = _mapping(raw, "host readiness receipt")
    _exact_keys(value, _HOST_RECEIPT_KEYS, "host readiness receipt")
    sources = tuple(_source_from_value(item) for item in _list(value, "sources"))
    capabilities = tuple(
        _capability_fingerprint_from_value(item) for item in _list(value, "capability_fingerprints")
    )
    permissions = tuple(
        _permission_evidence_from_value(item) for item in _list(value, "permission_evidence")
    )
    receipt = HostReadinessReceipt(
        format_version=_integer(value, "format_version"),
        vault_id=_required_string(value, "vault_id"),
        host_id=_required_string(value, "host_id"),
        app_version=_required_string(value, "app_version"),
        core_version=_required_string(value, "core_version"),
        plugin_version=_required_string(value, "plugin_version"),
        capability_fingerprints=capabilities,
        permission_evidence=permissions,
        scheduled_wake_evidence=_scheduled_wake_from_value(value.get("scheduled_wake_evidence")),
        codex_state=_enum(HostCapabilityState, value.get("codex_state"), "Codex state"),
        bridge_state=_enum(HostCapabilityState, value.get("bridge_state"), "Bridge state"),
        pulse_state=_enum(HostCapabilityState, value.get("pulse_state"), "Pulse state"),
        computer_use_state=_enum(
            HostCapabilityState, value.get("computer_use_state"), "Computer Use state"
        ),
        context_synthesis_proved=_boolean(value, "context_synthesis_proved"),
        initial_orientation_proved=_boolean(value, "initial_orientation_proved"),
        fresh_task_context_proved=_boolean(value, "fresh_task_context_proved"),
        sources=sources,
        created_at=_required_string(value, "created_at"),
        updated_at=_required_string(value, "updated_at"),
        revision=sha256_bytes(encoded),
    )
    _validate_host_receipt(receipt)
    if render_host_readiness_receipt(replace(receipt, revision="")) != encoded:
        raise ValidationError("host readiness receipt is not canonical JSON")
    return receipt


_HOST_RECEIPT_KEYS: Final = frozenset(
    {
        "app_version",
        "bridge_state",
        "capability_fingerprints",
        "codex_state",
        "computer_use_state",
        "context_synthesis_proved",
        "core_version",
        "created_at",
        "format_version",
        "fresh_task_context_proved",
        "host_id",
        "initial_orientation_proved",
        "permission_evidence",
        "plugin_version",
        "pulse_state",
        "scheduled_wake_evidence",
        "sources",
        "updated_at",
        "vault_id",
    }
)


def validate_tool_attestation(
    attestation: ToolAttestation,
    *,
    expected_task_id: str,
    prior: SourceReadiness,
    known_tool_shapes: Mapping[str, Mapping[str, str]],
    trusted_receipts: Mapping[str, TrustedToolReceipt],
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_ATTESTATION_MAX_AGE,
) -> AttestationDecision:
    """Validate one proof without allowing model narrative to create readiness.

    A tool-result attestation is accepted only when every bounded field matches
    a host-trusted receipt for a currently known tool shape. It proves one
    bounded read, not the separate identity-confirmation and Pulse-canary bundle
    required for ``ready``. A tool-absence
    observation can record ``tool_absent`` for an unverified source, but cannot
    erase a previously proven ready state.
    """

    _validated_source(prior)
    _validate_tool_attestation_shape(attestation)
    current_task_id = _uuid(expected_task_id, "expected task ID")
    if attestation.task_id != current_task_id:
        raise ValidationError("tool attestation belongs to a different Codex task")
    if attestation.source_id != prior.source_id:
        raise ValidationError("tool attestation is for a different source")
    observed_now = now or datetime.now(UTC)
    _validate_fresh_time(attestation.observed_at, now=observed_now, max_age=max_age)

    if attestation.event is AttestationEvent.TOOL_ABSENT:
        if prior.state is SourceState.READY:
            return AttestationDecision(
                applied=False,
                reason=AttestationDecisionReason.PRIOR_READY_PRESERVED,
                readiness=prior,
            )
        unavailable = SourceReadiness(
            source_id=prior.source_id,
            state=SourceState.TOOL_ABSENT,
            expected_account_fingerprint=prior.expected_account_fingerprint,
            last_observed_at=attestation.observed_at,
            last_error_code="tool-absent",
        )
        return AttestationDecision(
            applied=True,
            reason=AttestationDecisionReason.APPLIED,
            readiness=_validated_source(unavailable),
        )

    expected_account = prior.expected_account_fingerprint
    if expected_account is None:
        raise ValidationError("source has no expected account fingerprint")
    if attestation.account_fingerprint != expected_account:
        raise ValidationError("tool attestation account does not match the selected account")

    source_shapes = known_tool_shapes.get(attestation.source_id)
    if source_shapes is None:
        raise ValidationError("source has no known tool shapes")
    assert attestation.tool_name is not None
    expected_shape = source_shapes.get(attestation.tool_name)
    if expected_shape is None:
        raise ValidationError("tool is absent from the known source recipe")
    if attestation.tool_shape_fingerprint != expected_shape:
        raise ValidationError("tool shape is unknown or has drifted")

    if prior.state is SourceState.READY and (
        prior.tool_name != attestation.tool_name
        or prior.tool_shape_fingerprint != attestation.tool_shape_fingerprint
    ):
        raise ValidationError("ready source tool fingerprint drift requires explicit revalidation")

    assert attestation.tool_call_id is not None
    trusted = trusted_receipts.get(attestation.tool_call_id)
    if trusted is None:
        raise ValidationError("tool attestation has no trusted host receipt and may be fabricated")
    _validate_trusted_receipt(trusted)
    expected = (
        trusted.task_id,
        trusted.source_id,
        trusted.tool_name,
        trusted.tool_shape_fingerprint,
        trusted.account_fingerprint,
        trusted.stable_ref,
        trusted.result_digest,
        trusted.read_complete,
        trusted.records_observed,
        trusted.observed_at,
    )
    submitted = (
        attestation.task_id,
        attestation.source_id,
        attestation.tool_name,
        attestation.tool_shape_fingerprint,
        attestation.account_fingerprint,
        attestation.stable_ref,
        attestation.result_digest,
        attestation.read_complete,
        attestation.records_observed,
        attestation.observed_at,
    )
    if submitted != expected or trusted.call_id != attestation.tool_call_id:
        raise ValidationError("tool attestation does not match its trusted host receipt")
    _validate_fresh_time(trusted.observed_at, now=observed_now, max_age=max_age)
    if trusted.read_complete is not True:
        raise ValidationError("bounded source read is incomplete")
    if trusted.records_observed > get_recipe(attestation.source_id).read_limit:
        raise ValidationError("bounded source read exceeds its recipe limit")

    if prior.state is SourceState.READY:
        refreshed_ready = replace(
            prior,
            observed_account_fingerprint=attestation.account_fingerprint,
            stable_ref=attestation.stable_ref,
            result_digest=attestation.result_digest,
            last_observed_at=attestation.observed_at,
            last_error_code=None,
        )
        return AttestationDecision(
            applied=True,
            reason=AttestationDecisionReason.PRIOR_READY_PRESERVED,
            readiness=_validated_source(refreshed_ready),
        )

    read_verified = SourceReadiness(
        source_id=prior.source_id,
        state=SourceState.READ_UNVERIFIED,
        expected_account_fingerprint=expected_account,
        observed_account_fingerprint=attestation.account_fingerprint,
        tool_name=attestation.tool_name,
        tool_shape_fingerprint=attestation.tool_shape_fingerprint,
        stable_ref=attestation.stable_ref,
        result_digest=attestation.result_digest,
        last_verified_at=None,
        last_observed_at=attestation.observed_at,
        last_error_code=None,
    )
    return AttestationDecision(
        applied=True,
        reason=AttestationDecisionReason.APPLIED,
        readiness=_validated_source(read_verified),
    )


def derive_onboarding_state(
    session: OnboardingSession,
    receipt: HostReadinessReceipt,
    *,
    expected_vault_id: str,
    expected_host_id: str,
    now: datetime | None = None,
) -> tuple[OnboardingPhase, CompletionState]:
    """Derive the next phase and completion state from persisted facts."""

    _validate_session(session)
    _validate_host_receipt(receipt)
    bound_vault_id = _uuid(expected_vault_id, "expected vault ID")
    if receipt.vault_id != bound_vault_id:
        raise ValidationError("host readiness receipt belongs to a different vault")
    bound_host_id = _uuid(expected_host_id, "expected host ID")
    if receipt.host_id != bound_host_id:
        raise ValidationError("host readiness receipt belongs to a different host")
    observed_now = now or datetime.now(UTC)
    core = (receipt.codex_state, receipt.bridge_state)
    if any(item in {HostCapabilityState.BLOCKED, HostCapabilityState.UNSUPPORTED} for item in core):
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.BLOCKED
    if any(item is HostCapabilityState.WAITING_USER for item in core):
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.WAITING_USER
    if any(item is HostCapabilityState.STALE for item in core):
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.NEEDS_REVALIDATION
    if any(item is not HostCapabilityState.READY for item in core):
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.IN_PROGRESS
    host_times = (
        receipt.updated_at,
        *(item.observed_at for item in receipt.capability_fingerprints),
        *(item.observed_at for item in receipt.permission_evidence),
    )
    if any(
        _proof_time_is_stale(value, now=observed_now, ttl=HOST_PROOF_MAX_AGE)
        for value in host_times
    ):
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.NEEDS_REVALIDATION
    permission_states = {item.state for item in receipt.permission_evidence}
    if permission_states & {
        PermissionState.DENIED,
        PermissionState.BLOCKED_BY_POLICY,
        PermissionState.UNSUPPORTED,
    }:
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.BLOCKED
    if PermissionState.WAITING_USER in permission_states:
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.WAITING_USER
    if PermissionState.UNKNOWN in permission_states:
        return OnboardingPhase.CODEX_SUBSTRATE, CompletionState.IN_PROGRESS

    context_states = {item.state for item in session.context}
    if ContextState.DRAFTED in context_states:
        return OnboardingPhase.PRIVACY_AND_CONTEXT_CAPTURE, CompletionState.WAITING_USER
    if ContextState.UNASKED in context_states:
        return OnboardingPhase.PRIVACY_AND_CONTEXT_CAPTURE, CompletionState.IN_PROGRESS
    if not session.source_selection_confirmed:
        return OnboardingPhase.SOURCE_SELECTION, CompletionState.IN_PROGRESS

    by_source = {item.source_id: item for item in receipt.sources}
    selected = [by_source.get(source_id) for source_id in session.selected_sources]
    if any(item is None for item in selected):
        return OnboardingPhase.ENABLEMENT_WAIT, CompletionState.IN_PROGRESS
    selected_values = [item for item in selected if item is not None]
    source_zero = by_source["gsv"]
    if source_zero.state in {SourceState.UNAVAILABLE, SourceState.DECLINED}:
        return OnboardingPhase.ENABLEMENT_WAIT, CompletionState.BLOCKED
    if any(
        item.state is SourceState.READY
        and item.last_verified_at is not None
        and _proof_time_is_stale(
            item.last_verified_at,
            now=observed_now,
            ttl=get_recipe(item.source_id).proof_ttl,
        )
        for item in selected_values
    ):
        return OnboardingPhase.FRESH_TASK_VERIFICATION, CompletionState.NEEDS_REVALIDATION
    waiting_states = {
        SourceState.PLUGIN_MISSING,
        SourceState.AUTH_REQUIRED,
        SourceState.IDENTITY_PENDING,
        SourceState.IDENTITY_MISMATCH,
    }
    if any(item.state in waiting_states for item in selected_values):
        return OnboardingPhase.ENABLEMENT_WAIT, CompletionState.WAITING_USER
    blocked_states = {
        SourceState.TOOL_ABSENT,
        SourceState.BLOCKED_BY_POLICY,
        SourceState.UNSUPPORTED_TOOL_SHAPE,
    }
    if any(item.state in blocked_states for item in selected_values):
        return OnboardingPhase.ENABLEMENT_WAIT, CompletionState.BLOCKED
    if any(item.state is SourceState.NOT_SELECTED for item in selected_values):
        return OnboardingPhase.SOURCE_SELECTION, CompletionState.IN_PROGRESS
    if any(item.state is SourceState.FRESH_TASK_REQUIRED for item in selected_values):
        return OnboardingPhase.FRESH_TASK_VERIFICATION, CompletionState.FRESH_TASK_REQUIRED
    if any(item.state is SourceState.STALE for item in selected_values):
        return OnboardingPhase.FRESH_TASK_VERIFICATION, CompletionState.NEEDS_REVALIDATION
    if any(
        item.state in {SourceState.READ_UNVERIFIED, SourceState.CANARY_FAILED}
        for item in selected_values
    ):
        return OnboardingPhase.FRESH_TASK_VERIFICATION, CompletionState.IN_PROGRESS
    if not receipt.fresh_task_context_proved:
        return OnboardingPhase.FRESH_TASK_VERIFICATION, CompletionState.FRESH_TASK_REQUIRED

    if not receipt.context_synthesis_proved:
        return OnboardingPhase.CONTEXT_SYNTHESIS, CompletionState.IN_PROGRESS
    if not receipt.initial_orientation_proved:
        return OnboardingPhase.INITIAL_ORIENTATION, CompletionState.IN_PROGRESS
    if receipt.pulse_state in {HostCapabilityState.BLOCKED, HostCapabilityState.UNSUPPORTED}:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.BLOCKED
    if receipt.pulse_state is HostCapabilityState.WAITING_USER:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.WAITING_USER
    if receipt.pulse_state is HostCapabilityState.STALE:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.NEEDS_REVALIDATION
    if receipt.pulse_state is not HostCapabilityState.READY:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.IN_PROGRESS

    wake_state = receipt.scheduled_wake_evidence.state
    if wake_state is ScheduledWakeState.FAILED:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.BLOCKED
    if wake_state is ScheduledWakeState.STALE:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.NEEDS_REVALIDATION
    if wake_state is ScheduledWakeState.NOT_CONFIGURED:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.WAITING_USER
    if wake_state is ScheduledWakeState.SCHEDULED:
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.IN_PROGRESS
    if _proof_time_is_stale(
        receipt.scheduled_wake_evidence.observed_at,
        now=observed_now,
        ttl=HOST_PROOF_MAX_AGE,
    ):
        return OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, CompletionState.NEEDS_REVALIDATION

    gaps = any(
        item.state in {SourceState.UNAVAILABLE, SourceState.DECLINED} for item in selected_values
    )
    completion = CompletionState.OPERATIONAL_WITH_GAPS if gaps else CompletionState.FULLY_CONNECTED
    return OnboardingPhase.DONE, completion


def _proof_time_is_stale(value: str, *, now: datetime, ttl: timedelta) -> bool:
    observed = parse_time(value)
    return observed > now + MAX_FUTURE_SKEW or now - observed > ttl


class OnboardingStore:
    """CAS-safe portable Markdown persistence under ``onboarding/session.md``."""

    def __init__(self, vault_root: Path | str):
        self.root = Path(vault_root).expanduser().resolve()
        self.path = self.root / "onboarding/session.md"
        self.lock_path = self.root / ".gsv/locks/onboarding-session.lock"

    def create(self, session: OnboardingSession) -> OnboardingSession:
        _validate_session(session)
        rendered = render_onboarding_session(replace(session, revision=""))
        rendered_revision = sha256_bytes(rendered.encode("utf-8"))
        if session.revision and session.revision != rendered_revision:
            raise ValidationError("new onboarding session revision does not match its content")
        encoded = rendered.encode("utf-8")
        with self._locked_store(create_directories=True) as store:
            if self._read_with_store(store, missing_ok=True) is not None:
                raise ConflictError("onboarding session already exists")
            store.compare_and_swap_regular_file(
                "onboarding/session.md",
                expected=None,
                replacement=encoded,
                label="onboarding session",
                max_bytes=MAX_SESSION_BYTES,
            )
            return parse_onboarding_session(rendered)

    def load(self) -> OnboardingSession:
        with self._locked_store() as store:
            encoded = self._read_with_store(store, missing_ok=False)
            assert encoded is not None
            return self._parse_session_bytes(encoded)

    def save(
        self,
        session: OnboardingSession,
        *,
        expected_revision: str,
        owner_ref: str | None = None,
        lease_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> OnboardingSession:
        now = observed_at or datetime.now(UTC)
        clean_owner, clean_lease_id = _optional_lease_credentials(owner_ref, lease_id)

        def change(before: OnboardingSession) -> OnboardingSession:
            self._authorize_session_write(
                before,
                owner_ref=clean_owner,
                lease_id=clean_lease_id,
                observed_at=now,
            )
            if session.lease != before.lease:
                raise ValidationError("generic onboarding save cannot change the active lease")
            if session.phase != before.phase or session.completion != before.completion:
                raise ValidationError(
                    "onboarding phase and completion must be changed by deterministic reconcile"
                )
            return session

        return self._mutate(
            expected_revision=expected_revision,
            observed_at=now,
            change=change,
        )

    def reconcile(
        self,
        receipt: HostReadinessReceipt,
        *,
        expected_vault_id: str,
        expected_host_id: str,
        expected_revision: str,
        owner_ref: str,
        lease_id: str,
        observed_at: datetime | None = None,
    ) -> OnboardingSession:
        """Persist only the phase/completion deterministically derived from bound proof."""

        now = observed_at or datetime.now(UTC)
        clean_owner = _owner_ref(owner_ref)
        clean_lease_id = _uuid(lease_id, "lease ID")

        def change(before: OnboardingSession) -> OnboardingSession:
            self._authorize_session_write(
                before,
                owner_ref=clean_owner,
                lease_id=clean_lease_id,
                observed_at=now,
            )
            phase, completion = derive_onboarding_state(
                before,
                receipt,
                expected_vault_id=expected_vault_id,
                expected_host_id=expected_host_id,
                now=now,
            )
            return replace(before, phase=phase, completion=completion)

        return self._mutate(
            expected_revision=expected_revision,
            observed_at=now,
            change=change,
        )

    def claim_lease(
        self,
        *,
        owner_ref: str,
        expected_revision: str,
        observed_at: datetime | None = None,
        ttl: timedelta = DEFAULT_LEASE_TTL,
    ) -> OnboardingSession:
        now = observed_at or datetime.now(UTC)
        clean_owner = _owner_ref(owner_ref)
        _lease_ttl(ttl)

        def change(before: OnboardingSession) -> OnboardingSession:
            previous = before.lease
            if previous is not None and parse_time(previous.expires_at) > now.astimezone(UTC):
                if previous.owner_ref == clean_owner:
                    raise ConflictError("onboarding lease is already held; renew the exact lease")
                raise ConflictError("onboarding session has an active Codex hand")
            generation = previous.generation + 1 if previous else 1
            lease = SessionLease(
                owner_ref=clean_owner,
                lease_id=str(uuid.uuid4()),
                acquired_at=format_time(now),
                expires_at=format_time(now + ttl),
                generation=generation,
                predecessor_lease_id=previous.lease_id if previous else None,
            )
            return replace(before, lease=lease)

        return self._mutate(expected_revision=expected_revision, observed_at=now, change=change)

    def renew_lease(
        self,
        *,
        owner_ref: str,
        lease_id: str,
        expected_revision: str,
        observed_at: datetime | None = None,
        ttl: timedelta = DEFAULT_LEASE_TTL,
    ) -> OnboardingSession:
        now = observed_at or datetime.now(UTC)
        clean_owner = _owner_ref(owner_ref)
        clean_lease_id = _uuid(lease_id, "lease ID")
        _lease_ttl(ttl)

        def change(before: OnboardingSession) -> OnboardingSession:
            lease = before.lease
            if lease is None or lease.owner_ref != clean_owner or lease.lease_id != clean_lease_id:
                raise ConflictError("onboarding lease owner or ID changed")
            if parse_time(lease.expires_at) <= now.astimezone(UTC):
                raise ConflictError("onboarding lease expired; claim or take over the session")
            return replace(before, lease=replace(lease, expires_at=format_time(now + ttl)))

        return self._mutate(expected_revision=expected_revision, observed_at=now, change=change)

    def release_lease(
        self,
        *,
        owner_ref: str,
        lease_id: str,
        expected_revision: str,
        observed_at: datetime | None = None,
    ) -> OnboardingSession:
        clean_owner = _owner_ref(owner_ref)
        clean_lease_id = _uuid(lease_id, "lease ID")

        def change(before: OnboardingSession) -> OnboardingSession:
            lease = before.lease
            if lease is None or lease.owner_ref != clean_owner or lease.lease_id != clean_lease_id:
                raise ConflictError("onboarding lease owner or ID changed")
            return replace(before, lease=None)

        return self._mutate(
            expected_revision=expected_revision, observed_at=observed_at, change=change
        )

    def takeover_lease(
        self,
        *,
        owner_ref: str,
        expected_lease_id: str,
        expected_revision: str,
        observed_at: datetime | None = None,
        ttl: timedelta = DEFAULT_LEASE_TTL,
    ) -> OnboardingSession:
        """Explicitly transfer even an active lease after caller-side authorization."""

        now = observed_at or datetime.now(UTC)
        clean_owner = _owner_ref(owner_ref)
        prior_id = _uuid(expected_lease_id, "expected lease ID")
        _lease_ttl(ttl)

        def change(before: OnboardingSession) -> OnboardingSession:
            prior = before.lease
            if prior is None or prior.lease_id != prior_id:
                raise ConflictError("onboarding lease changed before takeover")
            lease = SessionLease(
                owner_ref=clean_owner,
                lease_id=str(uuid.uuid4()),
                acquired_at=format_time(now),
                expires_at=format_time(now + ttl),
                generation=prior.generation + 1,
                predecessor_lease_id=prior.lease_id,
            )
            return replace(before, lease=lease)

        return self._mutate(expected_revision=expected_revision, observed_at=now, change=change)

    @contextmanager
    def _locked_store(self, *, create_directories: bool = False) -> Iterator[PinnedPathRoot]:
        if create_directories:
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            store = PinnedPathRoot(self.root)
        except ValidationError as exc:
            raise ValidationError(f"portable onboarding storage is unavailable: {exc}") from exc
        try:
            if create_directories:
                store.ensure_directory(".gsv")
                store.ensure_directory(".gsv/locks")
                store.ensure_directory("onboarding")
            with (
                store.exclusive_file_lock(".gsv/locks/onboarding-session.lock"),
                store.exclusive_root_lock(),
                store.bind_directory("onboarding"),
            ):
                yield store
        finally:
            store.close()

    @staticmethod
    def _read_with_store(
        store: PinnedPathRoot,
        *,
        missing_ok: bool,
    ) -> bytes | None:
        encoded = store.read_regular_file(
            "onboarding/session.md",
            label="onboarding session",
            max_bytes=MAX_SESSION_BYTES,
            missing_ok=missing_ok,
        )
        if encoded is None and not missing_ok:
            raise NotFoundError("onboarding session does not exist")
        return encoded

    @staticmethod
    def _parse_session_bytes(encoded: bytes) -> OnboardingSession:
        try:
            markdown = encoded.decode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("onboarding session must be UTF-8 Markdown") from exc
        return parse_onboarding_session(markdown)

    @staticmethod
    def _authorize_session_write(
        session: OnboardingSession,
        *,
        owner_ref: str | None,
        lease_id: str | None,
        observed_at: datetime,
    ) -> None:
        lease = session.lease
        if lease is None:
            if owner_ref is not None or lease_id is not None:
                raise ConflictError("onboarding session has no active Codex hand")
            return
        observed = observed_at.astimezone(UTC)
        if observed < parse_time(lease.acquired_at):
            raise ConflictError("onboarding write precedes the active lease acquisition")
        if parse_time(lease.expires_at) <= observed:
            raise ConflictError("onboarding lease expired; claim or take over the session")
        if owner_ref != lease.owner_ref or lease_id != lease.lease_id:
            raise ConflictError("onboarding lease owner or ID changed")

    def _mutate(
        self,
        *,
        expected_revision: str,
        observed_at: datetime | None,
        change: Callable[[OnboardingSession], OnboardingSession],
    ) -> OnboardingSession:
        _expected_revision(expected_revision)
        with self._locked_store() as store:
            before_encoded = self._read_with_store(store, missing_ok=False)
            assert before_encoded is not None
            before = self._parse_session_bytes(before_encoded)
            if before.revision != expected_revision:
                raise ConflictError("onboarding session changed; reload before retrying")
            candidate = change(before)
            if (
                candidate.onboarding_id != before.onboarding_id
                or candidate.created_at != before.created_at
            ):
                raise ValidationError("onboarding identity and creation time are immutable")
            candidate = replace(
                candidate,
                updated_at=next_timestamp(before.updated_at, observed_at),
                revision="",
            )
            rendered = render_onboarding_session(candidate)
            store.compare_and_swap_regular_file(
                "onboarding/session.md",
                expected=before_encoded,
                replacement=rendered.encode("utf-8"),
                label="onboarding session",
                max_bytes=MAX_SESSION_BYTES,
            )
            return parse_onboarding_session(rendered)


class HostReadinessStore:
    """CAS-safe machine-local JSON persistence, separate from the portable vault."""

    def __init__(self, data_root: Path | str, *, vault_id: str):
        self.root = Path(data_root).expanduser().resolve()
        self.vault_id = _uuid(vault_id, "vault ID")
        self.path = self.root / "onboarding" / self.vault_id / "host-readiness.json"
        self.lock_path = self.root / "locks" / f"onboarding-{self.vault_id}.lock"

    def create(self, receipt: HostReadinessReceipt) -> HostReadinessReceipt:
        _validate_host_receipt(receipt)
        if receipt.vault_id != self.vault_id:
            raise ValidationError("host readiness receipt belongs to a different vault")
        encoded = render_host_readiness_receipt(replace(receipt, revision=""))
        rendered_revision = sha256_bytes(encoded)
        if receipt.revision and receipt.revision != rendered_revision:
            raise ValidationError("new host readiness revision does not match its content")
        with exclusive_lock(self.lock_path):
            if os.path.lexists(self.path):
                raise ConflictError("host readiness receipt already exists")
            atomic_write(self.path, encoded)
            return parse_host_readiness_receipt(encoded)

    def load(self) -> HostReadinessReceipt:
        with exclusive_lock(self.lock_path):
            return self._load_unlocked()

    def save(
        self,
        receipt: HostReadinessReceipt,
        *,
        expected_revision: str,
        observed_at: datetime | None = None,
    ) -> HostReadinessReceipt:
        _expected_revision(expected_revision)
        with exclusive_lock(self.lock_path):
            before = self._load_unlocked()
            if before.revision != expected_revision:
                raise ConflictError("host readiness receipt changed; reload before retrying")
            if (
                receipt.vault_id != before.vault_id
                or receipt.host_id != before.host_id
                or receipt.created_at != before.created_at
            ):
                raise ValidationError("host readiness identity and creation time are immutable")
            candidate = replace(
                receipt,
                updated_at=next_timestamp(before.updated_at, observed_at),
                revision="",
            )
            encoded = render_host_readiness_receipt(candidate)
            atomic_write(self.path, encoded)
            return parse_host_readiness_receipt(encoded)

    def _load_unlocked(self) -> HostReadinessReceipt:
        if not os.path.lexists(self.path):
            raise NotFoundError("host readiness receipt does not exist")
        encoded = read_regular_file(
            self.path,
            label="host readiness receipt",
            max_bytes=MAX_HOST_RECEIPT_BYTES,
        )
        receipt = parse_host_readiness_receipt(encoded)
        if receipt.vault_id != self.vault_id:
            raise ValidationError("host readiness receipt belongs to a different vault")
        return receipt


def _validate_session(session: OnboardingSession) -> None:
    if session.format_version != ONBOARDING_FORMAT_VERSION:
        raise ValidationError("unsupported onboarding session version")
    _uuid(session.onboarding_id, "onboarding ID")
    stored_time(session.created_at, "created_at")
    stored_time(session.updated_at, "updated_at")
    if parse_time(session.updated_at) < parse_time(session.created_at):
        raise ValidationError("onboarding updated_at cannot precede created_at")
    if session.revision:
        _expected_revision(session.revision)
    selected = _source_ids(session.selected_sources)
    if tuple(sorted(selected)) != session.selected_sources:
        raise ValidationError("selected sources must be unique and sorted")
    if "gsv" not in selected:
        raise ValidationError("onboarding session must include GSV source zero")
    for source_id in selected:
        get_recipe(source_id)
    if not isinstance(session.source_selection_confirmed, bool):
        raise ValidationError("source selection confirmation must be a boolean")
    expected_sections = tuple(ContextSection)
    if tuple(item.section for item in session.context) != expected_sections:
        raise ValidationError("context checkpoints must contain every section in canonical order")
    if session.next_actor is None or session.next_action_code is None:
        if session.next_actor is not None or session.next_action_code is not None:
            raise ValidationError("next actor and next action code must be set or cleared together")
    else:
        _safe_token(session.next_action_code, "next action code")
    if session.phase is OnboardingPhase.DONE and session.completion not in {
        CompletionState.OPERATIONAL_WITH_GAPS,
        CompletionState.FULLY_CONNECTED,
    }:
        raise ValidationError("done onboarding requires an operational completion state")
    if (
        session.completion
        in {
            CompletionState.OPERATIONAL_WITH_GAPS,
            CompletionState.FULLY_CONNECTED,
        }
        and session.phase is not OnboardingPhase.DONE
    ):
        raise ValidationError("operational completion requires the done phase")
    if session.lease:
        _validate_lease(session.lease)


def _validate_lease(lease: SessionLease) -> None:
    _owner_ref(lease.owner_ref)
    _uuid(lease.lease_id, "lease ID")
    acquired = parse_time(stored_time(lease.acquired_at, "lease acquired_at"))
    expires = parse_time(stored_time(lease.expires_at, "lease expires_at"))
    if expires <= acquired:
        raise ValidationError("lease expiry must follow acquisition")
    if isinstance(lease.generation, bool) or lease.generation < 1:
        raise ValidationError("lease generation must be a positive integer")
    if lease.predecessor_lease_id is not None:
        _uuid(lease.predecessor_lease_id, "predecessor lease ID")
        if lease.predecessor_lease_id == lease.lease_id:
            raise ValidationError("lease cannot name itself as predecessor")


def _optional_lease_credentials(
    owner_ref: str | None,
    lease_id: str | None,
) -> tuple[str | None, str | None]:
    if (owner_ref is None) != (lease_id is None):
        raise ValidationError("onboarding lease owner and ID must be supplied together")
    if owner_ref is None or lease_id is None:
        return None, None
    return _owner_ref(owner_ref), _uuid(lease_id, "lease ID")


def _validate_host_receipt(receipt: HostReadinessReceipt) -> None:
    if receipt.format_version != HOST_READINESS_FORMAT_VERSION:
        raise ValidationError("unsupported host readiness receipt version")
    _uuid(receipt.vault_id, "vault ID")
    _uuid(receipt.host_id, "host ID")
    _version(receipt.app_version, "app version")
    _version(receipt.core_version, "core version")
    _version(receipt.plugin_version, "plugin version")
    stored_time(receipt.created_at, "created_at")
    stored_time(receipt.updated_at, "updated_at")
    if parse_time(receipt.updated_at) < parse_time(receipt.created_at):
        raise ValidationError("host readiness updated_at cannot precede created_at")
    if receipt.revision:
        _expected_revision(receipt.revision)
    if not all(
        isinstance(value, bool)
        for value in (
            receipt.context_synthesis_proved,
            receipt.initial_orientation_proved,
            receipt.fresh_task_context_proved,
        )
    ):
        raise ValidationError("host proof fields must be booleans")
    capabilities = tuple(
        _validated_capability_fingerprint(item) for item in receipt.capability_fingerprints
    )
    if tuple(sorted(capabilities, key=lambda item: item.capability_id)) != capabilities:
        raise ValidationError("capability fingerprints must be unique and sorted")
    if len({item.capability_id for item in capabilities}) != len(capabilities):
        raise ValidationError("capability fingerprints contain duplicate capability IDs")
    permissions = tuple(
        _validated_permission_evidence(item) for item in receipt.permission_evidence
    )
    if tuple(sorted(permissions, key=lambda item: item.permission_id)) != permissions:
        raise ValidationError("permission evidence must be unique and sorted")
    if len({item.permission_id for item in permissions}) != len(permissions):
        raise ValidationError("permission evidence contains duplicate permission IDs")
    _validated_scheduled_wake(receipt.scheduled_wake_evidence)
    sources = tuple(_validated_source(item) for item in receipt.sources)
    if tuple(sorted(sources, key=lambda item: item.source_id)) != receipt.sources:
        raise ValidationError("source readiness records must be unique and sorted")
    if len({item.source_id for item in sources}) != len(sources):
        raise ValidationError("source readiness records contain duplicate source IDs")


def _validated_capability_fingerprint(
    value: CapabilityFingerprint,
) -> CapabilityFingerprint:
    _safe_token(value.capability_id, "capability ID")
    _fingerprint(value.fingerprint, "capability fingerprint")
    stored_time(value.observed_at, "capability observed_at")
    return value


def _capability_fingerprint_mapping(value: CapabilityFingerprint) -> dict[str, str]:
    _validated_capability_fingerprint(value)
    return {
        "capability_id": value.capability_id,
        "fingerprint": value.fingerprint,
        "observed_at": value.observed_at,
    }


def _capability_fingerprint_from_value(value: object) -> CapabilityFingerprint:
    data = _mapping(value, "capability fingerprint")
    _exact_keys(data, _CAPABILITY_FINGERPRINT_KEYS, "capability fingerprint")
    return _validated_capability_fingerprint(
        CapabilityFingerprint(
            capability_id=_required_string(data, "capability_id"),
            fingerprint=_required_string(data, "fingerprint"),
            observed_at=_required_string(data, "observed_at"),
        )
    )


_CAPABILITY_FINGERPRINT_KEYS: Final = frozenset({"capability_id", "fingerprint", "observed_at"})


def _validated_permission_evidence(value: PermissionEvidence) -> PermissionEvidence:
    permission_id = _safe_token(value.permission_id, "permission ID")
    prefix = f"permission:{permission_id}:"
    if not value.evidence_ref.startswith(prefix):
        raise ValidationError("permission evidence reference must name the matching permission")
    suffix = value.evidence_ref.removeprefix(prefix)
    if not suffix or len(suffix) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", suffix):
        raise ValidationError("permission evidence reference must be one bounded opaque reference")
    stored_time(value.observed_at, "permission observed_at")
    return value


def _permission_evidence_mapping(value: PermissionEvidence) -> dict[str, str]:
    _validated_permission_evidence(value)
    return {
        "evidence_ref": value.evidence_ref,
        "observed_at": value.observed_at,
        "permission_id": value.permission_id,
        "state": value.state.value,
    }


def _permission_evidence_from_value(value: object) -> PermissionEvidence:
    data = _mapping(value, "permission evidence")
    _exact_keys(data, _PERMISSION_EVIDENCE_KEYS, "permission evidence")
    return _validated_permission_evidence(
        PermissionEvidence(
            permission_id=_required_string(data, "permission_id"),
            state=_enum(PermissionState, data.get("state"), "permission state"),
            evidence_ref=_required_string(data, "evidence_ref"),
            observed_at=_required_string(data, "observed_at"),
        )
    )


_PERMISSION_EVIDENCE_KEYS: Final = frozenset(
    {"evidence_ref", "observed_at", "permission_id", "state"}
)


def _validated_scheduled_wake(value: ScheduledWakeEvidence) -> ScheduledWakeEvidence:
    stored_time(value.observed_at, "scheduled wake observed_at")
    proof = (value.automation_ref, value.task_id, value.scheduled_for)
    if value.state is ScheduledWakeState.NOT_CONFIGURED:
        if any(item is not None for item in (*proof, value.result_digest)):
            raise ValidationError("unconfigured scheduled wake cannot contain proof fields")
        return value
    if any(item is None for item in proof):
        raise ValidationError("configured scheduled wake requires its exact automation and task")
    assert value.automation_ref is not None
    assert value.task_id is not None
    assert value.scheduled_for is not None
    if not _SAFE_AUTOMATION_REF.fullmatch(value.automation_ref):
        raise ValidationError("scheduled wake automation reference is invalid")
    _uuid(value.task_id, "scheduled wake task ID")
    stored_time(value.scheduled_for, "scheduled_for")
    if value.state in {ScheduledWakeState.VERIFIED, ScheduledWakeState.STALE}:
        if value.result_digest is None:
            raise ValidationError("verified or stale scheduled wake requires a result digest")
    elif value.state is ScheduledWakeState.SCHEDULED and value.result_digest is not None:
        raise ValidationError("pending scheduled wake cannot contain a result digest")
    if value.result_digest is not None:
        _fingerprint(value.result_digest, "scheduled wake result digest")
    return value


def _scheduled_wake_mapping(value: ScheduledWakeEvidence) -> dict[str, Any]:
    _validated_scheduled_wake(value)
    return {
        "automation_ref": value.automation_ref,
        "observed_at": value.observed_at,
        "result_digest": value.result_digest,
        "scheduled_for": value.scheduled_for,
        "state": value.state.value,
        "task_id": value.task_id,
    }


def _scheduled_wake_from_value(value: object) -> ScheduledWakeEvidence:
    data = _mapping(value, "scheduled wake evidence")
    _exact_keys(data, _SCHEDULED_WAKE_KEYS, "scheduled wake evidence")
    return _validated_scheduled_wake(
        ScheduledWakeEvidence(
            state=_enum(ScheduledWakeState, data.get("state"), "scheduled wake state"),
            automation_ref=_optional_string(data, "automation_ref"),
            task_id=_optional_string(data, "task_id"),
            scheduled_for=_optional_string(data, "scheduled_for"),
            observed_at=_required_string(data, "observed_at"),
            result_digest=_optional_string(data, "result_digest"),
        )
    )


_SCHEDULED_WAKE_KEYS: Final = frozenset(
    {"automation_ref", "observed_at", "result_digest", "scheduled_for", "state", "task_id"}
)


def _validated_source(value: SourceReadiness) -> SourceReadiness:
    _safe_token(value.source_id, "source ID")
    for fingerprint, label in (
        (value.expected_account_fingerprint, "expected account fingerprint"),
        (value.observed_account_fingerprint, "observed account fingerprint"),
        (value.tool_shape_fingerprint, "tool shape fingerprint"),
        (value.result_digest, "result digest"),
    ):
        if fingerprint is not None:
            _fingerprint(fingerprint, label)
    if value.tool_name is not None:
        _tool_name(value.tool_name)
    if value.stable_ref is not None:
        _stable_ref(value.stable_ref, value.source_id)
    if value.last_verified_at is not None:
        stored_time(value.last_verified_at, "last_verified_at")
    if value.last_observed_at is not None:
        stored_time(value.last_observed_at, "last_observed_at")
    if value.last_error_code is not None:
        _safe_token(value.last_error_code, "source error code")
    if value.state is SourceState.NOT_SELECTED and any(
        item is not None
        for item in (
            value.expected_account_fingerprint,
            value.observed_account_fingerprint,
            value.tool_name,
            value.tool_shape_fingerprint,
            value.stable_ref,
            value.result_digest,
            value.last_verified_at,
            value.last_observed_at,
            value.last_error_code,
        )
    ):
        raise ValidationError("not-selected source cannot contain readiness evidence")
    if value.state is SourceState.READY:
        required = (
            value.expected_account_fingerprint,
            value.observed_account_fingerprint,
            value.tool_name,
            value.tool_shape_fingerprint,
            value.stable_ref,
            value.result_digest,
            value.last_verified_at,
            value.last_observed_at,
        )
        if any(item is None for item in required):
            raise ValidationError("ready source requires complete structured proof")
        if value.expected_account_fingerprint != value.observed_account_fingerprint:
            raise ValidationError("ready source account fingerprints must match")
        if value.last_error_code is not None:
            raise ValidationError("ready source cannot contain an error code")
    return value


def _validate_tool_attestation_shape(value: ToolAttestation) -> None:
    if value.format_version != 1:
        raise ValidationError("unsupported tool attestation version")
    _safe_token(value.source_id, "source ID")
    _uuid(value.task_id, "attestation task ID")
    stored_time(value.observed_at, "attestation observed_at")
    proof_fields = (
        value.tool_call_id,
        value.tool_name,
        value.tool_shape_fingerprint,
        value.account_fingerprint,
        value.stable_ref,
        value.result_digest,
        value.read_complete,
        value.records_observed,
    )
    if value.event is AttestationEvent.TOOL_ABSENT:
        if any(item is not None for item in proof_fields):
            raise ValidationError("tool-absence event cannot contain invented proof fields")
        return
    if any(item is None for item in proof_fields):
        raise ValidationError("tool-result attestation requires every structured proof field")
    assert value.tool_call_id is not None
    assert value.tool_name is not None
    assert value.tool_shape_fingerprint is not None
    assert value.account_fingerprint is not None
    assert value.stable_ref is not None
    assert value.result_digest is not None
    assert value.records_observed is not None
    if not _SAFE_TOOL_CALL_ID.fullmatch(value.tool_call_id):
        raise ValidationError("tool call ID must be one bounded opaque reference")
    _tool_name(value.tool_name)
    _fingerprint(value.tool_shape_fingerprint, "tool shape fingerprint")
    _fingerprint(value.account_fingerprint, "account fingerprint")
    _stable_ref(value.stable_ref, value.source_id)
    _fingerprint(value.result_digest, "result digest")
    if value.read_complete is not True:
        raise ValidationError("tool-result attestation must prove a complete bounded read")
    if isinstance(value.records_observed, bool) or value.records_observed < 0:
        raise ValidationError("records_observed must be a non-negative integer")


def _validate_trusted_receipt(value: TrustedToolReceipt) -> None:
    if not _SAFE_TOOL_CALL_ID.fullmatch(value.call_id):
        raise ValidationError("trusted receipt call ID is invalid")
    _uuid(value.task_id, "trusted receipt task ID")
    _safe_token(value.source_id, "trusted receipt source ID")
    _tool_name(value.tool_name)
    _fingerprint(value.tool_shape_fingerprint, "trusted tool shape fingerprint")
    _fingerprint(value.account_fingerprint, "trusted account fingerprint")
    _stable_ref(value.stable_ref, value.source_id)
    _fingerprint(value.result_digest, "trusted result digest")
    if value.read_complete is not True:
        raise ValidationError("trusted receipt must describe a complete bounded read")
    if isinstance(value.records_observed, bool) or value.records_observed < 0:
        raise ValidationError("trusted records_observed must be a non-negative integer")
    stored_time(value.observed_at, "trusted receipt observed_at")


def _validate_fresh_time(value: str, *, now: datetime, max_age: timedelta) -> None:
    if max_age <= timedelta(0):
        raise ValidationError("attestation max age must be positive")
    observed = parse_time(value)
    current = now if now.tzinfo else now.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    if observed > current + MAX_FUTURE_SKEW:
        raise ValidationError("tool attestation timestamp is in the future")
    if observed < current - max_age:
        raise ValidationError("tool attestation is stale")


def _lease_mapping(value: SessionLease | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "acquired_at": value.acquired_at,
        "expires_at": value.expires_at,
        "generation": value.generation,
        "lease_id": value.lease_id,
        "owner_ref": value.owner_ref,
        "predecessor_lease_id": value.predecessor_lease_id,
    }


def _lease_from_value(value: object) -> SessionLease | None:
    if value is None:
        return None
    data = _mapping(value, "session lease")
    _exact_keys(data, _LEASE_KEYS, "session lease")
    lease = SessionLease(
        owner_ref=_required_string(data, "owner_ref"),
        lease_id=_required_string(data, "lease_id"),
        acquired_at=_required_string(data, "acquired_at"),
        expires_at=_required_string(data, "expires_at"),
        generation=_integer(data, "generation"),
        predecessor_lease_id=_optional_string(data, "predecessor_lease_id"),
    )
    _validate_lease(lease)
    return lease


_LEASE_KEYS: Final = frozenset(
    {
        "acquired_at",
        "expires_at",
        "generation",
        "lease_id",
        "owner_ref",
        "predecessor_lease_id",
    }
)


def _source_mapping(value: SourceReadiness) -> dict[str, Any]:
    _validated_source(value)
    return {
        "expected_account_fingerprint": value.expected_account_fingerprint,
        "last_error_code": value.last_error_code,
        "last_observed_at": value.last_observed_at,
        "last_verified_at": value.last_verified_at,
        "observed_account_fingerprint": value.observed_account_fingerprint,
        "result_digest": value.result_digest,
        "source_id": value.source_id,
        "stable_ref": value.stable_ref,
        "state": value.state.value,
        "tool_name": value.tool_name,
        "tool_shape_fingerprint": value.tool_shape_fingerprint,
    }


def _source_from_value(value: object) -> SourceReadiness:
    data = _mapping(value, "source readiness")
    _exact_keys(data, _SOURCE_KEYS, "source readiness")
    return _validated_source(
        SourceReadiness(
            source_id=_required_string(data, "source_id"),
            state=_enum(SourceState, data.get("state"), "source state"),
            expected_account_fingerprint=_optional_string(data, "expected_account_fingerprint"),
            observed_account_fingerprint=_optional_string(data, "observed_account_fingerprint"),
            tool_name=_optional_string(data, "tool_name"),
            tool_shape_fingerprint=_optional_string(data, "tool_shape_fingerprint"),
            stable_ref=_optional_string(data, "stable_ref"),
            result_digest=_optional_string(data, "result_digest"),
            last_verified_at=_optional_string(data, "last_verified_at"),
            last_observed_at=_optional_string(data, "last_observed_at"),
            last_error_code=_optional_string(data, "last_error_code"),
        )
    )


_SOURCE_KEYS: Final = frozenset(
    {
        "expected_account_fingerprint",
        "last_error_code",
        "last_observed_at",
        "last_verified_at",
        "observed_account_fingerprint",
        "result_digest",
        "source_id",
        "stable_ref",
        "state",
        "tool_name",
        "tool_shape_fingerprint",
    }
)


EnumValue = TypeVar("EnumValue", bound=StrEnum)


def _enum(kind: type[EnumValue], value: object, label: str) -> EnumValue:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    try:
        return kind(value)
    except ValueError as exc:
        raise ValidationError(f"invalid {label}: {value}") from exc


def _optional_enum(kind: type[EnumValue], value: object, label: str) -> EnumValue | None:
    return None if value is None else _enum(kind, value, label)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValidationError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        extras = sorted(actual - expected)
        missing = sorted(expected - actual)
        details = []
        if extras:
            details.append(f"unexpected fields: {', '.join(extras)}")
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        raise ValidationError(
            f"{label} must contain only its structured fields ({'; '.join(details)}); "
            "free-form or raw provider content is not accepted"
        )


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise ValidationError(f"{key} must be a string")
    return result


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str):
        raise ValidationError(f"{key} must be a string or null")
    return result


def _integer(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValidationError(f"{key} must be an integer")
    return result


def _optional_integer(value: Mapping[str, Any], key: str) -> int | None:
    result = value.get(key)
    if result is None:
        return None
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValidationError(f"{key} must be an integer or null")
    return result


def _boolean(value: Mapping[str, Any], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ValidationError(f"{key} must be a boolean")
    return result


def _optional_boolean(value: Mapping[str, Any], key: str) -> bool | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, bool):
        raise ValidationError(f"{key} must be a boolean or null")
    return result


def _list(value: Mapping[str, Any], key: str) -> list[Any]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ValidationError(f"{key} must be a list")
    return result


def _string_list(value: Mapping[str, Any], key: str) -> list[str]:
    result = _list(value, key)
    if any(not isinstance(item, str) for item in result):
        raise ValidationError(f"{key} must be a string list")
    return result


def _safe_token(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        raise ValidationError(f"{label} must be a lowercase bounded token")
    return value


def _source_ids(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(values) > MAX_SOURCES:
        raise ValidationError("too many selected sources")
    clean = tuple(_safe_token(item, "source ID") for item in values)
    if len(set(clean)) != len(clean):
        raise ValidationError("selected source IDs must be unique")
    return clean


def _owner_ref(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_OWNER_REF.fullmatch(value):
        raise ValidationError("lease owner must be one bounded opaque reference")
    return value


def _tool_name(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_TOOL_NAME.fullmatch(value):
        raise ValidationError("tool name must be one bounded identifier, not free text")
    return value


def _stable_ref(value: str, source_id: str) -> str:
    prefix = f"source:{source_id}:"
    suffix = value[len(prefix) :] if value.startswith(prefix) else ""
    if (
        not suffix
        or len(value) > 256
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/=-]{0,191}", suffix)
    ):
        raise ValidationError(
            f"stable reference must be a bounded opaque {prefix}<reference> value"
        )
    return value


def _fingerprint(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValidationError(f"{label} must be a sha256 fingerprint")
    return value


def _uuid(value: str, label: str) -> str:
    clean = str(value).lower()
    if not _UUID.fullmatch(clean):
        raise ValidationError(f"{label} must be a canonical UUID")
    return clean


def _version(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_VERSION.fullmatch(value):
        raise ValidationError(f"{label} must be one bounded version token")
    return value


def _expected_revision(value: str) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise ValidationError("expected revision must be a SHA-256 digest")
    return value


def _lease_ttl(value: timedelta) -> timedelta:
    if value < MIN_LEASE_TTL or value > MAX_LEASE_TTL:
        raise ValidationError("lease TTL must be between 5 seconds and 24 hours")
    return value
