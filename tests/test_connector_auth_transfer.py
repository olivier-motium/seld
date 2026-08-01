from __future__ import annotations

import base64
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from continuity_kernel import connector_auth_transfer
from continuity_kernel.connections import ConnectionSnapshot
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_auth_transfer import export_auth_archive, import_auth_archive
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import ConnectionId, parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.errors import ConflictError, MutationCommittedError, ValidationError
from continuity_kernel.vault import Vault

SENTINEL = b"synthetic-portable-secret-never-in-vault"
CONNECTION_ID = parse_connection_id("con-" + "t" * 32)


def _source_manager(tmp_path: Path) -> tuple[Vault, ConnectorAuthManager]:
    source_vault = Vault(tmp_path / "source-vault")
    source_vault.initialize(name="Source host")
    now = datetime.now(UTC)
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider="example",
        source_ids=("github",),
        credential_kind=CredentialKind.API_KEY,
        account=AccountMetadata(label="Synthetic"),
        scopes=(),
        client=ClientMetadata(kind=ClientKind.EXTERNAL),
        health=ConnectionHealth.READY,
        created_at=now,
        updated_at=now,
        version=1,
        last_verified_at=now,
    )
    source_vault.put_connection(
        expected_revision=source_vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        source_vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "source-host-state",
    )
    manager.store_credential(CONNECTION_ID, SENTINEL, expected_token_version=0)
    return source_vault, manager


def test_age_export_restore_import_moves_credentials_without_plaintext_leakage(
    tmp_path: Path,
) -> None:
    age = shutil.which("age")
    age_keygen = shutil.which("age-keygen")
    if age is None or age_keygen is None:
        pytest.skip("age and age-keygen are required for encrypted transfer proof")

    source_vault, source_manager = _source_manager(tmp_path)

    identity = tmp_path / "age-identity.txt"
    generated = subprocess.run(
        [age_keygen, "--output", str(identity)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert generated.returncode == 0, generated.stderr
    recipient_result = subprocess.run(
        [age_keygen, "--y", str(identity)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert recipient_result.returncode == 0, recipient_result.stderr
    recipient = recipient_result.stdout.strip()

    encrypted = tmp_path / "connector-auth.age"
    exported = export_auth_archive(
        source_manager,
        encrypted,
        recipient=recipient,
        age_executable=Path(age),
    )
    backup = Path(source_vault.create_backup(tmp_path / "vault.zip")["backup"])
    restored_root = tmp_path / "restored-vault"
    Vault.restore_backup(backup, restored_root)
    restored_vault = Vault(restored_root)
    target_manager = ConnectorAuthManager(
        restored_vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "target-host-state",
    )

    assert exported["connection_count"] == 1
    assert target_manager.status()["connections"][0]["host_credential"] == "missing"
    imported = import_auth_archive(
        target_manager,
        encrypted,
        identity=identity,
        age_executable=Path(age),
    )

    assert imported["requires_verification"] is True
    assert target_manager.resolve_credential(CONNECTION_ID) == SENTINEL
    restored = restored_vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert restored is not None
    assert restored.health is ConnectionHealth.UNVERIFIED
    assert SENTINEL not in encrypted.read_bytes()
    assert SENTINEL not in backup.read_bytes()
    assert SENTINEL not in (restored_root / "CONNECTIONS.md").read_bytes()
    assert all(
        SENTINEL not in path.read_bytes()
        for path in (tmp_path / "target-host-state").rglob("*")
        if path.is_file()
    )
    repeated = import_auth_archive(
        target_manager,
        encrypted,
        identity=identity,
        age_executable=Path(age),
    )
    assert repeated["imported"] is True


def test_import_resumes_after_metadata_was_committed_but_reported_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_vault, source_manager = _source_manager(tmp_path)
    captured_plaintext: bytes | None = None

    def fake_age(
        arguments: tuple[str, ...],
        content: bytes,
        *,
        executable: Path | None,
        output_bound: int,
    ) -> bytes:
        nonlocal captured_plaintext
        del executable, output_bound
        if "--encrypt" in arguments:
            captured_plaintext = content
            return b"synthetic-age-ciphertext"
        assert captured_plaintext is not None
        return captured_plaintext

    monkeypatch.setattr(connector_auth_transfer, "_run_age", fake_age)
    encrypted = tmp_path / "connector-auth.age"
    export_auth_archive(source_manager, encrypted, recipient="synthetic-recipient")
    backup = Path(source_vault.create_backup(tmp_path / "vault.zip")["backup"])
    restored_root = tmp_path / "restored-vault"
    Vault.restore_backup(backup, restored_root)
    target_manager = ConnectorAuthManager(
        Vault(restored_root),
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "target-host-state",
    )
    original_replace = target_manager.vault.replace_connections
    fail_once = True

    def committed_failure(
        *,
        expected_revision: str,
        snapshot: ConnectionSnapshot,
        operation: str = "connection.import",
    ) -> dict[str, object]:
        nonlocal fail_once
        result = original_replace(
            expected_revision=expected_revision,
            snapshot=snapshot,
            operation=operation,
        )
        if fail_once:
            fail_once = False
            raise MutationCommittedError("synthetic committed metadata")
        return result

    monkeypatch.setattr(target_manager.vault, "replace_connections", committed_failure)
    identity = tmp_path / "synthetic-identity"
    identity.write_text("synthetic", encoding="utf-8")

    with pytest.raises(MutationCommittedError, match="visible"):
        import_auth_archive(target_manager, encrypted, identity=identity)

    visible = target_manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert visible is not None
    assert visible.health is ConnectionHealth.UNVERIFIED
    assert target_manager.status()["connections"][0]["host_credential"] == "missing"

    resumed = import_auth_archive(target_manager, encrypted, identity=identity)
    assert resumed["imported"] is True
    assert target_manager.resolve_credential(CONNECTION_ID) == SENTINEL


def test_export_rejects_connection_drift_before_encrypting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_vault, source_manager = _source_manager(tmp_path)
    original_read = source_manager.tokens.read

    def read_then_drift(connection_id: ConnectionId):
        resolved = original_read(connection_id)
        snapshot = source_vault.get_connection_snapshot()
        metadata = snapshot.connection(CONNECTION_ID)
        assert metadata is not None
        observed_at = max(datetime.now(UTC), metadata.updated_at + timedelta(microseconds=1))
        source_vault.put_connection(
            expected_revision=snapshot.revision,
            connection=replace(
                metadata,
                health=ConnectionHealth.DEGRADED,
                updated_at=observed_at,
                version=metadata.version + 1,
            ),
            observed_at=observed_at,
        )
        return resolved

    monkeypatch.setattr(source_manager.tokens, "read", read_then_drift)
    monkeypatch.setattr(
        connector_auth_transfer,
        "_run_age",
        lambda *_args, **_kwargs: pytest.fail("encryption must not start after snapshot drift"),
    )
    destination = tmp_path / "connector-auth.age"

    with pytest.raises(ConflictError, match="changed during auth export"):
        export_auth_archive(
            source_manager,
            destination,
            recipient="synthetic-recipient",
        )

    assert not destination.exists()


def test_export_reports_missing_host_credential_before_encryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_vault, manager = _source_manager(tmp_path)
    manager.tokens.delete(CONNECTION_ID)
    destination = tmp_path / "connector-auth.age"
    monkeypatch.setattr(
        connector_auth_transfer,
        "_run_age",
        lambda *_args, **_kwargs: pytest.fail("encryption must not start without custody"),
    )

    with pytest.raises(ValidationError) as failure:
        export_auth_archive(
            manager,
            destination,
            recipient="synthetic-recipient",
        )

    message = str(failure.value)
    assert str(CONNECTION_ID) in message
    assert "authorize or remove it" in message
    assert not destination.exists()


def test_import_rejects_an_overbroad_oauth_grant_before_mutating_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="OAuth import scope boundary")
    now = datetime.now(UTC)
    profile = get_profile("slack")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(label="Synthetic Slack"),
        scopes=profile.read_scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-slack-client",
            redirect_uris=("http://localhost:49152/oauth/callback",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    before = vault.get_connection_snapshot()
    overbroad = OAuthCredential(
        access_token="overbroad-slack-token",
        refresh_token=None,
        token_type=OAuthTokenType.BEARER,
        scopes=(*profile.read_scopes, "chat:write"),
        issued_at=now,
        expires_at=None,
    ).to_bytes()
    plaintext = connector_auth_transfer._encode_archive(
        {
            "connection_revision": before.revision,
            "connections": [
                {
                    "credential": base64.b64encode(overbroad).decode("ascii"),
                    "metadata": metadata.to_dict(),
                }
            ],
            "format_version": connector_auth_transfer.AUTH_ARCHIVE_FORMAT_VERSION,
            "vault_id": manager.vault_id,
        }
    )

    def decrypt(
        arguments: tuple[str, ...],
        content: bytes,
        *,
        executable: Path | None,
        output_bound: int,
    ) -> bytes:
        del arguments, content, executable, output_bound
        return plaintext

    monkeypatch.setattr(connector_auth_transfer, "_run_age", decrypt)
    encrypted = tmp_path / "auth.age"
    encrypted.write_bytes(b"synthetic ciphertext")
    identity = tmp_path / "identity.txt"
    identity.write_text("synthetic identity", encoding="utf-8")

    with pytest.raises(ValidationError, match="outside its selected access"):
        import_auth_archive(manager, encrypted, identity=identity)

    assert vault.get_connection_snapshot() == before
    assert manager.tokens.state(CONNECTION_ID) is None
