from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.vault import BACKUP_MANIFEST, Vault


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
