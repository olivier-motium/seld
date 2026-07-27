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

PORTFOLIO_FORMAT_VERSIONS: Final = frozenset({1, 2})
ABSENT_PORTFOLIO_REVISION: Final = "absent"
MAX_PORTFOLIO_ITEMS: Final = 10_000
MAX_GUIDED_REVIEW_OUTCOMES: Final = 512
MAX_DIRECTION_AIMS_PER_ITEM: Final = 100
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
    direction_aim_ids: tuple[str, ...] = ()
    unaligned_reason: str | None = None


@dataclass(frozen=True)
class Portfolio:
    summary: str
    items: tuple[PortfolioItem, ...]
    updated_at: str
    revision: str
    direction_revision: str | None = None
    format_version: int = 1


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
) -> Portfolio:
    format_version = 2 if direction_revision is not None else 1
    candidate = Portfolio(
        summary=body_text(summary, "Portfolio summary", required=True),
        items=portfolio_items(items, require_alignment=format_version == 2),
        updated_at=format_time(observed_at or datetime.now(UTC)),
        revision="",
        direction_revision=(
            _revision(direction_revision, "Direction revision")
            if direction_revision is not None
            else None
        ),
        format_version=format_version,
    )
    return parse_portfolio(render_portfolio(candidate))


def render_portfolio(portfolio: Portfolio) -> str:
    if portfolio.format_version not in PORTFOLIO_FORMAT_VERSIONS:
        raise ValidationError(f"unsupported Portfolio version: {portfolio.format_version}")
    if (portfolio.direction_revision is None) != (portfolio.format_version == 1):
        raise ValidationError("Portfolio version 2 requires one exact Direction revision")
    items = portfolio_items(portfolio.items, require_alignment=portfolio.format_version == 2)
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
                    if portfolio.format_version == 2
                    else {}
                ),
            }
            for item in items
        ],
        "kind": "portfolio",
        "updated_at": updated_at,
        "version": portfolio.format_version,
    }
    if portfolio.format_version == 2:
        metadata["direction_revision"] = _revision(
            portfolio.direction_revision, "Direction revision"
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
    version = metadata.get("version")
    if version not in PORTFOLIO_FORMAT_VERSIONS:
        raise ValidationError(f"unsupported Portfolio version: {version}")
    if lines[2:5] != ["# Portfolio", "", "## Working summary"]:
        raise ValidationError("Portfolio sections must be exactly the working summary")
    raw_items = metadata.get("items")
    if not isinstance(raw_items, list):
        raise ValidationError("Portfolio items must be a list")
    items = portfolio_items(
        tuple(_parse_item(value, format_version=version) for value in raw_items),
        require_alignment=version == 2,
    )
    summary = body_text("\n".join(lines[5:]).strip(), "Portfolio summary", required=True)
    return Portfolio(
        summary=summary,
        items=items,
        updated_at=_stored_time(metadata.get("updated_at")),
        revision=sha256_bytes(encoded),
        direction_revision=(
            _revision(metadata.get("direction_revision"), "Direction revision")
            if version == 2
            else None
        ),
        format_version=version,
    )


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
    )


def portfolio_items(
    values: tuple[PortfolioItem, ...], *, require_alignment: bool = False
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
    return clean


def portfolio_dict(portfolio: Portfolio) -> dict[str, Any]:
    return {
        "items": [asdict(item) for item in portfolio.items],
        "revision": portfolio.revision,
        "summary": portfolio.summary,
        "updated_at": portfolio.updated_at,
        "direction_revision": portfolio.direction_revision,
        "format_version": portfolio.format_version,
    }


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
    if review_thread is not None and review_thread.status == "closed":
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
                if review_thread is not None and review_thread.status == "closed"
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
        if thread.identifier == REVIEW_WORK_THREAD_ID or thread.status == "closed":
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
    if format_version == 2:
        allowed |= {"direction_aim_ids", "unaligned_reason"}
    if set(value) - allowed:
        raise ValidationError("Portfolio item contains unsupported fields")
    return portfolio_item(
        task_id_value=_string(value, "task_id"),
        task_revision=_string(value, "task_revision"),
        stance=_string(value, "stance"),
        reason=_string(value, "reason"),
        work_thread_id=_optional_string(value, "work_thread_id"),
        work_thread_revision=_optional_string(value, "work_thread_revision"),
        direction_aim_ids=(_string_list(value, "direction_aim_ids") if format_version == 2 else ()),
        unaligned_reason=(
            _optional_string(value, "unaligned_reason") if format_version == 2 else None
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
