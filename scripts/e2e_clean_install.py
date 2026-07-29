#!/usr/bin/env python3
"""Exercise the released binary through an isolated Codex installation and two sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_IS_WINDOWS = os.name == "nt"
_EMPTY_REVISION = hashlib.sha256(b"").hexdigest()


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
            "Seld refuses redirects during the clean-install proof",
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
        raise RuntimeError("clean-install proof received an invalid Bridge URL") from exc
    if (
        target.scheme != "http"
        or target.hostname != "127.0.0.1"
        or port is None
        or port < 1
        or target.username is not None
        or target.password is not None
        or target.fragment
    ):
        raise RuntimeError("clean-install proof accepts only http://127.0.0.1:<port> URLs")
    return build_opener(ProxyHandler({}), _RejectRedirects()).open(request, timeout=timeout)


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
    root = root.resolve()
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
        raise RuntimeError("installer did not place the Seld executable")
    # The first setup used the explicit isolated vault. All later bare commands
    # must prove the persisted configuration path rather than an env override.
    environment.pop("GSV_VAULT", None)
    installed_version = _run([str(installed), "--version"], environment).stdout.strip()
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()

    status = _cli(installed, environment, ["status"])
    bridge_status = _cli(installed, environment, ["bridge", "status"])
    if not bridge_status["running"] or not bridge_status["identity_verified"]:
        raise RuntimeError("installer did not leave a verified Bridge running")
    bridge_state = json.loads((data / "bridge-state.json").read_text(encoding="utf-8"))
    _verify_bridge_http(
        bridge_status["url"],
        bridge_state["token"],
        status["vault_id"],
        vault_path=vault,
    )
    control_event_ids: tuple[str, str] | None = None
    if not _IS_WINDOWS:
        control_event_ids = _verify_installed_control_round_trip(
            installed,
            environment,
            bridge_status["url"],
            bridge_state["token"],
        )
        status = _cli(installed, environment, ["status"])
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
        upgraded_bridge["url"],
        upgraded_state["token"],
        upgraded_status["vault_id"],
        vault_path=vault,
    )
    if control_event_ids is not None:
        _verify_archived_control_bridge(
            upgraded_bridge["url"],
            upgraded_state["token"],
            control_event_ids,
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
        raise RuntimeError("actual ChatGPT plugin list does not contain Seld")

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
    orphan_bytes = b"partial interrupted write"
    orphan.write_bytes(orphan_bytes)
    doctor_run = _run(
        [str(installed), "--json", "doctor", "--repair"],
        environment,
        check=False,
    )
    doctor_payload = json.loads(doctor_run.stdout)
    doctor = doctor_payload.get("result")
    expected_orphan = "tasks/.fresh-session-proof.md.tmp-crash"
    issues = doctor.get("issues") if isinstance(doctor, dict) else None
    orphan_issue = (
        next(
            (
                issue
                for issue in issues
                if isinstance(issue, dict)
                and issue.get("code") == "orphan-temp"
                and issue.get("path") == expected_orphan
            ),
            None,
        )
        if isinstance(issues, list)
        else None
    )
    if (
        doctor_run.returncode != 3
        or doctor_payload.get("ok") is not False
        or not isinstance(doctor, dict)
        or doctor.get("healthy") is not False
        or not isinstance(orphan_issue, dict)
        or orphan_issue.get("repairable") is not False
        or orphan.read_bytes() != orphan_bytes
    ):
        raise RuntimeError("doctor did not preserve the unowned crash-looking file safely")
    orphan.unlink()
    recovered_doctor = _cli(installed, environment, ["doctor", "--repair"])
    if recovered_doctor["healthy"] is not True:
        raise RuntimeError("doctor did not recover after exact owner cleanup")

    source_digest_at_backup = _cli(installed, environment, ["status"])["digest"]
    backup = _cli(installed, environment, ["backup", "create"])
    config_path = config / "config.json"
    source_config = config_path.read_bytes()
    config_path.write_bytes(b"{deliberately broken for restore proof")
    verified_backup = _cli(installed, environment, ["backup", "verify", backup["backup"]])
    restored = _cli(
        installed,
        environment,
        ["backup", "restore", backup["backup"], str(restore)],
    )
    if (
        verified_backup.get("valid") is not True
        or config_path.read_bytes() != b"{deliberately broken for restore proof"
    ):
        raise RuntimeError("config-independent backup verification or restore mutated config")
    restored_status = _cli(installed, environment, ["--vault", str(restore), "status"])
    if (
        restored["digest"] != restored_status["digest"]
        or source_digest_at_backup != restored_status["digest"]
        or status["vault_id"] != restored_status["vault_id"]
    ):
        raise RuntimeError("backup restore is not logically equivalent")
    if (
        restored.get("configuration_changed") is not False
        or restored.get("configuration_matches_target") != "unknown"
        or restored.get("activation_required") is not True
        or restored.get("activation_commands")
        != ["gsv bridge stop", f"gsv --vault {restore} setup"]
    ):
        raise RuntimeError("restore did not report the deliberate activation contract")
    config_path.write_bytes(source_config)
    if Path(_cli(installed, environment, ["status"])["vault"]) != vault:
        raise RuntimeError("restore changed the configured vault before activation")

    source_bridge_pid = int(bridge_state["pid"])
    stopped_for_restore = _cli(installed, environment, ["bridge", "stop"])
    if (
        not stopped_for_restore["stopped"]
        or (data / "bridge-state.json").exists()
        or not _wait_for_process_exit(source_bridge_pid, timeout=8.0)
    ):
        raise RuntimeError("restore activation did not stop the verified source Bridge")
    activated = _cli(
        installed,
        environment,
        ["--vault", str(restore), "setup", "--no-browser"],
    )
    if activated.get("setup_complete") is not True:
        raise RuntimeError("restored-vault setup did not complete")
    activated_status = _cli(installed, environment, ["status"])
    activated_bridge = _cli(installed, environment, ["bridge", "status"])
    activated_state = json.loads((data / "bridge-state.json").read_text(encoding="utf-8"))
    if (
        Path(activated_status["vault"]) != restore
        or activated_status["digest"] != restored["digest"]
    ):
        raise RuntimeError("bare status did not bind to the restored vault after setup")
    if not activated_bridge["running"] or not activated_bridge["identity_verified"]:
        raise RuntimeError("restored-vault setup did not start a verified Bridge")
    _verify_bridge_http(
        activated_bridge["url"],
        activated_state["token"],
        activated_status["vault_id"],
        vault_path=restore,
    )
    activated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    activated_server = activated_manifest["mcpServers"]["gsv"]
    if activated_server.get("env", {}).get("GSV_VAULT") != str(restore):
        raise RuntimeError("Codex MCP integration did not rebind to the restored vault")
    status = activated_status
    bridge_status = activated_bridge
    bridge_state = activated_state

    native_result = False
    if native_codex:
        native_result = _native_codex_sessions(
            codex=codex,
            environment=environment,
            workspace=workspace,
            output=root / "native-output",
        )
        _require_native_codex(native_result)

    config_before = config_path.read_bytes()
    vault_digest_before = _cli(installed, environment, ["status"])["digest"]
    source_vault_before_legacy = _directory_digest(vault)
    restored_vault_before_legacy = _directory_digest(restore)
    marketplace_root = manifest_path.parents[2]
    integration_receipts = list(data.glob("codex-integration-*.json"))
    if len(integration_receipts) != 1:
        raise RuntimeError("installed Codex integration did not have exactly one ownership receipt")
    integration_receipt = integration_receipts[0]
    current_receipt = json.loads(integration_receipt.read_text(encoding="utf-8"))
    for retained_record in current_receipt.get("cleanup_pending", []):
        retained_path = marketplace_root.parent / retained_record.get("basename", "")
        if (
            retained_path.parent != marketplace_root.parent
            or not retained_path.is_dir()
            or _directory_manifest(retained_path) != retained_record.get("manifest")
        ):
            raise RuntimeError("could not isolate a clean synthetic v1 receipt fixture")
        shutil.rmtree(retained_path)
    legacy_receipt = {
        "codex_home": current_receipt["codex_home"],
        "format_version": 1,
        "marketplace_digest": current_receipt["marketplace_digest"],
        "marketplace_owned": True,
        "marketplace_root": current_receipt["marketplace_root"],
        "plugin_owned": True,
    }
    integration_receipt.write_text(
        json.dumps(legacy_receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (codex_home / "AGENTS.md").write_bytes(original_agents)
    shutil.rmtree(marketplace_root)
    if not integration_receipt.is_file() or marketplace_root.exists():
        raise RuntimeError("could not stage the valid-receipt legacy uninstall state")
    config_path.unlink()
    removed = _cli_expected_incomplete_cleanup(
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
        raise RuntimeError("Seld plugin remains after uninstall")
    if any(item.get("name") == "gsv-local" for item in after_marketplaces["marketplaces"]):
        raise RuntimeError("Seld marketplace remains after uninstall")
    if marketplace_root.exists() or not integration_receipt.is_file():
        raise RuntimeError("legacy uninstall did not isolate its recovery state")
    if (
        removed.get("cleanup_complete") is not False
        or removed.get("integration_removed") is not True
        or removed.get("marketplace_files_state") != "retained"
        or removed.get("recovery_retained") is not True
    ):
        raise RuntimeError("legacy removal scaffold was not retained as explicit recovery evidence")
    retained_paths = removed.get("retained_cleanup_paths")
    if not isinstance(retained_paths, list) or len(retained_paths) != 1:
        raise RuntimeError("legacy uninstall did not report its one exact recovery path")
    recovery_receipt = json.loads(integration_receipt.read_text(encoding="utf-8"))
    recovery_records = recovery_receipt.get("cleanup_pending")
    if (
        recovery_receipt.get("format_version") != 2
        or recovery_receipt.get("integration_active") is not False
        or not isinstance(recovery_records, list)
        or len(recovery_records) != 1
    ):
        raise RuntimeError("legacy uninstall did not migrate to one inactive recovery catalog")
    retained_path = Path(retained_paths[0])
    recovery_record = recovery_records[0]
    if (
        retained_path != marketplace_root.parent / recovery_record.get("basename", "")
        or not retained_path.is_dir()
        or _directory_manifest(retained_path) != recovery_record.get("manifest")
    ):
        raise RuntimeError("legacy recovery path does not match its receipt-bound manifest")
    if (
        _directory_digest(vault) != source_vault_before_legacy
        or _directory_digest(restore) != restored_vault_before_legacy
    ):
        raise RuntimeError("legacy Codex uninstall changed a user vault")

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
    _verify_bridge_http(
        restarted_status["url"],
        live_state["token"],
        status["vault_id"],
        vault_path=restore,
    )

    vault_files_before = _directory_digest(vault)
    restored_files_before = _directory_digest(restore)
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise RuntimeError("PowerShell is required for the Windows uninstall proof")
        uninstall_command = [
            shell,
            "-NoProfile",
            "-File",
            str(uninstaller),
            "--codex-home",
            str(codex_home),
        ]
    else:
        uninstall_command = [
            "/bin/sh",
            str(uninstaller),
            "--codex-home",
            str(codex_home),
        ]
    retained_gate = _run(uninstall_command, environment, check=False)
    if (
        retained_gate.returncode != 3
        or not installed.is_file()
        or "retained_cleanup_paths" not in retained_gate.stdout
    ):
        raise RuntimeError("release uninstaller did not gate executable removal on recovery")
    if live_receipt.exists() or not _wait_for_process_exit(live_pid, timeout=8.0):
        raise RuntimeError("recovery-gated uninstaller left the live Bridge running")
    if _directory_manifest(retained_path) != recovery_record["manifest"]:
        raise RuntimeError("synthetic recovery evidence changed before explicit retirement")
    shutil.rmtree(retained_path)
    _run(uninstall_command, environment)
    if installed.exists():
        raise RuntimeError("full uninstall left the Seld executable installed")
    if live_receipt.exists() or not _wait_for_process_exit(live_pid, timeout=8.0):
        raise RuntimeError("full uninstaller left the live Bridge running or its receipt behind")
    if (
        config_path.read_bytes() != config_before
        or _directory_digest(vault) != vault_files_before
        or _directory_digest(restore) != restored_files_before
    ):
        raise RuntimeError("full uninstall changed configuration or a user vault")

    return {
        "backup_restore": True,
        "backup_restore_activated": True,
        "backup_restore_without_readable_config": True,
        "backup_restore_preserved_both_vaults": True,
        "bridge_authenticated_loopback": True,
        "bridge_control_archive_visible_after_restart": control_event_ids is not None,
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
        "installed_control_round_trip": control_event_ids is not None,
        "installed_control_round_trip_skipped": _IS_WINDOWS,
        "native_codex_two_session": native_result,
        "native_auth_bridge_used": False,
        "real_codex_legacy_missing_manifest_uninstall": True,
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stale_write_rejected": stale.returncode == 2,
        "two_mcp_processes": True,
        "uninstaller_stopped_live_bridge": True,
        "uninstaller_retained_recovery_gate": True,
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
        required = (
            "uname",
            "mktemp",
            "cp",
            "awk",
            "mkdir",
            "install",
            "mv",
            "rm",
            "env",
            "tr",
        )
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


def _verify_bridge_http(
    url: str,
    token: str,
    vault_id: str,
    *,
    vault_path: Path | None = None,
) -> None:
    snapshot_url = f"{url.rstrip('/')}/api/v1/snapshot"
    try:
        _open_loopback(Request(snapshot_url, headers={"Accept": "application/json"}), timeout=5)
    except HTTPError as exc:
        if exc.code != 403:
            raise RuntimeError(f"unauthenticated Bridge returned {exc.code}, expected 403") from exc
    else:
        raise RuntimeError("unauthenticated Bridge exposed the vault snapshot")
    request = Request(
        snapshot_url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with _open_loopback(request, timeout=5) as response:
        snapshot = json.loads(response.read())
    if snapshot["status"]["vault_id"] != vault_id:
        raise RuntimeError("Bridge snapshot did not match the installed vault")
    if (
        vault_path is not None
        and Path(snapshot["status"]["vault"]).resolve() != vault_path.resolve()
    ):
        raise RuntimeError("Bridge snapshot did not match the expected vault path")
    with _open_loopback(url, timeout=5) as response:
        static = response.read()
    if b"<title>Seld</title>" not in static or b'id="view"' not in static:
        raise RuntimeError("Bridge static product surface was not bundled")


def _post_bridge_control(
    url: str,
    token: str,
    *,
    expected_revision: str,
    choice: str,
) -> dict[str, Any]:
    base = url.rstrip("/")
    request = Request(
        f"{base}/api/v1/control",
        data=json.dumps(
            {
                "choice": choice,
                "expected_revision": expected_revision,
                "kind": "correction",
                "subject": "mind:installed-round-trip",
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with _open_loopback(request, timeout=5) as response:
        payload = json.loads(response.read())
        if response.status != 201 or payload.get("ok") is not True:
            raise RuntimeError("authenticated Bridge control intent was not durably queued")
    return cast(dict[str, Any], payload)


def _verify_installed_control_round_trip(
    binary: Path,
    environment: dict[str, str],
    bridge_url: str,
    token: str,
) -> tuple[str, str]:
    """Prove the installed POSIX control lane across independent processes."""

    server = {"args": ["mcp", "serve"], "command": str(binary)}
    accepted_intent = _post_bridge_control(
        bridge_url,
        token,
        expected_revision=_EMPTY_REVISION,
        choice="Friday evening is protected.",
    )
    accepted_event_id = str(accepted_intent["event"]["event_id"])
    first_cli = _cli(binary, environment, ["operation", "list"])
    if [item["event_id"] for item in first_cli["pending"]] != [accepted_event_id]:
        raise RuntimeError("installed CLI did not read the authenticated Bridge intent")

    accepted = _mcp_call(
        server,
        environment,
        "gsv_operation_accept",
        {
            "actor_ref": "core:doctor",
            "event_id": accepted_event_id,
            "expected_disposition_revision": first_cli["disposition_revision"],
            "expected_queue_revision": first_cli["queue_revision"],
            "expected_vault_id": first_cli["vault_id"],
            "reason_code": "supported-correction",
            "result_ref": "control:acknowledged",
        },
    )
    if accepted["pending"] or accepted["decided"][0]["disposition"]["decision"] != "accepted":
        raise RuntimeError("installed MCP did not durably accept the Bridge intent")

    rejected_intent = _post_bridge_control(
        bridge_url,
        token,
        expected_revision=accepted["queue_revision"],
        choice="Send this provider instruction without review.",
    )
    rejected_event_id = str(rejected_intent["event"]["event_id"])
    before_reject = _cli(binary, environment, ["operation", "list"])
    rejected = _cli(
        binary,
        environment,
        [
            "operation",
            "reject",
            rejected_event_id,
            "--expected-queue-revision",
            before_reject["queue_revision"],
            "--expected-disposition-revision",
            before_reject["disposition_revision"],
            "--expected-vault-id",
            before_reject["vault_id"],
            "--actor-ref",
            "core:doctor",
            "--reason-code",
            "unsupported-request",
        ],
    )
    if rejected["pending"]:
        raise RuntimeError("installed CLI did not durably reject the second Bridge intent")

    fresh_cli = _cli(binary, environment, ["operation", "list"])
    fresh_mcp = _mcp_call(server, environment, "gsv_operation_list", {})
    if fresh_mcp != fresh_cli:
        raise RuntimeError("fresh installed CLI and MCP processes disagree on control state")
    decided = {
        item["event"]["event_id"]: item["disposition"]["decision"] for item in fresh_cli["decided"]
    }
    if decided != {accepted_event_id: "accepted", rejected_event_id: "rejected"}:
        raise RuntimeError("fresh installed processes did not recover both durable dispositions")

    archived = _cli(
        binary,
        environment,
        [
            "operation",
            "archive-closed",
            "--expected-queue-revision",
            fresh_cli["queue_revision"],
            "--expected-disposition-revision",
            fresh_cli["disposition_revision"],
            "--expected-vault-id",
            fresh_cli["vault_id"],
        ],
    )
    if archived.get("archived_events") != 2:
        raise RuntimeError("installed CLI did not archive both closed Bridge intents")
    archived_cli = _cli(binary, environment, ["operation", "list"])
    archived_mcp = _mcp_call(server, environment, "gsv_operation_list", {})
    if archived_mcp != archived_cli or archived_cli["pending"] or archived_cli["decided"]:
        raise RuntimeError("fresh installed processes did not recover the archived control state")
    return accepted_event_id, rejected_event_id


def _verify_archived_control_bridge(
    url: str,
    token: str,
    event_ids: tuple[str, str],
) -> None:
    request = Request(
        f"{url.rstrip('/')}/api/v1/snapshot",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with _open_loopback(request, timeout=5) as response:
        snapshot = json.loads(response.read())
    controls = snapshot["controls"]
    history = {
        item["event"]["event_id"]: (item["status"], item["archived"])
        for item in controls["history"]
    }
    expected = {event_ids[0]: ("accepted", True), event_ids[1]: ("rejected", True)}
    if (
        controls.get("pending") != 0
        or controls.get("decided") != 0
        or controls.get("archived_decided") != 2
        or history != expected
    ):
        raise RuntimeError("restarted Bridge did not recover both archived dispositions")


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
        "Use the Seld MCP tool gsv_task_create to create task id native-codex-proof, "
        "title Native Codex proof, outcome A separate native Codex session recovers this state, "
        "status doing, next actor agent, and next action Read this in a fresh Codex session. "
        "After the tool succeeds, reply exactly NATIVE_CREATED."
    )
    second_prompt = (
        "Use the Seld MCP tool gsv_task_show to read task native-codex-proof. "
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
        raise RuntimeError("fresh native ChatGPT session did not recover synthetic Seld state")


def _cli(binary: Path, environment: dict[str, str], arguments: list[str]) -> dict[str, Any]:
    result = _run([str(binary), "--json", *arguments], environment)
    payload = json.loads(result.stdout)
    if not payload.get("ok"):
        raise RuntimeError(f"Seld command failed: {arguments}")
    return cast(dict[str, Any], payload["result"])


def _cli_expected_incomplete_cleanup(
    binary: Path,
    environment: dict[str, str],
    arguments: list[str],
) -> dict[str, Any]:
    result = _run([str(binary), "--json", *arguments], environment, check=False)
    if result.returncode != 3:
        raise RuntimeError(
            f"Seld incomplete-cleanup command returned {result.returncode}, expected 3: {arguments}"
        )
    payload = json.loads(result.stdout)
    cleanup = payload.get("result")
    if payload.get("ok") is not False or not isinstance(cleanup, dict):
        raise RuntimeError(f"Seld incomplete-cleanup JSON was not fail-closed: {arguments}")
    if cleanup.get("cleanup_complete") is not False:
        raise RuntimeError(f"Seld incomplete-cleanup result claimed completion: {arguments}")
    return cast(dict[str, Any], cleanup)


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


def _directory_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"synthetic marketplace contains a symbolic link: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            manifest[relative] = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            manifest[relative] = f"file:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        else:
            raise RuntimeError(f"synthetic marketplace contains a special file: {relative}")
    return manifest


if __name__ == "__main__":
    raise SystemExit(main())
