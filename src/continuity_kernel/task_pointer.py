"""Author a task and deliver one exact typed pointer through Workbench mail."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.errors import ConflictError, NotFoundError, ValidationError
from continuity_kernel.records import Task, target_seat_value
from continuity_kernel.vault import Vault

MAX_EVENT_KEY_LENGTH = 128
POINTER_MAIL_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class TaskPointerResult:
    """Result of one task authoring act and its keyed pointer delivery."""

    task: Task
    created: bool
    event_key: str | None


def create_or_update_task_and_place_pointer_mail(
    project_root: Path,
    *,
    identifier: str,
    title: str,
    outcome: str,
    target_seat: str | None,
    authoring_seat: str | None,
    expected_revision: str | None = None,
    status: str | None = None,
    rank: int | None = None,
    next_actor: str | None = None,
    next_action: str | None = None,
    waiting_on: str | None = None,
    claim_by: str | None = None,
    progress_check_by: str | None = None,
    blocker_owner: str | None = None,
    blocker_condition: str | None = None,
    agent_run: str | None = None,
    project: str | None = None,
    workspace: str | None = None,
    observed_at: datetime | None = None,
) -> TaskPointerResult:
    """Author one task revision and deliver its typed pointer in the same act.

    ``authoring_seat`` is mandatory even when ``target_seat`` is empty. The
    operation never supplies or infers a sender identity.
    """

    clean_author = _authoring_seat(authoring_seat)
    clean_target = target_seat_value(target_seat)
    vault = Vault(project_root)
    try:
        existing = vault.get_task(identifier)
    except NotFoundError:
        existing = None

    if existing is None:
        if expected_revision is not None:
            raise ValidationError("a new task cannot carry expected_revision")
        task = vault.create_task(
            identifier=identifier,
            title=title,
            outcome=outcome,
            status=status or "captured",
            rank=rank,
            next_actor=next_actor,
            target_seat=clean_target,
            next_action=next_action,
            waiting_on=waiting_on,
            claim_by=claim_by,
            progress_check_by=progress_check_by,
            blocker_owner=blocker_owner,
            blocker_condition=blocker_condition,
            agent_run=agent_run,
            project=project,
            workspace=workspace,
            observed_at=observed_at,
        )
        created = True
    else:
        if expected_revision is None:
            raise ValidationError("existing task update requires expected_revision")
        task = vault.update_task(
            existing.identifier,
            expected_revision=expected_revision,
            title=title,
            outcome=outcome,
            status=status,
            rank=rank,
            next_actor=next_actor,
            target_seat=clean_target,
            next_action=next_action,
            waiting_on=waiting_on,
            claim_by=claim_by,
            progress_check_by=progress_check_by,
            blocker_owner=blocker_owner,
            blocker_condition=blocker_condition,
            agent_run=agent_run,
            project=project,
            workspace=workspace,
            observed_at=observed_at,
        )
        created = False

    read_back = vault.get_task(task.identifier)
    if read_back != task:
        raise ValidationError("task revision readback failed before pointer placement")
    event_key = _place_pointer_mail(
        project_root,
        read_back,
        authoring_seat=clean_author,
    )
    return TaskPointerResult(task=read_back, created=created, event_key=event_key)


create_or_update_task_and_place_pointer = create_or_update_task_and_place_pointer_mail


def place_task_pointer_mail(
    project_root: Path,
    identifier: str,
    *,
    expected_revision: str,
    authoring_seat: str | None,
) -> str | None:
    """Deliver one keyed pointer for an already authored task revision."""

    clean_author = _authoring_seat(authoring_seat)
    vault = Vault(project_root)
    task = vault.get_task(identifier)
    if task.revision != expected_revision:
        raise ConflictError("task changed before pointer placement")
    read_back = vault.get_task(identifier)
    if read_back != task:
        raise ValidationError("task revision readback failed before pointer placement")
    return _place_pointer_mail(project_root, read_back, authoring_seat=clean_author)


def task_pointer_event_key(identifier: str, revision: str, target_seat: str) -> str:
    """Derive the stable, bounded pointer event key used by Workbench mail."""

    readable = f"task:{identifier}:rev-{revision[:8]}:{target_seat}"
    if len(readable) <= MAX_EVENT_KEY_LENGTH:
        return readable
    digest = hashlib.sha256(f"{identifier}\0{revision}\0{target_seat}".encode()).hexdigest()
    return f"task-pointer:{digest}"


def _place_pointer_mail(
    project_root: Path,
    task: Task,
    *,
    authoring_seat: str,
) -> str | None:
    target = target_seat_value(task.target_seat)
    if target is None:
        return None
    revision = _task_revision_readback(project_root, task)
    event_key = task_pointer_event_key(task.identifier, revision, target)
    mail_root = _mail_root()
    executable = _mail_executable()
    body = (
        "POINTER MAIL, placed in the same authored act as the task revision: "
        f"task {task.identifier} at revision {revision} names {target} as target.\n"
        f"OUTCOME: {task.outcome}\n"
        f"NEXT-ACTION: {task.next_action or 'Not recorded.'}"
    )
    command = [
        str(executable),
        "--root",
        str(mail_root),
        "send",
        "--from",
        authoring_seat,
        "--to",
        target,
        "--kind",
        "request",
        "--refs",
        f"task:{task.identifier},task-revision:{task.identifier}:{revision}",
        "--event-key",
        event_key,
        "--slug",
        "task-pointer",
        "--body",
        body,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=POINTER_MAIL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError(f"pointer mail placement failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise ValidationError(
            f"pointer mail placement failed with exit {result.returncode}: {detail}"
        )

    output = (result.stdout or "").strip()
    path_text = output.splitlines()[-1].strip() if output else ""
    if not path_text:
        raise ValidationError("pointer mail readback failed: sender returned no path")
    mail_path = Path(path_text).expanduser()
    if not mail_path.is_absolute():
        mail_path = mail_root / mail_path
    try:
        mail_path = mail_path.resolve()
        mail_path.relative_to(mail_root.resolve())
    except ValueError as exc:
        raise ValidationError("pointer mail readback escaped the configured mail root") from exc
    if not mail_path.is_file():
        raise ValidationError("pointer mail readback failed: file does not exist")
    try:
        content = mail_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("pointer mail readback failed: file is unreadable") from exc
    if not _header_matches(content, "from", authoring_seat):
        raise ValidationError("pointer mail readback failed: sender does not match authoring seat")
    if not _header_matches(content, "to", target):
        raise ValidationError("pointer mail readback failed: recipient does not match target seat")
    if not _header_matches(content, "event-key", event_key):
        raise ValidationError(
            "pointer mail readback failed: event key does not match task revision"
        )
    return event_key


def _task_revision_readback(project_root: Path, task: Task) -> str:
    path = Vault(project_root).root / "tasks" / f"{task.identifier}.md"
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValidationError("task revision readback failed") from exc
    revision = sha256_bytes(content)
    if revision != task.revision:
        raise ValidationError("task revision readback failed")
    return revision


def _header_matches(content: str, name: str, expected: str) -> bool:
    prefix = f"{name}:"
    for line in content.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"') == expected
    return False


def _authoring_seat(value: str | None) -> str:
    if value is None:
        raise ValidationError("authoring seat is required")
    clean = target_seat_value(value)
    if clean is None:
        raise ValidationError("authoring seat is required")
    return clean


def _mail_root() -> Path:
    configured = os.environ.get("WB_MAIL_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".workbench/mail"


def _mail_executable() -> Path:
    configured = os.environ.get("WB_MAIL_BIN")
    if configured:
        return Path(configured).expanduser()
    installed = Path.home() / ".workbench/bin/wb-mail"
    if installed.is_file():
        return installed
    discovered = shutil.which("wb-mail")
    return Path(discovered) if discovered is not None else installed
