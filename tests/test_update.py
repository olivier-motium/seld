from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError

import pytest

import continuity_kernel.update as self_update
from continuity_kernel import cli, codex_integration, whatsapp
from continuity_kernel.errors import ConflictError, SetupError, ValidationError
from continuity_kernel.vault import Vault

FROM_SHA = "1" * 40
TO_SHA = "2" * 40
APPROVAL_REF = "codex:019f0000-0000-7000-8000-000000000777"
NATIVE_BEFORE = "4" * 64
NATIVE_CANDIDATE = "5" * 64
NATIVE_RESTORED = "6" * 64
SOURCE_UPDATE_RUNTIME_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="source self-update is macOS-only"
)


def _installed(tmp_path: Path, *, sha: str = FROM_SHA) -> self_update.InstallProvenance:
    environment = tmp_path / "tools" / "gsv"
    environment.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bin").mkdir(parents=True, exist_ok=True)
    return self_update.InstallProvenance(
        supported=True,
        install_mode="uv-source",
        version="0.3.0",
        sha=sha,
        repository=self_update.REPOSITORY_URL,
        environment=str(environment),
        launcher=str(tmp_path / "bin" / "gsv"),
        tool_dir=str(environment.parent),
        bin_dir=str(tmp_path / "bin"),
        python=str(environment / "bin" / "python"),
    )


def _check_receipt(*, checked_at: str | None = None) -> dict[str, Any]:
    return {
        "format_version": self_update.CHECK_FORMAT_VERSION,
        "checked_at": checked_at or self_update._now(),
        "from_sha": FROM_SHA,
        "state": "available",
        "candidate": {
            "sha": TO_SHA,
            "committed_at": "2026-07-28T20:00:00Z",
            "verified": True,
            "verification_reason": "valid",
            "ci_checks": len(self_update.REQUIRED_CHECK_NAMES),
            "check_names": sorted(self_update.REQUIRED_CHECK_NAMES),
        },
        "error_code": None,
    }


def _transaction_receipt(tmp_path: Path, vault: Vault) -> dict[str, Any]:
    token = "019f0000-0000-7000-8000-000000000778"
    tool_dir = tmp_path / "tools"
    vault_status = vault.status()
    return {
        "format_version": self_update.LEGACY_TRANSACTION_FORMAT_VERSION,
        "token": token,
        "from_sha": FROM_SHA,
        "to_sha": TO_SHA,
        "check_revision": "3" * 64,
        "approval_ref": APPROVAL_REF,
        "started_at": "2026-07-28T19:59:00Z",
        "updated_at": "2026-07-28T20:00:00Z",
        "phase": "candidate_installed",
        "outcome": None,
        "error_code": None,
        "recovery_command": "gsv update recover --token exact",
        "active_environment": str(tool_dir / "gsv"),
        "previous_environment": str(tool_dir / f".gsv.previous.{token}"),
        "failed_environment": str(tool_dir / f".gsv.failed.{token}"),
        "tool_dir": str(tool_dir),
        "bin_dir": str(tmp_path / "bin"),
        "launcher": str(tmp_path / "bin" / ("gsv.exe" if os.name == "nt" else "gsv")),
        "python_version": "3.11",
        "vault": str(vault.root),
        "vault_id": vault_status["vault_id"],
        "vault_digest": vault_status["digest"],
        "bridge_was_running": False,
        "bridge_instance_before": None,
        "backup": None,
    }


def _absent_native_evidence(tmp_path: Path) -> dict[str, Any]:
    return {
        "native_bridge_was_installed": False,
        "native_bridge_application": str(tmp_path / "Applications/Seld.app"),
        "native_bridge_revision_before": "absent",
        "native_bridge_revision_current": "absent",
    }


def _absent_native_status(tmp_path: Path) -> dict[str, Any]:
    return {
        "application": str(tmp_path / "Applications/Seld.app"),
        "healthy": False,
        "installed": False,
        "owned": False,
        "ownership_revision": "absent",
    }


def _with_absent_native(transaction: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    transaction["format_version"] = self_update.TRANSACTION_FORMAT_VERSION
    transaction.update(_absent_native_evidence(tmp_path))
    return transaction


def _with_owned_native(transaction: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    transaction["format_version"] = self_update.TRANSACTION_FORMAT_VERSION
    transaction.update(
        native_bridge_was_installed=True,
        native_bridge_application=str(tmp_path / "Applications/Seld.app"),
        native_bridge_revision_before=NATIVE_BEFORE,
        native_bridge_revision_current=NATIVE_BEFORE,
    )
    return transaction


def _native_status(
    transaction: dict[str, Any],
    *,
    revision: str,
    current: bool,
) -> dict[str, Any]:
    return {
        "application": transaction["native_bridge_application"],
        "current": current,
        "healthy": True,
        "installed": True,
        "owned": True,
        "ownership_revision": revision,
        "vault": transaction["vault"],
        "vault_id": transaction["vault_id"],
    }


def _legacy_owned_native_status(
    transaction: dict[str, Any],
    tmp_path: Path,
    *,
    revision: str,
    current: bool,
    **changes: Any,
) -> dict[str, Any]:
    payload = {
        "application": str(tmp_path / "Applications/Seld.app"),
        "current": current,
        "healthy": True,
        "installed": True,
        "owned": True,
        "ownership_revision": revision,
        "receipt_revision": "7" * 64,
        "vault": transaction["vault"],
        "vault_id": transaction["vault_id"],
    }
    payload.update(changes)
    return payload


def _json_command(payload: dict[str, Any]) -> self_update.CommandResult:
    encoded = json.dumps({"ok": True, "result": payload}).encode("utf-8")
    return self_update.CommandResult(0, encoded, b"")


def _write_receipt_bound_mcp(
    tmp_path: Path,
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
    *,
    service_label: str | None,
) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "data"))
    marketplace = codex_integration._marketplace_root(home.resolve())
    mcp = marketplace / self_update.INSTALLED_MCP_RELATIVE
    mcp.parent.mkdir(parents=True)
    environment = {"GSV_VAULT": str(vault.root), "UNRELATED_SETTING": "ignored"}
    if service_label is not None:
        environment[whatsapp.SERVICE_LABEL_ENV] = service_label
    mcp.write_text(
        json.dumps({"mcpServers": {"gsv": {"env": environment}}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = codex_integration._tree_manifest(marketplace)
    receipt = {
        "cleanup_pending": [],
        "codex_home": str(home.resolve()),
        "format_version": codex_integration.RECEIPT_FORMAT_VERSION,
        "integration_active": True,
        "marketplace_digest": codex_integration._tree_digest_from_manifest(manifest),
        "marketplace_manifest": manifest,
        "marketplace_owned": True,
        "marketplace_root": str(marketplace),
        "plugin_owned": True,
    }
    receipt_path = codex_integration._receipt_path(home.resolve())
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return mcp


def _github_fetcher(
    *,
    verified: bool = True,
    check_head: str = TO_SHA,
    check_names: tuple[str, ...] | None = None,
) -> Callable[[str], tuple[dict[str, Any], dict[str, str]]]:
    names = check_names or tuple(sorted(self_update.REQUIRED_CHECK_NAMES))
    responses: dict[str, dict[str, Any]] = {
        f"{self_update.API_ROOT}/commits/main": {
            "sha": TO_SHA,
            "commit": {
                "committer": {"date": "2026-07-28T20:00:00Z"},
                "verification": {"verified": verified, "reason": "valid"},
            },
        },
        f"{self_update.API_ROOT}/compare/{FROM_SHA}...{TO_SHA}?per_page=1": {
            "status": "ahead",
            "behind_by": 0,
            "ahead_by": 1,
        },
        f"{self_update.API_ROOT}/commits/{TO_SHA}/check-runs?per_page=100": {
            "total_count": len(names),
            "check_runs": [
                {
                    "name": name,
                    "head_sha": check_head,
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"slug": "github-actions"},
                }
                for name in names
            ],
        },
    }

    def fetch(url: str) -> tuple[dict[str, Any], dict[str, str]]:
        return responses[url], {}

    return fetch


def test_status_reads_only_cached_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    monkeypatch.setattr(
        self_update,
        "build_opener",
        lambda *_: pytest.fail("status must not construct a network client"),
    )
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)

    result = self_update.status()

    assert result["state"] == "available"
    assert result["candidate"]["sha"] == TO_SHA
    assert result["check_revision"] == self_update._receipt_revision(receipt)
    assert result["transaction"] is None


def test_check_honors_the_six_hour_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    recent = (datetime.now(UTC) - timedelta(hours=5, minutes=59)).isoformat().replace("+00:00", "Z")
    receipt = _check_receipt(checked_at=recent)
    self_update._write_receipt(self_update._check_path(), receipt)
    calls: list[str] = []

    def fetch(url: str) -> tuple[dict[str, Any], dict[str, str]]:
        calls.append(url)
        raise AssertionError("fresh cached checks must not use the network")

    result = self_update.check(fetcher=fetch)

    assert calls == []
    assert result["check_revision"] == self_update._receipt_revision(receipt)


def test_check_returns_cached_state_when_an_update_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)

    @contextmanager
    def busy() -> Iterator[None]:
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(self_update, "_update_lock", busy)
    result = self_update.check(
        force=True,
        fetcher=lambda _url: pytest.fail("a busy update check must not use the network"),
    )

    assert result["state"] == "available"
    assert result["check_revision"] == self_update._receipt_revision(receipt)


def test_verified_green_descendant_is_the_exact_available_candidate() -> None:
    result = self_update._resolve_public_main(FROM_SHA, fetcher=_github_fetcher())

    assert result == {
        "state": "available",
        "candidate": {
            "sha": TO_SHA,
            "committed_at": "2026-07-28T20:00:00Z",
            "verified": True,
            "verification_reason": "valid",
            "ci_checks": len(self_update.REQUIRED_CHECK_NAMES),
            "check_names": sorted(self_update.REQUIRED_CHECK_NAMES),
        },
        "error_code": None,
    }


@pytest.mark.parametrize(
    ("fetcher", "error_code"),
    [
        (_github_fetcher(verified=False), "commit_not_verified"),
        (_github_fetcher(check_head="3" * 40), "checks_not_green"),
        (_github_fetcher(check_names=("decoy",)), "checks_not_green"),
    ],
)
def test_candidate_requires_verified_commit_and_checks_for_the_exact_head(
    fetcher: self_update.JsonFetcher, error_code: str
) -> None:
    result = self_update._resolve_public_main(FROM_SHA, fetcher=fetcher)

    assert result["state"] == "not_ready"
    assert result["error_code"] == error_code
    assert result["candidate"]["sha"] == TO_SHA


@pytest.mark.parametrize(
    "commit",
    [
        {"sha": "ABC"},
        {"sha": TO_SHA, "commit": {}, "verification": {}},
        {
            "sha": TO_SHA,
            "commit": {
                "committer": {"date": "x" * 65},
                "verification": {"verified": True, "reason": "valid"},
            },
        },
        {
            "sha": TO_SHA,
            "commit": {
                "committer": {"date": "nope"},
                "verification": {"verified": True, "reason": "valid"},
            },
        },
    ],
)
def test_malformed_github_commit_responses_fail_closed(commit: dict[str, Any]) -> None:
    def fetch(_url: str) -> tuple[dict[str, Any], dict[str, str]]:
        return commit, {}

    with pytest.raises(ValidationError):
        self_update._resolve_public_main(FROM_SHA, fetcher=fetch)


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


@pytest.mark.parametrize(
    "response",
    [
        _Response(b"not-json"),
        _Response(b"{}", {"Content-Length": str(self_update.MAX_NETWORK_BYTES + 1)}),
        _Response(b"x" * (self_update.MAX_NETWORK_BYTES + 1)),
    ],
)
def test_fetch_json_rejects_malformed_or_oversized_responses(
    response: _Response, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
    monkeypatch.setattr(self_update, "build_opener", lambda *_: opener)

    with pytest.raises(ValidationError):
        self_update._fetch_json(f"{self_update.API_ROOT}/commits/main")


@SOURCE_UPDATE_RUNTIME_ONLY
def test_command_timeout_terminates_the_entire_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "late-child-write"
    child = (
        f"import pathlib,time; time.sleep(0.4); pathlib.Path({str(marker)!r}).write_text('late')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )

    with pytest.raises(SetupError, match="timed out"):
        self_update._run_command(
            [sys.executable, "-c", parent],
            os.environ.copy(),
            0.1,
        )
    time.sleep(0.6)

    assert not marker.exists()


def test_command_output_is_drained_into_a_hard_memory_bound() -> None:
    payload_size = self_update.MAX_COMMAND_OUTPUT_BYTES + 64 * 1024

    with pytest.raises(SetupError, match="output bound"):
        self_update._run_command(
            [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {payload_size})"],
            os.environ.copy(),
            5.0,
        )


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (URLError("offline"), "github_unavailable"),
        (ValidationError("malformed"), "github_response_invalid"),
        (OSError("socket"), "update_check_failed"),
    ],
)
def test_failed_check_preserves_the_last_successful_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    error_code: str,
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    previous = _check_receipt()
    self_update._write_receipt(self_update._check_path(), previous)

    def fail(_url: str) -> tuple[dict[str, Any], dict[str, str]]:
        raise failure

    result = self_update.check(force=True, fetcher=fail)
    persisted = self_update._read_receipt(self_update._check_path(), label="Seld update check")

    assert result["state"] == "unavailable"
    assert result["error_code"] == error_code
    assert persisted is not None
    assert persisted["candidate"] is None
    assert persisted["last_success"] == {
        "candidate": previous["candidate"],
        "checked_at": previous["checked_at"],
        "state": "available",
    }


@pytest.mark.parametrize(
    "change",
    (
        {"verified": False},
        {"ci_checks": 0},
        {"committed_at": "not-a-time"},
        {"check_names": ["decoy"]},
    ),
)
def test_forged_available_receipt_cannot_authorize_an_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    receipt = _check_receipt()
    receipt["candidate"].update(change)
    self_update._write_receipt(self_update._check_path(), receipt)

    with pytest.raises((ValidationError, ValueError)):
        self_update.status()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_recovery_preserves_concurrent_vault_change_and_rejects_unbound_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bound recovery vault")
    transaction = _transaction_receipt(tmp_path, vault)
    self_update._write_receipt(self_update._transaction_path(), transaction)
    calls: list[list[str]] = []

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        calls.append(command)
        return self_update.CommandResult(0, b"", b"")

    now = vault.read_document("NOW.md")
    vault.write_document(
        "NOW.md",
        "changed after update began",
        expected_revision=now["revision"],
    )
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: FROM_SHA)
    monkeypatch.setattr(
        self_update,
        "_finish_restored",
        lambda *_args, **_kwargs: {"outcome": "rolled_back"},
    )
    assert self_update.recover(vault, token=transaction["token"], runner=runner) == {
        "outcome": "rolled_back"
    }
    assert vault.read_document("NOW.md")["content"] == "changed after update began\n"
    assert calls == []

    transaction = _transaction_receipt(tmp_path, vault)
    transaction["previous_environment"] = str(tmp_path / "outside")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    with pytest.raises(ValidationError, match="paths do not match"):
        self_update.recover(vault, token=transaction["token"], runner=runner)
    assert calls == []


@SOURCE_UPDATE_RUNTIME_ONLY
def test_recovery_quarantines_partial_install_and_restores_verified_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Interrupted update recovery")
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "previous_preserved"
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    failed = Path(transaction["failed_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    (active / "partial").write_text("candidate", encoding="utf-8")
    (previous / "working").write_text("previous", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else None,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)

    def finish(
        _vault: Vault,
        current: dict[str, Any],
        *,
        runner: self_update.CommandRunner,
    ) -> dict[str, Any]:
        del runner
        assert current["phase"] == "previous_restored"
        assert (active / "working").read_text(encoding="utf-8") == "previous"
        assert (failed / "partial").read_text(encoding="utf-8") == "candidate"
        return {"outcome": "rolled_back"}

    monkeypatch.setattr(self_update, "_finish_restored", finish)

    result = self_update.recover(
        vault,
        token=transaction["token"],
        runner=lambda *_args: pytest.fail("partial-install recovery must not run a candidate"),
    )

    assert result == {"outcome": "rolled_back"}
    assert not previous.exists()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_rollback_restores_missing_stable_launcher_after_partial_uv_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Launcher rollback")
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "previous_preserved"
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    launcher = Path(transaction["launcher"])
    active.mkdir(parents=True)
    (active / "partial").write_text("candidate", encoding="utf-8")
    previous_executable = previous / "bin/gsv"
    previous_executable.parent.mkdir(parents=True)
    previous_executable.write_text("previous", encoding="utf-8")
    launcher.parent.mkdir(parents=True)
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else None,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(_absent_native_status(tmp_path))
        return self_update.CommandResult(0, b"", b"")

    result = self_update._rollback_after_failure(
        vault,
        transaction,
        ValidationError("synthetic partial install"),
        runner=runner,
    )

    assert result["outcome"] == "rolled_back"
    assert launcher.is_symlink()
    assert launcher.resolve(strict=True) == (active / "bin/gsv").resolve(strict=True)
    assert (active / "bin/gsv").read_text(encoding="utf-8") == "previous"


def test_rollback_rejects_wrong_preserved_revision_before_any_execution_or_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Wrong preserved revision")
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "previous_preserved"
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active_executable = active / "bin/gsv"
    previous_python = previous / "bin/python"
    active_executable.parent.mkdir(parents=True)
    previous_python.parent.mkdir(parents=True)
    active_executable.write_text("candidate", encoding="utf-8")
    previous_python.write_text("wrong previous", encoding="utf-8")
    launcher = Path(transaction["launcher"])
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(active_executable)
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: "f" * 40 if root == previous else TO_SHA,
    )
    monkeypatch.setattr(
        self_update,
        "_runtime_reads_vault",
        lambda *_args: pytest.fail("wrong preserved code must not execute"),
    )
    monkeypatch.setattr(
        self_update,
        "move_no_replace",
        lambda *_args: pytest.fail("wrong preserved environment must not move"),
    )

    result = self_update._rollback_after_failure(
        vault,
        transaction,
        ValidationError("synthetic candidate failure"),
        runner=lambda *_args: pytest.fail("wrong preserved code must not run commands"),
    )

    assert result["outcome"] == "repair_required"
    assert result["error_code"] == "previous_revision_mismatch"
    assert str(result["recovery_command"]).startswith(
        "Do not execute the preserved Seld environment"
    )
    assert active.is_dir()
    assert previous.is_dir()
    assert launcher.resolve(strict=True) == active_executable.resolve(strict=True)
    assert not Path(transaction["failed_environment"]).exists()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_launcher_repair_never_replaces_an_existing_entry(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Launcher no-clobber")
    transaction = _transaction_receipt(tmp_path, vault)
    active = Path(transaction["active_environment"])
    executable = active / "bin/gsv"
    executable.parent.mkdir(parents=True)
    executable.write_text("runtime", encoding="utf-8")
    launcher = Path(transaction["launcher"])
    launcher.parent.mkdir(parents=True)
    launcher.write_text("user-owned", encoding="utf-8")

    with pytest.raises(SetupError, match="not a symbolic link"):
        self_update._verify_external_launcher(transaction, repair_missing=True)

    assert launcher.read_text(encoding="utf-8") == "user-owned"


@SOURCE_UPDATE_RUNTIME_ONLY
@pytest.mark.parametrize("failure", ("setup", "launcher"))
def test_restored_failure_reports_an_existing_token_recovery_runtime(
    tmp_path: Path,
    failure: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Restored recovery command")
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "restoring_previous"
    active = Path(transaction["active_environment"])
    executable = active / "bin/gsv"
    python = active / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text("runtime", encoding="utf-8")
    python.write_text("python", encoding="utf-8")
    launcher = Path(transaction["launcher"])
    launcher.parent.mkdir(parents=True)
    if failure == "setup":
        launcher.symlink_to(executable)
    else:
        launcher.write_text("foreign", encoding="utf-8")

    def runner(*_args: Any) -> self_update.CommandResult:
        if failure == "launcher":
            pytest.fail("launcher conflict must stop before setup")
        return self_update.CommandResult(86, b"", b"")

    result = self_update._finish_restored(vault, transaction, runner=runner)

    assert result["outcome"] == "repair_required"
    command = shlex.split(str(result["recovery_command"]))
    assert command == [
        str(python),
        "-m",
        "continuity_kernel",
        "--vault",
        str(vault.root),
        "update",
        "recover",
        "--token",
        transaction["token"],
    ]
    assert Path(command[0]).is_file()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_unchanged_preflight_failure_never_points_at_an_absent_previous_runtime(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unchanged recovery command")
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "preflighting"
    active = Path(transaction["active_environment"])
    executable = active / "bin/gsv"
    python = active / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text("runtime", encoding="utf-8")
    python.write_text("python", encoding="utf-8")

    result = self_update._finish_unchanged(
        vault,
        transaction,
        runner=lambda *_args: pytest.fail("missing launcher must stop before health checks"),
    )

    assert result["outcome"] == "repair_required"
    command = shlex.split(str(result["recovery_command"]))
    assert command[0] == str(python)
    assert Path(command[0]).is_file()
    assert str(Path(transaction["previous_environment"])) not in command[0]


@SOURCE_UPDATE_RUNTIME_ONLY
def test_owned_recovery_launcher_survives_swap_and_sanitizes_python_state(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Recovery launcher")
    transaction = _transaction_receipt(tmp_path, vault)
    previous = Path(transaction["previous_environment"])
    active = Path(transaction["active_environment"])
    python = previous / "bin/python"
    python.parent.mkdir(parents=True)
    report = tmp_path / "recovery-args.txt"
    python.write_text(
        "#!/bin/sh\n"
        "{\n"
        "  printf 'PYTHONHOME=%s\\n' \"${PYTHONHOME-unset}\"\n"
        "  printf 'PYTHONPATH=%s\\n' \"${PYTHONPATH-unset}\"\n"
        "  printf 'VIRTUAL_ENV=%s\\n' \"${VIRTUAL_ENV-unset}\"\n"
        "  printf 'ARG=%s\\n' \"$@\"\n"
        f"}} > {shlex.quote(str(report))}\n",
        encoding="utf-8",
    )
    python.chmod(0o700)
    Path(transaction["bin_dir"]).mkdir(parents=True)
    content = self_update._recovery_launcher_content(previous, active)
    launcher = self_update._recovery_launcher_path(transaction)
    transaction.update(
        recovery_launcher=str(launcher),
        recovery_launcher_digest=hashlib.sha256(content).hexdigest(),
    )
    self_update._publish_recovery_launcher(transaction, content)
    environment = os.environ.copy()
    environment.update(
        PYTHONHOME="hostile-home",
        PYTHONPATH="hostile-path",
        VIRTUAL_ENV="hostile-environment",
    )

    subprocess.run([str(launcher), "--json", "update", "status"], check=True, env=environment)
    first = report.read_text(encoding="utf-8")
    previous.rename(active)
    subprocess.run([str(launcher), "--json", "update", "status"], check=True, env=environment)

    assert first == report.read_text(encoding="utf-8")
    assert first.splitlines() == [
        "PYTHONHOME=unset",
        "PYTHONPATH=unset",
        "VIRTUAL_ENV=unset",
        "ARG=-I",
        "ARG=-m",
        "ARG=continuity_kernel",
        "ARG=--json",
        "ARG=update",
        "ARG=status",
    ]
    assert launcher.stat().st_mode & 0o777 == 0o700
    self_update._cleanup_recovery_launcher(transaction)
    assert not launcher.exists()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_recovery_launcher_never_replaces_a_foreign_entry(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Recovery launcher no-clobber")
    transaction = _transaction_receipt(tmp_path, vault)
    previous = Path(transaction["previous_environment"])
    active = Path(transaction["active_environment"])
    launcher = self_update._recovery_launcher_path(transaction)
    launcher.parent.mkdir(parents=True)
    launcher.write_text("user-owned\n", encoding="utf-8")
    content = self_update._recovery_launcher_content(previous, active)
    transaction.update(
        recovery_launcher=str(launcher),
        recovery_launcher_digest=hashlib.sha256(content).hexdigest(),
    )

    with pytest.raises(SetupError, match=r"owned executable|does not match"):
        self_update._publish_recovery_launcher(transaction, content)

    assert launcher.read_text(encoding="utf-8") == "user-owned\n"


@SOURCE_UPDATE_RUNTIME_ONLY
def test_apply_verifies_backup_before_any_candidate_preflight(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from continuity_kernel import bridge

    installed = _installed(tmp_path)
    monkeypatch.setattr(self_update, "installed_provenance", lambda: installed)
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)
    monkeypatch.setattr(
        bridge,
        "bridge_status",
        lambda: {"running": False, "health_unavailable": False, "instance_id": None},
    )
    monkeypatch.setattr(
        bridge,
        "stop_bridge",
        lambda: pytest.fail("preflight failure must not touch the Bridge"),
    )
    events: list[str] = []
    create_backup = vault.create_backup

    def backup() -> dict[str, Any]:
        events.append("backup")
        return create_backup()

    def preflight(_sha: str, _runner: self_update.CommandRunner) -> None:
        events.append("preflight")
        raise ValidationError("synthetic candidate rejection")

    def verify(
        _vault: Vault,
        transaction: dict[str, Any],
        *,
        runner: self_update.CommandRunner,
        expected_sha: str,
    ) -> None:
        del runner
        events.append("verify")
        assert transaction["backup"] is not None
        assert expected_sha == FROM_SHA

    monkeypatch.setattr(vault, "create_backup", backup)
    monkeypatch.setattr(self_update, "_preflight_candidate", preflight)
    monkeypatch.setattr(
        self_update,
        "_native_bridge_before",
        lambda *_args: _absent_native_evidence(tmp_path),
    )
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: FROM_SHA)
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", verify)
    monkeypatch.setattr(
        self_update,
        "_verify_unchanged_native_bridge",
        lambda *_args, **_kwargs: None,
    )

    result = self_update._apply_locked(
        vault,
        from_sha=FROM_SHA,
        to_sha=TO_SHA,
        expected_check_revision=self_update._receipt_revision(receipt),
        approval_ref=APPROVAL_REF,
        runner=lambda *_args: pytest.fail("candidate command must not run"),
    )

    assert result["outcome"] == "rolled_back"
    assert events == ["backup", "preflight", "verify"]


@SOURCE_UPDATE_RUNTIME_ONLY
def test_concurrent_vault_write_during_preflight_is_retained(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from continuity_kernel import bridge

    installed = _installed(tmp_path)
    active = Path(installed.environment or "")
    active.mkdir(parents=True)
    monkeypatch.setattr(self_update, "installed_provenance", lambda: installed)
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)
    monkeypatch.setattr(
        bridge,
        "bridge_status",
        lambda: {"running": False, "health_unavailable": False},
    )

    def preflight(_sha: str, _runner: self_update.CommandRunner) -> None:
        now = vault.read_document("NOW.md")
        vault.write_document(
            "NOW.md",
            "legitimate concurrent Pulse change",
            expected_revision=now["revision"],
        )

    monkeypatch.setattr(self_update, "_preflight_candidate", preflight)
    monkeypatch.setattr(self_update, "_install_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        self_update,
        "_native_bridge_before",
        lambda *_args: _absent_native_evidence(tmp_path),
    )
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/synthetic/gsv"])
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: FROM_SHA)
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(
                {
                    "application": str(tmp_path / "Applications/Seld.app"),
                    "error_code": None,
                    "healthy": False,
                    "installed": False,
                    "owned": False,
                    "ownership_revision": "absent",
                }
            )
        return self_update.CommandResult(0, b"", b"")

    result = self_update._apply_locked(
        vault,
        from_sha=FROM_SHA,
        to_sha=TO_SHA,
        expected_check_revision=self_update._receipt_revision(receipt),
        approval_ref=APPROVAL_REF,
        runner=runner,
    )

    assert result["outcome"] == "installed"
    assert result["vault_changed"] is True
    assert vault.read_document("NOW.md")["content"] == "legitimate concurrent Pulse change\n"


def test_candidate_setup_cannot_report_success_after_mutating_canonical_bytes(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "candidate_installed"
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/synthetic/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)

    def mutating_setup(
        _command: list[str], _environment: Any, _timeout: float
    ) -> self_update.CommandResult:
        (vault.root / "NOW.md").write_text("candidate mutation\n", encoding="utf-8")
        return self_update.CommandResult(0, b"", b"")

    with pytest.raises(SetupError, match="canonical vault bytes"):
        self_update._finish_candidate(
            vault,
            transaction,
            runner=mutating_setup,
            recovering=False,
        )


def test_update_anchors_only_a_current_owned_native_app(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from continuity_kernel import bridge_launcher

    installed = _installed(tmp_path)
    application = tmp_path / "Applications/Seld.app"
    observed = {
        "application": str(application),
        "current": True,
        "healthy": True,
        "installed": True,
        "owned": True,
        "ownership_revision": NATIVE_BEFORE,
        "vault": str(vault.root),
        "vault_id": vault.identity()["vault_id"],
    }
    monkeypatch.setattr(bridge_launcher, "native_bridge_status", lambda **_kwargs: observed)

    assert self_update._native_bridge_before(vault, installed) == {
        "native_bridge_application": str(application),
        "native_bridge_revision_before": NATIVE_BEFORE,
        "native_bridge_revision_current": NATIVE_BEFORE,
        "native_bridge_was_installed": True,
    }

    observed.update(current=False)
    with pytest.raises(SetupError, match="needs repair before self-update"):
        self_update._native_bridge_before(vault, installed)

    observed.update(current=True, owned=False, error_code="foreign_install")
    with pytest.raises(SetupError, match="needs repair before self-update"):
        self_update._native_bridge_before(vault, installed)


def test_foreign_native_app_blocks_update_before_backup_or_transaction(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from continuity_kernel import bridge, bridge_launcher

    installed = _installed(tmp_path)
    monkeypatch.setattr(self_update, "installed_provenance", lambda: installed)
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)
    monkeypatch.setattr(
        bridge,
        "bridge_status",
        lambda: {"running": False, "health_unavailable": False, "instance_id": None},
    )
    monkeypatch.setattr(
        bridge_launcher,
        "native_bridge_status",
        lambda **_kwargs: {
            "application": str(tmp_path / "Applications/Seld.app"),
            "current": False,
            "error_code": "foreign_install",
            "healthy": False,
            "installed": True,
            "owned": False,
            "ownership_revision": "absent",
        },
    )
    monkeypatch.setattr(
        vault,
        "create_backup",
        lambda: pytest.fail("foreign native state must fail before backup"),
    )

    with pytest.raises(SetupError, match="needs repair before self-update"):
        self_update._apply_locked(
            vault,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            expected_check_revision=self_update._receipt_revision(receipt),
            approval_ref=APPROVAL_REF,
            runner=lambda *_args: pytest.fail("foreign native state must not run commands"),
        )

    assert not self_update._transaction_path().exists()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_candidate_native_app_activation_is_last_and_exact_cas(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _with_owned_native(_transaction_receipt(tmp_path, vault), tmp_path)
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/candidate/bin/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    events: list[str] = []
    statuses = iter(
        (
            _native_status(transaction, revision=NATIVE_BEFORE, current=False),
            _native_status(transaction, revision=NATIVE_CANDIDATE, current=True),
        )
    )

    def runner(command: list[str], environment: Any, _timeout: float) -> self_update.CommandResult:
        if "setup" in command:
            events.append("setup")
            return self_update.CommandResult(0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            events.append("native-status")
            return _json_command(next(statuses))
        if "native-install" in command:
            events.append("native-install")
            assert command[-2:] == ["--expected-revision", NATIVE_BEFORE]
            assert str(environment["PATH"]).split(os.pathsep, 1)[0] == "/candidate/bin"
            return _json_command(
                {
                    **_native_status(
                        transaction,
                        revision=NATIVE_CANDIDATE,
                        current=True,
                    ),
                    "changed": True,
                }
            )
        raise AssertionError(f"unexpected command: {command}")

    result = self_update._finish_candidate(
        vault,
        transaction,
        runner=runner,
        recovering=False,
    )
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == "installed"
    assert result["native_bridge_revision_before"] == NATIVE_BEFORE
    assert result["native_bridge_revision_current"] == NATIVE_CANDIDATE
    assert events == ["setup", "native-status", "native-install", "native-status"]
    assert persisted is not None
    assert persisted["phase"] == "complete"
    assert persisted["native_bridge_revision_current"] == NATIVE_CANDIDATE


def test_update_does_not_create_or_adopt_an_unselected_native_app(
    vault: Vault,
    tmp_path: Path,
) -> None:
    transaction = _with_absent_native(
        _transaction_receipt(tmp_path, vault),
        tmp_path,
    )
    self_update._write_receipt(self_update._transaction_path(), transaction)

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(
                {
                    "application": transaction["native_bridge_application"],
                    "healthy": False,
                    "installed": False,
                    "owned": False,
                    "ownership_revision": "absent",
                }
            )
        raise AssertionError(f"an absent native app must not be installed: {command}")

    reconciled = self_update._reconcile_native_bridge(
        vault,
        transaction,
        command=["/candidate/bin/gsv"],
        expected_sha=TO_SHA,
        phase="activating_native_bridge",
        complete_phase="native_bridge_activated",
        runner=runner,
    )

    assert reconciled["phase"] == "native_bridge_activated"
    assert reconciled["native_bridge_revision_current"] == "absent"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("native_bridge_application", "relative/Seld.app"),
        ("native_bridge_revision_current", "absent"),
    ),
)
def test_update_rejects_invalid_installed_native_transaction_evidence(
    vault: Vault,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    transaction = _with_owned_native(_transaction_receipt(tmp_path, vault), tmp_path)
    transaction[field] = value

    with pytest.raises(ValidationError, match="native"):
        self_update._validate_transaction(transaction)


@pytest.mark.parametrize(
    "phase",
    (
        "candidate_installed",
        "activating_native_bridge",
        "native_bridge_activated",
        "restoring_native_bridge",
        "native_bridge_restored",
    ),
)
def test_current_update_transaction_requires_complete_native_evidence(
    vault: Vault,
    tmp_path: Path,
    phase: str,
) -> None:
    transaction = _with_absent_native(_transaction_receipt(tmp_path, vault), tmp_path)
    transaction["phase"] = phase
    for field in (
        "native_bridge_was_installed",
        "native_bridge_application",
        "native_bridge_revision_before",
        "native_bridge_revision_current",
    ):
        transaction.pop(field)

    with pytest.raises(ValidationError, match="native-app evidence is incomplete"):
        self_update._validate_transaction(transaction)


def test_legacy_update_transaction_cannot_claim_native_lifecycle_phase(
    vault: Vault,
    tmp_path: Path,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    self_update._validate_transaction(transaction)
    transaction["phase"] = "activating_native_bridge"
    with pytest.raises(ValidationError, match=r"legacy.*native-app evidence"):
        self_update._validate_transaction(transaction)


@SOURCE_UPDATE_RUNTIME_ONLY
def test_legacy_candidate_recovery_reconciles_owned_old_native_before_cleanup(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    (active / "candidate").write_text("candidate", encoding="utf-8")
    (previous / "previous").write_text("previous", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: TO_SHA)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/candidate/bin/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    legacy_matches: list[Path] = []
    monkeypatch.setattr(
        self_update,
        "_require_native_app_matches_legacy_runtime",
        lambda _status, _transaction, *, legacy_environment: legacy_matches.append(
            legacy_environment
        ),
    )
    events: list[str] = []
    statuses = iter(
        (
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_BEFORE,
                current=False,
            ),
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_BEFORE,
                current=False,
            ),
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_CANDIDATE,
                current=True,
            ),
        )
    )

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        if "setup" in command:
            events.append("setup")
            return self_update.CommandResult(0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            events.append("native-status")
            return _json_command(next(statuses))
        if "native-install" in command:
            events.append("native-install")
            assert previous.is_dir()
            assert command[-2:] == ["--expected-revision", NATIVE_BEFORE]
            return _json_command(
                {
                    **_legacy_owned_native_status(
                        transaction,
                        tmp_path,
                        revision=NATIVE_CANDIDATE,
                        current=True,
                    ),
                    "changed": True,
                }
            )
        raise AssertionError(f"unexpected command: {command}")

    result = self_update.recover(vault, token=transaction["token"], runner=runner)
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == "installed"
    assert events == [
        "setup",
        "native-status",
        "native-status",
        "native-install",
        "native-status",
    ]
    assert legacy_matches == [previous]
    assert not previous.exists()
    assert persisted is not None
    assert persisted["format_version"] == self_update.TRANSACTION_FORMAT_VERSION
    assert persisted["native_bridge_revision_before"] == NATIVE_BEFORE
    assert persisted["native_bridge_revision_current"] == NATIVE_CANDIDATE


def test_legacy_native_matcher_binds_receipt_to_preserved_runtime(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from continuity_kernel import bridge_launcher

    transaction = _transaction_receipt(tmp_path, vault)
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active_executable = active / "bin/gsv"
    previous_executable = previous / "bin/gsv"
    active_executable.parent.mkdir(parents=True)
    previous_executable.parent.mkdir(parents=True)
    active_executable.write_text("candidate", encoding="utf-8")
    previous_executable.write_text("previous", encoding="utf-8")
    runtime_root = previous / "lib/python3.11/site-packages/continuity_kernel"
    runtime_root.mkdir(parents=True)
    receipt = {
        "executable": str(active_executable.resolve(strict=True)),
        "executable_sha256": "8" * 64,
        "source_runtime_digest": "9" * 64,
    }
    status_payload = _legacy_owned_native_status(
        transaction,
        tmp_path,
        revision=NATIVE_BEFORE,
        current=False,
    )
    monkeypatch.setattr(
        self_update,
        "_runtime_command",
        lambda root: [str(previous_executable if root == previous else active_executable)],
    )
    monkeypatch.setattr(self_update, "_environment_runtime_root", lambda _root: runtime_root)
    monkeypatch.setattr(bridge_launcher, "_owned_receipt", lambda _application: receipt)
    monkeypatch.setattr(bridge_launcher, "_receipt_revision", lambda _receipt: "7" * 64)
    monkeypatch.setattr(bridge_launcher, "_sha256_file", lambda _path: "8" * 64)
    monkeypatch.setattr(bridge_launcher, "_source_runtime_manifest", lambda _root: [])
    monkeypatch.setattr(bridge_launcher, "_tree_digest", lambda _manifest: "9" * 64)

    self_update._require_native_app_matches_legacy_runtime(
        status_payload,
        transaction,
        legacy_environment=previous,
    )

    receipt["source_runtime_digest"] = "a" * 64
    with pytest.raises(SetupError, match="preserved legacy runtime"):
        self_update._require_native_app_matches_legacy_runtime(
            status_payload,
            transaction,
            legacy_environment=previous,
        )


@SOURCE_UPDATE_RUNTIME_ONLY
def test_legacy_candidate_recovery_restores_native_after_lost_commit_response(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    (active / "candidate").write_text("candidate", encoding="utf-8")
    (previous / "previous").write_text("previous", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else TO_SHA,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/runtime/bin/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        self_update,
        "_require_native_app_matches_legacy_runtime",
        lambda *_args, **_kwargs: None,
    )
    statuses = iter(
        (
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_BEFORE,
                current=False,
            ),
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_BEFORE,
                current=False,
            ),
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_CANDIDATE,
                current=True,
            ),
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_CANDIDATE,
                current=False,
            ),
            _legacy_owned_native_status(
                transaction,
                tmp_path,
                revision=NATIVE_RESTORED,
                current=True,
            ),
        )
    )
    native_installs = 0

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        nonlocal native_installs
        if "bridge" in command and "stop" in command:
            return self_update.CommandResult(0, b"", b"")
        if "setup" in command:
            return self_update.CommandResult(0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(next(statuses))
        if "native-install" in command:
            native_installs += 1
            if native_installs == 1:
                return self_update.CommandResult(86, b"", b"synthetic lost response")
            assert command[-2:] == ["--expected-revision", NATIVE_CANDIDATE]
            return _json_command(
                {
                    **_legacy_owned_native_status(
                        transaction,
                        tmp_path,
                        revision=NATIVE_RESTORED,
                        current=True,
                    ),
                    "changed": True,
                }
            )
        raise AssertionError(f"unexpected command: {command}")

    result = self_update.recover(vault, token=transaction["token"], runner=runner)
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == "rolled_back"
    assert native_installs == 2
    assert (active / "previous").read_text(encoding="utf-8") == "previous"
    assert persisted is not None
    assert persisted["format_version"] == self_update.TRANSACTION_FORMAT_VERSION
    assert persisted["native_bridge_revision_current"] == NATIVE_RESTORED


@SOURCE_UPDATE_RUNTIME_ONLY
@pytest.mark.parametrize(
    "failure",
    ("foreign", "wrong-vault", "unhealthy", "ambiguous", "stale"),
)
def test_legacy_candidate_native_probe_fails_closed_and_retains_prior_runtime(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    failed = Path(transaction["failed_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    (active / "candidate").write_text("candidate", encoding="utf-8")
    (previous / "previous").write_text("previous", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else TO_SHA,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/runtime/bin/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    base = _legacy_owned_native_status(
        transaction,
        tmp_path,
        revision=NATIVE_BEFORE,
        current=failure == "ambiguous",
    )
    if failure == "foreign":
        base.update(healthy=False, owned=False, error_code="foreign_install")
    elif failure == "wrong-vault":
        base["vault"] = str(tmp_path / "other-vault")
    elif failure == "unhealthy":
        base.update(healthy=False, error_code="tampered_install")
    restored = dict(base)
    restored["current"] = False
    statuses = iter((base, restored))

    def match_legacy(*_args: Any, **_kwargs: Any) -> None:
        if failure == "stale":
            raise SetupError("synthetic stale legacy app")
        pytest.fail("only stale healthy state should reach legacy-runtime matching")

    monkeypatch.setattr(
        self_update,
        "_require_native_app_matches_legacy_runtime",
        match_legacy,
    )
    native_install_seen = False

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        nonlocal native_install_seen
        if "bridge" in command and "stop" in command:
            return self_update.CommandResult(0, b"", b"")
        if "setup" in command:
            return self_update.CommandResult(0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(next(statuses))
        if "native-install" in command:
            native_install_seen = True
        raise AssertionError(f"unexpected command: {command}")

    result = self_update.recover(vault, token=transaction["token"], runner=runner)

    assert result["outcome"] == "repair_required"
    assert result["error_code"] == "native_bridge_restore_failed"
    assert native_install_seen is False
    assert (active / "previous").read_text(encoding="utf-8") == "previous"
    assert failed.is_dir()
    assert not previous.exists()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_recovery_adopts_committed_native_activation_without_replay(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _with_owned_native(_transaction_receipt(tmp_path, vault), tmp_path)
    transaction["phase"] = "activating_native_bridge"
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: TO_SHA)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/candidate/bin/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    native_installs = 0

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        nonlocal native_installs
        if "setup" in command:
            return self_update.CommandResult(0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(
                _native_status(transaction, revision=NATIVE_CANDIDATE, current=True)
            )
        if "native-install" in command:
            native_installs += 1
        raise AssertionError(f"unexpected command: {command}")

    result = self_update.recover(vault, token=transaction["token"], runner=runner)
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == "installed"
    assert native_installs == 0
    assert persisted is not None
    assert persisted["native_bridge_revision_current"] == NATIVE_CANDIDATE
    assert not previous.exists()


@SOURCE_UPDATE_RUNTIME_ONLY
def test_candidate_native_failure_restores_prior_app_before_cleanup(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _with_owned_native(_transaction_receipt(tmp_path, vault), tmp_path)
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    (active / "candidate").write_text("candidate", encoding="utf-8")
    (previous / "previous").write_text("previous", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else TO_SHA,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/runtime/bin/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    setup_calls = 0
    status_calls = 0
    native_install_calls = 0
    statuses = iter(
        (
            _native_status(transaction, revision=NATIVE_BEFORE, current=False),
            _native_status(transaction, revision=NATIVE_CANDIDATE, current=True),
            _native_status(transaction, revision=NATIVE_CANDIDATE, current=False),
            _native_status(transaction, revision=NATIVE_RESTORED, current=True),
        )
    )

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        nonlocal native_install_calls, setup_calls, status_calls
        if "bridge" in command and "stop" in command:
            return self_update.CommandResult(0, b"", b"")
        if "setup" in command:
            setup_calls += 1
            return self_update.CommandResult(0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            status_calls += 1
            return _json_command(next(statuses))
        if "native-install" in command:
            native_install_calls += 1
            if native_install_calls == 1:
                # The app committed, but the child died before returning its JSON receipt.
                return self_update.CommandResult(86, b"", b"synthetic response failure")
            assert command[-2:] == ["--expected-revision", NATIVE_CANDIDATE]
            return _json_command(
                {
                    **_native_status(
                        transaction,
                        revision=NATIVE_RESTORED,
                        current=True,
                    ),
                    "changed": True,
                }
            )
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(SetupError, match="native-install") as failed:
        self_update._finish_candidate(
            vault,
            transaction,
            runner=runner,
            recovering=False,
        )
    interrupted = self_update._read_transaction(required=True)
    assert interrupted is not None
    result = self_update._rollback_after_failure(
        vault,
        interrupted,
        failed.value,
        runner=runner,
    )
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == "rolled_back"
    assert result["native_bridge_revision_current"] == NATIVE_RESTORED
    assert setup_calls == 2
    assert status_calls == 4
    assert native_install_calls == 2
    assert (active / "previous").read_text(encoding="utf-8") == "previous"
    assert persisted is not None
    assert persisted["native_bridge_revision_current"] == NATIVE_RESTORED


@SOURCE_UPDATE_RUNTIME_ONLY
def test_rollback_reinstalls_owned_native_app_for_restored_runtime(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _with_owned_native(_transaction_receipt(tmp_path, vault), tmp_path)
    transaction.update(
        phase="native_bridge_activated",
        native_bridge_revision_current=NATIVE_CANDIDATE,
    )
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    (active / "candidate").write_text("candidate", encoding="utf-8")
    (previous / "previous").write_text("previous", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else TO_SHA,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/restored/bin/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    events: list[str] = []
    statuses = iter(
        (
            _native_status(transaction, revision=NATIVE_CANDIDATE, current=True),
            _native_status(transaction, revision=NATIVE_CANDIDATE, current=False),
            _native_status(transaction, revision=NATIVE_RESTORED, current=True),
        )
    )

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        if "bridge" in command and "stop" in command:
            events.append("bridge-stop")
            return self_update.CommandResult(0, b"", b"")
        if "setup" in command:
            events.append("setup")
            return self_update.CommandResult(0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            events.append("native-status")
            return _json_command(next(statuses))
        if "native-install" in command:
            events.append("native-install")
            assert command[-2:] == ["--expected-revision", NATIVE_CANDIDATE]
            return _json_command(
                {
                    **_native_status(
                        transaction,
                        revision=NATIVE_RESTORED,
                        current=True,
                    ),
                    "changed": True,
                }
            )
        raise AssertionError(f"unexpected command: {command}")

    result = self_update._rollback_after_failure(
        vault,
        transaction,
        ValidationError("synthetic post-activation failure"),
        runner=runner,
    )
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == "rolled_back"
    assert result["native_bridge_revision_current"] == NATIVE_RESTORED
    assert events == [
        "bridge-stop",
        "native-status",
        "setup",
        "native-status",
        "native-install",
        "native-status",
    ]
    assert persisted is not None
    assert persisted["native_bridge_revision_current"] == NATIVE_RESTORED
    assert (active / "previous").read_text(encoding="utf-8") == "previous"


@SOURCE_UPDATE_RUNTIME_ONLY
def test_update_reads_only_receipt_bound_whatsapp_service_label(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_receipt_bound_mcp(
        tmp_path,
        vault,
        monkeypatch,
        service_label="ai.example.cutover-wacli",
    )

    assert self_update._installed_whatsapp_service_label(vault) == ("ai.example.cutover-wacli")


@SOURCE_UPDATE_RUNTIME_ONLY
def test_update_omits_unsupplied_whatsapp_service_label(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_receipt_bound_mcp(tmp_path, vault, monkeypatch, service_label=None)
    monkeypatch.setenv(whatsapp.SERVICE_LABEL_ENV, "ai.ambient.must-not-win")

    assert self_update._installed_whatsapp_service_label(vault) is None
    assert whatsapp.SERVICE_LABEL_ENV not in self_update._setup_environment(
        _transaction_receipt(tmp_path, vault)
    )


@pytest.mark.parametrize("failure", ("changed", "wrong-vault", "secret-looking"))
@SOURCE_UPDATE_RUNTIME_ONLY
def test_update_rejects_untrusted_installed_whatsapp_service_label(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service_label = (
        "ai.sk-" + "proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456"
        if failure == "secret-looking"
        else "ai.example.cutover-wacli"
    )
    mcp = _write_receipt_bound_mcp(
        tmp_path,
        vault,
        monkeypatch,
        service_label=service_label,
    )
    if failure == "changed":
        mcp.write_text("{}\n", encoding="utf-8")
    elif failure == "wrong-vault":
        payload = json.loads(mcp.read_text(encoding="utf-8"))
        payload["mcpServers"]["gsv"]["env"]["GSV_VAULT"] = str(tmp_path / "other")
        mcp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        marketplace = mcp.parents[2]
        manifest = codex_integration._tree_manifest(marketplace)
        receipt_path = codex_integration._receipt_path(Path(os.environ["CODEX_HOME"]).resolve())
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["marketplace_manifest"] = manifest
        receipt["marketplace_digest"] = codex_integration._tree_digest_from_manifest(manifest)
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        self_update._installed_whatsapp_service_label(vault)


def test_update_transaction_rejects_invalid_whatsapp_service_label(
    vault: Vault,
    tmp_path: Path,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["whatsapp_service_label"] = "ai.example service"

    with pytest.raises(ValidationError, match="bounded non-secret launchd service label"):
        self_update._validate_transaction(transaction)


@pytest.mark.parametrize("finish", ("candidate", "restored"))
def test_update_setup_always_binds_the_transaction_vault(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finish: str,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "candidate_installed"
    transaction["whatsapp_service_label"] = "ai.example.cutover-wacli"
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/synthetic/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setenv(whatsapp.SERVICE_LABEL_ENV, "ai.ambient.must-not-win")
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    def fail_setup(
        command: list[str], environment: Any, _timeout: float
    ) -> self_update.CommandResult:
        commands.append(command)
        environments.append(dict(environment))
        return self_update.CommandResult(86, b"", b"")

    if finish == "candidate":
        with pytest.raises(SetupError, match="candidate setup failed"):
            self_update._finish_candidate(
                vault,
                transaction,
                runner=fail_setup,
                recovering=False,
            )
    else:
        result = self_update._finish_restored(vault, transaction, runner=fail_setup)
        assert result["outcome"] == "repair_required"

    assert commands == [
        [
            "/synthetic/gsv",
            "--json",
            "--vault",
            str(vault.root),
            "setup",
            "--no-browser",
            "--no-bridge",
        ]
    ]
    assert environments[0][whatsapp.SERVICE_LABEL_ENV] == "ai.example.cutover-wacli"


def test_update_refuses_to_stop_a_bridge_for_another_vault(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from continuity_kernel import bridge

    installed = _installed(tmp_path)
    monkeypatch.setattr(self_update, "installed_provenance", lambda: installed)
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)
    monkeypatch.setattr(
        bridge,
        "bridge_status",
        lambda: {
            "running": True,
            "identity_verified": True,
            "vault": str(tmp_path / "different-vault"),
            "vault_id": "018f6a20-7b3c-7d42-8a19-2e5f603b91c4",
        },
    )
    monkeypatch.setattr(
        bridge,
        "stop_bridge",
        lambda: pytest.fail("another vault's Bridge must not be stopped"),
    )

    with pytest.raises(ConflictError, match="different Seld vault"):
        self_update._apply_locked(
            vault,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            expected_check_revision=self_update._receipt_revision(receipt),
            approval_ref=APPROVAL_REF,
            runner=lambda *_args: pytest.fail("mismatched Bridge must fail before commands"),
        )


@SOURCE_UPDATE_RUNTIME_ONLY
def test_preflight_rebinds_every_seld_and_chatgpt_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = {
        "HOME": str(tmp_path / "live-home"),
        "CODEX_HOME": str(tmp_path / "live-codex"),
        "GSV_CONFIG_DIR": str(tmp_path / "live-config"),
        "GSV_DATA_DIR": str(tmp_path / "live-data"),
        "GSV_VAULT": str(tmp_path / "live-vault"),
    }
    for key, value in live.items():
        monkeypatch.setenv(key, value)
    observed: list[dict[str, str]] = []

    def install(
        _sha: str,
        *,
        tool_dir: Path,
        bin_dir: Path,
        runner: self_update.CommandRunner,
        base_environment: Any,
    ) -> None:
        del bin_dir, runner
        observed.append(dict(base_environment))
        executable = tool_dir / "gsv/bin/gsv"
        executable.parent.mkdir(parents=True)
        executable.write_text("synthetic", encoding="utf-8")

    monkeypatch.setattr(self_update, "_uv_install", install)
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: TO_SHA)

    def runner(_command: list[str], environment: Any, _timeout: float) -> self_update.CommandResult:
        observed.append(dict(environment))
        return self_update.CommandResult(0, b"ok\n", b"")

    self_update._preflight_candidate(TO_SHA, runner)

    assert len(observed) == 3
    for environment in observed:
        for key, value in live.items():
            assert environment[key] != value


def test_rollback_keeps_environments_when_candidate_bridge_stop_is_unprovable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Candidate Bridge rollback")
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "setting_up_candidate"
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active_executable = active / "bin/gsv"
    active_executable.parent.mkdir(parents=True)
    active_executable.write_text("candidate", encoding="utf-8")
    previous.mkdir()
    (previous / "working").write_text("previous", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else TO_SHA,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)

    def unavailable(*_args: Any) -> self_update.CommandResult:
        raise OSError("candidate command unavailable")

    result = self_update._rollback_after_failure(
        vault,
        transaction,
        ValidationError("candidate failed"),
        runner=unavailable,
    )

    assert result["outcome"] == "repair_required"
    assert result["error_code"] == "candidate_bridge_stop_failed"
    assert active.is_dir()
    assert previous.is_dir()
    assert not Path(transaction["failed_environment"]).exists()


def test_unsupported_provenance_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Distribution:
        def read_text(self, _name: str) -> str:
            return json.dumps(
                {
                    "url": "https://github.com/example/not-seld.git",
                    "vcs_info": {"vcs": "git", "commit_id": FROM_SHA},
                }
            )

    monkeypatch.setattr(
        self_update,
        "_installed_direct_url",
        lambda: Distribution().read_text("direct_url.json"),
    )

    installed = self_update.installed_provenance()

    assert installed.supported is False
    assert installed.install_mode == "source"
    assert installed.reason_code == "repository_not_official"


def test_frozen_provenance_does_not_read_source_install_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        self_update,
        "_installed_direct_url",
        lambda: pytest.fail("frozen builds must not read source-install provenance"),
    )

    installed = self_update.installed_provenance()

    assert installed.supported is False
    assert installed.install_mode == "prebuilt"
    assert installed.reason_code == "prebuilt_updates_not_supported"


def test_apply_rejects_invalid_approval_and_stale_check_before_runner(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)
    calls: list[list[str]] = []

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        calls.append(command)
        return self_update.CommandResult(0, b"", b"")

    with pytest.raises(ValidationError, match="approval"):
        self_update.apply(
            vault,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            expected_check_revision="3" * 64,
            approval_ref="chat:unbound",
            runner=runner,
        )
    with pytest.raises(ConflictError, match="status changed"):
        self_update.apply(
            vault,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            expected_check_revision="3" * 64,
            approval_ref=APPROVAL_REF,
            runner=runner,
        )

    assert calls == []


def test_apply_accepts_the_check_revision_published_by_status(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)
    revision = self_update.status()["check_revision"]
    assert isinstance(revision, str)
    invoked: list[dict[str, Any]] = []

    def apply_locked(_vault: Vault, **arguments: Any) -> dict[str, Any]:
        invoked.append(arguments)
        return {"outcome": "installed"}

    monkeypatch.setattr(self_update, "_apply_locked", apply_locked)

    assert self_update.apply(
        vault,
        from_sha=FROM_SHA,
        to_sha=TO_SHA,
        expected_check_revision=revision,
        approval_ref=APPROVAL_REF,
    ) == {"outcome": "installed"}
    assert invoked[0]["expected_check_revision"] == revision


@pytest.mark.parametrize(
    "checked_at",
    (
        (datetime.now(UTC) - timedelta(hours=6, seconds=1)).isoformat().replace("+00:00", "Z"),
        (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
    ),
)
def test_apply_rejects_expired_or_future_check_receipts(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checked_at: str,
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    receipt = _check_receipt(checked_at=checked_at)
    self_update._write_receipt(self_update._check_path(), receipt)

    with pytest.raises(ConflictError, match="check expired"):
        self_update.apply(
            vault,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            expected_check_revision=self_update._receipt_revision(receipt),
            approval_ref=APPROVAL_REF,
            runner=lambda *_args: pytest.fail("expired approval must not invoke a command"),
        )


@SOURCE_UPDATE_RUNTIME_ONLY
@pytest.mark.parametrize(
    ("outcome", "recovery_command"),
    (
        ("repair_required", None),
        ("installed_bridge_repair", None),
        ("installed", "Review retained previous environment"),
        ("rolled_back", "Review retained failed environment"),
    ),
)
def test_apply_preserves_unresolved_transaction_lineage(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    recovery_command: str | None,
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    receipt = _check_receipt()
    self_update._write_receipt(self_update._check_path(), receipt)
    transaction = _transaction_receipt(tmp_path, vault)
    transaction.update(
        phase="complete",
        outcome=outcome,
        recovery_command=recovery_command,
    )
    self_update._write_receipt(self_update._transaction_path(), transaction)
    before = self_update._transaction_path().read_bytes()

    with pytest.raises(ConflictError, match="must be resolved"):
        self_update.apply(
            vault,
            from_sha=FROM_SHA,
            to_sha=TO_SHA,
            expected_check_revision=self_update._receipt_revision(receipt),
            approval_ref=APPROVAL_REF,
            runner=lambda *_args: pytest.fail("unresolved update must not invoke a command"),
        )

    assert self_update._transaction_path().read_bytes() == before


@SOURCE_UPDATE_RUNTIME_ONLY
@pytest.mark.parametrize(
    ("outcome", "active_sha", "clean_outcome"),
    (
        ("installed_bridge_repair", TO_SHA, "installed"),
        ("repair_required", TO_SHA, "installed"),
        ("installed", TO_SHA, "installed"),
        ("rolled_back", FROM_SHA, "rolled_back"),
    ),
)
def test_token_recovery_can_close_any_unresolved_terminal_lineage(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    active_sha: str,
    clean_outcome: str,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    transaction.update(
        phase="complete",
        outcome=outcome,
        recovery_command="Review retained exact environment",
    )
    Path(transaction["tool_dir"]).mkdir(parents=True, exist_ok=True)
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: active_sha)

    def finish_candidate(
        _vault: Vault,
        current: dict[str, Any],
        *,
        runner: self_update.CommandRunner,
        recovering: bool,
    ) -> dict[str, Any]:
        del runner
        assert recovering is True
        assert current["phase"] == "recovering"
        assert current["outcome"] is None
        resolved = self_update._advance_transaction(
            current,
            phase="complete",
            outcome="installed",
            recovery_command=None,
        )
        return self_update._transaction_result(resolved)

    def finish_restored(
        _vault: Vault,
        current: dict[str, Any],
        *,
        runner: self_update.CommandRunner,
    ) -> dict[str, Any]:
        del runner
        assert current["phase"] == "recovering"
        resolved = self_update._advance_transaction(
            current,
            phase="complete",
            outcome="rolled_back",
            recovery_command=None,
        )
        return self_update._transaction_result(resolved)

    monkeypatch.setattr(self_update, "_finish_candidate", finish_candidate)
    monkeypatch.setattr(self_update, "_finish_restored", finish_restored)

    result = self_update.recover(
        vault,
        token=transaction["token"],
        runner=lambda *_args: pytest.fail("recovery finish was stubbed"),
    )
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == clean_outcome
    assert persisted is not None
    assert self_update._transaction_resolved(persisted) is True


@SOURCE_UPDATE_RUNTIME_ONLY
def test_recovery_reanchors_routine_writes_after_a_completed_activation_audit(
    vault: Vault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    transaction.update(
        phase="complete",
        outcome="installed",
        recovery_command="Review retained previous environment",
        protected_vault_digest=transaction["vault_digest"],
    )
    Path(transaction["tool_dir"]).mkdir(parents=True, exist_ok=True)
    self_update._write_receipt(self_update._transaction_path(), transaction)
    now = vault.read_document("NOW.md")
    vault.write_document(
        "NOW.md",
        "routine Pulse write after completed audit",
        expected_revision=now["revision"],
    )
    current_digest = vault.status()["digest"]
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: TO_SHA)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/synthetic/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(_absent_native_status(tmp_path))
        return self_update.CommandResult(0, b"", b"")

    result = self_update.recover(
        vault,
        token=transaction["token"],
        runner=runner,
    )

    assert result["outcome"] == "installed"
    assert result["vault_changed"] is True
    persisted = self_update._read_transaction(required=True)
    assert persisted is not None
    assert persisted["protected_vault_digest"] == current_digest


@SOURCE_UPDATE_RUNTIME_ONLY
def test_ambiguous_vault_drift_requires_exact_fresh_approval_before_reanchoring(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    transaction.update(
        phase="verifying_candidate",
        protected_vault_digest=transaction["vault_digest"],
    )
    Path(transaction["tool_dir"]).mkdir(parents=True, exist_ok=True)
    active_python = Path(transaction["active_environment"]) / "bin/python"
    active_python.parent.mkdir(parents=True)
    active_python.write_text("synthetic python", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    now = vault.read_document("NOW.md")
    vault.write_document(
        "NOW.md",
        "changed after activation started",
        expected_revision=now["revision"],
    )
    monkeypatch.setattr(self_update, "_runtime_sha", lambda *_args: TO_SHA)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/synthetic/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)

    result = self_update.recover(
        vault,
        token=transaction["token"],
        runner=lambda *_args: pytest.fail("digest drift must stop before candidate setup"),
    )
    persisted = self_update._read_transaction(required=True)

    assert result["outcome"] == "repair_required"
    assert result["error_code"] == "vault_changed_during_activation"
    assert persisted is not None
    assert persisted["protected_vault_digest"] == transaction["vault_digest"]
    current_digest = vault.status()["digest"]
    recovery_command = str(result["recovery_command"])
    assert current_digest in recovery_command
    assert "--expected-vault-digest" in recovery_command
    assert "codex:<current-task-uuid>" in recovery_command

    with pytest.raises(ConflictError, match="changed after recovery was approved"):
        self_update.recover(
            vault,
            token=transaction["token"],
            expected_vault_digest="9" * 64,
            approval_ref=APPROVAL_REF,
            runner=lambda *_args: pytest.fail("stale vault approval must not execute code"),
        )

    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)

    def recovery_runner(
        command: list[str], _environment: Any, _timeout: float
    ) -> self_update.CommandResult:
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(_absent_native_status(tmp_path))
        return self_update.CommandResult(0, b"", b"")

    completed = self_update.recover(
        vault,
        token=transaction["token"],
        expected_vault_digest=current_digest,
        approval_ref=APPROVAL_REF,
        runner=recovery_runner,
    )
    resolved = self_update._read_transaction(required=True)

    assert completed["outcome"] == "installed"
    assert completed["vault_changed"] is True
    assert resolved is not None
    assert resolved["protected_vault_digest"] == current_digest
    assert resolved["recovery_approval_ref"] == APPROVAL_REF
    assert resolved["recovery_approval_vault_digest"] == current_digest


@SOURCE_UPDATE_RUNTIME_ONLY
def test_recovery_rolls_back_when_interrupted_candidate_setup_fails(
    vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = _transaction_receipt(tmp_path, vault)
    transaction["phase"] = "candidate_installed"
    active = Path(transaction["active_environment"])
    previous = Path(transaction["previous_environment"])
    active.mkdir(parents=True)
    previous.mkdir()
    (active / "candidate").write_text("new", encoding="utf-8")
    (previous / "previous").write_text("old", encoding="utf-8")
    self_update._write_receipt(self_update._transaction_path(), transaction)
    monkeypatch.setattr(
        self_update,
        "_runtime_sha",
        lambda root, _runner: FROM_SHA if root == previous else TO_SHA,
    )
    monkeypatch.setattr(self_update, "_runtime_reads_vault", lambda *_args: True)
    monkeypatch.setattr(self_update, "_runtime_command", lambda _root: ["/synthetic/gsv"])
    monkeypatch.setattr(self_update, "_verify_external_launcher", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(self_update, "_verify_active_runtime", lambda *_args, **_kwargs: None)
    candidate_setup = 0

    def runner(command: list[str], _environment: Any, _timeout: float) -> self_update.CommandResult:
        nonlocal candidate_setup
        if "bridge" in command and "stop" in command:
            return self_update.CommandResult(0, b"", b"")
        if "setup" in command:
            candidate_setup += 1
            return self_update.CommandResult(86 if candidate_setup == 1 else 0, b"", b"")
        if command[-2:] == ["bridge", "native-status"]:
            return _json_command(_absent_native_status(tmp_path))
        return self_update.CommandResult(0, b"", b"")

    result = self_update.recover(vault, token=transaction["token"], runner=runner)

    assert result["outcome"] == "rolled_back"
    assert candidate_setup == 2
    assert (active / "previous").read_text(encoding="utf-8") == "old"


@SOURCE_UPDATE_RUNTIME_ONLY
def test_status_projects_interrupted_and_terminal_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(self_update, "installed_provenance", lambda: _installed(tmp_path))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Update transaction status")
    transaction = _transaction_receipt(tmp_path, vault)
    self_update._write_receipt(self_update._transaction_path(), transaction)

    interrupted = self_update.status()
    assert interrupted["state"] == "interrupted"
    assert interrupted["transaction"]["phase"] == "candidate_installed"

    transaction.update(phase="complete", outcome="installed")
    transaction["recovery_command"] = None
    self_update._write_receipt(self_update._transaction_path(), transaction)
    terminal = self_update.status()
    assert terminal["state"] == "unchecked"
    assert terminal["transaction"]["outcome"] == "installed"

    transaction["recovery_command"] = "Review retained previous environment"
    self_update._write_receipt(self_update._transaction_path(), transaction)
    cleanup_pending = self_update.status()
    assert cleanup_pending["state"] == "interrupted"
    assert cleanup_pending["transaction"]["outcome"] == "installed"


def test_cli_update_help_names_approval_and_recovery(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["update", "--help"])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "cached update state" in help_text
    assert "network" in help_text
    assert "apply" in help_text
    assert "recover" in help_text


def test_cli_update_check_forwards_force_and_returns_actionable_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[bool] = []

    def unavailable(*, force: bool) -> dict[str, Any]:
        seen.append(force)
        return {"state": "unavailable", "error_code": "github_unavailable"}

    monkeypatch.setattr(self_update, "check", unavailable)

    assert cli.main(["--json", "update", "check", "--force"]) == 3
    payload = json.loads(capsys.readouterr().out)

    assert seen == [True]
    assert payload["ok"] is False
    assert payload["result"]["error_code"] == "github_unavailable"
    assert "inspect result.error_code" in payload["error"]


def test_cli_update_recovery_forwards_exact_vault_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}

    def recover(_vault: Vault, **arguments: Any) -> dict[str, Any]:
        observed.update(arguments)
        return {"outcome": "installed", "repair_required": False}

    monkeypatch.setattr(self_update, "recover", recover)
    digest = "4" * 64
    token = "019f0000-0000-7000-8000-000000000778"

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(tmp_path / "vault"),
                "update",
                "recover",
                "--token",
                token,
                "--expected-vault-digest",
                digest,
                "--approval-ref",
                APPROVAL_REF,
            ]
        )
        == 0
    )
    json.loads(capsys.readouterr().out)

    assert observed == {
        "token": token,
        "expected_vault_digest": digest,
        "approval_ref": APPROVAL_REF,
    }


@pytest.mark.parametrize(
    ("command", "result", "exit_code"),
    [
        ("apply", {"outcome": "rolled_back", "rolled_back": True}, 3),
        ("apply", {"outcome": "installed_bridge_repair"}, 4),
        ("recover", {"repair_required": True, "recovery_command": "repair"}, 3),
    ],
)
def test_cli_update_result_failures_keep_the_structured_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    result: dict[str, Any],
    exit_code: int,
) -> None:
    monkeypatch.setattr(self_update, command, lambda *_args, **_kwargs: result)
    if command == "apply":
        arguments = [
            "--json",
            "--vault",
            str(tmp_path / "vault"),
            "update",
            "apply",
            "--from-sha",
            FROM_SHA,
            "--to-sha",
            TO_SHA,
            "--expected-check-revision",
            "3" * 40,
            "--approval-ref",
            APPROVAL_REF,
        ]
    else:
        arguments = [
            "--json",
            "--vault",
            str(tmp_path / "vault"),
            "update",
            "recover",
            "--token",
            "019f0000-0000-7000-8000-000000000778",
        ]

    assert cli.main(arguments) == exit_code
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["result"] == result
