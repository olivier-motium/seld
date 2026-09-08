from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
    parse_connection_id,
)
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.pulse_codex import LunaTurnResult
from continuity_kernel.pulse_delivery import PulseDelivery
from continuity_kernel.pulse_reports import PulseReportStore
from continuity_kernel.pulse_runtime import PulseRuntime, _source_access_signature
from continuity_kernel.pulse_sources import AcquiredSourceWindow, AcquisitionReceipt
from continuity_kernel.source_state import source_fingerprint
from continuity_kernel.vault import Vault

PULSE_THREAD = "11111111-1111-4111-8111-111111111111"
CHIEF_THREAD = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
FINGERPRINT = "e" * 64


class FakeSourceAdapter:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.acquisitions = 0
        self.commits: list[bool] = []
        self.releases = 0

    def acquire(self, source: str) -> AcquiredSourceWindow:
        assert source == "slack"
        self.acquisitions += 1
        return AcquiredSourceWindow(
            source_id="slack",
            items=({"kind": "changed"},),
            fingerprint=FINGERPRINT,
            coverage={"scope": "bounded-test-window"},
            completeness="partial",
            observed_at="2026-09-08T12:00:00Z",
            status="success",
            result="success",
            changed=True,
            empty=False,
            error_code=None,
            source_revision=self.vault.get_source_snapshot().revision,
            receipt=AcquisitionReceipt(
                identifier=f"test-receipt-{self.acquisitions}",
                kind="connector",
                source_revision=self.vault.get_source_snapshot().revision,
                prior_observation=None,
                record=None,
            ),
        )

    def commit(
        self,
        window: AcquiredSourceWindow,
        *,
        report_ref: str,
        report_revision: str,
        replay: bool = False,
    ) -> Mapping[str, object]:
        identifier = report_ref.removeprefix("source-report:").partition("@")[0]
        report = PulseReportStore(self.vault.root).show(identifier)
        assert report.revision == report_revision
        assert report.event_key == window.report_event_key
        self.commits.append(replay)
        return {"revision": window.source_revision}

    def release(self, window: AcquiredSourceWindow) -> None:
        assert window.source_id == "slack"
        self.releases += 1


@dataclass
class FakeLunaSession:
    role: str
    turns: list[str]
    alive: bool = True

    @property
    def configuration(self) -> dict[str, object]:
        return {"model": "fake-luna", "role": self.role}

    async def turn(self, prompt: str, *, output_schema: Mapping[str, object]) -> LunaTurnResult:
        del output_schema
        self.turns.append(self.role)
        if self.role == "relevance":
            reports = json.loads(prompt)["reports"]
            output = {
                "decisions": [
                    {
                        "report_id": report["report_id"],
                        "decision": "wake",
                        "reason": "The changed source needs Pulse integration.",
                    }
                    for report in reports
                ]
            }
        else:
            output = {
                "claim": "A source change needs one Pulse decision.",
                "uncertainty": "The bounded source coverage is partial.",
            }
        return LunaTurnResult(
            thread_id=f"fake-{self.role}",
            turn_id=f"turn-{len(self.turns)}",
            output=output,
            usage=None,
            compactions=0,
            elapsed_seconds=0.0,
        )

    async def close(self) -> None:
        self.alive = False


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
        ),
        observed_at=NOW,
    )


def test_delivery_reconsiders_waiting_reports_without_a_new_source_event(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision, sources=("slack",)
    )
    store = PulseReportStore(vault.root)
    report = store.append(
        event_key=f"pulse-report:{FINGERPRINT}",
        claim="A retained change awaits Pulse.",
        uncertainty="The source read is partial.",
        source_id="slack",
        observed_at=NOW,
        coverage_ref="source:slack",
        coverage_revision="a" * 64,
        completeness="partial",
    )
    store.decide(
        report.identifier, expected_revision=report.revision, decision="wake", reason="Useful."
    )
    calls: list[tuple[str, ...]] = []

    async def exercise() -> None:
        resumed = asyncio.Event()
        loop = asyncio.get_running_loop()

        def queue(ids: Sequence[str]) -> None:
            calls.append(tuple(ids))
            if len(calls) == 2:
                loop.call_soon_threadsafe(resumed.set)

        async def no_curation() -> None:
            pass

        runtime = PulseRuntime(vault, wake_handler=queue)
        monkeypatch.setattr(runtime, "curate_pending", no_curation)
        task = asyncio.create_task(runtime._curation_loop())
        try:
            await asyncio.wait_for(resumed.wait(), timeout=10)
            assert not runtime._arrivals.is_set()
        finally:
            runtime._stop.set()
            await task

    asyncio.run(exercise())
    assert calls == [(report.identifier,), (report.identifier,)]
    assert store.show(report.identifier).delivery is None


def test_runtime_recovers_a_committed_report_without_another_model_or_actual_wake(
    vault: Vault,
) -> None:
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("slack", "gsv"),
    )
    _bind_pulse(vault)
    adapter = FakeSourceAdapter(vault)
    turns: list[str] = []
    queue_calls: list[tuple[str, ...]] = []

    def factory(*, instructions: str, **_values: object) -> FakeLunaSession:
        role = (
            "relevance" if instructions.startswith("You are the separate relevance") else "source"
        )
        return FakeLunaSession(role, turns)

    delivery = PulseDelivery(
        vault,
        queue_runner=lambda command: queue_calls.append(tuple(command)) or 0,
        now=lambda: NOW,
    )
    runtime = PulseRuntime(
        vault,
        session_factory=factory,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        wake_handler=delivery.queue_wake,
    )

    state = asyncio.run(runtime.run(once=True))
    assert state["monitored_sources"] == ["slack"]
    assert state["unmonitored_sources"] == ["gsv"]

    reports = PulseReportStore(vault.root).recent(source_id="slack").reports
    assert len(reports) == 1
    assert reports[0].decision is not None and reports[0].decision.decision == "wake"
    assert adapter.commits == [False]
    assert turns == ["source", "relevance"]
    assert len(queue_calls) == 1

    # Simulate a restart after report commit but before the runtime projection
    # retained its source fingerprint. The durable report is the replay key.
    runtime.state.change(lambda state: state["sources"].pop("slack", None))
    restarted = PulseRuntime(
        vault,
        session_factory=factory,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        wake_handler=delivery.queue_wake,
    )
    asyncio.run(restarted.run(once=True))

    assert adapter.commits == [False, True]
    assert adapter.releases == 2
    assert PulseReportStore(vault.root).recent(source_id="slack").reports == reports
    assert turns == ["source", "relevance"]
    assert len(queue_calls) == 1


def test_runtime_replays_a_report_outside_the_small_recent_context_without_a_model(
    vault: Vault,
) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))
    adapter = FakeSourceAdapter(vault)
    reports = PulseReportStore(vault.root)
    source_revision = vault.get_source_snapshot().revision
    replay = reports.append(
        event_key=f"pulse-report:{FINGERPRINT}",
        claim="The original source report is durable.",
        uncertainty="The source window is partial.",
        source_id="slack",
        observed_at=NOW - timedelta(minutes=6),
        coverage_ref="source:slack",
        coverage_revision=source_revision,
        completeness="partial",
        created_at=NOW - timedelta(minutes=6),
    )
    for index in range(5):
        reports.append(
            event_key=f"pulse-report:{index:064x}",
            claim=f"Newer report {index}.",
            uncertainty="The source window is partial.",
            source_id="slack",
            observed_at=NOW - timedelta(minutes=5 - index),
            coverage_ref="source:slack",
            coverage_revision=source_revision,
            completeness="partial",
            created_at=NOW - timedelta(minutes=5 - index),
        )

    def no_source_model(*, instructions: str, **_values: object) -> FakeLunaSession:
        del instructions
        raise AssertionError("A durable report replay must not start a source model turn")

    runtime = PulseRuntime(
        vault,
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=no_source_model,  # type: ignore[arg-type]
    )
    asyncio.run(runtime.process_source("slack"))

    assert adapter.commits == [True]
    assert adapter.releases == 1
    assert PulseReportStore(vault.root).show(replay.identifier) == replay
    assert runtime.state.read()["sources"]["slack"]["report_id"] == replay.identifier


def test_investigation_replays_an_older_child_before_starting_another_model(
    vault: Vault,
) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))
    reports = PulseReportStore(vault.root)
    source_revision = vault.get_source_snapshot().revision
    parent = reports.append(
        event_key="pulse-report:" + "a" * 64,
        claim="The source needs a bounded follow-up.",
        uncertainty="The original source window is partial.",
        source_id="slack",
        observed_at=NOW - timedelta(minutes=20),
        coverage_ref="source:slack",
        coverage_revision=source_revision,
        completeness="partial",
        created_at=NOW - timedelta(minutes=20),
    )
    investigation = reports.decide(
        parent.identifier,
        expected_revision=parent.revision,
        decision="investigate",
        reason="Check the retained source context once.",
        decided_at=NOW - timedelta(minutes=19),
    )
    child_event_key = "pulse-report:" + sha256_bytes(
        (investigation.event_key + investigation.decision.reason).encode()
    )
    child = reports.append(
        event_key=child_event_key,
        claim="The original follow-up is already durable.",
        uncertainty="No broader coverage was added.",
        source_id="slack",
        observed_at=NOW - timedelta(minutes=10),
        coverage_ref="source:slack",
        coverage_revision=source_revision,
        completeness="partial",
        causal_key=f"source-report:{sha256_bytes(investigation.identifier.encode())}",
        created_at=NOW - timedelta(minutes=10),
    )
    for index in range(8):
        reports.append(
            event_key=f"pulse-report:{index:064x}",
            claim=f"Newer report {index}.",
            uncertainty="The source window is partial.",
            source_id="slack",
            observed_at=NOW - timedelta(minutes=8 - index),
            coverage_ref="source:slack",
            coverage_revision=source_revision,
            completeness="partial",
            created_at=NOW - timedelta(minutes=8 - index),
        )

    def no_follow_up_model(*_args: object, **_values: object) -> FakeLunaSession:
        raise AssertionError("A durable investigation child must not start a model turn")

    runtime = PulseRuntime(vault, session_factory=no_follow_up_model)  # type: ignore[arg-type]
    asyncio.run(runtime.investigate_pending())

    delivered = PulseReportStore(vault.root).show(investigation.identifier)
    assert delivered.delivery is not None
    assert delivered.delivery.result_ref == child.report_ref


def test_known_fingerprint_replays_the_latest_report_without_another_model_or_wake(
    vault: Vault,
) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))
    _bind_pulse(vault)
    adapter = FakeSourceAdapter(vault)
    turns: list[str] = []
    queue_calls: list[tuple[str, ...]] = []

    def factory(*, instructions: str, **_values: object) -> FakeLunaSession:
        role = (
            "relevance" if instructions.startswith("You are the separate relevance") else "source"
        )
        return FakeLunaSession(role, turns)

    def queue_runner(command: Sequence[str]) -> int:
        queue_calls.append(tuple(command))
        return 0

    delivery = PulseDelivery(
        vault,
        queue_runner=queue_runner,
        now=lambda: NOW,
    )
    runtime = PulseRuntime(
        vault,
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
        wake_handler=delivery.queue_wake,
    )
    asyncio.run(runtime.run(once=True))

    report_store = PulseReportStore(vault.root)
    report = report_store.recent(source_id="slack").reports[0]
    task = vault.get_task("resident-pulse")
    delivered = report_store.record_delivery(
        report.identifier,
        expected_revision=report.revision,
        result_ref=f"{task.identifier}@{task.revision}",
        delivered_at=NOW,
    )
    runtime._source_state(
        "slack",
        {
            "coverage_status": "failure",
            "error_code": "auth_required",
            "failure_code": "checkpoint_validation_error",
            "incident_signature": "stale-incident-signature",
        },
    )

    asyncio.run(runtime.run(once=True))

    source_state = runtime.state.read()["sources"]["slack"]
    assert adapter.commits == [False, True]
    assert adapter.releases == 2
    assert source_state["report_id"] == delivered.identifier
    assert source_state["coverage_status"] == "success"
    assert source_state["error_code"] is None
    assert source_state["failure_code"] is None
    assert source_state["incident_signature"] == _source_access_signature(vault, "slack")
    assert report_store.show(delivered.identifier) == delivered
    assert turns == ["source", "relevance"]
    assert len(queue_calls) == 1


@pytest.mark.parametrize(
    ("failure", "failure_code"),
    [
        (ValidationError("Pulse report receipt is invalid"), "report_receipt_invalid"),
        (ValidationError("source evidence digests contain a duplicate"), "duplicate_evidence_refs"),
        (ContinuityError("local source content changed after polling"), "local_content_changed"),
    ],
)
def test_checkpoint_failure_keeps_a_fixed_content_free_code(
    vault: Vault,
    failure: Exception,
    failure_code: str,
) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))
    _bind_pulse(vault)

    class CheckpointFailureAdapter(FakeSourceAdapter):
        def commit(
            self,
            window: AcquiredSourceWindow,
            *,
            report_ref: str,
            report_revision: str,
            replay: bool = False,
        ) -> Mapping[str, object]:
            del window, report_ref, report_revision
            self.commits.append(replay)
            raise failure

    turns: list[str] = []
    runtime = PulseRuntime(
        vault,
        adapter=CheckpointFailureAdapter(vault),  # type: ignore[arg-type]
        session_factory=lambda *, instructions, **_values: FakeLunaSession(
            "relevance" if instructions.startswith("You are the separate relevance") else "source",
            turns,
        ),
        wake_handler=lambda _ids: None,
    )

    asyncio.run(runtime.run(once=True))

    source_state = runtime.state.read()["sources"]["slack"]
    assert source_state["state"] == "unavailable"
    assert source_state["failed_stage"] == "source_checkpoint"
    assert source_state["failure_type"] == type(failure).__name__
    assert source_state["failure_code"] == failure_code


def test_checkpoint_validation_records_only_its_local_exception_origin(vault: Vault) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))
    _bind_pulse(vault)

    class DynamicValidationAdapter(FakeSourceAdapter):
        def commit(
            self,
            window: AcquiredSourceWindow,
            *,
            report_ref: str,
            report_revision: str,
            replay: bool = False,
        ) -> Mapping[str, object]:
            del window, report_ref, report_revision
            self.commits.append(replay)
            source_fingerprint("x" * 10_000, "tool binding")
            raise AssertionError("source_fingerprint must reject the oversized binding")

    turns: list[str] = []
    runtime = PulseRuntime(
        vault,
        adapter=DynamicValidationAdapter(vault),  # type: ignore[arg-type]
        session_factory=lambda *, instructions, **_values: FakeLunaSession(
            "relevance" if instructions.startswith("You are the separate relevance") else "source",
            turns,
        ),
        wake_handler=lambda _ids: None,
    )

    asyncio.run(runtime.run(once=True))

    source_state = runtime.state.read()["sources"]["slack"]
    assert source_state["failure_code"] == "checkpoint_validation_error"
    assert source_state["failure_origin"].startswith(
        "continuity_kernel.source_state:source_fingerprint:"
    )
    assert "tool binding" not in source_state["failure_origin"]


def test_unavailable_source_creates_a_gap_report_without_starting_a_source_model(
    vault: Vault,
) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))
    _bind_pulse(vault)

    class UnavailableAdapter(FakeSourceAdapter):
        def acquire(self, source: str) -> AcquiredSourceWindow:
            return replace(
                super().acquire(source),
                items=(),
                result="failure",
                status="failure",
                completeness="unavailable",
                error_code="identity_mismatch",
            )

    turns: list[str] = []

    def factory(*, instructions: str, **_values: object) -> FakeLunaSession:
        assert instructions.startswith("You are the separate relevance")
        return FakeLunaSession("relevance", turns)

    runtime = PulseRuntime(
        vault,
        sources=("slack",),
        adapter=UnavailableAdapter(vault),  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
        wake_handler=lambda _ids: None,
    )
    asyncio.run(runtime.run(once=True))
    report = PulseReportStore(vault.root).recent(source_id="slack").reports[0]
    assert report.completeness == "unavailable"
    assert turns == ["relevance"]


def test_verified_connection_repair_resumes_an_incident_skipped_source(vault: Vault) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))
    _bind_pulse(vault)
    connection = ConnectionMetadata(
        connection_id=parse_connection_id("con-" + "a" * 32),
        provider="slack",
        source_ids=("slack",),
        credential_kind=CredentialKind.BEARER,
        account=AccountMetadata(),
        scopes=(),
        client=ClientMetadata(kind=ClientKind.EXTERNAL),
        health=ConnectionHealth.REAUTHORIZATION_REQUIRED,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
        version=1,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=connection,
        observed_at=NOW - timedelta(days=1),
    )
    adapter = FakeSourceAdapter(vault)
    turns: list[str] = []

    def factory(*, instructions: str, **_values: object) -> FakeLunaSession:
        role = (
            "relevance" if instructions.startswith("You are the separate relevance") else "source"
        )
        return FakeLunaSession(role, turns)

    runtime = PulseRuntime(
        vault,
        adapter=adapter,
        session_factory=factory,  # type: ignore[arg-type]
        wake_handler=lambda _ids: None,
    )
    runtime._source_state(
        "slack",
        {
            "error_code": "auth_required",
            "incident_signature": _source_access_signature(vault, "slack"),
        },
    )
    asyncio.run(runtime.run(once=True))
    assert adapter.acquisitions == 0
    assert turns == []
    vault.mark_connection_health(
        expected_revision=vault.get_connection_snapshot().revision,
        connection_id=connection.connection_id,
        health=ConnectionHealth.READY,
        verified=True,
        observed_at=NOW - timedelta(days=1) + timedelta(seconds=1),
    )
    asyncio.run(runtime.run(once=True))
    assert adapter.acquisitions == 1
    assert turns == ["source", "relevance"]


def test_complete_empty_source_read_commits_without_a_model_or_blank_uncertainty(
    vault: Vault,
) -> None:
    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=("slack",))

    class CompleteEmptyAdapter(FakeSourceAdapter):
        def acquire(self, source: str) -> AcquiredSourceWindow:
            return replace(
                super().acquire(source),
                items=(),
                result="explicit_empty",
                empty=True,
                completeness="complete",
                status="empty",
            )

    def no_model(*_args: object, **_values: object) -> FakeLunaSession:
        raise AssertionError("An explicit empty source read must not start a model turn")

    adapter = CompleteEmptyAdapter(vault)
    runtime = PulseRuntime(
        vault,
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=no_model,  # type: ignore[arg-type]
    )
    asyncio.run(runtime.process_source("slack"))

    report = PulseReportStore(vault.root).recent(source_id="slack").reports[0]
    assert report.completeness == "complete"
    assert report.uncertainty == "The bounded read is complete."
    assert adapter.commits == [False]
    assert adapter.releases == 1
