from __future__ import annotations

import os
from pathlib import Path

import pytest

from continuity_kernel import atomic
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    PersistenceError,
)
from continuity_kernel.vault import Vault


def test_failed_atomic_replace_preserves_canonical_file(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="atomic-failure",
        title="Atomic failure",
        outcome="Canonical content survives.",
    )
    path = vault.root / "tasks/atomic-failure.md"
    before = path.read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("continuity_kernel.atomic.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        atomic.atomic_write(path, b"partial replacement")

    assert path.read_bytes() == before
    assert not list(path.parent.glob(".atomic-failure.md.tmp-*"))
    assert vault.get_task(task.identifier).revision == task.revision


def test_doctor_recovers_orphan_from_hard_crash(vault: Vault) -> None:
    task = vault.create_task(
        identifier="crash-recovery",
        title="Crash recovery",
        outcome="Interrupted temp data is ignored.",
    )
    canonical = vault.root / "tasks/crash-recovery.md"
    before = canonical.read_bytes()
    orphan = canonical.parent / ".crash-recovery.md.tmp-injected-crash"
    descriptor = os.open(orphan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"partial bytes from a terminated process")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    unhealthy = Vault(vault.root).doctor()
    repaired = Vault(vault.root).doctor(repair=True)

    assert not unhealthy.healthy
    assert any(issue.code == "orphan-temp" for issue in unhealthy.issues)
    assert repaired.healthy
    assert str(orphan.relative_to(vault.root)) in repaired.repaired
    assert canonical.read_bytes() == before
    assert Vault(vault.root).get_task(task.identifier).revision == task.revision


def test_failed_append_restores_exact_previous_journal_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "events.jsonl"
    before = b'{"existing":true}\n'
    journal.write_bytes(before)
    real_fsync = os.fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected journal fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)

    with pytest.raises(atomic.DurableAppendError) as raised:
        atomic.append_durable(journal, b'{"new":true}\n')

    assert raised.value.outcome is atomic.AppendOutcome.RESTORED
    assert journal.read_bytes() == before


def test_prewrite_append_failure_leaves_exact_journal_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "events.jsonl"
    before = b'{"existing":true}\n'
    journal.write_bytes(before)

    def fail_before_write(descriptor: int, content: bytes) -> None:
        raise OSError("injected prewrite failure")

    monkeypatch.setattr(atomic, "_write_all", fail_before_write)

    with pytest.raises(atomic.DurableAppendError) as raised:
        atomic.append_durable(journal, b'{"new":true}\n')

    assert raised.value.outcome is atomic.AppendOutcome.RESTORED
    assert journal.read_bytes() == before


def test_failed_append_reports_degraded_when_journal_truncate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "events.jsonl"
    journal.write_bytes(b'{"existing":true}\n')

    def fail_fsync(descriptor: int) -> None:
        raise OSError("injected journal fsync failure")

    def fail_truncate(descriptor: int, length: int) -> None:
        raise OSError("injected journal truncate failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    monkeypatch.setattr(os, "ftruncate", fail_truncate)

    with pytest.raises(atomic.DurableAppendError) as raised:
        atomic.append_durable(journal, b'{"new":true}\n')

    assert raised.value.outcome is atomic.AppendOutcome.UNKNOWN


def test_create_rolls_back_when_required_event_append_fails(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = vault.root / "journal/events.jsonl"
    journal_before = journal.read_bytes()

    def fail_append(path: Path, content: bytes) -> None:
        raise atomic.DurableAppendError(
            "injected append failure", outcome=atomic.AppendOutcome.RESTORED
        )

    monkeypatch.setattr("continuity_kernel.vault.append_durable", fail_append)

    with pytest.raises(PersistenceError, match="was not committed"):
        vault.create_task(
            identifier="event-create-failure",
            title="Event create failure",
            outcome="No unaudited task survives.",
        )

    assert not (vault.root / "tasks/event-create-failure.md").exists()
    assert journal.read_bytes() == journal_before

    monkeypatch.undo()
    created = vault.create_task(
        identifier="event-create-failure",
        title="Event create failure",
        outcome="A deliberate retry succeeds after clean rollback.",
    )
    assert created.identifier == "event-create-failure"


def test_update_rolls_back_exact_bytes_when_required_event_append_fails(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="event-update-failure",
        title="Event update failure",
        outcome="The prior task bytes survive.",
    )
    path = vault.root / "tasks/event-update-failure.md"
    journal = vault.root / "journal/events.jsonl"
    canonical_before = path.read_bytes()
    journal_before = journal.read_bytes()

    def fail_append(target: Path, content: bytes) -> None:
        raise atomic.DurableAppendError(
            "injected append failure", outcome=atomic.AppendOutcome.RESTORED
        )

    monkeypatch.setattr("continuity_kernel.vault.append_durable", fail_append)

    with pytest.raises(PersistenceError, match="canonical file was restored"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="This update must roll back.",
        )

    assert path.read_bytes() == canonical_before
    assert journal.read_bytes() == journal_before
    assert vault.get_task(task.identifier).revision == task.revision

    monkeypatch.undo()
    retried = vault.update_task(
        task.identifier,
        expected_revision=task.revision,
        outcome="The retry uses the same restored CAS revision.",
    )
    assert retried.revision != task.revision


def test_document_rolls_back_exact_bytes_when_required_event_append_fails(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = vault.read_document("NOW.md")
    path = vault.root / "NOW.md"
    journal = vault.root / "journal/events.jsonl"
    canonical_before = path.read_bytes()
    journal_before = journal.read_bytes()

    def fail_append(target: Path, content: bytes) -> None:
        raise atomic.DurableAppendError(
            "injected append failure", outcome=atomic.AppendOutcome.RESTORED
        )

    monkeypatch.setattr("continuity_kernel.vault.append_durable", fail_append)

    with pytest.raises(PersistenceError, match="canonical file was restored"):
        vault.write_document(
            "NOW.md",
            "# Now\n\nThis update must roll back.",
            expected_revision=before["revision"],
        )

    assert path.read_bytes() == canonical_before
    assert journal.read_bytes() == journal_before
    assert vault.read_document("NOW.md") == before

    monkeypatch.undo()
    retried = vault.write_document(
        "NOW.md",
        "# Now\n\nThe retry uses the same restored CAS revision.",
        expected_revision=before["revision"],
    )
    assert retried["revision"] != before["revision"]


def test_canonical_rollback_failure_is_explicitly_degraded(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="canonical-rollback-failure",
        title="Canonical rollback failure",
        outcome="A failed rollback is never reported as clean.",
    )
    path = vault.root / "tasks/canonical-rollback-failure.md"
    journal = vault.root / "journal/events.jsonl"
    journal_before = journal.read_bytes()
    real_atomic_write = atomic.atomic_write
    writes = 0

    def fail_second_write(target: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected canonical rollback failure")
        real_atomic_write(target, content, mode=mode)

    def fail_append(target: Path, content: bytes) -> None:
        raise atomic.DurableAppendError(
            "injected append failure", outcome=atomic.AppendOutcome.RESTORED
        )

    monkeypatch.setattr("continuity_kernel.vault.atomic_write", fail_second_write)
    monkeypatch.setattr("continuity_kernel.vault.append_durable", fail_append)

    with pytest.raises(DegradedIntegrityError, match="Run gsv doctor"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="The new bytes remain after rollback fails.",
        )

    assert b"The new bytes remain after rollback fails." in path.read_bytes()
    assert journal.read_bytes() == journal_before


def test_unrestored_journal_failure_retains_canonical_and_reports_degraded(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="journal-rollback-failure",
        title="Journal rollback failure",
        outcome="Unknown journal state keeps canonical state intact.",
    )
    path = vault.root / "tasks/journal-rollback-failure.md"
    journal = vault.root / "journal/events.jsonl"
    journal_before = journal.read_bytes()

    def fail_append(target: Path, content: bytes) -> None:
        raise atomic.DurableAppendError(
            "injected append rollback failure", outcome=atomic.AppendOutcome.UNKNOWN
        )

    monkeypatch.setattr("continuity_kernel.vault.append_durable", fail_append)

    with pytest.raises(DegradedIntegrityError, match="canonical state was retained"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="The canonical update remains because journal state is unknown.",
        )

    assert b"journal state is unknown" in path.read_bytes()
    assert journal.read_bytes() == journal_before


@pytest.mark.parametrize("external_change", ["changed", "disappeared"])
def test_canonical_external_change_during_rollback_is_left_untouched(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, external_change: str
) -> None:
    task = vault.create_task(
        identifier=f"rollback-{external_change}",
        title=f"Rollback {external_change}",
        outcome="Recovery never overwrites an unowned concurrent change.",
    )
    path = vault.root / f"tasks/rollback-{external_change}.md"
    journal = vault.root / "journal/events.jsonl"
    journal_before = journal.read_bytes()
    external_bytes = b"external concurrent bytes\n"

    def change_then_fail(
        operation: str, identifier: str, before_revision: str | None, after_revision: str
    ) -> None:
        if external_change == "changed":
            path.write_bytes(external_bytes)
        else:
            path.unlink()
        raise OSError("injected event failure after external change")

    monkeypatch.setattr(vault, "_event", change_then_fail)

    with pytest.raises(DegradedIntegrityError, match="could not restore"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="This invocation must not overwrite the external change.",
        )

    if external_change == "changed":
        assert path.read_bytes() == external_bytes
    else:
        assert not path.exists()
    assert journal.read_bytes() == journal_before


def test_missing_append_target_is_restored_without_creating_a_file(tmp_path: Path) -> None:
    journal = tmp_path / "missing-events.jsonl"

    with pytest.raises(atomic.DurableAppendError) as raised:
        atomic.append_durable(journal, b'{"new":true}\n')

    assert raised.value.outcome is atomic.AppendOutcome.RESTORED
    assert not journal.exists()


def test_post_fsync_close_failure_reports_committed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "events.jsonl"
    before = b'{"existing":true}\n'
    addition = b'{"new":true}\n'
    journal.write_bytes(before)
    real_close = os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("injected post-fsync close failure")

    monkeypatch.setattr(os, "close", close_then_fail)

    with pytest.raises(atomic.DurableAppendError) as raised:
        atomic.append_durable(journal, addition)

    assert raised.value.outcome is atomic.AppendOutcome.COMMITTED
    assert journal.read_bytes() == before + addition


def test_committed_event_error_keeps_canonical_and_forces_cas_reload(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="committed-event-cleanup-failure",
        title="Committed event cleanup failure",
        outcome="A committed mutation is never described as rolled back.",
    )
    path = vault.root / "tasks/committed-event-cleanup-failure.md"
    journal = vault.root / "journal/events.jsonl"
    journal_before = journal.read_bytes()
    real_append = atomic.append_durable

    def append_then_fail(target: Path, content: bytes) -> None:
        real_append(target, content)
        raise atomic.DurableAppendError(
            "injected post-commit cleanup failure", outcome=atomic.AppendOutcome.COMMITTED
        )

    monkeypatch.setattr("continuity_kernel.vault.append_durable", append_then_fail)

    with pytest.raises(MutationCommittedError, match="were committed"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="The committed bytes remain authoritative.",
        )

    committed = vault.get_task(task.identifier)
    assert b"committed bytes remain authoritative" in path.read_bytes()
    assert len(journal.read_bytes()) > len(journal_before)
    with pytest.raises(ConflictError, match="record changed"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="A blind retry must not overwrite the committed mutation.",
        )
    assert committed.revision != task.revision


def test_post_commit_journal_lock_release_error_keeps_committed_state(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="journal-lock-release-failure",
        title="Journal lock release failure",
        outcome="Post-commit cleanup never triggers canonical rollback.",
    )
    path = vault.root / "tasks/journal-lock-release-failure.md"
    journal = vault.root / "journal/events.jsonl"
    journal_before = journal.read_bytes()
    real_release = atomic._release

    def release_then_fail(handle: object) -> None:
        real_release(handle)  # type: ignore[arg-type]
        if str(getattr(handle, "name", "")).endswith("journal.lock"):
            raise OSError("injected journal lock release failure")

    monkeypatch.setattr(atomic, "_release", release_then_fail)

    with pytest.raises(MutationCommittedError, match="were committed"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="Canonical and journal bytes both commit before lock cleanup fails.",
        )

    committed = vault.get_task(task.identifier)
    assert b"both commit before lock cleanup fails" in path.read_bytes()
    assert journal.read_bytes().startswith(journal_before)
    assert len(journal.read_bytes()) > len(journal_before)
    with pytest.raises(ConflictError):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            outcome="The old revision cannot be retried blindly.",
        )
    assert committed.revision != task.revision
