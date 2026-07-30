from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel import cli, mcp_server
from continuity_kernel.direction import ABSENT_DIRECTION_REVISION, Direction, direction_aim
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.portfolio import ABSENT_PORTFOLIO_REVISION, Portfolio, portfolio_item
from continuity_kernel.records import Task, WorkThread
from continuity_kernel.vault import Vault

BASE = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
FUTURE = "2035-08-01T10:00:00.000000Z"
STALE = "2026-07-01T10:00:00.000000Z"


@dataclass(frozen=True)
class _RichSeed:
    direction: Direction
    portfolio: Portfolio
    task: Task
    thread: WorkThread


def _seed_rich_vault(path: Path) -> _RichSeed:
    vault = Vault(path)
    vault.initialize(name="Rich authoring")
    direction = vault.set_direction(
        expected_revision=ABSENT_DIRECTION_REVISION,
        status="provisional",
        current_chapter="Keep the important work coherent.",
        aims=(
            direction_aim(
                identifier="protect-attention",
                title="Protect attention",
                desired_state="Important work progresses without avoidable interruption.",
            ),
        ),
        constraints=("Do not invent authority.",),
        tensions=("Move quickly without losing context.",),
        refs=("source:owner-context",),
        source_observed_at="2026-07-29T09:55:00.000000Z",
        recorded_at="2026-07-29T10:00:00.000000Z",
        recheck_at=FUTURE,
        note="Direction authored from owner context.",
        observed_at=BASE,
    )
    task = vault.create_task(
        identifier="ship-rich-continuity",
        title="Ship rich continuity",
        outcome="Rich authored context survives every supported mutation surface.",
        status="doing",
        next_actor="agent",
        next_action="Prove the complete round trip.",
        observed_at=BASE + timedelta(minutes=1),
    )
    thread = vault.create_thread(
        identifier="thread:rich-continuity",
        title="Rich continuity",
        purpose="Carry the public continuity outcome.",
        summary="The no-downgrade seam is under test.",
        task_ids=(task.identifier,),
        observed_at=BASE + timedelta(minutes=2),
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact outcome is active.",
        direction_revision=direction.revision,
        source_direction_updated_at=direction.updated_at,
        items=(
            portfolio_item(
                task_id_value=task.identifier,
                task_revision=task.revision,
                stance="agent-can-carry",
                reason="The next step is local and reversible.",
                work_thread_id=thread.identifier,
                work_thread_revision=thread.revision,
                direction_aim_ids=("protect-attention",),
            ),
        ),
        refs=("source:resident-portfolio",),
        source_observed_at="2026-07-29T10:02:00.000000Z",
        recorded_at="2026-07-29T10:03:00.000000Z",
        review_after=FUTURE,
        note="Portfolio authored from the complete open set.",
        observed_at=BASE + timedelta(minutes=3),
    )
    return _RichSeed(
        direction=direction,
        portfolio=portfolio,
        task=task,
        thread=thread,
    )


def _cli_process(vault: Path, *arguments: str) -> dict[str, Any]:
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


def _start_mcp(vault: Path, *, guided: bool) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["GSV_VAULT"] = str(vault)
    command = [sys.executable, "-m", "continuity_kernel", "mcp", "serve"]
    if guided:
        command.extend(
            (
                "--profile",
                mcp_server.GUIDED_REVIEW_PROFILE,
                "--event-id",
                "11111111-1111-4111-8111-111111111111",
            )
        )
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )


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
    assert line, process.stderr.read() if process.stderr is not None else "MCP exited"
    return cast(dict[str, Any], json.loads(line))


def _close_mcp(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=10) == 0


def test_vault_carries_rich_fields_and_rejects_noncanonical_source_anchors(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    seeded = _seed_rich_vault(vault_path)
    vault = Vault(vault_path)
    before_bytes = (vault_path / "PORTFOLIO.md").read_bytes()

    mismatches = (
        {"source_task_updated_at": STALE},
        {
            "source_task_updated_at": seeded.task.updated_at,
            "source_thread_updated_at": STALE,
        },
    )
    for mismatch in mismatches:
        with pytest.raises(ConflictError, match="timestamp anchor changed"):
            vault.set_portfolio(
                expected_revision=seeded.portfolio.revision,
                summary="Reject a caller-trusted timestamp.",
                direction_revision=seeded.direction.revision,
                items=(
                    portfolio_item(
                        task_id_value=seeded.task.identifier,
                        task_revision=seeded.task.revision,
                        stance="agent-can-carry",
                        reason="The source anchor must match canonical truth.",
                        work_thread_id=seeded.thread.identifier,
                        work_thread_revision=seeded.thread.revision,
                        direction_aim_ids=("protect-attention",),
                        **mismatch,
                    ),
                ),
                observed_at=BASE + timedelta(minutes=4),
            )
        assert (vault_path / "PORTFOLIO.md").read_bytes() == before_bytes

    with pytest.raises(ConflictError, match="Direction timestamp anchor changed"):
        vault.set_portfolio(
            expected_revision=seeded.portfolio.revision,
            summary="Reject a stale Direction timestamp.",
            direction_revision=seeded.direction.revision,
            source_direction_updated_at=STALE,
            items=seeded.portfolio.items,
            observed_at=BASE + timedelta(minutes=4),
        )
    assert (vault_path / "PORTFOLIO.md").read_bytes() == before_bytes

    direction = vault.set_direction(
        expected_revision=seeded.direction.revision,
        status="confirmed",
        current_chapter="Keep the same context while moving the chapter forward.",
        aims=seeded.direction.aims,
        observed_at=BASE + timedelta(minutes=5),
    )
    assert direction.format_version == 2
    assert direction.constraints == seeded.direction.constraints
    assert direction.tensions == seeded.direction.tensions
    assert direction.refs == seeded.direction.refs
    assert direction.history == seeded.direction.history

    portfolio = vault.set_portfolio(
        expected_revision=seeded.portfolio.revision,
        summary="The guided review changed only its authored judgment.",
        direction_revision=direction.revision,
        items=(
            portfolio_item(
                task_id_value=seeded.task.identifier,
                task_revision=seeded.task.revision,
                stance="needs-human",
                reason="The next choice now needs the owner.",
                work_thread_id=seeded.thread.identifier,
                work_thread_revision=seeded.thread.revision,
                direction_aim_ids=("protect-attention",),
            ),
        ),
        observed_at=BASE + timedelta(minutes=6),
    )
    restarted = Vault(vault_path).get_portfolio()
    assert restarted == portfolio
    assert restarted.format_version == 3
    assert restarted.refs == seeded.portfolio.refs
    assert restarted.history == seeded.portfolio.history
    assert restarted.review_after == seeded.portfolio.review_after
    assert restarted.source_direction_updated_at == direction.updated_at
    assert restarted.items[0].source_task_updated_at == seeded.task.updated_at
    assert restarted.items[0].source_thread_updated_at == seeded.thread.updated_at


def test_carried_due_horizons_expire_fail_closed_without_history_mutation(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    seeded = _seed_rich_vault(vault_path)
    vault = Vault(vault_path)
    direction_bytes = (vault_path / "DIRECTION.md").read_bytes()
    portfolio_bytes = (vault_path / "PORTFOLIO.md").read_bytes()
    after_due_horizon = datetime(2036, 1, 1, tzinfo=UTC)

    with pytest.raises(ValidationError, match="recheck_at must be later"):
        vault.set_direction(
            expected_revision=seeded.direction.revision,
            status="confirmed",
            current_chapter="A stale horizon requires new authored judgment.",
            aims=seeded.direction.aims,
            note="This note must not land when the horizon is stale.",
            observed_at=after_due_horizon,
        )
    assert (vault_path / "DIRECTION.md").read_bytes() == direction_bytes
    assert Vault(vault_path).get_direction().history == seeded.direction.history

    with pytest.raises(ValidationError, match="review_after must be later"):
        vault.set_portfolio(
            expected_revision=seeded.portfolio.revision,
            summary="A stale review horizon requires new authored judgment.",
            direction_revision=seeded.direction.revision,
            items=seeded.portfolio.items,
            note="This note must not land when the horizon is stale.",
            observed_at=after_due_horizon,
        )
    assert (vault_path / "PORTFOLIO.md").read_bytes() == portfolio_bytes
    assert Vault(vault_path).get_portfolio().history == seeded.portfolio.history


def test_cli_strictly_replaces_rich_fields_and_fresh_process_preserves_versions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "vault"
    seeded = _seed_rich_vault(vault_path)
    aim_json = json.dumps(
        {
            "id": "protect-attention",
            "title": "Protect attention",
            "desired_state": "Important work progresses without avoidable interruption.",
        }
    )
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "direction",
                "set",
                "--expected-revision",
                seeded.direction.revision,
                "--status",
                "confirmed",
                "--current-chapter",
                "Use the complete context without carrying stale framing.",
                "--aim-json",
                aim_json,
                "--constraints-json",
                "[]",
                "--tensions-json",
                json.dumps(["Stay ambitious and selective."]),
                "--refs-json",
                json.dumps(["source:explicit-cli-replacement"]),
                "--source-observed-at",
                "2026-07-29T10:04:00.000000Z",
                "--recorded-at",
                seeded.direction.recorded_at or "",
                "--recheck-at",
                FUTURE,
                "--note",
                "Direction updated through the CLI.",
            ]
        )
        == 0
    )
    direction = json.loads(capsys.readouterr().out)["result"]
    assert direction["format_version"] == 2
    assert direction["constraints"] == []
    assert direction["history"] == [
        "Direction authored from owner context.",
        "Direction updated through the CLI.",
    ]

    bad_aim = json.dumps({**json.loads(aim_json), "score": 99})
    direction_bytes = (vault_path / "DIRECTION.md").read_bytes()
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "direction",
                "set",
                "--expected-revision",
                direction["revision"],
                "--status",
                "confirmed",
                "--current-chapter",
                "Reject invented schema.",
                "--aim-json",
                bad_aim,
            ]
        )
        == 2
    )
    assert "unknown field score" in json.loads(capsys.readouterr().err)["error"]
    assert (vault_path / "DIRECTION.md").read_bytes() == direction_bytes

    item = {
        "task_id": seeded.task.identifier,
        "task_revision": seeded.task.revision,
        "stance": "keep-in-view",
        "reason": "The context is current and the next move remains sound.",
        "work_thread_id": seeded.thread.identifier,
        "work_thread_revision": seeded.thread.revision,
        "direction_aim_ids": ["protect-attention"],
        "source_position": 10,
        "source_task_updated_at": seeded.task.updated_at,
        "source_thread_updated_at": seeded.thread.updated_at,
    }
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "portfolio",
                "set",
                "--expected-revision",
                seeded.portfolio.revision,
                "--summary",
                "The CLI replaced every authored v3 field.",
                "--direction-revision",
                direction["revision"],
                "--source-direction-updated-at",
                direction["updated_at"],
                "--item-json",
                json.dumps(item),
                "--refs-json",
                "[]",
                "--source-observed-at",
                "2026-07-29T10:05:00.000000Z",
                "--recorded-at",
                seeded.portfolio.recorded_at or "",
                "--review-after",
                FUTURE,
                "--note",
                "Portfolio updated through the CLI.",
            ]
        )
        == 0
    )
    portfolio = json.loads(capsys.readouterr().out)["result"]
    assert portfolio["format_version"] == 3
    assert portfolio["refs"] == []
    assert portfolio["history"] == [
        "Portfolio authored from the complete open set.",
        "Portfolio updated through the CLI.",
    ]

    bad_item = json.dumps({**item, "invented_score": 99})
    portfolio_bytes = (vault_path / "PORTFOLIO.md").read_bytes()
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "portfolio",
                "set",
                "--expected-revision",
                portfolio["revision"],
                "--summary",
                "Reject invented schema.",
                "--direction-revision",
                direction["revision"],
                "--item-json",
                bad_item,
            ]
        )
        == 2
    )
    assert "unknown field invented_score" in json.loads(capsys.readouterr().err)["error"]
    assert (vault_path / "PORTFOLIO.md").read_bytes() == portfolio_bytes

    assert _cli_process(vault_path, "direction", "show")["format_version"] == 2
    restarted = _cli_process(vault_path, "portfolio", "show")
    assert restarted == portfolio
    assert restarted["items"][0]["source_task_updated_at"] == seeded.task.updated_at
    assert restarted["items"][0]["source_thread_updated_at"] == seeded.thread.updated_at


def test_guided_review_mcp_replaces_then_carries_v3_across_processes(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    seeded = _seed_rich_vault(vault_path)
    process = _start_mcp(vault_path, guided=True)
    try:
        _exchange(process, "initialize", 1)
        tools = _exchange(process, "tools/list", 2)["result"]["tools"]
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        item_schema = schemas["gsv_portfolio_set"]["properties"]["items"]["items"]
        assert "history" not in schemas["gsv_portfolio_set"]["properties"]
        assert {
            "source_position",
            "source_task_updated_at",
            "source_thread_updated_at",
        } <= set(item_schema["properties"])

        authored = _exchange(
            process,
            "tools/call",
            3,
            {
                "name": "gsv_portfolio_set",
                "arguments": {
                    "expected_revision": seeded.portfolio.revision,
                    "summary": "The guided review explicitly replaced its v3 judgment.",
                    "direction_revision": seeded.direction.revision,
                    "source_direction_updated_at": seeded.direction.updated_at,
                    "items": [
                        {
                            "task_id": seeded.task.identifier,
                            "task_revision": seeded.task.revision,
                            "stance": "needs-human",
                            "reason": "The owner now has a consequential choice.",
                            "work_thread_id": seeded.thread.identifier,
                            "work_thread_revision": seeded.thread.revision,
                            "direction_aim_ids": ["protect-attention"],
                            "source_position": 20,
                            "source_task_updated_at": seeded.task.updated_at,
                            "source_thread_updated_at": seeded.thread.updated_at,
                        }
                    ],
                    "refs": ["source:mcp-guided-review"],
                    "source_observed_at": "2026-07-29T10:04:00.000000Z",
                    "recorded_at": seeded.portfolio.recorded_at,
                    "review_after": FUTURE,
                    "note": "Portfolio updated through guided-review MCP.",
                },
            },
        )["result"]["structuredContent"]
        assert authored["format_version"] == 3
        assert authored["refs"] == ["source:mcp-guided-review"]

        carried = _exchange(
            process,
            "tools/call",
            4,
            {
                "name": "gsv_portfolio_set",
                "arguments": {
                    "expected_revision": authored["revision"],
                    "summary": "The next guided turn changed only authored judgment.",
                    "direction_revision": seeded.direction.revision,
                    "items": [
                        {
                            "task_id": seeded.task.identifier,
                            "task_revision": seeded.task.revision,
                            "stance": "agent-can-carry",
                            "reason": "The selected local step can proceed without interruption.",
                            "work_thread_id": seeded.thread.identifier,
                            "work_thread_revision": seeded.thread.revision,
                            "direction_aim_ids": ["protect-attention"],
                        }
                    ],
                },
            },
        )["result"]["structuredContent"]
    finally:
        _close_mcp(process)

    restarted_process = _start_mcp(vault_path, guided=False)
    try:
        _exchange(restarted_process, "initialize", 1)
        restarted = _exchange(
            restarted_process,
            "tools/call",
            2,
            {"name": "gsv_portfolio_show", "arguments": {}},
        )["result"]["structuredContent"]
    finally:
        _close_mcp(restarted_process)

    assert restarted == carried
    assert restarted["format_version"] == 3
    assert restarted["refs"] == ["source:mcp-guided-review"]
    assert restarted["history"] == [
        "Portfolio authored from the complete open set.",
        "Portfolio updated through guided-review MCP.",
    ]
    assert restarted["items"][0]["source_task_updated_at"] == seeded.task.updated_at
    assert restarted["items"][0]["source_thread_updated_at"] == seeded.thread.updated_at


def test_mcp_direction_schema_exposes_every_v2_field() -> None:
    tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
    properties = tools["gsv_direction_set"]["inputSchema"]["properties"]
    assert "history" not in properties
    assert {
        "constraints",
        "note",
        "recorded_at",
        "recheck_at",
        "refs",
        "source_observed_at",
        "tensions",
    } <= set(properties)


def test_mcp_direction_appends_note_then_carries_v2_across_fresh_processes(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    seeded = _seed_rich_vault(vault_path)
    aim = {
        "id": "protect-attention",
        "title": "Protect attention",
        "desired_state": "Important work progresses without avoidable interruption.",
    }
    first = _start_mcp(vault_path, guided=False)
    try:
        _exchange(first, "initialize", 1)
        replaced = _exchange(
            first,
            "tools/call",
            2,
            {
                "name": "gsv_direction_set",
                "arguments": {
                    "expected_revision": seeded.direction.revision,
                    "status": "confirmed",
                    "current_chapter": "Use current context and keep the next horizon explicit.",
                    "aims": [aim],
                    "constraints": [],
                    "tensions": ["Stay ambitious and selective."],
                    "refs": ["source:mcp-direction"],
                    "source_observed_at": "2026-07-29T10:04:00.000000Z",
                    "recorded_at": seeded.direction.recorded_at,
                    "recheck_at": FUTURE,
                    "note": "Direction updated through MCP.",
                },
            },
        )["result"]["structuredContent"]
    finally:
        _close_mcp(first)

    second = _start_mcp(vault_path, guided=False)
    try:
        _exchange(second, "initialize", 1)
        shown = _exchange(
            second,
            "tools/call",
            2,
            {"name": "gsv_direction_show", "arguments": {}},
        )["result"]["structuredContent"]
        assert shown == replaced
        carried = _exchange(
            second,
            "tools/call",
            3,
            {
                "name": "gsv_direction_set",
                "arguments": {
                    "expected_revision": shown["revision"],
                    "status": "confirmed",
                    "current_chapter": "Change only the chapter and preserve resident context.",
                    "aims": [aim],
                },
            },
        )["result"]["structuredContent"]
    finally:
        _close_mcp(second)

    final = _start_mcp(vault_path, guided=False)
    try:
        _exchange(final, "initialize", 1)
        restarted = _exchange(
            final,
            "tools/call",
            2,
            {"name": "gsv_direction_show", "arguments": {}},
        )["result"]["structuredContent"]
    finally:
        _close_mcp(final)

    assert restarted == carried
    assert restarted["format_version"] == 2
    assert restarted["constraints"] == []
    assert restarted["refs"] == ["source:mcp-direction"]
    assert restarted["history"] == [
        "Direction authored from owner context.",
        "Direction updated through MCP.",
    ]
