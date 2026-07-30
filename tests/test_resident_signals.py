from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import continuity_kernel.resident_signals as resident_signals_module
from continuity_kernel import atomic
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.resident_signals import (
    ResidentSignalStore,
    SignalAppendRequest,
    signal_view_dict,
)
from continuity_kernel.vault import Vault

SIGNAL_A = "019f0000-0000-7000-8000-000000000001"
SIGNAL_B = "019f0000-0000-7000-8000-000000000002"
ACK_A = "019f0000-0000-7000-8000-000000000011"
REVISION_A = "a" * 64
REVISION_B = "b" * 64


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


def _signal(identifier: str, *, kind: str = "task-checkpoint") -> dict[str, object]:
    return {
        "envelope": {"summary": f"Signal {identifier[-1]} is waiting."},
        "event_key": f"fixture:{identifier}",
        "input_id": identifier,
        "kind": kind,
        "observed_at": "2026-07-29T08:00:00Z",
        "ref": "task:cut-over-resident-mind",
    }


def _leave_prepared_compaction(
    store: ResidentSignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    actual_recover = ResidentSignalStore._recover_compaction

    def stop_after_prepare(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
    ) -> None:
        if signal_store.compaction_marker_path.exists():
            raise SystemExit("hard stop after compaction prepare")
        actual_recover(signal_store, pinned)

    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", stop_after_prepare)
    with pytest.raises(SystemExit, match="hard stop after compaction prepare"):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", actual_recover)
    marker = json.loads(store.compaction_marker_path.read_text(encoding="utf-8"))
    assert isinstance(marker, dict)
    return marker


def test_list_filters_acknowledged_signals_and_preserves_private_shape(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B, kind="source-due")])
    _write_jsonl(
        store.acknowledgements_path,
        [
            {
                "acknowledged_at": "2026-07-29T08:10:00Z",
                "acknowledgement_id": ACK_A,
                "consumer": "resident-mind",
                "input_id": SIGNAL_A,
            }
        ],
    )

    view = store.list(limit=1)
    assert [item.input_id for item in view.signals] == [SIGNAL_B]
    assert view.acknowledged_ids == (SIGNAL_A,)
    assert view.remaining == 0
    assert signal_view_dict(view)["revision"] == view.revision
    assert signal_view_dict(view)["next_cursor"] is None
    assert store.get(SIGNAL_A).envelope["summary"] == "Signal 1 is waiting."
    assert store.status().inputs == 2
    assert store.status().acknowledged == 1
    assert store.status().pending == 1

    complete = store.list(include_acknowledged=True, limit=1)
    assert [item.input_id for item in complete.signals] == [SIGNAL_A]
    assert complete.remaining == 1


def test_acknowledgement_is_cas_bound_idempotent_and_visible_after_restart(
    tmp_path: Path,
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    before = store.list()

    results = store.acknowledge(
        [SIGNAL_A, SIGNAL_A],
        expected_revision=before.revision,
        consumer="resident-mind",
        acknowledged_at=datetime(2026, 7, 29, 8, 10, tzinfo=UTC),
    )
    assert len(results) == 1
    after = ResidentSignalStore(tmp_path).list()
    assert [item.input_id for item in after.signals] == [SIGNAL_B]
    assert after.revision != before.revision

    with pytest.raises(ConflictError, match="queue changed"):
        store.acknowledge(
            [SIGNAL_B],
            expected_revision=before.revision,
            consumer="resident-mind",
        )

    replay = store.acknowledge(
        [SIGNAL_A],
        expected_revision=after.revision,
        consumer="resident-mind",
    )
    assert replay == results
    assert ResidentSignalStore(tmp_path).list().revision == after.revision


def test_signal_queue_fails_closed_on_unknown_shapes_and_conflicting_consumers(
    tmp_path: Path,
) -> None:
    store = ResidentSignalStore(tmp_path)
    malformed = _signal(SIGNAL_A)
    malformed["provider_body"] = "must not become a queue field"
    _write_jsonl(store.inputs_path, [malformed])
    with pytest.raises(ValidationError, match="unsupported shape"):
        store.list()

    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A)])
    before = store.list()
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=before.revision,
        consumer="first-consumer",
    )
    current = store.list(include_acknowledged=True)
    with pytest.raises(ConflictError, match="already acknowledged"):
        store.acknowledge(
            [SIGNAL_A],
            expected_revision=current.revision,
            consumer="second-consumer",
        )


def test_signal_show_and_ack_reject_unknown_ids(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A)])
    with pytest.raises(NotFoundError, match="does not exist"):
        store.get(SIGNAL_B)
    with pytest.raises(NotFoundError, match="does not exist"):
        store.acknowledge(
            [SIGNAL_B],
            expected_revision=store.list().revision,
            consumer="resident-mind",
        )


def test_signal_pages_are_revision_bound_and_reach_the_tail(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    values = [_signal(f"019f0000-0000-7000-8000-{index:012d}") for index in range(1, 10_004)]
    _write_jsonl(store.inputs_path, values)

    first = store.list(limit=10_000)
    assert len(first.signals) == 10_000
    assert first.remaining == 3
    assert first.next_cursor is not None

    tail = ResidentSignalStore(tmp_path).list(limit=10_000, cursor=first.next_cursor)
    assert [item.input_id for item in tail.signals] == [
        f"019f0000-0000-7000-8000-{index:012d}" for index in range(10_001, 10_004)
    ]
    assert tail.remaining == 0
    assert tail.next_cursor is None

    _write_jsonl(store.inputs_path, [*values, _signal("019f0000-0000-7000-8000-000000010004")])
    with pytest.raises(ConflictError, match="restart listing"):
        store.list(limit=10_000, cursor=first.next_cursor)


def test_signal_queue_rejects_acknowledgement_for_unknown_input(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A)])
    _write_jsonl(
        store.acknowledgements_path,
        [
            {
                "acknowledged_at": "2026-07-29T08:10:00Z",
                "acknowledgement_id": ACK_A,
                "consumer": "resident-mind",
                "input_id": SIGNAL_B,
            }
        ],
    )
    with pytest.raises(ValidationError, match="unknown signal"):
        store.list()


def test_signal_append_is_event_key_idempotent_and_durable(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    first = store.append(
        kind="source-due",
        ref="source:gmail",
        event_key="source-due:gmail:2026-07-29",
        envelope={"covered_through": "2026-07-29T08:00:00Z"},
        observed_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
    )
    replay = ResidentSignalStore(tmp_path).append(
        kind="source-due",
        ref="source:gmail",
        event_key="source-due:gmail:2026-07-29",
        envelope={"covered_through": "2026-07-29T08:00:00Z"},
        observed_at=datetime(2026, 7, 29, 9, tzinfo=UTC),
    )

    assert replay == first
    assert ResidentSignalStore(tmp_path).list().signals == (first,)
    with pytest.raises(ConflictError, match="different envelope"):
        store.append(
            kind="source-due",
            ref="source:gmail",
            event_key="source-due:gmail:2026-07-29",
            envelope={"covered_through": "2026-07-29T09:00:00Z"},
        )


def test_model_facing_signal_is_a_content_free_canonical_pointer(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    record_ref = f"task:launch-review@{REVISION_A}"

    signal = store.append_canonical_change(
        record_ref=record_ref,
        change_type="observation",
    )
    replay = store.append_canonical_change(
        record_ref=record_ref,
        change_type="observation",
    )

    assert replay == signal
    assert signal.kind == "canonical-change"
    assert signal.ref == record_ref
    assert signal.envelope == {"change_type": "observation"}
    assert signal.event_key is not None and signal.event_key.startswith("canonical-change:")
    with pytest.raises(ValidationError, match="opaque record revision"):
        store.append_canonical_change(
            record_ref="Email body: cancel the order and token sk-live-secret",
            change_type="observation",
        )
    with pytest.raises(ValidationError, match="change type"):
        store.append_canonical_change(record_ref=record_ref, change_type="raw-provider-body")


def test_signal_compaction_archives_only_settled_old_evidence(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(
        store.inputs_path,
        [_signal(SIGNAL_A), _signal(SIGNAL_B), _signal("019f0000-0000-7000-8000-000000000003")],
    )
    before = store.list()
    store.acknowledge(
        [SIGNAL_A, SIGNAL_B],
        expected_revision=before.revision,
        consumer="resident-mind",
        acknowledged_at=datetime(2026, 7, 29, 8, 10, tzinfo=UTC),
    )

    result = store.compact(
        retain_recent=1,
        observed_at=datetime(2026, 7, 29, 9, tzinfo=UTC),
    )

    assert result.archived_signals == 2
    assert result.archived_acknowledgements == 2
    assert result.live_signals == 1
    assert result.live_acknowledgements == 0
    assert result.archive_inputs_path is not None
    assert result.archive_acknowledgements_path is not None
    archived_inputs = tmp_path / result.archive_inputs_path
    archived_acks = tmp_path / result.archive_acknowledgements_path
    assert [json.loads(line)["input_id"] for line in archived_inputs.read_text().splitlines()] == [
        SIGNAL_A,
        SIGNAL_B,
    ]
    assert len(archived_acks.read_text().splitlines()) == 2
    after = ResidentSignalStore(tmp_path).list(include_acknowledged=True)
    assert [item.input_id for item in after.signals] == ["019f0000-0000-7000-8000-000000000003"]
    assert not any((store.root / "operations").iterdir())


def test_compaction_rejects_prospective_archive_file_overflow_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    first = store.append(
        kind="test",
        event_key="archive-bound:first",
        envelope={"summary": "first"},
    )
    store.acknowledge(
        [first.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)
    second = store.append(
        kind="test",
        event_key="archive-bound:second",
        envelope={"summary": "second"},
    )
    store.acknowledge(
        [second.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    before = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(resident_signals_module, "MAX_SIGNAL_HISTORY_FILES", 3)

    with pytest.raises(ValidationError, match="archive file bound"):
        store.compact(retain_recent=0)

    after = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not store.compaction_marker_path.exists()
    assert not any((store.root / "operations").iterdir())


def test_compaction_rejects_prospective_archive_byte_overflow_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    first = store.append(
        kind="test",
        event_key="archive-size:first",
        envelope={"summary": "first"},
    )
    store.acknowledge(
        [first.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)
    second = store.append(
        kind="test",
        event_key="archive-size:second",
        envelope={"summary": "second"},
    )
    store.acknowledge(
        [second.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    archive_bytes = sum(path.stat().st_size for path in (store.root / "archive").iterdir())
    before = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        resident_signals_module,
        "MAX_SIGNAL_HISTORY_BYTES",
        archive_bytes + 1,
    )

    with pytest.raises(ValidationError, match="archive size bound"):
        store.compact(retain_recent=0)

    after = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_recovery_rejects_prospective_archive_overflow_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    marker = _leave_prepared_compaction(store, monkeypatch)
    before_marker = store.compaction_marker_path.read_bytes()
    before_live = store.inputs_path.read_bytes()
    monkeypatch.setattr(resident_signals_module, "MAX_SIGNAL_HISTORY_FILES", 1)

    with pytest.raises(ValidationError, match="archive file bound"):
        ResidentSignalStore(tmp_path).status()

    assert store.compaction_marker_path.read_bytes() == before_marker
    assert store.inputs_path.read_bytes() == before_live
    assert not (tmp_path / str(marker["archive_inputs"])).exists()
    assert not (tmp_path / str(marker["archive_acknowledgements"])).exists()


@pytest.mark.parametrize("fraction", (1.0, 0.5))
def test_recovery_removes_exact_hard_death_archive_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fraction: float,
) -> None:
    store = ResidentSignalStore(tmp_path)
    marker = _leave_prepared_compaction(store, monkeypatch)
    token = str(marker["token"])
    operation_root = store.root / "operations" / token
    target = tmp_path / str(marker["archive_inputs"])
    staged = (operation_root / "archive_inputs.jsonl").read_bytes()
    prefix = staged if fraction == 1.0 else staged[: max(1, len(staged) // 2)]
    orphan = target.parent / f".{target.name}.seld-stage-{'a' * 32}"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(prefix)

    status = ResidentSignalStore(tmp_path).status()

    assert status.pending == 1
    assert not orphan.exists()
    assert not store.compaction_marker_path.exists()
    assert target.read_bytes() == staged


def test_recovery_cleans_marker_stage_before_near_capacity_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    first = store.append(
        kind="test",
        event_key="near-capacity:first",
        envelope={"summary": "first"},
    )
    store.acknowledge(
        [first.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)
    second = store.append(
        kind="test",
        event_key="near-capacity:second",
        envelope={"summary": "second"},
    )
    store.acknowledge(
        [second.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    actual_recover = ResidentSignalStore._recover_compaction

    def stop_after_prepare(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
    ) -> None:
        if signal_store.compaction_marker_path.exists():
            raise SystemExit("hard stop after near-capacity prepare")
        actual_recover(signal_store, pinned)

    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", stop_after_prepare)
    with pytest.raises(SystemExit, match="near-capacity prepare"):
        store.compact(retain_recent=0)
    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", actual_recover)
    marker = json.loads(store.compaction_marker_path.read_text(encoding="utf-8"))
    operation = store.root / "operations" / str(marker["token"])
    target = tmp_path / str(marker["archive_inputs"])
    staged = (operation / "archive_inputs.jsonl").read_bytes()
    orphan = target.parent / f".{target.name}.seld-stage-{'b' * 32}"
    orphan.write_bytes(staged[: max(1, len(staged) // 2)])
    monkeypatch.setattr(resident_signals_module, "MAX_SIGNAL_HISTORY_FILES", 4)

    assert ResidentSignalStore(tmp_path).status().pending == 0
    assert not orphan.exists()
    assert not store.compaction_marker_path.exists()
    assert len(tuple((store.root / "archive").iterdir())) == 4
    assert ResidentSignalStore(tmp_path).status().pending == 0


def test_archive_stage_unlink_hard_stop_retries_without_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    marker = _leave_prepared_compaction(store, monkeypatch)
    operation = store.root / "operations" / str(marker["token"])
    target = tmp_path / str(marker["archive_inputs"])
    staged = (operation / "archive_inputs.jsonl").read_bytes()
    orphan = target.parent / f".{target.name}.seld-stage-{'c' * 32}"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(staged[: max(1, len(staged) // 2)])
    actual_unlink = ResidentSignalStore._unlink_private_exact
    stopped = False

    def stop_after_archive_stage_unlink(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal stopped
        actual_unlink(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        if not stopped and path.name == orphan.name:
            stopped = True
            raise SystemExit("hard stop after archive-stage unlink")

    monkeypatch.setattr(
        ResidentSignalStore,
        "_unlink_private_exact",
        stop_after_archive_stage_unlink,
    )
    with pytest.raises(SystemExit, match="archive-stage unlink"):
        ResidentSignalStore(tmp_path).status()

    assert stopped is True
    assert not orphan.exists()
    assert store.compaction_marker_path.exists()
    assert not tuple(target.parent.glob("*.seld-quarantine-*"))
    monkeypatch.setattr(
        ResidentSignalStore,
        "_unlink_private_exact",
        actual_unlink,
    )
    assert ResidentSignalStore(tmp_path).status().pending == 1
    assert not store.compaction_marker_path.exists()


def test_archive_verification_rejects_hostile_atomic_temp_near_match(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    archive = store.root / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    hostile = archive / f".inputs-20260729T090000Z-{'a' * 32}.jsonl.seld-stage-short"
    hostile.write_bytes(b"not writer-owned\n")

    with pytest.raises(ValidationError, match="invalid entry"):
        store.status(verify_archive_history=True)


def test_compacted_event_key_replays_exactly_across_a_fresh_process(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    first, created = store.append_result(
        kind="source-due",
        ref="source:gmail",
        event_key="source-due:gmail:2026-07-29",
        envelope={"summary": "Private provider text must not enter the replay ledger."},
        observed_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
    )
    store.acknowledge(
        [first.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)

    assert created is True
    assert store.status().inputs == 0
    ledger = store.settled_event_keys_path.read_text(encoding="utf-8")
    assert "source-due:gmail" not in ledger
    assert "Private provider text" not in ledger
    replayed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from pathlib import Path; "
                "from continuity_kernel.resident_signals import ResidentSignalStore; "
                "s=ResidentSignalStore(Path(sys.argv[1])); signal,created=s.append_result("
                "kind='source-due',ref='source:gmail',"
                "event_key='source-due:gmail:2026-07-29',"
                "envelope={'summary':'Private provider text must not enter the replay ledger.'}); "
                "print(json.dumps({'created':created,'id':signal.input_id,"
                "'observed_at':signal.observed_at,'pending':s.status().pending}))"
            ),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(replayed.stdout) == {
        "created": False,
        "id": first.input_id,
        "observed_at": first.observed_at,
        "pending": 0,
    }
    with pytest.raises(ConflictError, match="different envelope"):
        ResidentSignalStore(tmp_path).append(
            kind="source-due",
            ref="source:gmail",
            event_key="source-due:gmail:2026-07-29",
            envelope={"summary": "Changed after compaction."},
        )


def test_batch_append_is_atomic_and_coalesces_duplicate_event_keys(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    requests = (
        SignalAppendRequest(
            kind="source-due",
            ref="source:gmail",
            event_key="source-due:gmail:current",
            envelope={"source_id": "gmail"},
            observed_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
        ),
        SignalAppendRequest(
            kind="source-due",
            ref="source:slack",
            event_key="source-due:slack:current",
            envelope={"source_id": "slack"},
            observed_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
        ),
        SignalAppendRequest(
            kind="source-due",
            ref="source:gmail",
            event_key="source-due:gmail:current",
            envelope={"source_id": "gmail"},
            observed_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
        ),
    )

    first = store.append_many_results(requests)
    replay = ResidentSignalStore(tmp_path).append_many_results(requests)

    assert [created for _signal, created in first] == [True, True, False]
    assert [created for _signal, created in replay] == [False, False, False]
    assert [signal.input_id for signal, _created in replay] == [
        signal.input_id for signal, _created in first
    ]
    assert first[2][0].input_id == first[0][0].input_id
    before = store.status()
    with pytest.raises(ConflictError, match="different envelope"):
        store.append_many_results(
            (
                SignalAppendRequest(
                    kind="source-due",
                    ref="source:gmail",
                    event_key="source-due:gmail:current",
                    envelope={"source_id": "changed"},
                ),
                SignalAppendRequest(
                    kind="source-due",
                    ref="source:github",
                    event_key="source-due:github:current",
                    envelope={"source_id": "github"},
                ),
            )
        )
    assert store.status() == before


def test_batch_append_rejects_queue_overflow_without_partial_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    store.append(kind="source-due", envelope={"source": "existing"})
    before = store.inputs_path.read_bytes()
    monkeypatch.setattr(
        resident_signals_module,
        "MAX_SIGNAL_QUEUE_BYTES",
        len(before) + 1,
    )

    with pytest.raises(ValidationError, match="queue is full"):
        store.append_many_results(
            (
                SignalAppendRequest(kind="source-due", envelope={"source": "first"}),
                SignalAppendRequest(kind="source-due", envelope={"source": "second"}),
            )
        )

    assert store.inputs_path.read_bytes() == before


def test_current_settled_index_keeps_hot_status_off_archive_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    signal = store.append(
        kind="test",
        event_key="hot-status:no-history-scan",
        envelope={"summary": "settled"},
    )
    store.acknowledge(
        [signal.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)

    def reject_history_scan(
        _self: ResidentSignalStore,
        _store: atomic.PinnedPathRoot | None,
    ) -> dict[str, bytes]:
        raise AssertionError("hot status scanned archive history")

    monkeypatch.setattr(
        ResidentSignalStore,
        "_read_archive_history_files",
        reject_history_scan,
    )

    assert ResidentSignalStore(tmp_path).status().pending == 0
    with pytest.raises(AssertionError, match="scanned archive history"):
        ResidentSignalStore(tmp_path).status(verify_archive_history=True)


def test_doctor_rejects_malformed_or_live_overlapping_settled_event_keys(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Settled event-key doctor")
    store = ResidentSignalStore(vault.root)
    signal = store.append(
        kind="source-due",
        event_key="source-due:gmail:one",
        envelope={"source": "gmail"},
    )
    store.acknowledge(
        [signal.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    original_input = store.inputs_path.read_bytes()
    store.compact(retain_recent=0)

    store.inputs_path.write_bytes(original_input)
    report = Vault(vault.root).doctor()
    assert not report.healthy
    assert any(
        issue.code == "invalid-resident-signals" and "both live and settled" in issue.message
        for issue in report.issues
    )

    store.inputs_path.write_bytes(b"")
    store.settled_event_keys_path.write_text('{"format_version":1}\n', encoding="utf-8")
    malformed = Vault(vault.root).doctor()
    assert not malformed.healthy
    assert any(issue.code == "invalid-resident-signals" for issue in malformed.issues)


def test_archive_and_settled_index_must_match_before_replay_is_admitted(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Exact settled event-key history")
    store = ResidentSignalStore(vault.root)
    originals = [
        store.append(
            kind="source-due",
            event_key=f"source-due:gmail:{number}",
            envelope={"source": "gmail", "number": number},
        )
        for number in (1, 2)
    ]
    store.acknowledge(
        [item.input_id for item in originals],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)
    ledger_lines = store.settled_event_keys_path.read_bytes().splitlines(keepends=True)
    assert len(ledger_lines) == 3
    store.settled_event_keys_path.write_bytes(b"".join(ledger_lines[:2]))
    live_before = store.inputs_path.read_bytes()

    report = vault.doctor()
    assert report.healthy is False
    assert any(
        issue.code == "invalid-resident-signals" and "does not match its receipts" in issue.message
        for issue in report.issues
    )
    with pytest.raises(ValidationError, match="does not match its receipts"):
        store.append(
            kind="source-due",
            event_key="source-due:gmail:2",
            envelope={"source": "gmail", "number": 2},
        )
    assert store.inputs_path.read_bytes() == live_before


def test_completed_v1_archive_history_upgrades_before_replay(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    original = store.append(
        kind="source-due",
        event_key="source-due:gmail:completed-v1",
        envelope={"source": "gmail"},
    )
    store.acknowledge(
        [original.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)
    store.settled_event_keys_path.unlink()

    assert ResidentSignalStore(tmp_path).status().inputs == 0
    assert store.settled_event_keys_path.is_file()
    replay, created = ResidentSignalStore(tmp_path).append_result(
        kind="source-due",
        event_key="source-due:gmail:completed-v1",
        envelope={"source": "gmail"},
    )
    assert created is False
    assert replay == original


def test_boolean_settled_event_key_version_is_rejected(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    signal = store.append(
        kind="source-due",
        event_key="source-due:gmail:boolean-version",
        envelope={"source": "gmail"},
    )
    store.acknowledge(
        [signal.input_id],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    store.compact(retain_recent=0)
    lines = store.settled_event_keys_path.read_bytes().splitlines(keepends=True)
    assert len(lines) == 2
    receipt = json.loads(lines[1])
    receipt["format_version"] = True
    store.settled_event_keys_path.write_bytes(
        lines[0] + (json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n").encode()
    )

    with pytest.raises(ValidationError, match="unsupported version"):
        ResidentSignalStore(tmp_path).status()


@pytest.mark.parametrize(
    "boundary",
    (
        "archive_inputs",
        "archive_acknowledgements",
        "settled_event_keys",
        "live_inputs",
        "live_acknowledgements",
    ),
)
def test_compaction_recovers_after_each_target_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    store = ResidentSignalStore(tmp_path)
    signal_c = "019f0000-0000-7000-8000-000000000003"
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B), _signal(signal_c)])
    before = store.list()
    store.acknowledge(
        [SIGNAL_A, SIGNAL_B],
        expected_revision=before.revision,
        consumer="resident-mind",
    )
    actual_replace = ResidentSignalStore._replace_exact
    crashed = False

    def crash_after_target(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal crashed
        actual_replace(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        target = path
        observed_boundary: str | None = None
        if target.parent == store.root / "archive":
            observed_boundary = (
                "archive_inputs"
                if target.name.startswith("inputs-")
                else "archive_acknowledgements"
            )
        elif target == store.inputs_path:
            observed_boundary = "live_inputs"
        elif target == store.acknowledgements_path:
            observed_boundary = "live_acknowledgements"
        elif target == store.settled_event_keys_path:
            observed_boundary = "settled_event_keys"
        if not crashed and store.compaction_marker_path.exists() and observed_boundary == boundary:
            crashed = True
            raise SystemExit(f"crash after {boundary}")

    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", crash_after_target)
    with pytest.raises(SystemExit, match=boundary):
        store.compact(retain_recent=1)
    assert crashed and store.compaction_marker_path.exists()

    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", actual_replace)
    fresh = ResidentSignalStore(tmp_path)
    status = fresh.status()
    visible = fresh.list(include_acknowledged=True)

    assert status.inputs == 1
    assert status.acknowledged == 0
    assert status.pending == 1
    assert [signal.input_id for signal in visible.signals] == [signal_c]
    assert not fresh.compaction_marker_path.exists()
    assert len(list((fresh.root / "archive").glob("inputs-*.jsonl"))) == 1
    assert len(list((fresh.root / "archive").glob("acks-*.jsonl"))) == 1
    replay, created = fresh.append_result(
        kind="task-checkpoint",
        ref="task:cut-over-resident-mind",
        event_key=f"fixture:{SIGNAL_A}",
        envelope={"summary": "Signal 1 is waiting."},
    )
    assert replay.input_id == SIGNAL_A
    assert created is False


def test_true_v1_compaction_marker_upgrades_index_before_removing_live_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    actual_recover = ResidentSignalStore._recover_compaction

    def stop_after_prepare(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
    ) -> None:
        if signal_store.compaction_marker_path.exists():
            raise SystemExit("prepared v1 fixture")
        actual_recover(signal_store, pinned)

    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", stop_after_prepare)
    with pytest.raises(SystemExit, match="prepared v1"):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", actual_recover)

    marker = json.loads(store.compaction_marker_path.read_bytes())
    marker["format_version"] = 1
    marker["digests"].pop("settled_event_keys")
    marker["expected_digests"].pop("settled_event_keys")
    operation = store.root / "operations" / marker["token"]
    (operation / "settled_event_keys.jsonl").unlink()
    store.compaction_marker_path.write_text(
        json.dumps(marker, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert ResidentSignalStore(tmp_path).status().pending == 1
    assert store.settled_event_keys_path.is_file()
    replay, created = ResidentSignalStore(tmp_path).append_result(
        kind="task-checkpoint",
        ref="task:cut-over-resident-mind",
        event_key=f"fixture:{SIGNAL_A}",
        envelope={"summary": "Signal 1 is waiting."},
    )
    assert created is False
    assert replay.input_id == SIGNAL_A


def test_boolean_compaction_marker_version_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    actual_recover = ResidentSignalStore._recover_compaction

    def stop_after_prepare(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
    ) -> None:
        if signal_store.compaction_marker_path.exists():
            raise SystemExit("prepared boolean fixture")
        actual_recover(signal_store, pinned)

    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", stop_after_prepare)
    with pytest.raises(SystemExit, match="prepared boolean"):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_recover_compaction", actual_recover)
    marker = json.loads(store.compaction_marker_path.read_bytes())
    marker["format_version"] = True
    store.compaction_marker_path.write_text(
        json.dumps(marker, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unsupported version"):
        ResidentSignalStore(tmp_path).status()


@pytest.mark.parametrize("crash_after", ("marker", "first_stage"))
def test_markerless_committed_compaction_residue_is_cleaned_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after: str,
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    actual_unlink = ResidentSignalStore._unlink_private_exact
    crashed = False

    def crash_during_cleanup(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal crashed
        actual_unlink(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        marker_removed = path == store.compaction_marker_path
        stage_removed = path.parent.parent == store.root / "operations"
        if not crashed and (
            (crash_after == "marker" and marker_removed)
            or (crash_after == "first_stage" and stage_removed)
        ):
            crashed = True
            raise SystemExit(f"crash after {crash_after}")

    monkeypatch.setattr(ResidentSignalStore, "_unlink_private_exact", crash_during_cleanup)
    with pytest.raises(SystemExit, match=crash_after):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_unlink_private_exact", actual_unlink)
    assert store.compaction_marker_path.exists() is False
    assert any((store.root / "operations").iterdir())

    fresh = ResidentSignalStore(tmp_path)
    assert fresh.status().pending == 1
    assert not any((store.root / "operations").iterdir())
    replay, created = fresh.append_result(
        kind="task-checkpoint",
        ref="task:cut-over-resident-mind",
        event_key=f"fixture:{SIGNAL_A}",
        envelope={"summary": "Signal 1 is waiting."},
    )
    assert created is False
    assert replay.input_id == SIGNAL_A


@pytest.mark.parametrize(
    "boundary",
    (
        "empty",
        "archive_acknowledgements.jsonl",
        "archive_inputs.jsonl",
        "live_acknowledgements.jsonl",
        "live_inputs.jsonl",
        "settled_event_keys.jsonl",
    ),
)
def test_markerless_prepared_operation_is_discarded_after_each_stage_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    before_inputs = store.inputs_path.read_bytes()
    before_acks = store.acknowledgements_path.read_bytes()
    actual_replace = ResidentSignalStore._replace_exact

    def stop_during_staging(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        is_operation_stage = path.parent.parent == store.root / "operations"
        if is_operation_stage and boundary == "empty":
            raise SystemExit("hard stop at empty operation")
        actual_replace(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        if is_operation_stage and path.name == boundary:
            raise SystemExit(f"hard stop after {boundary}")

    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", stop_during_staging)
    with pytest.raises(SystemExit, match="hard stop"):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", actual_replace)

    assert not store.compaction_marker_path.exists()
    assert any((store.root / "operations").iterdir())
    status = ResidentSignalStore(tmp_path).status()

    assert status.inputs == 2
    assert status.acknowledged == 1
    assert status.pending == 1
    assert store.inputs_path.read_bytes() == before_inputs
    assert store.acknowledgements_path.read_bytes() == before_acks
    assert not any((store.root / "operations").iterdir())
    archive = store.root / "archive"
    assert not archive.exists() or not any(archive.iterdir())


@pytest.mark.parametrize("fraction", (1.0, 0.5))
def test_markerless_hidden_operation_stage_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fraction: float,
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=store.status().revision,
        consumer="resident-mind",
    )
    actual_replace = ResidentSignalStore._replace_exact
    injected = False

    def leave_hidden_stage(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        if not injected and path.parent.parent == store.root / "operations":
            replacement = kwargs["replacement"]
            assert isinstance(replacement, bytes)
            content = (
                replacement if fraction == 1.0 else replacement[: max(1, len(replacement) // 2)]
            )
            hidden = path.parent / f".{path.name}.seld-stage-{'a' * 32}"
            hidden.write_bytes(content)
            injected = True
            raise SystemExit("hard stop inside operation-stage publication")
        actual_replace(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", leave_hidden_stage)
    with pytest.raises(SystemExit, match="operation-stage publication"):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", actual_replace)

    assert ResidentSignalStore(tmp_path).status().pending == 1
    assert not any((store.root / "operations").iterdir())


def test_markerless_operation_preserves_unowned_stage_near_match(tmp_path: Path) -> None:
    store = ResidentSignalStore(tmp_path)
    token = "a" * 32
    operation = store.root / "operations" / token
    operation.mkdir(parents=True)
    hostile = operation / ".archive_inputs.jsonl.seld-stage-short"
    hostile.write_bytes(b"not writer-owned\n")

    with pytest.raises(ValidationError, match="operation is invalid"):
        store.status()

    assert hostile.read_bytes() == b"not writer-owned\n"


def test_private_operation_stage_unlink_hard_stop_is_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    operation = store.root / "operations" / ("a" * 32)
    operation.mkdir(parents=True)
    stage = operation / "live_inputs.jsonl"
    stage.write_bytes(b"private stage\n")
    actual_unlink = ResidentSignalStore._unlink_private_exact
    stopped = False

    def stop_after_private_unlink(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal stopped
        actual_unlink(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        if not stopped:
            stopped = True
            raise SystemExit("hard stop after private unlink")

    monkeypatch.setattr(
        ResidentSignalStore,
        "_unlink_private_exact",
        stop_after_private_unlink,
    )
    with pytest.raises(SystemExit, match="hard stop after private unlink"):
        store.status()

    assert stopped is True
    assert not stage.exists()
    assert not tuple(operation.glob("*.seld-quarantine-*"))
    monkeypatch.setattr(
        ResidentSignalStore,
        "_unlink_private_exact",
        actual_unlink,
    )
    assert ResidentSignalStore(tmp_path).status().pending == 0
    assert not operation.exists()


def test_compaction_recovery_completes_in_a_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    signal_c = "019f0000-0000-7000-8000-000000000003"
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B), _signal(signal_c)])
    before = store.list()
    store.acknowledge(
        [SIGNAL_A, SIGNAL_B],
        expected_revision=before.revision,
        consumer="resident-mind",
    )
    actual_replace = ResidentSignalStore._replace_exact
    crashed = False

    def crash_after_live_inputs(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal crashed
        actual_replace(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        if not crashed and path == store.inputs_path and store.compaction_marker_path.exists():
            crashed = True
            raise SystemExit("crash after live inputs")

    monkeypatch.setattr(
        ResidentSignalStore,
        "_replace_exact",
        crash_after_live_inputs,
    )
    with pytest.raises(SystemExit, match="live inputs"):
        store.compact(retain_recent=1)
    assert crashed and store.compaction_marker_path.exists()

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from pathlib import Path; "
                "from continuity_kernel.resident_signals import ResidentSignalStore; "
                "store=ResidentSignalStore(Path(sys.argv[1])); status=store.status(); "
                "visible=store.list(include_acknowledged=True); "
                "print(json.dumps({'inputs': status.inputs, "
                "'acknowledged': status.acknowledged, 'pending': status.pending, "
                "'ids': [item.input_id for item in visible.signals], "
                "'marker': store.compaction_marker_path.exists(), "
                "'input_archives': len(list((store.root/'archive').glob('inputs-*.jsonl'))), "
                "'ack_archives': len(list((store.root/'archive').glob('acks-*.jsonl')))}))"
            ),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result == {
        "ack_archives": 1,
        "acknowledged": 0,
        "ids": [signal_c],
        "input_archives": 1,
        "inputs": 1,
        "marker": False,
        "pending": 1,
    }


def test_compaction_recovers_capacity_without_dropping_pending_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B)])
    before = store.list()
    store.acknowledge(
        [SIGNAL_A],
        expected_revision=before.revision,
        consumer="resident-mind",
    )
    queue_size = store.inputs_path.stat().st_size
    monkeypatch.setattr(resident_signals_module, "MAX_SIGNAL_QUEUE_BYTES", queue_size + 1)

    with pytest.raises(ValidationError, match="queue is full"):
        store.append(
            kind="task-checkpoint",
            event_key="capacity:blocked",
            envelope={"summary": "This event does not fit before compaction."},
        )

    compacted = store.compact(retain_recent=1)
    appended = store.append(
        kind="task-checkpoint",
        event_key="capacity:recovered",
        envelope={"summary": "This event fits after compaction."},
    )

    assert compacted.archived_signals == 1
    assert [item.input_id for item in store.list().signals] == [SIGNAL_B, appended.input_id]


def test_current_acknowledgement_requires_and_replays_one_exact_disposition(
    tmp_path: Path,
) -> None:
    store = ResidentSignalStore(tmp_path)
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A)])
    before = store.list()

    with pytest.raises(ValidationError, match="durable disposition reference"):
        store.acknowledge(
            [SIGNAL_A],
            expected_revision=before.revision,
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="accepted",
        )
    with pytest.raises(ValidationError, match="opaque record revision"):
        store.acknowledge(
            [SIGNAL_A],
            expected_revision=before.revision,
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="accepted",
            result_refs=("provider body with sk-live-secret",),
        )

    accepted = store.acknowledge(
        [SIGNAL_A],
        expected_revision=before.revision,
        consumer="pulse:019f0000-0000-7000-8000-000000000099",
        disposition="accepted",
        result_refs=(f"task:cut-over-resident-mind@{REVISION_A}",),
        acknowledged_at=datetime(2026, 7, 29, 8, 10, tzinfo=UTC),
    )
    assert accepted[0].disposition == "accepted"
    assert accepted[0].result_refs == (f"task:cut-over-resident-mind@{REVISION_A}",)
    current = ResidentSignalStore(tmp_path).list(include_acknowledged=True)
    assert (
        ResidentSignalStore(tmp_path).acknowledge(
            [SIGNAL_A],
            expected_revision=current.revision,
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="accepted",
            result_refs=(f"task:cut-over-resident-mind@{REVISION_A}",),
        )
        == accepted
    )
    with pytest.raises(ConflictError, match="different disposition"):
        store.acknowledge(
            [SIGNAL_A],
            expected_revision=current.revision,
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="rejected",
            result_refs=(f"task:cut-over-resident-mind@{REVISION_B}",),
        )


def test_work_thread_recheck_ack_requires_closed_or_rearmed_current_revision(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Recheck guard")
    observed = datetime(2026, 7, 29, 8, tzinfo=UTC)
    thread = vault.create_thread(
        identifier="guarded-recheck",
        title="Guarded recheck",
        purpose="Prove evidence cannot disappear before semantic handling.",
        summary="The authored review horizon is due.",
        recheck_at="2026-07-29T09:00:00Z",
        observed_at=observed,
    )
    signal = vault.append_resident_signal(
        kind="work-thread-recheck",
        ref=thread.identifier,
        event_key=f"work-thread-recheck:{thread.identifier}:{thread.recheck_at}",
        envelope={
            "work_thread_id": thread.identifier,
            "recheck_at": thread.recheck_at,
            "thread_updated_at": thread.updated_at,
        },
        observed_at=observed + timedelta(hours=1),
    )
    before = vault.resident_signal_status()
    with pytest.raises(ConflictError, match="remains due"):
        vault.acknowledge_resident_signals(
            (signal["input_id"],),
            expected_revision=before["revision"],
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="accepted",
            result_refs=(f"{thread.identifier}@{thread.revision}",),
            acknowledged_at=observed + timedelta(hours=2),
        )

    rearmed = vault.update_thread(
        thread.identifier,
        expected_revision=thread.revision,
        recheck_at="2026-07-30T09:00:00Z",
        note="Reviewed the due horizon and scheduled the next one.",
        observed_at=observed + timedelta(hours=2),
    )
    acknowledgements = vault.acknowledge_resident_signals(
        (signal["input_id"],),
        expected_revision=before["revision"],
        consumer="pulse:019f0000-0000-7000-8000-000000000099",
        disposition="accepted",
        result_refs=(f"{rearmed.identifier}@{rearmed.revision}",),
        acknowledged_at=observed + timedelta(hours=2),
    )
    after = vault.resident_signal_status()
    assert acknowledgements[0]["disposition"] == "accepted"
    assert after["pending"] == 0

    closed = vault.update_thread(
        rearmed.identifier,
        expected_revision=rearmed.revision,
        status="resolved",
        clear_recheck_at=True,
        note="Closed after the acknowledged review.",
        observed_at=observed + timedelta(hours=3),
    )
    assert (
        vault.acknowledge_resident_signals(
            (signal["input_id"],),
            expected_revision=after["revision"],
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="accepted",
            result_refs=(f"{rearmed.identifier}@{rearmed.revision}",),
            acknowledged_at=observed + timedelta(hours=3),
        )
        == acknowledgements
    )
    assert closed.status == "resolved"


def test_doctor_validates_the_complete_resident_mailbox(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Signal doctor")
    store = ResidentSignalStore(vault.root)
    store.append(kind="source-due", envelope={"source": "gmail"})

    healthy = vault.doctor()
    assert healthy.healthy
    assert healthy.counts["signals"] == 1
    assert healthy.counts["signals_pending"] == 1

    store.acknowledgements_path.write_text(
        '{"acknowledgement_id":"not-a-uuid"}\n',
        encoding="utf-8",
    )
    failed = Vault(vault.root).doctor()
    assert not failed.healthy
    assert any(issue.code == "invalid-resident-signals" for issue in failed.issues)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exchange crash injection is unavailable")
def test_signal_append_hard_crash_after_exchange_keeps_complete_queue_for_restart(
    tmp_path: Path,
) -> None:
    store = ResidentSignalStore(tmp_path)
    first = store.append(
        kind="source-due",
        event_key="exchange-crash:first",
        envelope={"source": "first"},
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; from pathlib import Path; "
                "from continuity_kernel import atomic; "
                "from continuity_kernel.resident_signals import ResidentSignalStore; "
                "actual=atomic._exchange_regular_files_at; "
                "exec('def crash(parent,left,right):\\n "
                "actual(parent,left,right)\\n os._exit(91)'); "
                "atomic._exchange_regular_files_at=crash; "
                "ResidentSignalStore(Path(sys.argv[1])).append("
                "kind='source-due',event_key='exchange-crash:second',"
                "envelope={'source':'second'})"
            ),
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == 91, crashed.stderr
    visible = ResidentSignalStore(tmp_path).list(include_acknowledged=True)
    assert [item.event_key for item in visible.signals] == [
        first.event_key,
        "exchange-crash:second",
    ]


def test_compaction_recovery_rejects_foreign_live_append_and_preserves_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    signal_c = "019f0000-0000-7000-8000-000000000003"
    signal_d = "019f0000-0000-7000-8000-000000000004"
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B), _signal(signal_c)])
    store.acknowledge(
        [SIGNAL_A, SIGNAL_B],
        expected_revision=store.list().revision,
        consumer="resident-mind",
    )
    actual_replace = ResidentSignalStore._replace_exact
    crashed = False

    def crash_after_archives(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal crashed
        actual_replace(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        if (
            not crashed
            and path.parent == store.root / "archive"
            and path.name.startswith("acks-")
            and store.compaction_marker_path.exists()
        ):
            crashed = True
            raise SystemExit("crash before live queue replacement")

    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", crash_after_archives)
    with pytest.raises(SystemExit, match="before live"):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", actual_replace)

    with store.inputs_path.open("ab") as handle:
        handle.write((json.dumps(_signal(signal_d), sort_keys=True) + "\n").encode())
    foreign_live = store.inputs_path.read_bytes()

    with pytest.raises(ConflictError, match="live queue changed"):
        ResidentSignalStore(tmp_path).status()

    assert store.inputs_path.read_bytes() == foreign_live
    assert store.compaction_marker_path.exists()


def test_legacy_compaction_marker_without_prestate_fails_closed_and_doctor_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Legacy compaction marker")
    store = ResidentSignalStore(vault.root)
    signal_c = "019f0000-0000-7000-8000-000000000003"
    _write_jsonl(store.inputs_path, [_signal(SIGNAL_A), _signal(SIGNAL_B), _signal(signal_c)])
    store.acknowledge(
        [SIGNAL_A, SIGNAL_B],
        expected_revision=store.list().revision,
        consumer="resident-mind",
    )
    actual_replace = ResidentSignalStore._replace_exact

    def crash_before_live(
        signal_store: ResidentSignalStore,
        pinned: atomic.PinnedPathRoot | None,
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        actual_replace(signal_store, pinned, path, *args, **kwargs)  # type: ignore[arg-type]
        if path.parent == store.root / "archive" and path.name.startswith("acks-"):
            raise SystemExit("legacy marker fixture")

    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", crash_before_live)
    with pytest.raises(SystemExit, match="legacy marker"):
        store.compact(retain_recent=1)
    monkeypatch.setattr(ResidentSignalStore, "_replace_exact", actual_replace)
    marker = json.loads(store.compaction_marker_path.read_bytes())
    marker.pop("expected_digests")
    store.compaction_marker_path.write_text(
        json.dumps(marker, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    before = store.inputs_path.read_bytes()

    with pytest.raises(ConflictError, match="legacy resident signal compaction"):
        ResidentSignalStore(vault.root).status()
    report = vault.doctor()

    assert store.inputs_path.read_bytes() == before
    issue = next(issue for issue in report.issues if issue.code == "invalid-resident-signals")
    assert "lacks exact live prestate" in issue.message


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    (
        (atomic.PublishOutcome.UNPUBLISHED, PersistenceError),
        (atomic.PublishOutcome.COMMITTED, MutationCommittedError),
        (atomic.PublishOutcome.UNKNOWN, DegradedIntegrityError),
    ),
)
def test_signal_publish_outcomes_map_to_public_continuity_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: atomic.PublishOutcome,
    error_type: type[Exception],
) -> None:
    store = ResidentSignalStore(tmp_path)
    store.append(kind="source-due", envelope={"source": "first"})

    def fail_exchange(*args: object, **kwargs: object) -> None:
        raise atomic.DurablePublishError("injected signal outcome", outcome=outcome)

    if atomic.PINNED_PATH_ROOT_SUPPORTED:
        monkeypatch.setattr(
            atomic.PinnedPathRoot,
            "exchange_regular_file_if_exact",
            fail_exchange,
        )
    else:
        monkeypatch.setattr(resident_signals_module, "atomic_write", fail_exchange)
    with pytest.raises(error_type):
        store.append_many_results(
            (
                SignalAppendRequest(kind="source-due", envelope={"source": "second"}),
                SignalAppendRequest(kind="source-due", envelope={"source": "third"}),
            )
        )


def test_unpinned_signal_publication_error_keeps_its_durable_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ResidentSignalStore(tmp_path)
    store.append(kind="source-due", envelope={"source": "first"})
    monkeypatch.setattr(resident_signals_module, "PINNED_PATH_ROOT_SUPPORTED", False)

    def fail_publish(*args: object, **kwargs: object) -> None:
        raise atomic.DurablePublishError(
            "injected fallback outcome",
            outcome=atomic.PublishOutcome.UNKNOWN,
        )

    monkeypatch.setattr(resident_signals_module, "atomic_write", fail_publish)

    with pytest.raises(DegradedIntegrityError, match="unknown publication state"):
        store.append(kind="source-due", envelope={"source": "second"})
