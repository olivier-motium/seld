from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from continuity_kernel.connector_contract import ConnectorEffect
from continuity_kernel.connector_session import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    ConnectorSession,
)
from continuity_kernel.errors import ConflictError, ValidationError


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _session(clock: FakeClock | None = None, *, capacity: int = 4_096) -> ConnectorSession:
    return ConnectorSession(
        secret=b"a" * 32,
        clock=clock or FakeClock(),
        max_spent_nonces=capacity,
    )


def _cursor(session: ConnectorSession) -> str:
    return session.issue_cursor(
        provider="demo",
        operation="items.list",
        connection_id="connection-1",
        input_value={"limit": 10, "query": "inbox"},
        connection_revision="revision-1",
        credential_version=4,
        continuation={"opaque": [1, 2]},
    )


def _open(session: ConnectorSession, token: str, **changes: object) -> object:
    expected: dict[str, object] = {
        "provider": "demo",
        "operation": "items.list",
        "connection_id": "connection-1",
        "input_value": {"query": "inbox", "limit": 10},
        "connection_revision": "revision-1",
        "credential_version": 4,
    }
    expected.update(changes)
    return session.open_cursor(token, **expected)


def _confirmation(session: ConnectorSession) -> str:
    return session.issue_confirmation(
        provider="demo",
        operation="items.send",
        connection_id="connection-1",
        effect=ConnectorEffect.OUTWARD,
        authorization_tier="full",
        granted_scopes={"items:read", "items:write"},
        mutation={"item_id": "42", "body": "hello"},
        connection_version=9,
        credential_version=4,
    )


def _consume(session: ConnectorSession, token: str, **changes: object) -> None:
    expected: dict[str, object] = {
        "provider": "demo",
        "operation": "items.send",
        "connection_id": "connection-1",
        "effect": ConnectorEffect.OUTWARD,
        "authorization_tier": "full",
        "granted_scopes": {"items:write", "items:read"},
        "mutation": {"body": "hello", "item_id": "42"},
        "connection_version": 9,
        "credential_version": 4,
    }
    expected.update(changes)
    session.consume_confirmation(token, **expected)


def test_cursor_is_canonical_detached_and_invalid_after_restart() -> None:
    clock = FakeClock()
    session = _session(clock)
    token = _cursor(session)

    continuation = _open(session, token)
    assert continuation == {"opaque": [1, 2]}
    continuation["opaque"].append(3)
    assert _open(session, token) == {"opaque": [1, 2]}
    with pytest.raises(ValidationError):
        _open(ConnectorSession(secret=b"b" * 32, clock=clock), token)


def test_cursor_rejects_tamper_cross_binding_credential_change_and_expiry() -> None:
    clock = FakeClock()
    session = _session(clock)
    token = _cursor(session)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ValidationError) as error:
        _open(session, tampered)
    assert token not in str(error.value)

    for change in (
        {"operation": "items.other"},
        {"connection_id": "connection-2"},
        {"connection_revision": "revision-2"},
        {"credential_version": 5},
        {"input_value": {"query": "private-value", "limit": 10}},
    ):
        with pytest.raises(ConflictError) as mismatch:
            _open(session, token, **change)
        assert "private-value" not in str(mismatch.value)
        assert "demo" not in str(mismatch.value)

    clock.advance(DEFAULT_TTL_SECONDS + 1)
    with pytest.raises(ConflictError, match="expired"):
        _open(session, token)


def test_confirmation_binds_exact_effect_tier_grant_mutation_and_versions() -> None:
    session = _session()
    token = _confirmation(session)
    for change in (
        {"effect": ConnectorEffect.DESTRUCTIVE},
        {"authorization_tier": "read"},
        {"granted_scopes": {"items:write"}},
        {"mutation": {"item_id": "42", "body": "changed"}},
        {"connection_version": 10},
        {"credential_version": 5},
    ):
        with pytest.raises(ConflictError):
            _consume(session, token, **change)

    _consume(session, token)
    with pytest.raises(ConflictError, match="already been consumed"):
        _consume(session, token)


def test_confirmation_capacity_fails_closed_without_enabling_replay() -> None:
    clock = FakeClock()
    session = _session(clock, capacity=1)
    first = _confirmation(session)
    second = _confirmation(session)
    _consume(session, first)

    with pytest.raises(ConflictError, match="capacity"):
        _consume(session, second)
    with pytest.raises(ConflictError, match="already been consumed"):
        _consume(session, first)

    clock.advance(DEFAULT_TTL_SECONDS + 1)
    current = _confirmation(session)
    _consume(session, current)


def test_confirmation_ttl_tamper_and_input_bounds_fail_without_payload_echo() -> None:
    clock = FakeClock()
    session = _session(clock)
    with pytest.raises(ValidationError):
        session.issue_confirmation(
            provider="demo",
            operation="items.send",
            connection_id="connection-1",
            effect=ConnectorEffect.OUTWARD,
            authorization_tier="full",
            granted_scopes={"items:write"},
            mutation={},
            connection_version=1,
            credential_version=1,
            ttl_seconds=MAX_TTL_SECONDS + 1,
        )
    token = _confirmation(session)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ValidationError):
        _consume(session, tampered)

    with pytest.raises(ValidationError):
        session.issue_cursor(
            provider="demo",
            operation="items.list",
            connection_id="connection-1",
            input_value=float("nan"),
            connection_revision="revision",
            credential_version=1,
            continuation={},
        )
    deep: object = "leaf"
    for _ in range(20):
        deep = [deep]
    with pytest.raises(ValidationError):
        session.issue_cursor(
            provider="demo",
            operation="items.list",
            connection_id="connection-1",
            input_value={},
            connection_revision="revision",
            credential_version=1,
            continuation=deep,
        )
    with pytest.raises(ValidationError):
        ConnectorSession(secret=b"short")
