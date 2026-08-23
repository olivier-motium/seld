from __future__ import annotations

import hashlib
import hmac
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
ADMISSION_KEY: bytes = b"test-admission-secret-key-12345"


def create_factory_admission_token(
    key_bytes: bytes,
    project_root: Path,
    task_id: str,
    expected_revision: str,
    dispatch_id: str,
    allocation_id: str = "alloc-1",
) -> dict[str, Any]:
    vault_sha256 = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest().lower()
    payload = {
        "allocation_id": allocation_id,
        "dispatch_id": dispatch_id,
        "expected_revision": expected_revision,
        "issuer": "ai-accounts-runtime",
        "schema": "seld.factory-allocation-admission.v1",
        "task_id": task_id,
        "vault_sha256": vault_sha256,
    }
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key_bytes, canonical_payload, hashlib.sha256).hexdigest()
    return {
        "payload": payload,
        "signature": signature,
    }


@pytest.fixture(autouse=True)
def setup_admission_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key_file = tmp_path / "factory_admission.key"
    key_file.write_bytes(ADMISSION_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(key_file))
    return key_file


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
    admission = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "dispatch-one"
    )
    claimed = claim_task(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="dispatch-one",
        admission=admission,
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
    assert cleared.claim_by is None
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

    admission = create_factory_admission_token(
        ADMISSION_KEY,
        vault.root,
        task.identifier,
        task.revision,
        "surface-dispatch",
    )
    claimed = mcp_server._call(
        "gsv_dispatch_claim",
        {
            "id": task.identifier,
            "expected_revision": task.revision,
            "dispatch_id": "surface-dispatch",
            "admission": admission,
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
        admission = create_factory_admission_token(
            ADMISSION_KEY, vault.root, refused.identifier, refused.revision, "dispatch-test"
        )
        with pytest.raises(ValidationError, match="not ready and eligible for claim"):
            claim_task(
                vault.root,
                refused.identifier,
                expected_revision=refused.revision,
                dispatch_id="dispatch-test",
                admission=admission,
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


def test_agent_run_claim_by_deadline_suppression_and_transitions(
    vault: Vault,
) -> None:
    # 1. Tasks created with agent_run="yes" never auto-mint claim_by
    agent_task = vault.create_task(
        identifier="agent-auto-suppress",
        title="Agent auto suppress",
        outcome="Do not auto-mint claim_by.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        target_seat="worker-one",
        observed_at=NOW,
    )
    assert agent_task.claim_by is None

    # 2. Legacy / tasks without agent_run="yes" still auto-mint claim_by on create
    legacy_task = vault.create_task(
        identifier="legacy-auto-mint",
        title="Legacy auto mint",
        outcome="Auto-mint claim_by for non-agent_run=yes tasks.",
        status="ready",
        next_actor="agent",
        target_seat="worker-one",
        observed_at=NOW,
    )
    assert legacy_task.claim_by == "2026-07-22T12:05:00.000000Z"

    # 3. Transitioning an existing task with auto claim_by to agent_run="yes" preserves claim_by
    promoted = vault.update_task(
        legacy_task.identifier,
        expected_revision=legacy_task.revision,
        agent_run="yes",
        observed_at=NOW,
    )
    assert promoted.agent_run == "yes"
    assert promoted.claim_by == "2026-07-22T12:05:00.000000Z"

    # Only an explicit clear removes claim_by
    cleared_claim = vault.update_task(
        promoted.identifier,
        expected_revision=promoted.revision,
        clear_claim_by=True,
        observed_at=NOW,
    )
    assert cleared_claim.claim_by is None

    # 4. Transitioning to agent_run="yes" with an explicit claim_by preserves the authored deadline
    authored_deadline = "2026-07-22T14:00:00.000000Z"
    authored_task = vault.create_task(
        identifier="authored-deadline-task",
        title="Authored deadline task",
        outcome="Preserve explicitly supplied deadline.",
        status="ready",
        next_actor="agent",
        target_seat="worker-one",
        observed_at=NOW,
    )
    updated_authored = vault.update_task(
        authored_task.identifier,
        expected_revision=authored_task.revision,
        agent_run="yes",
        claim_by=authored_deadline,
        observed_at=NOW,
    )
    assert updated_authored.agent_run == "yes"
    assert updated_authored.claim_by == authored_deadline

    # 5. Updating an agent_run="yes" task to ready does NOT auto-mint claim_by
    captured = vault.create_task(
        identifier="captured-agent-task",
        title="Captured agent task",
        outcome="Remain without claim_by when moving to ready.",
        status="captured",
        next_actor="agent",
        agent_run="yes",
        target_seat="worker-one",
        observed_at=NOW,
    )
    assert captured.claim_by is None
    made_ready = vault.update_task(
        captured.identifier,
        expected_revision=captured.revision,
        status="ready",
        observed_at=NOW,
    )
    assert made_ready.status == "ready"
    assert made_ready.claim_by is None


def test_factory_allocation_admission_and_mutation_invariants(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Matching admission succeeds exactly once, and idempotent replay is safe
    task = vault.create_task(
        identifier="admission-task",
        title="Admission task",
        outcome="Test admission validation.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        target_seat=None,  # target seat has no effect on eligibility
        claim_by="2026-07-22T13:00:00.000000Z",
        observed_at=NOW,
    )
    initial_revision = task.revision

    other_vault = Vault(tmp_path / "other-vault")
    other_vault.initialize(name="Other vault")

    # Missing admission
    with pytest.raises(ValidationError, match="factory allocation admission"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=None,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Malformed admission
    with pytest.raises(ValidationError, match="exact payload and signature"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission={"payload": "bad"},
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Extra field in admission
    valid_token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "disp-1"
    )
    extra_top = dict(valid_token)
    extra_top["extra_key"] = "extra"
    with pytest.raises(ValidationError, match="exact payload and signature"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=extra_top,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Extra field in payload
    extra_payload = dict(valid_token["payload"])
    extra_payload["extra_field"] = "extra"
    extra_payload_sig = hmac.new(
        ADMISSION_KEY,
        json.dumps(extra_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(ValidationError, match="unsupported shape"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission={"payload": extra_payload, "signature": extra_payload_sig},
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Uppercase hex signature rejected without case-folding
    sig_upper = dict(valid_token)
    sig_upper["signature"] = valid_token["signature"].upper()
    with pytest.raises(ValidationError, match="64 lowercase hex characters"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=sig_upper,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Whitespace around signature rejected without stripping
    sig_space = dict(valid_token)
    sig_space["signature"] = f" {valid_token['signature']} "
    with pytest.raises(ValidationError, match="64 lowercase hex characters"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=sig_space,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Non-64-char hex signature rejected
    sig_short = dict(valid_token)
    sig_short["signature"] = "a" * 63
    with pytest.raises(ValidationError, match="64 lowercase hex characters"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=sig_short,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Tampered signature
    tampered_sig = dict(valid_token)
    tampered_sig["signature"] = "a" * 64
    with pytest.raises(ValidationError, match="signature verification failed"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=tampered_sig,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Wrong vault
    wrong_vault_token = create_factory_admission_token(
        ADMISSION_KEY, other_vault.root, task.identifier, task.revision, "disp-1"
    )
    with pytest.raises(ValidationError, match="vault hash does not match"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=wrong_vault_token,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Wrong task
    wrong_task_token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, "other-task", task.revision, "disp-1"
    )
    with pytest.raises(ValidationError, match="task ID does not match"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=wrong_task_token,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Wrong revision
    wrong_rev_token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, "0" * 64, "disp-1"
    )
    with pytest.raises(ValidationError, match="expected revision does not match"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=wrong_rev_token,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Wrong dispatch
    wrong_disp_token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "wrong-disp"
    )
    with pytest.raises(ValidationError, match="dispatch ID does not match"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-1",
            admission=wrong_disp_token,
        )
    assert vault.get_task(task.identifier).revision == initial_revision

    # Matching admission succeeds
    claimed = claim_task(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="disp-1",
        admission=valid_token,
    )
    assert claimed.dispatch_id == "disp-1"
    assert claimed.dispatch_revision == initial_revision
    assert claimed.revision != initial_revision

    # Idempotent replay succeeds without mutation
    replay = claim_task(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="disp-1",
        admission=valid_token,
    )
    assert replay == claimed

    # 2. Generic task create and update cannot manufacture dispatch or active Factory state
    with pytest.raises(ValidationError, match="cannot be created with a dispatch ID"):
        vault.create_task(
            identifier="bypass-create-disp",
            title="Bypass create disp",
            outcome="Outcome.",
            dispatch_id="disp-forged",
            dispatch_revision="0" * 64,
        )

    with pytest.raises(ValidationError, match="cannot be created with an active thread ID"):
        vault.create_task(
            identifier="bypass-create-hand",
            title="Bypass create hand",
            outcome="Outcome.",
            agent_run="yes",
            active_thread_id="hand-forged",
        )

    with pytest.raises(ValidationError, match="task is already claimed by another dispatch"):
        vault.update_task(
            task.identifier,
            expected_revision=claimed.revision,
            dispatch_id="forged-disp",
            dispatch_revision=claimed.revision,
        )

    unclaimed_agent = vault.create_task(
        identifier="unclaimed-agent",
        title="Unclaimed agent",
        outcome="Cannot set active hand without dispatch claim.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    with pytest.raises(ValidationError, match="must be bound through dispatch"):
        vault.update_task(
            unclaimed_agent.identifier,
            expected_revision=unclaimed_agent.revision,
            active_thread_id="hand-bypass",
        )

    # 3. Authored claim_by survives agent-run and blocker transitions
    assert claimed.claim_by == "2026-07-22T13:00:00.000000Z"
    blocked = write_task_blocker(
        vault.root,
        task.identifier,
        expected_revision=initial_revision,
        dispatch_id="disp-1",
        owner="provider",
        condition="Rate limit.",
        observed_at=NOW,
    )
    assert blocked.claim_by == "2026-07-22T13:00:00.000000Z"
    cleared = clear_task_blocker(
        vault.root,
        task.identifier,
        expected_revision=initial_revision,
        dispatch_id="disp-1",
        owner="provider",
        condition="Rate limit.",
        observed_at=NOW,
    )
    assert cleared.claim_by == "2026-07-22T13:00:00.000000Z"

    # 4. Binary key bytes with exact whitespace preserved and zero-length key rejection
    binary_key = b" key\nwith\rspaces\t\x00"
    bin_key_file = tmp_path / "binary_key.key"
    bin_key_file.write_bytes(binary_key)
    bin_key_file.chmod(0o600)
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(bin_key_file))

    unclaimed_for_bin = vault.create_task(
        identifier="bin-key-task",
        title="Binary key task",
        outcome="Outcome.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    bin_token = create_factory_admission_token(
        binary_key,
        vault.root,
        unclaimed_for_bin.identifier,
        unclaimed_for_bin.revision,
        "disp-bin",
        "alloc-bin",
    )
    claimed_bin = claim_task(
        vault.root,
        unclaimed_for_bin.identifier,
        expected_revision=unclaimed_for_bin.revision,
        dispatch_id="disp-bin",
        admission=bin_token,
    )
    assert claimed_bin.dispatch_id == "disp-bin"

    # Zero length key file rejected
    zero_key_file = tmp_path / "zero_key.key"
    zero_key_file.write_bytes(b"")
    zero_key_file.chmod(0o600)
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(zero_key_file))

    zero_task = vault.create_task(
        identifier="zero-key-task",
        title="Zero key task",
        outcome="Outcome.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    zero_token = create_factory_admission_token(
        ADMISSION_KEY,
        vault.root,
        zero_task.identifier,
        zero_task.revision,
        "disp-zero",
        "alloc-zero",
    )
    with pytest.raises(ValidationError, match="factory admission key file is empty"):
        claim_task(
            vault.root,
            zero_task.identifier,
            expected_revision=zero_task.revision,
            dispatch_id="disp-zero",
            admission=zero_token,
        )
