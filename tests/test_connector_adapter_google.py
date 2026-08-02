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
from continuity_kernel.connector_gmail_transfer import (
    GMAIL_MIGRATION_UPLOAD_MAX_BYTES,
    GMAIL_UPLOAD_MAX_BYTES,
)
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
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError


class _Transport(ConnectorTransport):
    def __init__(
        self,
        *,
        body: bytes = b"{}",
        bodies: Sequence[bytes] = (),
        headers: Mapping[str, str] | None = None,
        download_body: bytes | None = None,
        provider_errors: Sequence[int | None] = (),
    ) -> None:
        self.body = body
        self.bodies = list(bodies)
        self.headers = dict(headers or {})
        self.download_body = body if download_body is None else download_body
        self.provider_errors = list(provider_errors)
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append({"kind": "request", **kwargs})
        provider_error = self.provider_errors.pop(0) if self.provider_errors else None
        if provider_error is not None:
            raise ConnectorProviderError(origin=kwargs["origin"], status=provider_error)
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


class _OutcomeUnknownInitTransport(_Transport):
    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append({"kind": "request", **kwargs})
        raise ConnectorOutcomeUnknown("ambiguous upload-session initialization")


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
            "https://www.googleapis.com/auth/gmail.settings.basic",
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


def _event_end_time() -> dict[str, str]:
    return {"date_time": "2026-08-01T10:00:00+02:00", "time_zone": "Europe/Brussels"}


def _expected_event(
    *,
    etag: str = "etag",
    event_id: str = "event",
    event_type: str = "default",
    organizer: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "end": None,
        "etag": etag,
        "eventType": event_type,
        "id": event_id,
        "organizer": organizer,
        "start": None,
        "status": "confirmed",
        "summary": None,
    }


def _expected_calendar(
    calendar_id: str,
    *,
    access_role: str = "owner",
    primary: bool = False,
    summary: str | None = "Calendar",
) -> dict[str, object]:
    return {
        "accessRole": access_role,
        "id": calendar_id,
        "primary": primary,
        "summary": summary,
    }


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
    assert (
        adapter.max_local_file_bytes(
            _operation("gmail", "messages.send"),
            gmail_input,
            path=("attachments", 0, "local_file"),
        )
        == (GMAIL_UPLOAD_MAX_BYTES * 3) // 4
    )
    raw_input = {"local_file": "opaque-local-file"}
    assert (
        adapter.max_local_file_bytes(
            _operation("gmail", "messages.send"),
            raw_input,
            path=("local_file",),
        )
        == GMAIL_UPLOAD_MAX_BYTES
    )
    for name in ("messages.insert", "messages.import"):
        assert (
            adapter.max_local_file_bytes(
                _operation("gmail", name),
                raw_input,
                path=("local_file",),
            )
            == GMAIL_MIGRATION_UPLOAD_MAX_BYTES
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
        (_operation("gmail", "messages.insert"), gmail_input, ("local_file",)),
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
        if name in {
            "labels.list",
            "profile.get",
            "settings.auto_forwarding.get",
            "settings.filters.list",
            "settings.forwarding_addresses.list",
            "settings.imap.get",
            "settings.language.get",
            "settings.pop.get",
            "settings.send_as.list",
            "settings.vacation.get",
        }:
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
            "messages.trash",
            "messages.restore",
            "messages.purge",
        }:
            return {"message_id": "message"}
        if name == "messages.modify":
            return {"add_label_ids": ["INBOX"], "message_id": "message"}
        if name == "messages.batch_modify":
            return {"add_label_ids": ["INBOX"], "message_ids": ["message"]}
        if name == "messages.batch_purge":
            return {"message_ids": ["message"]}
        if name in {
            "threads.get",
            "threads.trash",
            "threads.restore",
            "threads.purge",
        }:
            return {"thread_id": "thread"}
        if name == "threads.modify":
            return {"remove_label_ids": ["INBOX"], "thread_id": "thread"}
        if name in {"drafts.create", "messages.send"}:
            return {"text_body": "hello", "to": ["recipient@example.test"]}
        if name in {"messages.insert", "messages.import"}:
            return {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}}
        if name in {"drafts.get", "drafts.update", "drafts.delete", "drafts.send"}:
            return {"draft_id": "draft"}
        if name == "labels.create":
            return {"name": "Projects"}
        if name in {"labels.get", "labels.update", "labels.delete"}:
            return {"label_id": "label"}
        if name == "settings.filters.get":
            return {"filter_id": "filter"}
        if name == "settings.filters.delete":
            return {
                "expected_filter": {
                    "action": {"addLabelIds": ["STARRED"]},
                    "criteria": {"from": "sender@example.test"},
                    "id": "filter",
                },
                "filter_id": "filter",
            }
        if name == "settings.filters.create":
            return {
                "action": {"add_label_ids": ["STARRED"]},
                "criteria": {"from": "sender@example.test"},
            }
        if name == "settings.forwarding_addresses.get":
            return {"forwarding_email": "forward@example.test"}
        if name == "settings.imap.update":
            return {
                "auto_expunge": False,
                "enabled": False,
                "expunge_behavior": "archive",
                "max_folder_size": 0,
            }
        if name == "settings.language.update":
            return {"display_language": "en-GB"}
        if name == "settings.pop.update":
            return {"access_window": "disabled", "disposition": "leaveInInbox"}
        if name == "settings.send_as.get":
            return {"send_as_email": "primary@example.test"}
        if name == "settings.send_as.patch":
            return {"send_as_email": "primary@example.test", "signature": "Signed"}
        if name == "settings.vacation.update":
            return {
                "enable_auto_reply": False,
                "end_time": None,
                "response_body_html": "",
                "response_body_plain_text": "",
                "response_subject": "",
                "restrict_to_contacts": False,
                "restrict_to_domain": False,
                "start_time": None,
            }
    if operation.provider == "google_calendar":
        if name == "colors.get":
            return {}
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
            return {"calendar_id": "primary", "etag": "etag", "summary": "Updated"}
        if name == "calendars.delete":
            return {
                "calendar_id": "secondary",
                "etag": "etag",
                "expected_calendar": _expected_calendar("secondary", summary="Secondary"),
            }
        if name == "events.create":
            return {
                "calendar_id": "primary",
                "end": _event_end_time(),
                "start": _event_time(),
            }
        if name == "events.update":
            return {
                "calendar_id": "primary",
                "etag": "etag",
                "event_id": "event",
                "summary": "Updated",
            }
        if name == "events.move":
            return {
                "calendar_id": "primary",
                "destination_calendar_id": "destination",
                "etag": "etag",
                "event_id": "event",
                "expected_destination_calendar": _expected_calendar("destination"),
                "expected_event": _expected_event(
                    organizer={"email": "owner@example.test", "self": True}
                ),
                "send_updates": "none",
            }
        if name == "events.respond":
            return {
                "calendar_id": "primary",
                "etag": "etag",
                "event_id": "event",
                "expected_event": _expected_event(
                    organizer={"email": "owner@example.test", "self": True}
                ),
                "response_status": "accepted",
            }
        if name == "events.delete":
            return {
                "calendar_id": "primary",
                "etag": "etag",
                "event_id": "event",
                "expected_event": _expected_event(
                    organizer={"email": "owner@example.test", "self": True}
                ),
                "send_updates": "none",
            }
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


def test_every_google_operation_uses_only_bounded_fixed_adapter_requests(tmp_path: Path) -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    credential = _credential()
    raw_upload = _prepared_upload(
        tmp_path,
        content=(
            b"Date: Sat, 1 Aug 2026 10:00:00 +0200\r\n"
            b"From: sender@example.test\r\n"
            b"To: recipient@example.test\r\n\r\nBody"
        ),
    )
    try:
        for operation in GOOGLE_OPERATIONS:
            transport.body = b"{}"
            transport.bodies = []
            before = len(transport.calls)
            expected_requests = 1
            transfer = None
            write_idempotency_key = None
            if operation.provider == "google_calendar" and operation.name == "calendars.get":
                transport.body = (
                    b'{"etag":"calendar-etag","id":"calendar-id","kind":"calendar#calendar"}'
                )
            if operation.provider == "google_calendar" and operation.name == "calendars.delete":
                transport.body = (
                    b'{"accessRole":"owner","id":"secondary","primary":false,"summary":"Secondary"}'
                )
                expected_requests = 2
            if operation.provider == "google_calendar" and operation.name in {
                "events.delete",
                "events.move",
                "events.respond",
                "events.update",
            }:
                transport.body = json.dumps(
                    {
                        "attendees": [{"email": "owner@example.test", "self": True}],
                        "etag": "etag",
                        "eventType": "default",
                        "id": "event",
                        "organizer": {"email": "owner@example.test", "self": True},
                        "status": "confirmed",
                    }
                ).encode()
                expected_requests = 2
                if operation.name == "events.move":
                    transport.bodies = [
                        transport.body,
                        b'{"accessRole":"owner","id":"destination","primary":false,'
                        b'"summary":"Calendar"}',
                        b"{}",
                    ]
                    expected_requests = 3
                if operation.name in {"events.delete", "events.move", "events.respond"}:
                    write_idempotency_key = "confirmed-calendar-change"
            if operation.provider == "google_drive" and operation.name == "files.download":
                transport.body = _drive_download_operation_body(
                    done=True,
                    response=_drive_download_response(partial=True),
                )
                expected_requests = 2
            if operation.provider == "gmail" and operation.name in {
                "messages.import",
                "messages.insert",
                "messages.send",
            }:
                transport.body = b'{"id":"message"}'
            if operation.provider == "gmail" and operation.name == "settings.send_as.patch":
                transport.body = b'{"isPrimary":true,"sendAsEmail":"primary@example.test"}'
                expected_requests = 2
            if operation.provider == "gmail" and operation.name in {
                "settings.send_as.get",
                "settings.send_as.list",
            }:
                transport.body = (
                    b'{"sendAs":[{"sendAsEmail":"primary@example.test"}]}'
                    if operation.name.endswith(".list")
                    else b'{"sendAsEmail":"primary@example.test"}'
                )
            if operation.provider == "gmail" and operation.name == "settings.filters.delete":
                transport.body = (
                    b'{"action":{"addLabelIds":["STARRED"]},'
                    b'"criteria":{"from":"sender@example.test"},"id":"filter"}'
                )
                expected_requests = 2
            if operation.provider == "gmail" and operation.name in {
                "messages.import",
                "messages.insert",
            }:
                transfer = ConnectorTransferContext(uploads={("local_file",): raw_upload})
                write_idempotency_key = "confirmed-raw-upload"
                expected_requests = 2
            adapter.execute(
                operation,
                _sample(operation),
                continuation=None,
                credential=credential,
                transport=transport,
                transfer=transfer,
                write_idempotency_key=write_idempotency_key,
            )
            assert len(transport.calls) == before + expected_requests
    finally:
        raw_upload.close()
    assert {call["origin"] for call in transport.calls} == {
        ConnectorOrigin.GMAIL,
        ConnectorOrigin.GOOGLE,
    }
    assert all(
        ("path" not in call or call["path"].startswith("/"))
        and (
            "location" not in call
            or call["location"].startswith(
                ("https://drive.usercontent.google.com/", "https://gmail.googleapis.com/")
            )
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


def test_calendar_incremental_sync_preserves_paired_tokens_and_rotates_atomically() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.list")
    values = {
        "calendar_id": "primary",
        "event_types": ["default"],
        "max_attendees": 10,
        "show_deleted": True,
        "single_events": True,
        "time_zone": "Europe/Brussels",
    }

    initial = adapter.execute(
        operation,
        values,
        continuation=None,
        credential=_credential(),
        transport=_Transport(body=b'{"items":[],"nextSyncToken":"sync-1"}'),
    )
    assert initial.continuation == {"syncToken": "sync-1"}

    page_transport = _Transport(body=b'{"items":[],"nextPageToken":"page-2"}')
    page = adapter.execute(
        operation,
        values,
        continuation=initial.continuation,
        credential=_credential(),
        transport=page_transport,
    )
    assert page.continuation == {"pageToken": "page-2", "syncToken": "sync-1"}
    assert ("syncToken", "sync-1") in page_transport.calls[0]["query"]

    final_transport = _Transport(body=b'{"items":[],"nextSyncToken":"sync-2"}')
    final = adapter.execute(
        operation,
        values,
        continuation=page.continuation,
        credential=_credential(),
        transport=final_transport,
    )
    assert final.continuation == {"syncToken": "sync-2"}
    assert ("syncToken", "sync-1") in final_transport.calls[0]["query"]
    assert ("pageToken", "page-2") in final_transport.calls[0]["query"]


def test_calendar_sync_suppresses_unreplayable_cursors_and_maps_expiry() -> None:
    adapter = GoogleConnectorAdapter()
    credential = _credential()
    events = _operation("google_calendar", "events.list")
    filtered = adapter.execute(
        events,
        {
            "calendar_id": "primary",
            "time_min": "2026-08-01T00:00:00Z",
        },
        continuation=None,
        credential=credential,
        transport=_Transport(body=b'{"items":[],"nextSyncToken":"unusable"}'),
    )
    assert filtered.continuation is None

    calendars = _operation("google_calendar", "calendars.list")
    filtered_calendars = adapter.execute(
        calendars,
        {"min_access_role": "writer"},
        continuation=None,
        credential=credential,
        transport=_Transport(body=b'{"items":[],"nextSyncToken":"unusable"}'),
    )
    assert filtered_calendars.continuation is None

    instances = adapter.execute(
        _operation("google_calendar", "events.instances"),
        {"calendar_id": "primary", "event_id": "recurring-event"},
        continuation=None,
        credential=credential,
        transport=_Transport(body=b'{"items":[],"nextSyncToken":"unusable"}'),
    )
    assert instances.continuation is None

    expired = _Transport(provider_errors=(410,))
    with pytest.raises(ConnectorProviderError) as exc_info:
        adapter.execute(
            events,
            {"calendar_id": "primary", "show_deleted": True},
            continuation={"syncToken": "expired"},
            credential=credential,
            transport=expired,
        )
    assert exc_info.value.code == "full_sync_required"


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


def test_gmail_recoverable_moves_remain_one_step() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("gmail", "messages.modify")
    for mutation in (
        {"add_label_ids": ["TRASH"], "message_id": "message"},
        {"add_label_ids": ["SPAM"], "message_id": "message"},
        {"message_id": "message", "remove_label_ids": ["INBOX"]},
    ):
        assert adapter.classify_effect(operation, mutation) is ConnectorEffect.SAFE_MUTATION
        transport = _Transport()
        adapter.execute(
            operation,
            mutation,
            continuation=None,
            credential=_credential(),
            transport=transport,
        )
        assert transport.calls[-1]["path"] == "/gmail/v1/users/me/messages/message/modify"

    for name, identifier in (
        ("messages.trash", {"message_id": "message"}),
        ("threads.trash", {"thread_id": "thread"}),
    ):
        assert (
            adapter.classify_effect(_operation("gmail", name), identifier)
            is ConnectorEffect.SAFE_MUTATION
        )


@pytest.mark.parametrize(
    ("name", "values", "expected"),
    (
        (
            "messages.insert",
            {"internal_date_source": "receivedTime"},
            ConnectorEffect.SAFE_MUTATION,
        ),
        ("messages.insert", {"deleted": True}, ConnectorEffect.PERMANENT),
        (
            "messages.import",
            {"internal_date_source": "dateHeader", "process_for_calendar": True},
            ConnectorEffect.OUTWARD,
        ),
        (
            "messages.import",
            {
                "deleted": True,
                "internal_date_source": "dateHeader",
                "process_for_calendar": True,
            },
            ConnectorEffect.PERMANENT,
        ),
    ),
)
def test_gmail_raw_migration_effects_follow_exact_provider_consequences(
    tmp_path: Path,
    name: str,
    values: dict[str, object],
    expected: ConnectorEffect,
) -> None:
    upload = _prepared_upload(
        tmp_path,
        content=(
            b"Date: Sat, 1 Aug 2026 10:00:00 +0200\r\n"
            b"From: sender@example.test\r\n"
            b"To: recipient@example.test\r\n\r\nBody"
        ),
    )
    try:
        effect = GoogleConnectorAdapter().classify_effect(
            _operation("gmail", name),
            values,
            transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
        )
        assert effect is expected
    finally:
        upload.close()


@pytest.mark.parametrize(
    ("name", "values", "path"),
    (
        ("settings.auto_forwarding.get", {}, "/gmail/v1/users/me/settings/autoForwarding"),
        ("settings.imap.get", {}, "/gmail/v1/users/me/settings/imap"),
        ("settings.language.get", {}, "/gmail/v1/users/me/settings/language"),
        ("settings.pop.get", {}, "/gmail/v1/users/me/settings/pop"),
        ("settings.vacation.get", {}, "/gmail/v1/users/me/settings/vacation"),
        ("settings.filters.list", {}, "/gmail/v1/users/me/settings/filters"),
        (
            "settings.filters.get",
            {"filter_id": "filter/id"},
            "/gmail/v1/users/me/settings/filters/filter%2Fid",
        ),
        (
            "settings.forwarding_addresses.list",
            {},
            "/gmail/v1/users/me/settings/forwardingAddresses",
        ),
        (
            "settings.forwarding_addresses.get",
            {"forwarding_email": "archive+mail@example.test"},
            "/gmail/v1/users/me/settings/forwardingAddresses/archive%2Bmail%40example.test",
        ),
    ),
)
def test_gmail_settings_reads_use_only_fixed_me_routes(
    name: str,
    values: dict[str, object],
    path: str,
) -> None:
    transport = _Transport()
    GoogleConnectorAdapter().execute(
        _operation("gmail", name),
        values,
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] is ConnectorMethod.GET
    assert transport.calls[0]["path"] == path
    assert transport.calls[0]["query"] == ()


def test_gmail_send_as_reads_allowlist_output_and_drop_smtp_credentials() -> None:
    adapter = GoogleConnectorAdapter()
    resource = {
        "displayName": "Ada",
        "isDefault": True,
        "isPrimary": True,
        "replyToAddress": "reply@example.test",
        "sendAsEmail": "primary@example.test",
        "signature": "<p>Signed</p>",
        "smtpMsa": {
            "host": "smtp.example.test",
            "password": "must-never-escape",
            "username": "private-user",
        },
        "unexpectedCredential": "must-never-escape",
        "verificationStatus": "accepted",
    }
    list_transport = _Transport(body=json.dumps({"sendAs": [resource]}).encode())
    listed = adapter.execute(
        _operation("gmail", "settings.send_as.list"),
        {},
        continuation=None,
        credential=_credential(),
        transport=list_transport,
    )
    get_transport = _Transport(body=json.dumps(resource).encode())
    fetched = adapter.execute(
        _operation("gmail", "settings.send_as.get"),
        {"send_as_email": "primary@example.test"},
        continuation=None,
        credential=_credential(),
        transport=get_transport,
    )

    expected = {
        key: value
        for key, value in resource.items()
        if key not in {"smtpMsa", "unexpectedCredential"}
    }
    assert listed.payload == {"sendAs": [expected]}
    assert fetched.payload == expected
    assert "must-never-escape" not in repr((listed.payload, fetched.payload))
    assert list_transport.calls[0]["query"] == (
        (
            "fields",
            "sendAs(displayName,isDefault,isPrimary,replyToAddress,sendAsEmail,signature,"
            "treatAsAlias,verificationStatus)",
        ),
    )
    assert get_transport.calls[0]["query"] == (
        (
            "fields",
            "displayName,isDefault,isPrimary,replyToAddress,sendAsEmail,signature,treatAsAlias,"
            "verificationStatus",
        ),
    )


@pytest.mark.parametrize(
    "malformed_field",
    (
        {"signature": {"smtpMsa": {"password": "nested-secret"}}},
        {"isPrimary": "true"},
    ),
)
def test_gmail_send_as_reads_reject_non_scalar_provider_fields(
    malformed_field: dict[str, object],
) -> None:
    resource = {"sendAsEmail": "primary@example.test", **malformed_field}

    with pytest.raises(ValidationError, match="send-as response has an invalid"):
        GoogleConnectorAdapter().execute(
            _operation("gmail", "settings.send_as.get"),
            {"send_as_email": "primary@example.test"},
            continuation=None,
            credential=_credential(),
            transport=_Transport(body=json.dumps(resource).encode()),
        )


@pytest.mark.parametrize(
    ("name", "values", "path", "body"),
    (
        (
            "settings.imap.update",
            {
                "auto_expunge": False,
                "enabled": True,
                "expunge_behavior": "trash",
                "max_folder_size": 5_000,
            },
            "/gmail/v1/users/me/settings/imap",
            {
                "autoExpunge": False,
                "enabled": True,
                "expungeBehavior": "trash",
                "maxFolderSize": 5_000,
            },
        ),
        (
            "settings.language.update",
            {"display_language": "fr"},
            "/gmail/v1/users/me/settings/language",
            {"displayLanguage": "fr"},
        ),
        (
            "settings.pop.update",
            {"access_window": "allMail", "disposition": "markRead"},
            "/gmail/v1/users/me/settings/pop",
            {"accessWindow": "allMail", "disposition": "markRead"},
        ),
        (
            "settings.vacation.update",
            {
                "enable_auto_reply": True,
                "end_time": "1785772800000",
                "response_body_html": "",
                "response_body_plain_text": "Back Monday",
                "response_subject": "Away",
                "restrict_to_contacts": True,
                "restrict_to_domain": False,
                "start_time": "1785686400000",
            },
            "/gmail/v1/users/me/settings/vacation",
            {
                "enableAutoReply": True,
                "endTime": "1785772800000",
                "responseBodyHtml": "",
                "responseBodyPlainText": "Back Monday",
                "responseSubject": "Away",
                "restrictToContacts": True,
                "restrictToDomain": False,
                "startTime": "1785686400000",
            },
        ),
        (
            "settings.vacation.update",
            {
                "enable_auto_reply": False,
                "end_time": None,
                "response_body_html": "",
                "response_body_plain_text": "Saved for later",
                "response_subject": "",
                "restrict_to_contacts": False,
                "restrict_to_domain": False,
                "start_time": None,
            },
            "/gmail/v1/users/me/settings/vacation",
            {
                "enableAutoReply": False,
                "responseBodyHtml": "",
                "responseBodyPlainText": "Saved for later",
                "responseSubject": "",
                "restrictToContacts": False,
                "restrictToDomain": False,
            },
        ),
    ),
)
def test_gmail_basic_settings_updates_use_exact_put_payloads(
    name: str,
    values: dict[str, object],
    path: str,
    body: dict[str, object],
) -> None:
    transport = _Transport()
    GoogleConnectorAdapter().execute(
        _operation("gmail", name),
        values,
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] is ConnectorMethod.PUT
    assert transport.calls[0]["path"] == path
    assert transport.calls[0]["json_body"] == body


def test_gmail_filter_routes_validate_and_preserve_exact_criteria_and_actions() -> None:
    values = {
        "action": {
            "add_label_ids": ["STARRED"],
            "forward": "archive@example.test",
            "remove_label_ids": ["UNREAD"],
        },
        "criteria": {
            "exclude_chats": True,
            "from": "sender@example.test",
            "has_attachment": True,
            "negated_query": "label:spam",
            "query": "newer_than:1d",
            "size": 1_024,
            "size_comparison": "larger",
            "subject": "Status",
            "to": "team@example.test",
        },
    }
    transport = _Transport(body=b'{"id":"filter"}')
    result = GoogleConnectorAdapter().execute(
        _operation("gmail", "settings.filters.create"),
        values,
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert result.payload == {"id": "filter"}
    assert transport.calls[0]["method"] is ConnectorMethod.POST
    assert transport.calls[0]["path"] == "/gmail/v1/users/me/settings/filters"
    assert transport.calls[0]["json_body"] == {
        "action": {
            "addLabelIds": ["STARRED"],
            "forward": "archive@example.test",
            "removeLabelIds": ["UNREAD"],
        },
        "criteria": {
            "excludeChats": True,
            "from": "sender@example.test",
            "hasAttachment": True,
            "negatedQuery": "label:spam",
            "query": "newer_than:1d",
            "size": 1_024,
            "sizeComparison": "larger",
            "subject": "Status",
            "to": "team@example.test",
        },
    }

    expected_filter = {
        "action": {"addLabelIds": ["STARRED"]},
        "criteria": {"from": "sender@example.test"},
        "id": "filter/id",
    }
    deleted = _Transport(bodies=(json.dumps(expected_filter).encode(), b"{}"))
    GoogleConnectorAdapter().execute(
        _operation("gmail", "settings.filters.delete"),
        {"expected_filter": expected_filter, "filter_id": "filter/id"},
        continuation=None,
        credential=_credential(),
        transport=deleted,
    )
    assert [call["method"] for call in deleted.calls] == [
        ConnectorMethod.GET,
        ConnectorMethod.DELETE,
    ]
    assert {call["path"] for call in deleted.calls} == {
        "/gmail/v1/users/me/settings/filters/filter%2Fid"
    }


def test_gmail_filter_delete_fails_closed_when_the_reviewed_rule_changed() -> None:
    expected = {
        "action": {"addLabelIds": ["STARRED"]},
        "criteria": {"from": "sender@example.test"},
        "id": "filter",
    }
    changed = {
        **expected,
        "action": {"forward": "archive@example.test"},
    }
    transport = _Transport(body=json.dumps(changed).encode())

    with pytest.raises(ConflictError, match="read it again"):
        GoogleConnectorAdapter().classify_effect(
            _operation("gmail", "settings.filters.delete"),
            {"expected_filter": expected, "filter_id": "filter"},
            credential=_credential(),
            transport=transport,
        )

    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] is ConnectorMethod.GET


def test_gmail_filter_delete_rejects_unreviewed_provider_fields() -> None:
    expected = {
        "action": {"addLabelIds": ["STARRED"]},
        "criteria": {"from": "sender@example.test"},
        "id": "filter",
    }
    transport = _Transport(
        body=json.dumps({**expected, "futureAction": {"destination": "unknown"}}).encode()
    )

    with pytest.raises(ValidationError, match="unsupported fields"):
        GoogleConnectorAdapter().classify_effect(
            _operation("gmail", "settings.filters.delete"),
            {"expected_filter": expected, "filter_id": "filter"},
            credential=_credential(),
            transport=transport,
        )

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("name", "values", "expected"),
    (
        (
            "settings.filters.create",
            {
                "action": {"add_label_ids": ["STARRED"]},
                "criteria": {"from": "sender@example.test"},
            },
            ConnectorEffect.SAFE_MUTATION,
        ),
        (
            "settings.filters.create",
            {
                "action": {"forward": "archive@example.test"},
                "criteria": {"from": "sender@example.test"},
            },
            ConnectorEffect.OUTWARD,
        ),
        (
            "settings.filters.create",
            {
                "action": {"add_label_ids": ["TRASH"], "forward": "archive@example.test"},
                "criteria": {"from": "sender@example.test"},
            },
            ConnectorEffect.DESTRUCTIVE,
        ),
        (
            "settings.filters.create",
            {
                "action": {"remove_label_ids": ["INBOX"]},
                "criteria": {"from": "sender@example.test"},
            },
            ConnectorEffect.DESTRUCTIVE,
        ),
        (
            "settings.filters.create",
            {
                "action": {"add_label_ids": ["SPAM"]},
                "criteria": {"from": "sender@example.test"},
            },
            ConnectorEffect.DESTRUCTIVE,
        ),
        (
            "settings.imap.update",
            {
                "auto_expunge": True,
                "enabled": True,
                "expunge_behavior": "deleteForever",
                "max_folder_size": 0,
            },
            ConnectorEffect.PERMANENT,
        ),
        (
            "settings.imap.update",
            {
                "auto_expunge": False,
                "enabled": True,
                "expunge_behavior": "archive",
                "max_folder_size": 0,
            },
            ConnectorEffect.OUTWARD,
        ),
        (
            "settings.pop.update",
            {"access_window": "allMail", "disposition": "leaveInInbox"},
            ConnectorEffect.OUTWARD,
        ),
        (
            "settings.pop.update",
            {"access_window": "disabled", "disposition": "trash"},
            ConnectorEffect.DESTRUCTIVE,
        ),
        (
            "settings.vacation.update",
            {
                "enable_auto_reply": True,
                "end_time": None,
                "response_body_html": "",
                "response_body_plain_text": "",
                "response_subject": "Away",
                "restrict_to_contacts": False,
                "restrict_to_domain": False,
                "start_time": None,
            },
            ConnectorEffect.OUTWARD,
        ),
        (
            "settings.vacation.update",
            {
                "enable_auto_reply": False,
                "end_time": None,
                "response_body_html": "",
                "response_body_plain_text": "",
                "response_subject": "",
                "restrict_to_contacts": False,
                "restrict_to_domain": False,
                "start_time": None,
            },
            ConnectorEffect.SAFE_MUTATION,
        ),
    ),
)
def test_gmail_settings_effects_match_future_provider_consequences(
    name: str,
    values: dict[str, object],
    expected: ConnectorEffect,
) -> None:
    assert GoogleConnectorAdapter().classify_effect(_operation("gmail", name), values) is expected


@pytest.mark.parametrize(
    ("values", "message"),
    (
        (
            {"action": {}, "criteria": {"from": "sender@example.test"}},
            "at least one action",
        ),
        (
            {
                "action": {"add_label_ids": ["STARRED"], "remove_label_ids": ["STARRED"]},
                "criteria": {"from": "sender@example.test"},
            },
            "same label",
        ),
        (
            {"action": {"add_label_ids": ["STARRED"]}, "criteria": {"size": 10}},
            "size and comparison",
        ),
    ),
)
def test_gmail_filter_validation_fails_before_provider_access(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        GoogleConnectorAdapter().classify_effect(
            _operation("gmail", "settings.filters.create"), values
        )


@pytest.mark.parametrize(
    "values",
    (
        {"enable_auto_reply": True},
        {
            "enable_auto_reply": True,
            "end_time": "100",
            "response_body_html": "",
            "response_body_plain_text": "",
            "response_subject": "Away",
            "restrict_to_contacts": False,
            "restrict_to_domain": False,
            "start_time": "100",
        },
        {
            "enable_auto_reply": True,
            "end_time": None,
            "response_body_html": "",
            "response_body_plain_text": "",
            "response_subject": "Away",
            "restrict_to_contacts": False,
            "restrict_to_domain": False,
            "start_time": str(2**63),
        },
    ),
)
def test_gmail_vacation_validation_fails_before_provider_access(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GoogleConnectorAdapter().classify_effect(
            _operation("gmail", "settings.vacation.update"), values
        )


def test_gmail_send_as_patch_is_primary_only_and_never_accepts_smtp_credentials() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("gmail", "settings.send_as.patch")
    values = {
        "display_name": "Ada",
        "is_default": True,
        "reply_to_address": "reply@example.test",
        "send_as_email": "primary@example.test",
        "signature": "<p>Signed</p>",
    }
    primary = _Transport(body=b'{"isPrimary":true,"sendAsEmail":"primary@example.test"}')
    assert (
        adapter.classify_effect(
            operation,
            values,
            credential=_credential(),
            transport=primary,
        )
        is ConnectorEffect.OUTWARD
    )
    assert len(primary.calls) == 1
    assert primary.calls[0]["query"] == (("fields", "isPrimary,sendAsEmail"),)

    primary.calls.clear()
    adapter.execute(
        operation,
        values,
        continuation=None,
        credential=_credential(),
        transport=primary,
    )
    assert [call["method"] for call in primary.calls] == [
        ConnectorMethod.GET,
        ConnectorMethod.PATCH,
    ]
    assert primary.calls[1]["path"] == ("/gmail/v1/users/me/settings/sendAs/primary%40example.test")
    assert primary.calls[1]["json_body"] == {
        "displayName": "Ada",
        "isDefault": True,
        "replyToAddress": "reply@example.test",
        "signature": "<p>Signed</p>",
    }

    malformed_patch = _Transport(
        bodies=(
            b'{"isPrimary":true,"sendAsEmail":"primary@example.test"}',
            (
                b'{"sendAsEmail":"primary@example.test",'
                b'"signature":{"unexpectedCredential":"nested-secret"}}'
            ),
        )
    )
    with pytest.raises(ValidationError, match="invalid signature"):
        adapter.execute(
            operation,
            values,
            continuation=None,
            credential=_credential(),
            transport=malformed_patch,
        )

    custom = _Transport(body=b'{"isPrimary":false,"sendAsEmail":"alias@example.test"}')
    with pytest.raises(ValidationError, match="only the primary"):
        adapter.classify_effect(
            operation,
            {"send_as_email": "alias@example.test", "signature": "Signed"},
            credential=_credential(),
            transport=custom,
        )
    assert len(custom.calls) == 1

    with pytest.raises(ValidationError, match="at least one setting change"):
        adapter.classify_effect(
            operation,
            {"send_as_email": "primary@example.test"},
            credential=_credential(),
            transport=_Transport(),
        )

    with pytest.raises(ValidationError):
        operation.validate_input(
            {
                "send_as_email": "primary@example.test",
                "smtp_password": "must-never-enter",
            }
        )


def test_gmail_batch_modify_and_purge_use_fixed_provider_routes() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    adapter.execute(
        _operation("gmail", "messages.batch_modify"),
        {
            "add_label_ids": ["TRASH"],
            "message_ids": ["message-1", "message-2"],
            "remove_label_ids": ["INBOX"],
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    modify = transport.calls[-1]
    assert modify["path"] == "/gmail/v1/users/me/messages/batchModify"
    assert modify["json_body"] == {
        "addLabelIds": ["TRASH"],
        "ids": ["message-1", "message-2"],
        "removeLabelIds": ["INBOX"],
    }
    assert 204 in modify["expected_statuses"]

    adapter.execute(
        _operation("gmail", "messages.batch_purge"),
        {"message_ids": ["message-1", "message-2"]},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    purge = transport.calls[-1]
    assert purge["path"] == "/gmail/v1/users/me/messages/batchDelete"
    assert purge["json_body"] == {"ids": ["message-1", "message-2"]}
    assert 204 in purge["expected_statuses"]


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


def test_gmail_structured_message_send_uses_safe_mime() -> None:
    transport = _Transport(
        body=json.dumps(
            {
                "historyId": "42",
                "id": "message-1",
                "labelIds": ["SENT"],
                "payload": {"body": {"data": "private"}},
                "raw": "private",
                "sizeEstimate": 123,
                "snippet": "private body preview",
                "threadId": "thread-1",
            }
        ).encode()
    )
    result = GoogleConnectorAdapter().execute(
        _operation("gmail", "messages.send"),
        {
            "bcc": ["audit@example.test"],
            "subject": "Direct send",
            "text_body": "Hello",
            "to": ["recipient@example.test"],
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert result.payload == {
        "historyId": "42",
        "id": "message-1",
        "labelIds": ["SENT"],
        "sizeEstimate": 123,
        "threadId": "thread-1",
    }
    call = transport.calls[-1]
    assert call["path"] == "/gmail/v1/users/me/messages/send"
    encoded = call["json_body"]["raw"]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message["To"] == "recipient@example.test"
    assert message["Bcc"] == "audit@example.test"
    assert message["Subject"] == "Direct send"


def test_gmail_structured_send_malformed_success_receipt_is_outcome_unknown() -> None:
    transport = _Transport(body=b"{")

    with pytest.raises(
        ConnectorOutcomeUnknown,
        match=r"returned no usable receipt;.*do not retry automatically",
    ):
        GoogleConnectorAdapter().execute(
            _operation("gmail", "messages.send"),
            {"text_body": "Hello", "to": ["recipient@example.test"]},
            continuation=None,
            credential=_credential(),
            transport=transport,
        )

    assert [call["kind"] for call in transport.calls] == ["request"]


def test_gmail_structured_message_send_preserves_verified_reply_threading() -> None:
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
    transport = _Transport(bodies=(metadata, b'{"id":"message"}'))
    GoogleConnectorAdapter().execute(
        _operation("gmail", "messages.send"),
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
    sent = transport.calls[-1]
    assert sent["path"] == "/gmail/v1/users/me/messages/send"
    assert sent["json_body"]["threadId"] == "provider-thread-7"
    encoded = sent["json_body"]["raw"]
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    )
    assert message["In-Reply-To"] == "<original@example.test>"
    assert message["Subject"] == "Planning"


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


def test_gmail_raw_message_streams_to_a_private_rfc822_artifact(tmp_path: Path) -> None:
    content = b"From: sender@example.test\r\nTo: owner@example.test\r\n\r\nHello"
    encoded = base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")
    body = json.dumps({"raw": encoded}).encode("utf-8")
    store = ArtifactStore(tmp_path / "artifacts")
    scope = ConnectorArtifactScope(store)
    transfer = ConnectorTransferContext(_artifact_scope_factory=lambda: scope)
    transport = _Transport(download_body=body)
    result = GoogleConnectorAdapter().execute(
        _operation("gmail", "messages.get"),
        {"format": "raw", "message_id": "message"},
        continuation=None,
        credential=_credential(),
        transport=transport,
        transfer=transfer,
    )
    try:
        assert result.payload == {"bytes": len(content), "delivery": "artifact"}
        assert result.artifact is not None
        assert result.artifact.media_type == "message/rfc822"
        assert result.artifact.path.read_bytes() == content
        call = transport.calls[-1]
        assert call["path"] == "/gmail/v1/users/me/messages/message"
        assert call["query"] == (("format", "raw"), ("fields", "raw"))
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
    ("name", "method", "draft_path", "extra", "metadata"),
    (
        (
            "drafts.create",
            ConnectorMethod.POST,
            "/upload/gmail/v1/users/me/drafts",
            {},
            {"message": {}},
        ),
        (
            "drafts.update",
            ConnectorMethod.PUT,
            "/upload/gmail/v1/users/me/drafts/draft",
            {"draft_id": "draft"},
            {"message": {}},
        ),
        (
            "messages.send",
            ConnectorMethod.POST,
            "/upload/gmail/v1/users/me/messages/send",
            {"to": ["recipient@example.test"]},
            {},
        ),
    ),
)
def test_gmail_local_file_attachment_uses_resumable_rfc822_upload(
    tmp_path: Path,
    name: str,
    method: ConnectorMethod,
    draft_path: str,
    extra: dict[str, object],
    metadata: dict[str, object],
) -> None:
    upload = _prepared_upload(tmp_path)
    transfer = ConnectorTransferContext(uploads={("attachments", 0, "local_file"): upload})
    transport = _Transport(body=b'{"id":"message"}' if name == "messages.send" else b"{}")
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
        assert result.payload == ({"id": "message"} if name == "messages.send" else {})
        initiated, sent = transport.calls
        assert initiated["method"] is method
        assert initiated["path"] == draft_path
        assert initiated["query"] == (("uploadType", "resumable"),)
        assert initiated["headers"] == {
            "X-Upload-Content-Length": str(sent["content_length"]),
            "X-Upload-Content-Type": "message/rfc822",
        }
        assert initiated["json_body"] == metadata
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


def test_gmail_attachment_send_malformed_success_receipt_is_outcome_unknown(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(tmp_path)
    transfer = ConnectorTransferContext(uploads={("attachments", 0, "local_file"): upload})
    transport = _Transport(body=b"{")
    try:
        with pytest.raises(
            ConnectorOutcomeUnknown,
            match=r"returned no usable receipt;.*do not retry automatically",
        ):
            GoogleConnectorAdapter().execute(
                _operation("gmail", "messages.send"),
                {
                    "attachments": [{"filename": "note.txt", "mime_type": "text/plain"}],
                    "text_body": "Hello",
                    "to": ["recipient@example.test"],
                },
                continuation=None,
                credential=_credential(),
                transport=transport,
                transfer=transfer,
            )
        assert [call["kind"] for call in transport.calls] == ["request", "stream"]
    finally:
        upload.close()


@pytest.mark.parametrize(
    ("name", "values", "upload_path", "query", "metadata"),
    (
        (
            "messages.send",
            {},
            "/upload/gmail/v1/users/me/messages/send",
            (("uploadType", "resumable"),),
            {},
        ),
        (
            "messages.insert",
            {
                "deleted": False,
                "internal_date_source": "receivedTime",
                "label_ids": ["INBOX", "STARRED"],
            },
            "/upload/gmail/v1/users/me/messages",
            (
                ("uploadType", "resumable"),
                ("internalDateSource", "receivedTime"),
                ("deleted", "false"),
            ),
            {"labelIds": ["INBOX", "STARRED"]},
        ),
        (
            "messages.import",
            {
                "deleted": True,
                "internal_date_source": "dateHeader",
                "label_ids": ["archive"],
                "never_mark_spam": True,
                "process_for_calendar": True,
            },
            "/upload/gmail/v1/users/me/messages/import",
            (
                ("uploadType", "resumable"),
                ("internalDateSource", "dateHeader"),
                ("neverMarkSpam", "true"),
                ("processForCalendar", "true"),
                ("deleted", "true"),
            ),
            {"labelIds": ["archive"]},
        ),
    ),
)
def test_gmail_raw_message_upload_uses_exact_snapshot_route_flags_and_safe_receipt(
    tmp_path: Path,
    name: str,
    values: dict[str, object],
    upload_path: str,
    query: tuple[tuple[str, str], ...],
    metadata: dict[str, object],
) -> None:
    content = (
        b"Date: Sat, 1 Aug 2026 10:00:00 +0200\r\n"
        b"From: Sender <sender@example.test>\r\n"
        b"To: recipient@example.test\r\n"
        b"Bcc: hidden@example.test\r\n"
        b"Subject: Immutable raw message\r\n\r\nExact body bytes\x00\xff"
    )
    upload = _prepared_upload(tmp_path, content=content)
    transport = _Transport(
        body=json.dumps(
            {
                "historyId": "43",
                "id": "message-2",
                "labelIds": ["INBOX"],
                "location": "private-upload-location",
                "payload": {"headers": [{"name": "Subject", "value": "private"}]},
                "raw": "private-raw",
                "sizeEstimate": len(content),
                "snippet": "private body preview",
                "threadId": "thread-2",
            }
        ).encode()
    )
    try:
        result = GoogleConnectorAdapter().execute(
            _operation("gmail", name),
            {
                **values,
                "local_file": {"grant_id": "grant", "relative_path": "message.eml"},
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
            write_idempotency_key="confirmed-raw-upload",
        )

        assert result.payload == {
            "historyId": "43",
            "id": "message-2",
            "labelIds": ["INBOX"],
            "sizeEstimate": len(content),
            "threadId": "thread-2",
        }
        initiated, sent = transport.calls
        assert initiated["kind"] == "request"
        assert initiated["method"] is ConnectorMethod.POST
        assert initiated["path"] == upload_path
        assert initiated["query"] == query
        assert initiated["json_body"] == metadata
        assert initiated["headers"] == {
            "X-Upload-Content-Length": str(len(content)),
            "X-Upload-Content-Type": "message/rfc822",
        }
        assert initiated["expected_statuses"] == frozenset({200})
        assert sent["kind"] == "stream"
        assert sent["source"] is upload
        assert b"".join(upload.iter_chunks()) == content
        assert sent["content_length"] == len(content)
        assert sent["content_type"] == "message/rfc822"
        assert sent["credential"] is None
        assert sent["expected_statuses"] == frozenset({200, 201})
        assert "body" not in sent
        assert "headers" not in sent
    finally:
        upload.close()


def test_gmail_raw_message_upload_requires_confirmation_before_transport(tmp_path: Path) -> None:
    upload = _prepared_upload(
        tmp_path,
        content=b"To: recipient@example.test\r\nSubject: One shot\r\n\r\nBody",
    )
    transport = _Transport()
    try:
        with pytest.raises(ValidationError, match="fresh confirmation"):
            GoogleConnectorAdapter().execute(
                _operation("gmail", "messages.send"),
                {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}},
                continuation=None,
                credential=_credential(),
                transport=transport,
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
            )
        assert transport.calls == []
    finally:
        upload.close()


@pytest.mark.parametrize("name", ("messages.send", "messages.insert", "messages.import"))
def test_gmail_raw_selector_without_its_prepared_snapshot_fails_closed(
    name: str,
) -> None:
    transport = _Transport()

    with pytest.raises(ValidationError, match="matching prepared transfer"):
        GoogleConnectorAdapter().execute(
            _operation("gmail", name),
            {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}},
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-raw-upload",
        )

    assert transport.calls == []


def test_gmail_ambiguous_raw_upload_initialization_sends_no_message_bytes(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(
        tmp_path,
        content=b"To: recipient@example.test\r\nSubject: One shot\r\n\r\nBody",
    )
    transport = _OutcomeUnknownInitTransport()
    try:
        with pytest.raises(
            ConnectorOutcomeUnknown,
            match="no message bytes were sent, so request a fresh confirmation",
        ):
            GoogleConnectorAdapter().execute(
                _operation("gmail", "messages.send"),
                {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}},
                continuation=None,
                credential=_credential(),
                transport=transport,
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
                write_idempotency_key="confirmed-raw-upload",
            )
        assert [call["kind"] for call in transport.calls] == ["request"]
    finally:
        upload.close()


def test_gmail_malformed_success_receipt_is_outcome_unknown_and_not_replayed(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(
        tmp_path,
        content=b"To: recipient@example.test\r\nSubject: One shot\r\n\r\nBody",
    )
    transport = _Transport(body=b"{")
    try:
        with pytest.raises(
            ConnectorOutcomeUnknown,
            match=r"returned no usable receipt;.*do not retry automatically",
        ):
            GoogleConnectorAdapter().execute(
                _operation("gmail", "messages.send"),
                {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}},
                continuation=None,
                credential=_credential(),
                transport=transport,
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
                write_idempotency_key="confirmed-raw-upload",
            )
        assert [call["kind"] for call in transport.calls] == ["request", "stream"]
    finally:
        upload.close()


def test_gmail_empty_success_receipt_is_outcome_unknown_and_not_replayed(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(
        tmp_path,
        content=b"To: recipient@example.test\r\nSubject: One shot\r\n\r\nBody",
    )
    transport = _Transport(body=b"")
    try:
        with pytest.raises(
            ConnectorOutcomeUnknown,
            match=r"returned no usable receipt;.*do not retry automatically",
        ):
            GoogleConnectorAdapter().execute(
                _operation("gmail", "messages.send"),
                {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}},
                continuation=None,
                credential=_credential(),
                transport=transport,
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
                write_idempotency_key="confirmed-raw-upload",
            )
        assert [call["kind"] for call in transport.calls] == ["request", "stream"]
    finally:
        upload.close()


def test_gmail_identifierless_success_receipt_is_outcome_unknown_and_not_replayed(
    tmp_path: Path,
) -> None:
    upload = _prepared_upload(
        tmp_path,
        content=b"To: recipient@example.test\r\nSubject: One shot\r\n\r\nBody",
    )
    transport = _Transport(body=b"{}")
    try:
        with pytest.raises(
            ConnectorOutcomeUnknown,
            match=r"returned no usable receipt;.*do not retry automatically",
        ):
            GoogleConnectorAdapter().execute(
                _operation("gmail", "messages.send"),
                {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}},
                continuation=None,
                credential=_credential(),
                transport=transport,
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
                write_idempotency_key="confirmed-raw-upload",
            )
        assert [call["kind"] for call in transport.calls] == ["request", "stream"]
    finally:
        upload.close()


def test_gmail_ambiguous_raw_dispatch_is_never_replayed(tmp_path: Path) -> None:
    upload = _prepared_upload(
        tmp_path,
        content=b"To: recipient@example.test\r\nSubject: One shot\r\n\r\nBody",
    )
    transport = _DriveRecoveryTransport(
        stream_outcomes=(ConnectorOutcomeUnknown("ambiguous final dispatch"),)
    )
    try:
        with pytest.raises(ConnectorOutcomeUnknown, match="may already have been sent"):
            GoogleConnectorAdapter().execute(
                _operation("gmail", "messages.send"),
                {"local_file": {"grant_id": "grant", "relative_path": "message.eml"}},
                continuation=None,
                credential=_credential(),
                transport=transport,
                transfer=ConnectorTransferContext(uploads={("local_file",): upload}),
                write_idempotency_key="confirmed-raw-upload",
            )
        assert [call["kind"] for call in transport.calls] == ["request", "stream"]
        assert transport.stream_outcomes == []
    finally:
        upload.close()


@pytest.mark.parametrize(
    ("name", "expected_metadata"),
    (
        ("drafts.create", {"message": {"threadId": "provider-thread-7"}}),
        ("messages.send", {"threadId": "provider-thread-7"}),
    ),
)
def test_gmail_local_file_reply_preserves_verified_headers_and_thread_metadata(
    tmp_path: Path,
    name: str,
    expected_metadata: dict[str, object],
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
    final_body = b'{"id":"message"}' if name == "messages.send" else b"{}"
    transport = _Transport(body=final_body, bodies=(metadata,))
    try:
        GoogleConnectorAdapter().execute(
            _operation("gmail", name),
            {
                "attachments": [{"filename": "reply.txt", "mime_type": "text/plain"}],
                "reply_to_message_id": "gmail-resource-42",
                "text_body": "Reply body",
                "thread_id": "provider-thread-7",
                "to": ["recipient@example.test"],
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            transfer=transfer,
        )
        preflight, initiated, sent = transport.calls
        assert preflight["path"] == "/gmail/v1/users/me/messages/gmail-resource-42"
        assert initiated["json_body"] == expected_metadata
        message = BytesParser(policy=policy.default).parsebytes(b"".join(sent["source"]))
        assert message["In-Reply-To"] == "<original@example.test>"
        assert message["References"] == "<root@example.test> <original@example.test>"
        assert message["Subject"] == "Planning"
    finally:
        upload.close()


def test_calendar_effect_escalates_attendees_or_non_primary_event_changes() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.create")
    safe = {"calendar_id": "primary", "end": _event_end_time(), "start": _event_time()}
    assert adapter.classify_effect(operation, safe) is ConnectorEffect.SAFE_MUTATION
    assert (
        adapter.classify_effect(
            operation,
            {
                **safe,
                "attendee_emails": ["guest@example.test"],
                "send_updates": "all",
            },
        )
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(
            operation,
            {
                **safe,
                "attendees": [{"email": "guest@example.test"}],
                "send_updates": "none",
            },
        )
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(operation, {**safe, "send_updates": "none"})
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(operation, {**safe, "calendar_id": "team"})
        is ConnectorEffect.OUTWARD
    )
    with pytest.raises(ValidationError, match="unknown field"):
        adapter.classify_effect(
            operation,
            {**safe, "drive_attachments": [{"file_id": "file"}]},
        )


def test_calendar_validation_fails_before_confirmation_or_provider_dispatch() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    update = _operation("google_calendar", "events.update")
    with pytest.raises(ValidationError, match="at least one changed field"):
        adapter.classify_effect(
            update,
            {"calendar_id": "primary", "etag": "etag", "event_id": "event"},
            credential=_credential(),
            transport=transport,
        )

    create = _operation("google_calendar", "events.create")
    invalid_cases = (
        {
            "attendee_emails": ["guest@example.test", "GUEST@example.test"],
            "calendar_id": "primary",
            "end": _event_end_time(),
            "send_updates": "all",
            "start": _event_time(),
        },
        {
            "calendar_id": "primary",
            "end": _event_time(),
            "start": _event_time(),
        },
        {
            "calendar_id": "primary",
            "end": {"date": "2026-08-02"},
            "start": _event_time(),
        },
        {
            "calendar_id": "primary",
            "end": _event_end_time(),
            "recurrence": ["DTSTART:20260801T090000Z"],
            "start": _event_time(),
        },
        {
            "calendar_id": "primary",
            "end": _event_end_time(),
            "start": {"date_time": "2026-08-01T09:00+02:00"},
        },
        {
            "calendar_id": "primary",
            "end": {"date": "2026-08-03"},
            "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"],
            "start": {"date": "2026-08-01"},
        },
    )
    for invalid in invalid_cases:
        with pytest.raises(ValidationError):
            adapter.classify_effect(create, invalid)

    with pytest.raises(ValidationError, match="header value is invalid"):
        adapter.execute(
            _operation("google_calendar", "calendars.update"),
            {"calendar_id": "primary", "etag": "bad\nheader", "summary": "Updated"},
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-calendar-update",
        )
    assert (
        adapter.classify_effect(
            create,
            {
                "calendar_id": "primary",
                "end": {"date": "2026-08-03", "time_zone": "Europe/Brussels"},
                "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"],
                "start": {"date": "2026-08-01", "time_zone": "Europe/Brussels"},
            },
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert transport.calls == []


def test_calendar_event_patch_distinguishes_local_outward_and_destructive_changes() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    shared = {
        "attendees": [
            {"email": "owner@example.test", "self": True},
            {"email": "guest@example.test"},
        ],
        "end": {"dateTime": "2026-08-01T10:00:00+02:00", "timeZone": "Europe/Brussels"},
        "etag": "etag",
        "eventType": "default",
        "organizer": {"email": "owner@example.test", "self": True},
        "start": {"dateTime": "2026-08-01T09:00:00+02:00", "timeZone": "Europe/Brussels"},
    }
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    private_team_event = {
        "attendees": [],
        "etag": "etag",
        "eventType": "default",
        "organizer": {"email": "team@example.test", "self": True},
    }

    assert (
        adapter.classify_effect(
            operation,
            {**common, "reminders": {"use_default": True}},
            credential=_credential(),
            transport=_Transport(body=json.dumps(shared).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "calendar_id": "team", "reminders": {"use_default": True}},
            credential=_credential(),
            transport=_Transport(body=json.dumps(private_team_event).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "calendar_id": "team", "summary": "Visible team change"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(private_team_event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(
            operation,
            {
                **common,
                "attendee_emails": ["owner@example.test"],
                "send_updates": "all",
            },
            credential=_credential(),
            transport=_Transport(body=json.dumps(shared).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )
    recurrence_without_zone = {
        **shared,
        "end": {"dateTime": "2026-08-01T10:00:00+02:00"},
        "start": {"dateTime": "2026-08-01T09:00:00+02:00"},
    }
    with pytest.raises(ValidationError, match="matching explicit time zone"):
        adapter.classify_effect(
            operation,
            {**common, "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]},
            credential=_credential(),
            transport=_Transport(body=json.dumps(recurrence_without_zone).encode()),
        )
    email_less_attendee = {
        "attendees": [{"self": True}],
        "etag": "etag",
        "eventType": "default",
        "organizer": {"email": "owner@example.test", "self": True},
    }
    assert (
        adapter.classify_effect(
            operation,
            {
                **common,
                "attendee_emails": ["owner@example.test"],
                "send_updates": "all",
            },
            credential=_credential(),
            transport=_Transport(body=json.dumps(email_less_attendee).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]},
            credential=_credential(),
            transport=_Transport(body=json.dumps(shared).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "recurrence": []},
            credential=_credential(),
            transport=_Transport(body=json.dumps(recurrence_without_zone).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )


def test_calendar_move_and_etag_preconditions_fail_closed_and_map_provider_races() -> None:
    adapter = GoogleConnectorAdapter()
    move = _operation("google_calendar", "events.move")
    common_move = {
        "calendar_id": "source",
        "destination_calendar_id": "destination",
        "etag": "etag",
        "event_id": "event",
        "expected_destination_calendar": _expected_calendar("destination"),
        "expected_event": _expected_event(),
        "send_updates": "all",
    }
    with pytest.raises(ValidationError, match="different destination"):
        adapter.classify_effect(
            move,
            {**common_move, "destination_calendar_id": "source"},
        )

    non_default = (
        b'{"attendees":[],"etag":"etag","eventType":"birthday","id":"event","status":"confirmed"}'
    )
    with pytest.raises(ValidationError, match="only default events"):
        adapter.classify_effect(
            move,
            {**common_move, "expected_event": _expected_event(event_type="birthday")},
            credential=_credential(),
            transport=_Transport(body=non_default),
        )

    changed = b'{"attendees":[],"etag":"new-etag","eventType":"default"}'
    with pytest.raises(ConflictError, match="read it again"):
        adapter.classify_effect(
            move,
            common_move,
            credential=_credential(),
            transport=_Transport(body=changed),
        )

    private = b'{"attendees":[],"etag":"etag","eventType":"default","organizer":{"self":true}}'
    raced = _Transport(bodies=(private,), provider_errors=(None, 412))
    with pytest.raises(ConnectorProviderError) as exc_info:
        adapter.execute(
            _operation("google_calendar", "events.update"),
            {
                "calendar_id": "primary",
                "etag": "etag",
                "event_id": "event",
                "summary": "Changed",
            },
            continuation=None,
            credential=_credential(),
            transport=raced,
        )
    assert exc_info.value.code == "resource_changed_reread_required"


def test_calendar_delete_rejects_primary_alias_and_actual_primary_id() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "calendars.delete")
    with pytest.raises(ValidationError, match="primary calendar"):
        adapter.classify_effect(
            operation,
            {
                "calendar_id": "primary",
                "etag": "etag",
                "expected_calendar": _expected_calendar("primary", primary=True),
            },
        )

    actual_primary = b'{"accessRole":"owner","id":"actual-id","primary":true,"summary":"Primary"}'
    with pytest.raises(ValidationError, match="primary calendar"):
        adapter.classify_effect(
            operation,
            {
                "calendar_id": "actual-id",
                "etag": "etag",
                "expected_calendar": _expected_calendar(
                    "actual-id", primary=True, summary="Primary"
                ),
            },
            credential=_credential(),
            transport=_Transport(body=actual_primary),
        )

    assert (
        adapter.classify_effect(
            _operation("google_calendar", "calendars.update"),
            {"calendar_id": "primary", "etag": "etag", "summary": "Renamed"},
        )
        is ConnectorEffect.OUTWARD
    )


def test_calendar_human_target_snapshots_are_rechecked_before_confirmation() -> None:
    adapter = GoogleConnectorAdapter()
    event = (
        b'{"attendees":[],"etag":"etag","eventType":"default","id":"event",'
        b'"status":"confirmed","summary":"Changed event"}'
    )
    move = {
        "calendar_id": "source",
        "destination_calendar_id": "destination",
        "etag": "etag",
        "event_id": "event",
        "expected_destination_calendar": _expected_calendar("destination"),
        "expected_event": _expected_event(),
        "send_updates": "all",
    }
    with pytest.raises(ValidationError, match="normalize the reviewed read"):
        adapter.classify_effect(
            _operation("google_calendar", "events.move"),
            move,
            credential=_credential(),
            transport=_Transport(body=event),
        )

    calendar = b'{"accessRole":"owner","id":"secondary","summary":"Changed calendar"}'
    with pytest.raises(ConflictError, match="Calendar changed"):
        adapter.classify_effect(
            _operation("google_calendar", "calendars.delete"),
            {
                "calendar_id": "secondary",
                "etag": "etag",
                "expected_calendar": _expected_calendar("secondary"),
            },
            credential=_credential(),
            transport=_Transport(body=calendar),
        )

    missing_summary = b'{"accessRole":"owner","id":"secondary"}'
    assert (
        adapter.classify_effect(
            _operation("google_calendar", "calendars.delete"),
            {
                "calendar_id": "secondary",
                "etag": "etag",
                "expected_calendar": _expected_calendar("secondary", summary=None),
            },
            credential=_credential(),
            transport=_Transport(body=missing_summary),
        )
        is ConnectorEffect.PERMANENT
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
            "etag": "event-version-1",
            "eventType": "default",
            "organizer": {"email": "owner@example.test", "self": True},
        }
    ).encode()
    shared = json.dumps(
        {
            "attendees": [
                {"email": "owner@example.test", "self": True},
                {"email": "guest@example.test"},
            ],
            "etag": "event-version-1",
            "eventType": "default",
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
    preflight_fields = dict(private_transport.calls[0]["query"])["fields"]
    assert all(field in preflight_fields for field in ("attendees", "etag", "organizer"))
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
    with pytest.raises(ValidationError, match="fresh confirmation"):
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
    with pytest.raises(ValidationError, match="fresh confirmation"):
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
            "etag": "event-version-1",
            "eventType": "default",
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
            "eventType": "fromGmail",
            "id": "event-1",
            "status": "confirmed",
        }
    ).encode()
    transport = _Transport(bodies=(event, b"{}"))
    GoogleConnectorAdapter().execute(
        _operation("google_calendar", "events.respond"),
        {
            "calendar_id": "primary",
            "comment": "See you there",
            "etag": '"event-version-4"',
            "event_id": "event-1",
            "expected_event": _expected_event(
                etag='"event-version-4"',
                event_id="event-1",
                event_type="fromGmail",
            ),
            "response_status": "tentative",
            "send_updates": "all",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-rsvp",
    )

    preflight, response = transport.calls
    assert preflight["method"] is ConnectorMethod.GET
    assert preflight["path"] == "/calendar/v3/calendars/primary/events/event-1"
    assert preflight["query"] == (
        (
            "fields",
            "attendees(comment,email,self),"
            "focusTimeProperties(autoDeclineMode,chatStatus,declineMessage),"
            "outOfOfficeProperties(autoDeclineMode,declineMessage),"
            "workingLocationProperties(customLocation(label),homeOffice,"
            "officeLocation(buildingId,deskId,floorId,floorSectionId,label),type),"
            "end(date,dateTime,timeZone),etag,eventType,id,"
            "organizer(displayName,email,self),start(date,dateTime,timeZone),status,summary",
        ),
    )
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


def test_calendar_rsvp_preserves_existing_comment_when_no_replacement_is_supplied() -> None:
    event = json.dumps(
        {
            "attendees": [
                {
                    "comment": "Earlier note",
                    "email": "owner@example.test",
                    "self": True,
                }
            ],
            "etag": '"event-version-4"',
            "eventType": "default",
            "id": "event-1",
            "status": "confirmed",
        }
    ).encode()
    transport = _Transport(bodies=(event, b"{}"))
    GoogleConnectorAdapter().execute(
        _operation("google_calendar", "events.respond"),
        {
            "calendar_id": "primary",
            "etag": '"event-version-4"',
            "event_id": "event-1",
            "expected_event": _expected_event(etag='"event-version-4"', event_id="event-1"),
            "response_status": "accepted",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-rsvp",
    )

    assert transport.calls[-1]["json_body"] == {
        "attendees": [
            {
                "comment": "Earlier note",
                "email": "owner@example.test",
                "responseStatus": "accepted",
            }
        ],
        "attendeesOmitted": True,
    }


def test_calendar_rsvp_fails_closed_without_one_self_attendee() -> None:
    event = json.dumps(
        {
            "attendees": [{"email": "guest@example.test"}],
            "etag": '"event-version-4"',
            "eventType": "default",
            "id": "event-1",
            "status": "confirmed",
        }
    ).encode()
    transport = _Transport(body=event)
    with pytest.raises(ValidationError, match="one self attendee"):
        GoogleConnectorAdapter().execute(
            _operation("google_calendar", "events.respond"),
            {
                "calendar_id": "primary",
                "etag": '"event-version-4"',
                "event_id": "event-1",
                "expected_event": _expected_event(etag='"event-version-4"', event_id="event-1"),
                "response_status": "accepted",
            },
            continuation=None,
            credential=_credential(),
            transport=transport,
            write_idempotency_key="confirmed-rsvp",
        )
    assert len(transport.calls) == 1


def test_calendar_and_drive_shape_fixed_bodies_headers_and_resumable_uploads() -> None:
    adapter = GoogleConnectorAdapter()
    event = json.dumps(
        {
            "attendees": [{"email": "guest@example.test"}],
            "etag": "calendar-etag",
            "eventType": "default",
            "organizer": {"email": "owner@example.test", "self": True},
        }
    ).encode()
    transport = _Transport(bodies=(event, b"{}"))
    calendar = _operation("google_calendar", "events.update")
    adapter.execute(
        calendar,
        {
            "attendees": [
                {
                    "display_name": "Guest",
                    "email": "guest@example.test",
                    "optional": True,
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
            "send_updates": "all",
            "summary": "Updated",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-attendee-update",
    )
    calendar_call = transport.calls[-1]
    assert calendar_call["headers"] == {"If-Match": "calendar-etag"}
    assert calendar_call["json_body"] == {
        "attendees": [
            {
                "displayName": "Guest",
                "email": "guest@example.test",
                "optional": True,
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


def test_calendar_colors_use_the_fixed_existing_read_route() -> None:
    colors = {
        "calendar": {"1": {"background": "#ac725e", "foreground": "#1d1d1d"}},
        "event": {"7": {"background": "#46d6db", "foreground": "#1d1d1d"}},
        "updated": "2026-08-02T00:00:00Z",
    }
    transport = _Transport(body=json.dumps(colors).encode())
    result = GoogleConnectorAdapter().execute(
        _operation("google_calendar", "colors.get"),
        {},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )

    assert result.payload == colors
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] is ConnectorMethod.GET
    assert transport.calls[0]["path"] == "/calendar/v3/colors"
    assert transport.calls[0]["query"] == ()
    with pytest.raises(ValidationError, match="does not accept a continuation"):
        GoogleConnectorAdapter().execute(
            _operation("google_calendar", "colors.get"),
            {},
            continuation="page",
            credential=_credential(),
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_calendar_rich_event_create_uses_one_calendar_call_and_returns_pending_meet() -> None:
    response = {
        "conferenceData": {
            "createRequest": {
                "requestId": "seld-meet-20260802-01",
                "status": {"statusCode": "pending"},
            }
        },
        "id": "event-1",
    }
    transport = _Transport(body=json.dumps(response).encode())
    result = GoogleConnectorAdapter().execute(
        _operation("google_calendar", "events.create"),
        {
            "attachments": [
                {"file_url": "https://files.example.test/brief.pdf"},
                {"file_url": "https://files.example.test/agenda.pdf"},
            ],
            "attendees": [
                {
                    "additional_guests": 2,
                    "display_name": "Guest",
                    "email": "guest@example.test",
                    "optional": False,
                }
            ],
            "calendar_id": "primary",
            "color_id": "7",
            "end": _event_end_time(),
            "meet_request_id": "seld-meet-20260802-01",
            "private_extended_properties": [{"key": "source", "value": "seld"}],
            "send_updates": "none",
            "shared_extended_properties": [{"key": "team", "value": "platform"}],
            "start": _event_time(),
            "transparency": "transparent",
        },
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-rich-event",
    )

    assert result.payload == response
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["path"] == "/calendar/v3/calendars/primary/events"
    assert call["query"] == (
        ("sendUpdates", "none"),
        ("conferenceDataVersion", "1"),
        ("supportsAttachments", "true"),
    )
    assert call["json_body"] == {
        "attachments": [
            {"fileUrl": "https://files.example.test/brief.pdf"},
            {"fileUrl": "https://files.example.test/agenda.pdf"},
        ],
        "attendees": [
            {
                "additionalGuests": 2,
                "displayName": "Guest",
                "email": "guest@example.test",
                "optional": False,
            }
        ],
        "colorId": "7",
        "conferenceData": {
            "createRequest": {
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
                "requestId": "seld-meet-20260802-01",
            }
        },
        "end": {"dateTime": "2026-08-01T10:00:00+02:00", "timeZone": "Europe/Brussels"},
        "extendedProperties": {
            "private": {"source": "seld"},
            "shared": {"team": "platform"},
        },
        "start": {"dateTime": "2026-08-01T09:00:00+02:00", "timeZone": "Europe/Brussels"},
        "transparency": "transparent",
    }
    assert not any(call["path"].startswith("/drive/") for call in transport.calls)


def test_calendar_attachment_urls_are_http_or_https_unique_and_never_dereferenced() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.create")
    common = {"calendar_id": "primary", "end": _event_end_time(), "start": _event_time()}
    for file_url in (
        "ftp://files.example.test/brief.pdf",
        "javascript:alert(1)",
        "https://person:secret@files.example.test/brief.pdf",
        "https://files.example.test/brief pdf",
        "relative/brief.pdf",
    ):
        transport = _Transport()
        with pytest.raises(ValidationError, match="absolute HTTP or HTTPS"):
            GoogleConnectorAdapter().execute(
                operation,
                {**common, "attachments": [{"file_url": file_url}]},
                continuation=None,
                credential=_credential(),
                transport=transport,
            )
        assert transport.calls == []

    assert (
        adapter.classify_effect(
            operation,
            {**common, "attachments": [{"file_url": "http://files.example.test/brief.pdf"}]},
        )
        is ConnectorEffect.SAFE_MUTATION
    )

    with pytest.raises(ValidationError, match="must be unique"):
        GoogleConnectorAdapter().classify_effect(
            operation,
            {
                **common,
                "attachments": [
                    {"file_url": "https://files.example.test/brief.pdf"},
                    {"file_url": "https://files.example.test/brief.pdf"},
                ],
            },
        )


def test_calendar_extended_property_filters_disable_sync_cursors_on_both_paths() -> None:
    operation = _operation("google_calendar", "events.list")
    values = {
        "calendar_id": "primary",
        "private_extended_properties": [
            {"key": "source", "value": "seld"},
            {"key": "source", "value": "manual"},
        ],
        "shared_extended_properties": [{"key": "team", "value": "platform"}],
        "show_hidden_invitations": True,
    }
    transport = _Transport(body=b'{"items":[],"nextSyncToken":"not-replayable"}')
    result = GoogleConnectorAdapter().execute(
        operation,
        values,
        continuation=None,
        credential=_credential(),
        transport=transport,
    )

    assert result.continuation is None
    assert transport.calls[0]["query"] == (
        ("showHiddenInvitations", "true"),
        ("privateExtendedProperty", "source=seld"),
        ("privateExtendedProperty", "source=manual"),
        ("sharedExtendedProperty", "team=platform"),
    )

    malformed = _Transport()
    with pytest.raises(ValidationError, match="cannot contain equals signs"):
        GoogleConnectorAdapter().execute(
            operation,
            {
                "calendar_id": "primary",
                "private_extended_properties": [{"key": "source=kind", "value": "seld"}],
            },
            continuation=None,
            credential=_credential(),
            transport=malformed,
        )
    assert malformed.calls == []

    for field in ("private_extended_properties", "shared_extended_properties"):
        blocked = _Transport()
        with pytest.raises(ValidationError, match="cannot combine event list filters"):
            GoogleConnectorAdapter().execute(
                operation,
                {
                    "calendar_id": "primary",
                    field: [{"key": "source", "value": "seld"}],
                },
                continuation={"syncToken": "existing-sync"},
                credential=_credential(),
                transport=blocked,
            )
        assert blocked.calls == []


def test_calendar_show_hidden_invitations_remains_sync_replayable() -> None:
    operation = _operation("google_calendar", "events.list")
    values = {"calendar_id": "primary", "show_hidden_invitations": True}
    first = GoogleConnectorAdapter().execute(
        operation,
        values,
        continuation=None,
        credential=_credential(),
        transport=_Transport(body=b'{"items":[],"nextSyncToken":"sync-1"}'),
    )
    assert first.continuation == {"syncToken": "sync-1"}

    replay = _Transport()
    GoogleConnectorAdapter().execute(
        operation,
        values,
        continuation=first.continuation,
        credential=_credential(),
        transport=replay,
    )
    assert replay.calls[0]["query"] == (
        ("showHiddenInvitations", "true"),
        ("syncToken", "sync-1"),
    )


def test_calendar_rich_update_effects_fail_closed_for_removed_or_replaced_content() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    private = {
        "attendees": [],
        "etag": "etag",
        "eventType": "default",
        "organizer": {"email": "owner@example.test", "self": True},
    }
    shared = {
        **private,
        "attendees": [{"email": "guest@example.test"}],
    }

    assert (
        adapter.classify_effect(
            operation,
            {**common, "color_id": "7"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(private).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "color_id": "7"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(shared).encode()),
        )
        is ConnectorEffect.OUTWARD
    )

    attachment_event = {
        **private,
        "attachments": [{"fileUrl": "https://files.example.test/old.pdf"}],
    }
    assert (
        adapter.classify_effect(
            operation,
            {
                **common,
                "attachments": [{"file_url": "https://files.example.test/new.pdf"}],
            },
            credential=_credential(),
            transport=_Transport(body=json.dumps(attachment_event).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        adapter.classify_effect(
            operation,
            {
                **common,
                "private_extended_properties": [{"key": "source", "value": None}],
            },
            credential=_credential(),
            transport=_Transport(body=json.dumps(private).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )

    conference_event = {
        **private,
        "conferenceData": {"conferenceId": "existing-conference"},
    }
    assert (
        adapter.classify_effect(
            operation,
            {**common, "meet_request_id": "replacement-request"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(conference_event).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )
    hangout_event = {**private, "hangoutLink": "https://meet.google.com/existing"}
    assert (
        adapter.classify_effect(
            operation,
            {**common, "meet_request_id": "replacement-request"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(hangout_event).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "meet_request_id": "first-meet-request"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(private).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )

    guests = {
        **private,
        "attendees": [{"additionalGuests": 2, "email": "guest@example.test"}],
    }
    assert (
        adapter.classify_effect(
            operation,
            {
                **common,
                "attendees": [{"additional_guests": 1, "email": "guest@example.test"}],
                "send_updates": "none",
            },
            credential=_credential(),
            transport=_Transport(body=json.dumps(guests).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )


def test_calendar_rich_update_preflight_reads_only_fields_needed_for_the_effect() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    event = {
        "attendees": [],
        "etag": "etag",
        "eventType": "default",
        "organizer": {"email": "owner@example.test", "self": True},
    }
    cases = (
        (
            {**common, "attachments": []},
            ("attachments(fileUrl)",),
            ("additionalGuests", "conferenceData", "hangoutLink"),
        ),
        (
            {**common, "meet_request_id": "new-meet"},
            ("conferenceData", "hangoutLink"),
            ("additionalGuests", "attachments(fileUrl)"),
        ),
        (
            {
                **common,
                "attendees": [{"additional_guests": 1, "email": "guest@example.test"}],
                "send_updates": "none",
            },
            ("additionalGuests",),
            ("attachments(fileUrl)", "conferenceData", "hangoutLink"),
        ),
    )

    for values, present, absent in cases:
        transport = _Transport(body=json.dumps(event).encode())
        adapter.classify_effect(
            operation,
            values,
            credential=_credential(),
            transport=transport,
        )
        fields = dict(transport.calls[0]["query"])["fields"]
        assert all(value in fields for value in present)
        assert all(value not in fields for value in absent)


def test_calendar_extended_properties_enforce_combined_provider_bounds() -> None:
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    with pytest.raises(ValidationError, match="at most 300"):
        GoogleConnectorAdapter().classify_effect(
            operation,
            {
                **common,
                "private_extended_properties": [
                    {"key": f"private-{index}", "value": "x"} for index in range(150)
                ],
                "shared_extended_properties": [
                    {"key": f"shared-{index}", "value": "x"} for index in range(151)
                ],
            },
        )
    with pytest.raises(ValidationError, match="32 KiB"):
        GoogleConnectorAdapter().classify_effect(
            operation,
            {
                **common,
                "private_extended_properties": [
                    {"key": f"key-{index}", "value": "x" * 1024} for index in range(33)
                ],
            },
        )
    with pytest.raises(ValidationError, match="duplicate properties"):
        GoogleConnectorAdapter().classify_effect(
            operation,
            {
                **common,
                "private_extended_properties": [
                    {"key": "source", "value": "seld"},
                    {"key": "source", "value": "other"},
                ],
            },
        )


def test_calendar_status_creates_shape_fixed_provider_bodies_and_effects() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.create")
    timed = {"calendar_id": "primary", "end": _event_end_time(), "start": _event_time()}

    focus = {
        **timed,
        "event_type": "focusTime",
        "focus_time_properties": {},
    }
    assert adapter.classify_effect(operation, focus) is ConnectorEffect.SAFE_MUTATION
    focus_transport = _Transport()
    adapter.execute(
        operation,
        focus,
        continuation=None,
        credential=_credential(),
        transport=focus_transport,
    )
    assert focus_transport.calls[0]["json_body"] == {
        "end": {"dateTime": "2026-08-01T10:00:00+02:00", "timeZone": "Europe/Brussels"},
        "eventType": "focusTime",
        "focusTimeProperties": {
            "autoDeclineMode": "declineNone",
            "chatStatus": "available",
        },
        "start": {"dateTime": "2026-08-01T09:00:00+02:00", "timeZone": "Europe/Brussels"},
        "transparency": "opaque",
    }

    out_of_office = {
        **timed,
        "event_type": "outOfOffice",
        "out_of_office_properties": {
            "auto_decline_mode": "declineAllConflictingInvitations",
            "decline_message": "I am away",
        },
    }
    assert adapter.classify_effect(operation, out_of_office) is ConnectorEffect.OUTWARD
    out_transport = _Transport()
    adapter.execute(
        operation,
        out_of_office,
        continuation=None,
        credential=_credential(),
        transport=out_transport,
        write_idempotency_key="confirmed-out-of-office",
    )
    assert out_transport.calls[0]["json_body"] == {
        "end": {"dateTime": "2026-08-01T10:00:00+02:00", "timeZone": "Europe/Brussels"},
        "eventType": "outOfOffice",
        "outOfOfficeProperties": {
            "autoDeclineMode": "declineAllConflictingInvitations",
            "declineMessage": "I am away",
        },
        "start": {"dateTime": "2026-08-01T09:00:00+02:00", "timeZone": "Europe/Brussels"},
        "transparency": "opaque",
    }

    quiet_out_of_office = {
        **timed,
        "event_type": "outOfOffice",
        "out_of_office_properties": {},
    }
    assert adapter.classify_effect(operation, quiet_out_of_office) is ConnectorEffect.SAFE_MUTATION
    quiet_out_transport = _Transport()
    adapter.execute(
        operation,
        quiet_out_of_office,
        continuation=None,
        credential=_credential(),
        transport=quiet_out_transport,
    )
    assert quiet_out_transport.calls[0]["json_body"]["outOfOfficeProperties"] == {
        "autoDeclineMode": "declineNone"
    }

    decline_message_without_declines = {
        **timed,
        "event_type": "outOfOffice",
        "out_of_office_properties": {
            "auto_decline_mode": "declineNone",
            "decline_message": "No automatic replies",
        },
    }
    assert (
        adapter.classify_effect(operation, decline_message_without_declines)
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(operation, {**focus, "visibility": "public"})
        is ConnectorEffect.OUTWARD
    )

    working_location = {
        "calendar_id": "primary",
        "end": {"date": "2026-08-03"},
        "event_type": "workingLocation",
        "start": {"date": "2026-08-02"},
        "working_location_properties": {
            "custom_location": {"label": "Client site"},
            "type": "customLocation",
        },
    }
    assert adapter.classify_effect(operation, working_location) is ConnectorEffect.OUTWARD
    location_transport = _Transport()
    adapter.execute(
        operation,
        working_location,
        continuation=None,
        credential=_credential(),
        transport=location_transport,
        write_idempotency_key="confirmed-working-location",
    )
    assert location_transport.calls[0]["json_body"] == {
        "end": {"date": "2026-08-03"},
        "eventType": "workingLocation",
        "start": {"date": "2026-08-02"},
        "transparency": "transparent",
        "visibility": "public",
        "workingLocationProperties": {
            "customLocation": {"label": "Client site"},
            "type": "customLocation",
        },
    }


def test_calendar_birthday_creates_shape_fixed_provider_bodies_and_effects() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.create")
    birthday = {
        "calendar_id": "primary",
        "color_id": "7",
        "end": {"date": "2026-08-03"},
        "event_type": "birthday",
        "reminders": {
            "overrides": [{"delivery": "popup", "minutes": 60}],
            "use_default": False,
        },
        "start": {"date": "2026-08-02"},
        "summary": "Birthday",
    }
    assert adapter.classify_effect(operation, birthday) is ConnectorEffect.SAFE_MUTATION

    transport = _Transport()
    adapter.execute(
        operation,
        birthday,
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert transport.calls[0]["json_body"] == {
        "colorId": "7",
        "end": {"date": "2026-08-03"},
        "eventType": "birthday",
        "recurrence": ["RRULE:FREQ=YEARLY"],
        "reminders": {
            "overrides": [{"method": "popup", "minutes": 60}],
            "useDefault": False,
        },
        "start": {"date": "2026-08-02"},
        "summary": "Birthday",
        "transparency": "transparent",
        "visibility": "private",
    }
    assert "birthdayProperties" not in transport.calls[0]["json_body"]

    leap_day = {
        "calendar_id": "primary",
        "end": {"date": "2024-03-01"},
        "event_type": "birthday",
        "start": {"date": "2024-02-29"},
    }
    leap_transport = _Transport()
    adapter.execute(
        operation,
        leap_day,
        continuation=None,
        credential=_credential(),
        transport=leap_transport,
    )
    assert leap_transport.calls[0]["json_body"]["recurrence"] == [
        "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1"
    ]
    assert (
        adapter.classify_effect(operation, {**birthday, "calendar_id": "shared"})
        is ConnectorEffect.OUTWARD
    )


def test_calendar_birthday_create_invariants_fail_before_transport() -> None:
    operation = _operation("google_calendar", "events.create")
    common = {"calendar_id": "primary", "event_type": "birthday"}
    invalid_values = (
        {
            **common,
            "end": {"date_time": "2026-08-02T10:00:00+02:00"},
            "start": {"date_time": "2026-08-02T09:00:00+02:00"},
        },
        {
            **common,
            "end": {"date": "2026-08-04"},
            "start": {"date": "2026-08-02"},
        },
        {
            **common,
            "description": "Provider does not accept this on birthdays",
            "end": {"date": "2026-08-03"},
            "start": {"date": "2026-08-02"},
        },
        {
            **common,
            "end": {"date": "2026-08-03"},
            "start": {"date": "2026-08-02", "time_zone": "Europe/Brussels"},
        },
    )
    for values in invalid_values:
        transport = _Transport()
        with pytest.raises(ValidationError):
            GoogleConnectorAdapter().execute(
                operation,
                values,
                continuation=None,
                credential=_credential(),
                transport=transport,
            )
        assert transport.calls == []


def test_calendar_birthday_updates_use_live_provider_contract() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    event = {
        "attendees": [],
        "birthdayProperties": {"type": "birthday"},
        "end": {"date": "2026-08-03"},
        "etag": "etag",
        "eventType": "birthday",
        "organizer": {"email": "owner@example.test", "self": True},
        "start": {"date": "2026-08-02"},
    }

    summary = {**common, "summary": "Updated birthday"}
    assert (
        adapter.classify_effect(
            operation,
            summary,
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {**summary, "calendar_id": "shared"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )

    linked_event = {
        **event,
        "birthdayProperties": {
            "contact": "people/contact",
            "type": "birthday",
        },
    }
    linked_transport = _Transport(bodies=(json.dumps(linked_event).encode(), b"{}"))
    adapter.execute(
        operation,
        summary,
        continuation=None,
        credential=_credential(),
        transport=linked_transport,
    )
    assert len(linked_transport.calls) == 2
    assert linked_transport.calls[1]["json_body"] == {"summary": "Updated birthday"}

    move = {
        **common,
        "end": {"date": "2026-08-04"},
        "start": {"date": "2026-08-03"},
    }
    transport = _Transport(bodies=(json.dumps(event).encode(), b"{}"))
    adapter.execute(
        operation,
        move,
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    assert len(transport.calls) == 2
    assert "birthdayProperties(contact,type)" in dict(transport.calls[0]["query"])["fields"]
    assert transport.calls[1]["method"] is ConnectorMethod.PATCH
    assert transport.calls[1]["headers"] == {"If-Match": "etag"}
    assert transport.calls[1]["json_body"] == {
        "end": {"date": "2026-08-04"},
        "start": {"date": "2026-08-03"},
    }


@pytest.mark.parametrize(
    ("birthday_properties", "message"),
    (
        ({"contact": "people/contact", "type": "birthday"}, "Google Contacts"),
        ({"type": "self"}, "Google Account profile"),
    ),
)
def test_calendar_birthday_linked_date_updates_fail_before_patch(
    birthday_properties: dict[str, str], message: str
) -> None:
    operation = _operation("google_calendar", "events.update")
    values = {
        "calendar_id": "primary",
        "end": {"date": "2026-08-04"},
        "etag": "etag",
        "event_id": "event",
        "start": {"date": "2026-08-03"},
    }
    event = {
        "attendees": [],
        "birthdayProperties": birthday_properties,
        "end": {"date": "2026-08-03"},
        "etag": "etag",
        "eventType": "birthday",
        "organizer": {"email": "owner@example.test", "self": True},
        "start": {"date": "2026-08-02"},
    }
    transport = _Transport(body=json.dumps(event).encode())
    with pytest.raises(ValidationError, match=message):
        GoogleConnectorAdapter().classify_effect(
            operation,
            values,
            credential=_credential(),
            transport=transport,
        )
    assert len(transport.calls) == 1


def test_calendar_birthday_update_invariants_fail_before_patch() -> None:
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    event = {
        "attendees": [],
        "birthdayProperties": {"type": "birthday"},
        "end": {"date": "2026-08-03"},
        "etag": "etag",
        "eventType": "birthday",
        "organizer": {"email": "owner@example.test", "self": True},
        "start": {"date": "2026-08-02"},
    }
    invalid_values = (
        {**common, "description": "Unsupported"},
        {**common, "end": {"date": "2026-08-04"}},
        {**common, "start": {"date_time": "2026-08-03T09:00:00+02:00"}},
    )
    for values in invalid_values:
        transport = _Transport(body=json.dumps(event).encode())
        with pytest.raises(ValidationError):
            GoogleConnectorAdapter().classify_effect(
                operation,
                values,
                credential=_credential(),
                transport=transport,
            )
        assert len(transport.calls) == 1


def test_calendar_from_gmail_updates_use_live_type_and_copy_aware_effects() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    event = {
        "attendees": [{"additionalGuests": 0, "email": "guest@example.test"}],
        "etag": "etag",
        "eventType": "fromGmail",
        "organizer": {"email": "owner@example.test", "self": True},
        "status": "confirmed",
    }
    copy_private = {
        **common,
        "color_id": "7",
        "private_extended_properties": [{"key": "source", "value": "seld"}],
        "reminders": {"use_default": True},
    }
    assert (
        adapter.classify_effect(
            operation,
            copy_private,
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {**copy_private, "calendar_id": "delegated@example.test"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )

    outward_values = (
        {**common, "visibility": "private"},
        {**common, "transparency": "transparent"},
        {**common, "status": "tentative"},
        {
            **common,
            "shared_extended_properties": [{"key": "trip", "value": "confirmed"}],
        },
        {**common, "color_id": "7", "send_updates": "all"},
    )
    for values in outward_values:
        assert (
            adapter.classify_effect(
                operation,
                values,
                credential=_credential(),
                transport=_Transport(body=json.dumps(event).encode()),
            )
            is ConnectorEffect.OUTWARD
        )

    deletion = {
        **common,
        "private_extended_properties": [{"key": "source", "value": None}],
    }
    assert (
        adapter.classify_effect(
            operation,
            deletion,
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.DESTRUCTIVE
    )

    local_transport = _Transport(bodies=(json.dumps(event).encode(), b"{}"))
    adapter.execute(
        operation,
        copy_private,
        continuation=None,
        credential=_credential(),
        transport=local_transport,
    )
    assert local_transport.calls[1]["headers"] == {"If-Match": "etag"}
    assert local_transport.calls[1]["json_body"] == {
        "colorId": "7",
        "extendedProperties": {"private": {"source": "seld"}},
        "reminders": {"useDefault": True},
    }

    status_transport = _Transport(bodies=(json.dumps(event).encode(), b"{}"))
    adapter.execute(
        operation,
        {**common, "status": "tentative"},
        continuation=None,
        credential=_credential(),
        transport=status_transport,
        write_idempotency_key="confirmed-gmail-status",
    )
    assert status_transport.calls[1]["json_body"] == {"status": "tentative"}


def test_calendar_from_gmail_rejects_provider_owned_fields_before_patch() -> None:
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    event = {
        "attendees": [],
        "etag": "etag",
        "eventType": "fromGmail",
        "organizer": {"email": "owner@example.test", "self": True},
        "status": "confirmed",
    }
    invalid_values = (
        {**common, "summary": "Provider-owned title"},
        {**common, "start": _event_time()},
        {
            **common,
            "attendee_emails": ["traveller@example.test"],
            "send_updates": "none",
        },
        {
            **common,
            "attachments": [{"file_url": "https://files.example.test/brief.pdf"}],
        },
    )
    for values in invalid_values:
        transport = _Transport(body=json.dumps(event).encode())
        with pytest.raises(ValidationError, match="generated from Gmail"):
            GoogleConnectorAdapter().classify_effect(
                operation,
                values,
                credential=_credential(),
                transport=transport,
            )
        assert len(transport.calls) == 1


def test_calendar_from_gmail_attendee_replacement_is_lossless_and_complete() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    values = {
        "attendees": [
            {
                "additional_guests": 1,
                "comment": "Window seat requested",
                "display_name": "Traveller",
                "email": "traveller@example.test",
                "optional": False,
                "resource": False,
                "response_status": "accepted",
            }
        ],
        "calendar_id": "primary",
        "etag": "etag",
        "event_id": "event",
        "send_updates": "none",
    }
    event = {
        "attendees": [
            {
                "additionalGuests": 1,
                "comment": "Window seat requested",
                "displayName": "Traveller",
                "email": "traveller@example.test",
                "optional": False,
                "resource": False,
                "responseStatus": "accepted",
            }
        ],
        "attendeesOmitted": False,
        "etag": "etag",
        "eventType": "fromGmail",
        "organizer": {"email": "owner@example.test", "self": True},
        "status": "confirmed",
    }
    assert (
        adapter.classify_effect(
            operation,
            values,
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )

    transport = _Transport(bodies=(json.dumps(event).encode(), b"{}"))
    adapter.execute(
        operation,
        values,
        continuation=None,
        credential=_credential(),
        transport=transport,
        write_idempotency_key="confirmed-gmail-attendees",
    )
    fields = dict(transport.calls[0]["query"])["fields"]
    assert "attendeesOmitted" in fields
    assert "responseStatus" in fields
    assert transport.calls[1]["json_body"] == {
        "attendees": [
            {
                "additionalGuests": 1,
                "comment": "Window seat requested",
                "displayName": "Traveller",
                "email": "traveller@example.test",
                "optional": False,
                "resource": False,
                "responseStatus": "accepted",
            }
        ]
    }

    incomplete = _Transport(body=json.dumps({**event, "attendeesOmitted": True}).encode())
    with pytest.raises(ValidationError, match="complete attendee list"):
        adapter.classify_effect(
            operation,
            values,
            credential=_credential(),
            transport=incomplete,
        )
    assert len(incomplete.calls) == 1


def test_calendar_status_create_invariants_fail_before_transport() -> None:
    operation = _operation("google_calendar", "events.create")
    invalid_values = (
        {
            "calendar_id": "primary",
            "end": {"date": "2026-08-03"},
            "event_type": "focusTime",
            "focus_time_properties": {},
            "start": {"date": "2026-08-02"},
        },
        {
            "calendar_id": "primary",
            "end": {"date": "2026-08-04"},
            "event_type": "workingLocation",
            "start": {"date": "2026-08-02"},
            "working_location_properties": {"type": "homeOffice"},
        },
        {
            "calendar_id": "primary",
            "end": _event_end_time(),
            "event_type": "focusTime",
            "out_of_office_properties": {},
            "start": _event_time(),
        },
        {
            "calendar_id": "primary",
            "end": {"date_time": "2026-08-02T09:00:00+02:00"},
            "event_type": "focusTime",
            "focus_time_properties": {"chat_status": "doNotDisturb"},
            "start": {"date_time": "2026-08-01T09:00:00+02:00"},
        },
        {
            "calendar_id": "primary",
            "end": _event_end_time(),
            "event_type": "outOfOffice",
            "out_of_office_properties": {},
            "start": _event_time(),
            "transparency": "transparent",
        },
        {
            "calendar_id": "primary",
            "end": {"date": "2026-08-03"},
            "event_type": "workingLocation",
            "start": {"date": "2026-08-02"},
            "visibility": "private",
            "working_location_properties": {"type": "homeOffice"},
        },
    )
    for values in invalid_values:
        transport = _Transport()
        with pytest.raises(ValidationError):
            GoogleConnectorAdapter().execute(
                operation,
                values,
                continuation=None,
                credential=_credential(),
                transport=transport,
            )
        assert transport.calls == []


def test_calendar_focus_chat_duration_defers_naive_dst_math_to_google() -> None:
    values = {
        "calendar_id": "primary",
        "end": {
            "date_time": "2026-03-29T03:30:00",
            "time_zone": "Europe/Brussels",
        },
        "event_type": "focusTime",
        "focus_time_properties": {"chat_status": "doNotDisturb"},
        "start": {
            "date_time": "2026-03-28T03:00:00",
            "time_zone": "Europe/Brussels",
        },
    }
    assert (
        GoogleConnectorAdapter().classify_effect(
            _operation("google_calendar", "events.create"), values
        )
        is ConnectorEffect.OUTWARD
    )


def test_calendar_status_updates_use_live_type_and_effect_bearing_rules() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    focus_event = {
        "attendees": [],
        "end": {"dateTime": "2026-08-01T10:00:00+02:00"},
        "etag": "etag",
        "eventType": "focusTime",
        "focusTimeProperties": {
            "autoDeclineMode": "declineAllConflictingInvitations",
            "chatStatus": "doNotDisturb",
        },
        "organizer": {"email": "owner@example.test", "self": True},
        "start": {"dateTime": "2026-08-01T09:00:00+02:00"},
    }

    disable = {
        **common,
        "focus_time_properties": {"auto_decline_mode": "declineNone"},
    }
    assert (
        adapter.classify_effect(
            operation,
            disable,
            credential=_credential(),
            transport=_Transport(body=json.dumps(focus_event).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {
                **common,
                "focus_time_properties": {
                    "auto_decline_mode": "declineNone",
                    "decline_message": "No automatic replies",
                },
            },
            credential=_credential(),
            transport=_Transport(body=json.dumps(focus_event).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "visibility": "public"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(focus_event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )
    enable = {
        **common,
        "focus_time_properties": {
            "auto_decline_mode": "declineOnlyNewConflictingInvitations",
            "decline_message": "Protecting focus time",
        },
    }
    assert (
        adapter.classify_effect(
            operation,
            enable,
            credential=_credential(),
            transport=_Transport(body=json.dumps(focus_event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )
    unknown_decline_mode_event = {
        **focus_event,
        "focusTimeProperties": {"chatStatus": "available"},
    }
    assert (
        adapter.classify_effect(
            operation,
            {**common, "start": {"date_time": "2026-08-01T08:15:00+02:00"}},
            credential=_credential(),
            transport=_Transport(body=json.dumps(unknown_decline_mode_event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "start": {"date_time": "2026-08-01T08:00:00+02:00"}},
            credential=_credential(),
            transport=_Transport(body=json.dumps(focus_event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )
    assert (
        adapter.classify_effect(
            operation,
            {**common, "focus_time_properties": {"chat_status": "available"}},
            credential=_credential(),
            transport=_Transport(body=json.dumps(focus_event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )
    chat_only_event = {
        **focus_event,
        "focusTimeProperties": {
            "autoDeclineMode": "declineNone",
            "chatStatus": "doNotDisturb",
        },
    }
    assert (
        adapter.classify_effect(
            operation,
            {**common, "start": {"date_time": "2026-08-01T08:30:00+02:00"}},
            credential=_credential(),
            transport=_Transport(body=json.dumps(chat_only_event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )

    mismatch = _Transport(body=json.dumps(focus_event).encode())
    with pytest.raises(ValidationError, match="do not match"):
        adapter.classify_effect(
            operation,
            {**common, "working_location_properties": {"type": "homeOffice"}},
            credential=_credential(),
            transport=mismatch,
        )
    assert len(mismatch.calls) == 1

    empty_properties = _Transport(body=json.dumps(focus_event).encode())
    with pytest.raises(ValidationError, match="at least one changed field"):
        adapter.classify_effect(
            operation,
            {**common, "focus_time_properties": {}},
            credential=_credential(),
            transport=empty_properties,
        )
    assert empty_properties.calls == []

    too_long = _Transport(body=json.dumps(focus_event).encode())
    with pytest.raises(ValidationError, match="under 24 hours"):
        adapter.classify_effect(
            operation,
            {
                **common,
                "end": {"date_time": "2026-08-02T09:00:00+02:00"},
                "start": {"date_time": "2026-08-01T09:00:00+02:00"},
            },
            credential=_credential(),
            transport=too_long,
        )
    assert len(too_long.calls) == 1

    backwards = _Transport(body=json.dumps(focus_event).encode())
    with pytest.raises(ValidationError, match="end must follow"):
        adapter.classify_effect(
            operation,
            {**common, "start": {"date_time": "2026-08-01T11:00:00+02:00"}},
            credential=_credential(),
            transport=backwards,
        )
    assert len(backwards.calls) == 1

    execution = _Transport(bodies=(json.dumps(focus_event).encode(), b"{}"))
    adapter.execute(
        operation,
        enable,
        continuation=None,
        credential=_credential(),
        transport=execution,
        write_idempotency_key="confirmed-focus-rule",
    )
    assert len(execution.calls) == 2
    assert execution.calls[0]["query"][0][0] == "fields"
    fields = execution.calls[0]["query"][0][1]
    assert "focusTimeProperties(autoDeclineMode,chatStatus,declineMessage)" in fields
    assert execution.calls[1]["json_body"] == {
        "focusTimeProperties": {
            "autoDeclineMode": "declineOnlyNewConflictingInvitations",
            "declineMessage": "Protecting focus time",
        }
    }


def test_calendar_working_location_updates_keep_local_preferences_one_step() -> None:
    adapter = GoogleConnectorAdapter()
    operation = _operation("google_calendar", "events.update")
    common = {"calendar_id": "primary", "etag": "etag", "event_id": "event"}
    event = {
        "attendees": [],
        "end": {"date": "2026-08-03"},
        "etag": "etag",
        "eventType": "workingLocation",
        "organizer": {"email": "owner@example.test", "self": True},
        "start": {"date": "2026-08-02"},
        "workingLocationProperties": {"type": "homeOffice"},
    }
    assert (
        adapter.classify_effect(
            operation,
            {**common, "color_id": "7"},
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(
            operation,
            {
                **common,
                "working_location_properties": {
                    "custom_location": {"label": "Client site"},
                    "type": "customLocation",
                },
            },
            credential=_credential(),
            transport=_Transport(body=json.dumps(event).encode()),
        )
        is ConnectorEffect.OUTWARD
    )

    invalid = _Transport(body=json.dumps(event).encode())
    with pytest.raises(ValidationError, match="must remain public"):
        adapter.classify_effect(
            operation,
            {**common, "visibility": "private"},
            credential=_credential(),
            transport=invalid,
        )
    assert len(invalid.calls) == 1


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
