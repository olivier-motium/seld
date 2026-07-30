from __future__ import annotations

import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import continuity_kernel.connector_oauth as connector_oauth
from continuity_kernel.connections import render_connection_snapshot
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenEndpointError, OAuthTokenType
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.vault import Vault

BASE_TIME = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
CONNECTION_ID = parse_connection_id("con-" + "c" * 32)


def _manager(tmp_path: Path, *, redirect_port: int = 0) -> ConnectorAuthManager:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Portable auth")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider="google",
        source_ids=("gmail",),
        credential_kind=CredentialKind.OAUTH2,
        account=AccountMetadata(label="Synthetic account"),
        scopes=("mail.read",),
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-client",
            redirect_uris=(f"http://127.0.0.1:{redirect_port}/oauth/callback",),
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        ),
        health=ConnectionHealth.UNKNOWN,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=BASE_TIME,
    )
    return ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )


def _expired_credential() -> OAuthCredential:
    return OAuthCredential(
        access_token="expired-access",
        refresh_token="single-use-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=("mail.read",),
        issued_at=BASE_TIME,
        expires_at=BASE_TIME + timedelta(minutes=5),
    )


def test_concurrent_refresh_uses_a_rotating_provider_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manager.store_oauth_credential(
        CONNECTION_ID,
        _expired_credential(),
        expected_token_version=0,
    )
    calls = 0
    calls_lock = threading.Lock()

    def post_form(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        nonlocal calls
        assert endpoint == "https://oauth2.googleapis.com/token"
        assert fields["refresh_token"] == "single-use-refresh"
        assert 0 < timeout_seconds <= 15.0
        with calls_lock:
            calls += 1
        return 200, json.dumps(
            {
                "access_token": "fresh-access",
                "expires_in": 3600,
                "refresh_token": "rotated-refresh",
                "token_type": "Bearer",
            }
        ).encode()

    observed_at = BASE_TIME + timedelta(hours=1)
    monkeypatch.setattr(connector_oauth, "_post_form", post_form)
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(
            executor.map(
                lambda _index: manager.resolve_oauth_access_token(
                    CONNECTION_ID,
                    observed_at=observed_at,
                ),
                range(20),
            )
        )

    assert results == ["fresh-access"] * 20
    assert calls == 1
    resolved = manager.tokens.read(CONNECTION_ID)
    assert resolved.state.version == 2
    assert OAuthCredential.from_bytes(resolved.value).refresh_token == "rotated-refresh"
    assert "fresh-access" not in json.dumps(manager.status(), sort_keys=True)


def test_invalid_grant_preserves_secret_and_marks_reauthorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    original = manager.store_oauth_credential(
        CONNECTION_ID,
        _expired_credential(),
        expected_token_version=0,
    )

    def invalid_grant(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        assert endpoint == "https://oauth2.googleapis.com/token"
        assert fields["refresh_token"] == "single-use-refresh"
        assert 0 < timeout_seconds <= 15.0
        return 400, b'{"error":"invalid_grant"}'

    monkeypatch.setattr(connector_oauth, "_post_form", invalid_grant)
    with pytest.raises(OAuthTokenEndpointError) as failure:
        manager.resolve_oauth_access_token(
            CONNECTION_ID,
            observed_at=BASE_TIME + timedelta(hours=1),
        )

    assert failure.value.error == "invalid_grant"
    assert manager.tokens.state(CONNECTION_ID) == original
    assert manager.tokens.read(CONNECTION_ID).value == _expired_credential().to_bytes()
    connection = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert connection is not None
    assert connection.health is ConnectionHealth.REAUTHORIZATION_REQUIRED


def test_refresh_sink_rejects_a_connection_swap_and_unpinned_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    manager.store_oauth_credential(
        CONNECTION_ID,
        _expired_credential(),
        expected_token_version=0,
    )
    safe_snapshot = manager.vault.get_connection_snapshot()
    safe_metadata = safe_snapshot.connection(CONNECTION_ID)
    assert safe_metadata is not None
    tampered = replace(
        safe_metadata,
        client=replace(
            safe_metadata.client,
            token_endpoint="https://attacker.example/token",
        ),
        version=2,
    )
    (manager.vault.root / "CONNECTIONS.md").write_text(
        render_connection_snapshot(replace(safe_snapshot, connections=(tampered,))),
        encoding="utf-8",
    )

    def leak_refresh_token(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        del endpoint, fields, timeout_seconds
        pytest.fail("untrusted endpoint received the refresh token")

    monkeypatch.setattr(connector_oauth, "_post_form", leak_refresh_token)
    with pytest.raises(ConflictError, match="connection changed"):
        manager.resolve_oauth_access_token(
            CONNECTION_ID,
            expected_connection_revision=safe_snapshot.revision,
            observed_at=BASE_TIME + timedelta(hours=1),
        )
    with pytest.raises(ValidationError, match="built-in provider"):
        manager.resolve_oauth_access_token(
            CONNECTION_ID,
            expected_connection_revision=manager.vault.get_connection_snapshot().revision,
            observed_at=BASE_TIME + timedelta(hours=1),
        )


def test_native_loopback_flow_ignores_host_header_and_persists_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    callback_finished = threading.Event()

    def open_browser(authorization_url: str) -> bool:
        query = parse_qs(urlsplit(authorization_url).query)
        redirect = urlsplit(query["redirect_uri"][0])
        state = query["state"][0]
        assert redirect.port is not None and redirect.port > 0
        assert query["code_challenge_method"] == ["S256"]

        def callback() -> None:
            connection = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=5)
            target = f"{redirect.path}?code=one-time-code&state={state}"
            connection.putrequest("GET", target, skip_host=True)
            connection.putheader("Host", "attacker.invalid")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            connection.close()
            callback_finished.set()

        threading.Thread(target=callback, daemon=True).start()
        return True

    def exchange(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        assert endpoint == "https://oauth2.googleapis.com/token"
        assert fields["code"] == "one-time-code"
        assert len(fields["code_verifier"]) >= 43
        assert fields["redirect_uri"].startswith("http://127.0.0.1:")
        assert timeout_seconds == 30.0
        return 200, b'{"access_token":"loopback-access","token_type":"Bearer"}'

    monkeypatch.setattr("continuity_kernel.connector_auth_manager.webbrowser.open", open_browser)
    monkeypatch.setattr(connector_oauth, "_post_form", exchange)
    manager.authorize_oauth(CONNECTION_ID, timeout_seconds=30.0)

    assert callback_finished.wait(timeout=1)
    stored = OAuthCredential.from_bytes(manager.tokens.read(CONNECTION_ID).value)
    assert stored.access_token == "loopback-access"
    status = manager.status()["connections"]
    assert isinstance(status, list)
    assert status[0]["host_credential"] == "available"
    assert status[0]["health"] == "unverified"
