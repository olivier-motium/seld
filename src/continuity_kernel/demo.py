"""Synthetic, privacy-safe demonstration of the Seld guarantees."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from continuity_kernel.errors import ConflictError
from continuity_kernel.vault import Vault


def run_demo(output: Path | None = None) -> dict[str, Any]:
    if output is None:
        with tempfile.TemporaryDirectory(prefix="gsv-demo-") as raw:
            return _run_demo(Path(raw))
    return _run_demo(output)


def _run_demo(output: Path) -> dict[str, Any]:
    root = output.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ConflictError(f"demo target is not empty: {root}")
    seeded = seed_demo_vault(root)
    vault = Vault(root)
    task = vault.get_task("ship-atlas-migration")
    thread = vault.get_thread("thread:atlas-migration")
    engineering = vault.get_entity("person:alex-chen-engineering")
    research = vault.get_entity("person:alex-chen-research")
    updated, hand_process_killed = _update_with_synthetic_hand(root, task.revision)
    stale_write_rejected = False
    try:
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            next_action="This stale update must never win.",
        )
    except ConflictError:
        stale_write_rejected = True

    resumed_record = _fresh_process_task(root, task.identifier)
    context = Vault(root).context_pack()
    fresh_process_resumed = (
        resumed_record.get("revision") == updated["revision"]
        and resumed_record.get("next_action") == updated["next_action"]
        and thread.identifier in context
    )
    if fresh_process_resumed:
        current_now = vault.read_document("NOW.md")
        vault.write_document(
            "NOW.md",
            """# Synthetic handoff proof

Hand 01 wrote the Atlas commitment and returned its new revision.
Its process was then terminated. A fresh Seld process recovered the exact revision
and next action without a rebrief.

## Authored synthetic coverage

- Build: available through the synthetic failover result.
- Calendar: available through the demo launch window.
- Mail: unavailable in this demo; no live inbox claim has been made.
""",
            expected_revision=current_now["revision"],
        )

    orphan = root / "tasks/.ship-atlas-migration.md.tmp-demo-crash"
    orphan.write_bytes(b"synthetic interrupted write")
    repaired = vault.doctor(repair=True)
    interrupted_write_recovered = repaired.healthy and not orphan.exists()

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
    unavailable_evidence = "Mail: unavailable" in vault.read_document("NOW.md")["content"]
    return {
        "backup_verified": backup["verified"],
        "doctor_healthy": doctor.healthy,
        "fresh_process_resumed": fresh_process_resumed,
        "hand_process_killed": hand_process_killed,
        "initialized": seeded["initialized"],
        "interrupted_write_recovered": interrupted_write_recovered,
        "logical_restore_equivalent": equivalent,
        "restore": {**restore, "temporary_target_removed": not restored_path.exists()},
        "same_name_entities_disambiguated": engineering.identifier != research.identifier,
        "stale_write_rejected": stale_write_rejected,
        "task_revision_before": task.revision,
        "task_revision_after": updated["revision"],
        "unavailable_evidence_explicit": unavailable_evidence,
        "vault": str(root),
    }


def seed_demo_vault(root: Path) -> dict[str, Any]:
    """Create the neutral, deterministic vault used by docs and the proof command."""

    vault = Vault(root)
    initialized = vault.initialize(name="Acme Seld Demo")
    mind = vault.read_document("MIND.md")
    vault.write_document(
        "MIND.md",
        """# The Mind

## Purpose

Carry a small number of important outcomes across replaceable execution hands.

## Judgment

- Keep authored commitments separate from activity logs.
- Name unavailable evidence instead of filling the gap with confidence.
- Require observed proof before declaring Atlas ready.
""",
        expected_revision=mind["revision"],
    )
    now = vault.read_document("NOW.md")
    vault.write_document(
        "NOW.md",
        """# Current orientation

Atlas has passed its synthetic failover test. The release decision remains open.
The exact rollback evidence is attached to the commitment.

## Evidence coverage

- Build: available through the synthetic failover result.
- Calendar: available through the demo launch window.
- Mail: unavailable in this demo; no inbox claim has been made.
""",
        expected_revision=now["revision"],
    )

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
    vault.create_task(
        identifier="approve-atlas-launch",
        title="Approve the Atlas launch window",
        outcome="A human has made the final release decision with the evidence in view.",
        status="ready",
        next_actor="human",
        next_action="Review rollback evidence and approve or decline the launch window.",
        refs=("demo:calendar:launch-window",),
    )
    vault.create_task(
        identifier="receive-atlas-signoff",
        title="Receive the compliance sign-off",
        outcome="The external reviewer has returned the bounded Atlas sign-off.",
        status="waiting",
        next_actor="external",
        waiting_on="The synthetic compliance reviewer.",
        refs=("demo:review:compliance",),
    )
    return {
        "initialized": initialized,
        "task": task.identifier,
        "thread": thread.identifier,
        "vault": str(root),
    }


def _update_with_synthetic_hand(root: Path, expected_revision: str) -> tuple[dict[str, Any], bool]:
    environment = os.environ.copy()
    environment["GSV_VAULT"] = str(root)
    process = subprocess.Popen(
        [*_runtime_command(), "--vault", str(root), "mcp", "serve"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "gsv_task_update",
                "arguments": {
                    "id": "ship-atlas-migration",
                    "expected_revision": expected_revision,
                    "next_action": (
                        "Review the completed failover evidence and record the release decision."
                    ),
                    "add_refs": ["demo:test:failover-passed"],
                },
            },
        },
    ]
    try:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("synthetic hand did not expose MCP pipes")
        process.stdin.write("".join(json.dumps(item) + "\n" for item in requests))
        process.stdin.flush()
        _read_json_line(process.stdout)
        response = _read_json_line(process.stdout)
        result = response.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise RuntimeError("synthetic hand could not update the Atlas commitment")
        updated = result.get("structuredContent")
        if not isinstance(updated, dict) or updated.get("revision") == expected_revision:
            raise RuntimeError("synthetic hand did not return a new task revision")
        if process.poll() is not None:
            raise RuntimeError("synthetic hand exited before the interruption proof")
        process.kill()
        process.wait(timeout=5)
        return updated, process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _read_json_line(stream: Any) -> dict[str, Any]:
    lines: queue.Queue[str] = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True)
    reader.start()
    try:
        line = lines.get(timeout=5)
    except queue.Empty as exc:
        raise RuntimeError("synthetic hand did not acknowledge the task revision") from exc
    if not line:
        raise RuntimeError("synthetic hand closed before acknowledging the task revision")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise RuntimeError("synthetic hand returned an invalid MCP response")
    return payload


def _fresh_process_task(root: Path, identifier: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["GSV_VAULT"] = str(root)
    result = subprocess.run(
        [
            *_runtime_command(),
            "--json",
            "--vault",
            str(root),
            "task",
            "show",
            identifier,
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=environment,
        timeout=30,
    )
    if result.returncode != 0:
        return {}
    payload = json.loads(result.stdout)
    value = payload.get("result")
    return value if isinstance(value, dict) else {}


def _runtime_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "continuity_kernel"]
