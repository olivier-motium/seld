from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread, local

import pytest

from continuity_kernel import connector_runtime as connector_runtime_module
from continuity_kernel import local_files as local_files_module
from continuity_kernel.connector_adapter import (
    ConnectorAdapterRegistry,
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_contract import (
    ConnectorEffect,
    ConnectorMode,
    OperationCatalog,
    OperationSpec,
)
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_session import ConnectorSession
from continuity_kernel.connector_transfer import (
    ArtifactReceipt,
    ArtifactStore,
    PreparedUploadCache,
)
from continuity_kernel.connector_transport import ConnectorTransport
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.local_files import (
    FILE_TRANSFER_CHUNK_BYTES,
    MAX_FILE_TRANSFER_BYTES,
    LocalFileAuthorityUse,
)
from continuity_kernel.vault import Vault

CONNECTION_ID = parse_connection_id("con-" + "r" * 32)


class _Adapter:
    providers = frozenset({"gmail"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object | None, str | None]] = []
        self.classifications: list[object] = []
        self.transferred: list[dict[tuple[str | int, ...], bytes]] = []
        self.effect: ConnectorEffect | None = None
        self.continuation: object | None = None
        self.after_execute: Callable[[], None] | None = None
        self.artifact_body: bytes | None = None
        self.artifact_override: ArtifactReceipt | None = None
        self.created_artifact_path: Path | None = None
        self.raise_after_artifact = False
        self.upload_limit = MAX_FILE_TRANSFER_BYTES
        self.upload_limit_calls: list[tuple[str, tuple[str | int, ...]]] = []
        self.upload_limit_inputs: list[object] = []
        self.upload_chunk_started: Event | None = None
        self.release_upload_chunk: Event | None = None
        self.upload_chunks_delivered = 0

    def classify_effect(self, operation: OperationSpec, input_value: object) -> ConnectorEffect:
        self.classifications.append(input_value)
        return self.effect or operation.effect

    def max_local_file_bytes(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        path: tuple[str | int, ...],
    ) -> int:
        self.upload_limit_inputs.append(input_value)
        self.upload_limit_calls.append((operation.name, path))
        return self.upload_limit

    def execute(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        write_idempotency_key: str | None = None,
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorAdapterResult:
        del credential, transport
        self.calls.append((operation.name, input_value, continuation, write_idempotency_key))
        transferred: dict[tuple[str | int, ...], bytes] = {}
        if transfer is not None:
            for path, upload in transfer.uploads.items():
                chunks: list[bytes] = []
                for block in upload.iter_chunks():
                    self.upload_chunks_delivered += 1
                    if self.upload_chunks_delivered == 1 and self.upload_chunk_started is not None:
                        self.upload_chunk_started.set()
                        if self.release_upload_chunk is not None:
                            assert self.release_upload_chunk.wait(2)
                    chunks.append(block)
                transferred[path] = b"".join(chunks)
        self.transferred.append(transferred)
        artifact = None
        if self.artifact_body is not None:
            assert transfer is not None
            writer = transfer.artifacts.start(
                "export.csv",
                media_type="text/csv",
                expected_size=len(self.artifact_body),
            )
            writer.write(self.artifact_body)
            artifact = writer.finish()
            self.created_artifact_path = artifact.path
        if self.artifact_override is not None:
            artifact = self.artifact_override
        if self.raise_after_artifact:
            raise ValidationError("adapter failed after producing an artifact")
        if self.after_execute is not None:
            self.after_execute()
        return ConnectorAdapterResult(
            {"accepted": True, "operation": operation.name},
            continuation=self.continuation,
            artifact=artifact,
        )


class _PreflightAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.preflight: tuple[ConnectorRuntimeCredential, ConnectorTransport] | None = None

    def classify_effect(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        credential: ConnectorRuntimeCredential | None = None,
        transport: ConnectorTransport | None = None,
    ) -> ConnectorEffect:
        assert credential is not None
        assert transport is not None
        self.preflight = (credential, transport)
        return super().classify_effect(operation, input_value)


def _prepared(
    tmp_path: Path,
    *,
    access: str = "full",
    adapter: _Adapter | None = None,
    artifact_store: ArtifactStore | None = None,
    prepared_uploads: PreparedUploadCache | None = None,
) -> tuple[Vault, ConnectorAuthManager, _Adapter, ConnectorRuntime]:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Connector runtime")
    profile = get_profile("google")
    now = datetime.now(UTC)
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider="google",
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(label="Alice at Example"),
        scopes=profile.scopes_for(access),
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-google-client",
            redirect_uris=("http://127.0.0.1:0",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.READY,
        created_at=now,
        updated_at=now,
        version=1,
        last_verified_at=now,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    stored = vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert stored is not None
    manager.store_oauth_credential(
        CONNECTION_ID,
        OAuthCredential(
            access_token="runtime-access-token",
            refresh_token="runtime-refresh-token",
            token_type=OAuthTokenType.BEARER,
            scopes=stored.scopes,
            issued_at=now,
            expires_at=None,
        ),
        expected_token_version=0,
    )
    adapter = adapter or _Adapter()
    runtime = ConnectorRuntime(
        vault,
        adapters=ConnectorAdapterRegistry((adapter,)),
        auth_manager=manager,
        session=ConnectorSession(secret=b"s" * 32),
        artifact_store=artifact_store,
        prepared_uploads=prepared_uploads,
    )
    return vault, manager, adapter, runtime


def _install_local_upload_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    local_file = {
        "additionalProperties": False,
        "properties": {
            "grant_id": {"maxLength": 128, "minLength": 1, "type": "string"},
            "relative_path": {"maxLength": 16_384, "minLength": 1, "type": "string"},
        },
        "required": ["grant_id", "relative_path"],
        "type": "object",
    }
    attachment = {
        "additionalProperties": False,
        "properties": {
            "filename": {"maxLength": 512, "minLength": 1, "type": "string"},
            "local_file": local_file,
            "mime_type": {"maxLength": 256, "minLength": 1, "type": "string"},
        },
        "required": ["filename", "local_file", "mime_type"],
        "type": "object",
    }
    operation = OperationSpec(
        provider="gmail",
        mode=ConnectorMode.WRITE,
        name="attachments.send",
        effect=ConnectorEffect.OUTWARD,
        endpoint="attachments.send",
        required_scopes=(frozenset({"https://www.googleapis.com/auth/gmail.modify"}),),
        input_schema={
            "additionalProperties": False,
            "properties": {
                "attachments": {
                    "items": attachment,
                    "maxItems": 4,
                    "minItems": 1,
                    "type": "array",
                },
                "subject": {"maxLength": 998, "minLength": 1, "type": "string"},
            },
            "required": ["attachments", "subject"],
            "type": "object",
        },
    )
    monkeypatch.setattr(
        connector_runtime_module,
        "OPERATION_CATALOG",
        OperationCatalog((operation,)),
    )


def _local_upload_values(grant_id: str) -> dict[str, object]:
    return {
        "connection_id": str(CONNECTION_ID),
        "input": {
            "attachments": [
                {
                    "filename": "invoice.pdf",
                    "local_file": {
                        "grant_id": grant_id,
                        "relative_path": "private/source-document.bin",
                    },
                    "mime_type": "application/pdf",
                }
            ],
            "subject": "Reviewed invoice",
        },
        "operation": "attachments.send",
    }


def test_read_uses_process_local_cursor_bound_to_exact_input_and_state(tmp_path: Path) -> None:
    _vault, _manager, adapter, runtime = _prepared(tmp_path)
    adapter.continuation = {"page_token": "provider-private-page-two"}
    first = runtime.call_tool(
        "gsv_gmail_read",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {"page_size": 2},
            "operation": "messages.list",
        },
    )
    assert first["status"] == "ok"
    assert first["result"] == {"accepted": True, "operation": "messages.list"}
    cursor = first["cursor"]
    assert isinstance(cursor, str)
    assert "provider-private-page-two" not in cursor

    second = runtime.call_tool(
        "gsv_gmail_read",
        {
            "connection_id": str(CONNECTION_ID),
            "cursor": cursor,
            "input": {"page_size": 2},
            "operation": "messages.list",
        },
    )
    assert second["status"] == "ok"
    assert adapter.calls[-1][2] == {"page_token": "provider-private-page-two"}

    with pytest.raises(ConflictError, match="binding"):
        runtime.call_tool(
            "gsv_gmail_read",
            {
                "connection_id": str(CONNECTION_ID),
                "cursor": cursor,
                "input": {"page_size": 3},
                "operation": "messages.list",
            },
        )


def test_read_only_tier_rejects_write_before_secret_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, manager, adapter, runtime = _prepared(tmp_path, access="read")

    def fail_resolution(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("secret resolution was reached")

    monkeypatch.setattr(manager, "resolve_oauth_access_token_state", fail_resolution)
    with pytest.raises(ValidationError, match="Read-only"):
        runtime.call_tool(
            "gsv_gmail_write",
            {
                "connection_id": str(CONNECTION_ID),
                "input": {},
                "operation": "drafts.create",
            },
        )
    assert adapter.calls == []


def test_safe_mutation_executes_once_but_outward_effect_requires_bound_confirmation(
    tmp_path: Path,
) -> None:
    _vault, _manager, adapter, runtime = _prepared(tmp_path)
    safe = runtime.call_tool(
        "gsv_gmail_write",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {"subject": "Draft only", "text_body": "Not sent"},
            "operation": "drafts.create",
        },
    )
    assert safe["status"] == "ok"
    assert [call[0] for call in adapter.calls] == ["drafts.create"]

    values = {
        "connection_id": str(CONNECTION_ID),
        "input": {"draft_id": "draft-one"},
        "operation": "drafts.send",
    }
    preview = runtime.call_tool("gsv_gmail_write", values)
    assert preview["status"] == "confirmation_required"
    assert preview["account"] == "Alice at Example"
    assert preview["preview"] == {"draft_id": "draft-one"}
    assert [call[0] for call in adapter.calls] == ["drafts.create"]

    confirmed = runtime.call_tool(
        "gsv_gmail_write",
        {**values, "confirmation_token": preview["confirmation_token"]},
    )
    assert confirmed["status"] == "ok"
    assert [call[0] for call in adapter.calls] == ["drafts.create", "drafts.send"]
    assert adapter.calls[-1][3] is not None
    assert len(adapter.calls[-1][3] or "") == 22
    with pytest.raises(ConflictError, match="already been consumed"):
        runtime.call_tool(
            "gsv_gmail_write",
            {**values, "confirmation_token": preview["confirmation_token"]},
        )
    assert [call[0] for call in adapter.calls] == ["drafts.create", "drafts.send"]


def test_confirmation_is_bound_to_exact_mutation_and_adapter_cannot_downgrade(
    tmp_path: Path,
) -> None:
    _vault, _manager, adapter, runtime = _prepared(tmp_path)
    original = {
        "connection_id": str(CONNECTION_ID),
        "input": {"draft_id": "draft-one"},
        "operation": "drafts.send",
    }
    preview = runtime.call_tool("gsv_gmail_write", original)
    with pytest.raises(ConflictError, match="binding"):
        runtime.call_tool(
            "gsv_gmail_write",
            {
                **original,
                "confirmation_token": preview["confirmation_token"],
                "input": {"draft_id": "draft-two"},
            },
        )
    adapter.effect = ConnectorEffect.SAFE_MUTATION
    with pytest.raises(ValidationError, match="cannot downgrade"):
        runtime.call_tool("gsv_gmail_write", original)


def test_successful_mutation_reports_connection_change_without_inviting_retry(
    tmp_path: Path,
) -> None:
    vault, _manager, adapter, runtime = _prepared(tmp_path)

    def change_connection() -> None:
        snapshot = vault.get_connection_snapshot()
        connection = snapshot.connection(CONNECTION_ID)
        assert connection is not None
        vault.mark_connection_health(
            expected_revision=snapshot.revision,
            connection_id=CONNECTION_ID,
            health=ConnectionHealth.DEGRADED,
            observed_at=connection.updated_at + timedelta(microseconds=1),
        )

    adapter.after_execute = change_connection
    result = runtime.call_tool(
        "gsv_gmail_write",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {"subject": "Draft"},
            "operation": "drafts.create",
        },
    )
    assert result["status"] == "completed_state_changed"
    assert result["do_not_retry"] is True
    assert "Review provider state" in str(result["warning"])
    assert len(adapter.calls) == 1


def test_unverified_connection_and_wrong_source_binding_fail_closed(tmp_path: Path) -> None:
    vault, _manager, adapter, runtime = _prepared(tmp_path)
    snapshot = vault.get_connection_snapshot()
    connection = snapshot.connection(CONNECTION_ID)
    assert connection is not None
    vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=CONNECTION_ID,
        health=ConnectionHealth.UNVERIFIED,
        observed_at=connection.updated_at + timedelta(microseconds=1),
    )
    with pytest.raises(ValidationError, match="verified"):
        runtime.call_tool(
            "gsv_gmail_read",
            {
                "connection_id": str(CONNECTION_ID),
                "input": {},
                "operation": "messages.list",
            },
        )
    assert adapter.calls == []


def test_effect_preflight_receives_live_credential_and_transport(tmp_path: Path) -> None:
    preflight = _PreflightAdapter()
    _vault, _manager, adapter, runtime = _prepared(tmp_path, adapter=preflight)
    result = runtime.call_tool(
        "gsv_gmail_write",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {"subject": "Draft"},
            "operation": "drafts.create",
        },
    )
    assert result["status"] == "ok"
    assert adapter is preflight
    assert preflight.preflight is not None
    assert preflight.preflight[0].version == 1
    assert preflight.preflight[1] is runtime.transport


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_confirmed_upload_reuses_reviewed_snapshot_and_mismatch_stays_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    _install_local_upload_catalog(monkeypatch)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    vault, _manager, adapter, runtime = _prepared(tmp_path, artifact_store=artifacts)
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    selected = tmp_path / "selected"
    source_parent = selected / "private"
    source_parent.mkdir(parents=True)
    source = source_parent / "source-document.bin"
    source.write_bytes(b"reviewed bytes")
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    values = _local_upload_values(grant_id)
    source_inode = int(os.stat(source).st_ino)
    source_bytes_read = 0
    source_nonempty_reads = 0
    actual_read = os.read

    def tracked_read(descriptor: int, amount: int) -> bytes:
        nonlocal source_bytes_read, source_nonempty_reads
        block = actual_read(descriptor, amount)
        if int(os.fstat(descriptor).st_ino) == source_inode and block:
            source_bytes_read += len(block)
            source_nonempty_reads += 1
        return block

    monkeypatch.setattr(local_files_module.os, "read", tracked_read)

    preview = runtime.call_tool("gsv_gmail_write", values)
    token = preview["confirmation_token"]
    assert isinstance(token, str)
    public_preview = preview["preview"]
    assert isinstance(public_preview, dict)
    attachments = public_preview["attachments"]
    assert isinstance(attachments, list)
    local_file = attachments[0]["local_file"]
    assert local_file == {
        "bytes": len(b"reviewed bytes"),
        "filename": "invoice.pdf",
        "media_type": "application/pdf",
        "sha256": hashlib.sha256(b"reviewed bytes").hexdigest(),
    }
    assert grant_id not in repr(public_preview)
    assert "private/source-document.bin" not in repr(public_preview)
    assert adapter.classifications[-1] == {
        "attachments": [
            {
                "filename": "invoice.pdf",
                "mime_type": "application/pdf",
            }
        ],
        "subject": "Reviewed invoice",
    }
    cached = runtime.prepared_uploads.peek(token)
    assert cached is not None
    prepared_upload = cached.bindings[0].upload
    assert source_bytes_read == len(b"reviewed bytes")
    assert source_nonempty_reads == 1
    assert adapter.upload_limit_calls == [("attachments.send", ("attachments", 0, "local_file"))]
    assert adapter.upload_limit_inputs == [
        {
            "attachments": [
                {
                    "filename": "invoice.pdf",
                    "local_file": "opaque-local-file",
                    "mime_type": "application/pdf",
                }
            ],
            "subject": "Reviewed invoice",
        }
    ]
    assert grant_id not in repr(adapter.upload_limit_inputs)
    assert "private/source-document.bin" not in repr(adapter.upload_limit_inputs)

    source.write_bytes(b"changed after preview")
    original_input = values["input"]
    assert isinstance(original_input, dict)
    changed_values = {
        **values,
        "confirmation_token": token,
        "input": {**original_input, "subject": "Not the reviewed subject"},
    }
    with pytest.raises(ConflictError, match="binding"):
        runtime.call_tool("gsv_gmail_write", changed_values)
    assert runtime.prepared_uploads.peek(token) is cached

    result = runtime.call_tool(
        "gsv_gmail_write",
        {**values, "confirmation_token": token},
    )
    assert result["status"] == "ok"
    assert adapter.calls[-1][1] == {
        "attachments": [
            {
                "filename": "invoice.pdf",
                "mime_type": "application/pdf",
            }
        ],
        "subject": "Reviewed invoice",
    }
    assert adapter.transferred[-1] == {("attachments", 0, "local_file"): b"reviewed bytes"}
    assert all(grant_id not in repr(value) for value in adapter.classifications)
    assert all(
        "private/source-document.bin" not in repr(value) for value in adapter.classifications
    )
    assert source_bytes_read == len(b"reviewed bytes")
    assert source_nonempty_reads == 1
    assert runtime.prepared_uploads.peek(token) is None
    with pytest.raises(ValidationError, match="closed"):
        list(prepared_upload.iter_chunks())
    runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_expired_or_revoked_upload_requires_a_fresh_preview_and_closes_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    _install_local_upload_catalog(monkeypatch)
    cache_time = [100.0]
    cache = PreparedUploadCache(clock=lambda: cache_time[0])
    artifacts = ArtifactStore(tmp_path / "artifacts")
    vault, _manager, adapter, runtime = _prepared(
        tmp_path,
        artifact_store=artifacts,
        prepared_uploads=cache,
    )
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    selected = tmp_path / "selected"
    source_parent = selected / "private"
    source_parent.mkdir(parents=True)
    (source_parent / "source-document.bin").write_bytes(b"reviewed bytes")
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    values = _local_upload_values(grant_id)

    preview = runtime.call_tool("gsv_gmail_write", values)
    expired_token = preview["confirmation_token"]
    assert isinstance(expired_token, str)
    expired_bundle = cache.peek(expired_token)
    assert expired_bundle is not None
    expired_upload = expired_bundle.bindings[0].upload
    cache_time[0] += 600
    with pytest.raises(ConflictError, match="fresh preview"):
        runtime.call_tool(
            "gsv_gmail_write",
            {**values, "confirmation_token": expired_token},
        )
    with pytest.raises(ValidationError, match="closed"):
        list(expired_upload.iter_chunks())

    preview = runtime.call_tool("gsv_gmail_write", values)
    revoked_token = preview["confirmation_token"]
    assert isinstance(revoked_token, str)
    revoked_bundle = cache.peek(revoked_token)
    assert revoked_bundle is not None
    revoked_upload = revoked_bundle.bindings[0].upload
    vault.revoke_local_file_grant(grant_id)
    with pytest.raises(ConflictError, match="fresh preview"):
        runtime.call_tool(
            "gsv_gmail_write",
            {**values, "confirmation_token": revoked_token},
        )
    assert cache.peek(revoked_token) is None
    with pytest.raises(ValidationError, match="closed"):
        list(revoked_upload.iter_chunks())
    assert adapter.calls == []
    runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_revoke_waits_for_delivered_chunk_and_prevents_the_next_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    _install_local_upload_catalog(monkeypatch)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    vault, _manager, adapter, runtime = _prepared(tmp_path, artifact_store=artifacts)
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    selected = tmp_path / "selected"
    source = selected / "private" / "source-document.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * (FILE_TRANSFER_CHUNK_BYTES + 1))
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    values = _local_upload_values(grant_id)
    preview = runtime.call_tool("gsv_gmail_write", values)
    token = preview["confirmation_token"]
    assert isinstance(token, str)

    chunk_started = Event()
    release_chunk = Event()
    revoke_finished = Event()
    adapter.upload_chunk_started = chunk_started
    adapter.release_upload_chunk = release_chunk
    confirmation_errors: list[BaseException] = []
    revoke_errors: list[BaseException] = []

    def confirm() -> None:
        try:
            runtime.call_tool(
                "gsv_gmail_write",
                {**values, "confirmation_token": token},
            )
        except BaseException as exc:
            confirmation_errors.append(exc)

    def revoke() -> None:
        try:
            vault.revoke_local_file_grant(grant_id)
        except BaseException as exc:
            revoke_errors.append(exc)
        finally:
            revoke_finished.set()

    confirming = Thread(target=confirm)
    revoking = Thread(target=revoke)
    confirming.start()
    assert chunk_started.wait(2)
    revoking.start()
    assert not revoke_finished.wait(0.05)
    release_chunk.set()
    confirming.join(3)
    revoking.join(3)

    assert not confirming.is_alive()
    assert not revoking.is_alive()
    assert revoke_errors == []
    assert len(confirmation_errors) == 1
    assert isinstance(confirmation_errors[0], ConflictError)
    assert adapter.upload_chunks_delivered == 1
    runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_snapshot_budget_is_reserved_before_artifact_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    _install_local_upload_catalog(monkeypatch)
    cache = PreparedUploadCache(max_snapshot_bytes=4)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    vault, _manager, _adapter, runtime = _prepared(
        tmp_path,
        artifact_store=artifacts,
        prepared_uploads=cache,
    )
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    selected = tmp_path / "selected"
    source_parent = selected / "private"
    source_parent.mkdir(parents=True)
    source = source_parent / "source-document.bin"
    source.write_bytes(b"too large")
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    source_inode = int(os.stat(source).st_ino)
    source_reads = 0
    actual_read = os.read

    def tracked_read(descriptor: int, amount: int) -> bytes:
        nonlocal source_reads
        if int(os.fstat(descriptor).st_ino) == source_inode:
            source_reads += 1
        return actual_read(descriptor, amount)

    def fail_staging(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("artifact staging ran before aggregate bytes were reserved")

    monkeypatch.setattr(artifacts, "prepare_upload", fail_staging)
    monkeypatch.setattr(local_files_module.os, "read", tracked_read)
    with pytest.raises(ConflictError, match="byte capacity"):
        runtime.call_tool("gsv_gmail_write", _local_upload_values(grant_id))
    assert source_reads == 0
    runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_adapter_upload_limit_fails_before_source_content_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    _install_local_upload_catalog(monkeypatch)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    vault, _manager, adapter, runtime = _prepared(tmp_path, artifact_store=artifacts)
    adapter.upload_limit = 4
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    selected = tmp_path / "selected"
    source = selected / "private" / "source-document.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"five!")
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    source_inode = int(os.stat(source).st_ino)
    source_reads = 0
    actual_read = os.read

    def tracked_read(descriptor: int, amount: int) -> bytes:
        nonlocal source_reads
        if int(os.fstat(descriptor).st_ino) == source_inode:
            source_reads += 1
        return actual_read(descriptor, amount)

    monkeypatch.setattr(local_files_module.os, "read", tracked_read)
    with pytest.raises(ValidationError, match="provider operation's size limit"):
        runtime.call_tool("gsv_gmail_write", _local_upload_values(grant_id))

    assert source_reads == 0
    assert adapter.upload_limit_calls == [("attachments.send", ("attachments", 0, "local_file"))]
    assert not list(artifacts.root.glob("*.stage"))

    monkeypatch.setattr(adapter, "max_local_file_bytes", None)
    with pytest.raises(ValidationError, match="no exact local-file limit"):
        runtime.call_tool("gsv_gmail_write", _local_upload_values(grant_id))
    assert source_reads == 0
    runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-pinned transfer proof is POSIX-only")
def test_revoke_during_multifile_snapshot_stops_at_current_source_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    _install_local_upload_catalog(monkeypatch)
    first_payload = b"a" * (FILE_TRANSFER_CHUNK_BYTES + 17)
    second_payload = b"second file must never begin"
    aggregate_size = len(first_payload) + len(second_payload)
    cache = PreparedUploadCache(max_snapshot_bytes=aggregate_size)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    vault, _manager, _adapter, runtime = _prepared(
        tmp_path,
        artifact_store=artifacts,
        prepared_uploads=cache,
    )
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    first_root = tmp_path / "first-selected"
    second_root = tmp_path / "second-selected"
    first_file = first_root / "private" / "source-document.bin"
    second_file = second_root / "private" / "source-document.bin"
    first_file.parent.mkdir(parents=True)
    second_file.parent.mkdir(parents=True)
    first_file.write_bytes(first_payload)
    second_file.write_bytes(second_payload)
    first_grant = vault.grant_local_file_root(first_root)["grant"]["grant_id"]
    second_grant = vault.grant_local_file_root(second_root)["grant"]["grant_id"]
    values = _local_upload_values(first_grant)
    input_value = values["input"]
    assert isinstance(input_value, dict)
    attachments = input_value["attachments"]
    assert isinstance(attachments, list)
    attachments.append(
        {
            "filename": "second.pdf",
            "local_file": {
                "grant_id": second_grant,
                "relative_path": "private/source-document.bin",
            },
            "mime_type": "application/pdf",
        }
    )

    staging_started = Event()
    source_chunk_started = Event()
    release_source_chunk = Event()
    revocation_finished = Event()
    read_lock = Lock()
    thread_state = local()
    source_inodes = {
        int(os.stat(first_file).st_ino): "first",
        int(os.stat(second_file).st_ino): "second",
    }
    read_counts = {"first": 0, "second": 0}
    count_when_revoked: list[int] = []
    actual_read = os.read
    actual_chunk = LocalFileAuthorityUse.chunk
    actual_prepare = artifacts.prepare_upload

    @contextmanager
    def controlled_chunk(self: LocalFileAuthorityUse):
        previous_depth = getattr(thread_state, "authority_depth", 0)
        with actual_chunk(self):
            thread_state.authority_depth = previous_depth + 1
            try:
                yield
            finally:
                thread_state.authority_depth = previous_depth
        if getattr(thread_state, "wait_for_revocation", False):
            thread_state.wait_for_revocation = False
            assert revocation_finished.wait(2)

    def tracked_read(descriptor: int, amount: int) -> bytes:
        label = source_inodes.get(int(os.fstat(descriptor).st_ino))
        if label is None:
            return actual_read(descriptor, amount)
        assert getattr(thread_state, "authority_depth", 0) > 0
        pause = label == "first" and staging_started.is_set() and not source_chunk_started.is_set()
        if pause:
            source_chunk_started.set()
            assert release_source_chunk.wait(2)
        block = actual_read(descriptor, amount)
        with read_lock:
            read_counts[label] += 1
        if pause:
            thread_state.wait_for_revocation = True
        return block

    def marked_prepare(*args: object, **kwargs: object):
        staging_started.set()
        return actual_prepare(*args, **kwargs)

    monkeypatch.setattr(LocalFileAuthorityUse, "chunk", controlled_chunk)
    monkeypatch.setattr(local_files_module.os, "read", tracked_read)
    monkeypatch.setattr(artifacts, "prepare_upload", marked_prepare)
    preparation_errors: list[BaseException] = []
    revocation_errors: list[BaseException] = []

    def prepare() -> None:
        try:
            runtime.call_tool("gsv_gmail_write", values)
        except BaseException as exc:
            preparation_errors.append(exc)

    def revoke() -> None:
        try:
            vault.revoke_local_file_grant(first_grant)
            with read_lock:
                count_when_revoked.append(sum(read_counts.values()))
        except BaseException as exc:
            revocation_errors.append(exc)
        finally:
            revocation_finished.set()

    preparing = Thread(target=prepare)
    revoking = Thread(target=revoke)
    preparing.start()
    assert source_chunk_started.wait(2)
    revoking.start()
    assert not revocation_finished.wait(0.05)
    release_source_chunk.set()
    preparing.join(3)
    revoking.join(3)

    assert not preparing.is_alive()
    assert not revoking.is_alive()
    assert revocation_errors == []
    assert len(preparation_errors) == 1
    assert isinstance(preparation_errors[0], ConflictError)
    with read_lock:
        assert count_when_revoked == [sum(read_counts.values())]
        assert read_counts["first"] > 0
        assert read_counts["second"] == 0
    assert not list(artifacts.root.glob("*.stage"))
    reservation = cache.reserve(aggregate_size)
    reservation.release()
    runtime.close()


@pytest.mark.skipif(os.name == "nt", reason="secure artifact delivery is POSIX-only")
def test_adapter_artifact_receipt_is_returned_as_transient_local_output(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    _vault, _manager, adapter, runtime = _prepared(tmp_path, artifact_store=artifacts)
    adapter.artifact_body = b"item,value\ninvoice,42\n"
    response = runtime.call_tool(
        "gsv_gmail_read",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {},
            "operation": "messages.list",
        },
    )
    receipt = response["artifact"]
    assert isinstance(receipt, dict)
    assert receipt["bytes"] == len(adapter.artifact_body)
    assert receipt["filename"] == "export.csv"
    assert receipt["media_type"] == "text/csv"
    assert receipt["storage"] == "owner_only_transient_host_cache"
    assert receipt["cleanup"] == "after_expiry_while_runtime_active_or_on_next_start"
    assert receipt["transient"] is True
    artifact_path = Path(str(receipt["path"]))
    assert artifact_path.read_bytes() == adapter.artifact_body
    runtime.close()
    assert artifact_path.read_bytes() == adapter.artifact_body
    with pytest.raises(ValidationError, match="runtime is closed"):
        runtime.call_tool(
            "gsv_gmail_read",
            {
                "connection_id": str(CONNECTION_ID),
                "input": {},
                "operation": "messages.list",
            },
        )


@pytest.mark.skipif(os.name == "nt", reason="secure artifact delivery is POSIX-only")
def test_runtime_rejects_foreign_receipt_and_cleans_artifact_on_adapter_failure(
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    foreign_store = ArtifactStore(tmp_path / "foreign-artifacts")
    _vault, _manager, adapter, runtime = _prepared(tmp_path, artifact_store=artifacts)
    values = {
        "connection_id": str(CONNECTION_ID),
        "input": {},
        "operation": "messages.list",
    }

    foreign_writer = foreign_store.start("foreign.csv", expected_size=7)
    foreign_writer.write(b"foreign")
    foreign = foreign_writer.finish()
    adapter.artifact_override = foreign
    with pytest.raises(ValidationError, match="foreign artifact receipt"):
        runtime.call_tool("gsv_gmail_read", values)
    assert foreign.path.read_bytes() == b"foreign"

    adapter.artifact_override = None
    adapter.artifact_body = b"partial output"
    adapter.raise_after_artifact = True
    with pytest.raises(ValidationError, match="failed after producing"):
        runtime.call_tool("gsv_gmail_read", values)
    assert adapter.created_artifact_path is not None
    assert not adapter.created_artifact_path.exists()
    assert not list(artifacts.root.glob("*.part"))
    runtime.close()
    foreign_store.close()
