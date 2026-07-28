from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from continuity_kernel.bridge import (
    BridgeHTTPServer,
    _snapshot_revision_inputs,
    bridge_snapshot,
)
from continuity_kernel.codex_turn_transport import (
    RESUME_REVIEW_CHOICE,
    START_REVIEW_CHOICE,
    START_REVIEW_SUBJECT,
    TRANSPORT_SCHEMA_VERSION,
    ReceiptCapacityError,
    TurnContext,
    TurnMode,
    TurnReceipt,
    TurnState,
    canonical_revision_inputs,
)
from continuity_kernel.control_queue import EMPTY_REVISION, ControlQueue
from continuity_kernel.direction import ABSENT_DIRECTION_REVISION, direction_aim
from continuity_kernel.portfolio import ABSENT_PORTFOLIO_REVISION, portfolio_item
from continuity_kernel.records import REVIEW_SCOPE_REF, format_time
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)

ACCESS_TOKEN = "c" * 48
INSTANCE_ID = "d" * 32
THREAD_ID = "019f95fd-009e-7603-ab87-f9927cf31c4d"


class _FakeTransport:
    def __init__(self) -> None:
        self.receipts: dict[str, TurnReceipt] = {}
        self.contexts: list[TurnContext] = []
        self.snapshot_event_ids: list[str | None] = []

    def snapshot(self, event_id: str | None = None) -> dict[str, Any]:
        self.snapshot_event_ids.append(event_id)
        receipt = self.receipts.get(event_id or "")
        return {
            "automatic_resume": True,
            "automatic_start": True,
            "available": True,
            "enabled": True,
            "event": receipt.public(final_answer=None) if receipt is not None else None,
            "reason_code": None,
        }

    def receipt(self, event_id: object) -> TurnReceipt | None:
        return self.receipts.get(event_id) if isinstance(event_id, str) else None

    def submit(self, context: TurnContext) -> TurnReceipt:
        existing = self.receipts.get(context.event_id)
        if existing is not None:
            return existing
        self.contexts.append(context)
        now = format_time(datetime.now(UTC))
        receipt = TurnReceipt(
            schema_version=TRANSPORT_SCHEMA_VERSION,
            event_id=context.event_id,
            mode=context.mode,
            state=TurnState.PENDING,
            attempt=1,
            vault_id="11111111-1111-4111-8111-111111111111",
            context_hash=context.context_hash,
            queue_revision=context.queue_revision,
            target_revision=context.target_revision,
            thread_id=context.active_thread_id,
            owner_instance_id=None,
            decision=None,
            result_ref=None,
            canonical_revision=None,
            result_context_hash=None,
            reason_code=None,
            created_at=now,
            updated_at=now,
        )
        self.receipts[context.event_id] = receipt
        return receipt


class _ReceiptCapacityTransport(_FakeTransport):
    def submit(self, context: TurnContext) -> TurnReceipt:
        del context
        raise ReceiptCapacityError("guided-review transport receipt store is full")


@contextmanager
def _running_bridge(
    vault: Vault,
    static: Path,
    transport: _FakeTransport,
) -> Iterator[tuple[str, BridgeHTTPServer]]:
    server = BridgeHTTPServer(
        ("127.0.0.1", 0),
        vault,
        static,
        access_token=ACCESS_TOKEN,
        instance_id=INSTANCE_ID,
        integration_provider=lambda: {"available": True, "ready": True},
        turn_transport=transport,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
        outcome="Check every outcome without equating checked with resolved.",
        status="doing",
        next_actor="agent",
        next_action="Present this exact outcome.",
        waiting_on="What should change?",
        active_thread_id=THREAD_ID,
        refs=(REVIEW_SCOPE_REF, "review-subject:task:exact-outcome"),
    )
    vault.create_thread(
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
    return {"outcome": outcome, "portfolio": portfolio, "session": session}


def _post(base: str, path: str, payload: dict[str, Any], *, origin: str | None = None) -> Any:
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Origin": origin if origin is not None else base,
    }
    request = Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.loads(response.read())


def _append_filler_controls(queue: ControlQueue, *, count: int = 20) -> None:
    for index in range(count):
        current = queue.snapshot()
        queue.append(
            kind="setup_choice",
            subject=f"source:filler-{index}",
            choice="selected",
            expected_revision=current.revision,
        )


def test_bridge_snapshot_revision_inputs_equal_transport_canonical_inputs(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Complete revision projection")
    _seed_active(vault)
    vault.create_entity(
        identifier="topic:whole-life-review",
        title="Whole-life review",
        entity_type="topic",
        summary="Relevant canonical context.",
    )
    vault.set_direction(
        expected_revision=ABSENT_DIRECTION_REVISION,
        status="provisional",
        current_chapter="Review current work against the whole life.",
        aims=(
            direction_aim(
                identifier="protect-attention",
                title="Protect attention",
                desired_state="Chosen outcomes retain enough attention.",
            ),
        ),
    )
    snapshot = bridge_snapshot(
        vault,
        doctor={"healthy": True, "issues": []},
        integration={"available": True, "ready": True},
    )

    assert _snapshot_revision_inputs(snapshot) == canonical_revision_inputs(vault)


def test_guided_control_post_returns_service_unavailable_for_partial_vault(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Partial guided review vault")
    seeded = _seed_active(vault)
    (vault.root / "MIND.md").unlink()
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with _running_bridge(vault, static, transport) as (base, _server):
        with pytest.raises(HTTPError) as unavailable:
            _post(
                base,
                "/api/v1/control",
                {
                    "choice": "Keep this wording until the vault is repaired.",
                    "expected_revision": EMPTY_REVISION,
                    "kind": "correction",
                    "subject": f"record:task/{seeded['session'].identifier}",
                    "target_revision": seeded["session"].revision,
                },
            )

        assert unavailable.value.code == HTTPStatus.SERVICE_UNAVAILABLE
        assert json.loads(unavailable.value.read())["error"] == (
            "The local Seld record is unavailable"
        )
        assert ControlQueue(vault.root).snapshot().events == ()
        assert transport.contexts == []


def test_control_post_rejects_lone_surrogate_as_bounded_bad_request(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge invalid Unicode control")
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with (
        _running_bridge(vault, static, transport) as (base, _server),
        pytest.raises(HTTPError) as invalid,
    ):
        _post(
            base,
            "/api/v1/control",
            {
                "choice": "\ud800",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": "mind:user-correction",
            },
        )

    assert invalid.value.code == HTTPStatus.BAD_REQUEST
    assert json.loads(invalid.value.read()) == {
        "error": "choice contains invalid Unicode text",
        "ok": False,
    }
    assert not (vault.root / ".gsv/control").exists()
    assert transport.contexts == []


def test_authenticated_control_append_triggers_exact_event_and_poll_is_non_enumerating(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge turn endpoint")
    seeded = _seed_active(vault)
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with _running_bridge(vault, static, transport) as (base, _server):
        status, appended = _post(
            base,
            "/api/v1/control",
            {
                "choice": "keep this exact outcome visible",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": f"record:task/{seeded['session'].identifier}",
                "target_revision": seeded["session"].revision,
            },
        )
        event_id = appended["event"]["event_id"]
        assert status == HTTPStatus.CREATED
        assert appended["transport"]["event_id"] == event_id
        assert appended["transport"]["state"] == "pending"
        assert len(transport.contexts) == 1
        context = transport.contexts[0]
        assert context.mode is TurnMode.RESUME
        assert context.active_thread_id == THREAD_ID
        assert context.session_task_id == seeded["session"].identifier
        assert context.target_revision == seeded["session"].revision

        # A duplicate trigger observes the exact durable receipt and does not
        # submit another model turn.
        retry_status, retried = _post(
            base,
            "/api/v1/review-turn",
            {"event_id": event_id},
        )
        assert retry_status == HTTPStatus.ACCEPTED
        assert retried["transport"]["event_id"] == event_id
        assert len(transport.contexts) == 1

        request = Request(
            f"{base}/api/v1/review-turn?event_id={event_id}",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        with urlopen(request, timeout=2) as response:
            polled = json.loads(response.read())
        assert polled["transport"]["event_id"] == event_id
        assert polled["transport"]["final_answer"] is None

        snapshot_calls = list(transport.snapshot_event_ids)
        head = Request(
            f"{base}/api/v1/review-turn?event_id={event_id}",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            method="HEAD",
        )
        with pytest.raises(HTTPError) as method_not_allowed:
            urlopen(head, timeout=2)
        assert method_not_allowed.value.code == HTTPStatus.METHOD_NOT_ALLOWED
        assert transport.snapshot_event_ids == snapshot_calls

        for path in (
            "/api/v1/review-turn",
            "/api/v1/review-turn?event_id=11111111-1111-4111-8111-111111111111",
        ):
            request = Request(
                f"{base}{path}",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            )
            with pytest.raises(HTTPError) as unavailable:
                urlopen(request, timeout=2)
            assert unavailable.value.code in {HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND}


def test_stale_review_session_revision_is_rejected_before_answer_is_queued(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge stale review step")
    seeded = _seed_active(vault)
    stale_revision = seeded["session"].revision
    vault.update_task(
        seeded["session"].identifier,
        expected_revision=stale_revision,
        next_action="A concurrent semantic update changed this exact review step.",
    )
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with (
        _running_bridge(vault, static, transport) as (base, _server),
        pytest.raises(HTTPError) as stale,
    ):
        _post(
            base,
            "/api/v1/control",
            {
                "choice": "Preserve this typed answer until current truth is reloaded.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": f"record:task/{seeded['session'].identifier}",
                "target_revision": stale_revision,
            },
        )

    assert stale.value.code == HTTPStatus.CONFLICT
    assert "review step changed" in json.loads(stale.value.read())["error"]
    assert ControlQueue(vault.root).snapshot().revision == EMPTY_REVISION
    assert transport.contexts == []


def test_only_the_exact_guided_review_step_receives_the_larger_answer_bound(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge bounded prepared answers")
    seeded = _seed_active(vault)
    static = tmp_path / "static"
    static.mkdir()
    review_answer = '"' * 23_500

    with _running_bridge(vault, static, _FakeTransport()) as (base, _server):
        status, appended = _post(
            base,
            "/api/v1/control",
            {
                "choice": review_answer,
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": f"record:task/{seeded['session'].identifier}",
                "target_revision": seeded["session"].revision,
            },
        )

    assert status == HTTPStatus.CREATED
    assert appended["event"]["choice"] == review_answer

    non_correction = Vault(tmp_path / "non-correction-vault")
    non_correction.initialize(name="Non-correction control bound")
    _seed_active(non_correction)
    non_correction_static = tmp_path / "non-correction-static"
    non_correction_static.mkdir()
    operation_id = "11111111-1111-4111-8111-111111111111"
    with (
        _running_bridge(non_correction, non_correction_static, _FakeTransport()) as (base, _server),
        pytest.raises(HTTPError) as non_correction_rejected,
    ):
        _post(
            base,
            "/api/v1/control",
            {
                "choice": review_answer,
                "expected_revision": EMPTY_REVISION,
                "kind": "approval",
                "subject": f"operation:{operation_id}",
            },
        )

    assert non_correction_rejected.value.code == HTTPStatus.BAD_REQUEST
    assert "size bound" in json.loads(non_correction_rejected.value.read())["error"]
    assert ControlQueue(non_correction.root).snapshot().revision == EMPTY_REVISION

    ordinary = Vault(tmp_path / "ordinary-vault")
    ordinary.initialize(name="Ordinary control bound")
    ordinary_static = tmp_path / "ordinary-static"
    ordinary_static.mkdir()
    with (
        _running_bridge(ordinary, ordinary_static, _FakeTransport()) as (base, _server),
        pytest.raises(HTTPError) as rejected,
    ):
        _post(
            base,
            "/api/v1/control",
            {
                "choice": review_answer,
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": "mind:user-correction",
            },
        )

    assert rejected.value.code == HTTPStatus.BAD_REQUEST
    assert "size bound" in json.loads(rejected.value.read())["error"]
    assert ControlQueue(ordinary.root).snapshot().revision == EMPTY_REVISION


def test_review_turn_retry_reports_canonical_drift_without_replaying_queued_answer(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge queued answer drift")
    seeded = _seed_active(vault)
    queued = ControlQueue(vault.root).append(
        kind="correction",
        subject=f"record:task/{seeded['session'].identifier}",
        choice="Keep this exact wording queued once.",
        target_revision=seeded["session"].revision,
        expected_revision=EMPTY_REVISION,
    )
    event_id = queued.events[-1].event_id
    vault.update_task(
        seeded["session"].identifier,
        expected_revision=seeded["session"].revision,
        next_action="A concurrent semantic update changed the review step.",
    )
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with (
        _running_bridge(vault, static, transport) as (base, _server),
        pytest.raises(HTTPError) as changed,
    ):
        _post(base, "/api/v1/review-turn", {"event_id": event_id})

    assert changed.value.code == HTTPStatus.CONFLICT
    assert "remains queued once and was not replayed" in json.loads(changed.value.read())["error"]
    assert [event.event_id for event in ControlQueue(vault.root).snapshot().events] == [event_id]
    assert transport.contexts == []


def test_review_turn_trigger_reports_receipt_capacity_without_consuming_intent(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge receipt capacity trigger")
    seeded = _seed_active(vault)
    queue = ControlQueue(vault.root).append(
        kind="correction",
        subject=f"record:task/{seeded['session'].identifier}",
        choice="Keep this exact queued answer recoverable.",
        target_revision=seeded["session"].revision,
        expected_revision=EMPTY_REVISION,
    )
    event_id = queue.events[-1].event_id
    static = tmp_path / "static"
    static.mkdir()

    with (
        _running_bridge(vault, static, _ReceiptCapacityTransport()) as (base, _server),
        pytest.raises(HTTPError) as full,
    ):
        _post(base, "/api/v1/review-turn", {"event_id": event_id})

    assert full.value.code == HTTPStatus.CONFLICT
    payload = json.loads(full.value.read())
    assert payload["transport"]["state"] == "blocked"
    assert payload["transport"]["reason_code"] == "transport_receipt_store_full"
    assert [event.event_id for event in ControlQueue(vault.root).snapshot().events] == [event_id]


def test_review_turn_trigger_preserves_bearer_origin_shape_and_queue_boundaries(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge turn security")
    seeded = _seed_active(vault)
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()
    queued = ControlQueue(vault.root).append(
        kind="correction",
        subject=f"record:task/{seeded['session'].identifier}",
        choice="leave this pending",
        target_revision=seeded["session"].revision,
        expected_revision=EMPTY_REVISION,
    )
    event_id = queued.events[-1].event_id
    queue_before = (vault.root / ".gsv/control/queue.jsonl").read_bytes()

    with _running_bridge(vault, static, transport) as (base, _server):
        for headers in (
            {"Content-Type": "application/json", "Origin": base},
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:9",
            },
        ):
            request = Request(
                f"{base}/api/v1/review-turn",
                data=json.dumps({"event_id": event_id}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with pytest.raises(HTTPError) as rejected:
                urlopen(request, timeout=2)
            assert rejected.value.code == HTTPStatus.FORBIDDEN

        request = Request(
            f"{base}/api/v1/review-turn",
            data=json.dumps({"event_id": event_id, "provider_body": "forbidden"}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
                "Origin": base,
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as unsupported:
            urlopen(request, timeout=2)
        assert unsupported.value.code == HTTPStatus.BAD_REQUEST

    assert transport.contexts == []
    assert (vault.root / ".gsv/control/queue.jsonl").read_bytes() == queue_before


def test_fixed_start_event_is_validated_before_append_and_uses_portfolio_revision(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bridge start endpoint")
    outcome = vault.create_task(
        identifier="first-outcome",
        title="First outcome",
        outcome="Choose its current place.",
        status="ready",
        next_actor="human",
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
        ),
    )
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with _running_bridge(vault, static, transport) as (base, _server):
        with pytest.raises(HTTPError) as malformed:
            _post(
                base,
                "/api/v1/control",
                {
                    "choice": "start something else",
                    "expected_revision": EMPTY_REVISION,
                    "kind": "correction",
                    "subject": START_REVIEW_SUBJECT,
                    "target_revision": portfolio.revision,
                },
            )
        assert malformed.value.code == HTTPStatus.BAD_REQUEST
        assert ControlQueue(vault.root).snapshot().revision == EMPTY_REVISION

        status, appended = _post(
            base,
            "/api/v1/control",
            {
                "choice": START_REVIEW_CHOICE,
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": START_REVIEW_SUBJECT,
                "target_revision": portfolio.revision,
            },
        )
        assert status == HTTPStatus.CREATED
        assert appended["transport"]["mode"] == "start"
        assert transport.contexts[0].portfolio_revision == portfolio.revision
        assert transport.contexts[0].active_thread_id is None

        with pytest.raises(HTTPError) as duplicate:
            _post(
                base,
                "/api/v1/control",
                {
                    "choice": START_REVIEW_CHOICE,
                    "expected_revision": appended["revision"],
                    "kind": "correction",
                    "subject": START_REVIEW_SUBJECT,
                    "target_revision": portfolio.revision,
                },
            )
        assert duplicate.value.code == HTTPStatus.CONFLICT
        assert len(ControlQueue(vault.root).snapshot().events) == 1


def test_old_pending_start_is_projected_and_rejected_from_full_ledger(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Full pending start guard")
    outcome = vault.create_task(
        identifier="first-outcome",
        title="First outcome",
        outcome="Choose its current place.",
        status="ready",
        next_actor="human",
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
        ),
    )
    queue = ControlQueue(vault.root)
    started = queue.append(
        kind="correction",
        subject=START_REVIEW_SUBJECT,
        choice=START_REVIEW_CHOICE,
        target_revision=portfolio.revision,
        expected_revision=EMPTY_REVISION,
    )
    start_event = started.events[-1]
    _append_filler_controls(queue)
    current = queue.snapshot()
    projected = bridge_snapshot(
        vault,
        doctor={"healthy": True, "issues": []},
        integration={"available": True, "ready": True},
    )

    assert all(
        item["event"]["event_id"] != start_event.event_id for item in projected["controls"]["items"]
    )
    assert projected["portfolio"]["review"]["pending_start"]["event_id"] == start_event.event_id

    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()
    with _running_bridge(vault, static, transport) as (base, _server):
        with pytest.raises(HTTPError) as duplicate:
            _post(
                base,
                "/api/v1/control",
                {
                    "choice": START_REVIEW_CHOICE,
                    "expected_revision": current.revision,
                    "kind": "correction",
                    "subject": START_REVIEW_SUBJECT,
                    "target_revision": portfolio.revision,
                },
            )
        assert duplicate.value.code == HTTPStatus.CONFLICT
    assert queue.snapshot().events == current.events
    assert transport.contexts == []


def test_pending_start_outside_display_window_is_still_selected_when_portfolio_is_unavailable(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unavailable Portfolio pending start selection")
    queue = ControlQueue(vault.root)
    started = queue.append(
        kind="correction",
        subject=START_REVIEW_SUBJECT,
        choice=START_REVIEW_CHOICE,
        target_revision="a" * 64,
        expected_revision=EMPTY_REVISION,
    )
    start_event = started.events[-1]
    _append_filler_controls(queue)
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with _running_bridge(vault, static, transport) as (_base, server):
        snapshot = server.snapshot()

    assert snapshot["portfolio"]["available"] is False
    assert snapshot["portfolio"]["review"]["pending_start"]["event_id"] == start_event.event_id
    assert transport.snapshot_event_ids[-1] == start_event.event_id


def test_old_pending_answer_blocks_action_and_duplicate_from_full_ledger(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Full pending answer guard")
    seeded = _seed_active(vault)
    queue = ControlQueue(vault.root)
    answered = queue.append(
        kind="correction",
        subject=f"record:task/{seeded['session'].identifier}",
        choice="Keep this exact outcome visible.",
        target_revision=seeded["session"].revision,
        expected_revision=EMPTY_REVISION,
    )
    answer_event = answered.events[-1]
    _append_filler_controls(queue)
    current = queue.snapshot()
    projected = bridge_snapshot(
        vault,
        doctor={"healthy": True, "issues": []},
        integration={"available": True, "ready": True},
    )
    review = projected["portfolio"]["review"]

    assert all(
        item["event"]["event_id"] != answer_event.event_id
        for item in projected["controls"]["items"]
    )
    assert review["pending_intent"]["event_id"] == answer_event.event_id
    assert review["actionable"] is False

    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()
    with _running_bridge(vault, static, transport) as (base, _server):
        with pytest.raises(HTTPError) as duplicate:
            _post(
                base,
                "/api/v1/control",
                {
                    "choice": "A second answer must not enter the same review step.",
                    "expected_revision": current.revision,
                    "kind": "correction",
                    "subject": f"record:task/{seeded['session'].identifier}",
                    "target_revision": seeded["session"].revision,
                },
            )
        assert duplicate.value.code == HTTPStatus.CONFLICT
    assert queue.snapshot().events == current.events
    assert transport.contexts == []


def test_noncanonical_legacy_review_hand_is_rejected_before_answer_is_queued(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Legacy guided-review hand")
    seeded = _seed_active(vault)
    session = vault.update_task(
        seeded["session"].identifier,
        expected_revision=seeded["session"].revision,
        active_thread_id="legacy-review-hand",
    )
    projected = bridge_snapshot(
        vault,
        doctor={"healthy": True, "issues": []},
        integration={"available": True, "ready": True},
    )
    review = projected["portfolio"]["review"]

    assert review["state"] == "conflict"
    assert review["active_thread_id"] is None
    assert review["hand_url"] is None
    assert review["issue"] == (
        "The ChatGPT task linked to this review is invalid; repair it before continuing."
    )

    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()
    with (
        _running_bridge(vault, static, transport) as (base, _server),
        pytest.raises(HTTPError) as rejected,
    ):
        _post(
            base,
            "/api/v1/control",
            {
                "choice": "Do not strand this answer behind an unusable hand.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": f"record:task/{session.identifier}",
                "target_revision": session.revision,
            },
        )

    assert rejected.value.code == HTTPStatus.CONFLICT
    assert not (vault.root / ".gsv/control").exists()
    assert transport.contexts == []


def test_conflicted_review_state_is_rejected_before_answer_is_queued(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Conflicted guided review")
    seeded = _seed_active(vault)
    review_thread = vault.get_thread("thread:life-portfolio-review")
    vault.update_thread(
        review_thread.identifier,
        expected_revision=review_thread.revision,
        clear_focus_task=True,
    )
    projected = bridge_snapshot(
        vault,
        doctor={"healthy": True, "issues": []},
        integration={"available": True, "ready": True},
    )
    review = projected["portfolio"]["review"]
    assert review["state"] == "conflict"
    assert review["issue"]
    assert review["hand_url"] == f"codex://threads/{THREAD_ID}"
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with (
        _running_bridge(vault, static, transport) as (base, _server),
        pytest.raises(HTTPError) as rejected,
    ):
        _post(
            base,
            "/api/v1/control",
            {
                "choice": "Do not strand this answer in a conflicted session.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": f"record:task/{seeded['session'].identifier}",
                "target_revision": seeded["session"].revision,
            },
        )

    assert rejected.value.code == HTTPStatus.CONFLICT
    assert "needs repair" in json.loads(rejected.value.read())["error"]
    assert not (vault.root / ".gsv/control").exists()
    assert transport.contexts == []


def test_paused_review_rejects_non_resume_answer_before_queue_append(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Paused guided review")
    seeded = _seed_active(vault)
    paused = vault.update_task(
        seeded["session"].identifier,
        expected_revision=seeded["session"].revision,
        add_refs=("review-state:paused",),
    )
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with (
        _running_bridge(vault, static, transport) as (base, _server),
        pytest.raises(HTTPError) as rejected,
    ):
        _post(
            base,
            "/api/v1/control",
            {
                "choice": "This answer must wait until the review is resumed.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": f"record:task/{paused.identifier}",
                "target_revision": paused.revision,
            },
        )

    assert rejected.value.code == HTTPStatus.CONFLICT
    assert "is paused" in json.loads(rejected.value.read())["error"]
    assert not (vault.root / ".gsv/control").exists()
    assert transport.contexts == []


def test_paused_review_accepts_only_the_exact_resume_intent(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Paused guided review resume")
    seeded = _seed_active(vault)
    paused = vault.update_task(
        seeded["session"].identifier,
        expected_revision=seeded["session"].revision,
        add_refs=("review-state:paused",),
    )
    static = tmp_path / "static"
    static.mkdir()
    transport = _FakeTransport()

    with _running_bridge(vault, static, transport) as (base, _server):
        status, payload = _post(
            base,
            "/api/v1/control",
            {
                "choice": RESUME_REVIEW_CHOICE,
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": f"record:task/{paused.identifier}",
                "target_revision": paused.revision,
            },
        )

    assert status == HTTPStatus.CREATED
    assert payload["transport"]["state"] == "pending"
    assert len(transport.contexts) == 1
    assert transport.contexts[0].active_thread_id == THREAD_ID
