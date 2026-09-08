from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.pulse_delivery import PulseDelivery
from continuity_kernel.pulse_reports import PulseReport, PulseReportStore
from continuity_kernel.vault import Vault

PULSE_THREAD = "11111111-1111-4111-8111-111111111111"
CHIEF_THREAD = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
REVISION_A = "a" * 64
REVISION_B = "b" * 64


def _bind_pulse(vault: Vault) -> None:
    vault.create_task(
        identifier="resident-pulse",
        title="Resident Pulse",
        outcome="Keep selected source reports current.",
        status="doing",
        next_actor="agent",
        active_thread_id=PULSE_THREAD,
        refs=(
            "system-role:resident-pulse",
            f"codex-chief-of-staff:{CHIEF_THREAD}",
            "context:resident",
        ),
        observed_at=NOW,
    )


def _wake_report(vault: Vault, *, marker: str = "e") -> PulseReport:
    store = PulseReportStore(vault.root)
    report = store.append(
        event_key="pulse-report:" + marker * 64,
        claim="A source change needs one Pulse decision.",
        uncertainty="The source may omit related material.",
        source_id="slack",
        observed_at=NOW,
        coverage_ref="source:slack",
        coverage_revision=REVISION_A,
        completeness="partial",
        evidence_refs=(f"source:slack@{REVISION_B}",),
        created_at=NOW,
    )
    return store.decide(
        report.identifier,
        expected_revision=report.revision,
        decision="wake",
        reason="The new report requires a resident judgment.",
        decided_at=NOW,
    )


def test_queue_wake_persists_before_exit_zero_and_does_not_claim_consumption(vault: Vault) -> None:
    _bind_pulse(vault)
    report = _wake_report(vault)
    commands: list[tuple[str, ...]] = []
    delivery: PulseDelivery

    def queue(command: tuple[str, ...]) -> int:
        commands.append(command)
        assert delivery.status().wake_requests[0].state == "prepared"
        return 0

    delivery = PulseDelivery(vault, queue_runner=queue, now=lambda: NOW)
    queued = delivery.queue_wake((report.identifier,))

    assert queued.state == "queued"
    assert PulseReportStore(vault.root).show(report.identifier).delivery is None
    assert len(commands) == 1
    assert commands[0][:5] == ("codex", "queue", "--thread", PULSE_THREAD, "--message")
    assert report.identifier in commands[0][5]



def test_uncertain_wake_never_resends_and_becomes_fallback_due(vault: Vault) -> None:
    _bind_pulse(vault)
    report = _wake_report(vault)
    clock = [NOW]
    commands: list[tuple[str, ...]] = []

    def queue(command: tuple[str, ...]) -> int:
        commands.append(command)
        return 1

    delivery = PulseDelivery(vault, queue_runner=queue, now=lambda: clock[0])
    first = delivery.queue_wake((report.identifier,))
    second = delivery.queue_wake((report.identifier,))

    assert first.state == second.state == "uncertain"
    assert len(commands) == 1
    clock[0] += timedelta(minutes=30)
    assert delivery.status().fallback_request_ids == (first.request_id,)


def test_new_wake_reserves_only_reports_not_already_covered(vault: Vault) -> None:
    _bind_pulse(vault)
    first = _wake_report(vault, marker="e")
    second = _wake_report(vault, marker="f")
    commands: list[tuple[str, ...]] = []
    delivery = PulseDelivery(
        vault,
        queue_runner=lambda command: commands.append(tuple(command)) or 0,
        now=lambda: NOW,
    )

    delivery.queue_wake((first.identifier,))
    added = delivery.queue_wake((first.identifier, second.identifier))

    assert added.state == "queued"
    assert added.report_refs == (second.report_ref,)
    assert len(commands) == 2
    assert first.identifier not in commands[1][-1]
    assert second.identifier in commands[1][-1]
    assert delivery.queue_wake((first.identifier, second.identifier)).state == "noop"
    assert len(commands) == 2


def test_integration_queues_curation_only_for_interface_change_and_cas_records_result(
    vault: Vault,
) -> None:
    _bind_pulse(vault)
    report = _wake_report(vault)
    result = vault.create_task(
        identifier="pulse-result",
        title="Pulse result",
        outcome="Record the Pulse conclusion.",
        observed_at=NOW,
    )
    delivery = PulseDelivery(vault, now=lambda: NOW)

    integrated = delivery.integrate(
        report.identifier,
        expected_revision=report.revision,
        pulse_thread_id=PULSE_THREAD,
        result_refs=(f"task:{result.identifier}@{result.revision}",),
        summary="The native task now records the Pulse conclusion.",
        interface_change=True,
    )

    assert integrated.report.delivery is not None
    assert integrated.outbox_id is not None
    assert (
        delivery.integrate(
            report.identifier,
            expected_revision=report.revision,
            pulse_thread_id=PULSE_THREAD,
            result_refs=(f"task:{result.identifier}@{result.revision}",),
            summary="The native task now records the Pulse conclusion.",
            interface_change=True,
        )
        == integrated
    )
    pending = delivery.pending_curation()
    assert pending.remaining == 0
    assert pending.outbox[0].identifier == integrated.outbox_id
    blocked = delivery.record_curation(
        integrated.outbox_id,
        expected_revision=pending.outbox[0].revision,
        state="blocked",
        receipt="Diane has not accepted this event yet.",
        observed_at=NOW + timedelta(minutes=1),
    )
    assert delivery.pending_curation().outbox == (blocked,)
    assert delivery.status().pending_curation == 1
    completed = delivery.record_curation(
        integrated.outbox_id,
        expected_revision=blocked.revision,
        state="completed",
        receipt="Diane applied the selected interface update.",
        observed_at=NOW + timedelta(minutes=2),
    )
    assert completed.state == "completed"
    assert delivery.pending_curation().outbox == ()


def test_source_report_self_result_requires_explicit_no_change(vault: Vault) -> None:
    _bind_pulse(vault)
    report = _wake_report(vault)
    delivery = PulseDelivery(vault, now=lambda: NOW)

    with pytest.raises(ValidationError, match="own result"):
        delivery.integrate(
            report.identifier,
            expected_revision=report.revision,
            pulse_thread_id=PULSE_THREAD,
            result_refs=(report.report_ref,),
            summary="A source report cannot drive a new interface change.",
            interface_change=True,
        )

    integrated = delivery.integrate(
        report.identifier,
        expected_revision=report.revision,
        pulse_thread_id=PULSE_THREAD,
        result_refs=(report.report_ref,),
        summary="No canonical change is required.",
        interface_change=False,
    )
    assert integrated.outbox_id is None
