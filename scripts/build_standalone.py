#!/usr/bin/env python3
"""Build and smoke-test one self-contained Continuity executable."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument(
        "--asset-name", default="continuity.exe" if os.name == "nt" else "continuity"
    )
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

    internal_name = "continuity"
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


if __name__ == "__main__":
    raise SystemExit(main())
