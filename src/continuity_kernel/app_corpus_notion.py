"""Read Notion page bodies through a host-local desktop-session bridge.

The public kernel never opens Notion's desktop cache, keychain, cookies, or private
API.  A separately installed local bridge owns that platform-specific work.  The
bridge receives one explicitly pinned account/workspace route, resolves raw provider
identifiers only in its process, and returns bounded page bodies with opaque document
and checkpoint references.

The host configuration is intentionally not a Seld vault record.  It contains no
session material, is a 0600 regular file inside a 0700 directory, and names either a
local executable or a local Python module.  Its account and workspace pins use SHA-256
fingerprints, with labels only for local display.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from continuity_kernel.app_corpus import AppCorpusDocument, AppCorpusSyncResult
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.errors import ValidationError

CONFIG_SCHEMA: Final = "seld.notion-desktop-session.corpus.v1"
REQUEST_SCHEMA: Final = "seld.notion-desktop-session.request.v1"
RESULT_SCHEMA: Final = "seld.notion-desktop-session.result.v1"
MAX_CONFIG_BYTES: Final = 64 * 1024
MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024
MAX_DOCUMENTS_PER_SYNC: Final = 100
MAX_DOCUMENT_TEXT_CHARS: Final = 240_000
MAX_CHECKPOINT_CHARS: Final = 4_096
MAX_ROUTES: Final = 32

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPAQUE_REF = re.compile(r"^notion:sha256:[0-9a-f]{64}$")
_OPAQUE_CHECKPOINT = re.compile(r"^[A-Za-z0-9._~-]{1,4096}$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_GAP_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DISCOVERY_COVERAGE = frozenset({"live_workspace_root_pages", "live_workspace_reachable_pages"})

BridgeRunner = Callable[[Sequence[str], bytes, float], subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True)
class NotionCorpusPin:
    """One owner-selected account/workspace route, represented without raw IDs."""

    account_fingerprint: str
    account_label: str
    workspace_fingerprint: str
    workspace_label: str

    def wire(self) -> dict[str, str]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "account_label": self.account_label,
            "workspace_fingerprint": self.workspace_fingerprint,
            "workspace_label": self.workspace_label,
        }


@dataclass(frozen=True)
class NotionCorpusRoute:
    """A corpus connection whose access is constrained to exactly one Notion pin."""

    connection_id: str
    pin: NotionCorpusPin


@dataclass(frozen=True)
class _HostConfiguration:
    command: tuple[str, ...]
    routes: tuple[NotionCorpusRoute, ...]

    def route(self, connection_id: str) -> NotionCorpusRoute:
        for route in self.routes:
            if route.connection_id == connection_id:
                return route
        raise ValidationError(
            "Notion corpus connection is not configured as an explicit workspace pin"
        )


class NotionAppCorpusAdapter:
    """An :class:`AppCorpusAdapter` for one host-configured Notion desktop bridge.

    ``runtime`` is intentionally retained in the constructor so app-corpus factories
    can use a uniform construction shape.  This adapter never asks the connector
    runtime to resolve a credential: Notion desktop-session custody stays inside the
    configured host bridge.
    """

    def __init__(
        self,
        runtime: ConnectorRuntime,
        *,
        config_path: Path | str,
        runner: BridgeRunner | None = None,
        timeout_seconds: float = 90,
    ) -> None:
        if not isinstance(config_path, (Path, str)):
            raise ValidationError("Notion desktop-session config path is invalid")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise ValidationError("Notion desktop-session timeout is invalid")
        if not 1 <= float(timeout_seconds) <= 180:
            raise ValidationError(
                "Notion desktop-session timeout must be between 1 and 180 seconds"
            )
        self._runtime = runtime
        self._config_path = Path(config_path).expanduser().absolute()
        self._runner = runner or _run_bridge
        self._timeout_seconds = float(timeout_seconds)

    def configured_connections(self) -> tuple[str, ...]:
        """Return the explicitly configured corpus connection IDs, never discovered routes."""

        return tuple(route.connection_id for route in _load_configuration(self._config_path).routes)

    def sync(
        self,
        connection_id: str,
        *,
        checkpoint: str | None = None,
        limit: int = 100,
    ) -> AppCorpusSyncResult:
        if not isinstance(connection_id, str) or _CONNECTION_ID.fullmatch(connection_id) is None:
            raise ValidationError("Notion corpus connection ID is invalid")
        if checkpoint is not None and (
            not isinstance(checkpoint, str) or _OPAQUE_CHECKPOINT.fullmatch(checkpoint) is None
        ):
            return _failure(
                checkpoint=None, status="error", detail="saved Notion checkpoint is invalid"
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValidationError("Notion corpus sync limit must be between 1 and 1000")
        bounded_limit = min(limit, MAX_DOCUMENTS_PER_SYNC)

        configuration = _load_configuration(self._config_path)
        route = configuration.route(connection_id)
        request = {
            "checkpoint": checkpoint,
            "connection_id": connection_id,
            "limit": bounded_limit,
            "operation": "sync_page_bodies",
            "pin": route.pin.wire(),
            "schema": REQUEST_SCHEMA,
        }
        try:
            completed = self._runner(
                configuration.command,
                json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8"),
                self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _failure(
                checkpoint=checkpoint,
                status="error",
                detail=(
                    "Notion desktop-session bridge did not complete; "
                    "the saved checkpoint remains retryable"
                ),
            )
        if completed.returncode != 0:
            return _failure(
                checkpoint=checkpoint,
                status="refused",
                detail="Notion desktop-session bridge refused the bounded read",
            )
        if len(completed.stdout) > MAX_RESPONSE_BYTES:
            return _failure(
                checkpoint=checkpoint,
                status="error",
                detail="Notion desktop-session bridge exceeded the bounded response size",
            )
        try:
            payload = json.loads(completed.stdout.decode("utf-8"))
            return _parse_result(payload, connection_id=connection_id, expected_pin=route.pin)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return _failure(
                checkpoint=checkpoint,
                status="refused",
                detail="Notion desktop-session bridge returned an invalid bounded result",
            )


def notion_app_corpus_adapters(
    runtime: ConnectorRuntime,
    *,
    config_path: Path | str,
) -> dict[str, NotionAppCorpusAdapter]:
    """Build the explicitly configured Notion corpus aliases.

    There is no default path.  A caller must deliberately provide the private host
    configuration, so a fresh install never discovers or reads a desktop session.
    """

    adapter = NotionAppCorpusAdapter(runtime, config_path=config_path)
    return {"notion": adapter, "notion_desktop_session": adapter}


def _run_bridge(
    command: Sequence[str], payload: bytes, timeout_seconds: float
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        check=False,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout_seconds,
    )


def _load_configuration(path: Path) -> _HostConfiguration:
    raw = _read_secure_config(path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Notion desktop-session config is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"backend", "routes", "schema"}:
        raise ValidationError("Notion desktop-session config has an invalid shape")
    if value.get("schema") != CONFIG_SCHEMA:
        raise ValidationError("Notion desktop-session config schema is unsupported")
    _reject_secret_fields(value)
    command = _backend_command(value.get("backend"))
    raw_routes = value.get("routes")
    if not isinstance(raw_routes, list) or not 1 <= len(raw_routes) <= MAX_ROUTES:
        raise ValidationError(
            "Notion desktop-session config must contain between 1 and 32 pinned routes"
        )
    routes = tuple(_route(item) for item in raw_routes)
    if len({route.connection_id for route in routes}) != len(routes):
        raise ValidationError("Notion desktop-session config has duplicate connection IDs")
    if len(
        {(route.pin.account_fingerprint, route.pin.workspace_fingerprint) for route in routes}
    ) != len(routes):
        raise ValidationError("Notion desktop-session config has duplicate account/workspace pins")
    return _HostConfiguration(command=command, routes=routes)


def _read_secure_config(path: Path) -> bytes:
    if not path.is_absolute():
        raise ValidationError("Notion desktop-session config path must be absolute")
    parent = path.parent
    try:
        directory_status = parent.lstat()
    except OSError as exc:
        raise ValidationError("Notion desktop-session config directory is unavailable") from exc
    if (
        not stat.S_ISDIR(directory_status.st_mode)
        or stat.S_IMODE(directory_status.st_mode) != 0o700
    ):
        raise ValidationError("Notion desktop-session config directory must have mode 0700")
    _check_owner(directory_status, "Notion desktop-session config directory")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValidationError("Notion desktop-session config is unavailable") from exc
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode) or stat.S_IMODE(file_status.st_mode) != 0o600:
            raise ValidationError("Notion desktop-session config must be a mode 0600 regular file")
        _check_owner(file_status, "Notion desktop-session config")
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(result) > MAX_CONFIG_BYTES:
        raise ValidationError("Notion desktop-session config exceeds its size limit")
    return result


def _check_owner(status: os.stat_result, label: str) -> None:
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise ValidationError(f"{label} must be owned by the current user")


def _reject_secret_fields(value: object) -> None:
    forbidden = {"authorization", "cookie", "credential", "password", "secret", "token"}
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValidationError("Notion desktop-session config has an invalid key")
            if key.casefold() in forbidden or key.casefold().endswith("_id"):
                raise ValidationError(
                    "Notion desktop-session config may not contain session material or raw IDs"
                )
            _reject_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_fields(child)
    elif isinstance(value, str):
        lowered = value.casefold()
        if "token=" in lowered or "cookie=" in lowered or "authorization:" in lowered:
            raise ValidationError("Notion desktop-session config may not contain session material")


def _backend_command(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        raise ValidationError("Notion desktop-session backend is invalid")
    if set(value) == {"command"}:
        command = value["command"]
        if not isinstance(command, list) or not 1 <= len(command) <= 12:
            raise ValidationError("Notion desktop-session backend command is invalid")
        result = tuple(_command_part(item) for item in command)
    elif set(value) == {"module", "python"}:
        module = value["module"]
        python = value["python"]
        if not isinstance(module, str) or _MODULE.fullmatch(module) is None:
            raise ValidationError("Notion desktop-session backend module is invalid")
        result = (_executable(python), "-m", module)
    else:
        raise ValidationError("Notion desktop-session backend must be a command or module")
    _ensure_private_executable(result[0])
    return result


def _command_part(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        raise ValidationError("Notion desktop-session backend command is invalid")
    return _executable(value) if value.startswith("/") else value


def _executable(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValidationError("Notion desktop-session backend executable must be an absolute path")
    return value


def _ensure_private_executable(value: str) -> None:
    executable = Path(value)
    try:
        status = executable.stat()
    except OSError as exc:
        raise ValidationError("Notion desktop-session backend executable is unavailable") from exc
    if not stat.S_ISREG(status.st_mode) or not os.access(executable, os.X_OK):
        raise ValidationError("Notion desktop-session backend executable is not executable")
    if stat.S_IMODE(status.st_mode) & 0o022:
        raise ValidationError(
            "Notion desktop-session backend executable may not be group or world writable"
        )


def _route(value: object) -> NotionCorpusRoute:
    if not isinstance(value, dict) or set(value) != {"connection", "pin"}:
        raise ValidationError("Notion desktop-session route is invalid")
    connection_id = value.get("connection")
    if not isinstance(connection_id, str) or _CONNECTION_ID.fullmatch(connection_id) is None:
        raise ValidationError("Notion desktop-session route connection is invalid")
    pin = _pin(value.get("pin"))
    return NotionCorpusRoute(connection_id=connection_id, pin=pin)


def _pin(value: object) -> NotionCorpusPin:
    expected = {
        "account_fingerprint",
        "account_label",
        "workspace_fingerprint",
        "workspace_label",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationError("Notion desktop-session route pin is invalid")
    account_fingerprint = value.get("account_fingerprint")
    workspace_fingerprint = value.get("workspace_fingerprint")
    account_label = _label(value.get("account_label"), "account")
    workspace_label = _label(value.get("workspace_label"), "workspace")
    if (
        not isinstance(account_fingerprint, str)
        or _FINGERPRINT.fullmatch(account_fingerprint) is None
    ):
        raise ValidationError("Notion desktop-session account fingerprint is invalid")
    if (
        not isinstance(workspace_fingerprint, str)
        or _FINGERPRINT.fullmatch(workspace_fingerprint) is None
    ):
        raise ValidationError("Notion desktop-session workspace fingerprint is invalid")
    return NotionCorpusPin(
        account_fingerprint=account_fingerprint,
        account_label=account_label,
        workspace_fingerprint=workspace_fingerprint,
        workspace_label=workspace_label,
    )


def _label(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"Notion desktop-session {kind} label is invalid")
    clean = " ".join(value.split())
    if not clean or "\x00" in clean or len(clean) > 512:
        raise ValidationError(f"Notion desktop-session {kind} label is invalid")
    return clean


def _parse_result(
    value: object,
    *,
    connection_id: str,
    expected_pin: NotionCorpusPin,
) -> AppCorpusSyncResult:
    if not isinstance(value, dict) or set(value) != {
        "checkpoint",
        "complete",
        "documents",
        "freshness",
        "pin",
        "scanned",
        "schema",
    }:
        raise ValidationError("Notion desktop-session result shape is invalid")
    if value.get("schema") != RESULT_SCHEMA:
        raise ValidationError("Notion desktop-session result schema is unsupported")
    _validate_pin(value.get("pin"), expected_pin)
    complete = value.get("complete")
    scanned = value.get("scanned")
    if not isinstance(complete, bool) or type(scanned) is not int or scanned < 0:
        raise ValidationError("Notion desktop-session result progress is invalid")
    checkpoint = _checkpoint(value.get("checkpoint"))
    if not complete and checkpoint is None:
        raise ValidationError("partial Notion corpus result must provide an opaque checkpoint")

    documents_value = value.get("documents")
    if not isinstance(documents_value, list) or len(documents_value) > MAX_DOCUMENTS_PER_SYNC:
        raise ValidationError("Notion desktop-session result documents are invalid")
    documents = tuple(_document(item, connection_id=connection_id) for item in documents_value)
    if len({document.object_id for document in documents}) != len(documents):
        raise ValidationError("Notion desktop-session result has duplicate documents")

    freshness = _freshness(
        value.get("freshness"), complete=complete, documents=len(documents), scanned=scanned
    )
    return AppCorpusSyncResult(
        documents=documents,
        checkpoint=checkpoint,
        scanned=scanned,
        complete=complete,
        freshness=freshness,
    )


def _validate_pin(value: object, expected: NotionCorpusPin) -> None:
    if not isinstance(value, dict) or set(value) != {
        "account_fingerprint",
        "workspace_fingerprint",
    }:
        raise ValidationError("Notion desktop-session result pin is invalid")
    if (
        value.get("account_fingerprint") != expected.account_fingerprint
        or value.get("workspace_fingerprint") != expected.workspace_fingerprint
    ):
        raise ValidationError("Notion desktop-session bridge did not validate the configured pin")


def _checkpoint(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _OPAQUE_CHECKPOINT.fullmatch(value) is None:
        raise ValidationError("Notion desktop-session result checkpoint is invalid")
    return value


def _document(value: object, *, connection_id: str) -> AppCorpusDocument:
    expected = {"fetched_at", "metadata", "object_id", "revision", "source_ref", "text", "title"}
    if (
        not isinstance(value, dict)
        or (
            set(value) != expected
            and set(value) != expected | {"deleted"}
            and set(value) != expected | {"source_event_at"}
            and set(value) != expected | {"deleted", "source_event_at"}
        )
        or ("deleted" in value and not isinstance(value["deleted"], bool))
    ):
        raise ValidationError("Notion desktop-session document is invalid")
    deleted = value.get("deleted", False)
    object_id = value.get("object_id")
    source_ref = value.get("source_ref")
    revision = value.get("revision")
    fetched_at = value.get("fetched_at")
    title = value.get("title")
    text = value.get("text")
    if not isinstance(object_id, str) or _OPAQUE_REF.fullmatch(object_id) is None:
        raise ValidationError("Notion desktop-session document must use an opaque object reference")
    if not isinstance(source_ref, str) or source_ref != object_id:
        raise ValidationError("Notion desktop-session document must use an opaque source reference")
    if not isinstance(revision, str) or not revision or len(revision) > 512:
        raise ValidationError("Notion desktop-session document revision is invalid")
    if not isinstance(fetched_at, str) or not fetched_at or len(fetched_at) > 128:
        raise ValidationError("Notion desktop-session document fetched time is invalid")
    if (
        not isinstance(title, str)
        or (not deleted and not title.strip())
        or "\x00" in title
        or len(title) > 8_192
    ):
        raise ValidationError("Notion desktop-session document title is invalid")
    if not isinstance(text, str) or "\x00" in text or len(text) > MAX_DOCUMENT_TEXT_CHARS:
        raise ValidationError("Notion desktop-session document text is invalid")
    metadata = _metadata(value.get("metadata"))
    source_event_at = _source_event_at(value.get("source_event_at"))
    if source_event_at is None:
        if "updated_at" in metadata:
            raise ValidationError(
                "Notion desktop-session document update time requires a source event time"
            )
    elif metadata.get("updated_at") != source_event_at:
        raise ValidationError(
            "Notion desktop-session document update time must match its source event time"
        )
    return AppCorpusDocument(
        connection_id=connection_id,
        provider="notion",
        object_id=object_id,
        revision=revision,
        fetched_at=fetched_at,
        source_ref=source_ref,
        title=title,
        text=text,
        metadata=metadata,
        freshness={},
        deleted=deleted,
        source_event_at=source_event_at,
    )


def _metadata(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "account_label",
        "updated_at",
        "workspace_label",
    }:
        raise ValidationError("Notion desktop-session document metadata is invalid")
    result: dict[str, str] = {}
    for key in ("account_label", "workspace_label"):
        item = value.get(key)
        if item is not None:
            result[key] = _label(item, key.replace("_", " "))
    updated_at = _source_event_at(value.get("updated_at"))
    if updated_at is not None:
        result["updated_at"] = updated_at
    return result


def _source_event_at(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 128:
        raise ValidationError("Notion desktop-session source event time is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValidationError("Notion desktop-session source event time is invalid") from exc
    if parsed.tzinfo is None or not 2000 <= parsed.year <= 2100:
        raise ValidationError("Notion desktop-session source event time is invalid")
    return value


def _freshness(value: object, *, complete: bool, documents: int, scanned: int) -> dict[str, Any]:
    expected = {
        "block_traversal",
        "cache_coverage",
        "discovery_coverage",
        "gaps",
        "live_page_coverage",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValidationError("Notion desktop-session freshness is invalid")
    if value.get("cache_coverage") != "desktop-cache-subset":
        raise ValidationError(
            "Notion desktop-session result must distinguish desktop-cache coverage"
        )
    discovery_coverage = value.get("discovery_coverage")
    if discovery_coverage not in _DISCOVERY_COVERAGE:
        raise ValidationError("Notion desktop-session result must report live workspace discovery")
    if value.get("live_page_coverage") != "page_bodies":
        raise ValidationError("Notion desktop-session result must report live page-body coverage")
    blocks = value.get("block_traversal")
    if not isinstance(blocks, dict) or set(blocks) != {"pagination_complete", "recursive"}:
        raise ValidationError("Notion desktop-session block traversal evidence is invalid")
    recursive = blocks.get("recursive")
    pagination_complete = blocks.get("pagination_complete")
    if (
        not isinstance(recursive, bool)
        or not isinstance(pagination_complete, bool)
        or not recursive
    ):
        raise ValidationError("Notion desktop-session result does not prove recursive block reads")
    gaps = _gaps(value.get("gaps"))
    gap_count = sum(gaps.values())
    if scanned < documents:
        raise ValidationError(
            "Notion desktop-session scanned pages are fewer than returned documents"
        )
    if complete and not pagination_complete:
        raise ValidationError("Notion desktop-session block pagination is incomplete")
    status = "complete" if complete and gap_count == 0 else "partial"
    return {
        "block_traversal": {
            "pagination_complete": pagination_complete,
            "recursive": recursive,
        },
        "cache_coverage": "desktop-cache-subset",
        "discovery_coverage": discovery_coverage,
        "gaps": gaps,
        "live_page_coverage": "page_bodies",
        "status": status,
    }


def _gaps(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > 32:
        raise ValidationError("Notion desktop-session gap counts are invalid")
    result: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _GAP_NAME.fullmatch(key) is None:
            raise ValidationError("Notion desktop-session gap counts are invalid")
        if type(item) is not int or item < 0:
            raise ValidationError("Notion desktop-session gap counts are invalid")
        result[key] = item
    return result


def _failure(*, checkpoint: str | None, status: str, detail: str) -> AppCorpusSyncResult:
    return AppCorpusSyncResult(
        documents=(),
        checkpoint=checkpoint,
        scanned=0,
        complete=False,
        freshness={
            "cache_coverage": "desktop-cache-subset",
            "detail": detail,
            "discovery_coverage": "unavailable",
            "gaps": {},
            "live_page_coverage": "unavailable",
            "status": status,
        },
    )
