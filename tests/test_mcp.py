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
from continuity_kernel import mcp_server, resident_import
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED, EMPTY_REVISION, ControlQueue
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.operations import OperationLedger
from continuity_kernel.records import Task, format_time, record_dict
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


class _TaskListVault:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks

    def list_tasks(self, *, status: str | None = None) -> list[Task]:
        return [task for task in self.tasks if status is None or task.status == status]

    def get_task(self, identifier: str) -> Task:
        return next(task for task in self.tasks if task.identifier == identifier)


def _task_list_record(index: int, *, revision: str | None = None) -> Task:
    timestamp = "2026-07-28T10:00:00Z"
    return Task(
        identifier=f"task-{index:04d}",
        title=f"Task {index:04d}",
        status="doing",
        next_actor="agent",
        outcome=(f"Literal outcome {index}.\n" * 80).strip(),
        next_action=(f"Literal next action {index}.\n" * 80).strip(),
        waiting_on=(f"Literal dependency {index}.\n" * 80).strip(),
        rank=index,
        active_thread_id=f"hand-{index:04d}",
        refs=tuple(f"test:reference-{index}-{item}" for item in range(50)),
        created_at=timestamp,
        updated_at=timestamp,
        revision=revision or f"{index + 1:064x}",
        project=f"project-{index % 7}",
        workspace=f"/synthetic/workspace/{index}",
        codex_episode_ids=(f"episode-{index:04d}",),
        state_changed_at=timestamp,
        history=tuple(f"Synthetic history {index}-{item} " + "x" * 900 for item in range(5)),
    )


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


def test_task_list_pages_large_ledgers_without_history_or_silent_snapshot_drift() -> None:
    tasks = [_task_list_record(index) for index in reversed(range(313))]
    fake = _TaskListVault(tasks)
    bound = cast(Vault, fake)
    unbounded_bytes = len(
        json.dumps(
            {"tasks": [record_dict(task) for task in tasks]},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    )
    assert unbounded_bytes > 1024 * 1024

    arguments: dict[str, Any] = {"limit": 50, "status": "doing"}
    seen: list[str] = []
    pages: list[dict[str, Any]] = []
    while True:
        page = mcp_server._call("gsv_task_list", arguments, vault=bound)
        pages.append(page)
        encoded = json.dumps(page, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        assert len(encoded) <= mcp_server.TASK_LIST_MAX_PAGE_BYTES
        for task in page["tasks"]:
            assert task["agent_run"] is None
            assert {
                "history",
                "refs",
                "codex_episode_ids",
                "entity_links",
                "workspace",
            }.isdisjoint(task)
            assert task["history_count"] == 5
            assert task["reference_count"] == 50
            assert task["codex_episode_count"] == 1
            assert set(task["truncated_fields"]) == {
                "next_action",
                "outcome",
                "waiting_on",
            }
            seen.append(cast(str, task["identifier"]))
        cursor = page["next_cursor"]
        if cursor is None:
            break
        arguments = {"cursor": cursor, "limit": 50, "status": "doing"}

    expected = [task.identifier for task in sorted(tasks, key=lambda item: item.identifier)]
    assert seen == expected
    assert len(seen) == len(set(seen)) == 313
    assert len(pages) > 1
    assert {page["snapshot_revision"] for page in pages} == {pages[0]["snapshot_revision"]}
    assert sum(cast(int, page["returned"]) for page in pages) == 313

    repeated = mcp_server._call(
        "gsv_task_list",
        {"limit": 50, "status": "doing"},
        vault=bound,
    )
    assert repeated == pages[0]
    transport = mcp_server._handle(
        {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "arguments": {"limit": 50, "status": "doing"},
                "name": "gsv_task_list",
            },
        },
        vault=bound,
    )
    assert transport is not None
    assert (
        len(json.dumps(transport, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        <= mcp_server.TASK_LIST_MAX_PAGE_BYTES * 4
    )
    exact = mcp_server._call("gsv_task_show", {"id": seen[0]}, vault=bound)
    assert len(exact["history"]) == 5
    assert len(exact["refs"]) == 50

    cursor = cast(str, pages[0]["next_cursor"])
    fake.tasks[0] = _task_list_record(312, revision="f" * 64)
    with pytest.raises(ConflictError, match="changed during pagination"):
        mcp_server._call(
            "gsv_task_list",
            {"cursor": cursor, "limit": 50, "status": "doing"},
            vault=bound,
        )


def test_task_list_cursor_contract_is_strict_and_fresh_process_portable(tmp_path: Path) -> None:
    vault_path = tmp_path / "paged-task-vault"
    vault = Vault(vault_path)
    vault.initialize(name="Paged tasks")
    for index in range(61):
        vault.create_task(
            identifier=f"paged-{index:03d}",
            title=f"Paged task {index:03d}",
            outcome=f"Remain visible on page {index}.",
            status="doing",
            next_actor="agent",
            next_action="Continue exact pagination.",
        )
    expected = [task.identifier for task in vault.list_tasks(status="doing")]

    first = _start(vault_path)
    try:
        _exchange(first, "initialize", 1)
        first_page = _exchange(
            first,
            "tools/call",
            2,
            {
                "name": "gsv_task_list",
                "arguments": {"limit": 13, "status": "doing"},
            },
        )["result"]["structuredContent"]
    finally:
        _close(first)

    seen = [task["identifier"] for task in first_page["tasks"]]
    cursor = first_page["next_cursor"]
    second = _start(vault_path)
    try:
        _exchange(second, "initialize", 1)
        request_id = 2
        while cursor is not None:
            page = _exchange(
                second,
                "tools/call",
                request_id,
                {
                    "name": "gsv_task_list",
                    "arguments": {"cursor": cursor, "limit": 13, "status": "doing"},
                },
            )["result"]["structuredContent"]
            seen.extend(task["identifier"] for task in page["tasks"])
            cursor = page["next_cursor"]
            request_id += 1
    finally:
        _close(second)

    assert seen == expected
    assert len(seen) == len(set(seen)) == 61

    tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "gsv_task_list")
    properties = tool["inputSchema"]["properties"]
    assert set(properties) == {"cursor", "limit", "status"}
    assert properties["limit"] == {
        "maximum": mcp_server.TASK_LIST_MAX_LIMIT,
        "minimum": 1,
        "type": "integer",
    }
    assert "gsv_task_show" in tool["description"]

    with pytest.raises(ValidationError, match="limit must be"):
        mcp_server._call("gsv_task_list", {"limit": 0}, vault=vault)
    with pytest.raises(ValidationError, match="cursor is invalid"):
        mcp_server._call("gsv_task_list", {"cursor": "not-a-cursor"}, vault=vault)
    with pytest.raises(ValidationError, match="status filter"):
        mcp_server._call(
            "gsv_task_list",
            {"cursor": first_page["next_cursor"], "limit": 13},
            vault=vault,
        )


def test_resident_activation_mcp_surfaces_are_exact_bounded_and_fresh_process(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "resident-mcp-vault"
    vault = Vault(vault_path)
    vault.initialize(name="Resident MCP")
    resident = vault_path / "context/resident"
    skill = resident / "skills/exact-native/scripts"
    skill.mkdir(parents=True)
    guidance = "# Resident guidance\n\nExact MCP guidance.\n"
    (resident / "AGENTS.md").write_bytes(guidance.encode("utf-8"))
    (skill.parent / "SKILL.md").write_text(
        "---\nname: exact-native\ndescription: Exact native skill.\n---\n\n# Exact\n",
        encoding="utf-8",
    )
    (skill / "check.py").write_text("print('native')\n", encoding="utf-8")
    control = resident / "control"
    control.mkdir()
    (control / "RESIDENT").write_text("legacy-private-hand\n", encoding="utf-8")
    task = vault.create_task(
        identifier="resident-mcp-task",
        title="Resident MCP task",
        outcome="Remain visible through the structural index.",
        status="doing",
        next_actor="agent",
        next_action="Read the activation tools.",
    )
    task = vault._update_task_dispatch(
        task.identifier,
        status="doing",
        active_thread_id="resident-mcp-hand",
        expected_revision=task.revision,
    )
    vault.create_thread(
        identifier="thread:resident-mcp",
        title="Resident MCP thread",
        purpose="Carry the activation proof.",
        summary="The proof is current.",
        focus_task_id=task.identifier,
        task_ids=(task.identifier,),
    )

    process = _start(vault_path)
    try:
        _exchange(process, "initialize", 1)
        names = {tool["name"] for tool in _exchange(process, "tools/list", 2)["result"]["tools"]}
        status = _exchange(
            process,
            "tools/call",
            3,
            {"name": "gsv_resident_context_status", "arguments": {}},
        )["result"]["structuredContent"]
        shown = _exchange(
            process,
            "tools/call",
            4,
            {"name": "gsv_resident_guidance_show", "arguments": {}},
        )["result"]["structuredContent"]
        bindings = _exchange(
            process,
            "tools/call",
            5,
            {"name": "gsv_execution_bindings", "arguments": {}},
        )["result"]["structuredContent"]
    finally:
        _close(process)

    assert {
        "gsv_execution_bindings",
        "gsv_resident_context_status",
        "gsv_resident_guidance_show",
    }.issubset(names)
    assert shown["content"] == guidance
    assert status["skills_total"] == 1
    assert status["skills"][0]["name"] == "exact-native"
    assert status["excluded_paths"] == ["context/resident/control"]
    assert "legacy-private-hand" not in repr(status)
    assert bindings["active_hands"][0]["task_id"] == task.identifier
    assert bindings["focused_threads"][0]["focus_task_id"] == task.identifier
    tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
    for name in {
        "gsv_execution_bindings",
        "gsv_resident_context_status",
        "gsv_resident_guidance_show",
    }:
        assert tools[name]["annotations"]["readOnlyHint"] is True


def test_fresh_mcp_process_rejects_fields_outside_new_read_surface_schemas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "strict-read-surface-vault"
    host_data = tmp_path / "host-data"
    nonexistent_store = tmp_path / "nonexistent-store"
    monkeypatch.setenv("GSV_DATA_DIR", str(host_data))
    Vault(vault_path).initialize(name="Strict MCP reads")
    process = _start(vault_path)
    cases = (
        (
            "gsv_resident_context_status",
            {"unexpected_private_payload": "synthetic"},
            "unexpected_private_payload",
        ),
        (
            "gsv_resident_guidance_show",
            {"unexpected_private_payload": "synthetic"},
            "unexpected_private_payload",
        ),
        (
            "gsv_execution_bindings",
            {"unexpected_private_payload": "synthetic"},
            "unexpected_private_payload",
        ),
        (
            "gsv_local_source_status",
            {"source": "apple_messages", "store_root": str(nonexistent_store)},
            "store_root",
        ),
        (
            "gsv_local_source_baseline",
            {"source": "apple_messages", "store_root": str(nonexistent_store)},
            "store_root",
        ),
        (
            "gsv_local_source_staged_status",
            {"store_root": str(nonexistent_store)},
            "store_root",
        ),
        (
            "gsv_local_source_adopt_staged",
            {
                "disposition": "adopt_verified_prefix",
                "expected_migration_revision": "a" * 64,
                "expected_source_revision": "b" * 64,
                "source": "apple_messages",
                "store_root": str(nonexistent_store),
            },
            "store_root",
        ),
        (
            "gsv_local_source_poll",
            {"source": "apple_messages", "store_root": str(nonexistent_store)},
            "store_root",
        ),
        (
            "gsv_local_source_rebaseline",
            {
                "disposition": "forward_only_reset",
                "expected_checkpoint_digest": "sha256:" + "a" * 64,
                "expected_sequence": 0,
                "source": "apple_messages",
                "store_root": str(nonexistent_store),
            },
            "store_root",
        ),
        (
            "gsv_local_source_acknowledge",
            {
                "actor_ref": "codex:synthetic",
                "disposition": "accepted",
                "expected_source_revision": "absent",
                "result_refs": ["task:synthetic@revision"],
                "source": "apple_messages",
                "store_root": str(nonexistent_store),
                "token": "synthetic-token",
            },
            "store_root",
        ),
        (
            "gsv_recall_status",
            {"index_root": str(tmp_path / "foreign-index")},
            "index_root",
        ),
        (
            "gsv_recall_search",
            {"index_root": str(tmp_path / "foreign-index"), "query": "synthetic"},
            "index_root",
        ),
    )
    try:
        _exchange(process, "initialize", 1)
        for request_id, (name, arguments, extra) in enumerate(cases, start=2):
            response = _exchange(
                process,
                "tools/call",
                request_id,
                {"name": name, "arguments": arguments},
            )["result"]
            assert response["isError"] is True
            assert response["content"] == [
                {
                    "type": "text",
                    "text": f"{name} arguments has unknown field {extra}",
                }
            ]
        missing = _exchange(
            process,
            "tools/call",
            len(cases) + 2,
            {"name": "gsv_local_source_baseline", "arguments": {}},
        )["result"]
        assert missing["content"] == [
            {
                "type": "text",
                "text": "source must be a non-empty string",
            }
        ]
        wrong_type = _exchange(
            process,
            "tools/call",
            len(cases) + 3,
            {"name": "gsv_recall_status", "arguments": {"timeout_seconds": "slow"}},
        )["result"]
        assert wrong_type["content"] == [
            {"type": "text", "text": "timeout_seconds must be an integer"}
        ]
    finally:
        _close(process)
    assert not host_data.exists()
    assert not nonexistent_store.exists()


def test_entity_relationship_mcp_mutation_survives_a_fresh_process(tmp_path: Path) -> None:
    vault_path = tmp_path / "entity-mcp-vault"
    vault = Vault(vault_path)
    vault.initialize(name="Entity MCP")
    person = vault.create_entity(
        identifier="person:mcp-owner",
        title="MCP owner",
        entity_type="person",
        summary="One exact identity.",
    )
    vault.create_entity(
        identifier="company:mcp-studio",
        title="MCP studio",
        entity_type="company",
        summary="One exact organization.",
    )

    first = _start(vault_path)
    _exchange(first, "initialize", 1)
    linked = _exchange(
        first,
        "tools/call",
        2,
        {
            "name": "gsv_entity_link",
            "arguments": {
                "id": person.identifier,
                "expected_revision": person.revision,
                "predicate": "works-at",
                "target_id": "company:mcp-studio",
                "refs": ["source:mcp-test"],
                "note": "Authored through MCP",
            },
        },
    )
    _close(first)

    second = _start(vault_path)
    _exchange(second, "initialize", 1)
    reread = _exchange(
        second,
        "tools/call",
        2,
        {"name": "gsv_entity_show", "arguments": {"id": person.identifier}},
    )
    _close(second)

    linked_payload = linked["result"]["structuredContent"]
    reread_payload = reread["result"]["structuredContent"]
    assert linked_payload["revision"] == reread_payload["revision"]
    assert reread_payload["relationships"] == [
        {
            "predicate": "works-at",
            "recorded_at": reread_payload["updated_at"],
            "refs": ["source:mcp-test"],
            "status": "current",
            "target": "company:mcp-studio",
            "valid_from": None,
            "valid_to": None,
        }
    ]
    assert any("Authored through MCP" in item for item in reread_payload["history"])


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
        "gsv_discord_source_acknowledge",
        "gsv_discord_source_poll",
        "gsv_discord_source_status",
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
        "gsv_connection_list",
        "gsv_connector_source_read",
        "gsv_execution_bindings",
        "gsv_dispatch_bind",
        "gsv_dispatch_hand_clear",
        "gsv_dispatch_blocker",
        "gsv_dispatch_blocker_clear",
        "gsv_dispatch_claim",
        "gsv_dispatch_deadline_eval",
        "gsv_dispatch_eligible",
        "gsv_direction_set",
        "gsv_direction_show",
        "gsv_discord_source_acknowledge",
        "gsv_discord_source_poll",
        "gsv_discord_source_status",
        "gsv_doctor",
        "gsv_document_show",
        "gsv_document_update",
        "gsv_entity_create",
        "gsv_entity_link",
        "gsv_entity_list",
        "gsv_entity_merge",
        "gsv_entity_resolve",
        "gsv_entity_show",
        "gsv_entity_unlink",
        "gsv_entity_update",
        "gsv_local_file_grant_list",
        "gsv_local_file_read",
        "gsv_local_source_acknowledge",
        "gsv_local_source_adopt_staged",
        "gsv_local_source_baseline",
        "gsv_local_source_poll",
        "gsv_local_source_rebaseline",
        "gsv_local_source_staged_status",
        "gsv_local_source_status",
        "gsv_operation_accept",
        "gsv_operation_archive_closed",
        "gsv_operation_list",
        "gsv_operation_reject",
        "gsv_portfolio_inspect",
        "gsv_portfolio_migrate_review_session",
        "gsv_portfolio_set",
        "gsv_portfolio_show",
        "gsv_pulse_status",
        "gsv_pulse_sweep",
        "gsv_recall_search",
        "gsv_recall_status",
        "gsv_resident_context_status",
        "gsv_resident_guidance_project",
        "gsv_resident_guidance_show",
        "gsv_status",
        "gsv_source_list",
        "gsv_source_record",
        "gsv_source_select",
        "gsv_signal_acknowledge",
        "gsv_signal_append",
        "gsv_signal_compact",
        "gsv_signal_list",
        "gsv_signal_show",
        "gsv_signal_status",
        "gsv_task_create",
        "gsv_task_create_and_place_pointer",
        "gsv_task_list",
        "gsv_task_show",
        "gsv_task_update",
        "gsv_thread_create",
        "gsv_thread_list",
        "gsv_thread_merge",
        "gsv_thread_resolve",
        "gsv_thread_show",
        "gsv_thread_update",
        "gsv_update_status",
    }
    if not CONTROL_STORE_SUPPORTED:
        expected.difference_update(mcp_server.OPERATION_TOOL_NAMES)
    assert names == expected


def test_mcp_connection_list_is_redacted_and_rejects_secret_arguments(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "connection-mcp-vault")
    vault.initialize(name="Connection MCP")
    now = datetime.now(UTC)
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=ConnectionMetadata(
            connection_id=parse_connection_id("con-" + "m" * 32),
            provider="github",
            source_ids=("github",),
            credential_kind=CredentialKind.API_KEY,
            account=AccountMetadata(fingerprint="sha256:" + "a" * 64, label="Synthetic"),
            scopes=(),
            client=ClientMetadata(kind=ClientKind.EXTERNAL, identifier="private-client-id"),
            health=ConnectionHealth.READY,
            created_at=now,
            updated_at=now,
            last_verified_at=now,
            version=1,
        ),
        observed_at=now,
    )

    result = mcp_server._call("gsv_connection_list", {}, vault=vault)
    serialized = json.dumps(result, sort_keys=True)

    assert result["connections"][0]["host_credential"] == "missing"
    assert "private-client-id" not in serialized
    assert "sha256:" not in serialized
    assert "secret://" not in serialized
    with pytest.raises(ValidationError, match="unknown field token"):
        mcp_server._call(
            "gsv_connection_list",
            {"token": "must-not-be-accepted"},
            vault=vault,
        )


def test_mcp_local_source_migration_routes_only_vault_staged_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Staged MCP")
    captured: dict[str, object] = {}

    def staged_status(selected: Vault) -> dict[str, object]:
        assert selected.root == vault.root
        return {"checkpoints": [], "migration_revision": "a" * 64}

    def adopt_staged(selected: Vault, **values: object) -> dict[str, object]:
        assert selected.root == vault.root
        captured.update(values)
        return {"adopted": True, "source": values["source"]}

    monkeypatch.setattr(
        resident_import,
        "staged_local_source_checkpoint_status",
        staged_status,
    )
    monkeypatch.setattr(
        resident_import,
        "adopt_staged_local_source_checkpoint",
        adopt_staged,
    )

    status = mcp_server._call("gsv_local_source_staged_status", {}, vault=vault)
    adopted = mcp_server._call(
        "gsv_local_source_adopt_staged",
        {
            "disposition": "adopt_verified_prefix",
            "expected_migration_revision": "a" * 64,
            "expected_source_revision": "b" * 64,
            "source": "apple_messages",
        },
        vault=vault,
    )

    assert status["migration_revision"] == "a" * 64
    assert adopted == {"adopted": True, "source": "apple_messages"}
    assert captured == {
        "disposition": "adopt_verified_prefix",
        "expected_migration_revision": "a" * 64,
        "expected_source_revision": "b" * 64,
        "source": "apple_messages",
    }
    with pytest.raises(ValidationError, match="unknown field prior_cursor"):
        mcp_server._call(
            "gsv_local_source_adopt_staged",
            {
                "disposition": "adopt_verified_prefix",
                "expected_migration_revision": "a" * 64,
                "expected_source_revision": "b" * 64,
                "prior_cursor": "raw-provider-cursor",
                "source": "apple_messages",
            },
            vault=vault,
        )


def test_fresh_mcp_reports_an_absent_staged_local_source_migration(tmp_path: Path) -> None:
    vault_path = tmp_path / "fresh-staged-mcp"
    vault = Vault(vault_path)
    vault.initialize(name="Fresh staged MCP")
    process = _start(vault_path)
    try:
        _exchange(process, "initialize", 1)
        response = _exchange(
            process,
            "tools/call",
            2,
            {"name": "gsv_local_source_staged_status", "arguments": {}},
        )["result"]
    finally:
        _close(process)

    assert response["isError"] is False
    status = json.loads(response["content"][0]["text"])
    assert status == {
        "checkpoints": [],
        "migration_revision": resident_import.ABSENT_LOCAL_SOURCE_MIGRATION_REVISION,
        "source_revision": vault.get_source_snapshot().revision,
        "vault_id": vault.identity()["vault_id"],
    }
    assert not (vault_path / ".gsv/migrations/local-source-checkpoints.json").exists()


def test_mcp_pulse_sweep_is_provider_free_and_visible_to_a_fresh_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "pulse-mcp-vault"
    vault = Vault(vault_path)
    vault.initialize(name="Pulse MCP")
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    swept = mcp_server._call("gsv_pulse_sweep", {}, vault=vault)
    process = _start(vault_path)
    try:
        _exchange(process, "initialize", 1)
        status = _exchange(
            process,
            "tools/call",
            2,
            {"name": "gsv_pulse_status", "arguments": {}},
        )["result"]["structuredContent"]
        names = {tool["name"] for tool in _exchange(process, "tools/list", 3)["result"]["tools"]}
    finally:
        _close(process)

    assert swept["status"] == "complete"
    assert status["heartbeat"]["observed_at"] == swept["observed_at"]
    assert status["heartbeat"]["sequence"] == 1
    assert not any(name.startswith("gsv_scheduler_") for name in names)
    assert {"gsv_pulse_status", "gsv_pulse_sweep"}.issubset(names)

    tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
    assert tools["gsv_pulse_status"]["annotations"]["readOnlyHint"] is True
    assert tools["gsv_pulse_sweep"]["annotations"]["readOnlyHint"] is False


def test_mcp_signal_append_rejects_arbitrary_provider_envelopes(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "signal-vault")
    vault.initialize(name="Signal privacy")

    with pytest.raises(ValidationError, match="unknown field envelope"):
        mcp_server._call(
            "gsv_signal_append",
            {
                "change_type": "observation",
                "envelope": {"body": "raw provider body", "token": "sk-live-secret"},
                "record_ref": f"task:launch@{'a' * 64}",
            },
            vault=vault,
        )

    tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "gsv_signal_append")
    properties = tool["inputSchema"]["properties"]
    assert set(properties) == {"change_type", "record_ref"}


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
    for name in (
        "gsv_dispatch_bind",
        "gsv_dispatch_hand_clear",
        "gsv_task_create",
        "gsv_task_update",
    ):
        properties = tools[name]["inputSchema"]["properties"]
        active_hand = properties["active_thread_id"]
        description = active_hand["description"]
        assert active_hand["type"] == "string"
        assert "raw Codex thread UUID" in description
        assert "never a Seld WorkThread ID" in description
        assert "pattern" not in active_hand and "format" not in active_hand

    update_properties = tools["gsv_task_update"]["inputSchema"]["properties"]
    assert "clear_active_thread_id" in update_properties
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


def test_mcp_wire_output_is_ascii_safe_and_round_trips_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    mcp_server._write({"summary": "One decision — ready"})

    encoded = stdout.getvalue()
    assert encoded.isascii()
    assert json.loads(encoded) == {"summary": "One decision — ready"}


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
