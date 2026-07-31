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
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError


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
        sink.write(self.download_body)
        artifact = sink.finish()
        return ConnectorStreamResponse(
            kwargs["origin"],
            200,
            self.headers,
            len(self.download_body),
            hashlib.sha256(self.download_body).hexdigest(),
            artifact=artifact,
        )


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


def _prepared_upload(tmp_path: Path) -> PreparedUpload:
    content = b"streamed Google Drive content"
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


def _sample(operation: OperationSpec) -> dict[str, object]:
    name = operation.name
    if operation.provider == "gmail":
        if name == "labels.list":
            return {}
        if name.endswith(".list"):
            return {"page_size": 1}
        if name == "attachments.get":
            return {"attachment_id": "attachment", "message_id": "message"}
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
        if name in {"labels.update", "labels.delete"}:
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
        if name == "files.download":
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
            return {"etag": "etag", "file_id": "file"}
        if name == "files.copy":
            return {"file_id": "file"}
        if name == "files.move":
            return {"file_id": "file"}
        if name in {"files.trash", "files.restore", "files.purge"}:
            return {"etag": "etag", "file_id": "file"}
        if name == "permissions.create":
            return {"file_id": "file", "permission_type": "user", "role": "reader"}
        if name == "permissions.update":
            return {
                "etag": "etag",
                "file_id": "file",
                "permission_id": "permission",
                "role": "reader",
            }
        if name == "permissions.delete":
            return {"etag": "etag", "file_id": "file", "permission_id": "permission"}
        if name == "comments.create":
            return {"content": "note", "file_id": "file"}
        if name == "comments.update":
            return {"comment_id": "comment", "content": "note", "etag": "etag", "file_id": "file"}
        if name == "comments.delete":
            return {"comment_id": "comment", "etag": "etag", "file_id": "file"}
        if name == "replies.create":
            return {"comment_id": "comment", "content": "note", "file_id": "file"}
        if name == "replies.update":
            return {
                "comment_id": "comment",
                "content": "note",
                "etag": "etag",
                "file_id": "file",
                "reply_id": "reply",
            }
        if name == "replies.delete":
            return {"comment_id": "comment", "etag": "etag", "file_id": "file", "reply_id": "reply"}
        if name == "revisions.keep":
            return {"file_id": "file", "keep_forever": True, "revision_id": "revision"}
        if name == "revisions.delete":
            return {"etag": "etag", "file_id": "file", "revision_id": "revision"}
    raise AssertionError(f"no sample for {operation.provider}:{name}")


def test_every_google_operation_uses_only_bounded_fixed_adapter_requests() -> None:
    adapter = GoogleConnectorAdapter()
    transport = _Transport()
    credential = _credential()
    for operation in GOOGLE_OPERATIONS:
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
        adapter.execute(
            operation,
            _sample(operation),
            continuation=None,
            credential=credential,
            transport=transport,
        )
        assert len(transport.calls) == before + expected_requests
    assert len(transport.calls) == len(GOOGLE_OPERATIONS) + 3
    assert {call["origin"] for call in transport.calls} == {
        ConnectorOrigin.GMAIL,
        ConnectorOrigin.GOOGLE,
    }
    assert all(call["path"].startswith("/") for call in transport.calls)
    assert all("url" not in call and "token" not in call for call in transport.calls)


def test_pagination_is_stripped_from_payload_and_replayed_only_as_runtime_continuation() -> None:
    body = json.dumps(
        {
            "items": [{"id": "one"}],
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
    assert first.payload == {"items": [{"id": "one"}]}
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
    )
    initiated, sent = transport.calls[-2:]
    assert initiated["path"] == "/upload/drive/v3/files"
    assert initiated["query"] == (("uploadType", "resumable"),)
    assert sent["kind"] == "provider_location"
    assert sent["location"] == "https://www.googleapis.com/upload/session/one"
    assert sent["body"] == b"hello"


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
            {"etag": "etag", "file_id": "file", "mime_type": "text/plain"},
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
                transfer=transfer,
            )
        finally:
            upload.close()

        initiated, sent = transport.calls
        assert result.payload == {}
        assert initiated["method"] is method
        assert initiated["path"] == path
        assert initiated["query"] == (("uploadType", "resumable"),)
        assert sent["kind"] == "stream"
        assert sent["source"] is upload
        assert sent["location"] == "https://www.googleapis.com/upload/session/one"
        assert sent["credential"] is None
        assert sent["content_length"] == upload.size
        assert sent["content_type"] == "application/octet-stream"
        assert "body" not in sent
        assert "headers" not in sent
        assert "relative_path" not in repr(sent)


def test_drive_artifact_download_uses_streaming_receipt_without_path_payload(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    scope = ConnectorArtifactScope(store)
    transfer = ConnectorTransferContext(_artifact_scope_factory=lambda: scope)
    transport = _Transport(download_body=b"provider-bytes")
    result = GoogleConnectorAdapter().execute(
        _operation("google_drive", "files.download"),
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


def test_drive_transfer_delivery_requires_a_transfer_context() -> None:
    adapter = GoogleConnectorAdapter()
    with pytest.raises(ValidationError, match="transfer context"):
        adapter.execute(
            _operation("google_drive", "files.download"),
            {"file_id": "file"},
            continuation=None,
            credential=_credential(),
            transport=_Transport(),
        )
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
        {"etag": "version-one", "file_id": "file", "name": "renamed.txt"},
        continuation=None,
        credential=_credential(),
        transport=transport,
    )
    call = transport.calls[-1]
    assert call["method"] is ConnectorMethod.PATCH
    assert call["path"] == "/drive/v3/files/file"
    assert call["headers"] == {"If-Match": "version-one"}
    assert call["json_body"] == {"name": "renamed.txt"}


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
        ("files.download", {"file_id": "file"}, "/drive/v3/files/file"),
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
            _operation("google_drive", "files.download"),
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
