from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from continuity_kernel.atomic import atomic_write
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_identifiers import (
    SecretReference,
    new_connection_id,
    parse_connection_id,
    parse_secret_name,
    parse_secret_reference,
    resolve_secret_reference,
)
from continuity_kernel.connector_secrets import InMemorySecretStore, KeyringSecretStore
from continuity_kernel.connector_token_store import AtomicTokenStore
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    NotFoundError,
    SetupError,
    ValidationError,
)


def _connection_id() -> str:
    return "con-" + "a" * 32


def test_connection_metadata_round_trips_without_a_secret_field() -> None:
    created = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
    updated = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    metadata = ConnectionMetadata(
        connection_id=parse_connection_id(_connection_id()),
        provider="example-provider",
        source_ids=("example.messages", "example.calendar"),
        credential_kind=CredentialKind.OAUTH2,
        account=AccountMetadata(fingerprint="sha256:" + "4" * 64, label="Work account"),
        scopes=("write:items", "read:items"),
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="portable-client",
            redirect_uris=("http://127.0.0.1:49152/callback",),
            authorization_endpoint="https://accounts.example/authorize",
            token_endpoint="https://accounts.example/token",
        ),
        health=ConnectionHealth.READY,
        created_at=created,
        updated_at=updated,
        last_verified_at=updated,
        version=3,
    )

    encoded = metadata.to_json()
    parsed = ConnectionMetadata.from_json(encoded)

    assert parsed == metadata
    assert ConnectionMetadata.from_dict(metadata.to_dict()) == metadata
    assert parsed.scopes == ("read:items", "write:items")
    assert b"secret" not in encoded.lower()
    assert set(json.loads(encoded)) == {
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
    }

    with pytest.raises(ValidationError, match="unsupported shape"):
        ConnectionMetadata.from_dict({**metadata.to_dict(), "secret_reference": "forbidden"})


def test_connection_metadata_requires_opaque_ids_and_utc_times() -> None:
    with pytest.raises(ValidationError, match="connection ID"):
        parse_connection_id("github-olivier")

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        ConnectionMetadata(
            connection_id=parse_connection_id(_connection_id()),
            provider="github",
            source_ids=("github",),
            credential_kind=CredentialKind.OAUTH2,
            account=AccountMetadata(),
            scopes=(),
            client=ClientMetadata(kind=ClientKind.PUBLIC),
            health=ConnectionHealth.UNKNOWN,
            created_at=datetime(2026, 7, 30),
            updated_at=datetime(2026, 7, 30),
            version=1,
        )


def test_connection_metadata_rejects_raw_account_ids_and_invalid_sources() -> None:
    with pytest.raises(ValidationError, match="fingerprint"):
        AccountMetadata(fingerprint="acct-42")

    with pytest.raises(ValidationError, match="source IDs"):
        ConnectionMetadata(
            connection_id=parse_connection_id(_connection_id()),
            provider="github",
            source_ids=("../github",),
            credential_kind=CredentialKind.BEARER,
            account=AccountMetadata(),
            scopes=(),
            client=ClientMetadata(kind=ClientKind.EXTERNAL),
            health=ConnectionHealth.UNKNOWN,
            created_at=datetime(2026, 7, 30, tzinfo=UTC),
            updated_at=datetime(2026, 7, 30, tzinfo=UTC),
            version=1,
        )


def test_generated_connection_ids_are_opaque_and_valid() -> None:
    first = new_connection_id()
    second = new_connection_id()

    assert first != second
    assert parse_connection_id(first) == first
    assert len(first) == 36


def test_secret_reference_parser_and_resolver_are_strict() -> None:
    connection_id = parse_connection_id(_connection_id())
    name = parse_secret_name("refresh-token")
    reference = SecretReference(connection_id=connection_id, name=name)
    store = InMemorySecretStore()
    store.set_secret(connection_id, name, b"opaque-token")

    assert parse_secret_reference(str(reference)) == reference
    assert resolve_secret_reference(str(reference), store) == b"opaque-token"
    with pytest.raises(ValidationError, match="secret reference"):
        parse_secret_reference(f"secret://{connection_id}/../refresh-token")
    with pytest.raises(ValidationError, match="secret reference"):
        parse_secret_reference(f"secret://{connection_id}/refresh-token?copy=true")
    with pytest.raises(NotFoundError, match="not found"):
        resolve_secret_reference(
            SecretReference(connection_id, parse_secret_name("access-token")),
            store,
        )


def test_keyring_backend_fails_closed_when_dependency_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_module(_name: str) -> object:
        raise ModuleNotFoundError("keyring is absent")

    monkeypatch.setattr(
        "continuity_kernel.connector_secrets.importlib.import_module",
        missing_module,
    )
    store = KeyringSecretStore()

    with pytest.raises(SetupError, match="OS keyring is unavailable"):
        store.get_secret(
            parse_connection_id(_connection_id()),
            parse_secret_name("access-token"),
        )


def test_keyring_backend_rejects_non_os_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    class InsecureBackend:
        priority = 1.0

    class FakeModule:
        def get_keyring(self) -> InsecureBackend:
            return InsecureBackend()

        def get_password(self, service: str, username: str) -> str | None:
            raise AssertionError((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            raise AssertionError((service, username, password))

        def delete_password(self, service: str, username: str) -> None:
            raise AssertionError((service, username))

    monkeypatch.setattr(
        "continuity_kernel.connector_secrets.importlib.import_module",
        lambda _name: FakeModule(),
    )

    with pytest.raises(SetupError, match="not an approved OS keyring"):
        KeyringSecretStore().get_secret(
            parse_connection_id(_connection_id()),
            parse_secret_name("access-token"),
        )


def test_keyring_backend_encodes_binary_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    class SecureBackend:
        priority = 5.0

    SecureBackend.__module__ = "keyring.backends.macOS"

    class FakeModule:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], str] = {}

        def get_keyring(self) -> SecureBackend:
            return SecureBackend()

        def get_password(self, service: str, username: str) -> str | None:
            return self.values.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.values[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            self.values.pop((service, username), None)

    module = FakeModule()
    monkeypatch.setattr(
        "continuity_kernel.connector_secrets.importlib.import_module",
        lambda _name: module,
    )
    store = KeyringSecretStore()
    connection_id = parse_connection_id(_connection_id())
    name = parse_secret_name("access-token")

    store.set_secret(connection_id, name, b"\x00binary\xff")

    assert store.get_secret(connection_id, name) == b"\x00binary\xff"
    assert all(value != "\x00binary\xff" for value in module.values.values())


def test_atomic_token_update_persists_only_a_secret_reference(tmp_path: Path) -> None:
    connection_id = parse_connection_id(_connection_id())
    secrets_store = InMemorySecretStore()
    tokens = AtomicTokenStore(tmp_path / "connector-auth", secrets_store)

    first = tokens.update(connection_id, expected_version=0, value=b"first-secret")
    first_reference = first.secret_reference
    second = tokens.update(connection_id, expected_version=1, value=b"second-secret")

    assert second.version == 2
    assert tokens.read(connection_id).value == b"second-secret"
    assert tokens.state(connection_id) == second
    state_bytes = (tmp_path / "connector-auth/state" / f"{connection_id}.json").read_bytes()
    assert b"first-secret" not in state_bytes
    assert b"second-secret" not in state_bytes
    assert b"secret://" in state_bytes
    with pytest.raises(NotFoundError):
        resolve_secret_reference(first_reference, secrets_store)


def test_atomic_token_update_rejects_a_stale_version(tmp_path: Path) -> None:
    connection_id = parse_connection_id(_connection_id())
    tokens = AtomicTokenStore(tmp_path / "connector-auth", InMemorySecretStore())
    tokens.update(connection_id, expected_version=0, value=b"first-secret")

    with pytest.raises(ConflictError, match="reload"):
        tokens.update(connection_id, expected_version=0, value=b"stale-secret")

    assert tokens.read(connection_id).value == b"first-secret"


def test_token_delete_revokes_pointer_and_both_bounded_rotation_slots(tmp_path: Path) -> None:
    connection_id = parse_connection_id(_connection_id())
    secrets_store = InMemorySecretStore()
    tokens = AtomicTokenStore(tmp_path / "connector-auth", secrets_store)
    tokens.update(connection_id, expected_version=0, value=b"first-secret")
    tokens.update(connection_id, expected_version=1, value=b"second-secret")

    assert tokens.delete(connection_id) is True

    assert tokens.state(connection_id) is None
    assert tokens.occupied(connection_id) is False
    with pytest.raises(NotFoundError):
        tokens.read(connection_id)


def test_exact_credential_import_resumes_after_visible_pointer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_id = parse_connection_id(_connection_id())
    tokens = AtomicTokenStore(tmp_path / "connector-auth", InMemorySecretStore())
    fail_once = True

    def visible_failure(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal fail_once
        atomic_write(path, content, mode=mode)
        if fail_once:
            fail_once = False
            raise OSError("synthetic post-publication failure")

    monkeypatch.setattr(
        "continuity_kernel.connector_token_store.atomic_write",
        visible_failure,
    )

    with pytest.raises(DegradedIntegrityError, match="partially committed"):
        tokens.ensure_imported(connection_id, b"archive-secret")

    assert tokens.read(connection_id).value == b"archive-secret"
    resumed = tokens.ensure_imported(connection_id, b"archive-secret")
    assert resumed.version == 1
    assert tokens.read(connection_id).value == b"archive-secret"


def test_two_token_writers_with_one_version_produce_one_winner(tmp_path: Path) -> None:
    connection_id = parse_connection_id(_connection_id())
    tokens = AtomicTokenStore(tmp_path / "connector-auth", InMemorySecretStore())
    tokens.update(connection_id, expected_version=0, value=b"seed")

    def update(value: bytes) -> str:
        try:
            tokens.update(connection_id, expected_version=1, value=value)
            return "won"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, (b"writer-one", b"writer-two")))

    assert sorted(outcomes) == ["conflict", "won"]
    assert tokens.read(connection_id).value in {b"writer-one", b"writer-two"}
