from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import tomllib
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler

import pytest

import continuity_kernel.cli as cli_module
from continuity_kernel import __version__
from scripts import build_standalone, e2e_clean_install, privacy_check
from scripts.e2e_clean_install import _require_native_codex

ROOT = Path(__file__).resolve().parents[1]


def test_standalone_smoke_does_not_discover_the_host_codex(tmp_path: Path) -> None:
    environment = build_standalone._isolated_environment(tmp_path, tmp_path / "vault")

    assert environment["GSV_CODEX"] == str(tmp_path / "missing-codex")
    assert not Path(environment["GSV_CODEX"]).exists()


def test_artifact_readiness_parity_allows_local_missing_but_release_requires_ready() -> None:
    missing = {
        "oauth_registration_ready": False,
        "registration_readiness": {"google": {"sign_in": "unavailable", "status": "missing"}},
    }

    assert build_standalone._require_connector_readiness_parity(missing, missing) == missing
    assert e2e_clean_install._connector_readiness_receipt(missing, required=False) == missing
    with pytest.raises(RuntimeError, match="missing one or more"):
        e2e_clean_install._connector_readiness_receipt(missing, required=True)


def test_artifact_readiness_parity_rejects_source_and_frozen_drift() -> None:
    source = {
        "oauth_registration_ready": True,
        "registration_readiness": {"google": {"sign_in": "available", "status": "ready"}},
    }
    frozen = {
        "oauth_registration_ready": False,
        "registration_readiness": {"google": {"sign_in": "unavailable", "status": "missing"}},
    }

    with pytest.raises(RuntimeError, match="differs from the source build"):
        build_standalone._require_connector_readiness_parity(source, frozen)


@pytest.mark.parametrize("module", [build_standalone, e2e_clean_install])
def test_release_bridge_http_disables_proxies_for_exact_loopback(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class FakeOpener:
        def open(self, request: object, *, timeout: float) -> object:
            observed["request"] = request
            observed["timeout"] = timeout
            return object()

    def fake_build_opener(*handlers: object) -> FakeOpener:
        observed["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr(module, "build_opener", fake_build_opener)
    request = module.Request(
        "http://127.0.0.1:43117/api/v1/snapshot",
        headers={"Authorization": "Bearer synthetic-token"},
    )

    result = module._open_loopback(request, timeout=5)

    handlers = observed["handlers"]
    assert isinstance(handlers, tuple)
    assert len(handlers) == 2
    assert isinstance(handlers[0], ProxyHandler)
    assert cast(Any, handlers[0]).proxies == {}
    assert isinstance(handlers[1], HTTPRedirectHandler)
    assert observed["request"] is request
    assert observed["timeout"] == 5
    assert type(result) is object


@pytest.mark.parametrize("module", [build_standalone, e2e_clean_install])
@pytest.mark.parametrize(
    "url",
    [
        "http://attacker.example:43117/api/v1/snapshot",
        "http://localhost:43117/api/v1/snapshot",
        "https://127.0.0.1:43117/api/v1/snapshot",
        "http://127.0.0.1/api/v1/snapshot",
    ],
)
def test_release_bridge_http_rejects_non_exact_loopback_before_opening(
    module: Any, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = False

    def forbidden_build_opener(*_handlers: object) -> object:
        nonlocal opened
        opened = True
        raise AssertionError("an invalid Bridge URL must not reach the network")

    monkeypatch.setattr(module, "build_opener", forbidden_build_opener)
    request = module.Request(url, headers={"Authorization": "Bearer synthetic-token"})

    with pytest.raises(RuntimeError, match=r"only http://127\.0\.0\.1:<port>"):
        module._open_loopback(request, timeout=5)

    assert opened is False


@pytest.mark.parametrize("module", [build_standalone, e2e_clean_install])
def test_release_bridge_http_refuses_redirect_before_sink_request(module: Any) -> None:
    sink_requests: list[str | None] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            sink_requests.append(self.headers.get("Authorization"))
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_address[1]}/sink")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    original = f"http://127.0.0.1:{redirect.server_address[1]}/source"
    try:
        request = module.Request(
            original,
            headers={"Authorization": "Bearer synthetic-token"},
        )
        with pytest.raises(HTTPError) as rejected:
            module._open_loopback(request, timeout=2)

        assert rejected.value.code == HTTPStatus.FOUND
        assert rejected.value.url == original
        assert sink_requests == []
    finally:
        redirect.shutdown()
        redirect_thread.join(timeout=3)
        redirect.server_close()
        sink.shutdown()
        sink_thread.join(timeout=3)
        sink.server_close()


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

    assert project["project"]["version"] == __version__ == "0.4.0"
    assert locked_gsv["version"] == __version__
    assert f"GSV_VERSION:-{__version__}" in (ROOT / "scripts/install.sh").read_text(
        encoding="utf-8"
    )
    assert f'else {{ "{__version__}" }}' in (ROOT / "scripts/install.ps1").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(os.name == "nt", reason="executes the POSIX installer through /bin/sh")
def test_unpublished_linux_prebuilt_fails_before_network(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "network-called"
    (tools / "uname").write_text(
        '#!/bin/sh\nif [ "${1:-}" = "-s" ]; then printf "Linux\\n"; else printf "x86_64\\n"; fi\n',
        encoding="utf-8",
    )
    (tools / "uname").chmod(0o755)
    (tools / "curl").write_text(
        '#!/bin/sh\nprintf called > "$GSV_TEST_NETWORK_MARKER"\nexit 1\n',
        encoding="utf-8",
    )
    (tools / "curl").chmod(0o755)
    environment = os.environ.copy()
    environment.pop("GSV_BINARY", None)
    environment["GSV_TEST_NETWORK_MARKER"] = str(marker)
    environment["PATH"] = f"{tools}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/install.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 2
    assert "does not publish a Linux prebuilt yet" in result.stderr
    assert not marker.exists()


def _powershell_download_command(tmp_path: Path) -> list[str]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    wrapper = tmp_path / "download-install.ps1"
    wrapper.write_text(
        """$ErrorActionPreference = "Stop"
$env:GSV_BINARY = $null
$env:GSV_BINARY_SHA256 = $null
$env:GSV_VERSION = $null
$env:GSV_RELEASE_BASE_URL = $null
function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile)
    Add-Content -LiteralPath $env:GSV_TEST_DOWNLOAD_LOG -Value $Uri
    if ($Uri.EndsWith('.sha256')) {
        Copy-Item -LiteralPath $env:GSV_TEST_CHECKSUM -Destination $OutFile
    } else {
        Copy-Item -LiteralPath $env:GSV_TEST_ARTIFACT -Destination $OutFile
    }
}
& $env:GSV_TEST_INSTALLER --no-browser
exit $LASTEXITCODE
""",
        encoding="utf-8",
    )
    return [powershell, "-NoProfile", "-File", str(wrapper)]


def test_powershell_installer_download_rejects_checksum_before_install(tmp_path: Path) -> None:
    artifact = tmp_path / "candidate.exe"
    artifact.write_bytes(b"untrusted artifact must never execute")
    checksum = tmp_path / "candidate.sha256"
    checksum.write_text("0" * 64 + "  gsv-windows-x86_64.exe\n", encoding="ascii")
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv.exe"
    target.write_bytes(b"previous executable")
    download_log = tmp_path / "downloads.log"
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_ARTIFACT": str(artifact),
            "GSV_TEST_CHECKSUM": str(checksum),
            "GSV_TEST_DOWNLOAD_LOG": str(download_log),
            "GSV_TEST_INSTALLER": str(ROOT / "scripts/install.ps1"),
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
            "TMPDIR": str(tmp_path),
        }
    )

    result = subprocess.run(
        _powershell_download_command(tmp_path),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode != 0
    assert "Seld artifact checksum verification failed" in result.stderr
    release = f"https://github.com/olivier-motium/seld/releases/download/v{__version__}"
    assert download_log.read_text(encoding="utf-8-sig").splitlines() == [
        f"{release}/gsv-windows-x86_64.exe",
        f"{release}/gsv-windows-x86_64.exe.sha256",
    ]
    assert target.read_bytes() == b"previous executable"
    assert list(install_dir.iterdir()) == [target]
    assert not list(tmp_path.glob("gsv-install-*"))


def test_release_claims_match_the_no_generated_visual_artifact_policy() -> None:
    artifact_suffixes = frozenset({".gif", ".jpeg", ".jpg", ".mov", ".mp4", ".png", ".webp"})
    ignored_roots = frozenset(
        {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist"}
    )
    artifacts: list[str] = []
    for directory, names, filenames in os.walk(ROOT):
        names[:] = [name for name in names if name not in ignored_roots]
        base = Path(directory)
        artifacts.extend(
            (base / name).relative_to(ROOT).as_posix()
            for name in filenames
            if Path(name).suffix.lower() in artifact_suffixes
        )

    assert artifacts == []


def test_local_review_notes_are_excluded_from_source_distributions() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert ".codex/" in ignored
    assert "/.codex" in configuration["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]


def test_repository_artifact_policy_allows_only_the_licensed_fonts(tmp_path: Path) -> None:
    allowed = tmp_path / "src/continuity_kernel/resources/bridge/fonts/nunito-var.woff2"
    allowed.parent.mkdir(parents=True)
    allowed.write_bytes(b"wOF2\0licensed-font")
    screenshot = tmp_path / "review.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")

    assert privacy_check.scan_repository_artifact_policy(tmp_path) == [
        privacy_check.Finding("review.png", "repository-binary-not-allowed")
    ]


def test_repository_artifact_policy_rejects_large_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "review.log"
    report.write_text("too much generated output", encoding="utf-8")
    monkeypatch.setattr(privacy_check, "MAX_REPOSITORY_FILE_BYTES", 8)

    assert privacy_check.scan_repository_artifact_policy(tmp_path) == [
        privacy_check.Finding("review.log", "repository-file-oversized")
    ]
    assert "README visuals" not in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


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


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX executable fixture")
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
    old = b"""#!/bin/sh
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "status" ]; then
  printf '{"ok":true,"result":{"digest":"stable","vault_id":"synthetic"}}\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "doctor" ]; then
  printf '{"ok":true,"result":{"healthy":true}}\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "operation" ] && [ "${3:-}" = "list" ]; then
  printf '{"ok":true,"result":{"pending":[]}}\\n'
  exit 0
fi
printf 'old binary\\n'
"""
    target.write_bytes(old)
    target.chmod(0o755)
    candidate = tmp_path / "candidate"
    candidate.write_text(
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  printf 'gsv 0.3.0\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "bridge" ] && [ "${3:-}" = "stop" ]; then
  printf '{"ok":true,"result":{"running":false,"stopped":false}}\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "rollback-check" ]; then
  printf '{"ok":true,"result":{"compatible":true}}\\n'
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
if [ "${{1:-}}" = "--json" ] && [ "${{2:-}}" = "status" ]; then
  printf '{{"ok":true,"result":{{"digest":"stable","vault_id":"synthetic"}}}}\\n'
  exit 0
fi
if [ "${{1:-}}" = "--json" ] && [ "${{2:-}}" = "doctor" ]; then
  printf '{{"ok":true,"result":{{"healthy":true}}}}\\n'
  exit 0
fi
if [ "${{1:-}}" = "--json" ] && [ "${{2:-}}" = "operation" ] && [ "${{3:-}}" = "list" ]; then
  printf '{{"ok":true,"result":{{"pending":[]}}}}\\n'
  exit 0
fi
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
  printf 'gsv 0.3.0\\n'
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
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "rollback-check" ]; then
  printf '{"ok":true,"result":{"compatible":true}}\\n'
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
def test_posix_failed_upgrade_keeps_candidate_when_previous_reader_is_incompatible(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    old = b"""#!/bin/sh
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "status" ]; then
  printf '{"ok":true,"result":{"digest":"stable","vault_id":"synthetic"}}\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "doctor" ]; then
  printf '{"ok":false,"error":"unsupported task record version 2"}\\n'
  exit 3
fi
exit 2
"""
    target.write_bytes(old)
    target.chmod(0o755)
    candidate = tmp_path / "candidate"
    candidate.write_text(
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  printf 'gsv 0.3.0\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "bridge" ] && [ "${3:-}" = "stop" ]; then
  printf '{"ok":true,"result":{"running":false,"stopped":false}}\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "rollback-check" ]; then
  printf '{"ok":true,"result":{"compatible":false,"reason_code":"previous_reader_probe_failed"}}\\n'
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
    assert "could not prove it can read" in result.stderr
    assert "no rollback was performed" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_posix_post_commit_bridge_failure_keeps_candidate_and_recovery_binary(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv"
    old = b"#!/bin/sh\nprintf 'old binary\\n'\n"
    target.write_bytes(old)
    target.chmod(0o755)
    compatibility_log = tmp_path / "compatibility.log"
    candidate = tmp_path / "candidate"
    candidate.write_text(
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
  printf 'gsv 0.3.0\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "bridge" ] && [ "${3:-}" = "stop" ]; then
  printf '{"ok":true,"result":{"running":false,"stopped":false}}\\n'
  exit 0
fi
if [ "${1:-}" = "--json" ] && [ "${2:-}" = "rollback-check" ]; then
  printf 'unexpected\\n' > "$GSV_TEST_COMPAT"
  exit 99
fi
if [ "${1:-}" = "setup" ]; then
  printf 'Bridge repair required\\n' >&2
  exit 4
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
            "GSV_TEST_COMPAT": str(compatibility_log),
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

    assert result.returncode == 4
    assert target.read_bytes() == candidate.read_bytes()
    backups = list(install_dir.glob(".gsv.previous.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == old
    assert not compatibility_log.exists()
    assert "Bridge needs repair" in result.stderr
    assert "no executable rollback was attempted" in result.stderr
    assert "The Bridge is ready" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer test")
def test_posix_install_primitive_failure_removes_staged_file(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    candidate = tmp_path / "candidate"
    candidate.write_text("#!/bin/sh\nprintf 'gsv 0.3.0\\n'\n", encoding="utf-8")
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
  printf 'gsv 0.3.0\n'
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
        assert "verified Seld-owned integration" in result.stdout
    else:
        assert target.exists()
        if cleanup_status:
            assert "Retry with gsv codex uninstall" in result.stdout
            assert "executable was kept" in result.stderr
        else:
            assert "Bridge could not be stopped" in result.stderr
            assert "executable was kept" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX uninstaller test")
def test_posix_release_uninstaller_rejects_managed_tool_shim(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    managed_binary = tmp_path / "uv-tools" / "gsv"
    managed_binary.parent.mkdir()
    _write_uninstall_fixture(managed_binary)
    target = install_dir / "gsv"
    target.symlink_to(managed_binary)
    environment = os.environ.copy()
    environment["GSV_BIN_DIR"] = str(install_dir)

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/uninstall.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 2
    assert target.is_symlink()
    assert managed_binary.exists()
    assert "uv tool uninstall gsv" in result.stderr
    assert "shim was kept" in result.stderr


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
        assert "verified Seld-owned integration" in result.stdout
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
@pytest.mark.parametrize("setup_exit", (0, 4, 42))
def test_powershell_distinguishes_post_commit_repair_from_rollback(
    tmp_path: Path, setup_exit: int
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    target = install_dir / "gsv.exe"
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        pytest.skip("the Windows runner has no .NET SDK for the executable fixture")
    project = tmp_path / "candidate-src"
    project.mkdir()
    project_file = project / "candidate.csproj"
    project_file.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>candidate</AssemblyName>
    <PublishSingleFile>true</PublishSingleFile>
    <DebugType>None</DebugType>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
    source = project / "Program.cs"
    source.write_text(
        r"""
using System;
using System.IO;

public static class Candidate {
    public static int Main(string[] args) {
        var executable = Path.GetFileName(Environment.ProcessPath ?? "");
        var previous = executable.StartsWith(".gsv.previous.", StringComparison.OrdinalIgnoreCase);
        if (args.Length == 3 && args[0] == "--json" && args[1] == "rollback-check") {
            var log = Environment.GetEnvironmentVariable("GSV_TEST_COMPAT")!;
            File.AppendAllText(log, "rollback-check\n");
            Console.WriteLine("{\"ok\":true,\"result\":{\"compatible\":true}}");
            return 0;
        }
        if (previous && args.Length == 2 && args[0] == "--json" && args[1] == "status") {
            File.AppendAllText(Environment.GetEnvironmentVariable("GSV_TEST_COMPAT")!, "status\n");
            Console.WriteLine("{\"ok\":true,\"result\":{\"digest\":\"stable\",\"vault_id\":\"synthetic\"}}");
            return 0;
        }
        if (previous && args.Length == 2 && args[0] == "--json" && args[1] == "doctor") {
            File.AppendAllText(Environment.GetEnvironmentVariable("GSV_TEST_COMPAT")!, "doctor\n");
            Console.WriteLine("{\"ok\":true,\"result\":{\"healthy\":true}}");
            return 0;
        }
        if (args.Length == 1 && args[0] == "--version") {
            Console.WriteLine("gsv 0.3.0");
            return 0;
        }
        if (args.Length == 3 && args[0] == "--json" && args[1] == "bridge" && args[2] == "stop") {
            Console.WriteLine("{\"ok\":true,\"result\":{\"running\":false,\"stopped\":false}}");
            return 0;
        }
        if (args.Length >= 1 && args[0] == "setup") {
            return int.Parse(Environment.GetEnvironmentVariable("GSV_TEST_SETUP_EXIT")!);
        }
        return 2;
    }
}
""",
        encoding="utf-8",
    )
    publish = tmp_path / "candidate-publish"
    subprocess.run(
        [
            dotnet,
            "publish",
            str(project_file),
            "--configuration",
            "Release",
            "--runtime",
            "win-x64",
            "--self-contained",
            "false",
            "--output",
            str(publish),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    candidate = publish / "candidate.exe"
    assert candidate.is_file()
    old = candidate.read_bytes()
    if setup_exit != 0:
        target.write_bytes(old)
    checksum = tmp_path / "candidate.sha256"
    checksum.write_text(
        hashlib.sha256(old).hexdigest() + "  gsv-windows-x86_64.exe\n", encoding="ascii"
    )
    compatibility_log = tmp_path / "compatibility.log"
    fake_tools = tmp_path / "tools"
    fake_tools.mkdir()
    (fake_tools / "codex.cmd").write_text("@exit /b 0\n", encoding="ascii")
    environment = os.environ.copy()
    environment.update(
        {
            "GSV_TEST_ARTIFACT": str(candidate),
            "GSV_TEST_CHECKSUM": str(checksum),
            "GSV_TEST_DOWNLOAD_LOG": str(tmp_path / "downloads.log"),
            "GSV_TEST_INSTALLER": str(ROOT / "scripts/install.ps1"),
            "GSV_BIN_DIR": str(install_dir),
            "GSV_TEST_COMPAT": str(compatibility_log),
            "GSV_TEST_SETUP_EXIT": str(setup_exit),
            "PATH": f"{fake_tools}{os.pathsep}{environment['PATH']}",
        }
    )

    result = subprocess.run(
        _powershell_download_command(tmp_path),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )

    assert result.returncode == setup_exit
    assert target.read_bytes() == old, result.stderr
    if setup_exit == 0:
        assert not compatibility_log.exists()
        assert not list(install_dir.glob(".gsv.*"))
    elif setup_exit == 4:
        assert not compatibility_log.exists()
        assert len(list(install_dir.glob(".gsv.previous.*"))) == 1
        assert "Bridge needs repair" in result.stderr
        assert "The Bridge is ready" not in result.stdout
    else:
        assert compatibility_log.read_text(encoding="utf-8").splitlines() == ["rollback-check"]
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


def test_artifact_scan_reads_deflated_wheel_members(tmp_path: Path) -> None:
    artifact = tmp_path / "gsv-test.whl"
    canary = b"-----BEGIN " + b"PRIVATE KEY-----"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("continuity_kernel/hidden.py", canary * 8)
    assert canary not in artifact.read_bytes()

    findings, scanned = privacy_check.scan_artifact_path(
        artifact,
        privacy_check.PATTERNS,
        root=tmp_path,
    )

    assert scanned == 2
    assert (
        privacy_check.Finding(
            "gsv-test.whl!continuity_kernel/hidden.py",
            "artifact-member",
        )
        in findings
    )


def test_artifact_scan_reads_compressed_sdist_members(tmp_path: Path) -> None:
    artifact = tmp_path / "gsv-test.tar.gz"
    canary = b"-----BEGIN " + b"PRIVATE KEY-----"
    content = canary * 8
    info = tarfile.TarInfo("gsv-test/hidden.py")
    info.size = len(content)
    with tarfile.open(artifact, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(content))
    assert canary not in artifact.read_bytes()

    findings, scanned = privacy_check.scan_artifact_path(
        artifact,
        privacy_check.PATTERNS,
        root=tmp_path,
    )

    assert scanned == 2
    assert (
        privacy_check.Finding(
            "gsv-test.tar.gz!gsv-test/hidden.py",
            "artifact-member",
        )
        in findings
    )


def test_artifact_scan_fails_closed_on_oversized_compressed_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "oversized.whl"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("continuity_kernel/large.py", b"x" * 512)
    assert artifact.stat().st_size < 256
    monkeypatch.setattr(privacy_check, "MAX_SCAN_BYTES", 256)

    findings, scanned = privacy_check.scan_artifact_path(
        artifact,
        privacy_check.PATTERNS,
        root=tmp_path,
    )

    assert scanned == 1
    assert findings == [
        privacy_check.Finding(
            "oversized.whl!continuity_kernel/large.py",
            "artifact-member-oversized",
        )
    ]


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
