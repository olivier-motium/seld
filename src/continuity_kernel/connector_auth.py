"""Typed connector metadata and host-local secret custody boundaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, cast

from continuity_kernel.connector_identifiers import ConnectionId, parse_connection_id
from continuity_kernel.connector_time import format_utc, parse_utc, validate_utc
from continuity_kernel.errors import ValidationError

CONNECTION_METADATA_FORMAT_VERSION: Final = 1
MAX_METADATA_BYTES: Final = 64 * 1024
_PROVIDER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,95}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class ConnectionHealth(StrEnum):
    """Portable connector health without provider-specific payloads."""

    UNKNOWN = "unknown"
    UNVERIFIED = "unverified"
    READY = "ready"
    DEGRADED = "degraded"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"
    REVOKED = "revoked"


class CredentialKind(StrEnum):
    """Portable secret shape without exposing the secret itself."""

    API_KEY = "api_key"
    BASIC = "basic"
    BEARER = "bearer"
    OAUTH2 = "oauth2"
    SERVICE_ACCOUNT = "service_account"


class ClientKind(StrEnum):
    """How the provider classifies the connector client registration."""

    PUBLIC = "public"
    CONFIDENTIAL = "confidential"
    EXTERNAL = "external"


@dataclass(frozen=True)
class AccountMetadata:
    """Non-secret provider account identity suitable for user review."""

    fingerprint: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.fingerprint is not None and _SHA256.fullmatch(self.fingerprint) is None:
            raise ValidationError("account fingerprint is invalid")
        _optional_text(self.label, "account label", max_bytes=512)


@dataclass(frozen=True)
class ClientMetadata:
    """Non-secret client-registration facts; client secrets never belong here."""

    kind: ClientKind
    identifier: str | None = None
    redirect_uris: tuple[str, ...] = ()
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ClientKind):
            raise ValidationError("client kind is invalid")
        _optional_text(self.identifier, "client identifier", max_bytes=2_048)
        canonical_redirects = _canonical_text_tuple(
            self.redirect_uris,
            "redirect URI",
            max_items=16,
            max_bytes=2_048,
        )
        object.__setattr__(self, "redirect_uris", canonical_redirects)
        _optional_text(self.authorization_endpoint, "authorization endpoint", max_bytes=2_048)
        _optional_text(self.token_endpoint, "token endpoint", max_bytes=2_048)
        if (self.authorization_endpoint is None) != (self.token_endpoint is None):
            raise ValidationError(
                "OAuth authorization and token endpoints must be provided together"
            )


@dataclass(frozen=True)
class ConnectionMetadata:
    """Portable, serializable connection facts with no secret-bearing fields."""

    connection_id: ConnectionId
    provider: str
    source_ids: tuple[str, ...]
    credential_kind: CredentialKind
    account: AccountMetadata
    scopes: tuple[str, ...]
    client: ClientMetadata
    health: ConnectionHealth
    created_at: datetime
    updated_at: datetime
    version: int
    last_verified_at: datetime | None = None
    format_version: int = CONNECTION_METADATA_FORMAT_VERSION

    def __post_init__(self) -> None:
        parse_connection_id(self.connection_id)
        if not isinstance(self.provider, str) or not _PROVIDER.fullmatch(self.provider):
            raise ValidationError("connector provider is invalid")
        canonical_sources = _canonical_text_tuple(
            self.source_ids,
            "source ID",
            max_items=64,
            max_bytes=96,
        )
        if not canonical_sources or any(
            _SOURCE_ID.fullmatch(item) is None for item in canonical_sources
        ):
            raise ValidationError("connector source IDs are invalid")
        object.__setattr__(self, "source_ids", canonical_sources)
        if not isinstance(self.credential_kind, CredentialKind):
            raise ValidationError("credential kind is invalid")
        if not isinstance(self.account, AccountMetadata):
            raise ValidationError("connector account metadata is invalid")
        if not isinstance(self.client, ClientMetadata):
            raise ValidationError("connector client metadata is invalid")
        if not isinstance(self.health, ConnectionHealth):
            raise ValidationError("connection health is invalid")
        canonical_scopes = _canonical_text_tuple(
            self.scopes,
            "scope",
            max_items=256,
            max_bytes=1_024,
        )
        object.__setattr__(self, "scopes", canonical_scopes)
        validate_utc(self.created_at, "connection creation time")
        validate_utc(self.updated_at, "connection update time")
        if self.last_verified_at is not None:
            validate_utc(self.last_verified_at, "connection verification time")
        if self.updated_at < self.created_at:
            raise ValidationError("connection update time precedes its creation time")
        if self.last_verified_at is not None and self.last_verified_at > self.updated_at:
            raise ValidationError("connection verification time follows its update time")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValidationError("connection version must be a positive integer")
        if self.format_version != CONNECTION_METADATA_FORMAT_VERSION:
            raise ValidationError("connection metadata format version is unsupported")
        if self.credential_kind is CredentialKind.OAUTH2 and (
            self.client.kind is not ClientKind.PUBLIC
            or self.client.identifier is None
            or self.client.authorization_endpoint is None
            or self.client.token_endpoint is None
            or len(self.client.redirect_uris) != 1
        ):
            raise ValidationError(
                "OAuth connections require one complete public-client registration"
            )

    def to_dict(self) -> dict[str, object]:
        """Return the complete public metadata shape, which cannot contain secrets."""

        return {
            "account": {
                "fingerprint": self.account.fingerprint,
                "label": self.account.label,
            },
            "client": {
                "authorization_endpoint": self.client.authorization_endpoint,
                "identifier": self.client.identifier,
                "kind": self.client.kind.value,
                "redirect_uris": list(self.client.redirect_uris),
                "token_endpoint": self.client.token_endpoint,
            },
            "connection_id": str(self.connection_id),
            "created_at": format_utc(self.created_at),
            "credential_kind": self.credential_kind.value,
            "format_version": self.format_version,
            "health": self.health.value,
            "last_verified_at": (
                format_utc(self.last_verified_at) if self.last_verified_at is not None else None
            ),
            "provider": self.provider,
            "scopes": list(self.scopes),
            "source_ids": list(self.source_ids),
            "updated_at": format_utc(self.updated_at),
            "version": self.version,
        }

    def to_json(self) -> bytes:
        encoded = (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise ValidationError("connection metadata exceeds its size bound")
        return encoded

    @classmethod
    def from_json(cls, encoded: bytes) -> ConnectionMetadata:
        if not encoded or len(encoded) > MAX_METADATA_BYTES:
            raise ValidationError("connection metadata is empty or exceeds its size bound")
        try:
            value = cast(object, json.loads(encoded.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("connection metadata is not valid JSON") from exc
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: object) -> ConnectionMetadata:
        """Strictly parse one record embedded in a canonical non-secret snapshot."""

        payload = _mapping(value, "connection metadata")
        _exact_keys(
            payload,
            {
                "account",
                "client",
                "connection_id",
                "created_at",
                "credential_kind",
                "format_version",
                "health",
                "last_verified_at",
                "provider",
                "scopes",
                "source_ids",
                "updated_at",
                "version",
            },
            "connection metadata",
        )
        account = _mapping(payload["account"], "connection account metadata")
        _exact_keys(account, {"fingerprint", "label"}, "connection account metadata")
        client = _mapping(payload["client"], "connection client metadata")
        _exact_keys(
            client,
            {
                "authorization_endpoint",
                "identifier",
                "kind",
                "redirect_uris",
                "token_endpoint",
            },
            "client metadata",
        )
        return cls(
            connection_id=parse_connection_id(payload["connection_id"]),
            provider=_required_text(payload["provider"], "provider", max_bytes=64),
            source_ids=_text_tuple(
                payload["source_ids"],
                "source ID",
                max_items=64,
                max_bytes=96,
            ),
            credential_kind=_credential_kind(payload["credential_kind"]),
            account=AccountMetadata(
                fingerprint=_fingerprint(account["fingerprint"]),
                label=_parsed_optional_text(account["label"], "account label"),
            ),
            scopes=_text_tuple(payload["scopes"], "scope", max_items=256, max_bytes=1_024),
            client=ClientMetadata(
                kind=_client_kind(client["kind"]),
                identifier=_parsed_optional_text(client["identifier"], "client identifier"),
                authorization_endpoint=_parsed_optional_text(
                    client["authorization_endpoint"], "authorization endpoint"
                ),
                token_endpoint=_parsed_optional_text(client["token_endpoint"], "token endpoint"),
                redirect_uris=_text_tuple(
                    client["redirect_uris"],
                    "redirect URI",
                    max_items=16,
                    max_bytes=2_048,
                ),
            ),
            health=_connection_health(payload["health"]),
            created_at=parse_utc(payload["created_at"], "connection creation time"),
            updated_at=parse_utc(payload["updated_at"], "connection update time"),
            version=_positive_integer(payload["version"], "connection version"),
            last_verified_at=(
                None
                if payload["last_verified_at"] is None
                else parse_utc(payload["last_verified_at"], "connection verification time")
            ),
            format_version=_positive_integer(payload["format_version"], "format version"),
        )


def _optional_text(value: object, label: str, *, max_bytes: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, max_bytes=max_bytes)


def _parsed_optional_text(value: object, label: str) -> str | None:
    return _optional_text(value, label, max_bytes=2_048)


def _required_text(value: object, label: str, *, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    encoded = value.encode("utf-8")
    if not value.strip() or len(encoded) > max_bytes or "\x00" in value:
        raise ValidationError(f"{label} is empty, too large, or contains a null byte")
    return value


def _canonical_text_tuple(
    value: object,
    label: str,
    *,
    max_items: int,
    max_bytes: int,
) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > max_items:
        raise ValidationError(f"{label} list is invalid or exceeds its item bound")
    parsed = tuple(_required_text(item, label, max_bytes=max_bytes) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValidationError(f"{label} list contains duplicates")
    return tuple(sorted(parsed))


def _text_tuple(
    value: object,
    label: str,
    *,
    max_items: int,
    max_bytes: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValidationError(f"{label} list is invalid or exceeds its item bound")
    return _canonical_text_tuple(tuple(value), label, max_items=max_items, max_bytes=max_bytes)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValidationError(f"{label} has a non-text key")
    return {cast(str, key): item for key, item in raw.items()}


def _exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValidationError(f"{label} has an unsupported shape")


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{label} must be a positive integer")
    return value


def _client_kind(value: object) -> ClientKind:
    if not isinstance(value, str):
        raise ValidationError("client kind is invalid")
    try:
        return ClientKind(value)
    except ValueError as exc:
        raise ValidationError("client kind is invalid") from exc


def _credential_kind(value: object) -> CredentialKind:
    if not isinstance(value, str):
        raise ValidationError("credential kind is invalid")
    try:
        return CredentialKind(value)
    except ValueError as exc:
        raise ValidationError("credential kind is invalid") from exc


def _fingerprint(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValidationError("account fingerprint is invalid")
    return value


def _connection_health(value: object) -> ConnectionHealth:
    if not isinstance(value, str):
        raise ValidationError("connection health is invalid")
    try:
        return ConnectionHealth(value)
    except ValueError as exc:
        raise ValidationError("connection health is invalid") from exc
