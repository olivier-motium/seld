"""The Bridge: a small, read-only loopback view of one GSV vault."""

from __future__ import annotations

import errno
import json
import mimetypes
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from hmac import compare_digest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import as_file, files
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from continuity_kernel import __version__
from continuity_kernel.atomic import atomic_write, exclusive_lock
from continuity_kernel.codex_integration import codex_status
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ContinuityError, SetupError, ValidationError
from continuity_kernel.records import (
    MAX_RECORD_BYTES,
    Entity,
    Record,
    Task,
    WorkThread,
    parse_entity,
    parse_task,
    parse_thread,
    record_dict,
)
from continuity_kernel.vault import Vault, doctor_dict

LOOPBACK_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 0
MAX_TARGET_LENGTH: Final = 4_096
MAX_STATIC_BYTES: Final = 4 * 1024 * 1024
STATE_VERSION: Final = 1
REPOSITORY_URL: Final = "https://github.com/olivier-motium/gsv"
METADATA_CACHE_SECONDS: Final = 30.0
_CREATE_NEW_PROCESS_GROUP: Final = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
_DETACHED_PROCESS: Final = int(getattr(subprocess, "DETACHED_PROCESS", 0))
_IS_WINDOWS = os.name == "nt"

_CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
_MIME_TYPES: Final = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


class _HealthOutcome(StrEnum):
    RESPONSE = "response"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _HealthProbe:
    outcome: _HealthOutcome
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class _RecordProjection:
    records: tuple[Record, ...]
    state: str
    issues: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "issues": [dict(issue) for issue in self.issues],
            "readable": len(self.records),
            "state": self.state,
            "unreadable": len(self.issues),
        }


@dataclass(frozen=True)
class _ObservedRecordFile:
    metadata: os.stat_result | None
    error: str | None = None

    def fingerprint(self) -> tuple[object, ...]:
        if self.metadata is None:
            return ("unreadable",)
        return (
            "observed",
            self.metadata.st_dev,
            self.metadata.st_ino,
            self.metadata.st_mode,
            self.metadata.st_size,
            self.metadata.st_mtime_ns,
            self.metadata.st_ctime_ns,
        )


_NEW_MIND_PROMPT: Final = (
    "Help me shape my GSV Mind. Read the installed GSV context first, then ask only the few "
    "questions needed to make MIND.md and NOW.md useful."
)
_NEW_HAND_PROMPT: Final = (
    "Start a new GSV hand. Read the installed GSV context and exact current records before "
    "deciding what deserves attention."
)
_TERMINAL_TASK_STATUSES: Final = frozenset({"done", "dropped"})


def bridge_snapshot(
    vault: Vault,
    *,
    doctor: dict[str, Any] | None = None,
    integration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the exact authored records needed by the read-only Bridge."""

    resolved_doctor = doctor if doctor is not None else _bridge_doctor(vault)
    resolved_integration = _codex_metadata() if integration is None else integration
    codex_ready = bool(
        resolved_integration.get("available") is True
        and resolved_integration.get("plugin_installed") is True
        and resolved_integration.get("instructions_installed") is True
    )
    task_projection = _project_record_section(
        vault,
        directory="tasks",
        parser=parse_task,
        sort_key=lambda item: (
            cast(Task, item).status,
            cast(Task, item).updated_at,
            cast(Task, item).identifier,
        ),
    )
    entity_projection = _project_record_section(
        vault,
        directory="entities",
        parser=parse_entity,
        sort_key=lambda item: (
            cast(Entity, item).entity_type,
            cast(Entity, item).title.casefold(),
            cast(Entity, item).identifier,
        ),
    )
    thread_projection = _project_record_section(
        vault,
        directory="threads",
        parser=parse_thread,
        sort_key=lambda item: (
            cast(WorkThread, item).status,
            cast(WorkThread, item).updated_at,
            cast(WorkThread, item).identifier,
        ),
    )
    projections = {
        "tasks": task_projection,
        "entities": entity_projection,
        "threads": thread_projection,
    }
    resolved_doctor = _merge_projection_doctor(resolved_doctor, projections)
    task_records = tuple(cast(Task, item) for item in task_projection.records)
    thread_records = tuple(cast(WorkThread, item) for item in thread_projection.records)
    entity_records = tuple(cast(Entity, item) for item in entity_projection.records)
    tasks = []
    for item in task_records:
        task = record_dict(item)
        if codex_ready and item.status not in _TERMINAL_TASK_STATUSES:
            task["codex_url"] = codex_deep_link(
                vault.root,
                f"Resume the GSV commitment `{item.identifier}`. Load its exact current record "
                "and revision before deciding or changing anything.",
            )
        tasks.append(task)
    threads = [record_dict(item) for item in thread_records]
    entities = [record_dict(item) for item in entity_records]
    mind = vault.read_document("MIND.md")
    now = vault.read_document("NOW.md")
    status = {
        **vault.identity(),
        "counts": {
            "tasks": len(task_records),
            "entities": len(entity_records),
            "threads": len(thread_records),
        },
    }
    return {
        "bridge": {
            "local": True,
            "read_only": True,
            "version": __version__,
        },
        "codex": {
            **resolved_integration,
            "ready": codex_ready,
            **(
                {
                    "new_hand_url": codex_deep_link(vault.root, _NEW_HAND_PROMPT),
                    **(
                        {"new_mind_url": codex_deep_link(vault.root, _NEW_MIND_PROMPT)}
                        if task_projection.state == "complete" and not task_records
                        else {}
                    ),
                }
                if codex_ready
                else {}
            ),
        },
        "doctor": resolved_doctor,
        "entities": entities,
        "mind": mind,
        "now": now,
        "projection": {
            "sections": {name: section.payload() for name, section in projections.items()}
        },
        "status": status,
        "tasks": tasks,
        "threads": threads,
    }


def _project_record_section(
    vault: Vault,
    *,
    directory: str,
    parser: Callable[[str], Record],
    sort_key: Callable[[Record], tuple[Any, ...]],
) -> _RecordProjection:
    root = vault.root / directory
    unavailable = _section_directory_issue(root, vault.root, directory)
    if unavailable is not None:
        return _RecordProjection((), "unavailable", (unavailable,))

    try:
        before = os.lstat(root)
        listed = _record_directory_snapshot(root)
    except OSError as exc:
        issue = _projection_issue(
            "unavailable-record-section",
            directory,
            f"could not enumerate the {directory} record section: {exc}",
        )
        return _RecordProjection((), "unavailable", (issue,))

    records: list[Record] = []
    issues: list[dict[str, Any]] = []
    for name, observed in listed.items():
        relative = f"{directory}/{name}"
        path = root / name
        try:
            metadata = observed.metadata
            if metadata is None:
                raise ValidationError(
                    f"could not inspect record: {observed.error or 'unknown error'}"
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise ValidationError("record must be a regular file and cannot be a symbolic link")
            if metadata.st_size > MAX_RECORD_BYTES:
                raise ValidationError("record exceeds its size bound")
            record = parser(vault._read_text(path, max_bytes=MAX_RECORD_BYTES))
            vault._assert_record_identity(path, record)
        except (ContinuityError, OSError, UnicodeError) as exc:
            issues.append(_projection_issue("invalid-record", relative, str(exc)))
            continue
        records.append(record)

    try:
        confirmed = _record_directory_snapshot(root)
        after = os.lstat(root)
    except OSError as exc:
        issue = _projection_issue(
            "unavailable-record-section",
            directory,
            f"the {directory} record section changed during inspection: {exc}",
        )
        return _RecordProjection((), "unavailable", (issue,))
    directory_changed = not stat.S_ISDIR(before.st_mode) or not stat.S_ISDIR(after.st_mode)
    directory_changed = directory_changed or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    )
    initial_fingerprints = {name: item.fingerprint() for name, item in listed.items()}
    confirmed_fingerprints = {name: item.fingerprint() for name, item in confirmed.items()}
    if directory_changed or initial_fingerprints != confirmed_fingerprints:
        issue = _projection_issue(
            "unavailable-record-section",
            directory,
            f"the {directory} record section changed during inspection; "
            "reload to use a stable view",
        )
        return _RecordProjection((), "unavailable", (issue,))

    return _RecordProjection(
        tuple(sorted(records, key=sort_key)),
        "partial" if issues else "complete",
        tuple(issues),
    )


def _record_directory_snapshot(root: Path) -> dict[str, _ObservedRecordFile]:
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries if entry.name.endswith(".md"))

    observed: dict[str, _ObservedRecordFile] = {}
    for name in names:
        try:
            observed[name] = _ObservedRecordFile(os.lstat(root / name))
        except OSError as exc:
            observed[name] = _ObservedRecordFile(None, str(exc))
    return observed


def _section_directory_issue(path: Path, vault_root: Path, directory: str) -> dict[str, Any] | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return _projection_issue(
            "unavailable-record-section",
            directory,
            f"the {directory} record section is missing",
        )
    except OSError as exc:
        return _projection_issue(
            "unavailable-record-section",
            directory,
            f"could not inspect the {directory} record section: {exc}",
        )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return _projection_issue(
            "unavailable-record-section",
            str(path.relative_to(vault_root)),
            f"the {directory} record section must be a real directory, not a link or other file",
        )
    return None


def _projection_issue(code: str, path: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "path": path, "repairable": False}


def _bridge_doctor(vault: Vault) -> dict[str, Any]:
    try:
        return doctor_dict(vault.doctor())
    except (ContinuityError, OSError, UnicodeError) as exc:
        identity = vault.identity()
        return {
            "counts": {"entities": 0, "tasks": 0, "threads": 0},
            "healthy": False,
            "issues": [
                _projection_issue(
                    "doctor-unavailable",
                    ".",
                    f"the full vault integrity check could not finish: {exc}",
                )
            ],
            "repaired": [],
            "vault": str(vault.root),
            "vault_id": identity["vault_id"],
        }


def _merge_projection_doctor(
    doctor: dict[str, Any], projections: dict[str, _RecordProjection]
) -> dict[str, Any]:
    merged = dict(doctor)
    prior_issues = doctor.get("issues")
    issues = [dict(issue) for issue in prior_issues] if isinstance(prior_issues, list) else []
    seen = {
        (issue.get("code"), issue.get("path"), issue.get("message"))
        for issue in issues
        if isinstance(issue, dict)
    }
    for projection in projections.values():
        for issue in projection.issues:
            identity = (issue.get("code"), issue.get("path"), issue.get("message"))
            if identity not in seen:
                issues.append(dict(issue))
                seen.add(identity)
    prior_counts = doctor.get("counts")
    counts = dict(prior_counts) if isinstance(prior_counts, dict) else {}
    counts.update({name: len(projection.records) for name, projection in projections.items()})
    merged["counts"] = counts
    merged["issues"] = issues
    if any(projection.state != "complete" for projection in projections.values()):
        merged["healthy"] = False
    return merged


def codex_deep_link(vault: Path, prompt: str) -> str:
    """Build the installed Codex app's verified new-task deep-link shape."""

    query = urlencode(
        {
            "originUrl": REPOSITORY_URL,
            "path": str(vault.expanduser().resolve()),
            "prompt": prompt,
        }
    )
    return f"codex://new?{query}"


def _codex_metadata() -> dict[str, Any]:
    try:
        return {"available": True, **codex_status()}
    except ContinuityError as exc:
        return {"available": False, "error": str(exc)}


class BridgeHTTPServer(ThreadingHTTPServer):
    """Loopback server carrying one vault and one immutable static root."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        vault: Vault,
        static_root: Path,
        *,
        access_token: str,
        instance_id: str,
        integration_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.access_token = access_token
        self.instance_id = instance_id
        self.vault = vault
        self.vault_id = str(vault.identity()["vault_id"])
        self.static_root = static_root.resolve()
        self.integration_provider = integration_provider or _codex_metadata
        self._doctor: dict[str, Any] | None = None
        self._doctor_at = 0.0
        self._integration: dict[str, Any] = {
            "available": False,
            "checking": True,
            "instructions_installed": False,
            "plugin_installed": False,
        }
        self._integration_at = 0.0
        self._integration_refreshing = False
        self._metadata_lock = threading.Lock()
        super().__init__(address, BridgeRequestHandler)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        doctor = self._doctor_snapshot(now)
        integration = self._integration_snapshot(now)
        return bridge_snapshot(
            self.vault,
            doctor=doctor,
            integration=integration,
        )

    def _doctor_snapshot(self, now: float) -> dict[str, Any]:
        with self._metadata_lock:
            cached = self._doctor
            cached_at = self._doctor_at
        if cached is not None and now - cached_at < METADATA_CACHE_SECONDS:
            return dict(cached)

        refreshed = _bridge_doctor(self.vault)
        with self._metadata_lock:
            if self._doctor is None or self._doctor_at <= cached_at:
                self._doctor = refreshed
                self._doctor_at = time.monotonic()
            return dict(self._doctor)

    def _integration_snapshot(self, now: float) -> dict[str, Any]:
        start_refresh = False
        with self._metadata_lock:
            if (
                now - self._integration_at >= METADATA_CACHE_SECONDS
                and not self._integration_refreshing
            ):
                self._integration_refreshing = True
                start_refresh = True
            integration = dict(self._integration)
        if start_refresh:
            thread = threading.Thread(
                target=self._refresh_integration,
                name="gsv-bridge-codex-status",
                daemon=True,
            )
            thread.start()
        return integration

    def _refresh_integration(self) -> None:
        try:
            refreshed = {"checking": False, **self.integration_provider()}
        except Exception as exc:  # pragma: no cover - final background safety net
            refreshed = {
                "available": False,
                "checking": False,
                "error": f"Codex integration check failed: {type(exc).__name__}",
                "instructions_installed": False,
                "plugin_installed": False,
            }
        with self._metadata_lock:
            self._integration = refreshed
            self._integration_at = time.monotonic()
            self._integration_refreshing = False


class BridgeRequestHandler(BaseHTTPRequestHandler):
    """Serve a read-only projection with strict loopback and browser headers."""

    protocol_version = "HTTP/1.1"

    @property
    def bridge(self) -> BridgeHTTPServer:
        return cast(BridgeHTTPServer, self.server)

    def do_GET(self) -> None:
        self._handle(head_only=False)

    def do_HEAD(self) -> None:
        self._handle(head_only=True)

    def do_POST(self) -> None:
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "The Bridge is read-only")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _handle(self, *, head_only: bool) -> None:
        if len(self.path) > MAX_TARGET_LENGTH:
            self._error(HTTPStatus.REQUEST_URI_TOO_LONG, "Request target is too long")
            return
        if not self._valid_host() or not self._valid_origin():
            self._error(HTTPStatus.FORBIDDEN, "The Bridge is available only on this computer")
            return
        target = urlsplit(self.path)
        try:
            if target.path == "/api/v1/health":
                if not self._authorized():
                    self._error(HTTPStatus.FORBIDDEN, "That Bridge session is not available")
                    return
                self._json(
                    {
                        "ok": True,
                        "instance_id": self.bridge.instance_id,
                        "pid": os.getpid(),
                        "port": int(self.bridge.server_address[1]),
                        "service": "gsv-bridge",
                        "vault_id": self.bridge.vault_id,
                        "version": __version__,
                    },
                    head_only=head_only,
                )
                return
            if target.path == "/api/v1/snapshot":
                if not self._authorized():
                    self._error(HTTPStatus.FORBIDDEN, "That Bridge session is not available")
                    return
                self._json(self.bridge.snapshot(), head_only=head_only)
                return
            if target.path in {"/", "/index.html"}:
                self._static("index.html", head_only=head_only)
                return
            if target.path.startswith("/static/"):
                self._static(target.path.removeprefix("/static/"), head_only=head_only)
                return
            self._error(HTTPStatus.NOT_FOUND, "That Bridge view does not exist")
        except (ContinuityError, OSError, UnicodeError, ValueError):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "The local GSV vault is unavailable")

    def _valid_host(self) -> bool:
        raw = self.headers.get("Host", "")
        if not raw:
            return False
        try:
            parsed = urlsplit(f"http://{raw}")
            return parsed.hostname in {LOOPBACK_HOST, "localhost", "::1"} and parsed.port == int(
                self.bridge.server_address[1]
            )
        except ValueError:
            return False

    def _valid_origin(self) -> bool:
        raw = self.headers.get("Origin")
        if raw is None:
            return True
        try:
            parsed = urlsplit(raw)
            return (
                parsed.scheme == "http"
                and parsed.hostname in {LOOPBACK_HOST, "localhost", "::1"}
                and parsed.port == int(self.bridge.server_address[1])
            )
        except ValueError:
            return False

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        supplied = (
            authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        )
        return bool(
            supplied
            and _valid_access_token(supplied)
            and compare_digest(supplied, self.bridge.access_token)
        )

    def _static(self, relative: str, *, head_only: bool) -> None:
        decoded = unquote(relative)
        path = PurePosixPath(decoded)
        if path.is_absolute() or ".." in path.parts or "\\" in decoded:
            self._error(HTTPStatus.NOT_FOUND, "That Bridge asset does not exist")
            return
        target = (self.bridge.static_root / Path(*path.parts)).resolve()
        try:
            target.relative_to(self.bridge.static_root)
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "That Bridge asset does not exist")
            return
        if not target.is_file() or target.is_symlink() or target.stat().st_size > MAX_STATIC_BYTES:
            self._error(HTTPStatus.NOT_FOUND, "That Bridge asset does not exist")
            return
        payload = target.read_bytes()
        content_type = _MIME_TYPES.get(
            target.suffix.lower(),
            mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        )
        self._send(
            HTTPStatus.OK,
            payload,
            content_type=content_type,
            cache_control="public, max-age=300",
            head_only=head_only,
        )

    def _json(self, payload: dict[str, Any], *, head_only: bool) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(
            HTTPStatus.OK,
            encoded,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
            head_only=head_only,
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        payload = json.dumps({"error": message, "ok": False}).encode("utf-8")
        self._send(
            status,
            payload,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
            head_only=False,
        )

    def _send(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str,
        cache_control: str,
        head_only: bool,
    ) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Content-Type", content_type)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                return


def serve_bridge(
    vault: Vault,
    *,
    port: int = DEFAULT_PORT,
    write_state: bool = True,
    access_token: str | None = None,
    instance_id: str | None = None,
) -> int:
    """Serve the Bridge until interrupted and return the bound port."""

    if not 0 <= port <= 65_535:
        raise ValidationError("Bridge port must be between 0 and 65535")
    identity = instance_id or uuid.uuid4().hex
    if not _valid_instance_id(identity):
        raise ValidationError("Bridge instance ID must be 32 lowercase hexadecimal characters")
    token = access_token or secrets.token_hex(24)
    if not _valid_access_token(token):
        raise ValidationError("Bridge access token must be 48 lowercase hexadecimal characters")
    static_resource = files("continuity_kernel") / "resources/bridge"
    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            (LOOPBACK_HOST, port),
            vault,
            Path(static_root),
            access_token=token,
            instance_id=identity,
        )
        bound_port = int(server.server_address[1])
        state = {
            "format_version": STATE_VERSION,
            "instance_id": identity,
            "pid": os.getpid(),
            "port": bound_port,
            "token": token,
            "url": f"http://{LOOPBACK_HOST}:{bound_port}/",
            "vault": str(vault.root),
            "vault_id": server.vault_id,
        }
        if write_state:
            _write_state(state)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            if write_state:
                _remove_state_if_owned(os.getpid(), identity)
        return bound_port


def open_bridge(vault: Vault, *, open_browser: bool = True) -> dict[str, Any]:
    """Reuse or start the detached Bridge, then optionally open its local URL."""

    root = _private_runtime_dir()
    with exclusive_lock(root / "bridge.lock"):
        current, current_running, health_unavailable = _current_state()
        if health_unavailable:
            raise SetupError(
                "The existing Bridge process is still alive, but its private health check "
                "did not answer. Its receipt was preserved; retry `gsv bridge status` or "
                "`gsv bridge stop`."
            )
        if current_running and current is not None:
            if Path(str(current["vault"])).resolve() != vault.root:
                raise SetupError(
                    "The Bridge is already running for another GSV vault. "
                    "Run `gsv bridge stop` before switching vaults."
                )
            url = str(current["url"])
            started = False
        else:
            _discard_state()
            instance_id = uuid.uuid4().hex
            command = [*_runtime_command(), "--vault", str(vault.root), "bridge", "serve"]
            command.extend(["--port", "0", "--instance-id", instance_id])
            log_path = root / "bridge.log"
            environment = _bridge_child_environment(vault)
            with _open_private_log(log_path) as log:
                options: dict[str, Any] = {
                    "close_fds": True,
                    "env": environment,
                    "stderr": log,
                    "stdin": subprocess.DEVNULL,
                    "stdout": log,
                }
                if os.name == "nt":
                    options["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS
                else:
                    options["start_new_session"] = True
                process = subprocess.Popen(command, **options)
            state = _wait_for_state(
                instance_id,
                process.pid,
                expected_pid=None if getattr(sys, "frozen", False) else process.pid,
                vault_id=str(vault.identity()["vault_id"]),
                timeout=8.0,
            )
            if state is None:
                _terminate_pid(process.pid)
                raise SetupError(f"The Bridge did not become healthy. See {log_path}")
            health = _health_payload(str(state["url"]), token=str(state["token"]), timeout=2.0)
            if not _state_matches_health(state, health):
                _terminate_pid(process.pid)
                _remove_state_if_owned(int(state["pid"]), instance_id)
                raise SetupError(f"The Bridge reported an unexpected identity. See {log_path}")
            url = str(state["url"])
            started = True
    opened = open_bridge_in_browser(vault) if open_browser else False
    result = {
        "browser_opened": opened,
        "local": True,
        "running": True,
        "started": started,
        "url": url,
        "vault": str(vault.root),
    }
    if open_browser and not opened:
        result["next"] = "The Bridge is running. Run gsv again to open its private session."
    return result


def open_bridge_in_browser(vault: Vault) -> bool:
    """Open only a live Bridge session that is verified for this exact vault."""

    root = _private_runtime_dir()
    with exclusive_lock(root / "bridge.lock"):
        state, running, _ = _current_state()
        if not running or state is None:
            return False
        if Path(str(state["vault"])).resolve() != vault.root:
            return False
        try:
            return _launch_browser(_open_url(state))
        except (webbrowser.Error, OSError):
            return False


def _launch_browser(url: str) -> bool:
    return webbrowser.open(url, new=1)


def bridge_status() -> dict[str, Any]:
    """Return current detached Bridge state without starting anything."""

    state, running, health_unavailable = _current_state()
    if state is None:
        return {"running": False}
    return {
        **_public_state(state),
        "health_unavailable": health_unavailable,
        "identity_verified": running,
        "running": running or health_unavailable,
    }


def stop_bridge() -> dict[str, Any]:
    """Stop only the process named by the GSV-owned Bridge receipt."""

    state, invalid_removed = _load_state_safely()
    if state is None:
        return {
            "running": False,
            "stale_receipt_removed": invalid_removed,
            "stopped": False,
        }
    pid = int(state["pid"])
    if not _pid_alive(pid):
        _remove_state_if_owned(pid, str(state["instance_id"]))
        return {
            "running": False,
            "stale_reason": "process_not_running",
            "stale_receipt_removed": True,
            "stopped": False,
        }
    probe = _probe_health(str(state["url"]), token=str(state["token"]), timeout=0.75)
    if probe.outcome is _HealthOutcome.UNAVAILABLE:
        raise SetupError(
            "The Bridge process is still alive, but its private health check did not answer. "
            "No process was signalled and the receipt was preserved; retry `gsv bridge stop`."
        )
    if probe.outcome is _HealthOutcome.REFUSED:
        _remove_state_if_owned(pid, str(state["instance_id"]))
        return {
            "running": False,
            "stale_reason": "connection_refused",
            "stale_receipt_removed": True,
            "stopped": False,
        }
    if not _state_matches_health(state, probe.payload):
        _remove_state_if_owned(pid, str(state["instance_id"]))
        return {
            "running": False,
            "stale_reason": "identity_mismatch",
            "stale_receipt_removed": True,
            "stopped": False,
        }
    if _pid_alive(pid):
        _terminate_pid(pid)
        deadline = time.monotonic() + 3.0
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    stopped = not _pid_alive(pid)
    if not stopped:
        raise SetupError(
            "The verified Bridge process did not stop. Its receipt was preserved; retry "
            "`gsv bridge stop` before installing or uninstalling."
        )
    _remove_state_if_owned(pid, str(state["instance_id"]))
    return {"running": not stopped, "stopped": stopped, "url": state["url"]}


def _state_path() -> Path:
    return data_dir() / "bridge-state.json"


def _write_state(payload: dict[str, Any]) -> None:
    path = _state_path()
    _private_runtime_dir()
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, encoded)


def _load_state() -> dict[str, Any] | None:
    path = _state_path()
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"invalid Bridge state path: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid Bridge state: {path}") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != STATE_VERSION:
        raise ValidationError("unsupported Bridge state version")
    if not isinstance(payload.get("pid"), int) or not isinstance(payload.get("port"), int):
        raise ValidationError("Bridge state is incomplete")
    if not isinstance(payload.get("url"), str) or not isinstance(payload.get("vault"), str):
        raise ValidationError("Bridge state is incomplete")
    if not isinstance(payload.get("vault_id"), str) or not _valid_instance_id(
        payload.get("instance_id")
    ):
        raise ValidationError("Bridge state has no valid instance identity")
    if not _valid_access_token(payload.get("token")) or not _state_url_matches(payload):
        raise ValidationError("Bridge state has no valid local session")
    return payload


def _load_state_safely() -> tuple[dict[str, Any] | None, bool]:
    try:
        return _load_state(), False
    except (OSError, ValidationError):
        return None, _discard_state()


def _current_state() -> tuple[dict[str, Any] | None, bool, bool]:
    state, _ = _load_state_safely()
    if state is None:
        return None, False, False
    pid = int(state["pid"])
    if not _pid_alive(pid):
        _remove_state_if_owned(pid, str(state["instance_id"]))
        return None, False, False
    probe = _probe_health(str(state["url"]), token=str(state["token"]), timeout=0.5)
    if probe.outcome is _HealthOutcome.UNAVAILABLE:
        return state, False, True
    if probe.outcome is _HealthOutcome.REFUSED or not _state_matches_health(state, probe.payload):
        _remove_state_if_owned(pid, str(state["instance_id"]))
        return None, False, False
    return state, True, False


def _remove_state_if_owned(pid: int, instance_id: str) -> None:
    state, _ = _load_state_safely()
    if state is not None and state["pid"] == pid and state["instance_id"] == instance_id:
        _state_path().unlink(missing_ok=True)


def _discard_state() -> bool:
    path = _state_path()
    if not os.path.lexists(path):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _health_payload(url: str, *, token: str, timeout: float) -> dict[str, Any] | None:
    probe = _probe_health(url, token=token, timeout=timeout)
    return probe.payload if probe.outcome is _HealthOutcome.RESPONSE else None


def _probe_health(url: str, *, token: str, timeout: float) -> _HealthProbe:
    deadline = time.monotonic() + timeout
    health = f"{url.rstrip('/')}/api/v1/health"
    loopback = urlsplit(url).hostname == LOOPBACK_HOST
    while True:
        try:
            request = Request(
                health,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
            with urlopen(request, timeout=min(0.5, max(timeout, 0.1))) as response:
                payload = json.loads(response.read())
                if response.status == HTTPStatus.OK and isinstance(payload, dict):
                    return _HealthProbe(_HealthOutcome.RESPONSE, payload)
        except HTTPError as exc:
            if exc.code in {
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
            }:
                return _HealthProbe(_HealthOutcome.RESPONSE, {})
        except (OSError, URLError) as exc:
            if loopback and _is_connection_refused(exc):
                return _HealthProbe(_HealthOutcome.REFUSED)
        except (json.JSONDecodeError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return _HealthProbe(_HealthOutcome.UNAVAILABLE)
        time.sleep(0.05)


def _is_connection_refused(error: BaseException) -> bool:
    refused_codes = {errno.ECONNREFUSED, int(getattr(errno, "WSAECONNREFUSED", 10061))}
    current: object = error
    for _ in range(4):
        if isinstance(current, ConnectionRefusedError):
            return True
        if isinstance(current, OSError) and current.errno in refused_codes:
            return True
        current = getattr(current, "reason", None)
        if current is None:
            return False
    return False


def _wait_for_state(
    instance_id: str,
    launcher_pid: int,
    *,
    expected_pid: int | None,
    vault_id: str,
    timeout: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = _load_state()
        except ValidationError:
            state = None
        if (
            state is not None
            and state["instance_id"] == instance_id
            and (expected_pid is None or state["pid"] == expected_pid)
            and state["vault_id"] == vault_id
        ):
            return state
        if expected_pid is not None and not _pid_alive(launcher_pid):
            return None
        time.sleep(0.05)
    return None


def _state_matches_health(state: dict[str, Any], health: dict[str, Any] | None) -> bool:
    if health is None:
        return False
    return (
        health.get("service") == "gsv-bridge"
        and health.get("instance_id") == state.get("instance_id")
        and health.get("pid") == state.get("pid")
        and health.get("port") == state.get("port")
        and health.get("vault_id") == state.get("vault_id")
        and _state_url_matches(state)
    )


def _state_url_matches(state: dict[str, Any]) -> bool:
    try:
        parsed = urlsplit(str(state.get("url", "")))
        return (
            parsed.scheme == "http"
            and parsed.hostname == LOOPBACK_HOST
            and parsed.port == state.get("port")
            and parsed.path == "/"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _open_url(state: dict[str, Any]) -> str:
    return f"{state['url']}#token={state['token']}"


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != "token"}


def _valid_instance_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_access_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 48
        and all(character in "0123456789abcdef" for character in value)
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        return _windows_pid_alive(pid)
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Probe a Windows process handle without delivering any signal."""

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

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    handle = open_process(synchronize, 0, pid)
    if not handle:
        return False
    try:
        return int(wait_for_single_object(handle, 0)) == wait_timeout
    finally:
        close_handle(handle)


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def _runtime_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    launcher = Path(sys.argv[0]).expanduser()
    if launcher.name.lower() in {"gsv", "gsv.exe"} and launcher.exists():
        return [str(launcher.resolve())]
    return [sys.executable, "-m", "continuity_kernel"]


def _bridge_child_environment(vault: Vault) -> dict[str, str]:
    environment = os.environ.copy()
    environment["GSV_VAULT"] = str(vault.root)
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def _private_runtime_dir() -> Path:
    root = data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise SetupError(f"The Bridge data path is not a private directory: {root}")
        if not _IS_WINDOWS:
            flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(root, flags)
            try:
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise SetupError(f"The Bridge data path is not a directory: {root}")
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
    except OSError as exc:
        raise SetupError(f"Could not secure the Bridge data directory {root}: {exc}") from exc
    return root


def _open_private_log(path: Path) -> BinaryIO:
    try:
        if os.path.lexists(path):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SetupError(f"Refusing unsafe Bridge log path: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SetupError(f"Refusing unsafe Bridge log path: {path}")
            if not _IS_WINDOWS:
                os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "ab")
        except Exception:
            os.close(descriptor)
            raise
    except OSError as exc:
        raise SetupError(f"Could not open the private Bridge log {path}: {exc}") from exc
