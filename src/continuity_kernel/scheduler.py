"""Owned macOS launchd lifecycle for the mechanical sense sweep.

The scheduler owns one per-user label, plist, and host-local receipt.  It
refuses to replace files or jobs it cannot prove it owns.  Windows remains an
explicitly unsupported host for this foundation slice.
"""

from __future__ import annotations

import base64
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from continuity_kernel.atomic import (
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    atomic_write,
    durable_unlink,
    exclusive_lock,
    read_regular_file,
    sha256_bytes,
)
from continuity_kernel.config import data_dir
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    PersistenceError,
    SetupError,
    ValidationError,
)
from continuity_kernel.records import format_time, parse_time
from continuity_kernel.sense_sweep import PULSE_CANARY_ENV, heartbeat_status

SCHEDULER_LABEL: Final = "ai.seld.sense-sweep"
SCHEDULER_FORMAT_VERSION: Final = 1
ABSENT_SCHEDULER_REVISION: Final = sha256_bytes(b"\0")
MIN_INTERVAL_SECONDS: Final = 60
MAX_INTERVAL_SECONDS: Final = 3_600
MAX_RECEIPT_BYTES: Final = 16 * 1024
MAX_PLIST_BYTES: Final = 64 * 1024
MAX_LAUNCHCTL_OUTPUT_BYTES: Final = 64 * 1024
MAX_INSTALL_OPERATION_BYTES: Final = 256 * 1024
INSTALL_OPERATION_FORMAT_VERSION: Final = 1
SCHEDULER_FINGERPRINT_ENV: Final = "SELD_SCHEDULER_FINGERPRINT"
GSV_DATA_DIR_ENV: Final = "GSV_DATA_DIR"
_LAUNCHCTL_SERVICE_NOT_FOUND: Final = 113


@dataclass(frozen=True)
class RunnerResult:
    returncode: int
    stdout: str = ""


class Runner(Protocol):
    def __call__(self, command: Sequence[str], *, timeout_seconds: float) -> RunnerResult: ...


@dataclass(frozen=True)
class SchedulerPlan:
    command: tuple[str, ...]
    interval_seconds: int
    label: str
    plist: str
    plist_digest: str
    receipt: str
    receipt_revision: str


@dataclass(frozen=True)
class SchedulerStatus:
    current: bool
    foreign: bool
    installed: bool
    label: str
    loaded: bool
    plist: str
    receipt_revision: str
    stale: bool


@dataclass(frozen=True)
class SchedulerInstall:
    changed: bool
    loaded: bool
    plist: str
    receipt_revision: str


@dataclass(frozen=True)
class SchedulerCanary:
    command: tuple[str, ...]
    duration_ms: int
    failure: str | None
    heartbeat_observed_at: str | None
    heartbeat_sequence: int | None
    proved: bool


@dataclass(frozen=True)
class _Receipt:
    format_version: int
    interval_seconds: int
    job_fingerprint: str
    label: str
    plan_digest: str
    plist: str
    plist_digest: str
    updated_at: str


@dataclass(frozen=True)
class _JobIdentity:
    command: tuple[str, ...]
    data_root: str
    fingerprint: str
    interval_seconds: int


@dataclass(frozen=True)
class _InstallOperation:
    format_version: int
    label: str
    phase: str
    plist: str
    prior_fingerprint: str | None
    prior_plist: str | None
    prior_receipt: str | None
    target_fingerprint: str
    target_plist: str
    target_receipt: str


class MacOSScheduler:
    """Plan and own one exact per-user launchd job."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        vault_root: Path | str,
        interval_seconds: int = 600,
        data_root: Path | str | None = None,
        launch_agents_dir: Path | str | None = None,
        runtime_root: Path | str | None = None,
        uid: int | None = None,
        platform: str | None = None,
    ):
        if (platform or sys.platform) != "darwin":
            raise SetupError("the sense sweep scheduler currently supports macOS only")
        self.command = _command(command)
        self.vault_root = _absolute(vault_root, "scheduler vault")
        _require_sweep_command(self.command, vault_root=self.vault_root)
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, int)
            or not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS
        ):
            raise ValidationError("scheduler interval must be between 60 and 3600 seconds")
        self.interval_seconds = interval_seconds
        self.data_root = _absolute(data_root or data_dir(), "scheduler data directory")
        self.uid = os.getuid() if uid is None else uid
        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise ValidationError("scheduler user ID is invalid")
        self.launch_agents_dir = _absolute(
            launch_agents_dir or Path.home() / "Library/LaunchAgents",
            "LaunchAgents directory",
        )
        self.runtime_root = _absolute(
            runtime_root or self.data_root / "scheduler/sense-sweep",
            "scheduler runtime directory",
        )

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{SCHEDULER_LABEL}.plist"

    @property
    def receipt_path(self) -> Path:
        return self.runtime_root / "receipt.json"

    @property
    def operation_path(self) -> Path:
        return self.runtime_root / "install-operation.json"

    @property
    def lock_path(self) -> Path:
        return self.runtime_root / "locks/scheduler.lock"

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def plan(self) -> SchedulerPlan:
        encoded = self._plist_bytes()
        revision = self._operation_prior_revision()
        return SchedulerPlan(
            command=self.command,
            interval_seconds=self.interval_seconds,
            label=SCHEDULER_LABEL,
            plist=str(self.plist_path),
            plist_digest=sha256_bytes(encoded),
            receipt=str(self.receipt_path),
            receipt_revision=revision,
        )

    def status(self, *, runner: Runner | None = None) -> SchedulerStatus:
        run = runner or _run
        with exclusive_lock(self.lock_path):
            return self._status_unlocked(run)

    def install(
        self,
        *,
        expected_revision: str,
        runner: Runner | None = None,
    ) -> SchedulerInstall:
        run = runner or _run
        with (
            exclusive_lock(self.lock_path),
            _pinned_scheduler_storage(
                self.operation_path,
                self.receipt_path,
                self.plist_path,
            ),
        ):
            self._recover_install_operation(run)
            receipt, revision, previous_receipt = self._read_receipt()
            _expect_revision(revision, expected_revision)
            previous_plist = _read_optional(
                self.plist_path,
                MAX_PLIST_BYTES,
                "scheduler plist",
            )
            prior_identity = _owned_job_identity(
                receipt,
                plist_path=self.plist_path,
                plist_bytes=previous_plist,
            )
            loaded, loaded_exact = self._loaded_identity_state(
                run,
                SCHEDULER_LABEL,
                prior_identity,
            )
            _require_owned_or_absent(
                receipt,
                plist_path=self.plist_path,
                plist_bytes=previous_plist,
                loaded=loaded,
                loaded_exact=loaded_exact,
            )
            planned_plist = self._plist_bytes()
            planned_digest = sha256_bytes(planned_plist)
            plan_digest = self._plan_digest(planned_digest)
            if (
                receipt is not None
                and previous_plist == planned_plist
                and receipt.plan_digest == plan_digest
                and loaded_exact
            ):
                return SchedulerInstall(False, True, str(self.plist_path), revision)

            self._prepare_directories()
            next_receipt = _Receipt(
                format_version=SCHEDULER_FORMAT_VERSION,
                interval_seconds=self.interval_seconds,
                job_fingerprint=self._job_fingerprint(),
                label=SCHEDULER_LABEL,
                plan_digest=plan_digest,
                plist=str(self.plist_path),
                plist_digest=planned_digest,
                updated_at=format_time(datetime.now(UTC)),
            )
            next_encoded = _receipt_bytes(next_receipt)
            operation = _InstallOperation(
                format_version=INSTALL_OPERATION_FORMAT_VERSION,
                label=SCHEDULER_LABEL,
                phase="prepared",
                plist=str(self.plist_path),
                prior_fingerprint=receipt.job_fingerprint if receipt else None,
                prior_plist=_encoded_bytes(previous_plist),
                prior_receipt=_encoded_bytes(previous_receipt),
                target_fingerprint=next_receipt.job_fingerprint,
                target_plist=_encoded_required(planned_plist),
                target_receipt=_encoded_required(next_encoded),
            )
            _cas_replace_file(
                self.operation_path,
                expected=None,
                replacement=_operation_bytes(operation),
                maximum=MAX_INSTALL_OPERATION_BYTES,
                label="scheduler install operation",
            )
            try:
                _cas_replace_file(
                    self.plist_path,
                    expected=previous_plist,
                    replacement=planned_plist,
                    maximum=MAX_PLIST_BYTES,
                    label="scheduler plist",
                    mode=0o644,
                )
                operation = self._set_operation_phase(operation, "plist_written")
                _cas_replace_file(
                    self.receipt_path,
                    expected=previous_receipt,
                    replacement=next_encoded,
                    maximum=MAX_RECEIPT_BYTES,
                    label="scheduler receipt",
                )
                operation = self._set_operation_phase(operation, "authority_published")
                if loaded:
                    result = run(
                        ("/bin/launchctl", "bootout", f"{self.domain}/{SCHEDULER_LABEL}"),
                        timeout_seconds=10,
                    )
                    if result.returncode:
                        raise SetupError("macOS could not unload the owned sense sweep")
                operation = self._set_operation_phase(operation, "previous_unloaded")
                result = run(
                    (
                        "/bin/launchctl",
                        "bootstrap",
                        self.domain,
                        str(self.plist_path),
                    ),
                    timeout_seconds=10,
                )
                if result.returncode:
                    raise SetupError("macOS could not load the sense sweep")
                operation = self._set_operation_phase(operation, "target_loaded")
                loaded_after, exact_after = self._loaded_state(
                    run,
                    SCHEDULER_LABEL,
                    expected_fingerprint=next_receipt.job_fingerprint,
                )
                if not loaded_after or not exact_after:
                    raise SetupError("macOS loaded a scheduler job with unexpected provenance")
                _unlink_if_exact(
                    self.operation_path,
                    expected=_operation_bytes(operation),
                    maximum=MAX_INSTALL_OPERATION_BYTES,
                    label="scheduler install operation",
                )
            except ConflictError:
                # A concurrent replacement is not ours to roll back.  Leave the
                # operation marker in place so the next operator can see the
                # interrupted transition without touching the foreign bytes.
                raise
            except Exception:
                try:
                    self._recover_install_operation(run)
                except Exception as rollback_exc:
                    raise SetupError(
                        "macOS scheduler update failed and rollback failed; repair is required"
                    ) from rollback_exc
                raise
            return SchedulerInstall(
                True,
                True,
                str(self.plist_path),
                sha256_bytes(next_encoded),
            )

    def uninstall(
        self,
        *,
        expected_revision: str,
        runner: Runner | None = None,
    ) -> SchedulerInstall:
        run = runner or _run
        with (
            exclusive_lock(self.lock_path),
            _pinned_scheduler_storage(
                self.operation_path,
                self.receipt_path,
                self.plist_path,
            ),
        ):
            self._recover_install_operation(run)
            receipt, revision, receipt_bytes = self._read_receipt()
            _expect_revision(revision, expected_revision)
            plist_bytes = _read_optional(
                self.plist_path,
                MAX_PLIST_BYTES,
                "scheduler plist",
            )
            identity = _owned_job_identity(
                receipt,
                plist_path=self.plist_path,
                plist_bytes=plist_bytes,
            )
            loaded, loaded_exact = self._loaded_identity_state(
                run,
                SCHEDULER_LABEL,
                identity,
            )
            if receipt is None:
                if plist_bytes is not None or loaded:
                    raise ConflictError("the scheduler label or plist is not owned by Seld")
                return SchedulerInstall(False, False, str(self.plist_path), revision)
            _require_owned_or_absent(
                receipt,
                plist_path=self.plist_path,
                plist_bytes=plist_bytes,
                loaded=loaded,
                loaded_exact=loaded_exact,
            )
            if loaded:
                result = run(
                    ("/bin/launchctl", "bootout", f"{self.domain}/{SCHEDULER_LABEL}"),
                    timeout_seconds=10,
                )
                if result.returncode:
                    raise SetupError("macOS could not unload the owned sense sweep")
                still_loaded, _ = self._loaded_output(run, SCHEDULER_LABEL)
                if still_loaded:
                    raise SetupError("macOS could not prove the owned sense sweep unloaded")
            if plist_bytes is not None:
                _unlink_if_exact(
                    self.plist_path,
                    expected=plist_bytes,
                    maximum=MAX_PLIST_BYTES,
                    label="scheduler plist",
                )
            assert receipt_bytes is not None
            _unlink_if_exact(
                self.receipt_path,
                expected=receipt_bytes,
                maximum=MAX_RECEIPT_BYTES,
                label="scheduler receipt",
            )
            return SchedulerInstall(
                True,
                False,
                str(self.plist_path),
                ABSENT_SCHEDULER_REVISION,
            )

    def run_canary(
        self,
        *,
        expected_revision: str,
        runner: Runner | None = None,
        timeout_seconds: float = 10,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> SchedulerCanary:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= float(timeout_seconds) <= 30
        ):
            raise ValidationError("scheduler canary timeout must be between 0.1 and 30 seconds")
        run = runner or _run
        with (
            exclusive_lock(self.lock_path),
            _pinned_scheduler_storage(
                self.operation_path,
                self.receipt_path,
                self.plist_path,
            ),
        ):
            self._recover_install_operation(run)
            _, revision, _ = self._read_receipt()
            _expect_revision(revision, expected_revision)
            status = self._status_unlocked(run)
            if not status.current or not status.installed or not status.loaded:
                return SchedulerCanary(
                    self.command,
                    0,
                    "scheduler_not_ready",
                    None,
                    None,
                    False,
                )
            return self._run_canary_unlocked(
                run,
                timeout_seconds=float(timeout_seconds),
                monotonic=monotonic,
                sleep=sleep,
            )

    def _run_canary_unlocked(
        self,
        run: Runner,
        *,
        timeout_seconds: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> SchedulerCanary:
        nonce = uuid.uuid4().hex
        canary_root = self.runtime_root / "canary"
        canary_root.mkdir(parents=True, exist_ok=True)
        if canary_root.is_symlink() or not canary_root.is_dir():
            raise ValidationError("scheduler canary directory must be an ordinary directory")
        if os.name != "nt":
            canary_root.chmod(0o700)
        plist_path = canary_root / f"{nonce}.plist"
        label = f"{SCHEDULER_LABEL}.canary.{nonce[:12]}"
        if os.path.lexists(plist_path):
            raise ConflictError("scheduler canary nonce already exists")
        atomic_write(
            plist_path,
            plistlib.dumps(
                {
                    "EnvironmentVariables": {
                        GSV_DATA_DIR_ENV: str(self.data_root),
                        PULSE_CANARY_ENV: nonce,
                        SCHEDULER_FINGERPRINT_ENV: self._job_fingerprint(),
                    },
                    "Label": label,
                    "ProcessType": "Standard",
                    "ProgramArguments": list(self.command),
                    "RunAtLoad": True,
                    "StandardErrorPath": "/dev/null",
                    "StandardOutPath": "/dev/null",
                },
                fmt=plistlib.FMT_XML,
                sort_keys=True,
            ),
            mode=0o600,
        )
        started_monotonic = monotonic()
        started_at = datetime.now(UTC)
        prior = heartbeat_status(self.vault_root)
        failure: str | None = None
        heartbeat_observed_at: str | None = None
        heartbeat_sequence: int | None = None
        proved = False
        try:
            result = run(
                ("/bin/launchctl", "bootstrap", self.domain, str(plist_path)),
                timeout_seconds=10,
            )
            if result.returncode:
                failure = "canary_bootstrap_failed"
            else:
                deadline = started_monotonic + float(timeout_seconds)
                while monotonic() < deadline:
                    current = heartbeat_status(self.vault_root)
                    if _heartbeat_advanced(
                        prior,
                        current,
                        started_at=started_at,
                        canary_nonce=nonce,
                    ):
                        assert current is not None
                        heartbeat_observed_at = str(current["observed_at"])
                        sequence = current["sequence"]
                        assert isinstance(sequence, int)
                        heartbeat_sequence = sequence
                        if current["status"] == "complete":
                            proved = True
                        else:
                            failure = "canary_sweep_failed"
                        break
                    sleep(0.02)
                else:
                    failure = "canary_missing"
        finally:
            run(
                ("/bin/launchctl", "bootout", f"{self.domain}/{label}"),
                timeout_seconds=10,
            )
            if not self._canary_job_absent(run, label):
                failure = "canary_cleanup_failed"
                proved = False
            elif os.path.lexists(plist_path):
                durable_unlink(plist_path)
        duration_ms = max(0, int((monotonic() - started_monotonic) * 1_000))
        return SchedulerCanary(
            self.command,
            duration_ms,
            failure,
            heartbeat_observed_at,
            heartbeat_sequence,
            proved,
        )

    def _status_unlocked(self, run: Runner) -> SchedulerStatus:
        if os.path.lexists(self.operation_path):
            operation = self._read_install_operation()
            prior = _operation_identity(operation, prior=True)
            target = _operation_identity(operation, prior=False)
            assert target is not None
            loaded, output = self._loaded_output(run, SCHEDULER_LABEL)
            recognized = bool(
                loaded
                and (
                    (prior is not None and _launchctl_matches_identity(output, prior))
                    or _launchctl_matches_identity(output, target)
                )
            )
            return SchedulerStatus(
                current=False,
                foreign=loaded and not recognized,
                installed=False,
                label=SCHEDULER_LABEL,
                loaded=loaded,
                plist=str(self.plist_path),
                receipt_revision=_operation_prior_revision(operation),
                stale=True,
            )
        receipt, revision, _ = self._read_receipt()
        plist_bytes = _read_optional(self.plist_path, MAX_PLIST_BYTES, "scheduler plist")
        identity = _owned_job_identity(
            receipt,
            plist_path=self.plist_path,
            plist_bytes=plist_bytes,
        )
        loaded, loaded_exact = self._loaded_identity_state(
            run,
            SCHEDULER_LABEL,
            identity,
        )
        exact = bool(
            receipt is not None
            and receipt.label == SCHEDULER_LABEL
            and receipt.plist == str(self.plist_path)
            and plist_bytes is not None
            and sha256_bytes(plist_bytes) == receipt.plist_digest
            and identity is not None
        )
        planned_digest = self._plan_digest(sha256_bytes(self._plist_bytes()))
        current = bool(
            exact and receipt is not None and receipt.plan_digest == planned_digest and loaded_exact
        )
        foreign = bool(
            (plist_bytes is not None and not exact) or (loaded and (not exact or not loaded_exact))
        )
        stale = bool(receipt is not None and (not exact or not current or not loaded_exact))
        return SchedulerStatus(
            current=current and loaded,
            foreign=foreign,
            installed=exact,
            label=SCHEDULER_LABEL,
            loaded=loaded,
            plist=str(self.plist_path),
            receipt_revision=revision,
            stale=stale,
        )

    def _loaded_state(
        self,
        run: Runner,
        label: str,
        *,
        expected_fingerprint: str | None,
    ) -> tuple[bool, bool]:
        identity = (
            _JobIdentity(
                self.command,
                str(self.data_root),
                expected_fingerprint,
                self.interval_seconds,
            )
            if expected_fingerprint is not None
            else None
        )
        return self._loaded_identity_state(run, label, identity)

    def _loaded_identity_state(
        self,
        run: Runner,
        label: str,
        identity: _JobIdentity | None,
    ) -> tuple[bool, bool]:
        loaded, output = self._loaded_output(run, label)
        if not loaded:
            return False, False
        if identity is None:
            return True, False
        return True, _launchctl_matches_identity(output, identity)

    def _loaded_output(self, run: Runner, label: str) -> tuple[bool, str]:
        result = run(
            ("/bin/launchctl", "print", f"{self.domain}/{label}"),
            timeout_seconds=5,
        )
        if result.returncode == 0:
            return True, result.stdout
        if result.returncode == _LAUNCHCTL_SERVICE_NOT_FOUND:
            return False, ""
        raise SetupError("macOS could not inspect the sense sweep")

    def _canary_job_absent(self, run: Runner, label: str) -> bool:
        result = run(
            ("/bin/launchctl", "print", f"{self.domain}/{label}"),
            timeout_seconds=5,
        )
        return result.returncode == _LAUNCHCTL_SERVICE_NOT_FOUND

    def _operation_prior_revision(self) -> str:
        if os.path.lexists(self.operation_path):
            return _operation_prior_revision(self._read_install_operation())
        _, revision, _ = self._read_receipt()
        return revision

    def _read_install_operation(self) -> _InstallOperation:
        encoded = _read_optional(
            self.operation_path,
            MAX_INSTALL_OPERATION_BYTES,
            "scheduler install operation",
        )
        if encoded is None:
            raise ValidationError("scheduler install operation is missing")
        return _parse_install_operation(encoded, plist_path=self.plist_path)

    def _set_operation_phase(
        self,
        operation: _InstallOperation,
        phase: str,
    ) -> _InstallOperation:
        updated = replace(operation, phase=phase)
        _cas_replace_file(
            self.operation_path,
            expected=_operation_bytes(operation),
            replacement=_operation_bytes(updated),
            maximum=MAX_INSTALL_OPERATION_BYTES,
            label="scheduler install operation",
        )
        return updated

    def _recover_install_operation(self, run: Runner) -> None:
        operation_bytes = _read_optional(
            self.operation_path,
            MAX_INSTALL_OPERATION_BYTES,
            "scheduler install operation",
        )
        if operation_bytes is None:
            return
        operation = _parse_install_operation(operation_bytes, plist_path=self.plist_path)
        prior_plist = _decoded_bytes(
            operation.prior_plist,
            maximum=MAX_PLIST_BYTES,
            label="prior scheduler plist",
        )
        prior_receipt = _decoded_bytes(
            operation.prior_receipt,
            maximum=MAX_RECEIPT_BYTES,
            label="prior scheduler receipt",
        )
        target_plist = _decoded_bytes(
            operation.target_plist,
            maximum=MAX_PLIST_BYTES,
            label="target scheduler plist",
        )
        target_receipt = _decoded_bytes(
            operation.target_receipt,
            maximum=MAX_RECEIPT_BYTES,
            label="target scheduler receipt",
        )
        assert target_plist is not None and target_receipt is not None
        _require_operation_file_state(
            self.plist_path,
            prior=prior_plist,
            target=target_plist,
            maximum=MAX_PLIST_BYTES,
            label="scheduler plist",
        )
        _require_operation_file_state(
            self.receipt_path,
            prior=prior_receipt,
            target=target_receipt,
            maximum=MAX_RECEIPT_BYTES,
            label="scheduler receipt",
        )
        prior = _operation_identity(operation, prior=True)
        target = _operation_identity(operation, prior=False)
        assert target is not None
        loaded, output = self._loaded_output(run, SCHEDULER_LABEL)
        prior_loaded = bool(
            loaded and prior is not None and _launchctl_matches_identity(output, prior)
        )
        target_loaded = bool(loaded and _launchctl_matches_identity(output, target))
        if loaded and not prior_loaded and not target_loaded:
            raise ConflictError(
                "the interrupted scheduler operation found a foreign loaded job and preserved it"
            )
        if target_loaded and not prior_loaded:
            result = run(
                ("/bin/launchctl", "bootout", f"{self.domain}/{SCHEDULER_LABEL}"),
                timeout_seconds=10,
            )
            if result.returncode:
                raise SetupError("macOS could not unload the interrupted scheduler target")
            still_loaded, _ = self._loaded_output(run, SCHEDULER_LABEL)
            if still_loaded:
                raise SetupError("macOS scheduler rollback could not prove target unload")
            prior_loaded = False
        _restore_operation_file(
            self.plist_path,
            prior=prior_plist,
            target=target_plist,
            maximum=MAX_PLIST_BYTES,
            label="scheduler plist",
            mode=0o644,
        )
        _restore_operation_file(
            self.receipt_path,
            prior=prior_receipt,
            target=target_receipt,
            maximum=MAX_RECEIPT_BYTES,
            label="scheduler receipt",
        )
        if prior is not None and not prior_loaded:
            restored = run(
                (
                    "/bin/launchctl",
                    "bootstrap",
                    self.domain,
                    str(self.plist_path),
                ),
                timeout_seconds=10,
            )
            if restored.returncode:
                raise SetupError("macOS scheduler rollback could not restore the previous job")
            loaded, output = self._loaded_output(run, SCHEDULER_LABEL)
            prior_loaded = loaded and _launchctl_matches_identity(output, prior)
        if prior is not None and not prior_loaded:
            raise SetupError("macOS scheduler rollback could not prove the previous job")
        if prior is None:
            loaded, _ = self._loaded_output(run, SCHEDULER_LABEL)
            if loaded:
                raise SetupError("macOS scheduler rollback found an unexpected loaded job")
        _unlink_if_exact(
            self.operation_path,
            expected=_operation_bytes(operation),
            maximum=MAX_INSTALL_OPERATION_BYTES,
            label="scheduler install operation",
        )

    def _prepare_directories(self) -> None:
        for path in (self.launch_agents_dir, self.runtime_root):
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or not path.is_dir():
                raise ValidationError("scheduler storage must use ordinary directories")
        if os.name != "nt":
            self.runtime_root.chmod(0o700)

    def _plist_bytes(self) -> bytes:
        return plistlib.dumps(
            {
                "EnvironmentVariables": {
                    GSV_DATA_DIR_ENV: str(self.data_root),
                    SCHEDULER_FINGERPRINT_ENV: self._job_fingerprint(),
                },
                "Label": SCHEDULER_LABEL,
                "ProcessType": "Standard",
                "ProgramArguments": list(self.command),
                "RunAtLoad": True,
                "StandardErrorPath": "/dev/null",
                "StandardOutPath": "/dev/null",
                "StartInterval": self.interval_seconds,
            },
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )

    def _plan_digest(self, plist_digest: str) -> str:
        return _plan_digest_for(
            interval_seconds=self.interval_seconds,
            plist_path=self.plist_path,
            plist_digest=plist_digest,
        )

    def _job_fingerprint(self) -> str:
        return _job_fingerprint_for(
            command=self.command,
            data_root=str(self.data_root),
            interval_seconds=self.interval_seconds,
            plist_path=self.plist_path,
        )

    def _read_receipt(self) -> tuple[_Receipt | None, str, bytes | None]:
        encoded = _read_optional(
            self.receipt_path,
            MAX_RECEIPT_BYTES,
            "scheduler receipt",
        )
        if encoded is None:
            return None, ABSENT_SCHEDULER_REVISION, None
        try:
            payload = json.loads(encoded)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("scheduler receipt is invalid") from exc
        receipt = _parse_receipt(payload)
        return receipt, sha256_bytes(encoded), encoded


def scheduler_dict(
    value: SchedulerPlan | SchedulerStatus | SchedulerInstall | SchedulerCanary,
) -> dict[str, object]:
    return asdict(value)


def _receipt_bytes(receipt: _Receipt) -> bytes:
    return (json.dumps(asdict(receipt), separators=(",", ":"), sort_keys=True) + "\n").encode()


def _operation_bytes(operation: _InstallOperation) -> bytes:
    encoded = (json.dumps(asdict(operation), separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_INSTALL_OPERATION_BYTES:
        raise ValidationError("scheduler install operation is too large")
    return encoded


def _encoded_bytes(value: bytes | None) -> str | None:
    return None if value is None else base64.b64encode(value).decode("ascii")


def _encoded_required(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decoded_bytes(value: str | None, *, maximum: int, label: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} encoding is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"{label} encoding is invalid") from exc
    if len(decoded) > maximum:
        raise ValidationError(f"{label} is too large")
    return decoded


def _parse_install_operation(encoded: bytes, *, plist_path: Path) -> _InstallOperation:
    try:
        value = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("scheduler install operation is invalid") from exc
    keys = {
        "format_version",
        "label",
        "phase",
        "plist",
        "prior_fingerprint",
        "prior_plist",
        "prior_receipt",
        "target_fingerprint",
        "target_plist",
        "target_receipt",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError("scheduler install operation has an unsupported shape")
    if value["format_version"] != INSTALL_OPERATION_FORMAT_VERSION:
        raise ValidationError("scheduler install operation has an unsupported version")
    if value["label"] != SCHEDULER_LABEL or value["plist"] != str(plist_path):
        raise ValidationError("scheduler install operation has an unexpected target")
    phases = {
        "prepared",
        "plist_written",
        "authority_published",
        "previous_unloaded",
        "target_loaded",
    }
    if value["phase"] not in phases:
        raise ValidationError("scheduler install operation phase is invalid")
    for key in ("prior_fingerprint", "target_fingerprint"):
        fingerprint = value[key]
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            raise ValidationError("scheduler install operation fingerprint is invalid")
    for key in ("prior_plist", "prior_receipt", "target_plist", "target_receipt"):
        if value[key] is not None and not isinstance(value[key], str):
            raise ValidationError("scheduler install operation payload is invalid")
    operation = _InstallOperation(
        format_version=INSTALL_OPERATION_FORMAT_VERSION,
        label=SCHEDULER_LABEL,
        phase=str(value["phase"]),
        plist=str(plist_path),
        prior_fingerprint=value["prior_fingerprint"],
        prior_plist=value["prior_plist"],
        prior_receipt=value["prior_receipt"],
        target_fingerprint=str(value["target_fingerprint"]),
        target_plist=str(value["target_plist"]),
        target_receipt=str(value["target_receipt"]),
    )
    _validate_operation_payload(operation, plist_path=plist_path)
    return operation


def _validate_operation_payload(operation: _InstallOperation, *, plist_path: Path) -> None:
    prior_plist = _decoded_bytes(
        operation.prior_plist,
        maximum=MAX_PLIST_BYTES,
        label="prior scheduler plist",
    )
    prior_receipt = _decoded_bytes(
        operation.prior_receipt,
        maximum=MAX_RECEIPT_BYTES,
        label="prior scheduler receipt",
    )
    target_plist = _decoded_bytes(
        operation.target_plist,
        maximum=MAX_PLIST_BYTES,
        label="target scheduler plist",
    )
    target_receipt = _decoded_bytes(
        operation.target_receipt,
        maximum=MAX_RECEIPT_BYTES,
        label="target scheduler receipt",
    )
    if target_plist is None or target_receipt is None:
        raise ValidationError("scheduler install operation target is incomplete")
    _validate_operation_pair(
        target_plist,
        target_receipt,
        fingerprint=operation.target_fingerprint,
        plist_path=plist_path,
    )
    if prior_receipt is None:
        if prior_plist is not None or operation.prior_fingerprint is not None:
            raise ValidationError("scheduler install operation prior state is inconsistent")
        return
    receipt = _receipt_from_bytes(prior_receipt)
    if receipt.job_fingerprint != operation.prior_fingerprint:
        raise ValidationError("scheduler install operation prior fingerprint changed")
    if prior_plist is not None:
        _validate_operation_pair(
            prior_plist,
            prior_receipt,
            fingerprint=receipt.job_fingerprint,
            plist_path=plist_path,
        )


def _validate_operation_pair(
    plist_bytes: bytes,
    receipt_bytes: bytes,
    *,
    fingerprint: str,
    plist_path: Path,
) -> None:
    receipt = _receipt_from_bytes(receipt_bytes)
    identity = _job_identity_from_plist(plist_bytes, plist_path=plist_path)
    plist_digest = sha256_bytes(plist_bytes)
    plan_digest = _plan_digest_for(
        interval_seconds=identity.interval_seconds,
        plist_path=plist_path,
        plist_digest=plist_digest,
    )
    if (
        receipt.plist != str(plist_path)
        or receipt.plist_digest != plist_digest
        or receipt.plan_digest != plan_digest
        or receipt.interval_seconds != identity.interval_seconds
        or receipt.job_fingerprint != fingerprint
        or identity.fingerprint != fingerprint
    ):
        raise ValidationError("scheduler install operation authority does not match its plist")


def _receipt_from_bytes(encoded: bytes) -> _Receipt:
    try:
        value = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("scheduler install operation receipt is invalid") from exc
    return _parse_receipt(value)


def _operation_identity(operation: _InstallOperation, *, prior: bool) -> _JobIdentity | None:
    encoded = _decoded_bytes(
        operation.prior_plist if prior else operation.target_plist,
        maximum=MAX_PLIST_BYTES,
        label="prior scheduler plist" if prior else "target scheduler plist",
    )
    if encoded is None:
        return None
    return _job_identity_from_plist(encoded, plist_path=Path(operation.plist))


def _operation_prior_revision(operation: _InstallOperation) -> str:
    encoded = _decoded_bytes(
        operation.prior_receipt,
        maximum=MAX_RECEIPT_BYTES,
        label="prior scheduler receipt",
    )
    return ABSENT_SCHEDULER_REVISION if encoded is None else sha256_bytes(encoded)


def _job_identity_from_plist(encoded: bytes, *, plist_path: Path) -> _JobIdentity:
    try:
        value = plistlib.loads(encoded)
    except (ValueError, TypeError) as exc:
        raise ValidationError("scheduler plist is invalid") from exc
    if not isinstance(value, dict) or value.get("Label") != SCHEDULER_LABEL:
        raise ValidationError("scheduler plist has an unexpected label")
    arguments = value.get("ProgramArguments")
    environment = value.get("EnvironmentVariables")
    interval = value.get("StartInterval")
    if (
        not isinstance(arguments, list)
        or not arguments
        or len(arguments) > 64
        or any(not isinstance(argument, str) or not argument for argument in arguments)
        or not isinstance(environment, dict)
        or set(environment) != {GSV_DATA_DIR_ENV, SCHEDULER_FINGERPRINT_ENV}
        or not isinstance(interval, int)
        or isinstance(interval, bool)
        or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS
        or value.get("RunAtLoad") is not True
    ):
        raise ValidationError("scheduler plist has an unsupported job shape")
    fingerprint = environment.get(SCHEDULER_FINGERPRINT_ENV)
    data_root = environment.get(GSV_DATA_DIR_ENV)
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or not isinstance(data_root, str)
        or not Path(data_root).is_absolute()
    ):
        raise ValidationError("scheduler plist environment binding is invalid")
    command = tuple(arguments)
    expected = _job_fingerprint_for(
        command=command,
        data_root=data_root,
        interval_seconds=interval,
        plist_path=plist_path,
    )
    if fingerprint != expected:
        raise ValidationError("scheduler plist fingerprint does not match its job")
    return _JobIdentity(command, data_root, fingerprint, interval)


def _parse_receipt(value: object) -> _Receipt:
    keys = {
        "format_version",
        "interval_seconds",
        "job_fingerprint",
        "label",
        "plan_digest",
        "plist",
        "plist_digest",
        "updated_at",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError("scheduler receipt has an unsupported shape")
    if value["format_version"] != SCHEDULER_FORMAT_VERSION:
        raise ValidationError("scheduler receipt has an unsupported version")
    if value["label"] != SCHEDULER_LABEL:
        raise ValidationError("scheduler receipt has an unexpected label")
    interval = value["interval_seconds"]
    if not isinstance(interval, int) or isinstance(interval, bool):
        raise ValidationError("scheduler receipt interval is invalid")
    for key in ("job_fingerprint", "plan_digest", "plist_digest"):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise ValidationError("scheduler receipt digest is invalid")
    if not isinstance(value["plist"], str) or not Path(value["plist"]).is_absolute():
        raise ValidationError("scheduler receipt plist path is invalid")
    if not isinstance(value["updated_at"], str):
        raise ValidationError("scheduler receipt timestamp is invalid")
    parse_time(value["updated_at"])
    return _Receipt(
        SCHEDULER_FORMAT_VERSION,
        interval,
        value["job_fingerprint"],
        SCHEDULER_LABEL,
        value["plan_digest"],
        value["plist"],
        value["plist_digest"],
        value["updated_at"],
    )


def _require_owned_or_absent(
    receipt: _Receipt | None,
    *,
    plist_path: Path,
    plist_bytes: bytes | None,
    loaded: bool,
    loaded_exact: bool,
) -> None:
    if receipt is None:
        if plist_bytes is not None or loaded:
            raise ConflictError("the scheduler label or plist is not owned by Seld")
        return
    if receipt.plist != str(plist_path):
        raise ConflictError("the scheduler receipt points to another plist")
    if plist_bytes is None:
        if loaded:
            raise ConflictError("the loaded scheduler job cannot be verified without its plist")
        return
    if sha256_bytes(plist_bytes) != receipt.plist_digest:
        raise ConflictError("the scheduler plist changed outside Seld and was preserved")
    if loaded and not loaded_exact:
        raise ConflictError("the loaded scheduler job does not match Seld's owned provenance")


def _owned_job_identity(
    receipt: _Receipt | None,
    *,
    plist_path: Path,
    plist_bytes: bytes | None,
) -> _JobIdentity | None:
    if receipt is None or plist_bytes is None or receipt.plist != str(plist_path):
        return None
    try:
        identity = _job_identity_from_plist(plist_bytes, plist_path=plist_path)
    except ValidationError:
        return None
    digest = sha256_bytes(plist_bytes)
    if (
        receipt.plist_digest != digest
        or receipt.job_fingerprint != identity.fingerprint
        or receipt.interval_seconds != identity.interval_seconds
        or receipt.plan_digest
        != _plan_digest_for(
            interval_seconds=identity.interval_seconds,
            plist_path=plist_path,
            plist_digest=digest,
        )
    ):
        return None
    return identity


def _expect_revision(current: str, expected: str) -> None:
    if not isinstance(expected, str) or current != expected:
        raise ConflictError("scheduler receipt changed; reload before retrying")


_ACTIVE_SCHEDULER_STORES: ContextVar[dict[Path, PinnedPathRoot] | None] = ContextVar(
    "continuity_kernel_active_scheduler_stores",
    default=None,
)


@contextmanager
def _pinned_scheduler_storage(
    *paths: Path,
) -> Iterator[dict[Path, PinnedPathRoot]]:
    """Hold one pinned root per scheduler storage tree for a whole mutation."""

    stores: dict[Path, PinnedPathRoot] = {}
    token = None
    try:
        for path in paths:
            root = Path(os.path.abspath(path.parent.parent))
            if root not in stores:
                stores[root] = PinnedPathRoot(root)
        token = _ACTIVE_SCHEDULER_STORES.set(stores)
        yield stores
    finally:
        active_error = sys.exc_info()[0] is not None
        close_error: Exception | None = None
        if token is not None:
            _ACTIVE_SCHEDULER_STORES.reset(token)
        for store in reversed(tuple(stores.values())):
            try:
                store.close()
            except Exception as exc:
                if close_error is None:
                    close_error = exc
        if close_error is not None and not active_error:
            raise close_error


def _scheduler_store(
    stores: dict[Path, PinnedPathRoot],
    path: Path,
) -> PinnedPathRoot:
    root = Path(os.path.abspath(path.parent.parent))
    try:
        return stores[root]
    except KeyError as exc:
        raise ValidationError("scheduler path has no active pinned storage root") from exc


def _active_scheduler_store(path: Path) -> PinnedPathRoot | None:
    stores = _ACTIVE_SCHEDULER_STORES.get()
    if stores is None:
        return None
    return _scheduler_store(stores, path)


def _read_optional(
    path: Path,
    maximum: int,
    label: str,
    *,
    store: PinnedPathRoot | None = None,
) -> bytes | None:
    if store is None:
        store = _active_scheduler_store(path)
    if store is not None:
        return store.read_regular_file(
            path.relative_to(store.root),
            label=label,
            max_bytes=maximum,
            missing_ok=True,
            retain=True,
        )
    if not os.path.lexists(path):
        return None
    return read_regular_file(path, label=label, max_bytes=maximum)


def _cas_replace_file(
    path: Path,
    *,
    expected: bytes | None,
    replacement: bytes,
    maximum: int,
    label: str,
    mode: int = 0o600,
    store: PinnedPathRoot | None = None,
) -> None:
    """Replace beneath one pinned parent without clobbering a foreign leaf."""

    active_store = store or _active_scheduler_store(path)
    owns_store = active_store is None
    if active_store is None:
        active_store = PinnedPathRoot(path.parent.parent)
    relative = path.relative_to(active_store.root)
    try:
        with active_store.bind_directory(relative.parent):
            try:
                active_store.replace_regular_file_if_exact(
                    relative,
                    expected=expected,
                    replacement=replacement,
                    label=label,
                    max_bytes=maximum,
                    mode=mode,
                )
            except DurablePublishError as exc:
                _raise_scheduler_publish_error(exc, label=label)
    finally:
        if owns_store:
            active_store.close()


def _unlink_if_exact(
    path: Path,
    *,
    expected: bytes,
    maximum: int,
    label: str,
    store: PinnedPathRoot | None = None,
) -> None:
    """Remove only exact owned bytes beneath one pinned parent."""

    active_store = store or _active_scheduler_store(path)
    owns_store = active_store is None
    if active_store is None:
        active_store = PinnedPathRoot(path.parent.parent)
    relative = path.relative_to(active_store.root)
    try:
        with active_store.bind_directory(relative.parent):
            try:
                active_store.unlink_regular_file_if_exact(
                    relative,
                    expected=expected,
                    label=label,
                    max_bytes=maximum,
                )
            except DurablePublishError as exc:
                _raise_scheduler_publish_error(exc, label=label)
    finally:
        if owns_store:
            active_store.close()


def _raise_scheduler_publish_error(error: DurablePublishError, *, label: str) -> None:
    if error.outcome is PublishOutcome.UNPUBLISHED:
        raise PersistenceError(f"{label} was not published") from error
    if error.outcome is PublishOutcome.COMMITTED:
        raise MutationCommittedError(
            f"{label} committed, but scheduler cleanup is unconfirmed"
        ) from error
    raise DegradedIntegrityError(f"{label} has an unknown scheduler storage state") from error


def _restore_operation_file(
    path: Path,
    *,
    prior: bytes | None,
    target: bytes,
    maximum: int,
    label: str,
    mode: int = 0o600,
    store: PinnedPathRoot | None = None,
) -> None:
    """Restore an interrupted operation only from its exact prior/target bytes."""

    current = _read_optional(path, maximum, label, store=store)
    if current == prior:
        return
    if current != target:
        raise ConflictError(
            f"the interrupted scheduler operation found a foreign {label} and preserved it"
        )
    if prior is None:
        _unlink_if_exact(
            path,
            expected=target,
            maximum=maximum,
            label=label,
        )
        return
    _cas_replace_file(
        path,
        expected=target,
        replacement=prior,
        maximum=maximum,
        label=label,
        mode=mode,
    )


def _require_operation_file_state(
    path: Path,
    *,
    prior: bytes | None,
    target: bytes,
    maximum: int,
    label: str,
    store: PinnedPathRoot | None = None,
) -> None:
    current = _read_optional(path, maximum, label, store=store)
    if current in (target, prior):
        return
    raise ConflictError(
        f"the interrupted scheduler operation found a foreign {label} and preserved it"
    )


def _plan_digest_for(
    *,
    interval_seconds: int,
    plist_path: Path,
    plist_digest: str,
) -> str:
    encoded = json.dumps(
        {
            "interval_seconds": interval_seconds,
            "label": SCHEDULER_LABEL,
            "plist": str(plist_path),
            "plist_digest": plist_digest,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256_bytes(encoded)


def _job_fingerprint_for(
    *,
    command: tuple[str, ...],
    data_root: str,
    interval_seconds: int,
    plist_path: Path,
) -> str:
    return sha256_bytes(
        json.dumps(
            {
                "command": command,
                "data_root": data_root,
                "interval_seconds": interval_seconds,
                "label": SCHEDULER_LABEL,
                "plist": str(plist_path),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _command(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values or len(values) > 64:
        raise ValidationError("scheduler command must contain 1 to 64 arguments")
    command = tuple(str(value) for value in values)
    if any(
        not value or len(value.encode("utf-8")) > 4_096 or "\x00" in value or "\n" in value
        for value in command
    ):
        raise ValidationError("scheduler command contains an invalid argument")
    executable = Path(command[0]).expanduser()
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise ValidationError("scheduler command requires an exact executable path")
    return (str(executable), *command[1:])


def _require_sweep_command(command: tuple[str, ...], *, vault_root: Path) -> None:
    if command[-3:] != ("--json", "pulse", "sweep"):
        raise ValidationError("scheduler command must run the JSON mechanical Pulse sweep")
    positions = [index for index, value in enumerate(command[:-1]) if value == "--vault"]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise ValidationError("scheduler command must bind one exact vault")
    if command[positions[0] + 1] != str(vault_root):
        raise ValidationError("scheduler command vault does not match its proof binding")


def _launchctl_matches(
    output: str,
    *,
    expected_fingerprint: str,
    expected_command: tuple[str, ...],
    expected_data_root: str,
    expected_interval_seconds: int,
) -> bool:
    if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_LAUNCHCTL_OUTPUT_BYTES:
        return False
    fingerprint_pattern = re.compile(
        rf"(?m)^\s*{re.escape(SCHEDULER_FINGERPRINT_ENV)}\s*(?:=>|=)\s*"
        rf"{re.escape(expected_fingerprint)}\s*$"
    )
    if fingerprint_pattern.search(output) is None:
        return False
    data_root_pattern = re.compile(
        rf"(?m)^\s*{re.escape(GSV_DATA_DIR_ENV)}\s*(?:=>|=)\s*"
        rf"{re.escape(expected_data_root)}\s*$"
    )
    if data_root_pattern.search(output) is None:
        return False
    block = re.search(r"(?ms)^\s*arguments\s*=\s*\{\s*\n(.*?)^\s*\}\s*$", output)
    if block is None:
        return False
    observed: list[str] = []
    for raw_line in block.group(1).splitlines():
        value = raw_line.strip()
        if not value:
            continue
        indexed = re.fullmatch(r"\d+\s*(?:=>|=)\s*(.*)", value)
        observed.append(indexed.group(1) if indexed is not None else value)
    if tuple(observed) != expected_command:
        return False
    interval = re.search(r"(?m)^\s*run interval\s*=\s*(\d+)\s+seconds\s*$", output)
    if interval is None or int(interval.group(1)) != expected_interval_seconds:
        return False
    run_at_load = re.search(
        r"(?mi)^\s*properties\s*=\s*[^\n]*\brunatload\b[^\n]*$",
        output,
    )
    return run_at_load is not None


def _launchctl_matches_identity(output: str, identity: _JobIdentity) -> bool:
    return _launchctl_matches(
        output,
        expected_fingerprint=identity.fingerprint,
        expected_command=identity.command,
        expected_data_root=identity.data_root,
        expected_interval_seconds=identity.interval_seconds,
    )


def _absolute(value: Path | str, label: str) -> Path:
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError(f"invalid {label}") from exc


def _heartbeat_advanced(
    previous: dict[str, object] | None,
    current: dict[str, object] | None,
    *,
    started_at: datetime,
    canary_nonce: str,
) -> bool:
    if current is None:
        return False
    if current.get("canary_nonce") != canary_nonce:
        return False
    sequence = current.get("sequence")
    observed_at = current.get("observed_at")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not isinstance(observed_at, str)
    ):
        return False
    prior_sequence = previous.get("sequence") if previous else 0
    if not isinstance(prior_sequence, int) or isinstance(prior_sequence, bool):
        return False
    try:
        observed = parse_time(observed_at)
    except ValidationError:
        return False
    return sequence > prior_sequence and observed >= started_at


def _run(command: Sequence[str], *, timeout_seconds: float) -> RunnerResult:
    try:
        process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return RunnerResult(127)
    stdout = process.stdout
    assert stdout is not None
    chunks: list[bytes] = []
    total = 0
    overflow = threading.Event()

    def drain() -> None:
        nonlocal total
        while True:
            chunk = stdout.read(8 * 1024)
            if not chunk:
                return
            remaining = max(0, MAX_LAUNCHCTL_OUTPUT_BYTES - total)
            if remaining:
                chunks.append(chunk[:remaining])
                total += min(remaining, len(chunk))
            if len(chunk) > remaining:
                overflow.set()
                return

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None and not overflow.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
    reader.join(timeout=1)
    if reader.is_alive() or overflow.is_set():
        return RunnerResult(process.returncode if process.returncode is not None else 127)
    output = b"".join(chunks)
    try:
        decoded = output.decode("utf-8")
    except UnicodeDecodeError:
        decoded = ""
    return RunnerResult(process.returncode if process.returncode is not None else 127, decoded)
