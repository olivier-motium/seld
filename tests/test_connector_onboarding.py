from __future__ import annotations

import json
import os
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
from continuity_kernel.connector_profiles import (
    ConnectorAccessTier,
    get_connector_profile,
    get_profile,
)
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_transport import AuthorizationScheme, ConnectorCredential
from continuity_kernel.errors import ConflictError, SetupError, ValidationError
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
    include_permanent_delete: bool = False,
) -> ConnectionMetadata:
    profile = get_connector_profile(connector)
    scopes = profile.scopes_for(
        access,
        include_supplemental=include_permanent_delete,
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


def test_sufficient_existing_oauth_fast_path_keeps_equal_and_broader_grants_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    before = manager.tokens.read(existing.connection_id)
    touched: list[str] = []
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: touched.append(review.display_label) is None,
    )
    assert result["status"] == "already_connected"
    assert result["credential"] == "unchanged"
    assert result["connection_id"] == str(existing.connection_id)
    assert result["health"] == "ready"
    assert touched == []
    after = manager.tokens.read(existing.connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value

    broader = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.FULL)
    broader_before = manager.tokens.read(broader.connection_id)
    retained = onboarding.connect_oauth(
        "gmail",
        access="read",
        confirm_identity=lambda review: True,
    )
    assert retained["credential"] == "unchanged"
    broader_after = manager.tokens.read(broader.connection_id)
    assert broader_after.state.version == broader_before.state.version
    assert broader_after.value == broader_before.value


def test_degraded_valid_oauth_fast_path_does_not_force_reauthorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "outlook_mail", access=ConnectorAccessTier.READ)
    before = manager.tokens.read(existing.connection_id)
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=existing.connection_id,
        health=ConnectionHealth.DEGRADED,
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    result = onboarding.connect_oauth(
        "outlook_mail",
        access="read",
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "already_connected"
    assert result["credential"] == "unchanged"
    assert result["connection_id"] == str(existing.connection_id)
    assert result["health"] == "degraded"
    after = manager.tokens.read(existing.connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value


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
    assert result["connection_id"] == str(pending.connection_id)
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

    assert result["upgrade_cleanup"] == "lower_capability_connection_retained"
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


def test_connector_entrypoints_reject_invalid_alias_before_state_dependent_work(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(
        manager,
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )

    with pytest.raises(ValidationError, match="alias is invalid"):
        onboarding.connect_discord(
            b"discord-bot-token",
            access="full",
            alias="invalid\nalias",
            confirm_identity=lambda review: pytest.fail("confirmation reached"),
        )
    with pytest.raises(ValidationError, match="alias is invalid"):
        onboarding.resume(
            "con-" + "a" * 32,
            alias="invalid\nalias",
            confirm_identity=lambda review: pytest.fail("confirmation reached"),
        )
    with pytest.raises(ValidationError, match="alias is invalid"):
        onboarding.reauthorize_oauth(
            "con-" + "a" * 32,
            alias="invalid\nalias",
            confirm_identity=lambda review: pytest.fail("confirmation reached"),
        )


def test_connector_entrypoints_reject_missing_confirmation_before_provider_work(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )

    with pytest.raises(ValidationError, match="confirmation is unavailable"):
        onboarding.connect_oauth(
            "google_drive",
            access="read",
            confirm_identity=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="confirmation is unavailable"):
        onboarding.connect_discord(
            b"discord-bot-token",
            access="full",
            confirm_identity=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="confirmation is unavailable"):
        onboarding.resume(
            "con-" + "a" * 32,
            confirm_identity=None,  # type: ignore[arg-type]
        )


def test_discord_same_bot_reconnect_reuses_connection_and_refreshes_credential(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(
        manager,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    first = onboarding.connect_discord(
        b"discord-bot-token-v1",
        access="full",
        confirm_identity=lambda review: True,
    )

    second = onboarding.connect_discord(
        b"discord-bot-token-v2",
        access="full",
        alias="Community Bot",
        confirm_identity=lambda review: True,
    )
    third = onboarding.connect_discord(
        b"discord-bot-token-v3",
        access="full",
        new_account=True,
        confirm_identity=lambda review: True,
    )

    assert first["status"] == "connected"
    assert second["status"] == "already_connected"
    assert second["credential"] == "refreshed"
    assert second["connection_id"] == first["connection_id"]
    assert third["status"] == "already_connected"
    assert third["connection_id"] == first["connection_id"]
    assert manager.resolve_credential(cast(str, first["connection_id"])) == (
        b"discord-bot-token-v3"
    )
    snapshot = manager.vault.get_connection_snapshot()
    assert len(snapshot.connections) == 1
    current = snapshot.connection(cast(str, first["connection_id"]))
    assert current is not None and current.account.label == "Community Bot"


def test_discord_reuse_fails_closed_if_metadata_changes_after_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(
        manager,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    first = onboarding.connect_discord(
        b"discord-bot-token-v1",
        access="full",
        confirm_identity=lambda review: True,
    )
    connection_id = cast(str, first["connection_id"])
    before = manager.resolve_credential_state(connection_id)
    real_rotate = manager.rotate_verified_bearer_credential

    def race(*args: object, **kwargs: object) -> dict[str, object]:
        snapshot = manager.vault.get_connection_snapshot()
        manager.vault.mark_connection_health(
            expected_revision=snapshot.revision,
            connection_id=connection_id,
            health=ConnectionHealth.DEGRADED,
        )
        return real_rotate(*args, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(manager, "rotate_verified_bearer_credential", race)

    with pytest.raises(ConflictError, match="connection changed"):
        onboarding.connect_discord(
            b"discord-bot-token-v2",
            access="full",
            confirm_identity=lambda review: True,
        )

    after = manager.resolve_credential_state(connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value


def test_discord_stuck_unverified_same_bot_returns_resume_instead_of_duplicate(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(
        manager,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    first = onboarding.connect_discord(
        b"discord-bot-token",
        access="full",
        confirm_identity=lambda review: True,
    )
    connection_id = cast(str, first["connection_id"])
    before = manager.resolve_credential_state(connection_id)
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=connection_id,
        health=ConnectionHealth.UNVERIFIED,
    )

    retry = onboarding.connect_discord(
        b"discord-bot-token",
        access="full",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert retry["status"] == "setup_incomplete"
    assert retry["connection_id"] == connection_id
    assert retry["next"] == f"gsv connectors resume {connection_id}"
    assert len(manager.vault.get_connection_snapshot().connections) == 1
    assert manager.resolve_credential_state(connection_id) == before

    resumed = onboarding.resume(connection_id, confirm_identity=lambda review: True)
    assert resumed["status"] == "connected"
    assert resumed["connection_id"] == connection_id


def test_discord_stuck_unverified_corrupt_custody_points_to_disconnect(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    onboarding = ConnectorOnboarding(
        manager,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    first = onboarding.connect_discord(
        b"discord-bot-token",
        access="full",
        confirm_identity=lambda review: True,
    )
    connection_id = cast(str, first["connection_id"])
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=connection_id,
        health=ConnectionHealth.UNVERIFIED,
    )
    state = manager.tokens.state(connection_id)
    assert state is not None
    manager.tokens.update(
        connection_id,
        expected_version=state.version,
        value=b"\xffcorrupt-bot-custody",
    )

    retry = onboarding.connect_discord(
        b"discord-bot-token",
        access="full",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert retry["status"] == "credential_invalid_reconnect_required"
    assert retry["next"] == f"gsv connectors disconnect {connection_id}"
    assert "resume" not in cast(str, retry["next"])


def test_discord_different_bot_requires_explicit_new_account(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)

    def verify(provider: str, credential: ConnectorCredential) -> ConnectorIdentity:
        fingerprint = FP_TWO if credential.secret.endswith("two") else FP_ONE
        return _identity(provider, fingerprint)

    onboarding = ConnectorOnboarding(manager, identity_verifier=verify)
    first = onboarding.connect_discord(
        b"discord-bot-one",
        access="full",
        confirm_identity=lambda review: True,
    )
    connection_id = cast(str, first["connection_id"])
    before = manager.resolve_credential_state(connection_id)

    second = onboarding.connect_discord(
        b"discord-bot-two",
        access="full",
        confirm_identity=lambda review: True,
    )

    assert second["status"] == "different_account"
    assert second["nothing_saved"] is True
    after = manager.resolve_credential_state(connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value
    assert len(manager.vault.get_connection_snapshot().connections) == 1


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


def test_storage_permissions_failure_is_backend_unavailable_and_does_not_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission mode is required")
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )
    state_directory = manager.tokens.root / "state"
    state_directory.chmod(0o755)
    try:
        status = manager.status()
        assert status["connections"][0]["host_credential"] == "backend_unavailable"

        with pytest.raises(SetupError, match="credential custody is unavailable"):
            onboarding.connect_oauth(
                "google_drive",
                access="read",
                confirm_identity=lambda review: pytest.fail("confirmation reached"),
            )

        current = manager.vault.get_connection_snapshot().connection(existing.connection_id)
        assert current is not None and current.health is ConnectionHealth.READY
    finally:
        state_directory.chmod(0o700)


def test_invalid_alias_is_rejected_before_a_healthy_preflight_noop(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )

    with pytest.raises(ValidationError, match="alias is invalid"):
        onboarding.connect_oauth(
            "google_drive",
            access="read",
            alias="invalid\nalias",
            confirm_identity=lambda review: pytest.fail("confirmation reached"),
        )


def test_legacy_google_bundle_reports_held_permanent_delete_capability(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    profile = get_profile("google")
    scopes = profile.scopes_for(ConnectorAccessTier.FULL, include_supplemental=True)
    now = datetime.now(UTC)
    metadata = ConnectionMetadata(
        connection_id=new_connection_id(),
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(fingerprint=FP_ONE, label="google:legacy"),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="google-public-client",
            redirect_uris=(_registration("google").redirect_template,),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
    )
    manager.publish_new_oauth_connection(metadata, _credential(scopes))

    result = ConnectorOnboarding(manager, registration_loader=_registration).list()
    row = cast(list[dict[str, object]], result["connections"])[0]

    assert row["connector"] == "legacy_provider_bundle"
    assert row["permanent_delete"] is True


def test_read_preflight_prefers_a_healthy_sufficient_grant_over_broken_broader_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    healthy_read = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    broken_full = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.FULL)
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=broken_full.connection_id,
        health=ConnectionHealth.REAUTHORIZATION_REQUIRED,
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "already_connected"
    assert result["connection_id"] == str(healthy_read.connection_id)
    assert result["credential"] == "unchanged"


def test_read_preflight_prefers_valid_narrower_custody_over_missing_broader_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    healthy_read = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    missing_full = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.FULL)
    manager.tokens.delete(missing_full.connection_id)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "already_connected"
    assert result["connection_id"] == str(healthy_read.connection_id)
    assert result["credential"] == "unchanged"


def test_unbound_lower_capability_record_requires_cleanup_before_step_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    unbound = _unverified_oauth(
        manager,
        "google_drive",
        with_credential=True,
        fingerprint=None,
    )
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=unbound.connection_id,
        health=ConnectionHealth.REAUTHORIZATION_REQUIRED,
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="full",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "identity_binding_missing_reconnect_required"
    assert result["connection_id"] == str(unbound.connection_id)
    assert result["next"] == f"gsv connectors disconnect {unbound.connection_id}"


@pytest.mark.parametrize("access", (ConnectorAccessTier.READ, ConnectorAccessTier.FULL))
def test_sufficient_preflight_does_not_load_registration_or_touch_oauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: ConnectorAccessTier,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=access)
    before = manager.tokens.read(existing.connection_id)
    registration_calls: list[str] = []
    identity_calls: list[str] = []
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: (
            registration_calls.append(provider) or _registration(provider)
        ),
        identity_verifier=lambda provider, credential: (
            identity_calls.append(provider) or _identity(provider)
        ),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access=access,
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "already_connected"
    assert result["connection_id"] == str(existing.connection_id)
    assert result["credential"] == "unchanged"
    assert result["access"] == access.value
    assert result["health"] == "ready"
    assert registration_calls == []
    assert identity_calls == []
    after = manager.tokens.read(existing.connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value


def test_two_bound_accounts_require_selection_and_exact_selector_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    first = _existing_oauth(
        manager, "google_drive", access=ConnectorAccessTier.READ, fingerprint=FP_ONE
    )
    second = _existing_oauth(
        manager,
        "google_drive",
        access=ConnectorAccessTier.READ,
        fingerprint=FP_TWO,
    )
    registration_calls: list[str] = []
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: (
            registration_calls.append(provider) or _registration(provider)
        ),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    selected = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert selected["status"] == "account_selection_required"
    rows = cast(list[dict[str, object]], selected["candidates"])
    assert {row["connection_id"] for row in rows} == {
        str(first.connection_id),
        str(second.connection_id),
    }
    assert all(row["access"] == "read" and row["health"] == "ready" for row in rows)
    assert all("--connection-id" in cast(str, row["command"]) for row in rows)
    assert registration_calls == []
    encoded = json.dumps(selected)
    assert FP_ONE not in encoded and FP_TWO not in encoded
    assert "ada@example.test" not in encoded

    exact = onboarding.connect_oauth(
        "google_drive",
        access="read",
        connection_id=str(second.connection_id),
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )
    assert exact["status"] == "already_connected"
    assert exact["connection_id"] == str(second.connection_id)
    assert registration_calls == []


def test_unverified_valid_custody_returns_setup_incomplete_without_oauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "setup_incomplete"
    assert result["connection_id"] == str(pending.connection_id)
    assert result["next"] == f"gsv connectors resume {pending.connection_id}"
    current = manager.vault.get_connection_snapshot().connection(pending.connection_id)
    assert current == pending


def test_missing_identity_binding_points_to_cleanup_then_reconnect(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    pending = _unverified_oauth(
        manager,
        "google_drive",
        with_credential=True,
        fingerprint=None,
    )
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=pending.connection_id,
        health=ConnectionHealth.REAUTHORIZATION_REQUIRED,
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "identity_binding_missing_reconnect_required"
    assert result["next"] == f"gsv connectors disconnect {pending.connection_id}"
    assert "--new-account" in cast(str, result["reconnect"])


def test_corrupt_unverified_custody_never_points_to_resume(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    pending = _unverified_oauth(
        manager,
        "google_drive",
        with_credential=True,
        fingerprint=FP_ONE,
    )
    state = manager.tokens.state(pending.connection_id)
    assert state is not None
    manager.tokens.update(
        pending.connection_id,
        expected_version=state.version,
        value=b"corrupt-oauth-custody",
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "credential_invalid_reconnect_required"
    assert result["next"] == f"gsv connectors disconnect {pending.connection_id}"
    assert "resume" not in cast(str, result["next"])


def test_new_account_oauth_retry_guides_corrupt_unverified_custody_to_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    pending = _unverified_oauth(
        manager,
        "google_drive",
        with_credential=True,
        fingerprint=FP_ONE,
    )
    state = manager.tokens.state(pending.connection_id)
    assert state is not None
    manager.tokens.update(
        pending.connection_id,
        expected_version=state.version,
        value=b"corrupt-oauth-custody",
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(metadata.scopes),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        new_account=True,
        confirm_identity=lambda review: pytest.fail("confirmation reached"),
    )

    assert result["status"] == "credential_invalid_reconnect_required"
    assert result["connection_id"] == str(pending.connection_id)
    assert result["next"] == f"gsv connectors disconnect {pending.connection_id}"
    assert len(manager.vault.get_connection_snapshot().connections) == 1


def test_new_account_oauth_retry_guides_corrupt_reusable_pointer_to_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    pointer = manager.tokens.root / "state" / f"{existing.connection_id}.json"
    pointer.write_text("not-json", encoding="utf-8")
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(metadata.scopes),
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        new_account=True,
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "credential_pointer_invalid_reconnect_required"
    assert result["connection_id"] == str(existing.connection_id)
    assert result["next"] == f"gsv connectors disconnect {existing.connection_id}"
    assert len(manager.vault.get_connection_snapshot().connections) == 1


@pytest.mark.parametrize("entrypoint", ("connect", "reauthorize"))
def test_unreadable_oauth_pointer_fails_before_sign_in_and_disconnect_repairs_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    pointer = manager.tokens.root / "state" / f"{existing.connection_id}.json"
    pointer.write_text("not-json", encoding="utf-8")
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: pytest.fail("identity verification reached"),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: pytest.fail("OAuth acquisition reached"),
    )

    if entrypoint == "connect":
        result = onboarding.connect_oauth(
            "google_drive",
            access="read",
            connection_id=str(existing.connection_id),
            confirm_identity=lambda review: pytest.fail("confirmation reached"),
        )
    else:
        result = onboarding.reauthorize_oauth(
            str(existing.connection_id),
            confirm_identity=lambda review: pytest.fail("confirmation reached"),
        )

    assert result["status"] == "credential_pointer_invalid_reconnect_required"
    assert result["next"] == f"gsv connectors disconnect {existing.connection_id}"
    assert onboarding.disconnect(str(existing.connection_id))["status"] == "disconnected_locally"
    assert manager.vault.get_connection_snapshot().connection(existing.connection_id) is None
    assert manager.tokens.state(existing.connection_id) is None


@pytest.mark.parametrize("custody", ("missing", "corrupt"))
def test_exact_connect_repairs_missing_or_corrupt_custody_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custody: str,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    if custody == "missing":
        manager.tokens.delete(existing.connection_id)
    else:
        state = manager.tokens.state(existing.connection_id)
        assert state is not None
        manager.tokens.update(
            existing.connection_id,
            expected_version=state.version,
            value=b"corrupt-oauth-custody",
        )
    replacement = _credential(existing.scopes)
    registration_calls: list[str] = []
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: (
            registration_calls.append(provider) or pytest.fail("registration loading reached")
        ),
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: replacement,
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        connection_id=str(existing.connection_id),
        alias="Recovered Drive",
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "connected"
    assert result["connection_id"] == str(existing.connection_id)
    assert registration_calls == []
    current = manager.vault.get_connection_snapshot().connection(existing.connection_id)
    assert current is not None and current.health is ConnectionHealth.READY
    assert current.account.label == "Recovered Drive"
    assert result["account_label"] == "Recovered Drive"
    assert manager.tokens.read(existing.connection_id).value == replacement.to_bytes()


def test_exact_connect_repairs_a_corrupt_keyring_value_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    replacement = _credential(existing.scopes)
    original_read = manager.tokens.read
    read_attempts = 0

    def corrupt_keyring_read(connection_id: object) -> object:
        nonlocal read_attempts
        read_attempts += 1
        if read_attempts <= 2:
            raise ValidationError("synthetic corrupt keyring value")
        return original_read(connection_id)  # type: ignore[arg-type]

    monkeypatch.setattr(manager.tokens, "read", corrupt_keyring_read)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: replacement,
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        connection_id=str(existing.connection_id),
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "connected"
    assert result["connection_id"] == str(existing.connection_id)
    assert read_attempts == 2
    assert manager.tokens.read(existing.connection_id).value == replacement.to_bytes()


def test_reauthorization_wrong_account_saves_nothing_and_keeps_privacy_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=existing.connection_id,
        health=ConnectionHealth.REAUTHORIZATION_REQUIRED,
    )
    before = manager.tokens.read(existing.connection_id)
    replacement = OAuthCredential(
        access_token="access-token-sentinel",
        refresh_token="refresh-token-sentinel",
        token_type=OAuthTokenType.BEARER,
        scopes=existing.scopes,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    identity = ConnectorIdentity(
        provider="google",
        fingerprint=FP_TWO,
        display_label="provider-email-sentinel",
        portable_label=f"google:{FP_TWO[-12:]}",
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: identity,
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: replacement,
    )

    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        connection_id=str(existing.connection_id),
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "different_account"
    encoded = json.dumps(result)
    for sentinel in (
        "provider-email-sentinel",
        FP_ONE,
        FP_TWO,
        "access-token-sentinel",
        "refresh-token-sentinel",
    ):
        assert sentinel not in encoded
    after = manager.tokens.read(existing.connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value
    current = manager.vault.get_connection_snapshot().connection(existing.connection_id)
    assert current is not None and current.health is ConnectionHealth.REAUTHORIZATION_REQUIRED


def test_reauthorization_race_after_confirmation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    before = manager.tokens.read(existing.connection_id)
    replacement = _credential(existing.scopes)
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail("registration loading reached"),
        identity_verifier=lambda provider, credential: _identity(provider),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: replacement,
    )
    real_rotate = manager.rotate_verified_oauth_credential

    def race(*args: object, **kwargs: object) -> dict[str, object]:
        current_snapshot = manager.vault.get_connection_snapshot()
        manager.vault.mark_connection_health(
            expected_revision=current_snapshot.revision,
            connection_id=existing.connection_id,
            health=ConnectionHealth.DEGRADED,
        )
        return real_rotate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(manager, "rotate_verified_oauth_credential", race)

    with pytest.raises(ConflictError, match="connection changed"):
        onboarding.reauthorize_oauth(
            str(existing.connection_id),
            confirm_identity=lambda review: True,
        )
    after = manager.tokens.read(existing.connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value


def test_read_to_full_failure_keeps_old_read_connection(
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
        lambda metadata, **kwargs: (_ for _ in ()).throw(SetupError("OAuth denied")),
    )

    with pytest.raises(SetupError, match="OAuth denied"):
        onboarding.connect_oauth(
            "google_drive",
            access="full",
            confirm_identity=lambda review: True,
        )
    snapshot = manager.vault.get_connection_snapshot()
    assert snapshot.connection(old.connection_id) is not None
    assert len(snapshot.connections) == 1


def test_gmail_purge_step_up_publishes_rank_two_then_removes_plain_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    old = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.FULL)
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
    observed_old_during_publish: list[bool] = []

    def publish(metadata: ConnectionMetadata, credential: OAuthCredential) -> dict[str, object]:
        observed_old_during_publish.append(
            manager.vault.get_connection_snapshot().connection(old.connection_id) is not None
        )
        return real_publish(metadata, credential)

    monkeypatch.setattr(manager, "publish_new_oauth_connection", publish)
    result = onboarding.connect_oauth(
        "gmail",
        access="full",
        include_permanent_delete=True,
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "connected"
    assert result["permanent_delete"] is True
    assert "capability" not in result
    assert result["replaced_connection_id"] == str(old.connection_id)
    assert observed_old_during_publish == [True]
    snapshot = manager.vault.get_connection_snapshot()
    assert snapshot.connection(old.connection_id) is None
    new = snapshot.connection(cast(str, result["connection_id"]))
    assert new is not None
    assert "https://mail.google.com/" in new.scopes


def test_retained_broader_grant_reports_its_actual_reauthorization_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.FULL)
    snapshot = manager.vault.get_connection_snapshot()
    manager.vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=existing.connection_id,
        health=ConnectionHealth.REAUTHORIZATION_REQUIRED,
    )
    before = manager.tokens.read(existing.connection_id)
    profile = get_connector_profile("gmail")
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(profile.scopes_for(ConnectorAccessTier.READ)),
    )

    result = onboarding.connect_oauth(
        "gmail",
        access="read",
        new_account=True,
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "already_connected"
    assert result["credential"] == "existing_broader_grant_retained"
    assert result["health"] == "reauthorization_required"
    after = manager.tokens.read(existing.connection_id)
    assert after.state.version == before.state.version
    assert after.value == before.value


@pytest.mark.parametrize(
    ("custody", "status"),
    (
        ("missing", "credential_missing_reconnect_required"),
        ("invalid", "credential_invalid_reconnect_required"),
    ),
)
def test_broken_broader_grant_never_claims_success_after_narrower_oauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    custody: str,
    status: str,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.FULL)
    if custody == "missing":
        manager.tokens.delete(existing.connection_id)
    else:
        state = manager.tokens.state(existing.connection_id)
        assert state is not None
        manager.tokens.update(
            existing.connection_id,
            expected_version=state.version,
            value=b"corrupt-oauth-custody",
        )
    profile = get_connector_profile("gmail")
    acquired: list[str] = []
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )

    def acquire(metadata: ConnectionMetadata, **kwargs: object) -> OAuthCredential:
        acquired.append(str(metadata.connection_id))
        return _credential(profile.scopes_for(ConnectorAccessTier.READ))

    monkeypatch.setattr(manager, "acquire_oauth_credential", acquire)
    result = onboarding.connect_oauth(
        "gmail",
        access="read",
        new_account=True,
        confirm_identity=lambda review: True,
    )

    assert result["status"] == status
    assert result["connection_id"] == str(existing.connection_id)
    assert result["next"] == (
        f"gsv connectors connect gmail --access full --connection-id {existing.connection_id}"
    )
    assert result["provider_access_may_remain"] is True
    assert result["revocation_help"]
    assert "credential" not in result
    assert acquired and acquired[0] != str(existing.connection_id)
    assert manager.tokens.state(acquired[0]) is None
    snapshot = manager.vault.get_connection_snapshot()
    assert snapshot.connections == (snapshot.connection(existing.connection_id),)


def test_new_account_reuse_prefers_valid_grant_over_broken_broader_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    healthy_read = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.READ)
    broken_full = _existing_oauth(manager, "gmail", access=ConnectorAccessTier.FULL)
    manager.tokens.delete(broken_full.connection_id)
    profile = get_connector_profile("gmail")
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider, FP_ONE),
    )
    monkeypatch.setattr(
        manager,
        "acquire_oauth_credential",
        lambda metadata, **kwargs: _credential(profile.scopes_for(ConnectorAccessTier.READ)),
    )

    result = onboarding.connect_oauth(
        "gmail",
        access="read",
        new_account=True,
        confirm_identity=lambda review: True,
    )

    assert result["status"] == "already_connected"
    assert result["credential"] == "refreshed"
    assert result["connection_id"] == str(healthy_read.connection_id)
    assert manager.tokens.state(broken_full.connection_id) is None
    assert len(manager.vault.get_connection_snapshot().connections) == 2


def test_new_account_bypasses_sufficient_fast_path_and_runs_fresh_oauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    existing = _existing_oauth(manager, "google_drive", access=ConnectorAccessTier.READ)
    acquired: list[str] = []
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=_registration,
        identity_verifier=lambda provider, credential: _identity(provider),
    )

    def acquire(metadata: ConnectionMetadata, **kwargs: object) -> OAuthCredential:
        acquired.append(str(metadata.connection_id))
        return _credential(metadata.scopes)

    monkeypatch.setattr(manager, "acquire_oauth_credential", acquire)
    result = onboarding.connect_oauth(
        "google_drive",
        access="read",
        new_account=True,
        confirm_identity=lambda review: True,
    )

    assert acquired
    assert result["status"] == "already_connected"
    assert result["connection_id"] == str(existing.connection_id)
