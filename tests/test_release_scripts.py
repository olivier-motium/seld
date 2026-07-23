from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import continuity_kernel.cli as cli_module
from continuity_kernel import __version__
from scripts import e2e_clean_install, privacy_check
from scripts.e2e_clean_install import _require_native_codex

ROOT = Path(__file__).resolve().parents[1]


def _write_uninstall_fixture(path: Path) -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "json_mode = bool(args and args[0] == '--json')\n"
        "if json_mode:\n"
        "    args = args[1:]\n"
        "if args[:2] == ['bridge', 'stop']:\n"
        "    status = int(os.environ.get('GSV_TEST_BRIDGE_STATUS', '0'))\n"
        "    print(json.dumps({'ok': status == 0, 'result': {'stopped': False}}))\n"
        "    raise SystemExit(status)\n"
        "if args[:2] == ['codex', 'uninstall']:\n"
        "    status = int(os.environ.get('GSV_TEST_UNINSTALL_STATUS', '0'))\n"
        "    retained = os.environ.get('GSV_TEST_RECOVERY_RETAINED', '0') == '1'\n"
        "    mode = os.environ.get('GSV_TEST_OUTPUT_MODE', 'compact')\n"
        "    if mode == 'malformed':\n"
        "        print('not-json')\n"
        "        raise SystemExit(status)\n"
        "    cleanup_complete = status == 0 and not retained and mode != 'cleanup-false'\n"
        "    payload = {'ok': status == 0, 'result': {"
        "'cleanup_complete': cleanup_complete, "
        "'integration_removed': status in (0, 3), 'recovery_retained': retained, "
        "'retained_cleanup_paths': ['/synthetic/recovery'] if retained else []}, "
        "'error': 'Retry with gsv codex uninstall.' if status else None}\n"
        "    if mode == 'pretty':\n"
        "        print(json.dumps(payload, indent=2))\n"
        "    else:\n"
        "        print(json.dumps(payload, separators=(',', ':')))\n"
        "    raise SystemExit(status)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


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


def test_cli_json_keeps_the_posix_cleanup_gate_stable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_module._print(
        {"cleanup_complete": True, "integration_removed": True, "recovery_retained": False},
        json_output=True,
        raw=False,
        ok=True,
    )

    output = capsys.readouterr().out
    assert output.startswith('{"ok":true,"result":{"cleanup_complete":true')


def test_e2e_helper_accepts_only_structured_exit_three_cleanup(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "gsv"
    _write_uninstall_fixture(binary)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_TEST_UNINSTALL_STATUS": "3",
            "GSV_TEST_RECOVERY_RETAINED": "1",
        }
    )

    result = e2e_clean_install._cli_expected_incomplete_cleanup(
        binary,
        environment,
        ["codex", "uninstall"],
    )

    assert result["cleanup_complete"] is False
    assert result["integration_removed"] is True
    assert result["recovery_retained"] is True


def test_clean_e2e_manifest_matches_receipt_shape(tmp_path: Path) -> None:
    root = tmp_path / "marketplace"
    nested = root / "plugins/gsv"
    nested.mkdir(parents=True)
    skill = nested / "SKILL.md"
    skill.write_bytes(b"# Synthetic skill\n")

    assert e2e_clean_install._directory_manifest(root) == {
        "plugins": "directory",
        "plugins/gsv": "directory",
        "plugins/gsv/SKILL.md": f"file:{hashlib.sha256(skill.read_bytes()).hexdigest()}",
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX minimal-PATH proof")
def test_clean_e2e_minimal_path_includes_uninstaller_json_normalizer(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "candidate"
    binary.write_bytes(b"synthetic candidate")
    environment = e2e_clean_install._isolated_environment(
        root=tmp_path,
        home=tmp_path / "home",
        config=tmp_path / "config",
        data=tmp_path / "data",
        codex_home=tmp_path / "codex",
        vault=tmp_path / "vault",
        install_bin=tmp_path / "bin",
        binary=binary,
        codex=Path(shutil.which("codex") or "/usr/bin/true"),
    )

    assert shutil.which("tr", path=environment["PATH"]) is not None


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX uninstaller test")
@pytest.mark.parametrize(
    ("bridge_status", "cleanup_status"),
    [(0, 0), (0, 3), (4, 0)],
)
def test_posix_uninstaller_removes_binary_only_after_verified_cleanup(
    tmp_path: Path, bridge_status: int, cleanup_status: int
) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    _write_uninstall_fixture(target)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_BRIDGE_STATUS": str(bridge_status),
            "GSV_TEST_UNINSTALL_STATUS": str(cleanup_status),
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/uninstall.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    expected_status = bridge_status or cleanup_status
    assert result.returncode == expected_status
    if expected_status == 0:
        assert not target.exists()
        assert "verified GSV-owned integration" in result.stdout
    else:
        assert target.exists()
        if cleanup_status:
            assert "Retry with gsv codex uninstall" in result.stdout
            assert "executable was kept" in result.stderr
        else:
            assert "Bridge could not be stopped" in result.stderr
            assert "executable was kept" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX uninstaller test")
@pytest.mark.parametrize("output_mode", ["compact", "pretty"])
def test_posix_uninstaller_keeps_binary_until_recovery_evidence_is_retired(
    tmp_path: Path,
    output_mode: str,
) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    _write_uninstall_fixture(target)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_BRIDGE_STATUS": "0",
            "GSV_TEST_UNINSTALL_STATUS": "3",
            "GSV_TEST_RECOVERY_RETAINED": "1",
            "GSV_TEST_OUTPUT_MODE": output_mode,
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/uninstall.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 3
    assert target.exists()
    assert '"recovery_retained"' in result.stdout
    assert "exact retained_cleanup_paths" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX uninstaller test")
@pytest.mark.parametrize("output_mode", ["malformed", "cleanup-false"])
def test_posix_uninstaller_keeps_binary_on_unverified_exit_zero_output(
    tmp_path: Path,
    output_mode: str,
) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    _write_uninstall_fixture(target)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_BRIDGE_STATUS": "0",
            "GSV_TEST_UNINSTALL_STATUS": "0",
            "GSV_TEST_OUTPUT_MODE": output_mode,
        }
    )

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/uninstall.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 3
    assert target.exists()
    assert "did not verify result.cleanup_complete:true" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX executable fixture under PowerShell")
@pytest.mark.parametrize(
    ("bridge_status", "cleanup_status", "output_mode"),
    [
        (0, 0, "compact"),
        (0, 3, "compact"),
        (4, 0, "compact"),
        (0, 0, "malformed"),
        (0, 0, "cleanup-false"),
    ],
)
def test_powershell_uninstaller_removes_binary_only_after_verified_cleanup(
    tmp_path: Path,
    bridge_status: int,
    cleanup_status: int,
    output_mode: str,
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv.exe"
    _write_uninstall_fixture(target)
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_BRIDGE_STATUS": str(bridge_status),
            "GSV_TEST_UNINSTALL_STATUS": str(cleanup_status),
            "GSV_TEST_OUTPUT_MODE": output_mode,
        }
    )

    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(ROOT / "scripts/uninstall.ps1")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    if bridge_status == 0 and cleanup_status == 0 and output_mode == "compact":
        assert result.returncode == 0, result.stderr
        assert not target.exists()
        assert "verified GSV-owned integration" in result.stdout
    else:
        assert result.returncode != 0
        assert target.exists()
        if output_mode in {"malformed", "cleanup-false"}:
            assert "executable was kept" in result.stderr.lower()
        elif cleanup_status:
            assert "Retry with gsv codex uninstall" in result.stdout
            assert "executable was kept" in result.stderr
        else:
            assert "Bridge stop failed" in result.stderr


def test_powershell_uninstaller_guards_both_failures_before_binary_removal() -> None:
    script = (ROOT / "scripts/uninstall.ps1").read_text(encoding="utf-8")
    bridge_call = script.index("& $Target bridge stop")
    bridge_guard = script.index("if ($LASTEXITCODE -ne 0)", bridge_call)
    codex_call = script.index("& $Target --json codex uninstall", bridge_guard)
    cleanup_guard = script.index("if ($CleanupStatus -ne 0)", codex_call)
    completion_guard = script.index(
        "$CleanupPayload.result.cleanup_complete -ne $true", cleanup_guard
    )
    binary_removal = script.index("Remove-Item -LiteralPath $Target -Force", completion_guard)

    assert (
        bridge_call < bridge_guard < codex_call < cleanup_guard < completion_guard < binary_removal
    )
    assert "The executable was kept" in script


def test_uninstallers_do_not_depend_on_jq_for_cleanup_decisions() -> None:
    for name in ("uninstall.sh", "uninstall.ps1"):
        assert "jq" not in (ROOT / "scripts" / name).read_text(encoding="utf-8").lower()


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
