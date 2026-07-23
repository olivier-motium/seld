from __future__ import annotations

import errno
import json
import os
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import vault as vault_module
from continuity_kernel.atomic import durable_publish_new as actual_durable_publish_new
from continuity_kernel.atomic import durable_replace as actual_durable_replace
from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    NotFoundError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.vault import BACKUP_MANIFEST, Vault


def _rewrite_backup(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
    *,
    update_hashes: bool,
    vault_id: str | None = None,
) -> None:
    with zipfile.ZipFile(source, "r") as archive:
        contents = {item.filename: archive.read(item.filename) for item in archive.infolist()}
    contents.update(replacements)
    manifest = json.loads(contents[BACKUP_MANIFEST])
    if update_hashes:
        for name, content in replacements.items():
            if name != BACKUP_MANIFEST:
                manifest["files"][name] = sha256_bytes(content)
    if vault_id is not None:
        manifest["vault_id"] = vault_id
    contents[BACKUP_MANIFEST] = json.dumps(manifest).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


def _write_crafted_backup(
    destination: Path,
    entries: list[tuple[zipfile.ZipInfo | str, bytes]],
) -> None:
    files = {
        item.filename if isinstance(item, zipfile.ZipInfo) else item: sha256_bytes(content)
        for item, content in entries
        if (item.filename if isinstance(item, zipfile.ZipInfo) else item) != BACKUP_MANIFEST
        and not (item.filename if isinstance(item, zipfile.ZipInfo) else item).endswith("/")
    }
    manifest = {"format_version": 1, "vault_id": "crafted", "files": files}
    with zipfile.ZipFile(destination, "w") as archive:
        for item, content in entries:
            archive.writestr(item, content)
        archive.writestr(BACKUP_MANIFEST, json.dumps(manifest))


def test_backup_verify_restore_and_logical_equivalence(vault: Vault, tmp_path: Path) -> None:
    vault.create_task(
        identifier="backup-proof",
        title="Backup proof",
        outcome="Restore all authoritative bytes.",
        status="ready",
        next_actor="agent",
        next_action="Restore.",
    )
    backup = vault.create_backup(tmp_path / "portable.zip")
    target = tmp_path / "restored"
    restored = Vault.restore_backup(Path(backup["backup"]), target)

    assert backup["verified"] is True
    assert Vault.verify_backup(Path(backup["backup"]))["valid"] is True
    assert restored["digest"] == vault.logical_digest()
    assert Vault(target).doctor().healthy


def test_tampered_backup_cannot_be_restored(vault: Vault, tmp_path: Path) -> None:
    vault.create_task(
        identifier="tamper-proof",
        title="Tamper proof",
        outcome="Detect changed bytes.",
    )
    original = Path(vault.create_backup(tmp_path / "original.zip")["backup"])
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(original, "r") as source, zipfile.ZipFile(tampered, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "tasks/tamper-proof.md":
                content += b"tampered"
            target.writestr(item, content)

    assert Vault.verify_backup(tampered)["valid"] is False
    with pytest.raises(ValidationError, match="hashes"):
        Vault.restore_backup(tampered, tmp_path / "must-not-exist")


def test_self_consistent_invalid_backup_never_publishes_and_can_retry(
    vault: Vault, tmp_path: Path
) -> None:
    vault.create_task(
        identifier="stage-proof",
        title="Stage proof",
        outcome="Validate before publication.",
    )
    original = Path(vault.create_backup(tmp_path / "original.zip")["backup"])
    invalid = tmp_path / "self-consistent-invalid.zip"
    _rewrite_backup(
        original,
        invalid,
        {"tasks/stage-proof.md": b"not a valid task\n"},
        update_hashes=True,
    )
    assert Vault.verify_backup(invalid)["valid"] is True
    target = tmp_path / "restored"

    with pytest.raises(ValidationError, match="before publication"):
        Vault.restore_backup(invalid, target)

    assert not target.exists()
    assert list(tmp_path.glob(".restored.tmp-restore-*")) == []

    restored = Vault.restore_backup(original, target)
    assert restored["digest"] == vault.logical_digest()
    assert Vault(target).doctor().healthy


def test_backup_manifest_identity_must_match_staged_vault(vault: Vault, tmp_path: Path) -> None:
    original = Path(vault.create_backup(tmp_path / "original.zip")["backup"])
    mismatched = tmp_path / "mismatched-id.zip"
    _rewrite_backup(
        original,
        mismatched,
        {},
        update_hashes=True,
        vault_id="different-vault-id",
    )
    target = tmp_path / "restored"

    with pytest.raises(ValidationError, match="identity"):
        Vault.restore_backup(mismatched, target)

    assert not target.exists()
    assert list(tmp_path.glob(".restored.tmp-restore-*")) == []


def test_path_traversal_archive_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "traversal.zip"
    manifest = {
        "format_version": 1,
        "vault_id": "synthetic",
        "files": {"../escape": "irrelevant"},
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape", b"escape")
        archive.writestr(BACKUP_MANIFEST, json.dumps(manifest))

    with pytest.raises(ValidationError, match="unsafe backup entry"):
        Vault.verify_backup(archive_path)


def test_restore_refuses_nonempty_target(vault: Vault, tmp_path: Path) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "user-file.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(ConflictError, match="not empty"):
        Vault.restore_backup(backup, target)

    assert (target / "user-file.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.skipif(os.name == "nt", reason="symlink setup needs elevated privileges")
def test_restore_rejects_symlink_target_without_touching_destination(
    vault: Vault, tmp_path: Path
) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_bytes(b"keep\n")
    target = tmp_path / "restored"
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError, match="symbolic link"):
        Vault.restore_backup(backup, target)

    assert target.is_symlink()
    assert marker.read_bytes() == b"keep\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_restore_rejects_special_target_without_blocking(vault: Vault, tmp_path: Path) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored.fifo"
    os.mkfifo(target)

    with pytest.raises(ConflictError, match="absent or empty directory"):
        Vault.restore_backup(backup, target)

    assert target.exists()


@pytest.mark.skipif(os.name == "nt", reason="atomic open-path replacement proof is POSIX-only")
def test_restore_uses_same_open_archive_after_source_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_vault = Vault(tmp_path / "original-vault")
    original_vault.initialize(name="Original")
    original_vault.create_task(
        identifier="original-task",
        title="Original task",
        outcome="Restore these exact bytes.",
    )
    replacement_vault = Vault(tmp_path / "replacement-vault")
    replacement_vault.initialize(name="Replacement")
    replacement_vault.create_task(
        identifier="replacement-task",
        title="Replacement task",
        outcome="Must not cross the open archive boundary.",
    )
    source = Path(original_vault.create_backup(tmp_path / "source.zip")["backup"])
    replacement = Path(replacement_vault.create_backup(tmp_path / "replacement.zip")["backup"])
    metadata = vault_module._backup_metadata
    swapped = False

    def metadata_and_replace(
        archive: zipfile.ZipFile,
    ) -> tuple[tuple[zipfile.ZipInfo, ...], dict[str, Any]]:
        nonlocal swapped
        infos, manifest = metadata(archive)
        if not swapped:
            replacement.replace(source)
            swapped = True
        return infos, manifest

    monkeypatch.setattr(vault_module, "_backup_metadata", metadata_and_replace)
    target = tmp_path / "restored"
    restored = Vault.restore_backup(source, target)

    assert restored["vault_id"] == original_vault.identity()["vault_id"]
    assert Vault(target).get_task("original-task").title == "Original task"
    with pytest.raises(NotFoundError):
        Vault(target).get_task("replacement-task")


@pytest.mark.skipif(os.name == "nt", reason="same-inode mutation proof is POSIX-only")
def test_restore_rejects_same_inode_mutation_between_metadata_and_entry_read(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault.create_task(
        identifier="mutation-proof",
        title="Mutation proof",
        outcome="Never verify different bytes than are extracted.",
    )
    source = Path(vault.create_backup(tmp_path / "source.zip")["backup"])
    replacement_vault = Vault(tmp_path / "replacement-vault")
    replacement_vault.initialize(name="Replacement")
    replacement = Path(replacement_vault.create_backup(tmp_path / "replacement.zip")["backup"])
    replacement_bytes = replacement.read_bytes()
    metadata = vault_module._backup_metadata
    mutated = False

    def metadata_and_mutate(
        archive: zipfile.ZipFile,
    ) -> tuple[tuple[zipfile.ZipInfo, ...], dict[str, Any]]:
        nonlocal mutated
        infos, manifest = metadata(archive)
        if not mutated:
            with source.open("r+b") as handle:
                handle.seek(0)
                handle.write(replacement_bytes)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            mutated = True
        return infos, manifest

    monkeypatch.setattr(vault_module, "_backup_metadata", metadata_and_mutate)
    target = tmp_path / "restored"

    with pytest.raises(ValidationError, match="invalid backup"):
        Vault.restore_backup(source, target)

    assert not target.exists()
    assert list(tmp_path.glob(".restored.tmp-restore-*")) == []


def test_failed_publication_restores_preexisting_empty_target(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored"
    target.mkdir()
    calls = 0

    def fail_stage_publish(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publication failure")
        actual_durable_replace(source, destination)

    monkeypatch.setattr(vault_module, "durable_replace", fail_stage_publish)
    with pytest.raises(PersistenceError, match="was not published"):
        Vault.restore_backup(backup, target)

    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert list(tmp_path.glob(".restored.tmp-restore-*")) == []


def test_concurrent_content_in_renamed_empty_target_is_restored_and_aborts(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored"
    target.mkdir()
    moved = False

    def add_content_after_move(source: Path, destination: Path) -> None:
        nonlocal moved
        actual_durable_replace(source, destination)
        if not moved and source == target:
            (destination / "concurrent.txt").write_bytes(b"preserve\n")
            moved = True

    monkeypatch.setattr(vault_module, "durable_replace", add_content_after_move)

    with pytest.raises(PersistenceError, match="restore was not published") as failure:
        Vault.restore_backup(backup, target)

    assert isinstance(failure.value.__cause__, ConflictError)
    assert (target / "concurrent.txt").read_bytes() == b"preserve\n"
    assert list(tmp_path.glob(".restored.tmp-restore-*")) == []


def test_prior_empty_cleanup_failure_reports_published_restore(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored"
    target.mkdir()

    def fail_cleanup(path: Path) -> str:
        return f"injected cleanup failure at {path}"

    monkeypatch.setattr(vault_module, "_remove_prior_empty", fail_cleanup)
    restored = Vault.restore_backup(backup, target)

    assert Vault(target).doctor().healthy is False
    assert restored["digest"] == vault.logical_digest()
    assert restored["cleanup_warning"].startswith("injected cleanup failure")
    preserved = Path(restored["preserved_prior_target"])
    assert preserved.is_dir()
    assert list(preserved.iterdir()) == []


def test_backup_rejects_duplicate_casefolded_names(tmp_path: Path) -> None:
    archive_path = tmp_path / "duplicates.zip"
    manifest = {"format_version": 1, "vault_id": "synthetic", "files": {}}
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("MIND.md", b"first")
        archive.writestr("mind.md", b"second")
        archive.writestr(BACKUP_MANIFEST, json.dumps(manifest))

    with pytest.raises(ValidationError, match="duplicate backup entry"):
        Vault.verify_backup(archive_path)


def test_backup_rejects_unknown_manifest_version(tmp_path: Path) -> None:
    archive_path = tmp_path / "future.zip"
    manifest = {"format_version": 999, "vault_id": "synthetic", "files": {}}
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(BACKUP_MANIFEST, json.dumps(manifest))

    with pytest.raises(ValidationError, match="manifest version"):
        Vault.verify_backup(archive_path)


def test_backup_rejects_oversized_decompressed_entry(tmp_path: Path) -> None:
    archive_path = tmp_path / "oversized.zip"
    manifest = {"format_version": 1, "vault_id": "synthetic", "files": {}}
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("oversized.bin", b"x" * (17 * 1024 * 1024))
        archive.writestr(BACKUP_MANIFEST, json.dumps(manifest))

    with pytest.raises(ValidationError, match="size bound"):
        Vault.verify_backup(archive_path)


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink setup needs elevated privileges")
def test_backup_rejects_symlink_instead_of_silently_omitting_it(
    vault: Vault, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be followed or silently omitted", encoding="utf-8")
    (vault.root / "linked.txt").symlink_to(outside)

    with pytest.raises(ValidationError, match="refuses symbolic link"):
        vault.create_backup(tmp_path / "backup.zip")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_backup_rejects_fifo_instead_of_silently_omitting_it(vault: Vault, tmp_path: Path) -> None:
    os.mkfifo(vault.root / "unrepresented.fifo")

    with pytest.raises(ValidationError, match="unsupported file type"):
        vault.create_backup(tmp_path / "backup.zip")


def test_backup_propagates_directory_traversal_failure(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = vault.root / "unreadable"
    unreadable.mkdir()
    (unreadable / "must-not-be-omitted.txt").write_text("authoritative", encoding="utf-8")
    original_scandir = os.scandir

    def fail_unreadable(path: str | os.PathLike[str]) -> Any:
        if Path(path) == unreadable:
            raise PermissionError(errno.EACCES, "injected unreadable subtree")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_unreadable)
    destination = tmp_path / "backup.zip"

    with pytest.raises(ValidationError, match="could not read vault backup directory"):
        vault.create_backup(destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".gsv-backup.tmp-*.zip"))


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600])
def test_backup_rejects_nonregular_unix_archive_entries(tmp_path: Path, mode: int) -> None:
    archive_path = tmp_path / "typed.zip"
    entry = zipfile.ZipInfo("payload.txt")
    entry.create_system = 3
    entry.external_attr = mode << 16
    _write_crafted_backup(archive_path, [(entry, b"manifest-correct bytes")])

    with pytest.raises(ValidationError, match="unsupported backup entry type"):
        Vault.verify_backup(archive_path)


@pytest.mark.parametrize(
    "name",
    [
        "C:/escape.txt",
        "payload.txt:alternate-stream",
        "CON",
        "aux.txt",
        "trailing-dot.",
        "trailing-space ",
    ],
)
def test_backup_rejects_nonportable_windows_aliases(tmp_path: Path, name: str) -> None:
    archive_path = tmp_path / "nonportable.zip"
    _write_crafted_backup(archive_path, [(name, b"must never be extracted")])

    with pytest.raises(ValidationError, match="non-portable backup entry"):
        Vault.restore_backup(archive_path, tmp_path / "restored")

    assert not (tmp_path / "restored").exists()
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("code_point", range(1, 32), ids=lambda value: f"U+{value:04X}")
def test_backup_rejects_every_windows_control_character(tmp_path: Path, code_point: int) -> None:
    archive_path = tmp_path / "control-character.zip"
    name = f"bad{chr(code_point)}name.txt"
    _write_crafted_backup(archive_path, [(name, b"must never be extracted")])

    with pytest.raises(ValidationError, match="non-portable backup entry"):
        Vault.restore_backup(archive_path, tmp_path / "restored")

    assert not (tmp_path / "restored").exists()


def test_backup_rejects_unicode_normalization_aliases(tmp_path: Path) -> None:
    archive_path = tmp_path / "unicode-alias.zip"
    _write_crafted_backup(
        archive_path,
        [
            ("\N{LATIN SMALL LETTER E WITH ACUTE}.txt", b"NFC"),
            ("e\N{COMBINING ACUTE ACCENT}.txt", b"NFD"),
        ],
    )

    with pytest.raises(ValidationError, match="duplicate backup entry"):
        Vault.restore_backup(archive_path, tmp_path / "restored")

    assert not (tmp_path / "restored").exists()


def test_backup_rejects_component_aliases_with_different_children(tmp_path: Path) -> None:
    archive_path = tmp_path / "component-alias.zip"
    _write_crafted_backup(
        archive_path,
        [("Folder/first.txt", b"first"), ("folder/second.txt", b"second")],
    )

    with pytest.raises(ValidationError, match="duplicate backup entry alias"):
        Vault.verify_backup(archive_path)


def test_backup_creation_hashes_and_writes_one_captured_source_read(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (vault.root / "MIND.md").read_bytes()
    read_source = vault_module._read_backup_source
    reads = 0

    def read_then_mutate(path: Path) -> bytes:
        nonlocal reads
        content = read_source(path)
        if path == vault.root / "MIND.md":
            reads += 1
            path.write_bytes(content + b"\nchanged after capture\n")
        return content

    monkeypatch.setattr(vault_module, "_read_backup_source", read_then_mutate)
    result = vault.create_backup(tmp_path / "snapshot.zip")

    assert reads == 1
    assert result["verified"] is True
    assert Vault.verify_backup(Path(result["backup"]))["valid"] is True
    with zipfile.ZipFile(result["backup"], "r") as archive:
        assert archive.read("MIND.md") == before
    assert (vault.root / "MIND.md").read_bytes() != before


def test_restore_rechecks_staged_files_against_manifest(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    monkeypatch.setattr(vault_module, "_hash_backup_files", lambda _files: {})
    target = tmp_path / "restored"

    with pytest.raises(ValidationError, match="staged vault files"):
        Vault.restore_backup(backup, target)

    assert not target.exists()


def test_stage_publication_replace_then_fsync_failure_reports_committed_restore(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored"

    def publish_then_raise(source: Path, destination: Path) -> None:
        actual_durable_replace(source, destination)
        if destination == target:
            raise OSError("injected parent fsync failure")

    monkeypatch.setattr(vault_module, "durable_replace", publish_then_raise)

    with pytest.raises(MutationCommittedError, match=r"published.*durability"):
        Vault.restore_backup(backup, target)

    assert Vault(target).logical_digest() == vault.logical_digest()


def test_backup_refuses_existing_destination_without_changing_its_bytes(
    vault: Vault, tmp_path: Path
) -> None:
    destination = tmp_path / "existing.zip"
    before = b"existing user bytes must survive\n"
    destination.write_bytes(before)

    with pytest.raises(ConflictError, match="already exists"):
        vault.create_backup(destination)

    assert destination.read_bytes() == before
    assert not list(tmp_path.glob(".gsv-backup.tmp-*.zip"))


def test_backup_refuses_authoritative_or_unowned_in_vault_destinations(vault: Vault) -> None:
    mind = vault.root / "MIND.md"
    mind_before = mind.read_bytes()

    with pytest.raises(ValidationError, match="owned backups"):
        vault.create_backup(mind)
    with pytest.raises(ValidationError, match="owned backups"):
        vault.create_backup(vault.root / "exports" / "archive.zip")

    assert mind.read_bytes() == mind_before
    assert not (vault.root / "exports").exists()


def test_default_backup_names_are_unique_within_one_frozen_second(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = datetime(2026, 7, 22, 23, 59, 59, tzinfo=UTC)

    class FrozenDateTime:
        @classmethod
        def now(cls, timezone: object) -> datetime:
            assert timezone is UTC
            return frozen

    monkeypatch.setattr(vault_module, "datetime", FrozenDateTime)
    first = Path(vault.create_backup()["backup"])
    second = Path(vault.create_backup()["backup"])

    assert first != second
    assert first.name.startswith("gsv-20260722T235959Z-")
    assert second.name.startswith("gsv-20260722T235959Z-")
    assert Vault.verify_backup(first)["valid"] is True
    assert Vault.verify_backup(second)["valid"] is True


def test_generated_backup_retries_only_a_generated_name_collision(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    backups = vault.root / "backups"
    collision = backups / "collision.zip"
    destination = backups / "available.zip"
    backups.mkdir(exist_ok=True)
    collision.write_bytes(b"preserve collision bytes\n")
    candidates = iter((collision, destination))
    monkeypatch.setattr(
        vault_module,
        "_generated_backup_destination",
        lambda _root: next(candidates),
    )

    result = vault.create_backup()

    assert Path(result["backup"]) == destination
    assert collision.read_bytes() == b"preserve collision bytes\n"
    assert Vault.verify_backup(destination)["valid"] is True


def test_backup_collision_created_during_publication_is_preserved(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "raced.zip"
    sentinel = b"race-created user archive\n"

    def race_publish(source: Path, target: Path) -> None:
        target.write_bytes(sentinel)
        actual_durable_publish_new(source, target)

    monkeypatch.setattr(vault_module, "durable_publish_new", race_publish)

    with pytest.raises(ConflictError, match="already exists"):
        vault.create_backup(destination)

    assert destination.read_bytes() == sentinel
    assert not list(tmp_path.glob(".gsv-backup.tmp-*.zip"))


def test_backup_fails_closed_when_hard_link_publication_is_unsupported(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "unsupported.zip"

    def unsupported(source: Path, target: Path) -> None:
        del source, target
        raise OSError(errno.ENOTSUP, "injected unsupported hard links")

    monkeypatch.setattr(vault_module, "durable_publish_new", unsupported)

    with pytest.raises(PersistenceError, match="was not published"):
        vault.create_backup(destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".gsv-backup.tmp-*.zip"))


def test_backup_publication_replace_then_fsync_failure_reports_committed_archive(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "backup.zip"

    def publish_then_raise(source: Path, target: Path) -> None:
        actual_durable_publish_new(source, target)
        if target == destination:
            raise OSError("injected backup parent fsync failure")

    monkeypatch.setattr(vault_module, "durable_publish_new", publish_then_raise)

    with pytest.raises(MutationCommittedError, match=r"published.*durability"):
        vault.create_backup(destination)

    assert Vault.verify_backup(destination)["valid"] is True
    assert not list(tmp_path.glob(".gsv-backup.tmp-*.zip"))


def test_backup_target_swap_after_link_never_reports_success(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement_vault = Vault(tmp_path / "replacement-vault")
    replacement_vault.initialize(name="Replacement")
    replacement = Path(replacement_vault.create_backup(tmp_path / "replacement.zip")["backup"])
    replacement_bytes = replacement.read_bytes()
    destination = tmp_path / "backup.zip"
    original_verify = Vault.verify_backup
    swapped = False

    def verify_after_swap(path: Path) -> dict[str, Any]:
        nonlocal swapped
        if Path(path) == destination and not swapped:
            os.replace(replacement, destination)
            swapped = True
        return original_verify(path)

    monkeypatch.setattr(Vault, "verify_backup", staticmethod(verify_after_swap))

    with pytest.raises(DegradedIntegrityError, match="changed during verification"):
        vault.create_backup(destination)

    assert swapped is True
    assert destination.read_bytes() == replacement_bytes


def test_prior_target_move_replace_then_fsync_failure_can_finish_durably(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored"
    target.mkdir()
    raised = False

    def move_then_raise_once(source: Path, destination: Path) -> None:
        nonlocal raised
        actual_durable_replace(source, destination)
        if source == target and not raised:
            raised = True
            raise OSError("injected prior-target fsync failure")

    monkeypatch.setattr(vault_module, "durable_replace", move_then_raise_once)
    restored = Vault.restore_backup(backup, target)

    assert raised is True
    assert restored["published"] is True
    assert restored["durability_confirmed"] is True
    assert Vault(target).logical_digest() == vault.logical_digest()
    assert list(tmp_path.glob(".restored.tmp-restore-prior-*")) == []
