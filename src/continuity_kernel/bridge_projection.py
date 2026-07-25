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
from continuity_kernel.codex_turn_transport import (
    START_REVIEW_CHOICE,
    START_REVIEW_SUBJECT,
    is_canonical_uuid,
)
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED, ControlEvent, event_dict
from continuity_kernel.direction import direction_dict
from continuity_kernel.errors import ContinuityError, NotFoundError, ValidationError
from continuity_kernel.operations import OperationLedger, disposition_dict
from continuity_kernel.portfolio import portfolio_dict
from continuity_kernel.records import (
    MAX_RECORD_BYTES,
    REVIEW_WORK_THREAD_ID,
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
    "Start or resume exactly one finite all-open GSV Portfolio review. Read Direction, the "
    "complete authored Portfolio, every open Task, and relevant WorkThreads and Entities. Use one "
    "ordinary "
    "nonterminal review-session Task owned only by thread:life-portfolio-review; that WorkThread "
    "must contain and focus the session. Keep exactly one review-scope:all-open ref, at most one "
    "review-subject:task:<id>, one exact active Codex hand, and review-state:paused only while the "
    "user explicitly pauses. Store the raw Codex thread UUID only in active_thread_id, the GSV "
    "WorkThread ID only in thread ownership and focus, and never add a codex-thread:* shadow ref. "
    "A nonterminal current subject uses status=waiting, next_actor=human, a nonempty next_action "
    "recommendation, and a nonempty waiting_on question. Present one exact authored outcome with "
    "current Task and owning "
    "WorkThread state, evidence and known staleness or contradiction, one grounded recommendation, "
    "authored option consequences, and one useful question. Read only the exact pending Bridge "
    "answer through gsv operation list. Interpret it yourself; apply only explicit semantic "
    "decisions through fresh native Task and WorkThread CAS, Direction CAS when relevant, and "
    "complete Portfolio CAS when affected, plus readback; then "
    "acknowledge or reject the receipt with a result ref. Acceptance is acknowledgement, never the "
    "semantic write. After an explicit answer or skip, anchor checked progress as "
    "review-covered:task:<id>@<task-revision>, adding "
    "|thread:<thread-id>@<thread-revision> when an owning WorkThread exists. Task or WorkThread "
    "drift makes that coverage stale; revisit it, and include new open outcomes. Checked never "
    "means resolved. The agent, not Bridge or Portfolio order, authors the next subject and option "
    "consequences through fresh session CAS. On explicit pause, preserve subject, coverage, focus, "
    "and the same hand. End only on explicit instruction or when every current open outcome has "
    "current anchored coverage; then clear the subject, paused state, active hand, every "
    "codex-thread:* shadow ref, and WorkThread focus while retaining scope and coverage evidence. "
    "Do not infer meaning, end because of time or energy, take unapproved external action, or "
    "create a transcript store."
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
    try:
        direction: dict[str, Any] | None = direction_dict(vault.get_direction())
    except NotFoundError:
        direction = None
    except (ContinuityError, OSError, UnicodeError, ValueError):
        direction = {"available": False}
    status = {
        **vault.identity(),
        "counts": {
            "tasks": len(task_records),
            "entities": len(entity_records),
            "threads": len(thread_records),
        },
    }
    controls, pending_controls = _project_controls(
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
        pending_controls=pending_controls,
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
        "direction": direction,
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
    pending_controls: tuple[ControlEvent, ...] | None,
) -> dict[str, Any]:
    """Project authored Portfolio and finite-review navigation without choosing meaning."""

    start_url = codex_deep_link(vault.root, _GUIDED_REVIEW_PROMPT) if codex_ready else None
    pending_starts = (
        []
        if pending_controls is None
        else [
            event
            for event in pending_controls
            if event.subject == START_REVIEW_SUBJECT and event.choice == START_REVIEW_CHOICE
        ]
    )
    pending_start = event_dict(pending_starts[0]) if len(pending_starts) == 1 else None
    try:
        portfolio = vault.get_portfolio()
        inspection = vault.inspect_portfolio()
    except (ContinuityError, OSError, UnicodeError, ValueError):
        return {
            "available": False,
            "items": [],
            "review": {
                "pending_start": pending_start,
                "state": "unavailable",
                "start_url": start_url,
            },
            "state": "missing",
        }
    if inspection.portfolio.revision != portfolio.revision:
        return {
            **portfolio_dict(portfolio),
            "available": False,
            "items": [],
            "review": {
                "issue": "The Portfolio changed during inspection; reload current truth.",
                "pending_start": pending_start,
                "start_url": start_url,
                "state": "unavailable",
            },
            "state": "unavailable",
        }

    task_by_id = {task.identifier: task for task in tasks}
    thread_by_id = {thread.identifier: thread for thread in threads}
    current_owners_by_task: dict[str, list[WorkThread]] = {}
    for candidate in threads:
        if candidate.identifier == REVIEW_WORK_THREAD_ID or candidate.status == "closed":
            continue
        for task_id_value in candidate.task_ids:
            current_owners_by_task.setdefault(task_id_value, []).append(candidate)
    projected_items = []
    stale_count = 0
    for position, item in enumerate(portfolio.items, 1):
        task = task_by_id.get(item.task_id)
        current_owners = tuple(
            sorted(
                current_owners_by_task.get(item.task_id, []),
                key=lambda value: value.identifier,
            )
        )
        thread = current_owners[0] if len(current_owners) == 1 else None
        task_stale = (
            item.task_id in inspection.stale_portfolio_task_ids
            or task is None
            or task.revision != item.task_revision
        )
        owner_is_current = len(current_owners) == 1 and (
            item.work_thread_id == current_owners[0].identifier
            and item.work_thread_revision == current_owners[0].revision
        )
        unthreaded_is_current = not current_owners and item.work_thread_id is None
        thread_stale = not owner_is_current and not unthreaded_is_current
        stale = task_stale or thread_stale
        stale_count += int(stale)
        projected_items.append(
            {
                **item.__dict__,
                "position": position,
                "stale": stale,
                "task_stale": task_stale,
                "task": record_dict(task) if task is not None else None,
                "thread_stale": thread_stale,
                "work_thread": record_dict(thread) if thread is not None else None,
            }
        )
    inspected_review = inspection.review
    review: dict[str, Any] = {
        "checked_count": len(inspected_review.coverages),
        "checked_current_count": len(inspected_review.current_coverage_task_ids),
        "covered_task_ids": list(inspected_review.current_coverage_task_ids),
        "issue": inspected_review.issue,
        "new_open_count": len(inspected_review.new_open_task_ids),
        "new_open_task_ids": list(inspected_review.new_open_task_ids),
        "open_count": len(inspected_review.open_task_ids),
        "options": [
            {"consequence": option.consequence, "intent": option.intent}
            for option in inspected_review.options
        ],
        "pending_start": pending_start,
        "revisit_count": len(inspected_review.revisit_task_ids),
        "revisit_task_ids": list(inspected_review.revisit_task_ids),
        "start_target_revision": portfolio.revision,
        "start_url": start_url,
        "state": inspected_review.state,
        "uncovered_count": len(inspected_review.uncovered_task_ids),
        "uncovered_task_ids": list(inspected_review.uncovered_task_ids),
    }
    if pending_controls is None:
        review["issue"] = (
            review["issue"] or "The review queue is unavailable; reload current truth."
        )
        if inspected_review.session_task_id is None:
            review["state"] = "unavailable"
    elif len(pending_starts) > 1:
        review["issue"] = review["issue"] or "More than one guided review start is pending."
        if inspected_review.session_task_id is None:
            review["state"] = "unavailable"
    session = (
        task_by_id.get(inspected_review.session_task_id)
        if inspected_review.session_task_id is not None
        else None
    )
    review_thread = thread_by_id.get(REVIEW_WORK_THREAD_ID)
    if session is not None:
        pending_intents = (
            []
            if pending_controls is None
            else [
                event_dict(event)
                for event in pending_controls
                if event.subject == f"record:task/{session.identifier}"
            ]
        )
        issue = review["issue"]
        active_thread_id = (
            session.active_thread_id if is_canonical_uuid(session.active_thread_id) else None
        )
        if session.active_thread_id is not None and active_thread_id is None and not issue:
            issue = (
                "The guided review hand is not an exact Codex task UUID; repair it before "
                "continuing."
            )
        if not issue and len(pending_intents) > 1:
            issue = "More than one pending answer targets this exact review session."
        if (
            not issue
            and len(pending_intents) == 1
            and pending_intents[0].get("target_revision") != session.revision
        ):
            issue = "The pending review answer targets an older session revision."
        subject_id = inspected_review.current_subject_task_id
        subject_matches = [item for item in projected_items if item["task_id"] == subject_id]
        subject = dict(subject_matches[0]) if len(subject_matches) == 1 else None
        if subject is not None and subject.get("task") is not None:
            task_refs = [
                ref for ref in subject["task"].get("refs", []) if not ref.startswith("review-")
            ]
            thread_refs = (
                subject["work_thread"].get("refs", [])
                if subject.get("work_thread") is not None
                else []
            )
            subject["evidence_refs"] = list(dict.fromkeys((*task_refs, *thread_refs)))
            staleness: list[str] = []
            if subject["task_stale"]:
                staleness.append("The Task changed after this Portfolio judgment was authored.")
            if subject["thread_stale"]:
                staleness.append(
                    "The owning WorkThread changed after this Portfolio judgment was authored."
                )
            if inspection.direction_changed:
                staleness.append("Direction changed after this Portfolio judgment was authored.")
            subject["staleness"] = staleness
        review.update(
            {
                "active_thread_id": active_thread_id,
                "actionable": bool(
                    inspected_review.state == "active"
                    and subject is not None
                    and subject.get("stale") is False
                    and not subject.get("staleness")
                    and active_thread_id
                    and not issue
                    and not pending_intents
                    and not pending_starts
                ),
                "hand_url": (f"codex://threads/{active_thread_id}" if active_thread_id else None),
                "issue": issue,
                "pending_intent": (pending_intents[0] if len(pending_intents) == 1 else None),
                "question": session.waiting_on,
                "recommendation": session.next_action,
                "review_thread": (
                    record_dict(review_thread) if review_thread is not None else None
                ),
                "session": record_dict(session),
                "session_revision": session.revision,
                "state": "conflict" if issue else inspected_review.state,
                "subject": subject,
                "subject_task_id": subject_id,
            }
        )
    return {
        **portfolio_dict(portfolio),
        "available": True,
        "direction_changed": inspection.direction_changed,
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
) -> tuple[dict[str, Any], tuple[ControlEvent, ...] | None]:
    """Degrade the noncanonical control lane without hiding canonical records."""

    try:
        snapshot = OperationLedger(vault.root).snapshot(
            expected_vault_id=expected_vault_id,
            expected_root_identity=expected_root_identity,
        )
    except (ContinuityError, OSError, UnicodeError, ValueError):
        return (
            {
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
            },
            None,
        )
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
    return (
        {
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
            "review_url": (
                codex_deep_link(vault.root, _CONTROL_REVIEW_PROMPT) if codex_ready else None
            ),
            "state": "ready",
        },
        snapshot.pending,
    )


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
