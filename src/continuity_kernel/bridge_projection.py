"""Pure vault-to-Bridge projection and deep-link rendering."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlencode

from continuity_kernel import __version__
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.records import (
    MAX_RECORD_BYTES,
    TERMINAL_TASK_STATUSES,
    Entity,
    Record,
    Task,
    WorkThread,
    parse_entity,
    parse_task,
    parse_thread,
    record_dict,
)
from continuity_kernel.vault import Vault, doctor_dict

REPOSITORY_URL: Final = "https://github.com/olivier-motium/gsv"


@dataclass(frozen=True)
class _RecordProjection:
    records: tuple[Record, ...]
    state: str
    issues: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "issues": [dict(issue) for issue in self.issues],
            "readable": len(self.records),
            "state": self.state,
            "unreadable": len(self.issues),
        }


@dataclass(frozen=True)
class _ObservedRecordFile:
    metadata: os.stat_result | None
    error: str | None = None

    def fingerprint(self) -> tuple[object, ...]:
        if self.metadata is None:
            return ("unreadable",)
        return (
            "observed",
            self.metadata.st_dev,
            self.metadata.st_ino,
            self.metadata.st_mode,
            self.metadata.st_size,
            self.metadata.st_mtime_ns,
            self.metadata.st_ctime_ns,
        )


_NEW_MIND_PROMPT: Final = (
    "Help me shape my GSV Mind. Read the installed GSV context first, then ask only the few "
    "questions needed to make MIND.md and NOW.md useful."
)
_NEW_HAND_PROMPT: Final = (
    "Start a new GSV hand. Read the installed GSV context and exact current records before "
    "deciding what deserves attention."
)


def project_snapshot(
    vault: Vault,
    *,
    doctor: dict[str, Any],
    integration: dict[str, Any],
) -> dict[str, Any]:
    """Return the exact authored records needed by the read-only Bridge."""

    resolved_doctor = doctor
    resolved_integration = integration
    codex_ready = resolved_integration.get("ready") is True
    task_projection = _project_record_section(
        vault,
        directory="tasks",
        parser=parse_task,
        sort_key=lambda item: (
            cast(Task, item).status,
            cast(Task, item).updated_at,
            cast(Task, item).identifier,
        ),
    )
    entity_projection = _project_record_section(
        vault,
        directory="entities",
        parser=parse_entity,
        sort_key=lambda item: (
            cast(Entity, item).entity_type,
            cast(Entity, item).title.casefold(),
            cast(Entity, item).identifier,
        ),
    )
    thread_projection = _project_record_section(
        vault,
        directory="threads",
        parser=parse_thread,
        sort_key=lambda item: (
            cast(WorkThread, item).status,
            cast(WorkThread, item).updated_at,
            cast(WorkThread, item).identifier,
        ),
    )
    projections = {
        "tasks": task_projection,
        "entities": entity_projection,
        "threads": thread_projection,
    }
    resolved_doctor = _merge_projection_doctor(resolved_doctor, projections)
    task_records = tuple(cast(Task, item) for item in task_projection.records)
    thread_records = tuple(cast(WorkThread, item) for item in thread_projection.records)
    entity_records = tuple(cast(Entity, item) for item in entity_projection.records)
    tasks = []
    for item in task_records:
        task = record_dict(item)
        if codex_ready and item.status not in TERMINAL_TASK_STATUSES:
            task["codex_url"] = codex_deep_link(
                vault.root,
                f"Resume the GSV commitment `{item.identifier}`. Load its exact current record "
                "and revision before deciding or changing anything.",
            )
        tasks.append(task)
    threads = [record_dict(item) for item in thread_records]
    entities = [record_dict(item) for item in entity_records]
    mind = vault.read_document("MIND.md")
    now = vault.read_document("NOW.md")
    status = {
        **vault.identity(),
        "counts": {
            "tasks": len(task_records),
            "entities": len(entity_records),
            "threads": len(thread_records),
        },
    }
    return {
        "bridge": {
            "local": True,
            "read_only": True,
            "version": __version__,
        },
        "codex": {
            **resolved_integration,
            "ready": codex_ready,
            **(
                {
                    "new_hand_url": codex_deep_link(vault.root, _NEW_HAND_PROMPT),
                    **(
                        {"new_mind_url": codex_deep_link(vault.root, _NEW_MIND_PROMPT)}
                        if task_projection.state == "complete" and not task_records
                        else {}
                    ),
                }
                if codex_ready
                else {}
            ),
        },
        "doctor": resolved_doctor,
        "entities": entities,
        "mind": mind,
        "now": now,
        "projection": {
            "sections": {name: section.payload() for name, section in projections.items()}
        },
        "status": status,
        "tasks": tasks,
        "threads": threads,
    }


def _project_record_section(
    vault: Vault,
    *,
    directory: str,
    parser: Callable[[str], Record],
    sort_key: Callable[[Record], tuple[Any, ...]],
) -> _RecordProjection:
    root = vault.root / directory
    unavailable = _section_directory_issue(root, vault.root, directory)
    if unavailable is not None:
        return _RecordProjection((), "unavailable", (unavailable,))

    try:
        before = os.lstat(root)
        listed = _record_directory_snapshot(root)
    except OSError as exc:
        issue = _projection_issue(
            "unavailable-record-section",
            directory,
            f"could not enumerate the {directory} record section: {exc}",
        )
        return _RecordProjection((), "unavailable", (issue,))

    records: list[Record] = []
    issues: list[dict[str, Any]] = []
    for name, observed in listed.items():
        relative = f"{directory}/{name}"
        path = root / name
        try:
            metadata = observed.metadata
            if metadata is None:
                raise ValidationError(
                    f"could not inspect record: {observed.error or 'unknown error'}"
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise ValidationError("record must be a regular file and cannot be a symbolic link")
            if metadata.st_size > MAX_RECORD_BYTES:
                raise ValidationError("record exceeds its size bound")
            record = parser(vault._read_text(path, max_bytes=MAX_RECORD_BYTES))
            vault._assert_record_identity(path, record)
        except (ContinuityError, OSError, UnicodeError) as exc:
            issues.append(_projection_issue("invalid-record", relative, str(exc)))
            continue
        records.append(record)

    try:
        confirmed = _record_directory_snapshot(root)
        after = os.lstat(root)
    except OSError as exc:
        issue = _projection_issue(
            "unavailable-record-section",
            directory,
            f"the {directory} record section changed during inspection: {exc}",
        )
        return _RecordProjection((), "unavailable", (issue,))
    directory_changed = not stat.S_ISDIR(before.st_mode) or not stat.S_ISDIR(after.st_mode)
    directory_changed = directory_changed or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    )
    initial_fingerprints = {name: item.fingerprint() for name, item in listed.items()}
    confirmed_fingerprints = {name: item.fingerprint() for name, item in confirmed.items()}
    if directory_changed or initial_fingerprints != confirmed_fingerprints:
        issue = _projection_issue(
            "unavailable-record-section",
            directory,
            f"the {directory} record section changed during inspection; "
            "reload to use a stable view",
        )
        return _RecordProjection((), "unavailable", (issue,))

    return _RecordProjection(
        tuple(sorted(records, key=sort_key)),
        "partial" if issues else "complete",
        tuple(issues),
    )


def _record_directory_snapshot(root: Path) -> dict[str, _ObservedRecordFile]:
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries if entry.name.endswith(".md"))

    observed: dict[str, _ObservedRecordFile] = {}
    for name in names:
        try:
            observed[name] = _ObservedRecordFile(os.lstat(root / name))
        except OSError as exc:
            observed[name] = _ObservedRecordFile(None, str(exc))
    return observed


def _section_directory_issue(path: Path, vault_root: Path, directory: str) -> dict[str, Any] | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return _projection_issue(
            "unavailable-record-section",
            directory,
            f"the {directory} record section is missing",
        )
    except OSError as exc:
        return _projection_issue(
            "unavailable-record-section",
            directory,
            f"could not inspect the {directory} record section: {exc}",
        )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return _projection_issue(
            "unavailable-record-section",
            str(path.relative_to(vault_root)),
            f"the {directory} record section must be a real directory, not a link or other file",
        )
    return None


def _projection_issue(code: str, path: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "path": path, "repairable": False}


def _bridge_doctor(vault: Vault) -> dict[str, Any]:
    try:
        return doctor_dict(vault.doctor())
    except (ContinuityError, OSError, UnicodeError) as exc:
        identity = vault.identity()
        return {
            "counts": {"entities": 0, "tasks": 0, "threads": 0},
            "healthy": False,
            "issues": [
                _projection_issue(
                    "doctor-unavailable",
                    ".",
                    f"the full vault integrity check could not finish: {exc}",
                )
            ],
            "repaired": [],
            "vault": str(vault.root),
            "vault_id": identity["vault_id"],
        }


def _merge_projection_doctor(
    doctor: dict[str, Any], projections: dict[str, _RecordProjection]
) -> dict[str, Any]:
    merged = dict(doctor)
    prior_issues = doctor.get("issues")
    issues = [dict(issue) for issue in prior_issues] if isinstance(prior_issues, list) else []
    seen = {
        (issue.get("code"), issue.get("path"), issue.get("message"))
        for issue in issues
        if isinstance(issue, dict)
    }
    for projection in projections.values():
        for issue in projection.issues:
            identity = (issue.get("code"), issue.get("path"), issue.get("message"))
            if identity not in seen:
                issues.append(dict(issue))
                seen.add(identity)
    prior_counts = doctor.get("counts")
    counts = dict(prior_counts) if isinstance(prior_counts, dict) else {}
    counts.update({name: len(projection.records) for name, projection in projections.items()})
    merged["counts"] = counts
    merged["issues"] = issues
    if any(projection.state != "complete" for projection in projections.values()):
        merged["healthy"] = False
    return merged


def codex_deep_link(vault: Path, prompt: str) -> str:
    """Build the installed Codex app's verified new-task deep-link shape."""

    query = urlencode(
        {
            "originUrl": REPOSITORY_URL,
            "path": str(vault.expanduser().resolve()),
            "prompt": prompt,
        }
    )
    return f"codex://new?{query}"
