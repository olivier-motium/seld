from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from continuity_kernel.connections import (
    ABSENT_CONNECTION_REVISION,
    MAX_CONNECTION_STATE_BYTES,
    ConnectionSnapshot,
    connection_snapshot_dict,
    empty_connection_snapshot,
    mark_connection_health,
    parse_connection_snapshot,
    put_connection,
    remove_connection,
    render_connection_snapshot,
)
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.errors import ConflictError, NotFoundError, ValidationError
from continuity_kernel.vault import Vault

BASE_TIME = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _metadata(
    marker: str = "a",
    *,
    health: ConnectionHealth = ConnectionHealth.UNKNOWN,
    version: int = 1,
    updated_at: datetime = BASE_TIME,
) -> ConnectionMetadata:
    return ConnectionMetadata(
        connection_id=parse_connection_id("con-" + marker * 32),
        provider="example",
        source_ids=("example.messages",),
        credential_kind=CredentialKind.OAUTH2,
        account=AccountMetadata(fingerprint="sha256:" + marker * 64, label="Example"),
        scopes=("messages.read",),
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="seld-public-client",
            redirect_uris=("http://127.0.0.1:49152/callback",),
            authorization_endpoint="https://accounts.example/authorize",
            token_endpoint="https://accounts.example/token",
        ),
        health=health,
        created_at=BASE_TIME,
        updated_at=updated_at,
        version=version,
    )


def test_connection_snapshot_round_trips_as_redacted_portable_state() -> None:
    metadata = replace(
        _metadata(),
        account=AccountMetadata(
            fingerprint="sha256:" + "a" * 64,
            label="Injected -->\n# heading",
        ),
    )
    snapshot = put_connection(
        empty_connection_snapshot(),
        metadata,
        observed_at=BASE_TIME,
    )
    encoded = render_connection_snapshot(snapshot).encode()

    assert parse_connection_snapshot(encoded, observed_at=BASE_TIME) == snapshot
    assert snapshot.revision != ABSENT_CONNECTION_REVISION
    public = json.dumps(connection_snapshot_dict(snapshot), sort_keys=True)
    assert "secret://" not in public
    assert "access-token-sentinel" not in public
    assert b"Injected" not in encoded


def test_connection_snapshot_enforces_record_version_lineage() -> None:
    initial = put_connection(empty_connection_snapshot(), _metadata(), observed_at=BASE_TIME)
    stale = replace(_metadata(), updated_at=BASE_TIME + timedelta(hours=1))

    with pytest.raises(ConflictError, match="version is stale"):
        put_connection(initial, stale, observed_at=stale.updated_at)

    rebound = replace(
        _metadata(),
        provider="other-provider",
        version=2,
        updated_at=BASE_TIME + timedelta(hours=1),
    )
    with pytest.raises(ConflictError, match="binding cannot change"):
        put_connection(initial, rebound, observed_at=rebound.updated_at)

    verified = mark_connection_health(
        initial,
        _metadata().connection_id,
        ConnectionHealth.READY,
        verified=True,
        observed_at=BASE_TIME + timedelta(hours=1),
    )
    current = verified.connection(_metadata().connection_id)
    assert current is not None
    assert current.version == 2
    assert current.health is ConnectionHealth.READY
    assert current.last_verified_at == BASE_TIME + timedelta(hours=1)
    with pytest.raises(ConflictError, match="observation is stale"):
        mark_connection_health(
            verified,
            _metadata().connection_id,
            ConnectionHealth.DEGRADED,
            observed_at=BASE_TIME,
        )


def test_connection_snapshot_rejects_duplicate_and_noncanonical_bytes() -> None:
    metadata = _metadata()
    duplicate = ConnectionSnapshot(
        format_version=1,
        connections=(metadata, metadata),
        updated_at="2026-07-01T09:00:00.000000Z",
        revision="",
    )
    with pytest.raises(ValidationError, match="sorted and unique"):
        parse_connection_snapshot(
            render_connection_snapshot(duplicate).encode(),
            observed_at=BASE_TIME,
        )

    canonical = put_connection(empty_connection_snapshot(), metadata, observed_at=BASE_TIME)
    with pytest.raises(ValidationError, match="canonical form"):
        parse_connection_snapshot(
            render_connection_snapshot(canonical).encode() + b" ",
            observed_at=BASE_TIME,
        )


def test_connection_snapshot_rejects_future_and_oversized_state() -> None:
    future = BASE_TIME + timedelta(minutes=2)
    with pytest.raises(ValidationError, match="future"):
        put_connection(
            empty_connection_snapshot(),
            _metadata(updated_at=future),
            observed_at=BASE_TIME,
        )
    with pytest.raises(ValidationError, match="size bound"):
        parse_connection_snapshot(b"x" * (MAX_CONNECTION_STATE_BYTES + 1))


def test_connection_removal_is_explicit_and_preserves_other_records() -> None:
    first = put_connection(empty_connection_snapshot(), _metadata("a"), observed_at=BASE_TIME)
    second = put_connection(first, _metadata("b"), observed_at=BASE_TIME)

    removed = remove_connection(
        second,
        _metadata("a").connection_id,
        observed_at=BASE_TIME + timedelta(hours=1),
    )

    assert removed.connection(_metadata("a").connection_id) is None
    assert removed.connection(_metadata("b").connection_id) is not None
    with pytest.raises(NotFoundError, match="not found"):
        remove_connection(removed, _metadata("a").connection_id)


def test_vault_persists_catalog_bound_connections_and_doctor_detects_corruption(
    tmp_path: Path,
) -> None:
    root = tmp_path / "connections-vault"
    vault = Vault(root)
    vault.initialize(name="Connections")
    absent = vault.get_connection_snapshot()

    with pytest.raises(ValidationError, match="unsupported source recipe"):
        vault.put_connection(
            expected_revision=absent.revision,
            connection=_metadata(),
            observed_at=BASE_TIME,
        )

    valid = replace(_metadata(), source_ids=("gmail",))
    persisted = vault.put_connection(
        expected_revision=absent.revision,
        connection=valid,
        observed_at=BASE_TIME,
    )
    restarted = Vault(root)
    assert restarted.connection_status()["revision"] == persisted["revision"]
    assert restarted.doctor().healthy is True

    (root / "CONNECTIONS.md").write_bytes(b"not canonical connection state\n")
    doctor = restarted.doctor()
    assert doctor.healthy is False
    assert any(issue.code == "invalid-connections" for issue in doctor.issues)
