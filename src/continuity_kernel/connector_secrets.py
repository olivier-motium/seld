"""Concrete connector secret stores behind the portable custody protocol."""

from __future__ import annotations

import base64
import binascii
import importlib
import threading
from typing import Final, Protocol, cast

from continuity_kernel.connector_identifiers import (
    ConnectionId,
    SecretName,
    parse_connection_id,
    parse_secret_name,
)
from continuity_kernel.errors import SetupError, ValidationError

MAX_SECRET_BYTES: Final = 1024 * 1024
_SECURE_KEYRING_MODULES: Final = (
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.Windows",
    "keyring.backends.kwallet",
)


class InMemorySecretStore:
    """Process-local test backend that deliberately has no persistence surface."""

    def __init__(self) -> None:
        self._values: dict[tuple[ConnectionId, SecretName], bytes] = {}
        self._lock = threading.RLock()

    def get_secret(self, connection_id: ConnectionId, name: SecretName) -> bytes | None:
        key = _secret_key(connection_id, name)
        with self._lock:
            value = self._values.get(key)
            return None if value is None else bytes(value)

    def set_secret(self, connection_id: ConnectionId, name: SecretName, value: bytes) -> None:
        key = _secret_key(connection_id, name)
        secret = _secret_bytes(value)
        with self._lock:
            self._values[key] = secret

    def delete_secret(self, connection_id: ConnectionId, name: SecretName) -> None:
        key = _secret_key(connection_id, name)
        with self._lock:
            self._values.pop(key, None)


class _KeyringBackend(Protocol):
    priority: float


class _KeyringModule(Protocol):
    def get_keyring(self) -> _KeyringBackend: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class KeyringSecretStore:
    """OS-keyring backend loaded only when a secret operation is requested."""

    def __init__(self, service_name: str = "seld.connector-auth") -> None:
        self._service_name = _service_name(service_name)
        self._module: _KeyringModule | None = None

    def get_secret(self, connection_id: ConnectionId, name: SecretName) -> bytes | None:
        username = _secret_username(connection_id, name)
        try:
            value = self._keyring().get_password(self._service_name, username)
        except SetupError:
            raise
        except Exception as exc:
            raise SetupError("OS keyring secret lookup failed") from exc
        if value is None:
            return None
        try:
            marker, encoded = value.split(":", 1)
            if marker != "v1":
                raise ValueError("unsupported keyring value")
            return _secret_bytes(base64.b64decode(encoded.encode("ascii"), validate=True))
        except (UnicodeError, ValueError, binascii.Error) as exc:
            raise ValidationError("stored OS keyring value is invalid") from exc

    def set_secret(self, connection_id: ConnectionId, name: SecretName, value: bytes) -> None:
        username = _secret_username(connection_id, name)
        encoded = "v1:" + base64.b64encode(_secret_bytes(value)).decode("ascii")
        try:
            self._keyring().set_password(self._service_name, username, encoded)
        except SetupError:
            raise
        except Exception as exc:
            raise SetupError("OS keyring secret write failed") from exc

    def delete_secret(self, connection_id: ConnectionId, name: SecretName) -> None:
        username = _secret_username(connection_id, name)
        try:
            self._keyring().delete_password(self._service_name, username)
        except SetupError:
            raise
        except Exception as exc:
            raise SetupError("OS keyring secret deletion failed") from exc

    def _keyring(self) -> _KeyringModule:
        if self._module is not None:
            return self._module
        try:
            module = cast(_KeyringModule, importlib.import_module("keyring"))
            backend = module.get_keyring()
            priority = float(backend.priority)
        except Exception as exc:
            raise SetupError("a supported OS keyring is unavailable") from exc
        backend_module = type(backend).__module__
        approved = any(
            backend_module == allowed or backend_module.startswith(f"{allowed}.")
            for allowed in _SECURE_KEYRING_MODULES
        )
        if priority <= 0 or not approved:
            raise SetupError("the selected keyring backend is not an approved OS keyring")
        self._module = module
        return module


def _secret_key(connection_id: ConnectionId, name: SecretName) -> tuple[ConnectionId, SecretName]:
    return parse_connection_id(connection_id), parse_secret_name(name)


def _secret_username(connection_id: ConnectionId, name: SecretName) -> str:
    clean_connection, clean_name = _secret_key(connection_id, name)
    return f"{clean_connection}/{clean_name}"


def _secret_bytes(value: object) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > MAX_SECRET_BYTES:
        raise ValidationError("connector secret is empty or exceeds its size bound")
    return bytes(value)


def _service_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("keyring service must be text")
    encoded = value.encode("utf-8")
    if not value.strip() or len(encoded) > 128 or "\x00" in value:
        raise ValidationError("keyring service is empty, too large, or contains a null byte")
    return value
