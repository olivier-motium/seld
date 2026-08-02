from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from continuity_kernel.connector_adapter import (
    ConnectorAdapterRegistry,
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_adapter_collaboration import CollaborationConnectorAdapter
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_contract import ConnectorEffect, OperationSpec
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_session import ConnectorSession
from continuity_kernel.connector_transfer import ArtifactStore, PreparedUpload
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorOrigin,
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="descriptor-pinned connector transfer is POSIX-only",
)

_CONNECTION_ID = parse_connection_id("con-" + "s" * 32)
_UPLOAD_LOCATION = "https://files.slack.com/upload/v1/opaque"
_DOWNLOAD_LOCATION = "https://files.slack.com/private/download"


class _RecordingCollaborationAdapter(CollaborationConnectorAdapter):
    def __init__(self) -> None:
        self.observed_inputs: list[object] = []
        self.observed_uploads: list[frozenset[tuple[str | int, ...]]] = []
        self.upload_limit_calls: list[tuple[str | int, ...]] = []

    def max_local_file_bytes(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        path: tuple[str | int, ...],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> int:
        self.upload_limit_calls.append(path)
        return super().max_local_file_bytes(
            operation,
            input_value,
            path=path,
            credential=credential,
            transport=transport,
        )

    def _record(self, input_value: object, transfer: ConnectorTransferContext | None) -> None:
        self.observed_inputs.append(input_value)
        self.observed_uploads.append(
            frozenset() if transfer is None else frozenset(transfer.uploads)
        )

    def classify_effect(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorEffect:
        self._record(input_value, transfer)
        return super().classify_effect(
            operation,
            input_value,
            transfer=transfer,
        )

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
        self._record(input_value, transfer)
        return super().execute(
            operation,
            input_value,
            continuation=continuation,
            credential=credential,
            transport=transport,
            write_idempotency_key=write_idempotency_key,
            transfer=transfer,
        )


class _SlackTransport(ConnectorTransport):
    def __init__(
        self, *, download_body: bytes = b"downloaded file", reported_size: int | None = None
    ):
        self.download_body = download_body
        self.reported_size = len(download_body) if reported_size is None else reported_size
        self.requests: list[dict[str, Any]] = []
        self.stream_uploads: list[dict[str, Any]] = []
        self.downloads: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.requests.append(kwargs)
        path = kwargs["path"]
        if path == "/api/files.getUploadURLExternal":
            payload: object = {"file_id": "F123", "ok": True, "upload_url": _UPLOAD_LOCATION}
        elif path == "/api/files.completeUploadExternal":
            payload = {"files": [{"id": "F123"}], "ok": True}
        elif path == "/api/files.info":
            payload = {
                "file": {
                    "id": "F123",
                    "mimetype": "application/octet-stream",
                    "name": "provider-file.bin",
                    "size": self.reported_size,
                    "url_private_download": _DOWNLOAD_LOCATION,
                },
                "ok": True,
            }
        else:  # pragma: no cover - fixed-route regression guard
            raise AssertionError(f"unexpected Slack route: {path}")
        return ConnectorResponse(
            origin=ConnectorOrigin.SLACK,
            status=200,
            headers=MappingProxyType({}),
            body=json.dumps(payload).encode("utf-8"),
        )

    def request_provider_location(self, **kwargs: Any) -> ConnectorResponse:
        del kwargs
        raise AssertionError("local-file upload must use the streaming transport")

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        source = kwargs["source"]
        assert isinstance(source, PreparedUpload)
        body = b"".join(source.iter_chunks())
        observed = {**kwargs, "body": body}
        self.stream_uploads.append(observed)
        return ConnectorStreamResponse(
            origin=ConnectorOrigin.SLACK,
            status=200,
            headers=MappingProxyType({}),
            bytes_transferred=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )

    def download_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        self.downloads.append(kwargs)
        sink = kwargs["sink"]
        sink.write(self.download_body)
        if len(self.download_body) != kwargs["expected_length"]:
            raise ValidationError("provider download byte count does not match its length")
        receipt = sink.finish()
        return ConnectorStreamResponse(
            origin=ConnectorOrigin.SLACK,
            status=200,
            headers=MappingProxyType({}),
            bytes_transferred=len(self.download_body),
            sha256=hashlib.sha256(self.download_body).hexdigest(),
            artifact=receipt,
        )


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: _SlackTransport | None = None,
) -> tuple[Vault, ConnectorRuntime, _RecordingCollaborationAdapter, _SlackTransport, Path]:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Slack collaboration transfer runtime")
    profile = get_profile("slack")
    now = datetime.now(UTC)
    scopes = profile.scopes_for("full")
    connection = ConnectionMetadata(
        connection_id=_CONNECTION_ID,
        provider="slack",
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(
            fingerprint="sha256:" + "b" * 64,
            label="Configured Slack Full",
        ),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-slack-client",
            redirect_uris=("http://localhost:43127/oauth/callback",),
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
        connection=connection,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    manager.ensure_imported_credential(
        connection,
        OAuthCredential(
            access_token="runtime-slack-token",
            refresh_token=None,
            token_type=OAuthTokenType.BEARER,
            scopes=scopes,
            issued_at=now,
            expires_at=None,
        ).to_bytes(),
    )
    adapter = _RecordingCollaborationAdapter()
    bound_transport = transport or _SlackTransport()
    artifact_root = tmp_path / "artifacts"
    runtime = ConnectorRuntime(
        vault,
        adapters=ConnectorAdapterRegistry((adapter,)),
        auth_manager=manager,
        transport=bound_transport,
        session=ConnectorSession(secret=b"s" * 32),
        artifact_store=ArtifactStore(artifact_root),
    )
    return vault, runtime, adapter, bound_transport, artifact_root


def _local_upload(vault: Vault, root: Path, content: bytes) -> tuple[dict[str, object], str]:
    root.mkdir()
    (root / "large.bin").write_bytes(content)
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    grant_id = vault.grant_local_file_root(root)["grant"]["grant_id"]
    assert isinstance(grant_id, str)
    values: dict[str, object] = {
        "connection_id": str(_CONNECTION_ID),
        "input": {
            "channel": "C123",
            "filename": "large.bin",
            "local_file": {"grant_id": grant_id, "relative_path": "large.bin"},
        },
        "operation": "files.upload",
    }
    return values, grant_id


def test_slack_local_file_is_snapshot_bound_then_streamed_to_the_credentialless_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, adapter, transport, _artifact_root = _runtime(tmp_path, monkeypatch)
    content = b"streamed-slack-upload-" * 12_000
    values, grant_id = _local_upload(vault, tmp_path / "selected", content)
    try:
        preview = runtime.call_tool("gsv_slack_write", values)
        token = preview["confirmation_token"]
        assert isinstance(token, str)
        assert preview["status"] == "confirmation_required"
        assert transport.requests == []
        preview_payload = preview["preview"]
        assert isinstance(preview_payload, Mapping)
        assert preview_payload["local_file"] == {
            "bytes": len(content),
            "filename": "large.bin",
            "media_type": None,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        assert grant_id not in repr(preview)
        assert adapter.observed_inputs[-1] == {"channel": "C123", "filename": "large.bin"}
        assert adapter.observed_uploads[-1] == {("local_file",)}

        original_input = values["input"]
        assert isinstance(original_input, dict)
        changed = {
            **values,
            "confirmation_token": token,
            "input": {
                **original_input,
                "local_file": {
                    "grant_id": grant_id,
                    "relative_path": "different.bin",
                },
            },
        }
        with pytest.raises(ConflictError, match="binding"):
            runtime.call_tool("gsv_slack_write", changed)
        assert transport.requests == []

        completed = runtime.call_tool(
            "gsv_slack_write",
            {**values, "confirmation_token": token},
        )
        assert completed["status"] == "ok"
        assert transport.stream_uploads[0]["location"] == _UPLOAD_LOCATION
        assert transport.stream_uploads[0]["credential"] is None
        assert transport.stream_uploads[0]["content_length"] == len(content)
        assert transport.stream_uploads[0]["body"] == content
        assert all("local_file" not in repr(item) for item in transport.requests)
    finally:
        runtime.close()


def test_slack_empty_local_file_fails_before_confirmation_or_provider_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, _adapter, transport, _artifact_root = _runtime(tmp_path, monkeypatch)
    values, _grant_id = _local_upload(vault, tmp_path / "selected", b"")
    try:
        with pytest.raises(ValidationError, match="cannot be empty"):
            runtime.call_tool("gsv_slack_write", values)
        assert transport.requests == []
        assert transport.stream_uploads == []
    finally:
        runtime.close()


def test_revoked_local_file_authority_fails_before_upload_limit_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, adapter, transport, _artifact_root = _runtime(tmp_path, monkeypatch)
    values, grant_id = _local_upload(vault, tmp_path / "selected", b"reviewed")
    vault.revoke_local_file_grant(grant_id)
    try:
        with pytest.raises(ConflictError, match="revoked or changed"):
            runtime.call_tool("gsv_slack_write", values)
        assert adapter.upload_limit_calls == []
        assert transport.requests == []
    finally:
        runtime.close()


def test_slack_download_defaults_to_an_owner_only_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"full Slack file contents"
    vault, runtime, _adapter, transport, _artifact_root = _runtime(
        tmp_path,
        monkeypatch,
        transport=_SlackTransport(download_body=content),
    )
    del vault
    try:
        result = runtime.call_tool(
            "gsv_slack_read",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": {"file_id": "F123"},
                "operation": "files.download",
            },
        )
        artifact = result["artifact"]
        assert isinstance(artifact, Mapping)
        path = Path(artifact["path"])
        assert result["result"] == {
            "bytes": len(content),
            "delivery": "artifact",
            "file_id": "F123",
        }
        assert artifact["storage"] == "owner_only_transient_host_cache"
        assert artifact["transient"] is True
        assert artifact["bytes"] == len(content)
        assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == 0o600
        assert transport.downloads[0]["location"] == _DOWNLOAD_LOCATION
        assert transport.downloads[0]["credential"].scheme is AuthorizationScheme.BEARER
    finally:
        runtime.close()


def test_slack_download_length_mismatch_aborts_the_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _SlackTransport(download_body=b"short", reported_size=6)
    _vault, runtime, _adapter, _transport, artifact_root = _runtime(
        tmp_path,
        monkeypatch,
        transport=transport,
    )
    try:
        with pytest.raises(ValidationError, match="byte count"):
            runtime.call_tool(
                "gsv_slack_read",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {"file_id": "F123"},
                    "operation": "files.download",
                },
            )
        assert list(artifact_root.iterdir()) == []
    finally:
        runtime.close()
