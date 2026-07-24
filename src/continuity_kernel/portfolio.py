"""Authored whole-portfolio judgment over the complete open task set."""

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
    task_id,
)

PORTFOLIO_FORMAT_VERSION: Final = 1
ABSENT_PORTFOLIO_REVISION: Final = "absent"
MAX_PORTFOLIO_ITEMS: Final = 10_000
MAX_REASON_BYTES: Final = 8 * 1024
SHA256_REVISION = re.compile(r"^[0-9a-f]{64}$")
META = re.compile(r"^<!-- gsv-portfolio:(\{.*\}) -->$")

PortfolioStance = Literal["needs-human", "agent-can-carry", "keep-in-view", "reconsider"]
PORTFOLIO_STANCES: Final = frozenset(
    {"needs-human", "agent-can-carry", "keep-in-view", "reconsider"}
)


@dataclass(frozen=True)
class PortfolioItem:
    task_id: str
    task_revision: str
    stance: PortfolioStance
    reason: str
    work_thread_id: str | None = None
    work_thread_revision: str | None = None


@dataclass(frozen=True)
class Portfolio:
    summary: str
    items: tuple[PortfolioItem, ...]
    updated_at: str
    revision: str


def new_portfolio(
    *,
    summary: str,
    items: tuple[PortfolioItem, ...],
    observed_at: datetime | None = None,
) -> Portfolio:
    candidate = Portfolio(
        summary=body_text(summary, "Portfolio summary", required=True),
        items=portfolio_items(items),
        updated_at=format_time(observed_at or datetime.now(UTC)),
        revision="",
    )
    return parse_portfolio(render_portfolio(candidate))


def render_portfolio(portfolio: Portfolio) -> str:
    items = portfolio_items(portfolio.items)
    updated_at = _stored_time(portfolio.updated_at)
    metadata = {
        "items": [asdict(item) for item in items],
        "kind": "portfolio",
        "updated_at": updated_at,
        "version": PORTFOLIO_FORMAT_VERSION,
    }
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    summary = body_text(portfolio.summary, "Portfolio summary", required=True)
    markdown = f"<!-- gsv-portfolio:{header} -->\n\n# Portfolio\n\n## Working summary\n{summary}\n"
    if len(markdown.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValidationError("Portfolio record is too large")
    return markdown


def parse_portfolio(markdown: str) -> Portfolio:
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValidationError("Portfolio record is too large")
    lines = markdown.splitlines()
    if len(lines) < 6:
        raise ValidationError("Portfolio record is incomplete")
    matched = META.fullmatch(lines[0])
    if matched is None:
        raise ValidationError("Portfolio metadata header is missing")
    try:
        metadata = json.loads(matched.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError("Portfolio metadata is invalid JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("kind") != "portfolio":
        raise ValidationError("expected a Portfolio record")
    if metadata.get("version") != PORTFOLIO_FORMAT_VERSION:
        raise ValidationError(f"unsupported Portfolio version: {metadata.get('version')}")
    if lines[2:5] != ["# Portfolio", "", "## Working summary"]:
        raise ValidationError("Portfolio sections must be exactly the working summary")
    raw_items = metadata.get("items")
    if not isinstance(raw_items, list):
        raise ValidationError("Portfolio items must be a list")
    items = portfolio_items(tuple(_parse_item(value) for value in raw_items))
    summary = body_text("\n".join(lines[5:]).strip(), "Portfolio summary", required=True)
    return Portfolio(
        summary=summary,
        items=items,
        updated_at=_stored_time(metadata.get("updated_at")),
        revision=sha256_bytes(encoded),
    )


def portfolio_item(
    *,
    task_id_value: object,
    task_revision: object,
    stance: object,
    reason: object,
    work_thread_id: object = None,
    work_thread_revision: object = None,
) -> PortfolioItem:
    if not isinstance(task_id_value, str):
        raise ValidationError("Portfolio task ID must be a string")
    if not isinstance(task_revision, str):
        raise ValidationError("Portfolio task revision must be a string")
    if not isinstance(stance, str):
        raise ValidationError("Portfolio stance must be a string")
    if not isinstance(reason, str):
        raise ValidationError("Portfolio reason must be a string")
    if work_thread_id is not None and not isinstance(work_thread_id, str):
        raise ValidationError("Portfolio work-thread ID must be a string or null")
    if work_thread_revision is not None and not isinstance(work_thread_revision, str):
        raise ValidationError("Portfolio work-thread revision must be a string or null")
    clean_thread = _optional_thread_id(work_thread_id)
    clean_thread_revision = _optional_revision(work_thread_revision)
    if (clean_thread is None) != (clean_thread_revision is None):
        raise ValidationError("Portfolio thread ID and revision must be authored together")
    return PortfolioItem(
        task_id=task_id(task_id_value),
        task_revision=_revision(task_revision, "task revision"),
        stance=_stance(stance),
        reason=_reason(reason),
        work_thread_id=clean_thread,
        work_thread_revision=clean_thread_revision,
    )


def portfolio_items(values: tuple[PortfolioItem, ...]) -> tuple[PortfolioItem, ...]:
    if len(values) > MAX_PORTFOLIO_ITEMS:
        raise ValidationError("Portfolio contains too many items")
    clean = tuple(
        portfolio_item(
            task_id_value=item.task_id,
            task_revision=item.task_revision,
            stance=item.stance,
            reason=item.reason,
            work_thread_id=item.work_thread_id,
            work_thread_revision=item.work_thread_revision,
        )
        for item in values
    )
    identifiers = [item.task_id for item in clean]
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError("Portfolio may contain each task exactly once")
    return clean


def portfolio_dict(portfolio: Portfolio) -> dict[str, Any]:
    return {
        "items": [asdict(item) for item in portfolio.items],
        "revision": portfolio.revision,
        "summary": portfolio.summary,
        "updated_at": portfolio.updated_at,
    }


def _parse_item(value: object) -> PortfolioItem:
    if not isinstance(value, dict):
        raise ValidationError("each Portfolio item must be an object")
    allowed = {
        "task_id",
        "task_revision",
        "stance",
        "reason",
        "work_thread_id",
        "work_thread_revision",
    }
    if set(value) - allowed:
        raise ValidationError("Portfolio item contains unsupported fields")
    return portfolio_item(
        task_id_value=_string(value, "task_id"),
        task_revision=_string(value, "task_revision"),
        stance=_string(value, "stance"),
        reason=_string(value, "reason"),
        work_thread_id=_optional_string(value, "work_thread_id"),
        work_thread_revision=_optional_string(value, "work_thread_revision"),
    )


def _string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValidationError(f"Portfolio item field {key} must be a string")
    return item


def _optional_string(value: dict[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ValidationError(f"Portfolio item field {key} must be a string or null")
    return item


def _stance(value: str) -> PortfolioStance:
    if value not in PORTFOLIO_STANCES:
        raise ValidationError(f"invalid Portfolio stance: {value}")
    return value  # type: ignore[return-value]


def _reason(value: str) -> str:
    clean = body_text(value, "Portfolio reason", required=True)
    if len(clean.encode("utf-8")) > MAX_REASON_BYTES:
        raise ValidationError("Portfolio reason is too large")
    return clean


def _revision(value: str, label: str) -> str:
    clean = str(value).strip().lower()
    if not SHA256_REVISION.fullmatch(clean):
        raise ValidationError(f"{label} must be a SHA-256 revision")
    return clean


def _optional_revision(value: str | None) -> str | None:
    return None if value is None else _revision(value, "work-thread revision")


def _optional_thread_id(value: str | None) -> str | None:
    if value is None:
        return None
    clean = str(value).strip().lower()
    if not re.fullmatch(r"thread:[a-z0-9][a-z0-9-]{0,95}", clean):
        raise ValidationError("Portfolio work-thread ID must look like thread:stable-id")
    return clean


def _stored_time(value: object) -> str:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise ValidationError("Portfolio updated_at must be an ISO-8601 UTC timestamp")
    parse_time(value)
    return value
