"""Canonical portable connection metadata; connector secrets stay host-local."""

from __future__ import annotations

import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.connector_auth import (
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_identifiers import ConnectionId, parse_connection_id
from continuity_kernel.connector_time import format_utc, parse_utc
from continuity_kernel.errors import ConflictError, NotFoundError, ValidationError

CONNECTION_STATE_FORMAT_VERSION: Final = 1
ABSENT_CONNECTION_REVISION: Final = "absent"
MAX_CONNECTION_STATE_BYTES: Final = 256 * 1024
MAX_CONNECTIONS: Final = 128
MAX_FUTURE_SKEW: Final = timedelta(minutes=1)
_META = re.compile(r"^<!-- seld-connections:v1:([A-Za-z0-9_-]+) -->$")


@dataclass(frozen=True)
class ConnectionSnapshot:
    format_version: int
    connections: tuple[ConnectionMetadata, ...]
    updated_at: str | None
    revision: str

    def connection(self, connection_id: ConnectionId | str) -> ConnectionMetadata | None:
        clean_id = parse_connection_id(connection_id)
        return next((item for item in self.connections if item.connection_id == clean_id), None)


def empty_connection_snapshot() -> ConnectionSnapshot:
    return ConnectionSnapshot(
        format_version=CONNECTION_STATE_FORMAT_VERSION,
        connections=(),
        updated_at=None,
        revision=ABSENT_CONNECTION_REVISION,
    )


def connection_snapshot_dict(snapshot: ConnectionSnapshot) -> dict[str, Any]:
    """Return a redacted status projection without account or client bindings."""

    return {
        "connection_count": len(snapshot.connections),
        "connections": [
            {
                "account_label": item.account.label,
                "connection_id": str(item.connection_id),
                "credential_kind": item.credential_kind.value,
                "health": item.health.value,
                "last_verified_at": (
                    format_utc(item.last_verified_at) if item.last_verified_at is not None else None
                ),
                "provider": item.provider,
                "source_ids": list(item.source_ids),
                "updated_at": format_utc(item.updated_at),
                "version": item.version,
            }
            for item in snapshot.connections
        ],
        "format_version": snapshot.format_version,
        "revision": snapshot.revision,
        "updated_at": snapshot.updated_at,
    }


def render_connection_snapshot(snapshot: ConnectionSnapshot) -> str:
    payload = {
        "connections": [item.to_dict() for item in snapshot.connections],
        "format_version": snapshot.format_version,
        "updated_at": snapshot.updated_at,
    }
    encoded_meta = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    meta = urlsafe_b64encode(encoded_meta).rstrip(b"=").decode("ascii")
    lines = [
        f"<!-- seld-connections:v1:{meta} -->",
        "# Connections",
        "",
        "Portable metadata only. Connector credentials remain in host-local secret custody.",
    ]
    if not snapshot.connections:
        lines.extend(("", "No connections configured."))
    for item in snapshot.connections:
        lines.extend(
            (
                "",
                f"## {item.provider} ({str(item.connection_id)[-8:]})",
                "",
                f"- Connection: `{item.connection_id}`",
                f"- Provider: `{item.provider}`",
                f"- Sources: {', '.join(f'`{source}`' for source in item.source_ids)}",
                f"- Credential kind: `{item.credential_kind.value}`",
                f"- Health: `{item.health.value}`",
                f"- Updated: {format_utc(item.updated_at)}",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_connection_snapshot(
    encoded: bytes,
    *,
    observed_at: datetime | None = None,
) -> ConnectionSnapshot:
    if not encoded or len(encoded) > MAX_CONNECTION_STATE_BYTES:
        raise ValidationError("connection state is empty or exceeds its size bound")
    try:
        text = encoded.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("connection state is not UTF-8") from exc
    first_line = text.splitlines()[0] if text.splitlines() else ""
    matched = _META.fullmatch(first_line)
    if matched is None:
        raise ValidationError("connection state metadata is missing or malformed")
    try:
        token = matched.group(1)
        padding = "=" * (-len(token) % 4)
        decoded = urlsafe_b64decode((token + padding).encode("ascii"))
        value = cast(object, json.loads(decoded.decode("utf-8")))
    except (Base64Error, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("connection state metadata is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("connection state has an unsupported shape")
    payload = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in payload) or set(payload) != {
        "connections",
        "format_version",
        "updated_at",
    }:
        raise ValidationError("connection state has an unsupported shape")
    if payload["format_version"] != CONNECTION_STATE_FORMAT_VERSION:
        raise ValidationError("connection state has an unsupported version")
    raw_connections = payload["connections"]
    if not isinstance(raw_connections, list) or len(raw_connections) > MAX_CONNECTIONS:
        raise ValidationError("connection state has an invalid connection list")
    connections = tuple(ConnectionMetadata.from_dict(item) for item in raw_connections)
    identifiers = tuple(str(item.connection_id) for item in connections)
    if identifiers != tuple(sorted(identifiers)) or len(set(identifiers)) != len(identifiers):
        raise ValidationError("connections must be sorted and unique")
    updated_raw = payload["updated_at"]
    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    if updated_raw is None:
        if connections:
            raise ValidationError("connection state updated_at is invalid")
        updated_at = None
    elif isinstance(updated_raw, str):
        parsed_updated_at = parse_utc(updated_raw, "connection state update time")
        if parsed_updated_at > now + MAX_FUTURE_SKEW:
            raise ValidationError("connection state updated_at is implausibly in the future")
        if any(item.updated_at > parsed_updated_at for item in connections):
            raise ValidationError("connection update follows the connection state update")
        updated_at = format_utc(parsed_updated_at)
    else:
        raise ValidationError("connection state updated_at is invalid")
    snapshot = ConnectionSnapshot(
        format_version=CONNECTION_STATE_FORMAT_VERSION,
        connections=connections,
        updated_at=updated_at,
        revision=sha256_bytes(encoded),
    )
    if render_connection_snapshot(snapshot).encode("utf-8") != encoded:
        raise ValidationError("connection state is not in canonical form")
    return snapshot


def put_connection(
    snapshot: ConnectionSnapshot,
    connection: ConnectionMetadata,
    *,
    observed_at: datetime | None = None,
) -> ConnectionSnapshot:
    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    existing = snapshot.connection(connection.connection_id)
    if existing is None:
        if connection.version != 1:
            raise ConflictError("a new connection must start at version 1")
        if len(snapshot.connections) >= MAX_CONNECTIONS:
            raise ValidationError("too many connections")
    else:
        if connection.created_at != existing.created_at:
            raise ConflictError("connection creation time cannot change")
        if _credential_binding(connection) != _credential_binding(existing):
            raise ConflictError("connection credential binding cannot change")
        if connection.version != existing.version + 1:
            raise ConflictError("connection version is stale; reload before updating")
        if connection.updated_at <= existing.updated_at:
            raise ConflictError("connection update time must advance")
    if connection.updated_at > now + MAX_FUTURE_SKEW:
        raise ValidationError("connection update time is implausibly in the future")
    snapshot_time = _snapshot_time(snapshot)
    if snapshot_time is not None and now < snapshot_time:
        raise ConflictError("connection observation predates the current snapshot")
    by_id = {item.connection_id: item for item in snapshot.connections}
    by_id[connection.connection_id] = connection
    return _round_trip(
        ConnectionSnapshot(
            format_version=CONNECTION_STATE_FORMAT_VERSION,
            connections=tuple(by_id[item] for item in sorted(by_id)),
            updated_at=format_utc(max(now, connection.updated_at, snapshot_time or now)),
            revision="",
        ),
        observed_at=max(now, connection.updated_at),
    )


def mark_connection_health(
    snapshot: ConnectionSnapshot,
    connection_id: ConnectionId | str,
    health: ConnectionHealth,
    *,
    verified: bool = False,
    observed_at: datetime | None = None,
) -> ConnectionSnapshot:
    if not isinstance(health, ConnectionHealth):
        raise ValidationError("connection health is invalid")
    clean_id = parse_connection_id(connection_id)
    existing = snapshot.connection(clean_id)
    if existing is None:
        raise NotFoundError("connection was not found")
    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    if now <= existing.updated_at:
        raise ConflictError("connection health observation is stale")
    last_verified_at = now if verified else existing.last_verified_at
    if existing.health is health and last_verified_at == existing.last_verified_at:
        return snapshot
    return put_connection(
        snapshot,
        replace(
            existing,
            health=health,
            last_verified_at=last_verified_at,
            updated_at=now,
            version=existing.version + 1,
        ),
        observed_at=now,
    )


def remove_connection(
    snapshot: ConnectionSnapshot,
    connection_id: ConnectionId | str,
    *,
    observed_at: datetime | None = None,
) -> ConnectionSnapshot:
    clean_id = parse_connection_id(connection_id)
    if snapshot.connection(clean_id) is None:
        raise NotFoundError("connection was not found")
    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    snapshot_time = _snapshot_time(snapshot)
    if snapshot_time is not None and now <= snapshot_time:
        raise ConflictError("connection removal observation is stale")
    remaining = tuple(item for item in snapshot.connections if item.connection_id != clean_id)
    return _round_trip(
        ConnectionSnapshot(
            format_version=CONNECTION_STATE_FORMAT_VERSION,
            connections=remaining,
            updated_at=format_utc(now),
            revision="",
        ),
        observed_at=now,
    )


def _round_trip(
    snapshot: ConnectionSnapshot,
    *,
    observed_at: datetime,
) -> ConnectionSnapshot:
    return parse_connection_snapshot(
        render_connection_snapshot(snapshot).encode("utf-8"),
        observed_at=observed_at,
    )


def _snapshot_time(snapshot: ConnectionSnapshot) -> datetime | None:
    if snapshot.updated_at is None:
        return None
    return parse_utc(snapshot.updated_at, "connection state update time")


def _credential_binding(connection: ConnectionMetadata) -> tuple[object, ...]:
    return (
        connection.provider,
        connection.source_ids,
        connection.credential_kind,
        connection.account.fingerprint,
        connection.scopes,
        connection.client,
    )
