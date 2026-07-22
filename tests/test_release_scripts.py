from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import privacy_check
from scripts.e2e_clean_install import _require_native_codex
from scripts.e2e_clean_install import main as e2e_main

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_posix_reinstall_failure_restores_previous_binary(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    old = b"#!/bin/sh\nprintf 'old binary\\n'\n"
    target.write_bytes(old)
    target.chmod(0o755)
    candidate = tmp_path / "candidate"
    candidate.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
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


def test_powershell_reinstall_failure_restores_previous_binary(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv.exe"
    old = b"synthetic previous executable"
    target.write_bytes(old)
    candidate = Path(sys.executable)
    fake_tools = tmp_path / "tools"
    fake_tools.mkdir()
    if os.name == "nt":
        (fake_tools / "codex.cmd").write_text("@exit /b 0\n", encoding="ascii")
    else:
        (fake_tools / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_tools / "codex").chmod(0o755)
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


def test_native_auth_copy_requires_explicit_risk_acknowledgment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "e2e_clean_install.py",
            "--binary",
            str(tmp_path / "binary"),
            "--native-codex",
            "--codex-auth-from",
            str(auth),
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        e2e_main()

    assert stopped.value.code == 2
    assert "--acknowledge-auth-copy-risk" in capsys.readouterr().err
