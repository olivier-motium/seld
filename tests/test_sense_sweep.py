from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from continuity_kernel.atomic import atomic_write
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ValidationError
from continuity_kernel.records import format_time
from continuity_kernel.resident_signals import (
    ResidentSignal,
    ResidentSignalStore,
    SignalAppendRequest,
)
from continuity_kernel.sense_sweep import SweepRecallStatus, heartbeat_status, sense_sweep
from continuity_kernel.source_state import (
    record_source_observation,
    render_source_snapshot,
    select_sources,
    source_fingerprint,
)
from continuity_kernel.vault import Vault


def _sources(
    vault: Vault,
    *,
    selected: tuple[str, ...],
    observed_at: datetime,
    successful: tuple[str, ...] = (),
) -> None:
    snapshot = select_sources(
        vault.get_source_snapshot(),
        selected,
        observed_at=observed_at,
    )
    for source_id in successful:
        snapshot = record_source_observation(
            snapshot,
            source_id=source_id,
            actor_ref="pulse:019f0000-0000-7000-8000-000000000001",
            result="success",
            covered_through=format_time(observed_at),
            completeness="complete",
            account_fingerprint=source_fingerprint("account", "account"),
            host_fingerprint=source_fingerprint("host", "host"),
            tool_fingerprint=source_fingerprint("tool", "tool"),
            observed_at=observed_at,
        )
    atomic_write(vault.root / "SOURCES.md", render_source_snapshot(snapshot).encode())


def _signals(vault: Vault) -> tuple[ResidentSignal, ...]:
    return ResidentSignalStore(vault.root).list(include_acknowledged=True).signals


def test_empty_sweep_records_only_host_local_heartbeat(vault: Vault) -> None:
    before = vault.logical_digest()
    now = datetime(2026, 7, 29, 8, tzinfo=UTC)
    assert heartbeat_status(vault.root) is None

    result = sense_sweep(vault, observed_at=now)
    heartbeat = heartbeat_status(vault.root)

    assert result.status == "complete"
    assert result.selected_sources == 0
    assert result.signals_emitted == 0
    assert heartbeat is not None
    assert heartbeat["sequence"] == 1
    assert heartbeat["status"] == "complete"
    assert vault.logical_digest() == before


def test_scheduled_recall_result_is_published_in_same_heartbeat(vault: Vault) -> None:
    result = sense_sweep(
        vault,
        observed_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
        recall_refresh=lambda: SweepRecallStatus(True, True, True, None),
    )

    assert result.recall == SweepRecallStatus(True, True, True, None)
    status = heartbeat_status(vault.root)
    assert status is not None
    assert status["recall"] == {
        "attempted": True,
        "changed": True,
        "failure": None,
        "updated": True,
    }


def test_fresh_source_is_not_due_and_never_read_source_is_due(vault: Vault) -> None:
    read_at = datetime(2026, 7, 29, 8, tzinfo=UTC)
    _sources(
        vault,
        selected=("gmail", "github"),
        successful=("gmail",),
        observed_at=read_at,
    )

    result = sense_sweep(
        vault,
        observed_at=read_at + timedelta(hours=1),
    )
    signals = _signals(vault)

    assert result.selected_sources == 2
    assert result.source_due == 1
    assert len(signals) == 1
    signal = signals[0]
    assert signal.kind == "source-due"
    assert signal.ref == "source:github"
    assert signal.envelope == {
        "attempted_at": None,
        "covered_through": None,
        "due_at": None,
        "last_success_at": None,
        "recipe_version": "1",
        "source_id": "github",
    }


def test_partial_source_is_due_immediately_while_fresh_complete_source_stays_quiet(
    vault: Vault,
) -> None:
    observed_at = datetime(2026, 7, 29, 8, tzinfo=UTC)
    snapshot = select_sources(
        vault.get_source_snapshot(),
        ("gmail", "github"),
        observed_at=observed_at,
    )
    for source_id, completeness in (("gmail", "partial"), ("github", "complete")):
        snapshot = record_source_observation(
            snapshot,
            source_id=source_id,
            actor_ref="pulse:019f0000-0000-7000-8000-000000000001",
            result="success",
            covered_through=format_time(observed_at),
            completeness=completeness,
            account_fingerprint=source_fingerprint("account", source_id),
            host_fingerprint=source_fingerprint("host", "host"),
            tool_fingerprint=source_fingerprint("tool", source_id),
            observed_at=observed_at,
        )
    atomic_write(vault.root / "SOURCES.md", render_source_snapshot(snapshot).encode())

    result = sense_sweep(
        vault,
        observed_at=observed_at + timedelta(minutes=1),
    )
    signals = _signals(vault)

    assert result.source_due == 1
    assert len(signals) == 1
    assert signals[0].ref == "source:gmail"
    assert signals[0].envelope["due_at"] == format_time(observed_at)


def test_stale_source_emits_one_exact_coverage_signal(vault: Vault) -> None:
    read_at = datetime(2026, 7, 27, 8, tzinfo=UTC)
    _sources(vault, selected=("gmail",), successful=("gmail",), observed_at=read_at)

    sense_sweep(
        vault,
        observed_at=read_at + timedelta(days=2),
    )
    signal = _signals(vault)[0]

    assert signal.event_key == f"source-due:gmail:1:{format_time(read_at)}"
    assert signal.envelope["due_at"] == format_time(read_at + timedelta(days=1))
    assert signal.envelope["covered_through"] == format_time(read_at)


def test_repeated_and_post_sleep_sweeps_coalesce_current_due_event(vault: Vault) -> None:
    selected_at = datetime(2026, 7, 20, 8, tzinfo=UTC)
    _sources(vault, selected=("github",), observed_at=selected_at)

    first = sense_sweep(
        vault,
        observed_at=selected_at,
    )
    after_sleep = sense_sweep(
        vault,
        observed_at=selected_at + timedelta(days=7),
    )

    assert first.source_due == after_sleep.source_due == 1
    assert first.signals_emitted == 1
    assert after_sleep.signals_emitted == 0
    assert len(_signals(vault)) == 1
    assert heartbeat_status(vault.root)["sequence"] == 2  # type: ignore[index]


def test_sweep_publishes_all_due_pointers_in_one_mailbox_batch(
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 7, 29, 8, tzinfo=UTC)
    _sources(vault, selected=("gmail", "github"), observed_at=observed_at)
    vault.create_thread(
        identifier="due-batch",
        title="Due batch",
        purpose="Prove one mechanical mailbox transaction.",
        summary="The authored horizon is due.",
        recheck_at=format_time(observed_at + timedelta(hours=1)),
        observed_at=observed_at,
    )
    calls: list[tuple[SignalAppendRequest, ...]] = []
    original = ResidentSignalStore.append_many_results

    def observed_batch(
        store: ResidentSignalStore,
        requests: tuple[SignalAppendRequest, ...] | list[SignalAppendRequest],
    ) -> tuple[tuple[ResidentSignal, bool], ...]:
        calls.append(tuple(requests))
        return original(store, requests)

    monkeypatch.setattr(ResidentSignalStore, "append_many_results", observed_batch)

    result = sense_sweep(vault, observed_at=observed_at + timedelta(hours=2))

    assert result.status == "complete"
    assert result.source_due == 2
    assert result.thread_rechecks == 1
    assert result.signals_emitted == 3
    assert len(calls) == 1
    assert len(calls[0]) == 3


def test_only_nonterminal_due_threads_emit_and_rearming_gets_a_new_event(vault: Vault) -> None:
    created_at = datetime(2026, 7, 29, 8, tzinfo=UTC)
    due = vault.create_thread(
        identifier="due-review",
        title="Due review",
        purpose="Revisit one authored horizon.",
        summary="The clock is due.",
        recheck_at=format_time(created_at + timedelta(hours=1)),
        observed_at=created_at,
    )
    closed = vault.create_thread(
        identifier="closed-review",
        title="Closed review",
        purpose="Prove terminal work stays quiet.",
        summary="This work is finished.",
        recheck_at=format_time(created_at + timedelta(hours=1)),
        observed_at=created_at,
    )
    vault.update_thread(
        closed.identifier,
        expected_revision=closed.revision,
        status="closed",
        note="Closed before the authored horizon.",
        observed_at=created_at + timedelta(minutes=30),
    )

    first = sense_sweep(
        vault,
        observed_at=created_at + timedelta(hours=2),
    )
    rearmed = vault.update_thread(
        due.identifier,
        expected_revision=due.revision,
        recheck_at=format_time(created_at + timedelta(hours=3)),
        note="Reviewed and authored the next horizon.",
        observed_at=created_at + timedelta(hours=2, minutes=30),
    )
    second = sense_sweep(
        vault,
        observed_at=created_at + timedelta(hours=4),
    )

    assert first.thread_rechecks == second.thread_rechecks == 1
    signals = _signals(vault)
    assert len(signals) == 2
    assert all(signal.ref == due.identifier for signal in signals)
    assert signals[1].event_key == (
        f"work-thread-recheck:{rearmed.identifier}:{rearmed.recheck_at}:{rearmed.updated_at}"
    )


class _Clock:
    def __init__(self, values: tuple[float, ...]):
        self.values = iter(values)
        self.last = values[-1]

    def __call__(self) -> float:
        return next(self.values, self.last)


def test_sweep_budget_times_out_at_five_seconds_and_still_heartbeats(vault: Vault) -> None:
    clock = _Clock((0.0, 0.0, 6.0, 6.0))

    result = sense_sweep(
        vault,
        budget_seconds=5,
        monotonic=clock,
    )

    assert result.status == "timed_out"
    assert result.failure == "budget_exceeded"
    assert result.duration_ms == 6_000
    assert heartbeat_status(vault.root)["status"] == "timed_out"  # type: ignore[index]


def test_partial_source_coverage_is_due_immediately(vault: Vault) -> None:
    observed = datetime(2026, 7, 29, 8, tzinfo=UTC)
    snapshot = select_sources(vault.get_source_snapshot(), ("gmail",), observed_at=observed)
    snapshot = record_source_observation(
        snapshot,
        source_id="gmail",
        actor_ref="pulse:partial",
        result="success",
        covered_through=format_time(observed),
        completeness="partial",
        account_fingerprint=source_fingerprint("account", "account"),
        host_fingerprint=source_fingerprint("host", "host"),
        tool_fingerprint=source_fingerprint("tool", "tool"),
        cursor_digest=source_fingerprint("next-page", "cursor"),
        observed_at=observed,
    )
    atomic_write(vault.root / "SOURCES.md", render_source_snapshot(snapshot).encode())

    result = sense_sweep(
        vault,
        observed_at=observed + timedelta(seconds=1),
    )

    assert result.source_due == 1
    signal = _signals(vault)[0]
    assert signal.envelope["due_at"] == format_time(observed)


def test_heartbeat_rejects_extra_fields_and_wrong_host_binding(vault: Vault) -> None:
    sense_sweep(vault)
    heartbeat_path = next((data_dir() / "sense-sweep").glob("*/heartbeat.json"))
    original = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    original["provider_body"] = "must never be accepted"
    atomic_write(
        heartbeat_path,
        (json.dumps(original, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )

    with pytest.raises(ValidationError, match="unsupported shape"):
        heartbeat_status(vault.root)

    original.pop("provider_body")
    original["host_id"] = "00000000-0000-4000-8000-000000000000"
    atomic_write(
        heartbeat_path,
        (json.dumps(original, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )
    with pytest.raises(ValidationError, match="another vault or host"):
        heartbeat_status(vault.root)


def test_mechanical_sweep_does_not_scan_a_wide_recall_tree(vault: Vault) -> None:
    journal = vault.root / "journal/wide"
    journal.mkdir(parents=True)
    for index in range(2_000):
        (journal / f"note-{index}.md").write_text("# Note\n", encoding="utf-8")

    started = time.monotonic()
    result = sense_sweep(vault, budget_seconds=1)
    elapsed = time.monotonic() - started

    assert result.status == "complete"
    assert result.recall.attempted is False
    assert elapsed < 1
