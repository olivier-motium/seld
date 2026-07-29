from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import continuity_kernel.update as self_update
from continuity_kernel import mcp_server
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED, EMPTY_REVISION, ControlQueue
from continuity_kernel.operations import OperationLedger
from continuity_kernel.records import format_time
from continuity_kernel.source_state import ABSENT_SOURCE_REVISION
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


def _start(
    vault: Path,
    *,
    profile: str | None = None,
    event_id: str | None = None,
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["GSV_VAULT"] = str(vault)
    command = [sys.executable, "-m", "continuity_kernel", "mcp", "serve"]
    if profile is not None:
        command.extend(("--profile", profile))
    if event_id is not None:
        command.extend(("--event-id", event_id))
    return subprocess.Popen(
        command,
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
            "name": "gsv_task_create",
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
        {"name": "gsv_task_show", "arguments": {"id": "cross-session-proof"}},
    )
    _close(second)

    assert initialized["result"]["serverInfo"]["name"] == "gsv"
    assert any(tool["name"] == "gsv_task_create" for tool in tools["result"]["tools"])
    assert created["result"]["structuredContent"]["identifier"] == "cross-session-proof"
    assert resumed["result"]["structuredContent"]["next_action"] == ("Read from a second process.")


@pytest.mark.skipif(
    not CONTROL_STORE_SUPPORTED,
    reason="exact guided-review operation binding requires secure local control storage",
)
def test_guided_review_profile_lists_and_dispatches_only_its_explicit_tools(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "guided-review-vault"
    Vault(vault_path).initialize(name="Guided review MCP")
    queue = ControlQueue(vault_path)
    first = queue.append(
        kind="correction",
        subject="mind:guided-review",
        choice="start exact review",
        expected_revision=EMPTY_REVISION,
    )
    exact_event = first.events[-1]
    second = queue.append(
        kind="correction",
        subject="mind:guided-review",
        choice="unrelated private choice",
        expected_revision=first.revision,
    )
    unrelated_event = second.events[-1]
    process = _start(
        vault_path,
        profile=mcp_server.GUIDED_REVIEW_PROFILE,
        event_id=exact_event.event_id,
    )
    omitted = {
        "gsv_backup_create",
        "gsv_direction_set",
        "gsv_doctor",
        "gsv_document_show",
        "gsv_document_update",
        "gsv_entity_create",
        "gsv_entity_update",
        "gsv_operation_archive_closed",
        "gsv_portfolio_migrate_review_session",
    }
    try:
        _exchange(process, "initialize", 1)
        tools = _exchange(process, "tools/list", 2)["result"]["tools"]
        names = {tool["name"] for tool in tools}
        expected = set(mcp_server.GUIDED_REVIEW_TOOL_NAMES)
        if not CONTROL_STORE_SUPPORTED:
            expected.difference_update(mcp_server.OPERATION_TOOL_NAMES)
        assert names == expected
        assert not names.intersection(omitted)

        operation = _exchange(
            process,
            "tools/call",
            3,
            {"name": "gsv_operation_list", "arguments": {}},
        )["result"]["structuredContent"]
        assert [event["event_id"] for event in operation["pending"]] == [exact_event.event_id]
        assert unrelated_event.event_id not in json.dumps(operation)

        rejected_other = _exchange(
            process,
            "tools/call",
            4,
            {
                "name": "gsv_operation_reject",
                "arguments": {
                    "actor_ref": "codex:11111111-1111-4111-8111-111111111111",
                    "event_id": unrelated_event.event_id,
                    "expected_disposition_revision": operation["disposition_revision"],
                    "expected_queue_revision": operation["queue_revision"],
                    "expected_vault_id": operation["vault_id"],
                    "reason_code": "outside_exact_event",
                },
            },
        )
        assert rejected_other["result"]["isError"] is True
        assert (
            "outside this guided-review MCP binding"
            in rejected_other["result"]["content"][0]["text"]
        )

        rejected_exact = _exchange(
            process,
            "tools/call",
            5,
            {
                "name": "gsv_operation_reject",
                "arguments": {
                    "actor_ref": "codex:11111111-1111-4111-8111-111111111111",
                    "event_id": exact_event.event_id,
                    "expected_disposition_revision": operation["disposition_revision"],
                    "expected_queue_revision": operation["queue_revision"],
                    "expected_vault_id": operation["vault_id"],
                    "reason_code": "guided-review-bound-event",
                },
            },
        )
        assert rejected_exact["result"]["isError"] is False
        exact_result = rejected_exact["result"]["structuredContent"]
        assert [item["event"]["event_id"] for item in exact_result["decided"]] == [
            exact_event.event_id
        ]
        assert unrelated_event.event_id not in json.dumps(exact_result)

        for request_id, name in enumerate((*sorted(omitted), "gsv_not_a_tool"), start=6):
            response = _exchange(
                process,
                "tools/call",
                request_id,
                {"name": name, "arguments": {}},
            )
            assert response["result"]["isError"] is True
            assert response["result"]["content"][0]["text"] == f"unknown tool: {name}"
    finally:
        _close(process)

    after = OperationLedger(vault_path).snapshot()
    assert [event.event_id for event in after.pending] == [unrelated_event.event_id]
    assert [event.event_id for event, _ in after.decided] == [exact_event.event_id]


def test_default_mcp_profile_remains_the_full_backwards_compatible_surface(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "default-profile-vault"
    Vault(vault_path).initialize(name="Default MCP")
    process = _start(vault_path)
    try:
        _exchange(process, "initialize", 1)
        names = {tool["name"] for tool in _exchange(process, "tools/list", 2)["result"]["tools"]}
    finally:
        _close(process)

    expected = {
        "gsv_backup_create",
        "gsv_context",
        "gsv_direction_set",
        "gsv_direction_show",
        "gsv_doctor",
        "gsv_document_show",
        "gsv_document_update",
        "gsv_entity_create",
        "gsv_entity_list",
        "gsv_entity_show",
        "gsv_entity_update",
        "gsv_local_file_grant_list",
        "gsv_local_file_read",
        "gsv_operation_accept",
        "gsv_operation_archive_closed",
        "gsv_operation_list",
        "gsv_operation_reject",
        "gsv_portfolio_inspect",
        "gsv_portfolio_migrate_review_session",
        "gsv_portfolio_set",
        "gsv_portfolio_show",
        "gsv_status",
        "gsv_source_list",
        "gsv_source_record",
        "gsv_source_select",
        "gsv_task_create",
        "gsv_task_list",
        "gsv_task_show",
        "gsv_task_update",
        "gsv_thread_create",
        "gsv_thread_list",
        "gsv_thread_show",
        "gsv_thread_update",
        "gsv_update_status",
    }
    if not CONTROL_STORE_SUPPORTED:
        expected.difference_update(mcp_server.OPERATION_TOOL_NAMES)
    assert names == expected


def test_update_mcp_surface_is_cache_only_and_cannot_apply_or_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = {"state": "available", "candidate": {"to_sha": "a" * 40}}
    monkeypatch.setattr(self_update, "status", lambda: cached)
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="MCP update status")

    assert mcp_server._call("gsv_update_status", {}, vault=vault) == cached
    tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
    description = tools["gsv_update_status"]["description"]
    assert "never uses the network" in description
    assert "cannot install or approve" in description
    assert "gsv_update_check" not in tools
    assert "gsv_update_apply" not in tools


def test_task_schema_distinguishes_codex_hand_from_gsv_workthread_without_narrowing_type() -> None:
    tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
    for name in ("gsv_task_create", "gsv_task_update"):
        properties = tools[name]["inputSchema"]["properties"]
        active_hand = properties["active_thread_id"]
        description = active_hand["description"]
        assert active_hand["type"] == "string"
        assert "raw Codex thread UUID" in description
        assert "never a Seld WorkThread ID" in description
        assert "pattern" not in active_hand and "format" not in active_hand

    update_properties = tools["gsv_task_update"]["inputSchema"]["properties"]
    assert "codex-thread:*" in update_properties["add_refs"]["description"]
    assert "codex-thread:*" in update_properties["remove_refs"]["description"]


@pytest.mark.skipif(os.name == "nt", reason="secure descriptor-pinned reads are POSIX-only")
def test_mcp_local_file_read_returns_safe_content_without_vault_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault_path = tmp_path / "local-file-vault"
    vault = Vault(vault_path)
    vault.initialize(name="MCP local file")
    selected_sources = vault.select_sources(
        expected_revision=ABSENT_SOURCE_REVISION,
        sources=("local_files",),
    )
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "note.txt").write_text("Prepare the invoice reply.", encoding="utf-8")
    (selected / "private-note.txt").write_text(
        "password: this-is-a-real-password-value",
        encoding="utf-8",
    )
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    before = vault.logical_digest()
    monkeypatch.setenv("GSV_VAULT", str(vault_path))

    process = _start(vault_path)
    try:
        _exchange(process, "initialize", 1)
        grants = _exchange(
            process,
            "tools/call",
            2,
            {"name": "gsv_local_file_grant_list", "arguments": {}},
        )["result"]["structuredContent"]
        safe = _exchange(
            process,
            "tools/call",
            3,
            {
                "name": "gsv_local_file_read",
                "arguments": {"grant_id": grant_id, "relative_path": "note.txt"},
            },
        )["result"]["structuredContent"]
        quarantined = _exchange(
            process,
            "tools/call",
            4,
            {
                "name": "gsv_local_file_read",
                "arguments": {"grant_id": grant_id, "relative_path": "private-note.txt"},
            },
        )["result"]["structuredContent"]
        arbitrary_root = _exchange(
            process,
            "tools/call",
            5,
            {
                "name": "gsv_local_file_read",
                "arguments": {"relative_path": "note.txt", "selected_root": str(selected)},
            },
        )
    finally:
        _close(process)

    assert grants["source_selected"] is True
    assert set(grants) == {"grants", "source_selected"}
    assert [item["grant_id"] for item in grants["grants"]] == [grant_id]
    assert set(grants["grants"][0]) == {
        "created_at",
        "current",
        "grant_id",
        "selected_root",
    }
    assert safe["content"] == "Prepare the invoice reply."
    assert safe["tool_binding"] == "gsv_local_file_read"
    assert safe["transient"] is True
    assert quarantined["decision"] == "quarantine"
    assert "content" not in quarantined
    assert arbitrary_root["result"]["isError"] is True
    assert "grant_id must be a non-empty string" in arbitrary_root["result"]["content"][0]["text"]
    assert Vault(vault_path).logical_digest() == before

    observed = vault.record_source_observation(
        expected_revision=selected_sources["revision"],
        source_id="local_files",
        actor_ref="codex:fresh-process-local-file-proof",
        result="success",
        covered_through=format_time(datetime.now(UTC)),
        completeness="complete",
        tool_binding="gsv_local_file_read",
    )
    assert observed["sources"][0]["freshness"] == "current"

    vault.revoke_local_file_grant(grant_id)
    restarted = _start(vault_path)
    try:
        _exchange(restarted, "initialize", 1)
        after_revoke = _exchange(
            restarted,
            "tools/call",
            2,
            {"name": "gsv_local_file_grant_list", "arguments": {}},
        )["result"]["structuredContent"]
        rejected = _exchange(
            restarted,
            "tools/call",
            3,
            {
                "name": "gsv_local_file_read",
                "arguments": {"grant_id": grant_id, "relative_path": "note.txt"},
            },
        )
        source_after_revoke = _exchange(
            restarted,
            "tools/call",
            4,
            {"name": "gsv_source_list", "arguments": {}},
        )["result"]["structuredContent"]
    finally:
        _close(restarted)

    assert after_revoke["grants"] == []
    assert source_after_revoke["state"]["sources"][0]["freshness"] == ("needs_revalidation")
    assert rejected["result"]["isError"] is True
    assert "not found for this vault" in rejected["result"]["content"][0]["text"]

    vault.select_sources(expected_revision=vault.get_source_snapshot().revision, sources=())
    after_deselect = _direct_call("gsv_local_file_grant_list", {})["result"]["structuredContent"]
    assert after_deselect["grants"] == []
    assert after_deselect["source_selected"] is False


def test_mcp_authors_and_reads_complete_portfolio(tmp_path: Path) -> None:
    vault_path = tmp_path / "portfolio-vault"
    Vault(vault_path).initialize(name="MCP Portfolio")
    process = _start(vault_path)
    try:
        _exchange(process, "initialize", 1)
        tools = _exchange(process, "tools/list", 2)["result"]["tools"]
        created = _exchange(
            process,
            "tools/call",
            3,
            {
                "name": "gsv_task_create",
                "arguments": {
                    "active_thread_id": "exact-hand",
                    "id": "mcp-ranked-outcome",
                    "outcome": "Remain exact across processes.",
                    "rank": 5,
                    "title": "MCP ranked outcome",
                },
            },
        )["result"]["structuredContent"]
        authored = _exchange(
            process,
            "tools/call",
            4,
            {
                "name": "gsv_portfolio_set",
                "arguments": {
                    "expected_revision": "absent",
                    "items": [
                        {
                            "reason": "Keep exact authored order.",
                            "stance": "keep-in-view",
                            "task_id": created["identifier"],
                            "task_revision": created["revision"],
                        }
                    ],
                    "summary": "One complete outcome.",
                },
            },
        )["result"]["structuredContent"]
        shown = _exchange(
            process,
            "tools/call",
            5,
            {"name": "gsv_portfolio_show", "arguments": {}},
        )["result"]["structuredContent"]
    finally:
        _close(process)

    names = {tool["name"] for tool in tools}
    assert {"gsv_portfolio_show", "gsv_portfolio_set"} <= names
    assert authored == shown
    assert created["rank"] == 5
    assert created["active_thread_id"] == "exact-hand"


def test_cli_explicit_vault_overrides_mcp_environment_for_process_lifetime(
    tmp_path: Path,
) -> None:
    environment_vault = tmp_path / "environment-vault"
    explicit_vault = tmp_path / "explicit-vault"
    Vault(environment_vault).initialize(name="Environment vault")
    Vault(explicit_vault).initialize(name="Explicit vault")
    environment = os.environ.copy()
    environment["GSV_VAULT"] = str(environment_vault)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--vault",
            str(explicit_vault),
            "mcp",
            "serve",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )

    try:
        _exchange(process, "initialize", 1)
        status = _exchange(
            process,
            "tools/call",
            2,
            {"name": "gsv_status", "arguments": {}},
        )["result"]["structuredContent"]
        created = _exchange(
            process,
            "tools/call",
            3,
            {
                "name": "gsv_task_create",
                "arguments": {
                    "id": "explicit-route",
                    "title": "Explicit route",
                    "outcome": "The CLI override owns this MCP process.",
                },
            },
        )["result"]["structuredContent"]
    finally:
        _close(process)

    assert status["vault"] == str(explicit_vault.resolve())
    assert created["identifier"] == "explicit-route"
    assert Vault(explicit_vault).get_task("explicit-route").identifier == "explicit-route"
    assert Vault(environment_vault).list_tasks() == []


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
            "name": "gsv_task_update",
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "stream-vault")
    vault.initialize(name="Stream vault")
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

    assert mcp_server.serve(vault) == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert [response.get("id") for response in responses] == [None, None, None, 4]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["error"]["code"] == -32000
    assert responses[2]["error"]["code"] == -32600
    assert responses[3]["result"] == {}


def test_serve_resolves_one_vault_for_the_whole_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path = tmp_path / "first-vault"
    second_path = tmp_path / "second-vault"
    Vault(first_path).initialize(name="First vault")
    Vault(second_path).initialize(name="Second vault")
    resolved: list[Path] = []

    def alternating_resolution() -> Path:
        path = first_path if not resolved else second_path
        resolved.append(path)
        return path

    requests = b"".join(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "gsv_status", "arguments": {}},
            }
        ).encode("utf-8")
        + b"\n"
        for request_id in (1, 2)
    )
    stdin = io.TextIOWrapper(io.BytesIO(requests), encoding="utf-8")
    stdout = io.StringIO()
    monkeypatch.setattr(mcp_server, "resolve_vault", alternating_resolution)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert mcp_server.serve() == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert resolved == [first_path]
    assert [response["result"]["structuredContent"]["vault"] for response in responses] == [
        str(first_path.resolve()),
        str(first_path.resolve()),
    ]


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


def test_mcp_does_not_advertise_or_dispatch_unsupported_control_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "CONTROL_STORE_SUPPORTED", False)

    listed = mcp_server._handle({"id": 1, "method": "tools/list"})
    assert listed is not None
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert not names.intersection(mcp_server.OPERATION_TOOL_NAMES)

    called = mcp_server._handle(
        {
            "id": 2,
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"arguments": {}, "name": "gsv_operation_list"},
        }
    )
    assert called is not None
    assert called["result"]["isError"] is True
    assert called["result"]["content"][0]["text"] == "unknown tool: gsv_operation_list"


def test_direct_protocol_surface_exercises_all_record_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "direct-vault"
    vault = Vault(vault_path)
    vault.initialize(name="Direct MCP")
    monkeypatch.setenv("GSV_VAULT", str(vault_path))

    initialized = mcp_server._handle({"id": 1, "method": "initialize"})
    ping = mcp_server._handle({"id": 2, "method": "ping"})
    listed_tools = mcp_server._handle({"id": 3, "method": "tools/list"})
    assert mcp_server._handle({"method": "notifications/initialized"}) is None

    created_task = _direct_call(
        "gsv_task_create",
        {
            "id": "direct-task",
            "title": "Direct task",
            "outcome": "Exercise the protocol.",
            "status": "doing",
            "next_actor": "agent",
            "next_action": "Update through MCP.",
            "active_thread_id": "legacy-free-form-hand",
            "refs": ["test:direct"],
        },
    )["result"]["structuredContent"]
    updated_task = _direct_call(
        "gsv_task_update",
        {
            "id": "direct-task",
            "expected_revision": created_task["revision"],
            "next_action": "Create related records.",
            "add_refs": ["test:updated"],
        },
    )["result"]["structuredContent"]
    created_entity = _direct_call(
        "gsv_entity_create",
        {
            "id": "person:direct-owner",
            "title": "Direct Owner",
            "entity_type": "person",
            "summary": "Owns the direct protocol test.",
            "aliases": ["Owner"],
        },
    )["result"]["structuredContent"]
    updated_entity = _direct_call(
        "gsv_entity_update",
        {
            "id": created_entity["identifier"],
            "expected_revision": created_entity["revision"],
            "summary": "Owns and reviews the direct protocol test.",
            "aliases": ["Owner", "Reviewer"],
            "add_refs": ["test:entity-updated"],
        },
    )["result"]["structuredContent"]
    created_thread = _direct_call(
        "gsv_thread_create",
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
        "gsv_thread_update",
        {
            "id": created_thread["identifier"],
            "expected_revision": created_thread["revision"],
            "summary": "Records verified.",
            "next_move": "Finish.",
        },
    )["result"]["structuredContent"]

    now = vault.read_document("NOW.md")
    updated_document = _direct_call(
        "gsv_document_update",
        {
            "name": "NOW.md",
            "content": "# Now\n\nDirect MCP state.",
            "expected_revision": now["revision"],
        },
    )["result"]["structuredContent"]
    source_catalog = _direct_call("gsv_source_list", {})["result"]["structuredContent"]
    selected_sources = _direct_call(
        "gsv_source_select",
        {"expected_revision": source_catalog["state"]["revision"], "sources": ["gmail"]},
    )["result"]["structuredContent"]
    recorded_source = _direct_call(
        "gsv_source_record",
        {
            "account_binding": "workspace:test-account",
            "actor_ref": "direct-mcp-task",
            "completeness": "complete",
            "covered_through": "2026-07-28T10:00:00Z",
            "expected_revision": selected_sources["revision"],
            "result": "explicit_empty",
            "source": "gmail",
            "tool_binding": "gmail.search.v1",
        },
    )["result"]["structuredContent"]
    calls = {
        "status": _direct_call("gsv_status", {}),
        "sources": _direct_call("gsv_source_list", {}),
        "context": _direct_call("gsv_context", {"max_characters": 4000}),
        "doctor": _direct_call("gsv_doctor", {}),
        "task_list": _direct_call("gsv_task_list", {"status": "doing"}),
        "task_show": _direct_call("gsv_task_show", {"id": "direct-task"}),
        "entity_list": _direct_call("gsv_entity_list", {}),
        "entity_show": _direct_call("gsv_entity_show", {"id": "person:direct-owner"}),
        "thread_list": _direct_call("gsv_thread_list", {"status": "active"}),
        "thread_show": _direct_call("gsv_thread_show", {"id": "thread:direct"}),
        "document_show": _direct_call("gsv_document_show", {"name": "NOW.md"}),
        "backup": _direct_call("gsv_backup_create", {}),
    }

    assert initialized and initialized["result"]["protocolVersion"]
    assert ping and ping["result"] == {}
    assert listed_tools and len(listed_tools["result"]["tools"]) >= 10
    assert updated_task["next_action"] == "Create related records."
    assert updated_task["active_thread_id"] == "legacy-free-form-hand"
    assert updated_entity["aliases"] == ["Owner", "Reviewer"]
    assert updated_thread["summary"] == "Records verified."
    assert "Direct MCP state" in updated_document["content"]
    assert recorded_source["sources"][0]["observation"]["result"] == "explicit_empty"
    assert all(response["result"]["isError"] is False for response in calls.values())


def test_protocol_validation_and_tool_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "errors"
    Vault(vault_path).initialize(name="MCP errors")
    monkeypatch.setenv("GSV_VAULT", str(vault_path))

    missing_method = mcp_server._handle({"id": 1, "method": "unknown"})
    missing_name = mcp_server._handle({"id": 2, "method": "tools/call", "params": {}})
    bad_arguments = mcp_server._handle(
        {"id": 3, "method": "tools/call", "params": {"name": "x", "arguments": []}}
    )
    unknown_tool = _direct_call("not_a_tool", {})
    missing_field = _direct_call("gsv_task_show", {})
    unknown_notification = mcp_server._handle(
        {"jsonrpc": "2.0", "method": "notifications/future-extension"}
    )

    assert missing_method and missing_method["error"]["code"] == -32601
    assert missing_name and missing_name["error"]["code"] == -32602
    assert bad_arguments and bad_arguments["error"]["code"] == -32602
    assert unknown_tool["result"]["isError"] is True
    assert missing_field["result"]["isError"] is True
    assert unknown_notification is None


def test_source_record_losing_to_concurrent_deselection_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "source-race"
    Vault(vault_path).initialize(name="Source race")
    monkeypatch.setenv("GSV_VAULT", str(vault_path))

    initial = _direct_call("gsv_source_list", {})["result"]["structuredContent"]
    selected = _direct_call(
        "gsv_source_select",
        {"expected_revision": initial["state"]["revision"], "sources": ["gmail"]},
    )["result"]["structuredContent"]
    deselected = _direct_call(
        "gsv_source_select",
        {"expected_revision": selected["revision"], "sources": []},
    )["result"]["structuredContent"]

    stale_record = _direct_call(
        "gsv_source_record",
        {
            "actor_ref": "stale-mcp-task",
            "error_code": "timeout",
            "expected_revision": selected["revision"],
            "result": "failure",
            "source": "gmail",
        },
    )
    assert stale_record["result"]["isError"] is True
    assert "record changed" in stale_record["result"]["content"][0]["text"]

    current = _direct_call("gsv_source_list", {})["result"]["structuredContent"]
    assert current["state"]["revision"] == deselected["revision"]
    assert current["state"]["selected_count"] == 0
