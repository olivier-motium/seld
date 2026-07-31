from __future__ import annotations

import json
from types import MappingProxyType
from typing import cast

import pytest

from continuity_kernel.connector_adapter import ConnectorRuntimeCredential
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


class _FakeTransport:
    def __init__(self, response: bytes = b"{}", failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response
        self._failure = failure

    def request(self, **kwargs: object) -> ConnectorResponse:
        self.calls.append(kwargs)
        if self._failure is not None:
            raise self._failure
        return ConnectorResponse(
            origin=ConnectorOrigin.MICROSOFT_GRAPH,
            status=200,
            headers=MappingProxyType({}),
            body=self._response,
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
            "messages.restore": {"message_id": "message-1"},
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
        "events.update": {"calendar_id": "primary", "event_id": "event-1", "subject": "Changed"},
        "events.delete": {"calendar_id": "primary", "event_id": "event-1"},
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
            "event_id": "event-1",
            "attachment": _attachment(),
        },
        "attachments.delete": {
            "calendar_id": "primary",
            "event_id": "event-1",
            "attachment_id": "attachment-1",
        },
        "events.purge": {"calendar_id": "primary", "event_id": "event-1"},
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
        "folders.purge": (ConnectorMethod.POST, f"{_MAIL_FOLDER}/permanentDelete"),
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
        "messages.purge": (ConnectorMethod.POST, f"{_MESSAGE}/permanentDelete"),
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
        "calendars.purge": (ConnectorMethod.POST, f"{_CALENDAR}/permanentDelete"),
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
        "events.purge": (ConnectorMethod.POST, f"{_EVENT}/permanentDelete"),
    },
}


def _operation(provider: str, mode: ConnectorMode, name: str) -> OperationSpec:
    return next(
        operation
        for operation in MICROSOFT_OPERATIONS
        if operation.provider == provider and operation.mode is mode and operation.name == name
    )


def test_every_microsoft_operation_uses_its_fixed_graph_route_once() -> None:
    adapter = MicrosoftConnectorAdapter()
    catalog_keys = {(operation.provider, operation.name) for operation in MICROSOFT_OPERATIONS}
    expected_keys = {
        (provider, name) for provider, requests in _EXPECTED_REQUESTS.items() for name in requests
    }

    assert expected_keys == catalog_keys
    for operation in MICROSOFT_OPERATIONS:
        transport = _FakeTransport()
        adapter.execute(
            operation,
            _input_for(operation),
            continuation=None,
            credential=_credential(),
            transport=cast(ConnectorTransport, transport),
        )
        assert len(transport.calls) == 1
        call = transport.calls[0]
        expected = _EXPECTED_REQUESTS[operation.provider][operation.name]
        assert (call["method"], call["path"]) == expected
        assert call["headers"] and call["headers"]["Prefer"].startswith('IdType="ImmutableId"')


def test_immutable_ids_and_continuations_are_internal_and_stripped_from_payload() -> None:
    next_link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=opaque&$top=10"
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
    assert transport.calls[1]["query"] == (("$skiptoken", "opaque"), ("$top", "10"))


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
    message_call, event_call = transport.calls
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
    assert transport.calls[0]["query"] == (("$top", "1000"),)


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
    assert event_call["json_body"]["transactionId"] == "client-transaction-1"
    assert event_call["json_body"]["attendees"] == [
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


def test_unsupported_permanent_delete_is_clear_and_never_retried() -> None:
    failure = ConnectorProviderError(
        origin=ConnectorOrigin.MICROSOFT_GRAPH,
        status=404,
        code="Request_ResourceNotFound",
    )
    adapter = MicrosoftConnectorAdapter()
    transport = _FakeTransport(failure=failure)
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
    assert len(transport.calls) == 1
