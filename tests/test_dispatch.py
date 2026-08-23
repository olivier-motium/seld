from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import cli, mcp_server, task_pointer
from continuity_kernel.dispatch import (
    bind_task_hand,
    claim_task,
    clear_task_blocker,
    dispatch_eligible,
    evaluate_task_deadline,
    write_task_blocker,
)
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.vault import Vault

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_dispatch_lifecycle_preserves_rank_and_clears_blocker_idempotently(
    vault: Vault,
) -> None:
    ignored = vault.create_task(
        identifier="untyped-task",
        title="Untyped task",
        outcome="Remain outside the typed dispatch queue.",
        status="ready",
        next_actor="agent",
        rank=0,
        observed_at=NOW,
    )
    task = vault.create_task(
        identifier="typed-task",
        title="Typed task",
        outcome="Move through one dispatch lifecycle.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        target_seat="worker-one",
        rank=4,
        observed_at=NOW,
    )

    assert [item.identifier for item in dispatch_eligible(vault.root)] == [task.identifier]
    claimed = claim_task(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="dispatch-one",
        observed_at=NOW,
    )
    assert claimed.dispatch_id == "dispatch-one"
    assert claimed.dispatch_revision == task.revision

    rebound = bind_task_hand(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="dispatch-one",
        active_thread_id="hand-one",
        observed_at=NOW,
    )
    assert rebound.status == "doing"
    assert rebound.active_thread_id == "hand-one"
    assert rebound.rank == task.rank

    blocked = write_task_blocker(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="dispatch-one",
        owner="provider",
        condition="Capacity is unavailable.",
        observed_at=NOW,
    )
    assert blocked.status == "waiting"
    assert blocked.waiting_on == "Capacity is unavailable."
    assert blocked.blocker_owner == "provider"
    assert blocked.blocker_condition == "Capacity is unavailable."
    assert blocked.active_thread_id is None
    assert blocked.rank == task.rank
    assert [item.identifier for item in dispatch_eligible(vault.root)] == []

    cleared = clear_task_blocker(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="dispatch-one",
        owner="provider",
        condition="Capacity is unavailable.",
        observed_at=NOW,
    )
    assert cleared.status == "ready"
    assert cleared.waiting_on is None
    assert cleared.blocker_owner is None
    assert cleared.blocker_condition is None
    assert cleared.dispatch_id is None
    assert cleared.dispatch_revision is None
    assert cleared.active_thread_id is None
    assert cleared.rank == task.rank
    assert cleared.claim_by is not None
    assert [item.identifier for item in dispatch_eligible(vault.root)] == [task.identifier]

    replay = clear_task_blocker(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="dispatch-one",
        owner="provider",
        condition="Capacity is unavailable.",
        observed_at=NOW,
    )
    assert replay == cleared
    assert ignored.identifier not in [item.identifier for item in dispatch_eligible(vault.root)]


def test_dispatch_rejects_stale_claim_and_projects_unknown_deadlines_without_mutation(
    vault: Vault,
) -> None:
    task = vault.create_task(
        identifier="deadline-task",
        title="Deadline task",
        outcome="Project dispatch attention without changing task truth.",
        status="ready",
        next_actor="agent",
        target_seat="worker-one",
        progress_check_by="2026-07-22T11:00:00.000000Z",
        observed_at=NOW,
    )
    deadline_now = datetime(2026, 7, 22, 12, 10, tzinfo=UTC)

    findings = evaluate_task_deadline(
        vault.root,
        task.identifier,
        now=deadline_now,
        clock_health="healthy",
    )
    assert [(finding.field, finding.band) for finding in findings] == [
        ("claim_by", "OVERDUE"),
        ("progress_check_by", "OVERDUE"),
    ]
    unknown = evaluate_task_deadline(
        vault.root,
        task.identifier,
        now=deadline_now,
        clock_health="unknown",
    )
    assert [finding.band for finding in unknown] == ["UNKNOWN", "UNKNOWN"]
    assert Vault(vault.root).get_task(task.identifier).revision == task.revision

    with pytest.raises(ConflictError, match="dispatch revision"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision="0" * 64,
            dispatch_id="dispatch-stale",
            observed_at=NOW,
        )

    waiting = vault.update_task(
        task.identifier,
        expected_revision=task.revision,
        status="waiting",
        next_actor="agent",
        waiting_on="Human decision.",
        observed_at=NOW,
    )
    assert evaluate_task_deadline(vault.root, waiting.identifier, now=NOW) == ()


def test_cli_and_mcp_expose_typed_dispatch_operations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "dispatch-surface")
    vault.initialize(name="Dispatch surface")
    task = vault.create_task(
        identifier="surface-task",
        title="Surface task",
        outcome="Expose typed dispatch through supported surfaces.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        target_seat="worker-one",
        observed_at=NOW,
    )

    assert cli.main(["--json", "--vault", str(vault.root), "task-" + "dispatch-eligible"]) == 0
    cli_result = json.loads(capsys.readouterr().out)["result"]
    assert [item["identifier"] for item in cli_result] == [task.identifier]

    mcp_tools = {tool["name"] for tool in mcp_server.TOOLS}
    assert {
        "gsv_dispatch_eligible",
        "gsv_dispatch_claim",
        "gsv_dispatch_bind",
        "gsv_dispatch_blocker",
        "gsv_dispatch_blocker_clear",
        "gsv_dispatch_deadline_eval",
    } <= mcp_tools
    mcp_result = mcp_server._call("gsv_dispatch_eligible", {}, vault=vault)
    assert [item["identifier"] for item in mcp_result["tasks"]] == [task.identifier]

    claimed = mcp_server._call(
        "gsv_dispatch_claim",
        {
            "id": task.identifier,
            "expected_revision": task.revision,
            "dispatch_id": "surface-dispatch",
            "observed_at": "2026-07-22T12:00:00.000000Z",
        },
        vault=vault,
    )
    assert claimed["dispatch_id"] == "surface-dispatch"

    def run_cli(*arguments: str) -> Any:
        assert cli.main(["--json", "--vault", str(vault.root), *arguments]) == 0
        return json.loads(capsys.readouterr().out)["result"]

    bound = run_cli(
        "dispatch-bind",
        task.identifier,
        "--expected-revision",
        task.revision,
        "--dispatch-id",
        "surface-dispatch",
        "--active-thread-id",
        "surface-hand",
    )
    assert bound["status"] == "doing"
    blocked = run_cli(
        "dispatch-blocker",
        task.identifier,
        "--expected-revision",
        task.revision,
        "--dispatch-id",
        "surface-dispatch",
        "--owner",
        "capacity",
        "--condition",
        "Capacity is unavailable.",
    )
    assert blocked["status"] == "waiting"
    assert (
        run_cli(
            "dispatch-deadline-eval",
            task.identifier,
            "--now",
            "2026-07-22T12:10:00.000000Z",
        )
        == []
    )
    cleared = run_cli(
        "task-" + "dispatch-blocker-clear",
        task.identifier,
        "--expected-revision",
        task.revision,
        "--dispatch-id",
        "surface-dispatch",
        "--owner",
        "capacity",
        "--condition",
        "Capacity is unavailable.",
    )
    assert cleared["status"] == "ready"
    assert cleared["dispatch_id"] is None


def test_dispatch_rejects_invalid_clock_health(vault: Vault) -> None:
    task = vault.create_task(
        identifier="clock-task",
        title="Clock task",
        outcome="Reject an unsupported clock state.",
        status="ready",
        next_actor="agent",
        target_seat="worker-one",
    )
    with pytest.raises(ValidationError, match="clock health"):
        evaluate_task_deadline(vault.root, task.identifier, clock_health="stale")


def test_pointer_operation_requires_explicit_sender_and_is_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_inbox = Path.home() / ".workbench/mail/worker-sbrain-typed-target-1/inbox"
    before_live = tuple(sorted(path.name for path in live_inbox.glob("*.md")))
    mail_root = tmp_path / "mail"
    monkeypatch.setenv("WB_MAIL_ROOT", str(mail_root))

    with pytest.raises(ValidationError, match="authoring seat is required"):
        task_pointer.create_or_update_task_and_place_pointer_mail(
            tmp_path / "vault",
            identifier="missing-sender",
            title="Missing sender",
            outcome="Refuse before pointer placement.",
            target_seat="worker-one",
            authoring_seat=None,
            status="ready",
            next_actor="agent",
            observed_at=NOW,
        )
    assert not mail_root.exists()
    pointer_vault = Vault(tmp_path / "vault")
    pointer_vault.initialize(name="Pointer test")

    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        event_key = command[command.index("--event-key") + 1]
        sender = command[command.index("--from") + 1]
        target = command[command.index("--to") + 1]
        inbox = mail_root / target / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / "task-pointer.md"
        path.write_text(
            f'---\nfrom: {sender}\nto: {target}\nevent-key: "{event_key}"\n---\n\nPointer.\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout=f"{path}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    first = task_pointer.create_or_update_task_and_place_pointer_mail(
        tmp_path / "vault",
        identifier="explicit-pointer",
        title="Explicit pointer",
        outcome="Deliver one exact pointer.",
        target_seat="worker-one",
        authoring_seat="author-seat",
        status="ready",
        next_actor="agent",
        next_action="Read the task.",
        observed_at=NOW,
    )
    replay = task_pointer.create_or_update_task_and_place_pointer_mail(
        tmp_path / "vault",
        identifier="explicit-pointer",
        title="Explicit pointer",
        outcome="Deliver one exact pointer.",
        target_seat="worker-one",
        authoring_seat="author-seat",
        expected_revision=first.task.revision,
        status="ready",
        next_actor="agent",
        next_action="Read the task.",
        observed_at=NOW,
    )

    assert first.created is True
    assert replay.created is False
    assert replay.task == first.task
    assert replay.event_key == first.event_key
    assert len(calls) == 2
    assert calls[0][calls[0].index("--from") + 1] == "author-seat"
    assert first.event_key is not None
    assert first.event_key.startswith("task:explicit-pointer:rev-")
    assert len(tuple((mail_root / "worker-one/inbox").glob("*.md"))) == 1
    assert tuple(sorted(path.name for path in live_inbox.glob("*.md"))) == before_live


def test_dispatch_eligibility_enforces_explicit_agent_run_decision(
    vault: Vault,
) -> None:
    eligible = vault.create_task(
        identifier="eligible-agent-task",
        title="Eligible agent task",
        outcome="Satisfies all deterministic dispatch preconditions.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        target_seat="worker-one",
        rank=1,
        observed_at=NOW,
    )
    no_agent_run = vault.create_task(
        identifier="refused-agent-no",
        title="Refused agent no",
        outcome="Explicitly marked as agent_run=no.",
        status="ready",
        next_actor="agent",
        agent_run="no",
        target_seat="worker-one",
        rank=2,
        observed_at=NOW,
    )
    unset_agent_run = vault.create_task(
        identifier="refused-agent-unset",
        title="Refused agent unset",
        outcome="Lacks an explicit agent_run decision.",
        status="ready",
        next_actor="agent",
        target_seat="worker-one",
        rank=3,
        observed_at=NOW,
    )
    human_next_actor = vault.create_task(
        identifier="refused-human-actor",
        title="Refused human actor",
        outcome="Has agent_run=yes but next_actor is human.",
        status="ready",
        next_actor="human",
        agent_run="yes",
        target_seat="worker-one",
        rank=4,
        observed_at=NOW,
    )
    external_next_actor = vault.create_task(
        identifier="refused-external-actor",
        title="Refused external actor",
        outcome="Has agent_run=yes but next_actor is external.",
        status="ready",
        next_actor="external",
        agent_run="yes",
        target_seat="worker-one",
        rank=5,
        observed_at=NOW,
    )
    inferred_words = vault.create_task(
        identifier="refused-inferred-words",
        title="run agent in autonomous worker loop",
        outcome="Agent must run automatically without human intervention.",
        status="ready",
        next_actor="agent",
        next_action="execute autonomous run",
        project="agent-runtime",
        target_seat="agent-runner-seat",
        agent_run="no",
        rank=6,
        observed_at=NOW,
    )
    no_seat_task = vault.create_task(
        identifier="eligible-no-seat",
        title="Eligible without target seat",
        outcome="Target seat has no eligibility meaning.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        target_seat=None,
        rank=7,
        observed_at=NOW,
    )

    # 1. Explicit yes + next_actor=agent tasks are returned regardless of target_seat
    assert [item.identifier for item in dispatch_eligible(vault.root)] == [
        eligible.identifier,
        no_seat_task.identifier,
    ]

    # 2. Refused tasks cannot be claimed
    for refused in (no_agent_run, unset_agent_run, human_next_actor, external_next_actor, inferred_words):
        with pytest.raises(ValidationError, match="not ready and eligible for claim"):
            claim_task(
                vault.root,
                refused.identifier,
                expected_revision=refused.revision,
                dispatch_id="dispatch-test",
                observed_at=NOW,
            )

    # 3. Explicitly updating agent_run to "yes" grants eligibility
    promoted = vault.update_task(
        unset_agent_run.identifier,
        expected_revision=unset_agent_run.revision,
        agent_run="yes",
        observed_at=NOW,
    )
    assert promoted.agent_run == "yes"
    assert [item.identifier for item in dispatch_eligible(vault.root)] == [
        eligible.identifier,
        promoted.identifier,
        no_seat_task.identifier,
    ]

    # 4. Explicitly demoting or clearing agent_run revokes eligibility
    demoted = vault.update_task(
        eligible.identifier,
        expected_revision=eligible.revision,
        agent_run="no",
        observed_at=NOW,
    )
    assert demoted.agent_run == "no"
    assert [item.identifier for item in dispatch_eligible(vault.root)] == [
        promoted.identifier,
        no_seat_task.identifier,
    ]

    cleared = vault.update_task(
        promoted.identifier,
        expected_revision=promoted.revision,
        clear_agent_run=True,
        observed_at=NOW,
    )
    assert cleared.agent_run is None
    assert [item.identifier for item in dispatch_eligible(vault.root)] == [no_seat_task.identifier]


