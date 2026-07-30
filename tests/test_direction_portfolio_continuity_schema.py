from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from continuity_kernel.direction import (
    Direction,
    DirectionAim,
    direction_aim,
    direction_dict,
    new_direction,
    parse_direction,
    render_direction,
)
from continuity_kernel.errors import ValidationError
from continuity_kernel.portfolio import (
    Portfolio,
    new_portfolio,
    parse_portfolio,
    portfolio_dict,
    portfolio_item,
    render_portfolio,
)

UPDATED = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
UPDATED_TEXT = "2026-07-29T12:00:00.000000Z"


def _with_metadata(markdown: str, **changes: object) -> str:
    first, remainder = markdown.split("\n", 1)
    prefix = (
        "<!-- gsv-direction:" if first.startswith("<!-- gsv-direction:") else "<!-- gsv-portfolio:"
    )
    metadata = json.loads(first.removeprefix(prefix).removesuffix(" -->"))
    metadata.update(changes)
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{prefix}{header} -->\n{remainder}"


def _aim() -> DirectionAim:
    return direction_aim(
        identifier="keep-the-week-coherent",
        title="Keep the week coherent",
        desired_state="Important commitments have an explicit next move.",
    )


def test_direction_v1_bytes_remain_stable_and_reject_rich_field_drift() -> None:
    metadata = {
        "aims": [
            {
                "desired_state": "Important commitments have an explicit next move.",
                "identifier": "keep-the-week-coherent",
                "title": "Keep the week coherent",
            }
        ],
        "id": "direction:current",
        "kind": "direction",
        "status": "confirmed",
        "updated_at": UPDATED_TEXT,
        "version": 1,
    }
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    legacy = (
        f"<!-- gsv-direction:{header} -->\n\n# Direction\n\n## Current chapter\n"
        "Keep the operating picture current.\n"
    )

    parsed = parse_direction(legacy)

    assert parsed.format_version == 1
    assert render_direction(parsed) == legacy
    assert set(direction_dict(parsed)) == {
        "aims",
        "current_chapter",
        "revision",
        "status",
        "updated_at",
    }
    with pytest.raises(ValidationError, match="version 1 cannot contain"):
        render_direction(replace(parsed, refs=("source:example",)))
    with pytest.raises(ValidationError, match="unsupported shape"):
        parse_direction(_with_metadata(legacy, refs=[]))
    with pytest.raises(ValidationError, match="unsupported Direction version"):
        parse_direction(_with_metadata(legacy, version=True))


def test_direction_v2_round_trip_preserves_bounded_continuity_without_derived_outcomes() -> None:
    direction = new_direction(
        status="confirmed",
        current_chapter="Keep the operating picture current.",
        aims=(_aim(),),
        observed_at=UPDATED,
        constraints=("Do not expose private provider bodies.",),
        tensions=("Stay current without creating noise.",),
        refs=("source:local-context",),
        source_observed_at="2026-07-29T11:30:00.000000Z",
        recorded_at="2026-07-29T11:45:00.000000Z",
        recheck_at="2026-07-29T13:00:00.000000Z",
        history=("2026-07-29T12:00:00Z — Imported exact authored continuity.",),
    )
    rendered = render_direction(direction)

    assert direction.format_version == 2
    assert parse_direction(rendered) == direction
    assert "desired_outcomes" not in rendered
    with pytest.raises(ValidationError, match="version 2 requires continuity"):
        render_direction(
            Direction(
                status="confirmed",
                current_chapter=direction.current_chapter,
                aims=direction.aims,
                updated_at=direction.updated_at,
                revision="",
                format_version=2,
            )
        )
    with pytest.raises(ValidationError, match="later than updated_at"):
        render_direction(replace(direction, recheck_at=direction.updated_at))


def test_portfolio_v1_and_v2_bytes_remain_stable() -> None:
    item = portfolio_item(
        task_id_value="keep-one-outcome-current",
        task_revision="a" * 64,
        stance="keep-in-view",
        reason="It remains part of the current picture.",
    )
    legacy = Portfolio(
        summary="One exact authored outcome.",
        items=(item,),
        updated_at=UPDATED_TEXT,
        revision="",
    )
    v2 = replace(
        legacy,
        items=(replace(item, direction_aim_ids=("keep-the-week-coherent",)),),
        direction_revision="b" * 64,
        format_version=2,
    )

    for record in (legacy, v2):
        rendered = render_portfolio(record)
        parsed = parse_portfolio(rendered)
        assert render_portfolio(parsed) == rendered
        assert "source_position" not in portfolio_dict(parsed)["items"][0]


def test_portfolio_v3_keeps_public_revisions_and_source_anchors_separate() -> None:
    first = portfolio_item(
        task_id_value="first-outcome",
        task_revision="a" * 64,
        stance="needs-human",
        reason="A decision is needed.",
        direction_aim_ids=("keep-the-week-coherent",),
        source_position=10,
        source_task_updated_at="2026-07-29T10:30:00.000000Z",
    )
    second = portfolio_item(
        task_id_value="second-outcome",
        task_revision="b" * 64,
        stance="agent-can-carry",
        reason="The next local move is explicit.",
        work_thread_id="thread:second-outcome",
        work_thread_revision="c" * 64,
        direction_aim_ids=("keep-the-week-coherent",),
        source_position=20,
        source_task_updated_at="2026-07-29T10:45:00.000000Z",
        source_thread_updated_at="2026-07-29T10:50:00.000000Z",
    )
    portfolio = new_portfolio(
        summary="Two exact authored outcomes.",
        items=(first, second),
        direction_revision="d" * 64,
        observed_at=UPDATED,
        source_direction_updated_at="2026-07-29T10:00:00.000000Z",
        refs=("source:resident-portfolio",),
        source_observed_at="2026-07-29T11:30:00.000000Z",
        recorded_at="2026-07-29T11:45:00.000000Z",
        review_after="2026-07-29T13:00:00.000000Z",
        history=("2026-07-29T12:00:00Z — Imported exact authored continuity.",),
    )
    rendered = render_portfolio(portfolio)

    parsed = parse_portfolio(rendered)
    assert portfolio.format_version == 3
    assert parsed == portfolio
    assert parsed.items[1].task_revision == "b" * 64
    assert parsed.items[1].source_task_updated_at == "2026-07-29T10:45:00.000000Z"
    assert parsed.items[1].work_thread_revision == "c" * 64
    assert parsed.items[1].source_thread_updated_at == "2026-07-29T10:50:00.000000Z"
    assert "review_thread_id" not in rendered

    with pytest.raises(ValidationError, match="unsupported shape"):
        parse_portfolio(_with_metadata(rendered, review_thread_id="thread:life-portfolio-review"))
    with pytest.raises(ValidationError, match="source position order"):
        render_portfolio(
            replace(
                portfolio,
                items=(
                    replace(portfolio.items[0], source_position=30),
                    replace(portfolio.items[1], source_position=20),
                ),
            )
        )
    with pytest.raises(ValidationError, match="version 2 cannot contain"):
        render_portfolio(replace(portfolio, format_version=2))
    with pytest.raises(ValidationError, match="requires source position and task timestamp"):
        render_portfolio(
            replace(
                portfolio,
                items=(
                    replace(portfolio.items[0], source_task_updated_at=None),
                    portfolio.items[1],
                ),
            )
        )
    with pytest.raises(ValidationError, match="unsupported Portfolio version"):
        parse_portfolio(_with_metadata(rendered, version=True))
