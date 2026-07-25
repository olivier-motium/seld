from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import codex_turn_transport as transport_module
from continuity_kernel.codex_turn_transport import (
    CodexTurnCoordinator,
    ProcessResult,
    ReceiptCapacityError,
    SubprocessTurnRunner,
    TurnContext,
    TurnMode,
    TurnState,
    canonical_revision_inputs,
)
from continuity_kernel.control_queue import (
    EMPTY_REVISION,
    ControlQueue,
    ControlStorageError,
    locked_control_store,
)
from continuity_kernel.direction import ABSENT_DIRECTION_REVISION, direction_aim
from continuity_kernel.errors import ValidationError
from continuity_kernel.operations import OperationLedger
from continuity_kernel.portfolio import ABSENT_PORTFOLIO_REVISION, portfolio_item
from continuity_kernel.records import REVIEW_SCOPE_REF, REVIEW_WORK_THREAD_ID
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)

THREAD_ID = "019f95fd-009e-7603-ab87-f9927cf31c4d"
OTHER_THREAD_ID = "019f95fd-009e-7603-ab87-f9927cf31c4e"
INSTANCE_ID = "1" * 32
OTHER_INSTANCE_ID = "2" * 32


@dataclass
class _Action:
    result: ProcessResult | OSError
    callback: Callable[[], None] | None = None
    wait: threading.Event | None = None
    collect_error: Exception | None = None


class _FakeRunning:
    def __init__(self, action: _Action):
        self.action = action

    def collect(self, *, timeout: float) -> ProcessResult:
        del timeout
        if self.action.wait is not None:
            assert self.action.wait.wait(timeout=5)
        if self.action.callback is not None:
            self.action.callback()
        if self.action.collect_error is not None:
            raise self.action.collect_error
        assert isinstance(self.action.result, ProcessResult)
        return self.action.result


class _FakeRunner:
    def __init__(
        self,
        *actions: _Action,
        probe: bool = True,
        probe_reason: str | None = None,
    ):
        self.actions = list(actions)
        self.probe_reason = (
            probe_reason
            if probe_reason is not None
            else (None if probe else "isolation_probe_failed")
        )
        self.probes: list[tuple[list[str], dict[str, str]]] = []
        self.spawns: list[tuple[list[str], bytes, dict[str, str]]] = []

    def probe(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
    ) -> str | None:
        del cwd, timeout
        self.probes.append((list(argv), dict(environment)))
        return self.probe_reason

    def spawn(
        self,
        argv: Sequence[str],
        *,
        prompt: bytes,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> _FakeRunning:
        del cwd
        self.spawns.append((list(argv), prompt, dict(environment)))
        if not self.actions:
            raise AssertionError("unexpected Codex spawn")
        action = self.actions.pop(0)
        if isinstance(action.result, OSError):
            raise action.result
        return _FakeRunning(action)


def _jsonl(*payloads: dict[str, Any]) -> bytes:
    return b"".join(
        json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n" for payload in payloads
    )


def _success_output(
    *, thread_id: str = THREAD_ID, answer: str = "The next review question is ready."
) -> ProcessResult:
    return ProcessResult(
        returncode=0,
        stdout=_jsonl(
            {"thread_id": thread_id, "type": "thread.started"},
            {
                "item": {"text": answer, "type": "agent_message"},
                "type": "item.completed",
            },
            {"type": "turn.completed"},
        ),
    )


def _coordinator(
    vault: Vault, runner: _FakeRunner, *, instance_id: str = INSTANCE_ID
) -> CodexTurnCoordinator:
    metadata = os.lstat(vault.root)
    return CodexTurnCoordinator(
        vault.root,
        instance_id=instance_id,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=runner,
        enabled=True,
        codex_executable="/usr/bin/true",
        turn_timeout=5,
    )


def _captured_coordinator(
    vault: Vault,
    runner: _FakeRunner,
) -> tuple[CodexTurnCoordinator, list[tuple[Callable[..., None], tuple[Any, ...]]]]:
    captured: list[tuple[Callable[..., None], tuple[Any, ...]]] = []

    class _CapturedThread:
        def __init__(self, *, target: Callable[..., None], args: tuple[Any, ...], **_kwargs: Any):
            captured.append((target, args))

        def start(self) -> None:
            return None

    metadata = os.lstat(vault.root)
    return (
        CodexTurnCoordinator(
            vault.root,
            instance_id=INSTANCE_ID,
            expected_vault_id=str(vault.identity()["vault_id"]),
            expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
            runner=runner,
            enabled=True,
            codex_executable="/usr/bin/true",
            turn_timeout=5,
            thread_factory=_CapturedThread,
        ),
        captured,
    )


def _seed_active(vault: Vault) -> dict[str, Any]:
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Decide its current place.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="portfolio-review-session",
        title="Review every open outcome",
        outcome="Check every open outcome without equating checked with resolved.",
        status="waiting",
        next_actor="human",
        next_action="Present this exact outcome.",
        waiting_on="What should change?",
        active_thread_id=THREAD_ID,
        refs=(REVIEW_SCOPE_REF, "review-subject:task:exact-outcome"),
    )
    review_thread = vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Life Portfolio review",
        purpose="Carry one finite all-open review.",
        summary="One exact review session is active.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The user should decide deliberately.",
            ),
        ),
    )
    return {
        "outcome": outcome,
        "portfolio": portfolio,
        "review_thread": review_thread,
        "session": session,
    }


def _queue_resume(
    vault: Vault, session: Any, *, choice: str = "keep this visible"
) -> tuple[Any, TurnContext]:
    queued = ControlQueue(vault.root).append(
        kind="correction",
        subject=f"record:task/{session.identifier}",
        choice=choice,
        target_revision=session.revision,
        expected_revision=EMPTY_REVISION,
    )
    event = queued.events[-1]
    return event, TurnContext(
        event_id=event.event_id,
        mode=TurnMode.RESUME,
        queue_revision=queued.revision,
        target_revision=session.revision,
        portfolio_revision=vault.get_portfolio().revision,
        canonical_revisions=canonical_revision_inputs(vault),
        session_task_id=session.identifier,
        active_thread_id=THREAD_ID,
    )


def _queue_start(vault: Vault) -> tuple[Any, TurnContext]:
    outcome = vault.create_task(
        identifier="start-outcome",
        title="Start outcome",
        outcome="Choose its current place.",
        status="ready",
        next_actor="human",
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One start outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="Ask the user directly.",
            ),
        ),
    )
    queued = ControlQueue(vault.root).append(
        kind="correction",
        subject="mind:guided-review",
        choice="start-all-open-review",
        target_revision=portfolio.revision,
        expected_revision=EMPTY_REVISION,
    )
    event = queued.events[-1]
    return event, TurnContext(
        event_id=event.event_id,
        mode=TurnMode.START,
        queue_revision=queued.revision,
        target_revision=portfolio.revision,
        portfolio_revision=portfolio.revision,
        canonical_revisions=canonical_revision_inputs(vault),
    )


def _decide(
    vault: Vault,
    event_id: str,
    *,
    decision: str,
    result_ref: str | None = None,
) -> None:
    observed = OperationLedger(vault.root).snapshot()
    OperationLedger(vault.root).decide(
        event_id=event_id,
        decision=decision,
        actor_ref=f"codex:{THREAD_ID}",
        reason_code="guided-review-test",
        result_ref=result_ref,
        expected_queue_revision=observed.queue_revision,
        expected_disposition_revision=observed.disposition_revision,
        expected_vault_id=observed.vault_id,
    )


def _wait_for(
    coordinator: CodexTurnCoordinator,
    event_id: str,
    state: TurnState,
) -> Any:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        receipt = coordinator.receipt(event_id)
        if receipt is not None and receipt.state is state:
            return receipt
        time.sleep(0.01)
    receipt = coordinator.receipt(event_id)
    raise AssertionError(f"transport did not reach {state}: {receipt}")


def test_receipt_poll_serializes_with_atomic_state_transition(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Serialized receipt polling")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    coordinator, _captured = _captured_coordinator(vault, _FakeRunner())
    pending = coordinator.submit(context)
    transitioned: list[Any] = []
    observed: list[Any] = []
    errors: list[BaseException] = []
    writer_done = threading.Event()
    reader_done = threading.Event()

    def transition() -> None:
        try:
            transitioned.append(
                coordinator._transition(
                    pending,
                    state=TurnState.FAILED_SAFE,
                    reason_code="deterministic_test_transition",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion capture
            errors.append(exc)
        finally:
            writer_done.set()

    def read() -> None:
        try:
            observed.append(coordinator.receipt(event.event_id))
        except BaseException as exc:  # pragma: no cover - assertion capture
            errors.append(exc)
        finally:
            reader_done.set()

    metadata = os.lstat(vault.root)
    with locked_control_store(
        vault.root,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
    ):
        writer = threading.Thread(target=transition)
        reader = threading.Thread(target=read)
        writer.start()
        reader.start()
        assert not writer_done.wait(timeout=0.1)
        assert not reader_done.wait(timeout=0.1)

    writer.join(timeout=2)
    reader.join(timeout=2)
    assert not writer.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert transitioned[0].state is TurnState.FAILED_SAFE
    assert observed[0].state in {TurnState.PENDING, TurnState.FAILED_SAFE}
    final = coordinator.receipt(event.event_id)
    assert final is not None
    assert final.state is TurnState.FAILED_SAFE


def test_resume_uses_exact_isolated_codex_hand_and_persists_only_content_free_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Resume transport")
    seeded = _seed_active(vault)
    secret_choice = "keep this visible SECRET-CHOICE-DO-NOT-PERSIST-IN-RECEIPT"
    event, context = _queue_resume(vault, seeded["session"], choice=secret_choice)

    def apply_and_acknowledge() -> None:
        current = vault.get_task(seeded["session"].identifier)
        changed = vault.update_task(
            current.identifier,
            expected_revision=current.revision,
            next_action="Advance to the next exact outcome.",
        )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{changed.identifier}/{changed.revision}",
        )

    runner = _FakeRunner(
        _Action(_success_output(answer="Transient model answer"), apply_and_acknowledge)
    )
    monkeypatch.setenv("SECRET_PROVIDER_TOKEN", "must-not-reach-worker")
    coordinator = _coordinator(vault, runner)
    coordinator.submit(context)
    receipt = _wait_for(coordinator, event.event_id, TurnState.COMPLETED)

    assert receipt.thread_id == THREAD_ID
    assert receipt.decision == "accepted"
    assert len(runner.spawns) == 1
    argv, prompt, environment = runner.spawns[0]
    assert argv[-3:] == ["resume", THREAD_ID, "-"]
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--strict-config" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    for feature in (
        "shell_tool",
        "browser_use",
        "browser_use_external",
        "computer_use",
        "plugins",
        "apps",
        "multi_agent",
        "image_generation",
    ):
        assert ["--disable", feature] == argv[argv.index(feature) - 1 : argv.index(feature) + 1]
    assert "--model" not in argv and "-m" not in argv
    mcp_args_value = next(value for value in argv if value.startswith("mcp_servers.gsv.args="))
    assert json.loads(mcp_args_value.split("=", 1)[1])[-6:] == [
        "mcp",
        "serve",
        "--profile",
        "guided-review",
        "--event-id",
        event.event_id,
    ]
    assert b"current Direction" in prompt
    assert b"complete Portfolio and all open Tasks" in prompt
    assert b"exact relevant WorkThreads and entities" in prompt
    assert b"only one next outcome" in prompt
    assert b"raw UUID" in prompt
    assert b"codex-thread:* shadow ref" in prompt
    assert b"Before either nonterminal or terminal acceptance" in prompt
    assert b"status=waiting and next_actor=human" in prompt
    assert b"nonempty next_action recommendation" in prompt
    assert b"nonempty waiting_on question" in prompt
    assert b"active_thread_id, status, next_actor" in prompt
    assert b"store a transcript" in prompt
    assert secret_choice.encode() not in prompt
    assert "SECRET_PROVIDER_TOKEN" not in environment
    assert "must-not-reach-worker" not in environment.values()

    receipt_bytes = b"".join(
        path.read_bytes() for path in (vault.root / ".gsv/control/runtime/turns").glob("*.json")
    )
    assert secret_choice.encode() not in receipt_bytes
    assert b"Transient model answer" not in receipt_bytes
    assert b"thread.started" not in receipt_bytes
    assert coordinator.snapshot(event.event_id)["event"]["final_answer"] == "Transient model answer"

    restarted = _coordinator(vault, _FakeRunner(), instance_id=OTHER_INSTANCE_ID)
    restarted_receipt = restarted.receipt(event.event_id)
    assert restarted_receipt == receipt
    assert restarted.snapshot(event.event_id)["event"]["final_answer"] is None


@pytest.mark.parametrize(
    "defect",
    (
        "workthread-as-hand",
        "shadow-ref",
        "not-waiting",
        "non-human-actor",
        "missing-recommendation",
        "missing-question",
        "missing-subject",
    ),
)
def test_nonterminal_accepted_review_requires_exact_hand_and_bridge_ready_question(
    tmp_path: Path,
    defect: str,
) -> None:
    vault = Vault(tmp_path / defect)
    vault.initialize(name=f"Invalid accepted review {defect}")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])

    def apply_invalid_result() -> None:
        current = vault.get_task(seeded["session"].identifier)
        if defect == "workthread-as-hand":
            changed = vault.update_task(
                current.identifier,
                expected_revision=current.revision,
                active_thread_id=REVIEW_WORK_THREAD_ID,
            )
        elif defect == "shadow-ref":
            changed = vault.update_task(
                current.identifier,
                expected_revision=current.revision,
                add_refs=(f"codex-thread:{THREAD_ID}",),
            )
        elif defect == "not-waiting":
            changed = vault.update_task(
                current.identifier,
                expected_revision=current.revision,
                status="doing",
            )
        elif defect == "non-human-actor":
            changed = vault.update_task(
                current.identifier,
                expected_revision=current.revision,
                next_actor="agent",
            )
        elif defect == "missing-recommendation":
            changed = vault.update_task(
                current.identifier,
                expected_revision=current.revision,
                clear_next_action=True,
            )
        elif defect == "missing-question":
            changed = vault.update_task(
                current.identifier,
                expected_revision=current.revision,
                clear_waiting_on=True,
            )
        else:
            assert defect == "missing-subject"
            changed = vault.update_task(
                current.identifier,
                expected_revision=current.revision,
                remove_refs=("review-subject:task:exact-outcome",),
            )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{changed.identifier}/{changed.revision}",
        )

    coordinator = _coordinator(
        vault,
        _FakeRunner(_Action(_success_output(), apply_invalid_result)),
    )
    coordinator.submit(context)

    receipt = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert receipt.reason_code == "semantic_result_unverified"


def test_resume_may_complete_a_terminal_session_without_active_question_fields(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Terminal resume")
    seeded = _seed_active(vault)
    shadow_ref = f"CoDeX-ThReAd:{THREAD_ID}"
    session_with_shadow = vault.update_task(
        seeded["session"].identifier,
        expected_revision=seeded["session"].revision,
        add_refs=(shadow_ref,),
    )
    event, context = _queue_resume(vault, session_with_shadow, choice="end this review")

    def terminalize_and_acknowledge() -> None:
        session = vault.get_task(seeded["session"].identifier)
        review_thread = vault.get_thread(REVIEW_WORK_THREAD_ID)
        vault.update_thread(
            review_thread.identifier,
            expected_revision=review_thread.revision,
            clear_focus_task=True,
        )
        terminal = vault.update_task(
            session.identifier,
            expected_revision=session.revision,
            status="done",
            clear_next_actor=True,
            clear_next_action=True,
            clear_waiting_on=True,
            clear_active_thread_id=True,
            remove_refs=(shadow_ref,),
        )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{terminal.identifier}/{terminal.revision}",
        )

    coordinator = _coordinator(
        vault,
        _FakeRunner(_Action(_success_output(), terminalize_and_acknowledge)),
    )
    coordinator.submit(context)

    receipt = _wait_for(coordinator, event.event_id, TurnState.COMPLETED)
    assert receipt.decision == "accepted"
    terminal = vault.get_task(seeded["session"].identifier)
    assert terminal.active_thread_id is None
    assert shadow_ref not in terminal.refs


def test_resume_terminal_result_retaining_shadow_hand_ref_is_unverified(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Terminal resume with shadow hand")
    seeded = _seed_active(vault)
    session = vault.update_task(
        seeded["session"].identifier,
        expected_revision=seeded["session"].revision,
        add_refs=(f"CoDeX-ThReAd:{THREAD_ID}",),
    )
    event, context = _queue_resume(vault, session, choice="end this review")

    def terminalize_and_acknowledge() -> None:
        current = vault.get_task(session.identifier)
        review_thread = vault.get_thread(REVIEW_WORK_THREAD_ID)
        vault.update_thread(
            review_thread.identifier,
            expected_revision=review_thread.revision,
            clear_focus_task=True,
        )
        terminal = vault.update_task(
            current.identifier,
            expected_revision=current.revision,
            status="done",
            clear_next_actor=True,
            clear_next_action=True,
            clear_waiting_on=True,
            clear_active_thread_id=True,
        )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{terminal.identifier}/{terminal.revision}",
        )

    coordinator = _coordinator(
        vault,
        _FakeRunner(_Action(_success_output(), terminalize_and_acknowledge)),
    )
    coordinator.submit(context)

    receipt = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert receipt.reason_code == "semantic_result_unverified"
    terminal = vault.get_task(session.identifier)
    assert terminal.status == "done"
    assert f"CoDeX-ThReAd:{THREAD_ID}" in terminal.refs


def test_initial_start_uses_canonical_review_focus_despite_unowned_scope_mistag(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Start transport")
    outcome = vault.create_task(
        identifier="first-outcome",
        title="First outcome",
        outcome="Choose its current place.",
        status="ready",
        next_actor="human",
    )
    mistagged = vault.create_task(
        identifier="ordinary-mistagged-outcome",
        title="Ordinary mistagged outcome",
        outcome="Remain ordinary unless the dedicated review WorkThread owns it.",
        status="ready",
        next_actor="human",
        refs=(REVIEW_SCOPE_REF,),
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="Ask the user directly.",
            ),
            portfolio_item(
                task_id_value=mistagged.identifier,
                task_revision=mistagged.revision,
                stance="keep-in-view",
                reason="A bare scope tag does not make this the review session.",
            ),
        ),
    )
    queued = ControlQueue(vault.root).append(
        kind="correction",
        subject="mind:guided-review",
        choice="start-all-open-review",
        target_revision=portfolio.revision,
        expected_revision=EMPTY_REVISION,
    )
    event = queued.events[-1]
    context = TurnContext(
        event_id=event.event_id,
        mode=TurnMode.START,
        queue_revision=queued.revision,
        target_revision=portfolio.revision,
        portfolio_revision=portfolio.revision,
        canonical_revisions=canonical_revision_inputs(vault),
    )

    def create_session() -> None:
        session = vault.create_task(
            identifier="portfolio-review-session",
            title="Review every open outcome",
            outcome="Check every open outcome without equating checked with resolved.",
            status="waiting",
            next_actor="human",
            next_action="Present the first outcome.",
            waiting_on="What should change?",
            refs=(REVIEW_SCOPE_REF, "review-subject:task:first-outcome"),
        )
        vault.create_thread(
            identifier="thread:life-portfolio-review",
            title="Life Portfolio review",
            purpose="Carry one finite all-open review.",
            summary="The review is starting.",
            focus_task_id=session.identifier,
            task_ids=(session.identifier,),
        )

    def bind_and_acknowledge() -> None:
        session = vault.get_task("portfolio-review-session")
        bound = vault.update_task(
            session.identifier,
            expected_revision=session.revision,
            active_thread_id=THREAD_ID,
        )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{bound.identifier}/{bound.revision}",
        )

    runner = _FakeRunner(
        _Action(_success_output(answer="First turn"), create_session),
        _Action(_success_output(answer="Review ready"), bind_and_acknowledge),
    )
    coordinator = _coordinator(vault, runner)
    coordinator.submit(context)
    receipt = _wait_for(coordinator, event.event_id, TurnState.COMPLETED)

    assert receipt.thread_id == THREAD_ID
    assert vault.get_task("portfolio-review-session").active_thread_id == THREAD_ID
    assert len(runner.spawns) == 2
    first_argv, first_prompt, _ = runner.spawns[0]
    second_argv, second_prompt, _ = runner.spawns[1]
    assert "resume" not in first_argv
    assert first_argv[-3:] == ["-C", str(vault.root), "-"]
    assert second_argv[-3:] == ["resume", THREAD_ID, "-"]
    assert b"thread UUID is not available" in first_prompt
    assert b"thread:life-portfolio-review" in first_prompt
    assert b"thread:life-portfolio-review" in second_prompt
    assert b"current Direction" in first_prompt
    assert b"complete Portfolio and all open Tasks" in first_prompt
    assert b"exact relevant WorkThreads and entities" in first_prompt
    assert b"Present only one exact current outcome" in first_prompt
    assert b"omit active_thread_id on create" in first_prompt
    assert b"clear_active_thread_id when repairing" in first_prompt
    assert b"Never put the GSV WorkThread ID in that field" in first_prompt
    assert b"never invent or retain a codex-thread:* ref" in first_prompt
    assert b"status=waiting with next_actor=human" in first_prompt
    assert THREAD_ID.encode() in second_prompt
    assert b"Call gsv_task_update" in second_prompt
    assert f"raw UUID {THREAD_ID}".encode() in second_prompt
    assert b"remove_refs for every codex-thread:* shadow ref" in second_prompt
    assert b"active_thread_id, status, next_actor, next_action, waiting_on, refs" in second_prompt
    assert b"start-all-open-review" not in first_prompt + second_prompt


def test_start_canary_shape_with_workthread_hand_shadow_ref_and_no_question_is_unverified(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Invalid canary start")
    event, context = _queue_start(vault)

    def create_invalid_session() -> None:
        session = vault.create_task(
            identifier="life-portfolio-review-session",
            title="Review every open outcome",
            outcome="Check every open outcome without equating checked with resolved.",
            status="doing",
            next_actor="agent",
            next_action="Present the first outcome.",
            active_thread_id=REVIEW_WORK_THREAD_ID,
            refs=(
                REVIEW_SCOPE_REF,
                "review-subject:task:start-outcome",
                f"codex-thread:{THREAD_ID}",
            ),
        )
        vault.create_thread(
            identifier=REVIEW_WORK_THREAD_ID,
            title="Life Portfolio review",
            purpose="Carry one finite all-open review.",
            summary="The review is starting with an invalid hand binding.",
            focus_task_id=session.identifier,
            task_ids=(session.identifier,),
        )

    def acknowledge_invalid_session() -> None:
        session = vault.get_task("life-portfolio-review-session")
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{session.identifier}/{session.revision}",
        )

    coordinator = _coordinator(
        vault,
        _FakeRunner(
            _Action(_success_output(), create_invalid_session),
            _Action(_success_output(), acknowledge_invalid_session),
        ),
    )
    coordinator.submit(context)

    receipt = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert receipt.reason_code == "semantic_result_unverified"


def test_start_accepted_result_cannot_terminalize_the_new_review_session(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Invalid terminal start")
    event, context = _queue_start(vault)

    def create_session() -> None:
        session = vault.create_task(
            identifier="portfolio-review-session",
            title="Review every open outcome",
            outcome="Check every open outcome without equating checked with resolved.",
            status="waiting",
            next_actor="human",
            next_action="Keep this outcome visible for now.",
            waiting_on="What should change about this outcome?",
            refs=(REVIEW_SCOPE_REF, "review-subject:task:start-outcome"),
        )
        vault.create_thread(
            identifier=REVIEW_WORK_THREAD_ID,
            title="Life Portfolio review",
            purpose="Carry one finite all-open review.",
            summary="The review session should remain open after START.",
            focus_task_id=session.identifier,
            task_ids=(session.identifier,),
        )

    def terminalize_and_acknowledge() -> None:
        session = vault.get_task("portfolio-review-session")
        review_thread = vault.get_thread(REVIEW_WORK_THREAD_ID)
        vault.update_thread(
            review_thread.identifier,
            expected_revision=review_thread.revision,
            clear_focus_task=True,
        )
        terminal = vault.update_task(
            session.identifier,
            expected_revision=session.revision,
            status="done",
            clear_next_actor=True,
            clear_next_action=True,
            clear_waiting_on=True,
        )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{terminal.identifier}/{terminal.revision}",
        )

    coordinator = _coordinator(
        vault,
        _FakeRunner(
            _Action(_success_output(), create_session),
            _Action(_success_output(), terminalize_and_acknowledge),
        ),
    )
    coordinator.submit(context)

    receipt = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert receipt.reason_code == "semantic_result_unverified"


def test_initial_start_binding_spawn_failure_is_terminal_and_never_replayed(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Start binding failure")
    outcome = vault.create_task(
        identifier="start-outcome",
        title="Start outcome",
        outcome="Choose its current place.",
        status="ready",
        next_actor="human",
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One start outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="Ask the user directly.",
            ),
        ),
    )
    queued = ControlQueue(vault.root).append(
        kind="correction",
        subject="mind:guided-review",
        choice="start-all-open-review",
        target_revision=portfolio.revision,
        expected_revision=EMPTY_REVISION,
    )
    event = queued.events[-1]
    context = TurnContext(
        event_id=event.event_id,
        mode=TurnMode.START,
        queue_revision=queued.revision,
        target_revision=portfolio.revision,
        portfolio_revision=portfolio.revision,
        canonical_revisions=canonical_revision_inputs(vault),
    )
    runner = _FakeRunner(
        _Action(_success_output(answer="Initial start delivered")),
        _Action(OSError("injected binding spawn failure")),
    )
    coordinator = _coordinator(vault, runner)

    coordinator.submit(context)
    uncertain = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert uncertain.reason_code == "binding_turn_spawn_failed_after_initial_turn"
    assert uncertain.thread_id == THREAD_ID
    assert uncertain.retryable is False
    assert len(runner.spawns) == 2

    coordinator.submit(context)
    time.sleep(0.05)
    assert len(runner.spawns) == 2


def test_interrupted_initial_start_keeps_emitted_hand_without_replay(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Interrupted start before semantic commit")
    event, context = _queue_start(vault)
    runner = _FakeRunner(
        _Action(
            ProcessResult(
                returncode=1,
                stdout=_success_output(answer="Opening one exact outcome").stdout,
            )
        ),
    )
    coordinator = _coordinator(vault, runner)

    coordinator.submit(context)
    uncertain = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert uncertain.reason_code == "initial_turn_did_not_complete"
    assert uncertain.thread_id == THREAD_ID
    assert uncertain.retryable is False
    assert uncertain.terminal is True
    assert len(runner.spawns) == 1
    assert vault.inspect_portfolio().review.session_task_id is None
    assert vault.list_threads() == []
    assert [pending.event_id for pending in OperationLedger(vault.root).snapshot().pending] == [
        event.event_id
    ]
    public = coordinator.snapshot(event.event_id)["event"]
    assert public["thread_id"] == THREAD_ID
    assert public["final_answer"] is None

    coordinator.submit(context)
    time.sleep(0.05)
    assert len(runner.spawns) == 1

    restarted_runner = _FakeRunner()
    restarted = _coordinator(vault, restarted_runner, instance_id=OTHER_INSTANCE_ID)
    assert restarted.receipt(event.event_id) == uncertain
    restarted.submit(context)
    assert restarted_runner.spawns == []


def test_started_initial_process_without_recoverable_hand_is_never_replayed(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unrecoverable initial hand")
    event, context = _queue_start(vault)
    runner = _FakeRunner(_Action(ProcessResult(returncode=1, stdout=b"")))
    coordinator = _coordinator(vault, runner)

    coordinator.submit(context)
    uncertain = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)

    assert uncertain.reason_code == "initial_turn_did_not_complete"
    assert uncertain.thread_id is None
    assert uncertain.retryable is False
    coordinator.submit(context)
    time.sleep(0.05)
    assert len(runner.spawns) == 1


def test_unexpected_binding_worker_failure_keeps_emitted_hand_without_replay(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unexpected worker failure after emitted hand")
    event, context = _queue_start(vault)
    runner = _FakeRunner(
        _Action(_success_output(answer="Opening the exact review hand")),
        _Action(
            ProcessResult(returncode=0, stdout=b""),
            collect_error=BrokenPipeError("injected post-spawn collection failure"),
        ),
    )
    coordinator = _coordinator(vault, runner)

    coordinator.submit(context)
    uncertain = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)

    assert uncertain.reason_code == "unexpected_worker_failure_after_possible_spawn"
    assert uncertain.thread_id == THREAD_ID
    assert uncertain.retryable is False
    assert len(runner.spawns) == 2
    coordinator.submit(context)
    time.sleep(0.05)
    assert len(runner.spawns) == 2


def test_restart_reschedules_durable_pending_receipt_before_any_spawn(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pending restart")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])

    class _DormantThread:
        def start(self) -> None:
            return None

    metadata = os.lstat(vault.root)
    first_runner = _FakeRunner()
    first = CodexTurnCoordinator(
        vault.root,
        instance_id=INSTANCE_ID,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=first_runner,
        enabled=True,
        codex_executable="/usr/bin/true",
        turn_timeout=5,
        thread_factory=lambda **_kwargs: _DormantThread(),
    )
    assert first.submit(context).state is TurnState.PENDING
    assert first_runner.spawns == []

    def apply_and_acknowledge() -> None:
        current = vault.get_task(seeded["session"].identifier)
        changed = vault.update_task(
            current.identifier,
            expected_revision=current.revision,
            next_action="Continue after the safe pre-spawn restart.",
        )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{changed.identifier}/{changed.revision}",
        )

    second_runner = _FakeRunner(
        _Action(_success_output(), apply_and_acknowledge),
    )
    restarted = _coordinator(vault, second_runner, instance_id=OTHER_INSTANCE_ID)
    restarted.submit(context)
    assert _wait_for(restarted, event.event_id, TurnState.COMPLETED).attempt == 1
    assert len(second_runner.spawns) == 1


def test_second_event_lock_contention_fails_safe_without_stranding_pending(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Two exact events")
    seeded = _seed_active(vault)
    first_event, first_context = _queue_resume(vault, seeded["session"], choice="first answer")
    release = threading.Event()
    first = _coordinator(
        vault,
        _FakeRunner(_Action(ProcessResult(returncode=1, stdout=b""), wait=release)),
    )
    first.submit(first_context)
    _wait_for(first, first_event.event_id, TurnState.RUNNING)

    queue = ControlQueue(vault.root)
    current_queue = queue.snapshot()
    second_queue = queue.append(
        kind="correction",
        subject=f"record:task/{seeded['session'].identifier}",
        choice="second answer",
        target_revision=seeded["session"].revision,
        expected_revision=current_queue.revision,
    )
    second_event = second_queue.events[-1]
    second_context = replace(
        first_context,
        event_id=second_event.event_id,
        queue_revision=second_queue.revision,
    )
    second_runner = _FakeRunner()
    second = _coordinator(vault, second_runner, instance_id=OTHER_INSTANCE_ID)
    second.submit(second_context)

    contended = _wait_for(second, second_event.event_id, TurnState.FAILED_SAFE)
    assert contended.reason_code == "worker_lock_contended_before_spawn"
    assert contended.retryable is True
    assert second_runner.spawns == []

    release.set()
    _wait_for(first, first_event.event_id, TurnState.DELIVERY_UNCERTAIN)


def test_restart_blocks_pending_receipt_when_canonical_context_changed_before_delivery(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pre-delivery drift")
    seeded = _seed_active(vault)
    _event, context = _queue_resume(vault, seeded["session"])

    class _DormantThread:
        def start(self) -> None:
            return None

    metadata = os.lstat(vault.root)
    first = CodexTurnCoordinator(
        vault.root,
        instance_id=INSTANCE_ID,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=_FakeRunner(),
        enabled=True,
        codex_executable="/usr/bin/true",
        turn_timeout=5,
        thread_factory=lambda **_kwargs: _DormantThread(),
    )
    assert first.submit(context).state is TurnState.PENDING

    vault.create_entity(
        identifier="topic:unrelated-drift",
        title="Unrelated drift",
        entity_type="topic",
        summary="Changed after the receipt but before any child spawn.",
    )
    changed_context = replace(context, canonical_revisions=canonical_revision_inputs(vault))
    runner = _FakeRunner()
    restarted = _coordinator(vault, runner, instance_id=OTHER_INSTANCE_ID)
    blocked = restarted.submit(changed_context)

    assert blocked.state is TurnState.BLOCKED
    assert blocked.reason_code == "context_changed_before_delivery"
    assert blocked.terminal is True
    assert blocked.retryable is False
    assert runner.spawns == []


def test_initial_worker_blocks_canonical_drift_before_first_spawn(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Immediate pre-spawn drift")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    captured: list[tuple[Callable[..., None], tuple[Any, ...]]] = []

    class _CapturedThread:
        def __init__(self, *, target: Callable[..., None], args: tuple[Any, ...], **_kwargs: Any):
            captured.append((target, args))

        def start(self) -> None:
            return None

    metadata = os.lstat(vault.root)
    runner = _FakeRunner()
    coordinator = CodexTurnCoordinator(
        vault.root,
        instance_id=INSTANCE_ID,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=runner,
        enabled=True,
        codex_executable="/usr/bin/true",
        turn_timeout=5,
        thread_factory=_CapturedThread,
    )
    assert coordinator.submit(context).state is TurnState.PENDING
    assert len(captured) == 1

    vault.create_entity(
        identifier="topic:changed-before-worker",
        title="Changed before worker",
        entity_type="topic",
        summary="This revision was not part of the submitted context.",
    )
    target, args = captured[0]
    target(*args)

    blocked = coordinator.receipt(event.event_id)
    assert blocked is not None
    assert blocked.state is TurnState.BLOCKED
    assert blocked.reason_code == "context_changed_before_delivery"
    assert runner.spawns == []


def test_worker_blocks_when_exact_event_was_dispositioned_before_first_spawn(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="No stale event delivery")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner()
    coordinator, captured = _captured_coordinator(vault, runner)
    assert coordinator.submit(context).state is TurnState.PENDING
    assert len(captured) == 1

    _decide(vault, event.event_id, decision="rejected")
    target, args = captured[0]
    target(*args)

    blocked = coordinator.receipt(event.event_id)
    assert blocked is not None
    assert blocked.state is TurnState.BLOCKED
    assert blocked.reason_code == "event_not_pending_before_delivery"
    assert blocked.terminal is True
    assert runner.spawns == []


def test_codex_disappearing_before_spawn_is_failed_safe_not_delivery_uncertain(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Codex disappears before spawn")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner()
    coordinator, captured = _captured_coordinator(vault, runner)
    assert coordinator.submit(context).state is TurnState.PENDING
    assert len(captured) == 1

    coordinator.codex_executable = None
    target, args = captured[0]
    target(*args)

    failed = coordinator.receipt(event.event_id)
    assert failed is not None
    assert failed.state is TurnState.FAILED_SAFE
    assert failed.reason_code == "spawn_failed_before_child"
    assert failed.retryable is True
    assert runner.spawns == []


def test_restart_with_unavailable_codex_fails_pending_receipt_safe_without_worker(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unavailable Codex pending restart")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    first, captured = _captured_coordinator(vault, _FakeRunner())
    assert first.submit(context).state is TurnState.PENDING
    assert len(captured) == 1

    metadata = os.lstat(vault.root)
    restarted_runner = _FakeRunner()
    restarted = CodexTurnCoordinator(
        vault.root,
        instance_id=OTHER_INSTANCE_ID,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=restarted_runner,
        enabled=True,
        codex_executable=tmp_path / "missing-codex",
        turn_timeout=5,
    )

    failed = restarted.submit(context)

    assert failed.event_id == event.event_id
    assert failed.state is TurnState.FAILED_SAFE
    assert failed.reason_code == "codex_unavailable"
    assert restarted_runner.spawns == []


def test_worker_blocks_when_operation_state_is_unreadable_before_first_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unreadable operation state")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner()
    coordinator, captured = _captured_coordinator(vault, runner)
    assert coordinator.submit(context).state is TurnState.PENDING
    assert len(captured) == 1

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise ValidationError("injected unreadable operation state")

    monkeypatch.setattr(OperationLedger, "snapshot", unavailable)
    target, args = captured[0]
    target(*args)

    blocked = coordinator.receipt(event.event_id)
    assert blocked is not None
    assert blocked.state is TurnState.BLOCKED
    assert blocked.reason_code == "operation_state_unavailable_before_delivery"
    assert blocked.terminal is True
    assert runner.spawns == []


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"decision": "accepted"}, "non-completed.*semantic result"),
        ({"owner_instance_id": INSTANCE_ID}, "non-running.*owner"),
        (
            {
                "state": "completed",
                "result_context_hash": "a" * 64,
            },
            "lacks exact thread or decision",
        ),
        (
            {"updated_at": "2000-01-01T00:00:00.000000Z"},
            "timestamps are reversed",
        ),
    ],
)
def test_tampered_receipt_cross_field_state_is_rejected(
    tmp_path: Path,
    changes: dict[str, Any],
    error: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Tampered receipt")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])

    class _DormantThread:
        def start(self) -> None:
            return None

    metadata = os.lstat(vault.root)
    coordinator = CodexTurnCoordinator(
        vault.root,
        instance_id=INSTANCE_ID,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=_FakeRunner(),
        enabled=True,
        codex_executable="/usr/bin/true",
        turn_timeout=5,
        thread_factory=lambda **_kwargs: _DormantThread(),
    )
    pending = coordinator.submit(context)
    receipt_path = vault.root / ".gsv/control/runtime/turns" / f"{event.event_id}.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload.update(changes)
    receipt_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ControlStorageError,
        match="guided-review transport receipt failed integrity validation",
    ) as classified:
        coordinator.receipt(event.event_id)
    assert re.search(error, str(classified.value.__cause__)) is not None

    with pytest.raises(
        ValidationError,
        match="completed transport receipt lacks",
    ):
        coordinator._transition(pending, state=TurnState.COMPLETED)


def test_only_pre_spawn_failure_is_retryable(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Safe retry")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])

    def apply_and_acknowledge() -> None:
        current = vault.get_task(seeded["session"].identifier)
        changed = vault.update_task(
            current.identifier,
            expected_revision=current.revision,
            next_action="Retry succeeded before any ambiguous delivery.",
        )
        _decide(
            vault,
            event.event_id,
            decision="accepted",
            result_ref=f"task:{changed.identifier}/{changed.revision}",
        )

    runner = _FakeRunner(
        _Action(OSError("injected pre-spawn failure")),
        _Action(_success_output(), apply_and_acknowledge),
    )
    coordinator = _coordinator(vault, runner)
    coordinator.submit(context)
    failed = _wait_for(coordinator, event.event_id, TurnState.FAILED_SAFE)
    assert failed.retryable is True

    coordinator.submit(context)
    completed = _wait_for(coordinator, event.event_id, TurnState.COMPLETED)
    assert completed.attempt == 2
    assert len(runner.spawns) == 2


@pytest.mark.parametrize(
    "result",
    (
        ProcessResult(returncode=1, stdout=b""),
        ProcessResult(returncode=0, stdout=b"", timed_out=True),
        ProcessResult(returncode=0, stdout=b"", output_truncated=True),
    ),
)
def test_any_ambiguous_post_spawn_outcome_is_terminal_and_never_replayed(
    tmp_path: Path,
    result: ProcessResult,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Ambiguous delivery")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner(_Action(result))
    coordinator = _coordinator(vault, runner)

    coordinator.submit(context)
    uncertain = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert uncertain.retryable is False
    assert uncertain.terminal is True
    coordinator.submit(context)
    time.sleep(0.05)
    assert len(runner.spawns) == 1


def test_restart_converts_in_flight_receipt_to_delivery_uncertain_without_replay(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Interrupted delivery")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    release = threading.Event()
    first_runner = _FakeRunner(_Action(ProcessResult(returncode=1, stdout=b""), wait=release))
    first = _coordinator(vault, first_runner)
    first.submit(context)
    _wait_for(first, event.event_id, TurnState.RUNNING)

    second_runner = _FakeRunner()
    restarted = _coordinator(vault, second_runner, instance_id=OTHER_INSTANCE_ID)
    recovered = restarted.receipt(event.event_id)
    assert recovered is not None
    assert recovered.state is TurnState.DELIVERY_UNCERTAIN
    restarted.submit(context)
    assert second_runner.spawns == []
    release.set()


def test_exit_zero_without_matching_disposition_and_revision_is_not_success(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Evidence required")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    coordinator = _coordinator(vault, _FakeRunner(_Action(_success_output())))

    coordinator.submit(context)
    receipt = _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)
    assert receipt.reason_code == "semantic_result_unverified"
    assert coordinator.snapshot(event.event_id)["event"]["final_answer"] is None
    assert OperationLedger(vault.root).snapshot().pending[0].event_id == event.event_id


def test_rejection_completes_only_when_canonical_revision_set_is_unchanged(tmp_path: Path) -> None:
    unchanged = Vault(tmp_path / "unchanged")
    unchanged.initialize(name="Safe rejection")
    seeded = _seed_active(unchanged)
    event, context = _queue_resume(unchanged, seeded["session"])
    safe = _coordinator(
        unchanged,
        _FakeRunner(
            _Action(
                _success_output(),
                lambda: _decide(unchanged, event.event_id, decision="rejected"),
            )
        ),
    )
    safe.submit(context)
    assert _wait_for(safe, event.event_id, TurnState.COMPLETED).decision == "rejected"

    changed = Vault(tmp_path / "changed")
    changed.initialize(name="Unsafe rejection")
    changed_seed = _seed_active(changed)
    changed_event, changed_context = _queue_resume(changed, changed_seed["session"])

    def mutate_then_reject() -> None:
        session = changed.get_task(changed_seed["session"].identifier)
        changed.update_task(
            session.identifier,
            expected_revision=session.revision,
            next_action="This mutation makes a rejection ambiguous.",
        )
        _decide(changed, changed_event.event_id, decision="rejected")

    unsafe = _coordinator(
        changed,
        _FakeRunner(_Action(_success_output(), mutate_then_reject)),
    )
    unsafe.submit(changed_context)
    assert _wait_for(unsafe, changed_event.event_id, TurnState.DELIVERY_UNCERTAIN)


@pytest.mark.parametrize("record_kind", ["direction", "entity"])
def test_rejection_is_delivery_uncertain_after_direction_or_entity_mutation(
    tmp_path: Path,
    record_kind: str,
) -> None:
    vault = Vault(tmp_path / record_kind)
    vault.initialize(name=f"Changed {record_kind}")
    seeded = _seed_active(vault)
    if record_kind == "direction":
        direction = vault.set_direction(
            expected_revision=ABSENT_DIRECTION_REVISION,
            status="provisional",
            current_chapter="Review the whole life deliberately.",
            aims=(
                direction_aim(
                    identifier="protect-attention",
                    title="Protect attention",
                    desired_state="Attention remains available for chosen work.",
                ),
            ),
        )
        expected_pair = ("direction:current", direction.revision)
    else:
        entity = vault.create_entity(
            identifier="person:exact-review-owner",
            title="Exact Review Owner",
            entity_type="person",
            summary="Owns one exact review concern.",
        )
        expected_pair = (f"entity:{entity.identifier}", entity.revision)

    event, context = _queue_resume(vault, seeded["session"])
    assert expected_pair in context.canonical_revisions

    def mutate_then_reject() -> None:
        if record_kind == "direction":
            current = vault.get_direction()
            vault.set_direction(
                expected_revision=current.revision,
                status=current.status,
                current_chapter="Review changed whole-life direction deliberately.",
                aims=current.aims,
            )
        else:
            current_entity = vault.get_entity("person:exact-review-owner")
            vault.update_entity(
                current_entity.identifier,
                expected_revision=current_entity.revision,
                summary="The exact entity changed during delivery.",
            )
        _decide(vault, event.event_id, decision="rejected")

    coordinator = _coordinator(
        vault,
        _FakeRunner(_Action(_success_output(), mutate_then_reject)),
    )
    coordinator.submit(context)
    assert _wait_for(coordinator, event.event_id, TurnState.DELIVERY_UNCERTAIN)


def test_isolation_capability_failure_blocks_without_spawning(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Capability gate")
    seeded = _seed_active(vault)
    _event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner(probe=False)
    coordinator = _coordinator(vault, runner)

    receipt = coordinator.submit(context)
    assert receipt.state is TurnState.BLOCKED
    assert receipt.reason_code == "isolation_probe_failed"
    assert runner.spawns == []
    assert len(runner.probes) == 1


def test_capability_failure_is_reprobed_after_bounded_cache_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Capability repair becomes visible")
    runner = _FakeRunner(probe=False)
    clock = [100.0]
    monkeypatch.setattr("continuity_kernel.codex_turn_transport.time.monotonic", lambda: clock[0])
    coordinator = _coordinator(vault, runner)

    assert coordinator.capability().available is False
    runner.probe_reason = None
    assert coordinator.capability().available is False
    assert len(runner.probes) == 1

    clock[0] += transport_module.CAPABILITY_CACHE_SECONDS + 0.001
    assert coordinator.capability().available is True
    assert len(runner.probes) == 2


def test_crash_orphan_receipt_temps_do_not_consume_receipt_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Receipt temp capacity")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    receipt_directory = vault.root / ".gsv/control/runtime/turns"
    receipt_directory.mkdir(parents=True)
    for index in range(5):
        (receipt_directory / f".{index:032x}.json.tmp-crash").write_bytes(b"orphan\n")
    monkeypatch.setattr(transport_module, "MAX_EVENTS", 2)

    class _DormantThread:
        def start(self) -> None:
            return None

    metadata = os.lstat(vault.root)
    coordinator = CodexTurnCoordinator(
        vault.root,
        instance_id=INSTANCE_ID,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=_FakeRunner(),
        enabled=True,
        codex_executable="/usr/bin/true",
        turn_timeout=5,
        thread_factory=lambda **_kwargs: _DormantThread(),
    )

    receipt = coordinator.submit(context)

    assert receipt.event_id == event.event_id
    assert receipt.state is TurnState.PENDING


def test_full_receipt_store_keeps_the_exact_event_pending_for_manual_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Receipt capacity fail-closed")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    receipt_directory = vault.root / ".gsv/control/runtime/turns"
    receipt_directory.mkdir(parents=True)
    (receipt_directory / "occupied.json").write_bytes(b"{}\n")
    monkeypatch.setattr(transport_module, "MAX_EVENTS", 1)
    coordinator = _coordinator(vault, _FakeRunner())

    with pytest.raises(ReceiptCapacityError, match="receipt store is full"):
        coordinator.submit(context)

    operation = OperationLedger(vault.root).snapshot()
    assert [pending.event_id for pending in operation.pending] == [event.event_id]
    assert coordinator.receipt(event.event_id) is None


def test_corrupt_receipt_is_classified_as_control_storage_failure(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corrupt receipt integrity")
    seeded = _seed_active(vault)
    event, _context = _queue_resume(vault, seeded["session"])
    receipt_directory = vault.root / ".gsv/control/runtime/turns"
    receipt_directory.mkdir(parents=True)
    (receipt_directory / f"{event.event_id}.json").write_bytes(b"{}\n")
    coordinator = _coordinator(vault, _FakeRunner())

    with pytest.raises(ControlStorageError, match="failed integrity validation"):
        coordinator.receipt(event.event_id)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("event_id", OTHER_THREAD_ID),
        ("vault_id", "11111111-1111-4111-8111-111111111111"),
    ],
)
def test_receipt_filename_and_vault_binding_mismatch_is_storage_failure(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Receipt binding integrity")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    coordinator, _captured = _captured_coordinator(vault, _FakeRunner())
    coordinator.submit(context)
    receipt_path = vault.root / ".gsv/control/runtime/turns" / f"{event.event_id}.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload[field] = replacement
    receipt_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ControlStorageError, match="does not match its bound event and vault"):
        coordinator.receipt(event.event_id)


def test_worker_recreates_missing_lock_directory_before_spawn(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Missing worker lock directory")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner(_Action(OSError("injected pre-spawn failure")))
    coordinator, captured = _captured_coordinator(vault, runner)
    coordinator.submit(context)
    locks = vault.root / ".gsv/locks"
    for child in locks.iterdir():
        child.unlink()
    locks.rmdir()

    target, args = captured.pop()
    target(*args)

    assert locks.is_dir()
    receipt = coordinator.receipt(event.event_id)
    assert receipt is not None
    assert receipt.state is TurnState.FAILED_SAFE
    assert receipt.reason_code == "spawn_failed_before_child"
    assert len(runner.spawns) == 1


def test_retry_attempt_limit_is_classified_as_control_storage_failure(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Receipt retry limit")
    seeded = _seed_active(vault)
    _event, context = _queue_resume(vault, seeded["session"])
    coordinator, captured = _captured_coordinator(vault, _FakeRunner())
    pending = coordinator.submit(context)
    assert len(captured) == 1
    coordinator._transition(
        pending,
        state=TurnState.FAILED_SAFE,
        attempt=1_000,
        reason_code="worker_lock_contended_before_spawn",
    )

    with pytest.raises(ControlStorageError, match="retry limit reached"):
        coordinator.submit(context)


def test_feature_disabled_reports_transient_block_without_persisting_receipt(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Feature disabled transport")
    seeded = _seed_active(vault)
    event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner()
    metadata = os.lstat(vault.root)
    coordinator = CodexTurnCoordinator(
        vault.root,
        instance_id=INSTANCE_ID,
        expected_vault_id=str(vault.identity()["vault_id"]),
        expected_root_identity=(int(metadata.st_dev), int(metadata.st_ino)),
        runner=runner,
        enabled=False,
        codex_executable="/usr/bin/true",
        turn_timeout=5,
    )

    receipt = coordinator.submit(context)

    assert receipt.event_id == event.event_id
    assert receipt.state is TurnState.BLOCKED
    assert receipt.reason_code == "feature_disabled"
    assert coordinator.receipt(event.event_id) is None
    assert not (vault.root / ".gsv/control/runtime/turns").exists()
    assert runner.probes == []
    assert runner.spawns == []


def test_missing_local_codex_auth_blocks_with_exact_reason(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Auth capability gate")
    seeded = _seed_active(vault)
    _event, context = _queue_resume(vault, seeded["session"])
    runner = _FakeRunner(probe_reason="codex_auth_unavailable")
    coordinator = _coordinator(vault, runner)

    receipt = coordinator.submit(context)

    assert receipt.state is TurnState.BLOCKED
    assert receipt.reason_code == "codex_auth_unavailable"
    assert runner.spawns == []


def test_mcp_command_ignores_a_vault_local_continuity_kernel_shadow(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Isolated MCP import")
    shadow = vault.root / "continuity_kernel"
    shadow.mkdir()
    sentinel = vault.root / "shadow-imported.txt"
    (shadow / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('shadow imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (shadow / "__main__.py").write_text("raise SystemExit(37)\n", encoding="utf-8")
    coordinator = _coordinator(vault, _FakeRunner())
    command = coordinator._mcp_command(THREAD_ID)

    completed = subprocess.run(
        command,
        cwd=vault.root,
        env=coordinator._environment(),
        input=b"",
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert command[1:4] == ["-I", "-m", "continuity_kernel"]
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("auth_returncode", "expected_reason"),
    [(0, None), (1, "codex_auth_unavailable")],
)
def test_subprocess_probe_checks_local_auth_without_model_or_provider_traffic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_returncode: int,
    expected_reason: str | None,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: Sequence[str], **kwargs: Any) -> Any:
        command = list(argv)
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            auth_returncode if command[1:] == ["login", "status"] else 0,
        )

    monkeypatch.setattr(subprocess, "run", run)
    environment = {"CODEX_HOME": str(tmp_path / "isolated-codex-home")}
    reason = SubprocessTurnRunner().probe(
        ["/opt/codex", "exec", "--strict-config"],
        cwd=tmp_path,
        environment=environment,
        timeout=3,
    )

    assert reason == expected_reason
    assert [command for command, _ in calls] == [
        ["/opt/codex", "exec", "--strict-config", "--help"],
        ["/opt/codex", "login", "status"],
    ]
    assert all(call["env"] == environment for _, call in calls)
    assert all(call["stdin"] is subprocess.DEVNULL for _, call in calls)
    assert all(call["stdout"] is subprocess.DEVNULL for _, call in calls)
    assert all(call["stderr"] is subprocess.DEVNULL for _, call in calls)
