"""DAU-oriented connector onboarding without ambient host credentials."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast

from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager, CustodyStatus
from continuity_kernel.connector_client_registration import (
    PublicClientRegistration,
    load_public_client_registration,
)
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import (
    ConnectionId,
    new_connection_id,
    parse_connection_id,
)
from continuity_kernel.connector_identity import ConnectorIdentity, verify_identity
from continuity_kernel.connector_oauth import OAuthAuthorizationRejectedError
from continuity_kernel.connector_profiles import (
    CONNECTOR_PROFILES,
    ConnectorAccessTier,
    ConnectorProfile,
    connector_browser_options,
    format_connector_command,
    get_connector_profile,
    get_profile_for_connection,
    validate_connector_alias,
)
from continuity_kernel.connector_profiles import (
    connector_connect_command as _connect_command,
)
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
)
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    OAuthPermissionGrantError,
    SetupError,
    ValidationError,
    mark_provider_authorization_may_remain,
)

ConfirmIdentity = Callable[["ConnectorIdentityReview"], bool]
IdentityVerifier = Callable[[str, ConnectorCredential], ConnectorIdentity]
RegistrationLoader = Callable[[str], PublicClientRegistration]
BrowserOpener = Callable[[str], bool]
AuthorizationPresenter = Callable[[str, bool], None]
PermissionUpdatePresenter = Callable[[str], None]
BrowserMode = Literal["default", "firefox", "manual"]

_DEFAULT_BROWSER: Final = object()
_CONNECTED_HEALTH: Final = frozenset({ConnectionHealth.READY, ConnectionHealth.DEGRADED})
_REAUTH_HEALTH: Final = _CONNECTED_HEALTH | frozenset({ConnectionHealth.REAUTHORIZATION_REQUIRED})
_CANDIDATE_HEALTH: Final = _REAUTH_HEALTH | frozenset({ConnectionHealth.UNVERIFIED})
_RETAINED_BROADER_CREDENTIAL: Final = "existing_broader_grant_retained"
_GMAIL_SETTINGS_BASIC: Final = "https://www.googleapis.com/auth/gmail.settings.basic"


@dataclass(frozen=True, repr=False)
class ConnectorIdentityReview:
    """Transient provider identity shown before anything is persisted."""

    connector: str
    provider: str
    access: ConnectorAccessTier
    display_label: str
    permanent_delete: bool = False
    gmail_settings_control: bool = False
    permission_update: str | None = None

    def __repr__(self) -> str:
        return (
            "ConnectorIdentityReview("
            f"connector={self.connector!r}, provider={self.provider!r}, "
            f"access={self.access!r}, permanent_delete={self.permanent_delete!r}, "
            f"gmail_settings_control={self.gmail_settings_control!r}, "
            f"permission_update={self.permission_update is not None!r}, "
            "display_label=<redacted>)"
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
        connection_id: str | None = None,
        alias: str | None = None,
        include_permanent_delete: bool = False,
        timeout_seconds: float | None = None,
        browser_opener: BrowserOpener | None | object = _DEFAULT_BROWSER,
        browser_mode: BrowserMode | None = None,
        present_authorization_url: AuthorizationPresenter | None = None,
        present_permission_update: PermissionUpdatePresenter | None = None,
    ) -> dict[str, object]:
        """Connect one OAuth source, keeping every credential transient until confirmation."""

        if not callable(confirm_identity):
            raise ValidationError("connector identity confirmation is unavailable")
        if present_permission_update is not None and not callable(present_permission_update):
            raise ValidationError("connector permission update presenter is invalid")
        profile = get_connector_profile(connector)
        if profile.credential_kind is not CredentialKind.OAUTH2:
            raise ValidationError("this connector uses bot onboarding, not OAuth")
        tier = _access(access)
        if include_permanent_delete and (
            profile.name != "gmail" or tier is not ConnectorAccessTier.FULL
        ):
            raise ValidationError("--with-permanent-delete is available only for Gmail Full access")
        requested_scopes = profile.scopes_for(
            tier,
            include_supplemental=include_permanent_delete,
        )
        if connection_id is not None and new_account:
            raise ValidationError("--connection-id cannot be combined with --new-account")
        validated_alias = _alias(alias)
        validated_browser_mode = _browser_mode(browser_mode)
        clean_connection_id = None if connection_id is None else parse_connection_id(connection_id)
        effective_timeout_seconds = 180.0 if timeout_seconds is None else timeout_seconds

        candidate: ConnectionMetadata | None = None
        pinned_fingerprint: str | None = None
        preferred_label: str | None = None
        permission_update: str | None = None
        force_fresh = False
        if not new_account:
            candidate, early_result, reauthorize = self._preflight_oauth(
                profile,
                tier,
                clean_connection_id,
                requested_scopes=requested_scopes,
                include_permanent_delete=include_permanent_delete,
                alias=validated_alias,
                browser_mode=validated_browser_mode,
                timeout_seconds=timeout_seconds,
            )
            if early_result is not None:
                return early_result
            if (
                candidate is not None
                and tier is ConnectorAccessTier.FULL
                and _permanent_delete_enabled(profile, candidate.scopes)
            ):
                # Scope evolution must not silently discard a stronger, separately chosen
                # Gmail grant. Retain it while adding the new base Full permissions.
                include_permanent_delete = True
                requested_scopes = profile.scopes_for(tier, include_supplemental=True)
            if candidate is not None and reauthorize:
                return self.reauthorize_oauth(
                    str(candidate.connection_id),
                    confirm_identity=confirm_identity,
                    alias=validated_alias,
                    timeout_seconds=timeout_seconds,
                    browser_opener=browser_opener,
                    browser_mode=validated_browser_mode,
                    present_authorization_url=present_authorization_url,
                )
            if candidate is not None:
                pinned_fingerprint = candidate.account.fingerprint
                preferred_label = candidate.account.label
                force_fresh = pinned_fingerprint is not None
                if not _scopes_satisfy_requirement(
                    profile,
                    candidate.scopes,
                    requested_scopes,
                ):
                    permission_update = _permission_update(
                        profile,
                        candidate.scopes,
                        tier,
                        include_permanent_delete=include_permanent_delete,
                    )

        existing_connections = tuple(self._existing_connections(profile))
        preserved_connections = existing_connections
        if candidate is not None and candidate.account.fingerprint is not None:
            preserved_connections = tuple(
                connection
                for connection in existing_connections
                if connection.account.fingerprint == candidate.account.fingerprint
            )
        existing_effective_access, existing_permanent_delete = _preserved_authority(
            profile,
            preserved_connections,
        )
        if permission_update is not None and present_permission_update is not None:
            present_permission_update(permission_update)

        self.manager.probe_credential_custody()
        registration = self.registration_loader(profile.provider)
        pending = _pending_oauth_metadata(
            profile,
            tier,
            registration,
            include_permanent_delete=include_permanent_delete,
        )
        retry_connection_id = clean_connection_id
        if retry_connection_id is None and candidate is not None:
            retry_connection_id = candidate.connection_id
        retry_command = _connect_command(
            profile,
            pending.scopes,
            new_account=new_account,
            connection_id=(str(retry_connection_id) if retry_connection_id is not None else None),
            alias=validated_alias,
            browser_mode=validated_browser_mode,
            timeout_seconds=timeout_seconds,
        )
        try:
            credential = self._acquire_oauth_credential(
                pending,
                timeout_seconds=effective_timeout_seconds,
                browser_opener=browser_opener,
                present_authorization_url=present_authorization_url,
            )
        except OAuthPermissionGrantError as exc:
            return _oauth_permission_failure_result(
                profile,
                tier,
                pending.scopes,
                exc,
                retry_command=retry_command,
                connection_id=(
                    str(retry_connection_id) if retry_connection_id is not None else None
                ),
                existing_connection_preserved=bool(preserved_connections),
                existing_effective_access=existing_effective_access,
                existing_permanent_delete=existing_permanent_delete,
            )
        except OAuthAuthorizationRejectedError:
            return _oauth_authorization_rejected_result(
                profile,
                tier,
                pending.scopes,
                retry_command=retry_command,
                connection_id=(
                    str(retry_connection_id) if retry_connection_id is not None else None
                ),
                existing_connection_preserved=bool(preserved_connections),
                existing_effective_access=existing_effective_access,
                existing_permanent_delete=existing_permanent_delete,
            )
        with _provider_authorization_phase():
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
                alias=validated_alias,
                requested_capability=_requested_capability_rank(
                    profile,
                    tier,
                    include_permanent_delete=include_permanent_delete,
                ),
                pinned_fingerprint=pinned_fingerprint,
                preferred_label=preferred_label,
                permission_update=permission_update,
                force_fresh=force_fresh,
                retry_command=retry_command,
                new_account_command=_connect_command(
                    profile,
                    pending.scopes,
                    new_account=True,
                    browser_mode=validated_browser_mode,
                    timeout_seconds=timeout_seconds,
                ),
                publish=lambda metadata: self.manager.publish_new_oauth_connection(
                    metadata,
                    credential,
                ),
                reuse=lambda metadata, account_label: self._reuse_oauth_connection(
                    metadata,
                    credential,
                    identity_fingerprint=identity.fingerprint,
                    account_label=account_label,
                ),
            )

    def reauthorize_oauth(
        self,
        connection_id: str,
        *,
        confirm_identity: ConfirmIdentity,
        alias: str | None = None,
        timeout_seconds: float | None = None,
        browser_opener: BrowserOpener | None | object = _DEFAULT_BROWSER,
        browser_mode: BrowserMode | None = None,
        present_authorization_url: AuthorizationPresenter | None = None,
    ) -> dict[str, object]:
        """Replace a verified OAuth credential while keeping its connection ID."""

        clean_id = parse_connection_id(connection_id)
        if not callable(confirm_identity):
            raise ValidationError("connector identity confirmation is unavailable")
        validated_alias = _alias(alias)
        validated_browser_mode = _browser_mode(browser_mode)
        effective_timeout_seconds = 180.0 if timeout_seconds is None else timeout_seconds
        snapshot = self.manager.vault.get_connection_snapshot()
        connection = snapshot.connection(clean_id)
        if connection is None:
            raise ValidationError("connector connection was not found")
        if connection.credential_kind is not CredentialKind.OAUTH2:
            raise ValidationError("connection does not use OAuth")
        if connection.health not in _REAUTH_HEALTH:
            raise ValidationError("credential reauthorization requires a verified connection")
        if connection.account.fingerprint is None:
            raise ValidationError("credential reauthorization requires a bound account")
        profile = get_profile_for_connection(
            connection.provider,
            connection.source_ids,
            connection.scopes,
        )
        access = profile.access_for_scopes(connection.scopes)
        custody_status = self.manager.inspect_custody(connection)
        if custody_status == "pointer_invalid":
            return _credential_recovery_result(profile, connection, custody_status)
        self.manager.probe_credential_custody()
        retry_command = _reauthorize_command(
            clean_id,
            alias=validated_alias,
            browser_mode=validated_browser_mode,
            timeout_seconds=timeout_seconds,
        )
        try:
            credential = self._acquire_oauth_credential(
                connection,
                timeout_seconds=effective_timeout_seconds,
                browser_opener=browser_opener,
                present_authorization_url=present_authorization_url,
            )
        except OAuthPermissionGrantError as exc:
            return _oauth_permission_failure_result(
                profile,
                access,
                connection.scopes,
                exc,
                retry_command=retry_command,
                connection_id=str(clean_id),
                existing_connection_preserved=True,
                existing_effective_access=access.value,
                existing_permanent_delete=_permanent_delete_enabled(
                    profile,
                    connection.scopes,
                ),
            )
        except OAuthAuthorizationRejectedError:
            return _oauth_authorization_rejected_result(
                profile,
                access,
                connection.scopes,
                retry_command=retry_command,
                connection_id=str(clean_id),
                existing_connection_preserved=True,
                existing_effective_access=access.value,
                existing_permanent_delete=_permanent_delete_enabled(
                    profile,
                    connection.scopes,
                ),
            )
        with _provider_authorization_phase():
            identity = self.identity_verifier(
                profile.provider,
                ConnectorCredential(
                    scheme=AuthorizationScheme.BEARER,
                    secret=credential.access_token,
                ),
            )
            if identity.provider != profile.provider:
                return _different_account_result(
                    profile,
                    connection.scopes,
                    connection_id=str(clean_id),
                    retry_command=retry_command,
                    new_account_command=_connect_command(
                        profile,
                        connection.scopes,
                        new_account=True,
                        browser_mode=validated_browser_mode,
                        timeout_seconds=timeout_seconds,
                    ),
                    provider_authorization=True,
                    existing_connection_preserved=True,
                )
            review = ConnectorIdentityReview(
                connector=profile.name,
                provider=profile.provider,
                access=access,
                display_label=identity.display_label,
                permanent_delete=_permanent_delete_enabled(profile, connection.scopes),
                gmail_settings_control=_gmail_settings_control(
                    profile,
                    connection.scopes,
                ),
            )
            if confirm_identity(review) is not True:
                return _cancelled_identity_result(
                    profile,
                    connection_id=str(clean_id),
                    next_command=_status_command(clean_id),
                    retry_command=retry_command,
                    provider_authorization=True,
                    existing_connection_preserved=True,
                )
            if identity.fingerprint != connection.account.fingerprint:
                return _different_account_result(
                    profile,
                    connection.scopes,
                    connection_id=str(clean_id),
                    retry_command=retry_command,
                    new_account_command=_connect_command(
                        profile,
                        connection.scopes,
                        new_account=True,
                        browser_mode=validated_browser_mode,
                        timeout_seconds=timeout_seconds,
                    ),
                    provider_authorization=True,
                    existing_connection_preserved=True,
                )

            current_snapshot = self.manager.vault.get_connection_snapshot()
            current = current_snapshot.connection(clean_id)
            if current is None or not _same_credential_binding(connection, current):
                raise ConflictError("connection changed during OAuth reauthorization")
            if current.health not in _REAUTH_HEALTH or current.account.fingerprint is None:
                raise ConflictError("connection changed during OAuth reauthorization")
            account_label = validated_alias or current.account.label or identity.portable_label
            token_state = self.manager.tokens.state(clean_id)
            expected_token_version = token_state.version if token_state is not None else 0
            try:
                self.manager.rotate_verified_oauth_credential(
                    clean_id,
                    expected_revision=current_snapshot.revision,
                    expected_account_fingerprint=identity.fingerprint,
                    expected_token_version=expected_token_version,
                    replacement=credential,
                    account_label=account_label,
                )
            except OAuthPermissionGrantError as exc:
                return _oauth_permission_failure_result(
                    profile,
                    access,
                    connection.scopes,
                    exc,
                    retry_command=retry_command,
                    connection_id=str(clean_id),
                    existing_connection_preserved=True,
                    existing_effective_access=access.value,
                    existing_permanent_delete=_permanent_delete_enabled(
                        profile,
                        connection.scopes,
                    ),
                )
            return {
                **_connection_success_result(
                    profile,
                    current,
                    status="connected",
                    health=ConnectionHealth.READY,
                    account_label=account_label,
                ),
                "credential": "replaced",
            }

    def _preflight_oauth(
        self,
        profile: ConnectorProfile,
        access: ConnectorAccessTier,
        connection_id: object,
        *,
        requested_scopes: tuple[str, ...],
        include_permanent_delete: bool,
        alias: str | None,
        browser_mode: BrowserMode | None,
        timeout_seconds: float | None,
    ) -> tuple[ConnectionMetadata | None, dict[str, object] | None, bool]:
        snapshot = self.manager.vault.get_connection_snapshot()
        candidates = self._logical_candidates(profile, snapshot.connections)
        requested_rank = _requested_capability_rank(
            profile,
            access,
            include_permanent_delete=include_permanent_delete,
        )
        exact_selection = connection_id is not None
        if exact_selection:
            selected = next(
                (candidate for candidate in candidates if candidate.connection_id == connection_id),
                None,
            )
            if selected is None:
                raise ValidationError("connection ID does not identify this logical connector")
            if _try_capability_rank(profile, selected.scopes) is None:
                return None, _scope_profile_recovery_result(profile, selected), False
        else:
            bound_fingerprints = {
                candidate.account.fingerprint
                for candidate in candidates
                if candidate.account.fingerprint is not None
            }
            if len(bound_fingerprints) > 1:
                return (
                    None,
                    _account_selection_required(
                        profile,
                        candidates,
                        access,
                        include_permanent_delete=include_permanent_delete,
                        alias=alias,
                        browser_mode=browser_mode,
                        timeout_seconds=timeout_seconds,
                    ),
                    False,
                )
            usable = self._valid_sufficient_oauth_connection(
                profile,
                candidates,
                requested_rank,
                requested_scopes,
            )
            if usable is not None:
                if _capability_rank(profile, usable.scopes) > requested_rank:
                    return (
                        None,
                        _broader_access_result(
                            profile,
                            usable,
                            selected_access=access,
                            requested_scopes=requested_scopes,
                        ),
                        False,
                    )
                return (
                    None,
                    {
                        **_connection_success_result(
                            profile,
                            usable,
                            status="already_connected",
                        ),
                        "credential": "unchanged",
                    },
                    False,
                )
            selected = _select_preflight_candidate(
                profile,
                candidates,
                requested_rank,
                requested_scopes,
            )
        if selected is None:
            return None, None, False

        if selected.health is ConnectionHealth.UNVERIFIED:
            custody_status = self.manager.inspect_custody(selected)
            if custody_status == "valid":
                return (
                    None,
                    _setup_incomplete_result(profile, selected),
                    False,
                )
            return (
                None,
                _credential_recovery_result(profile, selected, custody_status),
                False,
            )

        if selected.account.fingerprint is None:
            return None, _identity_binding_recovery_result(profile, selected), False
        scopes_sufficient = _scopes_satisfy_requirement(
            profile,
            selected.scopes,
            requested_scopes,
        )
        if _capability_rank(profile, selected.scopes) > requested_rank and scopes_sufficient:
            custody_status = self.manager.inspect_custody(selected)
            if custody_status != "valid":
                return (
                    None,
                    _credential_recovery_result(profile, selected, custody_status),
                    False,
                )
            return (
                None,
                _broader_access_result(
                    profile,
                    selected,
                    selected_access=access,
                    requested_scopes=requested_scopes,
                ),
                False,
            )
        if not scopes_sufficient:
            return selected, None, False
        if selected.health is ConnectionHealth.REAUTHORIZATION_REQUIRED:
            return selected, None, True
        if selected.health not in _CONNECTED_HEALTH:
            raise ValidationError("connector health is not eligible for OAuth preflight")
        if not exact_selection:
            return selected, None, True

        custody_status = self.manager.inspect_custody(selected)
        if custody_status == "valid":
            return (
                None,
                {
                    **_connection_success_result(
                        profile,
                        selected,
                        status="already_connected",
                    ),
                    "credential": "unchanged",
                },
                False,
            )
        if custody_status == "pointer_invalid":
            return (
                None,
                _credential_recovery_result(profile, selected, custody_status),
                False,
            )
        return selected, None, True

    def _valid_sufficient_oauth_connection(
        self,
        profile: ConnectorProfile,
        candidates: Sequence[ConnectionMetadata],
        requested_capability: int,
        requested_scopes: tuple[str, ...],
    ) -> ConnectionMetadata | None:
        for candidate in sorted(
            candidates,
            key=lambda item: _preflight_candidate_key(
                profile,
                item,
                requested_capability,
                requested_scopes,
            ),
            reverse=True,
        ):
            capability = _try_capability_rank(profile, candidate.scopes)
            if (
                capability is not None
                and candidate.health in _CONNECTED_HEALTH
                and candidate.account.fingerprint is not None
                and _scopes_satisfy_requirement(
                    profile,
                    candidate.scopes,
                    requested_scopes,
                )
                and self.manager.inspect_custody(candidate) == "valid"
            ):
                return candidate
        return None

    def _acquire_oauth_credential(
        self,
        metadata: ConnectionMetadata,
        *,
        timeout_seconds: float,
        browser_opener: BrowserOpener | None | object,
        present_authorization_url: AuthorizationPresenter | None,
    ) -> OAuthCredential:
        if browser_opener is _DEFAULT_BROWSER:
            return self.manager.acquire_oauth_credential(
                metadata,
                timeout_seconds=timeout_seconds,
                present_authorization_url=present_authorization_url,
            )
        return self.manager.acquire_oauth_credential(
            metadata,
            timeout_seconds=timeout_seconds,
            browser_opener=cast(BrowserOpener | None, browser_opener),
            present_authorization_url=present_authorization_url,
        )

    @staticmethod
    def _logical_candidates(
        profile: ConnectorProfile,
        connections: Sequence[ConnectionMetadata],
    ) -> list[ConnectionMetadata]:
        return [
            connection
            for connection in connections
            if connection.provider == profile.provider
            and connection.source_ids == profile.source_ids
            and connection.credential_kind is profile.credential_kind
            and connection.health in _CANDIDATE_HEALTH
        ]

    def connect_discord(
        self,
        token: bytes,
        *,
        access: ConnectorAccessTier | str,
        confirm_identity: ConfirmIdentity,
        new_account: bool = False,
        alias: str | None = None,
    ) -> dict[str, object]:
        """Connect one Discord bot from hidden input; Discord user tokens are never accepted."""

        if not callable(confirm_identity):
            raise ValidationError("connector identity confirmation is unavailable")
        profile = get_connector_profile("discord")
        tier = _access(access)
        if tier is not ConnectorAccessTier.FULL:
            raise ValidationError("Discord bot connections require Full access")
        validated_alias = _alias(alias)
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
        self.manager.probe_credential_custody()
        identity = self.identity_verifier("discord", runtime_credential)
        pending = _pending_bot_metadata(profile)
        result = self._confirm_and_publish(
            profile,
            tier,
            pending,
            identity,
            confirm_identity=confirm_identity,
            new_account=new_account,
            alias=validated_alias,
            requested_capability=1,
            pinned_fingerprint=None,
            preferred_label=None,
            permission_update=None,
            force_fresh=False,
            retry_command=_connect_command(profile, pending.scopes),
            new_account_command=_connect_command(
                profile,
                pending.scopes,
                new_account=True,
            ),
            publish=lambda metadata: self.manager.publish_new_bearer_connection(
                metadata,
                token,
            ),
            reuse=lambda metadata, account_label: self._reuse_bearer_connection(
                metadata,
                token,
                identity_fingerprint=identity.fingerprint,
                account_label=account_label,
            ),
        )
        connection_id = result.get("connection_id")
        if result.get("status") in {"connected", "already_connected"} and isinstance(
            connection_id, str
        ):
            result["source_status"] = "source_setup_required"
            result["next"] = "gsv discord-source binding-status"
            result["source_setup"] = {
                "available_now": "typed interactive Discord CRUD through gsv_connectors",
                "blocker": (
                    "This build does not package or recommend a Discord Pulse companion. "
                    "Leave the Discord Pulse source unselected until an exact runtime has "
                    "been independently audited."
                ),
                "checklist": [
                    "Install an independently audited discord-mcp runtime that implements "
                    "Seld's exact three-tool read-source contract.",
                    "Set DISCORD_CHANNEL_IDS privately to the exact comma-separated channel "
                    "allowlist.",
                    "Run gsv discord-source binding-status and retain its bindingRevision.",
                    "Bind the audited absolute runtime path, this connection ID, and that exact "
                    "bindingRevision with gsv discord-source bind.",
                    "Run gsv discord-source status before selecting Discord for Pulse.",
                ],
                "connection_id": connection_id,
            }
        return result

    def list(self) -> dict[str, object]:
        """Return redacted connections with their logical access tier."""

        status = self.manager.status()
        rows = status.get("connections")
        if not isinstance(rows, list):
            raise SetupError("connector status is invalid")
        snapshot = self.manager.vault.get_connection_snapshot()
        if status.get("revision") != snapshot.revision:
            raise ConflictError("connector status changed while it was being read")
        projected: list[dict[str, object]] = []
        for raw in rows:
            if not isinstance(raw, dict) or not isinstance(raw.get("connection_id"), str):
                raise SetupError("connector status is invalid")
            connection = snapshot.connection(cast(str, raw["connection_id"]))
            if connection is None:
                raise SetupError("connector status changed while it was being read")
            try:
                profile = get_profile_for_connection(
                    connection.provider,
                    connection.source_ids,
                    connection.scopes,
                )
                access = profile.access_for_scopes(connection.scopes).value
            except ValidationError:
                try:
                    recovery_profile = get_profile_for_connection(
                        connection.provider,
                        connection.source_ids,
                    )
                except ValidationError:
                    recovery_profile = None
                connector = (
                    connection.source_ids[0]
                    if len(connection.source_ids) == 1
                    else "legacy_provider_bundle"
                )
                projected.append(
                    {
                        **raw,
                        "access": "unknown",
                        "connector": connector,
                        "error_code": "oauth_scope_profile_unrecognized",
                        "next": format_connector_command(
                            (
                                "gsv",
                                "connectors",
                                "disconnect",
                                str(connection.connection_id),
                            )
                        ),
                        "permanent_delete": bool(
                            recovery_profile is not None
                            and _permanent_delete_enabled(
                                recovery_profile,
                                connection.scopes,
                            )
                        ),
                    }
                )
                continue
            projected.append(
                {
                    **raw,
                    "access": access,
                    "connector": connection.source_ids[0]
                    if len(connection.source_ids) == 1
                    else "legacy_provider_bundle",
                    "permanent_delete": _permanent_delete_enabled(profile, connection.scopes),
                    **_gmail_settings_status(profile, connection, access=access),
                }
            )
        readiness = self.registration_readiness()
        catalog: list[dict[str, object]] = []
        for connector, profile in sorted(CONNECTOR_PROFILES.items()):
            configured_rows = [row for row in projected if row.get("connector") == connector]
            connected = sum(1 for row in configured_rows if "next" not in row)
            needs_attention = len(configured_rows) - connected
            registration = (
                readiness[profile.provider]
                if profile.credential_kind is CredentialKind.OAUTH2
                else {"status": "external_bot", "sign_in": "user_setup_required"}
            )
            connector_status = "not_connected"
            if connected and needs_attention:
                connector_status = "connected_with_attention"
            elif connected:
                connector_status = "connected"
            elif needs_attention:
                connector_status = "needs_attention"
            catalog.append(
                {
                    "connected_accounts": connected,
                    "configured_accounts": len(configured_rows),
                    "connector": connector,
                    "credential_kind": profile.credential_kind.value,
                    "needs_attention_accounts": needs_attention,
                    "registration": registration,
                    "status": connector_status,
                }
            )
        return {
            **status,
            "connections": projected,
            "connector_catalog": catalog,
            "registration_readiness": readiness,
        }

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
            if connector_or_connection_id in CONNECTOR_PROFILES:
                profile = get_connector_profile(connector_or_connection_id)
                readiness = status.get("registration_readiness")
                registration = (
                    readiness.get(profile.provider) if isinstance(readiness, dict) else None
                )
                return {
                    **status,
                    "connections": [],
                    "connector": connector_or_connection_id,
                    "connect_commands": [
                        _connect_command(
                            profile,
                            (),
                            access=access,
                        )
                        for access in (
                            (ConnectorAccessTier.FULL,)
                            if profile.name == "discord"
                            else (ConnectorAccessTier.READ, ConnectorAccessTier.FULL)
                        )
                    ],
                    "registration": registration,
                    "status": "not_connected",
                }
            raise ValidationError("connector status target was not found")
        return {**status, "connections": selected}

    def disconnect(self, connection_id: str) -> dict[str, object]:
        """Forget one local credential and record; this does not claim provider revocation."""

        clean_id = parse_connection_id(connection_id)
        snapshot = self.manager.vault.get_connection_snapshot()
        connection = snapshot.connection(clean_id)
        if connection is None:
            raise ValidationError("connector connection was not found")
        result = self.manager.remove(clean_id, expected_revision=snapshot.revision)
        return {
            **result,
            "connection_id": str(clean_id),
            "next": provider_revocation_guidance(connection.provider),
            "provider_access_revoked": False,
            "status": "disconnected_locally",
        }

    def alias(
        self,
        connection_id: str,
        alias: str,
        *,
        expected_revision: str | None = None,
    ) -> dict[str, object]:
        """Repair only a local account label through an exact metadata CAS write."""

        clean_id = parse_connection_id(connection_id)
        validated_alias = _alias(alias)
        if validated_alias is None:
            raise ValidationError("connector alias is invalid")
        snapshot = self.manager.vault.get_connection_snapshot()
        connection = snapshot.connection(clean_id)
        if connection is None:
            raise ValidationError("connector connection was not found")
        updated = self.manager.update_connection_alias(
            clean_id,
            validated_alias,
            expected_revision=(
                snapshot.revision if expected_revision is None else expected_revision
            ),
        )
        revision = updated.get("revision")
        if not isinstance(revision, str):
            raise SetupError("connector alias update has no usable revision")
        return {
            "account_label": validated_alias,
            "connection_id": str(clean_id),
            "credential": "unchanged",
            "metadata": "label_only",
            "revision": revision,
            "status": "alias_updated",
            "token_custody": "unchanged",
        }

    def resume(
        self,
        connection_id: str,
        *,
        confirm_identity: ConfirmIdentity,
        alias: str | None = None,
    ) -> dict[str, object]:
        """Finish identity confirmation for one retained unverified credential."""

        if not callable(confirm_identity):
            raise ValidationError("connector identity confirmation is unavailable")
        clean_id = parse_connection_id(connection_id)
        validated_alias = _alias(alias)
        snapshot = self.manager.vault.get_connection_snapshot()
        connection = snapshot.connection(clean_id)
        if connection is None:
            raise ValidationError("connector connection was not found")
        if connection.health is not ConnectionHealth.UNVERIFIED:
            raise ValidationError("only an unverified connection can resume setup")
        token_state = self.manager.tokens.state(clean_id)
        if token_state is None:
            removed = self.manager.remove(clean_id, expected_revision=snapshot.revision)
            profile = get_profile_for_connection(
                connection.provider,
                connection.source_ids,
                connection.scopes,
            )
            return {
                **removed,
                "connection_id": str(clean_id),
                "next": _connect_command(profile, connection.scopes),
                "nothing_saved": True,
                "status": "credential_missing_reconnect_required",
            }
        if connection.credential_kind is CredentialKind.OAUTH2:
            resolved = self.manager.resolve_oauth_access_token_state(
                clean_id,
                expected_connection_revision=snapshot.revision,
                require_verified_identity=False,
            )
            credential = ConnectorCredential(
                scheme=AuthorizationScheme.BEARER,
                secret=resolved.access_token,
            )
            token_version = resolved.state.version
        elif connection.credential_kind is CredentialKind.BEARER:
            resolved_bearer = self.manager.resolve_credential_state(
                clean_id,
                require_verified_identity=False,
            )
            try:
                secret = resolved_bearer.value.decode("utf-8")
            except UnicodeError as exc:
                raise ValidationError("bot credential is invalid") from exc
            credential = ConnectorCredential(
                scheme=AuthorizationScheme.BOT,
                secret=secret,
            )
            token_version = resolved_bearer.state.version
        else:  # pragma: no cover - closed enum defense
            raise ValidationError("connector credential kind is unsupported")
        identity = self.identity_verifier(connection.provider, credential)
        profile = get_profile_for_connection(
            connection.provider,
            connection.source_ids,
            connection.scopes,
        )
        access = profile.access_for_scopes(connection.scopes)
        review = ConnectorIdentityReview(
            connector=profile.name,
            provider=profile.provider,
            access=access,
            display_label=identity.display_label,
            permanent_delete=_permanent_delete_enabled(profile, connection.scopes),
            gmail_settings_control=_gmail_settings_control(profile, connection.scopes),
        )
        if confirm_identity(review) is not True:
            return _cancelled_identity_result(
                profile,
                connection_id=str(clean_id),
                next_command=_resume_command(clean_id, alias=validated_alias),
                provider_authorization=False,
            )
        if connection.account.fingerprint is None:
            removed = self.manager.remove(clean_id, expected_revision=snapshot.revision)
            return {
                **removed,
                "connection_id": str(clean_id),
                "next": _connect_command(profile, connection.scopes),
                "nothing_saved": True,
                "status": "identity_binding_missing_reconnect_required",
            }
        if connection.account.fingerprint != identity.fingerprint:
            return _different_account_result(
                profile,
                connection.scopes,
                connection_id=str(clean_id),
                retry_command=_resume_command(clean_id, alias=validated_alias),
                new_account_command=_connect_command(
                    profile,
                    connection.scopes,
                    new_account=True,
                ),
                provider_authorization=False,
                existing_connection_preserved=True,
            )
        account_label = validated_alias or connection.account.label or identity.portable_label
        self.manager.verify_existing_connection_identity(
            clean_id,
            fingerprint=identity.fingerprint,
            label=account_label,
            expected_revision=snapshot.revision,
            expected_token_version=token_version,
        )
        return {
            "access": access.value,
            "account_label": account_label,
            "connection_id": str(clean_id),
            "connector": profile.name,
            "status": "connected",
        }

    def registration_readiness(self) -> dict[str, dict[str, str]]:
        """Report packaged OAuth registration readiness without exposing client IDs."""

        readiness: dict[str, dict[str, str]] = {}
        providers = sorted(
            {
                profile.provider
                for profile in CONNECTOR_PROFILES.values()
                if profile.credential_kind is CredentialKind.OAUTH2
            }
        )
        for provider in providers:
            try:
                self.registration_loader(provider)
            except SetupError as exc:
                readiness[provider] = {
                    "reason": str(exc),
                    "sign_in": "unavailable",
                    "status": "missing",
                }
            except ValidationError as exc:
                readiness[provider] = {
                    "reason": str(exc),
                    "sign_in": "unavailable",
                    "status": "invalid",
                }
            else:
                readiness[provider] = {"sign_in": "available", "status": "ready"}
        return readiness

    def _reuse_oauth_connection(
        self,
        connection: ConnectionMetadata,
        credential: OAuthCredential,
        *,
        identity_fingerprint: str,
        account_label: str,
    ) -> Mapping[str, object]:
        configured_scopes = connection.scopes
        if connection.provider == "microsoft":
            configured_scopes = tuple(
                scope for scope in configured_scopes if scope.casefold() != "offline_access"
            )
        if not set(configured_scopes).issubset(credential.scopes):
            return {
                "credential": _RETAINED_BROADER_CREDENTIAL,
                "warning": (
                    "The existing connection has broader access than this sign-in. Its current "
                    "credential was retained."
                ),
            }
        revision, token_version = self._credential_reuse_state(connection)
        self.manager.rotate_verified_oauth_credential(
            connection.connection_id,
            expected_revision=revision,
            expected_account_fingerprint=identity_fingerprint,
            expected_token_version=token_version,
            replacement=credential,
            account_label=account_label,
        )
        return {"credential": "refreshed"}

    def _reuse_bearer_connection(
        self,
        connection: ConnectionMetadata,
        credential: bytes,
        *,
        identity_fingerprint: str,
        account_label: str,
    ) -> Mapping[str, object]:
        revision, token_version = self._credential_reuse_state(connection)
        self.manager.rotate_verified_bearer_credential(
            connection.connection_id,
            expected_revision=revision,
            expected_account_fingerprint=identity_fingerprint,
            expected_token_version=token_version,
            replacement=credential,
            account_label=account_label,
        )
        return {"credential": "refreshed"}

    def _credential_reuse_state(self, connection: ConnectionMetadata) -> tuple[str, int]:
        snapshot = self.manager.vault.get_connection_snapshot()
        current = snapshot.connection(connection.connection_id)
        if current is None or not _same_credential_binding(connection, current):
            raise ConflictError("connection changed during credential reuse")
        token_state = self.manager.tokens.state(connection.connection_id)
        return snapshot.revision, token_state.version if token_state is not None else 0

    def _confirm_and_publish(
        self,
        profile: ConnectorProfile,
        access: ConnectorAccessTier,
        pending: ConnectionMetadata,
        identity: ConnectorIdentity,
        *,
        confirm_identity: ConfirmIdentity,
        new_account: bool,
        alias: str | None,
        requested_capability: int,
        pinned_fingerprint: str | None,
        preferred_label: str | None,
        permission_update: str | None,
        force_fresh: bool,
        retry_command: str | None,
        new_account_command: str | None,
        publish: Callable[[ConnectionMetadata], Mapping[str, object]],
        reuse: Callable[[ConnectionMetadata, str], Mapping[str, object]],
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
        incomplete = max(
            (
                connection
                for connection in same_account
                if connection.health is ConnectionHealth.UNVERIFIED
                and _try_capability_rank(profile, connection.scopes) is not None
            ),
            key=lambda connection: (connection.updated_at, str(connection.connection_id)),
            default=None,
        )
        if incomplete is not None:
            incomplete_custody_status = self.manager.inspect_custody(incomplete)
            if incomplete_custody_status != "valid":
                return _post_authorization_discard(
                    profile,
                    _credential_recovery_result(
                        profile,
                        incomplete,
                        incomplete_custody_status,
                    ),
                )
            return _post_authorization_discard(
                profile,
                _setup_incomplete_result(profile, incomplete),
            )
        review = ConnectorIdentityReview(
            connector=profile.name,
            provider=profile.provider,
            access=access,
            display_label=identity.display_label,
            permanent_delete=_permanent_delete_enabled(profile, pending.scopes),
            gmail_settings_control=_gmail_settings_control(profile, pending.scopes),
            permission_update=permission_update,
        )
        if confirm_identity(review) is not True:
            return _cancelled_identity_result(
                profile,
                next_command=retry_command or _connect_command(profile, pending.scopes),
                provider_authorization=profile.credential_kind is CredentialKind.OAUTH2,
                existing_connection_preserved=bool(existing),
            )
        if pinned_fingerprint is not None and identity.fingerprint != pinned_fingerprint:
            return _different_account_result(
                profile,
                pending.scopes,
                retry_command=retry_command,
                new_account_command=new_account_command,
                provider_authorization=profile.credential_kind is CredentialKind.OAUTH2,
                existing_connection_preserved=True,
            )
        if existing and not same_account and not new_account:
            return _different_account_result(
                profile,
                pending.scopes,
                retry_command=retry_command,
                new_account_command=new_account_command,
                provider_authorization=profile.credential_kind is CredentialKind.OAUTH2,
                existing_connection_preserved=True,
            )
        reusable, reusable_custody_status = (
            self._reusable_connection(
                profile,
                same_account,
                requested_capability,
                pending.scopes,
            )
            if not force_fresh
            else (None, None)
        )
        if reusable is not None and reusable_custody_status is not None:
            if reusable_custody_status == "pointer_invalid":
                return _post_authorization_discard(
                    profile,
                    _credential_recovery_result(
                        profile,
                        reusable,
                        reusable_custody_status,
                    ),
                )
            if _capability_rank(profile, reusable.scopes) > requested_capability:
                if reusable_custody_status != "valid":
                    return _post_authorization_discard(
                        profile,
                        _credential_recovery_result(
                            profile,
                            reusable,
                            reusable_custody_status,
                        ),
                    )
                return _post_authorization_discard(
                    profile,
                    _broader_access_result(
                        profile,
                        reusable,
                        selected_access=access,
                        requested_scopes=pending.scopes,
                    ),
                )
            account_label = _alias(alias) or reusable.account.label or identity.portable_label
            try:
                reused = reuse(reusable, account_label)
            except OAuthPermissionGrantError as exc:
                return _oauth_permission_failure_result(
                    profile,
                    access,
                    pending.scopes,
                    exc,
                    retry_command=retry_command or _connect_command(profile, pending.scopes),
                    connection_id=str(reusable.connection_id),
                    existing_connection_preserved=True,
                    existing_effective_access=profile.access_for_scopes(reusable.scopes).value,
                    existing_permanent_delete=_permanent_delete_enabled(
                        profile,
                        reusable.scopes,
                    ),
                )
            if (
                reusable_custody_status != "valid"
                and reused.get("credential") == _RETAINED_BROADER_CREDENTIAL
            ):
                return _post_authorization_discard(
                    profile,
                    _credential_recovery_result(
                        profile,
                        reusable,
                        reusable_custody_status,
                    ),
                )
            current = self.manager.vault.get_connection_snapshot().connection(
                reusable.connection_id
            )
            if current is None:
                raise ConflictError("connection changed during credential reuse")
            return {
                **reused,
                **_connection_success_result(
                    profile,
                    current,
                    status="already_connected",
                ),
            }
        account_label = _alias(alias) or preferred_label or identity.portable_label
        now = datetime.now(UTC)
        metadata = ConnectionMetadata(
            connection_id=pending.connection_id,
            provider=pending.provider,
            source_ids=pending.source_ids,
            credential_kind=pending.credential_kind,
            account=AccountMetadata(
                fingerprint=identity.fingerprint,
                label=account_label,
            ),
            scopes=pending.scopes,
            client=pending.client,
            health=ConnectionHealth.UNVERIFIED,
            created_at=now,
            updated_at=now,
            version=1,
        )
        try:
            publish(metadata)
        except OAuthPermissionGrantError as exc:
            preserved = tuple(
                connection
                for connection in existing
                if connection.account.fingerprint == identity.fingerprint
            )
            existing_effective_access, existing_permanent_delete = _preserved_authority(
                profile,
                preserved,
            )
            return _oauth_permission_failure_result(
                profile,
                access,
                pending.scopes,
                exc,
                retry_command=retry_command or _connect_command(profile, pending.scopes),
                existing_connection_preserved=bool(preserved),
                existing_effective_access=existing_effective_access,
                existing_permanent_delete=existing_permanent_delete,
            )
        result = _connection_success_result(
            profile,
            metadata,
            status="connected",
            health=ConnectionHealth.READY,
            account_label=account_label,
        )
        removed, failed = self._cleanup_superseded_connections(
            profile,
            identity.fingerprint,
            metadata.connection_id,
            _capability_rank(profile, metadata.scopes),
            metadata.scopes,
        )
        if failed:
            result["upgrade_cleanup"] = (
                "superseded_connection_retained"
                if len(failed) == 1
                else "superseded_connections_retained"
            )
            result["warning"] = (
                "The new capability is ready, but some superseded connections could not "
                f"be removed safely ({len(failed)} retained)."
            )
        if len(removed) == 1:
            result["replaced_connection_id"] = removed[0]
        elif removed:
            result["replaced_connection_ids"] = removed
        return result

    def _reusable_connection(
        self,
        profile: ConnectorProfile,
        connections: Sequence[ConnectionMetadata],
        requested_capability: int,
        requested_scopes: tuple[str, ...],
    ) -> tuple[ConnectionMetadata | None, CustodyStatus | None]:
        fallback: tuple[ConnectionMetadata, CustodyStatus] | None = None
        for candidate in _sufficient_connections(
            profile,
            connections,
            requested_capability,
            requested_scopes,
        ):
            custody_status = self.manager.inspect_custody(candidate)
            if custody_status == "valid":
                return candidate, custody_status
            if fallback is None:
                fallback = candidate, custody_status
        return fallback if fallback is not None else (None, None)

    def _existing_connections(self, profile: ConnectorProfile) -> Sequence[ConnectionMetadata]:
        snapshot = self.manager.vault.get_connection_snapshot()
        return self._logical_candidates(profile, snapshot.connections)

    def _cleanup_superseded_connections(
        self,
        profile: ConnectorProfile,
        fingerprint: str,
        new_connection_id: ConnectionId,
        new_capability: int,
        required_scopes: tuple[str, ...],
    ) -> tuple[Sequence[str], Sequence[str]]:
        removed: list[str] = []
        failed: list[str] = []
        snapshot = self.manager.vault.get_connection_snapshot()
        for candidate in snapshot.connections:
            if not _is_superseded_connection(
                profile,
                candidate,
                fingerprint=fingerprint,
                new_connection_id=new_connection_id,
                new_capability=new_capability,
                required_scopes=required_scopes,
            ):
                continue
            try:
                fresh_snapshot = self.manager.vault.get_connection_snapshot()
                current = fresh_snapshot.connection(candidate.connection_id)
                if current is None or not _is_superseded_connection(
                    profile,
                    current,
                    fingerprint=fingerprint,
                    new_connection_id=new_connection_id,
                    new_capability=new_capability,
                    required_scopes=required_scopes,
                ):
                    continue
                self.manager.remove(
                    current.connection_id,
                    expected_revision=fresh_snapshot.revision,
                )
            except (ContinuityError, OSError):
                failed.append(str(candidate.connection_id))
            else:
                removed.append(str(candidate.connection_id))
        return removed, failed


def _is_superseded_connection(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
    *,
    fingerprint: str,
    new_connection_id: ConnectionId,
    new_capability: int,
    required_scopes: tuple[str, ...],
) -> bool:
    if not (
        connection.connection_id != new_connection_id
        and connection.provider == profile.provider
        and connection.source_ids == profile.source_ids
        and connection.credential_kind is CredentialKind.OAUTH2
        and connection.account.fingerprint == fingerprint
        and connection.health not in {ConnectionHealth.REVOKED, ConnectionHealth.UNKNOWN}
    ):
        return False
    capability = _try_capability_rank(profile, connection.scopes)
    if capability is None:
        # Unknown historical authority is never deleted as an upgrade side effect.
        return False
    return capability < new_capability or (
        capability == new_capability
        and not _scopes_satisfy_requirement(
            profile,
            connection.scopes,
            required_scopes,
        )
    )


def _pending_oauth_metadata(
    profile: ConnectorProfile,
    access: ConnectorAccessTier,
    registration: PublicClientRegistration,
    *,
    include_permanent_delete: bool = False,
) -> ConnectionMetadata:
    if registration.provider != profile.provider:
        raise ValidationError("public client registration belongs to the wrong provider")
    if include_permanent_delete and (
        profile.name != "gmail" or access is not ConnectorAccessTier.FULL
    ):
        raise ValidationError("permanent delete requires Gmail Full access")
    if profile.authorization_endpoint is None or profile.token_endpoint is None:
        raise ValidationError("connector OAuth endpoints are unavailable")
    scopes = profile.scopes_for(
        access,
        include_supplemental=include_permanent_delete,
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


def _capability_rank(profile: ConnectorProfile, scopes: tuple[str, ...]) -> int:
    """Rank only the three user-visible OAuth capabilities used by onboarding."""

    access = profile.access_for_scopes(scopes)
    if access is ConnectorAccessTier.FULL and _permanent_delete_enabled(profile, scopes):
        return 2
    return 1 if access is ConnectorAccessTier.FULL else 0


def _try_capability_rank(
    profile: ConnectorProfile,
    scopes: tuple[str, ...],
) -> int | None:
    """Classify stored OAuth authority without trusting historical metadata."""

    try:
        return _capability_rank(profile, scopes)
    except ValidationError:
        return None


def _permanent_delete_enabled(profile: ConnectorProfile, scopes: tuple[str, ...]) -> bool:
    return profile.provider == "google" and any(
        scope in scopes for scope in profile.supplemental_scopes
    )


def _gmail_settings_control(profile: ConnectorProfile, scopes: tuple[str, ...]) -> bool:
    return (
        profile.provider == "google"
        and "gmail" in profile.source_ids
        and _GMAIL_SETTINGS_BASIC in scopes
    )


def _gmail_settings_status(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
    *,
    access: str,
) -> dict[str, object]:
    if profile.name != "gmail":
        return {}
    if access == ConnectorAccessTier.READ.value:
        return {"settings_control": "read_only"}
    keep_permanent_delete = _permanent_delete_enabled(profile, connection.scopes)
    current_scopes = profile.scopes_for(
        ConnectorAccessTier.FULL,
        include_supplemental=keep_permanent_delete,
    )
    if _scopes_satisfy_requirement(profile, connection.scopes, current_scopes):
        return {"settings_control": "ready"}
    return {
        "settings_control": "upgrade_required",
        "settings_upgrade": _connect_command(
            profile,
            current_scopes,
            access=ConnectorAccessTier.FULL,
            connection_id=str(connection.connection_id),
        ),
    }


def _requested_capability_rank(
    profile: ConnectorProfile,
    access: ConnectorAccessTier,
    *,
    include_permanent_delete: bool,
) -> int:
    return _capability_rank(
        profile,
        profile.scopes_for(access, include_supplemental=include_permanent_delete),
    )


def _scopes_satisfy_requirement(
    profile: ConnectorProfile,
    scopes: tuple[str, ...],
    required_scopes: tuple[str, ...],
) -> bool:
    capability = _capability_rank(profile, scopes)
    required_capability = _capability_rank(profile, required_scopes)
    if capability < required_capability:
        return False
    if capability > required_capability and required_capability == 0:
        # Full grants semantically include the provider's Read authority even when the
        # provider expresses those tiers with different scope strings.
        return True
    # Supplemental authority ranks above Full, but it cannot stand in for a newly added
    # base Full permission. Exact inclusion distinguishes current Full from legacy Full.
    return frozenset(required_scopes).issubset(scopes)


def _sufficient_connections(
    profile: ConnectorProfile,
    connections: Sequence[ConnectionMetadata],
    requested_capability: int,
    requested_scopes: tuple[str, ...],
) -> list[ConnectionMetadata]:
    eligible: list[tuple[ConnectionMetadata, int]] = []
    for connection in connections:
        capability = _try_capability_rank(profile, connection.scopes)
        if (
            capability is not None
            and connection.health in _REAUTH_HEALTH
            and connection.account.fingerprint is not None
            and _scopes_satisfy_requirement(
                profile,
                connection.scopes,
                requested_scopes,
            )
        ):
            eligible.append((connection, capability))
    eligible.sort(
        key=lambda item: (
            item[1] == requested_capability,
            item[1],
            item[0].updated_at,
            str(item[0].connection_id),
        ),
        reverse=True,
    )
    return [connection for connection, _capability in eligible]


def _select_preflight_candidate(
    profile: ConnectorProfile,
    connections: Sequence[ConnectionMetadata],
    requested_capability: int,
    requested_scopes: tuple[str, ...],
) -> ConnectionMetadata | None:
    recognized = [
        connection
        for connection in connections
        if _try_capability_rank(profile, connection.scopes) is not None
    ]
    if not recognized:
        return None
    return max(
        recognized,
        key=lambda item: _preflight_candidate_key(
            profile,
            item,
            requested_capability,
            requested_scopes,
        ),
    )


def _preflight_candidate_key(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
    requested_capability: int,
    requested_scopes: tuple[str, ...],
) -> tuple[object, ...]:
    capability = _try_capability_rank(profile, connection.scopes)
    if capability is None:
        return (
            False,
            False,
            False,
            False,
            connection.account.fingerprint is not None,
            connection.health in _CONNECTED_HEALTH,
            -1,
            connection.updated_at,
            str(connection.connection_id),
        )
    sufficient = _scopes_satisfy_requirement(
        profile,
        connection.scopes,
        requested_scopes,
    )
    return (
        connection.health in _CONNECTED_HEALTH and sufficient,
        sufficient,
        capability == requested_capability,
        capability >= requested_capability,
        connection.account.fingerprint is not None,
        connection.health in _CONNECTED_HEALTH,
        capability,
        connection.updated_at,
        str(connection.connection_id),
    )


def _account_selection_required(
    profile: ConnectorProfile,
    candidates: Sequence[ConnectionMetadata],
    access: ConnectorAccessTier,
    *,
    include_permanent_delete: bool,
    alias: str | None,
    browser_mode: BrowserMode | None,
    timeout_seconds: float | None,
) -> dict[str, object]:
    rows = [
        _account_selection_row(
            profile,
            candidate,
            access,
            include_permanent_delete=include_permanent_delete,
            alias=alias,
            browser_mode=browser_mode,
            timeout_seconds=timeout_seconds,
        )
        for candidate in sorted(
            candidates,
            key=lambda item: (
                item.account.fingerprint is None,
                str(item.connection_id),
            ),
        )
    ]
    return {
        "candidates": rows,
        "connector": profile.name,
        "next": "Choose one candidates[].command after confirming its account_label.",
        "nothing_saved": True,
        "status": "account_selection_required",
    }


def _account_selection_row(
    profile: ConnectorProfile,
    candidate: ConnectionMetadata,
    access: ConnectorAccessTier,
    *,
    include_permanent_delete: bool,
    alias: str | None,
    browser_mode: BrowserMode | None,
    timeout_seconds: float | None,
) -> dict[str, object]:
    try:
        effective_access = profile.access_for_scopes(candidate.scopes).value
    except ValidationError:
        return {
            "access": "unknown",
            "account_label": candidate.account.label,
            "command": format_connector_command(
                ("gsv", "connectors", "disconnect", str(candidate.connection_id))
            ),
            "connection_id": str(candidate.connection_id),
            "error_code": "oauth_scope_profile_unrecognized",
            "health": candidate.health.value,
            "permanent_delete": _permanent_delete_enabled(profile, candidate.scopes),
        }
    return {
        "access": effective_access,
        "account_label": candidate.account.label,
        "command": _connect_command(
            profile,
            candidate.scopes,
            access=access,
            connection_id=str(candidate.connection_id),
            include_permanent_delete=(
                include_permanent_delete
                or (
                    access is ConnectorAccessTier.FULL
                    and _permanent_delete_enabled(profile, candidate.scopes)
                )
            ),
            alias=alias,
            browser_mode=browser_mode,
            timeout_seconds=timeout_seconds,
        ),
        "connection_id": str(candidate.connection_id),
        "health": candidate.health.value,
        "permanent_delete": _permanent_delete_enabled(profile, candidate.scopes),
    }


def _scope_profile_recovery_result(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
) -> dict[str, object]:
    return {
        "access": "unknown",
        "account_label": connection.account.label,
        "connection_id": str(connection.connection_id),
        "connector": profile.name,
        "error_code": "oauth_scope_profile_unrecognized",
        "health": connection.health.value,
        "next": format_connector_command(
            ("gsv", "connectors", "disconnect", str(connection.connection_id))
        ),
        "nothing_saved": True,
        "permanent_delete": _permanent_delete_enabled(profile, connection.scopes),
        "status": "oauth_scope_profile_unrecognized",
    }


def _setup_incomplete_result(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
) -> dict[str, object]:
    return {
        "access": profile.access_for_scopes(connection.scopes).value,
        "account_label": connection.account.label,
        "connection_id": str(connection.connection_id),
        "connector": profile.name,
        "health": connection.health.value,
        "next": f"gsv connectors resume {connection.connection_id}",
        "nothing_saved": True,
        "permanent_delete": _permanent_delete_enabled(profile, connection.scopes),
        "status": "setup_incomplete",
    }


def _connection_success_result(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
    *,
    status: str,
    health: ConnectionHealth | None = None,
    account_label: str | None = None,
) -> dict[str, object]:
    return {
        "access": profile.access_for_scopes(connection.scopes).value,
        "account_label": account_label if account_label is not None else connection.account.label,
        "connection_id": str(connection.connection_id),
        "connector": profile.name,
        "health": (health or connection.health).value,
        "permanent_delete": _permanent_delete_enabled(profile, connection.scopes),
        "status": status,
    }


def _broader_access_result(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
    *,
    selected_access: ConnectorAccessTier,
    requested_scopes: tuple[str, ...],
) -> dict[str, object]:
    effective_access = profile.access_for_scopes(connection.scopes)
    reconnect = _connect_command(
        profile,
        requested_scopes,
        access=selected_access,
    )
    disconnect = format_connector_command(
        (
            "gsv",
            "connectors",
            "disconnect",
            str(connection.connection_id),
        )
    )
    status = "broader_access_already_connected"
    next_command = _status_command(connection.connection_id)
    if connection.health is ConnectionHealth.REAUTHORIZATION_REQUIRED:
        status = "broader_access_reauthorization_required"
        next_command = _reauthorize_command(connection.connection_id)
    return {
        **_connection_success_result(
            profile,
            connection,
            status=status,
        ),
        "credential": "unchanged",
        "disconnect": disconnect,
        "downgrade_help": (
            "Nothing changed. To use only the selected access, disconnect this local "
            "connection, remove Seld from the provider's app-consent page, then run reconnect."
        ),
        "effective_access": effective_access.value,
        "next": next_command,
        "nothing_saved": True,
        "reconnect": reconnect,
        "requested_access": selected_access.value,
        "revocation_help": provider_revocation_guidance(profile.provider),
    }


def _oauth_permission_failure_result(
    profile: ConnectorProfile,
    access: ConnectorAccessTier,
    scopes: tuple[str, ...],
    error: OAuthPermissionGrantError,
    *,
    retry_command: str,
    connection_id: str | None = None,
    existing_connection_preserved: bool,
    existing_effective_access: str,
    existing_permanent_delete: bool,
) -> dict[str, object]:
    status = (
        "oauth_permissions_missing"
        if error.reason == "missing_selected_permissions"
        else "oauth_permissions_outside_selected_tier"
    )
    result: dict[str, object] = {
        "access": existing_effective_access,
        "connector": profile.name,
        "existing_connection_preserved": existing_connection_preserved,
        "effective_access": existing_effective_access,
        "existing_permanent_delete": existing_permanent_delete,
        "next": retry_command,
        "nothing_saved": True,
        "permanent_delete": existing_permanent_delete,
        "reason": error.reason,
        "requested_access": access.value,
        "requested_permanent_delete": _permanent_delete_enabled(profile, scopes),
        "retry": retry_command,
        "status": status,
    }
    if connection_id is not None:
        result["connection_id"] = connection_id
    _add_provider_authorization_context(
        result,
        profile,
        provider_authorization=True,
        existing_connection_preserved=existing_connection_preserved,
    )
    return result


def _oauth_authorization_rejected_result(
    profile: ConnectorProfile,
    access: ConnectorAccessTier,
    scopes: tuple[str, ...],
    *,
    retry_command: str,
    connection_id: str | None = None,
    existing_connection_preserved: bool,
    existing_effective_access: str,
    existing_permanent_delete: bool,
) -> dict[str, object]:
    """Return a stable cancellation result without implying a provider grant exists."""

    result: dict[str, object] = {
        "access": existing_effective_access,
        "connector": profile.name,
        "effective_access": existing_effective_access,
        "existing_connection_preserved": existing_connection_preserved,
        "existing_permanent_delete": existing_permanent_delete,
        "next": retry_command,
        "nothing_saved": True,
        "permanent_delete": existing_permanent_delete,
        "reason": "authorization_rejected",
        "requested_access": access.value,
        "requested_permanent_delete": _permanent_delete_enabled(profile, scopes),
        "retry": retry_command,
        "status": "cancelled",
    }
    if connection_id is not None:
        result["connection_id"] = connection_id
    return result


def _preserved_authority(
    profile: ConnectorProfile,
    connections: Sequence[ConnectionMetadata],
) -> tuple[str, bool]:
    if not connections:
        return "none", False
    effective_access = "unknown"
    if len(connections) == 1:
        with suppress(ValidationError):
            effective_access = profile.access_for_scopes(connections[0].scopes).value
    permanent_delete = any(
        _permanent_delete_enabled(profile, connection.scopes) for connection in connections
    )
    return effective_access, permanent_delete


def _credential_recovery_result(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
    custody_status: CustodyStatus,
) -> dict[str, object]:
    if custody_status == "missing":
        status = "credential_missing_reconnect_required"
    elif custody_status == "pointer_invalid":
        status = "credential_pointer_invalid_reconnect_required"
    else:
        status = "credential_invalid_reconnect_required"
    if custody_status == "missing" and connection.health is ConnectionHealth.UNVERIFIED:
        next_command = f"gsv connectors resume {connection.connection_id}"
    elif (
        custody_status != "pointer_invalid"
        and connection.health is not ConnectionHealth.UNVERIFIED
        and connection.credential_kind is CredentialKind.OAUTH2
        and connection.account.fingerprint is not None
    ):
        next_command = _connect_command(
            profile,
            connection.scopes,
            connection_id=str(connection.connection_id),
        )
    else:
        next_command = f"gsv connectors disconnect {connection.connection_id}"
    return {
        "access": profile.access_for_scopes(connection.scopes).value,
        "account_label": connection.account.label,
        "connection_id": str(connection.connection_id),
        "connector": profile.name,
        "health": connection.health.value,
        "next": next_command,
        "nothing_saved": True,
        "permanent_delete": _permanent_delete_enabled(profile, connection.scopes),
        "reconnect": _connect_command(
            profile,
            connection.scopes,
            new_account=True,
        ),
        "status": status,
    }


def _post_authorization_discard(
    profile: ConnectorProfile,
    result: dict[str, object],
) -> dict[str, object]:
    if profile.credential_kind is not CredentialKind.OAUTH2:
        return result
    return {
        **result,
        "provider_access_may_remain": True,
        "revocation_help": provider_revocation_guidance(profile.provider),
    }


def _identity_binding_recovery_result(
    profile: ConnectorProfile,
    connection: ConnectionMetadata,
) -> dict[str, object]:
    return {
        "access": profile.access_for_scopes(connection.scopes).value,
        "account_label": connection.account.label,
        "connection_id": str(connection.connection_id),
        "connector": profile.name,
        "health": connection.health.value,
        "next": f"gsv connectors disconnect {connection.connection_id}",
        "nothing_saved": True,
        "permanent_delete": _permanent_delete_enabled(profile, connection.scopes),
        "reconnect": _connect_command(profile, connection.scopes, new_account=True),
        "status": "identity_binding_missing_reconnect_required",
    }


def _cancelled_identity_result(
    profile: ConnectorProfile,
    *,
    connection_id: str | None = None,
    next_command: str,
    retry_command: str | None = None,
    provider_authorization: bool,
    existing_connection_preserved: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "connector": profile.name,
        "next": next_command,
        "nothing_saved": True,
        "status": "cancelled",
    }
    if retry_command is not None:
        result["retry"] = retry_command
    _add_provider_authorization_context(
        result,
        profile,
        provider_authorization=provider_authorization,
        existing_connection_preserved=existing_connection_preserved,
    )
    if connection_id is not None:
        result["connection_id"] = connection_id
    return result


def _different_account_result(
    profile: ConnectorProfile,
    scopes: Sequence[str],
    *,
    connection_id: str | None = None,
    retry_command: str | None = None,
    provider_authorization: bool,
    existing_connection_preserved: bool = False,
    new_account_command: str | None = None,
) -> dict[str, object]:
    new_account_command = new_account_command or _connect_command(
        profile,
        scopes,
        new_account=True,
    )
    result: dict[str, object] = {
        "connector": profile.name,
        "new_account": new_account_command,
        "next": retry_command or new_account_command,
        "nothing_saved": True,
        "status": "different_account",
    }
    _add_provider_authorization_context(
        result,
        profile,
        provider_authorization=provider_authorization,
        existing_connection_preserved=existing_connection_preserved,
    )
    if retry_command is not None:
        result["retry"] = retry_command
    if connection_id is not None:
        result["connection_id"] = connection_id
    return result


def _add_provider_authorization_context(
    result: dict[str, object],
    profile: ConnectorProfile,
    *,
    provider_authorization: bool,
    existing_connection_preserved: bool,
) -> None:
    if not provider_authorization:
        return
    result["provider_access_may_remain"] = True
    result["revocation_help"] = provider_revocation_guidance(profile.provider)
    if existing_connection_preserved:
        result["revocation_warning"] = (
            "Provider-side revocation is optional cleanup and may also disconnect the retained "
            "existing connection."
        )


@contextmanager
def _provider_authorization_phase() -> Iterator[None]:
    try:
        yield
    except BaseException as exc:
        mark_provider_authorization_may_remain(exc)
        raise


def _same_credential_binding(
    before: ConnectionMetadata,
    after: ConnectionMetadata,
) -> bool:
    return (
        before.provider,
        before.source_ids,
        before.credential_kind,
        before.account,
        before.scopes,
        before.client,
    ) == (
        after.provider,
        after.source_ids,
        after.credential_kind,
        after.account,
        after.scopes,
        after.client,
    )


def _alias(value: str | None) -> str | None:
    return validate_connector_alias(value)


def _browser_mode(value: BrowserMode | None) -> BrowserMode | None:
    if value is None:
        return None
    connector_browser_options(value)
    return value


def _permission_update(
    profile: ConnectorProfile,
    existing_scopes: tuple[str, ...],
    access: ConnectorAccessTier,
    *,
    include_permanent_delete: bool,
) -> str:
    connector = profile.name.replace("_", " ").title()
    existing = profile.access_for_scopes(existing_scopes).value.title()
    tier = access.value.title()
    authority = f"{tier} permissions" if existing == tier else f"{tier} access"
    if include_permanent_delete:
        authority = f"{tier} access including permanent Gmail deletion"
    return (
        f"Replaces your existing {connector} {existing} connection with Seld's current "
        f"{authority}. The existing connection keeps working until this succeeds."
    )


def _status_command(connection_id: ConnectionId | str) -> str:
    return format_connector_command(("gsv", "connectors", "status", str(connection_id)))


def _resume_command(
    connection_id: ConnectionId | str,
    *,
    alias: str | None = None,
) -> str:
    arguments = ["gsv", "connectors", "resume", str(connection_id)]
    if alias is not None:
        arguments.append(f"--alias={alias}")
    return format_connector_command(arguments)


def _reauthorize_command(
    connection_id: ConnectionId | str,
    *,
    alias: str | None = None,
    browser_mode: BrowserMode | None = None,
    timeout_seconds: float | None = None,
) -> str:
    arguments = ["gsv", "connectors", "reauthorize", str(connection_id)]
    if alias is not None:
        arguments.append(f"--alias={alias}")
    if timeout_seconds is not None:
        arguments.extend(("--timeout", format(timeout_seconds, "g")))
    arguments.extend(connector_browser_options(browser_mode))
    return format_connector_command(arguments)


def provider_revocation_guidance(provider: str) -> str:
    """Return provider-side cleanup guidance without implying remote revocation."""

    guidance = {
        "discord": "Reset or delete the bot token in the Discord Developer Portal.",
        "google": "Remove Seld from your Google Account third-party access page.",
        "microsoft": ("Remove Seld from your Microsoft account or organization app-consent page."),
        "slack": "Remove Seld from the workspace's installed apps.",
    }
    try:
        return guidance[provider]
    except KeyError as exc:
        raise ValidationError("connector provider has no revocation guidance") from exc
