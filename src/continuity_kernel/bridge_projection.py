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
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED, event_dict
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.operations import OperationLedger, disposition_dict
from continuity_kernel.portfolio import portfolio_dict
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
    "Use $gsv-onboard to help me describe the context GSV should eventually use. First inspect "
    "the installed GSV help and state clearly whether a durable onboarding surface exists. In "
    "this foundation it does not: capture a proposal only, and do not claim sources are ready."
)
_NEW_HAND_PROMPT: Final = (
    "Start a new GSV hand. Read the installed GSV context and exact current records before "
    "deciding what deserves attention."
)
_CONTROL_REVIEW_PROMPT: Final = (
    "Review the pending Bridge intents for this GSV vault through the supported "
    "`gsv operation list` surface. Acknowledge or reject each intent against its current queue "
    "and disposition revisions. This is review only: do not apply the requested change, edit "
    "canonical records, use provider tools, or take external action. If nothing is pending, say so."
)
_GUIDED_REVIEW_PROMPT: Final = (
    "Start or resume one finite all-open GSV Portfolio review. Use one ordinary review-session "
    "Task with exactly one review-scope:all-open ref, at most one current "
    "review-subject:task:<id> ref, and review-covered:task:<id> refs only for outcomes the user "
    "explicitly checked. Covered never means resolved. Keep one exact active Codex hand on the "
    "session Task. Read pending Bridge correction intents through gsv operation list; interpret "
    "each answer yourself, apply only explicit semantic decisions through fresh native Task, "
    "WorkThread, and complete Portfolio CAS writes plus readback, then acknowledge or reject the "
    "intent with a result ref. Accepting an intent is only acknowledgement and never performs the "
    "semantic write. Present one exact current outcome, recommendation, and useful question. Do "
    "not infer meaning, end because of time or energy, or create a transcript store."
)


def project_snapshot(
    vault: Vault,
    *,
    doctor: dict[str, Any],
    integration: dict[str, Any],
    expected_vault_id: str | None = None,
    expected_root_identity: tuple[int, int] | None = None,
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
    controls = _project_controls(
        vault,
        codex_ready=codex_ready,
        expected_vault_id=expected_vault_id,
        expected_root_identity=expected_root_identity,
    )
    portfolio = _project_portfolio(
        vault,
        tasks=task_records,
        threads=thread_records,
        codex_ready=codex_ready,
        controls=controls,
    )
    return {
        "bridge": {
            "control_queue": CONTROL_STORE_SUPPORTED,
            "local": True,
            "semantic_write": False,
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
        "controls": controls,
        "doctor": resolved_doctor,
        "entities": entities,
        "mind": mind,
        "now": now,
        "portfolio": portfolio,
        "projection": {
            "sections": {name: section.payload() for name, section in projections.items()}
        },
        "status": status,
        "tasks": tasks,
        "threads": threads,
    }


def _project_portfolio(
    vault: Vault,
    *,
    tasks: tuple[Task, ...],
    threads: tuple[WorkThread, ...],
    codex_ready: bool,
    controls: dict[str, Any],
) -> dict[str, Any]:
    """Project authored Portfolio and finite-review navigation without choosing meaning."""

    start_url = codex_deep_link(vault.root, _GUIDED_REVIEW_PROMPT) if codex_ready else None
    try:
        portfolio = vault.get_portfolio()
    except (ContinuityError, OSError, UnicodeError, ValueError):
        return {
            "available": False,
            "items": [],
            "review": {"state": "unavailable", "start_url": start_url},
            "state": "missing",
        }

    task_by_id = {task.identifier: task for task in tasks}
    thread_by_id = {thread.identifier: thread for thread in threads}
    projected_items = []
    stale_count = 0
    for position, item in enumerate(portfolio.items, 1):
        task = task_by_id.get(item.task_id)
        thread = thread_by_id.get(item.work_thread_id) if item.work_thread_id else None
        task_stale = task is None or task.revision != item.task_revision
        thread_stale = bool(
            item.work_thread_id and (thread is None or thread.revision != item.work_thread_revision)
        )
        stale = task_stale or thread_stale
        stale_count += int(stale)
        projected_items.append(
            {
                **item.__dict__,
                "position": position,
                "stale": stale,
                "task": record_dict(task) if task is not None else None,
                "work_thread": record_dict(thread) if thread is not None else None,
            }
        )

    review_candidates = [
        task
        for task in tasks
        if task.status not in TERMINAL_TASK_STATUSES
        and any(ref.startswith("review-scope:") for ref in task.refs)
    ]
    review: dict[str, Any]
    if len(review_candidates) > 1:
        review = {
            "issue": "More than one nonterminal Task claims the guided-review scope.",
            "start_url": start_url,
            "state": "conflict",
        }
    elif not review_candidates:
        review = {"start_url": start_url, "state": "available"}
    else:
        session = review_candidates[0]
        scope_refs = [ref for ref in session.refs if ref.startswith("review-scope:")]
        subject_refs = [ref for ref in session.refs if ref.startswith("review-subject:")]
        covered_refs = [ref for ref in session.refs if ref.startswith("review-covered:")]
        issue = ""
        if scope_refs != ["review-scope:all-open"]:
            issue = "The review session must carry exactly one review-scope:all-open ref."
        subject_ids = [
            ref.removeprefix("review-subject:task:")
            for ref in subject_refs
            if ref.startswith("review-subject:task:")
        ]
        covered_ids = [
            ref.removeprefix("review-covered:task:")
            for ref in covered_refs
            if ref.startswith("review-covered:task:")
        ]
        if not issue and (len(subject_ids) != len(subject_refs) or len(subject_ids) > 1):
            issue = "The review session has malformed or conflicting subject refs."
        if not issue and (
            len(covered_ids) != len(covered_refs) or len(set(covered_ids)) != len(covered_ids)
        ):
            issue = "The review session has malformed or duplicate covered refs."
        if not issue and any(identifier not in task_by_id for identifier in covered_ids):
            issue = "The review session references a covered Task that is not present."
        pending_intents = [
            item["event"]
            for item in controls.get("items", [])
            if isinstance(item, dict)
            and item.get("status") == "pending"
            and isinstance(item.get("event"), dict)
            and item["event"].get("subject") == f"record:task/{session.identifier}"
        ]
        if not issue and len(pending_intents) > 1:
            issue = "More than one pending answer targets this exact review session."
        if (
            not issue
            and len(pending_intents) == 1
            and pending_intents[0].get("target_revision") != session.revision
        ):
            issue = "The pending review answer targets an older session revision."
        open_outcome_ids = {
            task.identifier
            for task in tasks
            if task.status not in TERMINAL_TASK_STATUSES
            and "review-scope:all-open" not in task.refs
        }
        subject_id = subject_ids[0] if subject_ids else None
        subject_matches = [item for item in projected_items if item["task_id"] == subject_id]
        subject = subject_matches[0] if len(subject_matches) == 1 else None
        subject_safe = bool(
            subject is not None
            and subject_id in open_outcome_ids
            and subject["stale"] is False
            and subject_id not in set(covered_ids)
        )
        if not issue and subject_id and not subject_safe:
            issue = (
                "The exact authored review subject is stale, closed, absent, or already covered."
            )
        checked_open = len(open_outcome_ids & set(covered_ids))
        review = {
            "active_thread_id": session.active_thread_id,
            "actionable": bool(
                subject_safe and session.active_thread_id and not issue and not pending_intents
            ),
            "checked_count": len(covered_ids),
            "checked_open_count": checked_open,
            "covered_task_ids": covered_ids,
            "hand_url": (
                f"codex://threads/{session.active_thread_id}" if session.active_thread_id else None
            ),
            "issue": issue or None,
            "open_count": len(open_outcome_ids),
            "pending_intent": pending_intents[0] if len(pending_intents) == 1 else None,
            "question": session.waiting_on,
            "recommendation": session.next_action,
            "session": record_dict(session),
            "session_revision": session.revision,
            "start_url": start_url,
            "state": "active" if not issue else "conflict",
            "subject": subject,
            "subject_task_id": subject_id,
            "uncovered_count": max(0, len(open_outcome_ids) - checked_open),
        }
    return {
        **portfolio_dict(portfolio),
        "available": True,
        "items": projected_items,
        "review": review,
        "stale_count": stale_count,
        "state": "stale" if stale_count else "current",
    }


def _project_controls(
    vault: Vault,
    *,
    codex_ready: bool,
    expected_vault_id: str | None,
    expected_root_identity: tuple[int, int] | None,
) -> dict[str, Any]:
    """Degrade the noncanonical control lane without hiding canonical records."""

    try:
        snapshot = OperationLedger(vault.root).snapshot(
            expected_vault_id=expected_vault_id,
            expected_root_identity=expected_root_identity,
        )
    except (ContinuityError, OSError, UnicodeError, ValueError):
        return {
            "archived_decided": None,
            "available": False,
            "decided": None,
            "disposition_revision": None,
            "generation": None,
            "history": [],
            "items": [],
            "pending": None,
            "queue_revision": None,
            "review_prompt": None,
            "review_url": None,
            "state": "unavailable",
        }
    items: list[dict[str, Any]] = [
        {"event": event_dict(event), "status": "pending"} for event in snapshot.pending
    ]
    items.extend(
        {
            "disposition": disposition_dict(disposition),
            "event": event_dict(event),
            "status": disposition.decision.value,
        }
        for event, disposition in snapshot.decided
    )
    items.sort(key=lambda item: (str(item["event"]["created_at"]), str(item["event"]["event_id"])))
    history: list[dict[str, Any]] = [
        {
            "archived": True,
            "disposition": disposition_dict(disposition),
            "event": event_dict(event),
            "generation": generation.queue_generation,
            "status": disposition.decision.value,
        }
        for generation in snapshot.archived
        for event, disposition in generation.decided
    ]
    history.sort(
        key=lambda item: (str(item["event"]["created_at"]), str(item["event"]["event_id"]))
    )
    return {
        "archived_decided": len(history),
        "available": True,
        "decided": len(snapshot.decided),
        "disposition_revision": snapshot.disposition_revision,
        "generation": snapshot.queue_generation,
        "history": history[-20:],
        "items": items[-20:],
        "pending": len(snapshot.pending),
        "queue_revision": snapshot.queue_revision,
        "review_prompt": _CONTROL_REVIEW_PROMPT,
        "review_url": codex_deep_link(vault.root, _CONTROL_REVIEW_PROMPT) if codex_ready else None,
        "state": "ready",
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
