from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import bridge as bridge_module
from continuity_kernel.bridge import BridgeHTTPServer
from continuity_kernel.codex_turn_transport import (
    TRANSPORT_SCHEMA_VERSION,
    ReceiptCapacityError,
    TurnContext,
    TurnMode,
    TurnReceipt,
    TurnState,
)
from continuity_kernel.operations import OperationLedger
from continuity_kernel.portfolio import ABSENT_PORTFOLIO_REVISION, portfolio_item
from continuity_kernel.records import (
    REVIEW_PAUSED_REF,
    format_time,
    review_coverage_ref,
    review_option_ref,
)
from continuity_kernel.vault import Vault, doctor_dict

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)

ACCESS_TOKEN = "e" * 48
INSTANCE_ID = "f" * 32
REVIEW_HAND_ID = "018f6a20-7b3c-7d42-8a19-2e5f603b91c4"


class SemanticReviewTransport:
    """Fake one exact Codex hand while exercising real canonical and queue CAS."""

    def __init__(
        self,
        vault: Vault,
        *,
        receipts: dict[str, TurnReceipt] | None = None,
    ) -> None:
        self.vault = vault
        self.receipts = dict(receipts or {})
        self.answers: dict[str, str] = {}
        self.submit_count = 0

    def seed_pending(self, context: TurnContext) -> TurnReceipt:
        exact = context.validated()
        now = format_time(datetime.now(UTC))
        receipt = TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=exact.event_id,
            mode=exact.mode,
            state=TurnState.PENDING,
            attempt=1,
            vault_id=str(self.vault.identity()["vault_id"]),
            context_hash=exact.context_hash,
            queue_revision=exact.queue_revision,
            target_revision=exact.target_revision,
            thread_id=exact.active_thread_id,
            owner_instance_id=None,
            decision=None,
            result_ref=None,
            canonical_revision=None,
            result_context_hash=None,
            reason_code=None,
            created_at=now,
            updated_at=now,
        )
        self.receipts[exact.event_id] = receipt
        return receipt

    def snapshot(
        self,
        event_id: str | None = None,
        *,
        include_capability: bool = True,
    ) -> dict[str, Any]:
        receipt = self.receipts.get(event_id) if event_id is not None else None
        return {
            "automatic_resume": True,
            "automatic_start": True,
            "available": True,
            "enabled": True,
            "event": (
                receipt.public(final_answer=self.answers.get(receipt.event_id))
                if receipt is not None
                else None
            ),
            "reason_code": None,
        }

    def receipt(self, event_id: object) -> TurnReceipt | None:
        return self.receipts.get(str(event_id))

    def submit(self, context: TurnContext) -> TurnReceipt:
        exact = context.validated()
        existing = self.receipts.get(exact.event_id)
        if existing is not None and existing.state is not TurnState.PENDING:
            return existing
        assert exact.mode is TurnMode.RESUME
        self.submit_count += 1

        operation = OperationLedger(self.vault.root).snapshot()
        event = next(value for value in operation.pending if value.event_id == exact.event_id)
        assert event.choice == (
            "Author the smallest local next move and keep external action gated."
        )

        outcome = self.vault.get_task("exact-outcome")
        changed_outcome = self.vault.update_task(
            outcome.identifier,
            expected_revision=outcome.revision,
            next_action="Prepare the smallest local proof before asking for approval.",
        )
        owner = self.vault.get_thread("thread:exact-work")
        changed_owner = self.vault.update_thread(
            owner.identifier,
            expected_revision=owner.revision,
            next_move="Carry the bounded local proof and keep external action gated.",
        )
        second = self.vault.get_task("next-outcome")
        portfolio = self.vault.get_portfolio()
        changed_portfolio = self.vault.set_portfolio(
            expected_revision=portfolio.revision,
            summary="Both current outcomes remain open in authored order.",
            items=(
                portfolio_item(
                    task_id_value=changed_outcome.identifier,
                    task_revision=changed_outcome.revision,
                    stance="agent-can-carry",
                    reason="The newly authored next move is local and reversible.",
                    work_thread_id=changed_owner.identifier,
                    work_thread_revision=changed_owner.revision,
                ),
                portfolio_item(
                    task_id_value=second.identifier,
                    task_revision=second.revision,
                    stance="needs-human",
                    reason="The desired boundary still needs an explicit answer.",
                ),
            ),
        )
        session = self.vault.get_task("review-session")
        removed = tuple(
            value
            for value in session.refs
            if value.startswith("review-subject:") or value.startswith("review-option:")
        )
        advanced = self.vault.update_task(
            session.identifier,
            expected_revision=session.revision,
            remove_refs=removed,
            add_refs=(
                review_coverage_ref(
                    task_id_value=changed_outcome.identifier,
                    task_revision=changed_outcome.revision,
                    work_thread_id=changed_owner.identifier,
                    work_thread_revision=changed_owner.revision,
                ),
                "review-subject:task:next-outcome",
                review_option_ref(
                    intent="keep",
                    subject_task_id="next-outcome",
                    consequence="Leave this outcome unchanged and check only this review step.",
                ),
            ),
            next_action="Keep the second outcome current if its boundary still matters.",
            waiting_on="Does the second outcome still earn its place?",
        )

        current = OperationLedger(self.vault.root).snapshot()
        OperationLedger(self.vault.root).decide(
            event_id=event.event_id,
            decision="accepted",
            actor_ref=f"codex:{REVIEW_HAND_ID}",
            reason_code="semantic-readback-complete",
            expected_queue_revision=current.queue_revision,
            expected_disposition_revision=current.disposition_revision,
            result_ref=f"task:{advanced.identifier}/{advanced.revision}",
        )
        now = format_time(datetime.now(UTC))
        receipt = TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=exact.event_id,
            mode=exact.mode,
            state=TurnState.COMPLETED,
            attempt=1,
            vault_id=str(self.vault.identity()["vault_id"]),
            context_hash=exact.context_hash,
            queue_revision=exact.queue_revision,
            target_revision=exact.target_revision,
            thread_id=REVIEW_HAND_ID,
            owner_instance_id=None,
            decision="accepted",
            result_ref=f"task:{advanced.identifier}/{advanced.revision}",
            canonical_revision=changed_portfolio.revision,
            result_context_hash=exact.context_hash,
            reason_code=None,
            created_at=now,
            updated_at=now,
        )
        self.receipts[exact.event_id] = receipt
        self.answers[exact.event_id] = (
            "I tightened the first next move and kept external action gated. "
            "Now: does the second outcome still earn its place?"
        )
        return receipt


class PreparedBoardTransport(SemanticReviewTransport):
    """Prepare two receipt-bound rows, then regenerate the same set safely."""

    def __init__(self, vault: Vault) -> None:
        super().__init__(vault)
        self.sheets: dict[str, list[dict[str, Any]]] = {}
        self.batch_choices: list[str] = []

    def snapshot(
        self,
        event_id: str | None = None,
        *,
        include_capability: bool = True,
    ) -> dict[str, Any]:
        snapshot = super().snapshot(event_id, include_capability=include_capability)
        event = snapshot.get("event")
        if isinstance(event, dict) and event_id in self.sheets:
            sheet = self.sheets[event_id]
            event.update(
                {
                    "sheet": sheet,
                    "sheet_state": "ready",
                    "subject_task_ids": [str(entry["task"]) for entry in sheet],
                }
            )
        return snapshot

    def submit(self, context: TurnContext) -> TurnReceipt:
        exact = context.validated()
        existing = self.receipts.get(exact.event_id)
        if existing is not None:
            return existing
        operation = OperationLedger(self.vault.root).snapshot()
        event = next(value for value in operation.pending if value.event_id == exact.event_id)
        self.submit_count += 1
        session = self.vault.get_task("review-session")
        refreshed = False
        if self.submit_count == 1:
            first = self.vault.get_task("exact-outcome")
            prepared_ids = [
                item.task_id
                for item in self.vault.get_portfolio().items
                if item.task_id != first.identifier
            ]
            removed = tuple(
                ref
                for ref in session.refs
                if ref.startswith("review-subject:") or ref.startswith("review-option:")
            )
            advanced = self.vault.update_task(
                session.identifier,
                expected_revision=session.revision,
                remove_refs=removed,
                add_refs=(
                    review_coverage_ref(
                        task_id_value=first.identifier,
                        task_revision=first.revision,
                    ),
                    *(f"review-subject:task:{task_id}" for task_id in prepared_ids),
                ),
                next_action="Review the two consequential prepared decisions.",
                waiting_on="Which of these exact outcomes should change?",
            )
            headline = "Two exact decisions genuinely need you."
        elif event.choice.startswith("Pause this guided all-open review"):
            prepared_ids = [
                ref.removeprefix("review-subject:task:")
                for ref in session.refs
                if ref.startswith("review-subject:task:")
            ]
            advanced = self.vault.update_task(
                session.identifier,
                expected_revision=session.revision,
                add_refs=(REVIEW_PAUSED_REF,),
            )
            headline = "The exact prepared review is paused."
        elif event.choice == "resume-guided-review":
            prepared_ids = [
                ref.removeprefix("review-subject:task:")
                for ref in session.refs
                if ref.startswith("review-subject:task:")
            ]
            advanced = self.vault.update_task(
                session.identifier,
                expected_revision=session.revision,
                remove_refs=(REVIEW_PAUSED_REF,),
            )
            headline = "The exact prepared review resumed."
        else:
            prepared_ids = [
                ref.removeprefix("review-subject:task:")
                for ref in session.refs
                if ref.startswith("review-subject:task:")
            ]
            self.batch_choices.append(event.choice)
            refreshed = True
            advanced = self.vault.update_task(
                session.identifier,
                expected_revision=session.revision,
                next_action="The same subject set was re-prepared after one row could not land.",
                waiting_on="Choose against the refreshed questions, not the previous receipt.",
            )
            headline = "One answer could not land, so I refreshed the exact set."
        current = OperationLedger(self.vault.root).snapshot()
        OperationLedger(self.vault.root).decide(
            event_id=event.event_id,
            decision="accepted",
            actor_ref=f"codex:{REVIEW_HAND_ID}",
            reason_code="prepared-board-readback-complete",
            expected_queue_revision=current.queue_revision,
            expected_disposition_revision=current.disposition_revision,
            result_ref=f"task:{advanced.identifier}/{advanced.revision}",
        )
        now = format_time(datetime.now(UTC))
        receipt = TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=exact.event_id,
            mode=exact.mode,
            state=TurnState.COMPLETED,
            attempt=1,
            vault_id=str(self.vault.identity()["vault_id"]),
            context_hash=exact.context_hash,
            queue_revision=exact.queue_revision,
            target_revision=exact.target_revision,
            thread_id=REVIEW_HAND_ID,
            owner_instance_id=None,
            decision="accepted",
            result_ref=f"task:{advanced.identifier}/{advanced.revision}",
            canonical_revision=advanced.revision,
            result_context_hash=exact.context_hash,
            reason_code=None,
            created_at=now,
            updated_at=now,
        )
        prepared_tasks = [self.vault.get_task(task_id) for task_id in prepared_ids]
        sheet: list[dict[str, Any]] = []
        for task in prepared_tasks:
            if task.identifier == "next-outcome":
                sheet.append(
                    {
                        "task": task.identifier,
                        "anchor": task.updated_at,
                        "current_anchor": task.updated_at,
                        "question": (
                            "Does the refreshed dependency still deserve priority?"
                            if refreshed
                            else "Should this dependency stay ahead of the third outcome?"
                        ),
                        "recommendation": (
                            "Keep the dependency visible and name the next local proof."
                        ),
                        "reasoning": (
                            "It gates the other prepared outcome without authorizing external "
                            "action."
                        ),
                        "choices": [
                            {
                                "answer": "Keep this first and name the local proof.",
                                "effect": "The dependent outcome stays explicitly behind it.",
                                "recommended": True,
                            },
                            {
                                "answer": "Move it behind the third outcome.",
                                "effect": "The authored order changes, but both remain open.",
                                "recommended": False,
                            },
                        ],
                        "dissent": (
                            "Deferring this again would preserve the blockage rather than the "
                            "option."
                        ),
                        "group": "One dependency chain",
                        "stale": False,
                    }
                )
            elif task.identifier == "third-outcome":
                sheet.append(
                    {
                        "task": task.identifier,
                        "anchor": task.updated_at,
                        "current_anchor": task.updated_at,
                        "question": "Should this remain behind the dependency?",
                        "recommendation": "Keep it open without pretending it is next.",
                        "reasoning": (
                            "It still matters, but the first prepared outcome controls its timing."
                        ),
                        "choices": [
                            {
                                "answer": "Keep it open behind the dependency.",
                                "effect": "No other record changes.",
                                "recommended": True,
                            },
                            {
                                "answer": "Make this the first prepared outcome.",
                                "effect": "The authored order changes.",
                                "recommended": False,
                            },
                        ],
                        "dissent": "",
                        "group": "One dependency chain",
                        "stale": False,
                    }
                )
            else:
                sheet.append(
                    {
                        "task": task.identifier,
                        "anchor": task.updated_at,
                        "current_anchor": task.updated_at,
                        "question": f"What should happen with {task.title}?",
                        "recommendation": (
                            f"Keep {task.title} open and name its next reversible move."
                        ),
                        "reasoning": (
                            "The outcome remains open, but its next move still needs an explicit "
                            "decision."
                        ),
                        "choices": [
                            {
                                "answer": f"Keep {task.title} moving.",
                                "effect": "Seld carries the next reversible move.",
                                "recommended": True,
                            },
                            {
                                "answer": f"Hold {task.title} for later.",
                                "effect": "The outcome stays open without becoming the next move.",
                                "recommended": False,
                            },
                        ],
                        "dissent": "",
                        "group": "Prepared decisions",
                        "stale": False,
                    }
                )
        self.sheets[event.event_id] = sheet
        self.receipts[event.event_id] = receipt
        self.answers[event.event_id] = headline
        return receipt


class NeverSubmittedTransport:
    def snapshot(
        self,
        event_id: str | None = None,
        *,
        include_capability: bool = True,
    ) -> dict[str, Any]:
        del event_id
        return {
            "automatic_resume": True,
            "automatic_start": True,
            "available": True,
            "enabled": True,
            "event": None,
            "reason_code": None,
        }

    def receipt(self, event_id: object) -> TurnReceipt | None:
        del event_id
        return None

    def submit(self, context: TurnContext) -> TurnReceipt:
        del context
        raise AssertionError("stale queue CAS must fail before transport submission")


class QueuedRecoveryTransport(NeverSubmittedTransport):
    """Prove a queued event can acquire its first receipt after Bridge restarts."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.submit_count = 0

    def submit(self, context: TurnContext) -> TurnReceipt:
        exact = context.validated()
        self.submit_count += 1
        now = format_time(datetime.now(UTC))
        return TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=exact.event_id,
            mode=exact.mode,
            state=TurnState.FAILED_SAFE,
            attempt=1,
            vault_id=str(self.vault.identity()["vault_id"]),
            context_hash=exact.context_hash,
            queue_revision=exact.queue_revision,
            target_revision=exact.target_revision,
            thread_id=exact.active_thread_id,
            owner_instance_id=None,
            decision=None,
            result_ref=None,
            canonical_revision=None,
            result_context_hash=None,
            reason_code="pre_dispatch_failure",
            created_at=now,
            updated_at=now,
        )


class ReceiptCapacityTransport(NeverSubmittedTransport):
    def submit(self, context: TurnContext) -> TurnReceipt:
        del context
        raise ReceiptCapacityError("guided-review transport receipt store is full")


class DeliveryUncertainTransport:
    """Expose an ambiguous delivery receipt without replaying the exact event."""

    def __init__(
        self,
        vault: Vault,
        *,
        receipts: dict[str, TurnReceipt] | None = None,
        thread_id: str | None = REVIEW_HAND_ID,
    ) -> None:
        self.vault = vault
        self.receipts = dict(receipts or {})
        self.thread_id = thread_id
        self.submit_count = 0

    def snapshot(
        self,
        event_id: str | None = None,
        *,
        include_capability: bool = True,
    ) -> dict[str, Any]:
        receipt = self.receipts.get(event_id) if event_id is not None else None
        return {
            "automatic_resume": True,
            "automatic_start": True,
            "available": True,
            "enabled": True,
            "event": receipt.public() if receipt is not None else None,
            "reason_code": None,
        }

    def receipt(self, event_id: object) -> TurnReceipt | None:
        return self.receipts.get(str(event_id))

    def submit(self, context: TurnContext) -> TurnReceipt:
        exact = context.validated()
        existing = self.receipts.get(exact.event_id)
        if existing is not None:
            return existing
        self.submit_count += 1
        now = format_time(datetime.now(UTC))
        receipt = TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=exact.event_id,
            mode=exact.mode,
            state=TurnState.DELIVERY_UNCERTAIN,
            attempt=1,
            vault_id=str(self.vault.identity()["vault_id"]),
            context_hash=exact.context_hash,
            queue_revision=exact.queue_revision,
            target_revision=exact.target_revision,
            thread_id=self.thread_id,
            owner_instance_id=None,
            decision=None,
            result_ref=None,
            canonical_revision=None,
            result_context_hash=None,
            reason_code="ambiguous_post_spawn_exit",
            created_at=now,
            updated_at=now,
        )
        self.receipts[exact.event_id] = receipt
        return receipt


class RunningTransport(DeliveryUncertainTransport):
    """Keep one receipt running until its queue event is dispositioned elsewhere."""

    def submit(self, context: TurnContext) -> TurnReceipt:
        exact = context.validated()
        existing = self.receipts.get(exact.event_id)
        if existing is not None:
            return existing
        self.submit_count += 1
        now = format_time(datetime.now(UTC))
        receipt = TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=exact.event_id,
            mode=exact.mode,
            state=TurnState.RUNNING,
            attempt=1,
            vault_id=str(self.vault.identity()["vault_id"]),
            context_hash=exact.context_hash,
            queue_revision=exact.queue_revision,
            target_revision=exact.target_revision,
            thread_id=self.thread_id,
            owner_instance_id=INSTANCE_ID,
            decision=None,
            result_ref=None,
            canonical_revision=None,
            result_context_hash=None,
            reason_code=None,
            created_at=now,
            updated_at=now,
        )
        self.receipts[exact.event_id] = receipt
        return receipt


class DispositionBeforeCompletionTransport(SemanticReviewTransport):
    """Expose the real disposition-before-process-exit completion window."""

    def __init__(self, vault: Vault) -> None:
        super().__init__(vault)
        self.completed: dict[str, TurnReceipt] = {}
        self.completion_released = threading.Event()

    def release_completion(self) -> None:
        self.completion_released.set()

    def submit(self, context: TurnContext) -> TurnReceipt:
        completed = super().submit(context)
        self.completed[completed.event_id] = completed
        running = replace(
            completed,
            state=TurnState.RUNNING,
            owner_instance_id=INSTANCE_ID,
            decision=None,
            result_ref=None,
            canonical_revision=None,
            result_context_hash=None,
            reason_code=None,
        )
        self.receipts[completed.event_id] = running
        return running

    def snapshot(
        self,
        event_id: str | None = None,
        *,
        include_capability: bool = True,
    ) -> dict[str, Any]:
        receipt = self.receipts.get(event_id) if event_id is not None else None
        if (
            receipt is not None
            and receipt.state is TurnState.RUNNING
            and self.completion_released.is_set()
        ):
            receipt = self.completed[receipt.event_id]
            self.receipts[receipt.event_id] = receipt
        return {
            "automatic_resume": True,
            "automatic_start": True,
            "available": True,
            "enabled": True,
            "event": (
                receipt.public(
                    final_answer=(
                        self.answers.get(receipt.event_id)
                        if receipt.state is TurnState.COMPLETED
                        else None
                    )
                )
                if receipt is not None
                else None
            ),
            "reason_code": None,
        }


class FailedSafeThenDriftTransport(DeliveryUncertainTransport):
    """Fail before delivery, then make the queued semantic context stale."""

    def submit(self, context: TurnContext) -> TurnReceipt:
        exact = context.validated()
        existing = self.receipts.get(exact.event_id)
        if existing is not None:
            return existing
        self.submit_count += 1
        now = format_time(datetime.now(UTC))
        receipt = TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=exact.event_id,
            mode=exact.mode,
            state=TurnState.FAILED_SAFE,
            attempt=1,
            vault_id=str(self.vault.identity()["vault_id"]),
            context_hash=exact.context_hash,
            queue_revision=exact.queue_revision,
            target_revision=exact.target_revision,
            thread_id=exact.active_thread_id,
            owner_instance_id=None,
            decision=None,
            result_ref=None,
            canonical_revision=None,
            result_context_hash=None,
            reason_code="pre_dispatch_failure",
            created_at=now,
            updated_at=now,
        )
        self.receipts[exact.event_id] = receipt
        assert exact.session_task_id is not None
        session = self.vault.get_task(exact.session_task_id)
        self.vault.update_task(
            session.identifier,
            expected_revision=session.revision,
            next_action="A concurrent semantic update changed this exact review step.",
        )
        return receipt


@pytest.mark.parametrize("mode", ("start", "resume"))
def test_browser_start_and_resume_stale_cas_notice_survives_refresh(
    tmp_path: Path,
    mode: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name=f"Guided review {mode} stale CAS")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep the start and resume controls honest across queue races.",
        status="ready",
        next_actor="human",
    )
    if mode == "resume":
        session = vault.create_task(
            identifier="review-session",
            title="Review every open outcome",
            outcome="Check every exact outcome.",
            status="waiting",
            next_actor="human",
            next_action="Resume at the exact current outcome.",
            waiting_on="Should this outcome remain current?",
            active_thread_id=REVIEW_HAND_ID,
            refs=(
                "review-scope:all-open",
                "review-state:paused",
                "review-subject:task:exact-outcome",
            ),
        )
        vault.create_thread(
            identifier="thread:life-portfolio-review",
            title="Finite Portfolio reviews",
            purpose="Own only bounded all-open review sessions.",
            summary="One exact review session is paused.",
            status="waiting",
            next_move="Resume the focused review session.",
            focus_task_id=session.identifier,
            task_ids=(session.identifier,),
        )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The review control must remain understandable after a race.",
            ),
        ),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=NeverSubmittedTransport(),
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="load")
                page.locator('[data-view="commitments"]').press("Enter")
                page.route(
                    "**/api/v1/control",
                    lambda route: route.fulfill(
                        body='{"error":"control queue changed; reload"}',
                        content_type="application/json",
                        status=409,
                    ),
                    times=1,
                )
                label = "Start review here" if mode == "start" else "Resume review here"
                page.get_by_role("button", name=label).click()

                notice = page.locator(".guided-review-notice")
                notice.wait_for(timeout=5_000)
                assert "queue changed" in notice.text_content().casefold()
                assert "retry" in notice.text_content().casefold()
                assert page.get_by_role("button", name=label).is_visible()
                ledger = OperationLedger(vault.root)
                assert ledger.snapshot().pending == ()
                current = ledger.snapshot()
                ledger.queue.append(
                    kind="correction",
                    subject="mind:user-correction",
                    choice="A separately reconciled local queue update.",
                    expected_revision=current.queue_revision,
                )
                page.locator("#retry-button").evaluate("button => button.click()")
                page.wait_for_function(
                    "() => !document.querySelector('.guided-review-notice')",
                    timeout=5_000,
                )
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_continues_one_saved_review_answer_when_receipt_is_missing(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Queued review receipt recovery")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep one saved answer recoverable across a Bridge crash window.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome without replaying an answer.",
        status="waiting",
        next_actor="human",
        next_action="Continue the exact saved answer.",
        waiting_on="Should this outcome remain current?",
        active_thread_id=REVIEW_HAND_ID,
        refs=("review-scope:all-open", "review-subject:task:exact-outcome"),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session has a saved answer.",
        status="active",
        next_move="Continue the already queued event.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The saved answer must remain recoverable without replay.",
            ),
        ),
    )
    ledger = OperationLedger(vault.root)
    current = ledger.snapshot()
    queued = ledger.queue.append(
        kind="correction",
        subject=f"record:task/{session.identifier}",
        choice="Keep this exact saved answer and do not append it again.",
        target_revision=session.revision,
        expected_revision=current.queue_revision,
    )
    event_id = queued.events[-1].event_id
    transport = QueuedRecoveryTransport(vault)
    static_resource = files("continuity_kernel") / "resources/bridge"

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")

                page.get_by_role("button", name="Continue saved answer").click()
                page.get_by_text("The turn did not start", exact=True).wait_for(timeout=5_000)
                assert transport.submit_count == 1
                assert [event.event_id for event in ledger.snapshot().pending] == [event_id]
                assert page.get_by_role("button", name="Retry this answer").is_visible()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_unavailable_review_shows_repair_issue_without_new_hand(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unavailable guided review repair")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep unavailable review state visible and fail closed.",
        status="ready",
        next_actor="human",
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The review needs an honest repair surface.",
            ),
        ),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=NeverSubmittedTransport(),
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        snapshot = server.snapshot()
        snapshot["portfolio"]["review"].update(
            {
                "issue": "Two guided review starts are queued; reconcile them before continuing.",
                "pending_start": None,
                "state": "unavailable",
            }
        )
        snapshot["guided_review_transport"]["event"] = {
            "created_at": "2026-07-26T06:00:00Z",
            "event_id": "019f95fd-009e-7603-ab87-f9927cf31c52",
            "final_answer": None,
            "mode": "start",
            "reason_code": "ambiguous_post_spawn_exit",
            "retryable": False,
            "state": "delivery_uncertain",
            "terminal": True,
            "thread_id": REVIEW_HAND_ID,
            "updated_at": "2026-07-26T06:01:00Z",
        }
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.route(
                    "**/api/v1/snapshot",
                    lambda route: route.fulfill(
                        body=json.dumps(snapshot),
                        content_type="application/json",
                        status=200,
                    ),
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="load")
                page.locator('[data-view="commitments"]').press("Enter")

                page.get_by_text("Review state needs repair", exact=True).wait_for(timeout=5_000)
                assert page.get_by_text(
                    "Two guided review starts are queued", exact=False
                ).is_visible()
                assert page.get_by_text("Delivery could not be confirmed", exact=True).is_visible()
                assert (
                    page.get_by_role("link", name="Open the same ChatGPT task").get_attribute(
                        "href"
                    )
                    == f"codex://threads/{REVIEW_HAND_ID}"
                )
                assert page.get_by_role("button", name="Start review here").count() == 0
                assert page.get_by_role("link", name="Start in ChatGPT").count() == 0

                snapshot["portfolio"]["review"].update(
                    {
                        "active_thread_id": None,
                        "issue": "The durable review session needs repair before it can continue.",
                        "start_url": "codex://new?prompt=repair-guided-review",
                        "state": "conflict",
                    }
                )
                snapshot["guided_review_transport"]["event"] = None
                page.reload(wait_until="load")
                page.get_by_role("link", name="Repair in ChatGPT").wait_for(timeout=5_000)
                assert page.get_by_role("link", name="Resume the review hand").count() == 0
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_finished_review_keeps_terminal_delivery_recovery_visible(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Finished review recovery")
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="No outcomes are currently open.",
        items=(),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    event_id = "019f95fd-009e-7603-ab87-f9927cf31c4f"

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=NeverSubmittedTransport(),
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        snapshot = server.snapshot()
        observed_at = format_time(datetime.now(UTC))
        snapshot["guided_review_transport"]["event"] = {
            "created_at": observed_at,
            "event_id": event_id,
            "final_answer": None,
            "mode": "resume",
            "reason_code": "interrupted_after_possible_spawn",
            "retryable": False,
            "state": "delivery_uncertain",
            "terminal": True,
            "thread_id": REVIEW_HAND_ID,
            "updated_at": observed_at,
        }
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.route(
                    "**/api/v1/snapshot",
                    lambda route: route.fulfill(
                        body=json.dumps(snapshot),
                        content_type="application/json",
                        status=200,
                    ),
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")

                page.get_by_text("Nothing open is waiting for review", exact=True).wait_for(
                    timeout=5_000
                )
                page.get_by_text("Delivery could not be confirmed", exact=True).wait_for(
                    timeout=5_000
                )
                assert (
                    page.get_by_role("link", name="Open the same ChatGPT task").get_attribute(
                        "href"
                    )
                    == f"codex://threads/{REVIEW_HAND_ID}"
                )
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_clears_stale_working_message_when_receipt_completes(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Guided review receipt progression")
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="No outcomes are currently open.",
        items=(),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    event_id = "019f95fd-009e-4603-ab87-f9927cf31c50"

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=NeverSubmittedTransport(),
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        snapshot = server.snapshot()
        running_receipt = {
            "created_at": "2026-07-26T06:00:00Z",
            "event_id": event_id,
            "final_answer": None,
            "mode": "resume",
            "reason_code": None,
            "retryable": False,
            "state": "running",
            "terminal": False,
            "thread_id": REVIEW_HAND_ID,
            "updated_at": "2026-07-26T06:00:00Z",
        }
        snapshot["guided_review_transport"]["event"] = {
            **running_receipt,
            "state": "pending",
        }
        stale_message = (
            "The review task is still working. Seld will not resend your answer; open the same "
            "ChatGPT task if you need to inspect it now."
        )
        bridge_javascript = (Path(static_root) / "bridge.js").read_text(encoding="utf-8")
        fast_javascript = (
            bridge_javascript.replace(
                "const GUIDED_REVIEW_POLL_INTERVAL_MS = 750;",
                "const GUIDED_REVIEW_POLL_INTERVAL_MS = 10;",
            )
            .replace(
                "const GUIDED_REVIEW_POLL_LIMIT = 680;",
                "const GUIDED_REVIEW_POLL_LIMIT = 100;",
            )
            .replace(
                "window.setInterval(() => loadSnapshot({ quiet: true }), 10_000);",
                "window.setInterval(() => loadSnapshot({ quiet: true }), 50);",
            )
        )
        assert fast_javascript != bridge_javascript
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(
                    reduced_motion="reduce",
                    viewport={"height": 844, "width": 390},
                )
                page.route(
                    "**/bridge.js",
                    lambda route: route.fulfill(
                        body=fast_javascript,
                        content_type="text/javascript",
                        status=200,
                    ),
                )
                page.route(
                    "**/api/v1/snapshot",
                    lambda route: route.fulfill(
                        body=json.dumps(snapshot),
                        content_type="application/json",
                        status=200,
                    ),
                )
                page.route(
                    "**/api/v1/review-turn*",
                    lambda route: route.fulfill(
                        body=json.dumps({"ok": True, "transport": running_receipt}),
                        content_type="application/json",
                        status=202 if route.request.method == "POST" else 200,
                    ),
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="load")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_text("Seld is working", exact=True).wait_for(timeout=5_000)
                page.evaluate(
                    "window.__gsvReviewStatus = "
                    "document.querySelector('.guided-review-delivery-copy')"
                )
                page.wait_for_timeout(100)
                assert page.evaluate(
                    "window.__gsvReviewStatus === "
                    "document.querySelector('.guided-review-delivery-copy')"
                )
                page.get_by_text(stale_message, exact=True).wait_for(timeout=5_000)
                assert page.get_by_text("Same ChatGPT task active", exact=False).is_visible()
                assert page.get_by_text("Checked just now", exact=False).is_visible()
                assert (
                    page.get_by_role("link", name="Open the same ChatGPT task").get_attribute(
                        "href"
                    )
                    == f"codex://threads/{REVIEW_HAND_ID}"
                )
                snapshot["guided_review_transport"]["event"] = {
                    **running_receipt,
                    "final_answer": "The exact turn finished.",
                    "state": "completed",
                    "terminal": True,
                    "updated_at": "2026-07-26T06:01:00Z",
                }

                page.get_by_text("ChatGPT replied", exact=True).wait_for(timeout=5_000)
                assert page.get_by_text(stale_message, exact=True).count() == 0
                assert page.get_by_text("The exact turn finished.", exact=True).is_visible()
                assert page.locator(".guided-review-delivery-activity").count() == 0
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_failed_receipt_read_removes_stale_hand_liveness(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Guided review failed receipt observation")
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="No outcomes are currently open.",
        items=(),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    event_id = "019f95fd-009e-7603-ab87-f9927cf31c51"
    pending_receipt = {
        "created_at": "2026-07-26T06:00:00Z",
        "event_id": event_id,
        "final_answer": None,
        "mode": "resume",
        "reason_code": None,
        "retryable": False,
        "state": "pending",
        "terminal": False,
        "thread_id": REVIEW_HAND_ID,
        "updated_at": "2026-07-26T06:00:00Z",
    }
    running_receipt = {**pending_receipt, "state": "running"}

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=NeverSubmittedTransport(),
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        snapshot = server.snapshot()
        snapshot["portfolio"]["review"]["pending_intent"] = {"event_id": event_id}
        snapshot["guided_review_transport"]["event"] = pending_receipt
        bridge_javascript = (Path(static_root) / "bridge.js").read_text(encoding="utf-8")
        fast_javascript = bridge_javascript.replace(
            "const GUIDED_REVIEW_POLL_INTERVAL_MS = 750;",
            "const GUIDED_REVIEW_POLL_INTERVAL_MS = 50;",
        )
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(
                    reduced_motion="reduce",
                    viewport={"height": 844, "width": 390},
                )
                page.route(
                    "**/bridge.js",
                    lambda route: route.fulfill(
                        body=fast_javascript,
                        content_type="text/javascript",
                        status=200,
                    ),
                )
                page.route(
                    "**/api/v1/snapshot",
                    lambda route: route.fulfill(
                        body=json.dumps(snapshot),
                        content_type="application/json",
                        status=200,
                    ),
                )

                receipt_reads = 0

                def review_turn(route: Any) -> None:
                    nonlocal receipt_reads
                    if route.request.method == "POST":
                        route.fulfill(
                            body=json.dumps({"ok": True, "transport": running_receipt}),
                            content_type="application/json",
                            status=202,
                        )
                    elif receipt_reads < 10:
                        # Keep the positive observation visible long enough for the
                        # assertion, then fail the same exact read path. This proves
                        # both transitions without racing Playwright's locator poll.
                        receipt_reads += 1
                        route.fulfill(
                            body=json.dumps({"ok": True, "transport": running_receipt}),
                            content_type="application/json",
                            status=200,
                        )
                    else:
                        route.fulfill(
                            body='{"error":"receipt temporarily unavailable"}',
                            content_type="application/json",
                            status=503,
                        )

                page.route("**/api/v1/review-turn*", review_turn)
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="load")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_text("Same ChatGPT task active", exact=False).wait_for(timeout=5_000)
                page.get_by_text("Bridge could not read the delivery record", exact=False).wait_for(
                    timeout=5_000
                )
                assert page.get_by_text("Same ChatGPT task active", exact=False).count() == 0
                page.wait_for_function(
                    "() => document.querySelector('.guided-review-delivery-activity-copy')"
                    "?.textContent.includes(' ago')",
                    timeout=8_000,
                )
                activity = page.locator(".guided-review-delivery-activity-copy").text_content()
                assert "Checked just now" not in activity
                assert " ago" in activity
                assert (
                    page.get_by_role("link", name="Open the same ChatGPT task").get_attribute(
                        "href"
                    )
                    == f"codex://threads/{REVIEW_HAND_ID}"
                )
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_guided_review_runs_semantic_cas_and_survives_fresh_process(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Guided review proof")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Decide its current place without inventing completion.",
        status="ready",
        next_actor="human",
        rank=7,
    )
    owner = vault.create_thread(
        identifier="thread:exact-work",
        title="Exact work storyline",
        purpose="Carry the exact outcome context.",
        summary="The original outcome context is current.",
        status="active",
        next_move="Choose the bounded next move.",
        task_ids=(outcome.identifier,),
    )
    second = vault.create_task(
        identifier="next-outcome",
        title="Next outcome",
        outcome="Decide whether the second boundary still matters.",
        status="ready",
        next_actor="human",
        rank=9,
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Keep it current if the evidence still holds.",
        waiting_on="Does this outcome still earn its place?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            "review-subject:task:exact-outcome",
            review_option_ref(
                intent="act-next",
                subject_task_id="exact-outcome",
                consequence="Author the smallest local next move and keep external action gated.",
            ),
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="Two exact open outcomes.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The user should decide deliberately.",
                work_thread_id=owner.identifier,
                work_thread_revision=owner.revision,
            ),
            portfolio_item(
                task_id_value=second.identifier,
                task_revision=second.revision,
                stance="needs-human",
                reason="The user should decide its boundary deliberately.",
            ),
        ),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    ledger = OperationLedger(vault.root)
    transport = SemanticReviewTransport(vault)

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")

                page.get_by_role("heading", name="Review the decisions that need you").wait_for()
                assert (
                    page.locator("article.guided-review-subject")
                    .get_by_role("heading", name="Exact outcome")
                    .is_visible()
                )
                assert page.get_by_text("Rank 7", exact=True).is_visible()
                assert page.get_by_text(
                    "A checked item may still be unresolved.", exact=False
                ).is_visible()
                assert page.locator("body").evaluate(
                    "element => element.scrollWidth <= document.documentElement.clientWidth"
                )

                page.get_by_role("button", name="Do / next").evaluate(
                    "button => { button.click(); button.click(); }"
                )
                page.get_by_text("ChatGPT replied", exact=True).wait_for(timeout=5_000)
                page.get_by_text(
                    "I tightened the first next move and kept external action gated.",
                    exact=False,
                ).wait_for(timeout=5_000)
                page.locator("#guided-review-current-title").get_by_text(
                    "Next outcome", exact=True
                ).wait_for(timeout=5_000)
                assert transport.submit_count == 1
                assert (
                    vault.get_task(outcome.identifier).next_action
                    == "Prepare the smallest local proof before asking for approval."
                )
                assert (
                    vault.get_thread(owner.identifier).next_move
                    == "Carry the bounded local proof and keep external action gated."
                )
                applied = ledger.snapshot()
                assert len(applied.pending) == 0
                assert len(applied.decided) == 1
                assert applied.decided[0][1].decision.value == "accepted"
                assert page.get_by_text("1 checked on current evidence", exact=False).is_visible()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()

        fresh_transport = SemanticReviewTransport(vault)
        fresh_server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id="e" * 32,
            turn_transport=fresh_transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        fresh_thread = threading.Thread(target=fresh_server.serve_forever, daemon=True)
        fresh_thread.start()
        fresh_base = f"http://127.0.0.1:{fresh_server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{fresh_base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")
                page.locator("#guided-review-current-title").get_by_text(
                    "Next outcome", exact=True
                ).wait_for(timeout=5_000)
                assert page.get_by_text("1 checked on current evidence", exact=False).is_visible()
                assert not page.get_by_text(
                    "I tightened the first next move", exact=False
                ).is_visible()
                system = page.locator('[data-view="system"]')
                system.focus()
                system.press("Enter")
                page.get_by_text("Acknowledged, not applied", exact=True).wait_for(timeout=5_000)
                assert page.get_by_text("semantic-readback-complete", exact=True).count() == 0
                browser.close()
        finally:
            fresh_server.shutdown()
            fresh_server.server_close()
            fresh_thread.join(timeout=5)
            assert not fresh_thread.is_alive()


def test_browser_prepared_board_batches_only_answered_rows_and_clears_on_new_receipt(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Prepared intervention board")
    first = vault.create_task(
        identifier="exact-outcome",
        title="Opening outcome",
        outcome="Open the intervention-driven review.",
        status="ready",
        next_actor="human",
    )
    second = vault.create_task(
        identifier="next-outcome",
        title="Dependency decision",
        outcome="Choose whether this stays ahead of the dependent outcome.",
        status="ready",
        next_actor="human",
    )
    third = vault.create_task(
        identifier="third-outcome",
        title="Dependent decision",
        outcome="Choose its place without pretending it is resolved.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Surface only consequential interventions.",
        status="waiting",
        next_actor="human",
        next_action="Open the first exact review exchange.",
        waiting_on="Should the intervention review begin?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            "review-subject:task:exact-outcome",
            review_option_ref(
                intent="act-next",
                subject_task_id="exact-outcome",
                consequence="Prepare only the consequential decisions that need me.",
            ),
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="Three exact open outcomes.",
        items=tuple(
            portfolio_item(
                task_id_value=task.identifier,
                task_revision=task.revision,
                stance="needs-human",
                reason="The prepared board must preserve this exact decision boundary.",
            )
            for task in (first, second, third)
        ),
    )
    transport = PreparedBoardTransport(vault)
    static_resource = files("continuity_kernel") / "resources/bridge"
    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_role("button", name="Do / next").click()
                page.get_by_role("heading", name="Review the prepared decisions").wait_for(
                    timeout=5_000
                )

                assert page.get_by_text("Why Seld disagrees", exact=True).is_visible()
                assert page.get_by_role("heading", name="Open work", exact=True).count() == 0
                assert page.get_by_label("Tell Seld something about this set").is_visible()
                assert page.get_by_role("button", name="Pause here", exact=True).is_visible()
                assert page.get_by_role("button", name="End review", exact=True).is_visible()
                assert page.locator("body").evaluate(
                    "element => element.scrollWidth <= document.documentElement.clientWidth"
                )

                page.get_by_role("button", name="Edit batch", exact=True).click()
                opening_box = page.get_by_role("checkbox", name="Opening outcome", exact=False)
                dependency_box = page.get_by_role(
                    "checkbox", name="Dependency decision", exact=False
                )
                dependent_box = page.get_by_role("checkbox", name="Dependent decision", exact=False)
                assert not opening_box.is_checked()
                assert dependency_box.is_checked()
                assert dependent_box.is_checked()
                dependent_box.uncheck()
                assert (
                    page.evaluate("document.activeElement.id")
                    == "guided-review-batch-third-outcome"
                )
                opening_box.check()
                assert (
                    page.evaluate("document.activeElement.id")
                    == "guided-review-batch-exact-outcome"
                )
                pulled: list[str] = []

                def fail_batch_pull(route: Any) -> None:
                    payload = route.request.post_data_json
                    pulled.append(payload["choice"])
                    route.fulfill(
                        body='{"error":"injected batch-editor storage failure"}',
                        content_type="application/json",
                        status=503,
                    )

                page.route("**/api/v1/control", fail_batch_pull, times=1)
                page.get_by_role("button", name="Prepare these 2", exact=True).click()
                page.get_by_text("injected batch-editor storage failure", exact=True).wait_for()
                assert opening_box.is_checked()
                assert dependency_box.is_checked()
                assert not dependent_box.is_checked()
                assert len(pulled) == 1
                assert "task:exact-outcome" in pulled[0]
                assert "task:next-outcome" in pulled[0]
                assert "task:third-outcome" not in pulled[0]
                assert "navigation request only" in pulled[0]
                assert "add no review coverage" in pulled[0]
                page.get_by_role("button", name="Close batch editor", exact=True).click()

                session_controls: list[str] = []

                def fail_session_control(route: Any) -> None:
                    payload = route.request.post_data_json
                    session_controls.append(payload["choice"])
                    route.fulfill(
                        body='{"error":"injected prepared-session storage failure"}',
                        content_type="application/json",
                        status=503,
                    )

                page.route("**/api/v1/control", fail_session_control, times=3)
                session_note = page.get_by_label("Tell Seld something about this set")
                session_note.fill("The dependency changed; reassess only what this affects.")
                page.get_by_role("button", name="Send note and keep going", exact=True).click()
                assert len(session_controls) == 1
                assert "task:next-outcome, task:third-outcome" in session_controls[0]
                assert "Do not infer a decision for an unanswered row" in session_controls[0]
                assert session_note.input_value().startswith("The dependency changed")
                page.get_by_role("button", name="Pause here", exact=True).click()
                assert len(session_controls) == 2
                assert "Do not change or cover any outcome" in session_controls[1]
                page.get_by_role("button", name="End review", exact=True).click()
                assert len(session_controls) == 3
                assert "unchecked outcomes remain open" in session_controls[2]

                page.get_by_role("button", name="Pause here", exact=True).click()
                resume = page.get_by_role("button", name="Resume review here", exact=True)
                resume.wait_for(timeout=5_000)
                assert page.locator(".guided-review-prepared-form button:enabled").count() == 0
                assert resume.is_enabled()
                resume.click()
                enabled_choices = page.locator(".guided-review-prepared-choice:enabled")
                enabled_choices.first.wait_for(timeout=5_000)
                assert enabled_choices.count() >= 2

                first_card = page.locator('[data-prepared-task="next-outcome"]')
                first_card.get_by_role(
                    "button", name="Keep this first and name the local proof.", exact=False
                ).click()
                assert first_card.locator('[aria-pressed="true"]').count() == 1

                page.get_by_role("button", name="See everything").click()
                page.locator(".commitment-grid").wait_for()
                assert page.locator(".task-card").count() >= 3
                page.locator('[data-view="commitments"]').press("Enter")
                first_card = page.locator('[data-prepared-task="next-outcome"]')
                assert first_card.locator('[aria-pressed="true"]').count() == 1

                first_card.focus()
                first_card.press("2")
                assert (
                    page.evaluate("document.activeElement?.dataset?.preparedTask") == "next-outcome"
                )
                assert (
                    first_card.get_by_role(
                        "button", name="Move it behind the third outcome.", exact=False
                    ).get_attribute("aria-pressed")
                    == "true"
                )
                page.keyboard.press("1")
                assert (
                    page.evaluate("document.activeElement?.dataset?.preparedTask") == "next-outcome"
                )
                page.route(
                    "**/api/v1/control",
                    lambda route: route.fulfill(
                        body='{"error":"injected prepared-board storage failure"}',
                        content_type="application/json",
                        status=503,
                    ),
                    times=1,
                )
                page.get_by_role("button", name="Send answered decisions").click()
                page.get_by_text("injected prepared-board storage failure", exact=True).wait_for()
                assert first_card.locator('[aria-pressed="true"]').count() == 1
                assert page.get_by_role("button", name="Send answered decisions").is_enabled()
                page.get_by_role("button", name="Send answered decisions").click()

                page.get_by_text(
                    "Does the refreshed dependency still deserve priority?", exact=True
                ).wait_for(timeout=5_000)
                assert len(transport.batch_choices) == 1
                assert "task:next-outcome" in transport.batch_choices[0]
                assert "Keep this first and name the local proof." in transport.batch_choices[0]
                assert "task:third-outcome" not in transport.batch_choices[0]
                assert (
                    page.locator('.guided-review-prepared-choice[aria-pressed="true"]').count() == 0
                )
                recovered_answer = (
                    "Keep the dependency visible, but do not move the dependent outcome yet."
                )
                refreshed_card = page.locator('[data-prepared-task="next-outcome"]')
                refreshed_card.get_by_label("Or answer in your own words").fill(recovered_answer)

                def stale_prepared_session(route: Any) -> None:
                    current_session = vault.get_task("review-session")
                    vault.update_task(
                        current_session.identifier,
                        expected_revision=current_session.revision,
                        next_action="Out-of-band session change invalidates the retained sheet.",
                    )
                    route.fulfill(
                        body='{"error":"review session changed; reload"}',
                        content_type="application/json",
                        status=409,
                    )

                page.route("**/api/v1/control", stale_prepared_session, times=1)
                page.get_by_role("button", name="Send answered decisions").click()
                page.get_by_text("This prepared set is no longer current", exact=True).wait_for(
                    timeout=5_000
                )
                recovery = page.get_by_label(
                    "Unsent prepared answers from the previous set", exact=True
                )
                assert recovered_answer in recovery.input_value()
                assert "task:next-outcome" in recovery.input_value()
                assert page.get_by_text(
                    "will not attach it to the refreshed set", exact=False
                ).is_visible()
                notice = page.locator(".guided-review-notice")
                assert "queue changed" in notice.text_content().casefold()
                assert "retry" in notice.text_content().casefold()
                assert (
                    page.get_by_role(
                        "heading", name="Review the prepared decisions", exact=True
                    ).count()
                    == 0
                )
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_ten_decision_rundown_fits_mobile_and_submits_only_answered_row(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Ten-decision mobile Rundown")
    first = vault.create_task(
        identifier="exact-outcome",
        title="Opening outcome",
        outcome="Open the prepared Rundown.",
        status="ready",
        next_actor="human",
    )
    prepared = [
        vault.create_task(
            identifier=identifier,
            title=title,
            outcome=f"Choose the bounded next move for {title}.",
            status="ready",
            next_actor="human",
        )
        for identifier, title in (
            ("next-outcome", "Dependency decision"),
            ("third-outcome", "Dependent decision"),
            ("decision-04", "Decision 04"),
            ("decision-05", "Decision 05"),
            ("decision-06", "Decision 06"),
            ("decision-07", "Decision 07"),
            ("decision-08", "Decision 08"),
            ("decision-09", "Decision 09"),
            ("decision-10", "Decision 10"),
            ("decision-11", "Decision 11"),
        )
    ]
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Surface only consequential interventions.",
        status="waiting",
        next_actor="human",
        next_action="Open the first exact review exchange.",
        waiting_on="Should the intervention review begin?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            "review-subject:task:exact-outcome",
            review_option_ref(
                intent="act-next",
                subject_task_id="exact-outcome",
                consequence="Prepare the decisions that need me.",
            ),
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="Eleven exact open outcomes.",
        items=tuple(
            portfolio_item(
                task_id_value=task.identifier,
                task_revision=task.revision,
                stance="needs-human",
                reason="The prepared board must preserve this exact decision boundary.",
            )
            for task in (first, *prepared)
        ),
    )
    transport = PreparedBoardTransport(vault)
    static_resource = files("continuity_kernel") / "resources/bridge"
    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(
                    reduced_motion="reduce",
                    viewport={"height": 844, "width": 390},
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_role("button", name="Do / next").click()
                page.get_by_role("heading", name="Review the prepared decisions").wait_for(
                    timeout=5_000
                )
                cards = page.locator("[data-prepared-task]")
                assert cards.count() == 10
                assert page.evaluate(
                    "document.body.scrollWidth <= window.innerWidth && "
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                last = page.locator('[data-prepared-task="decision-11"]')
                last.scroll_into_view_if_needed()
                rect = last.evaluate(
                    "element => ({ bottom: element.getBoundingClientRect().bottom, "
                    "top: element.getBoundingClientRect().top, viewport: window.innerHeight })"
                )
                assert rect["top"] >= 0
                assert rect["bottom"] <= rect["viewport"]
                last.get_by_role("button", name="Keep Decision 11 moving.", exact=False).click()
                payloads: list[str] = []

                def capture_last_row(route: Any) -> None:
                    payloads.append(route.request.post_data_json["choice"])
                    route.fulfill(
                        body='{"error":"captured mobile row"}',
                        content_type="application/json",
                        status=503,
                    )

                page.route("**/api/v1/control", capture_last_row, times=1)
                page.get_by_role("button", name="Send answered decisions").click()
                page.get_by_text("captured mobile row", exact=True).wait_for(timeout=5_000)
                assert len(payloads) == 1
                assert "task:decision-11" in payloads[0]
                assert "Keep Decision 11 moving." in payloads[0]
                assert all(f"task:{task.identifier}" not in payloads[0] for task in prepared[:-1])
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_keeps_active_delivery_until_post_disposition_completion(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Guided review disposition window")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep the wait card through the disposition-to-completion window.",
        status="ready",
        next_actor="human",
    )
    owner = vault.create_thread(
        identifier="thread:exact-work",
        title="Exact work storyline",
        purpose="Carry the exact outcome context.",
        summary="The exact outcome context is current.",
        status="active",
        next_move="Choose the bounded next move.",
        task_ids=(outcome.identifier,),
    )
    second = vault.create_task(
        identifier="next-outcome",
        title="Next outcome",
        outcome="Remain available after the exact turn completes.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Keep it current if the evidence still holds.",
        waiting_on="Does this outcome still earn its place?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            "review-subject:task:exact-outcome",
            review_option_ref(
                intent="act-next",
                subject_task_id="exact-outcome",
                consequence="Author the smallest local next move and keep external action gated.",
            ),
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="Two exact open outcomes.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The user should decide deliberately.",
                work_thread_id=owner.identifier,
                work_thread_revision=owner.revision,
            ),
            portfolio_item(
                task_id_value=second.identifier,
                task_revision=second.revision,
                stance="needs-human",
                reason="The next exact outcome remains open.",
            ),
        ),
    )
    transport = DispositionBeforeCompletionTransport(vault)
    ledger = OperationLedger(vault.root)
    static_resource = files("continuity_kernel") / "resources/bridge"

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        bridge_javascript = (Path(static_root) / "bridge.js").read_text(encoding="utf-8")
        fast_javascript = (
            bridge_javascript.replace(
                "const GUIDED_REVIEW_POLL_INTERVAL_MS = 750;",
                "const GUIDED_REVIEW_POLL_INTERVAL_MS = 100;",
            )
            + "\nglobalThis.__seldTestLoadSnapshot = loadSnapshot;\n"
        )
        assert fast_javascript != bridge_javascript
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.route(
                    "**/bridge.js",
                    lambda route: route.fulfill(
                        body=fast_javascript,
                        content_type="text/javascript",
                        status=200,
                    ),
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="load")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_role("button", name="Do / next").click()
                page.get_by_text("Seld is working", exact=True).wait_for(timeout=5_000)

                decided = ledger.snapshot()
                assert decided.pending == ()
                assert len(decided.decided) == 1

                # Exercise the real snapshot where the queue is dispositioned
                # but the same Codex hand has not finished yet.
                assert (
                    page.evaluate("() => globalThis.__seldTestLoadSnapshot({ quiet: true })")
                    is True
                )
                assert page.get_by_text("Seld is working", exact=True).is_visible()

                transport.release_completion()
                page.get_by_text("ChatGPT replied", exact=True).wait_for(timeout=5_000)
                assert page.get_by_text(
                    "I tightened the first next move and kept external action gated.",
                    exact=False,
                ).is_visible()
                assert transport.submit_count == 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_fresh_process_resumes_one_restored_pending_receipt(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Restored pending review proof")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Resume one already-authorized pending answer after restart.",
        status="ready",
        next_actor="human",
    )
    owner = vault.create_thread(
        identifier="thread:exact-work",
        title="Exact work storyline",
        purpose="Carry the exact outcome context.",
        summary="The current owner relation is exact.",
        status="active",
        next_move="Choose the bounded next move.",
        task_ids=(outcome.identifier,),
    )
    second = vault.create_task(
        identifier="next-outcome",
        title="Next outcome",
        outcome="Remain available after the restored turn advances.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Keep it current if the evidence still holds.",
        waiting_on="Does this outcome still earn its place?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            "review-subject:task:exact-outcome",
            review_option_ref(
                intent="act-next",
                subject_task_id="exact-outcome",
                consequence="Author the smallest local next move and keep external action gated.",
            ),
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="Two exact open outcomes.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The restored answer should advance this exact outcome.",
                work_thread_id=owner.identifier,
                work_thread_revision=owner.revision,
            ),
            portfolio_item(
                task_id_value=second.identifier,
                task_revision=second.revision,
                stance="needs-human",
                reason="The second exact outcome remains next.",
            ),
        ),
    )
    ledger = OperationLedger(vault.root)
    pre_append_snapshot = bridge_module.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )
    queued = ledger.queue.append(
        kind="correction",
        subject=f"record:task/{session.identifier}",
        choice="Author the smallest local next move and keep external action gated.",
        expected_revision=ledger.snapshot().queue_revision,
        target_revision=session.revision,
    )
    event = queued.events[-1]
    context = bridge_module._guided_review_turn_context(
        pre_append_snapshot,
        event=event,
        queue_revision=queued.revision,
    )
    assert context is not None
    previous_process = SemanticReviewTransport(vault)
    previous_process.seed_pending(context)
    transport = SemanticReviewTransport(vault, receipts=previous_process.receipts)
    review_posts: list[str] = []
    static_resource = files("continuity_kernel") / "resources/bridge"

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.on(
                    "request",
                    lambda request: (
                        review_posts.append(request.url)
                        if request.method == "POST" and request.url.endswith("/api/v1/review-turn")
                        else None
                    ),
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                for _ in range(20):
                    if transport.submit_count == 1:
                        break
                    page.wait_for_timeout(50)
                assert transport.submit_count == 1
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")
                page.get_by_text("ChatGPT replied", exact=True).wait_for(timeout=5_000)
                page.locator("#guided-review-current-title").get_by_text(
                    "Next outcome", exact=True
                ).wait_for(timeout=5_000)
                assert review_posts == [f"{base}/api/v1/review-turn"]
                applied = ledger.snapshot()
                assert len(applied.pending) == 0
                assert len(applied.decided) == 1
                assert applied.decided[0][0].event_id == event.event_id
                assert applied.decided[0][1].decision.value == "accepted"
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("race_kind", ("queue", "session", "conflict"))
def test_browser_guided_review_preserves_unsent_draft_after_stale_cas(
    tmp_path: Path,
    race_kind: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Guided review stale queue proof")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep one exact answer visible across a queue race.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Ask the one useful question.",
        waiting_on="What should change about this outcome?",
        active_thread_id=REVIEW_HAND_ID,
        refs=("review-scope:all-open", "review-subject:task:exact-outcome"),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The answer must remain explicit.",
            ),
        ),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    ledger = OperationLedger(vault.root)
    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=NeverSubmittedTransport(),
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")
                if race_kind == "queue":
                    oversized = "🧠" * 1024
                    page.get_by_label("Tell Seld what you want").fill(oversized)
                    page.get_by_role("button", name="Send and keep going").click()
                    page.get_by_text(
                        "longer than the 4,096-byte local limit", exact=False
                    ).wait_for(timeout=5_000)
                    assert page.locator("#guided-review-answer").input_value() == oversized
                    assert len(ledger.snapshot().pending) == 0
                    page.wait_for_function(
                        "() => !document.querySelector("
                        "'.guided-review-form button.primary-action').disabled"
                    )
                draft = "Move this below maintenance, but keep the outcome open."
                page.get_by_label("Tell Seld what you want").fill(draft)
                if race_kind == "queue":
                    ledger.queue.append(
                        kind="correction",
                        subject="record:task/review-session",
                        choice="A concurrent exact answer won the queue CAS.",
                        expected_revision=ledger.snapshot().queue_revision,
                        target_revision=session.revision,
                    )
                elif race_kind == "session":
                    vault.update_task(
                        session.identifier,
                        expected_revision=session.revision,
                        next_action="A concurrent semantic update changed this review step.",
                    )
                else:
                    review_thread = vault.get_thread("thread:life-portfolio-review")
                    vault.update_thread(
                        review_thread.identifier,
                        expected_revision=review_thread.revision,
                        clear_focus_task=True,
                    )
                page.get_by_role("button", name="Send and keep going").click()
                conflict_text = "The review queue changed while you were answering."
                page.get_by_text(conflict_text, exact=False).wait_for(timeout=5_000)
                assert page.locator("#guided-review-answer").input_value() == draft
                assert (
                    "retry"
                    in page.get_by_text(conflict_text, exact=False).text_content().casefold()
                )
                assert vault.get_task(outcome.identifier).revision == outcome.revision
                assert len(ledger.snapshot().pending) == (1 if race_kind == "queue" else 0)
                if race_kind == "conflict":
                    assert (
                        page.get_by_role("link", name="Open the same ChatGPT task").get_attribute(
                            "href"
                        )
                        == f"codex://threads/{REVIEW_HAND_ID}"
                    )
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_failed_safe_retry_reports_semantic_drift_without_replay(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Guided review failed-safe drift")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep one exact answer durable when semantic context changes.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Ask the one useful question.",
        waiting_on="What should change about this outcome?",
        active_thread_id=REVIEW_HAND_ID,
        refs=("review-scope:all-open", "review-subject:task:exact-outcome"),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The answer must remain singular and durable.",
            ),
        ),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    ledger = OperationLedger(vault.root)
    transport = FailedSafeThenDriftTransport(vault)

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")
                answer = "Move this below maintenance, but keep it open."
                page.get_by_label("Tell Seld what you want").fill(answer)
                page.get_by_role("button", name="Send and keep going").click()
                page.get_by_text(
                    "The review changed after this answer was saved.", exact=True
                ).wait_for(timeout=5_000)

                pending = ledger.snapshot().pending
                assert len(pending) == 1
                assert answer in pending[0].choice
                assert transport.submit_count == 1
                assert page.get_by_text("wording remains stored once", exact=False).is_visible()
                assert page.get_by_text("Bridge could not read", exact=False).count() == 0
                assert (
                    page.get_by_role("link", name="Open the same ChatGPT task").get_attribute(
                        "href"
                    )
                    == f"codex://threads/{REVIEW_HAND_ID}"
                )
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_guided_review_never_replays_delivery_uncertain_event(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Guided review ambiguous delivery proof")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep the exact answer singular when delivery is ambiguous.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Keep the current outcome unless the evidence changes.",
        waiting_on="Does this still earn its place?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            "review-subject:task:exact-outcome",
            review_option_ref(
                intent="keep",
                subject_task_id="exact-outcome",
                consequence="Leave the canonical outcome unchanged while checking this step.",
            ),
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
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
    static_resource = files("continuity_kernel") / "resources/bridge"
    ledger = OperationLedger(vault.root)
    transport = DeliveryUncertainTransport(vault)

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")
                page.get_by_role("button", name="Keep current", exact=False).evaluate(
                    "button => { button.click(); button.click(); }"
                )
                page.get_by_text("Delivery could not be confirmed", exact=True).wait_for(
                    timeout=5_000
                )
                assert transport.submit_count == 1
                assert vault.get_task(outcome.identifier).revision == outcome.revision
                assert len(ledger.snapshot().pending) == 1
                assert not page.get_by_role("button", name="Retry this answer").is_visible()
                hand = page.get_by_role("link", name="Open the same ChatGPT task")
                assert hand.get_attribute("href") == f"codex://threads/{REVIEW_HAND_ID}"
                assert (
                    page.get_by_role("link", name="Resolve saved answer")
                    .get_attribute("href")
                    .startswith("codex://new?")
                )
                assert page.get_by_text("Copy prompt instead", exact=True).count() >= 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()

        restarted_transport = DeliveryUncertainTransport(
            vault,
            receipts=transport.receipts,
        )
        restarted_server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id="d" * 32,
            turn_transport=restarted_transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        restarted_thread = threading.Thread(target=restarted_server.serve_forever, daemon=True)
        restarted_thread.start()
        restarted_base = f"http://127.0.0.1:{restarted_server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{restarted_base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")
                page.get_by_text("Delivery could not be confirmed", exact=True).wait_for(
                    timeout=5_000
                )
                assert restarted_transport.submit_count == 0
                assert len(ledger.snapshot().pending) == 1
                assert not page.get_by_role("button", name="Retry this answer").is_visible()
                assert (
                    page.get_by_role("link", name="Open the same ChatGPT task").get_attribute(
                        "href"
                    )
                    == f"codex://threads/{REVIEW_HAND_ID}"
                )
                assert (
                    page.get_by_role("link", name="Resolve saved answer")
                    .get_attribute("href")
                    .startswith("codex://new?")
                )
                pending = ledger.snapshot()
                assert len(pending.pending) == 1
                ledger.decide(
                    event_id=pending.pending[0].event_id,
                    decision="rejected",
                    actor_ref="core:doctor",
                    reason_code="resolved-outside-bridge",
                    expected_queue_revision=pending.queue_revision,
                    expected_disposition_revision=pending.disposition_revision,
                )
                page.get_by_label("Tell Seld what you want").wait_for(timeout=12_000)
                assert page.get_by_text("Delivery could not be confirmed", exact=True).count() == 0
                assert restarted_transport.submit_count == 0
                browser.close()
        finally:
            restarted_server.shutdown()
            restarted_server.server_close()
            restarted_thread.join(timeout=5)
            assert not restarted_thread.is_alive()


def test_browser_start_delivery_uncertain_uses_created_exact_hand(
    tmp_path: Path,
) -> None:
    assert REVIEW_HAND_ID.split("-")[2].startswith("7")
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Ambiguous review start proof")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Start one finite review without duplicating the created hand.",
        status="ready",
        next_actor="human",
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The review should start with this exact outcome.",
            ),
        ),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    transport = DeliveryUncertainTransport(vault)

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")
                page.get_by_role("button", name="Start review here").click()
                page.get_by_text("Delivery could not be confirmed", exact=True).wait_for(
                    timeout=5_000
                )
                hand = page.get_by_role("link", name="Open the same ChatGPT task")
                assert hand.get_attribute("href") == f"codex://threads/{REVIEW_HAND_ID}"
                assert not hand.get_attribute("href").startswith("codex://new?")
                assert page.get_by_role("button", name="Start review here").count() == 0
                assert transport.submit_count == 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_start_delivery_uncertain_without_hand_never_offers_new_hand(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Ambiguous start without recoverable hand")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Never duplicate an ambiguously delivered review start.",
        status="ready",
        next_actor="human",
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact open outcome.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The review should start with this exact outcome.",
            ),
        ),
    )
    static_resource = files("continuity_kernel") / "resources/bridge"
    transport = DeliveryUncertainTransport(vault, thread_id=None)

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_role("button", name="Start review here").click()
                page.get_by_text("Delivery could not be confirmed", exact=True).wait_for(
                    timeout=5_000
                )
                assert page.get_by_role("link", name="Open the same ChatGPT task").count() == 0
                assert page.get_by_role("link", name="Start in ChatGPT").count() == 0
                assert page.get_by_role("button", name="Start review here").count() == 0
                assert (
                    "cannot recover the ChatGPT task"
                    in page.locator(".guided-review-delivery").text_content()
                )
                assert transport.submit_count == 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_full_receipt_store_keeps_queue_and_validated_exact_hand_fallback(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Transient resume exact-hand recovery")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Keep the known review hand reachable after transport failure.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Keep the exact outcome current if its evidence still holds.",
        waiting_on="Does this exact outcome still earn its place?",
        active_thread_id=REVIEW_HAND_ID,
        refs=("review-scope:all-open", "review-subject:task:exact-outcome"),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
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
    static_resource = files("continuity_kernel") / "resources/bridge"
    ledger = OperationLedger(vault.root)

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=ReceiptCapacityTransport(),
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_label("Tell Seld what you want").fill(
                    "Keep this exact outcome current."
                )
                page.get_by_role("button", name="Send and keep going").click()
                page.get_by_text("Automatic continuation is unavailable", exact=True).wait_for(
                    timeout=5_000
                )
                hand = page.get_by_role("link", name="Open the same ChatGPT task")
                assert hand.get_attribute("href") == f"codex://threads/{REVIEW_HAND_ID}"
                assert page.get_by_role("link", name="Start in ChatGPT").count() == 0
                assert page.get_by_text(
                    "Seld could not save another delivery record.", exact=True
                ).is_visible()
                assert (
                    page.get_by_role("link", name="Resolve saved answer")
                    .get_attribute("href")
                    .startswith("codex://new?")
                )
                assert len(ledger.snapshot().pending) == 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_clears_stale_running_receipt_after_out_of_band_disposition(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Out-of-band review disposition")
    outcome = vault.create_task(
        identifier="exact-outcome",
        title="Exact outcome",
        outcome="Restore the review form after the queued receipt is resolved elsewhere.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every exact outcome.",
        status="waiting",
        next_actor="human",
        next_action="Keep the exact outcome current if its evidence still holds.",
        waiting_on="Does this exact outcome still earn its place?",
        active_thread_id=REVIEW_HAND_ID,
        refs=("review-scope:all-open", "review-subject:task:exact-outcome"),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One exact review session is active.",
        status="active",
        next_move="Continue the focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
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
    static_resource = files("continuity_kernel") / "resources/bridge"
    ledger = OperationLedger(vault.root)
    transport = RunningTransport(vault)

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            turn_transport=transport,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"height": 844, "width": 390})
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="commitments"]').press("Enter")
                page.get_by_label("Tell Seld what you want").fill(
                    "Keep this exact outcome current."
                )
                page.get_by_role("button", name="Send and keep going").click()
                page.get_by_text("Seld is working", exact=True).wait_for(timeout=5_000)
                pending = ledger.snapshot()
                assert len(pending.pending) == 1
                ledger.decide(
                    event_id=pending.pending[0].event_id,
                    decision="rejected",
                    actor_ref="core:doctor",
                    reason_code="resolved-outside-bridge",
                    expected_queue_revision=pending.queue_revision,
                    expected_disposition_revision=pending.disposition_revision,
                )

                page.get_by_label("Tell Seld what you want").wait_for(timeout=12_000)
                assert page.get_by_text("Seld is working", exact=True).count() == 0
                assert transport.submit_count == 1
                current = ledger.snapshot()
                assert current.pending == ()
                assert len(current.decided) == 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_browser_queues_correction_and_renders_durable_disposition(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Browser control proof")
    static_resource = files("continuity_kernel") / "resources/bridge"
    console_errors: list[str] = []
    http_errors: list[str] = []

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(
                    color_scheme="dark",
                    reduced_motion="reduce",
                    viewport={"height": 720, "width": 960},
                )
                page.on(
                    "console",
                    lambda message: (
                        console_errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on(
                    "response",
                    lambda response: (
                        http_errors.append(f"{response.status} {response.url}")
                        if response.status >= 400
                        else None
                    ),
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="system"]').click()
                review_link = page.get_by_role("link", name="Continue in ChatGPT")
                review_link.wait_for(timeout=5_000)
                assert review_link.get_attribute("href").startswith("codex://new?")
                page.get_by_text("Copy prompt instead", exact=True).click()
                assert page.get_by_role("button", name="Copy prompt").is_visible()
                assert (
                    page.locator(".control-prompt-fallback code")
                    .filter(has_text="This is review only")
                    .is_visible()
                )
                correction = page.get_by_label("What should Seld correct?")
                correction.fill("Friday evening is protected.")
                correction.evaluate("element => element.setSelectionRange(7, 14)")
                focus_before_poll = page.evaluate(
                    """() => ({
                      id: document.activeElement?.id,
                      start: document.activeElement?.selectionStart,
                      end: document.activeElement?.selectionEnd,
                    })"""
                )

                ledger = OperationLedger(vault.root)
                before_refresh = ledger.snapshot()
                ledger.queue.append(
                    kind="correction",
                    subject="mind:user-correction",
                    choice="Background correction arrived first.",
                    expected_revision=before_refresh.queue_revision,
                )

                # The production ten-second poll rebuilds the System view when the queue changes.
                page.get_by_text("Background correction arrived first.").wait_for(timeout=12_000)
                assert (
                    page.get_by_label("What should Seld correct?").input_value()
                    == "Friday evening is protected."
                )
                assert (
                    page.evaluate(
                        """() => ({
                      id: document.activeElement?.id,
                      start: document.activeElement?.selectionStart,
                      end: document.activeElement?.selectionEnd,
                    })"""
                    )
                    == focus_before_poll
                    == {"id": "bridge-correction", "start": 7, "end": 14}
                )

                before_race = ledger.snapshot()
                ledger.queue.append(
                    kind="correction",
                    subject="mind:user-correction",
                    choice="A second correction won the CAS race.",
                    expected_revision=before_race.queue_revision,
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text("The queue changed while you were editing.", exact=False).wait_for(
                    timeout=5_000
                )
                assert (
                    page.get_by_label("What should Seld correct?").input_value()
                    == "Friday evening is protected."
                )
                assert page.get_by_text("Background correction arrived first.").is_visible()
                assert page.get_by_text("A second correction won the CAS race.").is_visible()

                page.evaluate(
                    """
                    window.__gsvOriginalFetch = window.fetch;
                    window.fetch = async (...args) => {
                      const request = String(args[0]);
                      if (request.endsWith('/api/v1/control')) {
                        await new Promise((resolve) => window.setTimeout(resolve, 700));
                        return new Response(
                          JSON.stringify({error: 'control queue changed; reload'}),
                          {status: 409, headers: {'Content-Type': 'application/json'}},
                        );
                      }
                      if (request.endsWith('/api/v1/snapshot')) {
                        return new Response(
                          JSON.stringify({error: 'snapshot temporarily unavailable'}),
                          {status: 503, headers: {'Content-Type': 'application/json'}},
                        );
                      }
                      return window.__gsvOriginalFetch(...args);
                    };
                    void 0;
                    """
                )
                page.get_by_label("What should Seld correct?").fill("Original submitted draft.")
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_label("What should Seld correct?").fill(
                    "Newer text typed while save was pending."
                )
                pending_editor_state = page.evaluate(
                    """() => ({
                      id: document.activeElement?.id,
                      start: document.activeElement?.selectionStart,
                      end: document.activeElement?.selectionEnd,
                    })"""
                )
                page.get_by_text("The queue changed while you were editing.", exact=False).wait_for(
                    timeout=5_000
                )
                assert (
                    page.get_by_label("What should Seld correct?").input_value()
                    == "Newer text typed while save was pending."
                )
                assert (
                    page.evaluate(
                        """() => ({
                      id: document.activeElement?.id,
                      start: document.activeElement?.selectionStart,
                      end: document.activeElement?.selectionEnd,
                    })"""
                    )
                    == pending_editor_state
                )
                assert page.get_by_text(
                    "The queue changed while you were editing. Your current draft is still "
                    "here; refresh the queue, then retry.",
                    exact=True,
                ).is_visible()
                assert not page.get_by_text("review the refreshed queue", exact=False).is_visible()
                page.evaluate("window.fetch = window.__gsvOriginalFetch; void 0;")

                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text("Newer text typed while save was pending.").wait_for(timeout=5_000)
                page.get_by_text("Waiting", exact=True).first.wait_for(timeout=5_000)

                pending = ledger.snapshot()
                assert len(pending.pending) == 3
                user_event = next(
                    event
                    for event in pending.pending
                    if event.choice == "Newer text typed while save was pending."
                )
                ledger.decide(
                    event_id=user_event.event_id,
                    decision="accepted",
                    actor_ref="core:doctor",
                    reason_code="supported-correction",
                    expected_queue_revision=pending.queue_revision,
                    expected_disposition_revision=pending.disposition_revision,
                    result_ref="control:acknowledged",
                )

                page.reload(wait_until="networkidle")
                page.locator('[data-view="system"]').click()
                page.get_by_text("Acknowledged, not applied", exact=True).wait_for(timeout=5_000)
                assert page.get_by_text("supported-correction", exact=True).count() == 0
                assert page.get_by_text("Newer text typed while save was pending.").is_visible()

                page.evaluate(
                    """
                    window.__gsvOriginalFetch = window.fetch;
                    window.fetch = async (...args) => {
                      const request = String(args[0]);
                      if (request.endsWith('/api/v1/snapshot')) {
                        return new Response(
                          JSON.stringify({error: 'snapshot temporarily unavailable'}),
                          {status: 503, headers: {'Content-Type': 'application/json'}},
                        );
                      }
                      return window.__gsvOriginalFetch(...args);
                    };
                    void 0;
                    """
                )
                page.get_by_label("What should Seld correct?").fill(
                    "This write survives a failed view refresh."
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "Queued the correction, but the view could not refresh. Refresh before "
                    "entering another correction.",
                    exact=True,
                ).wait_for(timeout=5_000)
                assert page.get_by_label("What should Seld correct?").input_value() == ""
                assert any(
                    event.choice == "This write survives a failed view refresh."
                    for event in ledger.snapshot().pending
                )
                assert not page.get_by_text(
                    "could not confirm whether the correction was saved", exact=False
                ).is_visible()
                page.evaluate("window.fetch = window.__gsvOriginalFetch; void 0;")
                page.reload(wait_until="networkidle")
                page.locator('[data-view="system"]').click()

                page.route(
                    "**/api/v1/control",
                    lambda route: route.fulfill(
                        body='{"error":"durability could not be confirmed"}',
                        content_type="application/json",
                        status=503,
                    ),
                    times=1,
                )
                page.get_by_label("What should Seld correct?").fill(
                    "Keep this text until storage is confirmed."
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "Seld could not confirm whether the correction was saved.", exact=False
                ).wait_for(timeout=5_000)
                assert (
                    page.get_by_label("What should Seld correct?").input_value()
                    == "Keep this text until storage is confirmed."
                )
                assert (
                    not page.locator(".control-form")
                    .get_by_text("Your local files were not changed.", exact=False)
                    .is_visible()
                )

                page.route(
                    "**/api/v1/control",
                    lambda route: route.fulfill(
                        body='{"error":"control queue reached its bounded event limit"}',
                        content_type="application/json",
                        status=400,
                    ),
                    times=1,
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "The correction was not queued: control queue reached its bounded event limit",
                    exact=True,
                ).wait_for(timeout=5_000)
                assert (
                    page.get_by_label("What should Seld correct?").input_value()
                    == "Keep this text until storage is confirmed."
                )

                closed = ledger.snapshot()
                for pending_event in closed.pending:
                    closed = ledger.decide(
                        event_id=pending_event.event_id,
                        decision="rejected",
                        actor_ref="core:doctor",
                        reason_code="superseded-test-race",
                        expected_queue_revision=closed.queue_revision,
                        expected_disposition_revision=closed.disposition_revision,
                    )
                ledger.archive_closed(
                    expected_queue_revision=closed.queue_revision,
                    expected_disposition_revision=closed.disposition_revision,
                )
                page.reload(wait_until="networkidle")
                page.locator('[data-view="system"]').click()
                page.get_by_text("Previous queue · history only", exact=True).wait_for(
                    timeout=5_000
                )
                assert page.get_by_text("Acknowledged, not applied", exact=True).is_visible()
                assert page.get_by_text("Newer text typed while save was pending.").is_visible()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    assert any("409" in message for message in console_errors)
    unexpected = [
        message
        for message in console_errors
        if "409" not in message and "503" not in message and "400" not in message
    ]
    assert unexpected == [], http_errors
