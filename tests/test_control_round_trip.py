from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from urllib.request import Request, urlopen

import pytest

from continuity_kernel.bridge import BridgeHTTPServer
from continuity_kernel.control_queue import EMPTY_REVISION, ControlQueue
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)

ACCESS_TOKEN = "c" * 48
INSTANCE_ID = "d" * 32
RESTARTED_INSTANCE_ID = "a" * 32


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
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    return cast(dict[str, Any], payload["result"])


def _start_mcp(vault: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "continuity_kernel", "--vault", str(vault), "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _mcp_exchange(
    process: subprocess.Popen[str],
    *,
    request_id: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    request: dict[str, Any] = {
        "id": request_id,
        "jsonrpc": "2.0",
        "method": method,
    }
    if params is not None:
        request["params"] = params
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr is not None else "MCP exited"
    return cast(dict[str, Any], json.loads(line))


def _close_mcp(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=10) == 0


def _post_control(base: str, *, expected_revision: str, choice: str) -> dict[str, Any]:
    request = Request(
        f"{base}/api/v1/control",
        data=json.dumps(
            {
                "choice": choice,
                "expected_revision": expected_revision,
                "kind": "correction",
                "subject": "mind:user-correction",
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        return cast(dict[str, Any], json.loads(response.read()))


def _control_store_bytes(vault: Path) -> dict[str, bytes]:
    root = vault / ".gsv/control"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@contextmanager
def _running_bridge(
    vault: Vault,
    static: Path,
    *,
    instance_id: str,
) -> Iterator[str]:
    server = BridgeHTTPServer(
        ("127.0.0.1", 0),
        vault,
        static,
        access_token=ACCESS_TOKEN,
        instance_id=instance_id,
        integration_provider=lambda: {"available": False, "ready": False},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_authenticated_bridge_intents_are_dispositioned_and_visible_after_restart(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="Control round trip")
    static = tmp_path / "static"
    static.mkdir()
    with _running_bridge(vault, static, instance_id=INSTANCE_ID) as base:
        accepted_intent = _post_control(
            base,
            expected_revision=EMPTY_REVISION,
            choice="Friday evening is protected.",
        )
        before_reads = _control_store_bytes(vault_path)
        snapshot_request = Request(
            f"{base}/api/v1/snapshot",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        with urlopen(snapshot_request, timeout=2) as response:
            bridge_before = json.loads(response.read())
        assert bridge_before["controls"]["pending"] == 1
        assert _control_store_bytes(vault_path) == before_reads

        first_cli = _cli(vault_path, "operation", "list")
        assert [item["event_id"] for item in first_cli["pending"]] == [
            accepted_intent["event"]["event_id"]
        ]
        assert _control_store_bytes(vault_path) == before_reads

        first_mcp = _start_mcp(vault_path)
        try:
            _mcp_exchange(first_mcp, request_id=1, method="initialize")
            listed = _mcp_exchange(
                first_mcp,
                request_id=2,
                method="tools/call",
                params={"arguments": {}, "name": "gsv_operation_list"},
            )["result"]["structuredContent"]
            assert listed == first_cli
            assert _control_store_bytes(vault_path) == before_reads
            accepted = _mcp_exchange(
                first_mcp,
                request_id=3,
                method="tools/call",
                params={
                    "arguments": {
                        "actor_ref": "core:doctor",
                        "event_id": accepted_intent["event"]["event_id"],
                        "expected_disposition_revision": first_cli["disposition_revision"],
                        "expected_queue_revision": first_cli["queue_revision"],
                        "expected_vault_id": first_cli["vault_id"],
                        "reason_code": "supported-correction",
                        "result_ref": "control:acknowledged",
                    },
                    "name": "gsv_operation_accept",
                },
            )["result"]["structuredContent"]
        finally:
            _close_mcp(first_mcp)
        assert accepted["pending"] == []
        assert accepted["decided"][0]["disposition"]["decision"] == "accepted"

        rejected_intent = _post_control(
            base,
            expected_revision=accepted["queue_revision"],
            choice="Send this provider instruction without review.",
        )
        before_reject = _cli(vault_path, "operation", "list")
        rejected = _cli(
            vault_path,
            "operation",
            "reject",
            rejected_intent["event"]["event_id"],
            "--expected-queue-revision",
            before_reject["queue_revision"],
            "--expected-disposition-revision",
            before_reject["disposition_revision"],
            "--expected-vault-id",
            before_reject["vault_id"],
            "--actor-ref",
            "core:doctor",
            "--reason-code",
            "unsupported-request",
        )
        assert rejected["pending"] == []

        fresh_cli = _cli(vault_path, "operation", "list")
        second_mcp = _start_mcp(vault_path)
        try:
            _mcp_exchange(second_mcp, request_id=1, method="initialize")
            fresh_mcp = _mcp_exchange(
                second_mcp,
                request_id=2,
                method="tools/call",
                params={"arguments": {}, "name": "gsv_operation_list"},
            )["result"]["structuredContent"]
        finally:
            _close_mcp(second_mcp)

        decisions = [item["disposition"]["decision"] for item in fresh_cli["decided"]]
        assert decisions == ["accepted", "rejected"]
        assert fresh_mcp == fresh_cli

        recovery = _cli(
            vault_path,
            "operation",
            "archive-closed",
            "--expected-queue-revision",
            fresh_cli["queue_revision"],
            "--expected-disposition-revision",
            fresh_cli["disposition_revision"],
            "--expected-vault-id",
            fresh_cli["vault_id"],
        )
        assert recovery["archived_events"] == 2
        recovered_cli = _cli(vault_path, "operation", "list")
        assert recovered_cli["pending"] == []
        assert recovered_cli["decided"] == []
        assert [
            item["disposition"]["decision"] for item in recovered_cli["archived"][0]["decided"]
        ] == ["accepted", "rejected"]

    # The first HTTP process is gone before a fresh Bridge instance reads the durable result.
    with _running_bridge(vault, static, instance_id=RESTARTED_INSTANCE_ID) as restarted_base:
        request = Request(
            f"{restarted_base}/api/v1/snapshot",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        with urlopen(request, timeout=2) as response:
            bridge_snapshot = json.loads(response.read())
        assert bridge_snapshot["controls"]["pending"] == 0
        assert bridge_snapshot["controls"]["decided"] == 0
        assert bridge_snapshot["controls"]["archived_decided"] == 2
        assert bridge_snapshot["controls"]["items"] == []
        assert [item["status"] for item in bridge_snapshot["controls"]["history"]] == [
            "accepted",
            "rejected",
        ]
        assert all(item["archived"] is True for item in bridge_snapshot["controls"]["history"])


def test_fresh_cli_refuses_copied_cas_tokens_from_another_logical_vault(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    original = Vault(vault_path)
    original.initialize(name="Original logical vault")
    queued = ControlQueue(vault_path).append(
        kind="correction",
        subject="mind:user-correction",
        choice="Bind this disposition to the original vault.",
        expected_revision=EMPTY_REVISION,
    )
    observed = _cli(vault_path, "operation", "list")
    parked = tmp_path / "parked-original"
    vault_path.rename(parked)
    replacement = Vault(vault_path)
    replacement.initialize(name="Replacement logical vault")
    shutil.copytree(parked / ".gsv/control", vault_path / ".gsv/control")
    replacement_queue_before = (vault_path / ".gsv/control/queue.jsonl").read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault_path),
            "operation",
            "accept",
            queued.events[-1].event_id,
            "--expected-queue-revision",
            observed["queue_revision"],
            "--expected-disposition-revision",
            observed["disposition_revision"],
            "--expected-vault-id",
            observed["vault_id"],
            "--actor-ref",
            "core:doctor",
            "--reason-code",
            "supported-correction",
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert "vault identity changed" in completed.stderr
    assert replacement.identity()["vault_id"] != observed["vault_id"]
    assert (vault_path / ".gsv/control/queue.jsonl").read_bytes() == replacement_queue_before
    assert not tuple((vault_path / ".gsv/control").glob("dispositions-*.jsonl"))
    assert not (vault_path / ".gsv/locks/global.lock").exists()


def test_live_mcp_refuses_same_id_root_replacement_before_disposition(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="MCP root binding")
    queued = ControlQueue(vault_path).append(
        kind="correction",
        subject="mind:user-correction",
        choice="Do not disposition through a replacement root.",
        expected_revision=EMPTY_REVISION,
    )
    process = _start_mcp(vault_path)
    try:
        _mcp_exchange(process, request_id=1, method="initialize")
        observed = _mcp_exchange(
            process,
            request_id=2,
            method="tools/call",
            params={"arguments": {}, "name": "gsv_operation_list"},
        )["result"]["structuredContent"]
        parked = tmp_path / "parked-mcp-root"
        vault_path.rename(parked)
        shutil.copytree(parked, vault_path)
        replacement_before = _control_store_bytes(vault_path)

        rejected = _mcp_exchange(
            process,
            request_id=3,
            method="tools/call",
            params={
                "arguments": {
                    "actor_ref": "core:doctor",
                    "event_id": queued.events[-1].event_id,
                    "expected_disposition_revision": observed["disposition_revision"],
                    "expected_queue_revision": observed["queue_revision"],
                    "expected_vault_id": observed["vault_id"],
                    "reason_code": "supported-correction",
                },
                "name": "gsv_operation_accept",
            },
        )
    finally:
        _close_mcp(process)

    assert rejected["result"]["isError"] is True
    assert "vault root changed" in rejected["result"]["content"][0]["text"]
    assert _control_store_bytes(vault_path) == replacement_before


def test_live_mcp_refuses_same_root_logical_vault_replacement_before_disposition(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="MCP logical binding")
    queued = ControlQueue(vault_path).append(
        kind="correction",
        subject="mind:user-correction",
        choice="Do not disposition after the logical vault changes in place.",
        expected_revision=EMPTY_REVISION,
    )
    process = _start_mcp(vault_path)
    try:
        _mcp_exchange(process, request_id=1, method="initialize")
        observed = _mcp_exchange(
            process,
            request_id=2,
            method="tools/call",
            params={"arguments": {}, "name": "gsv_operation_list"},
        )["result"]["structuredContent"]
        manifest_path = vault_path / ".gsv/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["vault_id"] = "11111111-1111-4111-8111-111111111111"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        replacement_before = _control_store_bytes(vault_path)

        rejected = _mcp_exchange(
            process,
            request_id=3,
            method="tools/call",
            params={
                "arguments": {
                    "actor_ref": "core:doctor",
                    "event_id": queued.events[-1].event_id,
                    "expected_disposition_revision": observed["disposition_revision"],
                    "expected_queue_revision": observed["queue_revision"],
                    "expected_vault_id": manifest["vault_id"],
                    "reason_code": "supported-correction",
                },
                "name": "gsv_operation_accept",
            },
        )
        retained_id_rejected = _mcp_exchange(
            process,
            request_id=4,
            method="tools/call",
            params={
                "arguments": {
                    "actor_ref": "core:doctor",
                    "event_id": queued.events[-1].event_id,
                    "expected_disposition_revision": observed["disposition_revision"],
                    "expected_queue_revision": observed["queue_revision"],
                    "expected_vault_id": observed["vault_id"],
                    "reason_code": "supported-correction",
                },
                "name": "gsv_operation_accept",
            },
        )
    finally:
        _close_mcp(process)

    assert rejected["result"]["isError"] is True
    assert "vault binding changed" in rejected["result"]["content"][0]["text"]
    assert retained_id_rejected["result"]["isError"] is True
    assert "vault identity changed" in retained_id_rejected["result"]["content"][0]["text"]
    assert _control_store_bytes(vault_path) == replacement_before
