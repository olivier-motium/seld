from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.pulse_reports import PulseReportStore

OBSERVED_AT = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 8, 9, 1, tzinfo=UTC)
DECIDED_AT = datetime(2026, 9, 8, 9, 2, tzinfo=UTC)
DELIVERED_AT = datetime(2026, 9, 8, 9, 3, tzinfo=UTC)
COVERAGE = "a" * 64
EVIDENCE = "b" * 64
TARGET = "c" * 64
RESULT = "d" * 64
EVENT = "pulse-report:" + "e" * 64


def _append(store: PulseReportStore, *, claim: str = "A source requires a Pulse decision."):
    return store.append(
        event_key=EVENT,
        claim=claim,
        uncertainty="The source scope may omit related work.",
        source_id="slack",
        observed_at=OBSERVED_AT,
        coverage_ref="source:slack",
        coverage_revision=COVERAGE,
        completeness="partial",
        evidence_refs=(f"source:slack@{EVIDENCE}",),
        created_at=CREATED_AT,
    )


def test_append_replays_only_the_same_opaque_event_content(tmp_path: Path) -> None:
    store = PulseReportStore(tmp_path)

    first = _append(store)
    replay = _append(PulseReportStore(tmp_path))

    assert replay == first
    assert replay.report_ref == f"source-report:{first.identifier}@{first.revision}"
    assert PulseReportStore(tmp_path).list_pending().reports == (first,)
    with pytest.raises(ConflictError, match="different source content"):
        _append(store, claim="A different source claim must not replace the first one.")


def test_decision_is_single_cas_transition_and_moves_wake_to_delivery(tmp_path: Path) -> None:
    store = PulseReportStore(tmp_path)
    report = _append(store)

    decided = store.decide(
        report.identifier,
        expected_revision=report.revision,
        decision="wake",
        reason="A native task needs a fresh decision.",
        target_refs=(f"task:pulse-review@{TARGET}",),
        decided_at=DECIDED_AT,
    )

    assert store.list_pending(stage="relevance").reports == ()
    assert store.list_pending(stage="delivery").reports == (decided,)
    assert [item.kind for item in decided.history] == ["created", "decided"]
    with pytest.raises(ConflictError, match="reload"):
        store.decide(
            report.identifier,
            expected_revision=report.revision,
            decision="wake",
            reason="A native task needs a fresh decision.",
            target_refs=(f"task:pulse-review@{TARGET}",),
            decided_at=DECIDED_AT,
        )
    assert (
        store.decide(
            report.identifier,
            expected_revision=decided.revision,
            decision="wake",
            reason="A native task needs a fresh decision.",
            target_refs=(f"task:pulse-review@{TARGET}",),
            decided_at=DECIDED_AT,
        )
        == decided
    )


def test_delivery_requires_a_wake_result_and_survives_store_reopen(tmp_path: Path) -> None:
    store = PulseReportStore(tmp_path)
    report = _append(store)

    with pytest.raises(ValidationError, match="wake or investigate decision"):
        store.record_delivery(
            report.identifier,
            expected_revision=report.revision,
            result_ref=f"task:pulse-review@{RESULT}",
        )
    assert store.show(report.identifier) == report

    decided = store.decide(
        report.identifier,
        expected_revision=report.revision,
        decision="wake",
        reason="A native task needs a fresh decision.",
        target_refs=(f"task:pulse-review@{TARGET}",),
        decided_at=DECIDED_AT,
    )
    delivered = store.record_delivery(
        report.identifier,
        expected_revision=decided.revision,
        result_ref=f"task:pulse-review@{RESULT}",
        delivered_at=DELIVERED_AT,
    )

    reopened = PulseReportStore(tmp_path)
    assert reopened.show(report.identifier) == delivered
    assert reopened.list_pending(stage="delivery").reports == ()
    assert reopened.recent(source_id="slack").reports == (delivered,)
    assert [item.kind for item in delivered.history] == ["created", "decided", "delivered"]


def test_investigation_stays_pending_until_a_child_report_is_recorded(tmp_path: Path) -> None:
    store = PulseReportStore(tmp_path)
    parent = _append(store)
    investigation = store.decide(
        parent.identifier,
        expected_revision=parent.revision,
        decision="investigate",
        reason="The source needs one bounded follow-up.",
        decided_at=DECIDED_AT,
    )

    assert store.list_pending(stage="investigation").reports == (investigation,)
    with pytest.raises(ValidationError, match="child source-report"):
        store.record_delivery(
            parent.identifier,
            expected_revision=investigation.revision,
            result_ref=f"task:pulse-review@{RESULT}",
        )
    child = store.append(
        event_key="pulse-report:" + "f" * 64,
        claim="The follow-up source was checked.",
        uncertainty="No broader source coverage was added.",
        source_id="slack",
        observed_at=OBSERVED_AT,
        coverage_ref="source:slack",
        coverage_revision=COVERAGE,
        completeness="partial",
        causal_key="pulse-report:" + "e" * 64,
    )
    completed = store.record_delivery(
        parent.identifier,
        expected_revision=investigation.revision,
        result_ref=child.report_ref,
        delivered_at=DELIVERED_AT,
    )

    assert completed.delivery is not None and completed.delivery.result_ref == child.report_ref
    assert store.list_pending(stage="investigation").reports == ()
