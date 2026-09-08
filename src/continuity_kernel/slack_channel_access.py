"""Fail-closed local channel policy for selected Slack workspaces.

The policy is separate from OAuth grants. It restricts a verified workspace to
an owner-selected set of channels without rewriting earlier host-local history.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from continuity_kernel.atomic import atomic_write
from continuity_kernel.errors import ValidationError

_CONFIG_VERSION: Final = 1
_MAX_CONFIG_BYTES: Final = 64 * 1024
_MAX_CHANNELS: Final = 256
_MAX_CONNECTIONS: Final = 256
_MAX_PENDING_NAMES: Final = 256
_MAX_TEXT_LENGTH: Final = 256
_CHANNEL_ID = re.compile(r"[CG][A-Z0-9]+")
_CHANNEL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
_CHANNEL_OPERATIONS: Final = frozenset(
    {"messages.list", "messages.get", "threads.list", "reactions.list", "files.list"}
)
_SEARCH_TERM = re.compile(r'"[^"\\\r\n]{1,128}"')
_SEARCH_AFTER = re.compile(r"after:[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SEARCH_CHANNEL = re.compile(r"in:([A-Za-z0-9]{1,128})")


@dataclass(frozen=True)
class SlackChannelAccessPolicy:
    """One exact workspace's connections, channels, and channel names."""

    workspace_id: str
    connection_ids: frozenset[str]
    channel_names: Mapping[str, str]
    pending_channel_names: frozenset[str]

    @property
    def channels(self) -> frozenset[str]:
        return frozenset(self.channel_names)

    @property
    def fingerprint(self) -> str:
        value = {
            "channel_names": dict(sorted(self.channel_names.items())),
            "connection_ids": sorted(self.connection_ids),
            "pending_channel_names": sorted(self.pending_channel_names),
            "workspace_id": self.workspace_id,
        }
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def allows_connection(self, connection_id: object) -> bool:
        return isinstance(connection_id, str) and connection_id in self.connection_ids

    def allows_channel(self, channel_id: object) -> bool:
        return isinstance(channel_id, str) and channel_id in self.channel_names

    def allows_document(self, metadata: object) -> bool:
        if not isinstance(metadata, Mapping):
            return False
        return self.allows_channel(metadata.get("channel_id"))

    def allows_read_operation(self, operation: str, input_value: object) -> bool:
        """Allow identity plus exact approved channel and scoped-search reads."""

        if operation == "identity.get":
            return input_value == {}
        if not isinstance(input_value, Mapping):
            return False
        if operation in _CHANNEL_OPERATIONS:
            return self.allows_channel(input_value.get("channel"))
        if operation == "search.messages":
            return self._safe_search_channel(input_value) is not None
        return False

    def validate_search_result(self, value: object) -> None:
        """Refuse a provider search result that contains an unapproved channel."""

        if not isinstance(value, Mapping):
            raise ValidationError("Slack search result is invalid")
        messages = value.get("messages")
        if not isinstance(messages, Mapping):
            raise ValidationError("Slack search result is invalid")
        matches = messages.get("matches")
        if not isinstance(matches, list):
            raise ValidationError("Slack search result is invalid")
        for item in matches:
            if not isinstance(item, Mapping):
                raise ValidationError("Slack search result is invalid")
            channel = item.get("channel")
            if not isinstance(channel, Mapping) or not self.allows_channel(channel.get("id")):
                raise ValidationError("Slack search result escaped the approved channel scope")

    def _safe_search_channel(self, input_value: Mapping[str, object]) -> str | None:
        query = input_value.get("query")
        if not isinstance(query, str):
            return None
        parts = query.split()
        if not parts:
            return None
        scoped = _SEARCH_CHANNEL.fullmatch(parts[-1])
        if scoped is None or not self.allows_channel(scoped.group(1)):
            return None
        for part in parts[:-1]:
            if _SEARCH_TERM.fullmatch(part) is None and _SEARCH_AFTER.fullmatch(part) is None:
                return None
        return scoped.group(1)


@dataclass(frozen=True)
class _PolicyConfiguration:
    policies: tuple[SlackChannelAccessPolicy, ...]

    def for_connection(self, connection_id: str) -> SlackChannelAccessPolicy | None:
        found = [policy for policy in self.policies if policy.allows_connection(connection_id)]
        if len(found) > 1:
            raise ValidationError("Slack channel access configuration duplicates a connection")
        return found[0] if found else None

    def for_workspace(self, workspace_id: str) -> SlackChannelAccessPolicy | None:
        found = [policy for policy in self.policies if policy.workspace_id == workspace_id]
        if len(found) > 1:
            raise ValidationError("Slack channel access configuration duplicates a workspace")
        return found[0] if found else None


@dataclass(frozen=True)
class _RestrictedMarker:
    connection_ids: frozenset[str]
    workspace_ids: frozenset[str]

    def covers_connection(self, connection_id: str) -> bool:
        return connection_id in self.connection_ids

    def covers_workspace(self, workspace_id: str) -> bool:
        return workspace_id in self.workspace_ids


def load_slack_channel_access(connection_id: str) -> SlackChannelAccessPolicy | None:
    """Load a pre-provider policy for one connection, or fail closed if required."""

    if not _text(connection_id):
        raise ValidationError("Slack channel access connection ID is invalid")
    configuration = _load_configuration()
    marker = _load_marker()
    if configuration is None:
        if marker.covers_connection(connection_id):
            raise ValidationError("Slack channel access policy is required for this connection")
        return None
    policy = configuration.for_connection(connection_id)
    if policy is None and marker.covers_connection(connection_id):
        raise ValidationError("Slack channel access policy is required for this connection")
    if policy is not None:
        _persist_marker(configuration)
    return policy


def policy_for_verified_slack_workspace(
    connection_id: str, workspace_id: str
) -> SlackChannelAccessPolicy | None:
    """Resolve a policy after a transient auth.test workspace verification."""

    if not _text(connection_id) or not _text(workspace_id):
        raise ValidationError("Slack channel access identity is invalid")
    configuration = _load_configuration()
    marker = _load_marker()
    if configuration is None:
        if marker.covers_connection(connection_id) or marker.covers_workspace(workspace_id):
            raise ValidationError("Slack channel access policy is required for this workspace")
        return None
    _persist_marker(configuration)
    policy = configuration.for_workspace(workspace_id)
    if policy is None:
        if marker.covers_connection(connection_id) or marker.covers_workspace(workspace_id):
            raise ValidationError("Slack channel access policy is required for this workspace")
        return None
    if not policy.allows_connection(connection_id):
        raise ValidationError("Slack connection is not approved for this workspace")
    _persist_marker(configuration)
    return policy


def _config_path() -> Path:
    return Path.home() / ".config" / "seld" / "slack-channel-access.json"


def _marker_path() -> Path:
    return _config_path().with_name("slack-channel-access-required.json")


def _load_configuration() -> _PolicyConfiguration | None:
    value = _read_json(_config_path(), missing_ok=True, label="configuration")
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"version", "workspaces"}:
        raise ValidationError("Slack channel access configuration is invalid")
    if value.get("version") != _CONFIG_VERSION or type(value.get("version")) is not int:
        raise ValidationError("Slack channel access configuration version is invalid")
    workspaces = value.get("workspaces")
    if not isinstance(workspaces, Mapping) or len(workspaces) > _MAX_CONNECTIONS:
        raise ValidationError("Slack channel access configuration is invalid")
    policies: list[SlackChannelAccessPolicy] = []
    for workspace_id, workspace in workspaces.items():
        if not _text(workspace_id) or not isinstance(workspace, Mapping):
            raise ValidationError("Slack channel access configuration is invalid")
        if set(workspace) != {"channels", "connection_ids", "pending_channel_names"}:
            raise ValidationError("Slack channel access configuration is invalid")
        channels = _channels(workspace.get("channels"))
        connection_ids = frozenset(
            _texts(workspace.get("connection_ids"), maximum=_MAX_CONNECTIONS)
        )
        pending_values = _texts(workspace.get("pending_channel_names"), maximum=_MAX_PENDING_NAMES)
        if any(_CHANNEL_NAME.fullmatch(item) is None for item in pending_values):
            raise ValidationError("Slack channel access configuration is invalid")
        pending_names = frozenset(item.casefold() for item in pending_values)
        if len(pending_names) != len(pending_values):
            raise ValidationError("Slack channel access configuration is invalid")
        policies.append(
            SlackChannelAccessPolicy(
                workspace_id=workspace_id,
                connection_ids=connection_ids,
                channel_names=channels,
                pending_channel_names=pending_names,
            )
        )
    return _PolicyConfiguration(tuple(policies))


def _persist_marker(configuration: _PolicyConfiguration) -> None:
    prior = _load_marker()
    value = {
        "connection_ids": sorted(
            set(prior.connection_ids)
            | {
                connection_id
                for policy in configuration.policies
                for connection_id in policy.connection_ids
            }
        ),
        "version": _CONFIG_VERSION,
        "workspace_ids": sorted(
            set(prior.workspace_ids) | {policy.workspace_id for policy in configuration.policies}
        ),
    }
    encoded = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    path = _marker_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            path.parent.chmod(0o700)
        atomic_write(path, encoded, mode=0o600)
    except OSError as exc:
        raise ValidationError("Slack channel access marker is unavailable") from exc


def _load_marker() -> _RestrictedMarker:
    value = _read_json(_marker_path(), missing_ok=True, label="marker")
    if value is None:
        return _RestrictedMarker(frozenset(), frozenset())
    if not isinstance(value, Mapping) or set(value) != {
        "connection_ids",
        "version",
        "workspace_ids",
    }:
        raise ValidationError("Slack channel access marker is invalid")
    if value.get("version") != _CONFIG_VERSION or type(value.get("version")) is not int:
        raise ValidationError("Slack channel access marker is invalid")
    return _RestrictedMarker(
        connection_ids=frozenset(_texts(value.get("connection_ids"), maximum=_MAX_CONNECTIONS)),
        workspace_ids=frozenset(_texts(value.get("workspace_ids"), maximum=_MAX_CONNECTIONS)),
    )


def _read_json(path: Path, *, missing_ok: bool, label: str) -> object | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ValidationError(f"Slack channel access {label} is unavailable") from None
    except OSError as exc:
        raise ValidationError(f"Slack channel access {label} is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValidationError(f"Slack channel access {label} must be a regular file")
    if info.st_mode & 0o022:
        raise ValidationError(f"Slack channel access {label} must not be writable by others")
    if info.st_size > _MAX_CONFIG_BYTES:
        raise ValidationError(f"Slack channel access {label} exceeds its size bound")
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Slack channel access {label} is invalid") from exc


def _channels(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or len(value) > _MAX_CHANNELS:
        raise ValidationError("Slack channel access configuration is invalid")
    result: dict[str, str] = {}
    for channel_id, name in value.items():
        if (
            not isinstance(channel_id, str)
            or _CHANNEL_ID.fullmatch(channel_id) is None
            or not isinstance(name, str)
            or _CHANNEL_NAME.fullmatch(name) is None
            or channel_id in result
        ):
            raise ValidationError("Slack channel access configuration is invalid")
        result[channel_id] = name
    return result


def _texts(value: object, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError("Slack channel access configuration is invalid")
    result: list[str] = []
    for item in value:
        if not _text(item) or item in result:
            raise ValidationError("Slack channel access configuration is invalid")
        result.append(item)
    return tuple(result)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= _MAX_TEXT_LENGTH
