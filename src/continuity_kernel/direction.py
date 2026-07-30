"""Owner-neutral authored life direction with stable aim identifiers."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.errors import ValidationError
from continuity_kernel.records import (
    MAX_RECORD_BYTES,
    TIMESTAMP,
    body_text,
    format_time,
    parse_time,
)

DIRECTION_FORMAT_VERSION: Final = 1
DIRECTION_RICH_FORMAT_VERSION: Final = 2
DIRECTION_FORMAT_VERSIONS: Final = frozenset(
    {DIRECTION_FORMAT_VERSION, DIRECTION_RICH_FORMAT_VERSION}
)
ABSENT_DIRECTION_REVISION: Final = "absent"
MAX_DIRECTION_AIMS: Final = 100
MAX_AIM_TITLE_LENGTH: Final = 180
MAX_DIRECTION_LIST_ITEMS: Final = 100
MAX_DIRECTION_REFS: Final = 100
MAX_DIRECTION_HISTORY: Final = 500
MAX_DIRECTION_REF_LENGTH: Final = 1_000
MAX_DIRECTION_HISTORY_LENGTH: Final = 2_000
MAX_DIRECTION_LIST_ITEM_BYTES: Final = 4_000
META = re.compile(r"^<!-- gsv-direction:(\{.*\}) -->$")
AIM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")

DirectionStatus = Literal["provisional", "confirmed"]
DIRECTION_STATUSES: Final = frozenset({"provisional", "confirmed"})


@dataclass(frozen=True)
class DirectionAim:
    """One explicitly authored, stable life aim."""

    identifier: str
    title: str
    desired_state: str


@dataclass(frozen=True)
class Direction:
    """The current user-correctable framing used by Portfolio judgment."""

    status: DirectionStatus
    current_chapter: str
    aims: tuple[DirectionAim, ...]
    updated_at: str
    revision: str
    constraints: tuple[str, ...] = ()
    tensions: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    observed_at: str | None = None
    recorded_at: str | None = None
    recheck_at: str | None = None
    history: tuple[str, ...] = ()
    format_version: int = DIRECTION_FORMAT_VERSION


def direction_aim(*, identifier: object, title: object, desired_state: object) -> DirectionAim:
    if not isinstance(identifier, str) or AIM_ID.fullmatch(identifier) is None:
        raise ValidationError("Direction aim ID must be a stable lowercase slug")
    if not isinstance(title, str):
        raise ValidationError("Direction aim title must be a string")
    clean_title = title.strip()
    if (
        not clean_title
        or len(clean_title) > MAX_AIM_TITLE_LENGTH
        or "\n" in clean_title
        or "\r" in clean_title
        or "\x00" in clean_title
    ):
        raise ValidationError(
            "Direction aim title must be one non-empty line up to "
            f"{MAX_AIM_TITLE_LENGTH} characters"
        )
    if not isinstance(desired_state, str):
        raise ValidationError("Direction aim desired state must be a string")
    return DirectionAim(
        identifier=identifier,
        title=clean_title,
        desired_state=body_text(desired_state, "Direction aim desired state", required=True),
    )


def direction_aims(values: tuple[DirectionAim, ...]) -> tuple[DirectionAim, ...]:
    if len(values) > MAX_DIRECTION_AIMS:
        raise ValidationError("Direction contains too many aims")
    clean = tuple(
        direction_aim(
            identifier=value.identifier,
            title=value.title,
            desired_state=value.desired_state,
        )
        for value in values
    )
    identifiers = [value.identifier for value in clean]
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError("Direction aim IDs must be unique")
    if not clean:
        raise ValidationError("Direction requires at least one explicit aim")
    return clean


def new_direction(
    *,
    status: str,
    current_chapter: str,
    aims: tuple[DirectionAim, ...],
    observed_at: datetime | None = None,
    constraints: tuple[str, ...] = (),
    tensions: tuple[str, ...] = (),
    refs: tuple[str, ...] = (),
    source_observed_at: str | None = None,
    recorded_at: str | None = None,
    recheck_at: str | None = None,
    history: tuple[str, ...] = (),
) -> Direction:
    rich = any(
        (
            constraints,
            tensions,
            refs,
            source_observed_at is not None,
            recorded_at is not None,
            recheck_at is not None,
            history,
        )
    )
    candidate = Direction(
        status=_status(status),
        current_chapter=body_text(current_chapter, "Direction current chapter", required=True),
        aims=direction_aims(aims),
        updated_at=format_time(observed_at or datetime.now(UTC)),
        revision="",
        constraints=constraints,
        tensions=tensions,
        refs=refs,
        observed_at=source_observed_at,
        recorded_at=recorded_at,
        recheck_at=recheck_at,
        history=history,
        format_version=(DIRECTION_RICH_FORMAT_VERSION if rich else DIRECTION_FORMAT_VERSION),
    )
    return parse_direction(render_direction(candidate))


def render_direction(direction: Direction) -> str:
    version = _format_version(direction.format_version)
    _validate_version_content(direction, version=version)
    metadata = {
        "aims": [asdict(value) for value in direction_aims(direction.aims)],
        "id": "direction:current",
        "kind": "direction",
        "status": _status(direction.status),
        "updated_at": _stored_time(direction.updated_at),
        "version": version,
    }
    if version == DIRECTION_RICH_FORMAT_VERSION:
        metadata.update(
            {
                "constraints": list(_text_values(direction.constraints, "Direction constraint")),
                "history": list(_history(direction.history)),
                "observed_at": _rich_time(direction.observed_at, "Direction observed_at"),
                "recorded_at": _rich_time(direction.recorded_at, "Direction recorded_at"),
                "recheck_at": _rich_time(direction.recheck_at, "Direction recheck_at"),
                "refs": list(_refs(direction.refs)),
                "tensions": list(_text_values(direction.tensions, "Direction tension")),
            }
        )
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    chapter = body_text(direction.current_chapter, "Direction current chapter", required=True)
    markdown = f"<!-- gsv-direction:{header} -->\n\n# Direction\n\n## Current chapter\n{chapter}\n"
    if len(markdown.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValidationError("Direction record is too large")
    return markdown


def parse_direction(markdown: str) -> Direction:
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValidationError("Direction record is too large")
    lines = markdown.splitlines()
    if len(lines) < 6:
        raise ValidationError("Direction record is incomplete")
    matched = META.fullmatch(lines[0])
    if matched is None:
        raise ValidationError("Direction metadata header is missing")
    try:
        metadata = json.loads(matched.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError("Direction metadata is invalid JSON") from exc
    if not isinstance(metadata, dict):
        raise ValidationError("Direction metadata must be an object")
    version = _format_version(metadata.get("version"))
    expected = {"aims", "id", "kind", "status", "updated_at", "version"}
    if version == DIRECTION_RICH_FORMAT_VERSION:
        expected |= {
            "constraints",
            "history",
            "observed_at",
            "recorded_at",
            "recheck_at",
            "refs",
            "tensions",
        }
    if set(metadata) != expected:
        raise ValidationError("Direction metadata has an unsupported shape")
    if metadata.get("id") != "direction:current" or metadata.get("kind") != "direction":
        raise ValidationError("expected the current Direction record")
    if lines[2:5] != ["# Direction", "", "## Current chapter"]:
        raise ValidationError("Direction sections must be exactly the current chapter")
    raw_aims = metadata.get("aims")
    if not isinstance(raw_aims, list):
        raise ValidationError("Direction aims must be a list")
    aims = []
    for value in raw_aims:
        if not isinstance(value, dict) or set(value) != {"identifier", "title", "desired_state"}:
            raise ValidationError(
                "each Direction aim must contain only id, title, and desired state"
            )
        aims.append(
            direction_aim(
                identifier=value["identifier"],
                title=value["title"],
                desired_state=value["desired_state"],
            )
        )
    direction = Direction(
        status=_status(metadata.get("status")),
        current_chapter=body_text(
            "\n".join(lines[5:]).strip(), "Direction current chapter", required=True
        ),
        aims=direction_aims(tuple(aims)),
        updated_at=_stored_time(metadata.get("updated_at")),
        revision=sha256_bytes(encoded),
        constraints=(
            _text_values(_string_list(metadata, "constraints"), "Direction constraint")
            if version == DIRECTION_RICH_FORMAT_VERSION
            else ()
        ),
        tensions=(
            _text_values(_string_list(metadata, "tensions"), "Direction tension")
            if version == DIRECTION_RICH_FORMAT_VERSION
            else ()
        ),
        refs=(
            _refs(_string_list(metadata, "refs"))
            if version == DIRECTION_RICH_FORMAT_VERSION
            else ()
        ),
        observed_at=(
            _rich_time(metadata.get("observed_at"), "Direction observed_at")
            if version == DIRECTION_RICH_FORMAT_VERSION
            else None
        ),
        recorded_at=(
            _rich_time(metadata.get("recorded_at"), "Direction recorded_at")
            if version == DIRECTION_RICH_FORMAT_VERSION
            else None
        ),
        recheck_at=(
            _rich_time(metadata.get("recheck_at"), "Direction recheck_at")
            if version == DIRECTION_RICH_FORMAT_VERSION
            else None
        ),
        history=(
            _history(_string_list(metadata, "history"))
            if version == DIRECTION_RICH_FORMAT_VERSION
            else ()
        ),
        format_version=version,
    )
    _validate_version_content(direction, version=version)
    return direction


def direction_dict(direction: Direction) -> dict[str, Any]:
    value: dict[str, Any] = {
        "aims": [asdict(value) for value in direction.aims],
        "current_chapter": direction.current_chapter,
        "revision": direction.revision,
        "status": direction.status,
        "updated_at": direction.updated_at,
    }
    if direction.format_version == DIRECTION_RICH_FORMAT_VERSION:
        value.update(
            {
                "constraints": list(direction.constraints),
                "format_version": direction.format_version,
                "history": list(direction.history),
                "observed_at": direction.observed_at,
                "recorded_at": direction.recorded_at,
                "recheck_at": direction.recheck_at,
                "refs": list(direction.refs),
                "tensions": list(direction.tensions),
            }
        )
    return value


def _status(value: object) -> DirectionStatus:
    if not isinstance(value, str) or value not in DIRECTION_STATUSES:
        raise ValidationError(f"invalid Direction status: {value}")
    return value  # type: ignore[return-value]


def _stored_time(value: object) -> str:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise ValidationError("Direction updated_at must be an ISO-8601 UTC timestamp")
    parse_time(value)
    return value


def _format_version(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in DIRECTION_FORMAT_VERSIONS
    ):
        raise ValidationError(f"unsupported Direction version: {value}")
    return value


def _validate_version_content(direction: Direction, *, version: int) -> None:
    rich_present = any(
        (
            direction.constraints,
            direction.tensions,
            direction.refs,
            direction.observed_at is not None,
            direction.recorded_at is not None,
            direction.recheck_at is not None,
            direction.history,
        )
    )
    if version == DIRECTION_FORMAT_VERSION:
        if rich_present:
            raise ValidationError("Direction version 1 cannot contain version 2 continuity fields")
        return
    if not rich_present:
        raise ValidationError("Direction version 2 requires continuity fields")
    observed = parse_time(_rich_time(direction.observed_at, "Direction observed_at"))
    recorded = parse_time(_rich_time(direction.recorded_at, "Direction recorded_at"))
    updated = parse_time(_stored_time(direction.updated_at))
    recheck = parse_time(_rich_time(direction.recheck_at, "Direction recheck_at"))
    if recorded > updated or observed > updated:
        raise ValidationError("Direction version 2 has an invalid continuity timeline")
    if recheck <= updated:
        raise ValidationError("Direction recheck_at must be later than updated_at")
    _text_values(direction.constraints, "Direction constraint")
    _text_values(direction.tensions, "Direction tension")
    _refs(direction.refs)
    _history(direction.history)


def _text_values(values: object, label: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise ValidationError(f"{label} values must be an array")
    if len(values) > MAX_DIRECTION_LIST_ITEMS:
        raise ValidationError(f"too many {label.lower()} values")
    clean = tuple(_rich_text(value, label) for value in values)
    if len(set(clean)) != len(clean):
        raise ValidationError(f"{label} values must be unique")
    return clean


def _refs(values: object) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise ValidationError("Direction refs must be an array")
    if len(values) > MAX_DIRECTION_REFS:
        raise ValidationError("Direction contains too many refs")
    clean = tuple(
        _single_line(value, MAX_DIRECTION_REF_LENGTH, "Direction ref") for value in values
    )
    if len(set(clean)) != len(clean):
        raise ValidationError("Direction refs must be unique")
    return clean


def _history(values: object) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise ValidationError("Direction history must be an array")
    if not values or len(values) > MAX_DIRECTION_HISTORY:
        raise ValidationError("Direction history must contain 1 to 500 entries")
    return tuple(
        _single_line(value, MAX_DIRECTION_HISTORY_LENGTH, "Direction history entry")
        for value in values
    )


def _single_line(value: object, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    clean = " ".join(value.split())
    if not clean or len(clean) > maximum or "\x00" in clean:
        raise ValidationError(f"{label} is invalid")
    return clean


def _rich_time(value: object, label: str) -> str:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    parse_time(value)
    return value


def _string_list(metadata: dict[str, object], key: str) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"Direction {key} must be a string array")
    return tuple(value)


def _rich_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    clean = value.strip()
    if not clean or "\x00" in clean or len(clean.encode("utf-8")) > MAX_DIRECTION_LIST_ITEM_BYTES:
        raise ValidationError(f"{label} is invalid or too large")
    return clean
