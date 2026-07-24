from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from continuity_kernel.atomic import PinnedPathRoot
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.onboarding import (
    AttestationDecision,
    AttestationDecisionReason,
    AttestationEvent,
    CapabilityFingerprint,
    CompletionState,
    ContextCheckpoint,
    ContextSection,
    ContextState,
    HostCapabilityState,
    HostReadinessReceipt,
    HostReadinessStore,
    NextActor,
    OnboardingPhase,
    OnboardingSession,
    OnboardingStore,
    PermissionEvidence,
    PermissionState,
    ScheduledWakeEvidence,
    ScheduledWakeState,
    SourceReadiness,
    SourceState,
    ToolAttestation,
    TrustedToolReceipt,
    derive_onboarding_state,
    new_host_readiness_receipt,
    new_onboarding_session,
    parse_host_readiness_receipt,
    parse_onboarding_session,
    render_host_readiness_receipt,
    render_onboarding_session,
    validate_tool_attestation,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ACCOUNT = "sha256:" + "a" * 64
OTHER_ACCOUNT = "sha256:" + "b" * 64
SHAPE = "sha256:" + "c" * 64
OTHER_SHAPE = "sha256:" + "d" * 64
RESULT = "sha256:" + "e" * 64
TOOL = "mcp__codex_apps__gmail_recent"
CALL_ID = "call:tool-123"
VAULT_ID = "019f1234-1234-4234-8234-123456789abc"
HOST_ID = "019f5678-1234-4234-8234-123456789abc"
OTHER_VAULT_ID = "019f1234-1234-4234-8234-123456789abd"
OTHER_HOST_ID = "019f5678-1234-4234-8234-123456789abd"
TASK_ID = "019f9999-1234-7234-8234-123456789abc"


def _pending_source(
    state: SourceState = SourceState.READ_UNVERIFIED,
    *,
    source_id: str = "gmail",
    account: str | None = ACCOUNT,
) -> SourceReadiness:
    return SourceReadiness(
        source_id=source_id,
        state=state,
        expected_account_fingerprint=account,
    )


def _ready_source(
    source_id: str = "gmail",
    *,
    verified_at: str = "2026-07-24T12:00:00.000000Z",
) -> SourceReadiness:
    return SourceReadiness(
        source_id=source_id,
        state=SourceState.READY,
        expected_account_fingerprint=ACCOUNT,
        observed_account_fingerprint=ACCOUNT,
        tool_name=f"mcp__gsv__{source_id}_bounded_read",
        tool_shape_fingerprint=SHAPE,
        stable_ref=f"source:{source_id}:bounded-read",
        result_digest=RESULT,
        last_verified_at=verified_at,
        last_observed_at=verified_at,
    )


def _proof(*, records: int = 0, shape: str = SHAPE, account: str = ACCOUNT) -> ToolAttestation:
    return ToolAttestation(
        format_version=1,
        event=AttestationEvent.TOOL_RESULT,
        task_id=TASK_ID,
        source_id="gmail",
        observed_at="2026-07-24T12:00:00.000000Z",
        tool_call_id=CALL_ID,
        tool_name=TOOL,
        tool_shape_fingerprint=shape,
        account_fingerprint=account,
        stable_ref="source:gmail:inbox/recent",
        result_digest=RESULT,
        read_complete=True,
        records_observed=records,
    )


def _trusted(*, records: int = 0, shape: str = SHAPE) -> TrustedToolReceipt:
    return TrustedToolReceipt(
        call_id=CALL_ID,
        task_id=TASK_ID,
        source_id="gmail",
        tool_name=TOOL,
        tool_shape_fingerprint=shape,
        account_fingerprint=ACCOUNT,
        stable_ref="source:gmail:inbox/recent",
        result_digest=RESULT,
        read_complete=True,
        records_observed=records,
        observed_at="2026-07-24T12:00:00.000000Z",
    )


def _validate(
    proof: ToolAttestation,
    *,
    prior: SourceReadiness | None = None,
    receipt: TrustedToolReceipt | None = None,
    known: dict[str, dict[str, str]] | None = None,
) -> AttestationDecision:
    trusted = receipt or _trusted(records=proof.records_observed or 0)
    return validate_tool_attestation(
        proof,
        expected_task_id=TASK_ID,
        prior=prior or _pending_source(),
        known_tool_shapes={"gmail": {TOOL: SHAPE}} if known is None else known,
        trusted_receipts={trusted.call_id: trusted},
        now=NOW,
    )


def _confirmed_session(*, selected_sources: tuple[str, ...] = ("gmail",)) -> OnboardingSession:
    session = new_onboarding_session(selected_sources=selected_sources, observed_at=NOW)
    return replace(
        session,
        source_selection_confirmed=True,
        context=tuple(
            ContextCheckpoint(section=section, state=ContextState.CONFIRMED)
            for section in ContextSection
        ),
    )


def _host(
    *,
    sources: tuple[SourceReadiness, ...] = (),
    synthesis: bool = False,
    orientation: bool = False,
    fresh_context: bool = False,
    codex: HostCapabilityState = HostCapabilityState.READY,
    bridge: HostCapabilityState = HostCapabilityState.READY,
    pulse: HostCapabilityState = HostCapabilityState.UNKNOWN,
    wake: ScheduledWakeState = ScheduledWakeState.NOT_CONFIGURED,
    permission: PermissionState = PermissionState.GRANTED,
) -> HostReadinessReceipt:
    if not any(source.source_id == "gsv" for source in sources):
        sources = (_ready_source("gsv"), *sources)
    wake_evidence = ScheduledWakeEvidence(
        state=wake,
        automation_ref=None if wake is ScheduledWakeState.NOT_CONFIGURED else "automation:pulse-1",
        task_id=None if wake is ScheduledWakeState.NOT_CONFIGURED else TASK_ID,
        scheduled_for=(
            None if wake is ScheduledWakeState.NOT_CONFIGURED else "2026-07-24T11:59:00.000000Z"
        ),
        observed_at="2026-07-24T12:00:00.000000Z",
        result_digest=(
            RESULT if wake in {ScheduledWakeState.VERIFIED, ScheduledWakeState.STALE} else None
        ),
    )
    receipt = new_host_readiness_receipt(
        vault_id=VAULT_ID,
        host_id=HOST_ID,
        app_version="0.146.0-alpha.3.1",
        core_version="0.3.0",
        plugin_version="1.0.0",
        capability_fingerprints=(
            CapabilityFingerprint(
                capability_id="codex-tools",
                fingerprint=SHAPE,
                observed_at="2026-07-24T12:00:00.000000Z",
            ),
        ),
        permission_evidence=(
            PermissionEvidence(
                permission_id="automation",
                state=permission,
                evidence_ref="permission:automation:system-settings",
                observed_at="2026-07-24T12:00:00.000000Z",
            ),
        ),
        scheduled_wake_evidence=wake_evidence,
        sources=sources,
        observed_at=NOW,
    )
    return replace(
        receipt,
        codex_state=codex,
        bridge_state=bridge,
        pulse_state=pulse,
        context_synthesis_proved=synthesis,
        initial_orientation_proved=orientation,
        fresh_task_context_proved=fresh_context,
    )


def _derive(
    session: OnboardingSession,
    receipt: HostReadinessReceipt,
) -> tuple[OnboardingPhase, CompletionState]:
    return derive_onboarding_state(
        session,
        receipt,
        expected_vault_id=VAULT_ID,
        expected_host_id=HOST_ID,
        now=NOW,
    )


def test_public_onboarding_vocabulary_is_exact() -> None:
    assert [item.value for item in OnboardingPhase] == [
        "codex_substrate",
        "privacy_and_context_capture",
        "source_selection",
        "enablement_wait",
        "fresh_task_verification",
        "context_synthesis",
        "initial_orientation",
        "continuity_and_autonomy_proof",
        "done",
    ]
    assert [item.value for item in CompletionState] == [
        "in_progress",
        "waiting_user",
        "fresh_task_required",
        "blocked",
        "operational_with_gaps",
        "fully_connected",
        "needs_revalidation",
    ]
    assert [item.value for item in SourceState] == [
        "not_selected",
        "plugin_missing",
        "auth_required",
        "fresh_task_required",
        "tool_absent",
        "identity_pending",
        "identity_mismatch",
        "read_unverified",
        "canary_failed",
        "ready",
        "stale",
        "blocked_by_policy",
        "unsupported_tool_shape",
        "unavailable",
        "declined",
    ]


def test_portable_session_round_trips_as_canonical_markdown() -> None:
    session = new_onboarding_session(
        selected_sources=("gmail", "github"),
        observed_at=NOW,
        onboarding_id="019faaaa-1234-4234-8234-123456789abc",
    )
    markdown = render_onboarding_session(session)
    parsed = parse_onboarding_session(markdown)

    assert parsed == session
    assert parsed.revision
    assert "# GSV onboarding" in markdown
    assert parsed.selected_sources == ("github", "gmail", "gsv")
    assert "gmail" in markdown
    assert "provider body" not in markdown.lower()


def test_session_parser_rejects_free_form_or_metadata_extension() -> None:
    markdown = render_onboarding_session(new_onboarding_session(observed_at=NOW))
    with pytest.raises(ValidationError, match="free-form"):
        parse_onboarding_session(markdown + "A model says setup is complete.\n")

    lines = markdown.splitlines()
    payload = json.loads(lines[0][len("<!-- gsv-onboarding:") : -len(" -->")])
    payload["provider_body"] = "raw inbox text"
    lines[0] = (
        "<!-- gsv-onboarding:" + json.dumps(payload, separators=(",", ":"), sort_keys=True) + " -->"
    )
    with pytest.raises(ValidationError, match="raw provider content"):
        parse_onboarding_session("\n".join(lines) + "\n")


def test_session_invariants_reject_duplicates_and_impossible_completion() -> None:
    with pytest.raises(ValidationError, match="unique"):
        new_onboarding_session(selected_sources=("gmail", "gmail"), observed_at=NOW)

    session = new_onboarding_session(observed_at=NOW)
    with pytest.raises(ValidationError, match="source zero"):
        render_onboarding_session(replace(session, selected_sources=()))
    with pytest.raises(ValidationError, match="operational completion"):
        render_onboarding_session(replace(session, completion=CompletionState.FULLY_CONNECTED))
    with pytest.raises(ValidationError, match="set or cleared together"):
        render_onboarding_session(replace(session, next_actor=None))


def test_onboarding_store_is_cas_safe_and_preserves_markdown_authority(tmp_path: Path) -> None:
    store = OnboardingStore(tmp_path / "vault")
    created = store.create(new_onboarding_session(observed_at=NOW))
    candidate = replace(
        created,
        next_actor=NextActor.USER,
        next_action_code="describe-life",
    )
    updated = store.save(
        candidate,
        expected_revision=created.revision,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert updated.revision != created.revision
    assert updated.phase is OnboardingPhase.CODEX_SUBSTRATE
    assert store.path == tmp_path / "vault/onboarding/session.md"
    assert store.load() == updated
    with pytest.raises(ConflictError, match="changed"):
        store.save(candidate, expected_revision=created.revision)
    with pytest.raises(ConflictError, match="already exists"):
        store.create(new_onboarding_session(observed_at=NOW))


def test_onboarding_cas_never_writes_into_a_replacement_vault_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OnboardingStore(tmp_path / "vault")
    created = store.create(new_onboarding_session(observed_at=NOW))
    candidate = replace(created, next_action_code="replacement-must-not-receive-this")
    parked = tmp_path / "parked-vault"
    actual_cas = PinnedPathRoot.compare_and_swap_regular_file
    replacement_before: bytes | None = None
    swapped = False

    def swap_before_cas(
        pinned: PinnedPathRoot,
        relative: Path | str,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int,
        mode: int = 0o600,
    ) -> None:
        nonlocal replacement_before, swapped
        if str(relative) == "onboarding/session.md" and not swapped:
            swapped = True
            pinned.root.rename(parked)
            shutil.copytree(parked, pinned.root)
            replacement_before = (pinned.root / "onboarding/session.md").read_bytes()
        actual_cas(
            pinned,
            relative,
            expected=expected,
            replacement=replacement,
            label=label,
            max_bytes=max_bytes,
            mode=mode,
        )

    monkeypatch.setattr(PinnedPathRoot, "compare_and_swap_regular_file", swap_before_cas)
    with pytest.raises((OSError, ValidationError)):
        store.save(
            candidate,
            expected_revision=created.revision,
            observed_at=NOW + timedelta(seconds=1),
        )

    assert swapped is True
    assert replacement_before is not None
    assert (store.root / "onboarding/session.md").read_bytes() == replacement_before
    assert b"replacement-must-not-receive-this" not in replacement_before


def test_generic_save_rejects_impossible_phase_and_reconcile_derives_bound_state(
    tmp_path: Path,
) -> None:
    store = OnboardingStore(tmp_path / "vault")
    created = store.create(new_onboarding_session(observed_at=NOW))
    impossible = replace(created, phase=OnboardingPhase.SOURCE_SELECTION)

    with pytest.raises(ValidationError, match="deterministic reconcile"):
        store.save(
            impossible,
            expected_revision=created.revision,
            observed_at=NOW + timedelta(seconds=1),
        )
    assert store.load() == created

    claimed = store.claim_lease(
        owner_ref="codex:first",
        expected_revision=created.revision,
        observed_at=NOW + timedelta(seconds=1),
    )
    assert claimed.lease is not None
    reconciled = store.reconcile(
        _host(),
        expected_vault_id=VAULT_ID,
        expected_host_id=HOST_ID,
        expected_revision=claimed.revision,
        owner_ref=claimed.lease.owner_ref,
        lease_id=claimed.lease.lease_id,
        observed_at=NOW + timedelta(seconds=2),
    )

    assert reconciled.phase is OnboardingPhase.PRIVACY_AND_CONTEXT_CAPTURE
    assert reconciled.completion is CompletionState.IN_PROGRESS
    assert reconciled.lease == claimed.lease
    assert store.load() == reconciled


def test_derive_requires_exact_vault_and_host_receipt_binding() -> None:
    session = new_onboarding_session(observed_at=NOW)
    receipt = _host()

    with pytest.raises(ValidationError, match="different vault"):
        derive_onboarding_state(
            session,
            receipt,
            expected_vault_id=OTHER_VAULT_ID,
            expected_host_id=HOST_ID,
            now=NOW,
        )
    with pytest.raises(ValidationError, match="different host"):
        derive_onboarding_state(
            session,
            receipt,
            expected_vault_id=VAULT_ID,
            expected_host_id=OTHER_HOST_ID,
            now=NOW,
        )

    assert derive_onboarding_state(
        session,
        receipt,
        expected_vault_id=VAULT_ID,
        expected_host_id=HOST_ID,
        now=NOW,
    ) == (OnboardingPhase.PRIVACY_AND_CONTEXT_CAPTURE, CompletionState.IN_PROGRESS)


def test_two_session_writers_with_one_revision_produce_one_winner(tmp_path: Path) -> None:
    store = OnboardingStore(tmp_path / "vault")
    before = store.create(new_onboarding_session(observed_at=NOW))

    def update(code: str) -> str:
        candidate = replace(before, next_action_code=code)
        try:
            return (
                OnboardingStore(store.root)
                .save(
                    candidate,
                    expected_revision=before.revision,
                    observed_at=NOW + timedelta(seconds=1),
                )
                .next_action_code
                or ""
            )
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ("writer-one", "writer-two")))

    assert results.count("conflict") == 1
    assert len([item for item in results if item.startswith("writer-")]) == 1


def test_lease_claim_renew_release_and_expired_takeover(tmp_path: Path) -> None:
    store = OnboardingStore(tmp_path / "vault")
    session = store.create(new_onboarding_session(observed_at=NOW))
    claimed = store.claim_lease(
        owner_ref="codex:first",
        expected_revision=session.revision,
        observed_at=NOW,
        ttl=timedelta(minutes=10),
    )
    assert claimed.lease is not None
    assert claimed.lease.generation == 1

    with pytest.raises(ConflictError, match="active Codex hand"):
        store.claim_lease(
            owner_ref="codex:second",
            expected_revision=claimed.revision,
            observed_at=NOW + timedelta(minutes=1),
        )

    renewed = store.renew_lease(
        owner_ref="codex:first",
        lease_id=claimed.lease.lease_id,
        expected_revision=claimed.revision,
        observed_at=NOW + timedelta(minutes=1),
        ttl=timedelta(minutes=5),
    )
    assert renewed.lease is not None
    assert renewed.lease.lease_id == claimed.lease.lease_id
    released = store.release_lease(
        owner_ref="codex:first",
        lease_id=renewed.lease.lease_id,
        expected_revision=renewed.revision,
        observed_at=NOW + timedelta(minutes=2),
    )
    assert released.lease is None

    again = store.claim_lease(
        owner_ref="codex:first",
        expected_revision=released.revision,
        observed_at=NOW + timedelta(minutes=3),
        ttl=timedelta(minutes=5),
    )
    assert again.lease is not None
    expired_takeover = store.claim_lease(
        owner_ref="codex:second",
        expected_revision=again.revision,
        observed_at=NOW + timedelta(minutes=9),
    )
    assert expired_takeover.lease is not None
    assert expired_takeover.lease.generation == 2
    assert expired_takeover.lease.predecessor_lease_id == again.lease.lease_id


def test_explicit_active_lease_takeover_requires_exact_lease_and_revision(tmp_path: Path) -> None:
    store = OnboardingStore(tmp_path / "vault")
    first = store.create(new_onboarding_session(observed_at=NOW))
    first = store.claim_lease(
        owner_ref="codex:first", expected_revision=first.revision, observed_at=NOW
    )
    assert first.lease is not None

    with pytest.raises(ConflictError, match="lease changed"):
        store.takeover_lease(
            owner_ref="codex:second",
            expected_lease_id="019f0000-1234-4234-8234-123456789abc",
            expected_revision=first.revision,
            observed_at=NOW + timedelta(seconds=1),
        )
    second = store.takeover_lease(
        owner_ref="codex:second",
        expected_lease_id=first.lease.lease_id,
        expected_revision=first.revision,
        observed_at=NOW + timedelta(seconds=1),
    )
    assert second.lease is not None
    assert second.lease.owner_ref == "codex:second"
    assert second.lease.predecessor_lease_id == first.lease.lease_id

    with pytest.raises(ConflictError, match="changed"):
        store.release_lease(
            owner_ref="codex:second",
            lease_id=second.lease.lease_id,
            expected_revision=first.revision,
        )


def test_generic_save_requires_current_live_hand_and_preserves_the_exact_lease(
    tmp_path: Path,
) -> None:
    store = OnboardingStore(tmp_path / "vault")
    created = store.create(new_onboarding_session(observed_at=NOW))
    claimed = store.claim_lease(
        owner_ref="codex:first",
        expected_revision=created.revision,
        observed_at=NOW,
        ttl=timedelta(minutes=5),
    )
    assert claimed.lease is not None
    candidate = replace(claimed, next_action_code="capture-context")

    with pytest.raises(ConflictError, match="owner or ID changed"):
        store.save(
            candidate,
            expected_revision=claimed.revision,
            owner_ref="codex:second",
            lease_id=claimed.lease.lease_id,
            observed_at=NOW + timedelta(minutes=1),
        )
    with pytest.raises(ConflictError, match="owner or ID changed"):
        store.save(
            candidate,
            expected_revision=claimed.revision,
            observed_at=NOW + timedelta(minutes=1),
        )

    saved = store.save(
        candidate,
        expected_revision=claimed.revision,
        owner_ref=claimed.lease.owner_ref,
        lease_id=claimed.lease.lease_id,
        observed_at=NOW + timedelta(minutes=1),
    )
    assert saved.lease == claimed.lease


def test_generic_save_rejects_expired_hand_and_only_takeover_owner_can_continue(
    tmp_path: Path,
) -> None:
    store = OnboardingStore(tmp_path / "vault")
    created = store.create(new_onboarding_session(observed_at=NOW))
    first = store.claim_lease(
        owner_ref="codex:first",
        expected_revision=created.revision,
        observed_at=NOW,
        ttl=timedelta(minutes=5),
    )
    assert first.lease is not None
    first_candidate = replace(first, next_action_code="first-context")

    with pytest.raises(ConflictError, match="lease expired"):
        store.save(
            first_candidate,
            expected_revision=first.revision,
            owner_ref=first.lease.owner_ref,
            lease_id=first.lease.lease_id,
            observed_at=NOW + timedelta(minutes=5),
        )
    assert store.load() == first

    second = store.takeover_lease(
        owner_ref="codex:second",
        expected_lease_id=first.lease.lease_id,
        expected_revision=first.revision,
        observed_at=NOW + timedelta(minutes=6),
    )
    assert second.lease is not None
    second_candidate = replace(second, next_action_code="second-context")
    with pytest.raises(ConflictError, match="owner or ID changed"):
        store.save(
            second_candidate,
            expected_revision=second.revision,
            owner_ref="codex:first",
            lease_id=first.lease.lease_id,
            observed_at=NOW + timedelta(minutes=7),
        )

    saved = store.save(
        second_candidate,
        expected_revision=second.revision,
        owner_ref=second.lease.owner_ref,
        lease_id=second.lease.lease_id,
        observed_at=NOW + timedelta(minutes=7),
    )
    assert saved.lease == second.lease
    assert saved.next_action_code == "second-context"


@pytest.mark.parametrize("ttl", [timedelta(seconds=4), timedelta(hours=25)])
def test_lease_rejects_unsafe_ttl(tmp_path: Path, ttl: timedelta) -> None:
    store = OnboardingStore(tmp_path / "vault")
    session = store.create(new_onboarding_session(observed_at=NOW))
    with pytest.raises(ValidationError, match="TTL"):
        store.claim_lease(
            owner_ref="codex:first",
            expected_revision=session.revision,
            observed_at=NOW,
            ttl=ttl,
        )


def test_host_receipt_round_trip_and_machine_local_store(tmp_path: Path) -> None:
    pending = _pending_source()
    receipt = new_host_readiness_receipt(
        vault_id=VAULT_ID,
        host_id=HOST_ID,
        app_version="0.146.0-alpha.3.1",
        core_version="0.3.0",
        plugin_version="1.0.0",
        capability_fingerprints=(
            CapabilityFingerprint(
                capability_id="codex-tools",
                fingerprint=SHAPE,
                observed_at="2026-07-24T12:00:00.000000Z",
            ),
        ),
        permission_evidence=(
            PermissionEvidence(
                permission_id="automation",
                state=PermissionState.GRANTED,
                evidence_ref="permission:automation:system-settings",
                observed_at="2026-07-24T12:00:00.000000Z",
            ),
        ),
        scheduled_wake_evidence=ScheduledWakeEvidence(
            state=ScheduledWakeState.SCHEDULED,
            automation_ref="automation:pulse-1",
            task_id=TASK_ID,
            scheduled_for="2026-07-24T12:01:00.000000Z",
            observed_at="2026-07-24T12:00:00.000000Z",
            result_digest=None,
        ),
        sources=(pending,),
        observed_at=NOW,
    )
    encoded = render_host_readiness_receipt(receipt)
    assert parse_host_readiness_receipt(encoded) == receipt
    assert receipt.app_version == "0.146.0-alpha.3.1"
    assert receipt.capability_fingerprints[0].capability_id == "codex-tools"
    assert receipt.permission_evidence[0].state is PermissionState.GRANTED
    assert receipt.scheduled_wake_evidence.state is ScheduledWakeState.SCHEDULED
    assert b"provider_body" not in encoded

    store = HostReadinessStore(tmp_path / "app-data", vault_id=VAULT_ID)
    created = store.create(receipt)
    updated = store.save(
        replace(created, codex_state=HostCapabilityState.READY),
        expected_revision=created.revision,
        observed_at=NOW + timedelta(seconds=1),
    )
    assert updated.codex_state is HostCapabilityState.READY
    assert store.path == tmp_path / "app-data/onboarding" / VAULT_ID / "host-readiness.json"
    with pytest.raises(ConflictError, match="changed"):
        store.save(created, expected_revision=created.revision)


def test_host_readiness_store_rejects_a_receipt_for_another_vault(tmp_path: Path) -> None:
    store = HostReadinessStore(tmp_path / "app-data", vault_id=VAULT_ID)
    created = store.create(
        new_host_readiness_receipt(
            vault_id=VAULT_ID,
            host_id=HOST_ID,
            app_version="0.146.0",
            core_version="0.3.0",
            plugin_version="1.0.0",
            observed_at=NOW,
        )
    )
    replacement = new_host_readiness_receipt(
        vault_id=OTHER_VAULT_ID,
        host_id=created.host_id,
        app_version=created.app_version,
        core_version=created.core_version,
        plugin_version=created.plugin_version,
        observed_at=NOW,
    )
    store.path.write_bytes(render_host_readiness_receipt(replacement))

    with pytest.raises(ValidationError, match="different vault"):
        store.load()


def test_host_receipt_parser_rejects_raw_provider_extension() -> None:
    receipt = new_host_readiness_receipt(
        vault_id=VAULT_ID,
        host_id=HOST_ID,
        app_version="0.146.0",
        core_version="0.3.0",
        plugin_version="1.0.0",
        sources=(_pending_source(),),
        observed_at=NOW,
    )
    payload = json.loads(render_host_readiness_receipt(receipt))
    payload["sources"][0]["message_body"] = "raw inbox body"
    with pytest.raises(ValidationError, match="raw provider content"):
        parse_host_readiness_receipt(
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        )


def test_host_receipt_rejects_duplicate_capabilities_and_unbounded_evidence() -> None:
    capability = CapabilityFingerprint(
        capability_id="codex-tools",
        fingerprint=SHAPE,
        observed_at="2026-07-24T12:00:00.000000Z",
    )
    with pytest.raises(ValidationError, match="duplicate capability"):
        new_host_readiness_receipt(
            vault_id=VAULT_ID,
            host_id=HOST_ID,
            app_version="0.146.0",
            core_version="0.3.0",
            plugin_version="1.0.0",
            capability_fingerprints=(capability, capability),
            observed_at=NOW,
        )

    with pytest.raises(ValidationError, match="permission evidence reference"):
        new_host_readiness_receipt(
            vault_id=VAULT_ID,
            host_id=HOST_ID,
            app_version="0.146.0",
            core_version="0.3.0",
            plugin_version="1.0.0",
            permission_evidence=(
                PermissionEvidence(
                    permission_id="automation",
                    state=PermissionState.GRANTED,
                    evidence_ref="permission:automation:raw provider response",
                    observed_at="2026-07-24T12:00:00.000000Z",
                ),
            ),
            observed_at=NOW,
        )


def test_scheduled_wake_evidence_requires_structured_state_specific_proof() -> None:
    with pytest.raises(ValidationError, match="unconfigured scheduled wake"):
        new_host_readiness_receipt(
            vault_id=VAULT_ID,
            host_id=HOST_ID,
            app_version="0.146.0",
            core_version="0.3.0",
            plugin_version="1.0.0",
            scheduled_wake_evidence=ScheduledWakeEvidence(
                state=ScheduledWakeState.NOT_CONFIGURED,
                automation_ref="automation:invented",
                task_id=None,
                scheduled_for=None,
                observed_at="2026-07-24T12:00:00.000000Z",
                result_digest=None,
            ),
            observed_at=NOW,
        )

    with pytest.raises(ValidationError, match="requires a result digest"):
        new_host_readiness_receipt(
            vault_id=VAULT_ID,
            host_id=HOST_ID,
            app_version="0.146.0",
            core_version="0.3.0",
            plugin_version="1.0.0",
            scheduled_wake_evidence=ScheduledWakeEvidence(
                state=ScheduledWakeState.VERIFIED,
                automation_ref="automation:pulse-1",
                task_id=TASK_ID,
                scheduled_for="2026-07-24T11:59:00.000000Z",
                observed_at="2026-07-24T12:00:00.000000Z",
                result_digest=None,
            ),
            observed_at=NOW,
        )


def test_ready_source_requires_complete_matching_structured_proof() -> None:
    invalid = SourceReadiness(
        source_id="gmail",
        state=SourceState.READY,
        expected_account_fingerprint=ACCOUNT,
    )
    with pytest.raises(ValidationError, match="complete structured proof"):
        new_host_readiness_receipt(
            vault_id=VAULT_ID,
            host_id=HOST_ID,
            app_version="0.146.0",
            core_version="0.3.0",
            plugin_version="1.0.0",
            sources=(invalid,),
            observed_at=NOW,
        )

    with pytest.raises(ValidationError, match="not-selected"):
        new_host_readiness_receipt(
            vault_id=VAULT_ID,
            host_id=HOST_ID,
            app_version="0.146.0",
            core_version="0.3.0",
            plugin_version="1.0.0",
            sources=(
                SourceReadiness(
                    source_id="gmail",
                    state=SourceState.NOT_SELECTED,
                    expected_account_fingerprint=ACCOUNT,
                ),
            ),
            observed_at=NOW,
        )


@pytest.mark.parametrize("records", [0, 7])
def test_valid_attestation_accepts_complete_empty_or_nonempty_read(records: int) -> None:
    proof = _proof(records=records)
    decision = _validate(proof, receipt=_trusted(records=records))

    assert decision.applied is True
    assert decision.reason is AttestationDecisionReason.APPLIED
    assert decision.readiness.state is SourceState.READ_UNVERIFIED
    assert decision.readiness.observed_account_fingerprint == ACCOUNT
    assert decision.readiness.last_verified_at is None
    assert decision.readiness.last_observed_at == proof.observed_at


def test_attestation_rejects_wrong_account_unknown_tool_and_unknown_shape() -> None:
    with pytest.raises(ValidationError, match="selected account"):
        _validate(_proof(account=OTHER_ACCOUNT))

    with pytest.raises(ValidationError, match="known source recipe"):
        _validate(_proof(), known={"gmail": {"different_tool": SHAPE}})

    with pytest.raises(ValidationError, match="unknown or has drifted"):
        _validate(_proof(shape=OTHER_SHAPE))

    with pytest.raises(ValidationError, match="no known tool shapes"):
        _validate(_proof(), known={})


def test_attestation_is_bound_to_the_exact_codex_task_and_trusted_receipt() -> None:
    other_task = "019f8888-1234-4234-8234-123456789abc"
    with pytest.raises(ValidationError, match="different Codex task"):
        _validate(replace(_proof(), task_id=other_task))

    with pytest.raises(ValidationError, match="does not match"):
        _validate(_proof(), receipt=replace(_trusted(), task_id=other_task))

    with pytest.raises(ValidationError, match="attestation task ID"):
        _validate(replace(_proof(), task_id="current task"))


def test_attestation_rejects_fabricated_or_receipt_mismatched_tool_call() -> None:
    with pytest.raises(ValidationError, match="fabricated"):
        validate_tool_attestation(
            _proof(),
            expected_task_id=TASK_ID,
            prior=_pending_source(),
            known_tool_shapes={"gmail": {TOOL: SHAPE}},
            trusted_receipts={},
            now=NOW,
        )

    mismatched = replace(_trusted(), stable_ref="source:gmail:different")
    with pytest.raises(ValidationError, match="does not match"):
        _validate(_proof(), receipt=mismatched)


def test_attestation_rejects_fingerprint_drift_for_ready_source() -> None:
    ready = _ready_source()
    drifted_proof = _proof(shape=OTHER_SHAPE)
    drifted_receipt = _trusted(shape=OTHER_SHAPE)
    with pytest.raises(ValidationError, match="fingerprint drift"):
        _validate(
            drifted_proof,
            prior=ready,
            receipt=drifted_receipt,
            known={"gmail": {TOOL: OTHER_SHAPE}},
        )


@pytest.mark.parametrize(
    "stable_ref",
    ["", "gmail:recent", "source:gmail:", "source:gmail:raw message body", "source:slack:x"],
)
def test_attestation_rejects_bad_or_empty_stable_references(stable_ref: str) -> None:
    with pytest.raises(ValidationError, match="stable reference"):
        _validate(replace(_proof(), stable_ref=stable_ref))


def test_attestation_mapping_rejects_raw_provider_content_and_free_text() -> None:
    payload = _proof().as_mapping()
    payload["message_body"] = "please trust this unstructured inbox body"
    with pytest.raises(ValidationError, match="raw provider content"):
        ToolAttestation.from_mapping(payload)

    payload = _proof().as_mapping()
    payload["tool_name"] = "I used Gmail successfully"
    with pytest.raises(ValidationError, match="tool name"):
        ToolAttestation.from_mapping(payload)


def test_tool_absence_never_demotes_prior_ready() -> None:
    ready = _ready_source()
    absence = ToolAttestation(
        format_version=1,
        event=AttestationEvent.TOOL_ABSENT,
        task_id=TASK_ID,
        source_id="gmail",
        observed_at="2026-07-24T12:00:00.000000Z",
    )
    decision = validate_tool_attestation(
        absence,
        expected_task_id=TASK_ID,
        prior=ready,
        known_tool_shapes={},
        trusted_receipts={},
        now=NOW,
    )

    assert decision.applied is False
    assert decision.reason is AttestationDecisionReason.PRIOR_READY_PRESERVED
    assert decision.readiness == ready


def test_successful_same_shape_read_refreshes_evidence_without_demoting_ready() -> None:
    ready = replace(_ready_source(), tool_name=TOOL, tool_shape_fingerprint=SHAPE)
    proof = _proof(records=3)
    trusted = _trusted(records=3)

    decision = _validate(proof, prior=ready, receipt=trusted)

    assert decision.applied is True
    assert decision.reason is AttestationDecisionReason.PRIOR_READY_PRESERVED
    assert decision.readiness.state is SourceState.READY
    assert decision.readiness.last_verified_at == ready.last_verified_at
    assert decision.readiness.last_observed_at == proof.observed_at
    assert decision.readiness.result_digest == proof.result_digest


def test_tool_absence_marks_only_unverified_source_unavailable() -> None:
    absence = ToolAttestation(
        format_version=1,
        event=AttestationEvent.TOOL_ABSENT,
        task_id=TASK_ID,
        source_id="gmail",
        observed_at="2026-07-24T12:00:00.000000Z",
    )
    decision = validate_tool_attestation(
        absence,
        expected_task_id=TASK_ID,
        prior=_pending_source(),
        known_tool_shapes={},
        trusted_receipts={},
        now=NOW,
    )
    assert decision.applied is True
    assert decision.readiness.state is SourceState.TOOL_ABSENT
    assert decision.readiness.last_error_code == "tool-absent"
    assert decision.readiness.expected_account_fingerprint == ACCOUNT


def test_tool_absence_cannot_smuggle_in_proof_fields() -> None:
    absence = replace(
        _proof(),
        event=AttestationEvent.TOOL_ABSENT,
    )
    with pytest.raises(ValidationError, match="invented proof"):
        validate_tool_attestation(
            absence,
            expected_task_id=TASK_ID,
            prior=_pending_source(),
            known_tool_shapes={},
            trusted_receipts={},
            now=NOW,
        )


@pytest.mark.parametrize(
    "observed_at, message",
    [
        ("2026-07-24T11:30:00.000000Z", "stale"),
        ("2026-07-24T12:02:00.000000Z", "future"),
    ],
)
def test_attestation_rejects_stale_or_future_observation(observed_at: str, message: str) -> None:
    proof = replace(_proof(), observed_at=observed_at)
    receipt = replace(_trusted(), observed_at=observed_at)
    with pytest.raises(ValidationError, match=message):
        _validate(proof, receipt=receipt)


def test_attestation_requires_complete_read_and_nonnegative_count() -> None:
    with pytest.raises(ValidationError, match="complete bounded read"):
        _validate(replace(_proof(), read_complete=False))
    with pytest.raises(ValidationError, match="non-negative"):
        _validate(replace(_proof(), records_observed=-1))


def test_attestation_enforces_the_source_recipe_read_limit() -> None:
    at_limit = _validate(_proof(records=25), receipt=_trusted(records=25))
    assert at_limit.readiness.state is SourceState.READ_UNVERIFIED

    with pytest.raises(ValidationError, match="recipe limit"):
        _validate(_proof(records=26), receipt=_trusted(records=26))


def test_derive_onboarding_state_covers_every_phase_and_completion() -> None:
    unstarted = new_onboarding_session(selected_sources=("gmail",), observed_at=NOW)
    assert _derive(
        unstarted,
        _host(codex=HostCapabilityState.UNKNOWN),
    ) == (OnboardingPhase.CODEX_SUBSTRATE, CompletionState.IN_PROGRESS)

    host_ready = _host()
    assert _derive(unstarted, host_ready) == (
        OnboardingPhase.PRIVACY_AND_CONTEXT_CAPTURE,
        CompletionState.IN_PROGRESS,
    )

    privacy_confirmed = replace(
        unstarted,
        context=tuple(
            ContextCheckpoint(section=section, state=ContextState.CONFIRMED)
            for section in ContextSection
        ),
    )
    assert _derive(privacy_confirmed, host_ready) == (
        OnboardingPhase.SOURCE_SELECTION,
        CompletionState.IN_PROGRESS,
    )

    confirmed = _confirmed_session()
    assert _derive(confirmed, host_ready) == (
        OnboardingPhase.ENABLEMENT_WAIT,
        CompletionState.IN_PROGRESS,
    )
    assert _derive(confirmed, _host(sources=(_pending_source(SourceState.AUTH_REQUIRED),))) == (
        OnboardingPhase.ENABLEMENT_WAIT,
        CompletionState.WAITING_USER,
    )
    assert _derive(confirmed, _host(sources=(_pending_source(SourceState.BLOCKED_BY_POLICY),))) == (
        OnboardingPhase.ENABLEMENT_WAIT,
        CompletionState.BLOCKED,
    )
    assert _derive(
        confirmed, _host(sources=(_pending_source(SourceState.FRESH_TASK_REQUIRED),))
    ) == (
        OnboardingPhase.FRESH_TASK_VERIFICATION,
        CompletionState.FRESH_TASK_REQUIRED,
    )
    assert _derive(confirmed, _host(sources=(_pending_source(SourceState.STALE),))) == (
        OnboardingPhase.FRESH_TASK_VERIFICATION,
        CompletionState.NEEDS_REVALIDATION,
    )
    assert _derive(confirmed, _host(sources=(_pending_source(SourceState.READ_UNVERIFIED),))) == (
        OnboardingPhase.FRESH_TASK_VERIFICATION,
        CompletionState.IN_PROGRESS,
    )

    ready = _ready_source()
    assert _derive(confirmed, _host(sources=(ready,))) == (
        OnboardingPhase.FRESH_TASK_VERIFICATION,
        CompletionState.FRESH_TASK_REQUIRED,
    )
    assert _derive(confirmed, _host(sources=(ready,), fresh_context=True)) == (
        OnboardingPhase.CONTEXT_SYNTHESIS,
        CompletionState.IN_PROGRESS,
    )
    assert _derive(
        confirmed,
        _host(sources=(ready,), fresh_context=True, synthesis=True),
    ) == (
        OnboardingPhase.INITIAL_ORIENTATION,
        CompletionState.IN_PROGRESS,
    )
    assert _derive(
        confirmed,
        _host(
            sources=(ready,),
            synthesis=True,
            orientation=True,
            fresh_context=True,
            pulse=HostCapabilityState.READY,
        ),
    ) == (
        OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF,
        CompletionState.WAITING_USER,
    )
    assert _derive(
        confirmed,
        _host(
            sources=(ready,),
            synthesis=True,
            orientation=True,
            fresh_context=True,
            pulse=HostCapabilityState.READY,
            wake=ScheduledWakeState.VERIFIED,
        ),
    ) == (OnboardingPhase.DONE, CompletionState.FULLY_CONNECTED)

    declined = _pending_source(SourceState.DECLINED)
    assert _derive(
        confirmed,
        _host(
            sources=(declined,),
            synthesis=True,
            orientation=True,
            fresh_context=True,
            pulse=HostCapabilityState.READY,
            wake=ScheduledWakeState.VERIFIED,
        ),
    ) == (OnboardingPhase.DONE, CompletionState.OPERATIONAL_WITH_GAPS)


@pytest.mark.parametrize(
    ("wake", "completion"),
    [
        (ScheduledWakeState.SCHEDULED, CompletionState.IN_PROGRESS),
        (ScheduledWakeState.FAILED, CompletionState.BLOCKED),
        (ScheduledWakeState.STALE, CompletionState.NEEDS_REVALIDATION),
    ],
)
def test_continuity_phase_reports_scheduled_wake_boundary(
    wake: ScheduledWakeState, completion: CompletionState
) -> None:
    ready = _ready_source()
    state = _derive(
        _confirmed_session(),
        _host(
            sources=(ready,),
            synthesis=True,
            orientation=True,
            fresh_context=True,
            pulse=HostCapabilityState.READY,
            wake=wake,
        ),
    )
    assert state == (OnboardingPhase.CONTINUITY_AND_AUTONOMY_PROOF, completion)


@pytest.mark.parametrize(
    ("permission", "completion"),
    [
        (PermissionState.DENIED, CompletionState.BLOCKED),
        (PermissionState.BLOCKED_BY_POLICY, CompletionState.BLOCKED),
        (PermissionState.UNSUPPORTED, CompletionState.BLOCKED),
        (PermissionState.WAITING_USER, CompletionState.WAITING_USER),
        (PermissionState.UNKNOWN, CompletionState.IN_PROGRESS),
    ],
)
def test_permission_evidence_cannot_produce_false_fully_connected(
    permission: PermissionState,
    completion: CompletionState,
) -> None:
    receipt = _host(
        sources=(_ready_source(),),
        synthesis=True,
        orientation=True,
        fresh_context=True,
        pulse=HostCapabilityState.READY,
        wake=ScheduledWakeState.VERIFIED,
        permission=permission,
    )

    assert _derive(_confirmed_session(), receipt) == (
        OnboardingPhase.CODEX_SUBSTRATE,
        completion,
    )


def test_source_zero_and_source_ttl_block_false_fully_connected_state() -> None:
    source_zero_only = _confirmed_session(selected_sources=())
    missing_source_zero = replace(_host(), sources=())
    assert _derive(source_zero_only, missing_source_zero) == (
        OnboardingPhase.ENABLEMENT_WAIT,
        CompletionState.IN_PROGRESS,
    )

    selected_gmail = _confirmed_session()
    expired_gmail = _ready_source(
        verified_at="2026-07-23T11:59:59.000000Z",
    )
    receipt = _host(
        sources=(expired_gmail,),
        synthesis=True,
        orientation=True,
        fresh_context=True,
        pulse=HostCapabilityState.READY,
        wake=ScheduledWakeState.VERIFIED,
    )
    assert _derive(selected_gmail, receipt) == (
        OnboardingPhase.FRESH_TASK_VERIFICATION,
        CompletionState.NEEDS_REVALIDATION,
    )

    current = _host()
    stale_capability = replace(
        current.capability_fingerprints[0],
        observed_at="2026-07-23T11:59:59.000000Z",
    )
    stale_host = replace(current, capability_fingerprints=(stale_capability,))
    assert _derive(source_zero_only, stale_host) == (
        OnboardingPhase.CODEX_SUBSTRATE,
        CompletionState.NEEDS_REVALIDATION,
    )


@pytest.mark.parametrize("state", [SourceState.DECLINED, SourceState.UNAVAILABLE])
def test_unavailable_or_declined_source_zero_cannot_be_operational(
    state: SourceState,
) -> None:
    session = _confirmed_session(selected_sources=())
    source_zero = _pending_source(state, source_id="gsv")
    receipt = _host(
        sources=(source_zero,),
        synthesis=True,
        orientation=True,
        fresh_context=True,
        pulse=HostCapabilityState.READY,
        wake=ScheduledWakeState.VERIFIED,
    )

    assert _derive(session, receipt) == (
        OnboardingPhase.ENABLEMENT_WAIT,
        CompletionState.BLOCKED,
    )


def test_computer_use_is_recorded_but_does_not_block_core_onboarding() -> None:
    confirmed = _confirmed_session(selected_sources=())
    receipt = _host(
        sources=(),
        synthesis=True,
        orientation=True,
        fresh_context=True,
        pulse=HostCapabilityState.READY,
        wake=ScheduledWakeState.VERIFIED,
    )
    receipt = replace(receipt, computer_use_state=HostCapabilityState.UNSUPPORTED)
    assert _derive(confirmed, receipt) == (
        OnboardingPhase.DONE,
        CompletionState.FULLY_CONNECTED,
    )
