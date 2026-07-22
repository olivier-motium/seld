"""Synthetic, privacy-safe demonstration of the continuity guarantees."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from continuity_kernel.errors import ConflictError
from continuity_kernel.vault import Vault


def run_demo(output: Path | None = None) -> dict[str, Any]:
    if output is None:
        with tempfile.TemporaryDirectory(prefix="continuity-demo-") as raw:
            return _run_demo(Path(raw))
    return _run_demo(output)


def _run_demo(output: Path) -> dict[str, Any]:
    root = output.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ConflictError(f"demo target is not empty: {root}")
    vault = Vault(root)
    initialized = vault.initialize(name="Acme Continuity Demo")

    engineering = vault.create_entity(
        identifier="person:alex-chen-engineering",
        title="Alex Chen",
        entity_type="person",
        summary="Engineering lead for the Atlas migration.",
        refs=("demo:directory:engineering",),
    )
    research = vault.create_entity(
        identifier="person:alex-chen-research",
        title="Alex Chen",
        entity_type="person",
        summary="Research reviewer for the Atlas evaluation protocol.",
        refs=("demo:directory:research",),
    )
    task = vault.create_task(
        identifier="ship-atlas-migration",
        title="Ship the Atlas migration",
        outcome="Atlas runs on the new storage path with verified rollback evidence.",
        status="doing",
        next_actor="agent",
        next_action="Run the synthetic failover test and attach its evidence reference.",
        refs=("demo:spec:atlas-v1",),
    )
    thread = vault.create_thread(
        identifier="thread:atlas-migration",
        title="Atlas migration",
        purpose="Carry the migration context across implementation and review sessions.",
        summary="The storage implementation is ready for a bounded failover test.",
        next_move="Complete failover verification, then decide whether the rollout is ready.",
        task_ids=(task.identifier,),
        entity_ids=(engineering.identifier, research.identifier),
        refs=("demo:decision:atlas",),
    )
    updated = vault.update_task(
        task.identifier,
        expected_revision=task.revision,
        next_action="Review the completed failover evidence and record the release decision.",
        add_refs=("demo:test:failover-passed",),
    )
    stale_write_rejected = False
    try:
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            next_action="This stale update must never win.",
        )
    except ConflictError:
        stale_write_rejected = True

    second_session = Vault(root)
    context = second_session.context_pack()
    resumed = (
        updated.identifier in context
        and "demo:test:failover-passed" not in context
        and bool(updated.next_action and updated.next_action in context)
        and thread.identifier in context
    )
    backup = vault.create_backup()
    restored_path = root.parent / f"{root.name}-restored"
    if restored_path.exists():
        raise ConflictError(f"demo restore target already exists: {restored_path}")
    try:
        restore = Vault.restore_backup(Path(backup["backup"]), restored_path)
        equivalent = vault.logical_digest() == Vault(restored_path).logical_digest()
    finally:
        shutil.rmtree(restored_path, ignore_errors=True)
    doctor = vault.doctor()
    return {
        "backup_verified": backup["verified"],
        "context_resumed": resumed,
        "doctor_healthy": doctor.healthy,
        "initialized": initialized,
        "logical_restore_equivalent": equivalent,
        "restore": {**restore, "temporary_target_removed": not restored_path.exists()},
        "same_name_entities_disambiguated": engineering.identifier != research.identifier,
        "stale_write_rejected": stale_write_rejected,
        "task_revision_before": task.revision,
        "task_revision_after": updated.revision,
        "vault": str(root),
    }
