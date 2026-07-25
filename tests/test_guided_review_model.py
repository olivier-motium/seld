from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TypedDict

import pytest

from continuity_kernel import bridge, cli, mcp_server
from continuity_kernel.direction import (
    ABSENT_DIRECTION_REVISION,
    Direction,
    direction_aim,
    parse_direction,
    render_direction,
)
from continuity_kernel.errors import ConflictError, NotFoundError, ValidationError
from continuity_kernel.portfolio import (
    ABSENT_PORTFOLIO_REVISION,
    MAX_GUIDED_REVIEW_OUTCOMES,
    Portfolio,
    inspect_portfolio_state,
    new_portfolio,
    parse_portfolio,
    portfolio_item,
    render_portfolio,
)
from continuity_kernel.records import (
    REVIEW_PAUSED_REF,
    REVIEW_SCOPE_REF,
    REVIEW_WORK_THREAD_ID,
    Task,
    WorkThread,
    new_task,
    new_thread,
    parse_review_references,
    parse_thread,
    render_thread,
    review_coverage_ref,
    review_option_ref,
    validate_review_references,
)
from continuity_kernel.vault import Vault, doctor_dict


class _SeededReview(TypedDict):
    direction: Direction
    first: Task
    owner: WorkThread
    portfolio: Portfolio
    review_thread: WorkThread
    second: Task
    session: Task


def _seed(vault: Vault) -> _SeededReview:
    first = vault.create_task(
        identifier="first-outcome",
        title="First outcome",
        outcome="Reach the first grounded result.",
        status="doing",
        next_actor="agent",
        next_action="Carry the next reversible step.",
    )
    second = vault.create_task(
        identifier="second-outcome",
        title="Second outcome",
        outcome="Reach the second grounded result.",
        status="ready",
        next_actor="human",
        next_action="Choose the desired boundary.",
    )
    owner = vault.create_thread(
        identifier="thread:first-outcome",
        title="First outcome storyline",
        purpose="Carry the exact first outcome.",
        summary="The first outcome is current.",
        focus_task_id=first.identifier,
        task_ids=(first.identifier,),
    )
    session = vault.create_task(
        identifier="portfolio-review-session",
        title="Review every open outcome",
        outcome="Check every open outcome without equating checked with resolved.",
        status="doing",
        next_actor="agent",
        next_action="Present the first exact outcome and its authored recommendation.",
        waiting_on="What do you want to change about this outcome?",
        active_thread_id="review-hand-1",
        refs=(
            REVIEW_SCOPE_REF,
            f"review-subject:task:{first.identifier}",
            review_option_ref(
                intent="act-next",
                consequence="GSV carries the next reversible step and leaves approval with you.",
            ),
        ),
    )
    review_thread = vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Life Portfolio review",
        purpose="Carry one finite, resumable all-open review.",
        summary="One exact review session is active.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    direction = vault.set_direction(
        expected_revision=ABSENT_DIRECTION_REVISION,
        status="provisional",
        current_chapter="Protect attention while turning important commitments into results.",
        aims=(
            direction_aim(
                identifier="protect-attention",
                title="Protect attention",
                desired_state="Important work progresses without uncontrolled interruption.",
            ),
        ),
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        direction_revision=direction.revision,
        summary="Review both outcomes in authored order.",
        items=(
            portfolio_item(
                task_id_value=first.identifier,
                task_revision=first.revision,
                stance="agent-can-carry",
                reason="The next step is local and reversible.",
                work_thread_id=owner.identifier,
                work_thread_revision=owner.revision,
                direction_aim_ids=("protect-attention",),
            ),
            portfolio_item(
                task_id_value=second.identifier,
                task_revision=second.revision,
                stance="needs-human",
                reason="The desired boundary is a personal judgment.",
                unaligned_reason="This loose end is not yet tied to one current aim.",
            ),
        ),
    )
    return {
        "direction": direction,
        "first": first,
        "owner": owner,
        "portfolio": portfolio,
        "review_thread": review_thread,
        "second": second,
        "session": session,
    }


def _seed_legacy_review(vault: Vault) -> tuple[Task, Task]:
    """Reproduce the active guided-review shape shipped at public head bdac8f7."""

    outcome = vault.create_task(
        identifier="legacy-outcome",
        title="Legacy outcome",
        outcome="Remain in the all-open review.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="legacy-review-session",
        title="Review every open outcome",
        outcome="Resume the same finite review hand after upgrading.",
        status="doing",
        next_actor="agent",
        next_action="Continue with the exact current subject.",
        waiting_on="What should change about this outcome?",
        active_thread_id="legacy-review-hand",
        refs=(REVIEW_SCOPE_REF, f"review-subject:task:{outcome.identifier}"),
    )
    legacy = new_portfolio(
        summary="The pre-focus Portfolio excludes its bounded review task.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The user should decide the intended change.",
            ),
        ),
    )
    (vault.root / "PORTFOLIO.md").write_text(render_portfolio(legacy), encoding="utf-8")
    return outcome, session


def _append_raw_task_reference(vault: Vault, task: Task, reference: str) -> None:
    path = vault.root / "tasks" / f"{task.identifier}.md"
    rendered = path.read_text(encoding="utf-8")
    header, body = rendered.split("\n", 1)
    metadata = json.loads(header.removeprefix("<!-- gsv:").removesuffix(" -->"))
    metadata["refs"].append(reference)
    path.write_text(
        "<!-- gsv:"
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + " -->\n"
        + body,
        encoding="utf-8",
    )


def test_review_refs_are_typed_revision_aware_and_options_are_authored() -> None:
    anchored = review_coverage_ref(
        task_id_value="one-outcome",
        task_revision="a" * 64,
        work_thread_id="thread:one-outcome",
        work_thread_revision="b" * 64,
    )
    option = review_option_ref(
        intent="defer",
        consequence="Keep it visible and ask again after the stated dependency changes.",
    )
    parsed = parse_review_references((REVIEW_SCOPE_REF, REVIEW_PAUSED_REF, anchored, option))

    assert parsed.paused is True
    assert parsed.coverages[0].task_revision == "a" * 64
    assert parsed.coverages[0].work_thread_id == "thread:one-outcome"
    assert parsed.options[0].intent == "defer"
    assert parsed.options[0].consequence.startswith("Keep it visible")
    assert parsed.issues == ()

    legacy = parse_review_references((REVIEW_SCOPE_REF, "review-covered:task:one-outcome"))
    assert legacy.coverages[0].task_revision is None

    malformed = parse_review_references(
        (
            REVIEW_SCOPE_REF,
            "review-covered:task:one-outcome@not-a-revision",
            "review-option:invented:Do%20something",
            "review-checkpoint:unknown",
        )
    )
    assert "review-checkpoint:unknown" in malformed.malformed_refs
    assert malformed.issues == ("review session contains malformed review refs",)

    with pytest.raises(ValidationError, match="uppercase hexadecimal"):
        validate_review_references((REVIEW_SCOPE_REF, "review-option:keep:Don't%20change%20this"))

    with pytest.raises(
        ValidationError,
        match="encoded review option reference exceeds the reference limit",
    ):
        review_option_ref(intent="keep", consequence="界" * 500)


def test_guided_review_fails_closed_above_single_session_coverage_bound() -> None:
    tasks = tuple(
        new_task(
            identifier=f"outcome-{index:04d}",
            title=f"Outcome {index}",
            outcome="Keep the bounded review contract explicit.",
        )
        for index in range(MAX_GUIDED_REVIEW_OUTCOMES + 1)
    )
    portfolio = new_portfolio(
        summary="A deliberately oversized guided-review set.",
        items=tuple(
            portfolio_item(
                task_id_value=task.identifier,
                task_revision=task.revision,
                stance="needs-human",
                reason="This remains a real open outcome.",
            )
            for task in tasks
        ),
    )

    review = inspect_portfolio_state(tasks=tasks, threads=(), portfolio=portfolio).review

    assert review.state == "conflict"
    assert review.issue is not None
    assert f"at most {MAX_GUIDED_REVIEW_OUTCOMES}" in review.issue


def test_guided_review_fails_closed_when_retained_coverage_exceeds_bound(vault: Vault) -> None:
    seeded = _seed(vault)
    session = seeded["session"]
    coverage_refs = tuple(
        review_coverage_ref(
            task_id_value=f"retired-outcome-{index:04d}",
            task_revision=f"{index + 1:064x}",
        )
        for index in range(MAX_GUIDED_REVIEW_OUTCOMES + 1)
    )
    vault.update_task(
        session.identifier,
        expected_revision=session.revision,
        add_refs=coverage_refs,
    )

    review = vault.inspect_portfolio().review

    assert review.state == "conflict"
    assert review.issue is not None
    assert f"at most {MAX_GUIDED_REVIEW_OUTCOMES} exact coverage anchors" in review.issue
    assert "prune older coverage refs" in review.issue


def test_duplicate_review_reference_makes_doctor_and_bridge_fail_closed(vault: Vault) -> None:
    seeded = _seed(vault)
    session = seeded["session"]
    assert isinstance(session, Task)

    with pytest.raises(ValidationError, match="duplicate review reference"):
        vault.update_task(
            session.identifier,
            expected_revision=session.revision,
            add_refs=(REVIEW_SCOPE_REF,),
        )
    assert vault.get_task(session.identifier).revision == session.revision

    _append_raw_task_reference(vault, session, REVIEW_SCOPE_REF)

    doctor = doctor_dict(vault.doctor())
    assert doctor["healthy"] is False
    assert any(
        issue["code"] == "invalid-record"
        and issue["path"] == f"tasks/{session.identifier}.md"
        and "duplicate review reference" in issue["message"]
        for issue in doctor["issues"]
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor,
        integration={"available": True, "ready": True},
    )
    tasks = snapshot["projection"]["sections"]["tasks"]
    assert tasks["state"] == "partial"
    assert tasks["unreadable"] == 1
    assert tasks["issues"] == [
        {
            "code": "invalid-record",
            "message": "duplicate review reference",
            "path": f"tasks/{session.identifier}.md",
            "repairable": False,
        }
    ]
    assert session.identifier not in {task["identifier"] for task in snapshot["tasks"]}
    assert snapshot["doctor"]["healthy"] is False
    assert snapshot["portfolio"]["available"] is False
    assert snapshot["portfolio"]["review"]["state"] == "unavailable"


def test_exact_coverage_reenters_after_task_or_owner_revision_changes(vault: Vault) -> None:
    seeded = _seed(vault)
    session = seeded["session"]
    first = seeded["first"]
    owner = seeded["owner"]
    assert (
        hasattr(session, "revision") and hasattr(first, "revision") and hasattr(owner, "revision")
    )
    coverage = review_coverage_ref(
        task_id_value=first.identifier,
        task_revision=first.revision,
        work_thread_id=owner.identifier,
        work_thread_revision=owner.revision,
    )
    session = vault.update_task(
        session.identifier,
        expected_revision=session.revision,
        remove_refs=tuple(
            value
            for value in session.refs
            if value.startswith("review-subject:") or value.startswith("review-option:")
        ),
        add_refs=(
            coverage,
            "review-subject:task:second-outcome",
            review_option_ref(
                intent="keep",
                consequence="Keep this exact outcome as authored and continue the review.",
            ),
        ),
    )

    current = vault.inspect_portfolio().review
    assert current.state == "active"
    assert current.current_coverage_task_ids == (first.identifier,)
    assert current.revisit_task_ids == ()

    first = vault.update_task(
        first.identifier,
        expected_revision=first.revision,
        next_action="A newly grounded next step.",
    )
    changed_task = vault.inspect_portfolio().review
    assert changed_task.current_coverage_task_ids == ()
    assert changed_task.revisit_task_ids == (first.identifier,)
    changed_task_reason = changed_task.coverages[0].reason
    assert changed_task_reason is not None
    assert "task changed" in changed_task_reason

    # Re-anchor task coverage, then prove a WorkThread-only change also revisits it.
    session = vault.update_task(
        session.identifier,
        expected_revision=session.revision,
        remove_refs=(coverage,),
        add_refs=(
            review_coverage_ref(
                task_id_value=first.identifier,
                task_revision=first.revision,
                work_thread_id=owner.identifier,
                work_thread_revision=owner.revision,
            ),
        ),
    )
    owner = vault.update_thread(
        owner.identifier,
        expected_revision=owner.revision,
        summary="The storyline changed after this review check.",
    )
    changed_thread = vault.inspect_portfolio().review
    assert changed_thread.revisit_task_ids == (first.identifier,)
    changed_thread_reason = changed_thread.coverages[0].reason
    assert changed_thread_reason is not None
    assert "WorkThread changed" in changed_thread_reason


def test_no_thread_coverage_becomes_stale_when_an_owner_is_added(vault: Vault) -> None:
    seeded = _seed(vault)
    session = seeded["session"]
    second = seeded["second"]
    coverage = review_coverage_ref(
        task_id_value=second.identifier,
        task_revision=second.revision,
    )
    vault.update_task(
        session.identifier,
        expected_revision=session.revision,
        add_refs=(coverage,),
    )
    assert vault.inspect_portfolio().review.current_coverage_task_ids == (second.identifier,)

    vault.create_thread(
        identifier="thread:second-outcome",
        title="Second outcome storyline",
        purpose="Own the second exact outcome.",
        summary="This owner was added after coverage.",
        focus_task_id=second.identifier,
        task_ids=(second.identifier,),
    )

    inspected = vault.inspect_portfolio().review
    assert inspected.current_coverage_task_ids == ()
    assert inspected.revisit_task_ids == (second.identifier,)
    owner_reason = inspected.coverages[0].reason
    assert owner_reason is not None
    assert "gained a WorkThread" in owner_reason


def test_new_open_work_and_mistagged_scope_never_hide_from_review(vault: Vault) -> None:
    seeded = _seed(vault)
    imposter = vault.create_task(
        identifier="mistagged-outcome",
        title="Mistagged real outcome",
        outcome="Remain visible even though an erroneous scope tag exists.",
        status="ready",
        next_actor="human",
        refs=(REVIEW_SCOPE_REF,),
    )

    inspected = vault.inspect_portfolio().review
    assert imposter.identifier in inspected.open_task_ids
    assert imposter.identifier in inspected.new_open_task_ids
    assert imposter.identifier in inspected.uncovered_task_ids

    portfolio = seeded["portfolio"]
    direction = seeded["direction"]
    with pytest.raises(ValidationError, match="missing open tasks: mistagged-outcome"):
        vault.set_portfolio(
            expected_revision=portfolio.revision,
            direction_revision=direction.revision,
            summary=portfolio.summary,
            items=portfolio.items,
        )


def test_duplicate_review_sessions_and_stale_cas_fail_closed(vault: Vault) -> None:
    seeded = _seed(vault)
    session = seeded["session"]
    review_thread = seeded["review_thread"]
    duplicate = vault.create_task(
        identifier="duplicate-review-session",
        title="Duplicate review",
        outcome="Must never become a second current session.",
        status="doing",
        next_actor="agent",
        refs=(REVIEW_SCOPE_REF,),
    )

    with pytest.raises(ValidationError, match="exactly one nonterminal review session"):
        vault.update_thread(
            review_thread.identifier,
            expected_revision=review_thread.revision,
            task_ids=(session.identifier, duplicate.identifier),
        )

    with pytest.raises(ConflictError, match="record changed"):
        vault.update_thread(
            review_thread.identifier,
            expected_revision="f" * 64,
            summary="A stale writer must not win.",
        )


def test_focus_lifecycle_rejects_terminalizing_any_focused_task(vault: Vault) -> None:
    task = vault.create_task(
        identifier="focused-outcome",
        title="Focused outcome",
        outcome="Finish without leaving a terminal WorkThread focus.",
        status="doing",
        next_actor="agent",
    )
    thread = vault.create_thread(
        identifier="thread:focused-outcome",
        title="Focused outcome",
        purpose="Carry the exact outcome.",
        summary="The outcome is current.",
        focus_task_id=task.identifier,
        task_ids=(task.identifier,),
    )

    with pytest.raises(ValidationError, match="clear WorkThread focus"):
        vault.update_task(task.identifier, expected_revision=task.revision, status="done")

    thread = vault.update_thread(
        thread.identifier,
        expected_revision=thread.revision,
        clear_focus_task=True,
    )
    finished = vault.update_task(task.identifier, expected_revision=task.revision, status="done")
    assert finished.status == "done"
    assert thread.focus_task_id is None
    assert vault.doctor().healthy is True


def test_thread_creation_serializes_focus_validation_with_terminalization(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="racing-outcome",
        title="Racing outcome",
        outcome="Never publish a terminal focus through a creation race.",
        status="doing",
        next_actor="agent",
    )
    validation_entered = threading.Event()
    allow_publication = threading.Event()
    update_started = threading.Event()
    original_validate = vault._validate_thread_focus

    def pause_after_validation(thread: WorkThread, *, allow_unfocused_review: bool = False) -> None:
        original_validate(thread, allow_unfocused_review=allow_unfocused_review)
        if thread.identifier == "thread:racing-outcome":
            validation_entered.set()
            assert allow_publication.wait(timeout=5)

    monkeypatch.setattr(vault, "_validate_thread_focus", pause_after_validation)

    def terminalize() -> Task:
        update_started.set()
        return vault.update_task(task.identifier, expected_revision=task.revision, status="done")

    with ThreadPoolExecutor(max_workers=2) as executor:
        creating = executor.submit(
            vault.create_thread,
            identifier="thread:racing-outcome",
            title="Racing outcome",
            purpose="Carry the exact outcome.",
            summary="Creation is paused after validation.",
            focus_task_id=task.identifier,
            task_ids=(task.identifier,),
        )
        assert validation_entered.wait(timeout=5)
        terminalizing = executor.submit(terminalize)
        assert update_started.wait(timeout=5)
        allow_publication.set()
        created = creating.result(timeout=5)
        with pytest.raises(ValidationError, match="clear WorkThread focus"):
            terminalizing.result(timeout=5)

    assert created.focus_task_id == task.identifier
    assert vault.get_task(task.identifier).status == "doing"
    assert vault.doctor().healthy is True


def test_bdac8f7_review_session_requires_and_accepts_exact_cas_migration(
    vault: Vault,
) -> None:
    outcome, session = _seed_legacy_review(vault)
    inspected = vault.inspect_portfolio().review
    assert inspected.state == "conflict"
    assert inspected.session_task_id == session.identifier
    assert "migration" in (inspected.issue or "")
    assert session.identifier in inspected.open_task_ids
    assert outcome.identifier in inspected.open_task_ids

    with pytest.raises(ConflictError, match="record changed"):
        vault.migrate_legacy_review_session(
            session.identifier,
            expected_session_revision="f" * 64,
            expected_review_thread_revision="absent",
            thread_title="Life Portfolio review",
            thread_purpose="Carry finite all-open review sessions.",
            thread_summary="One exact legacy session is being resumed.",
        )

    migrated = vault.migrate_legacy_review_session(
        session.identifier,
        expected_session_revision=session.revision,
        expected_review_thread_revision="absent",
        thread_title="Life Portfolio review",
        thread_purpose="Carry finite all-open review sessions.",
        thread_summary="One exact legacy session is being resumed.",
    )
    reread_session = vault.get_task(session.identifier)
    assert migrated.focus_task_id == session.identifier
    assert migrated.task_ids == (session.identifier,)
    assert reread_session.revision == session.revision
    assert reread_session.active_thread_id == "legacy-review-hand"
    assert vault.inspect_portfolio().review.state == "active"

    fresh = Vault(vault.root)
    assert fresh.get_thread(migrated.identifier) == migrated
    assert fresh.get_task(session.identifier).active_thread_id == "legacy-review-hand"


def test_legacy_review_migration_rejects_a_bare_mistagged_outcome(vault: Vault) -> None:
    task = vault.create_task(
        identifier="mistagged-outcome",
        title="Mistagged outcome",
        outcome="Remain ordinary work despite an accidental review scope tag.",
        status="ready",
        next_actor="human",
        refs=(REVIEW_SCOPE_REF,),
    )

    with pytest.raises(ValidationError, match="structural session state"):
        vault.migrate_legacy_review_session(
            task.identifier,
            expected_session_revision=task.revision,
            expected_review_thread_revision="absent",
            thread_title="Life Portfolio review",
            thread_purpose="Carry finite all-open review sessions.",
            thread_summary="This thread must not be created from an accidental tag.",
        )

    with pytest.raises(NotFoundError):
        vault.get_thread(REVIEW_WORK_THREAD_ID)


def test_portfolio_inspection_detects_full_owner_relation_drift(vault: Vault) -> None:
    task = vault.create_task(
        identifier="owner-drift",
        title="Owner drift",
        outcome="Keep exact WorkThread ownership visible.",
        status="ready",
        next_actor="human",
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="The outcome is deliberately unthreaded.",
        items=(
            portfolio_item(
                task_id_value=task.identifier,
                task_revision=task.revision,
                stance="needs-human",
                reason="The ownership choice remains explicit.",
            ),
        ),
    )
    first_owner = vault.create_thread(
        identifier="thread:first-owner",
        title="First owner",
        purpose="Own the exact outcome.",
        summary="Ownership was added after Portfolio authorship.",
        focus_task_id=task.identifier,
        task_ids=(task.identifier,),
    )
    assert vault.inspect_portfolio().stale_portfolio_thread_ids == (first_owner.identifier,)

    portfolio = vault.set_portfolio(
        expected_revision=portfolio.revision,
        summary="The first owner is now anchored.",
        items=(
            portfolio_item(
                task_id_value=task.identifier,
                task_revision=task.revision,
                stance="needs-human",
                reason="The ownership choice remains explicit.",
                work_thread_id=first_owner.identifier,
                work_thread_revision=first_owner.revision,
            ),
        ),
    )
    assert portfolio.items[0].work_thread_id == first_owner.identifier
    second_owner = vault.create_thread(
        identifier="thread:second-owner",
        title="Second owner",
        purpose="Expose conflicting ownership.",
        summary="A second nonterminal owner now exists.",
        focus_task_id=task.identifier,
        task_ids=(task.identifier,),
    )
    assert set(vault.inspect_portfolio().stale_portfolio_thread_ids) == {
        first_owner.identifier,
        second_owner.identifier,
    }

    second_owner = vault.update_thread(
        second_owner.identifier,
        expected_revision=second_owner.revision,
        status="closed",
    )
    assert second_owner.status == "closed"
    first_owner = vault.update_thread(
        first_owner.identifier,
        expected_revision=first_owner.revision,
        clear_focus_task=True,
        task_ids=(),
    )
    assert vault.inspect_portfolio().stale_portfolio_thread_ids == (first_owner.identifier,)


def test_pause_is_nonterminal_and_terminal_cleanup_preserves_history(vault: Vault) -> None:
    seeded = _seed(vault)
    session = seeded["session"]
    review_thread = seeded["review_thread"]
    first = seeded["first"]
    owner = seeded["owner"]
    coverage = review_coverage_ref(
        task_id_value=first.identifier,
        task_revision=first.revision,
        work_thread_id=owner.identifier,
        work_thread_revision=owner.revision,
    )
    session = vault.update_task(
        session.identifier,
        expected_revision=session.revision,
        remove_refs=tuple(
            value
            for value in session.refs
            if value.startswith("review-subject:") or value.startswith("review-option:")
        ),
        add_refs=(
            coverage,
            REVIEW_PAUSED_REF,
            "review-subject:task:second-outcome",
            review_option_ref(
                intent="keep",
                consequence="Keep this exact outcome as authored when the review resumes.",
            ),
        ),
    )

    paused = vault.inspect_portfolio().review
    assert paused.state == "paused"
    assert session.status == "doing"
    assert session.active_thread_id == "review-hand-1"
    assert paused.current_subject_task_id == "second-outcome"

    review_thread = vault.update_thread(
        review_thread.identifier,
        expected_revision=review_thread.revision,
        clear_focus_task=True,
    )
    session = vault.update_task(
        session.identifier,
        expected_revision=session.revision,
        status="done",
    )

    assert session.active_thread_id is None
    assert REVIEW_SCOPE_REF in session.refs
    assert coverage in session.refs
    assert not any(value.startswith("review-subject:") for value in session.refs)
    assert not any(value.startswith("review-option:") for value in session.refs)
    assert REVIEW_PAUSED_REF not in session.refs
    assert review_thread.focus_task_id is None
    assert vault.inspect_portfolio().review.state == "available"

    portfolio = vault.get_portfolio()
    rewritten = vault.set_portfolio(
        expected_revision=portfolio.revision,
        direction_revision=portfolio.direction_revision,
        summary="The finished review leaves ordinary Portfolio authoring available.",
        items=portfolio.items,
    )
    assert rewritten.revision != portfolio.revision


def test_direction_and_portfolio_v2_are_cas_bound_but_v1_remains_readable(
    vault: Vault,
) -> None:
    seeded = _seed(vault)
    direction = seeded["direction"]
    portfolio = seeded["portfolio"]
    assert parse_direction(render_direction(direction)) == direction
    assert portfolio.format_version == 2
    assert portfolio.direction_revision == direction.revision
    assert parse_portfolio(render_portfolio(portfolio)) == portfolio

    with pytest.raises(ConflictError, match="record changed"):
        vault.set_direction(
            expected_revision="e" * 64,
            status="confirmed",
            current_chapter=direction.current_chapter,
            aims=direction.aims,
        )
    with pytest.raises(ConflictError, match="Direction changed"):
        vault.set_portfolio(
            expected_revision=portfolio.revision,
            direction_revision="d" * 64,
            summary=portfolio.summary,
            items=portfolio.items,
        )
    with pytest.raises(ValidationError, match="requires the current Direction revision"):
        vault.set_portfolio(
            expected_revision=portfolio.revision,
            direction_revision=None,
            summary=portfolio.summary,
            items=portfolio.items,
        )

    legacy_item = portfolio_item(
        task_id_value="legacy-outcome",
        task_revision="a" * 64,
        stance="keep-in-view",
        reason="Readable pre-Direction Portfolio item.",
    )
    legacy = replace(
        portfolio,
        items=(legacy_item,),
        direction_revision=None,
        format_version=1,
        revision="",
    )
    parsed_legacy = parse_portfolio(render_portfolio(legacy))
    assert parsed_legacy.format_version == 1
    assert parsed_legacy.direction_revision is None


def test_old_work_thread_without_focus_metadata_remains_readable() -> None:
    thread = new_thread(
        identifier="thread:legacy",
        title="Legacy thread",
        purpose="Remain readable after the optional focus field ships.",
        summary="No focus was authored.",
    )
    encoded = render_thread(thread).replace('"focus_task_id":null,', "")

    parsed = parse_thread(encoded)

    assert parsed.identifier == thread.identifier
    assert parsed.focus_task_id is None
    assert parsed.revision != thread.revision


def test_direction_file_is_optional_for_an_old_vault(tmp_path: Path) -> None:
    old = Vault(tmp_path / "old-vault")
    old.initialize(name="Old vault")

    assert not (old.root / "DIRECTION.md").exists()
    assert old.doctor().healthy is True


def test_direction_cli_and_mcp_expose_exact_cas_readback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "interfaces"
    Vault(vault_path).initialize(name="Direction interfaces")
    aim = json.dumps(
        {
            "id": "protect-attention",
            "title": "Protect attention",
            "desired_state": "Important work progresses without uncontrolled interruption.",
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
                "absent",
                "--status",
                "provisional",
                "--current-chapter",
                "Build a calmer whole-life operating rhythm.",
                "--aim-json",
                aim,
            ]
        )
        == 0
    )
    authored = json.loads(capsys.readouterr().out)["result"]
    assert authored["aims"][0]["identifier"] == "protect-attention"
    assert cli.main(["--json", "--vault", str(vault_path), "direction", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)["result"]
    assert shown == authored

    mcp_shown = mcp_server._call(
        "gsv_direction_show",
        {},
        vault=Vault(vault_path),
    )
    assert mcp_shown == authored
    stale = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "gsv_direction_set",
                "arguments": {
                    "aims": [json.loads(aim)],
                    "current_chapter": "A stale overwrite.",
                    "expected_revision": "f" * 64,
                    "status": "confirmed",
                },
            },
        },
        vault=Vault(vault_path),
    )
    assert stale is not None
    assert stale["result"]["isError"] is True
    assert "reload" in stale["result"]["content"][0]["text"]


def test_legacy_review_migration_is_exposed_by_cli_and_mcp(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_vault = Vault(tmp_path / "legacy-cli")
    cli_vault.initialize(name="Legacy CLI")
    _, cli_session = _seed_legacy_review(cli_vault)
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(cli_vault.root),
                "portfolio",
                "migrate-review-session",
                "--session-id",
                cli_session.identifier,
                "--expected-session-revision",
                cli_session.revision,
                "--expected-review-thread-revision",
                "absent",
                "--thread-title",
                "Life Portfolio review",
                "--thread-purpose",
                "Carry finite all-open review sessions.",
                "--thread-summary",
                "One exact legacy session is being resumed.",
            ]
        )
        == 0
    )
    cli_result = json.loads(capsys.readouterr().out)["result"]
    assert cli_result["focus_task_id"] == cli_session.identifier
    assert cli_vault.get_task(cli_session.identifier).revision == cli_session.revision

    mcp_vault = Vault(tmp_path / "legacy-mcp")
    mcp_vault.initialize(name="Legacy MCP")
    _, mcp_session = _seed_legacy_review(mcp_vault)
    mcp_result = mcp_server._call(
        "gsv_portfolio_migrate_review_session",
        {
            "session_id": mcp_session.identifier,
            "expected_session_revision": mcp_session.revision,
            "expected_review_thread_revision": "absent",
            "thread_title": "Life Portfolio review",
            "thread_purpose": "Carry finite all-open review sessions.",
            "thread_summary": "One exact legacy session is being resumed.",
        },
        vault=mcp_vault,
    )
    assert mcp_result["focus_task_id"] == mcp_session.identifier
    assert mcp_vault.get_task(mcp_session.identifier).active_thread_id == "legacy-review-hand"
