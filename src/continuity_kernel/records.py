"""Typed, human-readable Markdown records with stable machine metadata."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, TypeVar

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.errors import ValidationError

FORMAT_VERSION: Final = 1
MAX_RECORD_BYTES: Final = 256 * 1024
MAX_TEXT_BYTES: Final = 64 * 1024
MAX_TITLE_LENGTH: Final = 180
MAX_REFERENCES: Final = 2_000
MAX_RELATIONS: Final = 100
MAX_TASK_RANK: Final = 2_147_483_647
SAFE_ID = re.compile(r"^[a-z][a-z0-9]*(?::[a-z0-9][a-z0-9-]{0,95})$")
SAFE_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
SAFE_HAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
META = re.compile(r"^<!-- gsv:(\{.*\}) -->$")

TaskStatus = Literal["captured", "ready", "doing", "waiting", "someday", "done", "dropped"]
Actor = Literal["agent", "human", "external"]
ThreadStatus = Literal["active", "waiting", "dormant", "closed"]

TASK_STATUSES: Final = frozenset(
    {"captured", "ready", "doing", "waiting", "someday", "done", "dropped"}
)
TERMINAL_TASK_STATUSES: Final = frozenset({"done", "dropped"})
ACTORS: Final = frozenset({"agent", "human", "external"})
THREAD_STATUSES: Final = frozenset({"active", "waiting", "dormant", "closed"})
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


@dataclass(frozen=True)
class WorkThread:
    identifier: str
    title: str
    status: ThreadStatus
    purpose: str
    summary: str
    next_move: str | None
    task_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]
    refs: tuple[str, ...]
    created_at: str
    updated_at: str
    revision: str


Record = Task | Entity | WorkThread
T = TypeVar("T", Task, Entity, WorkThread)


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
    observed_at: datetime | None = None,
) -> Task:
    now = format_time(observed_at or datetime.now(UTC))
    identifier = task_id(identifier)
    if identifier.rstrip(" .").casefold() in WINDOWS_RESERVED_NAMES:
        raise ValidationError(
            "task ID is reserved by Windows; choose a portable identifier before creating it"
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
        active_thread_id=hand_id(active_thread_id),
        refs=references(refs),
        created_at=now,
        updated_at=now,
        revision="",
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
    observed_at: datetime | None = None,
) -> Entity:
    now = format_time(observed_at or datetime.now(UTC))
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
    task_ids: tuple[str, ...] = (),
    entity_ids: tuple[str, ...] = (),
    refs: tuple[str, ...] = (),
    observed_at: datetime | None = None,
) -> WorkThread:
    now = format_time(observed_at or datetime.now(UTC))
    thread = WorkThread(
        identifier=canonical_id(identifier, "thread ID", prefix="thread"),
        title=title_text(title),
        status=thread_status(status),
        purpose=body_text(purpose, "thread purpose", required=True),
        summary=body_text(summary, "thread summary", required=True),
        next_move=optional_body(next_move, "next move"),
        task_ids=task_ids_value(task_ids),
        entity_ids=entity_ids_value(entity_ids),
        refs=references(refs),
        created_at=now,
        updated_at=now,
        revision="",
    )
    _validate_thread_state(thread)
    return parse_thread(render_thread(thread))


def render_task(task: Task) -> str:
    _validate_task_state(task)
    metadata = {
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
        "version": FORMAT_VERSION,
        "waiting_on_present": task.waiting_on is not None,
    }
    return _render(
        metadata,
        task.title,
        (
            ("Outcome", task.outcome),
            ("Next action", task.next_action or "Not recorded."),
            ("Waiting on", task.waiting_on or "Not recorded."),
        ),
    )


def render_entity(entity: Entity) -> str:
    metadata = {
        "aliases": list(lines(entity.aliases, "entity alias", MAX_RELATIONS)),
        "created_at": stored_time(entity.created_at, "created_at"),
        "entity_type": safe_token(entity.entity_type, "entity type"),
        "id": canonical_id(entity.identifier, "entity ID"),
        "kind": "entity",
        "refs": list(references(entity.refs)),
        "updated_at": stored_time(entity.updated_at, "updated_at"),
        "version": FORMAT_VERSION,
    }
    return _render(metadata, entity.title, (("Summary", entity.summary),))


def render_thread(thread: WorkThread) -> str:
    _validate_thread_state(thread)
    metadata = {
        "created_at": stored_time(thread.created_at, "created_at"),
        "entity_ids": list(entity_ids_value(thread.entity_ids)),
        "id": canonical_id(thread.identifier, "thread ID", prefix="thread"),
        "kind": "thread",
        "next_move_present": thread.next_move is not None,
        "refs": list(references(thread.refs)),
        "status": thread_status(thread.status),
        "task_ids": list(task_ids_value(thread.task_ids)),
        "updated_at": stored_time(thread.updated_at, "updated_at"),
        "version": FORMAT_VERSION,
    }
    return _render(
        metadata,
        thread.title,
        (
            ("Purpose", thread.purpose),
            ("Current summary", thread.summary),
            ("Next move", thread.next_move or "Not recorded."),
        ),
    )


def parse_task(markdown: str) -> Task:
    meta, title, sections, revision = _parse(
        markdown, "task", ("Outcome", "Next action", "Waiting on")
    )
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
    )
    _validate_task_state(task)
    return task


def parse_entity(markdown: str) -> Entity:
    meta, title, sections, revision = _parse(markdown, "entity", ("Summary",))
    return Entity(
        identifier=canonical_id(_string(meta, "id"), "entity ID"),
        title=title_text(title),
        entity_type=safe_token(_string(meta, "entity_type"), "entity type"),
        aliases=lines(_string_tuple(meta, "aliases"), "entity alias", MAX_RELATIONS),
        summary=body_text(sections["Summary"], "entity summary", required=True),
        refs=references(_string_tuple(meta, "refs")),
        created_at=stored_time(_string(meta, "created_at"), "created_at"),
        updated_at=stored_time(_string(meta, "updated_at"), "updated_at"),
        revision=revision,
    )


def parse_thread(markdown: str) -> WorkThread:
    meta, title, sections, revision = _parse(
        markdown, "thread", ("Purpose", "Current summary", "Next move")
    )
    thread = WorkThread(
        identifier=canonical_id(_string(meta, "id"), "thread ID", prefix="thread"),
        title=title_text(title),
        status=thread_status(_string(meta, "status")),
        purpose=body_text(sections["Purpose"], "thread purpose", required=True),
        summary=body_text(sections["Current summary"], "thread summary", required=True),
        next_move=_optional_section(meta, "next_move_present", sections["Next move"]),
        task_ids=task_ids_value(_string_tuple(meta, "task_ids")),
        entity_ids=entity_ids_value(_string_tuple(meta, "entity_ids")),
        refs=references(_string_tuple(meta, "refs")),
        created_at=stored_time(_string(meta, "created_at"), "created_at"),
        updated_at=stored_time(_string(meta, "updated_at"), "updated_at"),
        revision=revision,
    )
    _validate_thread_state(thread)
    return thread


def record_dict(record: Record) -> dict[str, Any]:
    payload = asdict(record)
    for key, value in tuple(payload.items()):
        if isinstance(value, tuple):
            payload[key] = list(value)
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


def safe_token(value: str, label: str) -> str:
    clean = str(value).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", clean):
        raise ValidationError(f"{label} must be a lowercase token")
    return clean


def references(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return lines(tuple(values), "reference", MAX_REFERENCES, max_length=1000)


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
    if len(values) > MAX_RELATIONS:
        raise ValidationError("too many task relationships")
    return tuple(dict.fromkeys(task_id(item) for item in values))


def entity_ids_value(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if len(values) > MAX_RELATIONS:
        raise ValidationError("too many entity relationships")
    return tuple(dict.fromkeys(canonical_id(item, "entity ID") for item in values))


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


def _validate_task_state(task: Task) -> None:
    if task.status in TERMINAL_TASK_STATUSES and any(
        (task.next_actor, task.next_action, task.waiting_on)
    ):
        raise ValidationError("terminal tasks cannot contain future-work fields")
    if task.status in TERMINAL_TASK_STATUSES and task.active_thread_id is not None:
        raise ValidationError("terminal tasks cannot claim an active Codex hand")
    if task.status == "waiting" and task.next_actor is None:
        raise ValidationError("waiting tasks require a next actor")


def _validate_thread_state(thread: WorkThread) -> None:
    if thread.status == "closed" and thread.next_move is not None:
        raise ValidationError("closed threads cannot contain a next move")


def _render(metadata: dict[str, Any], title: str, sections: tuple[tuple[str, str], ...]) -> str:
    meta = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    body = [f"<!-- gsv:{meta} -->", "", f"# {title_text(title)}", ""]
    for heading, content in sections:
        body.extend((f"## {heading}", body_text(content, heading, required=True), ""))
    markdown = "\n".join(body).rstrip() + "\n"
    if len(markdown.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValidationError("record is too large")
    return markdown


def _parse(
    markdown: str, expected_kind: str, headings: tuple[str, ...]
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
    if metadata.get("version") != FORMAT_VERSION:
        raise ValidationError(f"unsupported record version: {metadata.get('version')}")

    title_index = next((index for index, line in enumerate(lines_value[1:], 1) if line), -1)
    if title_index < 0 or not lines_value[title_index].startswith("# "):
        raise ValidationError("record title heading is missing")
    title = lines_value[title_index][2:]
    positions: list[tuple[int, str]] = []
    for index, line in enumerate(lines_value[title_index + 1 :], title_index + 1):
        if line.startswith("## "):
            positions.append((index, line[3:].strip()))
    if tuple(name for _, name in positions) != headings:
        raise ValidationError(f"record sections must be exactly: {', '.join(headings)}")
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
