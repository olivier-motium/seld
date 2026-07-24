from __future__ import annotations

import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import continuity_kernel.atomic as atomic_module
import continuity_kernel.migration as migration_module
from continuity_kernel.atomic import (
    atomic_write as actual_atomic_write,
)
from continuity_kernel.atomic import (
    move_no_replace as actual_move_no_replace,
)
from continuity_kernel.control_queue import EMPTY_REVISION, ControlQueue
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    ValidationError,
)
from continuity_kernel.migration import FOUNDATION_DIRECTORIES, FoundationMigration
from continuity_kernel.vault import Vault


def test_plan_apply_is_idempotent_and_creates_verified_rollback_point(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="0.2 vault")
    migration = FoundationMigration(vault)

    plan = migration.plan()
    receipt = migration.apply()
    repeated = migration.apply()

    assert plan.applicable is True
    assert set(plan.create_directories) == set(FOUNDATION_DIRECTORIES)
    assert repeated == receipt
    assert Vault.verify_backup(vault.root / receipt.backup_path)["valid"] is True
    assert migration.plan().already_applied is True
    assert all((vault.root / path).is_dir() for path in FOUNDATION_DIRECTORIES)


def test_applied_receipt_never_hides_a_missing_owned_directory(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Missing applied directory")
    migration = FoundationMigration(vault)
    migration.apply()
    (vault.root / "onboarding").rmdir()

    plan = migration.plan()

    assert plan.already_applied is False
    assert "receipt:applied-directories-missing" in plan.blockers


def test_portable_backup_reestablishes_host_local_migration_ownership(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Portable migrated vault")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    backup = Path(vault.create_backup(tmp_path / "portable.zip")["backup"])
    restored_root = tmp_path / "restored"

    Vault.restore_backup(backup, restored_root)

    assert migration.receipt_path.is_file()
    assert not (restored_root / migration.receipt_path.relative_to(vault.root)).exists()
    restored = FoundationMigration(Vault(restored_root))
    plan = restored.plan()
    assert plan.blockers == ()
    assert plan.applicable is True
    reapplied = restored.apply()
    assert reapplied.vault_id == applied.vault_id
    assert restored.plan().already_applied is True


def test_visible_receipt_after_ambiguous_publish_preserves_applied_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Ambiguous receipt")
    migration = FoundationMigration(vault)
    failed = False

    def publish_then_fail(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal failed
        actual_atomic_write(path, content, mode=mode)
        if path == migration.receipt_path and not failed:
            failed = True
            raise OSError("injected directory fsync uncertainty")

    monkeypatch.setattr(migration_module, "atomic_write", publish_then_fail)

    with pytest.raises(MutationCommittedError, match="was applied"):
        migration.apply()

    assert all((vault.root / relative).is_dir() for relative in FOUNDATION_DIRECTORIES)
    assert migration.load_receipt().phase == "applied"
    assert migration.plan().already_applied is True


def test_apply_never_returns_success_after_canonical_vault_root_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    parked = tmp_path / "parked-original"
    vault = Vault(root)
    vault.initialize(name="Canonical root swap")
    migration = FoundationMigration(vault)
    swapped = False

    def swap_before_receipt_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal swapped
        if path == migration.receipt_path and not swapped:
            swapped = True
            root.rename(parked)
            shutil.copytree(parked, root)
        actual_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(migration_module, "atomic_write", swap_before_receipt_write)

    with pytest.raises(DegradedIntegrityError, match="canonical vault path or ownership"):
        migration.apply()

    assert swapped is True
    visible = migration.load_receipt()
    assert migration.receipt_path.is_file()
    assert not (parked / migration.receipt_path.relative_to(root)).exists()
    for ownership in visible.directory_ownership:
        original = (parked / ownership.relative_path).stat()
        replacement = (root / ownership.relative_path).stat()
        expected = (ownership.device, ownership.inode)
        assert (original.st_dev, original.st_ino) == expected
        assert (replacement.st_dev, replacement.st_ino) != expected


@pytest.mark.skipif(os.name == "nt", reason="exact root-pinned lock proof is POSIX-only")
def test_apply_never_returns_success_after_vault_swap_during_lock_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    parked = tmp_path / "parked-original"
    vault = Vault(root)
    vault.initialize(name="Lock-exit root swap")
    migration = FoundationMigration(vault)
    actual_validate = atomic_module._validate_named_lock_target
    migration_lock_validations = 0

    def swap_before_final_lock_validation(
        path: Path,
        descriptor: int,
        *,
        parent_descriptor: int,
        parent_snapshot: tuple[int, int],
    ) -> os.stat_result:
        nonlocal migration_lock_validations
        if path.name == "migration.lock":
            migration_lock_validations += 1
            if migration_lock_validations == 4:
                root.rename(parked)
                (root / ".gsv").mkdir(parents=True)
                (parked / ".gsv/locks").rename(root / ".gsv/locks")
        return actual_validate(
            path,
            descriptor,
            parent_descriptor=parent_descriptor,
            parent_snapshot=parent_snapshot,
        )

    monkeypatch.setattr(
        atomic_module,
        "_validate_named_lock_target",
        swap_before_final_lock_validation,
    )

    with pytest.raises(DegradedIntegrityError, match="vault root changed during lock exit"):
        migration.apply()

    assert migration_lock_validations == 4
    assert not migration.receipt_path.exists()
    assert not (root / "onboarding").exists()
    assert (parked / migration.receipt_path.relative_to(root)).is_file()
    assert (parked / "onboarding").is_dir()


def test_idempotent_apply_never_returns_receipt_from_replacement_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    parked = tmp_path / "parked-original"
    vault = Vault(root)
    vault.initialize(name="Idempotent root swap")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    actual_load = migration.load_receipt
    load_calls = 0

    def swap_on_idempotent_load() -> migration_module.MigrationReceipt:
        nonlocal load_calls
        load_calls += 1
        if load_calls == 2:
            root.rename(parked)
            shutil.copytree(parked, root)
        return actual_load()

    monkeypatch.setattr(migration, "load_receipt", swap_on_idempotent_load)

    with pytest.raises(DegradedIntegrityError, match="canonical vault path"):
        migration.apply()

    assert load_calls == 2
    assert (parked / migration.receipt_path.relative_to(root)).is_file()
    assert migration.receipt_path.is_file()
    replacement = FoundationMigration(Vault(root)).load_receipt()
    assert replacement.revision == applied.revision
    assert FoundationMigration(Vault(parked)).load_receipt().revision == applied.revision


@pytest.mark.skipif(os.name == "nt", reason="secure pinned control storage is POSIX-only")
def test_migration_empty_control_directory_supports_first_queue_round_trip(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Migrated control queue")
    FoundationMigration(vault).apply()
    queue = ControlQueue(vault.root)

    assert queue.snapshot().revision == EMPTY_REVISION

    appended = queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="The migrated vault can accept its first Bridge intent.",
        expected_revision=EMPTY_REVISION,
    )

    assert ControlQueue(vault.root).snapshot() == appended


def test_concurrent_apply_serializes_backup_and_receipt_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Concurrent migration")
    barrier = threading.Barrier(2)
    count_lock = threading.Lock()
    backup_calls = 0
    actual_backup = Vault.create_backup

    def counted_backup(self: Vault, destination: Path | None = None) -> dict[str, Any]:
        nonlocal backup_calls
        with count_lock:
            backup_calls += 1
        return actual_backup(self, destination)

    def apply_from_fresh_instance() -> str:
        barrier.wait(timeout=5)
        return FoundationMigration(vault).apply().revision

    monkeypatch.setattr(Vault, "create_backup", counted_backup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = tuple(pool.map(lambda _: apply_from_fresh_instance(), range(2)))

    assert backup_calls == 1
    assert len(set(revisions)) == 1
    assert len(tuple((vault.root / "backups").glob("*.zip"))) == 1


def test_creation_publish_never_adopts_a_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Creation identity swap")
    migration = FoundationMigration(vault)
    target = vault.root / FOUNDATION_DIRECTORIES[0]
    owned_aside = target.with_name(f"{target.name}.owned-aside")
    actual_move = actual_move_no_replace
    replacement_identity: tuple[int, int] | None = None
    owned_identity: tuple[int, int] | None = None

    def publish_then_swap(source: Path, destination: Path) -> None:
        nonlocal owned_identity, replacement_identity
        actual_move(source, destination)
        if destination == target and replacement_identity is None:
            owned_identity = (destination.stat().st_dev, destination.stat().st_ino)
            destination.rename(owned_aside)
            destination.mkdir()
            (destination / "foreign.txt").write_text("keep", encoding="utf-8")
            replacement_identity = (destination.stat().st_dev, destination.stat().st_ino)

    monkeypatch.setattr(migration_module, "_move_no_replace", publish_then_swap)

    with pytest.raises(ConflictError, match="changed identity during creation"):
        migration.apply()

    assert owned_identity is not None
    assert replacement_identity is not None
    assert (owned_aside.stat().st_dev, owned_aside.stat().st_ino) == owned_identity
    assert (target.stat().st_dev, target.stat().st_ino) == replacement_identity
    assert (target / "foreign.txt").read_text(encoding="utf-8") == "keep"
    assert not migration.receipt_path.exists()


def test_creation_no_replace_conflict_preserves_foreign_target_and_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Creation target conflict")
    migration = FoundationMigration(vault)
    target = vault.root / FOUNDATION_DIRECTORIES[0]
    actual_move = actual_move_no_replace
    raced = False

    def occupy_then_move(source: Path, destination: Path) -> None:
        nonlocal raced
        if destination == target and not raced:
            raced = True
            destination.mkdir()
            (destination / "foreign.txt").write_text("keep", encoding="utf-8")
        actual_move(source, destination)

    monkeypatch.setattr(migration_module, "_move_no_replace", occupy_then_move)

    with pytest.raises(ConflictError, match="appeared during creation"):
        migration.apply()

    stages = tuple(target.parent.glob(f".{target.name}.gsv-create-*.stage"))
    assert (target / "foreign.txt").read_text(encoding="utf-8") == "keep"
    assert len(stages) == 1
    assert stages[0].is_dir()
    assert not tuple(stages[0].iterdir())
    assert not migration.receipt_path.exists()


def test_creation_publish_error_after_visible_move_fails_closed_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Creation unknown outcome")
    migration = FoundationMigration(vault)
    target = vault.root / FOUNDATION_DIRECTORIES[0]
    actual_move = actual_move_no_replace
    failed = False

    def publish_then_fail(source: Path, destination: Path) -> None:
        nonlocal failed
        actual_move(source, destination)
        if destination == target and not failed:
            failed = True
            raise OSError("injected post-publication failure")

    monkeypatch.setattr(migration_module, "_move_no_replace", publish_then_fail)

    with pytest.raises(DegradedIntegrityError, match="unknown outcome"):
        migration.apply()

    assert failed is True
    assert target.is_dir()
    assert not tuple(target.iterdir())
    assert not tuple(target.parent.glob(f".{target.name}.gsv-create-*.stage"))
    assert not migration.receipt_path.exists()


def test_pre_receipt_identity_check_preserves_late_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pre-receipt identity swap")
    migration = FoundationMigration(vault)
    target = vault.root / FOUNDATION_DIRECTORIES[0]
    owned_aside = target.with_name(f"{target.name}.owned-aside")
    actual_verify = migration_module._verify_owned_directory_for_receipt
    replacement_identity: tuple[int, int] | None = None

    def swap_then_verify(
        root: Path,
        ownership: migration_module.DirectoryOwnership,
    ) -> None:
        nonlocal replacement_identity
        if ownership.relative_path == FOUNDATION_DIRECTORIES[0] and replacement_identity is None:
            target.rename(owned_aside)
            target.mkdir()
            (target / "foreign.txt").write_text("keep", encoding="utf-8")
            replacement_identity = (target.stat().st_dev, target.stat().st_ino)
        actual_verify(root, ownership)

    monkeypatch.setattr(
        migration_module,
        "_verify_owned_directory_for_receipt",
        swap_then_verify,
    )

    with pytest.raises(ConflictError, match="changed identity before its ownership receipt"):
        migration.apply()

    assert replacement_identity is not None
    assert owned_aside.is_dir()
    assert (target.stat().st_dev, target.stat().st_ino) == replacement_identity
    assert (target / "foreign.txt").read_text(encoding="utf-8") == "keep"
    assert not migration.receipt_path.exists()


def test_pre_receipt_check_preserves_data_arriving_in_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pre-receipt data race")
    migration = FoundationMigration(vault)
    target = vault.root / FOUNDATION_DIRECTORIES[0]
    actual_verify = migration_module._verify_owned_directory_for_receipt
    injected = False

    def write_then_verify(
        root: Path,
        ownership: migration_module.DirectoryOwnership,
    ) -> None:
        nonlocal injected
        if ownership.relative_path == FOUNDATION_DIRECTORIES[0] and not injected:
            injected = True
            (target / "arrived.txt").write_text("keep", encoding="utf-8")
        actual_verify(root, ownership)

    monkeypatch.setattr(
        migration_module,
        "_verify_owned_directory_for_receipt",
        write_then_verify,
    )

    with pytest.raises(ConflictError, match="received data before its ownership receipt"):
        migration.apply()

    assert injected is True
    assert (target / "arrived.txt").read_text(encoding="utf-8") == "keep"
    assert not migration.receipt_path.exists()


def test_rollback_removes_only_migration_created_empty_directories(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Rollback")
    migration = FoundationMigration(vault)
    receipt = migration.apply()

    result = migration.rollback(expected_revision=receipt.revision)

    assert result["rolled_back"] is True
    assert set(result["removed_directories"]) == set(FOUNDATION_DIRECTORIES)
    terminal = migration.load_receipt()
    assert terminal.phase == "rolled_back"
    assert terminal.remaining_directories == ()
    assert set(terminal.removed_directories) == set(FOUNDATION_DIRECTORIES)
    assert Vault.verify_backup(vault.root / receipt.backup_path)["valid"] is True
    for ownership in receipt.directory_ownership:
        public_path = vault.root / ownership.relative_path
        quarantine, marker = migration_module._removal_paths(
            public_path,
            ownership.relative_path,
            ownership,
        )
        assert not public_path.exists()
        assert quarantine.is_dir()
        assert (quarantine.stat().st_dev, quarantine.stat().st_ino) == (
            ownership.device,
            ownership.inode,
        )
        assert quarantine.name.startswith(f".{public_path.name}.gsv-remove-")
        assert quarantine.name.endswith(".quarantine")
        assert marker.name.endswith(".marker")
        assert marker.is_file()


def test_rollback_refuses_a_receipt_after_logical_vault_identity_replacement(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Logical replacement")
    migration = FoundationMigration(vault)
    receipt = migration.apply()
    manifest_path = vault.root / ".gsv/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["vault_id"] = "11111111-1111-4111-8111-111111111111"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConflictError, match="different logical vault"):
        migration.rollback(expected_revision=receipt.revision)

    assert migration.receipt_path.is_file()
    assert all((vault.root / relative).is_dir() for relative in FOUNDATION_DIRECTORIES)


def test_terminal_rollback_is_idempotent_and_can_be_reapplied(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Resume")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    migration.rollback(expected_revision=applied.revision)
    terminal = migration.load_receipt()

    repeated = migration.rollback(expected_revision=terminal.revision)
    reapplied = migration.apply()

    assert repeated["already_rolled_back"] is True
    assert repeated["rolled_back"] is True
    assert reapplied.phase == "applied"
    assert reapplied.revision != terminal.revision
    assert migration.plan().already_applied is True


def test_rollback_blocks_after_portable_onboarding_data_exists(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Keep data")
    migration = FoundationMigration(vault)
    receipt = migration.apply()
    (vault.root / "onboarding/session.md").write_text("keep", encoding="utf-8")

    with pytest.raises(ConflictError, match="contains user or runtime data"):
        migration.rollback(expected_revision=receipt.revision)

    assert migration.receipt_path.exists()
    assert (vault.root / "onboarding/session.md").read_text(encoding="utf-8") == "keep"


def test_rollback_failure_keeps_durable_progress_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Interrupted")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    actual_publish = migration_module._publish_removal_marker
    calls = 0

    def fail_second_marker(
        marker: Path,
        relative: str,
        ownership: migration_module.DirectoryOwnership,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected marker failure")
        actual_publish(marker, relative, ownership)

    monkeypatch.setattr(migration_module, "_publish_removal_marker", fail_second_marker)

    with pytest.raises(OSError, match="marker failure"):
        migration.rollback(expected_revision=applied.revision)

    progress = migration.load_receipt()
    assert progress.phase == "removing"
    assert len(progress.removed_directories) == 1
    assert len(progress.remaining_directories) == len(FOUNDATION_DIRECTORIES) - 1
    assert migration.receipt_path.exists()

    monkeypatch.setattr(migration_module, "_publish_removal_marker", actual_publish)
    resumed = migration.rollback(expected_revision=progress.revision)

    assert resumed["rolled_back"] is True
    assert set(resumed["removed_directories"]) == set(FOUNDATION_DIRECTORIES)
    assert migration.load_receipt().phase == "rolled_back"


def test_progress_write_failure_after_removal_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Progress failure")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    failed = False

    def fail_first_removed_progress(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal failed
        payload = json.loads(content)
        if path == migration.receipt_path and payload["removed_directories"] and not failed:
            failed = True
            raise OSError("injected receipt write failure")
        actual_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(migration_module, "atomic_write", fail_first_removed_progress)

    with pytest.raises(OSError, match="receipt write failure"):
        migration.rollback(expected_revision=applied.revision)

    progress = migration.load_receipt()
    assert progress.phase == "removing"
    assert progress.removed_directories == ()
    assert not (vault.root / progress.remaining_directories[0]).exists()

    monkeypatch.setattr(migration_module, "atomic_write", actual_atomic_write)
    resumed = migration.rollback(expected_revision=progress.revision)

    assert resumed["rolled_back"] is True
    assert migration.load_receipt().phase == "rolled_back"


def test_restart_resumes_after_owned_directory_was_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Quarantine restart")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    actual_publish = migration_module._publish_removal_marker
    interrupted = False

    def interrupt_before_marker(
        marker: Path,
        relative: str,
        ownership: migration_module.DirectoryOwnership,
    ) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("injected process interruption")
        actual_publish(marker, relative, ownership)

    monkeypatch.setattr(migration_module, "_publish_removal_marker", interrupt_before_marker)

    with pytest.raises(OSError, match="process interruption"):
        migration.rollback(expected_revision=applied.revision)

    progress = migration.load_receipt()
    relative = progress.remaining_directories[0]
    ownership = next(
        item for item in progress.directory_ownership if item.relative_path == relative
    )
    quarantine, _marker = migration_module._removal_paths(
        vault.root / relative,
        relative,
        ownership,
    )
    assert not (vault.root / relative).exists()
    assert quarantine.is_dir()

    monkeypatch.setattr(migration_module, "_publish_removal_marker", actual_publish)
    resumed = FoundationMigration(vault).rollback(expected_revision=progress.revision)

    assert resumed["rolled_back"] is True
    assert FoundationMigration(vault).load_receipt().phase == "rolled_back"


def test_partial_staged_marker_write_never_poisons_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Partial marker restart")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    actual_write = os.write
    interrupted = False

    def write_marker_prefix_then_fail(descriptor: int, content: bytes) -> int:
        nonlocal interrupted
        if content.startswith(b'{"device"') and not interrupted:
            interrupted = True
            actual_write(descriptor, content[:1])
            raise OSError("injected short marker write")
        return actual_write(descriptor, content)

    monkeypatch.setattr(os, "write", write_marker_prefix_then_fail)

    with pytest.raises(OSError, match="short marker write"):
        migration.rollback(expected_revision=applied.revision)

    progress = migration.load_receipt()
    relative = progress.remaining_directories[0]
    ownership = next(
        item for item in progress.directory_ownership if item.relative_path == relative
    )
    quarantine, marker = migration_module._removal_paths(
        vault.root / relative,
        relative,
        ownership,
    )
    stages = tuple(marker.parent.glob(f".{marker.name}.gsv-stage-*"))
    assert interrupted is True
    assert quarantine.is_dir()
    assert not marker.exists()
    assert len(stages) == 1
    assert stages[0].read_bytes() == b"{"

    monkeypatch.setattr(os, "write", actual_write)
    resumed = FoundationMigration(vault).rollback(expected_revision=progress.revision)

    assert resumed["rolled_back"] is True
    assert migration_module._validate_removal_marker(marker, relative, ownership) is True
    assert FoundationMigration(vault).load_receipt().phase == "rolled_back"


def test_foreign_removal_marker_blocks_without_mutating_receipt_or_directories(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Foreign marker")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    relative = applied.remaining_directories[0]
    ownership = next(item for item in applied.directory_ownership if item.relative_path == relative)
    _quarantine, marker = migration_module._removal_paths(
        vault.root / relative,
        relative,
        ownership,
    )
    marker.write_bytes(b"foreign marker bytes\n")

    with pytest.raises(DegradedIntegrityError, match="marker changed"):
        migration.rollback(expected_revision=applied.revision)

    assert marker.read_bytes() == b"foreign marker bytes\n"
    assert migration.load_receipt() == applied
    assert all((vault.root / item).is_dir() for item in FOUNDATION_DIRECTORIES)


def test_identity_swap_during_rollback_never_deletes_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Identity swap")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    first_relative = next(reversed(applied.created_directories))
    target = vault.root / first_relative
    owned_identity = (target.stat().st_dev, target.stat().st_ino)
    owned_aside = target.with_name(f"{target.name}.owned-aside")
    actual_move = actual_move_no_replace
    replacement_identity: tuple[int, int] | None = None
    raced = False

    def swap_then_move(source: Path, destination: Path) -> None:
        nonlocal raced, replacement_identity
        if source == target and not raced:
            raced = True
            source.rename(owned_aside)
            source.mkdir()
            replacement_identity = (source.stat().st_dev, source.stat().st_ino)
        actual_move(source, destination)

    monkeypatch.setattr(migration_module, "_move_no_replace", swap_then_move)

    with pytest.raises(ConflictError, match="changed identity during removal"):
        migration.rollback(expected_revision=applied.revision)

    assert replacement_identity is not None
    assert (target.stat().st_dev, target.stat().st_ino) == replacement_identity
    assert (owned_aside.stat().st_dev, owned_aside.stat().st_ino) == owned_identity
    progress = migration.load_receipt()
    assert progress.phase == "removing"
    assert first_relative in progress.remaining_directories


def test_apply_failure_cleanup_never_deletes_identity_swap_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Apply cleanup identity swap")
    migration = FoundationMigration(vault)
    target = vault.root / FOUNDATION_DIRECTORIES[-1]
    owned_aside = target.with_name(f"{target.name}.owned-aside")
    actual_move = actual_move_no_replace
    raced = False
    replacement_identity: tuple[int, int] | None = None

    def fail_receipt_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        if path == migration.receipt_path:
            raise OSError("injected receipt failure")
        actual_atomic_write(path, content, mode=mode)

    def swap_then_move(source: Path, destination: Path) -> None:
        nonlocal raced, replacement_identity
        if source == target and not raced:
            raced = True
            source.rename(owned_aside)
            source.mkdir()
            replacement_identity = (source.stat().st_dev, source.stat().st_ino)
        actual_move(source, destination)

    monkeypatch.setattr(migration_module, "atomic_write", fail_receipt_write)
    monkeypatch.setattr(migration_module, "_move_no_replace", swap_then_move)

    with pytest.raises(OSError, match="receipt failure"):
        migration.apply()

    assert replacement_identity is not None
    assert (target.stat().st_dev, target.stat().st_ino) == replacement_identity
    assert owned_aside.is_dir()
    assert not migration.receipt_path.exists()


def test_race_after_preflight_validation_preserves_receipt_and_foreign_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Race")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    actual_publish = migration_module._publish_removal_marker
    raced = False

    def race_first_marker(
        marker: Path,
        relative: str,
        ownership: migration_module.DirectoryOwnership,
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            quarantine = marker.with_suffix(".quarantine")
            (quarantine / "arrived-after-validation.txt").write_text("keep", encoding="utf-8")
        actual_publish(marker, relative, ownership)

    monkeypatch.setattr(migration_module, "_publish_removal_marker", race_first_marker)

    with pytest.raises(ConflictError, match="contains user or runtime data"):
        migration.rollback(expected_revision=applied.revision)

    progress = migration.load_receipt()
    raced_path = vault.root / progress.remaining_directories[0] / "arrived-after-validation.txt"
    assert progress.phase == "removing"
    assert raced_path.read_text(encoding="utf-8") == "keep"
    assert migration.receipt_path.exists()


def test_terminal_removal_never_uses_pathname_rmdir_for_owned_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Terminal identity swap")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    actual_rmdir = Path.rmdir
    terminal_rmdir_calls = 0

    def swap_quarantine_then_remove(path: Path) -> None:
        nonlocal terminal_rmdir_calls
        if path.name.endswith(".quarantine"):
            terminal_rmdir_calls += 1
            owned_aside = path.with_suffix(".owned-aside")
            path.rename(owned_aside)
            path.mkdir()
        actual_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", swap_quarantine_then_remove)

    result = migration.rollback(expected_revision=applied.revision)

    assert result["rolled_back"] is True
    assert terminal_rmdir_calls == 0
    for ownership in applied.directory_ownership:
        public_path = vault.root / ownership.relative_path
        quarantine, marker = migration_module._removal_paths(
            public_path,
            ownership.relative_path,
            ownership,
        )
        assert not public_path.exists()
        assert (quarantine.stat().st_dev, quarantine.stat().st_ino) == (
            ownership.device,
            ownership.inode,
        )
        assert marker.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_target_type_swap_after_preflight_is_rejected_without_losing_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Type race")
    migration = FoundationMigration(vault)
    applied = migration.apply()
    actual_validate = migration_module._validate_rollback_targets
    outside = tmp_path / "outside"
    outside.mkdir()

    def validate_then_swap(
        root: Path,
        receipt_path: Path,
        receipt: migration_module.MigrationReceipt,
    ) -> None:
        actual_validate(root, receipt_path, receipt)
        first = root / next(reversed(receipt.created_directories))
        first.rmdir()
        first.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(migration_module, "_validate_rollback_targets", validate_then_swap)

    with pytest.raises(ValidationError, match="changed type"):
        migration.rollback(expected_revision=applied.revision)

    progress = migration.load_receipt()
    assert progress.phase == "removing"
    assert migration.receipt_path.exists()
    assert (vault.root / progress.remaining_directories[0]).is_symlink()


def test_stale_receipt_revision_cannot_rollback(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="CAS")
    migration = FoundationMigration(vault)
    receipt = migration.apply()

    with pytest.raises(ConflictError, match="changed"):
        migration.rollback(expected_revision="0" * 64)

    assert migration.load_receipt() == receipt


def test_symlink_or_file_at_migration_target_blocks_without_mutation(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Blocked")
    (vault.root / "onboarding").write_text("not a directory", encoding="utf-8")
    migration = FoundationMigration(vault)

    plan = migration.plan()

    assert plan.applicable is False
    assert "onboarding:not-real-directory" in plan.blockers
    with pytest.raises(ValidationError, match="blocked"):
        migration.apply()
    assert not migration.receipt_path.exists()


def test_receipt_rejects_extensions_and_preserves_no_private_payload(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Receipt")
    migration = FoundationMigration(vault)
    migration.apply()
    payload = json.loads(migration.receipt_path.read_text(encoding="utf-8"))
    payload["provider_body"] = "do not retain"
    migration.receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="unsupported shape"):
        migration.load_receipt()


def test_receipt_rejects_a_non_uuid_vault_identity(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Receipt vault identity")
    migration = FoundationMigration(vault)
    migration.apply()
    payload = json.loads(migration.receipt_path.read_text(encoding="utf-8"))
    payload["vault_id"] = "not-a-vault-id"
    migration.receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="vault ID"):
        migration.load_receipt()


def test_receipt_rejects_noncanonical_applied_timestamp(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Receipt timestamp")
    migration = FoundationMigration(vault)
    migration.apply()
    payload = json.loads(migration.receipt_path.read_text(encoding="utf-8"))
    payload["applied_at"] = "sometime after lunch"
    migration.receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="timestamp"):
        migration.load_receipt()
