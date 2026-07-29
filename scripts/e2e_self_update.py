#!/usr/bin/env python3
"""Prove source self-update activation and byte-preserving rollback in isolation.

The proof creates two real Git revisions from the exact candidate tree, installs
revision A as an isolated ``uv tool``, and drives the installed production update
core from a fresh process.  The only adapter replaces the fixed public repository
and GitHub-read boundary in-process with the exact local fixture repository.  No
production environment override or alternate update implementation is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_APPROVAL_REF = "codex:019f8a30-f5db-7b80-b53f-0377296f2d3c"
_FIXTURE_MARKER = ".seld-self-update-e2e-revision"
_RECOVERY_LAUNCHER = "seld-recover"
_SIGKILL = int(getattr(signal, "SIGKILL", signal.SIGTERM))
_ADAPTER = r"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from continuity_kernel import update
from continuity_kernel.errors import ConflictError
from continuity_kernel.vault import Vault

_OFFICIAL_REPOSITORY = "https://github.com/olivier-motium/seld.git"


def _bind_local_repository(repository_url: str) -> Path:
    parsed = urlsplit(repository_url)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise RuntimeError("the self-update proof accepts one local file repository")
    repository = Path(unquote(parsed.path)).resolve(strict=True)

    def same_repository(value: str) -> bool:
        try:
            candidate = urlsplit(value)
            if (
                candidate.scheme == "https"
                and candidate.hostname == "github.com"
                and candidate.path.rstrip("/")
                in {"/olivier-motium/seld", "/olivier-motium/seld.git"}
            ):
                return True
            if candidate.scheme != "file" or candidate.netloc not in {"", "localhost"}:
                return False
            return Path(unquote(candidate.path)).resolve(strict=True) == repository
        except (OSError, ValueError):
            return False

    update.REPOSITORY_URL = repository_url
    update._same_repository = same_repository
    return repository


def _normalize_source_provenance_for_external_recovery() -> None:
    root = Path(sys.prefix) / "lib"
    candidates = list(root.glob("python*/site-packages/gsv-*.dist-info/direct_url.json"))
    if len(candidates) != 1:
        raise RuntimeError("installed fixture has no unique source provenance to normalize")
    target = candidates[0]
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["url"] = _OFFICIAL_REPOSITORY
    temporary = target.with_name(f".{target.name}.e2e-tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _verified_fixture_fetcher(to_sha: str):
    def fetch(url: str):
        if url.endswith("/commits/main"):
            return (
                {
                    "sha": to_sha,
                    "commit": {
                        "committer": {"date": "2026-07-29T00:00:00Z"},
                        "verification": {"verified": True, "reason": "valid"},
                    },
                },
                {},
            )
        if "/compare/" in url:
            return {"status": "ahead", "ahead_by": 1, "behind_by": 0}, {}
        if url.endswith(f"/commits/{to_sha}/check-runs?per_page=100"):
            check_runs = [
                {
                    "app": {"slug": "github-actions"},
                    "conclusion": "success",
                    "head_sha": to_sha,
                    "name": name,
                    "status": "completed",
                }
                for name in sorted(update.REQUIRED_CHECK_NAMES)
            ]
            return (
                {
                    "total_count": len(check_runs),
                    "check_runs": check_runs,
                },
                {},
            )
        raise RuntimeError(f"unexpected local GitHub adapter request: {url}")

    return fetch


def _candidate_failure_runner(to_sha: str):
    base = update._run_command
    installed = update.installed_provenance()
    if installed.environment is None:
        raise RuntimeError("installed Seld has no environment for failure injection")
    active = Path(installed.environment)
    candidate_setup_failed = False

    def run(command: list[str], environment: Any, timeout: float):
        nonlocal candidate_setup_failed
        executable = Path(command[0]) if command else Path()
        is_active_setup = executable == active / "bin/gsv" and "setup" in command
        if is_active_setup and not candidate_setup_failed:
            try:
                active_sha = update._environment_sha(active)
            except Exception:
                active_sha = None
            if active_sha == to_sha:
                candidate_setup_failed = True
                return update.CommandResult(86, b"", b"injected candidate activation failure")
        return base(command, environment, timeout)

    return run, lambda: candidate_setup_failed


def _pause_before_candidate_runner(marker: Path):
    base = update._run_command
    installed = update.installed_provenance()
    if installed.tool_dir is None:
        raise RuntimeError("installed Seld has no tool directory for the crash hook")
    tool_dir = Path(installed.tool_dir)
    paused = False

    def run(command: list[str], environment: Any, timeout: float):
        nonlocal paused
        is_uv_install = "tool" in command and "install" in command
        is_live_target = environment.get("UV_TOOL_DIR") == str(tool_dir)
        if is_uv_install and is_live_target and not paused:
            transaction = update._read_transaction(required=True)
            if transaction is None or transaction.get("phase") != "previous_preserved":
                raise RuntimeError("crash hook reached the wrong transaction phase")
            paused = True
            payload = {
                "phase": transaction["phase"],
                "pid": os.getpid(),
                "token": transaction["token"],
            }
            temporary = marker.with_name(f".{marker.name}.tmp")
            temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, marker)
            while True:
                time.sleep(60)
        return base(command, environment, timeout)

    return run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("apply", "inspect"), required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--from-sha")
    parser.add_argument("--to-sha")
    parser.add_argument("--approval-ref")
    parser.add_argument("--inject-candidate-failure", action="store_true")
    parser.add_argument("--normalize-source-provenance", action="store_true")
    parser.add_argument("--pause-before-candidate", type=Path)
    args = parser.parse_args()

    _bind_local_repository(args.repository)
    if args.normalize_source_provenance:
        _normalize_source_provenance_for_external_recovery()
    if args.mode == "inspect":
        provenance = update.installed_provenance()
        print(
            json.dumps(
                {
                    "provenance": provenance.to_dict(),
                    "update_status": update.status(),
                },
                sort_keys=True,
            )
        )
        return 0

    if not args.from_sha or not args.to_sha or not args.approval_ref:
        raise RuntimeError("apply mode requires exact from/to SHAs and an approval reference")
    checked = update.check(force=True, fetcher=_verified_fixture_fetcher(args.to_sha))
    if checked.get("state") != "available" or checked.get("candidate", {}).get(
        "sha"
    ) != args.to_sha:
        raise RuntimeError(
            "local reviewed candidate was not admitted by the production checker: "
            f"{json.dumps(checked, sort_keys=True)}"
        )
    revision = checked.get("check_revision")
    if not isinstance(revision, str):
        raise RuntimeError("production checker did not publish an exact receipt revision")

    stale_rejected = False
    try:
        update.apply(
            Vault(args.vault),
            from_sha=args.from_sha,
            to_sha=args.to_sha,
            expected_check_revision="0" * 64,
            approval_ref=args.approval_ref,
        )
    except ConflictError:
        stale_rejected = True
    if not stale_rejected:
        raise RuntimeError("a stale approved check revision did not fail closed")

    runner = None
    injected = lambda: False
    if args.inject_candidate_failure:
        runner, injected = _candidate_failure_runner(args.to_sha)
    if args.pause_before_candidate is not None:
        if runner is not None:
            raise RuntimeError("the crash and activation-failure hooks are mutually exclusive")
        runner = _pause_before_candidate_runner(args.pause_before_candidate)
    result = update.apply(
        Vault(args.vault),
        from_sha=args.from_sha,
        to_sha=args.to_sha,
        expected_check_revision=revision,
        approval_ref=args.approval_ref,
        runner=runner,
    )
    if args.inject_candidate_failure and not injected():
        raise RuntimeError("the candidate activation failure was not injected")
    print(
        json.dumps(
            {
                "check_revision": revision,
                "result": result,
                "stale_check_rejected": stale_rejected,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


@dataclass(frozen=True)
class _PreparedInstall:
    before: dict[str, Any]
    environment: dict[str, str]
    gsv: Path
    paths: dict[str, Path]
    python: Path
    vault: Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="exact Seld candidate tree to turn into the two local revisions",
    )
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="seld-self-update-e2e-")).resolve()
    try:
        report = run_e2e(root, args.source.resolve())
        encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        if args.keep:
            print(f"Retained isolated proof at {root}", file=sys.stderr)
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


def run_e2e(root: Path, source: Path) -> dict[str, Any]:
    if os.name == "nt":
        raise RuntimeError("source self-update is not supported on Windows")
    uv = shutil.which("uv")
    codex = shutil.which("codex")
    git = shutil.which("git")
    if uv is None or codex is None or git is None:
        raise RuntimeError("the self-update proof requires uv, git, and the Codex CLI")
    if not (source / "pyproject.toml").is_file() or not (source / ".git").exists():
        raise RuntimeError(f"source is not a Seld Git checkout: {source}")

    fixture = root / "fixture"
    _copy_candidate_tree(source, fixture)
    _git(fixture, "init", "--quiet")
    _git(fixture, "config", "user.email", "seld-e2e@example.invalid")
    _git(fixture, "config", "user.name", "Seld update proof")
    _git(fixture, "add", "--all")
    _git(fixture, "commit", "--quiet", "-m", "fixture revision A")
    revision_a = _git(fixture, "rev-parse", "HEAD").strip()
    (fixture / _FIXTURE_MARKER).write_text("revision B\n", encoding="utf-8")
    _git(fixture, "add", _FIXTURE_MARKER)
    _git(fixture, "commit", "--quiet", "-m", "fixture revision B")
    revision_b = _git(fixture, "rev-parse", "HEAD").strip()
    _git(fixture, "merge-base", "--is-ancestor", revision_a, revision_b)
    repository_url = fixture.as_uri()

    adapter = root / "installed_update_adapter.py"
    adapter.write_text(textwrap.dedent(_ADAPTER).lstrip(), encoding="utf-8")
    success = _scenario(
        root / "success",
        uv=Path(uv),
        codex=Path(codex),
        repository_url=repository_url,
        revision_a=revision_a,
        revision_b=revision_b,
        adapter=adapter,
        inject_failure=False,
    )
    rollback = _scenario(
        root / "rollback",
        uv=Path(uv),
        codex=Path(codex),
        repository_url=repository_url,
        revision_a=revision_a,
        revision_b=revision_b,
        adapter=adapter,
        inject_failure=True,
    )
    crash_recovery = _crash_recovery_scenario(
        root / "crash-recovery",
        uv=Path(uv),
        codex=Path(codex),
        repository_url=repository_url,
        revision_a=revision_a,
        revision_b=revision_b,
        adapter=adapter,
    )
    return {
        "crash_recovery": crash_recovery,
        "isolated_execution": True,
        "fixture": {
            "from_sha": revision_a,
            "to_sha": revision_b,
            "to_is_descendant": True,
        },
        "rollback": rollback,
        "successful_update": success,
        "synthetic_boundary": (
            "Only the fixed local repository and its verified-GitHub response are adapted; "
            "uv installation, receipts, locks, activation, health checks, and rollback use "
            "the installed production update core."
        ),
    }


def _scenario(
    root: Path,
    *,
    uv: Path,
    codex: Path,
    repository_url: str,
    revision_a: str,
    revision_b: str,
    adapter: Path,
    inject_failure: bool,
) -> dict[str, Any]:
    prepared = _prepare_revision_a(
        root,
        uv=uv,
        codex=codex,
        repository_url=repository_url,
        revision_a=revision_a,
    )
    before = prepared.before
    environment = prepared.environment
    gsv = prepared.gsv
    paths = prepared.paths
    python = prepared.python
    vault = prepared.vault
    environment_before = _tree_manifest(paths["tools"] / "gsv")
    apply_command = [
        str(python),
        str(adapter),
        "--mode",
        "apply",
        "--repository",
        repository_url,
        "--vault",
        str(vault),
        "--from-sha",
        revision_a,
        "--to-sha",
        revision_b,
        "--approval-ref",
        _APPROVAL_REF,
    ]
    if inject_failure:
        apply_command.append("--inject-candidate-failure")
    try:
        availability: dict[str, Any] | None = None
        if inject_failure:
            applied = _json_process(apply_command, environment, timeout=1200)
        else:
            applied, availability = _json_process_with_availability_probe(
                apply_command,
                environment,
                primary=gsv,
                recovery=paths["bin"] / _RECOVERY_LAUNCHER,
                timeout=1200,
            )
        after = _fresh_health(gsv, environment, vault, paths["codex"])
        expected_sha = revision_a if inject_failure else revision_b
        inspected = _json_process(
            [
                str(paths["tools"] / "gsv/bin/python"),
                str(adapter),
                "--mode",
                "inspect",
                "--repository",
                repository_url,
                "--vault",
                str(vault),
            ],
            environment,
        )
        observed_sha = inspected.get("provenance", {}).get("sha")
        if observed_sha != expected_sha:
            raise RuntimeError(
                f"fresh installed process reported {observed_sha}, expected {expected_sha}"
            )
        if before["vault_id"] != after["vault_id"] or before["digest"] != after["digest"]:
            raise RuntimeError("self-update changed the vault identity or canonical digest")
        outcome = applied.get("result", {}).get("outcome")
        expected_outcome = "rolled_back" if inject_failure else "installed"
        if outcome != expected_outcome:
            raise RuntimeError(f"update outcome was {outcome}, expected {expected_outcome}")
        recovery_launcher_cleaned = not os.path.lexists(paths["bin"] / _RECOVERY_LAUNCHER)
        if not recovery_launcher_cleaned:
            raise RuntimeError("terminal update left its rescue launcher installed")
        restored_byte_for_byte = _tree_manifest(paths["tools"] / "gsv") == environment_before
        if inject_failure and not restored_byte_for_byte:
            raise RuntimeError("rollback did not restore revision A byte-for-byte")
        result = {
            "bridge_healthy": after["bridge_healthy"],
            "chatgpt_integration_ready": after["chatgpt_integration_ready"],
            "fresh_process_sha": observed_sha,
            "outcome": outcome,
            "recovery_launcher_cleaned": recovery_launcher_cleaned,
            "stale_check_rejected": applied.get("stale_check_rejected") is True,
            "vault_digest_preserved": before["digest"] == after["digest"],
            "vault_id_preserved": before["vault_id"] == after["vault_id"],
        }
        if inject_failure:
            result["environment_restored_byte_for_byte"] = restored_byte_for_byte
        else:
            result["candidate_environment_replaced_prior"] = not restored_byte_for_byte
            result["continuous_executable"] = availability
        return result
    finally:
        _run(
            [str(gsv), "--json", "bridge", "stop"],
            environment,
            check=False,
            timeout=30,
        )


def _prepare_revision_a(
    root: Path,
    *,
    uv: Path,
    codex: Path,
    repository_url: str,
    revision_a: str,
) -> _PreparedInstall:
    paths = {
        name: root / name
        for name in ("bin", "codex", "config", "data", "home", "tmp", "tools", "uv-cache")
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    vault = root / "vault"
    (paths["codex"] / "AGENTS.md").write_text(
        "# Existing synthetic instructions\n\nPreserve this file.\n",
        encoding="utf-8",
    )
    environment = _isolated_environment(root, paths, codex)
    source = f"git+{repository_url}@{revision_a}"
    _run(
        [
            str(uv.resolve()),
            "--no-config",
            "tool",
            "install",
            "--force",
            "--python",
            sys.executable,
            source,
        ],
        environment,
        timeout=600,
    )
    gsv = paths["bin"] / "gsv"
    python = paths["tools"] / "gsv/bin/python"
    if not gsv.is_symlink() or not python.exists():
        raise RuntimeError("isolated uv source install did not create the expected Seld tool")
    _json_cli(gsv, environment, ["demo", "--output", str(vault)])
    setup = _json_cli(
        gsv,
        environment,
        [
            "--vault",
            str(vault),
            "setup",
            "--codex-home",
            str(paths["codex"]),
            "--no-browser",
        ],
    )
    if setup.get("setup_complete") is not True:
        raise RuntimeError("revision A did not complete isolated setup")
    return _PreparedInstall(
        before=_fresh_health(gsv, environment, vault, paths["codex"]),
        environment=environment,
        gsv=gsv,
        paths=paths,
        python=python,
        vault=vault,
    )


def _crash_recovery_scenario(
    root: Path,
    *,
    uv: Path,
    codex: Path,
    repository_url: str,
    revision_a: str,
    revision_b: str,
    adapter: Path,
) -> dict[str, Any]:
    prepared = _prepare_revision_a(
        root,
        uv=uv,
        codex=codex,
        repository_url=repository_url,
        revision_a=revision_a,
    )
    environment = prepared.environment
    gsv = prepared.gsv
    paths = prepared.paths
    vault = prepared.vault
    pause_marker = root / "paused-before-candidate.json"
    recovery = paths["bin"] / _RECOVERY_LAUNCHER
    command = [
        str(prepared.python),
        str(adapter),
        "--mode",
        "apply",
        "--repository",
        repository_url,
        "--vault",
        str(vault),
        "--from-sha",
        revision_a,
        "--to-sha",
        revision_b,
        "--approval-ref",
        _APPROVAL_REF,
        "--normalize-source-provenance",
        "--pause-before-candidate",
        str(pause_marker),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    try:
        paused = _wait_for_json_file(pause_marker, process, timeout=900)
        token = paused.get("token")
        if paused.get("phase") != "previous_preserved" or not isinstance(token, str):
            raise RuntimeError("crash hook did not expose the exact preserved transaction")
        if paused.get("pid") != process.pid:
            raise RuntimeError("crash hook marker does not belong to the apply process")
        if not _is_executable(recovery):
            raise RuntimeError("stable Seld recovery launcher was absent at the crash boundary")
        process.send_signal(_SIGKILL)
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != -_SIGKILL:
            raise RuntimeError(
                "interrupted update did not terminate by SIGKILL: "
                f"returncode={process.returncode}; stdout={stdout[-1000:]}; "
                f"stderr={stderr[-1000:]}"
            )

        hostile = environment.copy()
        hostile.update(
            {
                "PYTHONHOME": str(root / "hostile-python-home"),
                "PYTHONPATH": str(root / "hostile-python-path"),
                "VIRTUAL_ENV": str(root / "hostile-virtual-env"),
            }
        )
        interrupted = _json_cli(
            recovery,
            hostile,
            ["--vault", str(vault), "update", "status"],
        )
        transaction = interrupted.get("transaction")
        if (
            interrupted.get("state") != "interrupted"
            or not isinstance(transaction, dict)
            or transaction.get("token") != token
            or transaction.get("phase") != "previous_preserved"
        ):
            raise RuntimeError("stable recovery launcher did not expose the interrupted token")

        now = _json_cli(
            recovery,
            hostile,
            ["--vault", str(vault), "document", "show", "NOW.md"],
        )
        changed_content = "legitimate concurrent Pulse change during interrupted update\n"
        changed = _json_cli(
            recovery,
            hostile,
            [
                "--vault",
                str(vault),
                "document",
                "update",
                "NOW.md",
                "--expected-revision",
                str(now["revision"]),
                "--content",
                changed_content,
            ],
        )
        recovered = _json_cli(
            recovery,
            hostile,
            ["--vault", str(vault), "update", "recover", "--token", token],
        )
        if recovered.get("outcome") != "rolled_back":
            raise RuntimeError(f"crash recovery did not roll back revision A: {recovered}")

        after = _fresh_health(gsv, environment, vault, paths["codex"])
        inspected = _json_process(
            [
                str(paths["tools"] / "gsv/bin/python"),
                str(adapter),
                "--mode",
                "inspect",
                "--repository",
                repository_url,
                "--vault",
                str(vault),
            ],
            environment,
        )
        observed_sha = inspected.get("provenance", {}).get("sha")
        if observed_sha != revision_a:
            raise RuntimeError("fresh crash-recovered runtime did not restore revision A")
        observed_now = _json_cli(
            gsv,
            environment,
            ["--vault", str(vault), "document", "show", "NOW.md"],
        )
        if (
            observed_now.get("revision") != changed.get("revision")
            or observed_now.get("content") != changed_content
        ):
            raise RuntimeError("crash recovery lost the legitimate concurrent vault change")
        active = paths["tools"] / "gsv"
        if not gsv.is_symlink() or gsv.resolve(strict=True) != (active / "bin/gsv").resolve(
            strict=True
        ):
            raise RuntimeError("crash recovery did not restore the direct gsv launcher")
        if os.path.lexists(recovery):
            raise RuntimeError("terminal crash recovery left its rescue launcher installed")
        if list(paths["tools"].glob(".gsv.previous.*")) or list(
            paths["tools"].glob(".gsv.failed.*")
        ):
            raise RuntimeError("crash recovery retained a duplicate candidate environment")

        before_replay = _tree_manifest(active)
        bridge_before_replay = _json_cli(gsv, environment, ["bridge", "status"])
        replay = _json_cli(
            gsv,
            environment,
            ["--vault", str(vault), "update", "recover", "--token", token],
        )
        bridge_after_replay = _json_cli(gsv, environment, ["bridge", "status"])
        no_replay = (
            replay.get("outcome") == "rolled_back"
            and _tree_manifest(active) == before_replay
            and bridge_before_replay.get("instance_id") == bridge_after_replay.get("instance_id")
        )
        if not no_replay:
            raise RuntimeError("terminal recovery replayed candidate installation or setup")
        return {
            "bridge_healthy": after["bridge_healthy"],
            "chatgpt_integration_ready": after["chatgpt_integration_ready"],
            "direct_gsv_launcher_restored": True,
            "fresh_process_sha": observed_sha,
            "hostile_python_environment_sanitized": True,
            "interrupted_phase": transaction["phase"],
            "interrupted_status_visible": True,
            "no_duplicate_candidate_or_replay": no_replay,
            "outcome": recovered["outcome"],
            "recovery_launcher_cleaned": not os.path.lexists(recovery),
            "recovery_token": token,
            "vault_change_preserved": observed_now.get("revision") == changed.get("revision"),
            "vault_digest_changed_legitimately": prepared.before["digest"] != after["digest"],
            "vault_id_preserved": prepared.before["vault_id"] == after["vault_id"],
        }
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)
        _run(
            [str(gsv), "--json", "bridge", "stop"],
            environment,
            check=False,
            timeout=30,
        )


def _fresh_health(
    gsv: Path,
    environment: dict[str, str],
    vault: Path,
    codex_home: Path,
) -> dict[str, Any]:
    status = _json_cli(gsv, environment, ["--vault", str(vault), "status"])
    doctor = _json_cli(gsv, environment, ["--vault", str(vault), "doctor"])
    bridge = _json_cli(gsv, environment, ["bridge", "status"])
    codex = _json_cli(
        gsv,
        environment,
        ["codex", "status", "--codex-home", str(codex_home)],
    )
    doctor_codex = doctor.get("codex")
    bridge_healthy = bridge.get("running") is True and bridge.get("identity_verified") is True
    integration_ready = (
        doctor.get("healthy") is True
        and isinstance(doctor_codex, dict)
        and doctor_codex.get("ready") is True
        and codex.get("ready") is True
    )
    if not bridge_healthy or not integration_ready:
        raise RuntimeError("fresh Seld process did not prove Bridge and ChatGPT health")
    return {
        "bridge_healthy": bridge_healthy,
        "chatgpt_integration_ready": integration_ready,
        "digest": status["digest"],
        "vault_id": status["vault_id"],
    }


def _isolated_environment(root: Path, paths: dict[str, Path], codex: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {
            "GSV_CONFIG_DIR",
            "GSV_DATA_DIR",
            "GSV_VAULT",
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "UV_INDEX",
            "UV_INDEX_URL",
            "UV_EXTRA_INDEX_URL",
            "UV_DEFAULT_INDEX",
        }:
            environment.pop(key, None)
    environment.update(
        {
            "APPDATA": str(paths["config"]),
            "CODEX_HOME": str(paths["codex"]),
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GSV_CONFIG_DIR": str(paths["config"]),
            "GSV_DATA_DIR": str(paths["data"]),
            "HOME": str(paths["home"]),
            "LOCALAPPDATA": str(paths["data"]),
            "NO_PROXY": "127.0.0.1,localhost",
            "PIP_CONFIG_FILE": os.devnull,
            "TMPDIR": str(paths["tmp"]),
            "USERPROFILE": str(paths["home"]),
            "UV_CACHE_DIR": str(paths["uv-cache"]),
            "UV_NO_PROGRESS": "1",
            "UV_TOOL_BIN_DIR": str(paths["bin"]),
            "UV_TOOL_DIR": str(paths["tools"]),
            "XDG_CONFIG_HOME": str(paths["config"]),
            "XDG_DATA_HOME": str(paths["data"]),
        }
    )
    path = [str(paths["bin"]), str(codex.resolve().parent), environment.get("PATH", "")]
    environment["PATH"] = os.pathsep.join(item for item in path if item)
    return environment


def _copy_candidate_tree(source: Path, target: Path) -> None:
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
    ).stdout
    relative_paths = [item.decode("utf-8") for item in listed.split(b"\0") if item]
    if not relative_paths:
        raise RuntimeError("candidate checkout has no files")
    target.mkdir(parents=True)
    for relative in relative_paths:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe Git path in candidate tree: {relative}")
        source_path = source / path
        target_path = target / path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = source_path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target_path.symlink_to(os.readlink(source_path))
        elif stat.S_ISREG(metadata.st_mode):
            shutil.copy2(source_path, target_path)
        else:
            raise RuntimeError(f"candidate tree contains an unsupported path: {relative}")


def _tree_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            manifest[relative] = f"link:{mode:o}:{os.readlink(path)}"
        elif stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest[relative] = f"file:{mode:o}:{digest}"
        elif stat.S_ISDIR(metadata.st_mode):
            manifest[relative] = f"directory:{mode:o}"
        else:
            raise RuntimeError(f"installed environment contains a special path: {relative}")
    return manifest


def _git(root: Path, *arguments: str) -> str:
    return _run(["git", "-C", str(root), *arguments], os.environ.copy()).stdout.strip()


def _json_cli(binary: Path, environment: dict[str, str], arguments: list[str]) -> dict[str, Any]:
    payload = _json_process([str(binary), "--json", *arguments], environment)
    if payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
        raise RuntimeError(f"Seld command failed closed: {' '.join(arguments)}")
    return cast(dict[str, Any], payload["result"])


def _json_process(
    command: list[str],
    environment: dict[str, str],
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    result = _run(command, environment, timeout=timeout)
    return _parse_json_output(command, result.stdout)


def _json_process_with_availability_probe(
    command: list[str],
    environment: dict[str, str],
    *,
    primary: Path,
    recovery: Path,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    deadline = time.monotonic() + timeout
    probes = 0
    gaps = 0
    while process.poll() is None:
        probes += 1
        if not _is_executable(primary) and not _is_executable(recovery):
            gaps += 1
        if time.monotonic() >= deadline:
            process.kill()
            process.communicate(timeout=30)
            raise RuntimeError("installed self-update exceeded the availability-probe timeout")
        time.sleep(0.005)
    stdout, stderr = process.communicate(timeout=30)
    if process.returncode != 0:
        raise RuntimeError(
            f"command failed ({process.returncode}): {' '.join(command)}\n"
            f"stdout: {stdout[-3000:]}\nstderr: {stderr[-3000:]}"
        )
    if gaps:
        raise RuntimeError(f"gsv and {_RECOVERY_LAUNCHER} were both unavailable in {gaps} probes")
    return _parse_json_output(command, stdout), {
        "gap_probes": gaps,
        "probe_interval_ms": 5,
        "probes": probes,
    }


def _parse_json_output(command: list[str], stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command returned invalid JSON: {command[0]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"command returned a non-object JSON result: {command[0]}")
    return payload


def _wait_for_json_file(
    path: Path,
    process: subprocess.Popen[str],
    *,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.01)
                continue
            if isinstance(payload, dict):
                return payload
        returncode = process.poll()
        if returncode is not None:
            stdout, stderr = process.communicate(timeout=30)
            raise RuntimeError(
                "update exited before reaching the crash boundary: "
                f"returncode={returncode}; stdout={stdout[-2000:]}; stderr={stderr[-2000:]}"
            )
        time.sleep(0.01)
    raise RuntimeError("update did not reach the crash boundary before timeout")


def _is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _run(
    command: list[str],
    environment: dict[str, str],
    *,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
