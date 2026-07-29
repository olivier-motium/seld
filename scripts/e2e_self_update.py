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

_APPROVAL_REF = "codex:019f0000-0000-7000-8000-000000000777"
_FIXTURE_MARKER = "src/continuity_kernel/_self_update_e2e_revision.py"
_RECOVERY_LAUNCHER = "seld-recover"
_SIGKILL = int(getattr(signal, "SIGKILL", signal.SIGTERM))
_WHATSAPP_SERVICE_LABEL_ENV = "GSV_WHATSAPP_SERVICE_LABEL"
_SYNTHETIC_WHATSAPP_SERVICE_LABEL = "ai.example.cutover-wacli"
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


def _post_native_commit_failure_runner(to_sha: str):
    # Fail the updater only after the candidate native command committed.

    base = update._run_command
    installed = update.installed_provenance()
    if installed.environment is None:
        raise RuntimeError("installed Seld has no environment for native failure injection")
    active = Path(installed.environment)
    expected_revisions: list[str] = []
    installed_revisions: list[str] = []
    injected = False

    def run(command: list[str], environment: Any, timeout: float):
        nonlocal injected
        is_native_install = "native-install" in command
        active_sha = None
        if is_native_install:
            try:
                active_sha = update._environment_sha(active)
            except Exception:
                active_sha = None
            try:
                revision_index = command.index("--expected-revision") + 1
                expected_revisions.append(command[revision_index])
            except (IndexError, ValueError):
                raise RuntimeError("native install did not carry its exact CAS revision") from None
        completed = base(command, environment, timeout)
        if is_native_install and completed.returncode == 0:
            try:
                payload = json.loads(completed.stdout)
                result = payload["result"]
                revision = result["ownership_revision"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("native install returned no ownership revision") from exc
            if not isinstance(revision, str):
                raise RuntimeError("native install returned an invalid ownership revision")
            installed_revisions.append(revision)
            if active_sha == to_sha and not injected:
                injected = True
                return update.CommandResult(
                    87,
                    completed.stdout,
                    b"injected failure after candidate native installation committed",
                )
        return completed

    def evidence() -> dict[str, Any]:
        return {
            "candidate_commit_injected": injected,
            "expected_revisions": expected_revisions,
            "installed_revisions": installed_revisions,
        }

    return run, evidence


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
    parser.add_argument("--inject-post-native-commit-failure", action="store_true")
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
    native_injection = lambda: {}
    if args.inject_candidate_failure:
        runner, injected = _candidate_failure_runner(args.to_sha)
    if args.inject_post_native_commit_failure:
        if runner is not None:
            raise RuntimeError("candidate and native failure hooks are mutually exclusive")
        runner, native_injection = _post_native_commit_failure_runner(args.to_sha)
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
    native_evidence = native_injection()
    if args.inject_post_native_commit_failure and not native_evidence.get(
        "candidate_commit_injected"
    ):
        raise RuntimeError("the post-native-commit failure was not injected")
    print(
        json.dumps(
            {
                "check_revision": revision,
                "native_injection": native_evidence,
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
    native_before: dict[str, Any] | None
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
    parser.add_argument(
        "--native",
        action="store_true",
        help="also prove the owned macOS app lifecycle inside the isolated HOME",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="seld-self-update-e2e-")).resolve()
    try:
        report = run_e2e(root, args.source.resolve(), native=args.native)
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


def run_e2e(root: Path, source: Path, *, native: bool = False) -> dict[str, Any]:
    if os.name == "nt":
        raise RuntimeError("source self-update is not supported on Windows")
    if native and sys.platform != "darwin":
        raise RuntimeError("the native self-update proof is available only on macOS")
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
    (fixture / _FIXTURE_MARKER).write_text('REVISION = "B"\n', encoding="utf-8")
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
        native=native,
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
        native=native,
    )
    crash_recovery = _crash_recovery_scenario(
        root / "crash-recovery",
        uv=Path(uv),
        codex=Path(codex),
        repository_url=repository_url,
        revision_a=revision_a,
        revision_b=revision_b,
        adapter=adapter,
        native=native,
    )
    return {
        "crash_recovery": crash_recovery,
        "isolated_execution": True,
        "native_lifecycle": native,
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
    native: bool,
) -> dict[str, Any]:
    prepared = _prepare_revision_a(
        root,
        uv=uv,
        codex=codex,
        repository_url=repository_url,
        revision_a=revision_a,
        native=native,
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
        apply_command.append(
            "--inject-post-native-commit-failure" if native else "--inject-candidate-failure"
        )
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
        whatsapp_service_label_preserved = (
            _installed_mcp_service_label(paths) == _SYNTHETIC_WHATSAPP_SERVICE_LABEL
        )
        if not whatsapp_service_label_preserved:
            raise RuntimeError("self-update did not preserve the installed WhatsApp service label")
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
        native_after: dict[str, Any] | None = None
        native_result: dict[str, Any] | None = None
        if native:
            if prepared.native_before is None:
                raise RuntimeError("native proof did not retain revision A ownership evidence")
            native_before_revision = str(prepared.native_before["ownership_revision"])
            native_after = _fresh_native_status(gsv, environment, vault, paths)
            native_after_revision = str(native_after["ownership_revision"])
            transaction = applied.get("result")
            if not isinstance(transaction, dict):
                raise RuntimeError("native update returned no transaction result")
            if transaction.get("native_bridge_revision_before") != native_before_revision:
                raise RuntimeError("native update did not anchor revision A ownership")
            if transaction.get("native_bridge_revision_current") != native_after_revision:
                raise RuntimeError("native update did not persist its final ownership revision")
            if inject_failure:
                injection = applied.get("native_injection")
                if not isinstance(injection, dict):
                    raise RuntimeError("native rollback returned no commit-boundary evidence")
                expected_revisions = injection.get("expected_revisions")
                installed_revisions = injection.get("installed_revisions")
                if (
                    injection.get("candidate_commit_injected") is not True
                    or not isinstance(expected_revisions, list)
                    or not isinstance(installed_revisions, list)
                    or len(expected_revisions) != 2
                    or len(installed_revisions) != 2
                    or expected_revisions[0] != native_before_revision
                    or expected_revisions[1] != installed_revisions[0]
                    or installed_revisions[1] != native_after_revision
                    or installed_revisions[0] == native_before_revision
                    or installed_revisions[0] == native_after_revision
                ):
                    raise RuntimeError(
                        "native rollback did not reinstall revision A from the committed "
                        "candidate's exact CAS revision"
                    )
                native_result = {
                    "candidate_commit_revision": installed_revisions[0],
                    "restored_current": native_after.get("current") is True,
                    "restored_revision": native_after_revision,
                    "restore_expected_revision": expected_revisions[1],
                    "revision_a": native_before_revision,
                }
            else:
                if native_after_revision == native_before_revision:
                    raise RuntimeError("native update did not advance the owned application")
                native_result = {
                    "advanced": True,
                    "current": native_after.get("current") is True,
                    "revision_a": native_before_revision,
                    "revision_b": native_after_revision,
                }
        result = {
            "bridge_healthy": after["bridge_healthy"],
            "chatgpt_integration_ready": after["chatgpt_integration_ready"],
            "fresh_process_sha": observed_sha,
            "outcome": outcome,
            "recovery_launcher_cleaned": recovery_launcher_cleaned,
            "stale_check_rejected": applied.get("stale_check_rejected") is True,
            "vault_digest_preserved": before["digest"] == after["digest"],
            "vault_id_preserved": before["vault_id"] == after["vault_id"],
            "whatsapp_service_label_preserved": whatsapp_service_label_preserved,
        }
        if inject_failure:
            result["environment_restored_byte_for_byte"] = restored_byte_for_byte
        else:
            result["candidate_environment_replaced_prior"] = not restored_byte_for_byte
            result["continuous_executable"] = availability
        if native:
            assert native_after is not None and native_result is not None
            native_result["uninstall"] = _prove_native_uninstall(
                gsv,
                environment,
                paths,
                native_after,
            )
            result["native"] = native_result
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
    native: bool,
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
    environment[_WHATSAPP_SERVICE_LABEL_ENV] = _SYNTHETIC_WHATSAPP_SERVICE_LABEL
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
    if _installed_mcp_service_label(paths) != _SYNTHETIC_WHATSAPP_SERVICE_LABEL:
        raise RuntimeError("revision A did not bind the synthetic WhatsApp service label")
    environment.pop(_WHATSAPP_SERVICE_LABEL_ENV, None)
    native_before = None
    if native:
        installed_native = _json_cli(
            gsv,
            environment,
            ["--vault", str(vault), "bridge", "native-install"],
        )
        native_before = _fresh_native_status(gsv, environment, vault, paths)
        if installed_native.get("changed") is not True or installed_native.get(
            "ownership_revision"
        ) != native_before.get("ownership_revision"):
            raise RuntimeError("revision A did not install one exact owned native application")
    return _PreparedInstall(
        before=_fresh_health(gsv, environment, vault, paths["codex"]),
        environment=environment,
        gsv=gsv,
        native_before=native_before,
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
    native: bool,
) -> dict[str, Any]:
    prepared = _prepare_revision_a(
        root,
        uv=uv,
        codex=codex,
        repository_url=repository_url,
        revision_a=revision_a,
        native=native,
    )
    environment = prepared.environment
    gsv = prepared.gsv
    paths = prepared.paths
    vault = prepared.vault
    pause_marker = root / "paused-before-candidate.json"
    recovery = paths["bin"] / _RECOVERY_LAUNCHER
    native_before = prepared.native_before
    native_manifest_before: dict[str, str] | None = None
    if native:
        if native_before is None:
            raise RuntimeError("native crash proof did not retain revision A ownership")
        native_manifest_before = _tree_manifest(Path(str(native_before["application"])))
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
        native_interrupted: dict[str, Any] | None = None
        if native:
            assert native_before is not None and native_manifest_before is not None
            native_interrupted = _json_cli(recovery, hostile, ["bridge", "native-status"])
            if (
                native_interrupted.get("installed") is not True
                or native_interrupted.get("owned") is not True
                or native_interrupted.get("healthy") is not True
                or native_interrupted.get("current") is not False
                or native_interrupted.get("ownership_revision")
                != native_before.get("ownership_revision")
                or native_interrupted.get("receipt_revision")
                != native_before.get("receipt_revision")
                or _tree_manifest(Path(str(native_before["application"]))) != native_manifest_before
            ):
                raise RuntimeError("SIGKILL-before-candidate did not leave native revision A")

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
        whatsapp_service_label_preserved = (
            _installed_mcp_service_label(paths) == _SYNTHETIC_WHATSAPP_SERVICE_LABEL
        )
        if not whatsapp_service_label_preserved:
            raise RuntimeError(
                "crash recovery did not preserve the installed WhatsApp service label"
            )
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
        native_after: dict[str, Any] | None = None
        if native:
            assert native_before is not None and native_manifest_before is not None
            native_after = _fresh_native_status(gsv, environment, vault, paths)
            if (
                native_after.get("ownership_revision") != native_before.get("ownership_revision")
                or native_after.get("receipt_revision") != native_before.get("receipt_revision")
                or _tree_manifest(Path(str(native_after["application"]))) != native_manifest_before
            ):
                raise RuntimeError("crash recovery replayed or replaced native revision A")
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
        native_after_replay = (
            _fresh_native_status(gsv, environment, vault, paths) if native else None
        )
        no_replay = (
            replay.get("outcome") == "rolled_back"
            and _tree_manifest(active) == before_replay
            and bridge_before_replay.get("instance_id") == bridge_after_replay.get("instance_id")
            and (
                not native
                or (
                    native_before is not None
                    and native_after_replay is not None
                    and native_after_replay.get("ownership_revision")
                    == native_before.get("ownership_revision")
                    and native_after_replay.get("receipt_revision")
                    == native_before.get("receipt_revision")
                    and _tree_manifest(Path(str(native_after_replay["application"])))
                    == native_manifest_before
                )
            )
        )
        if not no_replay:
            raise RuntimeError("terminal recovery replayed candidate installation or setup")
        result = {
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
            "whatsapp_service_label_preserved": whatsapp_service_label_preserved,
        }
        if native:
            assert (
                native_after_replay is not None
                and native_before is not None
                and native_interrupted is not None
            )
            result["native"] = {
                "bundle_remained_revision_a": True,
                "interrupted_runtime_current": native_interrupted.get("current") is True,
                "no_reinstall_or_replay": True,
                "revision_a": native_before["ownership_revision"],
                "uninstall": _prove_native_uninstall(
                    gsv,
                    environment,
                    paths,
                    native_after_replay,
                ),
            }
        return result
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


def _fresh_native_status(
    gsv: Path,
    environment: dict[str, str],
    vault: Path,
    paths: dict[str, Path],
) -> dict[str, Any]:
    status = _json_cli(gsv, environment, ["bridge", "native-status"])
    expected_application = paths["home"] / "Applications/Seld.app"
    if (
        status.get("application") != str(expected_application)
        or status.get("installed") is not True
        or status.get("owned") is not True
        or status.get("healthy") is not True
        or status.get("current") is not True
        or status.get("vault") != str(vault)
        or status.get("ownership_revision") in {None, "absent"}
        or not expected_application.is_dir()
    ):
        raise RuntimeError("isolated native Seld application is not healthy and current")
    return status


def _prove_native_uninstall(
    gsv: Path,
    environment: dict[str, str],
    paths: dict[str, Path],
    current: dict[str, Any],
) -> dict[str, Any]:
    revision = current.get("ownership_revision")
    if not isinstance(revision, str) or revision == "absent":
        raise RuntimeError("native uninstall requires one exact current ownership revision")
    removed = _json_cli(
        gsv,
        environment,
        ["bridge", "native-uninstall", "--expected-revision", revision],
    )
    absent = _json_cli(gsv, environment, ["bridge", "native-status"])
    application = paths["home"] / "Applications/Seld.app"
    residue = sorted(
        path.name
        for pattern in (".Seld.app.previous-*", ".Seld.app.uninstall-*", ".seld-native-*")
        for path in application.parent.glob(pattern)
    )
    lifecycle = paths["data"] / "native-bridge/state/lifecycle.json"
    authority = paths["data"] / "native-bridge/state/ownership.json"
    try:
        authority_state = json.loads(authority.read_text(encoding="utf-8")).get("state")
        lifecycle_state = json.loads(lifecycle.read_text(encoding="utf-8"))
    except (AttributeError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("native uninstall left no valid idle/absent lifecycle state") from exc
    checks = {
        "absent_application": absent.get("application") == str(application),
        "absent_installed": absent.get("installed") is False,
        "absent_owned": absent.get("owned") is False,
        "application_removed": not os.path.lexists(application),
        "authority_absent": authority_state == "absent",
        "lifecycle_idle": lifecycle_state == {"format_version": 1, "state": "idle"},
        "removed": removed.get("removed") is True,
        "revision_advanced": (
            removed.get("ownership_revision") == absent.get("ownership_revision")
        ),
        "temporary_residue_absent": not residue,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise RuntimeError(
            "exact native uninstall left application or lifecycle residue: " + ", ".join(failed)
        )
    return {
        "absent_state_cas_retained": True,
        "application_removed": True,
        "expected_revision": revision,
        "lifecycle_operation_residue": False,
        "ownership_revision": absent["ownership_revision"],
        "temporary_residue": [],
    }


def _installed_mcp_service_label(paths: dict[str, Path]) -> object:
    home = paths["codex"].resolve()
    identity = hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:16]
    mcp = paths["data"] / "marketplaces" / identity / "plugins/gsv/.mcp.json"
    payload = json.loads(mcp.read_text(encoding="utf-8"))
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    server = servers.get("gsv") if isinstance(servers, dict) else None
    environment = server.get("env") if isinstance(server, dict) else None
    if not isinstance(environment, dict):
        raise RuntimeError("installed Seld MCP configuration has no environment")
    return environment.get(_WHATSAPP_SERVICE_LABEL_ENV)


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
