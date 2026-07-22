from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from continuity_kernel import __version__
from scripts import e2e_clean_install, privacy_check
from scripts.e2e_clean_install import _require_native_codex

ROOT = Path(__file__).resolve().parents[1]


def test_candidate_version_is_consistent_across_runtime_installers_and_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_gsv = next(package for package in lock["package"] if package["name"] == "gsv")

    assert project["project"]["version"] == __version__ == "0.2.0"
    assert locked_gsv["version"] == __version__
    assert f"GSV_VERSION:-{__version__}" in (ROOT / "scripts/install.sh").read_text(
        encoding="utf-8"
    )
    assert f'else {{ "{__version__}" }}' in (ROOT / "scripts/install.ps1").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_posix_reinstall_failure_restores_previous_binary(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    old = b"#!/bin/sh\nprintf 'old binary\\n'\n"
    target.write_bytes(old)
    target.chmod(0o755)
    candidate = tmp_path / "candidate"
    candidate.write_text(
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  printf 'gsv 0.2.0\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "bridge" ] && [ "${3:-}" = "stop" ]; then
  printf '{"ok":true,"result":{"running":false,"stopped":false}}\\n'
  exit 0
fi
if [ "${1:-}" = "setup" ]; then
  exit 42
fi
exit 2
""",
        encoding="utf-8",
    )
    candidate.chmod(0o755)
    fake_tools = tmp_path / "tools"
    fake_tools.mkdir()
    (fake_tools / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_tools / "codex").chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BINARY": str(candidate),
            "GSV_BINARY_SHA256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "GSV_BIN_DIR": str(install_dir),
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_tools}{os.pathsep}{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/install.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 42
    assert target.read_bytes() == old, result.stderr
    assert not list(install_dir.glob(".gsv.*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_posix_failed_upgrade_restarts_previously_live_bridge(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    restarted = tmp_path / "restarted"
    old = f"""#!/bin/sh
if [ "${{1:-}}" = "--json" ] && [ "${{2:-}}" = "bridge" ] && [ "${{3:-}}" = "open" ]; then
  printf 'restarted\\n' > {restarted}
  printf '{{"ok":true,"result":{{"running":true}}}}\\n'
  exit 0
fi
printf 'old binary\\n'
""".encode()
    target.write_bytes(old)
    target.chmod(0o755)
    stop_count = tmp_path / "stop-count"
    candidate = tmp_path / "candidate"
    candidate.write_text(
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  printf 'gsv 0.2.0\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "bridge" ] && [ "${3:-}" = "stop" ]; then
  count=0
  [ ! -f "$GSV_TEST_STOP_COUNT" ] || count="$(cat "$GSV_TEST_STOP_COUNT")"
  count=$((count + 1))
  printf '%s\\n' "$count" > "$GSV_TEST_STOP_COUNT"
  if [ "$count" -eq 1 ]; then
    printf '{"ok":true,"result":{"running":false,"stopped":true}}\\n'
  else
    printf '{"ok":true,"result":{"running":false,"stopped":false}}\\n'
  fi
  exit 0
fi
if [ "${1:-}" = "setup" ]; then
  exit 42
fi
exit 2
""",
        encoding="utf-8",
    )
    candidate.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BINARY": str(candidate),
            "GSV_BINARY_SHA256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_STOP_COUNT": str(stop_count),
            "HOME": str(tmp_path / "home"),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/install.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 42
    assert target.read_bytes() == old, result.stderr
    assert restarted.read_text(encoding="utf-8") == "restarted\n"
    assert stop_count.read_text(encoding="utf-8") == "2\n"
    assert not list(install_dir.glob(".gsv.*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_posix_install_primitive_failure_removes_staged_file(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    candidate = tmp_path / "candidate"
    candidate.write_text("#!/bin/sh\nprintf 'gsv 0.2.0\\n'\n", encoding="utf-8")
    candidate.chmod(0o755)
    fake_tools = tmp_path / "tools"
    fake_tools.mkdir()
    fake_install = fake_tools / "install"
    fake_install.write_text(
        '#!/bin/sh\n: > "$4"\nexit 23\n',
        encoding="utf-8",
    )
    fake_install.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BINARY": str(candidate),
            "GSV_BINARY_SHA256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "GSV_BIN_DIR": str(install_dir),
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_tools}{os.pathsep}{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/install.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 23
    assert not (install_dir / "gsv").exists()
    assert not list(install_dir.glob(".gsv.*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_posix_rollback_preserves_backup_when_stop_result_is_invalid(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    old = b"#!/bin/sh\nprintf 'old binary\\n'\n"
    target.write_bytes(old)
    target.chmod(0o755)
    stop_count = tmp_path / "stop-count"
    candidate = tmp_path / "candidate"
    candidate.write_text(
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  printf 'gsv 0.2.0\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "bridge" ] && [ "${3:-}" = "stop" ]; then
  count=0
  [ ! -f "$GSV_TEST_STOP_COUNT" ] || count="$(cat "$GSV_TEST_STOP_COUNT")"
  count=$((count + 1))
  printf '%s\n' "$count" > "$GSV_TEST_STOP_COUNT"
  if [ "$count" -eq 1 ]; then
    printf '{"ok":true,"result":{"running":false,"stopped":false}}\n'
  else
    printf '{"ok":false}\n'
  fi
  exit 0
fi
if [ "${1:-}" = "setup" ]; then
  exit 42
fi
exit 2
""",
        encoding="utf-8",
    )
    candidate.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BINARY": str(candidate),
            "GSV_BINARY_SHA256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_STOP_COUNT": str(stop_count),
            "HOME": str(tmp_path / "home"),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/install.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 42
    assert target.read_bytes() == candidate.read_bytes()
    backups = list(install_dir.glob(".gsv.previous.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == old
    assert "previous executable remains staged" in result.stderr
    assert "exit 2" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="PowerShell executable rollback needs Windows")
def test_powershell_reinstall_failure_restores_previous_binary(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv.exe"
    old = b"synthetic previous executable"
    target.write_bytes(old)
    source = tmp_path / "Candidate.cs"
    source.write_text(
        r"""
using System;

public static class Candidate {
    public static int Main(string[] args) {
        if (args.Length == 1 && args[0] == "--version") {
            Console.WriteLine("gsv 0.2.0");
            return 0;
        }
        if (args.Length == 3 && args[0] == "--json" && args[1] == "bridge" && args[2] == "stop") {
            Console.WriteLine("{\"ok\":true,\"result\":{\"running\":false,\"stopped\":false}}");
            return 0;
        }
        if (args.Length >= 1 && args[0] == "setup") {
            return 42;
        }
        return 2;
    }
}
""",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate.exe"
    compile_environment = os.environ.copy()
    compile_environment.update(
        {
            "GSV_TEST_CANDIDATE": str(candidate),
            "GSV_TEST_CSHARP": str(source),
        }
    )
    subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-Command",
            "Add-Type -TypeDefinition (Get-Content -Raw -LiteralPath $env:GSV_TEST_CSHARP) "
            "-Language CSharp -OutputAssembly $env:GSV_TEST_CANDIDATE "
            "-OutputType ConsoleApplication",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=compile_environment,
        timeout=60,
    )
    fake_tools = tmp_path / "tools"
    fake_tools.mkdir()
    (fake_tools / "codex.cmd").write_text("@exit /b 0\n", encoding="ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BINARY": str(candidate),
            "GSV_BINARY_SHA256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "GSV_BIN_DIR": str(install_dir),
            "PATH": f"{fake_tools}{os.pathsep}{environment['PATH']}",
        }
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(ROOT / "scripts/install.ps1")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )

    assert result.returncode != 0
    assert target.read_bytes() == old, result.stderr
    assert not list(install_dir.glob(".gsv.*"))


def test_history_privacy_scan_flags_oversized_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    oversized = tmp_path / "large-fixture.bin"
    oversized.write_bytes(b"12345")
    subprocess.run(["git", "add", "large-fixture.bin"], cwd=tmp_path, check=True)
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_NAME": "Synthetic Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic Test",
        }
    )
    subprocess.run(["git", "commit", "-q", "-m", "test"], cwd=tmp_path, env=environment, check=True)
    monkeypatch.setattr(privacy_check, "MAX_SCAN_BYTES", 4)

    findings = privacy_check.scan_history(tmp_path, privacy_check.PATTERNS)

    assert findings == [privacy_check.Finding("large-fixture.bin", "git-history-oversized")]


def test_artifact_directory_scan_is_recursive(tmp_path: Path) -> None:
    nested = tmp_path / "artifacts/platform"
    nested.mkdir(parents=True)
    (nested / "gsv").write_text("-----BEGIN " + "PRIVATE KEY-----", encoding="utf-8")

    findings, scanned = privacy_check.scan_tree(tmp_path / "artifacts", privacy_check.PATTERNS)

    assert scanned == 1
    assert findings == [privacy_check.Finding("platform/gsv", "working-tree")]


def test_privacy_scan_detects_json_escaped_windows_home(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"path":"C:\\\\Users\\\\synthetic-user\\\\vault"}', encoding="utf-8")

    findings, scanned = privacy_check.scan_tree(tmp_path, privacy_check.PATTERNS)

    assert scanned == 1
    assert findings == [privacy_check.Finding("report.json", "working-tree")]


def test_native_codex_false_result_is_a_hard_e2e_failure() -> None:
    with pytest.raises(RuntimeError, match="did not recover"):
        _require_native_codex(False)

    _require_native_codex(True)


def test_e2e_windows_liveness_branch_never_calls_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []
    monkeypatch.setattr(e2e_clean_install, "_IS_WINDOWS", True)

    def windows_probe(pid: int) -> bool:
        observed.append(pid)
        return True

    monkeypatch.setattr(e2e_clean_install, "_windows_process_alive", windows_probe)

    def forbidden_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("the Windows E2E liveness branch must not call os.kill")

    monkeypatch.setattr("scripts.e2e_clean_install.os.kill", forbidden_kill)

    assert e2e_clean_install._process_alive(4242) is True
    assert observed == [4242]
