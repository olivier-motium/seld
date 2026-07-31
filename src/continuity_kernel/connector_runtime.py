"""Interactive connector authorization, confirmation, and adapter runtime."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping
from typing import Final, cast

from continuity_kernel.connector_adapter import (
    ConnectorAdapterRegistry,
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
)
from continuity_kernel.connector_auth import ConnectionHealth, CredentialKind
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_contract import (
    ConnectorEffect,
    ConnectorMode,
    canonical_json_digest,
    validate_json,
)
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_operations import (
    CONNECTOR_TOOL_BINDINGS,
    OPERATION_CATALOG,
)
from continuity_kernel.connector_profiles import ConnectorAccessTier, get_profile_for_connection
from continuity_kernel.connector_session import DEFAULT_TTL_SECONDS, ConnectorSession
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorTransport,
)
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    NotFoundError,
    ValidationError,
)
from continuity_kernel.vault import Vault

_PROFILE_PROVIDERS: Final = {
    "discord": "discord",
    "gmail": "google",
    "google_calendar": "google",
    "google_drive": "google",
    "outlook_calendar": "microsoft",
    "outlook_mail": "microsoft",
    "slack": "slack",
}
_EFFECT_ORDER: Final = {
    ConnectorEffect.READ: 0,
    ConnectorEffect.SAFE_MUTATION: 1,
    ConnectorEffect.OUTWARD: 2,
    ConnectorEffect.DESTRUCTIVE: 3,
    ConnectorEffect.PERMANENT: 4,
}
_PREVIEW_TEXT_CHARS: Final = 2_000


class ConnectorRuntime:
    """Execute closed connector operations against one exact live connection."""

    def __init__(
        self,
        vault: Vault,
        *,
        adapters: ConnectorAdapterRegistry,
        auth_manager: ConnectorAuthManager | None = None,
        transport: ConnectorTransport | None = None,
        session: ConnectorSession | None = None,
    ) -> None:
        self.vault = vault
        self.auth_manager = auth_manager or ConnectorAuthManager(vault)
        self.adapters = adapters
        self.transport = transport or ConnectorTransport()
        self.session = session or ConnectorSession()

    def call_tool(self, name: str, values: Mapping[str, object]) -> dict[str, object]:
        try:
            provider, mode = CONNECTOR_TOOL_BINDINGS[name]
        except KeyError as exc:
            raise ValidationError("unknown connector tool") from exc
        validated = validate_json(
            dict(values),
            OPERATION_CATALOG.tool_input_schema(provider, mode),
        )
        envelope = cast(dict[str, object], validated)
        operation_name = cast(str, envelope["operation"])
        operation = OPERATION_CATALOG.lookup(provider, mode, operation_name)
        input_value = operation.validate_input(envelope["input"])
        connection_id = cast(str, envelope["connection_id"])

        connection_snapshot = self.vault.get_connection_snapshot()
        connection = connection_snapshot.connection(connection_id)
        if connection is None:
            raise NotFoundError("connection was not found")
        profile_provider = _PROFILE_PROVIDERS[provider]
        if connection.provider != profile_provider or provider not in connection.source_ids:
            raise ValidationError("connection does not authorize this connector")
        if connection.health not in {ConnectionHealth.READY, ConnectionHealth.DEGRADED}:
            raise ValidationError("connection must be verified before interactive use")
        profile = get_profile_for_connection(
            connection.provider,
            connection.source_ids,
            connection.scopes,
        )
        access = profile.access_for_scopes(connection.scopes)
        if mode is ConnectorMode.WRITE and access is not ConnectorAccessTier.FULL:
            raise ValidationError(
                "this connection is Read-only; connect Full access before making changes"
            )

        credential = self._resolve_credential(
            connection_id=connection_id,
            expected_connection_revision=connection_snapshot.revision,
            credential_kind=connection.credential_kind,
            configured_scopes=connection.scopes,
        )
        if not operation.scope_grant_satisfies(credential.granted_scopes):
            raise ValidationError(_scope_error(provider, operation_name))
        if self.vault.get_connection_snapshot().revision != connection_snapshot.revision:
            raise ConflictError("connection changed while its credential was being resolved")

        adapter = self.adapters.get(provider)
        effect = _classified_effect(
            operation.effect,
            adapter.classify_effect(operation, input_value),
        )
        continuation: object | None = None
        write_idempotency_key: str | None = None
        if mode is ConnectorMode.READ:
            cursor = envelope.get("cursor")
            if cursor is not None:
                continuation = self.session.open_cursor(
                    cursor,
                    provider=provider,
                    operation=operation_name,
                    connection_id=connection_id,
                    input_value=input_value,
                    connection_revision=connection_snapshot.revision,
                    credential_version=credential.version,
                )
        else:
            confirmation = envelope.get("confirmation_token")
            if effect is ConnectorEffect.SAFE_MUTATION:
                if confirmation is not None:
                    raise ValidationError("this operation does not use a confirmation token")
            elif confirmation is None:
                return self._confirmation_preview(
                    provider=provider,
                    operation=operation_name,
                    connection_id=connection_id,
                    account_label=connection.account.label,
                    input_value=input_value,
                    effect=effect,
                    access=access,
                    granted_scopes=credential.granted_scopes,
                    connection_version=connection.version,
                    credential_version=credential.version,
                )
            else:
                write_idempotency_key = self.session.consume_confirmation(
                    confirmation,
                    provider=provider,
                    operation=operation_name,
                    connection_id=connection_id,
                    effect=effect,
                    authorization_tier=access.value,
                    granted_scopes=credential.granted_scopes,
                    mutation=input_value,
                    connection_version=connection.version,
                    credential_version=credential.version,
                )

        result = adapter.execute(
            operation,
            input_value,
            continuation=continuation,
            credential=credential,
            transport=self.transport,
            write_idempotency_key=write_idempotency_key,
        )
        if not isinstance(result, ConnectorAdapterResult):
            raise ValidationError("connector adapter returned an invalid result")
        state_changed = self._state_changed(
            connection_id,
            connection_revision=connection_snapshot.revision,
            credential_version=credential.version,
        )
        if state_changed and mode is ConnectorMode.READ:
            raise ConflictError("connection changed during the provider read")

        response: dict[str, object] = {
            "connection_id": connection_id,
            "effect": effect.value,
            "operation": operation_name,
            "provider": provider,
            "result": result.payload,
            "status": "completed_state_changed" if state_changed else "ok",
        }
        if state_changed:
            response["do_not_retry"] = True
            response["warning"] = (
                "The provider accepted the change, but the local connection changed afterward. "
                "Review provider state before another write."
            )
        if mode is ConnectorMode.READ and result.continuation is not None:
            response["cursor"] = self.session.issue_cursor(
                provider=provider,
                operation=operation_name,
                connection_id=connection_id,
                input_value=input_value,
                connection_revision=connection_snapshot.revision,
                credential_version=credential.version,
                continuation=result.continuation,
            )
        return response

    def _resolve_credential(
        self,
        *,
        connection_id: str,
        expected_connection_revision: str,
        credential_kind: CredentialKind,
        configured_scopes: tuple[str, ...],
    ) -> ConnectorRuntimeCredential:
        if credential_kind is CredentialKind.OAUTH2:
            resolved = self.auth_manager.resolve_oauth_access_token_state(
                connection_id,
                expected_connection_revision=expected_connection_revision,
            )
            return ConnectorRuntimeCredential(
                credential=ConnectorCredential(
                    scheme=AuthorizationScheme.BEARER,
                    secret=resolved.access_token,
                ),
                granted_scopes=resolved.scopes,
                version=resolved.state.version,
            )
        if credential_kind is CredentialKind.BEARER:
            resolved_bearer = self.auth_manager.resolve_credential_state(connection_id)
            try:
                secret = resolved_bearer.value.decode("utf-8")
            except UnicodeError as exc:
                raise ValidationError("bot credential is invalid") from exc
            return ConnectorRuntimeCredential(
                credential=ConnectorCredential(
                    scheme=AuthorizationScheme.BOT,
                    secret=secret,
                ),
                granted_scopes=configured_scopes,
                version=resolved_bearer.state.version,
            )
        raise ValidationError("connection credential kind is unsupported for interactive use")

    def _confirmation_preview(
        self,
        *,
        provider: str,
        operation: str,
        connection_id: str,
        account_label: str | None,
        input_value: object,
        effect: ConnectorEffect,
        access: ConnectorAccessTier,
        granted_scopes: tuple[str, ...],
        connection_version: int,
        credential_version: int,
    ) -> dict[str, object]:
        token = self.session.issue_confirmation(
            provider=provider,
            operation=operation,
            connection_id=connection_id,
            effect=effect,
            authorization_tier=access.value,
            granted_scopes=granted_scopes,
            mutation=input_value,
            connection_version=connection_version,
            credential_version=credential_version,
        )
        return {
            "account": account_label or "Verified account",
            "confirmation_token": token,
            "effect": effect.value,
            "expires_in_seconds": DEFAULT_TTL_SECONDS,
            "mutation_digest": canonical_json_digest(input_value),
            "operation": operation,
            "preview": _preview_value(input_value),
            "provider": provider,
            "status": "confirmation_required",
            "warning": _effect_warning(effect),
        }

    def _state_changed(
        self,
        connection_id: str,
        *,
        connection_revision: str,
        credential_version: int,
    ) -> bool:
        try:
            if self.vault.get_connection_snapshot().revision != connection_revision:
                return True
            current = self.auth_manager.tokens.state(parse_connection_id(connection_id))
            return current is None or current.version != credential_version
        except (ContinuityError, OSError):
            return True


def default_connector_adapters() -> ConnectorAdapterRegistry:
    """Build the finite provider registry without importing it into the Pulse lane."""

    from continuity_kernel.connector_adapter_collaboration import (
        CollaborationConnectorAdapter,
    )
    from continuity_kernel.connector_adapter_google import GoogleConnectorAdapter
    from continuity_kernel.connector_adapter_microsoft import MicrosoftConnectorAdapter

    return ConnectorAdapterRegistry(
        (
            GoogleConnectorAdapter(),
            MicrosoftConnectorAdapter(),
            CollaborationConnectorAdapter(),
        )
    )


def _classified_effect(
    catalog_effect: ConnectorEffect,
    adapter_effect: ConnectorEffect,
) -> ConnectorEffect:
    if not isinstance(adapter_effect, ConnectorEffect):
        raise ValidationError("connector adapter classified an invalid effect")
    if _EFFECT_ORDER[adapter_effect] < _EFFECT_ORDER[catalog_effect]:
        raise ValidationError("connector adapter cannot downgrade a catalog effect")
    return adapter_effect


def _scope_error(provider: str, operation: str) -> str:
    if provider == "gmail" and operation in {"messages.purge", "threads.purge"}:
        return (
            "Gmail permanent delete is not enabled; reconnect Full access with the separate "
            "permanent-delete permission"
        )
    return "the provider did not grant the permission required for this operation"


def _effect_warning(effect: ConnectorEffect) -> str:
    if effect is ConnectorEffect.PERMANENT:
        return "This cannot be undone. Review the exact target before confirming."
    if effect is ConnectorEffect.DESTRUCTIVE:
        return (
            "This removes or archives provider content. Review the exact target before confirming."
        )
    return "This affects other people or sends data outward. Review it before confirming."


def _preview_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _preview_field(key, item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_preview_value(item) for item in value]
    if isinstance(value, str) and len(value) > _PREVIEW_TEXT_CHARS:
        return {
            "characters": len(value),
            "digest": "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "preview": value[:_PREVIEW_TEXT_CHARS],
            "truncated": True,
        }
    return value


def _preview_field(name: str, value: object) -> object:
    if name == "content_base64" and isinstance(value, str):
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            decoded = value.encode("utf-8")
        return {
            "bytes": len(decoded),
            "digest": "sha256:" + hashlib.sha256(decoded).hexdigest(),
            "omitted": True,
        }
    return _preview_value(value)
