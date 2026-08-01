from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

import continuity_kernel.connector_adapter_collaboration as collaboration_module
from continuity_kernel.connector_adapter import (
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_adapter_collaboration import CollaborationConnectorAdapter
from continuity_kernel.connector_contract import ConnectorMode, OperationSpec
from continuity_kernel.connector_operations_collaboration import COLLABORATION_OPERATIONS
from continuity_kernel.connector_transfer import (
    ARTIFACT_CHUNK_BYTES,
    ArtifactStore,
    ConnectorArtifactScope,
    PreparedUpload,
)
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorOrigin,
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ContinuityError, ValidationError

_BOT_ID = "100000000000000001"
_CHANNEL_ID = "100000000000000002"
_MESSAGE_ID = "100000000000000003"
_ATTACHMENT_ID = "100000000000000004"
_CDN_LOCATION = (
    f"https://cdn.discordapp.com/attachments/{_CHANNEL_ID}/{_ATTACHMENT_ID}/"
    "provider-report.pdf?signature=opaque"
)


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        header_values = Message()
        for name, value in (headers or {}).items():
            header_values[name] = value
        self.headers: object = header_values
        self._body = io.BytesIO(body)

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _DiscordDownloadOpener:
    def __init__(self, body: bytes, *, reported_size: int | None = None) -> None:
        self.body = body
        self.reported_size = len(body) if reported_size is None else reported_size
        self.requests: list[Request] = []

    def __call__(self, request: Request, timeout: float) -> _Response:
        del timeout
        self.requests.append(request)
        headers = {name.lower(): value for name, value in request.header_items()}
        if request.full_url == "https://discord.com/api/v10/users/@me":
            assert headers["authorization"] == "Bot discord-test-token"
            return _json_response({"bot": True, "id": _BOT_ID})
        if request.full_url == (
            f"https://discord.com/api/v10/channels/{_CHANNEL_ID}/messages/{_MESSAGE_ID}"
        ):
            assert headers["authorization"] == "Bot discord-test-token"
            return _json_response(
                {
                    "attachments": [
                        {
                            "content_type": "application/pdf",
                            "filename": "provider-report.pdf",
                            "id": _ATTACHMENT_ID,
                            "size": self.reported_size,
                            "url": _CDN_LOCATION,
                        }
                    ]
                }
            )
        if request.full_url == _CDN_LOCATION:
            assert "authorization" not in headers
            return _Response(
                self.body,
                headers={"Content-Length": str(self.reported_size)},
            )
        raise AssertionError(f"unexpected Discord request: {request.full_url}")


class _DiscordUploadTransport(ConnectorTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.stream: dict[str, Any] | None = None

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.requests.append(kwargs)
        if kwargs["path"] == "/api/v10/users/@me":
            payload = {"bot": True, "id": _BOT_ID}
        elif kwargs["path"] == (f"/api/v10/channels/{_CHANNEL_ID}/messages/{_MESSAGE_ID}"):
            payload = {"attachments": [], "author": {"id": _BOT_ID}, "id": _MESSAGE_ID}
        else:
            raise AssertionError(f"unexpected Discord request: {kwargs['path']}")
        return ConnectorResponse(
            origin=ConnectorOrigin.DISCORD,
            status=200,
            headers={},
            body=json.dumps(payload).encode("utf-8"),
        )

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        source = kwargs["source"]
        assert not isinstance(source, (bytes, bytearray))
        iterator = iter(source)
        prefix = next(iterator)
        total = len(prefix)
        full_digest = hashlib.sha256(prefix)
        file_digest = hashlib.sha256()
        file_chunk_lengths: list[int] = []
        previous: bytes | None = None
        for chunk in iterator:
            assert isinstance(chunk, bytes)
            total += len(chunk)
            full_digest.update(chunk)
            if previous is not None:
                file_digest.update(previous)
                file_chunk_lengths.append(len(previous))
            previous = chunk
        assert previous is not None
        suffix = previous
        assert kwargs["content_length"] == total
        self.stream = {
            "content_length": kwargs["content_length"],
            "content_type": kwargs["content_type"],
            "credential": kwargs["credential"],
            "file_bytes": sum(file_chunk_lengths),
            "file_chunk_lengths": tuple(file_chunk_lengths),
            "file_sha256": file_digest.hexdigest(),
            "path": kwargs["path"],
            "prefix": prefix,
            "suffix": suffix,
        }
        return ConnectorStreamResponse(
            origin=ConnectorOrigin.DISCORD,
            status=200,
            headers={},
            bytes_transferred=total,
            sha256=full_digest.hexdigest(),
            control_body=b'{"id":"100000000000000003"}',
        )

    def request_provider_location(self, **kwargs: Any) -> ConnectorResponse:
        del kwargs
        raise AssertionError("prepared Discord upload must not use a materialized request")

    def download_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        del kwargs
        raise AssertionError("prepared Discord upload must not download")


class _NoDispatchTransport(ConnectorTransport):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append(kwargs)
        raise AssertionError("invalid transfer bindings must fail before provider dispatch")


def _json_response(payload: object) -> _Response:
    return _Response(
        json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _credential() -> ConnectorRuntimeCredential:
    return ConnectorRuntimeCredential(
        credential=ConnectorCredential(AuthorizationScheme.BOT, "discord-test-token"),
        granted_scopes=(),
        version=1,
    )


def _operation(mode: ConnectorMode, name: str) -> OperationSpec:
    return next(
        operation
        for operation in COLLABORATION_OPERATIONS
        if operation.provider == "discord" and operation.mode is mode and operation.name == name
    )


def _prepared_upload(tmp_path: Path, filename: str, content: bytes) -> PreparedUpload:
    path = tmp_path / filename
    path.write_bytes(content)
    return PreparedUpload(
        filename=filename,
        media_type="application/octet-stream",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        descriptor=os.open(path, os.O_RDONLY),
    )


def _download_input(*, delivery: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "attachment_id": _ATTACHMENT_ID,
        "channel_id": _CHANNEL_ID,
        "message_id": _MESSAGE_ID,
    }
    if delivery is not None:
        value["delivery"] = delivery
    return value


def _upload_input() -> dict[str, object]:
    return {
        "channel_id": _CHANNEL_ID,
        "filename": "large.bin",
        "message_id": _MESSAGE_ID,
    }


def test_discord_prepared_attachment_streams_exact_length_without_materializing_file_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"streamed-discord-attachment-" * (
        (2 * ARTIFACT_CHUNK_BYTES) // len(b"streamed-discord-attachment-") + 2
    )
    upload = _prepared_upload(tmp_path, "large.bin", content)
    transport = _DiscordUploadTransport()

    def reject_materialized_multipart(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("prepared file bytes must not enter the materialized multipart path")

    monkeypatch.setattr(collaboration_module, "_multipart_body", reject_materialized_multipart)
    try:
        result = CollaborationConnectorAdapter().execute(
            _operation(ConnectorMode.WRITE, "attachments.add"),
            _upload_input(),
            continuation=None,
            credential=_credential(),
            transport=transport,
            transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
        )
    finally:
        upload.close()

    assert result.payload == {"id": _MESSAGE_ID}
    assert transport.stream is not None
    stream = transport.stream
    assert stream["path"] == f"/api/v10/channels/{_CHANNEL_ID}/messages/{_MESSAGE_ID}"
    assert stream["credential"].scheme is AuthorizationScheme.BOT
    assert stream["file_bytes"] == len(content)
    assert stream["file_sha256"] == hashlib.sha256(content).hexdigest()
    assert len(stream["file_chunk_lengths"]) >= 3
    assert max(stream["file_chunk_lengths"]) <= ARTIFACT_CHUNK_BYTES
    boundary = stream["content_type"].removeprefix("multipart/form-data; boundary=")
    assert stream["prefix"].startswith(f"--{boundary}\r\n".encode("ascii"))
    assert stream["suffix"] == f"\r\n--{boundary}--\r\n".encode("ascii")
    assert stream["content_length"] == (
        len(stream["prefix"]) + len(content) + len(stream["suffix"])
    )
    assert [request["path"] for request in transport.requests] == [
        "/api/v10/users/@me",
        f"/api/v10/channels/{_CHANNEL_ID}/messages/{_MESSAGE_ID}",
    ]


def test_discord_download_defaults_to_provider_named_typed_sized_credentialless_artifact(
    tmp_path: Path,
) -> None:
    content = b"provider-owned Discord attachment"
    opener = _DiscordDownloadOpener(content)
    store = ArtifactStore(tmp_path / "artifacts")
    scope = ConnectorArtifactScope(store)
    try:
        result = CollaborationConnectorAdapter().execute(
            _operation(ConnectorMode.READ, "attachments.get"),
            _download_input(),
            continuation=None,
            credential=_credential(),
            transport=ConnectorTransport(opener=opener),
            transfer=ConnectorTransferContext(_artifact_scope_factory=lambda: scope),
        )
        assert result.payload == {
            "attachment_id": _ATTACHMENT_ID,
            "bytes": len(content),
            "delivery": "artifact",
        }
        assert result.artifact is not None
        assert result.artifact.filename == "provider-report.pdf"
        assert result.artifact.media_type == "application/pdf"
        assert result.artifact.size == len(content)
        assert result.artifact.path.read_bytes() == content
        scope.complete(result.artifact)
    finally:
        scope.close()
        store.close()

    cdn_request = next(request for request in opener.requests if request.full_url == _CDN_LOCATION)
    assert "authorization" not in {
        name.lower(): value for name, value in cdn_request.header_items()
    }


def test_discord_artifact_length_mismatch_aborts_without_a_residual_artifact(
    tmp_path: Path,
) -> None:
    opener = _DiscordDownloadOpener(b"ab", reported_size=3)
    artifact_root = tmp_path / "artifacts"
    store = ArtifactStore(artifact_root)
    scope = ConnectorArtifactScope(store)
    try:
        with pytest.raises(ValidationError, match="byte count does not match its length"):
            CollaborationConnectorAdapter().execute(
                _operation(ConnectorMode.READ, "attachments.get"),
                _download_input(),
                continuation=None,
                credential=_credential(),
                transport=ConnectorTransport(opener=opener),
                transfer=ConnectorTransferContext(_artifact_scope_factory=lambda: scope),
            )
        assert list(artifact_root.iterdir()) == []
    finally:
        scope.close()
        store.close()


def test_discord_inline_download_compatibility_remains_explicitly_bounded() -> None:
    content = b"small compatibility payload"
    opener = _DiscordDownloadOpener(content)
    result = CollaborationConnectorAdapter().execute(
        _operation(ConnectorMode.READ, "attachments.get"),
        _download_input(delivery="inline_chunk"),
        continuation=None,
        credential=_credential(),
        transport=ConnectorTransport(opener=opener),
    )

    assert result.artifact is None
    assert result.payload == {
        "attachment_id": _ATTACHMENT_ID,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "delivery": "inline_chunk",
    }

    oversized = b"x" * (collaboration_module._MAX_BINARY_BYTES + 1)
    with pytest.raises(ContinuityError, match="provider response exceeds its configured bound"):
        CollaborationConnectorAdapter().execute(
            _operation(ConnectorMode.READ, "attachments.get"),
            _download_input(delivery="inline_chunk"),
            continuation=None,
            credential=_credential(),
            transport=ConnectorTransport(opener=_DiscordDownloadOpener(oversized)),
        )


def test_discord_transfer_source_conflicts_fail_before_provider_dispatch(tmp_path: Path) -> None:
    upload = _prepared_upload(tmp_path, "large.bin", b"prepared")
    second = _prepared_upload(tmp_path, "second.bin", b"extra")
    transport = _NoDispatchTransport()
    cases = (
        (
            {**_upload_input(), "content_base64": base64.b64encode(b"inline").decode("ascii")},
            ConnectorTransferContext(uploads={("wrong",): upload}),
            "unexpected prepared-file binding",
        ),
        (
            _upload_input(),
            ConnectorTransferContext(uploads={("local_file",): upload, ("extra",): second}),
            "unexpected prepared-file binding",
        ),
        (
            {**_upload_input(), "content_base64": base64.b64encode(b"inline").decode("ascii")},
            ConnectorTransferContext(uploads={("local_file",): upload}),
            "multiple binary sources",
        ),
    )
    try:
        for input_value, transfer, message in cases:
            with pytest.raises(ValidationError, match=message):
                CollaborationConnectorAdapter().execute(
                    _operation(ConnectorMode.WRITE, "attachments.add"),
                    input_value,
                    continuation=None,
                    credential=_credential(),
                    transport=transport,
                    transfer=transfer,
                )
    finally:
        upload.close()
        second.close()

    assert transport.calls == []
