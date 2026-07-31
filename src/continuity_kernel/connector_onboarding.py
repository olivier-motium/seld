"""DAU-oriented connector onboarding without ambient host credentials."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast

from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_client_registration import (
    PublicClientRegistration,
    load_public_client_registration,
)
from continuity_kernel.connector_identifiers import new_connection_id, parse_connection_id
from continuity_kernel.connector_identity import ConnectorIdentity, verify_identity
from continuity_kernel.connector_profiles import (
    CONNECTOR_PROFILES,
    ConnectorAccessTier,
    ConnectorProfile,
    get_connector_profile,
    get_profile_for_connection,
)
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
)
from continuity_kernel.errors import ContinuityError, SetupError, ValidationError

ConfirmIdentity = Callable[["ConnectorIdentityReview"], bool]
IdentityVerifier = Callable[[str, ConnectorCredential], ConnectorIdentity]
RegistrationLoader = Callable[[str], PublicClientRegistration]
BrowserOpener = Callable[[str], bool]
AuthorizationPresenter = Callable[[str, bool], None]

_DEFAULT_BROWSER: Final = object()
_CONNECTED_HEALTH: Final = frozenset({ConnectionHealth.READY, ConnectionHealth.DEGRADED})


@dataclass(frozen=True, repr=False)
class ConnectorIdentityReview:
    """Transient provider identity shown before anything is persisted."""

    connector: str
    provider: str
    access: ConnectorAccessTier
    display_label: str

    def __repr__(self) -> str:
        return (
            "ConnectorIdentityReview("
            f"connector={self.connector!r}, provider={self.provider!r}, "
            f"access={self.access!r}, display_label=<redacted>)"
        )


class ConnectorOnboarding:
    """Acquire, verify, confirm, and atomically publish one logical connection."""

    def __init__(
        self,
        manager: ConnectorAuthManager,
        *,
        registration_loader: RegistrationLoader = load_public_client_registration,
        identity_verifier: IdentityVerifier = verify_identity,
    ) -> None:
        if not isinstance(manager, ConnectorAuthManager):
            raise ValidationError("connector onboarding manager is invalid")
        if not callable(registration_loader) or not callable(identity_verifier):
            raise ValidationError("connector onboarding dependency is invalid")
        self.manager = manager
        self.registration_loader = registration_loader
        self.identity_verifier = identity_verifier

    @property
    def connectors(self) -> tuple[str, ...]:
        return tuple(sorted(CONNECTOR_PROFILES))

    def connect_oauth(
        self,
        connector: str,
        *,
        access: ConnectorAccessTier | str,
        confirm_identity: ConfirmIdentity,
        new_account: bool = False,
        timeout_seconds: float = 180.0,
        browser_opener: BrowserOpener | None | object = _DEFAULT_BROWSER,
        present_authorization_url: AuthorizationPresenter | None = None,
    ) -> dict[str, object]:
        """Connect one OAuth source, keeping every credential transient until confirmation."""

        profile = get_connector_profile(connector)
        if profile.credential_kind is not CredentialKind.OAUTH2:
            raise ValidationError("this connector uses bot onboarding, not OAuth")
        tier = _access(access)
        registration = self.registration_loader(profile.provider)
        pending = _pending_oauth_metadata(profile, tier, registration)
        if browser_opener is _DEFAULT_BROWSER:
            credential = self.manager.acquire_oauth_credential(
                pending,
                timeout_seconds=timeout_seconds,
                present_authorization_url=present_authorization_url,
            )
        else:
            credential = self.manager.acquire_oauth_credential(
                pending,
                timeout_seconds=timeout_seconds,
                browser_opener=cast(BrowserOpener | None, browser_opener),
                present_authorization_url=present_authorization_url,
            )
        identity = self.identity_verifier(
            profile.provider,
            ConnectorCredential(
                scheme=AuthorizationScheme.BEARER,
                secret=credential.access_token,
            ),
        )
        return self._confirm_and_publish(
            profile,
            tier,
            pending,
            identity,
            confirm_identity=confirm_identity,
            new_account=new_account,
            publish=lambda metadata: self.manager.publish_new_oauth_connection(
                metadata,
                credential,
            ),
        )

    def connect_discord(
        self,
        token: bytes,
        *,
        access: ConnectorAccessTier | str,
        confirm_identity: ConfirmIdentity,
        new_account: bool = False,
    ) -> dict[str, object]:
        """Connect one Discord bot from hidden input; Discord user tokens are never accepted."""

        profile = get_connector_profile("discord")
        tier = _access(access)
        if tier is not ConnectorAccessTier.FULL:
            raise ValidationError("Discord bot connections require Full access")
        if type(token) is not bytes:
            raise ValidationError("Discord bot credential is invalid")
        try:
            secret = token.decode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("Discord bot credential is invalid") from exc
        runtime_credential = ConnectorCredential(
            scheme=AuthorizationScheme.BOT,
            secret=secret,
        )
        identity = self.identity_verifier("discord", runtime_credential)
        pending = _pending_bot_metadata(profile)
        return self._confirm_and_publish(
            profile,
            tier,
            pending,
            identity,
            confirm_identity=confirm_identity,
            new_account=new_account,
            publish=lambda metadata: self.manager.publish_new_bearer_connection(
                metadata,
                token,
            ),
        )

    def list(self) -> dict[str, object]:
        """Return redacted connections with their logical access tier."""

        status = self.manager.status()
        rows = status.get("connections")
        if not isinstance(rows, list):
            raise SetupError("connector status is invalid")
        snapshot = self.manager.vault.get_connection_snapshot()
        projected: list[dict[str, object]] = []
        for raw in rows:
            if not isinstance(raw, dict) or not isinstance(raw.get("connection_id"), str):
                raise SetupError("connector status is invalid")
            connection = snapshot.connection(cast(str, raw["connection_id"]))
            if connection is None:
                raise SetupError("connector status changed while it was being read")
            profile = get_profile_for_connection(
                connection.provider,
                connection.source_ids,
                connection.scopes,
            )
            projected.append(
                {
                    **raw,
                    "access": profile.access_for_scopes(connection.scopes).value,
                    "connector": connection.source_ids[0]
                    if len(connection.source_ids) == 1
                    else "legacy_provider_bundle",
                }
            )
        return {**status, "connections": projected}

    def status(self, connector_or_connection_id: str | None = None) -> dict[str, object]:
        """Return all connections or one exact logical source/connection selection."""

        status = self.list()
        if connector_or_connection_id is None:
            return status
        if not isinstance(connector_or_connection_id, str) or not connector_or_connection_id:
            raise ValidationError("connector status target is invalid")
        rows = cast(list[dict[str, object]], status["connections"])
        selected = [
            row
            for row in rows
            if row.get("connection_id") == connector_or_connection_id
            or row.get("connector") == connector_or_connection_id
        ]
        if not selected:
            raise ValidationError("connector status target was not found")
        return {**status, "connections": selected}

    def disconnect(self, connection_id: str) -> dict[str, object]:
        """Forget one local credential and record; this does not claim provider revocation."""

        clean_id = parse_connection_id(connection_id)
        snapshot = self.manager.vault.get_connection_snapshot()
        if snapshot.connection(clean_id) is None:
            raise ValidationError("connector connection was not found")
        result = self.manager.remove(clean_id, expected_revision=snapshot.revision)
        return {
            **result,
            "connection_id": str(clean_id),
            "provider_access_revoked": False,
            "status": "disconnected_locally",
        }

    def _confirm_and_publish(
        self,
        profile: ConnectorProfile,
        access: ConnectorAccessTier,
        pending: ConnectionMetadata,
        identity: ConnectorIdentity,
        *,
        confirm_identity: ConfirmIdentity,
        new_account: bool,
        publish: Callable[[ConnectionMetadata], Mapping[str, object]],
    ) -> dict[str, object]:
        if identity.provider != profile.provider:
            raise ValidationError("verified connector identity belongs to the wrong provider")
        if not callable(confirm_identity):
            raise ValidationError("connector identity confirmation is unavailable")
        existing = self._existing_connections(profile)
        same_account = [
            connection
            for connection in existing
            if connection.account.fingerprint == identity.fingerprint
        ]
        if existing and not same_account and not new_account:
            raise ValidationError(
                "the verified account differs from the existing connection; rerun with "
                "--new-account to keep both accounts"
            )
        review = ConnectorIdentityReview(
            connector=profile.name,
            provider=profile.provider,
            access=access,
            display_label=identity.display_label,
        )
        if confirm_identity(review) is not True:
            return {
                "account": identity.display_label,
                "connector": profile.name,
                "nothing_saved": True,
                "status": "cancelled",
            }
        reusable = _equal_or_broader(same_account, access)
        if reusable is not None:
            return {
                "access": get_profile_for_connection(
                    reusable.provider,
                    reusable.source_ids,
                    reusable.scopes,
                )
                .access_for_scopes(reusable.scopes)
                .value,
                "account_label": reusable.account.label,
                "connection_id": str(reusable.connection_id),
                "connector": profile.name,
                "status": "already_connected",
            }
        now = datetime.now(UTC)
        metadata = ConnectionMetadata(
            connection_id=pending.connection_id,
            provider=pending.provider,
            source_ids=pending.source_ids,
            credential_kind=pending.credential_kind,
            account=AccountMetadata(
                fingerprint=identity.fingerprint,
                label=identity.portable_label,
            ),
            scopes=pending.scopes,
            client=pending.client,
            health=ConnectionHealth.UNVERIFIED,
            created_at=now,
            updated_at=now,
            version=1,
        )
        publish(metadata)
        result: dict[str, object] = {
            "access": access.value,
            "account_label": identity.portable_label,
            "connection_id": str(metadata.connection_id),
            "connector": profile.name,
            "status": "connected",
        }
        prior_read = _read_upgrade_target(same_account, access)
        if prior_read is not None:
            try:
                snapshot = self.manager.vault.get_connection_snapshot()
                self.manager.remove(
                    prior_read.connection_id,
                    expected_revision=snapshot.revision,
                )
            except (ContinuityError, OSError) as exc:
                result["upgrade_cleanup"] = "old_read_connection_retained"
                result["warning"] = (
                    "Full access is ready, but the previous Read-only connection could not be "
                    f"removed safely ({type(exc).__name__}); both remain visible."
                )
            else:
                result["replaced_connection_id"] = str(prior_read.connection_id)
        return result

    def _existing_connections(self, profile: ConnectorProfile) -> Sequence[ConnectionMetadata]:
        snapshot = self.manager.vault.get_connection_snapshot()
        return [
            connection
            for connection in snapshot.connections
            if connection.provider == profile.provider
            and connection.source_ids == profile.source_ids
            and connection.health in _CONNECTED_HEALTH
        ]


def _pending_oauth_metadata(
    profile: ConnectorProfile,
    access: ConnectorAccessTier,
    registration: PublicClientRegistration,
) -> ConnectionMetadata:
    if registration.provider != profile.provider:
        raise ValidationError("public client registration belongs to the wrong provider")
    if profile.authorization_endpoint is None or profile.token_endpoint is None:
        raise ValidationError("connector OAuth endpoints are unavailable")
    scopes = profile.scopes_for(
        access,
        include_supplemental=(profile.name == "gmail" and access is ConnectorAccessTier.FULL),
    )
    now = datetime.now(UTC)
    return ConnectionMetadata(
        connection_id=new_connection_id(),
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier=registration.client_id,
            redirect_uris=(registration.redirect_template,),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _pending_bot_metadata(profile: ConnectorProfile) -> ConnectionMetadata:
    now = datetime.now(UTC)
    return ConnectionMetadata(
        connection_id=new_connection_id(),
        provider=profile.provider,
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(),
        scopes=profile.scopes_for(ConnectorAccessTier.FULL),
        client=ClientMetadata(kind=ClientKind.EXTERNAL),
        health=ConnectionHealth.UNVERIFIED,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _access(value: ConnectorAccessTier | str) -> ConnectorAccessTier:
    try:
        return ConnectorAccessTier(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("connector access must be read or full") from exc


def _equal_or_broader(
    connections: Sequence[ConnectionMetadata],
    requested: ConnectorAccessTier,
) -> ConnectionMetadata | None:
    for connection in sorted(connections, key=lambda item: item.updated_at, reverse=True):
        current = get_profile_for_connection(
            connection.provider,
            connection.source_ids,
            connection.scopes,
        ).access_for_scopes(connection.scopes)
        if current is ConnectorAccessTier.FULL or current is requested:
            return connection
    return None


def _read_upgrade_target(
    connections: Sequence[ConnectionMetadata],
    requested: ConnectorAccessTier,
) -> ConnectionMetadata | None:
    if requested is not ConnectorAccessTier.FULL:
        return None
    for connection in sorted(connections, key=lambda item: item.updated_at, reverse=True):
        current = get_profile_for_connection(
            connection.provider,
            connection.source_ids,
            connection.scopes,
        ).access_for_scopes(connection.scopes)
        if current is ConnectorAccessTier.READ:
            return connection
    return None
