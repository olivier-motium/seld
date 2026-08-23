"""Deterministic task-ledger operations used by the dispatch controller."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.records import (
    DEFAULT_CLAIM_WINDOW,
    SLA_CLOCK_HEALTH,
    TERMINAL_TASK_STATUSES,
    Task,
    claim_by_eligible,
    dispatch_id_value,
    dispatch_revision_value,
    format_time,
    parse_time,
)
from continuity_kernel.vault import Vault

DISPATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ACTIVE_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class TaskAttentionFinding:
    """One read-only deadline finding projected from one task snapshot."""

    task_id: str
    revision: str
    field: str
    band: str
    deadline: str
    evidence_at: str


def claim_task(
    project_root: Path,
    identifier: str,
    *,
    expected_revision: str,
    dispatch_id: str,
    observed_at: datetime | None = None,
) -> Task:
    """Claim one ready task with agent_run=yes and next_actor=agent by exact revision and dispatch ID."""

    clean_dispatch = _dispatch_id(dispatch_id)
    clean_revision = _dispatch_revision(expected_revision)
    vault = Vault(project_root)
    current = vault.get_task(identifier)
    if current.dispatch_id == clean_dispatch and current.dispatch_revision == clean_revision:
        return current
    if current.revision != clean_revision:
        raise ConflictError("task changed since the dispatch revision was read")
    if current.dispatch_id is not None or current.dispatch_revision is not None:
        raise ValidationError("task is already claimed by another dispatch")
    if not _eligible_for_claim(current):
        raise ValidationError("task is not ready and eligible for claim")

    claimed = vault.update_task(
        current.identifier,
        dispatch_id=clean_dispatch,
        dispatch_revision=clean_revision,
        expected_revision=current.revision,
        observed_at=observed_at,
    )
    read_back = vault.get_task(current.identifier)
    if read_back.dispatch_id != clean_dispatch or read_back.dispatch_revision != clean_revision:
        raise ValidationError("dispatch claim readback failed")
    if claimed != read_back:
        raise ValidationError("dispatch claim readback returned a different task")
    return read_back


def bind_task_hand(
    project_root: Path,
    identifier: str,
    *,
    expected_revision: str,
    dispatch_id: str,
    active_thread_id: str,
    observed_at: datetime | None = None,
) -> Task:
    """Bind the claimed task to one exact active execution hand."""

    clean_dispatch = _dispatch_id(dispatch_id)
    clean_revision = _dispatch_revision(expected_revision)
    clean_thread = _active_thread(active_thread_id)
    vault = Vault(project_root)
    current = vault.get_task(identifier)
    _require_claim(current, clean_dispatch, clean_revision)
    if current.status == "doing" and current.active_thread_id == clean_thread:
        return current
    if current.active_thread_id is not None and current.active_thread_id != clean_thread:
        raise ValidationError("task is already bound to another active hand")

    bound = vault.update_task(
        current.identifier,
        status="doing",
        active_thread_id=clean_thread,
        rank=current.rank,
        expected_revision=current.revision,
        observed_at=observed_at,
    )
    read_back = vault.get_task(current.identifier)
    if read_back.active_thread_id != clean_thread or read_back.status != "doing":
        raise ValidationError("active hand binding readback failed")
    if bound != read_back:
        raise ValidationError("active hand binding readback returned a different task")
    return read_back


def write_task_blocker(
    project_root: Path,
    identifier: str,
    *,
    expected_revision: str,
    dispatch_id: str,
    owner: str,
    condition: str,
    observed_at: datetime | None = None,
) -> Task:
    """Write one recoverable named blocker for the claimed dispatch."""

    clean_dispatch = _dispatch_id(dispatch_id)
    clean_revision = _dispatch_revision(expected_revision)
    clean_owner = _blocker_part(owner, "blocker owner", 120)
    clean_condition = _blocker_part(condition, "blocker condition", 500)
    vault = Vault(project_root)
    current = vault.get_task(identifier)
    _require_claim(current, clean_dispatch, clean_revision)
    if (
        current.status == "waiting"
        and current.waiting_on == clean_condition
        and current.blocker_owner == clean_owner
        and current.blocker_condition == clean_condition
        and current.active_thread_id is None
        and current.progress_check_by is None
    ):
        return current
    if current.waiting_on is not None and current.waiting_on != clean_condition:
        raise ValidationError("task already has a different named blocker")
    if current.blocker_owner is not None and current.blocker_owner != clean_owner:
        raise ValidationError("task already has a different blocker owner")
    if current.blocker_condition is not None and current.blocker_condition != clean_condition:
        raise ValidationError("task already has a different named blocker")

    blocked = vault.update_task(
        current.identifier,
        status="waiting",
        rank=current.rank,
        waiting_on=clean_condition,
        blocker_owner=clean_owner,
        blocker_condition=clean_condition,
        clear_active_thread_id=current.active_thread_id is not None,
        clear_progress_check_by=current.progress_check_by is not None,
        expected_revision=current.revision,
        observed_at=observed_at,
    )
    read_back = vault.get_task(current.identifier)
    if (
        read_back.status != "waiting"
        or read_back.waiting_on != clean_condition
        or read_back.blocker_owner != clean_owner
        or read_back.blocker_condition != clean_condition
        or read_back.active_thread_id is not None
        or read_back.progress_check_by is not None
        or read_back.rank != current.rank
    ):
        raise ValidationError("named blocker readback failed")
    if blocked != read_back:
        raise ValidationError("named blocker readback returned a different task")
    return read_back


def clear_task_blocker(
    project_root: Path,
    identifier: str,
    *,
    expected_revision: str,
    dispatch_id: str,
    owner: str,
    condition: str,
    observed_at: datetime | None = None,
) -> Task:
    """Clear one exact blocker and return its task to the ready queue."""

    clean_dispatch = _dispatch_id(dispatch_id)
    clean_revision = _dispatch_revision(expected_revision)
    clean_owner = _blocker_part(owner, "blocker owner", 120)
    clean_condition = _blocker_part(condition, "blocker condition", 500)
    vault = Vault(project_root)
    current = vault.get_task(identifier)
    marker = _blocker_clear_marker(clean_dispatch, clean_revision, clean_owner, clean_condition)
    if _is_clear_replay(current, marker):
        return current
    _require_claim(current, clean_dispatch, clean_revision)
    if current.status != "waiting":
        raise ValidationError("task is not blocked")
    if (
        current.blocker_owner != clean_owner
        or current.blocker_condition != clean_condition
        or current.waiting_on != clean_condition
    ):
        raise ValidationError("task blocker does not match the expected owner and condition")

    claim_by = None if current.agent_run == "yes" else _refreshed_claim_by(current, observed_at)
    cleared = vault.update_task(
        current.identifier,
        status="ready",
        rank=current.rank,
        claim_by=claim_by,
        clear_claim_by=current.agent_run == "yes" and current.claim_by is not None,
        clear_waiting_on=True,
        clear_blocker_owner=True,
        clear_blocker_condition=True,
        clear_dispatch_id=True,
        clear_dispatch_revision=True,
        clear_active_thread_id=current.active_thread_id is not None,
        clear_progress_check_by=current.progress_check_by is not None,
        note=marker,
        expected_revision=current.revision,
        observed_at=observed_at,
    )
    read_back = vault.get_task(current.identifier)
    if (
        read_back.status != "ready"
        or read_back.waiting_on is not None
        or read_back.blocker_owner is not None
        or read_back.blocker_condition is not None
        or read_back.dispatch_id is not None
        or read_back.dispatch_revision is not None
        or read_back.active_thread_id is not None
        or read_back.progress_check_by is not None
        or read_back.rank != current.rank
        or read_back.claim_by != claim_by
        or not any(marker in entry for entry in read_back.history)
    ):
        raise ValidationError("blocker clear readback failed")
    if cleared != read_back:
        raise ValidationError("blocker clear readback returned a different task")
    return read_back


def evaluate_task_deadline(
    project_root: Path,
    identifier: str,
    *,
    now: datetime | None = None,
    clock_health: str = "healthy",
) -> tuple[TaskAttentionFinding, ...]:
    """Read the task again and project deadline attention without mutation."""

    return project_task_attention(
        Vault(project_root).get_task(identifier),
        now=now,
        clock_health=clock_health,
    )


def scan_task_attention(
    project_root: Path,
    *,
    now: datetime | None = None,
    clock_health: str = "healthy",
) -> tuple[TaskAttentionFinding, ...]:
    """Project deadline attention from the current task scan without mutation."""

    vault = Vault(project_root)
    return tuple(
        finding
        for task in vault.list_tasks()
        for finding in project_task_attention(task, now=now, clock_health=clock_health)
    )


def project_task_attention(
    task: Task,
    *,
    now: datetime | None = None,
    clock_health: str = "healthy",
) -> tuple[TaskAttentionFinding, ...]:
    """Project passed task deadlines as attention only; never mutate task truth."""

    current = format_time(now or datetime.now(UTC))
    health = _clock_health(clock_health)
    if task.status in TERMINAL_TASK_STATUSES or task.waiting_on is not None:
        return ()

    deadlines: list[tuple[str, str]] = []
    if (
        task.claim_by is not None
        and claim_by_eligible(task.status, task.target_seat, task.waiting_on)
        and task.dispatch_id is None
        and task.active_thread_id is None
    ):
        deadlines.append(("claim_by", task.claim_by))
    if task.progress_check_by is not None:
        deadlines.append(("progress_check_by", task.progress_check_by))

    findings: list[TaskAttentionFinding] = []
    for field, deadline in deadlines:
        if parse_time(current) < parse_time(deadline):
            continue
        findings.append(
            TaskAttentionFinding(
                task_id=task.identifier,
                revision=task.revision,
                field=field,
                band="UNKNOWN" if health == "unknown" else "OVERDUE",
                deadline=deadline,
                evidence_at=current,
            )
        )
    return tuple(findings)


def dispatch_eligible(project_root: Path) -> tuple[Task, ...]:
    """Return every ready, unblocked, unclaimed task with agent_run=yes and next_actor=agent in queue order."""

    vault = Vault(project_root)
    eligible = tuple(task for task in vault.list_tasks() if _eligible_for_claim(task))
    return tuple(
        sorted(
            eligible,
            key=lambda task: (
                task.rank is None,
                task.rank if task.rank is not None else 0,
                task.created_at,
                task.identifier,
            ),
        )
    )


def _eligible_for_claim(task: Task) -> bool:
    return (
        task.status == "ready"
        and task.waiting_on is None
        and task.next_actor == "agent"
        and task.agent_run == "yes"
        and task.blocker_owner is None
        and task.blocker_condition is None
        and task.dispatch_id is None
        and task.dispatch_revision is None
        and task.active_thread_id is None
    )


def _require_claim(task: Task, dispatch_id: str, revision: str) -> None:
    if task.dispatch_id != dispatch_id or task.dispatch_revision != revision:
        raise ValidationError("task is not claimed by this dispatch revision")


def _dispatch_id(value: str) -> str:
    clean = dispatch_id_value(value)
    if clean is None or DISPATCH_ID.fullmatch(clean) is None:
        raise ValidationError("dispatch ID is invalid")
    return clean


def _dispatch_revision(value: str) -> str:
    clean = dispatch_revision_value(value)
    if clean is None:
        raise ValidationError("dispatch revision must be a task SHA-256 revision")
    return clean


def _active_thread(value: str) -> str:
    clean = value.strip()
    if ACTIVE_THREAD_ID.fullmatch(clean) is None:
        raise ValidationError("active hand must be one bounded opaque identifier")
    return clean


def _blocker_part(value: str, label: str, maximum: int) -> str:
    clean = " ".join(value.split())
    if not clean or len(clean) > maximum or "\x00" in clean:
        raise ValidationError(f"{label} is invalid")
    return clean


def _blocker_clear_marker(dispatch_id: str, revision: str, owner: str, condition: str) -> str:
    payload = "\0".join((dispatch_id, revision, owner, condition)).encode()
    return "blocker-clear:" + hashlib.sha256(payload).hexdigest()[:32]


def _is_clear_replay(task: Task, marker: str) -> bool:
    return (
        task.status == "ready"
        and task.waiting_on is None
        and task.blocker_owner is None
        and task.blocker_condition is None
        and task.dispatch_id is None
        and task.dispatch_revision is None
        and any(marker in entry for entry in task.history)
    )


def _refreshed_claim_by(task: Task, observed_at: datetime | None) -> str:
    base = observed_at or datetime.now(UTC)
    base_time = parse_time(format_time(base))
    updated_at = parse_time(task.updated_at)
    if base_time <= updated_at:
        base_time = updated_at + timedelta(microseconds=1)
    return format_time(base_time + DEFAULT_CLAIM_WINDOW)


def _clock_health(value: str) -> str:
    clean = value.strip().casefold()
    if clean not in SLA_CLOCK_HEALTH:
        raise ValidationError(f"clock health must be one of: {', '.join(SLA_CLOCK_HEALTH)}")
    return clean
