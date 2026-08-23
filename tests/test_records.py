from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.records import (
    REVIEW_SCOPE_REF,
    new_entity,
    new_task,
    new_thread,
    parse_entity,
    parse_review_references,
    parse_task,
    parse_thread,
    render_entity,
    render_task,
    render_thread,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_records_round_trip_unicode_and_stable_revisions() -> None:
    task = new_task(
        identifier="ship-atlas",
        title="Ship Atlas",
        outcome="Preserve a cafe note without losing Unicode: cafe\N{COMBINING ACUTE ACCENT}.",
        status="doing",
        next_actor="agent",
        next_action="Run verification.",
        refs=("spec:atlas",),
        observed_at=NOW,
    )
    entity = new_entity(
        identifier="person:alex-chen",
        title="Alex Chen",
        entity_type="person",
        summary="Owns review.",
        aliases=("A. Chen",),
        observed_at=NOW,
    )
    thread = new_thread(
        identifier="atlas",
        title="Atlas",
        purpose="Carry release context.",
        summary="Verification is pending.",
        task_ids=(task.identifier,),
        entity_ids=(entity.identifier,),
        observed_at=NOW,
    )

    assert parse_task(render_task(task)) == task
    assert parse_entity(render_entity(entity)) == entity
    assert parse_thread(render_thread(thread)) == thread
    assert len(task.revision) == 64


def test_task_round_trips_authored_rank_and_exact_active_hand() -> None:
    task = new_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check every outcome without equating checked with resolved.",
        status="doing",
        next_actor="agent",
        next_action="Present one exact outcome.",
        rank=17,
        active_thread_id="019f95fd-009e-7603-ab87-f9927cf31c4d",
        refs=("review-scope:all-open",),
        observed_at=NOW,
    )

    assert parse_task(render_task(task)) == task
    assert task.rank == 17
    assert task.active_thread_id == "019f95fd-009e-7603-ab87-f9927cf31c4d"


def test_typed_dispatch_fields_are_additive_and_nullable() -> None:
    legacy = new_task(
        identifier="legacy-task",
        title="Legacy task",
        outcome="Keep the existing task row unchanged.",
        observed_at=NOW,
    )
    legacy_stored = render_task(legacy)
    typed = new_task(
        identifier="typed-task",
        title="Typed task",
        outcome="Carry one explicit dispatch target.",
        status="ready",
        next_actor="agent",
        target_seat="worker-one",
        observed_at=NOW,
    )

    assert '"version":1' in legacy_stored.splitlines()[0]
    assert parse_task(legacy_stored) == legacy
    assert '"version":4' in render_task(typed).splitlines()[0]
    assert typed.claim_by == "2026-07-22T12:05:00.000000Z"
    assert typed.dispatch_id is None
    assert typed.blocker_condition is None
    assert parse_task(render_task(typed)) == typed


def test_task_versions_only_the_multi_subject_review_shape() -> None:
    single = new_task(
        identifier="single-subject-review",
        title="Single-subject review",
        outcome="Remain readable by the original task grammar.",
        refs=(REVIEW_SCOPE_REF, "review-subject:task:first-outcome"),
        observed_at=NOW,
    )
    multiple = new_task(
        identifier="multi-subject-review",
        title="Multi-subject review",
        outcome="Carry one bounded prepared intervention set.",
        refs=(
            REVIEW_SCOPE_REF,
            "review-subject:task:first-outcome",
            "review-subject:task:second-outcome",
        ),
        observed_at=NOW,
    )

    single_stored = render_task(single)
    multiple_stored = render_task(multiple)
    assert '"version":1' in single_stored.splitlines()[0]
    assert '"version":2' in multiple_stored.splitlines()[0]
    assert parse_task(single_stored) == single
    assert parse_task(multiple_stored) == multiple
    assert (
        '"version":1'
        in render_entity(
            new_entity(
                identifier="system:record-version",
                title="Record version",
                entity_type="system",
                summary="Entity grammar remains version one.",
                observed_at=NOW,
            )
        ).splitlines()[0]
    )
    assert (
        '"version":1'
        in render_thread(
            new_thread(
                identifier="version-proof",
                title="Version proof",
                purpose="Keep the WorkThread grammar unchanged.",
                summary="Only the expanded Task shape needs version two.",
                observed_at=NOW,
            )
        ).splitlines()[0]
    )


@pytest.mark.parametrize(
    ("stored_version", "subject_count", "message"),
    (
        (1, 2, "version 1 supports at most one"),
        (2, 1, "version 2 requires multiple"),
        (2, 26, "more than 25"),
        (3, 2, "unsupported record version"),
        (True, 1, "unsupported record version"),
        (1.0, 1, "unsupported record version"),
    ),
)
def test_task_version_and_review_subject_grammar_fail_closed(
    stored_version: object,
    subject_count: int,
    message: str,
) -> None:
    refs = (
        REVIEW_SCOPE_REF,
        *(f"review-subject:task:outcome-{index}" for index in range(subject_count)),
    )
    base = new_task(
        identifier="stored-review-version",
        title="Stored review version",
        outcome="Reject a version and shape mismatch.",
        refs=(REVIEW_SCOPE_REF, "review-subject:task:seed-outcome"),
        observed_at=NOW,
    )
    header, body = render_task(base).split("\n", 1)
    metadata = json.loads(header.removeprefix("<!-- gsv:").removesuffix(" -->"))
    metadata["version"] = stored_version
    metadata["refs"] = list(refs)
    malformed = (
        "<!-- gsv:"
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + " -->\n"
        + body
    )

    with pytest.raises(ValidationError, match=message):
        parse_task(malformed)


@pytest.mark.parametrize(
    "reference",
    [
        REVIEW_SCOPE_REF,
        "review-subject:task:exact-outcome",
        f"review-covered:task:exact-outcome@{'a' * 64}",
        "review-option:keep:task:exact-outcome:Keep%20the%20exact%20outcome.",
    ],
    ids=("scope", "subject", "coverage", "option"),
)
def test_exact_duplicate_review_references_fail_on_parse_and_write(reference: str) -> None:
    base_refs = (
        (REVIEW_SCOPE_REF, "review-subject:task:exact-outcome")
        if reference.startswith("review-option:")
        else (REVIEW_SCOPE_REF,)
    )
    refs = (
        (reference, reference)
        if reference == REVIEW_SCOPE_REF
        else (*base_refs, reference, reference)
    )

    with pytest.raises(ValidationError, match="duplicate review reference"):
        parse_review_references(refs)
    with pytest.raises(ValidationError, match="duplicate review reference"):
        new_task(
            identifier="duplicate-review-ref",
            title="Duplicate review ref",
            outcome="Reject ambiguous review control state.",
            refs=refs,
            observed_at=NOW,
        )

    valid_refs = (reference,) if reference == REVIEW_SCOPE_REF else (*base_refs, reference)
    task = new_task(
        identifier="raw-duplicate-review-ref",
        title="Raw duplicate review ref",
        outcome="Reject an invalid record loaded from disk.",
        refs=valid_refs,
        observed_at=NOW,
    )
    rendered = render_task(task)
    header, body = rendered.split("\n", 1)
    metadata = json.loads(header.removeprefix("<!-- gsv:").removesuffix(" -->"))
    metadata["refs"].append(reference)
    malformed = (
        "<!-- gsv:"
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + " -->\n"
        + body
    )
    with pytest.raises(ValidationError, match="duplicate review reference"):
        parse_task(malformed)


def test_ordinary_duplicate_references_remain_backward_compatible() -> None:
    task = new_task(
        identifier="ordinary-duplicate-ref",
        title="Ordinary duplicate ref",
        outcome="Keep the established normalization contract for ordinary references.",
        refs=("source:one", "source:one"),
        observed_at=NOW,
    )

    assert task.refs == ("source:one",)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"identifier": "UPPER CASE"}, "task ID"),
        ({"title": "line one\nline two"}, "title"),
        ({"outcome": "bad\n## Next action\ninjection"}, "level-two"),
    ],
)
def test_task_rejects_unsafe_values(values: dict[str, Any], message: str) -> None:
    defaults: dict[str, Any] = {
        "identifier": "safe-task",
        "title": "Safe",
        "outcome": "Safe outcome",
    }
    with pytest.raises(ValidationError, match=message):
        new_task(**(defaults | values), observed_at=NOW)


def test_terminal_task_cannot_claim_future_work() -> None:
    with pytest.raises(ValidationError, match="terminal tasks"):
        new_task(
            identifier="done-task",
            title="Done",
            outcome="Finished.",
            status="done",
            next_actor="agent",
            observed_at=NOW,
        )

    with pytest.raises(ValidationError, match="terminal tasks"):
        new_task(
            identifier="done-review",
            title="Done review",
            outcome="Finished.",
            status="done",
            active_thread_id="review-hand",
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("review-subject:task:one-outcome", "current subject"),
        ("review-state:paused", "remain paused"),
        (
            "review-option:keep:task:one-outcome:Leave%20it%20unchanged",
            "current options",
        ),
    ],
)
def test_terminal_review_session_cannot_retain_current_navigation(
    reference: str,
    message: str,
) -> None:
    refs = (
        (REVIEW_SCOPE_REF, "review-subject:task:one-outcome", reference)
        if reference.startswith("review-option:")
        else (REVIEW_SCOPE_REF, reference)
    )
    with pytest.raises(ValidationError, match=message):
        new_task(
            identifier="done-review-navigation",
            title="Done review navigation",
            outcome="The bounded review ended.",
            status="done",
            refs=refs,
            observed_at=NOW,
        )


def test_terminal_thread_invariant_is_enforced_during_parse() -> None:
    thread = new_thread(
        identifier="thread:closed-invariant",
        title="Closed invariant",
        purpose="Reject contradictory stored state.",
        summary="Synthetic.",
        next_move="This must not survive closure.",
        observed_at=NOW,
    )
    malformed = render_thread(thread).replace('"status":"active"', '"status":"closed"')

    with pytest.raises(ValidationError, match="terminal threads"):
        parse_thread(malformed)


def test_revision_changes_with_canonical_content() -> None:
    task = new_task(
        identifier="revision-test",
        title="Revision test",
        outcome="First outcome.",
        observed_at=NOW,
    )
    changed = replace(task, outcome="Second outcome.", revision="")
    changed = parse_task(render_task(changed))

    assert changed.revision != task.revision


def test_parser_rejects_unknown_or_reordered_sections() -> None:
    task = new_task(
        identifier="section-test",
        title="Section test",
        outcome="Outcome.",
        observed_at=NOW,
    )
    malformed = render_task(task).replace("## Next action", "## Surprise")

    with pytest.raises(ValidationError, match="record sections"):
        parse_task(malformed)


def test_literal_missing_marker_round_trips_as_user_content() -> None:
    task = new_task(
        identifier="literal-marker",
        title="Literal marker",
        outcome="Keep exact content.",
        next_action="Not recorded.",
        observed_at=NOW,
    )

    assert task.next_action == "Not recorded."
    assert parse_task(render_task(task)).next_action == "Not recorded."


def test_empty_actor_is_not_a_silent_clear_operation() -> None:
    with pytest.raises(ValidationError, match="invalid next actor"):
        new_task(
            identifier="empty-actor",
            title="Empty actor",
            outcome="Reject ambiguity.",
            next_actor="",
            observed_at=NOW,
        )


def test_agent_run_field_serialization_and_validation() -> None:
    yes_task = new_task(
        identifier="agent-run-yes",
        title="Agent run yes",
        outcome="Explicitly run agent.",
        agent_run="yes",
        observed_at=NOW,
    )
    no_task = new_task(
        identifier="agent-run-no",
        title="Agent run no",
        outcome="Explicitly refuse agent.",
        agent_run="no",
        observed_at=NOW,
    )
    unset_task = new_task(
        identifier="agent-run-unset",
        title="Agent run unset",
        outcome="Unset agent run.",
        observed_at=NOW,
    )

    yes_rendered = render_task(yes_task)
    assert '"agent_run":"yes"' in yes_rendered.splitlines()[0]
    assert '"version":4' in yes_rendered.splitlines()[0]
    assert parse_task(yes_rendered) == yes_task
    assert parse_task(yes_rendered).agent_run == "yes"

    no_rendered = render_task(no_task)
    assert '"agent_run":"no"' in no_rendered.splitlines()[0]
    assert '"version":4' in no_rendered.splitlines()[0]
    assert parse_task(no_rendered) == no_task
    assert parse_task(no_rendered).agent_run == "no"

    unset_rendered = render_task(unset_task)
    assert "agent_run" not in unset_rendered.splitlines()[0]
    assert parse_task(unset_rendered).agent_run is None

    for invalid in ("maybe", "true", "false", "1", "0", ""):
        with pytest.raises(ValidationError, match="invalid agent run"):
            new_task(
                identifier="invalid-agent-run",
                title="Invalid agent run",
                outcome="Outcome.",
                agent_run=invalid,
                observed_at=NOW,
            )
