from __future__ import annotations

import json
from dataclasses import replace

import pytest

from continuity_kernel.records import new_task
from continuity_kernel.review_sheet import (
    MAX_REVIEW_SHEET_BYTES,
    bind_review_sheet,
    parse_review_reply,
)


def _entry(task: str = "first-outcome", **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "task": task,
        "anchor": "2026-07-27T08:00:00Z",
        "question": "Should this remain the next commitment?",
        "recommendation": "Keep it open and make the next action concrete.",
        "reasoning": "The dependency is clear and delaying it blocks a second outcome.",
        "choices": [
            {
                "answer": "Keep it open and do the named next action.",
                "effect": "The dependent outcome remains unblocked.",
                "recommended": True,
            },
            "Defer it and keep the dependency visible.",
        ],
    }
    value.update(changes)
    return value


def _answer(*entries: dict[str, object], suffix: str = "") -> str:
    payload = json.dumps({"v": 1, "entries": list(entries)}, separators=(",", ":"))
    return f"I prepared only the decisions that need you.\n```bridge-sheet\n{payload}\n```{suffix}"


def test_review_sheet_is_visible_bounded_and_bound_to_exact_subjects() -> None:
    reply = parse_review_reply(
        _answer(
            _entry(),
            _entry(
                "second-outcome",
                dissent="You have deferred this repeatedly; I think that is now the wrong call.",
                group="One dependency chain",
            ),
        )
    )

    assert reply.message == "I prepared only the decisions that need you."
    assert tuple(entry.task for entry in reply.sheet) == ("first-outcome", "second-outcome")
    assert reply.sheet[0].choices[0].recommended is True
    assert reply.sheet[1].dissent.startswith("You have deferred")
    first = new_task(
        identifier="first-outcome",
        title="First",
        outcome="Finish the first outcome.",
        observed_at=None,
    )
    second = new_task(
        identifier="second-outcome",
        title="Second",
        outcome="Finish the second outcome.",
        observed_at=None,
    )
    first = replace(first, updated_at="2026-07-27T08:00:00Z")
    second = replace(second, updated_at="2026-07-27T08:01:00Z")
    bound = bind_review_sheet(
        reply.sheet,
        subject_task_ids=("second-outcome", "first-outcome"),
        task_by_id={first.identifier: first, second.identifier: second},
    )

    assert bound[0].stale is False
    assert bound[0].current_anchor == "2026-07-27T08:00:00Z"
    assert bound[1].stale is True
    assert (
        bind_review_sheet(
            reply.sheet,
            subject_task_ids=("first-outcome",),
            task_by_id={first.identifier: first, second.identifier: second},
        )
        == ()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {"v": 2, "entries": value["entries"]},
        lambda value: {"v": 1, "entries": []},
        lambda value: {"v": 1, "entries": [value["entries"][0], value["entries"][0]]},
        lambda value: {"v": 1, "entries": [{**value["entries"][0], "hidden": "value"}]},
        lambda value: {"v": 1, "entries": [{**value["entries"][0], "reasoning": "x" * 601}]},
        lambda value: {"v": 1, "entries": [{**value["entries"][0], "question": "a\u202eb"}]},
        lambda value: {"v": 1, "entries": [{**value["entries"][0], "choices": ["one"]}]},
        lambda value: {
            "v": 1,
            "entries": [
                {
                    **value["entries"][0],
                    "choices": [
                        {"answer": "one", "recommended": True},
                        {"answer": "two", "recommended": True},
                    ],
                }
            ],
        },
    ],
)
def test_review_sheet_rejects_malformed_or_partially_trusted_payloads(mutate: object) -> None:
    payload = mutate({"v": 1, "entries": [_entry()]})  # type: ignore[operator]
    answer = (
        "Visible result.\n```bridge-sheet\n" + json.dumps(payload, separators=(",", ":")) + "\n```"
    )

    reply = parse_review_reply(answer)

    assert reply.message == "Visible result."
    assert reply.sheet == ()


def test_review_sheet_must_be_the_one_terminal_envelope() -> None:
    valid = _answer(_entry(), suffix="\nMore prose.")
    mixed = valid + '\n```bridge-choices\n{"v":1,"choices":["one","two"]}\n```'

    assert parse_review_reply(valid).sheet == ()
    assert parse_review_reply(mixed).sheet == ()


def test_review_reply_hides_incomplete_terminal_envelope_from_visible_message() -> None:
    reply = parse_review_reply(
        'The safe visible answer.\n```bridge-sheet\n{"v":1,"entries":[{"task":"partial'
    )

    assert reply.message == "The safe visible answer."
    assert reply.sheet == ()


def test_review_reply_rejects_sheet_over_byte_limit() -> None:
    oversized = "x" * MAX_REVIEW_SHEET_BYTES
    answer = f'Visible answer.\n```bridge-sheet\n{{"v":1,"entries":[],"pad":"{oversized}"}}\n```'

    reply = parse_review_reply(answer)

    assert reply.message == "Visible answer."
    assert reply.sheet == ()
