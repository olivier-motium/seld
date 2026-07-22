from __future__ import annotations

import os
from pathlib import Path

import pytest

from continuity_kernel import atomic
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
