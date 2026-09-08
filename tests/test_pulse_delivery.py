from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.pulse_delivery import PulseDelivery
from continuity_kernel.pulse_reports import PulseReport, PulseReportStore
from continuity_kernel.vault import Vault

PULSE_THREAD = "11111111-1111-4111-8111-111111111111"
CHIEF_THREAD = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
REVISION_A = "a" * 64
REVISION_B = "b" * 64
TARGET_WORK = "33333333-3333-4333-8333-333333333333"
OTHER_WORK = "44444444-4444-4444-8444-444444444444"


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


def test_wake_backlog_coalesces_without_marking_waiting_reports_delivered(vault: Vault) -> None:
    _bind_pulse(vault)
    first = _wake_report(vault, marker="a")
    second = _wake_report(vault, marker="b")
    third = _wake_report(vault, marker="c")
    commands: list[tuple[str, ...]] = []
    delivery = PulseDelivery(
        vault,
        queue_runner=lambda command: commands.append(tuple(command)) or 0,
        now=lambda: NOW,
    )
    delivery.queue_wake((first.identifier,))
    delivery.queue_wake((second.identifier,))
    assert delivery.queue_wake((third.identifier,)).state == "noop"
    assert len(commands) == 2
    assert delivery.reports.show(third.identifier).delivery is None

    delivery.integrate(
        first.identifier,
        expected_revision=first.revision,
        pulse_thread_id=PULSE_THREAD,
        result_refs=(first.report_ref,),
        summary="The report requires no canonical change.",
        interface_change=False,
    )
    resumed = delivery.queue_wake((second.identifier, third.identifier))
    assert resumed.state == "queued"
    assert resumed.report_refs == (third.report_ref,)
    assert len(commands) == 3


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


def test_targeted_curation_followup_preserves_the_delivered_report(vault: Vault) -> None:
    _bind_pulse(vault)
    report = _wake_report(vault)
    result = vault.create_task(
        identifier="pulse-result",
        title="Pulse result",
        outcome="Record the Pulse conclusion.",
        status="doing",
        active_thread_id=TARGET_WORK,
        observed_at=NOW,
    )
    delivery = PulseDelivery(vault, now=lambda: NOW)
    normal = delivery.integrate(
        report.identifier,
        expected_revision=report.revision,
        pulse_thread_id=PULSE_THREAD,
        result_refs=(f"task:{result.identifier}@{result.revision}",),
        summary="The native task records the Pulse conclusion.",
        interface_change=False,
    )
    delivered = normal.report
    assert delivered.delivery is not None
    delivered_history = delivered.history

    refreshed = vault.update_task(
        result.identifier,
        expected_revision=result.revision,
        outcome="Record the refined Pulse conclusion.",
        observed_at=NOW + timedelta(minutes=1),
    )
    fresh_result = f"task:{refreshed.identifier}@{refreshed.revision}"

    with pytest.raises(ValidationError, match="requires an interface change"):
        delivery.integrate(
            report.identifier,
            expected_revision=delivered.revision,
            pulse_thread_id=PULSE_THREAD,
            result_refs=(fresh_result,),
            summary="A target cannot accompany a no-change integration.",
            interface_change=False,
            target_desktop_work_id=TARGET_WORK,
        )
    with pytest.raises(ValidationError, match="must match"):
        delivery.integrate(
            report.identifier,
            expected_revision=delivered.revision,
            pulse_thread_id=PULSE_THREAD,
            result_refs=(fresh_result,),
            summary="The target must be the result task's active desktop work.",
            interface_change=True,
            target_desktop_work_id=OTHER_WORK,
        )

    unrelated = vault.create_task(
        identifier="unrelated-result",
        title="Unrelated result",
        outcome="This task was not part of the original result.",
        status="doing",
        active_thread_id=OTHER_WORK,
        observed_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ValidationError, match="only to the original"):
        delivery.integrate(
            report.identifier,
            expected_revision=delivered.revision,
            pulse_thread_id=PULSE_THREAD,
            result_refs=(f"task:{unrelated.identifier}@{unrelated.revision}",),
            summary="An unrelated task cannot create a curation follow-up.",
            interface_change=True,
            target_desktop_work_id=OTHER_WORK,
        )
    with pytest.raises(ConflictError):
        delivery.integrate(
            report.identifier,
            expected_revision=report.revision,
            pulse_thread_id=PULSE_THREAD,
            result_refs=(fresh_result,),
            summary="A stale report revision cannot create a curation follow-up.",
            interface_change=True,
            target_desktop_work_id=TARGET_WORK,
        )

    followup = delivery.integrate(
        report.identifier,
        expected_revision=delivered.revision,
        pulse_thread_id=PULSE_THREAD,
        result_refs=(fresh_result,),
        summary="Curate the refined Pulse conclusion in the selected desktop work.",
        interface_change=True,
        target_desktop_work_id=TARGET_WORK,
    )

    assert followup.report.delivery == delivered.delivery
    assert followup.report.history == delivered_history
    assert followup.outbox_id is not None
    pending = delivery.pending_curation()
    assert pending.remaining == 0
    assert pending.outbox[0].target_desktop_work_id == TARGET_WORK
    assert pending.outbox[0].curation_followup is True
    snapshot = delivery._snapshot()
    assert len(snapshot.integrations) == 2
    followup_receipt = snapshot.integrations[-1]
    assert followup_receipt.curation_followup is True
    assert followup_receipt.primary_result_ref == delivered.delivery.result_ref
    assert (
        delivery.integrate(
            report.identifier,
            expected_revision=delivered.revision,
            pulse_thread_id=PULSE_THREAD,
            result_refs=(fresh_result,),
            summary="Curate the refined Pulse conclusion in the selected desktop work.",
            interface_change=True,
            target_desktop_work_id=TARGET_WORK,
        )
        == followup
    )
    assert len(delivery._snapshot().integrations) == 2
