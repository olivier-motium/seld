"""First-class, receipt-before-checkpoint bridge for the Discord companion.

The companion owns its one private provider cursor. Seld owns only a host-local
runtime binding and the portable, content-free source receipt. Tokens, channel
IDs, acknowledgement tokens, and provider bodies never enter either binding or
the vault.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from continuity_kernel.atomic import (
    atomic_write,
    durable_unlink,
    exclusive_lock,
    read_regular_file,
    sha256_bytes,
    sha256_regular_file,
)
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError
from continuity_kernel.source_state import source_fingerprint
from continuity_kernel.vault import Vault

SOURCE_ID: Final = "discord"
RECIPE_VERSION: Final = "2"
TOOL_BINDING: Final = "seld-discord-source:readonly:v2"
UNAVAILABLE_TOOL_BINDING: Final = "seld-discord-source:unavailable"
MCP_PROTOCOL_VERSION: Final = "2025-03-26"
USER_TOKEN_TERMS_WARNING: Final = (
    "Discord forbids normal-user self-bot automation and may terminate the account; "
    "GET-only access does not remove that risk."
)
EXPECTED_TOOLS: Final = (
    "discord_acknowledge_messages",
    "discord_poll_messages",
    "discord_source_status",
)
BINDING_VERSION: Final = 1
ABSENT_BINDING_REVISION: Final = "absent"
MAX_BINDING_BYTES: Final = 32 * 1024
MAX_RUNTIME_BYTES: Final = 32 * 1024 * 1024
MAX_INTERPRETER_BYTES: Final = 512 * 1024 * 1024
MAX_MCP_OUTPUT_BYTES: Final = 256 * 1024
# One poll can perform identity plus five sequential channel GETs. The
# companion caps each request at five seconds and permits one provider-directed
# retry of at most two seconds, so the host bound must cover the 72-second
# legitimate worst case while still terminating a wedged runtime.
MCP_TIMEOUT_SECONDS: Final = 80
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^(?:absent|[0-9a-f]{64})$")
_ACK_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT = re.compile(r"^discord-(?:user|bot)-v1:sha256:[0-9a-f]{64}$")
_POSIX_OS = cast(Any, os)
_POSIX_SIGNAL = cast(Any, signal)
_ERROR_CODES: Final = frozenset(
    {
        "auth_required",
        "identity_mismatch",
        "permission_denied",
        "policy_blocked",
        "provider_unavailable",
        "rate_limited",
        "read_failed",
    }
)


@dataclass(frozen=True)
class _FileIdentity:
    path: Path
    device: int
    inode: int
    size: int
    digest: str


@dataclass(frozen=True)
class _RuntimeBinding:
    vault_id: str
    vault_root_digest: str
    runtime: _FileIdentity
    interpreter: _FileIdentity | None
    inventory_digest: str


class DiscordSourceBridge:
    """Invoke one exact local companion without sharing provider authority."""

    def __init__(self, vault: Vault):
        self.vault = vault
        identity = vault.identity()
        self.vault_id = _required_text(identity.get("vault_id"), "vault identity", 128)
        self.vault_root_digest = _digest(str(vault.root))
        self.key = sha256_bytes(f"{self.vault_id}\0{self.vault_root_digest}".encode())[:32]
        self.root = data_dir() / "discord-source"
        self.binding_path = self.root / "bindings" / f"{self.key}.json"
        self.state_path = self.root / "state" / f"{self.key}.json"
        self.lock_path = self.root / "locks" / f"{self.key}.lock"

    def binding_status(self) -> dict[str, Any]:
        with exclusive_lock(self.lock_path):
            binding, encoded = self._load_binding(required=False)
            if binding is None:
                return {
                    "bound": False,
                    "bindingRevision": ABSENT_BINDING_REVISION,
                    "runtimeCurrent": False,
                }
            current = True
            try:
                self._verify_runtime(binding)
            except ValidationError:
                current = False
            return {
                "bound": True,
                "bindingRevision": sha256_bytes(encoded),
                "runtimeCurrent": current,
                "runtimeDigest": binding.runtime.digest,
                "inventoryDigest": binding.inventory_digest,
            }

    def bind(self, runtime: Path | str, *, expected_revision: str) -> dict[str, Any]:
        _binding_revision(expected_revision)
        with exclusive_lock(self.lock_path):
            _prior, prior_bytes = self._load_binding(required=False)
            current_revision = (
                ABSENT_BINDING_REVISION if not prior_bytes else sha256_bytes(prior_bytes)
            )
            if current_revision != expected_revision:
                raise ConflictError("Discord runtime binding changed; reload binding status")
            snapshot, interpreter = _runtime_snapshot(Path(runtime))
            with tempfile.TemporaryDirectory(prefix="seld-discord-bind-") as temporary:
                inventory, _ = _run_mcp(
                    snapshot,
                    interpreter,
                    state_path=Path(temporary) / "state.json",
                    tool=None,
                    arguments={},
                    provider_environment=_synthetic_provider_environment(),
                )
            inventory_digest = _inventory_digest(inventory)
            binding = _RuntimeBinding(
                vault_id=self.vault_id,
                vault_root_digest=self.vault_root_digest,
                runtime=snapshot,
                interpreter=interpreter,
                inventory_digest=inventory_digest,
            )
            encoded = _binding_bytes(binding)
            self.binding_path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                self.binding_path.parent.chmod(0o700)
            atomic_write(self.binding_path, encoded)
            return {
                "bound": True,
                "bindingRevision": sha256_bytes(encoded),
                "runtimeCurrent": True,
                "runtimeDigest": snapshot.digest,
                "inventoryDigest": inventory_digest,
            }

    def unbind(self, *, expected_revision: str) -> dict[str, Any]:
        _binding_revision(expected_revision)
        with exclusive_lock(self.lock_path):
            binding, encoded = self._load_binding(required=False)
            current_revision = ABSENT_BINDING_REVISION if binding is None else sha256_bytes(encoded)
            if current_revision != expected_revision:
                raise ConflictError("Discord runtime binding changed; reload binding status")
            if binding is None:
                return {"bound": False, "bindingRevision": ABSENT_BINDING_REVISION}
            durable_unlink(self.binding_path)
            return {"bound": False, "bindingRevision": ABSENT_BINDING_REVISION}

    def status(self) -> dict[str, Any]:
        snapshot = self.vault.get_source_snapshot()
        binding_status = self.binding_status()
        base: dict[str, Any] = {
            "source": SOURCE_ID,
            "selected": SOURCE_ID in snapshot.selected_sources,
            "sourceRevision": snapshot.revision,
            "binding": binding_status,
        }
        if not binding_status["bound"]:
            return {**base, "available": False, "errorCode": "tool_absent"}
        if not base["selected"]:
            return {**base, "available": False, "errorCode": "policy_blocked"}
        provider, error = _provider_environment(required=False)
        if error is not None:
            return {**base, "available": False, "errorCode": error}
        try:
            payload, bound_tool = self._invoke("discord_source_status", {}, provider)
            projected = _status_projection(payload)
            _promote_bound_tool(projected, bound_tool)
        except ContinuityError:
            return {**base, "available": False, "errorCode": "tool_error"}
        return {**base, **projected}

    def poll(self, *, limit: int = 5, max_content_chars: int = 280) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValidationError("Discord poll limit must be between 1 and 25")
        if (
            isinstance(max_content_chars, bool)
            or not isinstance(max_content_chars, int)
            or not 0 <= max_content_chars <= 500
        ):
            raise ValidationError("Discord preview bound must be between 0 and 500")
        provider, error = _provider_environment(required=True)
        assert error is None
        with exclusive_lock(self.vault.state / "locks/global.lock"):
            snapshot = self._selected_snapshot()
            payload, bound_tool = self._invoke(
                "discord_poll_messages",
                {"limit": limit, "maxContentChars": max_content_chars},
                provider,
            )
            if self.vault.get_source_snapshot().revision != snapshot.revision:
                raise ConflictError(
                    "source selection changed during Discord poll; retry from fresh source state"
                )
            projected = _poll_projection(
                payload,
                limit=limit,
                max_content_chars=max_content_chars,
            )
            _promote_bound_tool(projected, bound_tool)
            projected["sourceRevision"] = snapshot.revision
            if projected["result"] == "failure":
                projected["record"] = {
                    "source": SOURCE_ID,
                    "result": "failure",
                    "errorCode": projected["errorCode"],
                    "toolBinding": projected["toolBinding"],
                }
            else:
                projected["record"] = {
                    "source": SOURCE_ID,
                    "result": projected["result"],
                    "coveredThrough": projected["coveredThrough"],
                    "completeness": projected["completeness"],
                    "accountBinding": projected["accountBinding"],
                    "toolBinding": projected["toolBinding"],
                    "cursor": projected["cursorBinding"],
                    "evidenceRefs": projected["evidenceRefs"],
                }
            return projected

    def acknowledge(self, *, ack_token: str, expected_source_revision: str) -> dict[str, Any]:
        if _ACK_TOKEN.fullmatch(ack_token) is None:
            raise ValidationError("Discord acknowledgement token is invalid")
        _binding_revision(expected_source_revision)
        provider, error = _provider_environment(required=True)
        assert error is None
        with exclusive_lock(self.vault.state / "locks/global.lock"):
            snapshot = self._selected_snapshot()
            if snapshot.revision != expected_source_revision:
                raise ConflictError("source state changed before Discord acknowledgement")
            observation = snapshot.observation(SOURCE_ID)
            if observation is None:
                raise ConflictError(
                    "record the matching Discord source receipt before acknowledging"
                )
            status_payload, bound_tool = self._invoke("discord_source_status", {}, provider)
            status = _status_projection(status_payload)
            _promote_bound_tool(status, bound_tool)
            if not status["available"]:
                raise ContinuityError("Discord source identity is unavailable for acknowledgement")
            checkpoint = status["checkpoint"]
            pending = bool(checkpoint["pending"])
            self._verify_receipt(observation, status, pending=pending)
            if self.vault.get_source_snapshot().revision != expected_source_revision:
                raise ConflictError("source state changed during Discord acknowledgement")
            ack_payload, _ack_bound_tool = self._invoke(
                "discord_acknowledge_messages",
                {"ackToken": ack_token},
                provider,
                expected_bound_tool=bound_tool,
            )
            result = _ack_projection(ack_payload)
            committed = result["checkpoint"]
            if (
                committed["accountBinding"] != status["accountBinding"]
                or committed["channelSetDigest"] != status["configuredChannelSetDigest"]
            ):
                raise ValidationError(
                    "Discord acknowledgement changed its account or channel confinement"
                )
            self._verify_receipt(
                observation,
                {**status, "checkpoint": committed},
                pending=False,
            )
            if not pending and not result["alreadyAcknowledged"]:
                raise ConflictError(
                    "Discord committed checkpoint did not match an idempotent retry"
                )
            return {**result, "sourceRevision": expected_source_revision}

    def _verify_receipt(self, observation: Any, status: dict[str, Any], *, pending: bool) -> None:
        checkpoint = status["checkpoint"]
        result = checkpoint["pendingResult"] if pending else observation.result.value
        completeness = (
            checkpoint["pendingCompleteness"]
            if pending
            else observation.completeness.value
            if observation.completeness
            else None
        )
        covered = checkpoint["pendingCoveredThrough"] if pending else checkpoint["coveredThrough"]
        cursor = checkpoint["pendingCursorBinding"] if pending else checkpoint["cursorBinding"]
        delivery = checkpoint["pendingDeliveryBinding"] if pending else None
        if (
            observation.recipe_version != RECIPE_VERSION
            or observation.result.value != result
            or observation.completeness is None
            or observation.completeness.value != completeness
            or observation.covered_through != covered
            or observation.account_fingerprint
            != source_fingerprint(status["accountBinding"], "Discord account binding")
            or observation.tool_fingerprint
            != source_fingerprint(status["toolBinding"], "Discord tool binding")
            or observation.cursor_digest != source_fingerprint(cursor, "Discord cursor binding")
        ):
            raise ConflictError("portable Discord source receipt does not match the checkpoint")
        if pending:
            expected_evidence = source_fingerprint(delivery, "Discord delivery binding")
            if observation.evidence_digests != (expected_evidence,):
                raise ConflictError("portable Discord receipt does not match the pending delivery")

    def _selected_snapshot(self) -> Any:
        snapshot = self.vault.get_source_snapshot()
        if SOURCE_ID not in snapshot.selected_sources:
            raise ConflictError("Discord is not selected; reload source state and select it first")
        return snapshot

    def _invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        provider_environment: dict[str, str],
        *,
        expected_bound_tool: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        with exclusive_lock(self.lock_path):
            binding, encoded = self._load_binding(required=True)
            assert binding is not None
            self._verify_runtime(binding)
            bound_tool = _bound_tool_binding(sha256_bytes(encoded))
            if expected_bound_tool is not None and bound_tool != expected_bound_tool:
                raise ConflictError("Discord companion binding changed before acknowledgement")
            inventory, payload = _run_mcp(
                binding.runtime,
                binding.interpreter,
                state_path=self.state_path,
                tool=tool,
                arguments=arguments,
                provider_environment=provider_environment,
            )
            if _inventory_digest(inventory) != binding.inventory_digest:
                raise ValidationError("Discord companion tool inventory changed; rebind explicitly")
            if payload is None:
                raise ValidationError("Discord companion returned no tool result")
            return payload, bound_tool

    def _load_binding(self, *, required: bool) -> tuple[_RuntimeBinding | None, bytes]:
        if not os.path.lexists(self.binding_path):
            if required:
                raise ContinuityError("Discord companion is not bound; run gsv discord-source bind")
            return None, b""
        metadata = os.lstat(self.binding_path)
        if os.name != "nt" and (metadata.st_mode & 0o077):
            raise ValidationError("Discord runtime binding must have owner-only permissions")
        encoded = read_regular_file(
            self.binding_path,
            label="Discord runtime binding",
            max_bytes=MAX_BINDING_BYTES,
        )
        return _parse_binding(
            encoded,
            vault_id=self.vault_id,
            vault_root_digest=self.vault_root_digest,
        ), encoded

    @staticmethod
    def _verify_runtime(binding: _RuntimeBinding) -> None:
        current_runtime, current_interpreter = _runtime_snapshot(binding.runtime.path)
        if current_runtime != binding.runtime or current_interpreter != binding.interpreter:
            raise ValidationError("Discord companion runtime changed; rebind explicitly")


def current_discord_tool_fingerprint(vault: Vault) -> str:
    """Return the bound tool fingerprint without invoking Discord."""

    bridge = DiscordSourceBridge(vault)
    try:
        status = bridge.binding_status()
    except ContinuityError:
        status = {"bound": False, "runtimeCurrent": False}
    if not status["bound"] or not status["runtimeCurrent"]:
        return source_fingerprint(UNAVAILABLE_TOOL_BINDING, "Discord tool binding")
    revision = _required_text(status.get("bindingRevision"), "Discord binding revision", 64)
    return source_fingerprint(_bound_tool_binding(revision), "Discord tool binding")


def _bound_tool_binding(binding_revision: str) -> str:
    if _REVISION.fullmatch(binding_revision) is None or binding_revision == "absent":
        raise ValidationError("Discord binding revision is invalid")
    return f"{TOOL_BINDING}:binding:{binding_revision}"


def _promote_bound_tool(projected: dict[str, Any], bound_tool: str) -> None:
    projected["companionToolBinding"] = projected["toolBinding"]
    projected["toolBinding"] = bound_tool


def _runtime_snapshot(path: Path) -> tuple[_FileIdentity, _FileIdentity | None]:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValidationError("Discord runtime path must be absolute")
    try:
        listed = os.lstat(expanded)
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError("Discord runtime is unavailable") from exc
    if resolved != expanded or stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise ValidationError("Discord runtime must be one ordinary non-symlinked file")
    if os.name != "nt" and not (listed.st_mode & 0o111):
        raise ValidationError("Discord runtime is not executable")
    runtime = _file_identity(expanded, listed, max_bytes=MAX_RUNTIME_BYTES, label="Discord runtime")
    first_line = read_regular_file(expanded, label="Discord runtime", max_bytes=MAX_RUNTIME_BYTES)
    interpreter = _shebang_interpreter(first_line.splitlines()[0] if first_line else b"")
    if interpreter is None:
        raise ValidationError("Discord runtime must declare one pinned interpreter")
    return runtime, interpreter


def _shebang_interpreter(first_line: bytes) -> _FileIdentity | None:
    if not first_line.startswith(b"#!"):
        return None
    try:
        command = first_line[2:].decode("utf-8").strip().split()
    except UnicodeError as exc:
        raise ValidationError("Discord runtime has an invalid interpreter declaration") from exc
    if not command:
        raise ValidationError("Discord runtime has an invalid interpreter declaration")
    if command[0] == "/usr/bin/env":
        if len(command) != 2 or command[1].startswith("-"):
            raise ValidationError("Discord runtime interpreter declaration is unsupported")
        located = shutil.which(command[1])
        if not located:
            raise ValidationError("Discord runtime interpreter is unavailable")
        path = Path(located).resolve(strict=True)
    else:
        if len(command) != 1:
            raise ValidationError("Discord runtime interpreter declaration is unsupported")
        path = Path(command[0]).resolve(strict=True)
    metadata = os.lstat(path)
    if (
        not path.is_absolute()
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ValidationError("Discord runtime interpreter is unsafe")
    return _file_identity(
        path,
        metadata,
        max_bytes=MAX_INTERPRETER_BYTES,
        label="Discord runtime interpreter",
    )


def _file_identity(
    path: Path,
    metadata: os.stat_result,
    *,
    max_bytes: int,
    label: str,
) -> _FileIdentity:
    return _FileIdentity(
        path=path,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        digest=f"sha256:{sha256_regular_file(path, label=label, max_bytes=max_bytes)}",
    )


def _open_verified_runtime(identity: _FileIdentity) -> int:
    """Open and hash the bound script once so path replacement cannot change execution."""

    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(identity.path, flags)
    except OSError as exc:
        raise ValidationError("Discord companion runtime changed; rebind explicitly") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or int(before.st_dev) != identity.device
            or int(before.st_ino) != identity.inode
            or int(before.st_size) != identity.size
            or before.st_size > MAX_RUNTIME_BYTES
        ):
            raise ValidationError("Discord companion runtime changed; rebind explicitly")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RUNTIME_BYTES:
                raise ValidationError("Discord companion runtime changed; rebind explicitly")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or total != identity.size
            or f"sha256:{digest.hexdigest()}" != identity.digest
        ):
            raise ValidationError("Discord companion runtime changed; rebind explicitly")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _binding_bytes(binding: _RuntimeBinding) -> bytes:
    payload = {
        "format_version": BINDING_VERSION,
        "inventory_digest": binding.inventory_digest,
        "interpreter": _identity_dict(binding.interpreter),
        "runtime": _identity_dict(binding.runtime),
        "vault_id": binding.vault_id,
        "vault_root_digest": binding.vault_root_digest,
    }
    encoded = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    if len(encoded) > MAX_BINDING_BYTES:
        raise ValidationError("Discord runtime binding exceeds its size bound")
    return encoded


def _identity_dict(value: _FileIdentity | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "device": value.device,
        "digest": value.digest,
        "inode": value.inode,
        "path": str(value.path),
        "size": value.size,
    }


def _parse_binding(
    encoded: bytes,
    *,
    vault_id: str,
    vault_root_digest: str,
) -> _RuntimeBinding:
    try:
        value = json.loads(encoded.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Discord runtime binding is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "format_version",
        "inventory_digest",
        "interpreter",
        "runtime",
        "vault_id",
        "vault_root_digest",
    }:
        raise ValidationError("Discord runtime binding is invalid")
    if (
        value["format_version"] != BINDING_VERSION
        or value["vault_id"] != vault_id
        or value["vault_root_digest"] != vault_root_digest
        or _SHA256.fullmatch(str(value["inventory_digest"])) is None
    ):
        raise ValidationError("Discord runtime binding belongs to a different authority")
    return _RuntimeBinding(
        vault_id=vault_id,
        vault_root_digest=vault_root_digest,
        runtime=_parse_identity(value["runtime"], "runtime"),
        interpreter=(
            None
            if value["interpreter"] is None
            else _parse_identity(value["interpreter"], "interpreter")
        ),
        inventory_digest=str(value["inventory_digest"]),
    )


def _parse_identity(value: object, label: str) -> _FileIdentity:
    if not isinstance(value, dict) or set(value) != {"device", "digest", "inode", "path", "size"}:
        raise ValidationError(f"Discord {label} binding is invalid")
    path = value["path"]
    numbers = (value["device"], value["inode"], value["size"])
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or "\x00" in path
        or len(path) > 4096
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in numbers)
        or not isinstance(value["digest"], str)
        or _SHA256.fullmatch(value["digest"]) is None
    ):
        raise ValidationError(f"Discord {label} binding is invalid")
    return _FileIdentity(
        path=Path(path),
        device=int(value["device"]),
        inode=int(value["inode"]),
        size=int(value["size"]),
        digest=value["digest"],
    )


def _run_mcp(
    runtime: _FileIdentity,
    interpreter: _FileIdentity | None,
    *,
    state_path: Path,
    tool: str | None,
    arguments: dict[str, Any],
    provider_environment: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        state_path.parent.chmod(0o700)
    environment = _child_environment(
        provider_environment,
        state_path=state_path,
        interpreter=interpreter,
    )
    options: dict[str, Any] = {
        "cwd": str(runtime.path.parent),
        "env": environment,
        "stdin": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
    }
    if os.name == "nt":
        options["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        options["start_new_session"] = True
    runtime_descriptor: int | None = None
    command = [str(runtime.path)]
    if interpreter is not None:
        runtime_argument = str(runtime.path)
        if os.name != "nt":
            runtime_descriptor = _open_verified_runtime(runtime)
            runtime_argument = f"/dev/fd/{runtime_descriptor}"
            options["pass_fds"] = (runtime_descriptor,)
        command = [str(interpreter.path), runtime_argument]
    try:
        process = subprocess.Popen(command, **options)
    except OSError as exc:
        raise ContinuityError("Discord companion did not complete its bounded local call") from exc
    try:
        exchange = _MCPExchange(process)
        initialize = _mcp_result(
            exchange.request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "seld-discord-bridge", "version": "2"},
                    },
                },
                response_id=1,
            ),
            "initialize",
        )
        server = initialize.get("serverInfo")
        if (
            initialize.get("protocolVersion") != MCP_PROTOCOL_VERSION
            or not isinstance(server, dict)
            or server.get("name") != "discord-mcp"
        ):
            raise ValidationError("Discord companion identity is invalid")
        exchange.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = _mcp_result(
            exchange.request(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                response_id=2,
            ),
            "tools/list",
        ).get("tools")
        inventory = _validate_inventory(listed)
        payload: dict[str, Any] | None = None
        if tool is not None:
            call = _mcp_result(
                exchange.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                    response_id=3,
                ),
                "tools/call",
            )
            content = call.get("content")
            if (
                not isinstance(content, list)
                or len(content) != 1
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)
            ):
                raise ValidationError("Discord companion returned an unsupported tool result")
            try:
                decoded = json.loads(content[0]["text"])
            except json.JSONDecodeError as exc:
                raise ValidationError("Discord companion returned malformed tool JSON") from exc
            if not isinstance(decoded, dict):
                raise ValidationError("Discord companion returned an unsupported tool payload")
            payload = decoded
        exchange.close(expected_ids={1, 2} | ({3} if tool is not None else set()))
        return inventory, payload
    except BaseException:
        if "exchange" in locals():
            exchange.abort()
        raise
    finally:
        if runtime_descriptor is not None:
            os.close(runtime_descriptor)


class _MCPExchange:
    """One bounded, lifecycle-correct JSON-RPC exchange with a local MCP server."""

    def __init__(self, process: subprocess.Popen[bytes]):
        assert process.stdin is not None and process.stdout is not None
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.deadline = time.monotonic() + MCP_TIMEOUT_SECONDS
        self.condition = threading.Condition()
        self.responses: dict[int, dict[str, Any]] = {}
        self.total = 0
        self.error: Exception | None = None
        self.eof = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        while True:
            line = self.stdout.readline(MAX_MCP_OUTPUT_BYTES + 2)
            with self.condition:
                if not line:
                    self.eof = True
                    self.condition.notify_all()
                    return
                self.total += len(line)
                if self.total > MAX_MCP_OUTPUT_BYTES or not line.endswith(b"\n"):
                    self.error = ValidationError("Discord companion output exceeded its bound")
                    self.condition.notify_all()
                    return
                try:
                    value = json.loads(line.decode("utf-8"))
                    response_id = value.get("id") if isinstance(value, dict) else None
                    if (
                        not isinstance(value, dict)
                        or not set(value).issuperset({"jsonrpc", "id"})
                        or not isinstance(response_id, int)
                        or isinstance(response_id, bool)
                        or response_id in self.responses
                    ):
                        raise ValueError
                except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    self.error = ValidationError("Discord companion returned malformed MCP output")
                    self.error.__cause__ = exc
                    self.condition.notify_all()
                    return
                self.responses[response_id] = value
                self.condition.notify_all()

    def _send(self, message: dict[str, Any]) -> None:
        encoded = (
            json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        )
        try:
            self.stdin.write(encoded)
            self.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ContinuityError("Discord companion rejected its bounded local request") from exc

    def request(self, message: dict[str, Any], *, response_id: int) -> dict[str, Any]:
        self._send(message)
        with self.condition:
            while response_id not in self.responses:
                if self.error is not None:
                    raise self.error
                if self.eof:
                    raise ValidationError("Discord companion returned an incomplete MCP exchange")
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise ContinuityError(
                        "Discord companion did not complete its bounded local call"
                    )
                self.condition.wait(timeout=min(remaining, 0.1))
            return self.responses[response_id]

    def notify(self, message: dict[str, Any]) -> None:
        self._send(message)

    def close(self, *, expected_ids: set[int]) -> None:
        with contextlib.suppress(BrokenPipeError, OSError):
            self.stdin.close()
        while self.process.poll() is None:
            with self.condition:
                if self.error is not None:
                    raise self.error
            if time.monotonic() >= self.deadline:
                raise ContinuityError("Discord companion did not complete its bounded local call")
            time.sleep(0.01)
        self.reader.join(timeout=1.0)
        if self.reader.is_alive():
            raise ContinuityError("Discord companion left its bounded output stream open")
        if self.error is not None:
            raise self.error
        if self.process.returncode != 0:
            raise ContinuityError("Discord companion exited before returning a result")
        if set(self.responses) != expected_ids:
            raise ValidationError("Discord companion returned an incomplete MCP exchange")

    def abort(self) -> None:
        with contextlib.suppress(BrokenPipeError, OSError):
            self.stdin.close()
        _stop_mcp_process(self.process)
        self.reader.join(timeout=1.0)


def _stop_mcp_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError):
            process.terminate()
    else:
        with contextlib.suppress(OSError, ProcessLookupError):
            _POSIX_OS.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=0.25)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    if os.name == "nt":
        with contextlib.suppress(OSError):
            process.kill()
    else:
        with contextlib.suppress(OSError, ProcessLookupError):
            _POSIX_OS.killpg(process.pid, _POSIX_SIGNAL.SIGKILL)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=1.0)


def _mcp_result(response: dict[str, Any], label: str) -> dict[str, Any]:
    if set(response) != {"id", "jsonrpc", "result"} or response.get("jsonrpc") != "2.0":
        raise ValidationError(f"Discord companion {label} response is invalid")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValidationError(f"Discord companion {label} response is invalid")
    return result


def _validate_inventory(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValidationError("Discord companion tool inventory is invalid")
    if len(value) != len(EXPECTED_TOOLS):
        raise ValidationError("Discord companion must expose exactly the three read-source tools")
    inventory = [dict(item) for item in value]
    names = [item.get("name") for item in inventory]
    if not all(isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValidationError("Discord companion tool inventory contains invalid names")
    by_name = {str(item["name"]): item for item in inventory}
    if tuple(sorted(by_name)) != EXPECTED_TOOLS:
        raise ValidationError("Discord companion must expose exactly the three read-source tools")
    status_schema = by_name["discord_source_status"].get("inputSchema")
    poll_schema = by_name["discord_poll_messages"].get("inputSchema")
    ack_schema = by_name["discord_acknowledge_messages"].get("inputSchema")
    if not isinstance(status_schema, dict) or status_schema.get("properties") != {}:
        raise ValidationError("Discord status tool schema changed")
    if not isinstance(poll_schema, dict) or poll_schema.get("additionalProperties") is not False:
        raise ValidationError("Discord poll tool schema changed")
    properties = poll_schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != {
        "channelIds",
        "limit",
        "maxContentChars",
    }:
        raise ValidationError("Discord poll tool schema changed")
    if poll_schema.get("required") not in (None, []):
        raise ValidationError("Discord poll must use only its configured channel allowlist")
    limit = properties["limit"]
    preview = properties["maxContentChars"]
    if (
        not isinstance(limit, dict)
        or (limit.get("minimum"), limit.get("maximum")) != (1, 25)
        or not isinstance(preview, dict)
        or (preview.get("minimum"), preview.get("maximum")) != (0, 500)
    ):
        raise ValidationError("Discord poll bounds changed")
    if (
        not isinstance(ack_schema, dict)
        or ack_schema.get("additionalProperties") is not False
        or ack_schema.get("required") != ["ackToken"]
    ):
        raise ValidationError("Discord acknowledgement tool schema changed")
    return sorted(inventory, key=lambda item: str(item["name"]))


def _inventory_digest(inventory: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        inventory,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{sha256_bytes(encoded)}"


def _status_projection(value: dict[str, Any]) -> dict[str, Any]:
    _source_header(value)
    available = value.get("available")
    if not isinstance(available, bool):
        raise ValidationError("Discord status availability is invalid")
    result: dict[str, Any] = {
        "available": available,
        "attemptedAt": _time(value.get("attemptedAt"), "Discord status attempt"),
        "toolBinding": _exact(value.get("toolBinding"), TOOL_BINDING, "Discord tool binding"),
    }
    if not available:
        result["errorCode"] = _error_code(value.get("errorCode"))
        return result
    account = _required_text(value.get("accountBinding"), "Discord account binding", 256)
    if _ACCOUNT.fullmatch(account) is None:
        raise ValidationError("Discord account binding is invalid")
    count = value.get("configuredChannelCount")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
        raise ValidationError("Discord configured channel count is invalid")
    checkpoint = _checkpoint_projection(value.get("checkpoint"))
    channel_digest = _sha256(value.get("configuredChannelSetDigest"), "Discord channel set binding")
    if checkpoint["accountBinding"] not in (None, account):
        raise ValidationError("Discord checkpoint account does not match current identity")
    if checkpoint["channelSetDigest"] not in (None, channel_digest):
        raise ValidationError("Discord checkpoint channels do not match current confinement")
    auth_type = _one_of(value.get("authType"), {"user", "bot"}, "Discord auth type")
    terms_warning = value.get("termsWarning")
    if (auth_type == "user" and terms_warning != USER_TOKEN_TERMS_WARNING) or (
        auth_type == "bot" and terms_warning is not None
    ):
        raise ValidationError("Discord token-mode terms warning is invalid")
    result.update(
        {
            "authType": auth_type,
            "termsWarning": terms_warning,
            "accountLabel": _optional_text(value.get("accountLabel"), "Discord account label", 64),
            "accountBinding": account,
            "configuredChannelSetDigest": channel_digest,
            "configuredChannelCount": count,
            "checkpoint": checkpoint,
        }
    )
    return result


def _checkpoint_projection(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Discord checkpoint status is invalid")
    pending = value.get("pending")
    if not isinstance(pending, bool):
        raise ValidationError("Discord checkpoint pending flag is invalid")
    projected = {
        "accountBinding": _optional_account(value.get("accountBinding")),
        "channelSetDigest": _optional_sha256(value.get("channelSetDigest")),
        "cursorBinding": _optional_cursor(value.get("cursorBinding")),
        "lastAttempt": _attempt_projection(value.get("lastAttempt")),
        "lastSuccessAt": _optional_time(value.get("lastSuccessAt"), "Discord last success"),
        "coveredThrough": _optional_time(value.get("coveredThrough"), "Discord coverage"),
        "pending": pending,
        "pendingCursorBinding": _optional_cursor(value.get("pendingCursorBinding")),
        "pendingDeliveryBinding": _optional_sha256(value.get("pendingDeliveryBinding")),
        "pendingCoveredThrough": _optional_time(
            value.get("pendingCoveredThrough"), "Discord pending coverage"
        ),
        "pendingResult": _optional_one_of(
            value.get("pendingResult"), {"success", "explicit_empty"}, "Discord pending result"
        ),
        "pendingCompleteness": _optional_one_of(
            value.get("pendingCompleteness"), {"complete", "partial"}, "Discord completeness"
        ),
    }
    fields = (
        "pendingCursorBinding",
        "pendingDeliveryBinding",
        "pendingCoveredThrough",
        "pendingResult",
        "pendingCompleteness",
    )
    present = [projected[field] is not None for field in fields]
    if (pending and not all(present)) or (not pending and any(present)):
        raise ValidationError("Discord pending checkpoint status is incomplete")
    successful_fields = (
        projected["cursorBinding"],
        projected["lastSuccessAt"],
        projected["coveredThrough"],
    )
    if any(item is None for item in successful_fields) and any(
        item is not None for item in successful_fields
    ):
        raise ValidationError("Discord committed checkpoint status is incomplete")
    return projected


def _attempt_projection(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"at", "result", "errorCode"}:
        raise ValidationError("Discord checkpoint attempt is invalid")
    result = _one_of(
        value.get("result"), {"success", "explicit_empty", "failure"}, "Discord attempt result"
    )
    error = value.get("errorCode")
    if (result == "failure" and error is None) or (result != "failure" and error is not None):
        raise ValidationError("Discord checkpoint attempt error is invalid")
    return {
        "at": _time(value.get("at"), "Discord checkpoint attempt"),
        "result": result,
        "errorCode": _error_code(error) if error is not None else None,
    }


def _poll_projection(
    value: dict[str, Any],
    *,
    limit: int,
    max_content_chars: int,
) -> dict[str, Any]:
    _source_header(value)
    result = _one_of(
        value.get("result"), {"success", "explicit_empty", "failure"}, "Discord poll result"
    )
    tool_binding = _exact(value.get("toolBinding"), TOOL_BINDING, "Discord tool binding")
    attempted_at = _time(value.get("attemptedAt"), "Discord poll attempt")
    if result == "failure":
        if value.get("acknowledgementRequired") is not False or value.get("items") != []:
            raise ValidationError("failed Discord poll invented evidence or acknowledgement")
        return {
            "source": SOURCE_ID,
            "attemptedAt": attempted_at,
            "result": result,
            "errorCode": _error_code(value.get("errorCode")),
            "toolBinding": tool_binding,
            "items": [],
            "acknowledgementRequired": False,
        }
    account = _required_text(value.get("accountBinding"), "Discord account binding", 256)
    if _ACCOUNT.fullmatch(account) is None:
        raise ValidationError("Discord account binding is invalid")
    channel_digest = _sha256(value.get("channelSetDigest"), "Discord channel set binding")
    delivery = _sha256(value.get("deliveryBinding"), "Discord delivery binding")
    evidence = value.get("evidenceRefs")
    if evidence != [delivery]:
        raise ValidationError("Discord poll evidence does not attest its delivery")
    items = value.get("items")
    if not isinstance(items, list) or len(items) > limit:
        raise ValidationError("Discord poll items exceed their bound")
    projected_items = [
        _item_projection(item, max_content_chars=max_content_chars) for item in items
    ]
    completeness = _one_of(
        value.get("completeness"), {"complete", "partial"}, "Discord completeness"
    )
    covered_through = _time(value.get("coveredThrough"), "Discord coverage")
    cursor_binding = _cursor(value.get("cursorBinding"))
    expected_delivery = _delivery_binding(
        account=account,
        channel_digest=channel_digest,
        cursor=cursor_binding,
        result=result,
        completeness=completeness,
        covered_through=covered_through,
        items=projected_items,
    )
    if delivery != expected_delivery:
        raise ValidationError("Discord delivery binding does not attest its projection")
    ack = _required_text(value.get("ackToken"), "Discord acknowledgement token", 64)
    if _ACK_TOKEN.fullmatch(ack) is None or value.get("acknowledgementRequired") is not True:
        raise ValidationError("Discord successful poll has no valid acknowledgement")
    return {
        "source": SOURCE_ID,
        "attemptedAt": attempted_at,
        "result": result,
        "completeness": completeness,
        "coveredThrough": covered_through,
        "authType": _one_of(value.get("authType"), {"user", "bot"}, "Discord auth type"),
        "accountBinding": account,
        "channelSetDigest": channel_digest,
        "toolBinding": tool_binding,
        "cursorBinding": cursor_binding,
        "evidenceRefs": [delivery],
        "deliveryBinding": delivery,
        "baseline": _boolean(value.get("baseline"), "Discord baseline flag"),
        "replayedPending": _boolean(value.get("replayedPending"), "Discord pending replay flag"),
        "items": projected_items,
        "acknowledgementRequired": True,
        "ackToken": ack,
    }


def _delivery_binding(
    *,
    account: str,
    channel_digest: str,
    cursor: str,
    result: str,
    completeness: str,
    covered_through: str,
    items: list[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        {
            "accountFingerprint": source_fingerprint(account, "Discord account binding"),
            "channelSetDigest": channel_digest,
            "toolFingerprint": source_fingerprint(TOOL_BINDING, "Discord tool binding"),
            "cursorDigest": source_fingerprint(cursor, "Discord cursor binding"),
            "result": result,
            "completeness": completeness,
            "coveredThrough": covered_through,
            "items": items,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _namespaced_digest("discord-delivery-v1", canonical)


def _namespaced_digest(namespace: str, value: str) -> str:
    encoded = namespace.encode() + b"\0" + value.encode()
    return f"sha256:{sha256_bytes(encoded)}"


def _item_projection(value: object, *, max_content_chars: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "observedAt",
        "channelRef",
        "messageRef",
        "authorRef",
        "preview",
        "edited",
        "attachmentCount",
        "embedCount",
    }:
        raise ValidationError("Discord message projection shape is invalid")
    counts = (value["attachmentCount"], value["embedCount"])
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts):
        raise ValidationError("Discord message projection counts are invalid")
    preview = _required_text(
        value["preview"],
        "Discord message preview",
        max_content_chars,
        allow_empty=True,
    )
    return {
        "observedAt": _time(value["observedAt"], "Discord message observation"),
        "channelRef": _sha256(value["channelRef"], "Discord channel reference"),
        "messageRef": _sha256(value["messageRef"], "Discord message reference"),
        "authorRef": _sha256(value["authorRef"], "Discord author reference"),
        "preview": preview,
        "edited": _boolean(value["edited"], "Discord edited flag"),
        "attachmentCount": counts[0],
        "embedCount": counts[1],
    }


def _ack_projection(value: dict[str, Any]) -> dict[str, Any]:
    _source_header(value)
    if value.get("source") != SOURCE_ID or value.get("acknowledged") is not True:
        raise ValidationError("Discord acknowledgement response is invalid")
    already = _boolean(value.get("alreadyAcknowledged"), "Discord idempotence flag")
    checkpoint = _checkpoint_projection(value.get("checkpoint"))
    if checkpoint["pending"]:
        raise ValidationError("Discord acknowledgement left its checkpoint pending")
    return {
        "source": SOURCE_ID,
        "acknowledged": True,
        "alreadyAcknowledged": already,
        "checkpoint": checkpoint,
    }


def _source_header(value: dict[str, Any]) -> None:
    if value.get("source") != SOURCE_ID or value.get("recipeVersion") != RECIPE_VERSION:
        raise ValidationError("Discord companion source contract is incompatible")


def _provider_environment(*, required: bool) -> tuple[dict[str, str], str | None]:
    user = os.environ.get("DISCORD_USER_TOKEN", "")
    bot = os.environ.get("DISCORD_BOT_TOKEN", "")
    channels = os.environ.get("DISCORD_CHANNEL_IDS", "")
    if bool(user) == bool(bot):
        if required:
            raise ContinuityError("set exactly one Discord token in the calling environment")
        return {}, "auth_required"
    if not channels.strip():
        if required:
            raise ContinuityError("set the exact DISCORD_CHANNEL_IDS allowlist")
        return {}, "policy_blocked"
    token_name = "DISCORD_USER_TOKEN" if user else "DISCORD_BOT_TOKEN"
    return {token_name: user or bot, "DISCORD_CHANNEL_IDS": channels}, None


def _synthetic_provider_environment() -> dict[str, str]:
    return {
        "DISCORD_USER_TOKEN": "seld-binding-probe-not-a-provider-token",
        "DISCORD_CHANNEL_IDS": "111111111111111111",
    }


def _child_environment(
    provider: dict[str, str],
    *,
    state_path: Path,
    interpreter: _FileIdentity | None,
) -> dict[str, str]:
    environment = dict(provider)
    environment["DISCORD_STATE_FILE"] = str(state_path)
    environment["LOG_LEVEL"] = "silent"
    if interpreter is not None:
        environment["PATH"] = os.pathsep.join(
            dict.fromkeys((str(interpreter.path.parent), "/usr/bin", "/bin"))
        )
    for name in ("HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _binding_revision(value: str) -> str:
    if _REVISION.fullmatch(value) is None:
        raise ValidationError("Discord binding/source revision is invalid")
    return value


def _digest(value: str) -> str:
    return f"sha256:{sha256_bytes(value.encode('utf-8'))}"


def _required_text(value: object, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise ValidationError(f"{label} is invalid")
    if not allow_empty and not value:
        raise ValidationError(f"{label} is invalid")
    return value


def _optional_text(value: object, label: str, maximum: int) -> str | None:
    return None if value is None else _required_text(value, label, maximum)


def _sha256(value: object, label: str) -> str:
    text = _required_text(value, label, 71)
    if _SHA256.fullmatch(text) is None:
        raise ValidationError(f"{label} is invalid")
    return text


def _optional_sha256(value: object) -> str | None:
    return None if value is None else _sha256(value, "Discord digest")


def _optional_account(value: object) -> str | None:
    if value is None:
        return None
    text = _required_text(value, "Discord account binding", 256)
    if _ACCOUNT.fullmatch(text) is None:
        raise ValidationError("Discord account binding is invalid")
    return text


def _cursor(value: object) -> str:
    text = _required_text(value, "Discord cursor binding", 256)
    if (
        not text.startswith("discord-cursor-v1:sha256:")
        or _SHA256.fullmatch(text.removeprefix("discord-cursor-v1:")) is None
    ):
        raise ValidationError("Discord cursor binding is invalid")
    return text


def _optional_cursor(value: object) -> str | None:
    return None if value is None else _cursor(value)


def _time(value: object, label: str) -> str:
    text = _required_text(value, label, 64)
    from continuity_kernel.records import stored_time

    return stored_time(text, label)


def _optional_time(value: object, label: str) -> str | None:
    return None if value is None else _time(value, label)


def _one_of(value: object, allowed: set[str], label: str) -> str:
    text = _required_text(value, label, 64)
    if text not in allowed:
        raise ValidationError(f"{label} is invalid")
    return text


def _optional_one_of(value: object, allowed: set[str], label: str) -> str | None:
    return None if value is None else _one_of(value, allowed, label)


def _exact(value: object, expected: str, label: str) -> str:
    if value != expected:
        raise ValidationError(f"{label} is incompatible")
    return expected


def _error_code(value: object) -> str:
    return _one_of(value, set(_ERROR_CODES), "Discord error code")


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{label} is invalid")
    return value


__all__ = [
    "ABSENT_BINDING_REVISION",
    "SOURCE_ID",
    "TOOL_BINDING",
    "DiscordSourceBridge",
    "current_discord_tool_fingerprint",
]
