"""Authored whole-portfolio judgment over the complete open task set."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.direction import Direction
from continuity_kernel.errors import ValidationError
from continuity_kernel.records import (
    MAX_RECORD_BYTES,
    REVIEW_WORK_THREAD_ID,
    TERMINAL_TASK_STATUSES,
    TERMINAL_THREAD_STATUSES,
    TIMESTAMP,
    ReviewOption,
    Task,
    WorkThread,
    body_text,
    format_time,
    has_review_session_signal,
    is_resident_pulse_task,
    parse_review_references,
    parse_time,
    task_id,
)

PORTFOLIO_RICH_FORMAT_VERSION: Final = 3
PORTFOLIO_FORMAT_VERSIONS: Final = frozenset({1, 2, PORTFOLIO_RICH_FORMAT_VERSION})
ABSENT_PORTFOLIO_REVISION: Final = "absent"
MAX_PORTFOLIO_ITEMS: Final = 10_000
MAX_GUIDED_REVIEW_OUTCOMES: Final = 512
MAX_DIRECTION_AIMS_PER_ITEM: Final = 100
MAX_REASON_BYTES: Final = 8 * 1024
MAX_SOURCE_POSITION: Final = 1_000_000
MAX_PORTFOLIO_REFS: Final = 100
MAX_PORTFOLIO_HISTORY: Final = 500
MAX_PORTFOLIO_REF_LENGTH: Final = 1_000
MAX_PORTFOLIO_HISTORY_LENGTH: Final = 2_000
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
    direction_aim_ids: tuple[str, ...] = ()
    unaligned_reason: str | None = None
    source_position: int | None = None
    source_task_updated_at: str | None = None
    source_thread_updated_at: str | None = None


@dataclass(frozen=True)
class Portfolio:
    summary: str
    items: tuple[PortfolioItem, ...]
    updated_at: str
    revision: str
    direction_revision: str | None = None
    format_version: int = 1
    source_direction_updated_at: str | None = None
    refs: tuple[str, ...] = ()
    observed_at: str | None = None
    recorded_at: str | None = None
    review_after: str | None = None
    history: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewCoverageInspection:
    task_id: str
    current: bool
    reason: str | None
    reference: str


@dataclass(frozen=True)
class ReviewInspection:
    state: str
    issue: str | None
    session_task_id: str | None
    review_thread_revision: str | None
    current_subject_task_id: str | None
    current_subject_task_ids: tuple[str, ...]
    current_coverage_task_ids: tuple[str, ...]
    revisit_task_ids: tuple[str, ...]
    uncovered_task_ids: tuple[str, ...]
    new_open_task_ids: tuple[str, ...]
    open_task_ids: tuple[str, ...]
    coverages: tuple[ReviewCoverageInspection, ...]
    options: tuple[ReviewOption, ...]


@dataclass(frozen=True)
class PortfolioInspection:
    portfolio: Portfolio
    direction_revision: str | None
    direction_changed: bool
    stale_portfolio_task_ids: tuple[str, ...]
    stale_portfolio_thread_ids: tuple[str, ...]
    review: ReviewInspection


def new_portfolio(
    *,
    summary: str,
    items: tuple[PortfolioItem, ...],
    direction_revision: str | None = None,
    observed_at: datetime | None = None,
    source_direction_updated_at: str | None = None,
    refs: tuple[str, ...] = (),
    source_observed_at: str | None = None,
    recorded_at: str | None = None,
    review_after: str | None = None,
    history: tuple[str, ...] = (),
) -> Portfolio:
    rich = any(
        (
            source_direction_updated_at is not None,
            refs,
            source_observed_at is not None,
            recorded_at is not None,
            review_after is not None,
            history,
            any(_item_has_source_fields(item) for item in items),
        )
    )
    format_version = (
        PORTFOLIO_RICH_FORMAT_VERSION if rich else (2 if direction_revision is not None else 1)
    )
    candidate = Portfolio(
        summary=body_text(summary, "Portfolio summary", required=True),
        items=portfolio_items(
            items,
            require_alignment=format_version >= 2,
            require_source_anchors=format_version == PORTFOLIO_RICH_FORMAT_VERSION,
        ),
        updated_at=format_time(observed_at or datetime.now(UTC)),
        revision="",
        direction_revision=(
            _revision(direction_revision, "Direction revision")
            if direction_revision is not None
            else None
        ),
        format_version=format_version,
        source_direction_updated_at=source_direction_updated_at,
        refs=refs,
        observed_at=source_observed_at,
        recorded_at=recorded_at,
        review_after=review_after,
        history=history,
    )
    return parse_portfolio(render_portfolio(candidate))


def render_portfolio(portfolio: Portfolio) -> str:
    version = _format_version(portfolio.format_version)
    if (portfolio.direction_revision is None) != (version == 1):
        raise ValidationError("Portfolio versions 2 and 3 require one exact Direction revision")
    _validate_version_content(portfolio)
    items = portfolio_items(
        portfolio.items,
        require_alignment=version >= 2,
        require_source_anchors=version == PORTFOLIO_RICH_FORMAT_VERSION,
    )
    updated_at = _stored_time(portfolio.updated_at)
    metadata = {
        "items": [
            {
                "task_id": item.task_id,
                "task_revision": item.task_revision,
                "stance": item.stance,
                "reason": item.reason,
                "work_thread_id": item.work_thread_id,
                "work_thread_revision": item.work_thread_revision,
                **(
                    {
                        "direction_aim_ids": list(item.direction_aim_ids),
                        "unaligned_reason": item.unaligned_reason,
                    }
                    if version >= 2
                    else {}
                ),
                **(
                    {
                        "source_position": item.source_position,
                        "source_task_updated_at": item.source_task_updated_at,
                        "source_thread_updated_at": item.source_thread_updated_at,
                    }
                    if version == PORTFOLIO_RICH_FORMAT_VERSION
                    else {}
                ),
            }
            for item in items
        ],
        "kind": "portfolio",
        "updated_at": updated_at,
        "version": version,
    }
    if version >= 2:
        metadata["direction_revision"] = _revision(
            portfolio.direction_revision, "Direction revision"
        )
    if version == PORTFOLIO_RICH_FORMAT_VERSION:
        metadata.update(
            {
                "history": list(_history(portfolio.history)),
                "observed_at": _source_time(portfolio.observed_at, "Portfolio observed_at"),
                "recorded_at": _source_time(portfolio.recorded_at, "Portfolio recorded_at"),
                "refs": list(_refs(portfolio.refs)),
                "review_after": _source_time(portfolio.review_after, "Portfolio review_after"),
                "source_direction_updated_at": _source_time(
                    portfolio.source_direction_updated_at,
                    "Portfolio source_direction_updated_at",
                ),
            }
        )
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
    version = _format_version(metadata.get("version"))
    expected = {"items", "kind", "updated_at", "version"}
    if version >= 2:
        expected.add("direction_revision")
    if version == PORTFOLIO_RICH_FORMAT_VERSION:
        expected |= {
            "history",
            "observed_at",
            "recorded_at",
            "refs",
            "review_after",
            "source_direction_updated_at",
        }
    if set(metadata) != expected:
        raise ValidationError("Portfolio metadata has an unsupported shape")
    if lines[2:5] != ["# Portfolio", "", "## Working summary"]:
        raise ValidationError("Portfolio sections must be exactly the working summary")
    raw_items = metadata.get("items")
    if not isinstance(raw_items, list):
        raise ValidationError("Portfolio items must be a list")
    items = portfolio_items(
        tuple(_parse_item(value, format_version=version) for value in raw_items),
        require_alignment=version >= 2,
        require_source_anchors=version == PORTFOLIO_RICH_FORMAT_VERSION,
    )
    summary = body_text("\n".join(lines[5:]).strip(), "Portfolio summary", required=True)
    portfolio = Portfolio(
        summary=summary,
        items=items,
        updated_at=_stored_time(metadata.get("updated_at")),
        revision=sha256_bytes(encoded),
        direction_revision=(
            _revision(metadata.get("direction_revision"), "Direction revision")
            if version >= 2
            else None
        ),
        format_version=version,
        source_direction_updated_at=(
            _source_time(
                metadata.get("source_direction_updated_at"),
                "Portfolio source_direction_updated_at",
            )
            if version == PORTFOLIO_RICH_FORMAT_VERSION
            else None
        ),
        refs=(
            _refs(_metadata_string_list(metadata, "refs"))
            if version == PORTFOLIO_RICH_FORMAT_VERSION
            else ()
        ),
        observed_at=(
            _source_time(metadata.get("observed_at"), "Portfolio observed_at")
            if version == PORTFOLIO_RICH_FORMAT_VERSION
            else None
        ),
        recorded_at=(
            _source_time(metadata.get("recorded_at"), "Portfolio recorded_at")
            if version == PORTFOLIO_RICH_FORMAT_VERSION
            else None
        ),
        review_after=(
            _source_time(metadata.get("review_after"), "Portfolio review_after")
            if version == PORTFOLIO_RICH_FORMAT_VERSION
            else None
        ),
        history=(
            _history(_metadata_string_list(metadata, "history"))
            if version == PORTFOLIO_RICH_FORMAT_VERSION
            else ()
        ),
    )
    _validate_version_content(portfolio)
    return portfolio


def portfolio_item(
    *,
    task_id_value: object,
    task_revision: object,
    stance: object,
    reason: object,
    work_thread_id: object = None,
    work_thread_revision: object = None,
    direction_aim_ids: object = (),
    unaligned_reason: object = None,
    source_position: object = None,
    source_task_updated_at: object = None,
    source_thread_updated_at: object = None,
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
    if isinstance(direction_aim_ids, str) or not isinstance(direction_aim_ids, (tuple, list)):
        raise ValidationError("Portfolio Direction aim IDs must be an array")
    if any(not isinstance(value, str) for value in direction_aim_ids):
        raise ValidationError("Portfolio Direction aim IDs must be strings")
    if unaligned_reason is not None and not isinstance(unaligned_reason, str):
        raise ValidationError("Portfolio unaligned reason must be a string or null")
    clean_source_position = _optional_source_position(source_position)
    clean_source_task_updated_at = _optional_source_time(
        source_task_updated_at, "Portfolio source_task_updated_at"
    )
    clean_source_thread_updated_at = _optional_source_time(
        source_thread_updated_at, "Portfolio source_thread_updated_at"
    )
    clean_thread = _optional_thread_id(work_thread_id)
    clean_thread_revision = _optional_revision(work_thread_revision)
    if (clean_thread is None) != (clean_thread_revision is None):
        raise ValidationError("Portfolio thread ID and revision must be authored together")
    clean_aim_ids = _aim_ids(tuple(direction_aim_ids))
    clean_unaligned = _reason(unaligned_reason) if isinstance(unaligned_reason, str) else None
    if clean_aim_ids and clean_unaligned is not None:
        raise ValidationError(
            "Portfolio item must name Direction aims or an unaligned reason, not both"
        )
    return PortfolioItem(
        task_id=task_id(task_id_value),
        task_revision=_revision(task_revision, "task revision"),
        stance=_stance(stance),
        reason=_reason(reason),
        work_thread_id=clean_thread,
        work_thread_revision=clean_thread_revision,
        direction_aim_ids=clean_aim_ids,
        unaligned_reason=clean_unaligned,
        source_position=clean_source_position,
        source_task_updated_at=clean_source_task_updated_at,
        source_thread_updated_at=clean_source_thread_updated_at,
    )


def portfolio_items(
    values: tuple[PortfolioItem, ...],
    *,
    require_alignment: bool = False,
    require_source_anchors: bool = False,
) -> tuple[PortfolioItem, ...]:
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
            direction_aim_ids=item.direction_aim_ids,
            unaligned_reason=item.unaligned_reason,
            source_position=item.source_position,
            source_task_updated_at=item.source_task_updated_at,
            source_thread_updated_at=item.source_thread_updated_at,
        )
        for item in values
    )
    identifiers = [item.task_id for item in clean]
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError("Portfolio may contain each task exactly once")
    if require_alignment:
        missing = next(
            (
                item.task_id
                for item in clean
                if not item.direction_aim_ids and item.unaligned_reason is None
            ),
            None,
        )
        if missing is not None:
            raise ValidationError(
                "Portfolio version 2 requires Direction aim IDs or an explicit "
                f"unaligned reason for {missing}"
            )
    if require_source_anchors:
        missing_source = next(
            (
                item.task_id
                for item in clean
                if item.source_position is None or item.source_task_updated_at is None
            ),
            None,
        )
        if missing_source is not None:
            raise ValidationError(
                "Portfolio version 3 requires source position and task timestamp for "
                f"{missing_source}"
            )
        positions = [item.source_position for item in clean]
        if len(set(positions)) != len(positions):
            raise ValidationError("Portfolio source positions must be unique")
        mismatched_thread = next(
            (
                item.task_id
                for item in clean
                if (item.work_thread_id is None) != (item.source_thread_updated_at is None)
            ),
            None,
        )
        if mismatched_thread is not None:
            raise ValidationError(
                "Portfolio version 3 source thread timestamp must match its thread anchor for "
                f"{mismatched_thread}"
            )
    return clean


def portfolio_dict(portfolio: Portfolio) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in portfolio.items:
        value = asdict(item)
        if portfolio.format_version != PORTFOLIO_RICH_FORMAT_VERSION:
            value.pop("source_position")
            value.pop("source_task_updated_at")
            value.pop("source_thread_updated_at")
        items.append(value)
    result: dict[str, Any] = {
        "items": items,
        "revision": portfolio.revision,
        "summary": portfolio.summary,
        "updated_at": portfolio.updated_at,
        "direction_revision": portfolio.direction_revision,
        "format_version": portfolio.format_version,
    }
    if portfolio.format_version == PORTFOLIO_RICH_FORMAT_VERSION:
        result.update(
            {
                "history": list(portfolio.history),
                "observed_at": portfolio.observed_at,
                "recorded_at": portfolio.recorded_at,
                "refs": list(portfolio.refs),
                "review_after": portfolio.review_after,
                "source_direction_updated_at": portfolio.source_direction_updated_at,
            }
        )
    return result


def inspect_portfolio_state(
    *,
    tasks: tuple[Task, ...] | list[Task],
    threads: tuple[WorkThread, ...] | list[WorkThread],
    portfolio: Portfolio,
    direction: Direction | None = None,
) -> PortfolioInspection:
    """Report exact anchor drift and bounded review traversal without semantic judgment."""

    task_by_id = {value.identifier: value for value in tasks}
    thread_by_id = {value.identifier: value for value in threads}
    direction_changed = (
        direction is not None and portfolio.direction_revision != direction.revision
    ) or (direction is None and portfolio.direction_revision is not None)
    stale_tasks: list[str] = []
    stale_threads: list[str] = []
    owners = _task_owners(thread_by_id)
    for item in portfolio.items:
        task = task_by_id.get(item.task_id)
        if task is None or task.revision != item.task_revision:
            stale_tasks.append(item.task_id)
        current_owners = owners.get(item.task_id, ())
        owner_is_current = len(current_owners) == 1 and (
            item.work_thread_id == current_owners[0].identifier
            and item.work_thread_revision == current_owners[0].revision
        )
        unthreaded_is_current = not current_owners and item.work_thread_id is None
        if not owner_is_current and not unthreaded_is_current:
            if item.work_thread_id is not None:
                stale_threads.append(item.work_thread_id)
            stale_threads.extend(owner.identifier for owner in current_owners)

    review = _inspect_review(
        task_by_id=task_by_id,
        thread_by_id=thread_by_id,
        portfolio=portfolio,
    )
    return PortfolioInspection(
        portfolio=portfolio,
        direction_revision=direction.revision if direction is not None else None,
        direction_changed=direction_changed,
        stale_portfolio_task_ids=tuple(dict.fromkeys(stale_tasks)),
        stale_portfolio_thread_ids=tuple(dict.fromkeys(stale_threads)),
        review=review,
    )


def portfolio_inspection_dict(value: PortfolioInspection) -> dict[str, Any]:
    return {
        "direction_changed": value.direction_changed,
        "direction_revision": value.direction_revision,
        "portfolio": portfolio_dict(value.portfolio),
        "review": asdict(value.review),
        "stale_portfolio_task_ids": list(value.stale_portfolio_task_ids),
        "stale_portfolio_thread_ids": list(value.stale_portfolio_thread_ids),
    }


def _inspect_review(
    *,
    task_by_id: dict[str, Task],
    thread_by_id: dict[str, WorkThread],
    portfolio: Portfolio,
) -> ReviewInspection:
    review_thread = thread_by_id.get(REVIEW_WORK_THREAD_ID)
    issue: str | None = None
    session: Task | None = None
    candidates: list[Task] = []
    legacy_candidates = [
        task
        for task in task_by_id.values()
        if task.status not in TERMINAL_TASK_STATUSES
        and parse_review_references(task.refs).has_all_open_scope
        and has_review_session_signal(task)
        and (review_thread is None or task.identifier not in review_thread.task_ids)
    ]
    if review_thread is not None and review_thread.status in TERMINAL_THREAD_STATUSES:
        issue = "review WorkThread is closed"
    elif review_thread is not None:
        candidates = [
            task
            for identifier in review_thread.task_ids
            if (task := task_by_id.get(identifier)) is not None
            and task.status not in TERMINAL_TASK_STATUSES
            and parse_review_references(task.refs).has_all_open_scope
        ]
        if not candidates and review_thread.focus_task_id is None:
            session = None
        elif len(candidates) != 1:
            issue = "review WorkThread must own exactly one nonterminal review session"
        else:
            session = candidates[0]
            if review_thread.focus_task_id != session.identifier:
                issue = "review WorkThread focus must be the exact nonterminal review session"
        if legacy_candidates:
            issue = (
                "a legacy review session exists outside the canonical review WorkThread; "
                "migrate that exact session before starting or resuming"
                if len(legacy_candidates) == 1
                else "more than one legacy review session requires explicit repair"
            )
            if session is None and len(legacy_candidates) == 1:
                session = legacy_candidates[0]
    elif legacy_candidates:
        issue = (
            "a legacy review session requires CAS migration into the canonical review WorkThread"
            if len(legacy_candidates) == 1
            else "more than one legacy review session requires explicit repair"
        )
        if len(legacy_candidates) == 1:
            session = legacy_candidates[0]

    # Every structural review-session task is transport state, never a life
    # outcome. Exclude even degraded or legacy sessions so repair state cannot
    # leak onto the decision board as another thing to review.
    review_thread_task_ids = set(review_thread.task_ids) if review_thread is not None else set()
    review_session_ids = {
        task.identifier
        for task in task_by_id.values()
        if parse_review_references(task.refs).has_all_open_scope
        and (
            has_review_session_signal(task)
            or task.identifier in review_thread_task_ids
            or (session is not None and task.identifier == session.identifier)
        )
    }
    open_ids = {
        task.identifier
        for task in task_by_id.values()
        if task.status not in TERMINAL_TASK_STATUSES
        and task.identifier not in review_session_ids
        and not is_resident_pulse_task(task)
    }
    portfolio_order = [item.task_id for item in portfolio.items if item.task_id in open_ids]
    ordered_open = tuple((*portfolio_order, *sorted(open_ids - set(portfolio_order))))
    portfolio_ids = {item.task_id for item in portfolio.items}
    new_open = tuple(identifier for identifier in ordered_open if identifier not in portfolio_ids)
    if len(ordered_open) > MAX_GUIDED_REVIEW_OUTCOMES and issue is None:
        issue = (
            f"guided review supports at most {MAX_GUIDED_REVIEW_OUTCOMES} open outcomes in "
            "one session; use a bounded checkpoint before continuing"
        )

    if session is None or issue is not None:
        if issue is not None:
            state = (
                "unavailable"
                if review_thread is not None and review_thread.status in TERMINAL_THREAD_STATUSES
                else "conflict"
            )
        else:
            state = "finished" if not ordered_open else "available"
        return ReviewInspection(
            state=state,
            issue=issue,
            session_task_id=session.identifier if session is not None else None,
            review_thread_revision=(review_thread.revision if review_thread is not None else None),
            current_subject_task_id=None,
            current_subject_task_ids=(),
            current_coverage_task_ids=(),
            revisit_task_ids=(),
            uncovered_task_ids=ordered_open,
            new_open_task_ids=new_open,
            open_task_ids=ordered_open,
            coverages=(),
            options=(),
        )

    assert review_thread is not None
    parsed = parse_review_references(session.refs)
    if parsed.issues:
        issue = parsed.issues[0]
    if not parsed.has_all_open_scope:
        issue = "review session must carry review-scope:all-open"
    if len(parsed.coverages) > MAX_GUIDED_REVIEW_OUTCOMES and issue is None:
        issue = (
            f"guided review retains at most {MAX_GUIDED_REVIEW_OUTCOMES} exact coverage "
            "anchors in one session; prune older coverage refs through a fresh Task CAS before "
            "continuing"
        )

    owners = _task_owners(thread_by_id)
    coverage_inspections: list[ReviewCoverageInspection] = []
    current_coverage: set[str] = set()
    revisit: set[str] = set()
    for coverage in parsed.coverages:
        reason = _coverage_staleness(
            coverage=coverage,
            task_by_id=task_by_id,
            owners=owners,
        )
        current = reason is None and coverage.task_id in open_ids
        if current:
            current_coverage.add(coverage.task_id)
        elif coverage.task_id in open_ids:
            revisit.add(coverage.task_id)
        coverage_inspections.append(
            ReviewCoverageInspection(
                task_id=coverage.task_id,
                current=current,
                reason=reason or (None if current else "task is not currently open"),
                reference=coverage.reference,
            )
        )

    subject_ids = parsed.subject_task_ids
    for subject_id in subject_ids:
        task = task_by_id.get(subject_id)
        subject_current = bool(
            subject_id in open_ids and subject_id not in current_coverage and task is not None
        )
        if not subject_current and issue is None:
            issue = "review subject is absent, closed, or already covered on current truth"
    current_subject_id = subject_ids[0] if len(subject_ids) == 1 else None

    return ReviewInspection(
        state=("paused" if parsed.paused else "active") if issue is None else "conflict",
        issue=issue,
        session_task_id=session.identifier,
        review_thread_revision=review_thread.revision,
        current_subject_task_id=current_subject_id if issue is None else None,
        current_subject_task_ids=subject_ids if issue is None else (),
        current_coverage_task_ids=tuple(
            identifier for identifier in ordered_open if identifier in current_coverage
        ),
        revisit_task_ids=tuple(identifier for identifier in ordered_open if identifier in revisit),
        uncovered_task_ids=tuple(
            identifier for identifier in ordered_open if identifier not in current_coverage
        ),
        new_open_task_ids=new_open,
        open_task_ids=ordered_open,
        coverages=tuple(coverage_inspections),
        options=parsed.options,
    )


def _task_owners(threads: dict[str, WorkThread]) -> dict[str, tuple[WorkThread, ...]]:
    owners: dict[str, list[WorkThread]] = {}
    for thread in threads.values():
        if thread.identifier == REVIEW_WORK_THREAD_ID or thread.status in TERMINAL_THREAD_STATUSES:
            continue
        for identifier in thread.task_ids:
            owners.setdefault(identifier, []).append(thread)
    return {
        identifier: tuple(sorted(values, key=lambda value: value.identifier))
        for identifier, values in owners.items()
    }


def _coverage_staleness(
    *,
    coverage: Any,
    task_by_id: dict[str, Task],
    owners: dict[str, tuple[WorkThread, ...]],
) -> str | None:
    task = task_by_id.get(coverage.task_id)
    if coverage.task_revision is None:
        return "legacy coverage has no exact task revision"
    if task is None:
        return "covered task is missing"
    if task.revision != coverage.task_revision:
        return "covered task changed after it was checked"
    current_owners = owners.get(coverage.task_id, ())
    if len(current_owners) > 1:
        return "covered task has multiple nonterminal WorkThread owners"
    owner = current_owners[0] if current_owners else None
    if owner is None and coverage.work_thread_id is not None:
        return "covered task is no longer owned by the recorded WorkThread"
    if owner is not None and coverage.work_thread_id is None:
        return "covered task gained a WorkThread after it was checked"
    if owner is not None and (
        coverage.work_thread_id != owner.identifier
        or coverage.work_thread_revision != owner.revision
    ):
        return "covered task's WorkThread changed after it was checked"
    return None


def _parse_item(value: object, *, format_version: int) -> PortfolioItem:
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
    if format_version >= 2:
        allowed |= {"direction_aim_ids", "unaligned_reason"}
    if format_version == PORTFOLIO_RICH_FORMAT_VERSION:
        allowed |= {
            "source_position",
            "source_task_updated_at",
            "source_thread_updated_at",
        }
    if set(value) - allowed:
        raise ValidationError("Portfolio item contains unsupported fields")
    if format_version == PORTFOLIO_RICH_FORMAT_VERSION and set(value) != allowed:
        raise ValidationError("Portfolio version 3 item has an unsupported shape")
    return portfolio_item(
        task_id_value=_string(value, "task_id"),
        task_revision=_string(value, "task_revision"),
        stance=_string(value, "stance"),
        reason=_string(value, "reason"),
        work_thread_id=_optional_string(value, "work_thread_id"),
        work_thread_revision=_optional_string(value, "work_thread_revision"),
        direction_aim_ids=(_string_list(value, "direction_aim_ids") if format_version >= 2 else ()),
        unaligned_reason=(
            _optional_string(value, "unaligned_reason") if format_version >= 2 else None
        ),
        source_position=(
            _integer(value, "source_position")
            if format_version == PORTFOLIO_RICH_FORMAT_VERSION
            else None
        ),
        source_task_updated_at=(
            _string(value, "source_task_updated_at")
            if format_version == PORTFOLIO_RICH_FORMAT_VERSION
            else None
        ),
        source_thread_updated_at=(
            _optional_string(value, "source_thread_updated_at")
            if format_version == PORTFOLIO_RICH_FORMAT_VERSION
            else None
        ),
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


def _integer(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValidationError(f"Portfolio item field {key} must be an integer")
    return item


def _string_list(value: dict[str, object], key: str) -> tuple[str, ...]:
    item = value.get(key, [])
    if not isinstance(item, list) or any(not isinstance(entry, str) for entry in item):
        raise ValidationError(f"Portfolio item field {key} must be a string array")
    return tuple(item)


def _stance(value: str) -> PortfolioStance:
    if value not in PORTFOLIO_STANCES:
        raise ValidationError(f"invalid Portfolio stance: {value}")
    return value  # type: ignore[return-value]


def _reason(value: str) -> str:
    clean = body_text(value, "Portfolio reason", required=True)
    if len(clean.encode("utf-8")) > MAX_REASON_BYTES:
        raise ValidationError("Portfolio reason is too large")
    return clean


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a SHA-256 revision")
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


def _aim_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > MAX_DIRECTION_AIMS_PER_ITEM:
        raise ValidationError("Portfolio item names too many Direction aims")
    clean: list[str] = []
    for value in values:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", value) is None:
            raise ValidationError("Portfolio Direction aim ID must be a stable lowercase slug")
        if value in clean:
            raise ValidationError("Portfolio Direction aim IDs must be unique")
        clean.append(value)
    return tuple(clean)


def _stored_time(value: object) -> str:
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        raise ValidationError("Portfolio updated_at must be an ISO-8601 UTC timestamp")
    parse_time(value)
    return value


def _format_version(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in PORTFOLIO_FORMAT_VERSIONS
    ):
        raise ValidationError(f"unsupported Portfolio version: {value}")
    return value


def _validate_version_content(portfolio: Portfolio) -> None:
    rich_items = any(_item_has_source_fields(item) for item in portfolio.items)
    rich_top = any(
        (
            portfolio.source_direction_updated_at is not None,
            portfolio.refs,
            portfolio.observed_at is not None,
            portfolio.recorded_at is not None,
            portfolio.review_after is not None,
            portfolio.history,
        )
    )
    if portfolio.format_version in {1, 2}:
        if rich_items or rich_top:
            raise ValidationError(
                f"Portfolio version {portfolio.format_version} cannot contain version 3 "
                "continuity fields"
            )
        return
    if not rich_items and not rich_top:
        raise ValidationError("Portfolio version 3 requires continuity fields")
    source_direction = parse_time(
        _source_time(
            portfolio.source_direction_updated_at,
            "Portfolio source_direction_updated_at",
        )
    )
    observed = parse_time(_source_time(portfolio.observed_at, "Portfolio observed_at"))
    recorded = parse_time(_source_time(portfolio.recorded_at, "Portfolio recorded_at"))
    updated = parse_time(_stored_time(portfolio.updated_at))
    review_after = parse_time(_source_time(portfolio.review_after, "Portfolio review_after"))
    if source_direction > updated or observed > updated or recorded > updated:
        raise ValidationError("Portfolio version 3 has an invalid continuity timeline")
    if review_after <= updated:
        raise ValidationError("Portfolio review_after must be later than updated_at")
    _refs(portfolio.refs)
    _history(portfolio.history)
    items = portfolio_items(
        portfolio.items,
        require_alignment=True,
        require_source_anchors=True,
    )
    positions: list[int] = []
    for item in items:
        if item.source_position is None:  # Defensive after strict item validation above.
            raise ValidationError("Portfolio version 3 item has no source position")
        positions.append(item.source_position)
        source_task = parse_time(
            _source_time(
                item.source_task_updated_at,
                "Portfolio source_task_updated_at",
            )
        )
        if source_task > updated:
            raise ValidationError("Portfolio source task timestamp is later than updated_at")
        if item.source_thread_updated_at is not None:
            source_thread = parse_time(
                _source_time(
                    item.source_thread_updated_at,
                    "Portfolio source_thread_updated_at",
                )
            )
            if source_thread > updated:
                raise ValidationError("Portfolio source thread timestamp is later than updated_at")
    if positions != sorted(positions):
        raise ValidationError("Portfolio version 3 items must follow source position order")


def _item_has_source_fields(item: PortfolioItem) -> bool:
    return any(
        (
            item.source_position is not None,
            item.source_task_updated_at is not None,
            item.source_thread_updated_at is not None,
        )
    )


def _optional_source_position(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_SOURCE_POSITION
    ):
        raise ValidationError(
            f"Portfolio source_position must be an integer from 1 to {MAX_SOURCE_POSITION}"
        )
    return value


def _optional_source_time(value: object, label: str) -> str | None:
    return None if value is None else _source_time(value, label)


def _source_time(value: object, label: str) -> str:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise ValidationError(f"{label} must be an ISO-8601 UTC timestamp")
    parse_time(value)
    return value


def _refs(values: object) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise ValidationError("Portfolio refs must be an array")
    if len(values) > MAX_PORTFOLIO_REFS:
        raise ValidationError("Portfolio contains too many refs")
    clean = tuple(
        _single_line(value, MAX_PORTFOLIO_REF_LENGTH, "Portfolio ref") for value in values
    )
    if len(set(clean)) != len(clean):
        raise ValidationError("Portfolio refs must be unique")
    return clean


def _history(values: object) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise ValidationError("Portfolio history must be an array")
    if not values or len(values) > MAX_PORTFOLIO_HISTORY:
        raise ValidationError("Portfolio history must contain 1 to 500 entries")
    return tuple(
        _single_line(value, MAX_PORTFOLIO_HISTORY_LENGTH, "Portfolio history entry")
        for value in values
    )


def _single_line(value: object, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    clean = " ".join(value.split())
    if not clean or len(clean) > maximum or "\x00" in clean:
        raise ValidationError(f"{label} is invalid")
    return clean


def _metadata_string_list(metadata: dict[str, object], key: str) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"Portfolio {key} must be a string array")
    return tuple(value)
