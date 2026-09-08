#!/usr/bin/env python3
"""Manage the opt-in per-user launchd watcher for Pulse source events.

This is deliberately separate from Seld's mechanical sense-sweep scheduler and
from the thirty-minute Pulse automation.  It only writes or removes its own
``ai.seld.pulse.events`` LaunchAgent when the operator explicitly asks it to.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LABEL = "ai.seld.pulse.events"
JOB_MARKER = "SELD_PULSE_EVENTS_JOB"
JOB_MARKER_VALUE = "1"
LAUNCHCTL = Path("/bin/launchctl")
SERVICE_NOT_FOUND = 113
POLL_SECONDS = "60"
MAX_LAUNCHCTL_OUTPUT = 64 * 1024
STANDARD_PATHS = (
    Path.home() / ".local/bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/sbin"),
    Path("/sbin"),
)


class UsageError(Exception):
    """A command cannot safely manage the event watcher."""


@dataclass(frozen=True)
class LaunchctlResult:
    returncode: int
    stdout: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install, remove, or inspect the opt-in Pulse event watcher."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    install = commands.add_parser("install", help="Install and load the Pulse event watcher.")
    install.add_argument(
        "--gsv",
        type=Path,
        default=Path.home() / ".local/bin/gsv",
        help="absolute path to the installed gsv executable (default: ~/.local/bin/gsv)",
    )
    install.add_argument("--vault", required=True, type=Path)
    install.add_argument(
        "--source",
        action="append",
        required=True,
        help="source watched by this job; repeat for each selected source",
    )
    commands.add_parser("remove", help="Unload and remove this script's event watcher.")
    commands.add_parser("status", help="Show this script's local job and launchd state.")
    args = parser.parse_args()

    try:
        _require_macos()
        if args.command == "install":
            install_job(args.gsv, args.vault, args.source)
        elif args.command == "remove":
            remove_job()
        else:
            show_status()
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: launchd operation failed: {exc}", file=sys.stderr)
        return 2
    return 0


def install_job(gsv_value: Path, vault_value: Path, source_values: list[str]) -> None:
    gsv = _installed_executable(gsv_value)
    vault = _vault(vault_value)
    sources = _sources(source_values)
    plist_path = _plist_path()
    stdout_path, stderr_path = _log_paths()
    command = _command(gsv, vault, sources)
    expected = _plist(command, stdout_path=stdout_path, stderr_path=stderr_path)
    encoded = plistlib.dumps(expected, fmt=plistlib.FMT_XML, sort_keys=True)

    existing = _read_plist(plist_path)
    if existing is not None and existing != encoded:
        if _is_owned_job(_parse_plist(existing)):
            raise UsageError(f"refusing to overwrite mismatched existing owned job at {plist_path}")
        raise UsageError(f"refusing to overwrite non-Pulse plist at {plist_path}")

    loaded, loaded_output = _loaded()
    if loaded:
        if existing == encoded and _loaded_job_matches(loaded_output, command):
            print(json.dumps(_status_payload("installed", True, plist_path), sort_keys=True))
            return
        raise UsageError("the Pulse event label is already loaded with a mismatched job")

    _private_logs()
    created = existing is None
    if created:
        _create_plist(plist_path, encoded)
    result = _launchctl("bootstrap", _domain(), str(plist_path))
    if result.returncode:
        if created:
            _unlink_exact(plist_path, encoded)
        raise UsageError("launchctl could not load the Pulse event watcher")
    loaded, loaded_output = _loaded()
    if not loaded or not _loaded_job_matches(loaded_output, command):
        raise UsageError("launchctl loaded the Pulse event label with unexpected provenance")
    print(json.dumps(_status_payload("installed", True, plist_path), sort_keys=True))


def remove_job() -> None:
    plist_path = _plist_path()
    encoded = _read_plist(plist_path)
    loaded, loaded_output = _loaded()
    if encoded is None:
        if loaded:
            raise UsageError("the loaded Pulse event label has no owned plist; it was preserved")
        print(json.dumps(_status_payload("absent", False, plist_path), sort_keys=True))
        return
    payload = _parse_plist(encoded)
    if not _is_owned_job(payload):
        raise UsageError(f"refusing to remove non-Pulse plist at {plist_path}")
    command = tuple(payload["ProgramArguments"])
    if loaded:
        if not _loaded_job_matches(loaded_output, command):
            raise UsageError("the loaded Pulse event label does not match its owned plist")
        result = _launchctl("bootout", f"{_domain()}/{LABEL}")
        if result.returncode:
            raise UsageError("launchctl could not unload the Pulse event watcher")
        still_loaded, _ = _loaded()
        if still_loaded:
            raise UsageError("launchctl could not prove the Pulse event watcher stopped")
    _unlink_exact(plist_path, encoded)
    print(json.dumps(_status_payload("removed", False, plist_path), sort_keys=True))


def show_status() -> None:
    plist_path = _plist_path()
    encoded = _read_plist(plist_path)
    if encoded is None:
        plist_state = "absent"
        command: tuple[str, ...] | None = None
    else:
        try:
            payload = _parse_plist(encoded)
        except UsageError:
            plist_state = "invalid"
            command = None
        else:
            plist_state = "owned" if _is_owned_job(payload) else "foreign"
            values = payload.get("ProgramArguments")
            command = tuple(values) if isinstance(values, list) else None
    loaded, loaded_output = _loaded()
    loaded_state = "absent"
    if loaded:
        loaded_state = (
            "owned" if command and _loaded_job_matches(loaded_output, command) else "foreign"
        )
    print(
        json.dumps(
            {
                "label": LABEL,
                "launchd": loaded_state,
                "plist": str(plist_path),
                "plist_state": plist_state,
            },
            sort_keys=True,
        )
    )


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise UsageError("the Pulse event watcher requires macOS launchd")
    if not LAUNCHCTL.is_file() or not os.access(LAUNCHCTL, os.X_OK):
        raise UsageError("launchctl is not available at /bin/launchctl")


def _installed_executable(value: Path) -> Path:
    supplied = value.expanduser()
    if not supplied.is_absolute():
        raise UsageError("--gsv must be an absolute installed executable path")
    try:
        executable = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UsageError("--gsv does not resolve to an installed executable") from exc
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise UsageError("--gsv must name an executable file")
    return executable


def _vault(value: Path) -> Path:
    supplied = value.expanduser()
    if not supplied.is_absolute():
        raise UsageError("--vault must be an absolute path")
    try:
        vault = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UsageError("--vault does not exist") from exc
    if not vault.is_dir():
        raise UsageError("--vault must name a directory")
    return vault


def _sources(values: list[str]) -> tuple[str, ...]:
    if not values:
        raise UsageError("at least one --source is required")
    sources = tuple(value.strip() for value in values)
    if any(
        not value or len(value) > 80 or re.fullmatch(r"[a-z][a-z0-9_]*", value) is None
        for value in sources
    ):
        raise UsageError("each --source must be a lower-case source identifier")
    if len(set(sources)) != len(sources):
        raise UsageError("each --source may appear only once")
    return sources


def _command(gsv: Path, vault: Path, sources: tuple[str, ...]) -> tuple[str, ...]:
    arguments = [
        str(gsv),
        "--vault",
        str(vault),
        "--json",
        "pulse",
        "watch",
        "--poll-seconds",
        POLL_SECONDS,
    ]
    for source in sources:
        arguments.extend(("--source", source))
    return tuple(arguments)


def _plist(command: tuple[str, ...], *, stdout_path: Path, stderr_path: Path) -> dict[str, Any]:
    return {
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            JOB_MARKER: JOB_MARKER_VALUE,
            "PATH": ":".join(str(path) for path in STANDARD_PATHS),
        },
        "KeepAlive": True,
        "Label": LABEL,
        # This user-requested source watcher must make progress under system load.
        # Standard retains launchd light limits without the Background QoS clamp.
        "ProcessType": "Standard",
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "StandardErrorPath": str(stderr_path),
        "StandardOutPath": str(stdout_path),
        "ThrottleInterval": 10,
    }


def _plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _log_paths() -> tuple[Path, Path]:
    directory = Path.home() / ".local/state/seld/pulse-events"
    return directory / "stdout.log", directory / "stderr.log"


def _private_logs() -> tuple[Path, Path]:
    stdout_path, stderr_path = _log_paths()
    directory = stdout_path.parent
    if directory.is_symlink():
        raise UsageError(f"refusing to use symlinked Pulse log directory: {directory}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir():
        raise UsageError(f"Pulse log path is not a directory: {directory}")
    directory.chmod(0o700)
    for path in (stdout_path, stderr_path):
        if path.is_symlink():
            raise UsageError(f"refusing to use symlinked Pulse log file: {path}")
        path.touch(exist_ok=True)
        path.chmod(0o600)
    return stdout_path, stderr_path


def _read_plist(path: Path) -> bytes | None:
    if path.is_symlink():
        raise UsageError(f"refusing to use symlinked Pulse plist: {path}")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _parse_plist(encoded: bytes) -> dict[str, Any]:
    try:
        payload = plistlib.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise UsageError("existing Pulse plist is invalid") from exc
    if not isinstance(payload, dict):
        raise UsageError("existing Pulse plist has an invalid shape")
    return payload


def _is_owned_job(payload: dict[str, Any]) -> bool:
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    if (
        payload.get("Label") != LABEL
        or not isinstance(environment, dict)
        or environment.get(JOB_MARKER) != JOB_MARKER_VALUE
        or not isinstance(arguments, list)
        or not all(isinstance(value, str) and value for value in arguments)
    ):
        return False
    command = tuple(arguments)
    if (
        len(command) < 10
        or command[1] != "--vault"
        or not Path(command[2]).is_absolute()
        or command[3:6] != ("--json", "pulse", "watch")
        or command[6:8] != ("--poll-seconds", POLL_SECONDS)
    ):
        return False
    source_arguments = command[8:]
    if len(source_arguments) < 2 or len(source_arguments) % 2:
        return False
    return all(
        source_arguments[index] == "--source"
        and re.fullmatch(r"[a-z][a-z0-9_]*", source_arguments[index + 1]) is not None
        for index in range(0, len(source_arguments), 2)
    )


def _create_plist(path: Path, encoded: bytes) -> None:
    if path.parent.is_symlink():
        raise UsageError(f"refusing to use symlinked LaunchAgents directory: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise UsageError(f"Pulse plist appeared while preparing install: {path}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _unlink_exact(path: Path, expected: bytes) -> None:
    current = _read_plist(path)
    if current != expected:
        raise UsageError(f"Pulse plist changed outside this command and was preserved: {path}")
    path.unlink()


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*arguments: str) -> LaunchctlResult:
    completed = subprocess.run(
        (str(LAUNCHCTL), *arguments),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    return LaunchctlResult(
        completed.returncode,
        completed.stdout[:MAX_LAUNCHCTL_OUTPUT].decode("utf-8", errors="replace"),
    )


def _loaded() -> tuple[bool, str]:
    result = _launchctl("print", f"{_domain()}/{LABEL}")
    if result.returncode == 0:
        return True, result.stdout
    if result.returncode == SERVICE_NOT_FOUND:
        return False, ""
    raise UsageError("launchctl could not inspect the Pulse event watcher")


def _loaded_job_matches(output: str, command: tuple[str, ...]) -> bool:
    if len(output.encode("utf-8")) > MAX_LAUNCHCTL_OUTPUT:
        return False
    marker = re.compile(rf"(?m)^\s*{re.escape(JOB_MARKER)}\s*(?:=>|=)\s*{JOB_MARKER_VALUE}\s*$")
    if marker.search(output) is None:
        return False
    block = re.search(r"(?ms)^\s*arguments\s*=\s*\{\s*\n(.*?)^\s*\}\s*$", output)
    if block is None:
        return False
    observed: list[str] = []
    for raw_line in block.group(1).splitlines():
        value = raw_line.strip()
        if value:
            indexed = re.fullmatch(r"\d+\s*(?:=>|=)\s*(.*)", value)
            observed.append(indexed.group(1) if indexed is not None else value)
    return tuple(observed) == command


def _status_payload(state: str, loaded: bool, plist_path: Path) -> dict[str, object]:
    return {
        "label": LABEL,
        "loaded": loaded,
        "plist": str(plist_path),
        "state": state,
    }


if __name__ == "__main__":
    raise SystemExit(main())
