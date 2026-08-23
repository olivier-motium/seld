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
    vault_or_id: str | Path,
    task_id: str,
    expected_revision: str,
    dispatch_id: str,
    allocation_id: str = "alloc-1",
) -> dict[str, Any]:
    if isinstance(vault_or_id, Path):
        vault_id = Vault(vault_or_id).identity()["vault_id"]
    else:
        vault_id = vault_or_id
    payload = {
        "allocation_id": allocation_id,
        "dispatch_id": dispatch_id,
        "expected_revision": expected_revision,
        "issuer": "ai-accounts-runtime",
        "schema": "seld.factory-allocation-admission.v1",
        "task_id": task_id,
        "vault_id": vault_id,
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


def test_factory_admission_valid_claim_and_idempotent_replay(vault: Vault) -> None:
    task = vault.create_task(
        identifier="admission-task",
        title="Admission task",
        outcome="Test admission validation.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    initial_revision = task.revision
    valid_token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "disp-1"
    )

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


def test_factory_admission_vault_id_binding_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "original-vault"
    vault = Vault(vault_dir)
    vault.initialize(name="Original vault")

    task = vault.create_task(
        identifier="bound-task",
        title="Bound task",
        outcome="Test vault ID binding.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "disp-vault"
    )

    # 1. A moved vault keeps the same identity and accepts the admission token
    original_vault_id = vault.identity()["vault_id"]
    moved_dir = tmp_path / "moved-vault"
    vault_dir.rename(moved_dir)
    moved_vault = Vault(moved_dir)
    assert moved_vault.identity()["vault_id"] == original_vault_id

    claimed_moved = claim_task(
        moved_vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="disp-vault",
        admission=token,
    )
    assert claimed_moved.dispatch_id == "disp-vault"

    # 2. A replacement vault at the old path has a different vault_id and refuses the old token
    replacement_vault = Vault(vault_dir)
    replacement_vault.initialize(name="Replacement vault")
    replacement_task = replacement_vault.create_task(
        identifier="bound-task",
        title="Bound task",
        outcome="Test replacement vault.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    replacement_token = create_factory_admission_token(
        ADMISSION_KEY,
        original_vault_id,
        replacement_task.identifier,
        replacement_task.revision,
        "disp-vault",
    )
    with pytest.raises(ValidationError, match="vault ID does not match current vault"):
        claim_task(
            replacement_vault.root,
            replacement_task.identifier,
            expected_revision=replacement_task.revision,
            dispatch_id="disp-vault",
            admission=replacement_token,
        )


@pytest.mark.parametrize(
    ("allocation_id", "valid"),
    [
        ("alloc-1", True),
        ("A", True),
        ("alloc_123:abc.xyz-DEF", True),
        ("a" * 128, True),
        ("", False),
        ("   ", False),
        ("a" * 129, False),
        ("-invalid-leading", False),
        (".invalid-leading", False),
        ("alloc with spaces", False),
        ("alloc\nnewline", False),
        ("alloc@special", False),
        ("alloc/slash", False),
    ],
)
def test_factory_admission_allocation_id_validation(
    vault: Vault, allocation_id: str, valid: bool
) -> None:
    task = vault.create_task(
        identifier="alloc-task",
        title="Allocation task",
        outcome="Test allocation ID validation.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    token = create_factory_admission_token(
        ADMISSION_KEY,
        vault.root,
        task.identifier,
        task.revision,
        "disp-alloc",
        allocation_id=allocation_id,
    )
    if valid:
        claimed = claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-alloc",
            admission=token,
        )
        assert claimed.dispatch_id == "disp-alloc"
    else:
        with pytest.raises(ValidationError, match="invalid factory allocation ID"):
            claim_task(
                vault.root,
                task.identifier,
                expected_revision=task.revision,
                dispatch_id="disp-alloc",
                admission=token,
            )


def test_factory_admission_key_file_boundaries(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="key-bounds-task",
        title="Key bounds task",
        outcome="Test key file boundaries.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )

    # 1. Key file does not exist
    missing_key = tmp_path / "missing.key"
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(missing_key))
    token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "disp-k"
    )
    with pytest.raises(ValidationError, match="failed to stat factory admission key file"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-k",
            admission=token,
        )

    # 2. Key file is a directory (not regular file)
    key_dir = tmp_path / "key_directory"
    key_dir.mkdir()
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(key_dir))
    with pytest.raises(ValidationError, match="not a regular file"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-k",
            admission=token,
        )

    # 3. Key file has insecure permissions (0o664)
    insecure_key = tmp_path / "insecure.key"
    insecure_key.write_bytes(ADMISSION_KEY)
    insecure_key.chmod(0o664)
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(insecure_key))
    with pytest.raises(ValidationError, match="insecure group or world permissions"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-k",
            admission=token,
        )

    # 4. Zero byte key file
    zero_key = tmp_path / "zero.key"
    zero_key.write_bytes(b"")
    zero_key.chmod(0o600)
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(zero_key))
    with pytest.raises(ValidationError, match="factory admission key file is empty"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-k",
            admission=token,
        )

    # 5. Key file exceeding 4096 bytes (4097 bytes)
    huge_key = tmp_path / "huge.key"
    huge_key.write_bytes(b"x" * 4097)
    huge_key.chmod(0o600)
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(huge_key))
    with pytest.raises(ValidationError, match="exceeds maximum size of 4096 bytes"):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-k",
            admission=token,
        )

    # 6. Raw binary key bytes with whitespace and null bytes preserved exactly
    binary_key = b" key\nwith\rspaces\t\x00\xff"
    bin_key_file = tmp_path / "binary.key"
    bin_key_file.write_bytes(binary_key)
    bin_key_file.chmod(0o600)
    monkeypatch.setenv("SELD_FACTORY_ADMISSION_KEY_FILE", str(bin_key_file))

    bin_token = create_factory_admission_token(
        binary_key, vault.root, task.identifier, task.revision, "disp-bin"
    )
    claimed_bin = claim_task(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="disp-bin",
        admission=bin_token,
    )
    assert claimed_bin.dispatch_id == "disp-bin"


@pytest.mark.parametrize(
    ("mutation", "expected_match"),
    [
        (lambda t: None, "factory allocation admission"),
        (lambda t: "string-token", "exact payload and signature"),
        (lambda t: {"payload": t["payload"]}, "exact payload and signature"),
        (lambda t: {**t, "extra_top": "val"}, "exact payload and signature"),
        (lambda t: {"payload": "not-a-dict", "signature": t["signature"]}, "unsupported shape"),
        (lambda t: {"payload": {**t["payload"], "extra": 1}, "signature": t["signature"]}, "unsupported shape"),
        (lambda t: {"payload": {k: v for k, v in t["payload"].items() if k != "schema"}, "signature": t["signature"]}, "unsupported shape"),
        (lambda t: {"payload": {**t["payload"], "schema": "bad.schema"}, "signature": t["signature"]}, "unsupported factory allocation admission schema"),
        (lambda t: {"payload": {**t["payload"], "issuer": "bad.issuer"}, "signature": t["signature"]}, "unsupported factory allocation admission issuer"),
        (lambda t: {"payload": {**t["payload"], "dispatch_id": "other-disp"}, "signature": t["signature"]}, "dispatch ID does not match claim"),
        (lambda t: {"payload": {**t["payload"], "task_id": "other-task"}, "signature": t["signature"]}, "task ID does not match claim"),
        (lambda t: {"payload": {**t["payload"], "expected_revision": "0" * 64}, "signature": t["signature"]}, "expected revision does not match claim"),
        (lambda t: {"payload": {**t["payload"], "vault_id": "other-vault-id"}, "signature": t["signature"]}, "vault ID does not match current vault"),
    ],
)
def test_factory_admission_payload_envelope_rejections(
    vault: Vault, mutation: Any, expected_match: str
) -> None:
    task = vault.create_task(
        identifier="envelope-task",
        title="Envelope task",
        outcome="Test envelope rejections.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    initial_revision = task.revision
    valid_token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "disp-env"
    )
    bad_token = mutation(valid_token)
    with pytest.raises(ValidationError, match=expected_match):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-env",
            admission=bad_token,
        )
    assert vault.get_task(task.identifier).revision == initial_revision


@pytest.mark.parametrize(
    ("sig_modifier", "expected_match"),
    [
        (lambda s: s.upper(), "64 lowercase hex characters"),
        (lambda s: f" {s} ", "64 lowercase hex characters"),
        (lambda s: s[:63], "64 lowercase hex characters"),
        (lambda s: s + "0", "64 lowercase hex characters"),
        (lambda s: "g" * 64, "64 lowercase hex characters"),
        (lambda s: "a" * 64, "signature verification failed"),
    ],
)
def test_factory_admission_signature_rejections(
    vault: Vault, sig_modifier: Any, expected_match: str
) -> None:
    task = vault.create_task(
        identifier="sig-task",
        title="Sig task",
        outcome="Test signature rejections.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    initial_revision = task.revision
    valid_token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "disp-sig"
    )
    tampered_token = {
        "payload": valid_token["payload"],
        "signature": sig_modifier(valid_token["signature"]),
    }
    with pytest.raises(ValidationError, match=expected_match):
        claim_task(
            vault.root,
            task.identifier,
            expected_revision=task.revision,
            dispatch_id="disp-sig",
            admission=tampered_token,
        )
    assert vault.get_task(task.identifier).revision == initial_revision


def test_generic_surfaces_refuse_dispatch_and_active_thread_mutations(
    vault: Vault, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Direct Vault create and update rejections
    with pytest.raises(ValidationError, match="task cannot be created with a dispatch ID, dispatch revision, or active thread ID"):
        vault.create_task(
            identifier="bypass-create-disp",
            title="Bypass create disp",
            outcome="Outcome.",
            dispatch_id="disp-forged",
            dispatch_revision="0" * 64,
        )

    with pytest.raises(ValidationError, match="task cannot be created with a dispatch ID, dispatch revision, or active thread ID"):
        vault.create_task(
            identifier="bypass-create-hand",
            title="Bypass create hand",
            outcome="Outcome.",
            agent_run="yes",
            active_thread_id="hand-forged",
        )

    task = vault.create_task(
        identifier="surface-guard-task",
        title="Surface guard task",
        outcome="Outcome.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        observed_at=NOW,
    )
    initial_revision = task.revision

    # Direct Vault.update_task has no bypass switch and refuses unexpected keywords including dispatch/hand fields and _allow flags
    for kwargs in (
        {"dispatch_id": "forged"},
        {"clear_dispatch_id": True},
        {"dispatch_revision": "forged"},
        {"clear_dispatch_revision": True},
        {"active_thread_id": "forged"},
        {"clear_active_thread_id": True},
        {"_allow_dispatch_mutation": True},
        {"_allow": True},
        {"arbitrary_keyword": "bad"},
    ):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            vault.update_task(task.identifier, expected_revision=task.revision, **kwargs)  # type: ignore[call-arg]
        assert vault.get_task(task.identifier).revision == initial_revision

    # Calling Vault.update_task with accepted public keywords cannot introduce dispatch_id, dispatch_revision, or active_thread_id
    updated = vault.update_task(
        task.identifier,
        expected_revision=task.revision,
        title="Updated surface task",
        outcome="Updated outcome.",
        status="doing",
        next_actor="agent",
        next_action="Continue work.",
        rank=42,
        target_seat="worker-a",
        claim_by="2026-07-22T14:00:00.000000Z",
        progress_check_by="2026-07-22T14:30:00.000000Z",
        project="TestProject",
        workspace="/test/workspace",
        attention_at="2026-07-25",
        due="2026-07-30",
        add_refs=("ref:a", "ref:b"),
        note="Public update",
        observed_at=NOW,
    )
    assert updated.dispatch_id is None
    assert updated.dispatch_revision is None
    assert updated.active_thread_id is None
    assert updated.rank == 42
    assert updated.title == "Updated surface task"

    # 2. Generic CLI create and update rejections
    parser = cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "create", "--id", "cli-bad", "--title", "T", "--outcome", "O", "--active-thread-id", "hand"])

    with pytest.raises(SystemExit):
        parser.parse_args(["task", "update", task.identifier, "--expected-revision", updated.revision, "--active-thread-id", "hand"])

    with pytest.raises(SystemExit):
        parser.parse_args(["task", "update", task.identifier, "--expected-revision", updated.revision, "--clear-active-thread-id"])

    # 3. Generic MCP create and update rejections
    with pytest.raises(Exception):
        mcp_server._call(
            "gsv_task_update",
            {
                "id": task.identifier,
                "expected_revision": updated.revision,
                "active_thread_id": "forged-hand",
            },
            vault=vault,
        )
    assert vault.get_task(task.identifier).revision == updated.revision


def test_authored_claim_by_survives_agent_run_and_blocker_transitions(vault: Vault) -> None:
    task = vault.create_task(
        identifier="authored-deadline-lifecycle",
        title="Authored deadline lifecycle",
        outcome="Test preservation of authored deadlines.",
        status="ready",
        next_actor="agent",
        agent_run="yes",
        claim_by="2026-07-22T13:00:00.000000Z",
        observed_at=NOW,
    )
    initial_revision = task.revision
    token = create_factory_admission_token(
        ADMISSION_KEY, vault.root, task.identifier, task.revision, "disp-auth"
    )
    claimed = claim_task(
        vault.root,
        task.identifier,
        expected_revision=task.revision,
        dispatch_id="disp-auth",
        admission=token,
    )
    assert claimed.claim_by == "2026-07-22T13:00:00.000000Z"

    blocked = write_task_blocker(
        vault.root,
        task.identifier,
        expected_revision=initial_revision,
        dispatch_id="disp-auth",
        owner="provider",
        condition="Rate limit.",
        observed_at=NOW,
    )
    assert blocked.claim_by == "2026-07-22T13:00:00.000000Z"

    cleared = clear_task_blocker(
        vault.root,
        task.identifier,
        expected_revision=initial_revision,
        dispatch_id="disp-auth",
        owner="provider",
        condition="Rate limit.",
        observed_at=NOW,
    )
    assert cleared.claim_by == "2026-07-22T13:00:00.000000Z"
