from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
    parse_connection_id,
)
from continuity_kernel.pulse_codex import LunaTurnResult
from continuity_kernel.pulse_delivery import PulseDelivery
from continuity_kernel.pulse_reports import PulseReportStore
from continuity_kernel.pulse_runtime import PulseRuntime, _source_access_signature
from continuity_kernel.pulse_sources import AcquiredSourceWindow, AcquisitionReceipt
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
