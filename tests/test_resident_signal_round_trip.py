from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel.errors import ConflictError, NotFoundError
from continuity_kernel.vault import Vault


def _cli(vault: Path, *arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cast(dict[str, Any], json.loads(completed.stdout)["result"])


def _start_mcp(vault: Path) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["GSV_VAULT"] = str(vault)
    return subprocess.Popen(
        [sys.executable, "-m", "continuity_kernel", "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )


def _exchange(
    process: subprocess.Popen[str],
    request_id: int,
    name: str,
    arguments: dict[str, object],
) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        json.dumps(
            {
                "id": request_id,
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"arguments": arguments, "name": name},
            }
        )
        + "\n"
    )
    process.stdin.flush()
    response = process.stdout.readline()
    assert response, process.stderr.read() if process.stderr is not None else "MCP stopped"
    payload = cast(dict[str, Any], json.loads(response))
    return cast(dict[str, Any], payload["result"]["structuredContent"])


def _close(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=10) == 0


def test_cli_to_mcp_disposition_round_trip_is_durable_across_processes(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="Resident signal round trip")
    task = vault.create_task(
        identifier="signal-result",
        title="Durable signal result",
        outcome="The signal has one inspectable semantic disposition.",
        status="doing",
        next_actor="agent",
        next_action="Acknowledge the exact evidence envelope.",
    )
    record_ref = f"task:{task.identifier}@{task.revision}"

    appended = _cli(
        vault_path,
        "signal",
        "append",
        "--record-ref",
        record_ref,
        "--change-type",
        "observation",
    )

    first = _start_mcp(vault_path)
    listed = _exchange(
        first,
        1,
        "gsv_signal_list",
        {"include_acknowledged": False, "limit": 10},
    )
    shown = _exchange(
        first,
        2,
        "gsv_signal_show",
        {"input_id": appended["input_id"]},
    )
    acknowledged = _exchange(
        first,
        3,
        "gsv_signal_acknowledge",
        {
            "consumer": "pulse:019f0000-0000-7000-8000-000000000099",
            "disposition": "accepted",
            "expected_revision": listed["revision"],
            "input_ids": [appended["input_id"]],
            "result_refs": [record_ref],
        },
    )
    _close(first)

    fresh_status = _cli(vault_path, "signal", "status")
    fresh_all = _cli(vault_path, "signal", "list", "--include-acknowledged")
    fresh_task = _cli(vault_path, "task", "show", task.identifier)

    assert shown["envelope"] == {"change_type": "observation"}
    assert shown["ref"] == record_ref
    assert acknowledged["acknowledgements"][0]["disposition"] == "accepted"
    assert fresh_status["pending"] == 0
    assert fresh_all["acknowledged_ids"] == [appended["input_id"]]
    assert fresh_task["revision"] == task.revision


def test_signal_ack_requires_a_live_exact_canonical_result_across_processes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="Canonical signal result")
    task = vault.create_task(
        identifier="canonical-result",
        title="Canonical result",
        outcome="Every acknowledgement resolves to inspectable canonical state.",
        status="doing",
        next_actor="agent",
        next_action="Prove exact result integrity.",
    )
    signal = vault.append_canonical_signal(
        record_ref=f"task:{task.identifier}@{task.revision}",
        change_type="observation",
    )
    before = vault.resident_signal_status()
    missing = f"task:missing-result@{'a' * 64}"

    with pytest.raises(NotFoundError, match=r"file does not exist: tasks/missing-result\.md"):
        Vault(vault_path).acknowledge_resident_signals(
            (signal["input_id"],),
            expected_revision=before["revision"],
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="rejected",
            result_refs=(missing,),
        )
    assert Vault(vault_path).resident_signal_status()["pending"] == 1

    updated = vault.update_task(
        task.identifier,
        expected_revision=task.revision,
        next_action="Accept the exact current result.",
        note="Advanced the canonical record before acknowledgement.",
    )
    with pytest.raises(ConflictError, match="result record changed"):
        Vault(vault_path).acknowledge_resident_signals(
            (signal["input_id"],),
            expected_revision=before["revision"],
            consumer="pulse:019f0000-0000-7000-8000-000000000099",
            disposition="accepted",
            result_refs=(f"task:{task.identifier}@{task.revision}",),
        )
    assert Vault(vault_path).resident_signal_status()["pending"] == 1

    exact_ref = f"task:{updated.identifier}@{updated.revision}"
    acknowledged = Vault(vault_path).acknowledge_resident_signals(
        (signal["input_id"],),
        expected_revision=before["revision"],
        consumer="pulse:019f0000-0000-7000-8000-000000000099",
        disposition="accepted",
        result_refs=(exact_ref,),
    )

    assert acknowledged[0]["result_refs"] == (exact_ref,)
    assert Vault(vault_path).resident_signal_status()["pending"] == 0


def test_entity_result_reference_round_trips_through_durable_acknowledgement(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="Entity result")
    entity = vault.create_entity(
        identifier="person:canonical-result",
        title="Canonical result owner",
        entity_type="person",
        summary="Owns the inspectable result.",
    )
    entity_ref = f"entity:{entity.identifier}@{entity.revision}"
    signal = vault.append_canonical_signal(
        record_ref=entity_ref,
        change_type="observation",
    )
    before = vault.resident_signal_status()

    acknowledgements = Vault(vault_path).acknowledge_resident_signals(
        (signal["input_id"],),
        expected_revision=before["revision"],
        consumer="pulse:019f0000-0000-7000-8000-000000000099",
        disposition="accepted",
        result_refs=(entity_ref,),
    )

    assert acknowledgements[0]["result_refs"] == (entity_ref,)
    fresh = Vault(vault_path).list_resident_signals(include_acknowledged=True)
    assert fresh["acknowledged_ids"] == [signal["input_id"]]
