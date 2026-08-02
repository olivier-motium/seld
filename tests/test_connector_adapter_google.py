from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel.connector_adapter import ConnectorRuntimeCredential, ConnectorTransferContext
from continuity_kernel.connector_adapter_google import GoogleConnectorAdapter
from continuity_kernel.connector_contract import ConnectorEffect, OperationSpec
from continuity_kernel.connector_gmail_transfer import GMAIL_UPLOAD_MAX_BYTES
from continuity_kernel.connector_operations_google import GOOGLE_OPERATIONS
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
)
from continuity_kernel.errors import ContinuityError, ValidationError


class _Transport(ConnectorTransport):
    def __init__(
        self,
        *,
        body: bytes = b"{}",
        bodies: Sequence[bytes] = (),
        headers: Mapping[str, str] | None = None,
        download_body: bytes | None = None,
    ) -> None:
        self.body = body
        self.bodies = list(bodies)
        self.headers = dict(headers or {})
        self.download_body = body if download_body is None else download_body
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append({"kind": "request", **kwargs})
        body = self.bodies.pop(0) if self.bodies else self.body
        headers = dict(self.headers)
        status = 200
        if kwargs["path"].startswith("/upload/drive/v3/"):
            headers = {"location": "https://www.googleapis.com/upload/session/one"}
        if kwargs["path"].startswith("/upload/gmail/v1/"):
            headers = {"location": "https://gmail.googleapis.com/upload/session/one"}
        request_headers = kwargs.get("headers")
        if isinstance(request_headers, Mapping) and isinstance(request_headers.get("Range"), str):
            range_value = request_headers["Range"]
            start = int(range_value.removeprefix("bytes=").split("-", 1)[0])
            headers.setdefault(
                "content-range",
                f"bytes {start}-{start + len(body) - 1}/{start + len(body)}",
            )
            status = 206
        return ConnectorResponse(kwargs["origin"], status, headers, body)

    def request_provider_location(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append({"kind": "provider_location", **kwargs})
        return ConnectorResponse(kwargs["origin"], 200, self.headers, self.body)

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        self.calls.append({"kind": "stream", **kwargs})
        source = kwargs["source"]
        if isinstance(source, PreparedUpload):
            body = b"".join(source.iter_chunks())
        else:
            body = b"".join(source)
        return ConnectorStreamResponse(
            kwargs["origin"],
            200,
            self.headers,
            len(body),
            hashlib.sha256(body).hexdigest(),
            control_body=self.body,
        )

    def download_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        self.calls.append({"kind": "download_stream", **kwargs})
        sink = kwargs["sink"]
        body = self.download_body
        path = kwargs.get("path")
        if (
            isinstance(path, str)
            and path.startswith("/gmail/v1/users/me/messages/")
            and body == b"{}"
        ):
            body = b'{"data":"","size":0}'
        sink.write(body)
        finish = getattr(sink, "finish", None)
        artifact = finish() if callable(finish) else None
        status = 206 if kwargs.get("range_start") is not None else 200
        headers = dict(self.headers)
        if status == 206 and "content-range" not in headers:
            start = kwargs["range_start"]
            headers["content-range"] = f"bytes {start}-{start + len(body) - 1}/{start + len(body)}"
        return ConnectorStreamResponse(
            kwargs["origin"],
            status,
            headers,
            len(body),
            hashlib.sha256(body).hexdigest(),
            artifact=artifact,
        )


class _ProviderErrorTransport(_Transport):
    def __init__(self, error: ConnectorProviderError) -> None:
        super().__init__()
        self.error = error

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append({"kind": "request", **kwargs})
        raise self.error


class _DriveRecoveryTransport(ConnectorTransport):
    def __init__(
        self,
        *,
        stream_outcomes: Sequence[ConnectorStreamResponse | Exception],
        probe_outcomes: Sequence[ConnectorStreamResponse | Exception] = (),
    ) -> None:
        self.stream_outcomes = list(stream_outcomes)
        self.probe_outcomes = list(probe_outcomes)
        self.calls: list[dict[str, Any]] = []
        self.sessions = 0

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.sessions += 1
        self.calls.append({"kind": "request", **kwargs})
        return ConnectorResponse(
            kwargs["origin"],
            200,
            {"location": f"https://www.googleapis.com/upload/session/{self.sessions}"},
            b"{}",
        )

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        source = kwargs["source"]
        if isinstance(source, PreparedUpload):
            body = b"".join(
                source.iter_chunks(
                    offset=kwargs.get("byte_offset", 0),
                    length=kwargs["content_length"],
                )
            )
        else:
            body = b"".join(source)
        self.calls.append({"kind": "stream", "sent_body": body, **kwargs})
        outcome = self.stream_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def probe_resumable_upload(self, **kwargs: Any) -> ConnectorStreamResponse:
        self.calls.append({"kind": "probe", **kwargs})
        outcome = self.probe_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _credential() -> ConnectorRuntimeCredential:
    return ConnectorRuntimeCredential(
        credential=ConnectorCredential(AuthorizationScheme.BEARER, "test-secret"),
        granted_scopes=(
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/drive",
        ),
        version=1,
    )


def _operation(provider: str, name: str) -> OperationSpec:
    return next(
        item for item in GOOGLE_OPERATIONS if item.provider == provider and item.name == name
    )


def _event_time() -> dict[str, str]:
    return {"date_time": "2026-08-01T09:00:00+02:00", "time_zone": "Europe/Brussels"}


def _prepared_upload(
    tmp_path: Path,
    content: bytes = b"streamed Google Drive content",
) -> PreparedUpload:
    path = tmp_path / "private-source.bin"
    path.write_bytes(content)
    descriptor = os.open(path, os.O_RDONLY)
    return PreparedUpload(
        filename="source.bin",
        media_type="application/octet-stream",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        descriptor=descriptor,
    )


def _drive_stream_response(
    status: int,
    *,
    next_offset: int | None = None,
    body: bytes = b"",
) -> ConnectorStreamResponse:
    return ConnectorStreamResponse(
        ConnectorOrigin.GOOGLE,
        status,
        {},
        0,
        None,
        next_offset=next_offset,
        control_body=body,
    )


def _drive_download_operation_body(
    *,
    done: bool | None,
    name: str = "operations/download-1",
    resource_key: str | None = None,
    response: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
) -> bytes:
    operation: dict[str, object] = {"name": name}
    if done is not None:
        operation["done"] = done
    if resource_key is not None:
        operation["metadata"] = {"resourceKey": resource_key}
    if response is not None:
        operation["response"] = dict(response)
    if error is not None:
        operation["error"] = dict(error)
    return json.dumps(operation).encode()


def _drive_download_response(
    *,
    partial: bool | None,
    location: str = "https://drive.usercontent.google.com/download?ticket=one",
) -> dict[str, object]:
    response: dict[str, object] = {
        "@type": "type.googleapis.com/google.apps.drive.v3.DownloadFileResponse",
        "downloadUri": location,
    }
    if partial is not None:
        response["partialDownloadAllowed"] = partial
    return response


def test_google_local_file_limit_hook_allows_only_sanitized_provider_shapes() -> None:
    adapter = GoogleConnectorAdapter()
    gmail_operation = _operation("gmail", "drafts.create")
    gmail_input = {
        "attachments": [
            {
                "filename": "note.txt",
                "local_file": "opaque-local-file",
                "mime_type": "text/plain",
            }
        ],
        "text_body": "body",
    }
    assert (
        adapter.max_local_file_bytes(
            gmail_operation,
            gmail_input,
            path=("attachments", 0, "local_file"),
        )
        == (GMAIL_UPLOAD_MAX_BYTES * 3) // 4
    )

    drive_operation = _operation("google_drive", "files.update")
    drive_input = {
        "file_id": "file",
        "local_file": "opaque-local-file",
        "mime_type": "text/plain",
    }
    assert adapter.max_local_file_bytes(drive_operation, drive_input, path=("local_file",)) == (
        5 * 1024**4
    )

    rejected = (
        (gmail_operation, gmail_input, ("local_file",)),
        (
            gmail_operation,
            {"attachments": [{"mime_type": "text/plain"}]},
            ("attachments", 0, "local_file"),
        ),
        (
            gmail_operation,
            {
                "attachments": [
                    {
                        "filename": "note.txt",
                        "local_file": {"grant_id": "opaque", "relative_path": "note.txt"},
                        "mime_type": "text/plain",
                    }
                ]
            },
            ("attachments", 0, "local_file"),
        ),
        (_operation("gmail", "drafts.send"), gmail_input, ("attachments", 0, "local_file")),
        (
            _operation("google_calendar", "events.create"),
            drive_input,
            ("local_file",),
        ),
        (drive_operation, drive_input, ("attachments", 0, "local_file")),
        (drive_operation, {"mime_type": "text/plain"}, ("local_file",)),
    )
    for operation, input_value, path in rejected:
        with pytest.raises(ValidationError, match="local-file upload path"):
            adapter.max_local_file_bytes(operation, input_value, path=path)


def _sample(operation: OperationSpec) -> dict[str, object]:
    name = operation.name
    if operation.provider == "gmail":
        if name in {"labels.list", "profile.get"}:
            return {}
        if name == "history.list":
            return {"page_size": 1, "start_history_id": "1"}
        if name.endswith(".list"):
            return {"page_size": 1}
        if name == "attachments.get":
            return {
                "attachment_id": "attachment",
                "delivery": "inline_chunk",
                "message_id": "message",
            }
        if name in {
            "messages.get",
            "messages.modify",
            "messages.trash",
            "messages.restore",
            "messages.purge",
        }:
            return {"message_id": "message"}
        if name in {
            "threads.get",
            "threads.modify",
            "threads.trash",
            "threads.restore",
            "threads.purge",
        }:
            return {"thread_id": "thread"}
        if name == "drafts.create":
            return {"text_body": "hello", "to": ["recipient@example.test"]}
        if name in {"drafts.get", "drafts.update", "drafts.delete", "drafts.send"}:
            return {"draft_id": "draft"}
        if name == "labels.create":
            return {"name": "Projects"}
        if name in {"labels.get", "labels.update", "labels.delete"}:
            return {"label_id": "label"}
    if operation.provider == "google_calendar":
        if name == "calendars.list":
            return {"page_size": 1}
        if name == "calendars.get":
            return {"calendar_id": "primary"}
        if name == "events.list":
            return {"calendar_id": "primary", "page_size": 1}
        if name in {"events.get", "events.instances"}:
            return {"calendar_id": "primary", "event_id": "event"}
        if name == "freebusy.query":
            return {
                "calendar_ids": ["primary"],
                "time_max": "2026-08-02T00:00:00+02:00",
                "time_min": "2026-08-01T00:00:00+02:00",
                "time_zone": "Europe/Brussels",
            }
        if name == "calendars.create":
            return {"summary": "Calendar", "time_zone": "Europe/Brussels"}
        if name == "calendars.update":
            return {"calendar_id": "primary", "etag": "etag"}
        if name == "calendars.delete":
            return {"calendar_id": "primary", "etag": "etag"}
        if name == "events.create":
            return {"calendar_id": "primary", "end": _event_time(), "start": _event_time()}
        if name == "events.update":
            return {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
        if name == "events.move":
            return {
                "calendar_id": "primary",
                "destination_calendar_id": "primary",
                "event_id": "event",
            }
        if name == "events.respond":
            return {"calendar_id": "primary", "event_id": "event", "response_status": "accepted"}
        if name == "events.delete":
            return {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    if operation.provider == "google_drive":
        if name in {"drives.list", "files.list"}:
            return {"page_size": 1}
        if name in {"permissions.list", "comments.list", "revisions.list"}:
            return {"file_id": "file", "page_size": 1}
        if name == "replies.list":
            return {"comment_id": "comment", "file_id": "file", "page_size": 1}
        if name == "files.get":
            return {"file_id": "file"}
        if name in {"files.content", "files.download"}:
            return {"delivery": "inline_chunk", "file_id": "file"}
        if name == "files.export":
            return {
                "delivery": "inline_chunk",
                "export_mime_type": "text/plain",
                "file_id": "file",
            }
        if name == "revisions.download":
            return {
                "delivery": "inline_chunk",
                "file_id": "file",
                "revision_id": "revision",
            }
        if name == "files.create":
            return {"mime_type": "text/plain", "name": "plan.txt"}
        if name == "files.update":
            return {"file_id": "file", "name": "renamed.txt"}
        if name == "files.copy":
            return {"file_id": "file"}
        if name == "files.move":
            return {"add_parent_ids": ["parent"], "file_id": "file"}
        if name in {"files.trash", "files.restore", "files.purge"}:
            return {"file_id": "file"}
        if name == "permissions.create":
            return {
                "email_address": "person@example.test",
                "file_id": "file",
                "permission_type": "user",
                "role": "reader",
            }
        if name == "permissions.update":
            return {
                "file_id": "file",
                "permission_id": "permission",
                "role": "reader",
            }
        if name == "permissions.delete":
            return {"file_id": "file", "permission_id": "permission"}
        if name == "comments.create":
            return {"content": "note", "file_id": "file"}
        if name == "comments.update":
            return {"comment_id": "comment", "content": "note", "file_id": "file"}
        if name == "comments.delete":
            return {"comment_id": "comment", "file_id": "file"}
        if name == "replies.create":
            return {"comment_id": "comment", "content": "note", "file_id": "file"}
        if name == "replies.update":
            return {
                "comment_id": "comment",
                "content": "note",
                "file_id": "file",
                "reply_id": "reply",
            }
        if name == "replies.delete":
            return {"comment_id": "comment", "file_id": "file", "reply_id": "reply"}
        if name == "revisions.keep":
            return {"file_id": "file", "keep_forever": True, "revision_id": "revision"}
        if name == "revisions.delete":
            return {"file_id": "file", "revision_id": "revision"}
    raise AssertionError(f"no sample for {operation.provider}:{name}")


def test_every_google_operation_uses_only_bounded_fixed_adapter_requests() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    credential = _credential()
    for operation in GOOGLE_OPERATIONS:
        transport.body = b"{}"
        before = len(transport.calls)
        expected_requests = 1
        if operation.provider == "google_calendar" and operation.name in {
            "events.move",
            "events.update",
        }:
            expected_requests = 2
        if operation.provider == "google_calendar" and operation.name == "events.respond":
            transport.body = json.dumps(
                {
                    "attendees": [{"email": "owner@example.test", "self": True}],
                    "etag": "event-etag",
                }
            ).encode()
            expected_requests = 2
        if operation.provider == "google_drive" and operation.name == "files.download":
            transport.body = _drive_download_operation_body(
                done=True,
                response=_drive_download_response(partial=True),
            )
            expected_requests = 2
        adapter.execute(
            operation,
            _sample(operation),
            continuation=None,
            credential=credential,
            transport=transport,
        )
        assert len(transport.calls) == before + expected_requests
    assert len(transport.calls) == len(GOOGLE_OPERATIONS) + 4
    assert {call["origin"] for call in transport.calls} == {
        ConnectorOrigin.GMAIL,
        ConnectorOrigin.GOOGLE,
    }
    assert all(
        ("path" not in call or call["path"].startswith("/"))
        and (
            "location" not in call
            or call["location"].startswith("https://drive.usercontent.google.com/")
        )
        for call in transport.calls
    )
    assert all("url" not in call and "token" not in call for call in transport.calls)


def test_only_top_level_pagination_is_stripped_and_replayed_as_runtime_continuation() -> None:
    body = json.dumps(
        {
            "appProperties": {"location": "HQ", "nextPageToken": "business-value"},
            "items": [{"id": "one"}],
            "location": "Brussels",
            "nextLink": "https://provider.example/next",
            "nextPageToken": "provider-page",
            "uploadUrl": "https://provider.example/upload",
        }
    ).encode("utf-8")
    adapter = GoogleConnectorAdapter()
    transport = _Transport(body=body)
    operation = _operation("gmail", "messages.list")
    first = adapter.execute(
        operation,
        {"page_size": 1},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert first.payload == {
        "appProperties": {"location": "HQ", "nextPageToken": "business-value"},
        "items": [{"id": "one"}],
        "location": "Brussels",
        "uploadUrl": "https://provider.example/upload",
    }
    assert first.continuation == "provider-page"

    adapter.execute(
        operation,
        {"page_size": 1},
        continuation=first.continuation,
        credential=_credential(),
        transport=transport,
    )
    assert ("pageToken", "provider-page") in transport.calls[-1]["query"]
    assert not any(field in transport.calls[-1]["query"] for field in ("cursor", "nextLink", "url"))


def test_gmail_core_read_routes_and_history_continuation_are_provider_native() -> None:
    history_body = json.dumps(
        {
            "history": [{"id": "41", "messagesAdded": [{"message": {"id": "message"}}]}],
            "historyId": "42",
            "nextPageToken": "next-history-page",
        }
    ).encode()
    adapter = GoogleConnectorAdapter()
    transport = _Transport(body=history_body)
    credential = _credential()

    overflow_transport = _Transport()
    with pytest.raises(ValidationError):
        adapter.execute(
            _operation("gmail", "history.list"),
            {"start_history_id": "18446744073709551616"},
            continuation=None,
            credential=credential,
            transport=overflow_transport,
        )
    assert overflow_transport.calls == []

    adapter.execute(
        _operation("gmail", "profile.get"),
        {},
        continuation=None,
        credential=credential,
        transport=transport,
    )
    assert transport.calls[-1]["path"] == "/gmail/v1/users/me/profile"

    adapter.execute(
        _operation("gmail", "labels.get"),
        {"label_id": "Label_7"},
        continuation=None,
        credential=credential,
        transport=transport,
    )
    assert transport.calls[-1]["path"] == "/gmail/v1/users/me/labels/Label_7"

    history_input = {
        "history_types": ["messageAdded", "labelRemoved"],
        "label_id": "INBOX",
        "page_size": 500,
        "start_history_id": "40",
    }
    first = adapter.execute(
        _operation("gmail", "history.list"),
        history_input,
        continuation=None,
        credential=credential,
        transport=transport,
    )
    assert first.payload == {
        "history": [{"id": "41", "messagesAdded": [{"message": {"id": "message"}}]}],
        "historyId": "42",
    }
    assert first.continuation == "next-history-page"
    history_query = transport.calls[-1]["query"]
    assert set(history_query) == {
        ("historyTypes", "labelRemoved"),
        ("historyTypes", "messageAdded"),
        ("labelId", "INBOX"),
        ("maxResults", "500"),
        ("startHistoryId", "40"),
    }

    adapter.execute(
        _operation("gmail", "history.list"),
        history_input,
        continuation=first.continuation,
        credential=credential,
        transport=transport,
    )
    assert ("pageToken", "next-history-page") in transport.calls[-1]["query"]


def test_gmail_get_repeats_metadata_headers_and_draft_list_never_sends_label_ids() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    credential = _credential()
    for name, identifier, path in (
        ("messages.get", "message_id", "/gmail/v1/users/me/messages/message"),
        ("threads.get", "thread_id", "/gmail/v1/users/me/threads/thread"),
    ):
        value = "message" if identifier == "message_id" else "thread"
        adapter.execute(
            _operation("gmail", name),
            {
                "format": "metadata",
                identifier: value,
                "metadata_header_names": ["Subject", "From"],
            },
            continuation=None,
            credential=credential,
            transport=transport,
        )
        call = transport.calls[-1]
        assert call["path"] == path
        assert ("format", "metadata") in call["query"]
        assert [value for key, value in call["query"] if key == "metadataHeaders"] == [
            "Subject",
            "From",
        ]

    adapter.execute(
        _operation("gmail", "drafts.list"),
        {"include_spam_trash": True, "page_size": 25, "query": "is:draft"},
        continuation=None,
        credential=credential,
        transport=transport,
    )
    assert transport.calls[-1]["path"] == "/gmail/v1/users/me/drafts"
    assert {key for key, _value in transport.calls[-1]["query"]} == {
        "includeSpamTrash",
        "maxResults",
        "q",
    }


def test_gmail_modify_effects_escalate_mailbox_removal_and_require_confirmation() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("gmail", "messages.modify")
    assert (
        adapter.classify_effect(operation, {"add_label_ids": ["TRASH"], "message_id": "m"})
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        adapter.classify_effect(operation, {"add_label_ids": ["SPAM"], "message_id": "m"})
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        adapter.classify_effect(operation, {"message_id": "m", "remove_label_ids": ["INBOX"]})
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        adapter.classify_effect(operation, {"message_id": "m", "remove_label_ids": ["TRASH"]})
        is ConnectorEffect.SAFE_MUTATION
    )

    destructive = {"add_label_ids": ["TRASH"], "message_id": "message"}
    blocked_transport = _Transport()
    with pytest.raises(ValidationError):
        adapter.execute(
            operation,
            destructive,
            continuation=None,
            credential=_credential(),
            transport=blocked_transport,
        )
    assert blocked_transport.calls == []

    confirmed_transport = _Transport()
    adapter.execute(
        operation,
        destructive,
        continuation=None,
        credential=_credential(),
        transport=confirmed_transport,
        write_idempotency_key="confirmed-modify",
    )
    assert confirmed_transport.calls[-1]["path"] == "/gmail/v1/users/me/messages/message/modify"
    assert confirmed_transport.calls[-1]["json_body"] == {"addLabelIds": ["TRASH"]}


def test_only_expired_gmail_history_maps_to_full_sync_required() -> None:
    adapter = GoogleConnectorAdapter()
    credential = _credential()
    expired = ConnectorProviderError(origin=ConnectorOrigin.GMAIL, status=404)
    with pytest.raises(ConnectorProviderError) as mapped:
        adapter.execute(
            _operation("gmail", "history.list"),
            {"start_history_id": "1"},
            continuation=None,
            credential=credential,
            transport=_ProviderErrorTransport(expired),
        )
    assert mapped.value.origin is ConnectorOrigin.GMAIL
    assert mapped.value.status == 404
    assert mapped.value.code == "full_sync_required"

    unchanged_cases = (
        (
            _operation("gmail", "labels.get"),
            {"label_id": "missing"},
            ConnectorProviderError(origin=ConnectorOrigin.GMAIL, status=404),
        ),
        (
            _operation("gmail", "history.list"),
            {"start_history_id": "1"},
            ConnectorProviderError(origin=ConnectorOrigin.GMAIL, status=503, code="unavailable"),
        ),
    )
    for operation, values, error in unchanged_cases:
        with pytest.raises(ConnectorProviderError) as unchanged:
            adapter.execute(
                operation,
                values,
                continuation=None,
                credential=credential,
                transport=_ProviderErrorTransport(error),
            )
        assert unchanged.value is error


def test_calendar_location_and_nested_drive_properties_survive_response_sanitization() -> None:
    adapter = GoogleConnectorAdapter()
    calendar = adapter.execute(
        _operation("google_calendar", "events.get"),
        {"calendar_id": "primary", "event_id": "event"},
        continuation=None,
        credential=_credential(),
        transport=_Transport(body=b'{"id":"event","location":"Room 3"}'),
    )
    drive = adapter.execute(
        _operation("google_drive", "files.get"),
        {"file_id": "file"},
        continuation=None,
        credential=_credential(),
        transport=_Transport(
            body=b'{"appProperties":{"location":"HQ","uploadUrl":"business-value"}}'
        ),
    )

    assert calendar.payload == {"id": "event", "location": "Room 3"}
    assert drive.payload == {"appProperties": {"location": "HQ", "uploadUrl": "business-value"}}


def test_gmail_drafts_use_safe_mime_and_draft_then_send() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    draft = _operation("gmail", "drafts.create")
    adapter.execute(
        draft,
        {
            "attachments": [
                {"content_base64": "aGVsbG8=", "filename": "note.txt", "mime_type": "text/plain"}
            ],
            "html_body": "<p>Hello</p>",
            "subject": "Hello",
            "text_body": "Hello",
            "to": ["recipient@example.test"],
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    encoded = transport.calls[-1]["json_body"]["message"]["raw"]
    padded = encoded + "=" * (-len(encoded) % 4)
    mime = base64.urlsafe_b64decode(padded).decode("utf-8")
    assert "To: recipient@example.test" in mime
    assert "Content-Type: text/plain" in mime
    assert transport.calls[-1]["path"] == "/gmail/v1/users/me/drafts"

    send = _operation("gmail", "drafts.send")
    adapter.execute(
        send,
        {"draft_id": "draft"},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert transport.calls[-1]["path"] == "/gmail/v1/users/me/drafts/send"
    assert transport.calls[-1]["json_body"] == {"id": "draft"}
    with pytest.raises(ValidationError):
        adapter.execute(
            draft,
            {"raw": "untrusted"},
            continuation=None,
            credential=_credential(),
            transport=transport,
        )


def test_gmail_reply_preflights_rfc_headers_and_provider_thread() -> None:
    metadata = json.dumps(
        {
            "id": "gmail-resource-42",
            "payload": {
                "headers": [
                    {"name": "Message-ID", "value": "<original@example.test>"},
                    {"name": "References", "value": "<root@example.test>"},
                    {"name": "Subject", "value": "Planning"},
                ]
            },
            "threadId": "provider-thread-7",
        }
    ).encode()
    transport = _Transport(bodies=(metadata, b"{}"))
    GoogleConnectorAdapter().execute(
        _operation("gmail", "drafts.create"),
        {
            "reply_to_message_id": "gmail-resource-42",
            "text_body": "Reply body",
            "thread_id": "provider-thread-7",
            "to": ["recipient@example.test"],
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )

    preflight, create = transport.calls
    assert preflight["method"] is ConnectorMethod.GET
    assert preflight["path"] == "/gmail/v1/users/me/messages/gmail-resource-42"
    assert preflight["query"] == (
        ("format", "metadata"),
        ("metadataHeaders", "Message-ID"),
        ("metadataHeaders", "References"),
        ("metadataHeaders", "Subject"),
    )
    assert create["json_body"]["message"]["threadId"] == "provider-thread-7"
    encoded = create["json_body"]["message"]["raw"]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    mime = BytesParser(policy=policy.default).parsebytes(raw)
    assert mime["In-Reply-To"] == "<original@example.test>"
    assert mime["References"] == "<root@example.test> <original@example.test>"
    assert mime["Subject"] == "Planning"
    assert "gmail-resource-42" not in str(mime["In-Reply-To"])


def test_gmail_reply_rejects_unverified_thread_or_subject() -> None:
    metadata = json.dumps(
        {
            "payload": {
                "headers": [
                    {"name": "Message-ID", "value": "<original@example.test>"},
                    {"name": "Subject", "value": "Planning"},
                ]
            },
            "threadId": "provider-thread-7",
        }
    ).encode()
    adapter = GoogleConnectorAdapter()
    with pytest.raises(ValidationError, match="thread does not match"):
        adapter.execute(
            _operation("gmail", "drafts.create"),
            {
                "reply_to_message_id": "gmail-resource-42",
                "thread_id": "caller-guessed-thread",
            },
            continuation=None,
            credential=_credential(),
            transport=_Transport(body=metadata),
        )
    with pytest.raises(ValidationError, match="Subject must match"):
        adapter.execute(
            _operation("gmail", "drafts.create"),
            {"reply_to_message_id": "gmail-resource-42", "subject": "Different"},
            continuation=None,
            credential=_credential(),
            transport=_Transport(body=metadata),
        )
    with pytest.raises(ValidationError, match="derived from reply_to_message_id"):
        adapter.execute(
            _operation("gmail", "drafts.create"),
            {"thread_id": "caller-guessed-thread"},
            continuation=None,
            credential=_credential(),
            transport=_Transport(),
        )


def test_gmail_attachment_defaults_to_a_private_streaming_artifact(tmp_path: Path) -> None:
    content = b"attachment bytes from Gmail"
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")
    body = json.dumps({"data": encoded, "size": len(content)}).encode("utf-8")
    store = ArtifactStore(tmp_path / "artifacts")
    scope = ConnectorArtifactScope(store)
    transfer = ConnectorTransferContext(_artifact_scope_factory=lambda: scope)
    result = GoogleConnectorAdapter().execute(
        _operation("gmail", "attachments.get"),
        {"attachment_id": "attachment", "message_id": "message"},
        continuation=None,
        credential=_credential(),
        transport=_Transport(download_body=body),
        transfer=transfer,
    )
    try:
        assert result.payload == {"bytes": len(content), "delivery": "artifact"}
        assert result.artifact is not None
        assert result.artifact.path.read_bytes() == content
        assert "path" not in result.payload
    finally:
        if result.artifact is not None:
            scope.complete(result.artifact)
        store.close()


def test_gmail_attachment_inline_compatibility_decodes_base64url_without_artifact(
    tmp_path: Path,
) -> None:
    del tmp_path
    content = b"\xfb\xff\x00"
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")
    transport = _Transport(
        download_body=json.dumps({"size": len(content), "data": encoded}).encode("utf-8")
    )
    result = GoogleConnectorAdapter().execute(
        _operation("gmail", "attachments.get"),
        {"attachment_id": "attachment", "delivery": "inline_chunk", "message_id": "message"},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert result.payload == {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "delivery": "inline_chunk",
    }
    assert result.artifact is None


@pytest.mark.parametrize(
    ("name", "method", "draft_path", "extra"),
    (
        ("drafts.create", ConnectorMethod.POST, "/upload/gmail/v1/users/me/drafts", {}),
        (
            "drafts.update",
            ConnectorMethod.PUT,
            "/upload/gmail/v1/users/me/drafts/draft",
            {"draft_id": "draft"},
        ),
    ),
)
def test_gmail_local_file_attachment_uses_resumable_rfc822_upload(
    tmp_path: Path,
    name: str,
    method: ConnectorMethod,
    draft_path: str,
    extra: dict[str, object],
) -> None:
    upload = _prepared_upload(tmp_path)
    transfer = ConnectorTransferContext(uploads={("attachments", 0, "local_file"): upload})
    transport = _Transport()
    try:
        result = GoogleConnectorAdapter().execute(
            _operation("gmail", name),
            {
                **extra,
                "attachments": [{"filename": "résumé.txt", "mime_type": "text/plain"}],
                "html_body": "<p>HTML body</p>",
                "text_body": "Plain body",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            transfer=transfer,
        )
        assert result.payload == {}
        initiated, sent = transport.calls
        assert initiated["method"] is method
        assert initiated["path"] == draft_path
        assert initiated["query"] == (("uploadType", "resumable"),)
        assert initiated["headers"] == {
            "X-Upload-Content-Length": str(sent["content_length"]),
            "X-Upload-Content-Type": "message/rfc822",
        }
        assert initiated["json_body"] == {"message": {}}
        assert sent["credential"] is None
        assert sent["content_type"] == "message/rfc822"
        serialized = b"".join(sent["source"])
        assert len(serialized) == sent["content_length"]
        message = BytesParser(policy=policy.default).parsebytes(serialized)
        plain = next(part for part in message.walk() if part.get_content_type() == "text/plain")
        assert plain.get_content().strip() == "Plain body"
        attachment = next(message.iter_attachments())
        assert attachment.get_filename() == "résumé.txt"
        assert attachment.get_payload(decode=True) == b"streamed Google Drive content"
        assert "relative_path" not in repr(sent["source"])
    finally:
        upload.close()


def test_gmail_local_file_reply_preserves_verified_headers_and_thread_metadata(
    tmp_path: Path,
) -> None:
    metadata = json.dumps(
        {
            "payload": {
                "headers": [
                    {"name": "Message-ID", "value": "<original@example.test>"},
                    {"name": "References", "value": "<root@example.test>"},
                    {"name": "Subject", "value": "Planning"},
                ]
            },
            "threadId": "provider-thread-7",
        }
    ).encode()
    upload = _prepared_upload(tmp_path)
    transfer = ConnectorTransferContext(uploads={("attachments", 0, "local_file"): upload})
    transport = _Transport(bodies=(metadata, b"{}"))
    try:
        GoogleConnectorAdapter().execute(
            _operation("gmail", "drafts.create"),
            {
                "attachments": [{"filename": "reply.txt", "mime_type": "text/plain"}],
                "reply_to_message_id": "gmail-resource-42",
                "text_body": "Reply body",
                "thread_id": "provider-thread-7",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            transfer=transfer,
        )
        preflight, initiated, sent = transport.calls
        assert preflight["path"] == "/gmail/v1/users/me/messages/gmail-resource-42"
        assert initiated["json_body"] == {"message": {"threadId": "provider-thread-7"}}
        message = BytesParser(policy=policy.default).parsebytes(b"".join(sent["source"]))
        assert message["In-Reply-To"] == "<original@example.test>"
        assert message["References"] == "<root@example.test> <original@example.test>"
        assert message["Subject"] == "Planning"
    finally:
        upload.close()


def test_calendar_effect_escalates_shared_or_notified_event_changes() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.create")
    safe = {"calendar_id": "primary", "end": _event_time(), "start": _event_time()}
    assert adapter.classify_effect(operation, safe) is ConnectorEffect.SAFE_MUTATION
    assert (
        adapter.classify_effect(operation, {**safe, "attendee_emails": ["guest@example.test"]})
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(
            operation,
            {**safe, "attendees": [{"email": "guest@example.test"}]},
        )
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(operation, {**safe, "send_updates": "none"})
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(operation, {**safe, "calendar_id": "team"})
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(operation, {**safe, "drive_attachments": [{"file_id": "file"}]})
        is ConnectorEffect.OUTWARD
    )


def test_existing_calendar_event_effect_uses_live_sharing_state_and_rechecks_execution() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    mutation = {
        "calendar_id": "primary",
        "etag": "event-version-1",
        "event_id": "event-1",
        "summary": "Updated",
    }
    private = json.dumps(
        {
            "attendees": [{"email": "owner@example.test", "self": True}],
            "organizer": {"email": "owner@example.test", "self": True},
        }
    ).encode()
    shared = json.dumps(
        {
            "attendees": [
                {"email": "owner@example.test", "self": True},
                {"email": "guest@example.test"},
            ],
            "organizer": {"email": "owner@example.test", "self": True},
        }
    ).encode()

    private_transport = _Transport(body=private)
    assert (
        adapter.classify_effect(
            operation,
            mutation,
            credential=_credential(),
            transport=private_transport,
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert private_transport.calls[0]["query"] == (("fields", "attendees,organizer"),)
    assert adapter.classify_effect(operation, mutation) is ConnectorEffect.OUTWARD

    shared_transport = _Transport(body=shared)
    assert (
        adapter.classify_effect(
            operation,
            mutation,
            credential=_credential(),
            transport=shared_transport,
        )
        is ConnectorEffect.OUTWARD
    )
    with pytest.raises(ValidationError, match="fresh outward confirmation"):
        adapter.execute(
            operation,
            mutation,
            continuation=None,
            credential=_credential(),
            transport=_Transport(body=shared),
        )

    changed_transport = _Transport(bodies=(private, shared))
    assert (
        adapter.classify_effect(
            operation,
            mutation,
            credential=_credential(),
            transport=changed_transport,
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    with pytest.raises(ValidationError, match="fresh outward confirmation"):
        adapter.execute(
            operation,
            mutation,
            continuation=None,
            credential=_credential(),
            transport=changed_transport,
        )
    assert all(call["method"] is ConnectorMethod.GET for call in changed_transport.calls)


def test_confirmed_shared_calendar_event_update_rechecks_then_writes() -> None:
    shared = json.dumps(
        {
            "attendees": [{"email": "guest@example.test"}],
            "organizer": {"email": "owner@example.test", "self": True},
        }
    ).encode()
    transport = _Transport(bodies=(shared, b"{}"))
    GoogleConnectorAdapter().execute(
        _operation("google_calendar", "events.update"),
        {
            "calendar_id": "primary",
            "etag": "event-version-1",
            "event_id": "event-1",
            "summary": "Confirmed update",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-change",
    )

    preflight, update = transport.calls
    assert preflight["method"] is ConnectorMethod.GET
    assert update["method"] is ConnectorMethod.PATCH
    assert update["headers"] == {"If-Match": "event-version-1"}


def test_calendar_rsvp_preflights_self_attendee_and_etag() -> None:
    event = json.dumps(
        {
            "attendees": [
                {"email": "guest@example.test", "responseStatus": "accepted"},
                {
                    "comment": "Earlier note",
                    "displayName": "Owner",
                    "email": "owner@example.test",
                    "optional": True,
                    "responseStatus": "needsAction",
                    "self": True,
                },
            ],
            "etag": '"event-version-4"',
        }
    ).encode()
    transport = _Transport(bodies=(event, b"{}"))
    GoogleConnectorAdapter().execute(
        _operation("google_calendar", "events.respond"),
        {
            "calendar_id": "primary",
            "comment": "See you there",
            "event_id": "event-1",
            "response_status": "tentative",
            "send_updates": "all",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )

    preflight, response = transport.calls
    assert preflight["method"] is ConnectorMethod.GET
    assert preflight["path"] == "/calendar/v3/calendars/primary/events/event-1"
    assert preflight["query"] == (("maxAttendees", "1"), ("fields", "attendees,etag"))
    assert response["method"] is ConnectorMethod.PATCH
    assert response["headers"] == {"If-Match": '"event-version-4"'}
    assert response["query"] == (("sendUpdates", "all"),)
    assert response["json_body"] == {
        "attendees": [
            {
                "comment": "See you there",
                "email": "owner@example.test",
                "responseStatus": "tentative",
            }
        ],
        "attendeesOmitted": True,
    }


def test_calendar_rsvp_fails_closed_without_one_self_attendee() -> None:
    event = json.dumps(
        {"attendees": [{"email": "guest@example.test"}], "etag": '"event-version-4"'}
    ).encode()
    transport = _Transport(body=event)
    with pytest.raises(ValidationError, match="one self attendee"):
        GoogleConnectorAdapter().execute(
            _operation("google_calendar", "events.respond"),
            {"calendar_id": "primary", "event_id": "event-1", "response_status": "accepted"},
            continuation=None,
            credential=_credential(),
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_calendar_and_drive_shape_fixed_bodies_headers_and_resumable_uploads() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    calendar = _operation("google_calendar", "events.update")
    adapter.execute(
        calendar,
        {
            "attendees": [
                {
                    "display_name": "Guest",
                    "email": "guest@example.test",
                    "optional": True,
                    "response_status": "accepted",
                }
            ],
            "calendar_id": "primary",
            "etag": "calendar-etag",
            "event_id": "event",
            "guests_can_invite_others": False,
            "guests_can_modify": True,
            "guests_can_see_other_guests": False,
            "reminders": {
                "overrides": [{"delivery": "popup", "minutes": 15}],
                "use_default": False,
            },
            "summary": "Updated",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    calendar_call = transport.calls[-1]
    assert calendar_call["headers"] == {"If-Match": "calendar-etag"}
    assert calendar_call["json_body"] == {
        "attendees": [
            {
                "displayName": "Guest",
                "email": "guest@example.test",
                "optional": True,
                "responseStatus": "accepted",
            }
        ],
        "guestsCanInviteOthers": False,
        "guestsCanModify": True,
        "guestsCanSeeOtherGuests": False,
        "reminders": {
            "overrides": [{"method": "popup", "minutes": 15}],
            "useDefault": False,
        },
        "summary": "Updated",
    }

    upload = _operation("google_drive", "files.create")
    adapter.execute(
        upload,
        {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-upload",
    )
    initiated, sent = transport.calls[-2:]
    assert initiated["path"] == "/upload/drive/v3/files"
    assert initiated["query"] == (("uploadType", "resumable"),)
    assert initiated["headers"] == {
        "X-Upload-Content-Length": "5",
        "X-Upload-Content-Type": "text/plain",
    }
    assert sent["kind"] == "stream"
    assert sent["location"] == "https://www.googleapis.com/upload/session/one"
    assert b"".join(sent["source"]) == b"hello"
    assert sent["content_length"] == 5
    assert sent["total_length"] == 5
    assert sent["credential"] is None


def test_drive_binary_upload_requires_adapter_confirmation_before_transport() -> None:
    transport = _Transport()

    with pytest.raises(ValidationError, match="fresh outward confirmation"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {
                "content_base64": "aGVsbG8=",
                "mime_type": "text/plain",
                "name": "note.txt",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
        )

    assert transport.calls == []


def test_drive_local_file_upload_uses_the_prepared_snapshot_and_fixed_routes(
    tmp_path: Path,
) -> None:
    adapter = GoogleConnectorAdapter()
    credential = _credential()
    cases = (
        (
            _operation("google_drive", "files.create"),
            {"mime_type": "text/plain", "name": "note.txt"},
            ConnectorMethod.POST,
            "/upload/drive/v3/files",
        ),
        (
            _operation("google_drive", "files.update"),
            {"file_id": "file", "mime_type": "text/plain"},
            ConnectorMethod.PATCH,
            "/upload/drive/v3/files/file",
        ),
    )
    for operation, values, method, path in cases:
        upload = _prepared_upload(tmp_path)
        transfer = ConnectorTransferContext(uploads={("local_file",): upload})
        transport = _Transport()
        try:
            result = adapter.execute(
                operation,
                {**values, "local_file": {"grant_id": "grant", "relative_path": "note.txt"}},
                continuation=None,
                credential=credential,
                transport=transport,
                write_idempotency_key="confirmed-upload",
                transfer=transfer,
            )
        finally:
            upload.close()

        initiated, sent = transport.calls
        assert result.payload == {}
        assert initiated["method"] is method
        assert initiated["path"] == path
        assert initiated["query"] == (("uploadType", "resumable"),)
        assert initiated["headers"] == {
            "X-Upload-Content-Length": str(upload.size),
            "X-Upload-Content-Type": "application/octet-stream",
        }
        assert sent["kind"] == "stream"
        assert sent["source"] is upload
        assert sent["location"] == "https://www.googleapis.com/upload/session/one"
        assert sent["credential"] is None
        assert sent["content_length"] == upload.size
        assert sent["content_type"] == "application/octet-stream"
        assert sent["byte_offset"] == 0
        assert sent["total_length"] == upload.size
        assert "body" not in sent
        assert "headers" not in sent
        assert "relative_path" not in repr(sent)


def test_drive_resumable_upload_resends_only_the_unacknowledged_prepared_suffix(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(tmp_path)
    split = 8
    transport = _DriveRecoveryTransport(
        stream_outcomes=(
            _drive_stream_response(308, next_offset=split),
            _drive_stream_response(200, body=b'{"id":"file-1"}'),
        )
    )
    try:
        result = GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {
                "local_file": {"grant_id": "grant", "relative_path": "note.txt"},
                "mime_type": "application/octet-stream",
                "name": "note.txt",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-upload",
            transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
        )
    finally:
        upload.close()

    initiated, first, second = transport.calls
    assert result.payload == {"id": "file-1"}
    assert initiated["headers"] == {
        "X-Upload-Content-Length": str(upload.size),
        "X-Upload-Content-Type": "application/octet-stream",
    }
    assert first["source"] is upload
    assert second["source"] is upload
    assert first["sent_body"] == b"streamed Google Drive content"
    assert second["sent_body"] == b"streamed Google Drive content"[split:]
    assert first["byte_offset"] == 0
    assert second["byte_offset"] == split
    assert first["content_length"] == upload.size
    assert second["content_length"] == upload.size - split
    assert first["total_length"] == upload.size
    assert second["total_length"] == upload.size
    assert first["credential"] is None
    assert second["credential"] is None


def test_drive_resumable_upload_resolves_ambiguous_completion_without_resending() -> None:
    transport = _DriveRecoveryTransport(
        stream_outcomes=(ConnectorOutcomeUnknown("ambiguous final dispatch"),),
        probe_outcomes=(_drive_stream_response(200, body=b'{"id":"file-1"}'),),
    )

    result = GoogleConnectorAdapter().execute(
        _operation("google_drive", "files.create"),
        {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-upload",
    )

    assert result.payload == {"id": "file-1"}
    assert [call["kind"] for call in transport.calls] == ["request", "stream", "probe"]
    assert transport.calls[-1]["total_length"] == 5


def test_drive_resumable_upload_probes_after_server_failure() -> None:
    transport = _DriveRecoveryTransport(
        stream_outcomes=(ConnectorProviderError(origin=ConnectorOrigin.GOOGLE, status=500),),
        probe_outcomes=(_drive_stream_response(200, body=b'{"id":"file-1"}'),),
    )

    result = GoogleConnectorAdapter().execute(
        _operation("google_drive", "files.create"),
        {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-upload",
    )

    assert result.payload == {"id": "file-1"}
    assert [call["kind"] for call in transport.calls] == ["request", "stream", "probe"]


def test_drive_resumable_upload_keeps_malformed_probe_failures_outcome_unknown() -> None:
    transport = _DriveRecoveryTransport(
        stream_outcomes=(ConnectorOutcomeUnknown("ambiguous final dispatch"),),
        probe_outcomes=(ValidationError("provider response contains a duplicate safety header"),),
    )

    with pytest.raises(ConnectorOutcomeUnknown, match="status could not be resolved"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-upload",
        )

    assert [call["kind"] for call in transport.calls] == ["request", "stream", "probe"]
    assert transport.sessions == 1


def test_drive_resumable_upload_does_not_probe_after_definite_client_error() -> None:
    error = ConnectorProviderError(origin=ConnectorOrigin.GOOGLE, status=400)
    transport = _DriveRecoveryTransport(stream_outcomes=(error,))

    with pytest.raises(ConnectorProviderError) as raised:
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-upload",
        )

    assert raised.value is error
    assert [call["kind"] for call in transport.calls] == ["request", "stream"]


def test_drive_resumable_upload_never_restarts_after_ambiguous_session_loss() -> None:
    transport = _DriveRecoveryTransport(
        stream_outcomes=(ConnectorOutcomeUnknown("ambiguous final dispatch"),),
        probe_outcomes=(_drive_stream_response(404),),
    )

    with pytest.raises(ConnectorOutcomeUnknown, match="was not restarted"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {
                "content_base64": "aGVsbG8=",
                "mime_type": "text/plain",
                "name": "note.txt",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-upload",
        )

    assert [call["kind"] for call in transport.calls] == ["request", "stream", "probe"]
    assert transport.sessions == 1


def test_drive_resumable_upload_preserves_ambiguity_after_full_acknowledgement() -> None:
    transport = _DriveRecoveryTransport(
        stream_outcomes=(_drive_stream_response(308, next_offset=5),),
        probe_outcomes=(_drive_stream_response(404),),
    )

    with pytest.raises(ConnectorOutcomeUnknown, match="was not restarted"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {
                "content_base64": "aGVsbG8=",
                "mime_type": "text/plain",
                "name": "note.txt",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-upload",
        )

    assert [call["kind"] for call in transport.calls] == ["request", "stream", "probe"]
    assert transport.sessions == 1


@pytest.mark.parametrize(
    ("outcome", "match"),
    (
        (
            ConnectorProviderError(origin=ConnectorOrigin.GOOGLE, status=202),
            "unsupported success status",
        ),
        (_drive_stream_response(200, body=b"not-json"), "result could not be decoded"),
        (_drive_stream_response(200), "result could not be decoded"),
    ),
)
def test_drive_resumable_upload_never_replays_uncertain_success_response(
    outcome: ConnectorStreamResponse | Exception,
    match: str,
) -> None:
    transport = _DriveRecoveryTransport(stream_outcomes=(outcome,))

    with pytest.raises(ConnectorOutcomeUnknown, match=match):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-upload",
        )

    assert [call["kind"] for call in transport.calls] == ["request", "stream"]
    assert transport.sessions == 1


def test_drive_resumable_upload_restarts_one_cleanly_expired_session_only(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(tmp_path)
    expired = ConnectorProviderError(origin=ConnectorOrigin.GOOGLE, status=404)
    transport = _DriveRecoveryTransport(stream_outcomes=(expired, expired))
    try:
        with pytest.raises(ConnectorOutcomeUnknown, match="expired more than once"):
            GoogleConnectorAdapter().execute(
                _operation("google_drive", "files.update"),
                {
                    "file_id": "file-1",
                    "local_file": {"grant_id": "grant", "relative_path": "note.txt"},
                    "mime_type": "application/octet-stream",
                },
                continuation=None,
                credential=_credential(),
                transport=transport,
                write_idempotency_key="confirmed-upload",
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
            )
    finally:
        upload.close()

    assert [call["kind"] for call in transport.calls] == [
        "request",
        "stream",
        "request",
        "stream",
    ]
    assert transport.calls[1]["source"] is upload
    assert transport.calls[3]["source"] is upload
    assert transport.calls[1]["location"].endswith("/1")
    assert transport.calls[3]["location"].endswith("/2")
    assert transport.sessions == 2


def test_drive_resumable_upload_bounds_repeated_no_progress(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(tmp_path)
    no_progress = _DriveRecoveryTransport(
        stream_outcomes=(
            _drive_stream_response(308, next_offset=0),
            _drive_stream_response(308, next_offset=0),
        )
    )
    try:
        with pytest.raises(ConnectorOutcomeUnknown, match="made no progress"):
            GoogleConnectorAdapter().execute(
                _operation("google_drive", "files.create"),
                {
                    "local_file": {"grant_id": "grant", "relative_path": "note.txt"},
                    "mime_type": "application/octet-stream",
                    "name": "note.txt",
                },
                continuation=None,
                credential=_credential(),
                transport=no_progress,
                write_idempotency_key="confirmed-upload",
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
            )
    finally:
        upload.close()
    assert [call["kind"] for call in no_progress.calls] == ["request", "stream", "stream"]


def test_drive_inline_upload_replays_only_when_no_bytes_were_acknowledged() -> None:
    transport = _DriveRecoveryTransport(
        stream_outcomes=(
            _drive_stream_response(308, next_offset=0),
            _drive_stream_response(200, body=b'{"id":"file-1"}'),
        )
    )

    result = GoogleConnectorAdapter().execute(
        _operation("google_drive", "files.create"),
        {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-upload",
    )

    assert result.payload == {"id": "file-1"}
    assert [call["kind"] for call in transport.calls] == ["request", "stream", "stream"]
    assert transport.calls[1]["sent_body"] == b"hello"
    assert transport.calls[2]["sent_body"] == b"hello"


def test_drive_zero_byte_prepared_upload_can_retry_one_incomplete_dispatch(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(tmp_path, b"")
    transport = _DriveRecoveryTransport(
        stream_outcomes=(
            _drive_stream_response(308, next_offset=0),
            _drive_stream_response(200, body=b'{"id":"file-1"}'),
        )
    )
    try:
        result = GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {
                "local_file": {"grant_id": "grant", "relative_path": "empty.bin"},
                "mime_type": "application/octet-stream",
                "name": "empty.bin",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-upload",
            transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
        )
    finally:
        upload.close()

    assert result.payload == {"id": "file-1"}
    assert [call["kind"] for call in transport.calls] == ["request", "stream", "stream"]
    assert transport.calls[1]["content_length"] == 0
    assert transport.calls[1]["total_length"] is None
    assert transport.calls[2]["content_length"] == 0
    assert transport.calls[2]["total_length"] is None


def test_drive_inline_upload_requires_fresh_confirmation_after_partial_recovery() -> None:
    inline = _DriveRecoveryTransport(
        stream_outcomes=(ConnectorOutcomeUnknown("ambiguous final dispatch"),),
        probe_outcomes=(_drive_stream_response(308, next_offset=2),),
    )
    with pytest.raises(ConnectorOutcomeUnknown, match="fresh confirmation"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.create"),
            {"content_base64": "aGVsbG8=", "mime_type": "text/plain", "name": "note.txt"},
            continuation=None,
            credential=_credential(),
            transport=inline,
            write_idempotency_key="confirmed-upload",
        )
    assert [call["kind"] for call in inline.calls] == ["request", "stream", "probe"]


def test_drive_artifact_download_uses_streaming_receipt_without_path_payload(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    scope = ConnectorArtifactScope(store)
    transfer = ConnectorTransferContext(_artifact_scope_factory=lambda: scope)
    transport = _Transport(download_body=b"provider-bytes")
    result = GoogleConnectorAdapter().execute(
        _operation("google_drive", "files.content"),
        {
            "delivery": "artifact",
            "file_id": "file",
            "filename": "report.pdf",
            "mime_type": "application/pdf",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
        transfer=transfer,
    )
    try:
        assert result.payload == {"bytes": len(b"provider-bytes"), "delivery": "artifact"}
        assert result.artifact is not None
        assert result.artifact.filename == "report.pdf"
        assert result.artifact.media_type == "application/pdf"
        assert "path" not in result.payload
        call = transport.calls[-1]
        assert call["kind"] == "download_stream"
        assert call["path"] == "/drive/v3/files/file"
        assert call["query"] == (("alt", "media"),)
        assert call["max_bytes"] == 5 * 1024**4
    finally:
        if result.artifact is not None:
            scope.complete(result.artifact)
        store.close()


def test_drive_lro_download_pends_once_then_polls_and_streams_artifact(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "lro-artifacts")
    scope = ConnectorArtifactScope(store)
    transfer = ConnectorTransferContext(_artifact_scope_factory=lambda: scope)
    transport = _Transport(
        bodies=(
            _drive_download_operation_body(done=None, resource_key="resource_key-1"),
            _drive_download_operation_body(
                done=True,
                resource_key="resource_key-1",
                response=_drive_download_response(partial=False),
            ),
        ),
        download_body=b"video-bytes",
    )
    values = {
        "delivery": "artifact",
        "file_id": "file_1",
        "filename": "clip.mp4",
        "mime_type": "video/mp4",
        "resource_key": "resource_key-1",
        "revision_id": "revision-1",
    }
    adapter = GoogleConnectorAdapter()
    pending = adapter.execute(
        _operation("google_drive", "files.download"),
        values,
        continuation=None,
        credential=_credential(),
        transport=transport,
        transfer=transfer,
    )
    assert pending.payload == {"retry_after_seconds": 10, "status": "pending"}
    assert pending.artifact is None
    assert pending.continuation == {
        "operation_name": "download-1",
        "resource_key": "resource_key-1",
    }
    assert len(transport.calls) == 1
    start = transport.calls[0]
    assert start["method"] is ConnectorMethod.POST
    assert start["path"] == "/drive/v3/files/file_1/download"
    assert start["query"] == (("mimeType", "video/mp4"), ("revisionId", "revision-1"))
    assert start["body"] == b""
    assert start["headers"] == {"X-Goog-Drive-Resource-Keys": "file_1/resource_key-1"}

    completed = adapter.execute(
        _operation("google_drive", "files.download"),
        values,
        continuation=pending.continuation,
        credential=_credential(),
        transport=transport,
        transfer=transfer,
    )
    try:
        assert completed.payload == {"bytes": len(b"video-bytes"), "delivery": "artifact"}
        assert completed.continuation is None
        assert completed.artifact is not None
        assert completed.artifact.filename == "clip.mp4"
        assert completed.artifact.media_type == "video/mp4"
        assert [call["kind"] for call in transport.calls] == [
            "request",
            "request",
            "download_stream",
        ]
        poll = transport.calls[1]
        assert poll["method"] is ConnectorMethod.GET
        assert poll["path"] == "/drive/v3/operations/download-1"
        assert poll["headers"] == {"X-Goog-Drive-Resource-Keys": "file_1/resource_key-1"}
        download = transport.calls[2]
        assert download["location"] == ("https://drive.usercontent.google.com/download?ticket=one")
        assert download["credential"] is None
        assert download["google_drive_resource_key"] == ("file_1", "resource_key-1")
        assert download["max_bytes"] == 5 * 1024**4
        assert "downloadUri" not in repr(completed.payload)
        assert "resource_key-1" not in repr(completed.payload)
    finally:
        if completed.artifact is not None:
            scope.complete(completed.artifact)
        store.close()


def test_drive_lro_initial_completion_is_never_polled_and_carries_provider_resource_key(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "initial-artifacts")
    scope = ConnectorArtifactScope(store)
    transfer = ConnectorTransferContext(_artifact_scope_factory=lambda: scope)
    transport = _Transport(
        body=_drive_download_operation_body(
            done=True,
            resource_key="provider-key",
            response=_drive_download_response(partial=None),
        ),
        download_body=b"ready",
    )
    result = GoogleConnectorAdapter().execute(
        _operation("google_drive", "files.download"),
        {"delivery": "artifact", "file_id": "file_1", "mime_type": "video/mp4"},
        continuation=None,
        credential=_credential(),
        transport=transport,
        transfer=transfer,
    )
    try:
        assert result.continuation is None
        assert [call["kind"] for call in transport.calls] == ["request", "download_stream"]
        assert transport.calls[0]["headers"] is None
        assert transport.calls[1]["google_drive_resource_key"] == (
            "file_1",
            "provider-key",
        )
    finally:
        if result.artifact is not None:
            scope.complete(result.artifact)
        store.close()


def test_drive_lro_inline_delivery_requires_provider_partial_support() -> None:
    values = {
        "byte_offset": 5,
        "delivery": "inline_chunk",
        "file_id": "file_1",
        "max_chunk_size": 3,
    }
    allowed = _Transport(
        body=_drive_download_operation_body(
            done=True,
            response=_drive_download_response(partial=True),
        ),
        download_body=b"cde",
        headers={"content-range": "bytes 5-7/8"},
    )
    result = GoogleConnectorAdapter().execute(
        _operation("google_drive", "files.download"),
        values,
        continuation=None,
        credential=_credential(),
        transport=allowed,
    )
    assert result.payload == {
        "byte_offset": 5,
        "content_base64": "Y2Rl",
        "content_range": "bytes 5-7/8",
        "next_byte_offset": 8,
    }
    assert allowed.calls[-1]["range_start"] == 5
    assert allowed.calls[-1]["range_end"] == 7
    assert allowed.calls[-1]["max_bytes"] == 3

    forbidden = _Transport(
        body=_drive_download_operation_body(
            done=True,
            response=_drive_download_response(partial=None),
        )
    )
    with pytest.raises(ValidationError, match="use artifact delivery"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.download"),
            values,
            continuation=None,
            credential=_credential(),
            transport=forbidden,
        )
    assert [call["kind"] for call in forbidden.calls] == ["request"]


def test_drive_lro_failures_are_bounded_and_restartable() -> None:
    provider_error = _Transport(
        body=_drive_download_operation_body(done=True, error={"code": 16, "message": "secret"})
    )
    with pytest.raises(ConnectorProviderError) as caught:
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.download"),
            {"delivery": "inline_chunk", "file_id": "file"},
            continuation=None,
            credential=_credential(),
            transport=provider_error,
        )
    assert caught.value.status == 401
    assert caught.value.code == "download_unauthenticated"
    assert "secret" not in str(caught.value)

    class _StartUnknown(_Transport):
        def request(self, **kwargs: Any) -> ConnectorResponse:
            del kwargs
            raise ConnectorOutcomeUnknown("ambiguous POST")

    with pytest.raises(ContinuityError, match="retrying is safe"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.download"),
            {"delivery": "inline_chunk", "file_id": "file"},
            continuation=None,
            credential=_credential(),
            transport=_StartUnknown(),
        )

    class _CompletedPoll(_Transport):
        def request(self, **kwargs: Any) -> ConnectorResponse:
            del kwargs
            raise ConnectorProviderError(
                origin=ConnectorOrigin.GOOGLE,
                status=403,
                code="permission_denied",
            )

    with pytest.raises(ContinuityError, match=r"start files\.download again"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.download"),
            {"delivery": "inline_chunk", "file_id": "file"},
            continuation={"operation_name": "download-1"},
            credential=_credential(),
            transport=_CompletedPoll(),
        )


@pytest.mark.parametrize(
    "body",
    (
        b"[]",
        _drive_download_operation_body(done=False, response={}),
        _drive_download_operation_body(done=True),
        _drive_download_operation_body(done=True, response={}, error={"code": 13}),
        _drive_download_operation_body(
            done=True,
            response={"downloadUri": "https://drive.usercontent.google.com/file"},
        ),
        _drive_download_operation_body(
            done=True,
            response={
                **_drive_download_response(partial=None),
                "partialDownloadAllowed": "true",
            },
        ),
    ),
)
def test_drive_lro_rejects_malformed_operation_states(body: bytes) -> None:
    with pytest.raises(ContinuityError, match="Google Drive"):
        GoogleConnectorAdapter().execute(
            _operation("google_drive", "files.download"),
            {"delivery": "inline_chunk", "file_id": "file"},
            continuation=None,
            credential=_credential(),
            transport=_Transport(body=body),
        )


def test_drive_transfer_delivery_requires_a_transfer_context() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    with pytest.raises(ValidationError, match="transfer context"):
        adapter.execute(
            _operation("google_drive", "files.download"),
            {"file_id": "file"},
            continuation=None,
            credential=_credential(),
            transport=transport,
        )
    assert transport.calls == []
    with pytest.raises(ValidationError, match="transfer context"):
        adapter.execute(
            _operation("google_drive", "files.create"),
            {
                "local_file": {"grant_id": "grant", "relative_path": "note.txt"},
                "mime_type": "text/plain",
                "name": "note.txt",
            },
            continuation=None,
            credential=_credential(),
            transport=_Transport(),
            write_idempotency_key="confirmed-upload",
        )


def test_calendar_resolves_typed_drive_attachments_and_enables_support() -> None:
    first_file = json.dumps(
        {
            "id": "file-1",
            "mimeType": "application/vnd.google-apps.document",
            "name": "Plan",
            "webViewLink": "https://docs.google.com/document/d/file-1/edit",
        }
    ).encode()
    second_file = json.dumps(
        {
            "id": "file-2",
            "mimeType": "application/pdf",
            "name": "Brief.pdf",
            "webViewLink": "https://drive.google.com/file/d/file-2/view",
        }
    ).encode()
    transport = _Transport(bodies=(first_file, second_file, b"{}"))
    GoogleConnectorAdapter().execute(
        _operation("google_calendar", "events.create"),
        {
            "calendar_id": "primary",
            "drive_attachments": [{"file_id": "file-1"}, {"file_id": "file-2"}],
            "end": _event_time(),
            "start": _event_time(),
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )

    first_preflight, second_preflight, create = transport.calls
    assert first_preflight["path"] == "/drive/v3/files/file-1"
    assert second_preflight["path"] == "/drive/v3/files/file-2"
    assert first_preflight["query"] == (
        ("fields", "id,mimeType,name,webViewLink"),
        ("supportsAllDrives", "true"),
    )
    assert create["path"] == "/calendar/v3/calendars/primary/events"
    assert create["query"] == (("supportsAttachments", "true"),)
    assert create["json_body"]["attachments"] == [
        {
            "fileUrl": "https://docs.google.com/document/d/file-1/edit",
            "mimeType": "application/vnd.google-apps.document",
            "title": "Plan",
        },
        {
            "fileUrl": "https://drive.google.com/file/d/file-2/view",
            "mimeType": "application/pdf",
            "title": "Brief.pdf",
        },
    ]


def test_calendar_drive_attachments_reject_untrusted_provider_link() -> None:
    file = json.dumps(
        {
            "id": "file-1",
            "mimeType": "application/pdf",
            "name": "Brief.pdf",
            "webViewLink": "https://attacker.example/file-1",
        }
    ).encode()
    transport = _Transport(body=file)
    with pytest.raises(ValidationError, match="invalid web link"):
        GoogleConnectorAdapter().execute(
            _operation("google_calendar", "events.update"),
            {
                "calendar_id": "primary",
                "drive_attachments": [{"file_id": "file-1"}],
                "etag": "event-etag",
                "event_id": "event-1",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
        )
    assert [call["path"] for call in transport.calls] == [
        "/calendar/v3/calendars/primary/events/event-1",
        "/drive/v3/files/file-1",
    ]


def test_drive_metadata_scope_and_preconditions_remain_fixed() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    operation = _operation("google_drive", "files.update")
    adapter.execute(
        operation,
        {"file_id": "file", "name": "renamed.txt"},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    call = transport.calls[-1]
    assert call["method"] is ConnectorMethod.PATCH
    assert call["path"] == "/drive/v3/files/file"
    assert call["headers"] is None
    assert call["json_body"] == {"name": "renamed.txt"}


def test_drive_rejects_empty_updates_and_serializes_app_property_deletion() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_drive", "files.update")
    empty_transport = _Transport()
    with pytest.raises(ValidationError, match="metadata or content change"):
        adapter.execute(
            operation,
            {"file_id": "file"},
            continuation=None,
            credential=_credential(),
            transport=empty_transport,
        )
    assert empty_transport.calls == []

    transport = _Transport()
    adapter.execute(
        operation,
        {
            "app_properties": [{"key": "obsolete", "value": None}],
            "file_id": "file",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert transport.calls[-1]["json_body"] == {"appProperties": {"obsolete": None}}


def test_drive_move_renders_one_distinct_parent_transition() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    operation = _operation("google_drive", "files.move")
    adapter.execute(
        operation,
        {
            "add_parent_ids": ["new-parent"],
            "file_id": "file",
            "remove_parent_ids": ["old-parent"],
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert transport.calls[-1]["query"] == (
        ("addParents", "new-parent"),
        ("removeParents", "old-parent"),
    )

    with pytest.raises(ValidationError, match="same parent"):
        adapter.execute(
            operation,
            {
                "add_parent_ids": ["same-parent"],
                "file_id": "file",
                "remove_parent_ids": ["same-parent"],
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_drive_comment_requests_use_required_valid_field_projections() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport(body=b'{"comments":[]}')
    adapter.execute(
        _operation("google_drive", "comments.list"),
        {"file_id": "file", "page_size": 20},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    fields = dict(transport.calls[-1]["query"])["fields"]
    assert "comments(" in fields
    assert "fileId" not in fields

    adapter.execute(
        _operation("google_drive", "comments.create"),
        {
            "content": "note",
            "file_id": "file",
            "quoted_file_content": {"value": "quote"},
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-comment",
    )
    assert transport.calls[-1]["query"]
    assert "fileId" not in dict(transport.calls[-1]["query"])["fields"]
    assert transport.calls[-1]["json_body"] == {
        "content": "note",
        "quotedFileContent": {"value": "quote"},
    }


def test_calendar_list_and_event_filters_use_fixed_google_parameters() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    credential = _credential()

    adapter.execute(
        _operation("google_calendar", "calendars.list"),
        {
            "min_access_role": "reader",
            "page_size": 250,
            "show_deleted": True,
            "show_hidden": False,
            "show_own_organization_only": True,
        },
        continuation=None,
        credential=credential,
        transport=transport,
    )
    assert transport.calls[-1]["query"] == (
        ("minAccessRole", "reader"),
        ("maxResults", "250"),
        ("showDeleted", "true"),
        ("showHidden", "false"),
        ("showOwnOrganizationOnly", "true"),
    )

    event_list = _operation("google_calendar", "events.list")
    adapter.execute(
        event_list,
        {
            "calendar_id": "primary",
            "event_types": ["default", "focusTime"],
            "i_cal_uid": "uid-1",
            "max_attendees": 20,
            "order_by": "startTime",
            "page_size": 2_500,
            "query": "planning",
            "show_deleted": True,
            "single_events": True,
            "time_max": "2026-08-02T00:00:00+02:00",
            "time_min": "2026-08-01T00:00:00+02:00",
            "time_zone": "Europe/Brussels",
            "updated_min": "2026-07-31T00:00:00+02:00",
        },
        continuation=None,
        credential=credential,
        transport=transport,
    )
    assert transport.calls[-1]["query"] == (
        ("iCalUID", "uid-1"),
        ("maxAttendees", "20"),
        ("orderBy", "startTime"),
        ("maxResults", "2500"),
        ("q", "planning"),
        ("showDeleted", "true"),
        ("singleEvents", "true"),
        ("timeMax", "2026-08-02T00:00:00+02:00"),
        ("timeMin", "2026-08-01T00:00:00+02:00"),
        ("timeZone", "Europe/Brussels"),
        ("updatedMin", "2026-07-31T00:00:00+02:00"),
        ("eventTypes", "default"),
        ("eventTypes", "focusTime"),
    )
    with pytest.raises(ValidationError, match="start-time ordering"):
        adapter.execute(
            event_list,
            {"calendar_id": "primary", "order_by": "startTime"},
            continuation=None,
            credential=credential,
            transport=transport,
        )


def test_drive_shared_drive_routes_remain_fixed_and_require_support() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    credential = _credential()

    adapter.execute(
        _operation("google_drive", "drives.list"),
        {"page_size": 100, "query": "name contains 'team'"},
        continuation="next-page",
        credential=credential,
        transport=transport,
    )
    assert transport.calls[-1]["path"] == "/drive/v3/drives"
    assert transport.calls[-1]["query"] == (
        ("pageSize", "100"),
        ("q", "name contains 'team'"),
        ("pageToken", "next-page"),
    )

    file_list = _operation("google_drive", "files.list")
    adapter.execute(
        file_list,
        {
            "corpora": "drive",
            "drive_id": "shared-drive",
            "include_items_from_all_drives": True,
            "order_by": ["modifiedTime desc", "name"],
            "page_size": 1_000,
            "spaces": ["drive"],
            "supports_all_drives": True,
        },
        continuation=None,
        credential=credential,
        transport=transport,
    )
    assert transport.calls[-1]["path"] == "/drive/v3/files"
    assert transport.calls[-1]["query"] == (
        ("corpora", "drive"),
        ("driveId", "shared-drive"),
        ("includeItemsFromAllDrives", "true"),
        ("pageSize", "1000"),
        ("supportsAllDrives", "true"),
        ("orderBy", "modifiedTime desc,name"),
        ("spaces", "drive"),
    )

    with pytest.raises(ValidationError, match="requires a drive ID"):
        adapter.execute(
            file_list,
            {"corpora": "drive", "supports_all_drives": True},
            continuation=None,
            credential=credential,
            transport=transport,
        )
    with pytest.raises(ValidationError, match="requires supports-all-drives"):
        adapter.execute(
            file_list,
            {"include_items_from_all_drives": True},
            continuation=None,
            credential=credential,
            transport=transport,
        )


def test_drive_downloads_construct_ranges_and_return_provider_offsets() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport(body=b"cde", headers={"content-range": "bytes 5-7/12"})
    credential = _credential()
    requests = (
        ("files.content", {"file_id": "file"}, "/drive/v3/files/file"),
        (
            "revisions.download",
            {"file_id": "file", "revision_id": "revision"},
            "/drive/v3/files/file/revisions/revision",
        ),
    )
    for name, values, path in requests:
        result = adapter.execute(
            _operation("google_drive", name),
            {
                **values,
                "byte_offset": 5,
                "delivery": "inline_chunk",
                "max_chunk_size": 3,
            },
            continuation=None,
            credential=credential,
            transport=transport,
        )
        call = transport.calls[-1]
        assert call["path"] == path
        assert call["headers"] == {"Range": "bytes=5-7"}
        assert call["expected_statuses"] == frozenset({206})
        assert result.payload == {
            "byte_offset": 5,
            "content_base64": "Y2Rl",
            "content_range": "bytes 5-7/12",
            "next_byte_offset": 8,
        }

    malformed = _Transport(body=b"cde", headers={"content-range": "bytes 0-2/3"})
    with pytest.raises(ValidationError, match="does not match"):
        adapter.execute(
            _operation("google_drive", "files.content"),
            {
                "byte_offset": 1,
                "delivery": "inline_chunk",
                "file_id": "file",
                "max_chunk_size": 3,
            },
            continuation=None,
            credential=credential,
            transport=malformed,
        )
