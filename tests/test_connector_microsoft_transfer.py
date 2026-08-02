from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.request import Request

import pytest

from continuity_kernel.connector_adapter import (
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_adapter_microsoft import MicrosoftConnectorAdapter
from continuity_kernel.connector_contract import ConnectorMode, OperationSpec
from continuity_kernel.connector_operations_microsoft import MICROSOFT_OPERATIONS
from continuity_kernel.connector_transfer import (
    ArtifactStore,
    ConnectorArtifactScope,
    PreparedUpload,
)
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorOutcomeUnknown,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
    ResponseLike,
)
from continuity_kernel.errors import ValidationError

_ORIGIN = ConnectorOrigin.MICROSOFT_GRAPH
_UPLOAD_URL = "https://outlook.office.com/upload-session/opaque?sig=redacted"


class _ReadableRequestBody(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


class _RecordingTransport:
    def __init__(
        self,
        *,
        responses: list[tuple[int, object, MappingProxyType[str, str]]] | None = None,
        metadata: object | None = None,
        download_body: bytes = b"artifact bytes",
        download_failure: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.stream_calls: list[dict[str, object]] = []
        self._responses = list(responses or [])
        self._metadata = metadata
        self._download_body = download_body
        self._download_failure = download_failure

    def request(self, **kwargs: object) -> ConnectorResponse:
        self.calls.append(kwargs)
        path = kwargs["path"]
        if self._metadata is not None and kwargs["method"] is ConnectorMethod.GET:
            body = json.dumps(self._metadata).encode("utf-8")
            return ConnectorResponse(_ORIGIN, 200, MappingProxyType({}), body)
        if self._responses:
            status, payload, headers = self._responses.pop(0)
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            return ConnectorResponse(_ORIGIN, status, headers, body)
        if isinstance(path, str) and path.endswith("/createUploadSession"):
            return ConnectorResponse(
                _ORIGIN,
                201,
                MappingProxyType({}),
                json.dumps({"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}).encode(
                    "utf-8"
                ),
            )
        return ConnectorResponse(_ORIGIN, 201, MappingProxyType({}), b'{"id":"uploaded"}')

    def request_stream(self, **kwargs: object) -> ConnectorStreamResponse:
        source = kwargs["source"]
        assert isinstance(source, PreparedUpload)
        offset = cast(int, kwargs["byte_offset"])
        length = cast(int, kwargs["content_length"])
        body = b"".join(source.iter_chunks(offset=offset, length=length))
        self.stream_calls.append({**kwargs, "body": body})
        if self._responses:
            status, payload, headers = self._responses.pop(0)
            control_body = (
                payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            )
        else:
            status, control_body, headers = 201, b"", MappingProxyType({})
        return ConnectorStreamResponse(
            _ORIGIN,
            status,
            headers,
            len(body),
            hashlib.sha256(body).hexdigest(),
            control_body=control_body,
        )

    def download_stream(self, **kwargs: object) -> ConnectorStreamResponse:
        self.stream_calls.append(kwargs)
        sink = kwargs["sink"]
        writer = cast(Any, sink)
        writer.write(self._download_body)
        if self._download_failure is not None:
            raise self._download_failure
        receipt = writer.finish()
        return ConnectorStreamResponse(
            _ORIGIN,
            200,
            MappingProxyType({}),
            len(self._download_body),
            hashlib.sha256(self._download_body).hexdigest(),
            artifact=receipt,
        )


class _HTTPResponse:
    def __init__(self, status: int, body: bytes, headers: MappingProxyType[str, str]) -> None:
        self.status = status
        self.headers: object = headers
        self._body = body
        self._offset = 0

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._body) - self._offset
        result = self._body[self._offset : self._offset + amount]
        self._offset += len(result)
        return result

    def __enter__(self) -> _HTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _HTTPRecorder:
    def __init__(self, responses: list[tuple[int, bytes, MappingProxyType[str, str]]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: Request, timeout: float) -> ResponseLike:
        del timeout
        body = request.data
        if body is None:
            encoded = b""
        elif isinstance(body, bytes):
            encoded = body
        else:
            reader = cast(_ReadableRequestBody, body)
            chunks: list[bytes] = []
            while True:
                block = reader.read(1024 * 1024)
                if not block:
                    break
                chunks.append(block)
            encoded = b"".join(chunks)
        self.requests.append(
            {
                "body": encoded,
                "headers": dict(request.headers),
                "method": request.method,
                "url": request.full_url,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected provider request")
        status, response_body, headers = self.responses.pop(0)
        return _HTTPResponse(status, response_body, headers)


def _credential() -> ConnectorRuntimeCredential:
    return ConnectorRuntimeCredential(
        credential=ConnectorCredential(AuthorizationScheme.BEARER, "test-secret"),
        granted_scopes=("Mail.ReadWrite", "Calendars.ReadWrite"),
        version=1,
    )


def _operation(provider: str, mode: ConnectorMode, name: str) -> OperationSpec:
    return next(
        operation
        for operation in MICROSOFT_OPERATIONS
        if operation.provider == provider and operation.mode is mode and operation.name == name
    )


def _local_input(
    *,
    provider: str = "outlook_mail",
    grant_id: str = "grant-1",
    relative_path: str = "payload.bin",
    content_type: str = "application/octet-stream",
) -> dict[str, object]:
    attachment = {
        "content_type": content_type,
        "local_file": {"grant_id": grant_id, "relative_path": relative_path},
        "name": "payload.bin",
    }
    if provider == "outlook_mail":
        return {"attachment": attachment, "message_id": "message-1"}
    return {
        "attachment": attachment,
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
    }


def _prepared_upload(
    tmp_path: Path,
    content: bytes,
    *,
    filename: str = "payload.bin",
) -> PreparedUpload:
    source = tmp_path / filename
    source.write_bytes(content)
    return PreparedUpload(
        filename=filename,
        media_type="application/octet-stream",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        descriptor=os.open(source, os.O_RDONLY),
    )


def _transfer(
    upload: PreparedUpload,
    *,
    path: tuple[str | int, ...] = ("attachment", "local_file"),
) -> ConnectorTransferContext:
    return ConnectorTransferContext(uploads={path: upload})


def test_small_and_zero_local_files_use_direct_attachment_posts(tmp_path: Path) -> None:
    operation = _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add")
    for content in (b"hello", b""):
        upload = _prepared_upload(tmp_path, content, filename=f"payload-{len(content)}.bin")
        transport = _RecordingTransport()
        try:
            result = MicrosoftConnectorAdapter().execute(
                operation,
                _local_input(),
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
                transfer=_transfer(upload),
            )
        finally:
            upload.close()
        assert result.payload == {"id": "uploaded"}
        assert transport.stream_calls == []
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["method"] is ConnectorMethod.POST
        assert call["path"] == "/v1.0/me/messages/message-1/attachments"
        assert call["expected_statuses"] == frozenset({201})
        body = cast(dict[str, object], call["json_body"])
        assert body["contentBytes"] == base64.b64encode(content).decode("ascii")
        assert "local_file" not in body
        assert call["credential"] == _credential().credential


def test_calendar_local_file_keeps_event_preflight_and_if_match(tmp_path: Path) -> None:
    upload = _prepared_upload(tmp_path, b"calendar attachment")
    transport = _RecordingTransport(
        metadata={
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-1",
            "id": "event-1",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        }
    )
    try:
        MicrosoftConnectorAdapter().execute(
            _operation("outlook_calendar", ConnectorMode.WRITE, "attachments.add"),
            _local_input(provider="outlook_calendar"),
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
            transfer=_transfer(upload),
        )
    finally:
        upload.close()

    assert [call["path"] for call in transport.calls] == [
        "/v1.0/me/calendar/events/event-1",
        "/v1.0/me/calendar/events/event-1/attachments",
    ]
    assert transport.calls[-1]["headers"] == {
        "If-Match": "event-version-1",
        "Prefer": 'IdType="ImmutableId"',
    }


def test_calendar_large_file_uses_the_documented_event_upload_session_route(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(tmp_path, b"x" * (3 * 1000**2))
    transport = _RecordingTransport(
        metadata={
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-1",
            "id": "event-1",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        },
        responses=[
            (201, {"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
            (
                201,
                b"",
                MappingProxyType(
                    {
                        "Location": (
                            "https://outlook.office.com/v1.0/me/Attachments('calendar-attachment')"
                        )
                    }
                ),
            ),
        ],
    )
    try:
        result = MicrosoftConnectorAdapter().execute(
            _operation("outlook_calendar", ConnectorMode.WRITE, "attachments.add"),
            _local_input(
                provider="outlook_calendar",
                content_type="text/plain; charset=utf-8",
            ),
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
            transfer=_transfer(upload),
        )
    finally:
        upload.close()

    assert isinstance(result.payload, dict)
    assert result.payload["attachment_id"] == "calendar-attachment"
    assert [call["path"] for call in transport.calls] == [
        "/v1.0/me/calendar/events/event-1",
        "/v1.0/me/events/event-1/attachments/createUploadSession",
    ]
    assert transport.calls[-1]["json_body"] == {
        "AttachmentItem": {
            "attachmentType": "file",
            "contentType": "text/plain; charset=utf-8",
            "name": "payload.bin",
            "size": 3 * 1000**2,
        }
    }
    assert len(transport.stream_calls) == 1


def test_direct_upload_strips_a_large_content_bytes_echo_after_commit(tmp_path: Path) -> None:
    content = b"x" * 200_000
    upload = _prepared_upload(tmp_path, content)
    transport = _RecordingTransport(
        responses=[
            (
                201,
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "contentBytes": base64.b64encode(content).decode("ascii"),
                    "id": "uploaded",
                    "name": "payload.bin",
                },
                MappingProxyType({}),
            ),
        ]
    )
    try:
        result = MicrosoftConnectorAdapter().execute(
            _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
            _local_input(),
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
            transfer=_transfer(upload),
        )
    finally:
        upload.close()

    assert result.payload == {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": "uploaded",
        "name": "payload.bin",
    }


def test_large_upload_uses_credentialless_exact_ranges_and_parses_attachments_location(
    tmp_path: Path,
) -> None:
    content = b"x" * (6 * 1024 * 1024 + 7)
    responses = [
        (201, {"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
        (200, {"NextExpectedRanges": ["3145728"]}, MappingProxyType({})),
        (200, {"nextExpectedRanges": ["6291456-"]}, MappingProxyType({})),
        (
            201,
            b"",
            MappingProxyType(
                {"Location": "https://outlook.office.com/v1.0/me/Attachments('opaque-id')"}
            ),
        ),
    ]
    recorder = _HTTPRecorder(
        [
            (
                status,
                payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
                headers,
            )
            for status, payload, headers in responses
        ]
    )
    transport = ConnectorTransport(opener=recorder)
    upload = _prepared_upload(tmp_path, content)
    try:
        result = MicrosoftConnectorAdapter().execute(
            _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
            _local_input(),
            continuation=None,
            credential=_credential(),
            transport=transport,
            transfer=_transfer(upload),
        )
    finally:
        upload.close()

    assert result.payload == {
        "attachment_id": "opaque-id",
        "bytes": len(content),
        "status": "completed",
    }
    assert len(recorder.requests) == 4
    session_request, first, second, final = recorder.requests
    assert session_request["method"] == "POST"
    assert (
        session_request["url"]
        == "https://graph.microsoft.com/v1.0/me/messages/message-1/attachments/createUploadSession"
    )
    stream_requests = (first, second, final)
    assert [request["url"] for request in stream_requests] == [_UPLOAD_URL] * 3
    assert all(
        not any(
            name.casefold() == "authorization" for name in cast(dict[str, str], request["headers"])
        )
        for request in stream_requests
    )
    assert [
        next(
            value
            for name, value in cast(dict[str, str], request["headers"]).items()
            if name.casefold() == "content-range"
        )
        for request in stream_requests
    ] == [
        f"bytes 0-3145727/{len(content)}",
        f"bytes 3145728-6291455/{len(content)}",
        f"bytes 6291456-6291462/{len(content)}",
    ]
    assert [len(cast(bytes, request["body"])) for request in stream_requests] == [
        3 * 1024 * 1024,
        3 * 1024 * 1024,
        7,
    ]


@pytest.mark.parametrize(
    "intermediate",
    [
        {"nextExpectedRanges": ["3145727"]},
        {"nextExpectedRanges": ["3145728-4000000"]},
        {"nextExpectedRanges": ["3145728", "6291456-"]},
        {
            "NextExpectedRanges": ["3145728"],
            "nextExpectedRanges": ["3145728"],
        },
    ],
)
def test_divergent_or_ambiguous_next_ranges_fail_closed_without_retry(
    tmp_path: Path,
    intermediate: dict[str, object],
) -> None:
    content = b"x" * (3 * 1024 * 1024 + 1)
    upload = _prepared_upload(tmp_path, content)
    transport = _RecordingTransport(
        responses=[
            (201, {"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
            (200, intermediate, MappingProxyType({})),
        ]
    )
    try:
        with pytest.raises(ValidationError, match=r"next byte|ambiguous|invalid"):
            MicrosoftConnectorAdapter().execute(
                _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
                _local_input(),
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
                transfer=_transfer(upload),
            )
    finally:
        upload.close()
    assert len(transport.stream_calls) == 1


def test_upload_session_requires_the_literal_initial_zero_range(tmp_path: Path) -> None:
    upload = _prepared_upload(tmp_path, b"x" * (3 * 1024 * 1024))
    transport = _RecordingTransport(
        responses=[
            (201, {"nextExpectedRanges": ["00-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
        ]
    )
    try:
        with pytest.raises(ValidationError, match=r"start at byte zero"):
            MicrosoftConnectorAdapter().execute(
                _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
                _local_input(),
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
                transfer=_transfer(upload),
            )
    finally:
        upload.close()
    assert transport.stream_calls == []


def test_final_upload_requires_201_even_when_the_last_response_is_200(tmp_path: Path) -> None:
    content = b"x" * (3 * 1024 * 1024 + 1)
    upload = _prepared_upload(tmp_path, content)
    transport = _RecordingTransport(
        responses=[
            (201, {"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
            (200, {"nextExpectedRanges": ["3145728"]}, MappingProxyType({})),
            (200, {"nextExpectedRanges": ["3145729"]}, MappingProxyType({})),
        ]
    )
    try:
        with pytest.raises(ValidationError, match=r"final.*201"):
            MicrosoftConnectorAdapter().execute(
                _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
                _local_input(),
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
                transfer=_transfer(upload),
            )
    finally:
        upload.close()


def test_early_201_is_an_unknown_outcome_that_must_not_be_retried(tmp_path: Path) -> None:
    upload = _prepared_upload(tmp_path, b"x" * (6 * 1024 * 1024))
    transport = _RecordingTransport(
        responses=[
            (201, {"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
            (
                201,
                b"",
                MappingProxyType(
                    {"Location": ("https://outlook.office.com/v1.0/me/Attachments('ambiguous')")}
                ),
            ),
        ]
    )
    try:
        with pytest.raises(ConnectorOutcomeUnknown, match=r"must not be retried"):
            MicrosoftConnectorAdapter().execute(
                _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
                _local_input(),
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
                transfer=_transfer(upload),
            )
    finally:
        upload.close()
    assert len(transport.stream_calls) == 1


def test_unknown_but_pinned_final_location_returns_only_a_bounded_completion_receipt(
    tmp_path: Path,
) -> None:
    content = b"x" * (3 * 1024 * 1024 + 1)
    upload = _prepared_upload(tmp_path, content)
    transport = _RecordingTransport(
        responses=[
            (201, {"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
            (200, {"nextExpectedRanges": ["3145728"]}, MappingProxyType({})),
            (
                201,
                b"",
                MappingProxyType({"Location": "https://outlook.office.com/v1.0/me/opaque-result"}),
            ),
        ]
    )
    try:
        result = MicrosoftConnectorAdapter().execute(
            _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
            _local_input(),
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
            transfer=_transfer(upload),
        )
    finally:
        upload.close()
    assert result.payload == {"bytes": len(content), "status": "completed"}


def test_final_upload_requires_a_safe_location_receipt(tmp_path: Path) -> None:
    content = b"x" * (3 * 1024 * 1024)
    upload = _prepared_upload(tmp_path, content)
    transport = _RecordingTransport(
        responses=[
            (201, {"nextExpectedRanges": ["0-"], "uploadUrl": _UPLOAD_URL}, MappingProxyType({})),
            (201, b"", MappingProxyType({})),
        ]
    )
    try:
        with pytest.raises(ConnectorOutcomeUnknown, match=r"must not be retried"):
            MicrosoftConnectorAdapter().execute(
                _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add"),
                _local_input(),
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
                transfer=_transfer(upload),
            )
    finally:
        upload.close()


def test_attachment_metadata_select_is_concrete_and_content_bytes_are_stripped() -> None:
    metadata = {
        "value": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "contentBytes": "must-not-escape",
                "contentType": "text/plain",
                "id": "attachment-1",
                "name": "hello.txt",
            }
        ]
    }
    transport = _RecordingTransport(metadata=metadata)
    result = MicrosoftConnectorAdapter().execute(
        _operation("outlook_mail", ConnectorMode.READ, "attachments.list"),
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert result.payload == {
        "value": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "contentType": "text/plain",
                "id": "attachment-1",
                "name": "hello.txt",
            }
        ]
    }
    assert transport.calls[0]["query"] == (
        (
            "$select",
            "id,lastModifiedDateTime,name,contentType,size,isInline,contentId,contentLocation",
        ),
    )


def test_attachment_continuation_preserves_the_exact_metadata_projection() -> None:
    select = "id,lastModifiedDateTime,name,contentType,size,isInline,contentId,contentLocation"
    next_link = (
        "https://graph.microsoft.com/v1.0/me/messages/message-1/attachments"
        f"?$skiptoken=next-page&$select={select}"
    )
    transport = _RecordingTransport(metadata={"@odata.nextLink": next_link, "value": []})
    operation = _operation("outlook_mail", ConnectorMode.READ, "attachments.list")

    first = MicrosoftConnectorAdapter().execute(
        operation,
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert first.continuation is not None
    MicrosoftConnectorAdapter().execute(
        operation,
        {"message_id": "message-1"},
        continuation=first.continuation,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert transport.calls[-1]["query"] == (
        ("$select", select),
        ("$skiptoken", "next-page"),
    )


def test_attachment_continuation_rejects_a_changed_metadata_projection() -> None:
    transport = _RecordingTransport(
        metadata={
            "@odata.nextLink": (
                "https://graph.microsoft.com/v1.0/me/messages/message-1/attachments"
                "?$skiptoken=next-page&$select=id,contentBytes"
            ),
            "value": [],
        }
    )
    with pytest.raises(ConnectorProviderError, match=r"invalid_next_link"):
        MicrosoftConnectorAdapter().execute(
            _operation("outlook_mail", ConnectorMode.READ, "attachments.list"),
            {"message_id": "message-1"},
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )


def test_mime_and_attachment_artifacts_stream_value_routes_and_abort_on_failure(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    scope = ConnectorArtifactScope(store)
    transfer = ConnectorTransferContext(_artifact_scope_factory=lambda: scope)
    body = b"MIME or raw attachment bytes"
    try:
        mime_transport = _RecordingTransport(download_body=body)
        mime = MicrosoftConnectorAdapter().execute(
            _operation("outlook_mail", ConnectorMode.READ, "messages.mime"),
            {"delivery": "artifact", "message_id": "message-1"},
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, mime_transport),
            transfer=transfer,
        )
        assert mime.payload == {"bytes": len(body), "delivery": "artifact"}
        assert mime.artifact is not None
        assert mime.artifact.path.read_bytes() == body
        assert mime_transport.stream_calls[0]["path"] == "/v1.0/me/messages/message-1/$value"
        assert mime_transport.stream_calls[0]["max_bytes"] == 150 * 1024**2
        scope.complete(mime.artifact)

        attachment_scope = ConnectorArtifactScope(store)
        attachment_transfer = ConnectorTransferContext(
            _artifact_scope_factory=lambda: attachment_scope
        )
        attachment_transport = _RecordingTransport(
            metadata={"contentType": "application/octet-stream", "name": "raw.bin"},
            download_body=body,
        )
        attachment = MicrosoftConnectorAdapter().execute(
            _operation("outlook_mail", ConnectorMode.READ, "attachments.get"),
            {
                "attachment_id": "attachment-1",
                "delivery": "artifact",
                "message_id": "message-1",
            },
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, attachment_transport),
            transfer=attachment_transfer,
        )
        assert attachment.artifact is not None
        assert attachment.artifact.path.read_bytes() == body
        assert attachment_transport.stream_calls[0]["path"] == (
            "/v1.0/me/messages/message-1/attachments/attachment-1/$value"
        )
        assert attachment_transport.stream_calls[0]["max_bytes"] == 150 * 1024**2
        attachment_scope.complete(attachment.artifact)

        failing_scope = ConnectorArtifactScope(store)
        failing_transport = _RecordingTransport(
            download_body=b"partial",
            download_failure=ConnectorProviderError(
                origin=_ORIGIN,
                status=503,
                code="temporary",
            ),
        )
        with pytest.raises(ConnectorProviderError):
            MicrosoftConnectorAdapter().execute(
                _operation("outlook_mail", ConnectorMode.READ, "messages.mime"),
                {"delivery": "artifact", "message_id": "message-1"},
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, failing_transport),
                transfer=ConnectorTransferContext(_artifact_scope_factory=lambda: failing_scope),
            )
        failing_scope.close()
        assert {path for path in store.root.iterdir() if path.is_file()} == {
            mime.artifact.path,
            attachment.artifact.path,
        }
    finally:
        scope.close()
        store.close()


def test_transfer_bindings_fail_before_any_provider_dispatch(tmp_path: Path) -> None:
    upload = _prepared_upload(tmp_path, b"bound")
    adapter = MicrosoftConnectorAdapter()
    operation = _operation("outlook_mail", ConnectorMode.WRITE, "attachments.add")
    try:
        for transfer, input_value in (
            (
                ConnectorTransferContext(uploads={("wrong",): upload}),
                _local_input(),
            ),
            (ConnectorTransferContext(), _local_input()),
            (
                _transfer(upload),
                {
                    "attachment": {
                        "content_base64": "Yg==",
                        "content_type": "application/octet-stream",
                        "name": "payload.bin",
                    },
                    "message_id": "message-1",
                },
            ),
        ):
            transport = _RecordingTransport()
            with pytest.raises(
                ValidationError,
                match=r"binding|binary sources|transfer context",
            ):
                adapter.execute(
                    operation,
                    input_value,
                    continuation=None,
                    credential=_credential(),
                    transport=cast(ConnectorTransport, transport),
                    transfer=transfer,
                )
            assert transport.calls == []
            assert transport.stream_calls == []
    finally:
        upload.close()
