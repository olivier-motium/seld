from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_client_registration import PublicClientRegistration
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import new_connection_id
from continuity_kernel.connector_identity import ConnectorIdentity
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_onboarding import (
    ConnectorIdentityReview,
    ConnectorOnboarding,
)
from continuity_kernel.connector_profiles import ConnectorAccessTier, get_connector_profile
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_transport import AuthorizationScheme, ConnectorCredential
from continuity_kernel.errors import SetupError, ValidationError
from continuity_kernel.vault import Vault

FP_ONE = "sha256:" + "1" * 64
FP_TWO = "sha256:" + "2" * 64


def _manager(tmp_path: Path) -> ConnectorAuthManager:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Connector onboarding")
    return ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "tokens",
    )


def _registration(provider: str) -> PublicClientRegistration:
    redirects = {
        "google": "http://127.0.0.1:0",
        "microsoft": "http://localhost:0/oauth/callback",
        "slack": "http://localhost:43127/oauth/callback",
    }
    return PublicClientRegistration(
        provider=provider,
        client_id=f"{provider}-public-client",
        redirect_template=redirects[provider],
    )


def _identity(provider: str, fingerprint: str = FP_ONE) -> ConnectorIdentity:
    return ConnectorIdentity(
        provider=provider,
        fingerprint=fingerprint,
        display_label="Ada <ada@example.test>",
        portable_label=f"{provider}:{fingerprint[-12:]}",
    )


def _credential(scopes: tuple[str, ...]) -> OAuthCredential:
    now = datetime.now(UTC)
    return OAuthCredential(
        access_token="transient-provider-token",
        refresh_token="transient-refresh-token",
        token_type=OAuthTokenType.BEARER,
        scopes=scopes,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _existing_oauth(
    manager: ConnectorAuthManager,
    connector: str,
    *,
    access: ConnectorAccessTier,
    fingerprint: str = FP_ONE,
) -> ConnectionMetadata:
    profile = get_connector_profile(connector)
    scopes = profile.scopes_for(
        access,
        include_supplemental=(connector == "gmail" and access is ConnectorAccessTier.FULL),
    )
    now = datetime.now(UTC)
    metadata = ConnectionMetadata(
        connection_id=new_connection_id(),
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(
            fingerprint=fingerprint,
            label=f"{profile.provider}:{fingerprint[-12:]}",
        ),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier=f"{profile.provider}-public-client",
            redirect_uris=(_registration(profile.provider).redirect_template,),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
    )
    manager.publish_new_oauth_connection(metadata, _credential(scopes))
    snapshot = manager.vault.get_connection_snapshot()
    ready = snapshot.connection(metadata.connection_id)
    assert ready is not None and ready.health is ConnectionHealth.READY
    return ready


def _unverified_oauth(
    manager: ConnectorAuthManager,
    connector: str,
    *,
    with_credential: bool,
    fingerprint: str | None = None,
) -> ConnectionMetadata:
    profile = get_connector_profile(connector)
    scopes = profile.scopes_for(ConnectorAccessTier.READ)
    now = datetime.now(UTC)
    metadata = ConnectionMetadata(
        connection_id=new_connection_id(),
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(fingerprint=fingerprint),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier=f"{profile.provider}-public-client",
            redirect_uris=(_registration(profile.provider).redirect_template,),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
    )
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.put_connection(
        expected_revision=snapshot.revision,
        connection=metadata,
        observed_at=now,
    )
    if with_credential:
        manager.tokens.update(
            metadata.connection_id,
            expected_version=0,
            value=_credential(scopes).to_bytes(),
            updated_at=now,
        )
    return metadata


def test_oauth_stays_transient_until_identity_confirmation_and_denial_saves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    observed: dict[str, object] = {}

    def acquire(metadata: ConnectionMetadata, **kwargs: object) -> OAuthCredential:
        observed["metadata"] = metadata
        observed["kwargs"] = kwargs
        assert manager.vault.get_connection_snapshot().connections == ()
        assert manager.tokens.state(metadata.connection_id) is None
        return _credential(metadata.scopes)

    monkeypatch.setattr(manager, "acquire_oauth_credential", acquire)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )

    def deny(review: ConnectorIdentityReview) -> bool:
        assert review.display_label == "Ada <ada@example.test>"
        assert "Ada" not in repr(review)
        pending = cast(ConnectionMetadata, observed["metadata"])
        assert manager.vault.get_connection_snapshot().connections == ()
        assert manager.tokens.state(pending.connection_id) is None
        return False

    result = onboarding.connect_oauth(
        "gmail",
        access="read",
        confirm_identity=deny,
        browser_opener=None,
        present_authorization_url=lambda url, opened: None,
    )

    assert result["status"] == "cancelled"
    assert result["nothing_saved"] is True
    assert result["provider_access_may_remain"] is True
    assert "third-party access" in cast(str, result["next"])
    assert manager.vault.get_connection_snapshot().connections == ()
    assert cast(ConnectionMetadata, observed["metadata"]).account.fingerprint is None


def test_missing_registration_fails_friendly_before_oauth_or_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda *args, **kwargs: pytest.fail("OAuth acquisition was reached"),
    )

    def missing(provider: str) -> PublicClientRegistration:
        raise SetupError(f"{provider} sign-in is unavailable in this build; nothing was saved")

    onboarding = ConnectorOnboarding(manager, registration_loader=missing)
    with pytest.raises(SetupError, match=r"sign-in is unavailable.*nothing was saved"):
        onboarding.connect_oauth("slack", access="full", confirm_identity=lambda review: True)
    assert manager.vault.get_connection_snapshot().connections == ()


def test_browser_and_manual_url_callbacks_pass_through_without_becoming_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)

    def browser(url: str) -> bool:
        del url
        return False

    def presenter(url: str, opened: bool) -> None:
        del url, opened

    captured: dict[str, object] = {}

    def acquire(metadata: ConnectionMetadata, **kwargs: object) -> OAuthCredential:
        captured.update(kwargs)
        return _credential(metadata.scopes)

    monkeypatch.setattr(manager, "acquire_oauth_credential", acquire)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    onboarding.connect_oauth(
        "outlook_mail",
        access="read",
        confirm_identity=lambda review: False,
        browser_opener=browser,
        present_authorization_url=presenter,
    )

    assert captured["browser_opener"] is browser
    assert captured["present_authorization_url"] is presenter


def test_wrong_account_upgrade_aborts_and_explicit_new_account_keeps_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    old = _existing_oauth(manager, "outlook_calendar", access=ConnectorAccessTier.READ)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider, FP_TWO),
    )
    profile = get_connector_profile("outlook_calendar")
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(metadata.scopes),
    )

    reviewed: list[str] = []
    different = onboarding.connect_oauth(
        "outlook_calendar",
        access="full",
        confirm_identity=lambda review: reviewed.append(review.display_label) is None or True,
    )
    assert reviewed == ["Ada <ada@example.test>"]
    assert different["status"] == "different_account"
    assert different["nothing_saved"] is True
    assert "--new-account" in cast(str, different["next"])
    assert manager.vault.get_connection_snapshot().connection(old.connection_id) is not None

    connected = onboarding.connect_oauth(
        "outlook_calendar",
        access="full",
        confirm_identity=lambda review: True,
        new_account=True,
    )
    assert connected["status"] == "connected"
    snapshot = manager.vault.get_connection_snapshot()
    assert len(snapshot.connections) == 2
    new_connection = snapshot.connection(cast(str, connected["connection_id"]))
    assert new_connection is not None
    assert profile.access_for_scopes(new_connection.scopes) is ConnectorAccessTier.FULL


def test_same_account_upgrade_keeps_read_ready_until_full_is_published_then_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    old = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.READ)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(metadata.scopes),
    )
    real_publish = manager.publish_new_oauth_connection

    def publish(metadata: ConnectionMetadata, credential: OAuthCredential) -> dict[str, object]:
        snapshot = manager.vault.get_connection_snapshot()
        current = snapshot.connection(old.connection_id)
        assert current is not None and current.health is ConnectionHealth.READY
        assert manager.resolve_oauth_access_token(old.connection_id) == "transient-provider-token"
        return real_publish(metadata, credential)

    monkeypatch.setattr(manager, "publish_new_oauth_connection", publish)
    result = onboarding.connect_oauth(
        "gmail",
        access="full",
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "connected"
    assert result["replaced_connection_id"] == str(old.connection_id)
    snapshot = manager.vault.get_connection_snapshot()
    assert snapshot.connection(old.connection_id) is None
    new = snapshot.connection(cast(str, result["connection_id"]))
    assert new is not None and new.health is ConnectionHealth.READY
    assert "https://mail.google.com/" not in new.scopes


def test_gmail_permanent_delete_is_a_separate_explicit_full_step_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(metadata.scopes),
    )

    connected = onboarding.connect_oauth(
        "gmail",
        access="full",
        include_permanent_delete=True,
        confirm_identity=lambda review: True,
    )
    connection = manager.vault.get_connection_snapshot().connection(
        cast(str, connected["connection_id"])
    )
    assert connection is not None
    assert "https://mail.google.com/" in connection.scopes

    with pytest.raises(ValidationError, match="only for Gmail Full"):
        onboarding.connect_oauth(
            "gmail",
            access="read",
            include_permanent_delete=True,
            confirm_identity=lambda review: True,
        )


def test_reconnecting_same_account_refreshes_equal_grant_but_never_downgrades_broader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    profile = get_connector_profile("google_drive")
    refreshed = _credential(profile.scopes_for(ConnectorAccessTier.READ))
    refreshed = OAuthCredential(
        access_token="replacement-access-token",
        refresh_token="replacement-refresh-token",
        token_type=refreshed.token_type,
        scopes=refreshed.scopes,
        issued_at=refreshed.issued_at,
        expires_at=refreshed.expires_at,
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: refreshed,
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: True,
    )
    assert result["status"] == "already_connected"
    assert result["credential"] == "refreshed"
    assert manager.resolve_oauth_access_token(existing.connection_id) == "replacement-access-token"

    broader = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.FULL)
    gmail_read = _credential(
        get_connector_profile("gmail").scopes_for(ConnectorAccessTier.READ)
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: gmail_read,
    )
    retained = onboarding.connect_oauth(
        "gmail",
        access="read",
        confirm_identity=lambda review: True,
    )
    assert retained["credential"] == "existing_broader_grant_retained"
    assert manager.resolve_oauth_access_token(broader.connection_id) == "transient-provider-token"


def test_resume_verifies_exact_identity_supports_alias_and_cleans_missing_credential(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    pending = _unverified_oauth(
        manager,
        "google_drive",
        with_credential=True,
        fingerprint=FP_ONE,
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )

    resumed = onboarding.resume(
        str(pending.connection_id),
        confirm_identity=lambda review: review.display_label == "Ada <ada@example.test>",
        alias="Personal Drive",
    )
    assert resumed["status"] == "connected"
    ready = manager.vault.get_connection_snapshot().connection(pending.connection_id)
    assert ready is not None and ready.health is ConnectionHealth.READY
    assert ready.account.label == "Personal Drive"
    assert "ada@example.test" not in ready.account.label

    stale = _unverified_oauth(manager, "outlook_mail", with_credential=False)
    missing = onboarding.resume(
        str(stale.connection_id),
        confirm_identity=lambda review: pytest.fail("identity should not be read"),
    )
    assert missing["status"] == "credential_missing_reconnect_required"
    assert missing["next"] == "gsv connectors connect outlook_mail --access read"
    assert manager.vault.get_connection_snapshot().connection(stale.connection_id) is None


def test_resume_shows_wrong_identity_before_returning_exact_retry(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    pending = _unverified_oauth(
        manager,
        "outlook_calendar",
        with_credential=True,
        fingerprint=FP_ONE,
    )
    onboarding = ConnectorOnboarding(
        manager,
        identity_verifier=lambda provider, credential: _identity(provider, FP_TWO),
    )
    reviewed: list[str] = []
    result = onboarding.resume(
        str(pending.connection_id),
        confirm_identity=lambda review: reviewed.append(review.display_label) is None or True,
    )

    assert reviewed == ["Ada <ada@example.test>"]
    assert result["status"] == "different_account"
    assert "--new-account" in cast(str, result["next"])
    current = manager.vault.get_connection_snapshot().connection(pending.connection_id)
    assert current is not None and current.health is ConnectionHealth.UNVERIFIED


def test_list_and_disconnected_status_include_registration_readiness(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(manager, registration_loader=_registration)

    listed = onboarding.list()
    assert listed["registration_readiness"] == {
        "google": {"sign_in": "available", "status": "ready"},
        "microsoft": {"sign_in": "available", "status": "ready"},
        "slack": {"sign_in": "available", "status": "ready"},
    }
    status = onboarding.status("gmail")
    assert status["status"] == "not_connected"
    assert status["connect_commands"] == [
        "gsv connectors connect gmail --access read",
        "gsv connectors connect gmail --access full",
    ]


def test_failed_upgrade_cleanup_keeps_both_connections_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    old = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(metadata.scopes),
    )
    monkeypatch.setattr(
        manager,
        "remove",
        lambda *args, **kwargs: (_ for _ in ()).throw(SetupError("cleanup unavailable")),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="full",
        confirm_identity=lambda review: True,
    )

    assert result["upgrade_cleanup"] == "old_read_connection_retained"
    assert len(manager.vault.get_connection_snapshot().connections) == 2
    assert manager.vault.get_connection_snapshot().connection(old.connection_id) is not None


def test_discord_is_full_only_verifies_bot_and_never_persists_before_confirmation(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    observed: dict[str, object] = {}

    def verify(provider: str, credential: ConnectorCredential) -> ConnectorIdentity:
        observed["provider"] = provider
        observed["scheme"] = credential.scheme
        assert manager.vault.get_connection_snapshot().connections == ()
        return _identity(provider)

    onboarding = ConnectorOnboarding(manager, identity_verifier=verify)
    with pytest.raises(ValidationError, match="require Full"):
        onboarding.connect_discord(
            b"discord-bot-token",
            access="read",
            confirm_identity=lambda review: True,
        )
    result = onboarding.connect_discord(
        b"discord-bot-token",
        access="full",
        confirm_identity=lambda review: False,
    )

    assert result["nothing_saved"] is True
    assert observed == {"provider": "discord", "scheme": AuthorizationScheme.BOT}
    assert manager.vault.get_connection_snapshot().connections == ()


def test_status_and_disconnect_are_redacted_and_do_not_claim_remote_revocation(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    connection = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.READ)
    onboarding = ConnectorOnboarding(manager)

    status = onboarding.status("gmail")
    row = cast(list[dict[str, object]], status["connections"])[0]
    assert row["access"] == "read"
    assert "transient-provider-token" not in repr(status)

    result = onboarding.disconnect(str(connection.connection_id))
    assert result["status"] == "disconnected_locally"
    assert result["provider_access_revoked"] is False
    assert manager.vault.get_connection_snapshot().connections == ()
