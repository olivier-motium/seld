"""Interactive connector authorization, confirmation, and adapter runtime."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from inspect import Parameter, signature
from threading import RLock
from typing import Final, cast

from continuity_kernel.connector_adapter import (
    ConnectorAdapter,
    ConnectorAdapterRegistry,
    ConnectorAdapterResult,
    ConnectorPreparedConfirmationPreviewAdapter,
    ConnectorProviderUploadLimitAdapter,
    ConnectorRuntimeCredential,
    ConnectorTransferAdapter,
    ConnectorTransferContext,
    ConnectorUploadLimitAdapter,
)
from continuity_kernel.connector_auth import ConnectionHealth, CredentialKind
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_contract import (
    ConnectorEffect,
    ConnectorMode,
    OperationSpec,
    canonical_json,
    canonical_json_digest,
    canonicalize_json,
    validate_json,
)
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_operations import (
    CONNECTOR_TOOL_BINDINGS,
    OPERATION_CATALOG,
)
from continuity_kernel.connector_profiles import ConnectorAccessTier, get_profile_for_connection
from continuity_kernel.connector_session import DEFAULT_TTL_SECONDS, ConnectorSession
from continuity_kernel.connector_transfer import (
    ArtifactStore,
    ConnectorArtifactScope,
    PreparedUploadBinding,
    PreparedUploadBundle,
    PreparedUploadCache,
    PreparedUploadReservation,
)
from continuity_kernel.connector_transport import (
    SLACK_AUTH_FAILURE_CODES,
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorOrigin,
    ConnectorProviderError,
    ConnectorTransport,
)
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    NotFoundError,
    ValidationError,
)
from continuity_kernel.local_files import (
    MAX_FILE_TRANSFER_BYTES,
    LocalFileAuthorityUse,
    LocalFileGrantStore,
    LocalFileTransferCandidate,
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
_PREPARED_CONTENT_KEY: Final = "prepared_content"
_PREPARED_PREVIEW_MAX_BYTES: Final = 16 * 1_024
_LOCAL_FILE_LIMIT_MARKER: Final = "opaque-local-file"
_OPERATION_WARNINGS: Final = {
    (
        "gmail",
        "settings.filters.create",
    ): "This rule will apply automatically to future matching messages.",
    (
        "gmail",
        "settings.filters.delete",
    ): "Gmail has no restore operation for a deleted filter.",
    (
        "gmail",
        "settings.imap.update",
    ): (
        "Review enabled, auto-expunge, and expunge behavior; deleteForever makes future IMAP "
        "expunges unrecoverable."
    ),
    (
        "gmail",
        "settings.pop.update",
    ): "This changes how future POP retrieval affects messages.",
    (
        "gmail",
        "settings.send_as.patch",
    ): "These primary identity settings affect future outgoing messages.",
    (
        "gmail",
        "settings.vacation.update",
    ): "Gmail will send this reply automatically to future qualifying messages.",
    (
        "google_calendar",
        "calendars.delete",
    ): "Deleting an owned secondary calendar permanently removes it for everyone.",
    (
        "google_calendar",
        "calendars.update",
    ): "Calendar metadata can be visible to people who share this calendar.",
    (
        "google_calendar",
        "events.delete",
    ): (
        "Deleting an organized event cancels it for guests. Google usually keeps deleted "
        "events in Trash temporarily, but recurring-event recovery differs; send_updates=none "
        "can leave external calendars stale and may not suppress every email. Deleting focus-time "
        "or out-of-office events does not reverse invitations Google already declined."
    ),
    (
        "google_calendar",
        "events.create",
    ): (
        "If attendees are included, Google may send invitations; send_updates=none can leave "
        "external calendars stale and may not suppress every email. Working-location events "
        "are public. Focus-time and out-of-office auto-decline rules can respond to conflicting "
        "invitations, and a focus-time Chat status affects Google Chat and related products."
    ),
    (
        "google_calendar",
        "events.move",
    ): (
        "Moving an event changes its organizer; send_updates=none can leave external calendars "
        "stale and may not suppress every email."
    ),
    (
        "google_calendar",
        "events.respond",
    ): "The organizer can see this RSVP even when notification emails are suppressed.",
    (
        "google_calendar",
        "events.update",
    ): (
        "Supplying attendees, attachments, or recurrence replaces that complete array; a null "
        "extended-property value deletes that entry, and a new Meet request can replace existing "
        "conference data. send_updates=none can leave external calendars stale and may not "
        "suppress every email. Working-location changes are public. Focus-time and out-of-office "
        "auto-decline rules can respond to conflicting invitations, and a focus-time Chat status "
        "affects Google Chat and related products."
    ),
    (
        "outlook_calendar",
        "events.cancel",
    ): "Outlook will email event attendees a cancellation notice.",
    (
        "outlook_calendar",
        "events.delete",
    ): ("If this is an organized meeting, Outlook will email attendees a cancellation notice."),
}


@dataclass(frozen=True)
class _PreparedInput:
    adapter_input: object
    confirmation_input: object
    preview_input: object
    bundle: PreparedUploadBundle | None


@dataclass(frozen=True)
class _LocalFileSelector:
    path: tuple[str | int, ...]
    grant_id: str
    relative_path: str


class _PreparedInputAssembly:
    """Close partial snapshots safely while one registered authority is cancellable."""

    def __init__(self) -> None:
        self.bindings: list[PreparedUploadBinding] = []
        self.reservations: list[PreparedUploadReservation] = []
        self._authority: LocalFileAuthorityUse | None = None
        self._bundle: PreparedUploadBundle | None = None
        self._closed = False
        self._lock = RLock()

    def bind_authority(self, authority: LocalFileAuthorityUse) -> None:
        with self._lock:
            if self._closed:
                authority.close()
                raise ConflictError("local-file upload authority was revoked")
            self._authority = authority

    def add_reservation(self, reservation: PreparedUploadReservation) -> None:
        with self._lock:
            if self._closed:
                reservation.release()
                raise ConflictError("local-file upload authority was revoked")
            self.reservations.append(reservation)

    def add_binding(self, binding: PreparedUploadBinding) -> None:
        with self._lock:
            if self._closed:
                binding.upload.close()
                raise ConflictError("local-file upload authority was revoked")
            self.bindings.append(binding)

    def finish(self) -> PreparedUploadBundle:
        with self._lock:
            if self._closed or self._authority is None:
                raise ConflictError("local-file upload authority was revoked")
            bundle = PreparedUploadBundle(
                self.bindings,
                reservations=self.reservations,
            )
            bundle.bind_authority(self._authority)
            self._bundle = bundle
            return bundle

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            bundle = self._bundle
            authority = self._authority
            bindings = tuple(self.bindings)
            reservations = tuple(self.reservations)
        if bundle is not None:
            bundle.close()
            return
        for binding in bindings:
            binding.upload.close()
        for reservation in reservations:
            reservation.release()
        if authority is not None:
            authority.close()


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
        prepared_uploads: PreparedUploadCache | None = None,
        artifact_store: ArtifactStore | None = None,
        local_files: LocalFileGrantStore | None = None,
    ) -> None:
        self.vault = vault
        self.auth_manager = auth_manager or ConnectorAuthManager(vault)
        self.adapters = adapters
        self.transport = transport or ConnectorTransport()
        self.session = session or ConnectorSession()
        self.prepared_uploads = prepared_uploads or PreparedUploadCache()
        self._artifact_store_instance = artifact_store
        self._local_file_store = local_files
        self._closed = False

    def call_tool(self, name: str, values: Mapping[str, object]) -> dict[str, object]:
        if self._closed:
            raise ValidationError("connector runtime is closed")
        self.prepared_uploads.prune()
        try:
            return self._call_tool(name, values)
        finally:
            self.prepared_uploads.prune()

    def close(self) -> None:
        """Release process-local snapshots while preserving completed artifacts."""

        if self._closed:
            return
        self._closed = True
        self.prepared_uploads.close()
        if self._artifact_store_instance is not None:
            self._artifact_store_instance.close()

    def _call_tool(self, name: str, values: Mapping[str, object]) -> dict[str, object]:
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
        continuation: object | None = None
        if mode is ConnectorMode.READ:
            effect = self._classify_effect(
                operation.effect,
                adapter,
                operation,
                input_value,
                connection_id=connection_id,
                connection_revision=connection_snapshot.revision,
                credential=credential,
            )
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
            result = self._execute_adapter(
                adapter,
                operation,
                input_value,
                continuation=continuation,
                credential=credential,
                write_idempotency_key=None,
                prepared_bundle=None,
                connection_id=connection_id,
                connection_revision=connection_snapshot.revision,
            )
        else:
            confirmation = envelope.get("confirmation_token")
            if confirmation is None:
                prepared = self._prepare_input(
                    input_value,
                    adapter=adapter,
                    operation=operation,
                    connection_id=connection_id,
                    connection_revision=connection_snapshot.revision,
                    credential=credential,
                )
                preview_bundle = prepared.bundle
                try:
                    effect = self._classify_effect(
                        operation.effect,
                        adapter,
                        operation,
                        prepared.adapter_input,
                        connection_id=connection_id,
                        connection_revision=connection_snapshot.revision,
                        credential=credential,
                        transfer=_prepared_transfer_context(prepared.bundle),
                    )
                    effect = _promote_prepared_upload_effect(effect, prepared.bundle)
                    if effect is ConnectorEffect.SAFE_MUTATION:
                        result = self._execute_adapter(
                            adapter,
                            operation,
                            prepared.adapter_input,
                            continuation=None,
                            credential=credential,
                            write_idempotency_key=None,
                            prepared_bundle=preview_bundle,
                            connection_id=connection_id,
                            connection_revision=connection_snapshot.revision,
                        )
                    else:
                        preview_value = prepared.preview_input
                        if preview_bundle is not None:
                            preview_value = _prepared_confirmation_preview(
                                adapter,
                                operation,
                                preview_value,
                                bundle=preview_bundle,
                            )
                        preview = self._confirmation_preview(
                            provider=provider,
                            operation=operation_name,
                            connection_id=connection_id,
                            account_label=connection.account.label,
                            mutation_value=prepared.confirmation_input,
                            preview_value=preview_value,
                            effect=effect,
                            access=access,
                            granted_scopes=credential.granted_scopes,
                            connection_version=connection.version,
                            credential_version=credential.version,
                        )
                        if preview_bundle is not None:
                            token = cast(str, preview["confirmation_token"])
                            self.prepared_uploads.put(token, preview_bundle)
                            preview_bundle = None
                        return preview
                finally:
                    if preview_bundle is not None:
                        preview_bundle.close()
            else:
                with self.prepared_uploads.hold(confirmation) as cached:
                    if cached is None and _contains_local_file(input_value):
                        raise ConflictError(
                            "prepared upload is unavailable or expired; request a fresh preview"
                        )
                    prepared = (
                        self._bind_prepared_input(input_value, cached)
                        if cached is not None
                        else _PreparedInput(
                            adapter_input=input_value,
                            confirmation_input=input_value,
                            preview_input=input_value,
                            bundle=None,
                        )
                    )
                    if cached is not None and cached.closed:
                        self.prepared_uploads.discard(confirmation)
                        raise ConflictError(
                            "prepared upload authority changed; request a fresh preview"
                        )
                    effect = self._classify_effect(
                        operation.effect,
                        adapter,
                        operation,
                        prepared.adapter_input,
                        connection_id=connection_id,
                        connection_revision=connection_snapshot.revision,
                        credential=credential,
                        transfer=_prepared_transfer_context(prepared.bundle),
                    )
                    effect = _promote_prepared_upload_effect(effect, prepared.bundle)
                    if effect is ConnectorEffect.SAFE_MUTATION:
                        raise ValidationError("this operation does not use a confirmation token")
                    write_idempotency_key = self.session.consume_confirmation(
                        confirmation,
                        provider=provider,
                        operation=operation_name,
                        connection_id=connection_id,
                        effect=effect,
                        authorization_tier=access.value,
                        granted_scopes=credential.granted_scopes,
                        mutation=prepared.confirmation_input,
                        connection_version=connection.version,
                        credential_version=credential.version,
                    )
                    confirmed_bundle: PreparedUploadBundle | None = None
                    try:
                        if cached is not None:
                            confirmed_bundle = self.prepared_uploads.take(
                                confirmation,
                                expected=cached,
                            )
                        result = self._execute_adapter(
                            adapter,
                            operation,
                            prepared.adapter_input,
                            continuation=None,
                            credential=credential,
                            write_idempotency_key=write_idempotency_key,
                            prepared_bundle=confirmed_bundle,
                            connection_id=connection_id,
                            connection_revision=connection_snapshot.revision,
                        )
                    finally:
                        if confirmed_bundle is not None:
                            confirmed_bundle.close()
        if not isinstance(result, ConnectorAdapterResult):
            raise ValidationError("connector adapter returned an invalid result")
        try:
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
            if result.artifact is not None:
                response["artifact"] = result.artifact.to_dict()
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
        except Exception:
            if result.artifact is not None:
                with suppress(Exception):
                    self._artifacts().discard(result.artifact)
            raise

    def _classify_effect(
        self,
        catalog_effect: ConnectorEffect,
        adapter: ConnectorAdapter,
        operation: OperationSpec,
        input_value: object,
        *,
        connection_id: str,
        connection_revision: str,
        credential: ConnectorRuntimeCredential,
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorEffect:
        try:
            adapter_effect = _classify_adapter_effect(
                adapter,
                operation,
                input_value,
                credential=credential,
                transport=self.transport,
                transfer=transfer,
            )
        except ConnectorProviderError as exc:
            self._project_auth_failure(
                exc,
                connection_id=connection_id,
                connection_revision=connection_revision,
                credential_version=credential.version,
            )
            raise
        return _classified_effect(catalog_effect, adapter_effect)

    def _prepare_input(
        self,
        input_value: object,
        *,
        adapter: ConnectorAdapter,
        operation: OperationSpec,
        connection_id: str,
        connection_revision: str,
        credential: ConnectorRuntimeCredential,
    ) -> _PreparedInput:
        selectors = _local_file_selectors(input_value)
        if not selectors:
            clean = canonicalize_json(input_value)
            return _PreparedInput(
                adapter_input=clean,
                confirmation_input=clean,
                preview_input=clean,
                bundle=None,
            )

        grant_ids = tuple(sorted({selector.grant_id for selector in selectors}))
        self._assert_grant_authority(set(grant_ids))
        limit_input = _sanitize_upload_limit_input(input_value)
        try:
            limits = {
                selector.path: _adapter_upload_limit(
                    adapter,
                    operation,
                    limit_input,
                    path=selector.path,
                    credential=credential,
                    transport=self.transport,
                )
                for selector in selectors
            }
        except ConnectorProviderError as exc:
            self._project_auth_failure(
                exc,
                connection_id=connection_id,
                connection_revision=connection_revision,
                credential_version=credential.version,
            )
            raise
        assembly = _PreparedInputAssembly()
        authority = self._local_files().register_transfer_authority(
            grant_ids,
            invalidator=assembly.close,
        )
        assembly.bind_authority(authority)

        candidates: dict[tuple[str | int, ...], LocalFileTransferCandidate] = {}
        try:
            for selector in selectors:
                candidates[selector.path] = self._local_files().inspect_file_candidate(
                    selector.grant_id,
                    selector.relative_path,
                    authority=authority,
                    max_bytes=limits[selector.path],
                )
            reservation = self.prepared_uploads.reserve(
                sum(candidate.size for candidate in candidates.values())
            )
            assembly.add_reservation(reservation)
        except Exception:
            assembly.close()
            raise

        def prepare(
            marker: object,
            parent: Mapping[str, object],
            path: tuple[str | int, ...],
        ) -> PreparedUploadBinding:
            grant_id, relative_path = _local_file_selector(marker)
            candidate = candidates.get(path)
            if (
                candidate is None
                or candidate.grant_id != grant_id
                or candidate.relative_path != relative_path
            ):
                raise ConflictError("local-file upload input changed while it was prepared")
            upload = self._artifacts().prepare_upload(
                candidate,
                authority=authority,
                filename=_upload_filename(parent),
                media_type=_upload_media_type(parent),
                max_bytes=limits[path],
            )
            binding = PreparedUploadBinding(
                path=path,
                selector_digest=canonical_json_digest(marker),
                grant_id=grant_id,
                upload=upload,
            )
            assembly.add_binding(binding)
            return binding

        try:
            adapter_input, confirmation_input, preview_input = _transform_local_files(
                input_value,
                prepare,
            )
            bundle = assembly.finish()
            self._assert_upload_authority(bundle)
            return _PreparedInput(
                adapter_input=canonicalize_json(adapter_input),
                confirmation_input=canonicalize_json(confirmation_input),
                preview_input=canonicalize_json(preview_input),
                bundle=bundle,
            )
        except Exception:
            assembly.close()
            raise

    def _bind_prepared_input(
        self,
        input_value: object,
        bundle: PreparedUploadBundle,
    ) -> _PreparedInput:
        by_path = {binding.path: binding for binding in bundle.bindings}
        seen: set[tuple[str | int, ...]] = set()

        def bind(
            marker: object,
            _parent: Mapping[str, object],
            path: tuple[str | int, ...],
        ) -> PreparedUploadBinding:
            binding = by_path.get(path)
            if binding is None or canonical_json_digest(marker) != binding.selector_digest:
                raise ConflictError("prepared upload binding does not match the reviewed mutation")
            seen.add(path)
            return binding

        adapter_input, confirmation_input, preview_input = _transform_local_files(
            input_value,
            bind,
        )
        if seen != set(by_path):
            raise ConflictError("prepared upload binding does not match the reviewed mutation")
        return _PreparedInput(
            adapter_input=canonicalize_json(adapter_input),
            confirmation_input=canonicalize_json(confirmation_input),
            preview_input=canonicalize_json(preview_input),
            bundle=bundle,
        )

    def _assert_upload_authority(self, bundle: PreparedUploadBundle) -> None:
        self._assert_grant_authority({binding.grant_id for binding in bundle.bindings})

    def _assert_grant_authority(self, grant_ids: set[str]) -> None:
        state = self.vault.list_local_file_grants()
        if state.get("source_selected") is not True:
            raise ValidationError(
                "local_files source is not selected; select it and grant a root before uploading"
            )
        grants = state.get("grants")
        if not isinstance(grants, list):
            raise ValidationError("local-file grant state is invalid")
        current: set[str] = set()
        for item in grants:
            if not isinstance(item, dict) or item.get("current") is not True:
                continue
            grant_id = item.get("grant_id")
            if isinstance(grant_id, str):
                current.add(grant_id)
        if not grant_ids <= current:
            raise ConflictError(
                "local-file upload authority was revoked or changed; request a fresh preview"
            )

    def _local_files(self) -> LocalFileGrantStore:
        if self._local_file_store is None:
            identity = self.vault.identity()
            vault_id = identity.get("vault_id")
            if not isinstance(vault_id, str):
                raise ValidationError("vault identity is unavailable for local-file transfer")
            self._local_file_store = LocalFileGrantStore(
                vault_root=self.vault.root,
                vault_id=vault_id,
            )
        return self._local_file_store

    def _artifacts(self) -> ArtifactStore:
        if self._closed:
            raise ValidationError("connector runtime is closed")
        if self._artifact_store_instance is None:
            self._artifact_store_instance = ArtifactStore()
        return self._artifact_store_instance

    def _execute_adapter(
        self,
        adapter: ConnectorAdapter,
        operation: OperationSpec,
        input_value: object,
        *,
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        write_idempotency_key: str | None,
        prepared_bundle: PreparedUploadBundle | None,
        connection_id: str,
        connection_revision: str,
    ) -> ConnectorAdapterResult:
        artifact_scope: ConnectorArtifactScope | None = None

        def scoped_artifacts() -> ConnectorArtifactScope:
            nonlocal artifact_scope
            if artifact_scope is None:
                artifact_scope = ConnectorArtifactScope(self._artifacts())
            return artifact_scope

        context = ConnectorTransferContext(
            uploads={} if prepared_bundle is None else prepared_bundle.uploads,
            _artifact_scope_factory=scoped_artifacts,
        )
        try:
            if _accepts_transfer_context(adapter):
                transfer_adapter = cast(ConnectorTransferAdapter, adapter)
                result = transfer_adapter.execute(
                    operation,
                    input_value,
                    continuation=continuation,
                    credential=credential,
                    transport=self.transport,
                    write_idempotency_key=write_idempotency_key,
                    transfer=context,
                )
            else:
                if prepared_bundle is not None:
                    raise ValidationError(
                        "connector adapter does not support prepared local-file uploads"
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
            if artifact_scope is None:
                if result.artifact is not None:
                    raise ValidationError("connector adapter returned a foreign artifact receipt")
            else:
                artifact_scope.complete(result.artifact)
            return result
        except ConnectorProviderError as exc:
            if artifact_scope is not None:
                with suppress(Exception):
                    artifact_scope.close()
            self._project_auth_failure(
                exc,
                connection_id=connection_id,
                connection_revision=connection_revision,
                credential_version=credential.version,
            )
            raise
        except Exception:
            if artifact_scope is not None:
                with suppress(Exception):
                    artifact_scope.close()
            raise

    def _project_auth_failure(
        self,
        error: ConnectorProviderError,
        *,
        connection_id: str,
        connection_revision: str,
        credential_version: int,
    ) -> None:
        if not _requires_reauthorization(error):
            return
        self.auth_manager.mark_reauthorization_required(
            connection_id,
            expected_connection_revision=connection_revision,
            expected_token_version=credential_version,
        )

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
        mutation_value: object,
        preview_value: object,
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
            mutation=mutation_value,
            connection_version=connection_version,
            credential_version=credential_version,
        )
        return {
            "account": account_label or "Verified account",
            "confirmation_token": token,
            "effect": effect.value,
            "expires_in_seconds": DEFAULT_TTL_SECONDS,
            "mutation_digest": canonical_json_digest(mutation_value),
            "operation": operation,
            "preview": _preview_value(preview_value),
            "provider": provider,
            "status": "confirmation_required",
            "warning": _effect_warning(effect, provider=provider, operation=operation),
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


_PrepareUpload = Callable[
    [object, Mapping[str, object], tuple[str | int, ...]],
    PreparedUploadBinding,
]


def _transform_local_files(
    value: object,
    prepare: _PrepareUpload,
    *,
    path: tuple[str | int, ...] = (),
) -> tuple[object, object, object]:
    if isinstance(value, dict):
        adapter_value: dict[str, object] = {}
        confirmation_value: dict[str, object] = {}
        preview_value: dict[str, object] = {}
        parent = cast(Mapping[str, object], value)
        for key in sorted(value):
            item = value[key]
            child_path = (*path, key)
            if key == "local_file":
                binding = prepare(item, parent, child_path)
                public_binding = binding.upload.binding()
                confirmation_binding = {
                    **public_binding,
                    "selector_digest": binding.selector_digest,
                }
                confirmation_value[key] = confirmation_binding
                preview_value[key] = public_binding
                continue
            adapter_child, confirmation_child, preview_child = _transform_local_files(
                item,
                prepare,
                path=child_path,
            )
            adapter_value[key] = adapter_child
            confirmation_value[key] = confirmation_child
            preview_value[key] = preview_child
        return adapter_value, confirmation_value, preview_value
    if isinstance(value, list):
        adapter_items: list[object] = []
        confirmation_items: list[object] = []
        preview_items: list[object] = []
        for index, item in enumerate(value):
            adapter_child, confirmation_child, preview_child = _transform_local_files(
                item,
                prepare,
                path=(*path, index),
            )
            adapter_items.append(adapter_child)
            confirmation_items.append(confirmation_child)
            preview_items.append(preview_child)
        return adapter_items, confirmation_items, preview_items
    return value, value, value


def _contains_local_file(value: object) -> bool:
    if isinstance(value, dict):
        return "local_file" in value or any(_contains_local_file(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_local_file(item) for item in value)
    return False


def _local_file_selectors(value: object) -> tuple[_LocalFileSelector, ...]:
    """Collect every selector before registering authority or reading source bytes."""

    selectors: list[_LocalFileSelector] = []

    def collect(item: object, path: tuple[str | int, ...]) -> None:
        if isinstance(item, dict):
            for key in sorted(item):
                child = item[key]
                if key == "local_file":
                    grant_id, relative_path = _local_file_selector(child)
                    selectors.append(
                        _LocalFileSelector(
                            path=(*path, key),
                            grant_id=grant_id,
                            relative_path=relative_path,
                        )
                    )
                else:
                    collect(child, (*path, key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                collect(child, (*path, index))

    collect(value, ())
    return tuple(selectors)


def _sanitize_upload_limit_input(value: object) -> object:
    """Preserve operation structure while removing every host-local selector."""

    if isinstance(value, dict):
        return {
            key: (
                _LOCAL_FILE_LIMIT_MARKER
                if key == "local_file"
                else _sanitize_upload_limit_input(item)
            )
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_sanitize_upload_limit_input(item) for item in value]
    return value


def _local_file_selector(value: object) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"grant_id", "relative_path"}:
        raise ValidationError("local_file must contain exactly grant_id and relative_path")
    grant_id = value.get("grant_id")
    relative_path = value.get("relative_path")
    if (
        not isinstance(grant_id, str)
        or not grant_id
        or len(grant_id) > 128
        or "\x00" in grant_id
        or not isinstance(relative_path, str)
        or not relative_path
        or len(relative_path.encode("utf-8")) > 16 * 1024
        or "\x00" in relative_path
    ):
        raise ValidationError("local_file selector is invalid")
    return grant_id, relative_path


def _upload_filename(parent: Mapping[str, object]) -> str | None:
    for name in ("filename", "name"):
        value = parent.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _upload_media_type(parent: Mapping[str, object]) -> str | None:
    for name in ("mime_type", "content_type"):
        value = parent.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _accepts_transfer_context(adapter: ConnectorAdapter) -> bool:
    try:
        parameters = signature(adapter.execute).parameters
    except (TypeError, ValueError):
        return False
    return _accepts_keyword(parameters, "transfer")


def _adapter_upload_limit(
    adapter: ConnectorAdapter,
    operation: OperationSpec,
    input_value: object,
    *,
    path: tuple[str | int, ...],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> int:
    method = getattr(adapter, "max_local_file_bytes", None)
    if not callable(method):
        raise ValidationError("connector adapter has no exact local-file limit for this operation")
    parameters: Mapping[str, Parameter]
    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_credential = _accepts_keyword(parameters, "credential")
    accepts_transport = _accepts_keyword(parameters, "transport")
    if accepts_credential is not accepts_transport:
        raise ValidationError(
            "connector adapter upload-limit preflight must accept credential and transport together"
        )
    if accepts_credential:
        provider_limit_adapter = cast(ConnectorProviderUploadLimitAdapter, adapter)
        limit = provider_limit_adapter.max_local_file_bytes(
            operation,
            input_value,
            path=path,
            credential=credential,
            transport=transport,
        )
    else:
        limit_adapter = cast(ConnectorUploadLimitAdapter, adapter)
        limit = limit_adapter.max_local_file_bytes(
            operation,
            input_value,
            path=path,
        )
    if type(limit) is not int or not 1 <= limit <= MAX_FILE_TRANSFER_BYTES:
        raise ValidationError(
            "connector adapter returned no exact local-file limit for this operation"
        )
    return limit


def _classify_adapter_effect(
    adapter: ConnectorAdapter,
    operation: OperationSpec,
    input_value: object,
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
    transfer: ConnectorTransferContext | None = None,
) -> ConnectorEffect:
    parameters: Mapping[str, Parameter]
    try:
        parameters = signature(adapter.classify_effect).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_preflight = all(
        _accepts_keyword(parameters, name) for name in ("credential", "transport")
    )
    kwargs: dict[str, object] = {}
    if accepts_preflight:
        kwargs.update(credential=credential, transport=transport)
    if _accepts_keyword(parameters, "transfer"):
        kwargs["transfer"] = transfer
    if not kwargs:
        return adapter.classify_effect(operation, input_value)
    classify_effect = cast(Callable[..., ConnectorEffect], adapter.classify_effect)
    return classify_effect(operation, input_value, **kwargs)


def _prepared_transfer_context(
    bundle: PreparedUploadBundle | None,
) -> ConnectorTransferContext | None:
    if bundle is None:
        return None
    return ConnectorTransferContext(uploads=bundle.uploads)


def _prepared_confirmation_preview(
    adapter: ConnectorAdapter,
    operation: OperationSpec,
    preview_value: object,
    *,
    bundle: PreparedUploadBundle,
) -> object:
    method = getattr(adapter, "prepared_confirmation_preview", None)
    if method is None:
        return preview_value
    if not callable(method):
        raise ValidationError("connector adapter prepared preview hook is invalid")

    base_preview = canonicalize_json(preview_value)
    hook_preview = canonicalize_json(base_preview)
    transfer = ConnectorTransferContext(uploads=bundle.uploads)
    preview_adapter = cast(ConnectorPreparedConfirmationPreviewAdapter, adapter)
    try:
        enrichment = preview_adapter.prepared_confirmation_preview(
            operation,
            hook_preview,
            transfer=transfer,
        )
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError("connector adapter could not prepare the upload preview") from exc
    if enrichment is None:
        return preview_value
    if not isinstance(base_preview, dict):
        raise ValidationError("prepared upload preview enrichment requires an object input")
    if _PREPARED_CONTENT_KEY in base_preview:
        raise ValidationError("prepared upload preview input uses the reserved content key")

    prepared_content = canonicalize_json(enrichment)
    merged = {**base_preview, _PREPARED_CONTENT_KEY: prepared_content}
    if len(canonical_json(_preview_value(merged))) > _PREPARED_PREVIEW_MAX_BYTES:
        raise ValidationError(
            "prepared upload confirmation is too large to show safely; reduce its recipient "
            "or metadata set so every consequential detail can be shown"
        )
    return merged


def _accepts_keyword(parameters: Mapping[str, Parameter], name: str) -> bool:
    parameter = parameters.get(name)
    if parameter is not None:
        return parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
    return any(item.kind is Parameter.VAR_KEYWORD for item in parameters.values())


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


def _promote_prepared_upload_effect(
    effect: ConnectorEffect,
    bundle: PreparedUploadBundle | None,
) -> ConnectorEffect:
    if bundle is not None and _EFFECT_ORDER[effect] < _EFFECT_ORDER[ConnectorEffect.OUTWARD]:
        return ConnectorEffect.OUTWARD
    return effect


def _scope_error(provider: str, operation: str) -> str:
    if provider == "gmail" and operation in {
        "messages.batch_purge",
        "messages.purge",
        "threads.purge",
    }:
        return (
            "Gmail permanent delete is not enabled; reconnect Full access with the separate "
            "permanent-delete permission"
        )
    if provider == "gmail" and operation.startswith("settings."):
        return (
            "Gmail settings control is not enabled; reconnect Gmail Full access to add it while "
            "the existing connection keeps working"
        )
    return "the provider did not grant the permission required for this operation"


def _requires_reauthorization(error: ConnectorProviderError) -> bool:
    return error.status == 401 or (
        error.origin is ConnectorOrigin.SLACK and error.code in SLACK_AUTH_FAILURE_CODES
    )


def _effect_warning(
    effect: ConnectorEffect,
    *,
    provider: str | None = None,
    operation: str | None = None,
) -> str:
    if effect is ConnectorEffect.PERMANENT:
        effect_warning = "This cannot be undone. Review the exact target before confirming."
    elif effect is ConnectorEffect.DESTRUCTIVE:
        effect_warning = (
            "This removes or archives provider content. Review the exact target before confirming."
        )
    else:
        effect_warning = (
            "This affects other people or sends data outward. Review it before confirming."
        )
    if provider is None or operation is None:
        return effect_warning
    operation_warning = _OPERATION_WARNINGS.get((provider, operation))
    if operation_warning is None:
        return effect_warning
    return f"{effect_warning} {operation_warning}"


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
