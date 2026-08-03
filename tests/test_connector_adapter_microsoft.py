from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import cast
from urllib.parse import urlencode

import pytest

from continuity_kernel.connector_adapter import (
    ConnectorConfirmationTarget,
    ConnectorRuntimeCredential,
)
from continuity_kernel.connector_adapter_microsoft import MicrosoftConnectorAdapter
from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationSpec
from continuity_kernel.connector_operations_microsoft import MICROSOFT_OPERATIONS
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError

_MESSAGE_SUMMARY_SELECT = (
    "bodyPreview,categories,changeKey,conversationId,from,hasAttachments,id,importance,"
    "isDraft,isRead,lastModifiedDateTime,parentFolderId,receivedDateTime,sender,sentDateTime,"
    "subject,toRecipients,webLink"
)
_MESSAGE_DETAIL_SELECT = (
    "bccRecipients,body,bodyPreview,categories,ccRecipients,changeKey,conversationId,"
    "createdDateTime,flag,from,hasAttachments,id,importance,inferenceClassification,"
    "internetMessageId,isDeliveryReceiptRequested,isDraft,isRead,isReadReceiptRequested,"
    "lastModifiedDateTime,parentFolderId,receivedDateTime,replyTo,sender,sentDateTime,subject,"
    "toRecipients,uniqueBody,webLink"
)
_CONFIRMATION_MESSAGE_SELECT = (
    "id,changeKey,isDraft,parentFolderId,subject,toRecipients,ccRecipients,bccRecipients,"
    "replyTo,body,importance,hasAttachments,from,sender,isDeliveryReceiptRequested,"
    "isReadReceiptRequested,internetMessageHeaders"
)
_CONFIRMATION_ATTACHMENT_SELECT = "id,lastModifiedDateTime,name,contentType,size,isInline"
_CONFIRMATION_CALENDAR_SELECT = "id,name,owner,canEdit,isDefaultCalendar,isTallyingResponses"


class _FakeTransport:
    def __init__(
        self,
        response: bytes = b"{}",
        failure: Exception | None = None,
        *,
        calendar: dict[str, object] | None = None,
        fail_after: int = 0,
        event: dict[str, object] | None = None,
        message: dict[str, object] | None = None,
        subject_id: str = "user-1",
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response
        self._failure = failure
        self._fail_after = fail_after
        self._subject_id = subject_id
        self._calendar = calendar or {
            "id": "calendar-1",
            "isDefaultCalendar": False,
        }
        self._event = event or {
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-1",
            "id": "event-1",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        }
        self._message = message or {
            "id": "message-1",
            "parentFolderId": "original-folder",
        }

    def request(self, **kwargs: object) -> ConnectorResponse:
        self.calls.append(kwargs)
        if self._failure is not None and len(self.calls) > self._fail_after:
            raise self._failure
        path = kwargs.get("path")
        query = kwargs.get("query")
        response = self._response
        if path == "/v1.0/me" and query == (("$select", "id"),):
            response = json.dumps({"id": self._subject_id}).encode()
        elif query == (("$select", "id,isDefaultCalendar"),):
            response = json.dumps(self._calendar).encode()
        elif query == (("$select", "id,parentFolderId"),):
            response = json.dumps(self._message).encode()
        elif query == (("$select", "id"),) and isinstance(path, str) and "/events/" in path:
            response = json.dumps({"id": self._event["id"]}).encode()
        elif (
            isinstance(query, tuple)
            and query
            and query[0][0] == "$select"
            and "attendees" in str(query[0][1])
        ):
            response = json.dumps(self._event).encode()
        return ConnectorResponse(
            origin=ConnectorOrigin.MICROSOFT_GRAPH,
            status=200,
            headers=MappingProxyType({}),
            body=response,
        )


class _DraftConfirmationTransport:
    def __init__(
        self,
        message: dict[str, object],
        attachment_pages: list[dict[str, object]],
    ) -> None:
        self.message = message
        self.attachment_pages = list(attachment_pages)
        self.calls: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> ConnectorResponse:
        self.calls.append(kwargs)
        path = kwargs.get("path")
        query = kwargs.get("query")
        if path == "/v1.0/me/messages/message-1" and query == (
            ("$select", _CONFIRMATION_MESSAGE_SELECT),
        ):
            payload = self.message
        elif path == "/v1.0/me/messages/message-1/attachments":
            if (
                not isinstance(query, tuple)
                or not query
                or query[0]
                != (
                    "$select",
                    _CONFIRMATION_ATTACHMENT_SELECT,
                )
            ):
                raise AssertionError("confirmation attachment query is not fixed")
            if not self.attachment_pages:
                raise AssertionError("confirmation attachment page was not scripted")
            payload = self.attachment_pages.pop(0)
        else:
            raise AssertionError(f"unexpected confirmation request: {path!r} {query!r}")
        return ConnectorResponse(
            origin=ConnectorOrigin.MICROSOFT_GRAPH,
            status=200,
            headers=MappingProxyType({}),
            body=json.dumps(payload).encode(),
        )


class _CalendarConfirmationTransport:
    def __init__(self, calendar: object) -> None:
        self.calendar = calendar
        self.calls: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> ConnectorResponse:
        self.calls.append(kwargs)
        return ConnectorResponse(
            origin=ConnectorOrigin.MICROSOFT_GRAPH,
            status=200,
            headers=MappingProxyType({}),
            body=json.dumps(self.calendar).encode(),
        )


def _credential() -> ConnectorRuntimeCredential:
    return ConnectorRuntimeCredential(
        credential=ConnectorCredential(AuthorizationScheme.BEARER, "test-secret"),
        granted_scopes=("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"),
        version=1,
    )


def _draft() -> dict[str, object]:
    return {
        "body": {"content": "Hello", "content_type": "html"},
        "subject": "Hello",
        "to_recipients": [{"email": "ada@example.test", "name": "Ada"}],
    }


def _attachment() -> dict[str, object]:
    return {
        "content_base64": "aGVsbG8=",
        "content_type": "text/plain",
        "name": "hello.txt",
    }


def _confirmation_message(
    *,
    has_attachments: bool = True,
    subject: str = "Quarterly plan",
) -> dict[str, object]:
    return {
        "id": "message-1",
        "changeKey": "draft-version-1",
        "isDraft": True,
        "parentFolderId": "drafts-folder",
        "subject": subject,
        "toRecipients": [
            {"emailAddress": {"address": "Ada@Example.test", "name": "Ada"}},
            {"emailAddress": {"address": "bob@example.test", "name": "Bob"}},
        ],
        "ccRecipients": [{"emailAddress": {"address": "carol@example.test", "name": "Carol"}}],
        "bccRecipients": [{"emailAddress": {"address": "dave@example.test", "name": "Dave"}}],
        "replyTo": [{"emailAddress": {"address": "Replies@Example.test", "name": "Replies"}}],
        "body": {"contentType": "html", "content": "<p>Private draft body</p>"},
        "importance": "normal",
        "hasAttachments": has_attachments,
        "isDeliveryReceiptRequested": True,
        "isReadReceiptRequested": False,
        "internetMessageHeaders": [
            {"name": "X-Workflow", "value": "private-routing-value"},
        ],
        "from": {"emailAddress": {"address": "Alice@Example.test", "name": "Alice Sender"}},
        "sender": {"emailAddress": {"address": "delegate@example.test", "name": "Delegate"}},
    }


def _confirmation_attachment(
    attachment_id: str,
    name: str,
    *,
    inline: bool,
    size: int,
    kind: str = "file",
) -> dict[str, object]:
    return {
        "@odata.type": f"#microsoft.graph.{kind}Attachment",
        "id": attachment_id,
        "lastModifiedDateTime": "2026-08-03T05:00:00Z",
        "name": name,
        "contentType": "image/png" if inline else "application/pdf",
        "size": size,
        "isInline": inline,
        "contentId": f"cid-{attachment_id}" if inline else None,
        "contentLocation": f"inline/{name}" if inline else None,
    }


def _confirmation_calendar(
    calendar_id: str = "resolved-primary-calendar",
    *,
    default: bool = False,
) -> dict[str, object]:
    return {
        "id": calendar_id,
        "name": "Work Calendar",
        "owner": {"address": "owner@example.test", "name": "Calendar Owner"},
        "canEdit": True,
        "isDefaultCalendar": default,
        "isTallyingResponses": True,
    }


def _event(calendar_id: str = "primary", *, attendees: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "body": {"content": "Planning", "content_type": "text"},
        "calendar_id": calendar_id,
        "end": {"date_time": "2026-08-01T10:00:00", "time_zone": "Europe/Brussels"},
        "start": {"date_time": "2026-08-01T09:00:00", "time_zone": "Europe/Brussels"},
        "subject": "Planning",
        "transaction_id": "client-transaction-1",
    }
    if attendees:
        value["attendees"] = [{"email": "ada@example.test", "type": "required"}]
    return value


def _input_for(operation: OperationSpec) -> dict[str, object]:
    if operation.provider == "outlook_mail":
        values: dict[str, dict[str, object]] = {
            "folders.list": {},
            "folders.get": {"folder_id": "folder-1"},
            "messages.list": {"folder_id": "folder-1"},
            "messages.get": {"message_id": "message-1"},
            "messages.update": {"is_read": True, "message_id": "message-1"},
            "messages.mime": {"message_id": "message-1"},
            "attachments.list": {"message_id": "message-1"},
            "attachments.get": {"message_id": "message-1", "attachment_id": "attachment-1"},
            "folders.create": {"display_name": "Projects"},
            "folders.update": {"folder_id": "folder-1", "display_name": "Projects"},
            "folders.move": {"folder_id": "folder-1", "destination_folder_id": "folder-2"},
            "folders.trash": {"folder_id": "folder-1"},
            "folders.restore": {"folder_id": "folder-1"},
            "folders.purge": {"folder_id": "folder-1"},
            "drafts.create": _draft(),
            "drafts.update": {"message_id": "message-1", "subject": "Changed"},
            "drafts.reply": {"message_id": "message-1", "comment": "Thanks"},
            "drafts.reply_all": {"message_id": "message-1", "comment": "Thanks all"},
            "drafts.forward": {
                "message_id": "message-1",
                "to_recipients": [{"email": "ada@example.test"}],
            },
            "attachments.add": {"message_id": "message-1", "attachment": _attachment()},
            "attachments.delete": {"message_id": "message-1", "attachment_id": "attachment-1"},
            "drafts.send": {"message_id": "message-1"},
            "messages.copy": {"message_id": "message-1", "destination_folder_id": "folder-2"},
            "messages.move": {"message_id": "message-1", "destination_folder_id": "folder-2"},
            "messages.trash": {"message_id": "message-1"},
            "messages.restore": {
                "message_id": "message-1",
                "restore_handle": "rst-" + "A" * 43,
            },
            "messages.purge": {"message_id": "message-1"},
        }
        return values[operation.name]
    values = {
        "calendars.list": {},
        "calendars.get": {"calendar_id": "calendar-1"},
        "events.list": {"calendar_id": "primary"},
        "events.get": {"calendar_id": "primary", "event_id": "event-1"},
        "events.window": {
            "calendar_id": "primary",
            "start": "2026-08-01T09:00:00",
            "end": "2026-08-01T10:00:00",
        },
        "events.instances": {
            "calendar_id": "primary",
            "event_id": "event-1",
            "start": "2026-08-01T09:00:00",
            "end": "2026-08-01T10:00:00",
        },
        "freebusy.query": {
            "attendees": ["ada@example.test"],
            "start": "2026-08-01T09:00:00",
            "end": "2026-08-01T10:00:00",
        },
        "attachments.list": {"calendar_id": "primary", "event_id": "event-1"},
        "attachments.get": {
            "calendar_id": "primary",
            "event_id": "event-1",
            "attachment_id": "attachment-1",
        },
        "calendars.create": {"name": "Team"},
        "calendars.update": {"calendar_id": "calendar-1", "name": "Team"},
        "calendars.delete": {"calendar_id": "calendar-1"},
        "calendars.purge": {"calendar_id": "calendar-1"},
        "events.create": _event(),
        "events.update": {
            "calendar_id": "primary",
            "change_key": "event-version-1",
            "event_id": "event-1",
            "subject": "Changed",
        },
        "events.delete": {
            "calendar_id": "primary",
            "change_key": "event-version-1",
            "event_id": "event-1",
        },
        "events.cancel": {"calendar_id": "primary", "event_id": "event-1"},
        "events.accept": {"calendar_id": "primary", "event_id": "event-1"},
        "events.tentative": {"calendar_id": "primary", "event_id": "event-1"},
        "events.decline": {"calendar_id": "primary", "event_id": "event-1"},
        "events.forward": {
            "calendar_id": "primary",
            "event_id": "event-1",
            "recipients": [{"email": "ada@example.test"}],
        },
        "attachments.add": {
            "calendar_id": "primary",
            "change_key": "event-version-1",
            "event_id": "event-1",
            "attachment": _attachment(),
        },
        "attachments.delete": {
            "calendar_id": "primary",
            "event_id": "event-1",
            "attachment_id": "attachment-1",
        },
        "events.purge": {
            "calendar_id": "primary",
            "change_key": "event-version-1",
            "event_id": "event-1",
        },
    }
    return values[operation.name]


_ME = "/v1.0/me"
_MAIL_FOLDER = f"{_ME}/mailFolders/folder-1"
_MESSAGE = f"{_ME}/messages/message-1"
_MAIL_ATTACHMENT = f"{_MESSAGE}/attachments"
_CALENDAR = f"{_ME}/calendars/calendar-1"
_PRIMARY_CALENDAR = f"{_ME}/calendar"
_EVENT = f"{_PRIMARY_CALENDAR}/events/event-1"
_EVENT_ATTACHMENT = f"{_EVENT}/attachments"
_EXPECTED_REQUESTS = {
    "outlook_mail": {
        "folders.list": (ConnectorMethod.GET, f"{_ME}/mailFolders"),
        "folders.get": (ConnectorMethod.GET, _MAIL_FOLDER),
        "messages.list": (ConnectorMethod.GET, f"{_MAIL_FOLDER}/messages"),
        "messages.get": (ConnectorMethod.GET, _MESSAGE),
        "messages.mime": (ConnectorMethod.GET, f"{_MESSAGE}/$value"),
        "attachments.list": (ConnectorMethod.GET, _MAIL_ATTACHMENT),
        "attachments.get": (ConnectorMethod.GET, f"{_MAIL_ATTACHMENT}/attachment-1"),
        "folders.create": (ConnectorMethod.POST, f"{_ME}/mailFolders"),
        "folders.update": (ConnectorMethod.PATCH, _MAIL_FOLDER),
        "folders.move": (ConnectorMethod.POST, f"{_MAIL_FOLDER}/move"),
        "folders.trash": (ConnectorMethod.DELETE, _MAIL_FOLDER),
        "folders.restore": (ConnectorMethod.POST, f"{_MAIL_FOLDER}/move"),
        "folders.purge": (
            ConnectorMethod.POST,
            "/v1.0/users/user-1/mailFolders/folder-1/permanentDelete",
        ),
        "drafts.create": (ConnectorMethod.POST, f"{_ME}/messages"),
        "drafts.update": (ConnectorMethod.PATCH, _MESSAGE),
        "drafts.reply": (ConnectorMethod.POST, f"{_MESSAGE}/createReply"),
        "drafts.reply_all": (ConnectorMethod.POST, f"{_MESSAGE}/createReplyAll"),
        "drafts.forward": (ConnectorMethod.POST, f"{_MESSAGE}/createForward"),
        "attachments.add": (ConnectorMethod.POST, _MAIL_ATTACHMENT),
        "attachments.delete": (ConnectorMethod.DELETE, f"{_MAIL_ATTACHMENT}/attachment-1"),
        "drafts.send": (ConnectorMethod.POST, f"{_MESSAGE}/send"),
        "messages.update": (ConnectorMethod.PATCH, _MESSAGE),
        "messages.copy": (ConnectorMethod.POST, f"{_MESSAGE}/copy"),
        "messages.move": (ConnectorMethod.POST, f"{_MESSAGE}/move"),
        "messages.trash": (ConnectorMethod.DELETE, _MESSAGE),
        "messages.restore": (ConnectorMethod.POST, f"{_MESSAGE}/move"),
        "messages.purge": (
            ConnectorMethod.POST,
            "/v1.0/users/user-1/messages/message-1/permanentDelete",
        ),
    },
    "outlook_calendar": {
        "calendars.list": (ConnectorMethod.GET, f"{_ME}/calendars"),
        "calendars.get": (ConnectorMethod.GET, _CALENDAR),
        "events.list": (ConnectorMethod.GET, f"{_PRIMARY_CALENDAR}/events"),
        "events.get": (ConnectorMethod.GET, _EVENT),
        "events.window": (ConnectorMethod.GET, f"{_PRIMARY_CALENDAR}/calendarView"),
        "events.instances": (ConnectorMethod.GET, f"{_EVENT}/instances"),
        "freebusy.query": (ConnectorMethod.POST, f"{_PRIMARY_CALENDAR}/getSchedule"),
        "attachments.list": (ConnectorMethod.GET, _EVENT_ATTACHMENT),
        "attachments.get": (ConnectorMethod.GET, f"{_EVENT_ATTACHMENT}/attachment-1"),
        "calendars.create": (ConnectorMethod.POST, f"{_ME}/calendars"),
        "calendars.update": (ConnectorMethod.PATCH, _CALENDAR),
        "calendars.delete": (ConnectorMethod.DELETE, _CALENDAR),
        "calendars.purge": (
            ConnectorMethod.POST,
            "/v1.0/users/user-1/calendars/calendar-1/permanentDelete",
        ),
        "events.create": (ConnectorMethod.POST, f"{_PRIMARY_CALENDAR}/events"),
        "events.update": (ConnectorMethod.PATCH, _EVENT),
        "events.delete": (ConnectorMethod.DELETE, _EVENT),
        "events.cancel": (ConnectorMethod.POST, f"{_EVENT}/cancel"),
        "events.accept": (ConnectorMethod.POST, f"{_EVENT}/accept"),
        "events.tentative": (ConnectorMethod.POST, f"{_EVENT}/tentativelyAccept"),
        "events.decline": (ConnectorMethod.POST, f"{_EVENT}/decline"),
        "events.forward": (ConnectorMethod.POST, f"{_EVENT}/forward"),
        "attachments.add": (ConnectorMethod.POST, _EVENT_ATTACHMENT),
        "attachments.delete": (ConnectorMethod.DELETE, f"{_EVENT_ATTACHMENT}/attachment-1"),
        "events.purge": (
            ConnectorMethod.POST,
            "/v1.0/users/user-1/events/event-1/permanentDelete",
        ),
    },
}


def _operation(provider: str, mode: ConnectorMode, name: str) -> OperationSpec:
    return next(
        operation
        for operation in MICROSOFT_OPERATIONS
        if operation.provider == provider and operation.mode is mode and operation.name == name
    )


def test_event_create_confirmation_target_binds_live_primary_calendar() -> None:
    calendar = _confirmation_calendar()
    transport = _CalendarConfirmationTransport(calendar)

    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_calendar", ConnectorMode.WRITE, "events.create"),
        _event(),
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert target == ConnectorConfirmationTarget(
        binding={
            "kind": "outlook_calendar.events.create",
            "requested_calendar_id": "primary",
            "resolved_calendar_id": "resolved-primary-calendar",
            "name": "Work Calendar",
            "owner": {"email": "owner@example.test", "name": "Calendar Owner"},
            "can_edit": True,
            "is_default_calendar": False,
            "is_tallying_responses": True,
        },
        preview={
            "calendar_name": "Work Calendar",
            "calendar_owner": {
                "email": "owner@example.test",
                "name": "Calendar Owner",
            },
            "can_edit": True,
            "is_default_calendar": False,
            "is_tallying_responses": True,
            "consequence": (
                "Outlook will send meeting invitations to the confirmed attendee list."
            ),
        },
    )
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] is ConnectorMethod.GET
    assert transport.calls[0]["path"] == _PRIMARY_CALENDAR
    assert transport.calls[0]["query"] == (("$select", _CONFIRMATION_CALENDAR_SELECT),)
    assert transport.calls[0]["headers"] == {"Prefer": 'IdType="ImmutableId"'}
    assert transport.calls[0]["response_bound"] == 64 * 1024
    assert "resolved-primary-calendar" not in json.dumps(target.preview)


def test_event_create_confirmation_target_binds_non_primary_calendar() -> None:
    transport = _CalendarConfirmationTransport(_confirmation_calendar("calendar-1", default=False))

    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_calendar", ConnectorMode.WRITE, "events.create"),
        _event("calendar-1", attendees=False),
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert target is not None
    binding = cast(dict[str, object], target.binding)
    preview = cast(dict[str, object], target.preview)
    assert binding["requested_calendar_id"] == "calendar-1"
    assert binding["resolved_calendar_id"] == "calendar-1"
    assert "secondary, shared, or delegated calendar" in str(preview["consequence"])
    assert transport.calls[0]["path"] == _CALENDAR


def test_event_create_confirmation_target_changes_with_live_calendar() -> None:
    before = _confirmation_calendar()
    after = {**before, "name": "Renamed Calendar"}
    first = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_calendar", ConnectorMode.WRITE, "events.create"),
        _event(),
        credential=_credential(),
        transport=cast(ConnectorTransport, _CalendarConfirmationTransport(before)),
    )
    second = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_calendar", ConnectorMode.WRITE, "events.create"),
        _event(),
        credential=_credential(),
        transport=cast(ConnectorTransport, _CalendarConfirmationTransport(after)),
    )

    assert first is not None
    assert second is not None
    assert first.binding != second.binding


@pytest.mark.parametrize(
    ("input_value", "calendar"),
    (
        (_event(), []),
        (_event(), {**_confirmation_calendar(), "canEdit": None}),
        (_event(), {**_confirmation_calendar(), "@odata.nextLink": "opaque"}),
        (
            _event("calendar-1", attendees=False),
            _confirmation_calendar("different-calendar", default=False),
        ),
    ),
)
def test_event_create_confirmation_target_rejects_invalid_live_calendar(
    input_value: dict[str, object],
    calendar: object,
) -> None:
    transport = _CalendarConfirmationTransport(calendar)

    with pytest.raises(ConnectorProviderError):
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_calendar", ConnectorMode.WRITE, "events.create"),
            input_value,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
    assert len(transport.calls) == 1


def test_event_create_confirmation_target_rejects_noneditable_calendar() -> None:
    calendar = {**_confirmation_calendar(), "canEdit": False}
    transport = _CalendarConfirmationTransport(calendar)

    with pytest.raises(ValidationError, match="calendar is not editable"):
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_calendar", ConnectorMode.WRITE, "events.create"),
            _event(),
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
    assert len(transport.calls) == 1


def test_safe_event_create_has_no_confirmation_target_provider_read() -> None:
    transport = _CalendarConfirmationTransport(_confirmation_calendar())

    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_calendar", ConnectorMode.WRITE, "events.create"),
        _event(attendees=False),
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert target is None
    assert transport.calls == []


def test_draft_send_confirmation_target_binds_complete_private_projection() -> None:
    message = _confirmation_message()
    inline = _confirmation_attachment("attachment-2", "logo.png", inline=True, size=512)
    document = _confirmation_attachment(
        "attachment-1",
        "plan.pdf",
        inline=False,
        size=2_048,
    )
    transport = _DraftConfirmationTransport(message, [{"value": [inline, document]}])
    adapter = MicrosoftConnectorAdapter()

    target = adapter.resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1", "change_key": "draft-version-1"},
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    body_bytes = b"<p>Private draft body</p>"
    body_binding = {
        "content_type": "html",
        "bytes": len(body_bytes),
        "sha256": "sha256:" + hashlib.sha256(body_bytes).hexdigest(),
    }
    binding = cast(dict[str, object], target.binding)
    assert binding["kind"] == "outlook_mail.drafts.send"
    assert binding["message_id"] == "message-1"
    assert binding["change_key"] == "draft-version-1"
    assert binding["parent_folder_id"] == "drafts-folder"
    assert binding["subject"] == "Quarterly plan"
    assert binding["body"] == body_binding
    assert binding["to"] == [
        {"email": "Ada@Example.test", "name": "Ada"},
        {"email": "bob@example.test", "name": "Bob"},
    ]
    assert binding["reply_to"] == [
        {"email": "Replies@Example.test", "name": "Replies"},
    ]
    assert binding["delivery_receipt_requested"] is True
    assert binding["read_receipt_requested"] is False
    header_value = b"private-routing-value"
    assert binding["internet_headers"] == [
        {
            "name": "x-workflow",
            "value_bytes": len(header_value),
            "value_sha256": "sha256:" + hashlib.sha256(header_value).hexdigest(),
        }
    ]
    bound_attachments = cast(list[dict[str, object]], binding["attachments"])
    assert [attachment["id"] for attachment in bound_attachments] == [
        "attachment-1",
        "attachment-2",
    ]
    assert bound_attachments[1]["content_id"] == "cid-attachment-2"
    assert bound_attachments[1]["content_location"] == "inline/logo.png"
    assert all(attachment["kind"] == "file" for attachment in bound_attachments)

    preview = cast(dict[str, object], target.preview)
    assert preview["subject"] == "Quarterly plan"
    assert preview["attachment_count"] == 2
    assert preview["body"] == body_binding
    assert preview["reply_to"] == binding["reply_to"]
    assert preview["delivery_receipt_requested"] is True
    assert preview["read_receipt_requested"] is False
    assert preview["internet_headers"] == binding["internet_headers"]
    assert preview["from"] == {"email": "Alice@Example.test", "name": "Alice Sender"}
    assert preview["sender"] == {"email": "delegate@example.test", "name": "Delegate"}
    public_attachments = cast(list[dict[str, object]], preview["attachments"])
    assert public_attachments == [
        {
            "content_type": "application/pdf",
            "is_inline": False,
            "kind": "file",
            "name": "plan.pdf",
            "size": 2_048,
        },
        {
            "content_type": "image/png",
            "is_inline": True,
            "kind": "file",
            "name": "logo.png",
            "size": 512,
        },
    ]
    assert "Private draft body" not in repr(target)
    public_text = repr(target.preview)
    for private_value in (
        "message-1",
        "draft-version-1",
        "drafts-folder",
        "attachment-1",
        "attachment-2",
        "private-routing-value",
        "test-secret",
    ):
        assert private_value not in public_text

    assert len(transport.calls) == 2
    assert all(call["method"] is ConnectorMethod.GET for call in transport.calls)
    assert transport.calls[0]["path"] == "/v1.0/me/messages/message-1"
    assert transport.calls[0]["query"] == (("$select", _CONFIRMATION_MESSAGE_SELECT),)
    assert transport.calls[1]["path"] == "/v1.0/me/messages/message-1/attachments"
    assert transport.calls[1]["query"] == (("$select", _CONFIRMATION_ATTACHMENT_SELECT),)
    for call in transport.calls:
        headers = cast(dict[str, str], call["headers"])
        assert headers["Prefer"].startswith('IdType="ImmutableId"')
        if call["path"] == "/v1.0/me/messages/message-1":
            assert 'outlook.body-content-type="html"' in headers["Prefer"]
            assert "outlook.allow-unsafe-html" in headers["Prefer"]
        assert "json_body" not in call


def test_draft_confirmation_always_enumerates_inline_only_attachments() -> None:
    message = _confirmation_message(has_attachments=False)
    inline = _confirmation_attachment("inline-1", "signature.png", inline=True, size=128)
    transport = _DraftConfirmationTransport(message, [{"value": [inline]}])

    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    binding = cast(dict[str, object], target.binding)
    assert binding["has_attachments"] is False
    assert len(cast(list[object], binding["attachments"])) == 1
    assert cast(dict[str, object], target.preview)["attachment_count"] == 1
    assert len(transport.calls) == 2


def test_draft_confirmation_is_deterministic_across_provider_collection_order() -> None:
    first_message = _confirmation_message()
    second_message = json.loads(json.dumps(first_message))
    assert isinstance(second_message, dict)
    for name in ("toRecipients", "ccRecipients", "bccRecipients"):
        recipients = second_message[name]
        assert isinstance(recipients, list)
        recipients.reverse()
    first_attachments = [
        _confirmation_attachment("attachment-2", "logo.png", inline=True, size=512),
        _confirmation_attachment("attachment-1", "plan.pdf", inline=False, size=2_048),
    ]
    second_attachments = list(reversed(first_attachments))
    operation = _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send")

    first = MicrosoftConnectorAdapter().resolve_confirmation_target(
        operation,
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(
            ConnectorTransport,
            _DraftConfirmationTransport(first_message, [{"value": first_attachments}]),
        ),
    )
    second = MicrosoftConnectorAdapter().resolve_confirmation_target(
        operation,
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(
            ConnectorTransport,
            _DraftConfirmationTransport(second_message, [{"value": second_attachments}]),
        ),
    )

    assert first == second


def test_draft_confirmation_preserves_case_distinct_and_internationalized_local_parts() -> None:
    message = _confirmation_message()
    message["toRecipients"] = [
        {"emailAddress": {"address": "User@example.test", "name": "Case upper"}},
        {"emailAddress": {"address": "user@example.test", "name": "Case lower"}},
        {"emailAddress": {"address": "Straße@example.test", "name": "Unicode"}},
        {"emailAddress": {"address": "strasse@example.test", "name": "ASCII"}},
    ]
    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(
            ConnectorTransport,
            _DraftConfirmationTransport(message, [{"value": []}]),
        ),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    recipients = cast(list[dict[str, str]], cast(dict[str, object], target.binding)["to"])
    assert {recipient["email"] for recipient in recipients} == {
        "User@example.test",
        "user@example.test",
        "Straße@example.test",
        "strasse@example.test",
    }


def test_draft_confirmation_enforces_provider_total_recipient_limit() -> None:
    message = _confirmation_message()
    message["toRecipients"] = [
        {"emailAddress": {"address": f"person-{index}@example.test"}} for index in range(999)
    ]
    message["ccRecipients"] = [{"emailAddress": {"address": "cc@example.test"}}]
    message["bccRecipients"] = [{"emailAddress": {"address": "bcc@example.test"}}]
    transport = _DraftConfirmationTransport(message, [{"value": []}])

    with pytest.raises(ConnectorProviderError) as caught:
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert caught.value.code == "confirmation_recipient_limit_exceeded"
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("replyTo", "not-a-list", "invalid_confirmation_recipients"),
        (
            "isDeliveryReceiptRequested",
            "yes",
            "invalid_confirmation_delivery_receipt_state",
        ),
        ("isReadReceiptRequested", [], "invalid_confirmation_read_receipt_state"),
        (
            "internetMessageHeaders",
            [{"name": "invalid header", "value": "value"}],
            "invalid_confirmation_internet_header",
        ),
    ),
)
def test_draft_confirmation_rejects_malformed_outward_routing_projection(
    field: str,
    value: object,
    code: str,
) -> None:
    message = _confirmation_message()
    message[field] = value
    transport = _DraftConfirmationTransport(message, [{"value": []}])

    with pytest.raises(ConnectorProviderError) as caught:
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert caught.value.code == code
    assert len(transport.calls) == 1


def test_draft_confirmation_maps_absent_nullable_outward_fields_to_empty_state() -> None:
    message = _confirmation_message()
    for field in (
        "subject",
        "replyTo",
        "isDeliveryReceiptRequested",
        "isReadReceiptRequested",
        "internetMessageHeaders",
    ):
        message.pop(field)
    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(
            ConnectorTransport,
            _DraftConfirmationTransport(message, [{"value": []}]),
        ),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    binding = cast(dict[str, object], target.binding)
    assert binding["subject"] == ""
    assert binding["reply_to"] == []
    assert binding["delivery_receipt_requested"] is False
    assert binding["read_receipt_requested"] is False
    assert binding["internet_headers"] == []
    preview = cast(dict[str, object], target.preview)
    assert preview["subject"] == "(empty subject)"


def test_draft_confirmation_displays_empty_embedded_item_name_explicitly() -> None:
    embedded = _confirmation_attachment(
        "attachment-1",
        "",
        inline=False,
        size=128,
        kind="item",
    )
    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(
            ConnectorTransport,
            _DraftConfirmationTransport(_confirmation_message(), [{"value": [embedded]}]),
        ),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    binding = cast(dict[str, object], target.binding)
    attachment = cast(list[dict[str, object]], binding["attachments"])[0]
    assert attachment["name"] == ""
    preview = cast(dict[str, object], target.preview)
    public_attachment = cast(list[dict[str, object]], preview["attachments"])[0]
    assert public_attachment["name"] == "(empty name)"
    assert public_attachment["kind"] == "item"


def test_draft_confirmation_binds_reference_attachment_kind_without_upload_cap() -> None:
    reference = _confirmation_attachment(
        "attachment-1",
        "linked-document",
        inline=False,
        size=200 * 1024**2,
        kind="reference",
    )
    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(
            ConnectorTransport,
            _DraftConfirmationTransport(_confirmation_message(), [{"value": [reference]}]),
        ),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    binding = cast(dict[str, object], target.binding)
    attachments = cast(list[dict[str, object]], binding["attachments"])
    assert attachments[0]["kind"] == "reference"
    assert attachments[0]["size"] == 200 * 1024**2
    preview = cast(dict[str, object], target.preview)
    public_attachments = cast(list[dict[str, object]], preview["attachments"])
    assert public_attachments[0]["kind"] == "reference"


def test_draft_confirmation_follows_only_valid_fixed_attachment_pages() -> None:
    next_link = "https://graph.microsoft.com/v1.0/me/messages/message-1/attachments?" + urlencode(
        (
            ("$select", _CONFIRMATION_ATTACHMENT_SELECT),
            ("$skiptoken", "opaque-page-2"),
        )
    )
    transport = _DraftConfirmationTransport(
        _confirmation_message(),
        [
            {
                "@odata.nextLink": next_link,
                "value": [_confirmation_attachment("attachment-2", "b.pdf", inline=False, size=2)],
            },
            {"value": [_confirmation_attachment("attachment-1", "a.pdf", inline=False, size=1)]},
        ],
    )

    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    attachments = cast(
        list[dict[str, object]], cast(dict[str, object], target.binding)["attachments"]
    )
    assert [attachment["id"] for attachment in attachments] == [
        "attachment-1",
        "attachment-2",
    ]
    assert transport.calls[2]["query"] == (
        ("$select", _CONFIRMATION_ATTACHMENT_SELECT),
        ("$skiptoken", "opaque-page-2"),
    )


@pytest.mark.parametrize(
    "next_link",
    (
        "https://evil.example/v1.0/me/messages/message-1/attachments?"
        + urlencode((("$select", _CONFIRMATION_ATTACHMENT_SELECT), ("$skiptoken", "opaque"))),
        "https://graph.microsoft.com/v1.0/me/messages/other/attachments?"
        + urlencode((("$select", _CONFIRMATION_ATTACHMENT_SELECT), ("$skiptoken", "opaque"))),
        "https://graph.microsoft.com/v1.0/me/messages/message-1/attachments?"
        + urlencode((("$skiptoken", "opaque"),)),
    ),
)
def test_draft_confirmation_rejects_hostile_or_partial_attachment_pages(
    next_link: str,
) -> None:
    transport = _DraftConfirmationTransport(
        _confirmation_message(),
        [{"@odata.nextLink": next_link, "value": []}],
    )

    with pytest.raises(ConnectorProviderError) as caught:
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert caught.value.code == "invalid_next_link"
    assert all(call["method"] is ConnectorMethod.GET for call in transport.calls)


def test_draft_confirmation_rejects_cyclic_attachment_pages() -> None:
    next_link = "https://graph.microsoft.com/v1.0/me/messages/message-1/attachments?" + urlencode(
        (("$select", _CONFIRMATION_ATTACHMENT_SELECT), ("$skiptoken", "same"))
    )
    transport = _DraftConfirmationTransport(
        _confirmation_message(),
        [
            {"@odata.nextLink": next_link, "value": []},
            {"@odata.nextLink": next_link, "value": []},
        ],
    )

    with pytest.raises(ConnectorProviderError) as caught:
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert caught.value.code == "cyclic_confirmation_attachment_page"
    assert len(transport.calls) == 3


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("id", "other-message", "draft_confirmation_identity_mismatch"),
        ("isDraft", False, "confirmation_target_is_not_a_draft"),
        ("isDraft", None, "invalid_confirmation_draft_state"),
    ),
)
def test_draft_confirmation_rejects_wrong_or_non_draft_message(
    field: str,
    value: object,
    code: str,
) -> None:
    message = _confirmation_message()
    message[field] = value
    transport = _DraftConfirmationTransport(message, [{"value": []}])

    with pytest.raises(ConnectorProviderError) as caught:
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert caught.value.code == code
    assert len(transport.calls) == 1


def test_draft_confirmation_rejects_stale_caller_change_key_before_attachment_read() -> None:
    transport = _DraftConfirmationTransport(_confirmation_message(), [{"value": []}])

    with pytest.raises(ValidationError, match="changed"):
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1", "change_key": "stale-version"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] is ConnectorMethod.GET


@pytest.mark.parametrize("case", ("recipient", "body", "subject"))
def test_draft_confirmation_rejects_malformed_message_projection(case: str) -> None:
    message = _confirmation_message()
    if case == "recipient":
        message["toRecipients"] = [{"emailAddress": {"address": "not-an-address"}}]
    elif case == "body":
        message["body"] = {"contentType": "html", "content": 42}
    else:
        message["subject"] = "unsafe\x00subject"
    transport = _DraftConfirmationTransport(message, [{"value": []}])

    with pytest.raises(ConnectorProviderError) as caught:
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert isinstance(caught.value.code, str)
    assert caught.value.code.startswith("invalid_confirmation_")
    assert all(call["method"] is ConnectorMethod.GET for call in transport.calls)


@pytest.mark.parametrize("case", ("duplicate", "content", "size", "kind"))
def test_draft_confirmation_rejects_ambiguous_or_malformed_attachment(case: str) -> None:
    attachment = _confirmation_attachment("attachment-1", "plan.pdf", inline=False, size=10)
    values = [attachment]
    if case == "duplicate":
        values.append(dict(attachment))
    elif case == "content":
        attachment["contentBytes"] = "secret-content"
    elif case == "size":
        attachment["size"] = -1
    else:
        attachment["@odata.type"] = "#microsoft.graph.unknownAttachment"
    transport = _DraftConfirmationTransport(_confirmation_message(), [{"value": values}])

    with pytest.raises(ConnectorProviderError):
        MicrosoftConnectorAdapter().resolve_confirmation_target(
            _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
            {"message_id": "message-1"},
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )

    assert all(call["method"] is ConnectorMethod.GET for call in transport.calls)


def test_draft_confirmation_displays_empty_subject_explicitly() -> None:
    transport = _DraftConfirmationTransport(
        _confirmation_message(subject=""),
        [{"value": []}],
    )

    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert isinstance(target, ConnectorConfirmationTarget)
    assert cast(dict[str, object], target.binding)["subject"] == ""
    assert cast(dict[str, object], target.preview)["subject"] == "(empty subject)"


def test_non_draft_send_confirmation_target_returns_none_without_provider_read() -> None:
    transport = _DraftConfirmationTransport(_confirmation_message(), [{"value": []}])

    target = MicrosoftConnectorAdapter().resolve_confirmation_target(
        _operation("outlook_mail", ConnectorMode.WRITE, "messages.trash"),
        {"message_id": "message-1"},
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert target is None
    assert transport.calls == []


def test_every_microsoft_operation_uses_its_fixed_final_graph_route() -> None:
    adapter = MicrosoftConnectorAdapter()
    catalog_keys = {(operation.provider, operation.name) for operation in MICROSOFT_OPERATIONS}
    expected_keys = {
        (provider, name) for provider, requests in _EXPECTED_REQUESTS.items() for name in requests
    }

    assert expected_keys == catalog_keys
    restore_handle: str | None = None
    for operation in MICROSOFT_OPERATIONS:
        transport = _FakeTransport()
        input_value = _input_for(operation)
        if operation.provider == "outlook_mail" and operation.name == "messages.restore":
            assert restore_handle is not None
            input_value = {"message_id": "message-1", "restore_handle": restore_handle}
        result = adapter.execute(
            operation,
            input_value,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
        if operation.provider == "outlook_mail" and operation.name == "messages.trash":
            assert isinstance(result.payload, dict)
            restore_handle = cast(str, result.payload["restore_handle"])
        call = transport.calls[-1]
        expected = _EXPECTED_REQUESTS[operation.provider][operation.name]
        assert (call["method"], call["path"]) == expected
        headers = cast(dict[str, str], call["headers"])
        assert headers["Prefer"].startswith('IdType="ImmutableId"')


def test_immutable_ids_and_continuations_are_internal_and_stripped_from_payload() -> None:
    next_link = (
        "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque&$top=10"
        f"&$select={_MESSAGE_SUMMARY_SELECT}"
    )
    response = json.dumps({"@odata.nextLink": next_link, "value": [{"id": "message-1"}]}).encode()
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport(response)
    operation = _operation("outlook_mail", ConnectorMode.READ, "messages.list")

    result = adapter.execute(
        operation,
        {},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert result.payload == {"value": [{"id": "message-1"}]}
    assert result.continuation == {
        "path": "/v1.0/me/messages",
        "query": [["$skiptoken", "opaque"], ["$top", "10"]],
    }
    adapter.execute(
        operation,
        {},
        continuation=result.continuation,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[0]["headers"] == {"Prefer": 'IdType="ImmutableId"'}
    assert transport.calls[1]["query"] == (
        ("$select", _MESSAGE_SUMMARY_SELECT),
        ("$skiptoken", "opaque"),
        ("$top", "10"),
    )

    numeric = _FakeTransport(
        json.dumps(
            {
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/messages?$skip=1"
                    f"&$select={_MESSAGE_SUMMARY_SELECT}"
                ),
                "value": [],
            }
        ).encode()
    )
    numeric_result = adapter.execute(
        operation,
        {},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, numeric),
    )
    assert numeric_result.continuation == {
        "path": "/v1.0/me/messages",
        "query": [["$skip", "1"]],
    }

    well_known_folder = _FakeTransport(
        json.dumps(
            {
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/mailFolders('inbox')/messages"
                    f"?$skip=1&$top=1&$select={_MESSAGE_SUMMARY_SELECT}"
                ),
                "value": [],
            }
        ).encode()
    )
    folder_result = adapter.execute(
        operation,
        {"folder_id": "inbox", "page_size": 1},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, well_known_folder),
    )
    assert folder_result.continuation == {
        "path": "/v1.0/me/mailFolders/inbox/messages",
        "query": [["$skip", "1"]],
    }

    custom_transport = _FakeTransport(
        json.dumps(
            {
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/messages?$skip=2&$select=id,subject"
                ),
                "value": [],
            }
        ).encode()
    )
    custom_input = {"fields": ["subject", "id"]}
    custom_result = adapter.execute(
        operation,
        custom_input,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, custom_transport),
    )
    assert custom_result.continuation == {
        "path": "/v1.0/me/messages",
        "query": [["$skip", "2"]],
    }
    adapter.execute(
        operation,
        custom_input,
        continuation=custom_result.continuation,
        credential=_credential(),
        transport=cast(ConnectorTransport, custom_transport),
    )
    assert custom_transport.calls[-1]["query"] == (
        ("$select", "id,subject"),
        ("$skip", "2"),
    )

    for invalid_progress in (
        "$skiptoken=",
        "$skip=0",
        "$skip=1&$skiptoken=opaque",
    ):
        invalid = _FakeTransport(
            json.dumps(
                {
                    "@odata.nextLink": (
                        "https://graph.microsoft.com/v1.0/me/messages?"
                        f"{invalid_progress}&$select={_MESSAGE_SUMMARY_SELECT}"
                    ),
                    "value": [],
                }
            ).encode()
        )
        with pytest.raises(ConnectorProviderError) as raised:
            adapter.execute(
                operation,
                {},
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, invalid),
            )
        assert raised.value.code == "invalid_next_link"

    for projection_query in (
        "",
        "&$select=id,subject",
        f"&$select={_MESSAGE_SUMMARY_SELECT}&$select={_MESSAGE_SUMMARY_SELECT}",
    ):
        forged = _FakeTransport(
            json.dumps(
                {
                    "@odata.nextLink": (
                        "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque"
                        + projection_query
                    ),
                    "value": [],
                }
            ).encode()
        )
        with pytest.raises(ConnectorProviderError) as raised:
            adapter.execute(
                operation,
                {},
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, forged),
            )
        assert raised.value.code == "invalid_next_link"


def test_message_continuation_binds_search_projection_and_page_limit() -> None:
    operation = _operation("outlook_mail", ConnectorMode.READ, "messages.list")
    input_value = {
        "page_size": 50,
        "search": 'subject:"Quarterly Financials"',
    }
    graph_search = '"subject:\\"Quarterly Financials\\""'
    next_query = [
        ("$skiptoken", "opaque-page-two"),
        ("$top", "50"),
        ("$search", graph_search),
        ("$select", _MESSAGE_SUMMARY_SELECT),
    ]
    transport = _FakeTransport(
        json.dumps(
            {
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/messages?" + urlencode(next_query)
                ),
                "value": [],
            }
        ).encode()
    )
    adapter = MicrosoftConnectorAdapter()

    result = adapter.execute(
        operation,
        input_value,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert transport.calls[0]["query"] == (
        ("$select", _MESSAGE_SUMMARY_SELECT),
        ("$top", "50"),
        ("$search", graph_search),
    )
    assert result.continuation == {
        "path": "/v1.0/me/messages",
        "query": [["$skiptoken", "opaque-page-two"]],
    }
    adapter.execute(
        operation,
        input_value,
        continuation=result.continuation,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[1]["query"] == (
        ("$select", _MESSAGE_SUMMARY_SELECT),
        ("$top", "50"),
        ("$search", graph_search),
        ("$skiptoken", "opaque-page-two"),
    )

    forged_queries = (
        [("$skiptoken", "opaque"), ("$top", "50"), ("$select", _MESSAGE_SUMMARY_SELECT)],
        [
            ("$skiptoken", "opaque"),
            ("$top", "50"),
            ("$search", '"subject:Changed"'),
            ("$select", _MESSAGE_SUMMARY_SELECT),
        ],
        [
            ("$skiptoken", "opaque"),
            ("$top", "50"),
            ("$search", graph_search),
            ("$search", graph_search),
            ("$select", _MESSAGE_SUMMARY_SELECT),
        ],
        [
            ("$skiptoken", "opaque"),
            ("$top", "1001"),
            ("$search", graph_search),
            ("$select", _MESSAGE_SUMMARY_SELECT),
        ],
        [
            ("$skiptoken", "opaque"),
            ("$top", "50"),
            ("$top", "50"),
            ("$search", graph_search),
            ("$select", _MESSAGE_SUMMARY_SELECT),
        ],
        [
            ("$skiptoken", "opaque"),
            ("$top", "50"),
            ("$search", graph_search),
            ("$filter", "isRead eq true"),
            ("$select", _MESSAGE_SUMMARY_SELECT),
        ],
    )
    for query in forged_queries:
        forged = _FakeTransport(
            json.dumps(
                {
                    "@odata.nextLink": (
                        "https://graph.microsoft.com/v1.0/me/messages?" + urlencode(query)
                    ),
                    "value": [],
                }
            ).encode()
        )
        with pytest.raises(ConnectorProviderError) as raised:
            adapter.execute(
                operation,
                input_value,
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, forged),
            )
        assert raised.value.code == "invalid_next_link"


def test_tampered_microsoft_continuation_replay_fails_before_transport() -> None:
    start = "2026-08-01T09:00:00"
    end = "2026-08-01T10:00:00"
    messages = _operation("outlook_mail", ConnectorMode.READ, "messages.list")
    window = _operation("outlook_calendar", ConnectorMode.READ, "events.window")
    message_input: dict[str, object] = {}
    window_input = {
        "calendar_id": "primary",
        "end": end,
        "start": start,
    }
    cases = (
        (
            messages,
            message_input,
            {"path": "/v1.0/me/mailFolders", "query": [["$skiptoken", "opaque"]]},
            "fixed Graph route",
        ),
        (
            messages,
            message_input,
            {"path": "/v1.0/me/messages", "query": [["$top", "10"]]},
            "continuation is invalid",
        ),
        (
            messages,
            message_input,
            {
                "path": "/v1.0/me/messages",
                "query": [["$filter", "isRead eq true"], ["$skiptoken", "opaque"]],
            },
            "continuation is invalid",
        ),
        (
            messages,
            message_input,
            {
                "path": "/v1.0/me/messages",
                "query": [["$top", "1001"], ["$skiptoken", "opaque"]],
            },
            "continuation is invalid",
        ),
        (
            messages,
            message_input,
            {
                "path": "/v1.0/me/messages",
                "query": [["$select", _MESSAGE_SUMMARY_SELECT], ["$skiptoken", "opaque"]],
            },
            "continuation is invalid",
        ),
        (
            messages,
            message_input,
            {"path": "/v1.0/me/messages", "query": [["$skip", "0"]]},
            "continuation is invalid",
        ),
        (
            messages,
            message_input,
            {
                "path": "/v1.0/me/messages",
                "query": [["$skiptoken", "opaque"], ["url", "https://example.test"]],
            },
            "continuation is invalid",
        ),
        (
            window,
            window_input,
            {
                "path": "/v1.0/me/calendar/calendarView",
                "query": [
                    ["startDateTime", "2026-08-02T09:00:00"],
                    ["endDateTime", end],
                    ["$skiptoken", "opaque"],
                ],
            },
            "continuation is invalid",
        ),
        (
            window,
            window_input,
            {
                "path": "/v1.0/me/calendar/calendarView",
                "query": [
                    ["startDateTime", start],
                    ["startDateTime", start],
                    ["endDateTime", end],
                    ["$skiptoken", "opaque"],
                ],
            },
            "continuation is invalid",
        ),
    )

    for operation, input_value, continuation, error in cases:
        transport = _FakeTransport()
        with pytest.raises(ValidationError, match=error):
            MicrosoftConnectorAdapter().execute(
                operation,
                input_value,
                continuation=continuation,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
            )
        assert transport.calls == []


def test_calendar_read_options_map_to_graph_query_and_prefer_header() -> None:
    adapter = MicrosoftConnectorAdapter()
    events_list = _operation("outlook_calendar", ConnectorMode.READ, "events.list")
    events_get = _operation("outlook_calendar", ConnectorMode.READ, "events.get")
    window = _operation("outlook_calendar", ConnectorMode.READ, "events.window")

    transport = _FakeTransport()
    adapter.execute(
        events_list,
        {"calendar_id": "primary", "page_size": 100, "time_zone": "Europe/Brussels"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[0]["query"] == (("$top", "100"),)
    assert transport.calls[0]["headers"] == {
        "Prefer": 'IdType="ImmutableId", outlook.timezone="Europe/Brussels"'
    }

    adapter.execute(
        events_get,
        {"calendar_id": "primary", "event_id": "event-1", "time_zone": "UTC"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[1]["headers"] == {
        "Prefer": 'IdType="ImmutableId", outlook.timezone="UTC"'
    }

    window_transport = _FakeTransport()
    adapter.execute(
        window,
        {
            "calendar_id": "primary",
            "end": "2026-08-01T10:00:00",
            "page_size": 1_000,
            "start": "2026-08-01T09:00:00",
            "time_zone": "Europe/Brussels",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, window_transport),
    )
    assert window_transport.calls[0]["query"] == (
        ("startDateTime", "2026-08-01T09:00:00"),
        ("endDateTime", "2026-08-01T10:00:00"),
        ("$top", "1000"),
    )
    assert window_transport.calls[0]["headers"] == {
        "Prefer": 'IdType="ImmutableId", outlook.timezone="Europe/Brussels"'
    }


def test_calendar_window_materializes_the_documented_1000_event_page() -> None:
    events = [{"id": f"event-{index}"} for index in range(1_000)]
    transport = _FakeTransport(response=json.dumps({"value": events}).encode())

    result = MicrosoftConnectorAdapter().execute(
        _operation("outlook_calendar", ConnectorMode.READ, "events.window"),
        {
            "calendar_id": "primary",
            "end": "2026-08-01T10:00:00",
            "page_size": 1_000,
            "start": "2026-08-01T09:00:00",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    assert isinstance(result.payload, dict)
    assert result.payload["value"] == events


def test_calendar_window_next_link_round_trips_exact_dates_and_rejects_forgery() -> None:
    start = "2026-08-01T09:00:00"
    end = "2026-08-01T10:00:00"
    operation = _operation("outlook_calendar", ConnectorMode.READ, "events.window")
    value = {"calendar_id": "primary", "end": end, "start": start}
    next_link = (
        "https://graph.microsoft.com/v1.0/me/calendar/calendarView?"
        f"startDateTime={start}&endDateTime={end}&$skiptoken=opaque-page-two"
    )
    transport = _FakeTransport(
        response=json.dumps({"@odata.nextLink": next_link, "value": []}).encode()
    )
    adapter = MicrosoftConnectorAdapter()
    result = adapter.execute(
        operation,
        value,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert result.continuation == {
        "path": "/v1.0/me/calendar/calendarView",
        "query": [
            ["startDateTime", start],
            ["endDateTime", end],
            ["$skiptoken", "opaque-page-two"],
        ],
    }

    adapter.execute(
        operation,
        value,
        continuation=result.continuation,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[1]["query"] == (
        ("startDateTime", start),
        ("endDateTime", end),
        ("$skiptoken", "opaque-page-two"),
    )

    forged_links = (
        next_link.replace("/me/calendar/calendarView", "/me/calendars/other/calendarView"),
        next_link.replace(f"startDateTime={start}", "startDateTime=2026-08-02T09:00:00"),
        next_link.replace(f"endDateTime={end}", f"endDateTime={end}&endDateTime={end}"),
        next_link.replace("&$skiptoken=opaque-page-two", ""),
        next_link.replace("$skiptoken=opaque-page-two", "$skiptoken="),
        next_link.replace("$skiptoken=opaque-page-two", "$skip=0"),
    )
    for forged in forged_links:
        forged_transport = _FakeTransport(
            response=json.dumps({"@odata.nextLink": forged, "value": []}).encode()
        )
        with pytest.raises(ConnectorProviderError) as raised:
            adapter.execute(
                operation,
                value,
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, forged_transport),
            )
        assert raised.value.code == "invalid_next_link"


def test_message_update_and_event_fields_use_fixed_typed_graph_bodies() -> None:
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport()
    message_update = _operation("outlook_mail", ConnectorMode.WRITE, "messages.update")
    event_update = _operation("outlook_calendar", ConnectorMode.WRITE, "events.update")

    adapter.execute(
        message_update,
        {
            "categories": ["Follow up"],
            "follow_up": "flagged",
            "importance": "high",
            "is_read": True,
            "message_id": "message-1",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    adapter.execute(
        event_update,
        {
            "calendar_id": "primary",
            "change_key": "event-version-1",
            "event_id": "event-1",
            "importance": "high",
            "is_all_day": False,
            "response_requested": False,
            "sensitivity": "private",
            "show_as": "working_elsewhere",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    message_call, event_preflight, event_call = transport.calls
    assert message_call["json_body"] == {
        "categories": ["Follow up"],
        "flag": {"flagStatus": "flagged"},
        "importance": "high",
        "isRead": True,
    }
    assert event_call["json_body"] == {
        "importance": "high",
        "isAllDay": False,
        "responseRequested": False,
        "sensitivity": "private",
        "showAs": "workingElsewhere",
    }
    assert event_preflight["method"] is ConnectorMethod.GET
    assert event_call["headers"] == {
        "If-Match": "event-version-1",
        "Prefer": 'IdType="ImmutableId"',
    }


def test_message_list_accepts_the_documented_bounded_1000_page_size() -> None:
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport()
    operation = _operation("outlook_mail", ConnectorMode.READ, "messages.list")

    adapter.execute(
        operation,
        {"page_size": 1_000},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[0]["query"] == (
        ("$select", _MESSAGE_SUMMARY_SELECT),
        ("$top", "1000"),
    )


def test_message_queries_use_canonical_projections_and_get_body_format() -> None:
    adapter = MicrosoftConnectorAdapter()
    list_transport = _FakeTransport()
    search_transport = _FakeTransport()
    unread_transport = _FakeTransport()
    get_transport = _FakeTransport()
    custom_get_transport = _FakeTransport()

    adapter.execute(
        _operation("outlook_mail", ConnectorMode.READ, "messages.list"),
        {"fields": ["subject", "id"], "order_by": "sent_at"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, list_transport),
    )
    adapter.execute(
        _operation("outlook_mail", ConnectorMode.READ, "messages.list"),
        {"page_size": 50, "search": "from:ada@example.test"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, search_transport),
    )
    adapter.execute(
        _operation("outlook_mail", ConnectorMode.READ, "messages.list"),
        {"is_read": False},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, unread_transport),
    )
    adapter.execute(
        _operation("outlook_mail", ConnectorMode.READ, "messages.get"),
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, get_transport),
    )
    adapter.execute(
        _operation("outlook_mail", ConnectorMode.READ, "messages.get"),
        {
            "body_format": "text",
            "fields": ["unique_body", "internet_message_headers", "body", "id"],
            "message_id": "message-1",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, custom_get_transport),
    )

    assert list_transport.calls[0]["query"] == (
        ("$select", "id,subject"),
        ("$orderby", "sentDateTime asc"),
    )
    assert search_transport.calls[0]["query"] == (
        ("$select", _MESSAGE_SUMMARY_SELECT),
        ("$top", "50"),
        ("$search", '"from:ada@example.test"'),
    )
    assert unread_transport.calls[0]["query"] == (
        ("$select", _MESSAGE_SUMMARY_SELECT),
        ("$filter", "isRead eq false"),
    )
    assert get_transport.calls[0]["query"] == (("$select", _MESSAGE_DETAIL_SELECT),)
    assert custom_get_transport.calls[0]["query"] == (
        ("$select", "body,id,internetMessageHeaders,uniqueBody"),
    )
    assert custom_get_transport.calls[0]["headers"] == {
        "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"'
    }


def test_invalid_message_query_combinations_and_duplicate_fields_fail_before_transport() -> None:
    adapter = MicrosoftConnectorAdapter()
    cases = (
        ("messages.list", {"is_read": True, "order_by": "received_at"}),
        ("messages.list", {"search": "subject:planning", "sort_direction": "descending"}),
        ("messages.list", {"search": '"subject:planning'}),
        ("messages.list", {"search": '"a"b"'}),
        ("messages.list", {"search": '"planning\\"'}),
        ("messages.list", {"fields": ["id", "id"]}),
        ("messages.get", {"fields": ["subject", "subject"], "message_id": "message-1"}),
    )
    for name, value in cases:
        transport = _FakeTransport()
        with pytest.raises(ValidationError):
            adapter.execute(
                _operation("outlook_mail", ConnectorMode.READ, name),
                value,
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
            )
        assert transport.calls == []


def test_message_follow_up_dates_map_and_invalid_combinations_fail_before_transport() -> None:
    operation = _operation("outlook_mail", ConnectorMode.WRITE, "messages.update")
    start = {"date_time": "2026-08-03T09:00:00", "time_zone": "Europe/Brussels"}
    due = {"date_time": "2026-08-04T17:00:00", "time_zone": "Europe/Brussels"}
    completed = {"date_time": "2026-08-04T12:00:00", "time_zone": "Europe/Brussels"}
    transport = _FakeTransport()

    MicrosoftConnectorAdapter().execute(
        operation,
        {
            "follow_up": "complete",
            "follow_up_completed": completed,
            "follow_up_due": due,
            "follow_up_start": start,
            "message_id": "message-1",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[0]["json_body"] == {
        "flag": {
            "completedDateTime": {
                "dateTime": "2026-08-04T12:00:00",
                "timeZone": "Europe/Brussels",
            },
            "dueDateTime": {
                "dateTime": "2026-08-04T17:00:00",
                "timeZone": "Europe/Brussels",
            },
            "flagStatus": "complete",
            "startDateTime": {
                "dateTime": "2026-08-03T09:00:00",
                "timeZone": "Europe/Brussels",
            },
        }
    }

    invalid = (
        {"follow_up_start": start, "message_id": "message-1"},
        {"follow_up": "flagged", "follow_up_due": due, "message_id": "message-1"},
        {
            "follow_up": "not_flagged",
            "follow_up_start": start,
            "message_id": "message-1",
        },
        {
            "follow_up": "flagged",
            "follow_up_completed": completed,
            "message_id": "message-1",
        },
    )
    for value in invalid:
        invalid_transport = _FakeTransport()
        with pytest.raises(ValidationError):
            MicrosoftConnectorAdapter().execute(
                operation,
                value,
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, invalid_transport),
            )
        assert invalid_transport.calls == []


def test_forged_or_unknown_operation_never_reaches_graph_transport() -> None:
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport()
    forged = OperationSpec(
        provider="outlook_mail",
        mode=ConnectorMode.WRITE,
        name="drafts.send",
        effect=ConnectorEffect.SAFE_MUTATION,
        endpoint="drafts.send",
        required_scopes=(frozenset({"Mail.ReadWrite"}),),
        input_schema={
            "additionalProperties": False,
            "properties": {
                "message_id": {"maxLength": 1024, "minLength": 1, "type": "string"},
            },
            "required": ["message_id"],
            "type": "object",
        },
    )
    unknown = OperationSpec(
        provider="outlook_mail",
        mode=ConnectorMode.READ,
        name="messages.inspect",
        effect=ConnectorEffect.READ,
        endpoint="messages.inspect",
        required_scopes=(frozenset({"Mail.Read"}),),
        input_schema={
            "additionalProperties": False,
            "properties": {},
            "required": [],
            "type": "object",
        },
    )

    with pytest.raises(ValidationError, match="Microsoft catalog"):
        adapter.classify_effect(forged, {"message_id": "message-1"})
    for operation, input_value in ((forged, {"message_id": "message-1"}), (unknown, {})):
        with pytest.raises(ValidationError, match="Microsoft catalog"):
            adapter.execute(
                operation,
                input_value,
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
            )
    assert transport.calls == []


def test_send_effect_precondition_and_typed_message_and_event_bodies() -> None:
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport()
    draft_update = _operation("outlook_mail", ConnectorMode.WRITE, "drafts.update")
    event_create = _operation("outlook_calendar", ConnectorMode.WRITE, "events.create")
    send = _operation("outlook_mail", ConnectorMode.WRITE, "drafts.send")
    update_input = {**_draft(), "message_id": "message-1", "change_key": "version-1"}

    adapter.execute(
        draft_update,
        update_input,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    adapter.execute(
        event_create,
        _event(),
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    adapter.execute(
        send,
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    message_call, event_call, send_call = transport.calls
    assert message_call["headers"] == {"If-Match": "version-1", "Prefer": 'IdType="ImmutableId"'}
    assert message_call["json_body"] == {
        "body": {"content": "Hello", "contentType": "HTML"},
        "subject": "Hello",
        "toRecipients": [{"emailAddress": {"address": "ada@example.test", "name": "Ada"}}],
    }
    event_body = cast(dict[str, object], event_call["json_body"])
    assert event_body["transactionId"] == "client-transaction-1"
    assert event_body["attendees"] == [
        {"emailAddress": {"address": "ada@example.test"}, "type": "required"}
    ]
    assert send_call["json_body"] is None
    assert adapter.classify_effect(send, {"message_id": "message-1"}) is ConnectorEffect.OUTWARD
    assert adapter.classify_effect(event_create, _event()) is ConnectorEffect.OUTWARD
    assert (
        adapter.classify_effect(event_create, _event(attendees=False))
        is ConnectorEffect.SAFE_MUTATION
    )
    assert (
        adapter.classify_effect(event_create, _event("shared-calendar", attendees=False))
        is ConnectorEffect.OUTWARD
    )


def test_freebusy_interval_maps_and_graph_forbidden_is_typed() -> None:
    adapter = MicrosoftConnectorAdapter()
    operation = _operation("outlook_calendar", ConnectorMode.READ, "freebusy.query")
    value = {
        "attendees": ["ada@example.test"],
        "end": "2026-08-01T10:00:00",
        "interval_minutes": 30,
        "start": "2026-08-01T09:00:00",
    }
    transport = _FakeTransport()
    adapter.execute(
        operation,
        value,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[0]["json_body"] == {
        "availabilityViewInterval": 30,
        "endTime": {"dateTime": "2026-08-01T10:00:00", "timeZone": "UTC"},
        "schedules": ["ada@example.test"],
        "startTime": {"dateTime": "2026-08-01T09:00:00", "timeZone": "UTC"},
    }

    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=403,
        code="ErrorAccessDenied",
        retry_after="30",
    )
    with pytest.raises(ConnectorProviderError) as raised:
        adapter.execute(
            operation,
            value,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, _FakeTransport(failure=failure)),
        )
    assert raised.value.code == "freebusy_unsupported_for_account_or_permission"
    assert raised.value.retry_after == "30"


def test_event_delete_preflights_change_key_and_pins_if_match() -> None:
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.delete")
    value = {
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
    }
    adapter = MicrosoftConnectorAdapter()

    stale = _FakeTransport(
        event={
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-2",
            "id": "event-1",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        }
    )
    with pytest.raises(ValidationError, match="read it again"):
        adapter.classify_effect(
            operation,
            value,
            credential=_credential(),
            transport=cast(ConnectorTransport, stale),
        )
    assert [call["method"] for call in stale.calls] == [ConnectorMethod.GET]

    stale_execution = _FakeTransport(
        event={
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-2",
            "id": "event-1",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        }
    )
    with pytest.raises(ValidationError, match="read it again"):
        adapter.execute(
            operation,
            value,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, stale_execution),
            write_idempotency_key="confirmed-delete",
        )
    assert [call["method"] for call in stale_execution.calls] == [ConnectorMethod.GET]

    fresh = _FakeTransport()
    assert (
        adapter.classify_effect(
            operation,
            value,
            credential=_credential(),
            transport=cast(ConnectorTransport, fresh),
        )
        is ConnectorEffect.DESTRUCTIVE
    )
    fresh_execution = _FakeTransport()
    adapter.execute(
        operation,
        value,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, fresh_execution),
        write_idempotency_key="confirmed-delete",
    )
    assert [call["method"] for call in fresh_execution.calls] == [
        ConnectorMethod.GET,
        ConnectorMethod.DELETE,
    ]
    assert fresh_execution.calls[-1]["headers"] == {
        "If-Match": "event-version-1",
        "Prefer": 'IdType="ImmutableId"',
    }


@pytest.mark.parametrize(
    ("operation_name", "extra_input"),
    (("events.delete", {}), ("events.update", {"subject": "Updated"})),
)
def test_event_write_412_uses_the_same_fresh_preview_guidance(
    operation_name: str,
    extra_input: dict[str, object],
) -> None:
    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=412,
        code="ErrorPreconditionFailed",
    )
    transport = _FakeTransport(failure=failure, fail_after=1)

    with pytest.raises(ValidationError, match=r"read it again.*fresh preview"):
        MicrosoftConnectorAdapter().execute(
            _operation("outlook_calendar", ConnectorMode.WRITE, operation_name),
            {
                "calendar_id": "primary",
                "change_key": "event-version-1",
                "event_id": "event-1",
                **extra_input,
            },
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
            write_idempotency_key="confirmed-write",
        )

    assert [call["method"] for call in transport.calls] == [
        ConnectorMethod.GET,
        ConnectorMethod.DELETE if operation_name == "events.delete" else ConnectorMethod.PATCH,
    ]


@pytest.mark.parametrize(
    ("provider", "operation_name", "input_value", "fail_after", "resource"),
    (
        (
            "outlook_calendar",
            "events.cancel",
            {
                "calendar_id": "primary",
                "change_key": "event-version-1",
                "event_id": "event-1",
            },
            0,
            "event",
        ),
        (
            "outlook_calendar",
            "calendars.delete",
            {"calendar_id": "calendar-1", "change_key": "calendar-version-1"},
            1,
            "calendar",
        ),
        (
            "outlook_mail",
            "messages.update",
            {
                "change_key": "message-version-1",
                "is_read": True,
                "message_id": "message-1",
            },
            0,
            "message",
        ),
        (
            "outlook_mail",
            "folders.update",
            {
                "change_key": "folder-version-1",
                "display_name": "Updated",
                "folder_id": "folder-1",
            },
            0,
            "folder",
        ),
    ),
)
def test_optional_change_key_412_has_resource_specific_fresh_preview_guidance(
    provider: str,
    operation_name: str,
    input_value: dict[str, object],
    fail_after: int,
    resource: str,
) -> None:
    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=412,
        code="ErrorPreconditionFailed",
    )
    transport = _FakeTransport(failure=failure, fail_after=fail_after)

    with pytest.raises(ValidationError, match=f"Outlook {resource} changed"):
        MicrosoftConnectorAdapter().execute(
            _operation(provider, ConnectorMode.WRITE, operation_name),
            input_value,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
            write_idempotency_key="confirmed-write",
        )

    assert len(transport.calls) == fail_after + 1
    assert cast(dict[str, str], transport.calls[-1]["headers"])["If-Match"] == cast(
        str, input_value["change_key"]
    )


def test_primary_calendar_delete_and_purge_fail_before_transport() -> None:
    adapter = MicrosoftConnectorAdapter()
    for name in ("calendars.delete", "calendars.purge"):
        transport = _FakeTransport()
        with pytest.raises(ValidationError, match="primary calendar"):
            adapter.classify_effect(
                _operation("outlook_calendar", ConnectorMode.WRITE, name),
                {"calendar_id": "primary"},
            )
        with pytest.raises(ValidationError, match="primary calendar"):
            adapter.execute(
                _operation("outlook_calendar", ConnectorMode.WRITE, name),
                {"calendar_id": "primary"},
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, transport),
            )
        assert transport.calls == []

        real_id = _FakeTransport(calendar={"id": "calendar-1", "isDefaultCalendar": True})
        with pytest.raises(ValidationError, match="primary calendar"):
            adapter.classify_effect(
                _operation("outlook_calendar", ConnectorMode.WRITE, name),
                {"calendar_id": "calendar-1"},
                credential=_credential(),
                transport=cast(ConnectorTransport, real_id),
            )
        assert len(real_id.calls) == 1
        assert real_id.calls[0]["method"] is ConnectorMethod.GET
        assert real_id.calls[0]["path"] == _CALENDAR
        assert real_id.calls[0]["query"] == (("$select", "id,isDefaultCalendar"),)

        replay = _FakeTransport(calendar={"id": "calendar-1", "isDefaultCalendar": True})
        with pytest.raises(ValidationError, match="primary calendar"):
            adapter.execute(
                _operation("outlook_calendar", ConnectorMode.WRITE, name),
                {"calendar_id": "calendar-1"},
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, replay),
            )
        assert len(replay.calls) == 1
        assert replay.calls[0]["method"] is ConnectorMethod.GET


def test_teams_event_create_maps_default_and_rejects_cross_field_provider() -> None:
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.create")
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport()
    adapter.execute(
        operation,
        {**_event(attendees=False), "is_online_meeting": True},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[-1]["json_body"] == {
        "body": {"content": "Planning", "contentType": "Text"},
        "end": {"dateTime": "2026-08-01T10:00:00", "timeZone": "Europe/Brussels"},
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "start": {"dateTime": "2026-08-01T09:00:00", "timeZone": "Europe/Brussels"},
        "subject": "Planning",
        "transactionId": "client-transaction-1",
    }

    explicit_transport = _FakeTransport()
    adapter.execute(
        operation,
        {
            **_event(attendees=False),
            "is_online_meeting": True,
            "online_meeting_provider": "teams_for_business",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, explicit_transport),
    )
    assert (
        cast(dict[str, object], explicit_transport.calls[-1]["json_body"])["onlineMeetingProvider"]
        == "teamsForBusiness"
    )

    for invalid in (
        {**_event(attendees=False), "online_meeting_provider": "teams_for_business"},
        {
            **_event(attendees=False),
            "is_online_meeting": False,
            "online_meeting_provider": "teams_for_business",
        },
    ):
        invalid_transport = _FakeTransport()
        with pytest.raises(ValidationError):
            adapter.execute(
                operation,
                invalid,
                continuation=None,
                credential=_credential(),
                transport=cast(ConnectorTransport, invalid_transport),
            )
        assert invalid_transport.calls == []


def test_unsupported_permanent_delete_is_clear_and_never_retried() -> None:
    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=404,
        code="Request_ResourceNotFound",
    )
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport(failure=failure, fail_after=1)
    operation = _operation("outlook_mail", ConnectorMode.WRITE, "messages.purge")

    with pytest.raises(ConnectorProviderError) as raised:
        adapter.execute(
            operation,
            {"message_id": "message-1"},
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
    assert raised.value.code == "permanent_delete_unsupported"
    assert [call["path"] for call in transport.calls] == [
        "/v1.0/me",
        "/v1.0/users/user-1/messages/message-1/permanentDelete",
    ]


def test_graph_recurrence_uses_camel_case_and_explicit_week_and_range_fields() -> None:
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport()
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.create")
    value = {
        **_event(attendees=False),
        "recurrence": {
            "pattern": {
                "days_of_week": ["monday", "wednesday"],
                "first_day_of_week": "monday",
                "interval": 2,
                "type": "weekly",
            },
            "range": {
                "end_date": "2026-12-31",
                "recurrence_time_zone": "Europe/Brussels",
                "start_date": "2026-08-01",
                "type": "end_date",
            },
        },
    }

    adapter.execute(
        operation,
        value,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    graph_body = cast(dict[str, object], transport.calls[-1]["json_body"])
    assert graph_body["recurrence"] == {
        "pattern": {
            "daysOfWeek": ["monday", "wednesday"],
            "firstDayOfWeek": "monday",
            "interval": 2,
            "type": "weekly",
        },
        "range": {
            "endDate": "2026-12-31",
            "recurrenceTimeZone": "Europe/Brussels",
            "startDate": "2026-08-01",
            "type": "endDate",
        },
    }


def test_graph_recurrence_maps_every_typed_pattern_name() -> None:
    patterns = (
        ({"type": "daily", "interval": 1}, "daily"),
        (
            {
                "type": "absolute_monthly",
                "interval": 1,
                "day_of_month": 15,
            },
            "absoluteMonthly",
        ),
        (
            {
                "type": "relative_monthly",
                "interval": 1,
                "days_of_week": ["thursday"],
                "index": "second",
            },
            "relativeMonthly",
        ),
        (
            {
                "type": "absolute_yearly",
                "interval": 1,
                "day_of_month": 15,
                "month": 3,
            },
            "absoluteYearly",
        ),
        (
            {
                "type": "relative_yearly",
                "interval": 1,
                "days_of_week": ["thursday"],
                "index": "second",
                "month": 11,
            },
            "relativeYearly",
        ),
    )
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.create")

    for pattern, expected in patterns:
        transport = _FakeTransport()
        MicrosoftConnectorAdapter().execute(
            operation,
            {
                **_event(attendees=False),
                "recurrence": {
                    "pattern": pattern,
                    "range": {"start_date": "2026-08-01", "type": "no_end"},
                },
            },
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
        graph_body = cast(dict[str, object], transport.calls[-1]["json_body"])
        recurrence = cast(dict[str, object], graph_body["recurrence"])
        graph_pattern = cast(dict[str, object], recurrence["pattern"])
        graph_range = cast(dict[str, object], recurrence["range"])
        assert graph_pattern["type"] == expected
        assert graph_range["type"] == "noEnd"


def test_message_trash_returns_account_bound_restore_handle_and_restores_original_folder() -> None:
    adapter = MicrosoftConnectorAdapter()
    trash_transport = _FakeTransport()
    trash = _operation("outlook_mail", ConnectorMode.WRITE, "messages.trash")
    restore = _operation("outlook_mail", ConnectorMode.WRITE, "messages.restore")

    deleted = adapter.execute(
        trash,
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, trash_transport),
    )
    assert isinstance(deleted.payload, dict)
    handle = cast(str, deleted.payload["restore_handle"])
    assert handle.startswith("rst-")
    assert [call["method"] for call in trash_transport.calls] == [
        ConnectorMethod.GET,
        ConnectorMethod.GET,
        ConnectorMethod.DELETE,
    ]
    assert trash_transport.calls[0]["query"] == (("$select", "id,parentFolderId"),)
    assert trash_transport.calls[1]["query"] == (("$select", "id"),)
    assert all(
        call["headers"] == {"Prefer": 'IdType="ImmutableId"'} for call in trash_transport.calls
    )

    restore_transport = _FakeTransport()
    adapter.execute(
        restore,
        {"message_id": "message-1", "restore_handle": handle},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, restore_transport),
    )
    assert restore_transport.calls[0]["path"] == _ME
    assert restore_transport.calls[-1]["path"] == f"{_MESSAGE}/move"
    assert restore_transport.calls[-1]["json_body"] == {"destinationId": "original-folder"}
    with pytest.raises(ValidationError, match="unavailable"):
        adapter.execute(
            restore,
            {"message_id": "message-1", "restore_handle": handle},
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, _FakeTransport()),
        )


def test_restore_handle_survives_oauth_refresh_for_the_same_graph_subject() -> None:
    adapter = MicrosoftConnectorAdapter()
    deleted = adapter.execute(
        _operation("outlook_mail", ConnectorMode.WRITE, "messages.trash"),
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, _FakeTransport()),
    )
    handle = cast(dict[str, object], deleted.payload)["restore_handle"]
    refreshed = ConnectorRuntimeCredential(
        credential=ConnectorCredential(AuthorizationScheme.BEARER, "refreshed-secret"),
        granted_scopes=("Mail.ReadWrite",),
        version=2,
    )
    restore = _operation("outlook_mail", ConnectorMode.WRITE, "messages.restore")

    transport = _FakeTransport(subject_id="user-1")
    adapter.execute(
        restore,
        {"message_id": "message-1", "restore_handle": handle},
        continuation=None,
        credential=refreshed,
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.calls[0]["path"] == _ME
    assert transport.calls[-1]["path"] == f"{_MESSAGE}/move"


def test_restore_handle_rejects_another_graph_subject_without_consuming_it() -> None:
    adapter = MicrosoftConnectorAdapter()
    deleted = adapter.execute(
        _operation("outlook_mail", ConnectorMode.WRITE, "messages.trash"),
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, _FakeTransport(subject_id="user-1")),
    )
    handle = cast(dict[str, object], deleted.payload)["restore_handle"]
    restore = _operation("outlook_mail", ConnectorMode.WRITE, "messages.restore")

    with pytest.raises(ValidationError, match="does not match"):
        adapter.execute(
            restore,
            {"message_id": "message-1", "restore_handle": handle},
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, _FakeTransport(subject_id="user-2")),
        )
    adapter.execute(
        restore,
        {"message_id": "message-1", "restore_handle": handle},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, _FakeTransport(subject_id="user-1")),
    )


def test_known_restore_failure_keeps_the_handle_retryable() -> None:
    adapter = MicrosoftConnectorAdapter()
    deleted = adapter.execute(
        _operation("outlook_mail", ConnectorMode.WRITE, "messages.trash"),
        {"message_id": "message-1"},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, _FakeTransport()),
    )
    handle = cast(dict[str, object], deleted.payload)["restore_handle"]
    restore = _operation("outlook_mail", ConnectorMode.WRITE, "messages.restore")
    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=409,
        code="ErrorItemNotFound",
    )

    with pytest.raises(ConnectorProviderError):
        adapter.execute(
            restore,
            {"message_id": "message-1", "restore_handle": handle},
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, _FakeTransport(failure=failure, fail_after=1)),
        )
    adapter.execute(
        restore,
        {"message_id": "message-1", "restore_handle": handle},
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, _FakeTransport()),
    )


def test_existing_event_effect_uses_provider_preflight_and_fails_closed_without_it() -> None:
    adapter = MicrosoftConnectorAdapter()
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.update")
    value = {
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
        "subject": "Changed",
    }
    shared_event = {
        "attendees": [{"emailAddress": {"address": "ada@example.test"}}],
        "body": {"content": "Existing", "contentType": "html"},
        "changeKey": "event-version-1",
        "id": "event-1",
        "isOnlineMeeting": False,
        "isOrganizer": True,
    }

    assert adapter.classify_effect(operation, value) is ConnectorEffect.OUTWARD
    safe_transport = _FakeTransport()
    assert (
        adapter.classify_effect(
            operation,
            value,
            credential=_credential(),
            transport=cast(ConnectorTransport, safe_transport),
        )
        is ConnectorEffect.SAFE_MUTATION
    )
    shared_transport = _FakeTransport(event=shared_event)
    assert (
        adapter.classify_effect(
            operation,
            value,
            credential=_credential(),
            transport=cast(ConnectorTransport, shared_transport),
        )
        is ConnectorEffect.OUTWARD
    )
    execute_transport = _FakeTransport(event=shared_event)
    with pytest.raises(ValidationError, match="fresh outward confirmation"):
        adapter.execute(
            operation,
            value,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, execute_transport),
        )
    assert len(execute_transport.calls) == 1
    adapter.execute(
        operation,
        value,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, execute_transport),
        write_idempotency_key="confirmed-write",
    )
    assert execute_transport.calls[-1]["method"] is ConnectorMethod.PATCH


def test_existing_event_mutation_rejects_changed_recipients_and_revision_before_preview() -> None:
    changed_event = {
        "attendees": [{"emailAddress": {"address": "new-recipient@example.test"}}],
        "body": {"content": "Existing", "contentType": "html"},
        "changeKey": "event-version-2",
        "id": "event-1",
        "isOnlineMeeting": False,
        "isOrganizer": True,
    }
    adapter = MicrosoftConnectorAdapter()
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.update")
    transport = _FakeTransport(event=changed_event)
    confirmed_input = {
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
        "subject": "Changed",
    }

    with pytest.raises(ValidationError, match="read it again"):
        adapter.classify_effect(
            operation,
            confirmed_input,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
    assert len(transport.calls) == 1

    execute_transport = _FakeTransport(event=changed_event)
    with pytest.raises(ValidationError, match="read it again"):
        adapter.execute(
            operation,
            confirmed_input,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, execute_transport),
            write_idempotency_key="old-confirmation",
        )
    assert len(execute_transport.calls) == 1


def test_event_attachment_add_uses_the_confirmed_live_revision() -> None:
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "attachments.add")
    value = {
        "attachment": _attachment(),
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
    }
    transport = _FakeTransport()

    MicrosoftConnectorAdapter().execute(
        operation,
        value,
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )
    assert [call["method"] for call in transport.calls] == [
        ConnectorMethod.GET,
        ConnectorMethod.POST,
    ]
    assert transport.calls[-1]["headers"] == {
        "If-Match": "event-version-1",
        "Prefer": 'IdType="ImmutableId"',
    }

    changed = _FakeTransport(
        event={
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-2",
            "id": "event-1",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        }
    )
    with pytest.raises(ValidationError, match="read it again"):
        MicrosoftConnectorAdapter().execute(
            operation,
            value,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, changed),
        )
    assert len(changed.calls) == 1


def test_online_meeting_body_update_preserves_provider_blob_and_uses_fresh_change_key() -> None:
    join_url = "https://teams.example.test/join?tenant=one&meeting=two"
    meeting_blob = (
        '<div class="me-email-text" data-meeting="true">\n'
        "<div><strong>Microsoft Teams meeting</strong></div>\n"
        '<div><a href="https://teams.example.test/join?tenant=one&amp;meeting=two">'
        "Join meeting</a></div>\n"
        "</div>"
    )
    old_agenda = "Legacy agenda\n" + "O" * 5_000
    event = {
        "attendees": [],
        "body": {
            "content": f"<html>\n<body><div>{old_agenda}</div>\n{meeting_blob}\n</body></html>",
            "contentType": "html",
        },
        "changeKey": "provider-change-key",
        "id": "event-1",
        "isOnlineMeeting": True,
        "isOrganizer": True,
        "onlineMeeting": {"joinUrl": join_url},
    }
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport(event=event)
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.update")
    new_agenda = "New agenda\r\nLine two\n" + "N" * 5_000

    adapter.execute(
        operation,
        {
            "body": {"content": new_agenda, "content_type": "text"},
            "calendar_id": "primary",
            "change_key": "provider-change-key",
            "event_id": "event-1",
        },
        continuation=None,
        credential=_credential(),
        transport=cast(ConnectorTransport, transport),
    )

    patch = transport.calls[-1]
    assert patch["headers"] == {
        "If-Match": "provider-change-key",
        "Prefer": 'IdType="ImmutableId"',
    }
    patch_body = cast(dict[str, object], patch["json_body"])
    graph_body = cast(dict[str, object], patch_body["body"])
    content = cast(str, graph_body["content"])
    assert graph_body["contentType"] == "HTML"
    assert content.startswith("New agenda<br>Line two<br>" + "N" * 5_000)
    assert content.endswith(meeting_blob)
    assert old_agenda not in content
    assert "O" * 5_000 not in content

    stale_transport = _FakeTransport(event=event)
    with pytest.raises(ValidationError, match="event changed"):
        adapter.execute(
            operation,
            {
                "calendar_id": "primary",
                "change_key": "stale-change-key",
                "event_id": "event-1",
                "subject": "Must not be sent",
            },
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, stale_transport),
        )
    assert len(stale_transport.calls) == 1


def test_online_meeting_body_update_fails_closed_without_an_isolated_provider_blob() -> None:
    event = {
        "attendees": [],
        "body": {
            "content": (
                "<html>\n<body><div>Legacy agenda "
                + "O" * 5_000
                + '</div><a href="https://teams.example.test/join">Join</a></body></html>'
            ),
            "contentType": "html",
        },
        "changeKey": "provider-change-key",
        "id": "event-1",
        "isOnlineMeeting": True,
        "isOrganizer": True,
        "onlineMeeting": {"joinUrl": "https://teams.example.test/join"},
    }
    transport = _FakeTransport(event=event)

    with pytest.raises(ValidationError, match="edit the meeting body in Outlook"):
        MicrosoftConnectorAdapter().execute(
            _operation("outlook_calendar", ConnectorMode.WRITE, "events.update"),
            {
                "body": {"content": "New\nagenda", "content_type": "text"},
                "calendar_id": "primary",
                "change_key": "provider-change-key",
                "event_id": "event-1",
            },
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
    assert len(transport.calls) == 1


def test_event_purge_rejects_calendar_binding_mismatch_before_permanent_delete() -> None:
    transport = _FakeTransport(
        event={
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-1",
            "id": "another-event",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        }
    )

    with pytest.raises(ConnectorProviderError) as raised:
        MicrosoftConnectorAdapter().execute(
            _operation("outlook_calendar", ConnectorMode.WRITE, "events.purge"),
            {
                "calendar_id": "primary",
                "change_key": "event-version-1",
                "event_id": "event-1",
            },
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
    assert raised.value.code == "event_calendar_binding_mismatch"
    assert len(transport.calls) == 1
    assert transport.calls[0]["path"] == _EVENT
    assert "changeKey" in str(transport.calls[0]["query"])


def test_event_purge_requires_current_change_key_and_remaps_dispatch_412() -> None:
    stale = _FakeTransport(
        event={
            "attendees": [],
            "body": {"content": "Existing", "contentType": "html"},
            "changeKey": "event-version-2",
            "id": "event-1",
            "isOnlineMeeting": False,
            "isOrganizer": True,
        }
    )
    adapter = MicrosoftConnectorAdapter()
    operation = _operation("outlook_calendar", ConnectorMode.WRITE, "events.purge")
    value = {
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
    }
    with pytest.raises(ValidationError, match=r"read it again.*fresh preview"):
        adapter.classify_effect(
            operation,
            value,
            credential=_credential(),
            transport=cast(ConnectorTransport, stale),
        )
    assert len(stale.calls) == 1

    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=412,
        code="ErrorPreconditionFailed",
    )
    dispatch = _FakeTransport(failure=failure, fail_after=2)
    with pytest.raises(ValidationError, match=r"read it again.*fresh preview"):
        adapter.execute(
            operation,
            value,
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, dispatch),
            write_idempotency_key="confirmed-purge",
        )
    assert [call["method"] for call in dispatch.calls] == [
        ConnectorMethod.GET,
        ConnectorMethod.GET,
        ConnectorMethod.POST,
    ]
    assert dispatch.calls[-1]["headers"] == {
        "If-Match": "event-version-1",
        "Prefer": 'IdType="ImmutableId"',
    }


def test_personal_account_calendar_purge_returns_typed_unsupported_after_subject_resolution() -> (
    None
):
    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=403,
        code="ErrorAccessDenied",
    )
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport(failure=failure, fail_after=2)

    with pytest.raises(ConnectorProviderError) as raised:
        adapter.execute(
            _operation("outlook_calendar", ConnectorMode.WRITE, "calendars.purge"),
            {"calendar_id": "calendar-1"},
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
    assert raised.value.code == "calendar_permanent_delete_unsupported_for_account"
    assert transport.calls[0]["query"] == (("$select", "id,isDefaultCalendar"),)
    assert transport.calls[1]["query"] == (("$select", "id"),)
    assert transport.calls[2]["path"] == ("/v1.0/users/user-1/calendars/calendar-1/permanentDelete")
