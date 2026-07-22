#!/usr/bin/env python3
"""Build and smoke-test one self-contained GSV executable."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast
from urllib.request import Request, urlopen


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
    process = subprocess.Popen(
        [str(binary), "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
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
        home = root / "home"
        config = root / "config"
        data = root / "data"
        temporary = root / "tmp"
        vault = root / "vault"
        for path in (home, config, data, temporary):
            path.mkdir()
        environment = os.environ.copy()
        environment.update(
            {
                "APPDATA": str(config),
                "GSV_CONFIG_DIR": str(config),
                "GSV_DATA_DIR": str(data),
                "GSV_VAULT": str(vault),
                "HOME": str(home),
                "LOCALAPPDATA": str(data),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "TMPDIR": str(temporary),
                "USERPROFILE": str(home),
            }
        )
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
            with urlopen(url, timeout=5) as response:
                root_page = response.read()
            with urlopen(f"{url.rstrip('/')}/static/bridge.css", timeout=5) as response:
                stylesheet = response.read()
            snapshot_request = Request(
                f"{url.rstrip('/')}/api/v1/snapshot",
                headers={"Authorization": f"Bearer {state['token']}"},
            )
            with urlopen(snapshot_request, timeout=5) as response:
                snapshot = json.loads(response.read())
            if b"The agent is not the thread" not in root_page:
                raise RuntimeError("frozen Bridge root page was not bundled")
            if b".connection-notice" not in stylesheet:
                raise RuntimeError("frozen Bridge stylesheet was not bundled")
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


if __name__ == "__main__":
    raise SystemExit(main())
