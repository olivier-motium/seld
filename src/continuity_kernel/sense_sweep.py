"""Five-second mechanical wake for the resident Mind.

The sweep reads only canonical clocks and content-free coverage receipts.  It
does not call providers, inspect provider bodies, or decide what any signal
means.  Semantic handling remains the resident AI's job.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from continuity_kernel.atomic import atomic_write, exclusive_lock, read_regular_file, sha256_bytes
from continuity_kernel.config import data_dir, local_host_id
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.records import TERMINAL_THREAD_STATUSES, format_time, parse_time
from continuity_kernel.resident_signals import ResidentSignalStore, SignalAppendRequest
from continuity_kernel.source_recipes import get_recipe
from continuity_kernel.source_state import (
    SourceCompleteness,
    SourceObservation,
    SourceResult,
    SourceSnapshot,
)
from continuity_kernel.vault import Vault

MAX_SWEEP_SECONDS: Final = 5.0
MIN_SWEEP_SECONDS: Final = 0.01
MAX_HEARTBEAT_BYTES: Final = 16 * 1024
HEARTBEAT_FORMAT_VERSION: Final = 2
PULSE_CANARY_ENV: Final = "SELD_PULSE_CANARY_NONCE"
_CANARY_NONCE = re.compile(r"^[0-9a-f]{32}$")
_HEARTBEAT_KEYS: Final = frozenset(
    {
        "canary_nonce",
        "duration_ms",
        "failure",
        "format_version",
        "host_id",
        "observed_at",
        "recall",
        "selected_sources",
        "sequence",
        "signals_emitted",
        "source_due",
        "status",
        "thread_rechecks",
        "vault_id",
        "vault_root_digest",
    }
)


@dataclass(frozen=True)
class SweepRecallStatus:
    attempted: bool
    changed: bool | None
    updated: bool
    failure: str | None


@dataclass(frozen=True)
class SenseSweepResult:
    observed_at: str
    status: str
    duration_ms: int
    selected_sources: int
    source_due: int
    thread_rechecks: int
    signals_emitted: int
    recall: SweepRecallStatus
    failure: str | None
    heartbeat_revision: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _BudgetExceeded(Exception):
    pass


def sense_sweep(
    vault: Vault | Path | str,
    *,
    signal_store: ResidentSignalStore | None = None,
    recall_refresh: Callable[[], SweepRecallStatus] | None = None,
    observed_at: datetime | None = None,
    budget_seconds: float = MAX_SWEEP_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> SenseSweepResult:
    """Run one current sweep; missed intervals are never enumerated or replayed.

    The mechanical scan stays within its five-second budget. A scheduled caller
    may add one host-admitted recall refresh after that scan while the sweep lock
    still prevents overlap.
    """

    if (
        isinstance(budget_seconds, bool)
        or not isinstance(budget_seconds, (int, float))
        or not MIN_SWEEP_SECONDS <= float(budget_seconds) <= MAX_SWEEP_SECONDS
    ):
        raise ValidationError("sense sweep budget must be between 0.01 and 5 seconds")
    current = observed_at or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValidationError("sense sweep timestamp must include a timezone")
    now = current.astimezone(UTC)
    active_vault = vault if isinstance(vault, Vault) else Vault(vault)
    store = signal_store or ResidentSignalStore(active_vault.root)
    vault_id = str(active_vault.identity()["vault_id"])
    host_id = local_host_id(create=True)
    if host_id is None:  # pragma: no cover - create=True either returns or raises.
        raise ValidationError("sense sweep could not establish a local host identity")
    vault_root_digest = sha256_bytes(str(active_vault.root).encode("utf-8"))
    canary_nonce = _canary_nonce(os.environ.get(PULSE_CANARY_ENV))
    host_root = _host_root(
        active_vault.root,
        vault_id=vault_id,
        host_id=host_id,
    )
    started = monotonic()
    deadline = started + float(budget_seconds)
    counts = {"selected": 0, "source": 0, "thread": 0, "signals": 0}
    recall_status = SweepRecallStatus(False, None, False, None)
    status = "complete"
    failure: str | None = None

    try:
        with exclusive_lock(host_root / "locks/sweep.lock", timeout=float(budget_seconds)):
            try:
                pending: list[SignalAppendRequest] = []
                _within_budget(monotonic, deadline)
                snapshot = active_vault.get_source_snapshot()
                counts["selected"] = len(snapshot.selected_sources)
                deadlines: dict[str, datetime] = {}
                incident_keys: dict[str, str] = {}
                for source_id in snapshot.selected_sources:
                    obs = snapshot.observation(source_id)
                    if source_id == "slack" and obs is not None and obs.last_success_at is not None:
                        deadlines["slack"] = (
                            parse_time(obs.last_success_at) + get_recipe("slack").proof_ttl
                        )
                    if (
                        obs is not None
                        and obs.result is SourceResult.FAILURE
                        and obs.error_code in AUTH_OR_TOOL_INCIDENT_ERROR_CODES
                    ):
                        incident_keys[source_id] = _source_event_key(source_id, obs)
                _within_budget(monotonic, deadline)
                existing_incident_keys = store.existing_event_keys(tuple(incident_keys.values()))
                changed_incidents = {
                    source_id
                    for source_id, event_key in incident_keys.items()
                    if event_key not in existing_incident_keys
                }
                due_source_ids = order_due_sources(
                    snapshot,
                    observed_at=now,
                    credential_deadlines=deadlines,
                    changed_incident_sources=changed_incidents,
                )
                for source_id in due_source_ids:
                    _within_budget(monotonic, deadline)
                    observation = snapshot.observation(source_id)
                    due_at = _source_due_at(source_id, observation)
                    event_key = _source_event_key(source_id, observation)
                    counts["source"] += 1
                    if event_key in existing_incident_keys:
                        continue
                    pending.append(
                        SignalAppendRequest(
                            kind="source-due",
                            ref=f"source:{source_id}",
                            event_key=event_key,
                            envelope=_source_signal_envelope(source_id, observation, due_at),
                            observed_at=now,
                        )
                    )

                _within_budget(monotonic, deadline)
                for thread in active_vault.list_threads():
                    _within_budget(monotonic, deadline)
                    if (
                        thread.status in TERMINAL_THREAD_STATUSES
                        or thread.recheck_at is None
                        or parse_time(thread.recheck_at) > now
                    ):
                        continue
                    pending.append(
                        SignalAppendRequest(
                            kind="work-thread-recheck",
                            ref=thread.identifier,
                            event_key=(
                                f"work-thread-recheck:{thread.identifier}:"
                                f"{thread.recheck_at}:{thread.updated_at}"
                            ),
                            envelope={
                                "work_thread_id": thread.identifier,
                                "recheck_at": thread.recheck_at,
                                "thread_updated_at": thread.updated_at,
                            },
                            observed_at=now,
                        )
                    )
                    counts["thread"] += 1

                _within_budget(monotonic, deadline)
                results = store.append_many_results(pending)
                counts["signals"] = sum(int(created) for _signal, created in results)
                _within_budget(monotonic, deadline)

                if recall_refresh is not None:
                    recall_status = recall_refresh()

            except _BudgetExceeded:
                status = "timed_out"
                failure = "budget_exceeded"
            except (ContinuityError, OSError):
                status = "failed"
                failure = "mechanical_scan_failed"

            duration_ms = max(0, int((monotonic() - started) * 1_000))
            heartbeat_revision = _write_heartbeat(
                host_root,
                observed_at=now,
                status=status,
                duration_ms=duration_ms,
                counts=counts,
                recall=recall_status,
                failure=failure,
                vault_id=vault_id,
                host_id=host_id,
                vault_root_digest=vault_root_digest,
                canary_nonce=canary_nonce,
            )
    except (ContinuityError, OSError):
        raise

    return SenseSweepResult(
        observed_at=format_time(now),
        status=status,
        duration_ms=duration_ms,
        selected_sources=counts["selected"],
        source_due=counts["source"],
        thread_rechecks=counts["thread"],
        signals_emitted=counts["signals"],
        recall=recall_status,
        failure=failure,
        heartbeat_revision=heartbeat_revision,
    )


def heartbeat_status(vault_root: Path | str) -> dict[str, object] | None:
    """Read the last content-free host heartbeat without touching the vault."""

    active_vault = Vault(vault_root)
    host_id = local_host_id(create=False)
    if host_id is None:
        return None
    vault_id = str(active_vault.identity()["vault_id"])
    vault_root_digest = sha256_bytes(str(active_vault.root).encode("utf-8"))
    path = _host_root(active_vault.root, vault_id=vault_id, host_id=host_id) / "heartbeat.json"
    if not os.path.lexists(path):
        return None
    try:
        encoded = read_regular_file(
            path,
            label="sense sweep heartbeat",
            max_bytes=MAX_HEARTBEAT_BYTES,
        )
        payload = json.loads(encoded)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("sense sweep heartbeat is invalid") from exc
    return _heartbeat_payload(
        payload,
        vault_id=vault_id,
        host_id=host_id,
        vault_root_digest=vault_root_digest,
    )


def order_due_sources(
    snapshot: SourceSnapshot,
    *,
    observed_at: datetime | None = None,
    credential_deadlines: Mapping[str, datetime | None] | None = None,
    changed_incident_sources: frozenset[str] | set[str] | None = None,
) -> tuple[str, ...]:
    """Order due selected sources by deterministic fair acquisition rules.

    WhatsApp remains mandatory each wake; afterward order due sources by
    credential deadline inside the next two 30-minute wakes, newly changed
    incident fingerprint, never-read, oldest due_at, then source ID.
    Unchanged auth/tool-absent incidents retry only after their fingerprint changes.
    """
    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    two_wakes_horizon = now + timedelta(minutes=60)
    deadlines = credential_deadlines or {}
    changed_incidents = changed_incident_sources or frozenset()

    due_sources: list[str] = []
    for source_id in snapshot.selected_sources:
        observation = snapshot.observation(source_id)
        deadline = deadlines.get(source_id)
        if deadline is not None:
            deadline = deadline.astimezone(UTC)
        is_changed_incident = source_id in changed_incidents

        if source_id == "whatsapp":
            due_sources.append(source_id)
            continue

        if (
            observation is not None
            and observation.result is SourceResult.FAILURE
            and observation.error_code in AUTH_OR_TOOL_INCIDENT_ERROR_CODES
        ):
            if is_changed_incident:
                due_sources.append(source_id)
            continue

        if deadline is not None and now <= deadline <= two_wakes_horizon:
            due_sources.append(source_id)
            continue

        if is_changed_incident:
            due_sources.append(source_id)
            continue

        if observation is None:
            due_sources.append(source_id)
            continue

        if observation.result is SourceResult.FAILURE:
            attempted = parse_time(observation.attempted_at)
            ttl = get_recipe(source_id).proof_ttl
            if now >= attempted + ttl:
                due_sources.append(source_id)
            continue

        if observation.last_success_at is None:
            due_sources.append(source_id)
            continue

        if observation.completeness is SourceCompleteness.PARTIAL:
            due_sources.append(source_id)
            continue

        due_at = parse_time(observation.last_success_at) + get_recipe(source_id).proof_ttl
        if now >= due_at:
            due_sources.append(source_id)

    def sort_key(source_id: str) -> tuple[int, tuple[int, float], int, int, float, str]:
        observation = snapshot.observation(source_id)
        deadline = deadlines.get(source_id)
        if deadline is not None:
            deadline = deadline.astimezone(UTC)
        is_changed_incident = source_id in changed_incidents

        # 0. WhatsApp mandatory each wake (first if present)
        is_not_whatsapp = 0 if source_id == "whatsapp" else 1

        # 1. Credential deadline inside next two 30-minute wakes (within 60m)
        if deadline is not None and now <= deadline <= two_wakes_horizon:
            deadline_priority = (0, deadline.timestamp())
        else:
            deadline_priority = (1, 0.0)

        # 2. Newly changed incident fingerprint
        is_not_changed_incident = 0 if is_changed_incident else 1

        # 3. Never-read
        is_not_never_read = 0 if (observation is None or observation.last_success_at is None) else 1

        # 4. Oldest due_at (furthest in the past first)
        if observation is None:
            due_ts = datetime.min.replace(tzinfo=UTC).timestamp()
        elif (
            observation.last_success_at is None
            or observation.completeness is SourceCompleteness.PARTIAL
        ):
            due_ts = parse_time(observation.attempted_at).timestamp()
        elif observation.result is SourceResult.FAILURE:
            due_ts = (
                parse_time(observation.attempted_at) + get_recipe(source_id).proof_ttl
            ).timestamp()
        else:
            due_ts = (
                parse_time(observation.last_success_at) + get_recipe(source_id).proof_ttl
            ).timestamp()

        # 5. Alphabetical source ID
        return (
            is_not_whatsapp,
            deadline_priority,
            is_not_changed_incident,
            is_not_never_read,
            due_ts,
            source_id,
        )

    return tuple(sorted(due_sources, key=sort_key))


def _source_due_at(source_id: str, observation: SourceObservation | None) -> datetime | None:
    if source_id == "whatsapp":
        return (
            parse_time(observation.attempted_at)
            if observation and observation.attempted_at
            else None
        )
    if observation is None:
        return None
    if observation.completeness is SourceCompleteness.PARTIAL:
        return parse_time(observation.attempted_at)
    if observation.result is SourceResult.FAILURE:
        if observation.error_code in AUTH_OR_TOOL_INCIDENT_ERROR_CODES:
            return None
        return parse_time(observation.attempted_at) + get_recipe(source_id).proof_ttl
    if observation.last_success_at is not None:
        return parse_time(observation.last_success_at) + get_recipe(source_id).proof_ttl
    return None


AUTH_OR_TOOL_INCIDENT_ERROR_CODES: Final = frozenset(
    {
        "auth_expired",
        "auth_required",
        "identity_mismatch",
        "local_path_rejected",
        "permission_denied",
        "policy_blocked",
        "secret_quarantined",
        "tool_absent",
    }
)


def _error_fingerprint(observation: SourceObservation) -> str:
    parts = (
        str(observation.error_code or ""),
        str(observation.attempted_tool_fingerprint or observation.tool_fingerprint or ""),
        str(observation.account_fingerprint or ""),
        str(observation.host_fingerprint or ""),
        str(observation.recipe_version or ""),
    )
    payload = ":".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _source_event_key(source_id: str, observation: SourceObservation | None) -> str:
    if observation is None:
        return f"source-due:{source_id}:{get_recipe(source_id).recipe_version}:never-read"
    if (
        observation.result is SourceResult.FAILURE
        and observation.error_code in AUTH_OR_TOOL_INCIDENT_ERROR_CODES
    ):
        return f"source-incident:{source_id}:{_error_fingerprint(observation)}"
    anchor = (
        observation.attempted_at
        if observation.result is SourceResult.FAILURE
        else observation.last_success_at or observation.attempted_at
    )
    return f"source-due:{source_id}:{get_recipe(source_id).recipe_version}:{anchor}"


def _source_signal_envelope(
    source_id: str,
    observation: SourceObservation | None,
    due_at: datetime | None,
) -> dict[str, object]:
    recipe_version = get_recipe(source_id).recipe_version
    if (
        observation is not None
        and observation.result is SourceResult.FAILURE
        and observation.error_code in AUTH_OR_TOOL_INCIDENT_ERROR_CODES
    ):
        return {
            "source_id": source_id,
            "recipe_version": recipe_version,
            "result": "failure",
            "error_code": observation.error_code,
            "incident_fingerprint": _error_fingerprint(observation),
        }
    return {
        "source_id": source_id,
        "recipe_version": recipe_version,
        "attempted_at": observation.attempted_at if observation else None,
        "last_success_at": observation.last_success_at if observation else None,
        "covered_through": observation.covered_through if observation else None,
        "due_at": format_time(due_at) if due_at else None,
    }


def _within_budget(monotonic: Callable[[], float], deadline: float) -> None:
    if monotonic() >= deadline:
        raise _BudgetExceeded


def _host_root(vault_root: Path, *, vault_id: str, host_id: str) -> Path:
    identity = hashlib.sha256(
        json.dumps(
            {
                "host_id": host_id,
                "vault_id": vault_id,
                "vault_root": str(vault_root),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return data_dir() / "sense-sweep" / identity


def _write_heartbeat(
    host_root: Path,
    *,
    observed_at: datetime,
    status: str,
    duration_ms: int,
    counts: dict[str, int],
    recall: SweepRecallStatus,
    failure: str | None,
    vault_id: str,
    host_id: str,
    vault_root_digest: str,
    canary_nonce: str | None,
) -> str:
    prior = heartbeat_status_from_root(host_root)
    previous_sequence = prior.get("sequence") if prior else None
    if prior is not None and (
        not isinstance(previous_sequence, int) or isinstance(previous_sequence, bool)
    ):
        raise ValidationError("sense sweep heartbeat sequence is invalid")
    sequence = previous_sequence + 1 if isinstance(previous_sequence, int) else 1
    payload: dict[str, object] = {
        "format_version": HEARTBEAT_FORMAT_VERSION,
        "sequence": sequence,
        "observed_at": format_time(observed_at),
        "status": status,
        "duration_ms": duration_ms,
        "selected_sources": counts["selected"],
        "source_due": counts["source"],
        "thread_rechecks": counts["thread"],
        "signals_emitted": counts["signals"],
        "recall": asdict(recall),
        "failure": failure,
        "vault_id": vault_id,
        "host_id": host_id,
        "vault_root_digest": vault_root_digest,
        "canary_nonce": canary_nonce,
    }
    encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    atomic_write(host_root / "heartbeat.json", encoded)
    return sha256_bytes(encoded)


def heartbeat_status_from_root(host_root: Path) -> dict[str, object] | None:
    path = host_root / "heartbeat.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(
            read_regular_file(
                path,
                label="sense sweep heartbeat",
                max_bytes=MAX_HEARTBEAT_BYTES,
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("sense sweep heartbeat is invalid") from exc
    return _heartbeat_payload(payload)


def _heartbeat_payload(
    payload: object,
    *,
    vault_id: str | None = None,
    host_id: str | None = None,
    vault_root_digest: str | None = None,
) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or set(payload) != _HEARTBEAT_KEYS
        or payload.get("format_version") != HEARTBEAT_FORMAT_VERSION
    ):
        raise ValidationError("sense sweep heartbeat has an unsupported shape")
    if payload.get("status") not in {"complete", "failed", "timed_out"}:
        raise ValidationError("sense sweep heartbeat status is invalid")
    for key in (
        "duration_ms",
        "selected_sources",
        "sequence",
        "signals_emitted",
        "source_due",
        "thread_rechecks",
    ):
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError("sense sweep heartbeat counters are invalid")
    observed_at = payload.get("observed_at")
    if not isinstance(observed_at, str):
        raise ValidationError("sense sweep heartbeat timestamp is invalid")
    parse_time(observed_at)
    failure = payload.get("failure")
    if failure is not None and failure not in {"budget_exceeded", "mechanical_scan_failed"}:
        raise ValidationError("sense sweep heartbeat failure is invalid")
    recall = payload.get("recall")
    if not isinstance(recall, dict) or set(recall) != {
        "attempted",
        "changed",
        "failure",
        "updated",
    }:
        raise ValidationError("sense sweep heartbeat recall status is invalid")
    if not isinstance(recall.get("attempted"), bool) or not isinstance(recall.get("updated"), bool):
        raise ValidationError("sense sweep heartbeat recall flags are invalid")
    if recall.get("changed") is not None and not isinstance(recall.get("changed"), bool):
        raise ValidationError("sense sweep heartbeat recall change flag is invalid")
    if recall.get("failure") not in {None, "deferred_budget", "refresh_failed"}:
        raise ValidationError("sense sweep heartbeat recall failure is invalid")
    canary_nonce = payload.get("canary_nonce")
    if canary_nonce is not None and (
        not isinstance(canary_nonce, str) or _CANARY_NONCE.fullmatch(canary_nonce) is None
    ):
        raise ValidationError("sense sweep heartbeat canary nonce is invalid")
    for key, expected in (
        ("vault_id", vault_id),
        ("host_id", host_id),
        ("vault_root_digest", vault_root_digest),
    ):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValidationError("sense sweep heartbeat binding is invalid")
        if expected is not None and value != expected:
            raise ValidationError("sense sweep heartbeat belongs to another vault or host")
    return payload


def _canary_nonce(value: str | None) -> str | None:
    if value is None:
        return None
    if _CANARY_NONCE.fullmatch(value) is None:
        raise ValidationError("sense sweep canary nonce is invalid")
    return value
