#!/usr/bin/env python3
"""Build and smoke-test one self-contained Seld executable."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


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
        del message, new_url
        raise HTTPError(
            request.full_url,
            code,
            "Seld refuses redirects during the frozen Bridge smoke test",
            headers,
            file_pointer,
        )


def _open_loopback(request: str | Request, *, timeout: float) -> Any:
    """Open one exact IPv4 loopback URL without consulting ambient proxies."""

    url = request.full_url if isinstance(request, Request) else request
    target = urlsplit(url)
    try:
        port = target.port
    except ValueError as exc:
        raise RuntimeError("Bridge smoke test received an invalid loopback URL") from exc
    if (
        target.scheme != "http"
        or target.hostname != "127.0.0.1"
        or port is None
        or port < 1
        or target.username is not None
        or target.password is not None
        or target.fragment
    ):
        raise RuntimeError("Bridge smoke test accepts only http://127.0.0.1:<port> URLs")
    return build_opener(ProxyHandler({}), _RejectRedirects()).open(request, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--asset-name", default="gsv.exe" if os.name == "nt" else "gsv")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output = args.output.expanduser().resolve()
    work = root / "build/pyinstaller"
    specs = root / "build/specs"
    shutil.rmtree(work, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    specs.mkdir(parents=True, exist_ok=True)

    try:
        pyinstaller = importlib.import_module("PyInstaller.__main__")
    except ImportError as exc:
        raise SystemExit("PyInstaller is missing; run `uv sync --extra release`.") from exc

    internal_name = "gsv"
    pyinstaller.run(
        [
            "--clean",
            "--noconfirm",
            "--onefile",
            "--name",
            internal_name,
            "--distpath",
            str(output),
            "--workpath",
            str(work),
            "--specpath",
            str(specs),
            "--paths",
            str(root / "src"),
            "--collect-data",
            "continuity_kernel",
            str(root / "scripts/standalone_entry.py"),
        ]
    )
    built = output / (f"{internal_name}.exe" if os.name == "nt" else internal_name)
    target = output / args.asset_name
    if built != target:
        target.unlink(missing_ok=True)
        built.replace(target)
    if os.name != "nt":
        target.chmod(0o755)

    privacy = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/privacy_check.py"),
            str(root),
            "--no-history",
            "--artifact",
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if privacy.returncode != 0:
        raise RuntimeError(f"frozen artifact privacy scan failed: {privacy.stdout[:2000]}")
    privacy_report = json.loads(privacy.stdout.splitlines()[-1])

    version = subprocess.run(
        [str(target), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    handshake = _mcp_handshake(target)
    bridge_smoke = _bridge_static_smoke(target)
    result = handshake.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("frozen MCP smoke test omitted its result")
    server_info = result.get("serverInfo")
    if not isinstance(server_info, dict) or not isinstance(server_info.get("name"), str):
        raise RuntimeError("frozen MCP smoke test omitted server information")
    print(
        json.dumps(
            {
                "artifact": str(target),
                "artifact_privacy_scanned": privacy_report["scanned"],
                **bridge_smoke,
                "mcp_server": server_info["name"],
                "size": target.stat().st_size,
                "version": version,
            },
            sort_keys=True,
        )
    )
    return 0


def _mcp_handshake(binary: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="gsv-frozen-mcp-") as raw:
        root = Path(raw)
        vault = root / "vault"
        environment = _isolated_environment(root, vault)
        initialized = subprocess.run(
            [str(binary), "--json", "--vault", str(vault), "init"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=30,
        )
        if initialized.returncode != 0:
            raise RuntimeError(f"frozen MCP fixture setup failed: {initialized.stderr[:1000]}")
        process = subprocess.Popen(
            [str(binary), "mcp", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        stdout, stderr = process.communicate(request + "\n", timeout=30)
        if process.returncode != 0:
            raise RuntimeError(f"frozen MCP smoke test failed: {stderr[:1000]}")
        payload = json.loads(stdout.splitlines()[0])
        if not isinstance(payload, dict):
            raise RuntimeError("frozen MCP smoke test returned an invalid response")
        return cast(dict[str, Any], payload)


def _bridge_static_smoke(binary: Path) -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="gsv-frozen-bridge-") as raw:
        root = Path(raw)
        data = root / "data"
        vault = root / "vault"
        environment = _isolated_environment(root, vault)
        setup = subprocess.run(
            [
                str(binary),
                "--json",
                "--vault",
                str(vault),
                "setup",
                "--no-codex",
                "--no-browser",
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            timeout=30,
        )
        if setup.returncode != 0:
            raise RuntimeError(f"frozen Bridge setup failed: {setup.stderr[:1000]}")
        state_path = data / "bridge-state.json"
        if not state_path.is_file():
            raise RuntimeError("frozen Bridge setup omitted its state receipt")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        url = str(state["url"])
        try:
            with _open_loopback(url, timeout=5) as response:
                root_page = response.read()
            with _open_loopback(f"{url.rstrip('/')}/static/bridge.css", timeout=5) as response:
                stylesheet = response.read()
            with _open_loopback(
                f"{url.rstrip('/')}/static/fonts/nunito-var.woff2", timeout=5
            ) as response:
                font_content_type = response.headers.get_content_type()
                font_payload = response.read()
            snapshot_request = Request(
                f"{url.rstrip('/')}/api/v1/snapshot",
                headers={"Authorization": f"Bearer {state['token']}"},
            )
            with _open_loopback(snapshot_request, timeout=5) as response:
                snapshot = json.loads(response.read())
            if b"<title>Seld</title>" not in root_page or b'id="view"' not in root_page:
                raise RuntimeError("frozen Bridge root page was not bundled")
            if b".connection-notice" not in stylesheet:
                raise RuntimeError("frozen Bridge stylesheet was not bundled")
            if font_content_type != "font/woff2" or not font_payload.startswith(b"wOF2"):
                raise RuntimeError("frozen Bridge font was not bundled with deterministic MIME")
            if snapshot.get("status", {}).get("vault_id") != state.get("vault_id"):
                raise RuntimeError("frozen Bridge snapshot did not match its vault")
        finally:
            stopped = subprocess.run(
                [str(binary), "--json", "--vault", str(vault), "bridge", "stop"],
                check=False,
                capture_output=True,
                encoding="utf-8",
                env=environment,
                timeout=15,
            )
            if stopped.returncode != 0 or state_path.exists():
                raise RuntimeError(f"frozen Bridge cleanup failed: {stopped.stderr[:1000]}")
    return {"bridge_authenticated_snapshot": True, "bridge_static_assets": True}


def _isolated_environment(root: Path, vault: Path) -> dict[str, str]:
    home = root / "home"
    codex_home = root / "codex"
    config = root / "config"
    data = root / "data"
    temporary = root / "tmp"
    for path in (home, codex_home, config, data, temporary):
        path.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "APPDATA": str(config),
            "CODEX_HOME": str(codex_home),
            "GSV_CONFIG_DIR": str(config),
            "GSV_CODEX": str(root / "missing-codex"),
            "GSV_DATA_DIR": str(data),
            "GSV_VAULT": str(vault),
            "HOME": str(home),
            "LOCALAPPDATA": str(data),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
        }
    )
    return environment


if __name__ == "__main__":
    raise SystemExit(main())
