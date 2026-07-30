"""Standalone connector-auth orchestration with no host-account dependency."""

from __future__ import annotations

import math
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Final
from urllib.parse import urlsplit

from continuity_kernel.config import connector_auth_dir
from continuity_kernel.connector_auth import (
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_credentials import (
    OAuthCredential,
    credential_from_token_set,
)
from continuity_kernel.connector_identifiers import ConnectionId, SecretStore, parse_connection_id
from continuity_kernel.connector_oauth import (
    OAuthClientConfig,
    OAuthTokenEndpointError,
    OAuthTransportError,
    exchange_authorization_code,
    refresh_access_token,
)
from continuity_kernel.connector_oauth_loopback import BoundLoopbackCallback, begin_authorization
from continuity_kernel.connector_secrets import KeyringSecretStore
from continuity_kernel.connector_token_store import AtomicTokenStore, ResolvedToken, TokenState
from continuity_kernel.errors import ConflictError, NotFoundError, SetupError, ValidationError
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

    def status(self) -> dict[str, Any]:
        """Project portable metadata plus redacted host-local availability."""

        portable = self.vault.connection_status()
        rows = portable["connections"]
        assert isinstance(rows, list)
        projected: list[dict[str, object]] = []
        for raw in rows:
            assert isinstance(raw, dict)
            row = dict(raw)
            connection_id = parse_connection_id(row["connection_id"])
            row["host_credential"] = self._host_availability(connection_id)
            projected.append(row)
        return {**portable, "connections": projected, "vault_id": self.vault_id}

    def store_credential(
        self,
        connection_id: ConnectionId | str,
        value: bytes,
        *,
        expected_token_version: int,
    ) -> TokenState:
        metadata = self._metadata(connection_id)
        if metadata.credential_kind is CredentialKind.OAUTH2:
            raise ValidationError("use the OAuth credential path for an OAuth connection")
        return self.tokens.update(
            metadata.connection_id,
            expected_version=expected_token_version,
            value=value,
        )

    def store_oauth_credential(
        self,
        connection_id: ConnectionId | str,
        credential: OAuthCredential,
        *,
        expected_token_version: int,
    ) -> TokenState:
        metadata = self._oauth_metadata(connection_id)
        return self.tokens.update(
            metadata.connection_id,
            expected_version=expected_token_version,
            value=credential.to_bytes(),
            updated_at=credential.issued_at,
        )

    def resolve_credential(self, connection_id: ConnectionId | str) -> bytes:
        """Resolve only inside a connector runtime; never expose through MCP or status."""

        return self.resolve_credential_state(connection_id).value

    def resolve_credential_state(
        self,
        connection_id: ConnectionId | str,
    ) -> ResolvedToken:
        """Resolve runtime-only bytes with the pointer state needed for operation CAS."""

        metadata = self._metadata(connection_id)
        if metadata.credential_kind is CredentialKind.OAUTH2:
            raise ValidationError("use resolve_oauth_access_token for an OAuth connection")
        return self.tokens.read(metadata.connection_id)

    def resolve_oauth_access_token(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_connection_revision: str | None = None,
        minimum_validity_seconds: int = 60,
        timeout_seconds: float = 15.0,
        observed_at: datetime | None = None,
    ) -> str:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValidationError("OAuth timeout must be a positive finite number")
        metadata = self._oauth_metadata(
            connection_id,
            expected_revision=expected_connection_revision,
        )
        config = self._oauth_config(metadata)
        now = (observed_at or datetime.now(UTC)).astimezone(UTC)
        deadline = monotonic() + timeout_seconds

        def remaining() -> float:
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise OAuthTransportError("OAuth credential resolution timed out")
            return seconds

        def refresh_if_needed(resolved: ResolvedToken) -> bytes | None:
            previous = OAuthCredential.from_bytes(resolved.value)
            if previous.usable_at(
                now,
                minimum_validity_seconds=minimum_validity_seconds,
            ):
                return None
            if previous.refresh_token is None:
                raise ValidationError("OAuth connection requires reauthorization")
            token_set = refresh_access_token(
                config,
                refresh_token=previous.refresh_token,
                scopes=previous.scopes,
                timeout_seconds=remaining(),
            )
            return credential_from_token_set(
                token_set,
                issued_at=now,
                previous=previous,
            ).to_bytes()

        try:
            resolved = self.tokens.refresh_serialized(
                metadata.connection_id,
                transform=refresh_if_needed,
                lock_timeout_seconds=timeout_seconds,
                updated_at=now,
            )
        except OAuthTokenEndpointError as exc:
            health = (
                ConnectionHealth.REAUTHORIZATION_REQUIRED
                if exc.error == "invalid_grant"
                else ConnectionHealth.DEGRADED
            )
            self._mark_health(metadata.connection_id, health, observed_at=now)
            raise
        credential = OAuthCredential.from_bytes(resolved.value)
        return credential.access_token

    def authorize_oauth(
        self,
        connection_id: ConnectionId | str,
        *,
        timeout_seconds: float = 180.0,
    ) -> None:
        """Run one interactive native authorization and persist its secret result."""

        metadata = self._oauth_metadata(connection_id)
        template = urlsplit(metadata.client.redirect_uris[0])
        host = template.hostname
        if host not in {"127.0.0.1", "::1"}:
            raise ValidationError("OAuth redirect template must use a loopback IP")
        listener = BoundLoopbackCallback.bind(
            host=host,
            port=template.port or 0,
            path=template.path,
        )
        try:
            config = self._oauth_config(metadata, redirect_uri=listener.redirect_uri)
            attempt = begin_authorization(config)
            listener.configure(config, attempt)
            if not webbrowser.open(attempt.authorization_url):
                raise SetupError("the OAuth authorization page could not be opened")
            code = listener.wait_for_code(timeout_seconds=timeout_seconds)
            token_set = exchange_authorization_code(
                config,
                authorization_code=code,
                code_verifier=attempt.code_verifier,
                timeout_seconds=min(timeout_seconds, 30.0),
            )
        finally:
            listener.close()
        issued_at = datetime.now(UTC)
        credential = credential_from_token_set(token_set, issued_at=issued_at)
        current = self.tokens.state(metadata.connection_id)
        self.store_oauth_credential(
            metadata.connection_id,
            credential,
            expected_token_version=current.version if current else 0,
        )
        self._mark_health(
            metadata.connection_id,
            ConnectionHealth.UNVERIFIED,
            observed_at=issued_at,
        )

    def mark_verified(self, connection_id: ConnectionId | str) -> dict[str, Any]:
        return self._mark_health(
            parse_connection_id(connection_id),
            ConnectionHealth.READY,
            observed_at=datetime.now(UTC),
            verified=True,
        )

    def remove(
        self,
        connection_id: ConnectionId | str,
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        clean_id = parse_connection_id(connection_id)
        self.tokens.delete(clean_id)
        return self.vault.remove_connection(
            expected_revision=expected_revision,
            connection_id=clean_id,
        )

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
        return metadata

    @staticmethod
    def _oauth_config(
        metadata: ConnectionMetadata,
        *,
        redirect_uri: str | None = None,
    ) -> OAuthClientConfig:
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
        return OAuthClientConfig(
            authorization_endpoint=client.authorization_endpoint,
            token_endpoint=client.token_endpoint,
            client_id=client.identifier,
            redirect_uri=redirect_uri or client.redirect_uris[0],
            scopes=metadata.scopes,
        )

    def _host_availability(self, connection_id: ConnectionId) -> str:
        try:
            self.tokens.read(connection_id)
        except NotFoundError:
            return "missing"
        except SetupError:
            return "backend_unavailable"
        except (OSError, ValidationError):
            return "invalid"
        return "available"

    def _mark_health(
        self,
        connection_id: ConnectionId,
        health: ConnectionHealth,
        *,
        observed_at: datetime,
        verified: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.vault.get_connection_snapshot()
        return self.vault.mark_connection_health(
            expected_revision=snapshot.revision,
            connection_id=connection_id,
            health=health,
            verified=verified,
            observed_at=observed_at,
        )
