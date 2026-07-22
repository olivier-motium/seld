from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel import mcp_server
from continuity_kernel.vault import Vault


def _exchange(
    process: subprocess.Popen[str],
    method: str,
    request_id: int,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr else "MCP server exited"
    return cast(dict[str, Any], json.loads(line))


def _start(vault: Path) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["CONTINUITY_VAULT"] = str(vault)
    return subprocess.Popen(
        [sys.executable, "-m", "continuity_kernel", "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )


def _close(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=10) == 0


def test_two_independent_mcp_sessions_share_durable_state(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="MCP test")

    first = _start(vault)
    initialized = _exchange(first, "initialize", 1)
    tools = _exchange(first, "tools/list", 2)
    created = _exchange(
        first,
        "tools/call",
        3,
        {
            "name": "continuity_task_create",
            "arguments": {
                "id": "cross-session-proof",
                "title": "Cross-session proof",
                "outcome": "A fresh process can recover this task.",
                "status": "doing",
                "next_actor": "agent",
                "next_action": "Read from a second process.",
            },
        },
    )
    _close(first)

    second = _start(vault)
    _exchange(second, "initialize", 1)
    resumed = _exchange(
        second,
        "tools/call",
        2,
        {"name": "continuity_task_show", "arguments": {"id": "cross-session-proof"}},
    )
    _close(second)

    assert initialized["result"]["serverInfo"]["name"] == "continuity"
    assert any(tool["name"] == "continuity_task_create" for tool in tools["result"]["tools"])
    assert created["result"]["structuredContent"]["identifier"] == "cross-session-proof"
    assert resumed["result"]["structuredContent"]["next_action"] == ("Read from a second process.")


def test_mcp_returns_structured_conflict_instead_of_overwriting(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="Conflict test")
    task = vault.create_task(
        identifier="mcp-conflict",
        title="MCP conflict",
        outcome="Reject stale writes.",
    )
    vault.update_task(
        task.identifier,
        expected_revision=task.revision,
        outcome="Already changed.",
    )

    process = _start(vault_path)
    response = _exchange(
        process,
        "tools/call",
        1,
        {
            "name": "continuity_task_update",
            "arguments": {
                "id": task.identifier,
                "expected_revision": task.revision,
                "outcome": "Stale writer.",
            },
        },
    )
    _close(process)

    assert response["result"]["isError"] is True
    assert "reload it" in response["result"]["content"][0]["text"]
    assert vault.get_task(task.identifier).outcome == "Already changed."


def test_oversized_mcp_request_is_rejected_and_stream_recovers(tmp_path: Path) -> None:
    vault_path = tmp_path / "bounded"
    Vault(vault_path).initialize(name="Bounded MCP")
    process = _start(vault_path)
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write("x" * (mcp_server.MAX_REQUEST_BYTES + 1) + "\n")
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
    process.stdin.flush()

    oversized = json.loads(process.stdout.readline())
    recovered = json.loads(process.stdout.readline())
    _close(process)

    assert oversized["error"]["code"] == -32600
    assert recovered["id"] == 2
    assert recovered["result"] == {}


def test_bounded_line_reader_drains_oversized_frame() -> None:
    stream = io.BytesIO(
        b"x" * (mcp_server.MAX_REQUEST_BYTES + 1)
        + b"continued\n"
        + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )

    lines = list(mcp_server._bounded_lines(stream))

    assert lines == [None, b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n']


def test_serve_handles_bad_frames_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = (
        b"\n"
        b"\xff\n"
        b"[]\n"
        b'{"jsonrpc":"2.0","method":"notifications/future"}\n'
        + b"x" * (mcp_server.MAX_REQUEST_BYTES + 1)
        + b"\n"
        + b'{"jsonrpc":"2.0","id":4,"method":"ping"}\n'
    )
    stdin = io.TextIOWrapper(io.BytesIO(requests), encoding="utf-8")
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert mcp_server.serve() == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert [response.get("id") for response in responses] == [None, None, None, 4]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["error"]["code"] == -32000
    assert responses[2]["error"]["code"] == -32600
    assert responses[3]["result"] == {}


def _direct_call(name: str, arguments: dict[str, Any], request_id: int = 1) -> dict[str, Any]:
    response = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    return response


def test_direct_protocol_surface_exercises_all_record_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "direct-vault"
    vault = Vault(vault_path)
    vault.initialize(name="Direct MCP")
    monkeypatch.setenv("CONTINUITY_VAULT", str(vault_path))

    initialized = mcp_server._handle({"id": 1, "method": "initialize"})
    ping = mcp_server._handle({"id": 2, "method": "ping"})
    listed_tools = mcp_server._handle({"id": 3, "method": "tools/list"})
    assert mcp_server._handle({"method": "notifications/initialized"}) is None

    created_task = _direct_call(
        "continuity_task_create",
        {
            "id": "direct-task",
            "title": "Direct task",
            "outcome": "Exercise the protocol.",
            "status": "doing",
            "next_actor": "agent",
            "next_action": "Update through MCP.",
            "refs": ["test:direct"],
        },
    )["result"]["structuredContent"]
    updated_task = _direct_call(
        "continuity_task_update",
        {
            "id": "direct-task",
            "expected_revision": created_task["revision"],
            "next_action": "Create related records.",
            "add_refs": ["test:updated"],
        },
    )["result"]["structuredContent"]
    created_entity = _direct_call(
        "continuity_entity_create",
        {
            "id": "person:direct-owner",
            "title": "Direct Owner",
            "entity_type": "person",
            "summary": "Owns the direct protocol test.",
            "aliases": ["Owner"],
        },
    )["result"]["structuredContent"]
    updated_entity = _direct_call(
        "continuity_entity_update",
        {
            "id": created_entity["identifier"],
            "expected_revision": created_entity["revision"],
            "summary": "Owns and reviews the direct protocol test.",
            "aliases": ["Owner", "Reviewer"],
            "add_refs": ["test:entity-updated"],
        },
    )["result"]["structuredContent"]
    created_thread = _direct_call(
        "continuity_thread_create",
        {
            "id": "thread:direct",
            "title": "Direct thread",
            "purpose": "Connect direct records.",
            "summary": "Records created.",
            "task_ids": [created_task["identifier"]],
            "entity_ids": [created_entity["identifier"]],
        },
    )["result"]["structuredContent"]
    updated_thread = _direct_call(
        "continuity_thread_update",
        {
            "id": created_thread["identifier"],
            "expected_revision": created_thread["revision"],
            "summary": "Records verified.",
            "next_move": "Finish.",
        },
    )["result"]["structuredContent"]

    now = vault.read_document("NOW.md")
    updated_document = _direct_call(
        "continuity_document_update",
        {
            "name": "NOW.md",
            "content": "# Now\n\nDirect MCP state.",
            "expected_revision": now["revision"],
        },
    )["result"]["structuredContent"]
    calls = {
        "status": _direct_call("continuity_status", {}),
        "context": _direct_call("continuity_context", {"max_characters": 4000}),
        "doctor": _direct_call("continuity_doctor", {}),
        "task_list": _direct_call("continuity_task_list", {"status": "doing"}),
        "task_show": _direct_call("continuity_task_show", {"id": "direct-task"}),
        "entity_list": _direct_call("continuity_entity_list", {}),
        "entity_show": _direct_call("continuity_entity_show", {"id": "person:direct-owner"}),
        "thread_list": _direct_call("continuity_thread_list", {"status": "active"}),
        "thread_show": _direct_call("continuity_thread_show", {"id": "thread:direct"}),
        "document_show": _direct_call("continuity_document_show", {"name": "NOW.md"}),
        "backup": _direct_call("continuity_backup_create", {}),
    }

    assert initialized and initialized["result"]["protocolVersion"]
    assert ping and ping["result"] == {}
    assert listed_tools and len(listed_tools["result"]["tools"]) >= 10
    assert updated_task["next_action"] == "Create related records."
    assert updated_entity["aliases"] == ["Owner", "Reviewer"]
    assert updated_thread["summary"] == "Records verified."
    assert "Direct MCP state" in updated_document["content"]
    assert all(response["result"]["isError"] is False for response in calls.values())


def test_protocol_validation_and_tool_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "errors"
    Vault(vault_path).initialize(name="MCP errors")
    monkeypatch.setenv("CONTINUITY_VAULT", str(vault_path))

    missing_method = mcp_server._handle({"id": 1, "method": "unknown"})
    missing_name = mcp_server._handle({"id": 2, "method": "tools/call", "params": {}})
    bad_arguments = mcp_server._handle(
        {"id": 3, "method": "tools/call", "params": {"name": "x", "arguments": []}}
    )
    unknown_tool = _direct_call("not_a_tool", {})
    missing_field = _direct_call("continuity_task_show", {})
    unknown_notification = mcp_server._handle(
        {"jsonrpc": "2.0", "method": "notifications/future-extension"}
    )

    assert missing_method and missing_method["error"]["code"] == -32601
    assert missing_name and missing_name["error"]["code"] == -32602
    assert bad_arguments and bad_arguments["error"]["code"] == -32602
    assert unknown_tool["result"]["isError"] is True
    assert missing_field["result"]["isError"] is True
    assert unknown_notification is None
