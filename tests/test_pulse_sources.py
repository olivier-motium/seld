from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from continuity_kernel.connector_auth import ConnectionHealth
from continuity_kernel.errors import ConflictError, NotFoundError, SetupError, ValidationError
from continuity_kernel.pulse_reports import PulseReportStore
from continuity_kernel.pulse_sources import AcquiredSourceWindow, PulseSourceAdapter
from continuity_kernel.vault import Vault

REVISION_A = "a" * 64
REVISION_B = "b" * 64
REPORT_REVISION = "d" * 64
OBSERVED = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@dataclass
class FakeSourceSnapshot:
    selected_sources: tuple[str, ...]
    revision: str = REVISION_A
    observations: dict[str, object] = field(default_factory=dict)

    def observation(self, source_id: str) -> object | None:
        return self.observations.get(source_id)


@dataclass(frozen=True)
class FakeConnection:
    connection_id: str
    source_ids: tuple[str, ...]
    health: ConnectionHealth = ConnectionHealth.READY


@dataclass
class FakeConnectionSnapshot:
    connections: tuple[FakeConnection, ...]

    def connection(self, connection_id: str) -> FakeConnection | None:
        return next(
            (item for item in self.connections if item.connection_id == connection_id), None
        )


class FakeVault:
    def __init__(self, *sources: str) -> None:
        self.source_snapshot = FakeSourceSnapshot(tuple(sources))
        self.connection_snapshot = FakeConnectionSnapshot(
            tuple(FakeConnection("conn-1", (source,)) for source in sources)
        )
        self.record_calls: list[dict[str, object]] = []

    def get_source_snapshot(self) -> FakeSourceSnapshot:
        return self.source_snapshot

    def get_connection_snapshot(self) -> FakeConnectionSnapshot:
        return self.connection_snapshot

    def record_source_observation(self, **values: object) -> dict[str, object]:
        self.record_calls.append(values)
        self.source_snapshot.revision = REVISION_B
        return {"revision": REVISION_B}


@dataclass(frozen=True)
class FakeReport:
    identifier: str
    revision: str
    event_key: str
    source_id: str
    observed_at: str
    coverage_ref: str
    coverage_revision: str
    completeness: str

    @property
    def report_ref(self) -> str:
        return f"source-report:{self.identifier}@{self.revision}"


class FakeReportStore:
    def __init__(self) -> None:
        self.reports: dict[str, FakeReport] = {}

    def add(self, window: AcquiredSourceWindow) -> tuple[str, str]:
        identifier = "12345678-1234-1234-1234-123456789abc"
        report = FakeReport(
            identifier=identifier,
            revision=REPORT_REVISION,
            event_key=window.report_event_key,
            source_id=window.source_id,
            observed_at=window.observed_at,
            coverage_ref=window.coverage_ref,
            coverage_revision=window.source_revision,
            completeness=window.completeness,
        )
        self.reports[identifier] = report
        return report.report_ref, REPORT_REVISION

    def show(self, report_id: str) -> FakeReport:
        return self.reports[report_id]


class FakeLocalDelivery:
    def __init__(self) -> None:
        self.acknowledgements: list[dict[str, object]] = []

    def poll(self, source: str, *, limit: int) -> dict[str, object]:
        assert source == "whatsapp"
        assert limit == 100
        return {
            "source": source,
            "messages": [{"body": "PRIVATE TEST MESSAGE"}],
            "complete": False,
            "observed_at": "2026-09-08T12:01:00Z",
            "delivery": {"token": "opaque-local-token", "source_revision": REVISION_A},
        }

    def acknowledge(self, source: str, **values: object) -> dict[str, object]:
        assert source == "whatsapp"
        self.acknowledgements.append(values)
        return {"source_revision": REVISION_B, "acknowledged": True}


class UnavailableLocalDelivery:
    def poll(self, source: str, *, limit: int) -> dict[str, object]:
        assert source == "whatsapp"
        assert limit == 100
        raise SetupError("synthetic local tool is unavailable")


class UnavailableDiscordBridge:
    def poll(self, *, limit: int) -> dict[str, object]:
        assert limit == 25
        raise ValidationError("synthetic Discord tool is unavailable")


def _connector_reader(*args: object, **kwargs: object) -> dict[str, object]:
    assert kwargs["source_id"] == "gmail"
    assert kwargs["connection_id"] == "conn-1"
    vault = cast(FakeVault, args[0])
    return {
        "source": "gmail",
        "sourceRevision": vault.get_source_snapshot().revision,
        "result": "success",
        "items": [{"snippet": "PRIVATE TEST MESSAGE"}],
        "record": {
            "source": "gmail",
            "result": "success",
            "coveredThrough": "2026-09-08T12:00:00Z",
            "completeness": "partial",
            "accountBinding": "account-bound-in-memory",
            "toolBinding": "reader-bound-in-memory",
            "evidenceRefs": ["safe-ref"],
        },
    }


def _adapter(
    vault: FakeVault,
    reports: FakeReportStore,
    *,
    local_delivery: FakeLocalDelivery | None = None,
    now: datetime = OBSERVED,
) -> PulseSourceAdapter:
    return PulseSourceAdapter(
        vault,  # type: ignore[arg-type]
        report_store=reports,
        connector_reader=_connector_reader,
        local_delivery=local_delivery,  # type: ignore[arg-type]
        now=lambda: now,
    )


def _report_for(reports: FakeReportStore, window: AcquiredSourceWindow) -> tuple[str, str]:
    return reports.add(window)


def test_unavailable_source_subclasses_keep_their_incident_classification() -> None:
    def unavailable_connector(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise NotFoundError("synthetic connector is unavailable")

    connector = PulseSourceAdapter(
        FakeVault("gmail"),  # type: ignore[arg-type]
        connector_reader=unavailable_connector,
        now=lambda: OBSERVED,
    ).acquire("gmail")
    local = PulseSourceAdapter(
        FakeVault("whatsapp"),  # type: ignore[arg-type]
        local_delivery=UnavailableLocalDelivery(),  # type: ignore[arg-type]
        now=lambda: OBSERVED,
    ).acquire("whatsapp")
    discord = PulseSourceAdapter(
        FakeVault("discord"),  # type: ignore[arg-type]
        discord_bridge=UnavailableDiscordBridge(),  # type: ignore[arg-type]
        now=lambda: OBSERVED,
    ).acquire("discord")

    assert (connector.result, connector.error_code) == ("failure", "auth_required")
    assert (local.result, local.error_code) == ("failure", "tool_absent")
    assert (discord.result, discord.error_code) == ("failure", "tool_absent")


def test_account_mismatch_becomes_a_content_free_failure_checkpoint(tmp_path: Path) -> None:
    observed = datetime.now(UTC).replace(microsecond=0)
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pulse source account lease")
    selected = vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("slack",),
    )
    before = vault.record_source_observation(
        expected_revision=selected["revision"],
        source_id="slack",
        actor_ref="system-role:source-test",
        result="success",
        covered_through=observed.isoformat().replace("+00:00", "Z"),
        completeness="partial",
        account_binding="synthetic-prior-account",
        tool_binding="synthetic-reader",
        evidence_refs=("synthetic-prior-evidence",),
        observed_at=observed,
    )
    prior = vault.get_source_snapshot().observation("slack")
    assert prior is not None

    def mismatched_reader(vault: Vault, **_kwargs: object) -> dict[str, object]:
        return {
            "source": "slack",
            "sourceRevision": vault.get_source_snapshot().revision,
            "result": "success",
            "items": [{"summary": "synthetic"}],
            "record": {
                "source": "slack",
                "result": "success",
                "coveredThrough": observed.isoformat().replace("+00:00", "Z"),
                "completeness": "partial",
                "accountBinding": "synthetic-current-account",
                "toolBinding": "synthetic-reader",
                "evidenceRefs": ["synthetic-current-evidence"],
            },
        }

    adapter = PulseSourceAdapter(
        vault,
        report_store=PulseReportStore(vault.root),
        connector_reader=mismatched_reader,
        now=lambda: observed,
    )
    adapter._connection_id = lambda _source: "conn-1"  # type: ignore[assignment]

    window = adapter.acquire("slack")
    assert window.items == ()
    assert window.result == "failure"
    assert window.error_code == "identity_mismatch"
    assert window.receipt.record == {
        "source": "slack",
        "result": "failure",
        "errorCode": "identity_mismatch",
    }
    assert PulseReportStore(vault.root).list_pending().reports == ()

    reports = PulseReportStore(vault.root)
    report = reports.append(
        event_key=window.report_event_key,
        claim="The selected source identity does not match its recorded account.",
        uncertainty="No provider items were retained.",
        source_id=window.source_id,
        observed_at=window.observed_at,
        coverage_ref=window.coverage_ref,
        coverage_revision=window.source_revision,
        completeness=window.completeness,
    )
    adapter.commit(window, report_ref=report.report_ref, report_revision=report.revision)
    adapter.release(window)

    after = vault.get_source_snapshot()
    observation = after.observation("slack")
    assert observation is not None
    assert after.revision != before["revision"]
    assert observation.result.value == "failure"
    assert observation.error_code == "identity_mismatch"
    assert observation.account_fingerprint == prior.account_fingerprint


def test_connector_window_is_transient_stable_and_requires_report_readback() -> None:
    vault = FakeVault("gmail")
    reports = FakeReportStore()
    adapter = _adapter(vault, reports)

    first = adapter.acquire("gmail")
    repeated = adapter.acquire("gmail")

    assert first.fingerprint == repeated.fingerprint
    assert "PRIVATE TEST MESSAGE" not in repr(first)
    assert not vault.record_calls

    report_ref, report_revision = _report_for(reports, first)
    committed = adapter.commit(
        first,
        report_ref=report_ref,
        report_revision=report_revision,
    )

    assert committed["revision"] == REVISION_B
    assert vault.record_calls[0]["canonical_result_refs"] == (report_ref,)
    assert vault.record_calls[0]["expected_revision"] == REVISION_A

    unchanged = adapter.acquire("gmail")
    assert unchanged.changed is False
    adapter.release(first)
    with pytest.raises(ConflictError, match="active acquisition"):
        adapter.commit(first, report_ref=report_ref, report_revision=report_revision)


def test_explicit_empty_fingerprint_ignores_a_later_poll_attempt() -> None:
    def empty_reader(*args: object, **_kwargs: object) -> dict[str, object]:
        vault = cast(FakeVault, args[0])
        return {
            "source": "gmail",
            "sourceRevision": vault.get_source_snapshot().revision,
            "result": "explicit_empty",
            "items": [],
            "record": {
                "source": "gmail",
                "result": "explicit_empty",
                "coveredThrough": "2026-09-08T12:00:00Z",
                "completeness": "complete",
                "accountBinding": "account-bound-in-memory",
                "toolBinding": "reader-bound-in-memory",
                "evidenceRefs": [],
            },
        }

    vault = FakeVault("gmail")
    reports = FakeReportStore()
    first = PulseSourceAdapter(
        vault,  # type: ignore[arg-type]
        report_store=reports,
        connector_reader=empty_reader,
        now=lambda: OBSERVED,
    ).acquire("gmail")
    later = PulseSourceAdapter(
        vault,  # type: ignore[arg-type]
        report_store=reports,
        connector_reader=empty_reader,
        now=lambda: OBSERVED.replace(hour=13),
    ).acquire("gmail")

    assert first.empty and later.empty
    assert first.observed_at != later.observed_at
    assert first.fingerprint == later.fingerprint


def test_commit_rejects_a_report_that_does_not_attest_the_window() -> None:
    vault = FakeVault("gmail")
    reports = FakeReportStore()
    adapter = _adapter(vault, reports)
    window = adapter.acquire("gmail")
    report_ref, report_revision = _report_for(reports, window)
    report = reports.show("12345678-1234-1234-1234-123456789abc")
    reports.reports[report.identifier] = FakeReport(
        **{**report.__dict__, "event_key": "pulse-report:" + "e" * 64}
    )

    with pytest.raises(ConflictError, match="does not attest"):
        adapter.commit(window, report_ref=report_ref, report_revision=report_revision)

    assert not vault.record_calls


def test_unrelated_source_state_change_rebases_only_an_unchanged_source_lease() -> None:
    vault = FakeVault("gmail", "slack")
    reports = FakeReportStore()
    adapter = _adapter(vault, reports)
    window = adapter.acquire("gmail")
    report_ref, report_revision = _report_for(reports, window)
    vault.source_snapshot.revision = REVISION_B

    adapter.commit(window, report_ref=report_ref, report_revision=report_revision)

    assert vault.record_calls[0]["expected_revision"] == REVISION_B


def test_replay_commits_an_already_durable_report_after_restart_without_a_model_turn() -> None:
    vault = FakeVault("gmail", "slack")
    reports = FakeReportStore()
    original = _adapter(vault, reports, now=OBSERVED).acquire("gmail")
    report_ref, report_revision = _report_for(reports, original)
    vault.source_snapshot.revision = REVISION_B

    restarted = _adapter(vault, reports, now=OBSERVED.replace(hour=13))
    replay = restarted.acquire("gmail")

    assert replay.fingerprint == original.fingerprint
    assert replay.observed_at != original.observed_at
    with pytest.raises(ConflictError, match="does not attest"):
        restarted.commit(replay, report_ref=report_ref, report_revision=report_revision)

    restarted.commit(
        replay,
        report_ref=report_ref,
        report_revision=report_revision,
        replay=True,
    )

    assert vault.record_calls[0]["expected_revision"] == REVISION_B


def test_unsupported_selected_source_is_a_reportable_failure_without_a_provider_read() -> None:
    vault = FakeVault("box")
    reports = FakeReportStore()
    adapter = _adapter(vault, reports)

    window = adapter.acquire("box")

    assert window.status == "unsupported"
    assert window.error_code == "tool_absent"
    assert window.completeness == "unavailable"
    report_ref, report_revision = _report_for(reports, window)
    adapter.commit(window, report_ref=report_ref, report_revision=report_revision)
    assert vault.record_calls[0]["error_code"] == "tool_absent"
    assert vault.record_calls[0]["completeness"] is None


def test_local_checkpoint_acknowledges_only_after_matching_durable_report() -> None:
    vault = FakeVault("whatsapp")
    reports = FakeReportStore()
    local = FakeLocalDelivery()
    adapter = _adapter(vault, reports, local_delivery=local)

    window = adapter.acquire("whatsapp")

    assert window.completeness == "partial"
    assert "PRIVATE TEST MESSAGE" not in repr(window)
    assert not local.acknowledgements

    report_ref, report_revision = _report_for(reports, window)
    adapter.commit(window, report_ref=report_ref, report_revision=report_revision)

    assert local.acknowledgements == [
        {
            "token": "opaque-local-token",
            "expected_source_revision": REVISION_A,
            "disposition": "accepted",
            "result_refs": (report_ref,),
            "actor_ref": "system-role:resident-pulse",
        }
    ]


def test_pending_local_delivery_replays_after_an_unrelated_source_revision_change() -> None:
    vault = FakeVault("whatsapp", "slack")
    vault.source_snapshot.revision = REVISION_B
    reports = FakeReportStore()
    local = FakeLocalDelivery()
    adapter = _adapter(vault, reports, local_delivery=local)

    window = adapter.acquire("whatsapp")

    assert window.source_revision == REVISION_A
    assert window.receipt.source_revision == REVISION_A
    report_ref, report_revision = _report_for(reports, window)
    adapter.commit(window, report_ref=report_ref, report_revision=report_revision)

    assert local.acknowledgements[0]["expected_source_revision"] == REVISION_A
