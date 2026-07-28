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
ABSENT_DIRECTION_REVISION: Final = "absent"
MAX_DIRECTION_AIMS: Final = 100
MAX_AIM_TITLE_LENGTH: Final = 180
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
) -> Direction:
    candidate = Direction(
        status=_status(status),
        current_chapter=body_text(current_chapter, "Direction current chapter", required=True),
        aims=direction_aims(aims),
        updated_at=format_time(observed_at or datetime.now(UTC)),
        revision="",
    )
    return parse_direction(render_direction(candidate))


def render_direction(direction: Direction) -> str:
    metadata = {
        "aims": [asdict(value) for value in direction_aims(direction.aims)],
        "id": "direction:current",
        "kind": "direction",
        "status": _status(direction.status),
        "updated_at": _stored_time(direction.updated_at),
        "version": DIRECTION_FORMAT_VERSION,
    }
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
    expected = {"aims", "id", "kind", "status", "updated_at", "version"}
    if set(metadata) != expected:
        raise ValidationError("Direction metadata has an unsupported shape")
    if metadata.get("id") != "direction:current" or metadata.get("kind") != "direction":
        raise ValidationError("expected the current Direction record")
    if metadata.get("version") != DIRECTION_FORMAT_VERSION:
        raise ValidationError(f"unsupported Direction version: {metadata.get('version')}")
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
    return Direction(
        status=_status(metadata.get("status")),
        current_chapter=body_text(
            "\n".join(lines[5:]).strip(), "Direction current chapter", required=True
        ),
        aims=direction_aims(tuple(aims)),
        updated_at=_stored_time(metadata.get("updated_at")),
        revision=sha256_bytes(encoded),
    )


def direction_dict(direction: Direction) -> dict[str, Any]:
    return {
        "aims": [asdict(value) for value in direction.aims],
        "current_chapter": direction.current_chapter,
        "revision": direction.revision,
        "status": direction.status,
        "updated_at": direction.updated_at,
    }


def _status(value: object) -> DirectionStatus:
    if not isinstance(value, str) or value not in DIRECTION_STATUSES:
        raise ValidationError(f"invalid Direction status: {value}")
    return value  # type: ignore[return-value]


def _stored_time(value: object) -> str:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise ValidationError("Direction updated_at must be an ISO-8601 UTC timestamp")
    parse_time(value)
    return value
