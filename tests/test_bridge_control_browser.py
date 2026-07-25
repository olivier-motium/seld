from __future__ import annotations

import json
import os
import threading
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
from continuity_kernel.records import format_time, review_coverage_ref, review_option_ref
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
            attempt=0,
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

    def snapshot(self, event_id: str | None = None) -> dict[str, Any]:
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
        assert "act-next" in event.choice

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
            reason_code="semantic-readback-complete",
            created_at=now,
            updated_at=now,
        )
        self.receipts[exact.event_id] = receipt
        self.answers[exact.event_id] = (
            "I tightened the first next move and kept external action gated. "
            "Now: does the second outcome still earn its place?"
        )
        return receipt


class NeverSubmittedTransport:
    def snapshot(self, event_id: str | None = None) -> dict[str, Any]:
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

    def snapshot(self, event_id: str | None = None) -> dict[str, Any]:
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
        snapshot["guided_review_transport"]["event"] = {
            "event_id": event_id,
            "final_answer": None,
            "mode": "resume",
            "reason_code": "interrupted_after_possible_spawn",
            "retryable": False,
            "state": "delivery_uncertain",
            "terminal": True,
            "thread_id": REVIEW_HAND_ID,
            "updated_at": format_time(datetime.now(UTC)),
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
                    page.get_by_role("link", name="Open the exact review hand").get_attribute(
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

                page.get_by_role("heading", name="Work through every open outcome").wait_for()
                assert (
                    page.locator("article.guided-review-subject")
                    .get_by_role("heading", name="Exact outcome")
                    .is_visible()
                )
                assert page.get_by_text("Rank 7", exact=True).is_visible()
                assert page.get_by_text("Checked never means resolved.", exact=False).is_visible()
                assert page.locator("body").evaluate(
                    "element => element.scrollWidth <= document.documentElement.clientWidth"
                )

                page.get_by_role("button", name="Do / next").evaluate(
                    "button => { button.click(); button.click(); }"
                )
                page.get_by_text("The review hand replied", exact=True).wait_for(timeout=5_000)
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
                assert page.get_by_text("semantic-readback-complete", exact=True).is_visible()
                browser.close()
        finally:
            fresh_server.shutdown()
            fresh_server.server_close()
            fresh_thread.join(timeout=5)
            assert not fresh_thread.is_alive()


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
        choice=(
            "For task:exact-outcome, the user selected act-next. Practical consequence: "
            "author the smallest bounded local proof."
        ),
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
                commitments = page.locator('[data-view="commitments"]')
                commitments.focus()
                commitments.press("Enter")
                page.get_by_text("The review hand replied", exact=True).wait_for(timeout=5_000)
                page.locator("#guided-review-current-title").get_by_text(
                    "Next outcome", exact=True
                ).wait_for(timeout=5_000)
                assert review_posts == [f"{base}/api/v1/review-turn"]
                assert transport.submit_count == 1
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
                    page.get_by_label("Tell the Mind what you want").fill(oversized)
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
                page.get_by_label("Tell the Mind what you want").fill(draft)
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
                        page.get_by_role("link", name="Open the exact review hand").get_attribute(
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
                page.get_by_label("Tell the Mind what you want").fill(answer)
                page.get_by_role("button", name="Send and keep going").click()
                page.get_by_text(
                    "The review changed after this answer was queued.", exact=False
                ).wait_for(timeout=5_000)

                pending = ledger.snapshot().pending
                assert len(pending) == 1
                assert answer in pending[0].choice
                assert transport.submit_count == 1
                assert page.get_by_text("wording remains saved once", exact=False).is_visible()
                assert page.get_by_text("Bridge could not read", exact=False).count() == 0
                assert (
                    page.get_by_role("link", name="Open the exact review hand").get_attribute(
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
                page.get_by_role("button", name="Keep current", exact=True).evaluate(
                    "button => { button.click(); button.click(); }"
                )
                page.get_by_text("Delivery could not be confirmed", exact=True).wait_for(
                    timeout=5_000
                )
                assert transport.submit_count == 1
                assert vault.get_task(outcome.identifier).revision == outcome.revision
                assert len(ledger.snapshot().pending) == 1
                assert not page.get_by_role("button", name="Retry this exact turn").is_visible()
                hand = page.get_by_role("link", name="Open the exact review hand")
                assert hand.get_attribute("href") == f"codex://threads/{REVIEW_HAND_ID}"
                assert (
                    page.get_by_role("link", name="Resolve queued receipt")
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
                assert not page.get_by_role("button", name="Retry this exact turn").is_visible()
                assert (
                    page.get_by_role("link", name="Open the exact review hand").get_attribute(
                        "href"
                    )
                    == f"codex://threads/{REVIEW_HAND_ID}"
                )
                assert (
                    page.get_by_role("link", name="Resolve queued receipt")
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
                page.get_by_label("Tell the Mind what you want").wait_for(timeout=12_000)
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
                hand = page.get_by_role("link", name="Open the exact review hand")
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
                assert page.get_by_role("link", name="Open the exact review hand").count() == 0
                assert page.get_by_role("link", name="Start in Codex").count() == 0
                assert page.get_by_role("button", name="Start review here").count() == 0
                assert (
                    "cannot recover the Codex hand"
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
                page.get_by_label("Tell the Mind what you want").fill(
                    "Keep this exact outcome current."
                )
                page.get_by_role("button", name="Send and keep going").click()
                page.get_by_text("Automatic continuation is unavailable", exact=True).wait_for(
                    timeout=5_000
                )
                hand = page.get_by_role("link", name="Open the exact review hand")
                assert hand.get_attribute("href") == f"codex://threads/{REVIEW_HAND_ID}"
                assert page.get_by_role("link", name="Start in Codex").count() == 0
                assert page.get_by_text("Reason: transport receipt store full").is_visible()
                assert (
                    page.get_by_role("link", name="Resolve queued receipt")
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
                page.get_by_label("Tell the Mind what you want").fill(
                    "Keep this exact outcome current."
                )
                page.get_by_role("button", name="Send and keep going").click()
                page.get_by_text("The Mind is working", exact=True).wait_for(timeout=5_000)
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

                page.get_by_label("Tell the Mind what you want").wait_for(timeout=12_000)
                assert page.get_by_text("The Mind is working", exact=True).count() == 0
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
                review_link = page.get_by_role("link", name="Review in Codex")
                review_link.wait_for(timeout=5_000)
                assert review_link.get_attribute("href").startswith("codex://new?")
                page.get_by_text("Copy prompt instead", exact=True).click()
                assert page.get_by_role("button", name="Copy prompt").is_visible()
                assert (
                    page.locator(".control-prompt-fallback code")
                    .filter(has_text="This is review only")
                    .is_visible()
                )
                page.get_by_label("What should GSV correct?").fill("Friday evening is protected.")

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
                    page.get_by_label("What should GSV correct?").input_value()
                    == "Friday evening is protected."
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
                    page.get_by_label("What should GSV correct?").input_value()
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
                page.get_by_label("What should GSV correct?").fill("Original submitted draft.")
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_label("What should GSV correct?").fill(
                    "Newer text typed while save was pending."
                )
                page.get_by_text("The queue changed while you were editing.", exact=False).wait_for(
                    timeout=5_000
                )
                assert (
                    page.get_by_label("What should GSV correct?").input_value()
                    == "Newer text typed while save was pending."
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
                assert page.get_by_text("supported-correction", exact=True).is_visible()
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
                page.get_by_label("What should GSV correct?").fill(
                    "This write survives a failed view refresh."
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "Queued the correction, but the view could not refresh. Refresh before "
                    "entering another correction.",
                    exact=True,
                ).wait_for(timeout=5_000)
                assert page.get_by_label("What should GSV correct?").input_value() == ""
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
                page.get_by_label("What should GSV correct?").fill(
                    "Keep this text until storage is confirmed."
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "GSV could not confirm whether the correction was saved.", exact=False
                ).wait_for(timeout=5_000)
                assert (
                    page.get_by_label("What should GSV correct?").input_value()
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
                    page.get_by_label("What should GSV correct?").input_value()
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
