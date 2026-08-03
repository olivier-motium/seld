"""Host-local custody for OAuth desktop-client secrets."""

from __future__ import annotations

import base64
import hashlib
from typing import Final

from continuity_kernel.connector_identifiers import (
    ConnectionId,
    SecretName,
    SecretStore,
    parse_connection_id,
    parse_secret_name,
)
from continuity_kernel.errors import ValidationError

_CLIENT_SECRET_NAME: Final = parse_secret_name("oauth-client-secret")
_MAX_CLIENT_SECRET_BYTES: Final = 2 * 1024


def load_oauth_client_secret(
    store: SecretStore,
    *,
    provider: str,
    client_id: str,
) -> str | None:
    """Resolve one client secret without adding a portable pointer."""

    value = store.get_secret(_client_secret_connection_id(provider, client_id), _CLIENT_SECRET_NAME)
    if value is None:
        return None
    try:
        return _client_secret(value.decode("utf-8"))
    except UnicodeError as exc:
        raise ValidationError("stored OAuth client secret is invalid") from exc


def store_oauth_client_secret(
    store: SecretStore,
    *,
    provider: str,
    client_id: str,
    client_secret: str,
) -> None:
    """Store one validated client secret only in host-local custody."""

    clean = _client_secret(client_secret)
    store.set_secret(
        _client_secret_connection_id(provider, client_id),
        _CLIENT_SECRET_NAME,
        clean.encode("utf-8"),
    )


def _client_secret_connection_id(provider: object, client_id: object) -> ConnectionId:
    if not isinstance(provider, str) or provider.casefold() not in {"google"}:
        raise ValidationError("OAuth client-secret provider is unsupported")
    if not isinstance(client_id, str) or not client_id:
        raise ValidationError("OAuth client ID is invalid")
    digest = hashlib.sha256(f"{provider.casefold()}\0{client_id}".encode()).digest()[:24]
    opaque = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return parse_connection_id(f"con-{opaque}")


def _client_secret(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_CLIENT_SECRET_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValidationError("OAuth client secret is invalid")
    return value
