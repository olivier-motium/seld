"""Process-local authenticated cursors and single-use confirmations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from threading import Lock
from typing import Final, cast

from continuity_kernel.connector_contract import (
    MAX_COLLECTION_ITEMS,
    MAX_SEALED_TOKEN_LENGTH,
    ConnectorEffect,
    canonical_json,
    canonical_json_digest,
    canonicalize_json,
)
from continuity_kernel.errors import ConflictError, ValidationError

TOKEN_VERSION: Final = 1
DEFAULT_TTL_SECONDS: Final = 10 * 60
MAX_TTL_SECONDS: Final = 15 * 60
MAX_TOKEN_LENGTH: Final = MAX_SEALED_TOKEN_LENGTH
MAX_SPENT_NONCES: Final = 4_096

_SECRET_BYTES = 32
_NONCE_BYTES = 16
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROCESS_SECRET: Final = secrets.token_bytes(_SECRET_BYTES)

Clock = Callable[[], datetime | int | float]


class ConnectorSession:
    """Bind opaque continuations and approvals to exact live connector state."""

    def __init__(
        self,
        *,
        secret: bytes | None = None,
        clock: Clock | None = None,
        max_spent_nonces: int = MAX_SPENT_NONCES,
    ) -> None:
        key = _PROCESS_SECRET if secret is None else secret
        if not isinstance(key, bytes) or len(key) != _SECRET_BYTES:
            raise ValidationError("connector session secret must be exactly 32 bytes")
        if clock is not None and not callable(clock):
            raise ValidationError("connector session clock is invalid")
        if type(max_spent_nonces) is not int or not 1 <= max_spent_nonces <= 65_536:
            raise ValidationError("connector confirmation capacity is invalid")
        self._secret = key
        self._clock: Clock = clock or time.time
        self._max_spent_nonces = max_spent_nonces
        self._spent_nonces: dict[str, float] = {}
        self._spent_lock = Lock()

    def issue_cursor(
        self,
        *,
        provider: str,
        operation: str,
        connection_id: str,
        input_value: object,
        connection_revision: str,
        credential_version: int,
        continuation: object,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        issued_at = self._now()
        payload: dict[str, object] = {
            "connection_id": _binding_text(connection_id),
            "connection_revision": _binding_text(connection_revision),
            "continuation": canonicalize_json(continuation),
            "credential_version": _positive_version(credential_version),
            "expires_at": issued_at + _ttl(ttl_seconds),
            "input_digest": canonical_json_digest(input_value),
            "issued_at": issued_at,
            "kind": "cursor",
            "operation": _binding_text(operation),
            "provider": _binding_text(provider),
            "version": TOKEN_VERSION,
        }
        return self._encode(payload)

    def open_cursor(
        self,
        token: object,
        *,
        provider: str,
        operation: str,
        connection_id: str,
        input_value: object,
        connection_revision: str,
        credential_version: int,
    ) -> object:
        payload = self._decode(token, kind="cursor")
        self._validate_times(payload, kind="cursor")
        expected: dict[str, object] = {
            "connection_id": _binding_text(connection_id),
            "connection_revision": _binding_text(connection_revision),
            "credential_version": _positive_version(credential_version),
            "input_digest": canonical_json_digest(input_value),
            "operation": _binding_text(operation),
            "provider": _binding_text(provider),
        }
        _require_bindings(payload, expected, kind="cursor")
        return canonicalize_json(payload["continuation"])

    def issue_confirmation(
        self,
        *,
        provider: str,
        operation: str,
        connection_id: str,
        effect: ConnectorEffect,
        authorization_tier: str,
        granted_scopes: Sequence[str] | set[str],
        mutation: object,
        connection_version: int,
        credential_version: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        issued_at = self._now()
        payload: dict[str, object] = {
            "authorization_tier": _binding_text(authorization_tier),
            "connection_id": _binding_text(connection_id),
            "connection_version": _positive_version(connection_version),
            "credential_version": _positive_version(credential_version),
            "effect": _effect(effect),
            "expires_at": issued_at + _ttl(ttl_seconds),
            "granted_scope_digest": _scope_digest(granted_scopes),
            "issued_at": issued_at,
            "kind": "confirmation",
            "mutation_digest": canonical_json_digest(mutation),
            "nonce": _b64encode(secrets.token_bytes(_NONCE_BYTES)),
            "operation": _binding_text(operation),
            "provider": _binding_text(provider),
            "version": TOKEN_VERSION,
        }
        return self._encode(payload)

    def consume_confirmation(
        self,
        token: object,
        *,
        provider: str,
        operation: str,
        connection_id: str,
        effect: ConnectorEffect,
        authorization_tier: str,
        granted_scopes: Sequence[str] | set[str],
        mutation: object,
        connection_version: int,
        credential_version: int,
    ) -> None:
        """Validate and spend the nonce before the caller performs a mutation."""

        payload = self._decode(token, kind="confirmation")
        now, expires_at = self._validate_times(payload, kind="confirmation")
        expected: dict[str, object] = {
            "authorization_tier": _binding_text(authorization_tier),
            "connection_id": _binding_text(connection_id),
            "connection_version": _positive_version(connection_version),
            "credential_version": _positive_version(credential_version),
            "effect": _effect(effect),
            "granted_scope_digest": _scope_digest(granted_scopes),
            "mutation_digest": canonical_json_digest(mutation),
            "operation": _binding_text(operation),
            "provider": _binding_text(provider),
        }
        _require_bindings(payload, expected, kind="confirmation")
        nonce = cast(str, payload["nonce"])
        with self._spent_lock:
            self._prune_spent(now)
            if nonce in self._spent_nonces:
                raise ConflictError("confirmation token has already been consumed")
            if len(self._spent_nonces) >= self._max_spent_nonces:
                raise ConflictError("confirmation capacity is full; wait for an approval to expire")
            self._spent_nonces[nonce] = expires_at

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise ValidationError("connector session clock is unavailable") from exc
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValidationError("connector session clock must be timezone-aware")
            result = value.astimezone(UTC).timestamp()
        elif type(value) in {int, float}:
            result = float(value)
        else:
            raise ValidationError("connector session clock returned an invalid time")
        if not math.isfinite(result):
            raise ValidationError("connector session clock returned a non-finite time")
        return result

    def _encode(self, payload: Mapping[str, object]) -> str:
        encoded = canonical_json(payload)
        mac = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        token = f"v{TOKEN_VERSION}.{_b64encode(encoded)}.{_b64encode(mac)}"
        if len(token) > MAX_TOKEN_LENGTH:
            raise ValidationError("sealed token exceeds its size bound")
        return token

    def _decode(self, token: object, *, kind: str) -> dict[str, object]:
        if not isinstance(token, str) or not token.isascii() or len(token) > MAX_TOKEN_LENGTH:
            raise ValidationError("sealed token is invalid")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != f"v{TOKEN_VERSION}":
            raise ValidationError("sealed token is invalid")
        encoded = _b64decode(parts[1])
        actual_mac = _b64decode(parts[2])
        expected_mac = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        if len(actual_mac) != len(expected_mac) or not hmac.compare_digest(
            actual_mac, expected_mac
        ):
            raise ValidationError("sealed token is invalid")
        try:
            decoded = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("sealed token payload is invalid") from exc
        if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
            raise ValidationError("sealed token payload is invalid")
        payload = cast(dict[str, object], decoded)
        if canonical_json(payload) != encoded:
            raise ValidationError("sealed token payload is not canonical")
        expected_keys = _CURSOR_KEYS if kind == "cursor" else _CONFIRMATION_KEYS
        if set(payload) != expected_keys or payload.get("kind") != kind:
            raise ValidationError("sealed token payload is invalid")
        if payload.get("version") != TOKEN_VERSION:
            raise ValidationError("sealed token version is unsupported")
        _validate_payload_fields(payload, kind=kind)
        return payload

    def _validate_times(
        self,
        payload: Mapping[str, object],
        *,
        kind: str,
    ) -> tuple[float, float]:
        issued_at = payload.get("issued_at")
        expires_at = payload.get("expires_at")
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or not math.isfinite(issued_at)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at)
            or expires_at <= issued_at
            or expires_at - issued_at > MAX_TTL_SECONDS
        ):
            raise ValidationError(f"sealed {kind} time binding is invalid")
        now = self._now()
        if now < issued_at:
            raise ConflictError(f"sealed {kind} is not yet valid")
        if now >= expires_at:
            raise ConflictError(f"sealed {kind} has expired")
        return now, float(expires_at)

    def _prune_spent(self, now: float) -> None:
        for nonce, expiry in tuple(self._spent_nonces.items()):
            if expiry <= now:
                del self._spent_nonces[nonce]


_CURSOR_KEYS: Final = frozenset(
    {
        "connection_id",
        "connection_revision",
        "continuation",
        "credential_version",
        "expires_at",
        "input_digest",
        "issued_at",
        "kind",
        "operation",
        "provider",
        "version",
    }
)
_CONFIRMATION_KEYS: Final = frozenset(
    {
        "authorization_tier",
        "connection_id",
        "connection_version",
        "credential_version",
        "effect",
        "expires_at",
        "granted_scope_digest",
        "issued_at",
        "kind",
        "mutation_digest",
        "nonce",
        "operation",
        "provider",
        "version",
    }
)


def _validate_payload_fields(payload: Mapping[str, object], *, kind: str) -> None:
    for name in ("connection_id", "operation", "provider"):
        _binding_text(payload[name])
    _positive_version(payload["credential_version"])
    if kind == "cursor":
        _binding_text(payload["connection_revision"])
        _digest(payload["input_digest"])
        canonicalize_json(payload["continuation"])
        return
    _binding_text(payload["authorization_tier"])
    _positive_version(payload["connection_version"])
    _effect(payload["effect"])
    _digest(payload["granted_scope_digest"])
    _digest(payload["mutation_digest"])
    nonce = payload["nonce"]
    if not isinstance(nonce, str) or len(_b64decode(nonce)) != _NONCE_BYTES:
        raise ValidationError("confirmation nonce is invalid")


def _require_bindings(
    actual: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    kind: str,
) -> None:
    if any(actual.get(name) != value for name, value in expected.items()):
        raise ConflictError(f"sealed {kind} binding does not match")


def _binding_text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise ValidationError("connector binding text is invalid")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("connector binding text is invalid") from exc
    return value


def _positive_version(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValidationError("connector version is invalid")
    return value


def _effect(value: object) -> str:
    if isinstance(value, ConnectorEffect):
        return value.value
    if isinstance(value, str):
        try:
            return ConnectorEffect(value).value
        except ValueError as exc:
            raise ValidationError("connector effect is invalid") from exc
    raise ValidationError("connector effect is invalid")


def _scope_digest(scopes: object) -> str:
    if isinstance(scopes, str) or not isinstance(scopes, (list, tuple, set, frozenset)):
        raise ValidationError("confirmation granted scopes are invalid")
    if len(scopes) > MAX_COLLECTION_ITEMS:
        raise ValidationError("confirmation granted scopes exceed their bound")
    normalized: set[str] = set()
    for scope in scopes:
        if (
            not isinstance(scope, str)
            or not scope
            or len(scope) > 2_048
            or "\x00" in scope
            or any(character.isspace() for character in scope)
        ):
            raise ValidationError("confirmation granted scopes are invalid")
        normalized.add(scope)
    return canonical_json_digest(sorted(normalized))


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValidationError("connector binding digest is invalid")
    return value


def _ttl(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TTL_SECONDS:
        raise ValidationError("sealed token TTL is outside its bound")
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or _B64URL.fullmatch(value) is None:
        raise ValidationError("sealed token is invalid")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValidationError("sealed token is invalid") from exc
    if _b64encode(decoded) != value:
        raise ValidationError("sealed token is not canonical")
    return decoded


def _object_without_duplicates(pairs: list[tuple[object, object]]) -> dict[object, object]:
    result: dict[object, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError
