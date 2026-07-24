from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import control_queue as control_queue_module
from continuity_kernel import operations as operations_module
from continuity_kernel import vault_backup as vault_backup_module
from continuity_kernel.atomic import (
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    sha256_bytes,
)
from continuity_kernel.control_queue import EMPTY_REVISION, ControlStorageError
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    ValidationError,
)
from continuity_kernel.operations import (
    MAX_DISPOSITION_BYTES,
    MAX_DISPOSITIONS,
    ControlDisposition,
    DispositionDecision,
    OperationLedger,
    capture_operation_binding,
    disposition_dict,
)
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)


def _ledger(tmp_path: Path) -> OperationLedger:
    root = tmp_path / "vault"
    Vault(root).initialize(name="Operation ledger")
    return OperationLedger(root)


def _append(ledger: OperationLedger, *, expected: str = EMPTY_REVISION) -> str:
    return ledger.queue.append(
        kind="setup_choice",
        subject="source:gmail",
        choice="selected",
        expected_revision=expected,
        observed_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    ).revision


def _is_disposition_log(relative: Path | str) -> bool:
    name = Path(relative).name
    return name.startswith("dispositions-") and name.endswith(".jsonl") and ".head." not in name


def _control_store_bytes(ledger: OperationLedger) -> dict[str, bytes]:
    root = ledger.vault_root / ".gsv/control"
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _vault_file_bytes(ledger: OperationLedger) -> dict[str, bytes]:
    return {
        path.relative_to(ledger.vault_root).as_posix(): path.read_bytes()
        for path in sorted(ledger.vault_root.rglob("*"))
        if path.is_file()
    }


def _root_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _append_decide_and_archive(ledger: OperationLedger, *, subject: str) -> dict[str, Any]:
    current = ledger.snapshot()
    appended = ledger.queue.append(
        kind="correction",
        subject=subject,
        choice=f"Close {subject}.",
        expected_revision=current.queue_revision,
    )
    pending = ledger.snapshot()
    closed = ledger.decide(
        event_id=appended.events[-1].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=pending.queue_revision,
        expected_disposition_revision=pending.disposition_revision,
    )
    return ledger.archive_closed(
        expected_queue_revision=closed.queue_revision,
        expected_disposition_revision=closed.disposition_revision,
    )


def _expected_initialized_head(ledger: OperationLedger, generation: int) -> bytes:
    content = ledger._path_for_generation(generation).read_bytes()
    encoded = ledger._marker_path_for_generation(generation).read_bytes()
    head = operations_module._parse_disposition_head(encoded)
    assert head.state == "initialized"
    assert head.current == operations_module._disposition_state(content)
    return operations_module._encode_disposition_head(head)


def test_accept_disposition_is_durable_and_exactly_once(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    event_id = before.pending[0].event_id

    accepted = ledger.decide(
        event_id=event_id,
        decision="accepted",
        actor_ref="core:onboard-doctor",
        reason_code="supported-setup-choice",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
        result_ref="onboarding:session",
        observed_at=datetime(2026, 7, 24, 12, 1, tzinfo=UTC),
    )
    fresh = OperationLedger(ledger.vault_root).snapshot()

    assert accepted.pending == ()
    assert accepted.decided[0][1].decision.value == "accepted"
    assert accepted.decided[0][1].acknowledged_at == "2026-07-24T12:01:00.000000Z"
    assert fresh == accepted
    assert ledger._marker_path_for_generation(0).read_bytes() == _expected_initialized_head(
        ledger, 0
    )
    with pytest.raises(ConflictError, match="already"):
        ledger.decide(
            event_id=event_id,
            decision="rejected",
            actor_ref="core:onboard-doctor",
            reason_code="changed-mind",
            expected_queue_revision=fresh.queue_revision,
            expected_disposition_revision=fresh.disposition_revision,
        )


def test_snapshot_is_byte_for_byte_pure_before_first_disposition(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    appended_revision = _append(ledger)
    before = _control_store_bytes(ledger)

    first = ledger.snapshot()
    second = OperationLedger(ledger.vault_root).snapshot()

    assert first == second
    assert first.queue_revision == appended_revision
    assert first.disposition_revision == EMPTY_REVISION
    assert len(first.pending) == 1
    assert _control_store_bytes(ledger) == before
    assert not ledger._marker_path_for_generation(0).exists()


def test_snapshot_is_whole_vault_pure_before_the_control_store_exists(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    before = _vault_file_bytes(ledger)

    first = ledger.snapshot()
    second = OperationLedger(ledger.vault_root).snapshot()

    assert first == second
    assert first.vault_id == Vault(ledger.vault_root).identity()["vault_id"]
    assert first.queue_revision == EMPTY_REVISION
    assert _vault_file_bytes(ledger) == before
    assert not (ledger.vault_root / ".gsv/control").exists()


def test_markerless_empty_control_directory_is_recoverable_before_first_intent(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    (ledger.vault_root / ".gsv/control").mkdir()

    before = OperationLedger(ledger.vault_root).snapshot()
    appended_revision = _append(ledger, expected=before.queue_revision)
    after = OperationLedger(ledger.vault_root).snapshot()

    assert before.queue_revision == EMPTY_REVISION
    assert before.queue_generation == 0
    assert before.previous_queue_revision is None
    assert before.disposition_revision == EMPTY_REVISION
    assert before.pending == ()
    assert before.decided == ()
    assert before.archived == ()
    assert appended_revision != EMPTY_REVISION
    assert len(after.pending) == 1


def test_committed_disposition_publication_reports_honest_outcome_and_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    actual_write = PinnedPathRoot.atomic_write

    def write_then_report_committed(
        store: PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        actual_write(store, relative, content, mode=mode)
        if _is_disposition_log(relative):
            raise DurablePublishError(
                "injected disposition directory fsync failure",
                outcome=PublishOutcome.COMMITTED,
            )

    monkeypatch.setattr(
        PinnedPathRoot,
        "atomic_write",
        write_then_report_committed,
    )
    with pytest.raises(MutationCommittedError, match="visible"):
        ledger.decide(
            event_id=before.pending[0].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=before.queue_revision,
            expected_disposition_revision=before.disposition_revision,
        )

    fresh = OperationLedger(ledger.vault_root).snapshot()
    assert fresh.pending == ()
    assert fresh.decided[0][1].decision is DispositionDecision.ACCEPTED


def test_unknown_disposition_publication_fails_degraded_without_false_unchanged_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()

    def report_unknown(
        store: PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        if _is_disposition_log(relative):
            raise DurablePublishError(
                "injected unknown disposition publication",
                outcome=PublishOutcome.UNKNOWN,
            )
        actual_write(store, relative, content, mode=mode)

    actual_write = PinnedPathRoot.atomic_write
    monkeypatch.setattr(PinnedPathRoot, "atomic_write", report_unknown)
    with pytest.raises(DegradedIntegrityError, match="unknown visible state"):
        ledger.decide(
            event_id=before.pending[0].event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="unsupported",
            expected_queue_revision=before.queue_revision,
            expected_disposition_revision=before.disposition_revision,
        )

    observed = OperationLedger(ledger.vault_root).snapshot()
    assert observed.pending == before.pending
    assert observed.decided == before.decided
    assert observed.disposition_revision == before.disposition_revision
    assert observed.queue_revision != before.queue_revision


def test_unpublished_disposition_io_failure_preserves_the_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()

    def fail_before_publication(
        store: PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        if _is_disposition_log(relative):
            raise OSError("injected pre-publication failure")
        actual_write(store, relative, content, mode=mode)

    actual_write = PinnedPathRoot.atomic_write
    monkeypatch.setattr(
        PinnedPathRoot,
        "atomic_write",
        fail_before_publication,
    )
    with pytest.raises(ValidationError, match="was not published"):
        ledger.decide(
            event_id=before.pending[0].event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="unsupported",
            expected_queue_revision=before.queue_revision,
            expected_disposition_revision=before.disposition_revision,
        )

    observed = OperationLedger(ledger.vault_root).snapshot()
    assert observed.pending == before.pending
    assert observed.decided == before.decided
    assert observed.disposition_revision == before.disposition_revision
    assert observed.queue_revision != before.queue_revision


def test_disposition_bounds_can_close_a_maximum_sized_queue_generation() -> None:
    largest = ControlDisposition(
        schema_version=1,
        disposition_id="00000000-0000-4000-8000-000000000000",
        event_id="00000000-0000-4000-8000-000000000001",
        event_digest="f" * 64,
        decision=DispositionDecision.ACCEPTED,
        actor_ref=f"{'a' * 32}:{'b' * 128}",
        reason_code="r" * 64,
        result_ref="x" * 512,
        acknowledged_at="2026-07-24T12:01:00.000000Z",
    )
    encoded_size = len(
        json.dumps(
            disposition_dict(largest),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )

    assert MAX_DISPOSITIONS == control_queue_module.MAX_EVENTS
    assert encoded_size * MAX_DISPOSITIONS <= MAX_DISPOSITION_BYTES


def test_reject_disposition_requires_current_queue_and_disposition_revisions(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    snapshot = ledger.snapshot()
    event_id = snapshot.pending[0].event_id

    with pytest.raises(ConflictError, match="queue changed"):
        ledger.decide(
            event_id=event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="unsupported-choice",
            expected_queue_revision="0" * 64,
            expected_disposition_revision=snapshot.disposition_revision,
        )
    with pytest.raises(ConflictError, match="dispositions changed"):
        ledger.decide(
            event_id=event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="unsupported-choice",
            expected_queue_revision=snapshot.queue_revision,
            expected_disposition_revision="0" * 64,
        )


def test_new_control_event_preserves_the_current_disposition_head_anchor(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    accepted = ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    anchored_head = ledger.queue.snapshot().disposition_head_revision

    appended = ledger.queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="Preserve the first acknowledgement while adding this event.",
        expected_revision=accepted.queue_revision,
    )
    refreshed = ledger.snapshot()

    assert anchored_head is not None
    assert appended.disposition_head_revision == anchored_head
    assert refreshed.queue_revision == appended.revision
    assert len(refreshed.decided) == 1
    assert len(refreshed.pending) == 1


def test_bounded_queue_recovers_only_after_every_event_is_dispositioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(control_queue_module, "MAX_EVENTS", 2)
    monkeypatch.setattr(operations_module, "MAX_DISPOSITIONS", 2)
    ledger = _ledger(tmp_path)
    first_revision = _append(ledger)
    second_revision = ledger.queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="Friday evening is protected.",
        expected_revision=first_revision,
    ).revision
    with pytest.raises(ValidationError, match="event limit"):
        ledger.queue.append(
            kind="approval",
            subject="operation:test",
            choice="approve",
            expected_revision=second_revision,
        )

    first = ledger.snapshot()
    accepted = ledger.decide(
        event_id=first.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=first.queue_revision,
        expected_disposition_revision=first.disposition_revision,
    )
    with pytest.raises(ConflictError, match="pending"):
        ledger.archive_closed(
            expected_queue_revision=accepted.queue_revision,
            expected_disposition_revision=accepted.disposition_revision,
        )
    closed = ledger.decide(
        event_id=accepted.pending[0].event_id,
        decision="rejected",
        actor_ref="core:doctor",
        reason_code="out-of-scope",
        expected_queue_revision=accepted.queue_revision,
        expected_disposition_revision=accepted.disposition_revision,
    )
    first_generation_dispositions = ledger.path
    recovery = ledger.archive_closed(
        expected_queue_revision=closed.queue_revision,
        expected_disposition_revision=closed.disposition_revision,
        observed_at=datetime(2026, 7, 24, 12, 2, tzinfo=UTC),
    )
    fresh = OperationLedger(ledger.vault_root).snapshot()

    assert recovery["archived_events"] == 2
    assert fresh.queue_generation == 1
    assert fresh.previous_queue_revision == closed.queue_revision
    assert fresh.pending == ()
    assert fresh.decided == ()
    assert len(fresh.archived) == 1
    archived = fresh.archived[0]
    assert archived.queue_generation == 0
    assert archived.queue_revision == closed.queue_revision
    assert archived.previous_queue_revision is None
    assert archived.disposition_revision == closed.disposition_revision
    assert [item.decision.value for _, item in archived.decided] == ["accepted", "rejected"]
    assert fresh.to_dict()["archived"][0]["queue_revision"] == closed.queue_revision
    assert fresh.queue_revision != EMPTY_REVISION
    assert first_generation_dispositions.exists()
    assert ledger.path != first_generation_dispositions
    with pytest.raises(ConflictError, match="changed"):
        ledger.queue.append(
            kind="setup_choice",
            subject="source:slack",
            choice="selected",
            expected_revision=EMPTY_REVISION,
        )
    appended = ledger.queue.append(
        kind="setup_choice",
        subject="source:slack",
        choice="selected",
        expected_revision=fresh.queue_revision,
    )
    assert appended.generation == 1
    new_generation = ledger.snapshot()
    accepted_again = ledger.decide(
        event_id=new_generation.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=new_generation.queue_revision,
        expected_disposition_revision=new_generation.disposition_revision,
    )
    assert len(accepted_again.decided) == 1


def test_archived_decisions_survive_backup_restore_with_exact_lineage(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    pending = ledger.snapshot()
    closed = ledger.decide(
        event_id=pending.pending[0].event_id,
        decision="rejected",
        actor_ref="core:doctor",
        reason_code="unsupported",
        expected_queue_revision=pending.queue_revision,
        expected_disposition_revision=pending.disposition_revision,
        result_ref="control:rejected/setup-choice",
    )
    recovery = ledger.archive_closed(
        expected_queue_revision=closed.queue_revision,
        expected_disposition_revision=closed.disposition_revision,
        observed_at=datetime(2026, 7, 24, 12, 2, tzinfo=UTC),
    )
    expected = OperationLedger(ledger.vault_root).snapshot()

    backup = Vault(ledger.vault_root).create_backup(tmp_path / "archived-control.zip")
    restored_root = tmp_path / "restored-archived-control"
    Vault.restore_backup(Path(backup["backup"]), restored_root)
    restored = OperationLedger(restored_root).snapshot()

    assert restored == expected
    assert restored.previous_queue_revision == closed.queue_revision
    assert len(restored.archived) == 1
    assert restored.archived[0].queue_revision == closed.queue_revision
    assert restored.archived[0].disposition_revision == closed.disposition_revision
    assert restored.archived[0].decided[0][1].decision is DispositionDecision.REJECTED
    restored_ledger = OperationLedger(restored_root)
    assert restored_ledger._marker_path_for_generation(
        0
    ).read_bytes() == _expected_initialized_head(restored_ledger, 0)
    restored_queue = restored_ledger.queue.snapshot()
    successor_head_bytes = restored_ledger._marker_path_for_generation(1).read_bytes()
    successor_head = operations_module._parse_disposition_head(successor_head_bytes)
    assert successor_head.current == operations_module._disposition_state(b"")
    assert restored_queue.disposition_head_revision == sha256_bytes(successor_head_bytes)
    archived_queue_bytes = (restored_root / recovery["archive"]).read_bytes()
    archived_header, _ = control_queue_module._parse_queue_document(archived_queue_bytes)
    assert archived_header is not None
    assert archived_header.disposition_head_revision == sha256_bytes(
        restored_ledger._marker_path_for_generation(0).read_bytes()
    )


def test_successor_queue_lineage_rejects_archived_head_and_log_rollback(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    first_revision = _append(ledger)
    ledger.queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="Preserve both archived acknowledgements.",
        expected_revision=first_revision,
    )
    before = ledger.snapshot()
    after_first = ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    first_log = ledger.path.read_bytes()
    first_head = ledger._marker_path_for_generation(0).read_bytes()
    closed = ledger.decide(
        event_id=after_first.pending[0].event_id,
        decision="rejected",
        actor_ref="core:doctor",
        reason_code="unsupported",
        expected_queue_revision=after_first.queue_revision,
        expected_disposition_revision=after_first.disposition_revision,
    )
    ledger.archive_closed(
        expected_queue_revision=closed.queue_revision,
        expected_disposition_revision=closed.disposition_revision,
    )

    ledger._path_for_generation(0).write_bytes(first_log)
    ledger._marker_path_for_generation(0).write_bytes(first_head)

    with pytest.raises(ControlStorageError, match="does not match its queue anchor"):
        OperationLedger(ledger.vault_root).snapshot()


def test_archived_visibility_fails_closed_on_missing_or_mismatched_disposition(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    pending = ledger.snapshot()
    closed = ledger.decide(
        event_id=pending.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=pending.queue_revision,
        expected_disposition_revision=pending.disposition_revision,
    )
    disposition_path = ledger.path
    disposition_bytes = disposition_path.read_bytes()
    ledger.archive_closed(
        expected_queue_revision=closed.queue_revision,
        expected_disposition_revision=closed.disposition_revision,
    )

    disposition_path.unlink()
    with pytest.raises(ValidationError, match="disappeared after initialization"):
        OperationLedger(ledger.vault_root).snapshot()

    payload = json.loads(disposition_bytes)
    payload["event_digest"] = "f" * 64
    disposition_path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="does not match"):
        OperationLedger(ledger.vault_root).snapshot()


def test_initialized_live_disposition_cannot_be_deleted_to_resurrect_an_event(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    accepted = ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    marker = ledger._marker_path_for_generation(accepted.queue_generation)
    assert marker.read_bytes() == _expected_initialized_head(
        ledger,
        accepted.queue_generation,
    )
    ledger.path.unlink()

    restarted = OperationLedger(ledger.vault_root)
    with pytest.raises(ControlStorageError, match="disappeared after initialization"):
        restarted.snapshot()
    with pytest.raises(ControlStorageError, match="disappeared after initialization"):
        restarted.decide(
            event_id=before.pending[0].event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="changed-mind",
            expected_queue_revision=accepted.queue_revision,
            expected_disposition_revision=EMPTY_REVISION,
        )


def test_initialized_head_rejects_valid_prefix_rollback_and_redisposition(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    first_revision = _append(ledger)
    appended = ledger.queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="Keep the second correction distinct.",
        expected_revision=first_revision,
    )
    before = ledger.snapshot()
    after_first = ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    valid_prefix = ledger.path.read_bytes()
    ledger.decide(
        event_id=after_first.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=after_first.queue_revision,
        expected_disposition_revision=after_first.disposition_revision,
    )

    ledger.path.write_bytes(valid_prefix)

    with pytest.raises(ControlStorageError, match="cryptographic head"):
        OperationLedger(ledger.vault_root).snapshot()
    with pytest.raises(ControlStorageError, match="cryptographic head"):
        OperationLedger(ledger.vault_root).decide(
            event_id=appended.events[-1].event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="changed-mind",
            expected_queue_revision=after_first.queue_revision,
            expected_disposition_revision=after_first.disposition_revision,
        )


def test_queue_anchor_rejects_coordinated_head_and_log_rollback(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    first_revision = _append(ledger)
    second_event = ledger.queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="The second event must stay acknowledged.",
        expected_revision=first_revision,
    ).events[-1]
    before = ledger.snapshot()
    after_first = ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    first_log = ledger.path.read_bytes()
    first_head = ledger._marker_path_for_generation(0).read_bytes()
    after_second = ledger.decide(
        event_id=second_event.event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=after_first.queue_revision,
        expected_disposition_revision=after_first.disposition_revision,
    )

    ledger.path.write_bytes(first_log)
    ledger._marker_path_for_generation(0).write_bytes(first_head)

    with pytest.raises(ControlStorageError, match="does not descend from its queue anchor"):
        OperationLedger(ledger.vault_root).snapshot()
    with pytest.raises(ControlStorageError, match="does not descend from its queue anchor"):
        OperationLedger(ledger.vault_root).decide(
            event_id=second_event.event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="changed-mind",
            expected_queue_revision=after_second.queue_revision,
            expected_disposition_revision=after_first.disposition_revision,
        )
    assert ledger.queue.snapshot().revision == after_second.queue_revision


def test_queue_anchor_rejects_deleting_both_live_head_and_log(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    accepted = ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    ledger.path.unlink()
    ledger._marker_path_for_generation(0).unlink()

    with pytest.raises(ControlStorageError, match="head disappeared after queue anchoring"):
        OperationLedger(ledger.vault_root).snapshot()
    with pytest.raises(ControlStorageError, match="head disappeared after queue anchoring"):
        OperationLedger(ledger.vault_root).decide(
            event_id=before.pending[0].event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="changed-mind",
            expected_queue_revision=accepted.queue_revision,
            expected_disposition_revision=EMPTY_REVISION,
        )


def test_initialized_head_rejects_valid_log_replacement_or_missing_head(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    stored = ledger.path.read_bytes()
    replaced = stored.replace(b'"decision":"accepted"', b'"decision":"rejected"')
    assert replaced != stored
    ledger.path.write_bytes(replaced)

    with pytest.raises(ControlStorageError, match="cryptographic head"):
        OperationLedger(ledger.vault_root).snapshot()

    ledger.path.write_bytes(stored)
    ledger._marker_path_for_generation(0).unlink()
    with pytest.raises(ControlStorageError, match="disappeared after queue anchoring"):
        OperationLedger(ledger.vault_root).snapshot()


def test_preparing_head_recovers_the_previous_log_after_unpublished_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    first_revision = _append(ledger)
    ledger.queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="A second bounded intent.",
        expected_revision=first_revision,
    )
    first = ledger.snapshot()
    before_second = ledger.decide(
        event_id=first.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=first.queue_revision,
        expected_disposition_revision=first.disposition_revision,
    )
    actual_write = PinnedPathRoot.atomic_write

    def fail_log_write(
        store: PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        if _is_disposition_log(relative):
            raise OSError("injected unpublished append")
        actual_write(store, relative, content, mode=mode)

    monkeypatch.setattr(PinnedPathRoot, "atomic_write", fail_log_write)
    with pytest.raises(ControlStorageError, match="was not published"):
        ledger.decide(
            event_id=before_second.pending[0].event_id,
            decision="rejected",
            actor_ref="core:doctor",
            reason_code="unsupported",
            expected_queue_revision=before_second.queue_revision,
            expected_disposition_revision=before_second.disposition_revision,
        )

    before_view = _control_store_bytes(ledger)
    observed = OperationLedger(ledger.vault_root).snapshot()
    assert observed == before_second
    assert _control_store_bytes(ledger) == before_view
    preparing_head = operations_module._parse_disposition_head(
        ledger._marker_path_for_generation(observed.queue_generation).read_bytes()
    )
    assert preparing_head.state == "preparing"

    monkeypatch.setattr(PinnedPathRoot, "atomic_write", actual_write)
    recovered = ledger.decide(
        event_id=observed.pending[0].event_id,
        decision="rejected",
        actor_ref="core:doctor",
        reason_code="unsupported",
        expected_queue_revision=observed.queue_revision,
        expected_disposition_revision=observed.disposition_revision,
    )
    recovered_head = operations_module._parse_disposition_head(
        ledger._marker_path_for_generation(recovered.queue_generation).read_bytes()
    )
    assert recovered.pending == ()
    assert recovered_head.state == "initialized"


def test_preparing_head_recovers_the_proposed_log_after_final_head_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    actual_write = PinnedPathRoot.atomic_write
    head_writes = 0

    def fail_final_head_once(
        store: PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        nonlocal head_writes
        if str(relative).endswith(".head.jsonl"):
            head_writes += 1
            if head_writes == 3:
                raise OSError("injected final head failure")
        actual_write(store, relative, content, mode=mode)

    monkeypatch.setattr(PinnedPathRoot, "atomic_write", fail_final_head_once)
    with pytest.raises(DegradedIntegrityError, match="initialization is incomplete"):
        ledger.decide(
            event_id=before.pending[0].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=before.queue_revision,
            expected_disposition_revision=before.disposition_revision,
        )

    before_view = _control_store_bytes(ledger)
    observed = OperationLedger(ledger.vault_root).snapshot()
    assert observed.pending == ()
    assert observed.decided[0][1].decision is DispositionDecision.ACCEPTED
    assert head_writes == 3
    assert _control_store_bytes(ledger) == before_view
    assert (
        operations_module._parse_disposition_head(
            ledger._marker_path_for_generation(0).read_bytes()
        ).state
        == "preparing"
    )

    monkeypatch.setattr(PinnedPathRoot, "atomic_write", actual_write)
    archived = ledger.archive_closed(
        expected_queue_revision=observed.queue_revision,
        expected_disposition_revision=observed.disposition_revision,
    )
    assert archived["archived_events"] == 1


def test_successor_head_is_bound_after_one_unpublished_queue_anchor_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    actual_write = PinnedPathRoot.atomic_write
    queue_writes = 0

    def fail_anchor_once(
        store: PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        nonlocal queue_writes
        if str(relative) == ".gsv/control/queue.jsonl":
            queue_writes += 1
            if queue_writes == 2:
                raise OSError("injected queue anchor failure")
        actual_write(store, relative, content, mode=mode)

    monkeypatch.setattr(PinnedPathRoot, "atomic_write", fail_anchor_once)
    with pytest.raises(DegradedIntegrityError, match="queue anchor is incomplete"):
        ledger.decide(
            event_id=before.pending[0].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=before.queue_revision,
            expected_disposition_revision=before.disposition_revision,
        )

    before_view = _control_store_bytes(ledger)
    observed = OperationLedger(ledger.vault_root).snapshot()
    head_bytes = ledger._marker_path_for_generation(0).read_bytes()
    assert queue_writes == 2
    assert observed.pending == ()
    assert observed.decided[0][1].decision is DispositionDecision.ACCEPTED
    assert _control_store_bytes(ledger) == before_view
    assert observed.queue_revision == ledger.queue.snapshot().revision
    assert ledger.queue.snapshot().disposition_head_revision != sha256_bytes(head_bytes)

    monkeypatch.setattr(PinnedPathRoot, "atomic_write", actual_write)
    archived = ledger.archive_closed(
        expected_queue_revision=observed.queue_revision,
        expected_disposition_revision=observed.disposition_revision,
    )
    assert archived["archived_events"] == 1


def test_decide_validates_the_full_queue_lineage_before_mutating(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    oldest = _append_decide_and_archive(ledger, subject="record:first")
    _append_decide_and_archive(ledger, subject="record:second")
    current = ledger.snapshot()
    appended = ledger.queue.append(
        kind="correction",
        subject="record:third",
        choice="Keep this pending.",
        expected_revision=current.queue_revision,
    )
    pending = ledger.snapshot()
    oldest_archive = ledger.vault_root / oldest["archive"]
    oldest_archive.write_bytes(b"corrupt old history\n")

    assert ledger.snapshot() == pending
    with pytest.raises(ControlStorageError, match="predecessor archive"):
        ledger.decide(
            event_id=appended.events[-1].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=pending.queue_revision,
            expected_disposition_revision=pending.disposition_revision,
        )
    assert not ledger._path_for_generation(pending.queue_generation).exists()


def test_archive_validates_the_full_queue_lineage_before_mutating(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    oldest = _append_decide_and_archive(ledger, subject="record:first")
    _append_decide_and_archive(ledger, subject="record:second")
    current = ledger.snapshot()
    appended = ledger.queue.append(
        kind="correction",
        subject="record:third",
        choice="Close only after auditing every ancestor.",
        expected_revision=current.queue_revision,
    )
    pending = ledger.snapshot()
    closed = ledger.decide(
        event_id=appended.events[-1].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=pending.queue_revision,
        expected_disposition_revision=pending.disposition_revision,
    )
    oldest_archive = ledger.vault_root / oldest["archive"]
    oldest_archive.write_bytes(b"corrupt old history\n")

    assert ledger.snapshot() == closed
    with pytest.raises(ControlStorageError, match="predecessor archive"):
        ledger.archive_closed(
            expected_queue_revision=closed.queue_revision,
            expected_disposition_revision=closed.disposition_revision,
        )
    assert ledger.queue.snapshot().generation == closed.queue_generation


def test_decide_validates_every_archived_disposition_generation_before_mutating(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _append_decide_and_archive(ledger, subject="record:first")
    _append_decide_and_archive(ledger, subject="record:second")
    current = ledger.snapshot()
    appended = ledger.queue.append(
        kind="correction",
        subject="record:third",
        choice="Do not mutate across missing old acknowledgements.",
        expected_revision=current.queue_revision,
    )
    pending = ledger.snapshot()
    ledger._path_for_generation(0).unlink()

    assert ledger.snapshot() == pending
    with pytest.raises(ControlStorageError, match="disappeared after initialization"):
        ledger.decide(
            event_id=appended.events[-1].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=pending.queue_revision,
            expected_disposition_revision=pending.disposition_revision,
        )
    assert not ledger._path_for_generation(pending.queue_generation).exists()


def test_archive_validates_every_archived_disposition_generation_before_mutating(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _append_decide_and_archive(ledger, subject="record:first")
    _append_decide_and_archive(ledger, subject="record:second")
    current = ledger.snapshot()
    appended = ledger.queue.append(
        kind="correction",
        subject="record:third",
        choice="Archive only across intact acknowledgement history.",
        expected_revision=current.queue_revision,
    )
    pending = ledger.snapshot()
    closed = ledger.decide(
        event_id=appended.events[-1].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=pending.queue_revision,
        expected_disposition_revision=pending.disposition_revision,
    )
    ledger._path_for_generation(0).unlink()

    assert ledger.snapshot() == closed
    with pytest.raises(ControlStorageError, match="disappeared after initialization"):
        ledger.archive_closed(
            expected_queue_revision=closed.queue_revision,
            expected_disposition_revision=closed.disposition_revision,
        )
    assert ledger.queue.snapshot().generation == closed.queue_generation


def test_disposition_parent_swap_never_mutates_a_newer_canonical_control_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    stale = ledger.snapshot()
    control = ledger.vault_root / ".gsv/control"
    stale_copy = ledger.vault_root / ".gsv/control-stale-copy"
    shutil.copytree(control, stale_copy)

    ledger.queue.append(
        kind="correction",
        subject="record:newer",
        choice="Preserve the newer canonical queue.",
        expected_revision=stale.queue_revision,
    )
    newer = ledger.snapshot()
    newer_copy = ledger.vault_root / ".gsv/control-newer-copy"
    control.rename(newer_copy)
    stale_copy.rename(control)

    parked_stale = ledger.vault_root / ".gsv/control-parked-stale"
    actual_cas = PinnedPathRoot.compare_and_swap_regular_file
    swapped = False

    def install_newer_parent_before_first_write(
        store: PinnedPathRoot,
        relative: Path | str,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int,
        mode: int = 0o600,
    ) -> None:
        nonlocal swapped
        if not swapped:
            control.rename(parked_stale)
            newer_copy.rename(control)
            swapped = True
        actual_cas(
            store,
            relative,
            expected=expected,
            replacement=replacement,
            label=label,
            max_bytes=max_bytes,
            mode=mode,
        )

    monkeypatch.setattr(
        PinnedPathRoot,
        "compare_and_swap_regular_file",
        install_newer_parent_before_first_write,
    )

    with pytest.raises(
        (ControlStorageError, DegradedIntegrityError, MutationCommittedError, ValidationError)
    ):
        ledger.decide(
            event_id=stale.pending[0].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=stale.queue_revision,
            expected_disposition_revision=stale.disposition_revision,
        )

    monkeypatch.undo()
    assert swapped is True
    assert OperationLedger(ledger.vault_root).snapshot() == newer
    assert parked_stale.exists()


def test_backup_and_archive_share_one_global_lock_and_never_capture_mixed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    pending = ledger.snapshot()
    closed = ledger.decide(
        event_id=pending.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=pending.queue_revision,
        expected_disposition_revision=pending.disposition_revision,
        result_ref="control:acknowledged/setup-choice",
    )
    backup_inside_global_lock = threading.Event()
    release_backup = threading.Event()
    archive_attempted_global_lock = threading.Event()
    archive_acquired_global_lock = threading.Event()
    backup_results: list[dict[str, Any]] = []
    archive_results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    actual_read = vault_backup_module._read_backup_source
    actual_pinned_file_lock = PinnedPathRoot.exclusive_file_lock

    def blocking_read(path: Path, **kwargs: Any) -> bytes:
        if not backup_inside_global_lock.is_set():
            backup_inside_global_lock.set()
            if not release_backup.wait(timeout=5):
                raise AssertionError("archive serialization proof did not release backup")
        return actual_read(path, **kwargs)

    @contextmanager
    def observed_control_lock(
        store: PinnedPathRoot,
        relative: Path | str,
        *,
        timeout: float = 10.0,
    ) -> Iterator[None]:
        archive_attempted_global_lock.set()
        with actual_pinned_file_lock(store, relative, timeout=timeout):
            archive_acquired_global_lock.set()
            yield

    def create_backup() -> None:
        try:
            backup_results.append(
                Vault(ledger.vault_root).create_backup(tmp_path / "pre-archive.zip")
            )
        except BaseException as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)

    def archive_closed() -> None:
        try:
            archive_results.append(
                ledger.archive_closed(
                    expected_queue_revision=closed.queue_revision,
                    expected_disposition_revision=closed.disposition_revision,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)

    monkeypatch.setattr(vault_backup_module, "_read_backup_source", blocking_read)
    backup_thread = threading.Thread(target=create_backup)
    backup_thread.start()
    assert backup_inside_global_lock.wait(timeout=2)

    monkeypatch.setattr(
        PinnedPathRoot,
        "exclusive_file_lock",
        observed_control_lock,
    )
    archive_thread = threading.Thread(target=archive_closed)
    archive_thread.start()
    assert archive_attempted_global_lock.wait(timeout=2)
    assert not archive_acquired_global_lock.wait(timeout=0.2)

    release_backup.set()
    backup_thread.join(timeout=5)
    archive_thread.join(timeout=5)

    assert not backup_thread.is_alive()
    assert not archive_thread.is_alive()
    assert errors == []
    assert len(backup_results) == 1
    assert len(archive_results) == 1
    assert archive_acquired_global_lock.is_set()
    assert Vault.verify_backup(Path(backup_results[0]["backup"]))["valid"] is True

    restored_root = tmp_path / "restored-pre-archive"
    Vault.restore_backup(Path(backup_results[0]["backup"]), restored_root)
    restored_before_archive = OperationLedger(restored_root).snapshot()
    live_after_archive = OperationLedger(ledger.vault_root).snapshot()

    assert restored_before_archive.queue_generation == 0
    assert restored_before_archive.pending == ()
    assert len(restored_before_archive.decided) == 1
    assert restored_before_archive.archived == ()
    assert live_after_archive.queue_generation == 1
    assert live_after_archive.previous_queue_revision == closed.queue_revision
    assert live_after_archive.decided == ()
    assert live_after_archive.archived[0].decided == closed.decided


@pytest.mark.parametrize(
    "result_ref",
    (
        "not namespaced",
        "provider:quoted instructions from an email",
        "provider:line-one\nline-two",
        "provider:contains?query",
        f"result:{'x' * 513}",
    ),
)
def test_result_reference_is_an_opaque_bounded_identifier(tmp_path: Path, result_ref: str) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()

    with pytest.raises(ValidationError, match="opaque namespaced"):
        ledger.decide(
            event_id=before.pending[0].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=before.queue_revision,
            expected_disposition_revision=before.disposition_revision,
            result_ref=result_ref,
        )

    assert ledger.snapshot() == before


def test_stored_result_reference_uses_the_same_opaque_grammar(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
        result_ref="onboarding:session/verified",
    )
    payload = json.loads(ledger.path.read_text(encoding="utf-8"))
    payload["result_ref"] = "provider response body"
    ledger.path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="opaque namespaced"):
        OperationLedger(ledger.vault_root).snapshot()


def test_disposition_digest_must_match_the_live_control_event(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    decided = ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    stored = ledger.path.read_text(encoding="utf-8")
    ledger.path.write_text(
        stored.replace('"event_digest":"', '"event_digest":"f'), encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="event digest must be a SHA-256 revision"):
        ledger.snapshot()

    payload = stored.replace(decided.decided[0][1].event_digest, "f" * 64)
    ledger.path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValidationError, match="does not match"):
        ledger.snapshot()


def test_stored_disposition_requires_a_canonical_utc_timestamp(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    before = ledger.snapshot()
    ledger.decide(
        event_id=before.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=before.queue_revision,
        expected_disposition_revision=before.disposition_revision,
    )
    payload = json.loads(ledger.path.read_text(encoding="utf-8"))
    payload["acknowledged_at"] = "2026-07-24T12:01:00"
    ledger.path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="timestamp"):
        ledger.snapshot()


def test_disposition_log_rejects_partial_or_symlink_state(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    pending = ledger.snapshot()
    ledger.decide(
        event_id=pending.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=pending.queue_revision,
        expected_disposition_revision=pending.disposition_revision,
    )
    ledger.path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="partial"):
        ledger.snapshot()

    ledger.path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    ledger.path.symlink_to(outside)
    with pytest.raises(ValidationError, match="regular file, not a link"):
        ledger.snapshot()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_parser_rejects_a_stored_disposition_log_beyond_its_count_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    first_revision = _append(ledger)
    ledger.queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Second intent.",
        expected_revision=first_revision,
    )
    first = ledger.snapshot()
    after_first = ledger.decide(
        event_id=first.pending[0].event_id,
        decision="accepted",
        actor_ref="core:doctor",
        reason_code="supported",
        expected_queue_revision=first.queue_revision,
        expected_disposition_revision=first.disposition_revision,
    )
    ledger.decide(
        event_id=after_first.pending[0].event_id,
        decision="rejected",
        actor_ref="core:doctor",
        reason_code="out-of-scope",
        expected_queue_revision=after_first.queue_revision,
        expected_disposition_revision=after_first.disposition_revision,
    )
    monkeypatch.setattr(operations_module, "MAX_DISPOSITIONS", 1)

    with pytest.raises(ValidationError, match="disposition count is invalid"):
        ledger.snapshot()


def test_replacement_between_preflight_and_writer_lock_remains_byte_for_byte_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    _append(ledger)
    observed = ledger.snapshot()
    binding = capture_operation_binding(ledger.vault_root)
    prepared_root = tmp_path / "prepared-replacement"
    prepared = Vault(prepared_root)
    prepared.initialize(name="Prepared replacement")
    prepared_before = _root_file_bytes(prepared_root)
    parked = tmp_path / "parked-original"
    actual_preflight = ledger._preflight_binding
    swapped = False

    def preflight_then_replace(**kwargs: Any) -> None:
        nonlocal swapped
        actual_preflight(**kwargs)
        ledger.vault_root.rename(parked)
        prepared_root.rename(ledger.vault_root)
        swapped = True

    monkeypatch.setattr(ledger, "_preflight_binding", preflight_then_replace)

    with pytest.raises(ControlStorageError, match="vault root changed"):
        ledger.decide(
            event_id=observed.pending[0].event_id,
            decision="accepted",
            actor_ref="core:doctor",
            reason_code="supported",
            expected_queue_revision=observed.queue_revision,
            expected_disposition_revision=observed.disposition_revision,
            expected_vault_id=binding.vault_id,
            expected_root_identity=binding.root_identity,
        )

    assert swapped is True
    assert _root_file_bytes(ledger.vault_root) == prepared_before
