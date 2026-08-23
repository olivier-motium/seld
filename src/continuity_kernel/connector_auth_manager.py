"""Standalone connector-auth orchestration with no host-account dependency."""

from __future__ import annotations

import base64
import hashlib
import math
import secrets
import ssl
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

from continuity_kernel.config import connector_auth_dir
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_client_registration import (
    PublicClientRegistration,
    load_public_client_registration,
)
from continuity_kernel.connector_credentials import (
    OAuthCredential,
    credential_from_token_set,
)
from continuity_kernel.connector_identifiers import (
    ConnectionId,
    SecretStore,
    new_connection_id,
    parse_connection_id,
    parse_secret_name,
)
from continuity_kernel.connector_local_tls import ensure_local_tls
from continuity_kernel.connector_oauth import (
    OAuthClientConfig,
    OAuthDialect,
    OAuthTokenEndpointError,
    OAuthTransportError,
    canonicalize_google_scopes,
    canonicalize_microsoft_access_scopes,
    canonicalize_microsoft_scopes,
    exchange_authorization_code,
    refresh_access_token,
)
from continuity_kernel.connector_oauth_loopback import BoundLoopbackCallback, begin_authorization
from continuity_kernel.connector_profiles import (
    ConnectorAccessTier,
    connector_connect_command,
    get_profile,
    get_profile_for_connection,
    validate_connector_alias,
)
from continuity_kernel.connector_secrets import KeyringSecretStore
from continuity_kernel.connector_token_store import AtomicTokenStore, ResolvedToken, TokenState
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    MutationCommittedError,
    NotFoundError,
    OAuthPermissionGrantError,
    SetupError,
    ValidationError,
    mark_provider_authorization_may_remain,
)
from continuity_kernel.vault import Vault

_PINNED_OAUTH_ENDPOINTS: Final = {
    "google": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
    ),
    "microsoft": (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    ),
    "slack": (
        "https://slack.com/oauth/v2_user/authorize",
        "https://slack.com/api/oauth.v2.user.access",
    ),
}
_DEFAULT_BROWSER_OPENER: Final = object()
_VERIFIED_HEALTH: Final = frozenset({ConnectionHealth.READY, ConnectionHealth.DEGRADED})
_CUSTODY_PROBE_NAME: Final = parse_secret_name("readiness-probe")
_OAUTH_CLIENT_SECRET_NAME: Final = parse_secret_name("oauth-client-secret")
_MAX_OAUTH_CLIENT_SECRET_BYTES: Final = 2 * 1024
_GMAIL_MODIFY_SCOPE: Final = "https://www.googleapis.com/auth/gmail.modify"
_GMAIL_READ_SCOPE: Final = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_PURGE_SCOPE: Final = "https://mail.google.com/"
CustodyStatus = Literal["valid", "missing", "invalid", "pointer_invalid"]


@dataclass(frozen=True, repr=False)
class ResolvedOAuthAccessToken:
    access_token: str
    state: TokenState
    scopes: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "ResolvedOAuthAccessToken(access_token=<redacted>, "
            f"state={self.state!r}, scopes={self.scopes!r})"
        )


class ConnectorAuthManager:
    """Bind portable records to one vault-scoped host credential store."""

    def __init__(
        self,
        vault: Vault,
        *,
        secret_store: SecretStore | None = None,
        state_root: Path | None = None,
    ) -> None:
        identity = vault.identity()
        vault_id = str(identity["vault_id"])
        self.vault = vault
        self.vault_id = vault_id
        self.secret_store = secret_store or KeyringSecretStore(f"seld.connector-auth.{vault_id}")
        self.tokens = AtomicTokenStore(
            state_root or connector_auth_dir(vault_id),
            self.secret_store,
        )

    def oauth_client_secret_status(
        self,
        registration: PublicClientRegistration,
    ) -> Literal["configured", "invalid", "missing", "not_required"]:
        """Report only whether one packaged client has its host-local secret."""

        if not registration.client_secret_required:
            return "not_required"
        try:
            stored = self.secret_store.get_secret(
                _oauth_client_secret_key(registration),
                _OAUTH_CLIENT_SECRET_NAME,
            )
        except ValidationError:
            return "invalid"
        if stored is None:
            return "missing"
        try:
            _oauth_client_secret_text(stored, stored=True)
        except ValidationError:
            return "invalid"
        return "configured"

    def store_oauth_client_secret(
        self,
        provider: str,
        value: bytes,
        *,
        replace_existing: bool = False,
    ) -> dict[str, object]:
        """Put one provider client secret in the vault-scoped OS keyring."""

        registration = load_public_client_registration(provider)
        if not registration.client_secret_required:
            raise ValidationError(
                f"{registration.provider} does not require a host-local OAuth client secret"
            )
        secret = _oauth_client_secret_text(value, stored=False).encode("utf-8")
        key = _oauth_client_secret_key(registration)
        if not replace_existing:
            try:
                current = self.secret_store.get_secret(key, _OAUTH_CLIENT_SECRET_NAME)
            except ValidationError as exc:
                raise ValidationError(
                    "stored OAuth client secret is invalid; pass --replace to replace it"
                ) from exc
            if current is not None:
                raise ValidationError(
                    "OAuth client secret is already configured; pass --replace to rotate it"
                )
        self.secret_store.set_secret(key, _OAUTH_CLIENT_SECRET_NAME, secret)
        return {
            "client_secret": "configured",
            "next": "gsv connectors readiness",
            "provider": registration.provider,
            "status": "configured",
        }

    def clear_oauth_client_secret(self, provider: str) -> dict[str, object]:
        """Remove one provider client secret from the vault-scoped OS keyring."""

        registration = load_public_client_registration(provider)
        if not registration.client_secret_required:
            raise ValidationError(
                f"{registration.provider} does not require a host-local OAuth client secret"
            )
        key = _oauth_client_secret_key(registration)
        try:
            current = self.secret_store.get_secret(key, _OAUTH_CLIENT_SECRET_NAME)
        except ValidationError:
            had_value = True
        else:
            had_value = current is not None
        if had_value:
            self.secret_store.delete_secret(key, _OAUTH_CLIENT_SECRET_NAME)
        return {
            "client_secret": "missing",
            "next": f"gsv connectors client-secret set {registration.provider}",
            "nothing_changed": not had_value,
            "provider": registration.provider,
            "status": "cleared" if had_value else "already_clear",
        }

    def status(self) -> dict[str, Any]:
        """Project portable metadata plus redacted host-local availability."""

        portable = self.vault.connection_status()
        rows = portable["connections"]
        assert isinstance(rows, list)
        projected: list[dict[str, object]] = []
        snapshot = self.vault.get_connection_snapshot()
        if portable.get("revision") != snapshot.revision:
            raise ConflictError("connection status changed while it was being read")
        for raw in rows:
            assert isinstance(raw, dict)
            row = dict(raw)
            connection_id = parse_connection_id(row["connection_id"])
            connection = snapshot.connection(connection_id)
            if connection is None:
                raise ConflictError("connection status changed while it was being read")
            host_credential, custody = self._host_availability(connection)
            row["host_credential"] = host_credential
            next_action = self._recovery_action(connection, host_credential, custody)
            if next_action is not None:
                row["next"] = next_action
            projected.append(row)
        return {**portable, "connections": projected, "vault_id": self.vault_id}

    def update_connection_alias(
        self,
        connection_id: ConnectionId | str,
        alias: str,
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        """CAS-update only a user-owned connection label; custody is untouched."""

        clean_alias = validate_connector_alias(alias)
        if clean_alias is None:
            raise ValidationError("connector alias is invalid")
        clean_id = parse_connection_id(connection_id)
        with self.tokens.exclusive_lifecycle(clean_id):
            snapshot = self.vault.get_connection_snapshot()
            if snapshot.revision != expected_revision:
                raise ConflictError("connection changed; reload before repairing its alias")
            metadata = snapshot.connection(clean_id)
            if metadata is None:
                raise NotFoundError("connection was not found")
            self._assert_not_revoked(metadata)
            observed_at = max(
                datetime.now(UTC),
                metadata.updated_at + timedelta(microseconds=1),
            )
            updated = replace(
                metadata,
                account=replace(metadata.account, label=clean_alias),
                updated_at=observed_at,
                version=metadata.version + 1,
            )
            return self.vault.put_connection(
                expected_revision=expected_revision,
                connection=updated,
                observed_at=observed_at,
            )

    def store_credential(
        self,
        connection_id: ConnectionId | str,
        value: bytes,
        *,
        expected_token_version: int,
    ) -> TokenState:
        clean_id = parse_connection_id(connection_id)
        with self.tokens.exclusive_lifecycle(clean_id):
            metadata = self._metadata(clean_id)
            self._assert_not_revoked(metadata)
            if metadata.credential_kind is CredentialKind.OAUTH2:
                raise ValidationError("use the OAuth credential path for an OAuth connection")
            if (
                metadata.credential_kind is CredentialKind.BEARER
                and metadata.account.fingerprint is not None
            ):
                raise ValidationError(
                    "a bound bearer credential must be replaced through its verified connector "
                    "onboarding flow"
                )
            return self.tokens.update(
                metadata.connection_id,
                expected_version=expected_token_version,
                value=value,
            )

    def resolve_credential_state(
        self,
        connection_id: ConnectionId | str,
        *,
        require_verified_identity: bool = True,
    ) -> ResolvedToken:
        """Resolve runtime-only bytes with the pointer state needed for operation CAS."""

        clean_id = parse_connection_id(connection_id)
        with self.tokens.exclusive_lifecycle(clean_id):
            metadata = self._metadata(clean_id)
            self._assert_not_revoked(metadata)
            if require_verified_identity:
                self._assert_verified_identity(metadata)
            if metadata.credential_kind is CredentialKind.OAUTH2:
                raise ValidationError(
                    "use resolve_oauth_access_token_state for an OAuth connection"
                )
            return self.tokens.read(metadata.connection_id)

    def resolve_oauth_access_token_state(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_connection_revision: str | None = None,
        minimum_validity_seconds: int = 60,
        timeout_seconds: float = 15.0,
        observed_at: datetime | None = None,
        require_verified_identity: bool = True,
    ) -> ResolvedOAuthAccessToken:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValidationError("OAuth timeout must be a positive finite number")
        deadline = monotonic() + timeout_seconds

        def remaining() -> float:
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise OAuthTransportError("OAuth credential resolution timed out")
            return seconds

        clean_id = parse_connection_id(connection_id)
        with self.tokens.exclusive_lifecycle(
            clean_id,
            lock_timeout_seconds=remaining(),
        ):
            metadata = self._oauth_metadata(
                clean_id,
                expected_revision=expected_connection_revision,
            )
            if require_verified_identity:
                self._assert_verified_identity(metadata)
            now = (observed_at or datetime.now(UTC)).astimezone(UTC)

            def refresh_if_needed(resolved: ResolvedToken) -> bytes | None:
                previous = OAuthCredential.from_bytes(resolved.value)
                previous = self._canonicalize_oauth_credential(metadata, previous)
                self._validate_oauth_credential(metadata, previous)
                if previous.usable_at(
                    now,
                    minimum_validity_seconds=minimum_validity_seconds,
                ):
                    canonical = previous.to_bytes()
                    return None if canonical == resolved.value else canonical
                if not previous.refresh_token:
                    raise ValidationError("OAuth connection requires reauthorization")
                config = self._oauth_config(metadata)
                token_set = refresh_access_token(
                    config,
                    refresh_token=previous.refresh_token,
                    scopes=previous.scopes,
                    timeout_seconds=remaining(),
                )
                refreshed = credential_from_token_set(
                    token_set,
                    issued_at=now,
                    previous=previous,
                )
                refreshed = self._canonicalize_oauth_credential(metadata, refreshed)
                self._validate_oauth_credential(metadata, refreshed)
                return refreshed.to_bytes()

            try:
                resolved = self.tokens.refresh_serialized(
                    metadata.connection_id,
                    transform=refresh_if_needed,
                    lock_timeout_seconds=remaining(),
                    updated_at=now,
                )
                credential = OAuthCredential.from_bytes(resolved.value)
                credential = self._canonicalize_oauth_credential(metadata, credential)
                self._validate_oauth_credential(metadata, credential)
                return ResolvedOAuthAccessToken(
                    access_token=credential.access_token,
                    state=resolved.state,
                    scopes=credential.scopes,
                )
            except OAuthTokenEndpointError as exc:
                health = (
                    ConnectionHealth.REAUTHORIZATION_REQUIRED
                    if (
                        exc.error in {"invalid_grant", "invalid_refresh_token"}
                        or exc.status_code in {401, 403}
                        or (
                            metadata.provider == "google"
                            and exc.error == "invalid_request"
                            and exc.status_code == 400
                        )
                    )
                    else ConnectionHealth.DEGRADED
                )
                self._mark_health(
                    metadata.connection_id,
                    health,
                    observed_at=max(
                        now,
                        metadata.updated_at + timedelta(microseconds=1),
                    ),
                )
                raise
            except OAuthPermissionGrantError:
                self._mark_health(
                    metadata.connection_id,
                    ConnectionHealth.REAUTHORIZATION_REQUIRED,
                    observed_at=max(
                        now,
                        metadata.updated_at + timedelta(microseconds=1),
                    ),
                )
                raise

    def inspect_custody(self, connection: ConnectionMetadata) -> CustodyStatus:
        """Parse-validate one host credential without requiring verified identity."""

        if not isinstance(connection, ConnectionMetadata):
            raise ValidationError("connector connection metadata is invalid")
        try:
            state = self.tokens.state(connection.connection_id)
        except ValidationError:
            return "pointer_invalid"
        except (OSError, ContinuityError) as exc:
            raise SetupError("connector credential custody is unavailable") from exc
        if state is None:
            return "missing"
        try:
            resolved = self.tokens.read(connection.connection_id)
        except (ValidationError, NotFoundError):
            return "invalid"
        except (OSError, ContinuityError) as exc:
            raise SetupError("connector credential custody is unavailable") from exc
        try:
            if connection.credential_kind is CredentialKind.OAUTH2:
                self.validate_import_credential(connection, resolved.value)
            elif connection.credential_kind is CredentialKind.BEARER:
                bearer = resolved.value.decode("utf-8")
                if not bearer or any(character in bearer for character in "\x00\r\n"):
                    return "invalid"
        except (UnicodeError, ValidationError):
            return "invalid"
        return "valid"

    def probe_credential_custody(self) -> None:
        """Prove the configured secret store can round-trip and remove a sentinel."""

        connection_id = new_connection_id()
        marker = secrets.token_bytes(32)
        failure: BaseException | None = None
        try:
            self.secret_store.set_secret(connection_id, _CUSTODY_PROBE_NAME, marker)
            if self.secret_store.get_secret(connection_id, _CUSTODY_PROBE_NAME) != marker:
                raise SetupError("connector credential custody readiness check failed")
        except BaseException as exc:
            failure = exc
        cleanup_failure: BaseException | None = None
        try:
            self.secret_store.delete_secret(connection_id, _CUSTODY_PROBE_NAME)
            if self.secret_store.get_secret(connection_id, _CUSTODY_PROBE_NAME) is not None:
                raise SetupError("connector credential custody readiness cleanup failed")
        except BaseException as exc:
            cleanup_failure = exc
        if failure is not None:
            if cleanup_failure is not None:
                failure.add_note("connector credential custody readiness cleanup is unconfirmed")
            if not isinstance(failure, Exception):
                raise failure
            if isinstance(failure, SetupError):
                raise failure
            raise SetupError("connector credential custody readiness check failed") from failure
        if cleanup_failure is not None:
            if isinstance(cleanup_failure, SetupError):
                raise cleanup_failure
            if not isinstance(cleanup_failure, Exception):
                raise cleanup_failure
            raise SetupError(
                "connector credential custody readiness cleanup failed"
            ) from cleanup_failure

    def mark_reauthorization_required(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_connection_revision: str,
        expected_token_version: int | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """CAS-project a provider authentication rejection into portable health."""

        clean_id = parse_connection_id(connection_id)
        with self.tokens.exclusive_lifecycle(clean_id):
            snapshot = self.vault.get_connection_snapshot()
            if snapshot.revision != expected_connection_revision:
                raise ConflictError("connection changed; discard the provider result and retry")
            metadata = snapshot.connection(clean_id)
            if metadata is None:
                raise NotFoundError("connection was not found")
            self._assert_not_revoked(metadata)
            if expected_token_version is not None:
                token_state = self.tokens.state(clean_id)
                if token_state is None or token_state.version != expected_token_version:
                    raise ConflictError("credential changed; discard the provider result and retry")
            when = max(
                (observed_at or datetime.now(UTC)).astimezone(UTC),
                metadata.updated_at + timedelta(microseconds=1),
            )
            return self.vault.mark_connection_health(
                expected_revision=expected_connection_revision,
                connection_id=clean_id,
                health=ConnectionHealth.REAUTHORIZATION_REQUIRED,
                observed_at=when,
            )

    def access_tier(self, connection_id: ConnectionId | str) -> ConnectorAccessTier:
        """Read the exact locally selected tier without resolving a credential."""

        metadata = self._metadata(connection_id)
        return get_profile_for_connection(
            metadata.provider,
            metadata.source_ids,
            metadata.scopes,
        ).access_for_scopes(metadata.scopes)

    def verified_connection_metadata(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_connection_revision: str | None = None,
    ) -> ConnectionMetadata:
        """Resolve metadata only when its provider identity is confirmed and usable."""

        metadata = self._metadata(
            connection_id,
            expected_revision=expected_connection_revision,
        )
        self._assert_not_revoked(metadata)
        self._assert_verified_identity(metadata)
        return metadata

    def authorize_oauth(
        self,
        connection_id: ConnectionId | str,
        *,
        timeout_seconds: float = 180.0,
        browser_opener: Callable[[str], bool] | None | object = _DEFAULT_BROWSER_OPENER,
        present_authorization_url: Callable[[str, bool], None] | None = None,
    ) -> None:
        """Run one interactive native authorization and persist its secret result."""

        clean_id = parse_connection_id(connection_id)
        metadata = self._oauth_metadata(clean_id)
        credential = self.acquire_oauth_credential(
            metadata,
            timeout_seconds=timeout_seconds,
            browser_opener=browser_opener,
            present_authorization_url=present_authorization_url,
        )
        acquired_binding = self._credential_binding(metadata)
        with self.tokens.exclusive_lifecycle(clean_id):
            snapshot = self.vault.get_connection_snapshot()
            current = snapshot.connection(clean_id)
            if current is None:
                raise NotFoundError("connection was not found")
            if self._credential_binding(current) != acquired_binding:
                raise ConflictError("connection changed during OAuth authorization")
            self._assert_not_revoked(current)
            credential = self._canonicalize_oauth_credential(current, credential)
            self._validate_oauth_credential(current, credential)
            token_state = self.tokens.state(clean_id)
            expected_token_version = token_state.version if token_state else 0
            observed_at = max(
                datetime.now(UTC),
                current.updated_at + timedelta(microseconds=1),
            )
            self.vault.mark_connection_health(
                expected_revision=snapshot.revision,
                connection_id=clean_id,
                health=ConnectionHealth.UNVERIFIED,
                observed_at=observed_at,
            )

            transitioned_snapshot = self.vault.get_connection_snapshot()
            transitioned = transitioned_snapshot.connection(clean_id)
            if transitioned is None:
                raise NotFoundError("connection was not found")
            if (
                self._credential_binding(transitioned) != acquired_binding
                or transitioned.health is not ConnectionHealth.UNVERIFIED
            ):
                raise ConflictError("connection changed during OAuth authorization")
            self._validate_oauth_credential(transitioned, credential)
            self.tokens.update(
                clean_id,
                expected_version=expected_token_version,
                value=credential.to_bytes(),
                updated_at=credential.issued_at,
            )

    def rotate_verified_oauth_credential(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_revision: str,
        expected_account_fingerprint: str,
        expected_token_version: int,
        replacement: OAuthCredential,
        account_label: str | None = None,
    ) -> dict[str, Any]:
        """Atomically replace one verified connection's OAuth credential."""

        def prepare(metadata: ConnectionMetadata) -> tuple[bytes, datetime]:
            if metadata.credential_kind is not CredentialKind.OAUTH2:
                raise ValidationError("connection does not use OAuth")
            self._oauth_config(metadata)
            prepared = self._canonicalize_oauth_credential(metadata, replacement)
            self._validate_oauth_credential(metadata, prepared)
            return prepared.to_bytes(), prepared.issued_at

        return self._rotate_verified_credential(
            connection_id,
            expected_revision=expected_revision,
            expected_account_fingerprint=expected_account_fingerprint,
            expected_token_version=expected_token_version,
            prepare=prepare,
            account_label=account_label,
        )

    def rotate_verified_bearer_credential(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_revision: str,
        expected_account_fingerprint: str,
        expected_token_version: int,
        replacement: bytes,
        account_label: str | None = None,
    ) -> dict[str, Any]:
        """Atomically replace one verified bot connection's bearer credential."""

        def prepare(metadata: ConnectionMetadata) -> tuple[bytes, None]:
            if metadata.credential_kind is not CredentialKind.BEARER:
                raise ValidationError("connection does not use a bearer credential")
            return replacement, None

        return self._rotate_verified_credential(
            connection_id,
            expected_revision=expected_revision,
            expected_account_fingerprint=expected_account_fingerprint,
            expected_token_version=expected_token_version,
            prepare=prepare,
            account_label=account_label,
        )

    def _rotate_verified_credential(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_revision: str,
        expected_account_fingerprint: str,
        expected_token_version: int,
        prepare: Callable[[ConnectionMetadata], tuple[bytes, datetime | None]],
        account_label: str | None,
    ) -> dict[str, Any]:
        clean_id = parse_connection_id(connection_id)
        with self.tokens.exclusive_lifecycle(clean_id):
            snapshot = self.vault.get_connection_snapshot()
            if snapshot.revision != expected_revision:
                raise ConflictError("connection changed; reload before rotating credential")
            metadata = snapshot.connection(clean_id)
            if metadata is None:
                raise NotFoundError("connection was not found")
            if metadata.account.fingerprint != expected_account_fingerprint:
                raise ConflictError("account binding changed; reload before rotating credential")
            if metadata.health not in {
                ConnectionHealth.READY,
                ConnectionHealth.DEGRADED,
                ConnectionHealth.REAUTHORIZATION_REQUIRED,
            }:
                raise ValidationError("credential rotation requires a verified connection")
            updated_account = AccountMetadata(
                fingerprint=metadata.account.fingerprint,
                label=(metadata.account.label if account_label is None else account_label),
            )
            replacement, replacement_time = prepare(metadata)
            current = self.tokens.state(clean_id)
            current_version = current.version if current is not None else 0
            if current_version != expected_token_version:
                raise ConflictError(
                    "connector credential changed; reload before rotating credential"
                )

            try:
                self.tokens.update(
                    clean_id,
                    expected_version=expected_token_version,
                    value=replacement,
                    updated_at=replacement_time,
                )
            except MutationCommittedError as exc:
                raise MutationCommittedError(
                    "credential rotation committed; metadata repair is required"
                ) from exc

            observed_candidates = [
                datetime.now(UTC),
                metadata.updated_at + timedelta(microseconds=1),
            ]
            if replacement_time is not None:
                observed_candidates.append(replacement_time)
            observed_at = max(observed_candidates)
            updated = replace(
                metadata,
                account=updated_account,
                health=ConnectionHealth.READY,
                last_verified_at=observed_at,
                updated_at=observed_at,
                version=metadata.version + 1,
            )
            try:
                return self.vault.put_connection(
                    expected_revision=expected_revision,
                    connection=updated,
                    observed_at=observed_at,
                )
            except Exception as exc:
                raise MutationCommittedError(
                    "credential rotation committed; metadata repair is required"
                ) from exc

    def acquire_oauth_credential(
        self,
        metadata: ConnectionMetadata,
        *,
        timeout_seconds: float = 180.0,
        browser_opener: Callable[[str], bool] | None | object = _DEFAULT_BROWSER_OPENER,
        present_authorization_url: Callable[[str, bool], None] | None = None,
    ) -> OAuthCredential:
        """Acquire and validate one OAuth grant in memory without publishing it."""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValidationError("OAuth timeout must be a positive finite number")
        if metadata.credential_kind is not CredentialKind.OAUTH2:
            raise ValidationError("connection does not use OAuth")
        self._assert_not_revoked(metadata)
        deadline = monotonic() + timeout_seconds
        resolved_browser_opener = (
            webbrowser.open
            if browser_opener is _DEFAULT_BROWSER_OPENER
            else cast(Callable[[str], bool] | None, browser_opener)
        )

        def remaining() -> float:
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise OAuthTransportError("OAuth authorization timed out")
            return seconds

        template = urlsplit(metadata.client.redirect_uris[0])
        host = template.hostname
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValidationError("OAuth redirect template must use loopback")
        tls_context: ssl.SSLContext | None = None
        if template.scheme == "https":
            try:
                material = ensure_local_tls()
                tls_context = material.create_ssl_context()
            except SetupError:
                raise
            except Exception as exc:
                raise SetupError(
                    f"Failed to initialize local TLS for HTTPS redirect: {exc}"
                ) from exc
        elif template.scheme == "http":
            tls_context = None
        else:
            raise ValidationError(
                f"OAuth redirect template scheme must be http or https: {template.scheme}"
            )
        listener = BoundLoopbackCallback.bind(
            host=host,
            port=template.port or 0,
            path=template.path,
            tls_context=tls_context,
        )
        authorization_may_remain = False
        try:
            config = self._oauth_config(metadata, redirect_uri=listener.redirect_uri)
            attempt = begin_authorization(config)
            listener.configure(config, attempt)
            browser_opened = False
            if resolved_browser_opener is not None:
                authorization_may_remain = True
                try:
                    browser_opened = bool(resolved_browser_opener(attempt.authorization_url))
                    authorization_may_remain = browser_opened
                except (OSError, RuntimeError, webbrowser.Error):
                    authorization_may_remain = True
                    browser_opened = False
            if present_authorization_url is not None:
                authorization_may_remain = True
                present_authorization_url(attempt.authorization_url, browser_opened)
            elif not browser_opened:
                raise SetupError(
                    "the OAuth authorization page could not be opened; rerun with a manual "
                    "authorization URL presenter"
                )
            code = listener.wait_for_code(timeout_seconds=remaining())
            token_set = exchange_authorization_code(
                config,
                authorization_code=code,
                code_verifier=attempt.code_verifier,
                timeout_seconds=min(remaining(), 30.0),
            )
            issued_at = datetime.now(UTC)
            credential = credential_from_token_set(token_set, issued_at=issued_at)
            credential = self._canonicalize_oauth_credential(metadata, credential)
            self._validate_oauth_credential(metadata, credential)
        except BaseException as exc:
            if authorization_may_remain:
                mark_provider_authorization_may_remain(exc)
            raise
        finally:
            listener.close()
        return credential

    def publish_new_oauth_connection(
        self,
        metadata: ConnectionMetadata,
        credential: OAuthCredential,
    ) -> dict[str, Any]:
        """Publish a verified fresh-ID OAuth connection without exposing its token early."""

        if metadata.credential_kind is not CredentialKind.OAUTH2:
            raise ValidationError("connection does not use OAuth")
        self._validate_publishable_identity(metadata)
        credential = self._canonicalize_oauth_credential(metadata, credential)
        self._validate_oauth_credential(metadata, credential)
        return self._publish_new_connection(metadata, credential.to_bytes())

    def publish_new_bearer_connection(
        self,
        metadata: ConnectionMetadata,
        credential: bytes,
    ) -> dict[str, Any]:
        """Publish a verified fresh-ID bot connection from one transient credential."""

        if metadata.credential_kind is not CredentialKind.BEARER:
            raise ValidationError("connection does not use a bearer credential")
        self._validate_publishable_identity(metadata)
        if not credential:
            raise ValidationError("credential is empty")
        return self._publish_new_connection(metadata, credential)

    def _publish_new_connection(
        self,
        metadata: ConnectionMetadata,
        credential: bytes,
    ) -> dict[str, Any]:
        clean_id = metadata.connection_id
        with self.tokens.exclusive_lifecycle(clean_id):
            snapshot = self.vault.get_connection_snapshot()
            if snapshot.connection(clean_id) is not None or self.tokens.occupied(clean_id):
                raise ConflictError("fresh connector identity is already occupied")
            published_at = max(datetime.now(UTC), metadata.updated_at)
            self.vault.put_connection(
                expected_revision=snapshot.revision,
                connection=metadata,
                observed_at=published_at,
            )
            try:
                self.tokens.update(
                    clean_id,
                    expected_version=0,
                    value=credential,
                    updated_at=metadata.updated_at,
                )
            except Exception:
                self._remove_empty_published_connection(metadata)
                raise
            return self._mark_health(
                clean_id,
                ConnectionHealth.READY,
                observed_at=max(
                    datetime.now(UTC),
                    metadata.updated_at + timedelta(microseconds=1),
                ),
                verified=True,
            )

    def _remove_empty_published_connection(self, metadata: ConnectionMetadata) -> None:
        """Roll back metadata only when no credential material was published."""

        try:
            if self.tokens.occupied(metadata.connection_id):
                return
            snapshot = self.vault.get_connection_snapshot()
            current = snapshot.connection(metadata.connection_id)
            if current != metadata:
                return
            self.vault.remove_connection(
                expected_revision=snapshot.revision,
                connection_id=metadata.connection_id,
                observed_at=max(
                    datetime.now(UTC),
                    metadata.updated_at + timedelta(microseconds=1),
                ),
            )
        except Exception:
            # The UNVERIFIED record is safe and repairable; never turn cleanup
            # uncertainty into deletion of potentially committed secret state.
            return

    @staticmethod
    def _validate_publishable_identity(metadata: ConnectionMetadata) -> None:
        if metadata.version != 1 or metadata.health is not ConnectionHealth.UNVERIFIED:
            raise ValidationError("new verified connections must start as unverified version 1")
        if metadata.account.fingerprint is None:
            raise ValidationError("new verified connections require an account fingerprint")

    def verify_existing_connection_identity(
        self,
        connection_id: ConnectionId | str,
        *,
        fingerprint: str,
        label: str,
        expected_revision: str,
        expected_token_version: int,
    ) -> dict[str, Any]:
        """Publish the confirmed identity for one resumable unverified credential."""

        clean_id = parse_connection_id(connection_id)
        with self.tokens.exclusive_lifecycle(clean_id):
            snapshot = self.vault.get_connection_snapshot()
            if snapshot.revision != expected_revision:
                raise ConflictError("connection changed; restart identity confirmation")
            metadata = snapshot.connection(clean_id)
            if metadata is None:
                raise NotFoundError("connection was not found")
            if metadata.health is not ConnectionHealth.UNVERIFIED:
                raise ValidationError("only an unverified connection can resume identity setup")
            if metadata.account.fingerprint is None:
                raise ValidationError("unverified connection has no bound account identity")
            if metadata.account.fingerprint != fingerprint:
                raise ConflictError("verified account does not match the connection binding")
            token_state = self.tokens.state(clean_id)
            if token_state is None or token_state.version != expected_token_version:
                raise ConflictError("connector credential changed; restart identity confirmation")
            observed_at = max(
                datetime.now(UTC),
                metadata.updated_at + timedelta(microseconds=1),
            )
            verified = replace(
                metadata,
                account=AccountMetadata(fingerprint=fingerprint, label=label),
                health=ConnectionHealth.READY,
                last_verified_at=observed_at,
                updated_at=observed_at,
                version=metadata.version + 1,
            )
            return self.vault.put_connection(
                expected_revision=expected_revision,
                connection=verified,
                observed_at=observed_at,
            )

    def remove(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        clean_id = parse_connection_id(connection_id)
        snapshot = self.vault.get_connection_snapshot()
        if snapshot.revision != expected_revision:
            raise ConflictError("connection changed; reload before removing it")
        metadata = snapshot.connection(clean_id)
        if metadata is None:
            raise NotFoundError("connection was not found")
        revoked_at = max(
            datetime.now(UTC),
            metadata.updated_at + timedelta(microseconds=1),
        )
        revoked = self.vault.mark_connection_health(
            expected_revision=expected_revision,
            connection_id=clean_id,
            health=ConnectionHealth.REVOKED,
            observed_at=revoked_at,
        )
        revoked_revision = revoked.get("revision")
        if not isinstance(revoked_revision, str):
            raise SetupError("revoked connection state has no usable revision")
        with self.tokens.exclusive_lifecycle(clean_id):
            self.tokens.delete(clean_id)
        return self.vault.remove_connection(
            expected_revision=revoked_revision,
            connection_id=clean_id,
            observed_at=max(
                datetime.now(UTC),
                revoked_at + timedelta(microseconds=1),
            ),
        )

    def validate_import_credential(
        self,
        metadata: ConnectionMetadata,
        value: bytes,
    ) -> bytes:
        """Validate one decrypted credential before it enters host custody."""

        self._assert_not_revoked(metadata)
        if metadata.credential_kind is not CredentialKind.OAUTH2:
            return value
        credential = self._canonicalize_oauth_credential(
            metadata,
            OAuthCredential.from_bytes(value),
        )
        self._validate_oauth_credential(metadata, credential)
        return credential.to_bytes()

    def ensure_imported_credential(
        self,
        metadata: ConnectionMetadata,
        value: bytes,
    ) -> TokenState:
        """Publish one validated archive credential without racing removal."""

        clean_id = metadata.connection_id
        with self.tokens.exclusive_lifecycle(clean_id):
            current = self._metadata(clean_id)
            self._assert_not_revoked(current)
            if self._credential_binding(current) != self._credential_binding(metadata):
                raise ConflictError("portable connection changed during credential import")
            value = self.validate_import_credential(current, value)
            return self.tokens.ensure_imported(clean_id, value)

    def _metadata(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_revision: str | None = None,
    ) -> ConnectionMetadata:
        clean_id = parse_connection_id(connection_id)
        snapshot = self.vault.get_connection_snapshot()
        if expected_revision is not None and snapshot.revision != expected_revision:
            raise ConflictError("connection changed; reload before resolving its credential")
        metadata = snapshot.connection(clean_id)
        if metadata is None:
            raise NotFoundError("connection was not found")
        return metadata

    def _oauth_metadata(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_revision: str | None = None,
    ) -> ConnectionMetadata:
        metadata = self._metadata(connection_id, expected_revision=expected_revision)
        if metadata.credential_kind is not CredentialKind.OAUTH2:
            raise ValidationError("connection does not use OAuth")
        self._assert_not_revoked(metadata)
        return metadata

    def _oauth_config(
        self,
        metadata: ConnectionMetadata,
        *,
        redirect_uri: str | None = None,
    ) -> OAuthClientConfig:
        ConnectorAuthManager._oauth_allowed_scopes(metadata)
        client = metadata.client
        assert client.identifier is not None
        assert client.authorization_endpoint is not None
        assert client.token_endpoint is not None
        expected_endpoints = _PINNED_OAUTH_ENDPOINTS.get(metadata.provider)
        if (
            expected_endpoints is None
            or (
                client.authorization_endpoint,
                client.token_endpoint,
            )
            != expected_endpoints
        ):
            raise ValidationError("OAuth endpoints do not match a built-in provider")
        client_secret: str | None = None
        if metadata.provider == "google":
            registration = load_public_client_registration(metadata.provider)
            if registration.client_id == client.identifier and registration.client_secret_required:
                stored = self.secret_store.get_secret(
                    _oauth_client_secret_key(registration),
                    _OAUTH_CLIENT_SECRET_NAME,
                )
                if stored is None:
                    raise SetupError(
                        "Google sign-in needs its host-local OAuth client secret. Run "
                        "`gsv connectors client-secret set google` in an interactive terminal; "
                        "the value stays only in the OS keyring."
                    )
                client_secret = _oauth_client_secret_text(stored, stored=True)
        return OAuthClientConfig(
            authorization_endpoint=client.authorization_endpoint,
            token_endpoint=client.token_endpoint,
            client_id=client.identifier,
            client_secret=client_secret,
            redirect_uri=redirect_uri or client.redirect_uris[0],
            scopes=metadata.scopes,
            dialect={
                "google": OAuthDialect.GOOGLE,
                "microsoft": OAuthDialect.MICROSOFT,
                "slack": OAuthDialect.SLACK_USER,
            }.get(metadata.provider, OAuthDialect.STANDARD),
        )

    @staticmethod
    def _oauth_allowed_scopes(metadata: ConnectionMetadata) -> frozenset[str]:
        profile = get_profile_for_connection(
            metadata.provider,
            metadata.source_ids,
            metadata.scopes,
        )
        if profile.credential_kind is not CredentialKind.OAUTH2:
            raise ValidationError("connection provider does not have a built-in OAuth profile")
        allowed = profile.allowed_scopes
        configured = frozenset(metadata.scopes)
        profile.access_for_scopes(metadata.scopes)
        if not configured or not configured.issubset(allowed):
            raise ValidationError("OAuth scopes do not match a built-in access tier")
        return allowed

    @staticmethod
    def _validate_oauth_credential(
        metadata: ConnectionMetadata,
        credential: OAuthCredential,
    ) -> None:
        requires_refresh = metadata.provider == "google" or (
            metadata.provider == "microsoft"
            and any(
                scope.casefold() == "offline_access"
                for scope in canonicalize_microsoft_scopes(metadata.scopes)
            )
        )
        if requires_refresh and not credential.refresh_token:
            raise ValidationError("OAuth credential requires a non-empty refresh token")
        ConnectorAuthManager._oauth_allowed_scopes(metadata)
        canonical_scopes = (
            canonicalize_google_scopes(credential.scopes)
            if metadata.provider == "google"
            else (
                canonicalize_microsoft_scopes(credential.scopes)
                if metadata.provider == "microsoft"
                else credential.scopes
            )
        )
        granted = frozenset(canonical_scopes)
        configured = frozenset(
            canonicalize_microsoft_scopes(metadata.scopes)
            if metadata.provider == "microsoft"
            else metadata.scopes
        )
        if metadata.provider == "microsoft":
            configured_access = frozenset(canonicalize_microsoft_access_scopes(tuple(configured)))
            granted_access = frozenset(canonicalize_microsoft_access_scopes(tuple(granted)))
        else:
            configured_access = frozenset(
                scope for scope in configured if scope.casefold() != "offline_access"
            )
            granted_access = frozenset(
                scope for scope in granted if scope.casefold() != "offline_access"
            )
        profile = get_profile_for_connection(
            metadata.provider,
            metadata.source_ids,
            metadata.scopes,
        )
        if profile.name in {"gmail", "google"} and _GMAIL_PURGE_SCOPE in granted_access:
            granted_access |= {_GMAIL_MODIFY_SCOPE, _GMAIL_READ_SCOPE}
        configured_tier = profile.access_for_scopes(metadata.scopes)
        read_authority = ConnectorAuthManager._canonical_oauth_access_scopes(
            metadata.provider,
            (
                *profile.read_scopes,
                *(scope for bundle in profile.legacy_read_scopes for scope in bundle),
            ),
        )
        full_authority = read_authority | ConnectorAuthManager._canonical_oauth_access_scopes(
            metadata.provider,
            (
                *profile.full_scopes,
                *(scope for bundle in profile.legacy_full_scopes for scope in bundle),
            ),
        )
        purge_authority = full_authority | ConnectorAuthManager._canonical_oauth_access_scopes(
            metadata.provider,
            profile.supplemental_scopes,
        )
        grant_authority = purge_authority
        relevant_grant = granted_access
        if metadata.provider == "microsoft" and profile.name in {
            "outlook_mail",
            "outlook_calendar",
        }:
            provider_profile = get_profile("microsoft")
            grant_authority = ConnectorAuthManager._canonical_oauth_access_scopes(
                metadata.provider,
                (*provider_profile.read_scopes, *provider_profile.full_scopes),
            )
            # Microsoft returns the union of delegated Graph permissions already
            # granted to one app/account. Keep rejecting unknown provider grants,
            # but judge this logical connector only by the scopes its closed
            # operation surface can exercise.
            relevant_grant &= purge_authority
        elif metadata.provider == "slack" and profile.name == "slack":
            provider_profile = get_profile("slack")
            grant_authority = ConnectorAuthManager._canonical_oauth_access_scopes(
                metadata.provider,
                (*provider_profile.read_scopes, *provider_profile.full_scopes),
            )
            # Slack returns the union of workspace permissions already granted to
            # the app. Keep rejecting unknown provider grants, but judge this
            # connection only by the scopes its closed operation surface can exercise.
            relevant_surface = (
                read_authority if configured_tier is ConnectorAccessTier.READ else purge_authority
            )
            relevant_grant &= relevant_surface
        if not granted_access.issubset(grant_authority):
            raise OAuthPermissionGrantError("outside_selected_tier")
        if relevant_grant.issubset(read_authority):
            granted_capability = 0
        elif relevant_grant.issubset(full_authority):
            granted_capability = 1
        else:
            granted_capability = 2
        selected_capability = 0 if configured_tier is ConnectorAccessTier.READ else 1
        supplemental = ConnectorAuthManager._canonical_oauth_access_scopes(
            metadata.provider,
            profile.supplemental_scopes,
        )
        if configured_access & supplemental:
            selected_capability = 2
        if granted_capability > selected_capability:
            raise OAuthPermissionGrantError("outside_selected_tier")
        if not configured_access.issubset(granted_access):
            raise OAuthPermissionGrantError("missing_selected_permissions")

    @staticmethod
    def _canonical_oauth_access_scopes(
        provider: str,
        scopes: tuple[str, ...],
    ) -> frozenset[str]:
        scopes = tuple(dict.fromkeys(scopes))
        if provider == "google":
            canonical = canonicalize_google_scopes(scopes)
        elif provider == "microsoft":
            canonical = canonicalize_microsoft_access_scopes(scopes)
        else:
            canonical = scopes
        return frozenset(scope for scope in canonical if scope.casefold() != "offline_access")

    @staticmethod
    def _canonicalize_oauth_credential(
        metadata: ConnectionMetadata,
        credential: OAuthCredential,
    ) -> OAuthCredential:
        if metadata.provider == "google":
            scopes = canonicalize_google_scopes(credential.scopes)
        elif metadata.provider == "microsoft":
            scopes = canonicalize_microsoft_access_scopes(credential.scopes)
        else:
            return credential
        if scopes == credential.scopes:
            return credential
        return replace(credential, scopes=scopes)

    @staticmethod
    def _credential_binding(metadata: ConnectionMetadata) -> tuple[object, ...]:
        # Interactive acquisition/import pins the whole account record so a concurrent
        # alias edit restarts the operation instead of being overwritten at commit.
        return (
            metadata.provider,
            metadata.source_ids,
            metadata.credential_kind,
            metadata.account,
            metadata.scopes,
            metadata.client,
        )

    @staticmethod
    def _assert_not_revoked(metadata: ConnectionMetadata) -> None:
        if metadata.health is ConnectionHealth.REVOKED:
            raise ValidationError("connection is revoked")

    @staticmethod
    def _assert_verified_identity(metadata: ConnectionMetadata) -> None:
        if metadata.health in _VERIFIED_HEALTH and metadata.account.fingerprint is not None:
            return
        connection_id = metadata.connection_id
        if (
            metadata.health is ConnectionHealth.UNVERIFIED
            and metadata.account.fingerprint is not None
        ):
            raise ValidationError(
                f"connection identity is not confirmed; run `gsv connectors resume {connection_id}`"
            )
        if (
            metadata.health is ConnectionHealth.REAUTHORIZATION_REQUIRED
            and metadata.account.fingerprint is not None
        ):
            command = ConnectorAuthManager._reauthorization_command(metadata)
            raise ValidationError(f"connection requires its credential again; run `{command}`")
        reconnect = ConnectorAuthManager._new_account_command(metadata)
        if reconnect is None:
            raise ValidationError(
                "connection identity is not usable; inspect it with `gsv-auth status`"
            )
        raise ValidationError(
            "connection identity is not usable; run "
            f"`gsv connectors disconnect {connection_id}`, then `{reconnect}`"
        )

    def _host_availability(
        self,
        connection: ConnectionMetadata,
    ) -> tuple[str, CustodyStatus | None]:
        try:
            custody = self.inspect_custody(connection)
        except SetupError:
            return "backend_unavailable", None
        return (
            {
                "valid": "available",
                "missing": "missing",
                "invalid": "invalid",
                "pointer_invalid": "invalid",
            }[custody],
            custody,
        )

    @staticmethod
    def _connect_command(
        metadata: ConnectionMetadata,
        *,
        new_account: bool,
    ) -> str | None:
        try:
            profile = get_profile_for_connection(
                metadata.provider,
                metadata.source_ids,
                metadata.scopes,
            )
        except ValidationError:
            return None
        return connector_connect_command(
            profile,
            metadata.scopes,
            new_account=new_account,
        )

    @staticmethod
    def _new_account_command(metadata: ConnectionMetadata) -> str | None:
        return ConnectorAuthManager._connect_command(metadata, new_account=True)

    @staticmethod
    def _reauthorization_command(metadata: ConnectionMetadata) -> str:
        if metadata.credential_kind is CredentialKind.OAUTH2:
            return f"gsv connectors reauthorize {metadata.connection_id}"
        command = ConnectorAuthManager._connect_command(metadata, new_account=False)
        return command or "gsv-auth status"

    @staticmethod
    def _recovery_action(
        metadata: ConnectionMetadata,
        host_credential: str,
        custody: CustodyStatus | None,
    ) -> str | None:
        connection_id = metadata.connection_id
        if host_credential == "backend_unavailable":
            return (
                "Restore access to the approved OS keyring, then run "
                f"`gsv connectors status {connection_id}`."
            )
        if (
            metadata.health is ConnectionHealth.UNVERIFIED
            and metadata.account.fingerprint is not None
            and custody in {"valid", "missing"}
        ):
            return f"gsv connectors resume {connection_id}"
        if custody == "pointer_invalid" or (
            custody == "invalid" and metadata.health is ConnectionHealth.UNVERIFIED
        ):
            reconnect = ConnectorAuthManager._new_account_command(metadata)
            if reconnect is None:
                return "gsv-auth status"
            return f"gsv connectors disconnect {connection_id}; {reconnect}"
        if (
            metadata.health is ConnectionHealth.REAUTHORIZATION_REQUIRED
            and metadata.account.fingerprint is not None
        ) or (
            metadata.health in _VERIFIED_HEALTH
            and metadata.account.fingerprint is not None
            and host_credential != "available"
        ):
            return ConnectorAuthManager._reauthorization_command(metadata)
        if (
            metadata.health in _VERIFIED_HEALTH
            and metadata.account.fingerprint is not None
            and host_credential == "available"
        ):
            return None
        reconnect = ConnectorAuthManager._new_account_command(metadata)
        if reconnect is None:
            return "gsv-auth status"
        return f"gsv connectors disconnect {connection_id}; {reconnect}"

    def _mark_health(
        self,
        connection_id: ConnectionId,
        health: ConnectionHealth,
        *,
        observed_at: datetime,
        verified: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.vault.get_connection_snapshot()
        current = snapshot.connection(connection_id)
        if current is None:
            raise NotFoundError("connection was not found")
        if current.health is ConnectionHealth.REVOKED and health is not ConnectionHealth.REVOKED:
            raise ValidationError("revoked connection health cannot be restored")
        return self.vault.mark_connection_health(
            expected_revision=snapshot.revision,
            connection_id=connection_id,
            health=health,
            verified=verified,
            observed_at=observed_at,
        )


def _oauth_client_secret_key(registration: PublicClientRegistration) -> ConnectionId:
    """Fit one stable provider/client coordinate into the SecretStore key grammar."""

    material = (
        f"seld-oauth-client-secret-v1\x00{registration.provider}\x00{registration.client_id}"
    ).encode()
    token = base64.urlsafe_b64encode(hashlib.sha256(material).digest()[:24]).decode("ascii")
    return parse_connection_id(f"con-{token}")


def _oauth_client_secret_text(value: object, *, stored: bool) -> str:
    label = "stored OAuth client secret" if stored else "OAuth client secret"
    if not isinstance(value, bytes) or not value or len(value) > _MAX_OAUTH_CLIENT_SECRET_BYTES:
        raise ValidationError(f"{label} is invalid")
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded):
        raise ValidationError(f"{label} is invalid")
    return decoded
