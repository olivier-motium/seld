from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.records import (
    new_entity,
    new_task,
    new_thread,
    parse_entity,
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


def test_closed_thread_invariant_is_enforced_during_parse() -> None:
    thread = new_thread(
        identifier="thread:closed-invariant",
        title="Closed invariant",
        purpose="Reject contradictory stored state.",
        summary="Synthetic.",
        next_move="This must not survive closure.",
        observed_at=NOW,
    )
    malformed = render_thread(thread).replace('"status":"active"', '"status":"closed"')

    with pytest.raises(ValidationError, match="closed threads"):
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
