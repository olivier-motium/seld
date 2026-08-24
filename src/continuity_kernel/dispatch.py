"""Deterministic task-ledger operations used by the dispatch controller."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.records import (
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
ALLOCATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

ADMISSION_SCHEMA: Final = "seld.factory-allocation-admission.v1"
ADMISSION_ISSUER: Final = "ai-accounts-runtime"
ADMISSION_PAYLOAD_KEYS: Final = frozenset(
    {
        "allocation_id",
        "dispatch_id",
        "expected_revision",
        "issuer",
        "schema",
        "task_id",
        "vault_id",
    }
)
ADMISSION_TOP_KEYS: Final = frozenset({"payload", "signature"})
ADMISSION_SIGNATURE_HEX: Final = re.compile(r"^[0-9a-f]{64}$")
ADMISSION_KEY_MAX_BYTES: Final = 4096
if os.name == "posix":
    import pwd

    # Keep the module type-checkable on Windows, where typeshed intentionally
    # omits the POSIX-only attributes even though this branch cannot run.
    _TRUSTED_USER_HOME = Path(
        pwd.getpwuid(os.geteuid()).pw_dir  # type: ignore[attr-defined, unused-ignore]
    )
else:  # pragma: no cover - Factory serving is currently macOS; keep imports portable.
    _TRUSTED_USER_HOME = Path.home()
FACTORY_ADMISSION_KEY_FILE: Final = (
    _TRUSTED_USER_HOME / ".ai-cli-accounts" / "runtime-launch" / "factory-admission.key"
)


@dataclass(frozen=True)
class TaskAttentionFinding:
    """One read-only deadline finding projected from one task snapshot."""

    task_id: str
    revision: str
    field: str
    band: str
    deadline: str
    evidence_at: str


def _read_factory_admission_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
        raise ValidationError("factory admission key file must not be a symlink")
    # Windows refuses to open a directory before ``fstat`` can classify it.
    # Classify that case explicitly so every platform reports the same boundary.
    if os.name != "posix" and path.is_dir():
        raise ValidationError("factory admission key file is not a regular file")
    try:
        descriptor = os.open(path, flags)
    except OSError as err:
        raise ValidationError(f"failed to open factory admission key file: {err}") from err
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("factory admission key file is not a regular file")
        effective_uid = getattr(os, "geteuid", None)
        if effective_uid is not None and metadata.st_uid != effective_uid():
            raise ValidationError("factory admission key file is not owned by the current user")
        # POSIX mode bits express group/world access. Windows reports synthetic
        # mode bits here, so applying the POSIX mask would reject every key.
        if os.name == "posix" and metadata.st_mode & 0o077:
            raise ValidationError(
                "factory admission key file has insecure group or world permissions"
            )
        if metadata.st_size == 0:
            raise ValidationError("factory admission key file is empty")
        if metadata.st_size > ADMISSION_KEY_MAX_BYTES:
            raise ValidationError("factory admission key file exceeds maximum size of 4096 bytes")
        chunks: list[bytes] = []
        remaining = ADMISSION_KEY_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        key_bytes = b"".join(chunks)
        if not key_bytes:
            raise ValidationError("factory admission key file is empty")
        if len(key_bytes) > ADMISSION_KEY_MAX_BYTES:
            raise ValidationError("factory admission key file exceeds maximum size of 4096 bytes")
        return key_bytes
    finally:
        os.close(descriptor)


def _verify_factory_admission(
    project_root: Path,
    task_id: str,
    expected_revision: str,
    dispatch_id: str,
    admission: object,
) -> None:
    if not isinstance(admission, dict) or set(admission.keys()) != ADMISSION_TOP_KEYS:
        raise ValidationError(
            "factory allocation admission must be an object with exact payload and signature"
        )
    payload = admission["payload"]
    signature = admission["signature"]
    if not isinstance(payload, dict) or set(payload.keys()) != ADMISSION_PAYLOAD_KEYS:
        raise ValidationError("factory allocation admission payload has an unsupported shape")
    if not isinstance(signature, str) or ADMISSION_SIGNATURE_HEX.fullmatch(signature) is None:
        raise ValidationError(
            "factory allocation admission signature must be 64 lowercase hex characters"
        )

    if payload["schema"] != ADMISSION_SCHEMA:
        raise ValidationError("unsupported factory allocation admission schema")
    if payload["issuer"] != ADMISSION_ISSUER:
        raise ValidationError("unsupported factory allocation admission issuer")
    if (
        not isinstance(payload["allocation_id"], str)
        or ALLOCATION_ID.fullmatch(payload["allocation_id"]) is None
    ):
        raise ValidationError("invalid factory allocation ID")
    if payload["dispatch_id"] != dispatch_id:
        raise ValidationError("factory allocation admission dispatch ID does not match claim")
    if payload["task_id"] != task_id:
        raise ValidationError("factory allocation admission task ID does not match claim")
    if payload["expected_revision"] != expected_revision:
        raise ValidationError("factory allocation admission expected revision does not match claim")

    vault = Vault(project_root)
    canonical_vault_id = vault.identity()["vault_id"]
    if payload["vault_id"] != canonical_vault_id:
        raise ValidationError("factory allocation admission vault ID does not match current vault")

    key_bytes = _read_factory_admission_key(FACTORY_ADMISSION_KEY_FILE)

    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    computed_signature = hmac.new(key_bytes, canonical_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, computed_signature):
        raise ValidationError("factory allocation admission signature verification failed")


def claim_task(
    project_root: Path,
    identifier: str,
    *,
    expected_revision: str,
    dispatch_id: str,
    admission: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> Task:
    """Claim one exactly-revisioned task approved for agent execution."""

    clean_dispatch = _dispatch_id(dispatch_id)
    clean_revision = _dispatch_revision(expected_revision)
    vault = Vault(project_root)
    current = vault.get_task(identifier)
    if current.agent_run == "yes":
        _verify_factory_admission(
            project_root,
            current.identifier,
            clean_revision,
            clean_dispatch,
            admission,
        )
    if current.dispatch_id == clean_dispatch and current.dispatch_revision == clean_revision:
        return current
    if current.revision != clean_revision:
        raise ConflictError("task changed since the dispatch revision was read")
    if current.dispatch_id is not None or current.dispatch_revision is not None:
        raise ValidationError("task is already claimed by another dispatch")
    if not _eligible_for_claim(current):
        raise ValidationError("task is not ready and eligible for claim")

    claimed = vault._update_task_dispatch(
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
    if current.status not in {"ready", "doing"}:
        raise ValidationError("claimed task is not ready for hand binding")
    _require_factory_execution_approval(current)
    if current.status == "doing" and current.active_thread_id == clean_thread:
        return current
    if current.status != "ready":
        raise ValidationError("claimed task is not ready for hand binding")
    if current.active_thread_id is not None and current.active_thread_id != clean_thread:
        raise ValidationError("task is already bound to another active hand")

    bound = vault._update_task_dispatch(
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


def clear_task_hand(
    project_root: Path,
    identifier: str,
    *,
    expected_revision: str,
    dispatch_id: str,
    active_thread_id: str,
    admission: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> Task:
    """Clear one exact claimed Factory hand without changing task truth."""

    clean_dispatch = _dispatch_id(dispatch_id)
    clean_revision = _dispatch_revision(expected_revision)
    clean_thread = _active_thread(active_thread_id)
    vault = Vault(project_root)
    current = vault.get_task(identifier)
    _require_claim(current, clean_dispatch, clean_revision)
    if current.active_thread_id != clean_thread:
        raise ValidationError("task is not bound to this active Factory hand")
    _verify_factory_admission(
        project_root,
        current.identifier,
        clean_revision,
        clean_dispatch,
        admission,
    )

    cleared = vault._update_task_dispatch(
        current.identifier,
        clear_active_thread_id=True,
        expected_revision=current.revision,
        observed_at=observed_at,
    )
    read_back = vault.get_task(current.identifier)
    expected = replace(
        current,
        active_thread_id=None,
        history=read_back.history,
        updated_at=read_back.updated_at,
        revision=read_back.revision,
    )
    if read_back != expected:
        raise ValidationError("active Factory hand clear changed task truth")
    if cleared != read_back:
        raise ValidationError("active Factory hand clear readback returned a different task")
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
    _require_factory_execution_approval(current)
    if current.status not in {"ready", "doing"}:
        raise ValidationError("claimed task is not active for blocker handling")
    if current.waiting_on is not None and current.waiting_on != clean_condition:
        raise ValidationError("task already has a different named blocker")
    if current.blocker_owner is not None and current.blocker_owner != clean_owner:
        raise ValidationError("task already has a different blocker owner")
    if current.blocker_condition is not None and current.blocker_condition != clean_condition:
        raise ValidationError("task already has a different named blocker")

    blocked = vault._update_task_dispatch(
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
    _require_factory_execution_approval(current, allow_waiting=True)
    if current.status != "waiting":
        raise ValidationError("task is not blocked")
    if (
        current.blocker_owner != clean_owner
        or current.blocker_condition != clean_condition
        or current.waiting_on != clean_condition
    ):
        raise ValidationError("task blocker does not match the expected owner and condition")

    claim_by = current.claim_by
    cleared = vault._update_task_dispatch(
        current.identifier,
        status="ready",
        rank=current.rank,
        claim_by=claim_by,
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
    """Return every ready, unblocked, unclaimed, agent-approved task."""

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


def _require_factory_execution_approval(task: Task, *, allow_waiting: bool = False) -> None:
    if (
        task.agent_run != "yes"
        or task.next_actor != "agent"
        or (task.waiting_on is not None and not allow_waiting)
    ):
        raise ValidationError("Factory execution approval is no longer present")


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


def _clock_health(value: str) -> str:
    clean = value.strip().casefold()
    if clean not in SLA_CLOCK_HEALTH:
        raise ValidationError(f"clock health must be one of: {', '.join(SLA_CLOCK_HEALTH)}")
    return clean
