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
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from continuity_kernel.atomic import atomic_write, exclusive_lock, read_regular_file, sha256_bytes
from continuity_kernel.config import data_dir, local_host_id
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.records import TERMINAL_THREAD_STATUSES, format_time, parse_time
from continuity_kernel.resident_signals import ResidentSignalStore, SignalAppendRequest
from continuity_kernel.source_recipes import get_recipe
from continuity_kernel.source_state import SourceCompleteness, SourceObservation
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
                for source_id in snapshot.selected_sources:
                    _within_budget(monotonic, deadline)
                    observation = snapshot.observation(source_id)
                    due_at = _source_due_at(source_id, observation)
                    if due_at is not None and now < due_at:
                        continue
                    pending.append(
                        SignalAppendRequest(
                            kind="source-due",
                            ref=f"source:{source_id}",
                            event_key=_source_event_key(source_id, observation),
                            envelope={
                                "source_id": source_id,
                                "recipe_version": get_recipe(source_id).recipe_version,
                                "attempted_at": observation.attempted_at if observation else None,
                                "last_success_at": (
                                    observation.last_success_at if observation else None
                                ),
                                "covered_through": (
                                    observation.covered_through if observation else None
                                ),
                                "due_at": format_time(due_at) if due_at else None,
                            },
                            observed_at=now,
                        )
                    )
                    counts["source"] += 1

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


def _source_due_at(source_id: str, observation: SourceObservation | None) -> datetime | None:
    if observation is None or observation.last_success_at is None:
        return None
    if observation.completeness is SourceCompleteness.PARTIAL:
        return parse_time(observation.attempted_at)
    return parse_time(observation.last_success_at) + get_recipe(source_id).proof_ttl


def _source_event_key(source_id: str, observation: SourceObservation | None) -> str:
    anchor = observation.attempted_at if observation is not None else "never-read"
    return f"source-due:{source_id}:{get_recipe(source_id).recipe_version}:{anchor}"


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
