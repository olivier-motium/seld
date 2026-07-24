"""The Bridge: a loopback view plus narrow control queue for one GSV vault."""

from __future__ import annotations

import errno
import json
import mimetypes
import os
import secrets
import select
import signal
import socket
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
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import as_file, files
from pathlib import Path, PurePosixPath
from socketserver import TCPServer
from typing import Any, BinaryIO, Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from continuity_kernel import __version__
from continuity_kernel.atomic import atomic_write, exclusive_lock, read_regular_file
from continuity_kernel.bridge_projection import (
    REPOSITORY_URL as REPOSITORY_URL,
)
from continuity_kernel.bridge_projection import (
    _bridge_doctor,
    project_snapshot,
)
from continuity_kernel.bridge_projection import (
    codex_deep_link as codex_deep_link,
)
from continuity_kernel.config import data_dir
from continuity_kernel.control_queue import ControlQueue, ControlStorageError, event_dict
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    DegradedIntegrityError,
    MutationCommittedError,
    SetupError,
    ValidationError,
)
from continuity_kernel.operations import OperationLedger
from continuity_kernel.vault import Vault

LOOPBACK_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 0
MAX_TARGET_LENGTH: Final = 4_096
MAX_CONTROL_BODY_BYTES: Final = 16 * 1_024
MAX_STATIC_BYTES: Final = 4 * 1024 * 1024
MAX_STATE_BYTES: Final = 64 * 1024
STATE_VERSION: Final = 1
METADATA_CACHE_SECONDS: Final = 30.0
BRIDGE_LOCK_TIMEOUT: Final = 25.0
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
class BridgeState:
    """Validated owner-only receipt for one detached Bridge process."""

    format_version: int
    instance_id: str
    pid: int
    port: int
    token: str
    url: str
    vault: str
    vault_id: str

    @classmethod
    def from_payload(cls, payload: object) -> BridgeState:
        if not isinstance(payload, dict) or payload.get("format_version") != STATE_VERSION:
            raise ValidationError("unsupported Bridge state version")
        if type(payload.get("pid")) is not int or type(payload.get("port")) is not int:
            raise ValidationError("Bridge state is incomplete")
        required_strings = ("instance_id", "token", "url", "vault", "vault_id")
        if any(not isinstance(payload.get(key), str) for key in required_strings):
            raise ValidationError("Bridge state is incomplete")
        state = cls(
            format_version=STATE_VERSION,
            instance_id=cast(str, payload["instance_id"]),
            pid=cast(int, payload["pid"]),
            port=cast(int, payload["port"]),
            token=cast(str, payload["token"]),
            url=cast(str, payload["url"]),
            vault=cast(str, payload["vault"]),
            vault_id=cast(str, payload["vault_id"]),
        )
        if not _valid_instance_id(state.instance_id) or not _valid_access_token(state.token):
            raise ValidationError("Bridge state has no valid local session")
        if not _state_url_matches(state):
            raise ValidationError("Bridge state has no valid local session")
        return state

    def payload(self, *, include_token: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "format_version": self.format_version,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "port": self.port,
            "url": self.url,
            "vault": self.vault,
            "vault_id": self.vault_id,
        }
        if include_token:
            result["token"] = self.token
        return result


def bridge_snapshot(
    vault: Vault,
    *,
    doctor: dict[str, Any] | None = None,
    integration: dict[str, Any] | None = None,
    expected_vault_id: str | None = None,
    expected_root_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Return the exact authored records needed by the read-only Bridge."""

    return project_snapshot(
        vault,
        doctor=doctor if doctor is not None else _bridge_doctor(vault),
        integration=integration if integration is not None else _codex_metadata(),
        expected_vault_id=expected_vault_id,
        expected_root_identity=expected_root_identity,
    )


def _codex_metadata() -> dict[str, Any]:
    try:
        return {"available": True, **codex_status()}
    except (ContinuityError, OSError) as exc:
        return {"available": False, "error": str(exc)}


def codex_status() -> dict[str, Any]:
    """Load the optional provider integration only when a snapshot needs it."""

    from continuity_kernel.codex_integration import codex_status as read_status

    return read_status()


class BridgeHTTPServer(ThreadingHTTPServer):
    """Loopback server carrying one vault and one immutable static root."""

    allow_reuse_address = not _IS_WINDOWS
    daemon_threads = True
    request_queue_size = 16

    def server_bind(self) -> None:
        """Bind the numeric loopback address without a reverse-DNS lookup."""

        _set_windows_exclusive_address_use(self.socket)
        TCPServer.server_bind(self)
        self.server_name = LOOPBACK_HOST
        self.server_port = int(self.server_address[1])

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
        root_metadata = os.lstat(vault.root)
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise ValidationError("Bridge vault root must be a real directory")
        self.vault_root_identity = (
            int(root_metadata.st_dev),
            int(root_metadata.st_ino),
        )
        self.vault_id = str(vault.identity()["vault_id"])
        current_root = os.lstat(vault.root)
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or (int(current_root.st_dev), int(current_root.st_ino)) != self.vault_root_identity
        ):
            raise ValidationError("Bridge vault root changed during startup")
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
        self._require_current_vault_identity()
        now = time.monotonic()
        doctor = self._doctor_snapshot(now)
        integration = self._integration_snapshot(now)
        snapshot = bridge_snapshot(
            self.vault,
            doctor=doctor,
            integration=integration,
            expected_vault_id=self.vault_id,
            expected_root_identity=self.vault_root_identity,
        )
        self._require_current_vault_identity()
        return snapshot

    def _require_current_vault_identity(self) -> None:
        """Refuse reads or writes after the Bridge pathname names another vault."""

        try:
            root_metadata = os.lstat(self.vault.root)
            current = str(self.vault.identity()["vault_id"])
        except (ContinuityError, OSError, KeyError, TypeError) as exc:
            raise DegradedIntegrityError(
                "the Bridge vault identity is no longer available"
            ) from exc
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or (int(root_metadata.st_dev), int(root_metadata.st_ino)) != self.vault_root_identity
            or current != self.vault_id
        ):
            raise DegradedIntegrityError("the Bridge vault identity changed after startup")

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
    """Serve local reads plus one authenticated, bounded intent endpoint."""

    protocol_version = "HTTP/1.1"

    @property
    def bridge(self) -> BridgeHTTPServer:
        return cast(BridgeHTTPServer, self.server)

    def do_GET(self) -> None:
        self._handle(head_only=False)

    def do_HEAD(self) -> None:
        self._handle(head_only=True)

    def do_POST(self) -> None:
        if len(self.path) > MAX_TARGET_LENGTH:
            self._error(HTTPStatus.REQUEST_URI_TOO_LONG, "Request target is too long")
            return
        if not self._valid_host() or not self._valid_origin(required=True):
            self._error(HTTPStatus.FORBIDDEN, "The Bridge is available only on this computer")
            return
        target = urlsplit(self.path)
        if target.path != "/api/v1/control":
            self._error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "The Bridge accepts only bounded control requests",
            )
            return
        if not self._authorized():
            self._error(HTTPStatus.FORBIDDEN, "That Bridge session is not available")
            return
        self._append_control()

    def _reject_non_post_write(self) -> None:
        self.close_connection = True
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Only POST can append a Bridge intent")

    do_PUT = _reject_non_post_write
    do_PATCH = _reject_non_post_write
    do_DELETE = _reject_non_post_write

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _handle(self, *, head_only: bool) -> None:
        if len(self.path) > MAX_TARGET_LENGTH:
            self._error(
                HTTPStatus.REQUEST_URI_TOO_LONG,
                "Request target is too long",
                head_only=head_only,
            )
            return
        if not self._valid_host() or not self._valid_origin():
            self._error(
                HTTPStatus.FORBIDDEN,
                "The Bridge is available only on this computer",
                head_only=head_only,
            )
            return
        target = urlsplit(self.path)
        try:
            if target.path == "/api/v1/health":
                if not self._authorized():
                    self._error(
                        HTTPStatus.FORBIDDEN,
                        "That Bridge session is not available",
                        head_only=head_only,
                    )
                    return
                self._json(
                    {
                        "ok": True,
                        "instance_id": self.bridge.instance_id,
                        "pid": os.getpid(),
                        "port": int(self.bridge.server_address[1]),
                        "service": "gsv-bridge",
                        "vault_id": self.bridge.vault_id,
                        "vault_root_device": self.bridge.vault_root_identity[0],
                        "vault_root_inode": self.bridge.vault_root_identity[1],
                        "version": __version__,
                    },
                    head_only=head_only,
                )
                return
            if target.path == "/api/v1/snapshot":
                if not self._authorized():
                    self._error(
                        HTTPStatus.FORBIDDEN,
                        "That Bridge session is not available",
                        head_only=head_only,
                    )
                    return
                self._json(self.bridge.snapshot(), head_only=head_only)
                return
            if target.path in {"/", "/index.html"}:
                self._static("index.html", head_only=head_only)
                return
            if target.path.startswith("/static/"):
                self._static(target.path.removeprefix("/static/"), head_only=head_only)
                return
            self._error(
                HTTPStatus.NOT_FOUND,
                "That Bridge view does not exist",
                head_only=head_only,
            )
        except (ContinuityError, OSError, UnicodeError, ValueError):
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The local GSV vault is unavailable",
                head_only=head_only,
            )

    def _append_control(self) -> None:
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Control requests must be JSON")
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(HTTPStatus.BAD_REQUEST, "Streaming request bodies are not accepted")
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if not 0 < length <= MAX_CONTROL_BODY_BYTES:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "Control request is empty or too large",
            )
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "Control request is not valid JSON")
            return
        if not isinstance(payload, dict) or set(payload) - {
            "choice",
            "expected_revision",
            "kind",
            "subject",
            "target_revision",
        }:
            self._error(HTTPStatus.BAD_REQUEST, "Control request has an unsupported shape")
            return
        try:
            self.bridge._require_current_vault_identity()
            OperationLedger(self.bridge.vault.root).snapshot(
                expected_vault_id=self.bridge.vault_id,
                expected_root_identity=self.bridge.vault_root_identity,
            )
            snapshot = ControlQueue(self.bridge.vault.root).append(
                kind=payload.get("kind"),
                subject=payload.get("subject"),
                choice=payload.get("choice"),
                expected_revision=payload.get("expected_revision"),
                target_revision=payload.get("target_revision"),
                expected_vault_id=self.bridge.vault_id,
                expected_root_identity=self.bridge.vault_root_identity,
            )
            event = snapshot.events[-1]
            self.bridge._require_current_vault_identity()
        except ConflictError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        except (MutationCommittedError, DegradedIntegrityError):
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The control queue may have changed, but GSV could not confirm its durable "
                "result. Reload the queue before retrying",
            )
            return
        except (ControlStorageError, OSError):
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The private Bridge control queue is unavailable; canonical reads remain usable",
            )
            return
        except ValidationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json(
            {
                "event": event_dict(event),
                "ok": True,
                "revision": snapshot.revision,
            },
            head_only=False,
            status=HTTPStatus.CREATED,
        )

    def _valid_host(self) -> bool:
        raw = self.headers.get("Host", "")
        port = int(self.bridge.server_address[1])
        return raw in {f"{LOOPBACK_HOST}:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _valid_origin(self, *, required: bool = False) -> bool:
        raw = self.headers.get("Origin")
        if raw is None:
            return not required
        port = int(self.bridge.server_address[1])
        return raw in {
            f"http://{LOOPBACK_HOST}:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }

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
            self._error(
                HTTPStatus.NOT_FOUND,
                "That Bridge asset does not exist",
                head_only=head_only,
            )
            return
        target = self.bridge.static_root
        try:
            for part in path.parts:
                target /= part
                metadata = os.lstat(target)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValidationError("Bridge static assets cannot be symlinks")
            target = target.resolve(strict=True)
            target.relative_to(self.bridge.static_root)
        except (OSError, RuntimeError, ValidationError, ValueError):
            self._error(
                HTTPStatus.NOT_FOUND,
                "That Bridge asset does not exist",
                head_only=head_only,
            )
            return
        try:
            payload = read_regular_file(
                target,
                label="Bridge static asset",
                max_bytes=MAX_STATIC_BYTES,
            )
        except ValidationError:
            self._error(
                HTTPStatus.NOT_FOUND,
                "That Bridge asset does not exist",
                head_only=head_only,
            )
            return
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

    def _json(
        self,
        payload: dict[str, Any],
        *,
        head_only: bool,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(
            status,
            encoded,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
            head_only=head_only,
        )

    def _error(self, status: HTTPStatus, message: str, *, head_only: bool = False) -> None:
        payload = json.dumps({"error": message, "ok": False}).encode("utf-8")
        self._send(
            status,
            payload,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
            head_only=head_only,
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
        state = BridgeState(
            format_version=STATE_VERSION,
            instance_id=identity,
            pid=os.getpid(),
            port=bound_port,
            token=token,
            url=f"http://{LOOPBACK_HOST}:{bound_port}/",
            vault=str(vault.root),
            vault_id=server.vault_id,
        )
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
    with exclusive_lock(root / "bridge.lock", timeout=BRIDGE_LOCK_TIMEOUT):
        current, current_running, health_unavailable, current_health = _current_state()
        if health_unavailable:
            raise SetupError(
                "The existing Bridge process is still alive, but its private health check "
                "did not answer. Its receipt was preserved; retry `gsv bridge status` or "
                "`gsv bridge stop`."
            )
        if current_running and current is not None:
            if Path(current.vault).resolve() != vault.root:
                raise SetupError(
                    "The Bridge is already running for another GSV vault. "
                    "Run `gsv bridge stop` before switching vaults."
                )
            if not _health_matches_vault(current_health, vault):
                raise SetupError(
                    "The running Bridge belongs to an earlier vault directory at this path. "
                    "Run `gsv bridge stop`, then run `gsv bridge open` again."
                )
            url = current.url
            started = False
        else:
            _discard_state()
            instance_id = uuid.uuid4().hex
            command = _bridge_command(vault, instance_id=instance_id)
            log_path = root / "bridge.log"
            environment = _bridge_child_environment(vault)
            with _open_private_log(log_path) as log:
                child_detaches = _child_detaches_after_spawn()
                options: dict[str, Any] = {
                    "close_fds": not child_detaches,
                    "env": environment,
                    "stderr": log,
                    "stdin": subprocess.DEVNULL,
                    "stdout": log,
                }
                if os.name == "nt":
                    options["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS
                elif not child_detaches:
                    options["start_new_session"] = True
                process = subprocess.Popen(command, **options)
            state = _wait_for_state(
                instance_id,
                process,
                # A frozen launcher may hand off to a worker process. Source and
                # wheel installs launch the internal worker directly, so its PID
                # must match the authenticated receipt.
                expected_pid=(
                    None if _IS_WINDOWS or getattr(sys, "frozen", False) else process.pid
                ),
                vault_id=str(vault.identity()["vault_id"]),
                timeout=20.0,
            )
            if state is None:
                _terminate_pid(process.pid)
                raise SetupError(_bridge_start_failure(process, log_path))
            health = _health_payload(state.url, token=state.token, timeout=2.0)
            if not _state_matches_health(state, health):
                _terminate_pid(process.pid)
                _remove_state_if_owned(state.pid, instance_id)
                raise SetupError(f"The Bridge reported an unexpected identity. See {log_path}")
            if not _health_matches_vault(health, vault):
                _terminate_pid(process.pid)
                _remove_state_if_owned(state.pid, instance_id)
                raise SetupError(
                    "The Bridge started against a different vault directory identity. "
                    f"See {log_path}"
                )
            url = state.url
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
    with exclusive_lock(root / "bridge.lock", timeout=BRIDGE_LOCK_TIMEOUT):
        state, running, _, health = _current_state()
        if not running or state is None:
            return False
        if Path(state.vault).resolve() != vault.root:
            return False
        if not _health_matches_vault(health, vault):
            return False
        try:
            return _launch_browser(_open_url(state))
        except (webbrowser.Error, OSError):
            return False


def _launch_browser(url: str) -> bool:
    return webbrowser.open(url, new=1)


def bridge_status() -> dict[str, Any]:
    """Return current detached Bridge state without starting anything."""

    root = _private_runtime_dir()
    with exclusive_lock(root / "bridge.lock", timeout=BRIDGE_LOCK_TIMEOUT):
        return _bridge_status_locked()


def _bridge_status_locked() -> dict[str, Any]:
    state, running, health_unavailable, _ = _current_state()
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

    root = _private_runtime_dir()
    with exclusive_lock(root / "bridge.lock", timeout=BRIDGE_LOCK_TIMEOUT):
        return _stop_bridge_locked()


def _stop_bridge_locked() -> dict[str, Any]:
    state, invalid_removed = _load_state_safely()
    if state is None:
        return {
            "running": False,
            "stale_receipt_removed": invalid_removed,
            "stopped": False,
        }
    pid = state.pid
    if not _pid_alive(pid):
        _remove_state_if_owned(pid, state.instance_id)
        return {
            "running": False,
            "stale_reason": "process_not_running",
            "stale_receipt_removed": True,
            "stopped": False,
        }
    probe = _probe_health(state.url, token=state.token, timeout=0.75)
    if probe.outcome is _HealthOutcome.UNAVAILABLE:
        raise SetupError(
            "The Bridge process is still alive, but its private health check did not answer. "
            "No process was signalled and the receipt was preserved; retry `gsv bridge stop`."
        )
    if probe.outcome is _HealthOutcome.REFUSED:
        _remove_state_if_owned(pid, state.instance_id)
        return {
            "running": False,
            "stale_reason": "connection_refused",
            "stale_receipt_removed": True,
            "stopped": False,
        }
    if not _state_matches_health(state, probe.payload):
        _remove_state_if_owned(pid, state.instance_id)
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
    _remove_state_if_owned(pid, state.instance_id)
    return {"running": not stopped, "stopped": stopped, "url": state.url}


def _state_path() -> Path:
    return data_dir() / "bridge-state.json"


def _write_state(state: BridgeState | dict[str, Any]) -> None:
    path = _state_path()
    _private_runtime_dir()
    validated = state if isinstance(state, BridgeState) else BridgeState.from_payload(state)
    encoded = (json.dumps(validated.payload(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, encoded)


def _load_state() -> BridgeState | None:
    path = _state_path()
    if not os.path.lexists(path):
        return None
    try:
        encoded = read_regular_file(
            path,
            label="Bridge state",
            max_bytes=MAX_STATE_BYTES,
        )
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid Bridge state: {path}") from exc
    return BridgeState.from_payload(payload)


def _load_state_safely() -> tuple[BridgeState | None, bool]:
    try:
        return _load_state(), False
    except (OSError, ValidationError):
        return None, _discard_state()


def _current_state() -> tuple[BridgeState | None, bool, bool, dict[str, Any] | None]:
    state, _ = _load_state_safely()
    if state is None:
        return None, False, False, None
    pid = state.pid
    if not _pid_alive(pid):
        _remove_state_if_owned(pid, state.instance_id)
        return None, False, False, None
    probe = _probe_health(state.url, token=state.token, timeout=0.5)
    if probe.outcome is _HealthOutcome.UNAVAILABLE:
        return state, False, True, None
    if probe.outcome is _HealthOutcome.REFUSED or not _state_matches_health(state, probe.payload):
        _remove_state_if_owned(pid, state.instance_id)
        return None, False, False, None
    return state, True, False, probe.payload


def _remove_state_if_owned(pid: int, instance_id: str) -> None:
    state, _ = _load_state_safely()
    if state is not None and state.pid == pid and state.instance_id == instance_id:
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
            with _open_loopback(request, timeout=min(0.5, max(timeout, 0.1))) as response:
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
        except HTTPException:
            pass
        except (OSError, URLError) as exc:
            if loopback and (
                _is_connection_refused(exc) or _loopback_connection_refused(url, timeout=timeout)
            ):
                return _HealthProbe(_HealthOutcome.REFUSED)
        except (json.JSONDecodeError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return _HealthProbe(_HealthOutcome.UNAVAILABLE)
        time.sleep(0.05)


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
            "GSV refuses redirects for authenticated loopback requests",
            headers,
            file_pointer,
        )


def _open_loopback(request: Request, *, timeout: float) -> Any:
    """Open an exact local URL without proxies or redirect credential forwarding."""

    target = urlsplit(request.full_url)
    try:
        port = target.port
    except ValueError as exc:
        raise ValueError("Bridge health received an invalid loopback URL") from exc
    if (
        target.scheme != "http"
        or target.hostname != LOOPBACK_HOST
        or port is None
        or port < 1
        or target.username is not None
        or target.password is not None
        or target.fragment
    ):
        raise ValueError("Bridge health accepts only http://127.0.0.1:<port> URLs")
    return build_opener(ProxyHandler({}), _RejectRedirects()).open(request, timeout=timeout)


def _is_connection_refused(error: BaseException) -> bool:
    refused_codes = {errno.ECONNREFUSED, int(getattr(errno, "WSAECONNREFUSED", 10061))}
    current: object = error
    for _ in range(4):
        if isinstance(current, ConnectionRefusedError):
            return True
        if isinstance(current, OSError) and (
            current.errno in refused_codes or getattr(current, "winerror", None) in refused_codes
        ):
            return True
        current = getattr(current, "reason", None)
        if current is None:
            return False
    return False


def _loopback_connection_refused(url: str, *, timeout: float) -> bool:
    """Confirm an exact loopback TCP refusal when urllib hides the OS error."""

    try:
        parsed = urlsplit(url)
        if parsed.hostname != LOOPBACK_HOST or parsed.port is None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.setblocking(False)
            result = connection.connect_ex((LOOPBACK_HOST, parsed.port))
            if _is_connection_refused(OSError(result, "loopback connection failed")):
                return True
            pending = {
                errno.EINPROGRESS,
                errno.EWOULDBLOCK,
                errno.EALREADY,
                int(getattr(errno, "WSAEWOULDBLOCK", 10035)),
                int(getattr(errno, "WSAEINPROGRESS", 10036)),
                int(getattr(errno, "WSAEALREADY", 10037)),
            }
            if result not in pending:
                return False
            wait = min(0.5, max(timeout, 0.05))
            _, writable, exceptional = select.select([], [connection], [connection], wait)
            final_error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if not writable and not exceptional and final_error == 0:
                return _IS_WINDOWS and _loopback_port_is_unbound(parsed.port)
            return _is_connection_refused(OSError(final_error, "loopback connection failed"))
    except (OSError, ValueError):
        return False


def _loopback_port_is_unbound(port: int) -> bool:
    """Confirm on Windows that no socket owns the exact recorded loopback port."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            _set_windows_exclusive_address_use(probe)
            probe.bind((LOOPBACK_HOST, port))
    except OSError:
        return False
    return True


def _set_windows_exclusive_address_use(target: socket.socket) -> None:
    option = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if _IS_WINDOWS and option is not None:
        target.setsockopt(socket.SOL_SOCKET, option, 1)


def _wait_for_state(
    instance_id: str,
    process: subprocess.Popen[bytes],
    *,
    expected_pid: int | None,
    vault_id: str,
    timeout: float,
) -> BridgeState | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = _load_state()
        except ValidationError:
            state = None
        if (
            state is not None
            and state.instance_id == instance_id
            and (expected_pid is None or state.pid == expected_pid)
            and state.vault_id == vault_id
        ):
            return state
        if expected_pid is not None and process.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _bridge_start_failure(process: subprocess.Popen[bytes], log_path: Path) -> str:
    """Return a bounded startup diagnostic without treating an ephemeral log as evidence."""

    status = process.poll()
    prefix = (
        f"The Bridge process exited with status {status} before becoming healthy."
        if status is not None
        else "The Bridge did not become healthy within 20 seconds."
    )
    try:
        raw = read_regular_file(log_path, label="Bridge startup log", max_bytes=64 * 1024)
        tail = raw.decode("utf-8", errors="replace")[-2_000:]
        safe = " ".join(tail.split())
    except ValidationError:
        safe = ""
    return f"{prefix} Startup output: {safe}" if safe else f"{prefix} See {log_path}"


def _state_matches_health(state: BridgeState, health: dict[str, Any] | None) -> bool:
    if health is None:
        return False
    return (
        health.get("service") == "gsv-bridge"
        and health.get("instance_id") == state.instance_id
        and health.get("pid") == state.pid
        and health.get("port") == state.port
        and health.get("vault_id") == state.vault_id
        and _state_url_matches(state)
    )


def _health_matches_vault(health: dict[str, Any] | None, vault: Vault) -> bool:
    """Bind reuse/browser launch to the server's captured root, not only its path."""

    if health is None:
        return False
    try:
        metadata = os.lstat(vault.root)
        vault_id = str(vault.identity()["vault_id"])
    except (ContinuityError, OSError, KeyError, TypeError):
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and health.get("vault_id") == vault_id
        and health.get("vault_root_device") == int(metadata.st_dev)
        and health.get("vault_root_inode") == int(metadata.st_ino)
    )


def _state_url_matches(state: BridgeState) -> bool:
    try:
        parsed = urlsplit(state.url)
        return (
            parsed.scheme == "http"
            and parsed.hostname == LOOPBACK_HOST
            and parsed.port == state.port
            and parsed.path == "/"
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _open_url(state: BridgeState) -> str:
    return f"{state.url}#token={state.token}"


def _public_state(state: BridgeState) -> dict[str, Any]:
    return state.payload(include_token=False)


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
        waited, _ = os.waitpid(pid, int(getattr(os, "WNOHANG", 0)))
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


def _bridge_command(vault: Vault, *, instance_id: str) -> list[str]:
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--vault", str(vault.root), "bridge", "serve"]
    else:
        command = [
            sys.executable,
            "-m",
            "continuity_kernel.bridge_worker",
            "--vault",
            str(vault.root),
        ]
    return [*command, "--port", "0", "--instance-id", instance_id]


def _child_detaches_after_spawn() -> bool:
    return sys.platform == "darwin" and not getattr(sys, "frozen", False)


def _bridge_child_environment(vault: Vault) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("GSV_BRIDGE_DETACH_IN_CHILD", None)
    environment["GSV_VAULT"] = str(vault.root)
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    elif _child_detaches_after_spawn():
        environment["GSV_BRIDGE_DETACH_IN_CHILD"] = "1"
    return environment


def _private_runtime_dir() -> Path:
    root = data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise SetupError(f"The Bridge data path is not a private directory: {root}")
        if sys.platform != "win32":
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
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SetupError(f"Refusing unsafe Bridge log path: {path}")
            if sys.platform != "win32":
                os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "ab")
        except Exception:
            os.close(descriptor)
            raise
    except OSError as exc:
        raise SetupError(f"Could not open the private Bridge log {path}: {exc}") from exc
