#!/usr/bin/env python3
"""Exercise the released binary through an isolated Codex installation and two sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_IS_WINDOWS = os.name == "nt"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--native-codex", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="gsv-clean-e2e-"))
    report: dict[str, Any] = {"isolated_execution": True}
    try:
        report.update(
            run_e2e(
                root,
                args.binary.resolve(),
                native_codex=args.native_codex,
            )
        )
        encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


def run_e2e(
    root: Path,
    binary: Path,
    *,
    native_codex: bool,
) -> dict[str, Any]:
    if not binary.is_file():
        raise RuntimeError(f"release binary does not exist: {binary}")
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("Codex CLI is required for the clean-install proof")

    home = root / "home"
    config = root / "config"
    data = root / "data"
    codex_home = root / "codex"
    vault = root / "vault"
    restore = root / "restored"
    install_bin = root / "bin"
    workspace = root / "workspace"
    temporary = root / "tmp"
    for path in (home, config, data, codex_home, workspace, temporary):
        path.mkdir(parents=True)
    original_agents = b"# Existing synthetic Codex instructions\n\nPreserve this byte-for-byte.\n"
    (codex_home / "AGENTS.md").write_bytes(original_agents)

    environment = _isolated_environment(
        root=root,
        home=home,
        config=config,
        data=data,
        codex_home=codex_home,
        vault=vault,
        install_bin=install_bin,
        binary=binary,
        codex=Path(codex),
    )
    installer = Path(__file__).resolve().parent / (
        "install.ps1" if os.name == "nt" else "install.sh"
    )
    uninstaller = Path(__file__).resolve().parent / (
        "uninstall.ps1" if os.name == "nt" else "uninstall.sh"
    )
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise RuntimeError("PowerShell is required for the Windows installer proof")
        install_command = [
            shell,
            "-NoProfile",
            "-File",
            str(installer),
            "--no-browser",
        ]
        installed = install_bin / "gsv.exe"
    else:
        install_command = ["/bin/sh", str(installer), "--no-browser"]
        installed = install_bin / "gsv"
    _run(install_command, environment)
    if not installed.is_file():
        raise RuntimeError("installer did not place the GSV executable")
    installed_version = _run([str(installed), "--version"], environment).stdout.strip()
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()

    status = _cli(installed, environment, ["status"])
    bridge_status = _cli(installed, environment, ["bridge", "status"])
    if not bridge_status["running"] or not bridge_status["identity_verified"]:
        raise RuntimeError("installer did not leave a verified Bridge running")
    bridge_state = json.loads((data / "bridge-state.json").read_text(encoding="utf-8"))
    _verify_bridge_http(bridge_status["url"], bridge_state["token"], status["vault_id"])
    initial_bridge_pid = int(bridge_state["pid"])
    initial_instance_id = str(bridge_state["instance_id"])
    digest_before_upgrade = str(status["digest"])

    _run(install_command, environment)
    upgraded_status = _cli(installed, environment, ["status"])
    upgraded_bridge = _cli(installed, environment, ["bridge", "status"])
    upgraded_state = json.loads((data / "bridge-state.json").read_text(encoding="utf-8"))
    if not upgraded_bridge["running"] or not upgraded_bridge["identity_verified"]:
        raise RuntimeError("reinstall did not leave a verified candidate Bridge running")
    if (
        not _wait_for_process_exit(initial_bridge_pid, timeout=8.0)
        or upgraded_state["instance_id"] == initial_instance_id
        or upgraded_status["digest"] != digest_before_upgrade
    ):
        raise RuntimeError(
            "live-Bridge upgrade did not replace the instance and preserve the vault"
        )
    _verify_bridge_http(
        upgraded_bridge["url"], upgraded_state["token"], upgraded_status["vault_id"]
    )
    status = upgraded_status
    bridge_status = upgraded_bridge
    bridge_state = upgraded_state
    codex_status = _cli(
        installed, environment, ["codex", "status", "--codex-home", str(codex_home)]
    )
    plugin_list = _json_command([codex, "plugin", "list", "--json"], environment)
    marketplace_list = _json_command(
        [codex, "plugin", "marketplace", "list", "--json"], environment
    )
    if not codex_status["plugin_installed"] or not codex_status["instructions_installed"]:
        raise RuntimeError("Codex integration did not report installed")
    if not any(item.get("pluginId") == "gsv@gsv-local" for item in plugin_list["installed"]):
        raise RuntimeError("actual Codex plugin list does not contain GSV")

    marketplace = next(
        item for item in marketplace_list["marketplaces"] if item.get("name") == "gsv-local"
    )
    manifest_path = Path(str(marketplace["root"])) / "plugins/gsv/.mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    server = manifest["mcpServers"]["gsv"]
    if Path(server["command"]).resolve() != installed.resolve() or server["args"] != [
        "mcp",
        "serve",
    ]:
        raise RuntimeError("frozen install generated an invalid MCP launch command")

    created = _mcp_call(
        server,
        environment,
        "gsv_task_create",
        {
            "id": "fresh-session-proof",
            "title": "Fresh session proof",
            "outcome": "A separate process recovers this synthetic state.",
            "status": "doing",
            "next_actor": "agent",
            "next_action": "Resume from a second process.",
        },
    )
    resumed = _mcp_call(
        server,
        environment,
        "gsv_task_show",
        {"id": "fresh-session-proof"},
    )
    if resumed["identifier"] != created["identifier"]:
        raise RuntimeError("fresh MCP process did not recover the created task")

    updated = _cli(
        installed,
        environment,
        [
            "task",
            "update",
            "fresh-session-proof",
            "--expected-revision",
            created["revision"],
            "--next-action",
            "Verified by the second process.",
        ],
    )
    stale = _run(
        [
            str(installed),
            "--json",
            "task",
            "update",
            "fresh-session-proof",
            "--expected-revision",
            created["revision"],
            "--next-action",
            "A stale write must fail.",
        ],
        environment,
        check=False,
    )
    if stale.returncode != 2 or "record changed" not in stale.stderr:
        raise RuntimeError("stale-write proof did not fail closed")

    orphan = vault / "tasks/.fresh-session-proof.md.tmp-crash"
    orphan.write_bytes(b"partial interrupted write")
    doctor = _cli(installed, environment, ["doctor", "--repair"])
    if not doctor["healthy"] or orphan.exists():
        raise RuntimeError("crash-recovery doctor did not remove the orphan safely")

    source_digest_at_backup = _cli(installed, environment, ["status"])["digest"]
    backup = _cli(installed, environment, ["backup", "create"])
    restored = _cli(
        installed,
        environment,
        ["backup", "restore", backup["backup"], str(restore)],
    )
    restored_status = _cli(installed, environment, ["--vault", str(restore), "status"])
    if (
        restored["digest"] != restored_status["digest"]
        or source_digest_at_backup != restored_status["digest"]
        or status["vault_id"] != restored_status["vault_id"]
    ):
        raise RuntimeError("backup restore is not logically equivalent")

    native_result = False
    if native_codex:
        native_result = _native_codex_sessions(
            codex=codex,
            environment=environment,
            workspace=workspace,
            output=root / "native-output",
        )
        _require_native_codex(native_result)

    config_path = config / "config.json"
    config_before = config_path.read_bytes()
    vault_digest_before = _cli(installed, environment, ["status"])["digest"]
    config_path.unlink()
    removed = _cli(
        installed,
        environment,
        ["codex", "uninstall", "--codex-home", str(codex_home)],
    )
    if (codex_home / "AGENTS.md").read_bytes() != original_agents:
        raise RuntimeError("uninstall did not restore existing Codex instructions byte-for-byte")
    if config_path.exists():
        raise RuntimeError("vault-free Codex uninstall unexpectedly recreated configuration")
    config_path.write_bytes(config_before)
    if _cli(installed, environment, ["status"])["digest"] != vault_digest_before:
        raise RuntimeError("uninstall changed the user vault")
    after_plugins = _json_command([codex, "plugin", "list", "--json"], environment)
    after_marketplaces = _json_command(
        [codex, "plugin", "marketplace", "list", "--json"], environment
    )
    if any(item.get("pluginId") == "gsv@gsv-local" for item in after_plugins["installed"]):
        raise RuntimeError("GSV plugin remains after uninstall")
    if any(item.get("name") == "gsv-local" for item in after_marketplaces["marketplaces"]):
        raise RuntimeError("GSV marketplace remains after uninstall")
    if manifest_path.parents[2].exists():
        raise RuntimeError("generated marketplace files remain after uninstall")

    stopped = _cli(installed, environment, ["bridge", "stop"])
    if not stopped["stopped"] or (data / "bridge-state.json").exists():
        raise RuntimeError("verified Bridge did not stop cleanly")

    restarted = _cli(installed, environment, ["bridge", "open", "--no-browser"])
    restarted_status = _cli(installed, environment, ["bridge", "status"])
    live_receipt = data / "bridge-state.json"
    if not restarted["started"] or not restarted_status["identity_verified"]:
        raise RuntimeError("Bridge did not restart for the live-uninstall proof")
    if not live_receipt.is_file():
        raise RuntimeError("restarted Bridge omitted its state receipt")
    live_state = json.loads(live_receipt.read_text(encoding="utf-8"))
    live_pid = int(live_state["pid"])
    if restarted_status.get("pid") != live_pid or not _process_alive(live_pid):
        raise RuntimeError("restarted Bridge receipt did not match its live process")
    _verify_bridge_http(restarted_status["url"], live_state["token"], status["vault_id"])

    vault_files_before = _directory_digest(vault)
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise RuntimeError("PowerShell is required for the Windows uninstall proof")
        _run(
            [shell, "-NoProfile", "-File", str(uninstaller), "--codex-home", str(codex_home)],
            environment,
        )
    else:
        _run(
            ["/bin/sh", str(uninstaller), "--codex-home", str(codex_home)],
            environment,
        )
    if installed.exists():
        raise RuntimeError("full uninstall left the GSV executable installed")
    if live_receipt.exists() or not _wait_for_process_exit(live_pid, timeout=8.0):
        raise RuntimeError("full uninstaller left the live Bridge running or its receipt behind")
    if config_path.read_bytes() != config_before or _directory_digest(vault) != vault_files_before:
        raise RuntimeError("full uninstall changed the user vault or configuration")

    return {
        "backup_restore": True,
        "bridge_authenticated_loopback": True,
        "bridge_direct_stop": True,
        "binary_sha256": binary_sha256,
        "binary_version": installed_version,
        "codex_cli_contract": True,
        "codex_uninstall_without_config": True,
        "crash_recovery": True,
        "frozen_manifest": True,
        "full_uninstall_removed_binary": True,
        "installer_without_python_uv_make": True,
        "installer_upgrade_restarted_bridge": True,
        "native_codex_two_session": native_result,
        "native_auth_bridge_used": False,
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stale_write_rejected": stale.returncode == 2,
        "two_mcp_processes": True,
        "uninstaller_stopped_live_bridge": True,
        "uninstall_preserved_config_and_vault": removed["user_data_preserved"],
        "updated_revision": updated["revision"],
        "vault_id": status["vault_id"],
    }


def _isolated_environment(
    *,
    root: Path,
    home: Path,
    config: Path,
    data: Path,
    codex_home: Path,
    vault: Path,
    install_bin: Path,
    binary: Path,
    codex: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    tool_bin = root / "tool-bin"
    tool_bin.mkdir()
    node = shutil.which("node")
    if os.name == "nt":
        runtime_paths = [str(codex.parent)]
        if node is not None:
            runtime_paths.append(str(Path(node).resolve().parent))
        runtime_paths.append(str(Path(os.environ["SYSTEMROOT"]) / "System32"))
        minimal_path = os.pathsep.join(dict.fromkeys(runtime_paths))
    else:
        required = ("uname", "mktemp", "cp", "awk", "mkdir", "install", "mv", "rm", "env")
        for name in required:
            source = shutil.which(name)
            if source is None:
                raise RuntimeError(f"required installer primitive is missing: {name}")
            (tool_bin / name).symlink_to(Path(source).resolve())
        if node is not None:
            (tool_bin / "node").symlink_to(Path(node).resolve())
        checksum = shutil.which("sha256sum") or shutil.which("shasum")
        if checksum is None:
            raise RuntimeError("no SHA-256 command is available")
        (tool_bin / Path(checksum).name).symlink_to(Path(checksum).resolve())
        (tool_bin / "codex").symlink_to(codex.resolve())
        minimal_path = str(tool_bin)
    environment.update(
        {
            "APPDATA": str(config),
            "CODEX_HOME": str(codex_home),
            "GSV_BINARY": str(binary),
            "GSV_BINARY_SHA256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "GSV_BIN_DIR": str(install_bin),
            "GSV_CONFIG_DIR": str(config),
            "GSV_DATA_DIR": str(data),
            "GSV_VAULT": str(vault),
            "HOME": str(home),
            "LOCALAPPDATA": str(data),
            "PATH": minimal_path,
            "TMPDIR": str(root / "tmp"),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
        }
    )
    return environment


def _mcp_call(
    server: dict[str, Any],
    base_environment: dict[str, str],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    environment = base_environment.copy()
    environment.update(server.get("env", {}))
    process = subprocess.Popen(
        [server["command"], *server["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ]
    stdout, stderr = process.communicate(
        "".join(json.dumps(item) + "\n" for item in requests), timeout=30
    )
    if process.returncode != 0:
        raise RuntimeError(f"MCP session failed: {stderr[:1000]}")
    responses = [json.loads(line) for line in stdout.splitlines()]
    payload = responses[-1]["result"]
    if payload.get("isError"):
        raise RuntimeError(payload["content"][0]["text"])
    return cast(dict[str, Any], payload["structuredContent"])


def _verify_bridge_http(url: str, token: str, vault_id: str) -> None:
    snapshot_url = f"{url.rstrip('/')}/api/v1/snapshot"
    try:
        urlopen(Request(snapshot_url, headers={"Accept": "application/json"}), timeout=5)
    except HTTPError as exc:
        if exc.code != 403:
            raise RuntimeError(f"unauthenticated Bridge returned {exc.code}, expected 403") from exc
    else:
        raise RuntimeError("unauthenticated Bridge exposed the vault snapshot")
    request = Request(
        snapshot_url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=5) as response:
        snapshot = json.loads(response.read())
    if snapshot["status"]["vault_id"] != vault_id:
        raise RuntimeError("Bridge snapshot did not match the installed vault")
    with urlopen(url, timeout=5) as response:
        static = response.read()
    if b"The agent is not the thread" not in static:
        raise RuntimeError("Bridge static product surface was not bundled")


def _native_codex_sessions(
    *,
    codex: str,
    environment: dict[str, str],
    workspace: Path,
    output: Path,
) -> bool:
    output.mkdir()
    first_message = output / "first.txt"
    second_message = output / "second.txt"
    first_prompt = (
        "Use the GSV MCP tool gsv_task_create to create task id native-codex-proof, "
        "title Native Codex proof, outcome A separate native Codex session recovers this state, "
        "status doing, next actor agent, and next action Read this in a fresh Codex session. "
        "After the tool succeeds, reply exactly NATIVE_CREATED."
    )
    second_prompt = (
        "Use the GSV MCP tool gsv_task_show to read task native-codex-proof. "
        "If its next action says Read this in a fresh Codex session, reply exactly NATIVE_RESUMED."
    )
    base = [codex, "exec", "--skip-git-repo-check", "-C", str(workspace), "--color", "never"]
    _run([*base, "-o", str(first_message), first_prompt], environment, timeout=300)
    _run([*base, "-o", str(second_message), second_prompt], environment, timeout=300)
    return (
        first_message.read_text(encoding="utf-8").strip() == "NATIVE_CREATED"
        and second_message.read_text(encoding="utf-8").strip() == "NATIVE_RESUMED"
    )


def _require_native_codex(result: bool) -> None:
    if not result:
        raise RuntimeError("fresh native Codex session did not recover synthetic GSV state")


def _cli(binary: Path, environment: dict[str, str], arguments: list[str]) -> dict[str, Any]:
    result = _run([str(binary), "--json", *arguments], environment)
    payload = json.loads(result.stdout)
    if not payload.get("ok"):
        raise RuntimeError(f"GSV command failed: {arguments}")
    return cast(dict[str, Any], payload["result"])


def _json_command(command: list[str], environment: dict[str, str]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_run(command, environment).stdout))


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
            f"command failed ({result.returncode}): {command[0]}\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
    return result


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    """Check a Win32 process handle without delivering a signal."""

    import ctypes

    loader = cast(Any, getattr(ctypes, "WinDLL", None))
    if loader is None:
        return False
    kernel32 = loader("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait_for_single_object.restype = ctypes.c_uint32
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = open_process(0x00100000, 0, pid)
    if not handle:
        return False
    try:
        return int(wait_for_single_object(handle, 0)) == 0x00000102
    finally:
        close_handle(handle)


def _wait_for_process_exit(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.1)
    return not _process_alive(pid)


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
