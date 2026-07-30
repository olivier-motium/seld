"""Typed, human-readable Markdown records with stable machine metadata."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal, TypeVar
from urllib.parse import quote, unquote_to_bytes

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.errors import ValidationError

FORMAT_VERSION: Final = 1
TASK_FORMAT_VERSION: Final = 2
TASK_RESIDENT_FORMAT_VERSION: Final = 3
ENTITY_RESIDENT_FORMAT_VERSION: Final = 2
THREAD_RESIDENT_FORMAT_VERSION: Final = 2
TASK_FORMAT_VERSIONS: Final = frozenset(
    {FORMAT_VERSION, TASK_FORMAT_VERSION, TASK_RESIDENT_FORMAT_VERSION}
)
ENTITY_FORMAT_VERSIONS: Final = frozenset({FORMAT_VERSION, ENTITY_RESIDENT_FORMAT_VERSION})
THREAD_FORMAT_VERSIONS: Final = frozenset({FORMAT_VERSION, THREAD_RESIDENT_FORMAT_VERSION})
MAX_RECORD_BYTES: Final = 256 * 1024
MAX_TEXT_BYTES: Final = 64 * 1024
MAX_TITLE_LENGTH: Final = 180
MAX_REFERENCES: Final = 2_000
MAX_REFERENCE_LENGTH: Final = 1_000
MAX_RELATIONS: Final = 100
MAX_TASK_ENTITY_LINKS: Final = 50
MAX_THREAD_ENTITY_LINKS: Final = 200
MAX_THREAD_TASK_LINKS: Final = 512
MAX_CODEX_EPISODES: Final = 50
MAX_HISTORY_ENTRIES: Final = 2_000
MAX_HISTORY_LINE_LENGTH: Final = 2_000
MAX_TASK_RANK: Final = 2_147_483_647
SAFE_ID = re.compile(r"^[a-z][a-z0-9]*(?::[a-z0-9][a-z0-9-]{0,95})$")
SAFE_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
SAFE_HAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
CALENDAR_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
META = re.compile(r"^<!-- gsv:(\{.*\}) -->$")

TaskStatus = Literal[
    "captured", "ready", "doing", "waiting", "someday", "done", "dropped", "superseded"
]
Actor = Literal["agent", "human", "external"]
ThreadStatus = Literal[
    "active", "waiting", "dormant", "closed", "resolved", "dropped", "superseded"
]
EntityStatus = Literal["current", "historical", "uncertain", "merged", "superseded"]
RelationshipStatus = Literal["current", "historical"]

TASK_STATUSES: Final = frozenset(
    {"captured", "ready", "doing", "waiting", "someday", "done", "dropped", "superseded"}
)
TERMINAL_TASK_STATUSES: Final = frozenset({"done", "dropped", "superseded"})
ACTORS: Final = frozenset({"agent", "human", "external"})
THREAD_STATUSES: Final = frozenset(
    {"active", "waiting", "dormant", "closed", "resolved", "dropped", "superseded"}
)
TERMINAL_THREAD_STATUSES: Final = frozenset({"closed", "resolved", "dropped", "superseded"})
ENTITY_STATUSES: Final = frozenset({"current", "historical", "uncertain", "merged", "superseded"})
RELATIONSHIP_STATUSES: Final = frozenset({"current", "historical"})
REVIEW_WORK_THREAD_ID: Final = "thread:life-portfolio-review"
REVIEW_SCOPE_REF: Final = "review-scope:all-open"
REVIEW_PAUSED_REF: Final = "review-state:paused"
RESIDENT_PULSE_TASK_ID: Final = "resident-pulse"
RESIDENT_PULSE_REF: Final = "system-role:resident-pulse"
SHA256_REVISION = re.compile(r"^[0-9a-f]{64}$")
REVIEW_SUBJECT = re.compile(r"^review-subject:task:([a-z0-9][a-z0-9-]{0,95})$")
REVIEW_COVERED_LEGACY = re.compile(r"^review-covered:task:([a-z0-9][a-z0-9-]{0,95})$")
REVIEW_COVERED_ANCHORED = re.compile(
    r"^review-covered:task:([a-z0-9][a-z0-9-]{0,95})@([0-9a-f]{64})"
    r"(?:\|(thread:[a-z0-9][a-z0-9-]{0,95})@([0-9a-f]{64}))?$"
)
REVIEW_OPTION_INTENTS: Final = frozenset(
    {"keep", "act-next", "defer", "reprioritize", "reshape", "drop-or-merge", "skip"}
)
MAX_REVIEW_OPTION_LENGTH: Final = 200
MAX_REVIEW_OPTIONS: Final = 5
MAX_REVIEW_SUBJECTS: Final = 25
WINDOWS_RESERVED_NAMES: Final = frozenset(
    {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)

_TASK_LEGACY_KEYS: Final = frozenset(
    {
        "active_thread_id",
        "created_at",
        "id",
        "kind",
        "next_actor",
        "next_action_present",
        "rank",
        "refs",
        "status",
        "updated_at",
        "version",
        "waiting_on_present",
    }
)
_TASK_RESIDENT_KEYS: Final = _TASK_LEGACY_KEYS | {
    "attention_at",
    "codex_episode_ids",
    "due",
    "entity_links",
    "project",
    "state_changed_at",
    "superseded_by",
    "workspace",
}
_ENTITY_LEGACY_KEYS: Final = frozenset(
    {"aliases", "created_at", "entity_type", "id", "kind", "refs", "updated_at", "version"}
)
_ENTITY_RESIDENT_KEYS: Final = _ENTITY_LEGACY_KEYS | {
    "merge_absorptions",
    "merged_at",
    "merged_from_updated_at",
    "merged_into",
    "observed_at",
    "recheck_at",
    "relationships",
    "status",
}
_THREAD_LEGACY_KEYS: Final = frozenset(
    {
        "created_at",
        "entity_ids",
        "focus_task_id",
        "id",
        "kind",
        "next_move_present",
        "refs",
        "status",
        "task_ids",
        "updated_at",
        "version",
    }
)
_THREAD_RESIDENT_KEYS: Final = frozenset(
    {
        "closure_condition_present",
        "created_at",
        "entity_links",
        "focus_task_id",
        "id",
        "kind",
        "next_actor",
        "next_move_present",
        "observed_at",
        "recheck_at",
        "refs",
        "resolved_at",
        "state_changed_at",
        "status",
        "superseded_by",
        "task_links",
        "updated_at",
        "version",
        "waiting_on_present",
    }
)


@dataclass(frozen=True)
class TaskEntityLink:
    """One exact authored connection from a task to canonical context."""

    role: str
    entity_id: str


@dataclass(frozen=True)
class EntityRelationship:
    """One exact authored relationship and its validity history."""

    predicate: str
    target: str
    status: RelationshipStatus
    recorded_at: str
    valid_from: str | None
    valid_to: str | None
    refs: tuple[str, ...]


@dataclass(frozen=True)
class EntityMergeAbsorption:
    """Structured recovery evidence for one explicitly merged identity."""

    source_id: str
    source_updated_at: str
    merged_at: str


@dataclass(frozen=True)
class WorkThreadEntityLink:
    """One authored WorkThread-to-entity link; ``None`` preserves legacy uncertainty."""

    role: str | None
    entity_id: str


@dataclass(frozen=True)
class WorkThreadTaskLink:
    """One exact authored task position inside a WorkThread."""

    position: int
    task_id: str


@dataclass(frozen=True)
class Task:
    identifier: str
    title: str
    status: TaskStatus
    next_actor: Actor | None
    outcome: str
    next_action: str | None
    waiting_on: str | None
    rank: int | None
    active_thread_id: str | None
    refs: tuple[str, ...]
    created_at: str
    updated_at: str
    revision: str
    superseded_by: str | None = None
    project: str | None = None
    entity_links: tuple[TaskEntityLink, ...] = ()
    workspace: str | None = None
    attention_at: str | None = None
    due: str | None = None
    codex_episode_ids: tuple[str, ...] = ()
    state_changed_at: str | None = None
    history: tuple[str, ...] = ()


@dataclass(frozen=True)
class Entity:
    identifier: str
    title: str
    entity_type: str
    aliases: tuple[str, ...]
    summary: str
    refs: tuple[str, ...]
    created_at: str
    updated_at: str
    revision: str
    status: EntityStatus = "current"
    relationships: tuple[EntityRelationship, ...] = ()
    observed_at: str | None = None
    recheck_at: str | None = None
    merged_into: str | None = None
    merged_at: str | None = None
    merged_from_updated_at: str | None = None
    merge_absorptions: tuple[EntityMergeAbsorption, ...] = ()
    history: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkThread:
    identifier: str
    title: str
    status: ThreadStatus
    purpose: str
    summary: str
    next_move: str | None
    focus_task_id: str | None
    task_links: tuple[WorkThreadTaskLink, ...]
    entity_links: tuple[WorkThreadEntityLink, ...]
    refs: tuple[str, ...]
    created_at: str
    updated_at: str
    revision: str
    closure_condition: str | None = None
    next_actor: Actor | None = None
    waiting_on: str | None = None
    superseded_by: str | None = None
    observed_at: str | None = None
    state_changed_at: str | None = None
    recheck_at: str | None = None
    resolved_at: str | None = None
    history: tuple[str, ...] = ()

    @property
    def task_ids(self) -> tuple[str, ...]:
        """Compatibility projection; positioned links remain the only canonical storage."""

        return tuple(link.task_id for link in self.task_links)

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Compatibility projection; typed links remain the only canonical storage."""

        return tuple(link.entity_id for link in self.entity_links)


@dataclass(frozen=True)
class ReviewCoverage:
    """One checked anchor; missing revisions identify readable legacy coverage."""

    task_id: str
    task_revision: str | None
    work_thread_id: str | None
    work_thread_revision: str | None
    reference: str


@dataclass(frozen=True)
class ReviewOption:
    """One agent-authored contextual shortcut; deterministic code stores only."""

    intent: str
    subject_task_id: str
    consequence: str
    reference: str


@dataclass(frozen=True)
class ReviewReferences:
    """Typed navigation facts carried by one bounded review-session Task."""

    has_all_open_scope: bool
    paused: bool
    subject_task_ids: tuple[str, ...]
    coverages: tuple[ReviewCoverage, ...]
    options: tuple[ReviewOption, ...]
    malformed_refs: tuple[str, ...]
    issues: tuple[str, ...]


Record = Task | Entity | WorkThread
T = TypeVar("T", Task, Entity, WorkThread)


def is_resident_pulse_task(task: Task) -> bool:
    """Identify the one structural Codex Pulse hand, never an ordinary outcome."""

    return task.identifier == RESIDENT_PULSE_TASK_ID and RESIDENT_PULSE_REF in task.refs


def new_task(
    *,
    identifier: str,
    title: str,
    outcome: str,
    status: str = "captured",
    next_actor: str | None = None,
    next_action: str | None = None,
    waiting_on: str | None = None,
    rank: int | None = None,
    active_thread_id: str | None = None,
    refs: tuple[str, ...] = (),
    superseded_by: str | None = None,
    project: str | None = None,
    entity_links: tuple[TaskEntityLink, ...] = (),
    workspace: str | None = None,
    attention_at: str | None = None,
    due: str | None = None,
    codex_episode_ids: tuple[str, ...] = (),
    history: tuple[str, ...] = (),
    observed_at: datetime | None = None,
) -> Task:
    now = format_time(observed_at or datetime.now(UTC))
    identifier = task_id(identifier)
    if identifier.rstrip(" .").casefold() in WINDOWS_RESERVED_NAMES:
        raise ValidationError(
            "task ID is reserved by Windows; choose a portable identifier before creating it"
        )
    clean_active = hand_id(active_thread_id)
    clean_episodes = codex_episodes(
        (*codex_episode_ids, *((clean_active,) if clean_active is not None else ()))
    )
    rich = any(
        (
            superseded_by is not None,
            project is not None,
            bool(entity_links),
            workspace is not None,
            attention_at is not None,
            due is not None,
            bool(clean_episodes),
            bool(history),
        )
    )
    task = Task(
        identifier=identifier,
        title=title_text(title),
        status=task_status(status),
        next_actor=actor(next_actor),
        outcome=body_text(outcome, "outcome", required=True),
        next_action=optional_body(next_action, "next action"),
        waiting_on=optional_body(waiting_on, "waiting on"),
        rank=task_rank(rank),
        active_thread_id=clean_active,
        refs=references(refs),
        created_at=now,
        updated_at=now,
        revision="",
        superseded_by=(task_id(superseded_by) if superseded_by is not None else None),
        project=optional_line(project, "project", 120),
        entity_links=task_entity_links(entity_links),
        workspace=optional_line(workspace, "workspace", 2_048),
        attention_at=calendar_date(attention_at, "attention date"),
        due=calendar_date(due, "due date"),
        codex_episode_ids=clean_episodes,
        state_changed_at=now if rich else None,
        history=(
            history_entries(history)
            if history
            else (f"{now} — Created in {status}.",)
            if rich
            else ()
        ),
    )
    _validate_task_state(task)
    return parse_task(render_task(task))


def new_entity(
    *,
    identifier: str,
    title: str,
    entity_type: str,
    summary: str,
    aliases: tuple[str, ...] = (),
    refs: tuple[str, ...] = (),
    status: str = "current",
    relationships: tuple[EntityRelationship, ...] = (),
    recheck_at: str | None = None,
    merged_into: str | None = None,
    merged_at: str | None = None,
    merged_from_updated_at: str | None = None,
    merge_absorptions: tuple[EntityMergeAbsorption, ...] = (),
    history: tuple[str, ...] = (),
    observed_at: datetime | None = None,
) -> Entity:
    now = format_time(observed_at or datetime.now(UTC))
    clean_status = entity_status(status)
    rich = any(
        (
            clean_status != "current",
            bool(relationships),
            recheck_at is not None,
            merged_into is not None,
            merged_at is not None,
            merged_from_updated_at is not None,
            bool(merge_absorptions),
            bool(history),
        )
    )
    entity = Entity(
        identifier=canonical_id(identifier, "entity ID"),
        title=title_text(title),
        entity_type=safe_token(entity_type, "entity type"),
        aliases=lines(aliases, "entity alias", MAX_RELATIONS),
        summary=body_text(summary, "entity summary", required=True),
        refs=references(refs),
        created_at=now,
        updated_at=now,
        revision="",
        status=clean_status,
        relationships=entity_relationships(relationships),
        observed_at=now if rich else None,
        recheck_at=optional_stored_time(recheck_at, "recheck_at"),
        merged_into=(
            canonical_id(merged_into, "merged entity ID") if merged_into is not None else None
        ),
        merged_at=optional_stored_time(merged_at, "merged_at"),
        merged_from_updated_at=optional_stored_time(
            merged_from_updated_at, "merged_from_updated_at"
        ),
        merge_absorptions=entity_merge_absorptions(merge_absorptions),
        history=(
            history_entries(history)
            if history
            else (f"{now} — Created as {clean_status} {entity_type}.",)
            if rich
            else ()
        ),
    )
    return parse_entity(render_entity(entity))


def new_thread(
    *,
    identifier: str,
    title: str,
    purpose: str,
    summary: str,
    status: str = "active",
    next_move: str | None = None,
    focus_task_id: str | None = None,
    task_ids: tuple[str, ...] = (),
    entity_ids: tuple[str, ...] = (),
    task_links: tuple[WorkThreadTaskLink, ...] = (),
    entity_links: tuple[WorkThreadEntityLink, ...] = (),
    refs: tuple[str, ...] = (),
    closure_condition: str | None = None,
    next_actor: str | None = None,
    waiting_on: str | None = None,
    superseded_by: str | None = None,
    recheck_at: str | None = None,
    resolved_at: str | None = None,
    history: tuple[str, ...] = (),
    observed_at: datetime | None = None,
) -> WorkThread:
    now = format_time(observed_at or datetime.now(UTC))
    if task_ids and task_links:
        raise ValidationError("choose positioned task links or legacy task IDs, not both")
    if entity_ids and entity_links:
        raise ValidationError("choose typed entity links or legacy entity IDs, not both")
    clean_task_links = thread_task_links(
        task_links
        if task_links
        else tuple(
            WorkThreadTaskLink(position=index, task_id=identifier)
            for index, identifier in enumerate(task_ids, 1)
        )
    )
    clean_entity_links = thread_entity_links(
        entity_links
        if entity_links
        else tuple(WorkThreadEntityLink(role=None, entity_id=value) for value in entity_ids)
    )
    clean_status = thread_status(status)
    rich = any(
        (
            bool(task_links),
            bool(entity_links),
            closure_condition is not None,
            next_actor is not None,
            waiting_on is not None,
            superseded_by is not None,
            recheck_at is not None,
            resolved_at is not None,
            bool(history),
            clean_status not in {"active", "waiting", "dormant", "closed"},
        )
    )
    thread = WorkThread(
        identifier=canonical_id(identifier, "thread ID", prefix="thread"),
        title=title_text(title),
        status=clean_status,
        purpose=body_text(purpose, "thread purpose", required=True),
        summary=body_text(summary, "thread summary", required=True),
        next_move=optional_body(next_move, "next move"),
        focus_task_id=task_id(focus_task_id) if focus_task_id is not None else None,
        task_links=clean_task_links,
        entity_links=clean_entity_links,
        refs=references(refs),
        created_at=now,
        updated_at=now,
        revision="",
        closure_condition=optional_body(closure_condition, "closure condition"),
        next_actor=actor(next_actor),
        waiting_on=optional_body(waiting_on, "waiting on"),
        superseded_by=(
            canonical_id(superseded_by, "superseding thread ID", prefix="thread")
            if superseded_by is not None
            else None
        ),
        observed_at=now if rich else None,
        state_changed_at=now if rich else None,
        recheck_at=optional_stored_time(recheck_at, "recheck_at"),
        resolved_at=optional_stored_time(resolved_at, "resolved_at"),
        history=(
            history_entries(history)
            if history
            else (f"{now} — Created as {clean_status} WorkThread.",)
            if rich
            else ()
        ),
    )
    _validate_thread_state(thread)
    return parse_thread(render_thread(thread))


def render_task(task: Task) -> str:
    _validate_task_state(task)
    review = validate_review_references(task.refs, terminal=task.status in TERMINAL_TASK_STATUSES)
    rich = _task_is_rich(task)
    stored_version = (
        TASK_RESIDENT_FORMAT_VERSION
        if rich
        else TASK_FORMAT_VERSION
        if len(review.subject_task_ids) > 1
        else FORMAT_VERSION
    )
    metadata: dict[str, Any] = {
        "created_at": stored_time(task.created_at, "created_at"),
        "id": task_id(task.identifier),
        "kind": "task",
        "next_actor": actor(task.next_actor),
        "next_action_present": task.next_action is not None,
        "rank": task_rank(task.rank),
        "active_thread_id": hand_id(task.active_thread_id),
        "refs": list(references(task.refs)),
        "status": task_status(task.status),
        "updated_at": stored_time(task.updated_at, "updated_at"),
        "version": stored_version,
        "waiting_on_present": task.waiting_on is not None,
    }
    sections: tuple[tuple[str, str], ...] = (
        ("Outcome", task.outcome),
        ("Next action", task.next_action or "Not recorded."),
        ("Waiting on", task.waiting_on or "Not recorded."),
    )
    if rich:
        metadata.update(
            {
                "attention_at": calendar_date(task.attention_at, "attention date"),
                "codex_episode_ids": list(codex_episodes(task.codex_episode_ids)),
                "due": calendar_date(task.due, "due date"),
                "entity_links": [asdict(value) for value in task_entity_links(task.entity_links)],
                "project": optional_line(task.project, "project", 120),
                "state_changed_at": stored_time(
                    required_value(task.state_changed_at, "state_changed_at"),
                    "state_changed_at",
                ),
                "superseded_by": (
                    task_id(task.superseded_by) if task.superseded_by is not None else None
                ),
                "workspace": optional_line(task.workspace, "workspace", 2_048),
            }
        )
        sections = (*sections, ("History", render_history(task.history)))
    return _render(
        metadata,
        task.title,
        sections,
    )


def render_entity(entity: Entity) -> str:
    _validate_entity_state(entity)
    rich = _entity_is_rich(entity)
    metadata: dict[str, Any] = {
        "aliases": list(lines(entity.aliases, "entity alias", MAX_RELATIONS)),
        "created_at": stored_time(entity.created_at, "created_at"),
        "entity_type": safe_token(entity.entity_type, "entity type"),
        "id": canonical_id(entity.identifier, "entity ID"),
        "kind": "entity",
        "refs": list(references(entity.refs)),
        "updated_at": stored_time(entity.updated_at, "updated_at"),
        "version": ENTITY_RESIDENT_FORMAT_VERSION if rich else FORMAT_VERSION,
    }
    sections: tuple[tuple[str, str], ...] = (("Summary", entity.summary),)
    if rich:
        metadata.update(
            {
                "merge_absorptions": [
                    asdict(value) for value in entity_merge_absorptions(entity.merge_absorptions)
                ],
                "merged_at": optional_stored_time(entity.merged_at, "merged_at"),
                "merged_from_updated_at": optional_stored_time(
                    entity.merged_from_updated_at, "merged_from_updated_at"
                ),
                "merged_into": (
                    canonical_id(entity.merged_into, "merged entity ID")
                    if entity.merged_into is not None
                    else None
                ),
                "observed_at": stored_time(
                    required_value(entity.observed_at, "observed_at"), "observed_at"
                ),
                "recheck_at": optional_stored_time(entity.recheck_at, "recheck_at"),
                "relationships": [
                    asdict(value) for value in entity_relationships(entity.relationships)
                ],
                "status": entity_status(entity.status),
            }
        )
        sections = (*sections, ("History", render_history(entity.history)))
    return _render(metadata, entity.title, sections)


def render_thread(thread: WorkThread) -> str:
    _validate_thread_state(thread)
    rich = _thread_is_rich(thread)
    metadata: dict[str, Any] = {
        "created_at": stored_time(thread.created_at, "created_at"),
        "id": canonical_id(thread.identifier, "thread ID", prefix="thread"),
        "kind": "thread",
        "focus_task_id": thread.focus_task_id,
        "next_move_present": thread.next_move is not None,
        "refs": list(references(thread.refs)),
        "status": thread_status(thread.status),
        "updated_at": stored_time(thread.updated_at, "updated_at"),
        "version": THREAD_RESIDENT_FORMAT_VERSION if rich else FORMAT_VERSION,
    }
    sections: tuple[tuple[str, str], ...]
    if rich:
        metadata.update(
            {
                "closure_condition_present": thread.closure_condition is not None,
                "entity_links": [
                    asdict(value) for value in thread_entity_links(thread.entity_links)
                ],
                "next_actor": actor(thread.next_actor),
                "observed_at": stored_time(
                    required_value(thread.observed_at, "observed_at"), "observed_at"
                ),
                "recheck_at": optional_stored_time(thread.recheck_at, "recheck_at"),
                "resolved_at": optional_stored_time(thread.resolved_at, "resolved_at"),
                "state_changed_at": stored_time(
                    required_value(thread.state_changed_at, "state_changed_at"),
                    "state_changed_at",
                ),
                "superseded_by": (
                    canonical_id(thread.superseded_by, "superseding thread ID", prefix="thread")
                    if thread.superseded_by is not None
                    else None
                ),
                "task_links": [asdict(value) for value in thread_task_links(thread.task_links)],
                "waiting_on_present": thread.waiting_on is not None,
            }
        )
        sections = (
            ("Purpose", thread.purpose),
            ("Closure condition", thread.closure_condition or "Not recorded."),
            ("Current summary", thread.summary),
            ("Next move", thread.next_move or "Not recorded."),
            ("Waiting on", thread.waiting_on or "Not recorded."),
            ("History", render_history(thread.history)),
        )
    else:
        metadata.update(
            {
                "entity_ids": list(entity_ids_value(thread.entity_ids)),
                "task_ids": list(task_ids_value(thread.task_ids)),
            }
        )
        sections = (
            ("Purpose", thread.purpose),
            ("Current summary", thread.summary),
            ("Next move", thread.next_move or "Not recorded."),
        )
    return _render(
        metadata,
        thread.title,
        sections,
    )


def parse_task(markdown: str) -> Task:
    meta, title, sections, revision = _parse(
        markdown,
        "task",
        {
            FORMAT_VERSION: ("Outcome", "Next action", "Waiting on"),
            TASK_FORMAT_VERSION: ("Outcome", "Next action", "Waiting on"),
            TASK_RESIDENT_FORMAT_VERSION: ("Outcome", "Next action", "Waiting on", "History"),
        },
    )
    stored_version = meta["version"]
    _expect_metadata_keys(
        meta,
        (
            _TASK_RESIDENT_KEYS
            if stored_version == TASK_RESIDENT_FORMAT_VERSION
            else _TASK_LEGACY_KEYS
        ),
        "task",
    )
    rich = stored_version == TASK_RESIDENT_FORMAT_VERSION
    task = Task(
        identifier=task_id(_string(meta, "id")),
        title=title_text(title),
        status=task_status(_string(meta, "status")),
        next_actor=actor(_optional_string(meta, "next_actor")),
        outcome=body_text(sections["Outcome"], "outcome", required=True),
        next_action=_optional_section(meta, "next_action_present", sections["Next action"]),
        waiting_on=_optional_section(meta, "waiting_on_present", sections["Waiting on"]),
        rank=task_rank(_optional_integer(meta, "rank")),
        active_thread_id=hand_id(_optional_string(meta, "active_thread_id")),
        refs=references(_string_tuple(meta, "refs")),
        created_at=stored_time(_string(meta, "created_at"), "created_at"),
        updated_at=stored_time(_string(meta, "updated_at"), "updated_at"),
        revision=revision,
        superseded_by=(
            task_id(value)
            if rich and (value := _optional_string(meta, "superseded_by")) is not None
            else None
        ),
        project=optional_line(_optional_string(meta, "project"), "project", 120) if rich else None,
        entity_links=(task_entity_links_from_json(meta.get("entity_links")) if rich else ()),
        workspace=(
            optional_line(_optional_string(meta, "workspace"), "workspace", 2_048) if rich else None
        ),
        attention_at=(
            calendar_date(_optional_string(meta, "attention_at"), "attention date")
            if rich
            else None
        ),
        due=(calendar_date(_optional_string(meta, "due"), "due date") if rich else None),
        codex_episode_ids=(
            codex_episodes(_string_tuple(meta, "codex_episode_ids")) if rich else ()
        ),
        state_changed_at=(
            stored_time(_string(meta, "state_changed_at"), "state_changed_at") if rich else None
        ),
        history=(parse_history(sections["History"]) if rich else ()),
    )
    _validate_task_state(task)
    review = validate_review_references(task.refs, terminal=task.status in TERMINAL_TASK_STATUSES)
    if stored_version == FORMAT_VERSION and len(review.subject_task_ids) > 1:
        raise ValidationError("task record version 1 supports at most one current review subject")
    if stored_version == TASK_FORMAT_VERSION and len(review.subject_task_ids) <= 1:
        raise ValidationError("task record version 2 requires multiple current review subjects")
    if stored_version == TASK_RESIDENT_FORMAT_VERSION and not _task_is_rich(task):
        raise ValidationError("task record version 3 requires resident continuity fields")
    return task


def parse_entity(markdown: str) -> Entity:
    meta, title, sections, revision = _parse(
        markdown,
        "entity",
        {
            FORMAT_VERSION: ("Summary",),
            ENTITY_RESIDENT_FORMAT_VERSION: ("Summary", "History"),
        },
    )
    rich = meta["version"] == ENTITY_RESIDENT_FORMAT_VERSION
    _expect_metadata_keys(meta, _ENTITY_RESIDENT_KEYS if rich else _ENTITY_LEGACY_KEYS, "entity")
    entity = Entity(
        identifier=canonical_id(_string(meta, "id"), "entity ID"),
        title=title_text(title),
        entity_type=safe_token(_string(meta, "entity_type"), "entity type"),
        aliases=lines(_string_tuple(meta, "aliases"), "entity alias", MAX_RELATIONS),
        summary=body_text(sections["Summary"], "entity summary", required=True),
        refs=references(_string_tuple(meta, "refs")),
        created_at=stored_time(_string(meta, "created_at"), "created_at"),
        updated_at=stored_time(_string(meta, "updated_at"), "updated_at"),
        revision=revision,
        status=entity_status(_string(meta, "status")) if rich else "current",
        relationships=(entity_relationships_from_json(meta.get("relationships")) if rich else ()),
        observed_at=(stored_time(_string(meta, "observed_at"), "observed_at") if rich else None),
        recheck_at=(
            optional_stored_time(_optional_string(meta, "recheck_at"), "recheck_at")
            if rich
            else None
        ),
        merged_into=(
            canonical_id(value, "merged entity ID")
            if rich and (value := _optional_string(meta, "merged_into")) is not None
            else None
        ),
        merged_at=(
            optional_stored_time(_optional_string(meta, "merged_at"), "merged_at") if rich else None
        ),
        merged_from_updated_at=(
            optional_stored_time(
                _optional_string(meta, "merged_from_updated_at"), "merged_from_updated_at"
            )
            if rich
            else None
        ),
        merge_absorptions=(
            entity_merge_absorptions_from_json(meta.get("merge_absorptions")) if rich else ()
        ),
        history=(parse_history(sections["History"]) if rich else ()),
    )
    _validate_entity_state(entity)
    if rich and not _entity_is_rich(entity):
        raise ValidationError("entity record version 2 requires resident continuity fields")
    return entity


def parse_thread(markdown: str) -> WorkThread:
    meta, title, sections, revision = _parse(
        markdown,
        "thread",
        {
            FORMAT_VERSION: ("Purpose", "Current summary", "Next move"),
            THREAD_RESIDENT_FORMAT_VERSION: (
                "Purpose",
                "Closure condition",
                "Current summary",
                "Next move",
                "Waiting on",
                "History",
            ),
        },
    )
    rich = meta["version"] == THREAD_RESIDENT_FORMAT_VERSION
    expected_keys = _THREAD_RESIDENT_KEYS if rich else _THREAD_LEGACY_KEYS
    if not rich and "focus_task_id" not in meta:
        # Version 1 predates focus metadata. Accept that one frozen historical
        # shape without making any other legacy metadata field optional.
        expected_keys = _THREAD_LEGACY_KEYS - {"focus_task_id"}
    _expect_metadata_keys(meta, expected_keys, "thread")
    thread = WorkThread(
        identifier=canonical_id(_string(meta, "id"), "thread ID", prefix="thread"),
        title=title_text(title),
        status=thread_status(_string(meta, "status")),
        purpose=body_text(sections["Purpose"], "thread purpose", required=True),
        summary=body_text(sections["Current summary"], "thread summary", required=True),
        next_move=_optional_section(meta, "next_move_present", sections["Next move"]),
        focus_task_id=(
            task_id(value)
            if (value := _optional_string(meta, "focus_task_id")) is not None
            else None
        ),
        task_links=(
            thread_task_links_from_json(meta.get("task_links"))
            if rich
            else thread_task_links(
                tuple(
                    WorkThreadTaskLink(index, identifier)
                    for index, identifier in enumerate(
                        task_ids_value(_string_tuple(meta, "task_ids")), 1
                    )
                )
            )
        ),
        entity_links=(
            thread_entity_links_from_json(meta.get("entity_links"))
            if rich
            else thread_entity_links(
                tuple(
                    WorkThreadEntityLink(None, identifier)
                    for identifier in entity_ids_value(_string_tuple(meta, "entity_ids"))
                )
            )
        ),
        refs=references(_string_tuple(meta, "refs")),
        created_at=stored_time(_string(meta, "created_at"), "created_at"),
        updated_at=stored_time(_string(meta, "updated_at"), "updated_at"),
        revision=revision,
        closure_condition=(
            _optional_section(
                {"closure_condition_present": meta.get("closure_condition_present")},
                "closure_condition_present",
                sections["Closure condition"],
            )
            if rich
            else None
        ),
        next_actor=actor(_optional_string(meta, "next_actor")) if rich else None,
        waiting_on=(
            _optional_section(meta, "waiting_on_present", sections["Waiting on"]) if rich else None
        ),
        superseded_by=(
            canonical_id(value, "superseding thread ID", prefix="thread")
            if rich and (value := _optional_string(meta, "superseded_by")) is not None
            else None
        ),
        observed_at=(stored_time(_string(meta, "observed_at"), "observed_at") if rich else None),
        state_changed_at=(
            stored_time(_string(meta, "state_changed_at"), "state_changed_at") if rich else None
        ),
        recheck_at=(
            optional_stored_time(_optional_string(meta, "recheck_at"), "recheck_at")
            if rich
            else None
        ),
        resolved_at=(
            optional_stored_time(_optional_string(meta, "resolved_at"), "resolved_at")
            if rich
            else None
        ),
        history=(parse_history(sections["History"]) if rich else ()),
    )
    _validate_thread_state(thread)
    if rich and not _thread_is_rich(thread):
        raise ValidationError("thread record version 2 requires resident continuity fields")
    return thread


def record_dict(record: Record) -> dict[str, Any]:
    payload = asdict(record)
    for key, value in tuple(payload.items()):
        if isinstance(value, tuple):
            payload[key] = list(value)
    if isinstance(record, WorkThread):
        payload["task_ids"] = list(record.task_ids)
        payload["entity_ids"] = list(record.entity_ids)
    return payload


def next_timestamp(previous: str, observed_at: datetime | None = None) -> str:
    candidate = observed_at or datetime.now(UTC)
    before = parse_time(previous)
    if candidate <= before:
        candidate = before + timedelta(microseconds=1)
    return format_time(candidate)


def canonical_id(value: str, label: str, *, prefix: str | None = None) -> str:
    clean = str(value).strip().lower()
    if prefix and ":" not in clean:
        clean = f"{prefix}:{clean}"
    if not SAFE_ID.fullmatch(clean):
        raise ValidationError(f"{label} must look like type:stable-id")
    if prefix and not clean.startswith(f"{prefix}:"):
        raise ValidationError(f"{label} must start with {prefix}:")
    return clean


def task_id(value: str) -> str:
    clean = str(value).strip().lower()
    if not SAFE_TASK_ID.fullmatch(clean):
        raise ValidationError("task ID must contain only lowercase letters, digits, and hyphens")
    return clean


def title_text(value: str) -> str:
    clean = str(value).strip()
    if not clean or len(clean) > MAX_TITLE_LENGTH or "\n" in clean or "\r" in clean:
        raise ValidationError(
            f"title must be one non-empty line up to {MAX_TITLE_LENGTH} characters"
        )
    if "\x00" in clean:
        raise ValidationError("title contains a null byte")
    return clean


def body_text(value: str, label: str, *, required: bool) -> str:
    clean = str(value).strip()
    if required and not clean:
        raise ValidationError(f"{label} is required")
    if "\x00" in clean:
        raise ValidationError(f"{label} contains a null byte")
    if len(clean.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValidationError(f"{label} is too large")
    if any(line.startswith("## ") for line in clean.splitlines()):
        raise ValidationError(f"{label} cannot contain level-two Markdown headings")
    return clean


def optional_body(value: str | None, label: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    return body_text(value, label, required=True)


def task_status(value: str) -> TaskStatus:
    if value not in TASK_STATUSES:
        raise ValidationError(f"invalid task status: {value}")
    return value  # type: ignore[return-value]


def thread_status(value: str) -> ThreadStatus:
    if value not in THREAD_STATUSES:
        raise ValidationError(f"invalid thread status: {value}")
    return value  # type: ignore[return-value]


def actor(value: str | None) -> Actor | None:
    if value is None:
        return None
    if value not in ACTORS:
        raise ValidationError(f"invalid next actor: {value}")
    return value  # type: ignore[return-value]


def task_rank(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TASK_RANK:
        raise ValidationError(f"task rank must be an integer from 0 to {MAX_TASK_RANK}")
    return value


def hand_id(value: str | None) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not SAFE_HAND_ID.fullmatch(clean):
        raise ValidationError("active thread ID must be one bounded opaque identifier")
    return clean


def review_coverage_ref(
    *,
    task_id_value: str,
    task_revision: str,
    work_thread_id: str | None = None,
    work_thread_revision: str | None = None,
) -> str:
    """Render the one canonical revision-aware checked-coverage reference."""

    clean_task = task_id(task_id_value)
    clean_task_revision = _review_revision(task_revision, "review task revision")
    if (work_thread_id is None) != (work_thread_revision is None):
        raise ValidationError(
            "review coverage WorkThread ID and revision must be authored together"
        )
    suffix = ""
    if work_thread_id is not None:
        clean_thread = canonical_id(work_thread_id, "review WorkThread ID", prefix="thread")
        clean_thread_revision = _review_revision(work_thread_revision, "review WorkThread revision")
        suffix = f"|{clean_thread}@{clean_thread_revision}"
    return f"review-covered:task:{clean_task}@{clean_task_revision}{suffix}"


def review_option_ref(*, intent: str, subject_task_id: str, consequence: str) -> str:
    """Render one canonical agent-authored option without choosing it."""

    clean_intent = _review_option_intent(intent)
    clean_subject = task_id(subject_task_id)
    clean_consequence = _review_option_consequence(consequence)
    reference = (
        f"review-option:{clean_intent}:task:{clean_subject}:{quote(clean_consequence, safe='')}"
    )
    if len(reference) > MAX_REFERENCE_LENGTH:
        raise ValidationError("encoded review option reference exceeds the reference limit")
    references((reference,))
    return reference


def parse_review_references(values: tuple[str, ...] | list[str]) -> ReviewReferences:
    """Parse review refs without letting malformed or legacy facts hide work."""

    clean = references(values)
    scope = False
    paused = False
    subjects: list[str] = []
    coverages: list[ReviewCoverage] = []
    options: list[ReviewOption] = []
    malformed: list[str] = []
    for value in clean:
        if value == REVIEW_SCOPE_REF:
            scope = True
            continue
        if value.startswith("review-scope:"):
            malformed.append(value)
            continue
        if value == REVIEW_PAUSED_REF:
            paused = True
            continue
        if value.startswith("review-state:"):
            malformed.append(value)
            continue
        if value.startswith("review-subject:"):
            matched = REVIEW_SUBJECT.fullmatch(value)
            if matched is None:
                malformed.append(value)
            else:
                subjects.append(matched.group(1))
            continue
        if value.startswith("review-covered:"):
            anchored = REVIEW_COVERED_ANCHORED.fullmatch(value)
            if anchored is not None:
                coverages.append(
                    ReviewCoverage(
                        task_id=anchored.group(1),
                        task_revision=anchored.group(2),
                        work_thread_id=anchored.group(3),
                        work_thread_revision=anchored.group(4),
                        reference=value,
                    )
                )
                continue
            legacy = REVIEW_COVERED_LEGACY.fullmatch(value)
            if legacy is not None:
                coverages.append(
                    ReviewCoverage(
                        task_id=legacy.group(1),
                        task_revision=None,
                        work_thread_id=None,
                        work_thread_revision=None,
                        reference=value,
                    )
                )
            else:
                malformed.append(value)
            continue
        if value.startswith("review-option:"):
            option = _parse_review_option(value)
            if option is None:
                malformed.append(value)
            else:
                options.append(option)
            continue
        if value.startswith("review-"):
            malformed.append(value)
    issues: list[str] = []
    if len(set(subjects)) != len(subjects):
        issues.append("review session has duplicate current subjects")
    if len(subjects) > MAX_REVIEW_SUBJECTS:
        issues.append(f"review session has more than {MAX_REVIEW_SUBJECTS} current subjects")
    if options and len(subjects) != 1:
        issues.append("legacy review options require exactly one current subject")
    elif options and any(option.subject_task_id != subjects[0] for option in options):
        issues.append("review option subject does not match the current review subject")
    coverage_ids = [value.task_id for value in coverages]
    if len(set(coverage_ids)) != len(coverage_ids):
        issues.append("review session has conflicting coverage anchors for one task")
    option_intents = [value.intent for value in options]
    if len(set(option_intents)) != len(option_intents):
        issues.append("review session has duplicate option intents")
    option_consequences = [value.consequence.casefold() for value in options]
    if len(set(option_consequences)) != len(option_consequences):
        issues.append("review session has duplicate option answers")
    if len(options) > MAX_REVIEW_OPTIONS:
        issues.append(f"review session has more than {MAX_REVIEW_OPTIONS} current options")
    if (paused or subjects or coverages or options) and not scope:
        issues.append("review navigation refs require review-scope:all-open")
    if malformed:
        issues.append("review session contains malformed review refs")
    return ReviewReferences(
        has_all_open_scope=scope,
        paused=paused,
        subject_task_ids=tuple(subjects),
        coverages=tuple(coverages),
        options=tuple(options),
        malformed_refs=tuple(malformed),
        issues=tuple(issues),
    )


def validate_review_references(
    values: tuple[str, ...] | list[str], *, terminal: bool = False
) -> ReviewReferences:
    parsed = parse_review_references(values)
    if parsed.malformed_refs:
        raise ValidationError(
            "review session contains a malformed review ref; review-option consequences must "
            "percent-encode every character outside A-Z, a-z, 0-9, underscore, period, hyphen, "
            "and tilde using uppercase hexadecimal"
        )
    if parsed.issues:
        raise ValidationError(parsed.issues[0])
    if terminal and parsed.options:
        raise ValidationError("terminal review sessions cannot retain current options")
    if terminal and parsed.subject_task_ids:
        raise ValidationError("terminal review sessions cannot retain a current subject")
    if terminal and parsed.paused:
        raise ValidationError("terminal review sessions cannot remain paused")
    return parsed


def has_review_session_signal(task: Task) -> bool:
    """Identify structural session state without interpreting task prose."""

    parsed = parse_review_references(task.refs)
    return bool(
        task.active_thread_id is not None
        or parsed.paused
        or parsed.subject_task_ids
        or parsed.coverages
        or parsed.options
        or parsed.malformed_refs
    )


def safe_token(value: str, label: str) -> str:
    clean = str(value).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", clean):
        raise ValidationError(f"{label} must be a lowercase token")
    return clean


def references(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    raw = tuple(values)
    clean = lines(raw, "reference", MAX_REFERENCES, max_length=MAX_REFERENCE_LENGTH)
    seen_review_refs: set[str] = set()
    for value in raw:
        item = str(value).strip()
        if not item.startswith("review-"):
            continue
        if item in seen_review_refs:
            raise ValidationError("duplicate review reference")
        seen_review_refs.add(item)
    return clean


def lines(
    values: tuple[str, ...] | list[str],
    label: str,
    maximum: int,
    *,
    max_length: int = 200,
) -> tuple[str, ...]:
    if len(values) > maximum:
        raise ValidationError(f"too many {label} values")
    clean: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or len(item) > max_length or "\n" in item or "\r" in item:
            raise ValidationError(f"each {label} must be one bounded non-empty line")
        if item not in clean:
            clean.append(item)
    return tuple(clean)


def task_ids_value(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(values) > MAX_THREAD_TASK_LINKS:
        raise ValidationError("too many task relationships")
    return tuple(dict.fromkeys(task_id(item) for item in values))


def entity_ids_value(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(values) > MAX_THREAD_ENTITY_LINKS:
        raise ValidationError("too many entity relationships")
    return tuple(dict.fromkeys(canonical_id(item, "entity ID") for item in values))


def task_entity_links(
    values: tuple[TaskEntityLink, ...] | list[TaskEntityLink],
) -> tuple[TaskEntityLink, ...]:
    if len(values) > MAX_TASK_ENTITY_LINKS:
        raise ValidationError("task contains too many canonical entity links")
    clean: list[TaskEntityLink] = []
    for value in values:
        if not isinstance(value, TaskEntityLink):
            raise ValidationError("task entity link has an invalid shape")
        link = TaskEntityLink(
            role=safe_token(value.role, "task entity role"),
            entity_id=canonical_id(value.entity_id, "task entity ID"),
        )
        if link not in clean:
            clean.append(link)
    return tuple(clean)


def task_entity_links_from_json(value: object) -> tuple[TaskEntityLink, ...]:
    if not isinstance(value, list):
        raise ValidationError("task entity_links must be a list")
    parsed: list[TaskEntityLink] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"entity_id", "role"}:
            raise ValidationError("task entity link has unsupported fields")
        role = item.get("role")
        entity_id = item.get("entity_id")
        if not isinstance(role, str) or not isinstance(entity_id, str):
            raise ValidationError("task entity link fields must be strings")
        parsed.append(TaskEntityLink(role, entity_id))
    return task_entity_links(parsed)


def entity_relationships(
    values: tuple[EntityRelationship, ...] | list[EntityRelationship],
) -> tuple[EntityRelationship, ...]:
    if len(values) > MAX_THREAD_ENTITY_LINKS:
        raise ValidationError("entity contains too many relationships")
    clean: list[EntityRelationship] = []
    current: set[tuple[str, str]] = set()
    historical: set[tuple[object, ...]] = set()
    for value in values:
        if not isinstance(value, EntityRelationship):
            raise ValidationError("entity relationship has an invalid shape")
        status = relationship_status(value.status)
        relationship = EntityRelationship(
            predicate=safe_token(value.predicate, "relationship predicate"),
            target=canonical_id(value.target, "relationship target"),
            status=status,
            recorded_at=stored_time(value.recorded_at, "relationship recorded_at"),
            valid_from=optional_stored_time(value.valid_from, "relationship valid_from"),
            valid_to=optional_stored_time(value.valid_to, "relationship valid_to"),
            refs=references(value.refs),
        )
        if status == "current" and relationship.valid_to is not None:
            raise ValidationError("current relationship cannot have valid_to")
        if status == "historical" and relationship.valid_to is None:
            raise ValidationError("historical relationship requires valid_to")
        if (
            relationship.valid_from is not None
            and relationship.valid_to is not None
            and parse_time(relationship.valid_to) < parse_time(relationship.valid_from)
        ):
            raise ValidationError("relationship valid_to predates valid_from")
        key = (relationship.predicate, relationship.target)
        row = (
            *key,
            relationship.status,
            relationship.recorded_at,
            relationship.valid_from,
            relationship.valid_to,
            relationship.refs,
        )
        if status == "current":
            if key in current:
                raise ValidationError("entity contains a duplicate current relationship")
            current.add(key)
        elif row in historical:
            raise ValidationError("entity contains a duplicate historical relationship")
        else:
            historical.add(row)
        clean.append(relationship)
    return tuple(clean)


def entity_relationships_from_json(value: object) -> tuple[EntityRelationship, ...]:
    if not isinstance(value, list):
        raise ValidationError("entity relationships must be a list")
    parsed: list[EntityRelationship] = []
    expected = {"predicate", "recorded_at", "refs", "status", "target", "valid_from", "valid_to"}
    for item in value:
        if not isinstance(item, dict) or set(item) != expected:
            raise ValidationError("entity relationship has unsupported fields")
        if any(
            not isinstance(item.get(key), str)
            for key in ("predicate", "recorded_at", "status", "target")
        ):
            raise ValidationError("entity relationship scalar fields must be strings")
        refs_value = item.get("refs")
        if not isinstance(refs_value, list) or any(
            not isinstance(entry, str) for entry in refs_value
        ):
            raise ValidationError("entity relationship refs must be strings")
        for key in ("valid_from", "valid_to"):
            if item.get(key) is not None and not isinstance(item.get(key), str):
                raise ValidationError(f"entity relationship {key} must be a string or null")
        parsed.append(
            EntityRelationship(
                predicate=item["predicate"],
                target=item["target"],
                status=item["status"],
                recorded_at=item["recorded_at"],
                valid_from=item["valid_from"],
                valid_to=item["valid_to"],
                refs=tuple(refs_value),
            )
        )
    return entity_relationships(parsed)


def entity_merge_absorptions(
    values: tuple[EntityMergeAbsorption, ...] | list[EntityMergeAbsorption],
) -> tuple[EntityMergeAbsorption, ...]:
    if len(values) > MAX_THREAD_ENTITY_LINKS:
        raise ValidationError("entity contains too many merge absorptions")
    clean: list[EntityMergeAbsorption] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, EntityMergeAbsorption):
            raise ValidationError("entity merge absorption has an invalid shape")
        item = EntityMergeAbsorption(
            source_id=canonical_id(value.source_id, "merge source ID"),
            source_updated_at=stored_time(value.source_updated_at, "merge source_updated_at"),
            merged_at=stored_time(value.merged_at, "merge merged_at"),
        )
        if item.source_id in seen:
            raise ValidationError("entity contains a duplicate merge absorption")
        if parse_time(item.source_updated_at) >= parse_time(item.merged_at):
            raise ValidationError("merge absorption must follow its source version")
        seen.add(item.source_id)
        clean.append(item)
    return tuple(clean)


def entity_merge_absorptions_from_json(value: object) -> tuple[EntityMergeAbsorption, ...]:
    if not isinstance(value, list):
        raise ValidationError("entity merge_absorptions must be a list")
    parsed: list[EntityMergeAbsorption] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "merged_at",
            "source_id",
            "source_updated_at",
        }:
            raise ValidationError("entity merge absorption has unsupported fields")
        if any(not isinstance(item.get(key), str) for key in item):
            raise ValidationError("entity merge absorption fields must be strings")
        parsed.append(
            EntityMergeAbsorption(
                source_id=item["source_id"],
                source_updated_at=item["source_updated_at"],
                merged_at=item["merged_at"],
            )
        )
    return entity_merge_absorptions(parsed)


def thread_entity_links(
    values: tuple[WorkThreadEntityLink, ...] | list[WorkThreadEntityLink],
) -> tuple[WorkThreadEntityLink, ...]:
    if len(values) > MAX_THREAD_ENTITY_LINKS:
        raise ValidationError("WorkThread contains too many entity links")
    clean: list[WorkThreadEntityLink] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, WorkThreadEntityLink):
            raise ValidationError("WorkThread entity link has an invalid shape")
        link = WorkThreadEntityLink(
            role=(
                safe_token(value.role, "WorkThread entity role") if value.role is not None else None
            ),
            entity_id=canonical_id(value.entity_id, "WorkThread entity ID"),
        )
        if link.entity_id in seen:
            raise ValidationError("an entity can appear only once in a WorkThread")
        seen.add(link.entity_id)
        clean.append(link)
    return tuple(clean)


def thread_entity_links_from_json(value: object) -> tuple[WorkThreadEntityLink, ...]:
    if not isinstance(value, list):
        raise ValidationError("WorkThread entity_links must be a list")
    parsed: list[WorkThreadEntityLink] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"entity_id", "role"}:
            raise ValidationError("WorkThread entity link has unsupported fields")
        role = item.get("role")
        entity_id = item.get("entity_id")
        if role is not None and not isinstance(role, str):
            raise ValidationError("WorkThread entity link role must be a string or null")
        if not isinstance(entity_id, str):
            raise ValidationError("WorkThread entity link ID must be a string")
        parsed.append(WorkThreadEntityLink(role, entity_id))
    return thread_entity_links(parsed)


def thread_task_links(
    values: tuple[WorkThreadTaskLink, ...] | list[WorkThreadTaskLink],
) -> tuple[WorkThreadTaskLink, ...]:
    if len(values) > MAX_THREAD_TASK_LINKS:
        raise ValidationError("WorkThread contains too many task links")
    clean: list[WorkThreadTaskLink] = []
    positions: set[int] = set()
    identifiers: set[str] = set()
    for value in values:
        if not isinstance(value, WorkThreadTaskLink):
            raise ValidationError("WorkThread task link has an invalid shape")
        if (
            isinstance(value.position, bool)
            or not isinstance(value.position, int)
            or value.position < 1
        ):
            raise ValidationError("WorkThread task position must be a positive integer")
        identifier = task_id(value.task_id)
        if value.position in positions:
            raise ValidationError("WorkThread task positions must be unique")
        if identifier in identifiers:
            raise ValidationError("a task can appear only once in a WorkThread")
        positions.add(value.position)
        identifiers.add(identifier)
        clean.append(WorkThreadTaskLink(value.position, identifier))
    return tuple(sorted(clean, key=lambda item: (item.position, item.task_id)))


def thread_task_links_from_json(value: object) -> tuple[WorkThreadTaskLink, ...]:
    if not isinstance(value, list):
        raise ValidationError("WorkThread task_links must be a list")
    parsed: list[WorkThreadTaskLink] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"position", "task_id"}:
            raise ValidationError("WorkThread task link has unsupported fields")
        position = item.get("position")
        identifier = item.get("task_id")
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValidationError("WorkThread task position must be an integer")
        if not isinstance(identifier, str):
            raise ValidationError("WorkThread task ID must be a string")
        parsed.append(WorkThreadTaskLink(position, identifier))
    return thread_task_links(parsed)


def codex_episodes(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(values) > MAX_CODEX_EPISODES:
        raise ValidationError("task contains too many Codex episode IDs")
    clean: list[str] = []
    for value in values:
        identifier = hand_id(value)
        if identifier is None:
            raise ValidationError("Codex episode ID cannot be empty")
        if identifier not in clean:
            clean.append(identifier)
    return tuple(clean)


def history_entries(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(values) > MAX_HISTORY_ENTRIES:
        raise ValidationError("too many history entry values")
    clean: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValidationError("history entries must be strings")
        item = value.strip()
        if (
            not item
            or len(item) > MAX_HISTORY_LINE_LENGTH
            or "\n" in item
            or "\r" in item
            or "\x00" in item
        ):
            raise ValidationError("each history entry must be one bounded non-empty line")
        if item not in clean:
            clean.append(item)
    return tuple(clean)


def render_history(values: tuple[str, ...] | list[str]) -> str:
    clean = history_entries(values)
    if not clean:
        raise ValidationError("resident record history is required")
    return "\n".join(f"- {value}" for value in clean)


def parse_history(value: str) -> tuple[str, ...]:
    raw: list[str] = []
    for line in value.splitlines():
        if not line.startswith("- ") or not line[2:].strip():
            raise ValidationError("resident record history has an invalid line")
        raw.append(line[2:].strip())
    clean = history_entries(raw)
    if not clean:
        raise ValidationError("resident record history is required")
    return clean


def optional_line(value: str | None, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    if not clean:
        return None
    if len(clean) > maximum or "\x00" in clean:
        raise ValidationError(f"{label} must be one bounded line")
    return clean


def calendar_date(value: str | None, label: str) -> str | None:
    clean = optional_line(value, label, 40)
    if clean is None:
        return None
    try:
        parsed = date.fromisoformat(clean) if CALENDAR_DATE.fullmatch(clean) else None
    except ValueError as exc:
        raise ValidationError(f"{label} must use YYYY-MM-DD") from exc
    if parsed is None or parsed.isoformat() != clean:
        raise ValidationError(f"{label} must use YYYY-MM-DD")
    return clean


def optional_stored_time(value: str | None, label: str) -> str | None:
    return stored_time(value, label) if value is not None else None


def required_value(value: str | None, label: str) -> str:
    if value is None:
        raise ValidationError(f"{label} is required")
    return value


def entity_status(value: str) -> EntityStatus:
    if value not in ENTITY_STATUSES:
        raise ValidationError(f"invalid entity status: {value}")
    return value  # type: ignore[return-value]


def relationship_status(value: str) -> RelationshipStatus:
    if value not in RELATIONSHIP_STATUSES:
        raise ValidationError(f"invalid relationship status: {value}")
    return value  # type: ignore[return-value]


def format_time(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def stored_time(value: str, label: str) -> str:
    clean = str(value).strip()
    if not TIMESTAMP.fullmatch(clean):
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    parse_time(clean)
    return clean


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ValidationError("timestamp must include UTC timezone")
    return parsed.astimezone(UTC)


def _expect_metadata_keys(
    metadata: dict[str, Any], expected: frozenset[str] | set[str], label: str
) -> None:
    actual = set(metadata)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        detail = f"missing {missing[0]}" if missing else f"unsupported field {extra[0]}"
        raise ValidationError(f"{label} metadata has an unsupported shape: {detail}")


def _task_is_rich(task: Task) -> bool:
    return any(
        (
            task.superseded_by is not None,
            task.project is not None,
            bool(task.entity_links),
            task.workspace is not None,
            task.attention_at is not None,
            task.due is not None,
            bool(task.codex_episode_ids),
            task.state_changed_at is not None,
            bool(task.history),
        )
    )


def _entity_is_rich(entity: Entity) -> bool:
    return any(
        (
            entity.status != "current",
            bool(entity.relationships),
            entity.observed_at is not None,
            entity.recheck_at is not None,
            entity.merged_into is not None,
            entity.merged_at is not None,
            entity.merged_from_updated_at is not None,
            bool(entity.merge_absorptions),
            bool(entity.history),
        )
    )


def _thread_is_rich(thread: WorkThread) -> bool:
    legacy_positions = tuple(range(1, len(thread.task_links) + 1))
    return any(
        (
            tuple(link.position for link in thread.task_links) != legacy_positions,
            any(link.role is not None for link in thread.entity_links),
            thread.closure_condition is not None,
            thread.next_actor is not None,
            thread.waiting_on is not None,
            thread.superseded_by is not None,
            thread.observed_at is not None,
            thread.state_changed_at is not None,
            thread.recheck_at is not None,
            thread.resolved_at is not None,
            bool(thread.history),
            thread.status not in {"active", "waiting", "dormant", "closed"},
        )
    )


def _validate_timeline(
    *,
    created_at: str,
    updated_at: str,
    observed_at: str | None = None,
    state_changed_at: str | None = None,
) -> None:
    created = parse_time(created_at)
    updated = parse_time(updated_at)
    if created > updated:
        raise ValidationError("record created_at cannot postdate updated_at")
    if observed_at is not None and parse_time(observed_at) > updated:
        raise ValidationError("record observed_at cannot postdate updated_at")
    if state_changed_at is not None and not created <= parse_time(state_changed_at) <= updated:
        raise ValidationError("record state_changed_at must fall inside its version history")


def _validate_task_state(task: Task) -> None:
    rich = _task_is_rich(task)
    if task.status in TERMINAL_TASK_STATUSES and any(
        (task.next_actor, task.next_action, task.waiting_on)
    ):
        raise ValidationError("terminal tasks cannot contain future-work fields")
    if task.status in TERMINAL_TASK_STATUSES and task.active_thread_id is not None:
        raise ValidationError("terminal tasks cannot claim an active Codex hand")
    if task.status == "waiting" and task.next_actor is None:
        raise ValidationError("waiting tasks require a next actor")
    if task.status != "superseded" and task.superseded_by is not None:
        raise ValidationError("only superseded tasks may retain a superseding task ID")
    if task.superseded_by == task.identifier:
        raise ValidationError("task cannot supersede itself")
    task_entity_links(task.entity_links)
    episodes = codex_episodes(task.codex_episode_ids)
    if task.active_thread_id is not None and task.active_thread_id not in episodes and rich:
        raise ValidationError("active Codex hand must also be retained in Codex episodes")
    calendar_date(task.attention_at, "attention date")
    calendar_date(task.due, "due date")
    optional_line(task.project, "project", 120)
    optional_line(task.workspace, "workspace", 2_048)
    _validate_timeline(
        created_at=stored_time(task.created_at, "created_at"),
        updated_at=stored_time(task.updated_at, "updated_at"),
        state_changed_at=task.state_changed_at,
    )
    if rich:
        if task.state_changed_at is None or not task.history:
            raise ValidationError("resident task requires state_changed_at and history")
        history_entries(task.history)


def _validate_entity_state(entity: Entity) -> None:
    rich = _entity_is_rich(entity)
    identifier = canonical_id(entity.identifier, "entity ID")
    clean_type = safe_token(entity.entity_type, "entity type")
    relationships = entity_relationships(entity.relationships)
    absorptions = entity_merge_absorptions(entity.merge_absorptions)
    _validate_timeline(
        created_at=stored_time(entity.created_at, "created_at"),
        updated_at=stored_time(entity.updated_at, "updated_at"),
        observed_at=entity.observed_at,
    )
    if rich and identifier.split(":", 1)[0] != clean_type:
        raise ValidationError("resident entity ID type must match entity_type")
    merge_fields = (entity.merged_into, entity.merged_at, entity.merged_from_updated_at)
    if entity.status == "merged" and any(value is None for value in merge_fields):
        raise ValidationError("merged entity requires complete redirect recovery state")
    if entity.status != "merged" and any(value is not None for value in merge_fields):
        raise ValidationError("only merged entities may retain redirect recovery state")
    if entity.merged_into == entity.identifier:
        raise ValidationError("entity cannot redirect to itself")
    if entity.merged_into is not None and entity.merged_into.split(":", 1)[0] != clean_type:
        raise ValidationError("entity redirect target must have the same type")
    if entity.status in {"merged", "superseded"} and any(
        value.status == "current" for value in relationships
    ):
        raise ValidationError("merged or superseded entities cannot retain current relationships")
    updated = parse_time(entity.updated_at)
    for relationship in relationships:
        if relationship.target == entity.identifier:
            raise ValidationError("entity cannot relate to itself")
        if parse_time(relationship.recorded_at) > updated:
            raise ValidationError("entity relationship postdates the entity version")
        if (
            relationship.status == "current"
            and relationship.valid_from is not None
            and parse_time(relationship.valid_from) > parse_time(relationship.recorded_at)
        ):
            raise ValidationError("current relationship valid_from cannot be in the future")
        if relationship.valid_to is not None and parse_time(relationship.valid_to) > updated:
            raise ValidationError("entity relationship valid_to postdates the entity version")
    if entity.merged_at is not None:
        merged_at = parse_time(entity.merged_at)
        if not parse_time(entity.created_at) <= merged_at <= updated:
            raise ValidationError("entity merged_at falls outside its version history")
        assert entity.merged_from_updated_at is not None
        merged_from = parse_time(entity.merged_from_updated_at)
        if not parse_time(entity.created_at) <= merged_from < merged_at:
            raise ValidationError("entity merged-from version falls outside its merge history")
    for absorption in absorptions:
        if absorption.source_id == entity.identifier:
            raise ValidationError("entity cannot absorb itself")
        if absorption.source_id.split(":", 1)[0] != clean_type:
            raise ValidationError("entity merge absorption must have the same type")
        if parse_time(absorption.merged_at) > updated:
            raise ValidationError("entity merge absorption postdates the entity version")
    if rich:
        if entity.observed_at is None or not entity.history:
            raise ValidationError("resident entity requires observed_at and history")
        history_entries(entity.history)


def _validate_thread_state(thread: WorkThread) -> None:
    rich = _thread_is_rich(thread)
    thread_task_links(thread.task_links)
    thread_entity_links(thread.entity_links)
    terminal = thread.status in TERMINAL_THREAD_STATUSES
    if terminal and any(
        (
            thread.next_actor,
            thread.next_move,
            thread.waiting_on,
            thread.focus_task_id,
            thread.recheck_at,
        )
    ):
        raise ValidationError("terminal threads cannot retain future-work fields")
    if thread.focus_task_id is not None and thread.focus_task_id not in thread.task_ids:
        raise ValidationError("focus task must be an exact member of the WorkThread")
    if thread.status == "superseded" and thread.superseded_by is None:
        raise ValidationError("superseded WorkThread requires a redirect target")
    if thread.status != "superseded" and thread.superseded_by is not None:
        raise ValidationError("only superseded WorkThreads may retain a redirect target")
    if thread.superseded_by == thread.identifier:
        raise ValidationError("WorkThread cannot redirect to itself")
    _validate_timeline(
        created_at=stored_time(thread.created_at, "created_at"),
        updated_at=stored_time(thread.updated_at, "updated_at"),
        observed_at=thread.observed_at,
        state_changed_at=thread.state_changed_at,
    )
    if rich:
        if thread.observed_at is None or thread.state_changed_at is None or not thread.history:
            raise ValidationError(
                "resident WorkThread requires observed_at, state_changed_at, and history"
            )
        history_entries(thread.history)
        if terminal and thread.resolved_at is None:
            raise ValidationError("terminal resident WorkThread requires resolved_at")
        if not terminal and thread.resolved_at is not None:
            raise ValidationError("nonterminal WorkThread cannot retain resolved_at")
        if thread.resolved_at is not None:
            resolved = parse_time(thread.resolved_at)
            if not parse_time(thread.state_changed_at) <= resolved <= parse_time(thread.updated_at):
                raise ValidationError("WorkThread resolved_at falls outside its version history")


def _review_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_REVISION.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a SHA-256 revision")
    return value


def _review_option_intent(value: object) -> str:
    if not isinstance(value, str) or value not in REVIEW_OPTION_INTENTS:
        raise ValidationError(f"invalid review option intent: {value}")
    return value


def _review_option_consequence(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("review option consequence must be a string")
    clean = value.strip()
    if (
        not clean
        or len(clean) > MAX_REVIEW_OPTION_LENGTH
        or "\n" in clean
        or "\r" in clean
        or "\x00" in clean
        or any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in clean)
    ):
        raise ValidationError("review option consequence must be one bounded non-empty line")
    return clean


def _parse_review_option(value: str) -> ReviewOption | None:
    parts = value.split(":", 4)
    if len(parts) != 5 or parts[0] != "review-option" or parts[2] != "task":
        return None
    try:
        intent = _review_option_intent(parts[1])
        subject_task_id = task_id(parts[3])
        consequence = unquote_to_bytes(parts[4]).decode("utf-8")
        consequence = _review_option_consequence(consequence)
    except (UnicodeError, ValidationError):
        return None
    if quote(consequence, safe="") != parts[4]:
        return None
    return ReviewOption(
        intent=intent,
        subject_task_id=subject_task_id,
        consequence=consequence,
        reference=value,
    )


def _render(metadata: dict[str, Any], title: str, sections: tuple[tuple[str, str], ...]) -> str:
    meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    body = [f"<!-- gsv:{meta} -->", "", f"# {title_text(title)}", ""]
    for heading, content in sections:
        clean = (
            _history_body(content)
            if heading == "History"
            else body_text(content, heading, required=True)
        )
        body.extend((f"## {heading}", clean, ""))
    markdown = "\n".join(body).rstrip() + "\n"
    if len(markdown.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValidationError("record is too large")
    return markdown


def _history_body(value: str) -> str:
    clean = str(value).strip()
    if not clean or "\x00" in clean or any(line.startswith("## ") for line in clean.splitlines()):
        raise ValidationError("History is invalid")
    if len(clean.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValidationError("History is too large")
    return clean


def _parse(
    markdown: str,
    expected_kind: str,
    headings: tuple[str, ...] | dict[int, tuple[str, ...]],
) -> tuple[dict[str, Any], str, dict[str, str], str]:
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValidationError("record is too large")
    lines_value = markdown.splitlines()
    if len(lines_value) < 4:
        raise ValidationError("record is incomplete")
    match = META.fullmatch(lines_value[0])
    if not match:
        raise ValidationError("record metadata header is missing")
    try:
        metadata = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError("record metadata is invalid JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("kind") != expected_kind:
        raise ValidationError(f"expected a {expected_kind} record")
    supported_versions = {
        "task": TASK_FORMAT_VERSIONS,
        "entity": ENTITY_FORMAT_VERSIONS,
        "thread": THREAD_FORMAT_VERSIONS,
    }[expected_kind]
    stored_version = metadata.get("version")
    if type(stored_version) is not int or stored_version not in supported_versions:
        raise ValidationError(f"unsupported record version: {stored_version}")
    expected_headings = headings[stored_version] if isinstance(headings, dict) else headings

    title_index = next((index for index, line in enumerate(lines_value[1:], 1) if line), -1)
    if title_index < 0 or not lines_value[title_index].startswith("# "):
        raise ValidationError("record title heading is missing")
    title = lines_value[title_index][2:]
    positions: list[tuple[int, str]] = []
    for index, line in enumerate(lines_value[title_index + 1 :], title_index + 1):
        if line.startswith("## "):
            positions.append((index, line[3:].strip()))
    actual_headings = tuple(name for _, name in positions)
    if actual_headings != expected_headings:
        if isinstance(headings, dict) and actual_headings in set(headings.values()):
            raise ValidationError("unsupported record version for the stored section shape")
        raise ValidationError(f"record sections must be exactly: {', '.join(expected_headings)}")
    sections: dict[str, str] = {}
    for offset, (start, name) in enumerate(positions):
        end = positions[offset + 1][0] if offset + 1 < len(positions) else len(lines_value)
        sections[name] = "\n".join(lines_value[start + 1 : end]).strip()
    return metadata, title, sections, sha256_bytes(encoded)


def _string(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"record metadata field {key} must be a string")
    return value


def _optional_string(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"record metadata field {key} must be a string or null")
    return value


def _optional_integer(metadata: dict[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"record metadata field {key} must be an integer or null")
    return value


def _string_tuple(metadata: dict[str, Any], key: str) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"record metadata field {key} must be a string list")
    return tuple(value)


def _missing(value: str) -> str | None:
    clean = value.strip()
    return None if clean in {"", "Not recorded."} else clean


def _optional_section(metadata: dict[str, Any], key: str, value: str) -> str | None:
    present = metadata.get(key)
    if present is None:
        return _missing(value)
    if not isinstance(present, bool):
        raise ValidationError(f"record metadata field {key} must be a boolean")
    return body_text(value, key, required=True) if present else None
