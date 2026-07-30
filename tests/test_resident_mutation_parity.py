from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from continuity_kernel.errors import ConflictError, MutationCommittedError, ValidationError
from continuity_kernel.records import (
    REVIEW_WORK_THREAD_ID,
    TaskEntityLink,
    WorkThreadEntityLink,
    WorkThreadTaskLink,
)
from continuity_kernel.vault import Vault

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    result = Vault(tmp_path / "vault")
    result.initialize(name="Resident mutation parity")
    return result


def _entity(vault: Vault, identifier: str, entity_type: str = "person") -> None:
    vault.create_entity(
        identifier=identifier,
        title=identifier.split(":", 1)[1].replace("-", " ").title(),
        entity_type=entity_type,
        summary=f"Exact test identity for {identifier}.",
        observed_at=NOW,
    )


def _task(vault: Vault, identifier: str) -> None:
    vault.create_task(
        identifier=identifier,
        title=identifier.replace("-", " ").title(),
        outcome=f"Complete {identifier} without losing continuity.",
        status="ready",
        observed_at=NOW,
    )


def test_task_rich_patch_transfer_supersede_reopen_and_fresh_read(vault: Vault) -> None:
    _entity(vault, "person:owner")
    replacement = vault.create_task(
        identifier="replacement",
        title="Replacement",
        outcome="Retain the canonical successor.",
        status="ready",
        observed_at=NOW,
    )
    previous = vault.create_task(
        identifier="previous-owner",
        title="Previous owner",
        outcome="Retain this execution episode after transfer.",
        status="waiting",
        next_actor="human",
        waiting_on="An exact handoff.",
        active_thread_id="codex-episode-1",
        observed_at=NOW,
    )
    current = vault.create_task(
        identifier="current-owner",
        title="Current owner",
        outcome="Carry the complete resident task shape.",
        status="ready",
        project="Seld",
        workspace="/tmp/seld-worktree",
        attention_at="2026-07-30",
        due="2026-08-01",
        entity_links=(TaskEntityLink("owner", "person:owner"),),
        observed_at=NOW,
    )

    claimed = vault.update_task(
        current.identifier,
        expected_revision=current.revision,
        status="doing",
        next_actor="agent",
        next_action="Continue from exact state.",
        active_thread_id="codex-episode-1",
        add_codex_episode_ids=("codex-episode-0",),
        note="Transferred after the prior task stopped doing work",
        observed_at=NOW + timedelta(minutes=1),
    )

    released = Vault(vault.root).get_task(previous.identifier)
    assert released.active_thread_id is None
    assert released.codex_episode_ids == ("codex-episode-1",)
    assert claimed.codex_episode_ids == ("codex-episode-0", "codex-episode-1")
    assert claimed.project == "Seld"
    assert claimed.workspace == "/tmp/seld-worktree"
    assert claimed.entity_links == (TaskEntityLink("owner", "person:owner"),)
    assert any("Transferred after the prior task" in entry for entry in claimed.history)

    superseded = vault.update_task(
        claimed.identifier,
        expected_revision=claimed.revision,
        status="superseded",
        superseded_by=replacement.identifier,
        observed_at=NOW + timedelta(minutes=2),
    )
    assert superseded.active_thread_id is None
    assert superseded.superseded_by == replacement.identifier
    assert superseded.state_changed_at == superseded.updated_at

    reopened = Vault(vault.root).update_task(
        superseded.identifier,
        expected_revision=superseded.revision,
        status="ready",
        clear_superseded_by=True,
        observed_at=NOW + timedelta(minutes=3),
    )
    assert reopened.superseded_by is None
    assert reopened.project == "Seld"
    assert reopened.codex_episode_ids == ("codex-episode-0", "codex-episode-1")
    with pytest.raises(ConflictError, match="changed since it was read"):
        vault.update_task(reopened.identifier, expected_revision=superseded.revision, title="Stale")


def test_entity_temporal_relationship_merge_recovery_and_redirect(vault: Vault) -> None:
    _entity(vault, "person:duplicate")
    _entity(vault, "person:canonical")
    _entity(vault, "company:studio", "company")
    studio = vault.get_entity("company:studio")
    studio = vault.update_entity(
        studio.identifier,
        expected_revision=studio.revision,
        status="uncertain",
        recheck_at="2026-07-30T10:00:00.000000Z",
        note="Identity evidence needs a deliberate recheck",
        observed_at=NOW + timedelta(seconds=30),
    )
    assert studio.status == "uncertain"
    assert studio.recheck_at == "2026-07-30T10:00:00.000000Z"
    assert any("Identity evidence needs" in entry for entry in studio.history)
    duplicate = vault.get_entity("person:duplicate")

    linked = vault.link_entity(
        duplicate.identifier,
        expected_revision=duplicate.revision,
        predicate="works-at",
        target_id="company:studio",
        refs=("source:test",),
        valid_from="2026-07-01T00:00:00.000000Z",
        observed_at=NOW + timedelta(minutes=1),
    )
    assert linked.relationships[0].status == "current"
    unlinked = vault.unlink_entity(
        linked.identifier,
        expected_revision=linked.revision,
        predicate="works-at",
        target_id="company:studio",
        valid_to="2026-07-29T10:02:00.000000Z",
        observed_at=NOW + timedelta(minutes=2),
    )
    assert unlinked.relationships[0].status == "historical"
    relinked = vault.link_entity(
        unlinked.identifier,
        expected_revision=unlinked.revision,
        predicate="works-at",
        target_id="company:studio",
        refs=("source:new",),
        observed_at=NOW + timedelta(minutes=3),
    )
    canonical = vault.get_entity("person:canonical")
    merged = vault.merge_entity(
        relinked.identifier,
        merged_into=canonical.identifier,
        expected_revision=relinked.revision,
        expected_target_revision=canonical.revision,
        note="Identity was explicitly reconciled",
        observed_at=NOW + timedelta(minutes=4),
    )

    assert merged.changed is True
    assert merged.source.status == "merged"
    assert merged.source.merged_into == canonical.identifier
    assert Vault(vault.root).resolve_entity(relinked.identifier).identifier == canonical.identifier
    assert any(item.source_id == relinked.identifier for item in merged.target.merge_absorptions)
    assert any(
        relationship.status == "current" and relationship.target == "company:studio"
        for relationship in merged.target.relationships
    )
    replay = vault.merge_entity(
        relinked.identifier,
        merged_into=canonical.identifier,
        expected_revision=merged.source.revision,
        expected_target_revision=merged.target.revision,
    )
    assert replay.changed is False


def test_work_thread_typed_lifecycle_merge_redirect_and_horizon(vault: Vault) -> None:
    for identifier in ("target-task", "source-task", "later-task"):
        _task(vault, identifier)
    _entity(vault, "project:target", "project")
    _entity(vault, "project:source", "project")

    target = vault.create_thread(
        identifier="thread:target",
        title="Target thread",
        purpose="Carry the canonical situation.",
        closure_condition="The durable outcome is accepted.",
        summary="Target state.",
        next_actor="agent",
        next_move="Continue the target.",
        focus_task_id="target-task",
        task_links=(WorkThreadTaskLink(1, "target-task"),),
        entity_links=(WorkThreadEntityLink("primary", "project:target"),),
        observed_at=NOW,
    )
    source = vault.create_thread(
        identifier="thread:source",
        title="Source thread",
        purpose="Retain an explicit duplicate until merged.",
        closure_condition="Its exact facts are absorbed deliberately.",
        summary="Source state.",
        next_actor="agent",
        next_move="Prepare the merge.",
        focus_task_id="source-task",
        task_links=(WorkThreadTaskLink(2, "source-task"),),
        entity_links=(WorkThreadEntityLink("related", "project:source"),),
        refs=("source:thread",),
        observed_at=NOW,
    )

    waiting = vault.update_thread(
        target.identifier,
        expected_revision=target.revision,
        status="waiting",
        next_actor="human",
        waiting_on="A decision.",
        recheck_at="2026-07-30T10:00:00.000000Z",
        add_task_links=(WorkThreadTaskLink(3, "later-task"),),
        note="Waiting state was authored explicitly",
        observed_at=NOW + timedelta(minutes=1),
    )
    assert waiting.closure_condition == "The durable outcome is accepted."
    assert tuple(link.position for link in waiting.task_links) == (1, 3)
    assert waiting.state_changed_at == waiting.updated_at
    with pytest.raises(ValidationError, match="future recheck"):
        vault.update_thread(
            waiting.identifier,
            expected_revision=waiting.revision,
            status="dormant",
            clear_recheck_at=True,
            observed_at=NOW + timedelta(minutes=2),
        )

    active = vault.update_thread(
        waiting.identifier,
        expected_revision=waiting.revision,
        status="active",
        clear_waiting_on=True,
        clear_recheck_at=True,
        next_actor="agent",
        observed_at=NOW + timedelta(minutes=2),
    )
    closed = vault.update_thread(
        active.identifier,
        expected_revision=active.revision,
        status="resolved",
        observed_at=NOW + timedelta(minutes=2, seconds=10),
    )
    assert closed.resolved_at == closed.updated_at
    assert closed.focus_task_id is None
    active = vault.update_thread(
        closed.identifier,
        expected_revision=closed.revision,
        status="active",
        next_actor="agent",
        next_move="Continue from the reopened exact state.",
        observed_at=NOW + timedelta(minutes=2, seconds=20),
    )
    assert active.resolved_at is None
    assert active.state_changed_at == active.updated_at
    merged = vault.merge_thread(
        source.identifier,
        merged_into=active.identifier,
        expected_revision=source.revision,
        expected_target_revision=active.revision,
        absorb_source_entities=True,
        absorb_source_tasks=True,
        absorb_source_refs=True,
        note="Duplicate scope accepted deliberately",
        observed_at=NOW + timedelta(minutes=3),
    )
    assert merged.changed is True
    assert merged.source.status == "superseded"
    assert merged.source.focus_task_id is None
    assert merged.source.resolved_at == merged.source.updated_at
    assert {link.task_id for link in merged.target.task_links} == {
        "target-task",
        "later-task",
        "source-task",
    }
    assert {link.entity_id for link in merged.target.entity_links} == {
        "project:target",
        "project:source",
    }
    assert Vault(vault.root).resolve_thread(source.identifier).identifier == target.identifier
    replay = vault.merge_thread(
        source.identifier,
        merged_into=target.identifier,
        expected_revision=merged.source.revision,
        expected_target_revision=merged.target.revision,
    )
    assert replay.changed is False


def test_legacy_review_migration_adds_a_positioned_link_to_a_rich_thread(
    vault: Vault,
) -> None:
    _task(vault, "review-subject")
    session = vault.create_task(
        identifier="review-session",
        title="Review session",
        outcome="Resume the finite all-open review.",
        status="doing",
        next_actor="agent",
        next_action="Present the exact subject.",
        active_thread_id="review-episode",
        refs=("review-scope:all-open", "review-subject:task:review-subject"),
        observed_at=NOW,
    )
    thread = vault.create_thread(
        identifier=REVIEW_WORK_THREAD_ID,
        title="Life Portfolio review",
        purpose="Carry one finite review.",
        closure_condition="Every open outcome is checked once.",
        summary="The next exact review session has not been focused yet.",
        observed_at=NOW,
    )

    migrated = vault.migrate_legacy_review_session(
        session.identifier,
        expected_session_revision=session.revision,
        expected_review_thread_revision=thread.revision,
        observed_at=NOW + timedelta(minutes=1),
    )

    assert migrated.focus_task_id == session.identifier
    assert migrated.task_links == (WorkThreadTaskLink(1, session.identifier),)
    assert any("focused migrated review session" in entry for entry in migrated.history)
    assert Vault(vault.root).get_thread(REVIEW_WORK_THREAD_ID) == migrated


def test_work_thread_merge_recovers_exact_target_first_commit(
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _task(vault, "merge-recovery-task")
    target = vault.create_thread(
        identifier="thread:merge-recovery-target",
        title="Recovery target",
        purpose="Retain target-first recovery evidence.",
        closure_condition="The source redirect commits.",
        summary="No duplicate has been accepted yet.",
        observed_at=NOW,
    )
    source = vault.create_thread(
        identifier="thread:merge-recovery-source",
        title="Recovery source",
        purpose="Prove replay after a partial commit.",
        closure_condition="Its exact task membership is absorbed.",
        summary="The source redirect is not committed yet.",
        task_links=(WorkThreadTaskLink(1, "merge-recovery-task"),),
        observed_at=NOW,
    )
    original_replace = vault._replace_record

    def fail_source_redirect(*args: object, **kwargs: object) -> None:
        after = args[3]
        if (
            isinstance(after, type(source))
            and after.identifier == source.identifier
            and after.status == "superseded"
        ):
            raise OSError("synthetic source redirect failure")
        original_replace(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(vault, "_replace_record", fail_source_redirect)
    with pytest.raises(MutationCommittedError, match="retry the same merge"):
        vault.merge_thread(
            source.identifier,
            merged_into=target.identifier,
            expected_revision=source.revision,
            expected_target_revision=target.revision,
            absorb_source_tasks=True,
            observed_at=NOW + timedelta(minutes=1),
        )
    committed_target = Vault(vault.root).get_thread(target.identifier)
    assert committed_target.task_ids == ("merge-recovery-task",)

    monkeypatch.setattr(vault, "_replace_record", original_replace)
    recovered = vault.merge_thread(
        source.identifier,
        merged_into=target.identifier,
        expected_revision=source.revision,
        expected_target_revision=target.revision,
        absorb_source_tasks=True,
        observed_at=NOW + timedelta(minutes=2),
    )
    assert recovered.source.superseded_by == target.identifier
    assert recovered.target.revision == committed_target.revision
    assert sum("accepted superseded duplicate" in item for item in recovered.target.history) == 1


def test_rich_cli_mutation_is_visible_from_a_fresh_process(vault: Vault) -> None:
    _entity(vault, "person:cli-owner")
    created = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault.root),
            "task",
            "create",
            "--id",
            "cli-resident",
            "--title",
            "CLI resident",
            "--outcome",
            "Persist the complete authored shape.",
            "--project",
            "Seld",
            "--entity-link-json",
            '{"role":"owner","entity_id":"person:cli-owner"}',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    created_payload = json.loads(created.stdout)["result"]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault.root),
            "task",
            "update",
            "cli-resident",
            "--expected-revision",
            created_payload["revision"],
            "--workspace",
            "/tmp/cli-resident",
            "--note",
            "Authored through the public CLI",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    shown = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault.root),
            "task",
            "show",
            "cli-resident",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(shown.stdout)["result"]
    assert payload["project"] == "Seld"
    assert payload["workspace"] == "/tmp/cli-resident"
    assert payload["entity_links"] == [{"entity_id": "person:cli-owner", "role": "owner"}]
    assert any("Authored through the public CLI" in item for item in payload["history"])
