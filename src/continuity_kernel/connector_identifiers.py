"""Opaque connector identifiers and secret-reference boundary."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import NewType, Protocol

from continuity_kernel.errors import NotFoundError, ValidationError

ConnectionId = NewType("ConnectionId", str)
SecretName = NewType("SecretName", str)
_CONNECTION_ID = re.compile(r"^con-[A-Za-z0-9_-]{32}$")
_SECRET_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SECRET_REFERENCE = re.compile(
    r"^secret://(?P<connection>con-[A-Za-z0-9_-]{32})/(?P<name>[a-z][a-z0-9-]{0,63})$"
)


class SecretStore(Protocol):
    """Opaque secret custody; callers cannot enumerate or serialize values."""

    def get_secret(self, connection_id: ConnectionId, name: SecretName) -> bytes | None: ...

    def set_secret(self, connection_id: ConnectionId, name: SecretName, value: bytes) -> None: ...

    def delete_secret(self, connection_id: ConnectionId, name: SecretName) -> None: ...


@dataclass(frozen=True)
class SecretReference:
    connection_id: ConnectionId
    name: SecretName

    def __post_init__(self) -> None:
        parse_connection_id(self.connection_id)
        parse_secret_name(self.name)

    def __str__(self) -> str:
        return f"secret://{self.connection_id}/{self.name}"


def new_connection_id() -> ConnectionId:
    """Create an opaque identifier with no provider or account semantics."""

    return ConnectionId(f"con-{secrets.token_urlsafe(24)}")


def parse_connection_id(value: object) -> ConnectionId:
    if not isinstance(value, str) or not _CONNECTION_ID.fullmatch(value):
        raise ValidationError("connection ID is invalid")
    return ConnectionId(value)


def parse_secret_name(value: object) -> SecretName:
    if not isinstance(value, str) or not _SECRET_NAME.fullmatch(value):
        raise ValidationError("secret name is invalid")
    return SecretName(value)


def parse_secret_reference(value: object) -> SecretReference:
    if not isinstance(value, str):
        raise ValidationError("secret reference must be text")
    matched = _SECRET_REFERENCE.fullmatch(value)
    if matched is None:
        raise ValidationError("secret reference is invalid")
    return SecretReference(
        connection_id=parse_connection_id(matched.group("connection")),
        name=parse_secret_name(matched.group("name")),
    )


def resolve_secret_reference(value: str | SecretReference, store: SecretStore) -> bytes:
    reference = parse_secret_reference(value) if isinstance(value, str) else value
    secret = store.get_secret(reference.connection_id, reference.name)
    if secret is None:
        raise NotFoundError("referenced connector secret was not found")
    return secret
