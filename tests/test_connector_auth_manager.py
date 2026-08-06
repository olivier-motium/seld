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
from continuity_kernel.connector_client_registration import load_public_client_registration
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import (
    ConnectionId,
    SecretName,
    parse_connection_id,
)
from continuity_kernel.connector_oauth import (
    OAuthClientConfig,
    OAuthTokenEndpointError,
    OAuthTokenSet,
    OAuthTokenType,
)
from continuity_kernel.connector_profiles import get_connector_profile, get_profile
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_token_store import TokenState
from continuity_kernel.errors import (
    ConflictError,
    MutationCommittedError,
    OAuthPermissionGrantError,
    SetupError,
    ValidationError,
)
from continuity_kernel.vault import Vault

BASE_TIME = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
CONNECTION_ID = parse_connection_id("con-" + "c" * 32)
GOOGLE_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
GOOGLE_IDENTITY_SCOPES = ("openid", "email", GOOGLE_SCOPE)
GOOGLE_ACCOUNT_FINGERPRINT = "sha256:" + "f" * 64
MICROSOFT_ACCOUNT_FINGERPRINT = "sha256:" + "e" * 64


class _AmbiguousProbeStore(InMemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_calls = 0

    def set_secret(
        self,
        connection_id: ConnectionId,
        name: SecretName,
        value: bytes,
    ) -> None:
        super().set_secret(connection_id, name, value)
        if name == "readiness-probe":
            raise SetupError("synthetic ambiguous keyring write")

    def delete_secret(self, connection_id: ConnectionId, name: SecretName) -> None:
        self.cleanup_calls += 1
        super().delete_secret(connection_id, name)

    @property
    def retained_values(self) -> int:
        return len(self._values)


class _InterruptedProbeStore(InMemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.interruption = KeyboardInterrupt()

    def set_secret(
        self,
        connection_id: ConnectionId,
        name: SecretName,
        value: bytes,
    ) -> None:
        super().set_secret(connection_id, name, value)
        if name == "readiness-probe":
            raise self.interruption

    @property
    def retained_values(self) -> int:
        return len(self._values)


def _manager(
    tmp_path: Path,
    *,
    redirect_port: int = 0,
    scopes: tuple[str, ...] = (GOOGLE_SCOPE,),
    fingerprint: str | None = GOOGLE_ACCOUNT_FINGERPRINT,
    health: ConnectionHealth = ConnectionHealth.READY,
    client_id: str = "public-client",
) -> ConnectorAuthManager:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Portable auth")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider="google",
        source_ids=("gmail",),
        credential_kind=CredentialKind.OAUTH2,
        account=AccountMetadata(
            fingerprint=fingerprint,
            label="Synthetic account",
        ),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier=client_id,
            redirect_uris=(f"http://127.0.0.1:{redirect_port}",),
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        ),
        health=health,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
        last_verified_at=(
            BASE_TIME
            if fingerprint is not None
            and health
            in {
                ConnectionHealth.READY,
                ConnectionHealth.DEGRADED,
                ConnectionHealth.REAUTHORIZATION_REQUIRED,
            }
            else None
        ),
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


def _microsoft_manager(
    tmp_path: Path,
    *,
    fingerprint: str | None = MICROSOFT_ACCOUNT_FINGERPRINT,
    health: ConnectionHealth = ConnectionHealth.READY,
) -> ConnectorAuthManager:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Portable Microsoft auth")
    profile = get_profile("microsoft")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(fingerprint=fingerprint, label="Synthetic Microsoft account"),
        scopes=profile.read_scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-microsoft-client",
            redirect_uris=("http://localhost:49152/oauth/callback",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=health,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
        last_verified_at=(BASE_TIME if fingerprint is not None else None),
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


def _discord_manager(
    tmp_path: Path,
    *,
    health: ConnectionHealth = ConnectionHealth.READY,
    fingerprint: str | None = GOOGLE_ACCOUNT_FINGERPRINT,
    scopes: tuple[str, ...] | None = None,
) -> ConnectorAuthManager:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Portable Discord auth")
    profile = get_profile("discord")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(
            fingerprint=fingerprint,
            label="Synthetic Discord bot",
        ),
        scopes=profile.full_scopes if scopes is None else scopes,
        client=ClientMetadata(kind=ClientKind.EXTERNAL),
        health=health,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
        last_verified_at=BASE_TIME if fingerprint is not None else None,
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


def _fresh_metadata(
    manager: ConnectorAuthManager,
    connection_id: str,
    fingerprint: str,
) -> ConnectionMetadata:
    template = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert template is not None
    return replace(
        template,
        connection_id=parse_connection_id(connection_id),
        account=AccountMetadata(fingerprint=fingerprint, label="Synthetic fresh account"),
        health=ConnectionHealth.UNVERIFIED,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
        last_verified_at=None,
    )


def _expired_credential() -> OAuthCredential:
    return OAuthCredential(
        access_token="expired-access",
        refresh_token="single-use-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(GOOGLE_SCOPE,),
        issued_at=BASE_TIME,
        expires_at=BASE_TIME + timedelta(minutes=5),
    )


def _import_oauth_credential(
    manager: ConnectorAuthManager,
    credential: OAuthCredential,
) -> TokenState:
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None
    return manager.ensure_imported_credential(metadata, credential.to_bytes())


@pytest.mark.parametrize(
    ("health", "message"),
    (
        (ConnectionHealth.UNVERIFIED, "gsv connectors resume"),
        (ConnectionHealth.REAUTHORIZATION_REQUIRED, "gsv connectors reauthorize"),
        (ConnectionHealth.UNKNOWN, "--new-account"),
    ),
)
def test_runtime_resolution_requires_a_verified_identity(
    tmp_path: Path,
    health: ConnectionHealth,
    message: str,
) -> None:
    manager = _manager(tmp_path, health=health)
    _import_oauth_credential(manager, _expired_credential())

    with pytest.raises(ValidationError, match=message):
        manager.resolve_oauth_access_token_state(
            CONNECTION_ID,
            observed_at=BASE_TIME,
        )


def test_degraded_verified_identity_remains_runtime_usable(tmp_path: Path) -> None:
    manager = _manager(tmp_path, health=ConnectionHealth.DEGRADED)
    credential = replace(
        _expired_credential(),
        expires_at=BASE_TIME + timedelta(hours=1),
    )
    _import_oauth_credential(manager, credential)

    resolved = manager.resolve_oauth_access_token_state(
        CONNECTION_ID,
        observed_at=BASE_TIME,
    )

    assert resolved.access_token == credential.access_token


def test_custody_probe_cleans_up_an_ambiguous_write(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "probe-vault")
    vault.initialize(name="Custody probe")
    store = _AmbiguousProbeStore()
    manager = ConnectorAuthManager(
        vault,
        secret_store=store,
        state_root=tmp_path / "probe-state",
    )

    with pytest.raises(SetupError, match="ambiguous keyring write"):
        manager.probe_credential_custody()

    assert store.cleanup_calls == 1
    assert store.retained_values == 0


def test_custody_probe_cleans_up_and_preserves_an_interruption(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "interrupted-probe-vault")
    vault.initialize(name="Interrupted custody probe")
    store = _InterruptedProbeStore()
    manager = ConnectorAuthManager(
        vault,
        secret_store=store,
        state_root=tmp_path / "interrupted-probe-state",
    )

    with pytest.raises(KeyboardInterrupt) as failure:
        manager.probe_credential_custody()

    assert failure.value is store.interruption
    assert store.retained_values == 0


def test_status_parse_validates_oauth_custody_and_gives_recovery(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.tokens.update(
        CONNECTION_ID,
        expected_version=0,
        value=b"not-an-oauth-credential",
    )

    rows = manager.status()["connections"]

    assert isinstance(rows, list)
    assert rows[0]["host_credential"] == "invalid"
    assert rows[0]["next"] == f"gsv connectors reauthorize {CONNECTION_ID}"


def test_status_requires_disconnect_for_an_invalid_credential_pointer(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _import_oauth_credential(manager, _expired_credential())
    state_path = manager.tokens.root / "state" / f"{CONNECTION_ID}.json"
    state_path.write_text("not-json", encoding="utf-8")

    rows = manager.status()["connections"]

    assert isinstance(rows, list)
    assert rows[0]["host_credential"] == "invalid"
    assert rows[0]["next"] == (
        f"gsv connectors disconnect {CONNECTION_ID}; "
        "gsv connectors connect gmail --access read --new-account"
    )


def test_status_reconnects_legacy_ready_connection_without_verified_identity(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        fingerprint=None,
        health=ConnectionHealth.READY,
    )
    _import_oauth_credential(manager, _expired_credential())

    rows = manager.status()["connections"]

    assert isinstance(rows, list)
    assert rows[0]["host_credential"] == "available"
    assert rows[0]["next"] == (
        f"gsv connectors disconnect {CONNECTION_ID}; "
        "gsv connectors connect gmail --access read --new-account"
    )


def test_legacy_provider_bundle_recovery_never_emits_unsupported_connector(
    tmp_path: Path,
) -> None:
    manager = _microsoft_manager(
        tmp_path,
        fingerprint=None,
        health=ConnectionHealth.READY,
    )

    rows = manager.status()["connections"]

    assert isinstance(rows, list)
    assert rows[0]["next"] == (f"gsv connectors disconnect {CONNECTION_ID}; gsv-auth status")

    with pytest.raises(ValidationError, match="then `gsv-auth status`"):
        manager.resolve_oauth_access_token_state(CONNECTION_ID)


def test_status_reconnects_a_missing_discord_credential_without_oauth_language(
    tmp_path: Path,
) -> None:
    manager = _discord_manager(tmp_path)

    rows = manager.status()["connections"]

    assert isinstance(rows, list)
    assert rows[0]["host_credential"] == "missing"
    assert rows[0]["next"] == "gsv connectors connect discord --access full"


def test_legacy_empty_scope_discord_recovery_always_uses_full_access(tmp_path: Path) -> None:
    manager = _discord_manager(
        tmp_path,
        health=ConnectionHealth.UNKNOWN,
        fingerprint=None,
        scopes=(),
    )

    rows = manager.status()["connections"]

    assert isinstance(rows, list)
    assert rows[0]["next"] == (
        f"gsv connectors disconnect {CONNECTION_ID}; "
        "gsv connectors connect discord --access full --new-account"
    )


def test_stale_provider_auth_rejection_cannot_downgrade_a_rotated_credential(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    original = _import_oauth_credential(manager, _expired_credential())
    snapshot = manager.vault.get_connection_snapshot()
    manager.tokens.update(
        CONNECTION_ID,
        expected_version=original.version,
        value=replace(
            _expired_credential(),
            access_token="replacement-access",
        ).to_bytes(),
    )

    with pytest.raises(ConflictError, match="credential changed"):
        manager.mark_reauthorization_required(
            CONNECTION_ID,
            expected_connection_revision=snapshot.revision,
            expected_token_version=original.version,
        )

    current = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert current is not None
    assert current.health is ConnectionHealth.READY


def _rotation_manager(
    tmp_path: Path,
    *,
    health: ConnectionHealth = ConnectionHealth.READY,
) -> tuple[ConnectorAuthManager, OAuthCredential]:
    manager = _manager(
        tmp_path,
        scopes=GOOGLE_IDENTITY_SCOPES,
        fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
        health=health,
    )
    original = OAuthCredential(
        access_token="rotation-old-access",
        refresh_token="rotation-old-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=GOOGLE_IDENTITY_SCOPES,
        issued_at=BASE_TIME,
        expires_at=None,
    )
    _import_oauth_credential(manager, original)
    return manager, original


def _rotation_replacement(*, scopes: tuple[str, ...] = GOOGLE_IDENTITY_SCOPES) -> OAuthCredential:
    return OAuthCredential(
        access_token="rotation-new-access",
        refresh_token="rotation-new-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=scopes,
        issued_at=BASE_TIME + timedelta(minutes=1),
        expires_at=None,
    )


def test_concurrent_refresh_uses_a_rotating_provider_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    _import_oauth_credential(manager, _expired_credential())
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
                lambda _index: (
                    manager.resolve_oauth_access_token_state(
                        CONNECTION_ID,
                        observed_at=observed_at,
                    ).access_token
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


@pytest.mark.parametrize(
    "provider_error",
    ["invalid_grant", "invalid_refresh_token", "invalid_request"],
)
def test_invalid_refresh_preserves_secret_and_marks_reauthorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_error: str,
) -> None:
    manager = _manager(tmp_path)
    original = _import_oauth_credential(manager, _expired_credential())

    def invalid_grant(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        assert endpoint == "https://oauth2.googleapis.com/token"
        assert fields["refresh_token"] == "single-use-refresh"
        assert 0 < timeout_seconds <= 15.0
        return 400, json.dumps({"error": provider_error}).encode()

    monkeypatch.setattr(connector_oauth, "_post_form", invalid_grant)
    with pytest.raises(OAuthTokenEndpointError) as failure:
        manager.resolve_oauth_access_token_state(
            CONNECTION_ID,
            observed_at=BASE_TIME + timedelta(hours=1),
        )

    assert failure.value.error == provider_error
    assert manager.tokens.state(CONNECTION_ID) == original
    assert manager.tokens.read(CONNECTION_ID).value == _expired_credential().to_bytes()
    connection = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert connection is not None
    assert connection.health is ConnectionHealth.REAUTHORIZATION_REQUIRED
    row = manager.status()["connections"][0]
    assert row["health"] == ConnectionHealth.REAUTHORIZATION_REQUIRED.value
    assert row["next"] == f"gsv connectors reauthorize {CONNECTION_ID}"


def test_transient_refresh_failure_preserves_degraded_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    _import_oauth_credential(manager, _expired_credential())

    def transient_failure(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        assert endpoint == "https://oauth2.googleapis.com/token"
        assert fields["refresh_token"] == "single-use-refresh"
        assert timeout_seconds > 0
        return 503, b'{"error":"temporarily_unavailable"}'

    monkeypatch.setattr(connector_oauth, "_post_form", transient_failure)
    with pytest.raises(OAuthTokenEndpointError) as failure:
        manager.resolve_oauth_access_token_state(
            CONNECTION_ID,
            observed_at=BASE_TIME + timedelta(hours=1),
        )

    assert failure.value.status_code == 503
    assert manager.tokens.read(CONNECTION_ID).value == _expired_credential().to_bytes()
    connection = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert connection is not None
    assert connection.health is ConnectionHealth.DEGRADED
    row = manager.status()["connections"][0]
    assert row["health"] == ConnectionHealth.DEGRADED.value
    assert "next" not in row


def test_microsoft_invalid_request_preserves_degraded_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _microsoft_manager(tmp_path)
    profile = get_profile("microsoft")
    credential = replace(
        _expired_credential(),
        refresh_token="microsoft-refresh",
        scopes=profile.read_scopes,
    )
    _import_oauth_credential(manager, credential)

    def invalid_request(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        assert endpoint == profile.token_endpoint
        assert fields["refresh_token"] == "microsoft-refresh"
        assert timeout_seconds > 0
        return 400, b'{"error":"invalid_request"}'

    monkeypatch.setattr(connector_oauth, "_post_form", invalid_request)
    with pytest.raises(OAuthTokenEndpointError) as failure:
        manager.resolve_oauth_access_token_state(
            CONNECTION_ID,
            observed_at=BASE_TIME + timedelta(hours=1),
        )

    assert failure.value.error == "invalid_request"
    connection = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert connection is not None
    assert connection.health is ConnectionHealth.DEGRADED
    row = manager.status()["connections"][0]
    assert row["health"] == ConnectionHealth.DEGRADED.value
    assert "next" not in row


def test_refresh_error_downgrade_advances_a_stale_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    credential = replace(_expired_credential(), expires_at=BASE_TIME + timedelta(seconds=30))
    _import_oauth_credential(manager, credential)
    before = manager.vault.get_connection_snapshot()
    before_metadata = before.connection(CONNECTION_ID)
    assert before_metadata is not None

    def reject_refresh(
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        assert endpoint == "https://oauth2.googleapis.com/token"
        assert fields["refresh_token"] == "single-use-refresh"
        assert timeout_seconds > 0
        return 400, b'{"error":"invalid_grant"}'

    monkeypatch.setattr(connector_oauth, "_post_form", reject_refresh)
    with pytest.raises(OAuthTokenEndpointError, match="invalid_grant"):
        manager.resolve_oauth_access_token_state(
            CONNECTION_ID,
            observed_at=before_metadata.updated_at,
        )

    after = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert after is not None
    assert after.health is ConnectionHealth.REAUTHORIZATION_REQUIRED
    assert after.updated_at > before_metadata.updated_at
    assert manager.tokens.read(CONNECTION_ID).value == credential.to_bytes()


def test_refresh_sink_rejects_a_connection_swap_and_unpinned_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    _import_oauth_credential(manager, _expired_credential())
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
    (manager.vault.root / "CONNECTIONS.md").write_bytes(
        render_connection_snapshot(replace(safe_snapshot, connections=(tampered,))).encode("utf-8"),
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
        manager.resolve_oauth_access_token_state(
            CONNECTION_ID,
            expected_connection_revision=safe_snapshot.revision,
            observed_at=BASE_TIME + timedelta(hours=1),
        )
    with pytest.raises(ValidationError, match="built-in provider"):
        manager.resolve_oauth_access_token_state(
            CONNECTION_ID,
            expected_connection_revision=manager.vault.get_connection_snapshot().revision,
            observed_at=BASE_TIME + timedelta(hours=1),
        )


def test_native_loopback_flow_ignores_host_header_and_persists_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path, scopes=GOOGLE_IDENTITY_SCOPES)
    callback_finished = threading.Event()

    def open_browser(authorization_url: str) -> bool:
        query = parse_qs(urlsplit(authorization_url).query)
        redirect = urlsplit(query["redirect_uri"][0])
        state = query["state"][0]
        assert redirect.port is not None and redirect.port > 0
        assert query["code_challenge_method"] == ["S256"]
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent select_account"]
        assert redirect.path == ""

        def callback() -> None:
            connection = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=5)
            target = f"/?code=one-time-code&state={state}"
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
        assert 0 < timeout_seconds <= 30.0
        return 200, (
            b'{"access_token":"loopback-access","token_type":"Bearer",'
            b'"refresh_token":"loopback-refresh","expires_in":3600,'
            b'"scope":"openid https://www.googleapis.com/auth/userinfo.email '
            b'https://www.googleapis.com/auth/gmail.readonly"}'
        )

    monkeypatch.setattr("continuity_kernel.connector_auth_manager.webbrowser.open", open_browser)
    monkeypatch.setattr(connector_oauth, "_post_form", exchange)
    manager.authorize_oauth(CONNECTION_ID, timeout_seconds=30.0)

    assert callback_finished.wait(timeout=1)
    stored = OAuthCredential.from_bytes(manager.tokens.read(CONNECTION_ID).value)
    assert stored.access_token == "loopback-access"
    assert stored.scopes == GOOGLE_IDENTITY_SCOPES
    status = manager.status()["connections"]
    assert isinstance(status, list)
    assert status[0]["host_credential"] == "available"
    assert status[0]["health"] == "unverified"


def test_oauth_acquisition_survives_browser_failure_without_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None
    presented: list[tuple[str, bool]] = []

    class Listener:
        redirect_uri = "http://127.0.0.1:48123"

        def configure(self, config: object, attempt: object) -> None:
            del config, attempt

        def wait_for_code(self, *, timeout_seconds: float) -> str:
            assert timeout_seconds > 0
            return "one-time-code"

        def close(self) -> None:
            return None

    listener = Listener()
    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.BoundLoopbackCallback.bind",
        lambda **_values: listener,
    )
    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.exchange_authorization_code",
        lambda *_args, **_values: OAuthTokenSet(
            access_token="transient-access",
            token_type=OAuthTokenType.BEARER,
            refresh_token="transient-refresh",
            expires_in_seconds=3600,
            scopes=(GOOGLE_SCOPE,),
        ),
    )

    credential = manager.acquire_oauth_credential(
        metadata,
        timeout_seconds=30,
        browser_opener=lambda _url: False,
        present_authorization_url=lambda url, opened: presented.append((url, opened)),
    )

    assert credential.access_token == "transient-access"
    assert len(presented) == 1
    assert presented[0][0].startswith("https://accounts.google.com/")
    assert presented[0][1] is False
    assert manager.tokens.state(CONNECTION_ID) is None
    unchanged = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert unchanged is not None
    assert unchanged.health is ConnectionHealth.READY


def test_packaged_google_client_secret_is_required_once_and_used_for_exchange_and_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = load_public_client_registration("google")
    assert registration.client_secret_required is True
    manager = _manager(tmp_path, client_id=registration.client_id)
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None
    opened: list[str] = []

    class Listener:
        redirect_uri = "http://127.0.0.1:48123"

        def configure(self, config: object, attempt: object) -> None:
            del config, attempt

        def wait_for_code(self, *, timeout_seconds: float) -> str:
            assert timeout_seconds > 0
            return "one-time-code"

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.BoundLoopbackCallback.bind",
        lambda **_values: Listener(),
    )
    assert manager.oauth_client_secret_status(registration) == "missing"
    with pytest.raises(SetupError, match=r"client secret.*client-secret set google"):
        manager.acquire_oauth_credential(
            metadata,
            browser_opener=lambda url: opened.append(url) or True,
        )
    assert opened == []

    result = manager.store_oauth_client_secret("google", b"host-only-client-secret")
    assert result["client_secret"] == "configured"
    assert "host-only-client-secret" not in repr(result)
    assert manager.oauth_client_secret_status(registration) == "configured"

    exchange_secrets: list[str | None] = []

    def exchange(config: OAuthClientConfig, **_kwargs: object) -> OAuthTokenSet:
        exchange_secrets.append(config.client_secret)
        return OAuthTokenSet(
            access_token="new-access",
            refresh_token="new-refresh",
            token_type=OAuthTokenType.BEARER,
            expires_in_seconds=3600,
            scopes=(GOOGLE_SCOPE,),
        )

    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.exchange_authorization_code",
        exchange,
    )
    acquired = manager.acquire_oauth_credential(
        metadata,
        browser_opener=lambda url: opened.append(url) or True,
    )
    assert acquired.access_token == "new-access"
    assert exchange_secrets == ["host-only-client-secret"]

    _import_oauth_credential(manager, _expired_credential())
    refresh_secrets: list[str | None] = []

    def refresh(config: OAuthClientConfig, **_kwargs: object) -> OAuthTokenSet:
        refresh_secrets.append(config.client_secret)
        return OAuthTokenSet(
            access_token="refreshed-access",
            refresh_token=None,
            token_type=OAuthTokenType.BEARER,
            expires_in_seconds=3600,
            scopes=None,
        )

    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.refresh_access_token",
        refresh,
    )
    resolved = manager.resolve_oauth_access_token_state(
        CONNECTION_ID,
        observed_at=BASE_TIME + timedelta(hours=1),
    )
    assert resolved.access_token == "refreshed-access"
    assert refresh_secrets == ["host-only-client-secret"]

    cleared = manager.clear_oauth_client_secret("google")
    assert cleared["status"] == "cleared"
    assert manager.oauth_client_secret_status(registration) == "missing"


def test_oauth_failure_is_annotated_only_after_authorization_was_presented(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None

    class Listener:
        redirect_uri = "http://127.0.0.1:48123"

        def configure(self, config: object, attempt: object) -> None:
            del config, attempt

        def wait_for_code(self, *, timeout_seconds: float) -> str:
            assert timeout_seconds > 0
            raise SetupError("synthetic callback failure")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.BoundLoopbackCallback.bind",
        lambda **_values: Listener(),
    )

    with pytest.raises(SetupError, match="synthetic callback failure") as failure:
        manager.acquire_oauth_credential(
            metadata,
            browser_opener=lambda _url: True,
        )

    assert failure.value.provider_authorization_may_remain is True


def test_browser_open_interruption_retains_provider_cleanup_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None

    class Listener:
        redirect_uri = "http://127.0.0.1:48123"

        def configure(self, config: object, attempt: object) -> None:
            del config, attempt

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.BoundLoopbackCallback.bind",
        lambda **_values: Listener(),
    )

    with pytest.raises(KeyboardInterrupt) as failure:
        manager.acquire_oauth_credential(
            metadata,
            browser_opener=lambda _url: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    assert getattr(failure.value, "provider_authorization_may_remain", False) is True


@pytest.mark.parametrize(
    ("token_set", "message"),
    (
        (
            OAuthTokenSet(
                access_token="post-exchange-access",
                refresh_token="post-exchange-refresh",
                token_type=OAuthTokenType.BEARER,
                expires_in_seconds=3600,
                scopes=("unexpected.scope",),
            ),
            "outside the selected access tier",
        ),
        (
            OAuthTokenSet(
                access_token="post-exchange-access",
                refresh_token="post-exchange-refresh",
                token_type=OAuthTokenType.BEARER,
                expires_in_seconds=10**100,
                scopes=(GOOGLE_SCOPE,),
            ),
            "supported time range",
        ),
    ),
)
def test_post_exchange_validation_retains_provider_cleanup_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token_set: OAuthTokenSet,
    message: str,
) -> None:
    manager = _manager(tmp_path)
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None

    class Listener:
        redirect_uri = "http://127.0.0.1:48123"

        def configure(self, config: object, attempt: object) -> None:
            del config, attempt

        def wait_for_code(self, *, timeout_seconds: float) -> str:
            assert timeout_seconds > 0
            return "one-time-code"

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.BoundLoopbackCallback.bind",
        lambda **_values: Listener(),
    )
    monkeypatch.setattr(
        "continuity_kernel.connector_auth_manager.exchange_authorization_code",
        lambda *args, **kwargs: token_set,
    )

    with pytest.raises(ValidationError, match=message) as failure:
        manager.acquire_oauth_credential(
            metadata,
            browser_opener=lambda _url: True,
        )

    assert failure.value.provider_authorization_may_remain is True


def test_authorize_downgrades_before_token_publish_and_can_resume_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, original = _rotation_manager(tmp_path)
    replacement = _rotation_replacement()
    before = manager.vault.get_connection_snapshot()
    before_metadata = before.connection(CONNECTION_ID)
    assert before_metadata is not None

    def acquire(_metadata: ConnectionMetadata, **_kwargs: object) -> OAuthCredential:
        return replacement

    monkeypatch.setattr(manager, "acquire_oauth_credential", acquire)
    original_update = manager.tokens.update
    fail_once = True
    observed_health: list[ConnectionHealth] = []

    def publish(
        connection_id: ConnectionId,
        *,
        expected_version: int,
        value: bytes,
        updated_at: datetime | None = None,
    ) -> TokenState:
        nonlocal fail_once
        visible = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
        assert visible is not None
        observed_health.append(visible.health)
        assert visible.health is ConnectionHealth.UNVERIFIED
        assert visible.updated_at > before_metadata.updated_at
        assert expected_version == 1
        if fail_once:
            fail_once = False
            raise SetupError("synthetic token publication failure")
        return original_update(
            connection_id,
            expected_version=expected_version,
            value=value,
            updated_at=updated_at,
        )

    monkeypatch.setattr(manager.tokens, "update", publish)
    with pytest.raises(SetupError, match="synthetic token publication failure") as failure:
        manager.authorize_oauth(CONNECTION_ID)

    failed = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert failed is not None
    assert failed.health is ConnectionHealth.UNVERIFIED
    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    replacement_refresh_token = replacement.refresh_token
    assert replacement_refresh_token is not None
    assert replacement.access_token not in str(failure.value)
    assert replacement_refresh_token not in str(failure.value)
    assert replacement.access_token not in json.dumps(manager.status(), sort_keys=True)

    manager.authorize_oauth(CONNECTION_ID)
    stored = OAuthCredential.from_bytes(manager.tokens.read(CONNECTION_ID).value)
    assert stored == replacement
    assert observed_health == [ConnectionHealth.UNVERIFIED, ConnectionHealth.UNVERIFIED]


def test_authorize_rejects_binding_drift_without_mutating_metadata_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, original = _rotation_manager(tmp_path)
    replacement = _rotation_replacement()

    def acquire(_metadata: ConnectionMetadata, **_kwargs: object) -> OAuthCredential:
        snapshot = manager.vault.get_connection_snapshot()
        current = snapshot.connection(CONNECTION_ID)
        assert current is not None
        drifted = replace(
            current,
            account=AccountMetadata(
                fingerprint="sha256:" + "a" * 64,
                label=current.account.label,
            ),
            version=current.version + 1,
        )
        (manager.vault.root / "CONNECTIONS.md").write_bytes(
            render_connection_snapshot(replace(snapshot, connections=(drifted,))).encode("utf-8"),
        )
        return replacement

    monkeypatch.setattr(manager, "acquire_oauth_credential", acquire)
    monkeypatch.setattr(
        manager.vault,
        "mark_connection_health",
        lambda **_kwargs: pytest.fail("metadata must not mutate after binding drift"),
    )
    monkeypatch.setattr(
        manager.tokens,
        "update",
        lambda *_args, **_kwargs: pytest.fail("token must not mutate after binding drift"),
    )

    with pytest.raises(ConflictError, match="connection changed during OAuth authorization"):
        manager.authorize_oauth(CONNECTION_ID)

    drifted = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert drifted is not None
    assert drifted.account.fingerprint == "sha256:" + "a" * 64
    assert drifted.health is ConnectionHealth.READY
    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()


def test_fresh_verified_connection_is_unusable_until_token_and_identity_publish(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    template = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert template is not None
    now = datetime.now(UTC)
    new_id = parse_connection_id("con-" + "n" * 32)
    metadata = replace(
        template,
        connection_id=new_id,
        account=AccountMetadata(
            fingerprint="sha256:" + "a" * 64,
            label="Google account - aaaaaaaa",
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
        last_verified_at=None,
    )
    credential = OAuthCredential(
        access_token="fresh-access",
        refresh_token="fresh-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(GOOGLE_SCOPE,),
        issued_at=now,
        expires_at=None,
    )

    result = manager.publish_new_oauth_connection(metadata, credential)

    published = manager.vault.get_connection_snapshot().connection(new_id)
    assert published is not None
    assert published.health is ConnectionHealth.READY
    assert published.last_verified_at is not None
    assert manager.tokens.read(new_id).value == credential.to_bytes()
    assert "fresh-access" not in json.dumps(result, sort_keys=True)


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_oauth_import_requires_refresh_token_without_mutation(
    tmp_path: Path,
    provider: str,
) -> None:
    manager = _manager(tmp_path) if provider == "google" else _microsoft_manager(tmp_path)
    scopes = (GOOGLE_SCOPE,) if provider == "google" else ("User.Read", "Mail.Read")
    credential = OAuthCredential(
        access_token=f"{provider}-import-access",
        refresh_token=None,
        token_type=OAuthTokenType.BEARER,
        scopes=scopes,
        issued_at=BASE_TIME,
        expires_at=None,
    )
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(ValidationError, match="refresh token"):
        manager.ensure_imported_credential(metadata, credential.to_bytes())

    assert manager.tokens.state(CONNECTION_ID) is None
    assert manager.vault.get_connection_snapshot() == before


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_oauth_publish_requires_refresh_token_without_mutation(
    tmp_path: Path,
    provider: str,
) -> None:
    manager = _manager(tmp_path) if provider == "google" else _microsoft_manager(tmp_path)
    fingerprint = (
        GOOGLE_ACCOUNT_FINGERPRINT if provider == "google" else MICROSOFT_ACCOUNT_FINGERPRINT
    )
    new_id = "con-" + ("g" if provider == "google" else "m") * 32
    metadata = _fresh_metadata(manager, new_id, fingerprint)
    scopes = (GOOGLE_SCOPE,) if provider == "google" else ("User.Read", "Mail.Read")
    credential = OAuthCredential(
        access_token=f"{provider}-publish-access",
        refresh_token=None,
        token_type=OAuthTokenType.BEARER,
        scopes=scopes,
        issued_at=BASE_TIME,
        expires_at=None,
    )
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(ValidationError, match="refresh token"):
        manager.publish_new_oauth_connection(metadata, credential)

    assert manager.tokens.state(metadata.connection_id) is None
    assert manager.vault.get_connection_snapshot() == before


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_verified_oauth_rotation_requires_refresh_token_without_mutation(
    tmp_path: Path,
    provider: str,
) -> None:
    if provider == "google":
        manager, original = _rotation_manager(tmp_path)
        fingerprint = GOOGLE_ACCOUNT_FINGERPRINT
    else:
        manager = _microsoft_manager(
            tmp_path,
            fingerprint=MICROSOFT_ACCOUNT_FINGERPRINT,
            health=ConnectionHealth.READY,
        )
        original = OAuthCredential(
            access_token="microsoft-old-access",
            refresh_token="microsoft-old-refresh",
            token_type=OAuthTokenType.BEARER,
            scopes=("User.Read", "Mail.Read", "Calendars.Read"),
            issued_at=BASE_TIME,
            expires_at=None,
        )
        _import_oauth_credential(manager, original)
        fingerprint = MICROSOFT_ACCOUNT_FINGERPRINT
    replacement = replace(
        original,
        access_token=f"{provider}-replacement-access",
        refresh_token=None,
        issued_at=BASE_TIME + timedelta(minutes=1),
    )
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(ValidationError, match="refresh token"):
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=fingerprint,
            expected_token_version=1,
            replacement=replacement,
        )

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == before


def test_fresh_connection_rolls_back_metadata_when_secret_never_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    template = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert template is not None
    now = datetime.now(UTC)
    new_id = parse_connection_id("con-" + "p" * 32)
    metadata = replace(
        template,
        connection_id=new_id,
        account=AccountMetadata(fingerprint="sha256:" + "b" * 64),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
        last_verified_at=None,
    )
    credential = OAuthCredential(
        access_token="never-published",
        refresh_token="never-published-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(GOOGLE_SCOPE,),
        issued_at=now,
        expires_at=None,
    )

    def fail_update(*_args: object, **_values: object) -> object:
        raise SetupError("synthetic keyring failure")

    monkeypatch.setattr(manager.tokens, "update", fail_update)
    with pytest.raises(SetupError, match="synthetic keyring failure"):
        manager.publish_new_oauth_connection(metadata, credential)

    assert manager.vault.get_connection_snapshot().connection(new_id) is None
    assert manager.tokens.state(new_id) is None


def test_oauth_credential_rejects_provider_grants_outside_the_profile(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    overbroad = OAuthCredential(
        access_token="overbroad-access",
        refresh_token="overbroad-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(GOOGLE_SCOPE, "https://www.googleapis.com/auth/drive"),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    with pytest.raises(OAuthPermissionGrantError) as failure:
        _import_oauth_credential(manager, overbroad)
    assert failure.value.reason == "outside_selected_tier"
    assert manager.tokens.state(CONNECTION_ID) is None


def test_read_selection_rejects_a_known_full_tier_grant(tmp_path: Path) -> None:
    manager = _manager(tmp_path, scopes=GOOGLE_IDENTITY_SCOPES)
    profile = get_connector_profile("gmail")
    credential = OAuthCredential(
        access_token="full-tier-access",
        refresh_token="full-tier-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=profile.full_scopes,
        issued_at=BASE_TIME,
        expires_at=None,
    )

    with pytest.raises(OAuthPermissionGrantError) as failure:
        _import_oauth_credential(manager, credential)

    assert failure.value.reason == "outside_selected_tier"
    assert manager.tokens.state(CONNECTION_ID) is None


def test_gmail_purge_selection_reports_a_missing_supplemental_grant(tmp_path: Path) -> None:
    profile = get_connector_profile("gmail")
    manager = _manager(
        tmp_path,
        scopes=profile.scopes_for("full", include_supplemental=True),
    )
    credential = OAuthCredential(
        access_token="plain-full-access",
        refresh_token="plain-full-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=profile.full_scopes,
        issued_at=BASE_TIME,
        expires_at=None,
    )

    with pytest.raises(OAuthPermissionGrantError) as failure:
        _import_oauth_credential(manager, credential)

    assert failure.value.reason == "missing_selected_permissions"
    assert manager.tokens.state(CONNECTION_ID) is None


def test_gmail_purge_scope_satisfies_modify_but_retains_the_settings_grant(
    tmp_path: Path,
) -> None:
    profile = get_connector_profile("gmail")
    manager = _manager(
        tmp_path,
        scopes=profile.scopes_for("full", include_supplemental=True),
    )
    credential = OAuthCredential(
        access_token="purge-access",
        refresh_token="purge-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(
            "openid",
            "email",
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/gmail.settings.basic",
        ),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    result = _import_oauth_credential(manager, credential)

    assert result.version == 1
    resolved = manager.resolve_oauth_access_token_state(
        CONNECTION_ID,
        observed_at=BASE_TIME,
    )
    assert "https://mail.google.com/" in resolved.scopes
    assert "https://www.googleapis.com/auth/gmail.modify" not in resolved.scopes
    assert "https://www.googleapis.com/auth/gmail.settings.basic" in resolved.scopes


def test_gmail_purge_scope_does_not_substitute_for_the_settings_grant(tmp_path: Path) -> None:
    profile = get_connector_profile("gmail")
    manager = _manager(
        tmp_path,
        scopes=profile.scopes_for("full", include_supplemental=True),
    )
    credential = OAuthCredential(
        access_token="purge-without-settings-access",
        refresh_token="purge-without-settings-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=("openid", "email", "https://mail.google.com/"),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    with pytest.raises(OAuthPermissionGrantError) as failure:
        _import_oauth_credential(manager, credential)

    assert failure.value.reason == "missing_selected_permissions"
    assert manager.tokens.state(CONNECTION_ID) is None


def test_declared_legacy_calendar_grant_remains_usable_end_to_end(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Legacy Calendar grant")
    profile = get_connector_profile("google_calendar")
    scopes = profile.legacy_read_scopes[0]
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(
            fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            label="Legacy Calendar",
        ),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-google-client",
            redirect_uris=("http://127.0.0.1:0",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.READY,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
        last_verified_at=BASE_TIME,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=BASE_TIME,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    credential = OAuthCredential(
        access_token="legacy-calendar-access",
        refresh_token="legacy-calendar-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=scopes,
        issued_at=BASE_TIME,
        expires_at=None,
    )

    manager.ensure_imported_credential(metadata, credential.to_bytes())
    assert manager.inspect_custody(metadata) == "valid"
    resolved = manager.resolve_oauth_access_token_state(
        CONNECTION_ID,
        observed_at=BASE_TIME,
    )
    assert resolved.scopes == scopes


def test_google_identity_url_scope_is_canonicalized_on_persistence_and_resolution(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, scopes=GOOGLE_IDENTITY_SCOPES)
    raw = OAuthCredential(
        access_token="google-identity-access",
        refresh_token="google-identity-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=("openid", GOOGLE_USERINFO_EMAIL_SCOPE, GOOGLE_SCOPE),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    _import_oauth_credential(manager, raw)
    canonical = OAuthCredential.from_bytes(manager.tokens.read(CONNECTION_ID).value)
    assert canonical.scopes == GOOGLE_IDENTITY_SCOPES

    manager.tokens.update(
        CONNECTION_ID,
        expected_version=1,
        value=raw.to_bytes(),
        updated_at=BASE_TIME,
    )
    resolved = manager.resolve_oauth_access_token_state(
        CONNECTION_ID,
        observed_at=BASE_TIME,
    )

    assert resolved.scopes == GOOGLE_IDENTITY_SCOPES
    assert (
        OAuthCredential.from_bytes(manager.tokens.read(CONNECTION_ID).value).scopes
        == GOOGLE_IDENTITY_SCOPES
    )


def test_google_identity_alias_outside_the_canonical_profile_is_rejected(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, scopes=GOOGLE_IDENTITY_SCOPES)
    credential = OAuthCredential(
        access_token="google-alias-access",
        refresh_token="google-alias-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=("openid", "userinfo.email", GOOGLE_SCOPE),
        issued_at=BASE_TIME,
        expires_at=None,
    )
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(OAuthPermissionGrantError) as failure:
        _import_oauth_credential(manager, credential)
    assert failure.value.reason == "outside_selected_tier"

    assert manager.tokens.state(CONNECTION_ID) is None
    assert manager.vault.get_connection_snapshot() == before


def test_microsoft_accepts_canonical_short_grants_without_offline_access(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Microsoft scopes")
    profile = get_profile("microsoft")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(label="Synthetic Microsoft"),
        scopes=profile.read_scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-microsoft-client",
            redirect_uris=("http://localhost:0/oauth/callback",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=BASE_TIME,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    credential = OAuthCredential(
        access_token="microsoft-access",
        refresh_token="microsoft-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(
            "https://graph.microsoft.com/mail.read",
            "USER.READ",
            "https://graph.microsoft.com/CALENDARS.READ",
        ),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    stored = _import_oauth_credential(manager, credential)

    assert stored.version == 1
    assert OAuthCredential.from_bytes(manager.tokens.read(CONNECTION_ID).value).scopes == (
        "Mail.Read",
        "User.Read",
        "Calendars.Read",
    )


def test_microsoft_logical_connector_accepts_known_sibling_grants(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Microsoft logical scopes")
    profile = get_connector_profile("outlook_mail")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(label="Synthetic Microsoft"),
        scopes=profile.full_scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-microsoft-client",
            redirect_uris=("http://localhost:0/oauth/callback",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=BASE_TIME,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    credential = OAuthCredential(
        access_token="microsoft-access",
        refresh_token="microsoft-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(*profile.full_scopes, "Calendars.ReadWrite"),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    stored = _import_oauth_credential(manager, credential)

    assert stored.version == 1
    assert OAuthCredential.from_bytes(manager.tokens.read(CONNECTION_ID).value).scopes == (
        "User.Read",
        "Mail.ReadWrite",
        "Mail.Send",
        "Calendars.ReadWrite",
    )


def test_microsoft_publish_canonicalizes_access_scopes_and_omits_non_resource_scopes(
    tmp_path: Path,
) -> None:
    manager = _microsoft_manager(tmp_path)
    metadata = _fresh_metadata(
        manager,
        "con-" + "p" * 32,
        MICROSOFT_ACCOUNT_FINGERPRINT,
    )
    credential = OAuthCredential(
        access_token="microsoft-publish-access",
        refresh_token="microsoft-publish-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(
            "https://graph.microsoft.com/mail.read",
            "USER.READ",
            "https://graph.microsoft.com/CALENDARS.READ",
            "offline_access",
            "openid",
            "profile",
            "email",
        ),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    manager.publish_new_oauth_connection(metadata, credential)

    stored = OAuthCredential.from_bytes(manager.tokens.read(metadata.connection_id).value)
    assert stored.scopes == ("Mail.Read", "User.Read", "Calendars.Read")
    assert "offline_access" not in stored.scopes


def test_validate_import_returns_canonical_microsoft_credential_bytes(tmp_path: Path) -> None:
    manager = _microsoft_manager(tmp_path)
    metadata = manager.vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert metadata is not None
    raw = OAuthCredential(
        access_token="microsoft-import-access",
        refresh_token="microsoft-import-refresh",
        token_type=OAuthTokenType.BEARER,
        scopes=(
            "https://graph.microsoft.com/mail.read",
            "USER.READ",
            "https://graph.microsoft.com/calendars.read",
            "offline_access",
        ),
        issued_at=BASE_TIME,
        expires_at=None,
    )

    canonical_bytes = manager.validate_import_credential(metadata, raw.to_bytes())

    canonical = OAuthCredential.from_bytes(canonical_bytes)
    assert canonical.scopes == ("Mail.Read", "User.Read", "Calendars.Read")
    assert canonical.refresh_token == raw.refresh_token
    assert manager.tokens.state(CONNECTION_ID) is None


@pytest.mark.parametrize(
    "health",
    [
        ConnectionHealth.READY,
        ConnectionHealth.DEGRADED,
        ConnectionHealth.REAUTHORIZATION_REQUIRED,
    ],
)
def test_verified_same_id_oauth_rotation_accepts_recoverable_health(
    tmp_path: Path,
    health: ConnectionHealth,
) -> None:
    manager, original = _rotation_manager(tmp_path, health=health)
    before = manager.vault.get_connection_snapshot()
    replacement = _rotation_replacement()

    result = manager.rotate_verified_oauth_credential(
        CONNECTION_ID,
        expected_revision=before.revision,
        expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
        expected_token_version=1,
        replacement=replacement,
        account_label="Recovered account",
    )

    after = manager.vault.get_connection_snapshot()
    connection = after.connection(CONNECTION_ID)
    assert connection is not None
    assert connection.health is ConnectionHealth.READY
    assert connection.account.fingerprint == GOOGLE_ACCOUNT_FINGERPRINT
    assert connection.account.label == "Recovered account"
    assert after.revision != before.revision
    assert result["connections"][0]["health"] == "ready"
    assert manager.tokens.read(CONNECTION_ID).state.version == 2
    assert manager.tokens.read(CONNECTION_ID).value == replacement.to_bytes()
    assert original.to_bytes() != replacement.to_bytes()
    assert GOOGLE_ACCOUNT_FINGERPRINT not in json.dumps(result, sort_keys=True)
    assert "rotation-new-access" not in json.dumps(result, sort_keys=True)


def test_same_id_rotation_rejects_invalid_label_before_token_commit(tmp_path: Path) -> None:
    manager, original = _rotation_manager(tmp_path)
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(ValidationError, match="account label"):
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=1,
            replacement=_rotation_replacement(),
            account_label="x" * 513,
        )

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == before


def test_same_id_rotation_repairs_corrupt_old_credential_bytes(tmp_path: Path) -> None:
    manager, _original = _rotation_manager(tmp_path)
    manager.tokens.update(
        CONNECTION_ID,
        expected_version=1,
        value=b"corrupt-old-credential",
        updated_at=BASE_TIME + timedelta(microseconds=1),
    )
    before = manager.vault.get_connection_snapshot()
    current = manager.tokens.read(CONNECTION_ID)
    replacement = _rotation_replacement()

    result = manager.rotate_verified_oauth_credential(
        CONNECTION_ID,
        expected_revision=before.revision,
        expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
        expected_token_version=current.state.version,
        replacement=replacement,
    )

    after = manager.vault.get_connection_snapshot()
    connection = after.connection(CONNECTION_ID)
    assert connection is not None
    assert connection.health is ConnectionHealth.READY
    assert result["connections"][0]["health"] == "ready"
    assert after.revision != before.revision
    assert manager.tokens.read(CONNECTION_ID).state.version == current.state.version + 1
    assert manager.tokens.read(CONNECTION_ID).value == replacement.to_bytes()


def test_same_id_rotation_repairs_missing_pointer_at_expected_zero_version(
    tmp_path: Path,
) -> None:
    manager, _original = _rotation_manager(tmp_path)
    state_path = manager.tokens.root / "state" / f"{CONNECTION_ID}.json"
    state_path.unlink()
    before = manager.vault.get_connection_snapshot()
    replacement = _rotation_replacement()

    manager.rotate_verified_oauth_credential(
        CONNECTION_ID,
        expected_revision=before.revision,
        expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
        expected_token_version=0,
        replacement=replacement,
    )

    state = manager.tokens.state(CONNECTION_ID)
    assert state is not None
    assert state.version == 1
    assert manager.tokens.read(CONNECTION_ID).value == replacement.to_bytes()


def test_same_id_rotation_repairs_missing_referenced_secret(tmp_path: Path) -> None:
    manager, _original = _rotation_manager(tmp_path)
    current = manager.tokens.state(CONNECTION_ID)
    assert current is not None
    manager.tokens.secrets.delete_secret(
        CONNECTION_ID,
        current.secret_reference.name,
    )
    before = manager.vault.get_connection_snapshot()
    replacement = _rotation_replacement()

    manager.rotate_verified_oauth_credential(
        CONNECTION_ID,
        expected_revision=before.revision,
        expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
        expected_token_version=current.version,
        replacement=replacement,
    )

    state = manager.tokens.state(CONNECTION_ID)
    assert state is not None
    assert state.version == current.version + 1
    assert manager.tokens.read(CONNECTION_ID).value == replacement.to_bytes()


def test_same_id_rotation_rejects_unverified_connection_without_mutation(tmp_path: Path) -> None:
    manager, original = _rotation_manager(tmp_path, health=ConnectionHealth.UNVERIFIED)
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(ValidationError, match="verified connection"):
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=1,
            replacement=_rotation_replacement(),
        )

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == before


def test_same_id_rotation_rejects_revoked_connection_without_mutation(tmp_path: Path) -> None:
    manager, original = _rotation_manager(tmp_path, health=ConnectionHealth.UNVERIFIED)
    before = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=before.revision,
        connection_id=CONNECTION_ID,
        health=ConnectionHealth.REVOKED,
        observed_at=BASE_TIME + timedelta(microseconds=1),
    )
    revoked = manager.vault.get_connection_snapshot()

    with pytest.raises(ValidationError, match="verified connection"):
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=revoked.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=1,
            replacement=_rotation_replacement(),
        )

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == revoked


def test_same_id_rotation_rejects_partial_grant_before_token_publish(tmp_path: Path) -> None:
    manager, original = _rotation_manager(tmp_path)
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(OAuthPermissionGrantError) as failure:
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=1,
            replacement=_rotation_replacement(scopes=(GOOGLE_SCOPE,)),
        )
    assert failure.value.reason == "missing_selected_permissions"

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == before


def test_same_id_rotation_rejects_outside_profile_scope_before_token_publish(
    tmp_path: Path,
) -> None:
    manager, original = _rotation_manager(tmp_path)
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(OAuthPermissionGrantError) as failure:
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=1,
            replacement=_rotation_replacement(
                scopes=(*GOOGLE_IDENTITY_SCOPES, "https://www.googleapis.com/auth/drive"),
            ),
        )
    assert failure.value.reason == "outside_selected_tier"

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == before


def test_same_id_rotation_rejects_stale_token_version_without_mutation(tmp_path: Path) -> None:
    manager, original = _rotation_manager(tmp_path)
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(ConflictError, match="credential changed"):
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=0,
            replacement=_rotation_replacement(),
        )

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == before


def test_same_id_rotation_rejects_revision_drift_without_mutation(tmp_path: Path) -> None:
    manager, original = _rotation_manager(tmp_path)
    before = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=before.revision,
        connection_id=CONNECTION_ID,
        health=ConnectionHealth.DEGRADED,
    )
    drifted = manager.vault.get_connection_snapshot()

    with pytest.raises(ConflictError, match="connection changed"):
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=1,
            replacement=_rotation_replacement(),
        )

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == drifted


def test_same_id_rotation_rejects_fingerprint_mismatch_without_mutation(tmp_path: Path) -> None:
    manager, original = _rotation_manager(tmp_path)
    before = manager.vault.get_connection_snapshot()

    with pytest.raises(ConflictError, match="account binding changed"):
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint="sha256:" + "a" * 64,
            expected_token_version=1,
            replacement=_rotation_replacement(),
        )

    assert manager.tokens.read(CONNECTION_ID).value == original.to_bytes()
    assert manager.vault.get_connection_snapshot() == before


def test_same_id_rotation_reports_committed_token_when_metadata_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _original = _rotation_manager(tmp_path)
    before = manager.vault.get_connection_snapshot()
    replacement = _rotation_replacement()

    def fail_metadata_write(**_kwargs: object) -> dict[str, object]:
        raise SetupError("synthetic metadata write failure")

    monkeypatch.setattr(manager.vault, "put_connection", fail_metadata_write)
    with pytest.raises(
        MutationCommittedError,
        match="credential rotation committed; metadata repair is required",
    ) as failure:
        manager.rotate_verified_oauth_credential(
            CONNECTION_ID,
            expected_revision=before.revision,
            expected_account_fingerprint=GOOGLE_ACCOUNT_FINGERPRINT,
            expected_token_version=1,
            replacement=replacement,
        )

    assert manager.tokens.read(CONNECTION_ID).value == replacement.to_bytes()
    assert manager.tokens.read(CONNECTION_ID).state.version == 2
    assert manager.vault.get_connection_snapshot() == before
    assert "rotation-new-access" not in str(failure.value)
    assert GOOGLE_ACCOUNT_FINGERPRINT not in str(failure.value)


def test_stale_removal_preserves_credential_before_retryable_revocation(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    _import_oauth_credential(manager, _expired_credential())
    stale_revision = manager.vault.get_connection_snapshot().revision
    manager.vault.mark_connection_health(
        expected_revision=stale_revision,
        connection_id=CONNECTION_ID,
        health=ConnectionHealth.DEGRADED,
    )

    with pytest.raises(ConflictError, match="reload before removing"):
        manager.remove(CONNECTION_ID, expected_revision=stale_revision)

    assert manager.tokens.read(CONNECTION_ID).value == _expired_credential().to_bytes()
    current_revision = manager.vault.get_connection_snapshot().revision
    removed = manager.remove(CONNECTION_ID, expected_revision=current_revision)
    assert removed["connections"] == []
    assert manager.tokens.state(CONNECTION_ID) is None


def test_interrupted_removal_leaves_a_terminal_retryable_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    _import_oauth_credential(manager, _expired_credential())
    actual_delete = manager.tokens.delete
    fail_once = True

    def interrupted_delete(connection_id: object) -> bool:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise SetupError("synthetic keyring interruption")
        return actual_delete(parse_connection_id(connection_id))

    monkeypatch.setattr(manager.tokens, "delete", interrupted_delete)
    revision = manager.vault.get_connection_snapshot().revision
    with pytest.raises(SetupError, match="synthetic keyring interruption"):
        manager.remove(CONNECTION_ID, expected_revision=revision)

    interrupted = manager.vault.get_connection_snapshot()
    connection = interrupted.connection(CONNECTION_ID)
    assert connection is not None
    assert connection.health is ConnectionHealth.REVOKED
    assert manager.tokens.read(CONNECTION_ID).value == _expired_credential().to_bytes()
    with pytest.raises(ValidationError, match="revoked"):
        manager.resolve_oauth_access_token_state(CONNECTION_ID)

    removed = manager.remove(CONNECTION_ID, expected_revision=interrupted.revision)
    assert removed["connections"] == []
    assert manager.tokens.state(CONNECTION_ID) is None
