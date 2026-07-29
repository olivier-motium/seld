"""Bounded, approval-gated source updates for an installed Seld tool."""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any, Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from continuity_kernel import __version__
from continuity_kernel.atomic import (
    atomic_write,
    durable_publish_new,
    exclusive_lock,
    move_no_replace,
    read_regular_file,
    sha256_bytes,
)
from continuity_kernel.config import data_dir
from continuity_kernel.control_queue import CONTROL_STORE_SUPPORTED
from continuity_kernel.errors import ConflictError, SetupError, ValidationError
from continuity_kernel.vault import Vault

REPOSITORY_URL: Final = "https://github.com/olivier-motium/seld.git"
API_ROOT: Final = "https://api.github.com/repos/olivier-motium/seld"
CHECK_FORMAT_VERSION: Final = 1
TRANSACTION_FORMAT_VERSION: Final = 1
CHECK_INTERVAL: Final = timedelta(hours=6)
NETWORK_TIMEOUT_SECONDS: Final = 8.0
MAX_NETWORK_BYTES: Final = 2 * 1024 * 1024
MAX_RECEIPT_BYTES: Final = 128 * 1024
MAX_RECOVERY_LAUNCHER_BYTES: Final = 16 * 1024
COMMAND_TIMEOUT_SECONDS: Final = 10 * 60
MAX_COMMAND_OUTPUT_BYTES: Final = 256 * 1024
UPDATE_LOCK_TIMEOUT_SECONDS: Final = 0.25
RECOVERY_LAUNCHER_NAME: Final = "seld-recover"
REQUIRED_CHECK_NAMES: Final = frozenset(
    {
        "Chromium Bridge acceptance",
        "Python 3.11 on macos-15",
        "Python 3.11 on ubuntu-24.04",
        "Python 3.11 on windows-2025",
        "Python 3.12 on macos-15",
        "Python 3.12 on ubuntu-24.04",
        "Python 3.12 on windows-2025",
        "Python distributions",
    }
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_REVISION = re.compile(r"^[0-9a-f]{64}$")
_APPROVAL_REF = re.compile(
    r"^codex:[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TERMINAL_OUTCOMES = frozenset(
    {
        "installed",
        "installed_bridge_repair",
        "rolled_back",
        "repair_required",
    }
)
_TRANSACTION_PHASES = frozenset(
    {
        "preflighting",
        "backing_up",
        "backed_up",
        "prepared",
        "stopping_bridge",
        "bridge_stopped",
        "preserving_previous",
        "previous_preserved",
        "candidate_installed",
        "setting_up_candidate",
        "verifying_candidate",
        "recovering",
        "restoring_previous",
        "previous_restored",
        "complete",
    }
)


@dataclass(frozen=True)
class InstallProvenance:
    supported: bool
    install_mode: str
    version: str
    sha: str | None = None
    repository: str | None = None
    environment: str | None = None
    launcher: str | None = None
    tool_dir: str | None = None
    bin_dir: str | None = None
    python: str | None = None
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


CommandRunner = Callable[[list[str], Mapping[str, str], float], CommandResult]
JsonFetcher = Callable[[str], tuple[dict[str, Any], Mapping[str, str]]]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        del file_pointer, message, headers, new_url
        raise HTTPError(request.full_url, code, "redirect refused", Message(), None)


class _ActivationVaultChanged(SetupError):
    """Canonical bytes moved after activation acquired its exact anchor."""


def status() -> dict[str, Any]:
    """Return current provenance plus only host-cached update evidence."""

    installed = installed_provenance()
    cached = _read_receipt(_check_path(), label="Seld update check")
    transaction = _read_receipt(_transaction_path(), label="Seld update transaction")
    state = "unchecked"
    candidate: dict[str, Any] | None = None
    checked_at: str | None = None
    error_code: str | None = None
    check_revision: str | None = None
    if cached is not None:
        _validate_check_receipt(cached)
        state = str(cached["state"])
        candidate_value = cached.get("candidate")
        candidate = dict(candidate_value) if isinstance(candidate_value, dict) else None
        checked_at = str(cached["checked_at"])
        error_value = cached.get("error_code")
        error_code = str(error_value) if isinstance(error_value, str) else None
        check_revision = _receipt_revision(cached)
        if installed.sha is not None and cached.get("from_sha") != installed.sha:
            if candidate is not None and candidate.get("sha") == installed.sha:
                state = "current"
                candidate = None
                error_code = None
            else:
                state = "stale"
    if not installed.supported:
        state = "unsupported"
        error_code = installed.reason_code
    transaction_payload = _transaction_summary(transaction)
    if transaction is not None and not _transaction_resolved(transaction):
        state = "interrupted"
    return {
        "format_version": 1,
        "state": state,
        "installed": installed.to_dict(),
        "candidate": candidate,
        "checked_at": checked_at,
        "check_revision": check_revision,
        "error_code": error_code,
        "next_check_after": _next_check_after(cached),
        "transaction": transaction_payload,
    }


def check(*, force: bool = False, fetcher: JsonFetcher | None = None) -> dict[str, Any]:
    """Resolve public main into one exact reviewed SHA and cache the result."""

    installed = installed_provenance()
    if not installed.supported or installed.sha is None:
        return status()
    try:
        with _update_lock():
            cached = _read_receipt(_check_path(), label="Seld update check")
            if not force and not _check_due(cached):
                return status()
            checked_at = _now()
            try:
                payload = _resolve_public_main(installed.sha, fetcher=fetcher or _fetch_json)
                receipt: dict[str, Any] = {
                    "format_version": CHECK_FORMAT_VERSION,
                    "checked_at": checked_at,
                    "from_sha": installed.sha,
                    **payload,
                }
            except (HTTPError, URLError, TimeoutError, OSError, ValidationError) as exc:
                receipt = {
                    "format_version": CHECK_FORMAT_VERSION,
                    "checked_at": checked_at,
                    "from_sha": installed.sha,
                    "state": "unavailable",
                    "candidate": None,
                    "error_code": _network_error_code(exc),
                }
                if cached is not None:
                    try:
                        _validate_check_receipt(cached)
                    except ValidationError:
                        pass
                    else:
                        prior_candidate = cached.get("candidate")
                        if isinstance(prior_candidate, dict):
                            receipt["last_success"] = {
                                "candidate": prior_candidate,
                                "checked_at": cached.get("checked_at"),
                                "state": cached.get("state"),
                            }
            _write_receipt(_check_path(), receipt)
    except TimeoutError:
        return status()
    return status()


def apply(
    vault: Vault,
    *,
    from_sha: object,
    to_sha: object,
    expected_check_revision: object,
    approval_ref: object,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Apply one cached exact SHA after a fresh interactive approval."""

    clean_from = _clean_sha(from_sha, "installed revision")
    clean_to = _clean_sha(to_sha, "candidate revision")
    clean_revision = _clean_revision(expected_check_revision, "update check revision")
    clean_approval = _approval(approval_ref)
    run = runner or _run_command
    observed = installed_provenance()
    _require_supported_install(observed)
    if observed.tool_dir is None:
        raise ValidationError("installed Seld has no stable tool directory")
    try:
        with (
            exclusive_lock(
                Path(observed.tool_dir) / ".gsv-update.lock",
                timeout=UPDATE_LOCK_TIMEOUT_SECONDS,
            ),
            _update_lock(),
        ):
            return _apply_locked(
                vault,
                from_sha=clean_from,
                to_sha=clean_to,
                expected_check_revision=clean_revision,
                approval_ref=clean_approval,
                runner=run,
            )
    except TimeoutError as exc:
        raise ConflictError("another Seld update is already running") from exc


def recover(
    vault: Vault,
    *,
    token: object,
    expected_vault_digest: object | None = None,
    approval_ref: object | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Finish or reverse one interrupted transaction without choosing a new target."""

    clean_token = _uuid(token, "update token")
    if (expected_vault_digest is None) != (approval_ref is None):
        raise ValidationError(
            "vault-drift recovery requires both an expected vault digest and approval reference"
        )
    clean_vault_digest = (
        _clean_revision(expected_vault_digest, "approved recovery vault digest")
        if expected_vault_digest is not None
        else None
    )
    clean_approval = _approval(approval_ref) if approval_ref is not None else None
    run = runner or _run_command
    transaction = _read_transaction(required=True)
    assert transaction is not None
    _require_recovery_vault(transaction, vault)
    tool_dir = Path(_required_string(transaction, "tool_dir"))
    try:
        with (
            exclusive_lock(
                tool_dir / ".gsv-update.lock",
                timeout=UPDATE_LOCK_TIMEOUT_SECONDS,
            ),
            _update_lock(),
        ):
            transaction = _read_transaction(required=True)
            assert transaction is not None
            if Path(_required_string(transaction, "tool_dir")) != tool_dir:
                raise ConflictError("Seld update transaction changed before recovery began")
            _require_recovery_vault(transaction, vault)
            if transaction.get("token") != clean_token:
                raise ConflictError("update recovery token does not match the current transaction")
            if _transaction_resolved(transaction):
                return _transaction_result(transaction)
            previous_outcome = transaction.get("outcome")
            recovery_changes: dict[str, Any] = {}
            if clean_vault_digest is not None:
                observed_digest = _clean_revision(
                    vault.status().get("digest"), "current recovery vault digest"
                )
                if observed_digest != clean_vault_digest:
                    raise ConflictError(
                        "the Seld vault changed after recovery was approved; review it again"
                    )
                recovery_changes.update(
                    protected_vault_digest=clean_vault_digest,
                    recovery_approval_ref=clean_approval,
                    recovery_approval_vault_digest=clean_vault_digest,
                )
            elif previous_outcome in {
                "installed",
                "installed_bridge_repair",
                "rolled_back",
            }:
                # These outcomes are written only after an activation audit. Writes
                # completed after that audit are a new recovery attempt's baseline.
                recovery_changes["protected_vault_digest"] = None
            if previous_outcome in _TERMINAL_OUTCOMES:
                transaction = _advance_transaction(
                    transaction,
                    phase="recovering",
                    outcome=None,
                    **recovery_changes,
                )
            elif recovery_changes:
                transaction = _advance_transaction(
                    transaction,
                    phase=str(transaction["phase"]),
                    **recovery_changes,
                )
            active = Path(_required_string(transaction, "active_environment"))
            previous = Path(_required_string(transaction, "previous_environment"))
            active_sha = _runtime_sha(active, run)
            if active_sha == transaction.get("to_sha"):
                try:
                    return _finish_candidate(vault, transaction, runner=run, recovering=True)
                except _ActivationVaultChanged:
                    latest = _read_transaction(required=True) or transaction
                    return _mark_vault_reapproval_required(vault, latest)
                except (OSError, SetupError, ValidationError, ConflictError) as exc:
                    latest = _read_transaction(required=True) or transaction
                    return _rollback_after_failure(vault, latest, exc, runner=run)
            if active_sha == transaction.get("from_sha"):
                try:
                    if transaction.get("phase") in {
                        "backing_up",
                        "backed_up",
                        "preflighting",
                        "prepared",
                    }:
                        return _finish_unchanged(vault, transaction, runner=run)
                    return _finish_restored(vault, transaction, runner=run)
                except (OSError, SetupError, ValidationError, ConflictError) as exc:
                    latest = _read_transaction(required=True) or transaction
                    return _mark_repair_required(latest, _failure_code(exc))
            if previous.is_dir() and not previous.is_symlink():
                return _rollback_after_failure(
                    vault,
                    transaction,
                    SetupError("interrupted candidate installation was incomplete"),
                    runner=run,
                )
            return _mark_repair_required(
                transaction,
                "active_installation_does_not_match_transaction",
            )
    except TimeoutError as exc:
        raise ConflictError("another Seld update is already running") from exc


def installed_provenance() -> InstallProvenance:
    """Prove that this process belongs to the official uv-tool source install."""

    if getattr(sys, "frozen", False):
        return _unsupported("prebuilt", "prebuilt_updates_not_supported")
    try:
        raw_direct = _installed_direct_url()
    except (OSError, UnicodeError):
        return _unsupported("unknown", "package_provenance_unavailable")
    if raw_direct is None or len(raw_direct.encode("utf-8")) > 64 * 1024:
        return _unsupported("unknown", "package_provenance_unavailable")
    try:
        direct = json.loads(raw_direct)
    except json.JSONDecodeError:
        return _unsupported("unknown", "package_provenance_invalid")
    repository = direct.get("url") if isinstance(direct, dict) else None
    vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
    if not isinstance(repository, str) or not _same_repository(repository):
        return _unsupported("source", "repository_not_official")
    if not isinstance(vcs, dict) or vcs.get("vcs") != "git":
        return _unsupported("source", "git_provenance_missing")
    commit = vcs.get("commit_id")
    if not isinstance(commit, str) or _SHA.fullmatch(commit) is None:
        return _unsupported("source", "git_revision_missing")
    environment = Path(sys.prefix).expanduser().resolve()
    if environment.name != "gsv" or environment.is_symlink() or not environment.is_dir():
        return _unsupported("source", "uv_environment_not_recognized")
    receipt_path = environment / "uv-receipt.toml"
    try:
        receipt_raw = read_regular_file(
            receipt_path,
            label="uv tool receipt",
            max_bytes=64 * 1024,
        )
        receipt = tomllib.loads(receipt_raw.decode("utf-8"))
        launcher = _uv_launcher(receipt, environment)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError):
        return _unsupported("source", "uv_receipt_invalid")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_symlink():
        try:
            python.resolve(strict=True)
        except OSError:
            return _unsupported("source", "uv_python_unavailable")
    elif not python.is_file():
        return _unsupported("source", "uv_python_unavailable")
    return InstallProvenance(
        supported=os.name != "nt",
        install_mode="uv-source" if os.name != "nt" else "uv-source-windows",
        version=__version__,
        sha=commit,
        repository=REPOSITORY_URL,
        environment=str(environment),
        launcher=str(launcher),
        tool_dir=str(environment.parent),
        bin_dir=str(launcher.parent),
        python=str(python),
        reason_code=None if os.name != "nt" else "windows_source_update_not_supported",
    )


def _apply_locked(
    vault: Vault,
    *,
    from_sha: str,
    to_sha: str,
    expected_check_revision: str,
    approval_ref: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    # Import lazily because Bridge projection reads cached update state while
    # the Bridge module itself is still initializing.
    from continuity_kernel.bridge import bridge_status, stop_bridge

    if from_sha == to_sha:
        raise ValidationError("candidate revision must differ from the installed revision")
    installed = installed_provenance()
    _require_supported_install(installed)
    if installed.sha != from_sha:
        raise ConflictError("installed Seld changed after the update was reviewed")
    cached = _read_receipt(_check_path(), label="Seld update check")
    if cached is None:
        raise ValidationError("no checked Seld update is available")
    _validate_check_receipt(cached)
    if _receipt_revision(cached) != expected_check_revision:
        raise ConflictError("Seld update status changed after approval; review it again")
    if _check_due(cached):
        raise ConflictError("Seld update check expired after approval; check and review it again")
    candidate = cached.get("candidate")
    if (
        cached.get("state") != "available"
        or cached.get("from_sha") != from_sha
        or not isinstance(candidate, dict)
        or candidate.get("sha") != to_sha
    ):
        raise ConflictError("the approved Seld update is no longer the current checked candidate")
    existing = _read_transaction(required=False)
    if existing is not None and not _transaction_resolved(existing):
        raise ConflictError("the previous Seld update must be resolved before another begins")
    if (
        installed.environment is None
        or installed.tool_dir is None
        or installed.bin_dir is None
        or installed.launcher is None
    ):
        raise ValidationError("installed Seld paths are incomplete")
    recovery_launcher_target = Path(installed.bin_dir) / RECOVERY_LAUNCHER_NAME
    if os.path.lexists(recovery_launcher_target):
        raise ConflictError(
            f"the reserved Seld recovery launcher path already exists: {recovery_launcher_target}"
        )
    active = Path(installed.environment)
    token = str(uuid.uuid4())
    previous = active.parent / f".gsv.previous.{token}"
    failed = active.parent / f".gsv.failed.{token}"
    before = vault.status()
    bridge_before = bridge_status()
    if bridge_before.get("health_unavailable") is True:
        raise SetupError("the live Bridge could not be verified before update")
    if bridge_before.get("running") is True and (
        bridge_before.get("identity_verified") is not True
        or bridge_before.get("vault") != str(vault.root)
        or bridge_before.get("vault_id") != before["vault_id"]
    ):
        raise ConflictError("the live Bridge belongs to a different Seld vault")
    transaction: dict[str, Any] = {
        "format_version": TRANSACTION_FORMAT_VERSION,
        "token": token,
        "from_sha": from_sha,
        "to_sha": to_sha,
        "check_revision": expected_check_revision,
        "approval_ref": approval_ref,
        "started_at": _now(),
        "updated_at": _now(),
        "phase": "backing_up",
        "outcome": None,
        "active_environment": str(active),
        "previous_environment": str(previous),
        "failed_environment": str(failed),
        "tool_dir": installed.tool_dir,
        "bin_dir": installed.bin_dir,
        "launcher": installed.launcher,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "vault": str(vault.root),
        "vault_id": before["vault_id"],
        "vault_digest": before["digest"],
        "bridge_was_running": bridge_before.get("running") is True,
        "bridge_instance_before": bridge_before.get("instance_id"),
        "backup": None,
        "error_code": None,
        "recovery_command": None,
        "recovery_launcher": None,
        "recovery_launcher_digest": None,
    }
    _write_receipt(_transaction_path(), transaction)
    try:
        backup = vault.create_backup()
        backup_path = Path(str(backup["backup"]))
        verified = Vault.verify_backup(backup_path)
        if verified.get("valid") is not True:
            raise SetupError("the pre-update backup did not verify")
        transaction = _advance_transaction(
            transaction,
            phase="backed_up",
            backup=str(backup_path),
        )
        transaction = _advance_transaction(transaction, phase="preflighting")
        _preflight_candidate(to_sha, runner)
        transaction = _advance_transaction(transaction, phase="prepared")
        if bridge_before.get("running") is True:
            transaction = _advance_transaction(transaction, phase="stopping_bridge")
            stopped = stop_bridge()
            if stopped.get("stopped") is not True:
                raise SetupError("the live Bridge did not stop before update")
        transaction = _advance_transaction(transaction, phase="bridge_stopped")
        if _runtime_sha(active, runner) != from_sha:
            raise SetupError("the installed Seld revision changed before preservation")
        _verify_external_launcher(transaction, repair_missing=False)
        recovery_launcher = _recovery_launcher_path(transaction)
        recovery_launcher_content = _recovery_launcher_content(previous, active)
        recovery_launcher_digest = sha256_bytes(recovery_launcher_content)
        recovery = _recovery_launcher_command(
            recovery_launcher,
            token,
            vault.root,
        )
        transaction = _advance_transaction(
            transaction,
            phase="preserving_previous",
            recovery_command=recovery,
            recovery_launcher=str(recovery_launcher),
            recovery_launcher_digest=recovery_launcher_digest,
        )
        _publish_recovery_launcher(transaction, recovery_launcher_content)
        move_no_replace(active, previous)
        transaction = _advance_transaction(transaction, phase="previous_preserved")
        _install_candidate(transaction, runner)
        transaction = _advance_transaction(transaction, phase="candidate_installed")
        return _finish_candidate(vault, transaction, runner=runner, recovering=False)
    except Exception as exc:
        if not isinstance(exc, (OSError, SetupError, ValidationError, ConflictError)):
            raise
        latest = _read_transaction(required=True) or transaction
        return _rollback_after_failure(vault, latest, exc, runner=runner)


def _finish_candidate(
    vault: Vault,
    transaction: dict[str, Any],
    *,
    runner: CommandRunner,
    recovering: bool,
) -> dict[str, Any]:
    active = Path(_required_string(transaction, "active_environment"))
    _verify_external_launcher(transaction, repair_missing=False)
    candidate = _runtime_command(active)
    transaction = _advance_transaction(
        transaction,
        phase="verifying_candidate" if recovering else "setting_up_candidate",
    )
    setup_arguments = [
        *candidate,
        "--json",
        "--vault",
        str(vault.root),
        "setup",
        "--no-browser",
    ]
    if transaction.get("bridge_was_running") is not True:
        setup_arguments.append("--no-bridge")
    with exclusive_lock(vault.state / "locks/global.lock"):
        transaction = _protect_vault_for_activation(vault, transaction)
        setup = runner(setup_arguments, _child_environment(), COMMAND_TIMEOUT_SECONDS)
        if setup.returncode == 4:
            _verify_repair_install(vault, transaction, runner=runner)
            vault_audit = _vault_audit(vault, transaction)
        elif setup.returncode != 0:
            raise SetupError("candidate setup failed")
        else:
            _verify_active_runtime(
                vault, transaction, runner=runner, expected_sha=str(transaction["to_sha"])
            )
            vault_audit = _vault_audit(vault, transaction)
    recovery_launcher_error = None
    try:
        _cleanup_recovery_launcher(transaction)
    except (OSError, SetupError, ValidationError):
        recovery_launcher_error = "recovery_launcher_cleanup_failed"
    if setup.returncode == 4:
        transaction = _advance_transaction(
            transaction,
            phase="complete",
            outcome="installed_bridge_repair",
            error_code=recovery_launcher_error or "bridge_needs_repair",
            recovery_command=(
                _available_recovery_command(transaction)
                if recovery_launcher_error
                else "gsv --json bridge status"
            ),
            **vault_audit,
        )
        return _transaction_result(transaction)
    previous = Path(_required_string(transaction, "previous_environment"))
    cleanup_error = recovery_launcher_error
    if previous.is_dir() and not previous.is_symlink():
        try:
            shutil.rmtree(previous)
        except OSError:
            cleanup_error = cleanup_error or "previous_environment_cleanup_failed"
    transaction = _advance_transaction(
        transaction,
        phase="complete",
        outcome="installed",
        error_code=cleanup_error,
        recovery_command=(
            _available_recovery_command(transaction)
            if recovery_launcher_error
            else (f"Review retained previous environment: {previous}" if cleanup_error else None)
        ),
        **vault_audit,
    )
    return _transaction_result(transaction)


def _rollback_after_failure(
    vault: Vault,
    transaction: dict[str, Any],
    failure: Exception,
    *,
    runner: CommandRunner,
) -> dict[str, Any]:
    active = Path(_required_string(transaction, "active_environment"))
    previous = Path(_required_string(transaction, "previous_environment"))
    failed = Path(_required_string(transaction, "failed_environment"))
    error_code = _failure_code(failure)
    if not previous.is_dir() or previous.is_symlink():
        if _runtime_sha(active, runner) == transaction.get("from_sha"):
            phase_before_failure = transaction.get("phase")
            transaction = _advance_transaction(
                transaction,
                phase="restoring_previous",
                error_code=error_code,
            )
            if phase_before_failure in {
                "backing_up",
                "backed_up",
                "preflighting",
                "prepared",
            }:
                return _finish_unchanged(vault, transaction, runner=runner)
            return _finish_restored(vault, transaction, runner=runner)
        return _mark_repair_required(transaction, error_code)
    if _runtime_sha(previous, runner) != transaction.get("from_sha"):
        return _mark_repair_required(
            transaction,
            "previous_revision_mismatch",
            recovery_command=(
                "Do not execute the preserved Seld environment: its source revision does not "
                "match this transaction. Retain both environments and inspect the verified "
                f"backup for token {_required_string(transaction, 'token')}."
            ),
        )
    if not _runtime_reads_vault(previous, vault, transaction, runner):
        return _mark_repair_required(transaction, "previous_reader_incompatible")
    if active.is_dir() and not active.is_symlink():
        if _candidate_may_have_started_bridge(transaction):
            try:
                _stop_runtime_bridge(active, runner)
            except SetupError:
                return _mark_repair_required(transaction, "candidate_bridge_stop_failed")
        if os.path.lexists(failed):
            return _mark_repair_required(transaction, "failed_environment_path_conflict")
        move_no_replace(active, failed)
    elif os.path.lexists(active):
        return _mark_repair_required(transaction, "active_environment_unsafe")
    transaction = _advance_transaction(
        transaction,
        phase="restoring_previous",
        error_code=error_code,
    )
    try:
        move_no_replace(previous, active)
    except OSError:
        return _mark_repair_required(transaction, "previous_environment_restore_failed")
    transaction = _anchor_restored_runtime(vault, transaction)
    return _finish_restored(vault, transaction, runner=runner)


def _candidate_may_have_started_bridge(transaction: dict[str, Any]) -> bool:
    return transaction.get("phase") in {
        "setting_up_candidate",
        "verifying_candidate",
        "recovering",
    }


def _finish_restored(
    vault: Vault,
    transaction: dict[str, Any],
    *,
    runner: CommandRunner,
) -> dict[str, Any]:
    active = Path(_required_string(transaction, "active_environment"))
    try:
        transaction = _anchor_restored_runtime(vault, transaction)
        _verify_external_launcher(transaction, repair_missing=True)
        command = _runtime_command(active)
        setup_arguments = [
            *command,
            "--json",
            "--vault",
            str(vault.root),
            "setup",
            "--no-browser",
        ]
        if transaction.get("bridge_was_running") is not True:
            setup_arguments.append("--no-bridge")
        with exclusive_lock(vault.state / "locks/global.lock"):
            transaction = _protect_vault_for_activation(vault, transaction)
            setup = runner(setup_arguments, _child_environment(), COMMAND_TIMEOUT_SECONDS)
            if setup.returncode != 0:
                return _mark_repair_required(transaction, "previous_setup_failed")
            _verify_active_runtime(
                vault,
                transaction,
                runner=runner,
                expected_sha=str(transaction["from_sha"]),
            )
            vault_audit = _vault_audit(vault, transaction)
    except _ActivationVaultChanged:
        return _mark_vault_reapproval_required(vault, transaction)
    except (OSError, SetupError, ValidationError):
        return _mark_repair_required(transaction, "previous_health_failed")
    failed = Path(_required_string(transaction, "failed_environment"))
    cleanup_error = None
    if failed.is_dir() and not failed.is_symlink():
        try:
            shutil.rmtree(failed)
        except OSError:
            cleanup_error = "failed_environment_cleanup_failed"
    recovery_launcher_error = None
    try:
        _cleanup_recovery_launcher(transaction)
    except (OSError, SetupError, ValidationError):
        recovery_launcher_error = "recovery_launcher_cleanup_failed"
    cleanup_error = cleanup_error or recovery_launcher_error
    transaction = _advance_transaction(
        transaction,
        phase="complete",
        outcome="rolled_back",
        error_code=cleanup_error or transaction.get("error_code") or "candidate_failed",
        recovery_command=(
            _available_recovery_command(transaction)
            if recovery_launcher_error
            else (f"Review retained failed environment: {failed}" if cleanup_error else None)
        ),
        **vault_audit,
    )
    return _transaction_result(transaction)


def _anchor_restored_runtime(vault: Vault, transaction: dict[str, Any]) -> dict[str, Any]:
    active = Path(_required_string(transaction, "active_environment"))
    command = _recovery_command(
        active,
        _required_string(transaction, "token"),
        vault.root,
    )
    if (
        transaction.get("phase") == "previous_restored"
        and transaction.get("recovery_command") == command
    ):
        return transaction
    return _advance_transaction(
        transaction,
        phase="previous_restored",
        recovery_command=command,
    )


def _finish_unchanged(
    vault: Vault,
    transaction: dict[str, Any],
    *,
    runner: CommandRunner,
) -> dict[str, Any]:
    try:
        _verify_external_launcher(transaction, repair_missing=False)
        with exclusive_lock(vault.state / "locks/global.lock"):
            transaction = _protect_vault_for_activation(vault, transaction)
            _verify_active_runtime(
                vault,
                transaction,
                runner=runner,
                expected_sha=str(transaction["from_sha"]),
            )
            vault_audit = _vault_audit(vault, transaction)
        _cleanup_recovery_launcher(transaction)
    except _ActivationVaultChanged:
        return _mark_vault_reapproval_required(vault, transaction)
    except (OSError, SetupError, ValidationError):
        return _mark_repair_required(transaction, "unchanged_runtime_health_failed")
    transaction = _advance_transaction(
        transaction,
        phase="complete",
        outcome="rolled_back",
        error_code=transaction.get("error_code") or "candidate_preflight_failed",
        recovery_command=None,
        **vault_audit,
    )
    return _transaction_result(transaction)


def _verify_active_runtime(
    vault: Vault,
    transaction: dict[str, Any],
    *,
    runner: CommandRunner,
    expected_sha: str,
) -> None:
    active = Path(_required_string(transaction, "active_environment"))
    command = _runtime_command(active)
    environment = _child_environment()
    if _environment_sha(active) != expected_sha:
        raise SetupError("fresh Seld process did not report the expected installed revision")
    current = _run_json(command, ["--vault", str(vault.root), "status"], environment, runner)
    if current.get("vault_id") != transaction.get("vault_id"):
        raise SetupError("Seld update changed the configured vault identity")
    doctor = _run_json(command, ["--vault", str(vault.root), "doctor"], environment, runner)
    codex = doctor.get("codex")
    if (
        doctor.get("healthy") is not True
        or not isinstance(codex, dict)
        or codex.get("ready") is not True
    ):
        raise SetupError("fresh Seld process did not prove vault and ChatGPT health")
    bridge = _run_json(command, ["bridge", "status"], environment, runner)
    expected_running = transaction.get("bridge_was_running") is True
    if expected_running and (
        bridge.get("running") is not True
        or bridge.get("identity_verified") is not True
        or bridge.get("vault") != str(vault.root)
        or bridge.get("vault_id") != transaction.get("vault_id")
    ):
        raise SetupError("fresh Seld process did not prove the Bridge is running")
    if not expected_running and bridge.get("running") is True:
        raise SetupError("Seld update unexpectedly started a previously stopped Bridge")


def _vault_audit(vault: Vault, transaction: dict[str, Any]) -> dict[str, Any]:
    observed = vault.status()
    if observed.get("vault_id") != transaction.get("vault_id"):
        raise SetupError("Seld update changed the configured vault identity")
    digest = _clean_revision(observed.get("digest"), "post-update vault digest")
    if digest != transaction.get("protected_vault_digest"):
        raise SetupError("Seld setup changed canonical vault bytes during activation")
    return {
        "vault_digest_after": digest,
        "vault_changed": digest != transaction.get("vault_digest"),
    }


def _protect_vault_for_activation(vault: Vault, transaction: dict[str, Any]) -> dict[str, Any]:
    observed = vault.status()
    if observed.get("vault_id") != transaction.get("vault_id"):
        raise SetupError("Seld update changed the configured vault identity")
    digest = _clean_revision(observed.get("digest"), "pre-activation vault digest")
    protected = transaction.get("protected_vault_digest")
    if protected is not None:
        clean_protected = _clean_revision(protected, "protected vault digest")
        if digest != clean_protected:
            raise _ActivationVaultChanged("canonical vault bytes changed after activation began")
        return transaction
    return _advance_transaction(
        transaction,
        phase=str(transaction["phase"]),
        protected_vault_digest=digest,
    )


def _verify_repair_install(
    vault: Vault,
    transaction: dict[str, Any],
    *,
    runner: CommandRunner,
) -> None:
    active = Path(_required_string(transaction, "active_environment"))
    command = _runtime_command(active)
    environment = _child_environment()
    if _environment_sha(active) != transaction.get("to_sha"):
        raise SetupError("repair-state Seld does not match the approved revision")
    current = _run_json(command, ["--vault", str(vault.root), "status"], environment, runner)
    if current.get("vault_id") != transaction.get("vault_id"):
        raise SetupError("repair-state Seld changed the configured vault identity")
    doctor = _run_json(command, ["--vault", str(vault.root), "doctor"], environment, runner)
    codex = doctor.get("codex")
    if (
        doctor.get("healthy") is not True
        or not isinstance(codex, dict)
        or codex.get("ready") is not True
    ):
        raise SetupError("repair-state Seld did not preserve vault and ChatGPT health")


def _runtime_reads_vault(
    environment_root: Path,
    vault: Vault,
    transaction: dict[str, Any],
    runner: CommandRunner,
) -> bool:
    command = _runtime_module_command(environment_root)
    environment = _child_environment()
    try:
        current = _run_json(command, ["--vault", str(vault.root), "status"], environment, runner)
        doctor = _run_json(command, ["--vault", str(vault.root), "doctor"], environment, runner)
        if CONTROL_STORE_SUPPORTED:
            _run_json(
                command, ["--vault", str(vault.root), "operation", "list"], environment, runner
            )
    except (OSError, SetupError, ValidationError):
        return False
    return current.get("vault_id") == transaction.get("vault_id") and doctor.get("healthy") is True


def _preflight_candidate(sha: str, runner: CommandRunner) -> None:
    with tempfile.TemporaryDirectory(prefix="seld-update-preflight-") as raw_root:
        root = Path(raw_root)
        tool_dir = root / "tools"
        bin_dir = root / "bin"
        environment = _isolated_preflight_environment(root)
        _uv_install(
            sha,
            tool_dir=tool_dir,
            bin_dir=bin_dir,
            runner=runner,
            base_environment=environment,
        )
        candidate_root = tool_dir / "gsv"
        if _runtime_sha(candidate_root, runner) != sha:
            raise SetupError("staged Seld provenance does not match the approved revision")
        command = _runtime_command(candidate_root)
        version = runner([*command, "--version"], environment, 30.0)
        if version.returncode != 0 or not version.stdout.decode("utf-8", "replace").strip():
            raise SetupError("staged Seld version probe failed")
        demo = runner(
            [*command, "--json", "demo", "--output", str(root / "demo")],
            environment,
            120.0,
        )
        if demo.returncode != 0:
            raise SetupError("staged Seld synthetic proof failed")


def _install_candidate(transaction: dict[str, Any], runner: CommandRunner) -> None:
    tool_dir = Path(_required_string(transaction, "tool_dir"))
    bin_dir = Path(_required_string(transaction, "bin_dir"))
    _uv_install(str(transaction["to_sha"]), tool_dir=tool_dir, bin_dir=bin_dir, runner=runner)
    active = Path(_required_string(transaction, "active_environment"))
    if _runtime_sha(active, runner) != transaction.get("to_sha"):
        raise SetupError("installed Seld provenance does not match the approved revision")
    _verify_external_launcher(transaction, repair_missing=False)


def _uv_install(
    sha: str,
    *,
    tool_dir: Path,
    bin_dir: Path,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None = None,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SetupError("uv is required for this Seld source update")
    uv_path = Path(uv).expanduser().resolve()
    if not uv_path.is_file():
        raise SetupError("uv executable is unavailable")
    environment = dict(base_environment or _child_environment())
    environment.update(
        {
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "PIP_CONFIG_FILE": os.devnull,
        }
    )
    source = f"git+{REPOSITORY_URL}@{sha}"
    result = runner(
        [
            str(uv_path),
            "--no-config",
            "tool",
            "install",
            "--force",
            "--python",
            f"{sys.version_info.major}.{sys.version_info.minor}",
            source,
        ],
        environment,
        COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise SetupError("uv could not install the approved Seld revision")


def _resolve_public_main(from_sha: str, *, fetcher: JsonFetcher) -> dict[str, Any]:
    commit, _ = fetcher(f"{API_ROOT}/commits/main")
    to_sha = _payload_sha(commit.get("sha"), "public main revision")
    commit_data = commit.get("commit")
    verification = commit_data.get("verification") if isinstance(commit_data, dict) else None
    if not isinstance(commit_data, dict) or not isinstance(verification, dict):
        raise ValidationError("GitHub commit response is incomplete")
    committer = commit_data.get("committer")
    committed_at = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(committed_at, str) or len(committed_at) > 64:
        raise ValidationError("GitHub commit time is invalid")
    try:
        _parse_time(committed_at)
    except ValueError as exc:
        raise ValidationError("GitHub commit time is invalid") from exc
    verified = verification.get("verified") is True
    reason = verification.get("reason")
    if not isinstance(reason, str) or len(reason) > 64:
        reason = "unknown"
    if to_sha == from_sha:
        return {
            "state": "current",
            "candidate": None,
            "error_code": None,
        }
    comparison, _ = fetcher(
        f"{API_ROOT}/compare/{quote(from_sha, safe='')}...{quote(to_sha, safe='')}?per_page=1"
    )
    if (
        comparison.get("status") != "ahead"
        or comparison.get("behind_by") != 0
        or not isinstance(comparison.get("ahead_by"), int)
        or int(comparison["ahead_by"]) <= 0
    ):
        return {
            "state": "not_ready",
            "candidate": {
                "sha": to_sha,
                "committed_at": committed_at,
                "verified": verified,
                "verification_reason": reason,
                "ci_checks": 0,
            },
            "error_code": "candidate_not_descendant",
        }
    checks, _ = fetcher(f"{API_ROOT}/commits/{to_sha}/check-runs?per_page=100")
    check_runs = checks.get("check_runs")
    total_count = checks.get("total_count")
    check_names: list[str] = []
    if (
        not isinstance(check_runs, list)
        or not check_runs
        or not isinstance(total_count, int)
        or total_count != len(check_runs)
    ):
        ci_ready = False
        ci_count = 0
    else:
        ci_count = len(check_runs)
        ci_ready = all(_successful_github_check(item, to_sha) for item in check_runs)
        if ci_ready:
            check_names = [str(item["name"]) for item in check_runs]
            ci_ready = REQUIRED_CHECK_NAMES.issubset(check_names)
    candidate = {
        "sha": to_sha,
        "committed_at": committed_at,
        "verified": verified,
        "verification_reason": reason,
        "ci_checks": ci_count,
        "check_names": sorted(check_names),
    }
    if not verified:
        return {
            "state": "not_ready",
            "candidate": candidate,
            "error_code": "commit_not_verified",
        }
    if not ci_ready:
        return {
            "state": "not_ready",
            "candidate": candidate,
            "error_code": "checks_not_green",
        }
    return {"state": "available", "candidate": candidate, "error_code": None}


def _successful_github_check(value: object, sha: str) -> bool:
    if not isinstance(value, dict):
        return False
    app = value.get("app")
    name = value.get("name")
    return (
        isinstance(name, str)
        and 0 < len(name) <= 200
        and value.get("head_sha") == sha
        and value.get("status") == "completed"
        and value.get("conclusion") == "success"
        and isinstance(app, dict)
        and app.get("slug") == "github-actions"
    )


def _fetch_json(url: str) -> tuple[dict[str, Any], Mapping[str, str]]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed.username:
        raise ValidationError("Seld update checks use only the fixed public GitHub API")
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"seld/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = build_opener(_RejectRedirects())
    with opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length) > MAX_NETWORK_BYTES:
                    raise ValidationError("GitHub update response exceeds its size bound")
            except ValueError as exc:
                raise ValidationError("GitHub update response has an invalid size") from exc
        encoded = response.read(MAX_NETWORK_BYTES + 1)
        if len(encoded) > MAX_NETWORK_BYTES:
            raise ValidationError("GitHub update response exceeds its size bound")
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("GitHub update response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("GitHub update response is not an object")
        return payload, dict(response.headers.items())


def _runtime_sha(environment_root: Path, runner: CommandRunner) -> str | None:
    del runner
    try:
        return _environment_sha(environment_root)
    except (OSError, UnicodeError, ValidationError):
        return None


def _environment_sha(environment_root: Path) -> str:
    site_root = (
        environment_root / "Lib/site-packages" if os.name == "nt" else environment_root / "lib"
    )
    if os.name == "nt":
        candidates = list(site_root.glob("gsv-*.dist-info/direct_url.json"))
    else:
        candidates = list(site_root.glob("python*/site-packages/gsv-*.dist-info/direct_url.json"))
    if len(candidates) != 1:
        raise ValidationError("Seld environment has no unique package provenance")
    encoded = read_regular_file(
        candidates[0],
        label="installed Seld package provenance",
        max_bytes=64 * 1024,
    )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("installed Seld package provenance is invalid") from exc
    repository = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(repository, str) or not _same_repository(repository):
        raise ValidationError("installed Seld package repository is not the approved source")
    vcs = payload.get("vcs_info")
    if not isinstance(vcs, dict) or vcs.get("vcs") != "git":
        raise ValidationError("installed Seld Git provenance is missing")
    return _clean_sha(vcs.get("commit_id"), "installed Seld revision")


def _runtime_command(environment_root: Path) -> list[str]:
    executable = environment_root / ("Scripts/gsv.exe" if os.name == "nt" else "bin/gsv")
    if executable.is_symlink() or not executable.is_file():
        raise SetupError(f"Seld runtime is unavailable: {executable}")
    return [str(executable)]


def _runtime_module_command(environment_root: Path) -> list[str]:
    python = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists() or (not python.is_file() and not python.is_symlink()):
        raise SetupError(f"Seld recovery runtime is unavailable: {python}")
    return [str(python), "-m", "continuity_kernel"]


def _stop_runtime_bridge(environment_root: Path, runner: CommandRunner) -> None:
    try:
        result = runner(
            [*_runtime_command(environment_root), "--json", "bridge", "stop"],
            _child_environment(),
            30.0,
        )
    except (OSError, SetupError) as exc:
        raise SetupError("candidate Bridge could not be stopped for rollback") from exc
    if result.returncode != 0:
        raise SetupError("candidate Bridge could not be stopped for rollback")


def _run_json(
    command: list[str],
    arguments: list[str],
    environment: Mapping[str, str],
    runner: CommandRunner,
) -> dict[str, Any]:
    result = runner([*command, "--json", *arguments], environment, COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise SetupError(f"Seld subprocess failed: {' '.join(arguments)}")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError("Seld subprocess returned invalid JSON") from exc
    value = (
        payload.get("result") if isinstance(payload, dict) and payload.get("ok") is True else None
    )
    if not isinstance(value, dict):
        raise SetupError("Seld subprocess returned an invalid result")
    return value


def _run_command(
    command: list[str],
    environment: Mapping[str, str],
    timeout: float,
) -> CommandResult:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environment),
        start_new_session=os.name != "nt",
    )
    assert process.stdout is not None and process.stderr is not None
    out_buffer = bytearray()
    err_buffer = bytearray()
    readers = (
        threading.Thread(target=_drain_bounded, args=(process.stdout, out_buffer), daemon=True),
        threading.Thread(target=_drain_bounded, args=(process.stderr, err_buffer), daemon=True),
    )
    for reader in readers:
        reader.start()
    timed_out: subprocess.TimeoutExpired | None = None
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = exc
        _terminate_process_tree(process)
        returncode = process.returncode if process.returncode is not None else -signal.SIGKILL
    for reader in readers:
        reader.join(timeout=2.0)
    if any(reader.is_alive() for reader in readers):
        _terminate_process_tree(process)
        raise SetupError(f"Seld update command left output streams open: {Path(command[0]).name}")
    if timed_out is not None:
        raise SetupError(f"Seld update command timed out: {Path(command[0]).name}") from timed_out
    out = bytes(out_buffer)
    err = bytes(err_buffer)
    if len(out) > MAX_COMMAND_OUTPUT_BYTES or len(err) > MAX_COMMAND_OUTPUT_BYTES:
        raise SetupError(f"Seld update command exceeded its output bound: {Path(command[0]).name}")
    return CommandResult(returncode, out, err)


def _drain_bounded(stream: Any, destination: bytearray) -> None:
    while chunk := stream.read(64 * 1024):
        remaining = MAX_COMMAND_OUTPUT_BYTES + 1 - len(destination)
        if remaining > 0:
            destination.extend(chunk[:remaining])


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        process.kill()
        process.wait(timeout=2.0)
        return
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is None:
            process.wait(timeout=2.0)
        return
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.5)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            break
        time.sleep(0.05)
    else:
        with suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)
    if process.poll() is None:
        process.wait(timeout=2.0)
    deadline = time.monotonic() + 2.0
    while _process_group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_exists(process_group):
        raise SetupError("Seld update could not terminate its command process group")


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "UV_INDEX",
            "UV_INDEX_URL",
            "UV_EXTRA_INDEX_URL",
            "UV_DEFAULT_INDEX",
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
            "GIT_DIR",
            "GIT_WORK_TREE",
        } or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    return environment


def _isolated_preflight_environment(root: Path) -> dict[str, str]:
    environment = _child_environment()
    home = root / "home"
    config = root / "config"
    data = root / "data"
    codex = root / "codex"
    temporary = root / "tmp"
    for path in (home, config, data, codex, temporary):
        path.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "APPDATA": str(config),
            "CODEX_HOME": str(codex),
            "GSV_CONFIG_DIR": str(config),
            "GSV_DATA_DIR": str(data),
            "GSV_VAULT": str(root / "vault"),
            "HOME": str(home),
            "LOCALAPPDATA": str(data),
            "TMPDIR": str(temporary),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
        }
    )
    return environment


def _uv_launcher(receipt: dict[str, Any], environment: Path) -> Path:
    tool = receipt.get("tool")
    if not isinstance(tool, dict):
        raise ValidationError("uv tool receipt has no tool table")
    entrypoints = tool.get("entrypoints")
    if not isinstance(entrypoints, list):
        raise ValidationError("uv tool receipt has no entrypoints")
    matching = [
        entry for entry in entrypoints if isinstance(entry, dict) and entry.get("name") == "gsv"
    ]
    if len(matching) != 1:
        raise ValidationError("uv tool receipt must own one gsv entrypoint")
    raw = matching[0].get("install-path")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValidationError("uv tool receipt has an invalid entrypoint")
    launcher = Path(raw).expanduser()
    if not launcher.is_absolute() or not launcher.is_symlink():
        raise ValidationError("uv tool entrypoint must be one absolute symbolic link")
    expected = environment / ("Scripts/gsv.exe" if os.name == "nt" else "bin/gsv")
    try:
        if launcher.resolve(strict=True) != expected.resolve(strict=True):
            raise ValidationError("uv tool entrypoint does not target this Seld environment")
    except OSError as exc:
        raise ValidationError("uv tool entrypoint is unavailable") from exc
    return launcher


def _installed_direct_url() -> str | None:
    """Read source provenance dynamically so frozen builds do not collect it."""

    metadata = importlib.import_module("importlib.metadata")
    try:
        distribution = metadata.distribution("gsv")
    except Exception as exc:
        if isinstance(exc, metadata.PackageNotFoundError):
            return None
        raise
    return cast(str | None, distribution.read_text("direct_url.json"))


def _verify_external_launcher(transaction: dict[str, Any], *, repair_missing: bool) -> None:
    """Prove the stable ``gsv`` command reaches the transaction's active runtime.

    ``uv tool install`` owns this link.  An interrupted install can remove it
    after the old environment has already been preserved, so rollback may
    recreate only the exact recorded link.  Existing filesystem entries are
    never replaced or redirected here.
    """

    if os.name == "nt":
        raise SetupError("Windows source updates do not support launcher repair")
    active = Path(_required_string(transaction, "active_environment"))
    bin_dir = Path(_required_string(transaction, "bin_dir"))
    launcher = Path(_required_string(transaction, "launcher"))
    expected = active / "bin/gsv"
    _runtime_command(active)
    if launcher != bin_dir / "gsv":
        raise ValidationError("Seld update launcher does not match its recorded bin directory")

    if os.path.lexists(launcher):
        if not launcher.is_symlink():
            raise SetupError("the stable Seld launcher is not a symbolic link")
        try:
            observed = launcher.resolve(strict=True)
            target = expected.resolve(strict=True)
        except OSError as exc:
            raise SetupError("the stable Seld launcher is unavailable") from exc
        if observed != target:
            raise SetupError("the stable Seld launcher points at a different runtime")
        return

    if not repair_missing:
        raise SetupError("the stable Seld launcher is missing")
    if not bin_dir.is_dir() or bin_dir.is_symlink():
        raise SetupError("the stable Seld launcher directory is unsafe")
    try:
        os.symlink(str(expected), str(launcher))
        descriptor = os.open(bin_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SetupError("the stable Seld launcher could not be restored") from exc
    _verify_external_launcher(transaction, repair_missing=False)


def _recovery_launcher_path(transaction: dict[str, Any]) -> Path:
    return Path(_required_string(transaction, "bin_dir")) / RECOVERY_LAUNCHER_NAME


def _recovery_launcher_content(previous: Path, active: Path) -> bytes:
    previous_python = previous / "bin/python"
    active_python = active / "bin/python"
    script = (
        "#!/bin/sh\n"
        "unset PYTHONHOME PYTHONPATH VIRTUAL_ENV\n"
        f"previous={_shell_quote(str(previous_python))}\n"
        f"active={_shell_quote(str(active_python))}\n"
        'if [ -x "$previous" ]; then exec "$previous" -I -m continuity_kernel "$@"; fi\n'
        'if [ -x "$active" ]; then exec "$active" -I -m continuity_kernel "$@"; fi\n'
        'echo "Seld recovery runtime is unavailable" >&2\n'
        "exit 1\n"
    )
    return script.encode("utf-8")


def _publish_recovery_launcher(transaction: dict[str, Any], content: bytes) -> None:
    if os.name == "nt":
        raise SetupError("Windows source updates do not support the recovery launcher")
    if len(content) > MAX_RECOVERY_LAUNCHER_BYTES:
        raise ValidationError("Seld recovery launcher exceeds its size bound")
    expected_digest = _clean_revision(
        transaction.get("recovery_launcher_digest"),
        "recovery launcher digest",
    )
    if sha256_bytes(content) != expected_digest:
        raise ValidationError("Seld recovery launcher content changed before publication")
    path = _recovery_launcher_path(transaction)
    if transaction.get("recovery_launcher") != str(path):
        raise ValidationError("Seld recovery launcher path does not match the transaction")
    if os.path.lexists(path):
        _verify_recovery_launcher(transaction)
        return
    bin_dir = path.parent
    if not bin_dir.is_dir() or bin_dir.is_symlink():
        raise SetupError("the Seld launcher directory is unsafe")
    descriptor, raw_staged = tempfile.mkstemp(prefix=".seld-recover.stage-", dir=bin_dir)
    staged = Path(raw_staged)
    try:
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        durable_publish_new(staged, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            staged.unlink()
    _verify_recovery_launcher(transaction)


def _verify_recovery_launcher(transaction: dict[str, Any]) -> Path:
    raw_path = transaction.get("recovery_launcher")
    raw_digest = transaction.get("recovery_launcher_digest")
    if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
        raise SetupError("the Seld recovery launcher is not recorded")
    path = _recovery_launcher_path(transaction)
    if raw_path != str(path):
        raise ValidationError("Seld recovery launcher path does not match the transaction")
    digest = _clean_revision(raw_digest, "recovery launcher digest")
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SetupError("the Seld recovery launcher is unavailable") from exc
    owner_matches = not hasattr(os, "getuid") or metadata.st_uid == os.getuid()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or not owner_matches
    ):
        raise SetupError("the Seld recovery launcher is not the owned executable")
    encoded = read_regular_file(
        path,
        label="Seld recovery launcher",
        max_bytes=MAX_RECOVERY_LAUNCHER_BYTES,
    )
    if sha256_bytes(encoded) != digest:
        raise SetupError("the Seld recovery launcher does not match its transaction")
    return path


def _cleanup_recovery_launcher(transaction: dict[str, Any]) -> None:
    if transaction.get("recovery_launcher") is None:
        return
    path = _recovery_launcher_path(transaction)
    if not os.path.lexists(path):
        return
    _verify_recovery_launcher(transaction)
    path.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_supported_install(installed: InstallProvenance) -> None:
    if not installed.supported:
        detail = installed.reason_code or installed.install_mode
        raise ValidationError(f"this Seld install cannot self-update ({detail})")


def _unsupported(mode: str, reason: str) -> InstallProvenance:
    return InstallProvenance(False, mode, __version__, reason_code=reason)


def _same_repository(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path.rstrip("/") in {"/olivier-motium/seld", "/olivier-motium/seld.git"}
    )


def _check_due(receipt: dict[str, Any] | None) -> bool:
    if receipt is None:
        return True
    try:
        _validate_check_receipt(receipt)
        checked = _parse_time(str(receipt["checked_at"]))
    except (KeyError, TypeError, ValueError, ValidationError):
        return True
    age = datetime.now(UTC) - checked
    return age < timedelta(0) or age >= CHECK_INTERVAL


def _next_check_after(receipt: dict[str, Any] | None) -> str | None:
    if receipt is None:
        return None
    try:
        return (
            (
                datetime.fromisoformat(str(receipt["checked_at"]).replace("Z", "+00:00"))
                + CHECK_INTERVAL
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (KeyError, TypeError, ValueError):
        return None


def _validate_check_receipt(value: dict[str, Any]) -> None:
    if value.get("format_version") != CHECK_FORMAT_VERSION:
        raise ValidationError("unsupported Seld update-check receipt")
    _parse_time(_required_string(value, "checked_at"))
    _clean_sha(value.get("from_sha"), "checked installed revision")
    state = value.get("state")
    if state not in {"current", "available", "not_ready", "unavailable"}:
        raise ValidationError("Seld update-check receipt has an invalid state")
    candidate = value.get("candidate")
    if candidate is not None:
        if not isinstance(candidate, dict):
            raise ValidationError("Seld update-check candidate is invalid")
        _clean_sha(candidate.get("sha"), "checked candidate revision")
        _parse_time(_required_string(candidate, "committed_at"))
        verified = candidate.get("verified")
        checks = candidate.get("ci_checks")
        check_names = candidate.get("check_names")
        reason = candidate.get("verification_reason")
        if (
            not isinstance(verified, bool)
            or not isinstance(checks, int)
            or isinstance(checks, bool)
            or checks < 0
            or not isinstance(reason, str)
            or not reason
            or len(reason) > 64
        ):
            raise ValidationError("Seld update-check candidate evidence is invalid")
        if check_names is not None and (
            not isinstance(check_names, list)
            or any(not isinstance(name, str) or not name or len(name) > 200 for name in check_names)
            or len(set(check_names)) != len(check_names)
        ):
            raise ValidationError("Seld update-check names are invalid")
    error_code = value.get("error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or not error_code or len(error_code) > 128
    ):
        raise ValidationError("Seld update-check error code is invalid")
    if state == "available":
        if (
            not isinstance(candidate, dict)
            or candidate.get("verified") is not True
            or int(candidate.get("ci_checks", 0)) <= 0
            or not isinstance(candidate.get("check_names"), list)
            or not REQUIRED_CHECK_NAMES.issubset(candidate["check_names"])
            or error_code is not None
        ):
            raise ValidationError("available Seld update evidence is incomplete")
    elif state == "current":
        if candidate is not None or error_code is not None:
            raise ValidationError("current Seld update evidence is inconsistent")
    elif state == "unavailable":
        if candidate is not None or not isinstance(error_code, str):
            raise ValidationError("unavailable Seld update evidence is inconsistent")
    elif not isinstance(candidate, dict) or not isinstance(error_code, str):
        raise ValidationError("not-ready Seld update evidence is incomplete")


def _transaction_summary(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    _validate_transaction(value)
    return {
        key: value.get(key)
        for key in (
            "token",
            "from_sha",
            "to_sha",
            "phase",
            "outcome",
            "updated_at",
            "error_code",
            "recovery_command",
        )
    }


def _validate_transaction(value: dict[str, Any]) -> None:
    if value.get("format_version") != TRANSACTION_FORMAT_VERSION:
        raise ValidationError("unsupported Seld update transaction")
    token = _uuid(value.get("token"), "update token")
    from_sha = _clean_sha(value.get("from_sha"), "transaction installed revision")
    to_sha = _clean_sha(value.get("to_sha"), "transaction candidate revision")
    if from_sha == to_sha:
        raise ValidationError("Seld update transaction revisions must differ")
    _clean_revision(value.get("check_revision"), "transaction check revision")
    _approval(value.get("approval_ref"))
    _parse_time(_required_string(value, "updated_at"))
    phase = value.get("phase")
    if phase not in _TRANSACTION_PHASES:
        raise ValidationError("Seld update transaction phase is invalid")
    outcome = value.get("outcome")
    if outcome is not None and outcome not in _TERMINAL_OUTCOMES:
        raise ValidationError("Seld update transaction outcome is invalid")
    if phase == "complete" and outcome not in _TERMINAL_OUTCOMES:
        raise ValidationError("complete Seld update transaction has no terminal outcome")
    if phase != "complete" and outcome is not None:
        raise ValidationError("in-progress Seld update transaction has a terminal outcome")
    tool_dir = Path(_required_string(value, "tool_dir"))
    active = Path(_required_string(value, "active_environment"))
    previous = Path(_required_string(value, "previous_environment"))
    failed = Path(_required_string(value, "failed_environment"))
    bin_dir = Path(_required_string(value, "bin_dir"))
    launcher = Path(_required_string(value, "launcher"))
    vault = Path(_required_string(value, "vault"))
    if not all(
        path.is_absolute()
        for path in (tool_dir, active, previous, failed, bin_dir, launcher, vault)
    ):
        raise ValidationError("Seld update transaction paths must be absolute")
    if (
        active != tool_dir / "gsv"
        or previous != tool_dir / f".gsv.previous.{token}"
        or failed != tool_dir / f".gsv.failed.{token}"
        or launcher != bin_dir / ("gsv.exe" if os.name == "nt" else "gsv")
    ):
        raise ValidationError("Seld update transaction paths do not match its token")
    _uuid(value.get("vault_id"), "transaction vault id")
    _clean_revision(value.get("vault_digest"), "transaction vault digest")
    if not isinstance(value.get("bridge_was_running"), bool):
        raise ValidationError("Seld update transaction Bridge state is invalid")
    error_code = value.get("error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or not error_code or len(error_code) > 128
    ):
        raise ValidationError("Seld update transaction error code is invalid")
    recovery = value.get("recovery_command")
    if recovery is not None and (
        not isinstance(recovery, str) or not recovery or len(recovery) > 4096
    ):
        raise ValidationError("Seld update transaction recovery command is invalid")
    recovery_launcher = value.get("recovery_launcher")
    recovery_launcher_digest = value.get("recovery_launcher_digest")
    if recovery_launcher is None and recovery_launcher_digest is None:
        pass
    elif (
        not isinstance(recovery_launcher, str)
        or Path(recovery_launcher) != bin_dir / RECOVERY_LAUNCHER_NAME
        or not isinstance(recovery_launcher_digest, str)
    ):
        raise ValidationError("Seld update recovery launcher evidence is invalid")
    else:
        _clean_revision(recovery_launcher_digest, "recovery launcher digest")
    recovery_approval = value.get("recovery_approval_ref")
    recovery_approval_digest = value.get("recovery_approval_vault_digest")
    if recovery_approval is None and recovery_approval_digest is None:
        return
    if recovery_approval is None or recovery_approval_digest is None:
        raise ValidationError("Seld update recovery approval evidence is incomplete")
    _approval(recovery_approval)
    _clean_revision(recovery_approval_digest, "recovery approval vault digest")


def _require_recovery_vault(transaction: dict[str, Any], vault: Vault) -> None:
    if Path(_required_string(transaction, "vault")) != vault.root:
        raise ConflictError("update recovery is bound to a different Seld vault")
    observed = vault.status()
    if observed.get("vault_id") != transaction.get("vault_id"):
        raise ConflictError("the Seld vault identity changed after this update transaction began")


def _read_transaction(*, required: bool) -> dict[str, Any] | None:
    value = _read_receipt(_transaction_path(), label="Seld update transaction")
    if value is None:
        if required:
            raise ValidationError("Seld update transaction is missing")
        return None
    _validate_transaction(value)
    return value


def _advance_transaction(
    transaction: dict[str, Any],
    *,
    phase: str,
    outcome: str | None | object = ...,
    error_code: str | None | object = ...,
    recovery_command: str | None | object = ...,
    **changes: Any,
) -> dict[str, Any]:
    next_value = dict(transaction)
    next_value.update(changes)
    next_value["phase"] = phase
    next_value["updated_at"] = _now()
    if outcome is not ...:
        next_value["outcome"] = outcome
    if error_code is not ...:
        next_value["error_code"] = error_code
    if recovery_command is not ...:
        next_value["recovery_command"] = recovery_command
    _write_receipt(_transaction_path(), next_value)
    return next_value


def _mark_repair_required(
    transaction: dict[str, Any],
    error_code: str,
    *,
    recovery_command: str | None = None,
) -> dict[str, Any]:
    command = recovery_command or _available_recovery_command(transaction)
    next_value = _advance_transaction(
        transaction,
        phase="complete",
        outcome="repair_required",
        error_code=error_code,
        recovery_command=str(command),
    )
    return _transaction_result(next_value)


def _mark_vault_reapproval_required(
    vault: Vault,
    transaction: dict[str, Any],
) -> dict[str, Any]:
    observed = vault.status()
    if observed.get("vault_id") != transaction.get("vault_id"):
        return _mark_repair_required(transaction, "vault_identity_changed_during_activation")
    digest = _clean_revision(observed.get("digest"), "current recovery vault digest")
    command = _vault_reapproval_command(transaction, digest)
    return _mark_repair_required(
        transaction,
        "vault_changed_during_activation",
        recovery_command=command,
    )


def _available_recovery_command(transaction: dict[str, Any]) -> str:
    token = _required_string(transaction, "token")
    vault = Path(_required_string(transaction, "vault"))
    try:
        launcher = _verify_recovery_launcher(transaction)
    except (OSError, SetupError, ValidationError):
        launcher = None
    if launcher is not None:
        return _recovery_launcher_command(launcher, token, vault)
    roots = (
        Path(_required_string(transaction, "previous_environment")),
        Path(_required_string(transaction, "active_environment")),
    )
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            _runtime_module_command(root)
        except SetupError:
            continue
        return _recovery_command(root, token, vault)
    active, previous = roots[1], roots[0]
    return (
        "Repair one retained Seld runtime, then recover this token: "
        f"active={active}; previous={previous}; token={token}"
    )


def _vault_reapproval_command(transaction: dict[str, Any], digest: str) -> str:
    token = _required_string(transaction, "token")
    vault = Path(_required_string(transaction, "vault"))
    approval_placeholder = "codex:<current-task-uuid>"
    try:
        launcher = _verify_recovery_launcher(transaction)
    except (OSError, SetupError, ValidationError):
        launcher = None
    if launcher is not None:
        return _recovery_launcher_command(
            launcher,
            token,
            vault,
            expected_vault_digest=digest,
            approval_ref=approval_placeholder,
        )
    for root in (
        Path(_required_string(transaction, "previous_environment")),
        Path(_required_string(transaction, "active_environment")),
    ):
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            _runtime_module_command(root)
        except SetupError:
            continue
        return _recovery_command(
            root,
            token,
            vault,
            expected_vault_digest=digest,
            approval_ref=approval_placeholder,
        )
    return (
        "Repair one retained Seld runtime, review the current vault against the verified "
        f"backup, then recover token {token} with expected vault digest {digest}."
    )


def _transaction_result(transaction: dict[str, Any]) -> dict[str, Any]:
    return {
        "updated": transaction.get("outcome") in {"installed", "installed_bridge_repair"},
        "rolled_back": transaction.get("outcome") == "rolled_back",
        "repair_required": transaction.get("outcome") == "repair_required",
        "from_sha": transaction.get("from_sha"),
        "to_sha": transaction.get("to_sha"),
        "outcome": transaction.get("outcome"),
        "phase": transaction.get("phase"),
        "error_code": transaction.get("error_code"),
        "recovery_command": transaction.get("recovery_command"),
        "transaction_revision": _receipt_revision(transaction),
        "vault_changed": transaction.get("vault_changed"),
        "vault_digest_after": transaction.get("vault_digest_after"),
    }


def _transaction_resolved(transaction: dict[str, Any]) -> bool:
    return (
        transaction.get("outcome") in {"installed", "rolled_back"}
        and transaction.get("recovery_command") is None
    )


def _read_receipt(path: Path, *, label: str) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    encoded = read_regular_file(path, label=label, max_bytes=MAX_RECEIPT_BYTES)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid {label}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"invalid {label}")
    return payload


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    root = _update_dir()
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ValidationError("Seld update receipt exceeds its size bound")
    atomic_write(path, encoded)


def _update_dir() -> Path:
    return data_dir() / "updates"


def _check_path() -> Path:
    return _update_dir() / "update-check.json"


def _transaction_path() -> Path:
    return _update_dir() / "update-transaction.json"


def _update_lock() -> Any:
    return exclusive_lock(
        data_dir() / "locks/update.lock",
        timeout=UPDATE_LOCK_TIMEOUT_SECONDS,
    )


def _receipt_revision(payload: dict[str, Any]) -> str:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return sha256_bytes(encoded)


def _recovery_command(
    previous: Path,
    token: str,
    vault: Path,
    *,
    expected_vault_digest: str | None = None,
    approval_ref: str | None = None,
) -> str:
    python = previous / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = (
        f"{_shell_quote(str(python))} -m continuity_kernel --vault {_shell_quote(str(vault))} "
        f"update recover --token {token}"
    )
    if expected_vault_digest is not None and approval_ref is not None:
        command += (
            f" --expected-vault-digest {_shell_quote(expected_vault_digest)}"
            f" --approval-ref {_shell_quote(approval_ref)}"
        )
    return command


def _recovery_launcher_command(
    launcher: Path,
    token: str,
    vault: Path,
    *,
    expected_vault_digest: str | None = None,
    approval_ref: str | None = None,
) -> str:
    command = (
        f"{_shell_quote(str(launcher))} --json --vault {_shell_quote(str(vault))} "
        f"update recover --token {token}"
    )
    if expected_vault_digest is not None and approval_ref is not None:
        command += (
            f" --expected-vault-digest {_shell_quote(expected_vault_digest)}"
            f" --approval-ref {_shell_quote(approval_ref)}"
        )
    return command


def _shell_quote(value: str) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    import shlex

    return shlex.quote(value)


def _network_error_code(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        if exc.code == 403:
            return "github_rate_limited_or_forbidden"
        return f"github_http_{exc.code}"
    if isinstance(exc, (URLError, TimeoutError)):
        return "github_unavailable"
    if isinstance(exc, ValidationError):
        return "github_response_invalid"
    return "update_check_failed"


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, ConflictError):
        return "update_conflict"
    if isinstance(exc, ValidationError):
        return "update_validation_failed"
    if isinstance(exc, SetupError):
        return "candidate_activation_failed"
    return "update_io_failed"


def _payload_sha(value: object, label: str) -> str:
    return _clean_sha(value, label)


def _clean_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValidationError(f"{label} must be one full lowercase Git commit SHA")
    return value


def _clean_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ValidationError(f"{label} must be one full lowercase SHA-256 revision")
    return value


def _approval(value: object) -> str:
    if not isinstance(value, str) or _APPROVAL_REF.fullmatch(value) is None:
        raise ValidationError("update approval must name the current Codex task as codex:<uuid>")
    return value


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a UUID")
    try:
        clean = str(uuid.UUID(value))
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UUID") from exc
    if clean != value.lower():
        raise ValidationError(f"{label} must be a canonical UUID")
    return clean


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValidationError(f"Seld update receipt has an invalid {key}")
    return value


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
