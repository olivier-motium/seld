from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.records import (
    Entity,
    EntityMergeAbsorption,
    EntityRelationship,
    TaskEntityLink,
    WorkThread,
    WorkThreadEntityLink,
    WorkThreadTaskLink,
    new_task,
    new_thread,
    parse_entity,
    parse_task,
    parse_thread,
    render_entity,
    render_task,
    render_thread,
    thread_task_links,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
T10 = "2026-07-29T10:00:00.000000Z"
T11 = "2026-07-29T11:00:00.000000Z"
T12 = "2026-07-29T12:00:00.000000Z"
T13 = "2026-07-29T13:00:00.000000Z"


def _with_metadata(markdown: str, **updates: object) -> str:
    header, body = markdown.split("\n", 1)
    metadata = json.loads(header.removeprefix("<!-- gsv:").removesuffix(" -->"))
    metadata.update(updates)
    return (
        "<!-- gsv:"
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + " -->\n"
        + body
    )


def test_task_v3_round_trips_private_continuity_and_multi_subject_review() -> None:
    base = new_task(
        identifier="carry-resident-state",
        title="Carry resident state",
        outcome="Keep the same durable outcome after migration.",
        status="doing",
        next_actor="agent",
        next_action="Verify the migrated record.",
        rank=7,
        refs=(
            "review-scope:all-open",
            "review-subject:task:first-outcome",
            "review-subject:task:second-outcome",
        ),
        project="Seld",
        entity_links=(TaskEntityLink("product", "project:seld"),),
        workspace="/tmp/seld",
        attention_at="2026-07-30",
        due="2026-08-01",
        codex_episode_ids=("019f0000-0000-7000-8000-000000000777",),
        history=(f"{T12} — Migrated without losing task history.",),
        observed_at=NOW,
    )
    task = replace(base, active_thread_id="019f0000-0000-7000-8000-000000000777")

    stored = render_task(task)
    parsed = parse_task(stored)

    assert '"version":3' in stored.splitlines()[0]
    assert render_task(parsed) == stored
    assert parsed.active_thread_id == "019f0000-0000-7000-8000-000000000777"
    assert parsed.entity_links == (TaskEntityLink("product", "project:seld"),)
    assert parsed.codex_episode_ids == ("019f0000-0000-7000-8000-000000000777",)
    assert parsed.history == (f"{T12} — Migrated without losing task history.",)

    with pytest.raises(ValidationError, match="unsupported field surprise"):
        parse_task(_with_metadata(stored, surprise="must not disappear"))


def test_task_v1_stays_bijective_and_has_honest_unknown_rich_fields() -> None:
    legacy = (
        '<!-- gsv:{"active_thread_id":null,"created_at":"2026-07-29T12:00:00.000000Z",'
        '"id":"legacy-task","kind":"task","next_action_present":false,'
        '"next_actor":null,"rank":null,"refs":[],"status":"captured",'
        '"updated_at":"2026-07-29T12:00:00.000000Z","version":1,'
        '"waiting_on_present":false} -->\n\n'
        "# Legacy task\n\n"
        "## Outcome\nLegacy outcome.\n\n"
        "## Next action\nNot recorded.\n\n"
        "## Waiting on\nNot recorded.\n"
    )

    parsed = parse_task(legacy)

    assert parsed.state_changed_at is None
    assert parsed.history == ()
    assert parsed.entity_links == ()
    assert parsed.codex_episode_ids == ()
    assert render_task(parsed) == legacy


def test_task_v3_preserves_unresolved_supersession_without_inventing_a_redirect() -> None:
    task = new_task(
        identifier="historical-unresolved-supersession",
        title="Historical unresolved supersession",
        outcome="Preserve the source record exactly even when no successor was recorded.",
        status="superseded",
        history=(f"{T12} — Imported as superseded without a known replacement.",),
        observed_at=NOW,
    )

    parsed = parse_task(render_task(task))

    assert parsed.status == "superseded"
    assert parsed.superseded_by is None
    assert parsed.history == task.history

    with pytest.raises(ValidationError, match="only superseded tasks"):
        parse_task(_with_metadata(render_task(task), status="done", superseded_by="other-task"))


def test_entity_v2_preserves_temporal_edges_redirect_recovery_and_history() -> None:
    entity = Entity(
        identifier="person:current",
        title="Current person",
        entity_type="person",
        aliases=("Current",),
        summary="A canonical identity with temporal evidence.",
        refs=("source:bounded",),
        created_at=T10,
        updated_at=T13,
        revision="",
        status="merged",
        relationships=(
            EntityRelationship(
                predicate="worked-with",
                target="person:colleague",
                status="historical",
                recorded_at=T10,
                valid_from=T10,
                valid_to=T11,
                refs=("source:relationship",),
            ),
        ),
        observed_at=T12,
        recheck_at=None,
        merged_into="person:replacement",
        merged_at=T12,
        merged_from_updated_at=T11,
        merge_absorptions=(
            EntityMergeAbsorption(
                source_id="person:earlier-duplicate",
                source_updated_at=T10,
                merged_at=T11,
            ),
        ),
        history=(f"{T13} — Redirected only after explicit identity judgment.",),
    )

    stored = render_entity(entity)
    parsed = parse_entity(stored)

    assert '"version":2' in stored.splitlines()[0]
    assert parsed == replace(entity, revision=parsed.revision)
    assert parsed.relationships[0].valid_to == T11
    assert parsed.merge_absorptions[0].source_id == "person:earlier-duplicate"

    with pytest.raises(ValidationError, match="unsupported field surprise"):
        parse_entity(_with_metadata(stored, surprise=[]))


def test_work_thread_v2_keeps_typed_positions_and_derived_compatibility_ids() -> None:
    task_links = tuple(
        WorkThreadTaskLink(position=index * 10, task_id=f"outcome-{index}")
        for index in range(1, 202)
    )
    thread = WorkThread(
        identifier="thread:resident-cutover",
        title="Resident cutover",
        status="active",
        purpose="Carry one long-running concern.",
        summary="The exact migration is being verified.",
        next_move="Run the parity readback.",
        focus_task_id="outcome-1",
        task_links=task_links,
        entity_links=(WorkThreadEntityLink("primary", "project:seld"),),
        refs=("source:cutover",),
        created_at=T10,
        updated_at=T12,
        revision="",
        closure_condition="The live public stack retains every capability.",
        next_actor="agent",
        waiting_on=None,
        observed_at=T11,
        state_changed_at=T10,
        history=(f"{T12} — Preserved positioned task membership.",),
    )

    stored = render_thread(thread)
    parsed = parse_thread(stored)

    assert '"version":2' in stored.splitlines()[0]
    assert parsed == replace(thread, revision=parsed.revision)
    assert len(parsed.task_ids) == 201
    assert parsed.task_ids[0] == "outcome-1"
    assert parsed.task_links[-1].position == 2010
    assert parsed.entity_ids == ("project:seld",)

    with pytest.raises(ValidationError, match="too many task links"):
        thread_task_links(
            tuple(
                WorkThreadTaskLink(position=index, task_id=f"overflow-{index}")
                for index in range(1, 514)
            )
        )


def test_legacy_work_thread_roles_remain_unknown_instead_of_being_inferred() -> None:
    legacy = new_thread(
        identifier="thread:legacy-links",
        title="Legacy links",
        purpose="Preserve the old relation without inventing a role.",
        summary="One legacy task and entity remain linked.",
        task_ids=("legacy-task",),
        entity_ids=("project:legacy",),
        observed_at=NOW,
    )

    stored = render_thread(legacy)
    parsed = parse_thread(stored)

    assert '"version":1' in stored.splitlines()[0]
    assert parsed.task_links == (WorkThreadTaskLink(1, "legacy-task"),)
    assert parsed.entity_links == (WorkThreadEntityLink(None, "project:legacy"),)
    assert parsed.task_ids == ("legacy-task",)
    assert parsed.entity_ids == ("project:legacy",)
    assert render_thread(parsed) == stored


def test_pre_focus_work_thread_shape_remains_readable_but_strict() -> None:
    stored = render_thread(
        new_thread(
            identifier="thread:pre-focus",
            title="Pre-focus thread",
            purpose="Remain readable without inventing a focus.",
            summary="This record predates focus metadata.",
            observed_at=NOW,
        )
    )
    metadata, body = stored.split("\n", 1)
    parsed_metadata = json.loads(metadata.removeprefix("<!-- gsv:").removesuffix(" -->"))
    parsed_metadata.pop("focus_task_id")
    historical = (
        "<!-- gsv:"
        + json.dumps(parsed_metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + " -->\n"
        + body
    )

    parsed = parse_thread(historical)

    assert parsed.focus_task_id is None
    with pytest.raises(ValidationError, match="unsupported field surprise"):
        parse_thread(_with_metadata(historical, surprise="must not disappear"))
