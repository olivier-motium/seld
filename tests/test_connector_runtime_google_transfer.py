from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import connector_gmail_transfer as gmail_transfer
from continuity_kernel.connector_adapter import ConnectorAdapterRegistry
from continuity_kernel.connector_adapter_google import GoogleConnectorAdapter
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_contract import ConnectorEffect
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_session import ConnectorSession
from continuity_kernel.connector_transfer import ArtifactStore
from continuity_kernel.connector_transport import (
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault

_CONNECTION_ID = parse_connection_id("con-" + "g" * 32)


class _NoProviderHttp(ConnectorTransport):
    def __init__(self) -> None:
        self.call_count = 0

    def _fail(self) -> None:
        self.call_count += 1
        raise AssertionError("provider HTTP must not run during transfer preview")

    def request(self, **kwargs: Any) -> ConnectorResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")

    def request_provider_location(self, **kwargs: Any) -> ConnectorResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")

    def download_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")


def _runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Vault, ConnectorRuntime, _NoProviderHttp]:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Google transfer runtime")
    profile = get_profile("google")
    now = datetime.now(UTC)
    full_scopes = profile.scopes_for("full")
    connection = ConnectionMetadata(
        connection_id=_CONNECTION_ID,
        provider="google",
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(label="Configured Google Full"),
        scopes=full_scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-google-client",
            redirect_uris=("http://127.0.0.1:0",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.READY,
        created_at=now,
        updated_at=now,
        version=1,
        last_verified_at=now,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=connection,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    credential = OAuthCredential(
        access_token="runtime-access-token",
        refresh_token="runtime-refresh-token",
        token_type=OAuthTokenType.BEARER,
        scopes=full_scopes,
        issued_at=now,
        expires_at=None,
    )
    manager.ensure_imported_credential(connection, credential.to_bytes())
    transport = _NoProviderHttp()
    runtime = ConnectorRuntime(
        vault,
        adapters=ConnectorAdapterRegistry((GoogleConnectorAdapter(),)),
        auth_manager=manager,
        transport=transport,
        session=ConnectorSession(secret=b"g" * 32),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    return vault, runtime, transport


def _write_sized_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)


def _fail_confirmation_issue(*args: object, **kwargs: object) -> str:
    del args, kwargs
    raise AssertionError("confirmation must not be issued for an oversized Gmail MIME upload")


def test_gmail_near_limit_local_attachment_fails_before_confirmation_or_provider_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "near-limit-root"
        _write_sized_file(selected / "near-limit.bin", 26_818_981)
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        monkeypatch.setattr(runtime.session, "issue_confirmation", _fail_confirmation_issue)

        with pytest.raises(ValidationError, match="documented provider upload limit"):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {
                        "attachments": [
                            {
                                "filename": "near-limit.bin",
                                "local_file": {
                                    "grant_id": grant_id,
                                    "relative_path": "near-limit.bin",
                                },
                                "mime_type": "application/octet-stream",
                            }
                        ],
                        "text_body": "Draft body",
                    },
                    "operation": "drafts.create",
                },
            )
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_aggregate_local_attachments_fail_before_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "aggregate-root"
        _write_sized_file(selected / "first.bin", 13_409_491)
        _write_sized_file(selected / "second.bin", 13_409_490)
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        monkeypatch.setattr(runtime.session, "issue_confirmation", _fail_confirmation_issue)

        with pytest.raises(ValidationError, match="documented provider upload limit"):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {
                        "attachments": [
                            {
                                "filename": "first.bin",
                                "local_file": {
                                    "grant_id": grant_id,
                                    "relative_path": "first.bin",
                                },
                                "mime_type": "application/octet-stream",
                            },
                            {
                                "filename": "second.bin",
                                "local_file": {
                                    "grant_id": grant_id,
                                    "relative_path": "second.bin",
                                },
                                "mime_type": "application/octet-stream",
                            },
                        ],
                        "text_body": "Draft body",
                    },
                    "operation": "drafts.create",
                },
            )
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_confirmed_replay_rechecks_mime_limit_before_consuming_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "replay-root"
        _write_sized_file(selected / "small.bin", 64)
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        values = {
            "connection_id": str(_CONNECTION_ID),
            "input": {
                "attachments": [
                    {
                        "filename": "small.bin",
                        "local_file": {
                            "grant_id": grant_id,
                            "relative_path": "small.bin",
                        },
                        "mime_type": "application/octet-stream",
                    }
                ],
                "text_body": "Draft body",
            },
            "operation": "drafts.create",
        }
        preview = runtime.call_tool("gsv_gmail_write", values)
        token = preview["confirmation_token"]
        assert isinstance(token, str)
        assert transport.call_count == 0

        consumed = False

        def record_consume(*args: object, **kwargs: object) -> str:
            nonlocal consumed
            consumed = True
            del args, kwargs
            raise AssertionError("confirmation must not be consumed after MIME validation fails")

        monkeypatch.setattr(runtime.session, "consume_confirmation", record_consume)
        monkeypatch.setattr(gmail_transfer, "GMAIL_UPLOAD_MAX_BYTES", 1)

        with pytest.raises(ValidationError, match="documented provider upload limit"):
            runtime.call_tool("gsv_gmail_write", {**values, "confirmation_token": token})
        assert consumed is False
        assert runtime.prepared_uploads.peek(token) is not None
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_google_transfer_previews_reach_confirmation_without_provider_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "granted-root"
        selected.mkdir()
        (selected / "gmail.txt").write_bytes(b"gmail attachment")
        (selected / "drive.bin").write_bytes(b"drive upload")
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        gmail = runtime.call_tool(
            "gsv_gmail_write",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": {
                    "attachments": [
                        {
                            "filename": "gmail.txt",
                            "local_file": {
                                "grant_id": grant_id,
                                "relative_path": "gmail.txt",
                            },
                            "mime_type": "text/plain",
                        }
                    ],
                    "text_body": "Draft body",
                },
                "operation": "drafts.create",
            },
        )
        drive = runtime.call_tool(
            "gsv_google_drive_write",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": {
                    "local_file": {
                        "grant_id": grant_id,
                        "relative_path": "drive.bin",
                    },
                    "mime_type": "application/octet-stream",
                    "name": "drive.bin",
                },
                "operation": "files.create",
            },
        )
        assert gmail["status"] == "confirmation_required"
        assert gmail["effect"] == ConnectorEffect.OUTWARD.value
        assert gmail["provider"] == "gmail"
        assert drive["status"] == "confirmation_required"
        assert drive["effect"] == ConnectorEffect.OUTWARD.value
        assert drive["provider"] == "google_drive"
        assert transport.call_count == 0
    finally:
        runtime.close()
