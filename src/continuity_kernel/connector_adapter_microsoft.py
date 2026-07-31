"""Fixed Microsoft Graph adapter for the bounded Outlook operation catalog."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, quote, urlsplit

from continuity_kernel.connector_adapter import ConnectorAdapterResult, ConnectorRuntimeCredential
from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationSpec
from continuity_kernel.connector_operations_microsoft import MICROSOFT_OPERATIONS
from continuity_kernel.connector_transport import (
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError

_PRIMARY_CALENDAR_ALIAS: Final = "primary"
_ORIGIN: Final = ConnectorOrigin.MICROSOFT_GRAPH
_GRAPH_HOST: Final = "graph.microsoft.com"
_ROOT: Final = "/v1.0/me"
_PERMANENT_DELETE_UNSUPPORTED: Final = frozenset({404, 405, 501})
_CONTINUATION_KEYS: Final = frozenset(
    {"$filter", "$orderby", "$search", "$skip", "$skiptoken", "$top"}
)
_OPERATIONS: Final = {operation.key: operation for operation in MICROSOFT_OPERATIONS}
_FOLLOW_UP_STATUS: Final = {
    "not_flagged": "notFlagged",
    "flagged": "flagged",
    "complete": "complete",
}
_SHOW_AS: Final = {
    "free": "free",
    "tentative": "tentative",
    "busy": "busy",
    "oof": "oof",
    "working_elsewhere": "workingElsewhere",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class _RequestShape:
    path: str
    method: ConnectorMethod
    query: tuple[tuple[str, str], ...] = ()
    json_body: object | None = None
    expected_statuses: frozenset[int] = frozenset({200})
    time_zone: str | None = None
    mime: bool = False
    response_bound: int = 16 * 1024 * 1024


class MicrosoftConnectorAdapter:
    """Translate only catalogued Outlook operations into pinned Graph requests."""

    @property
    def providers(self) -> frozenset[str]:
        return frozenset({"outlook_mail", "outlook_calendar"})

    def classify_effect(self, operation: OperationSpec, input_value: object) -> ConnectorEffect:
        known = _known_operation(operation)
        data = _input(known, input_value)
        if (
            known.provider == "outlook_calendar"
            and known.effect is ConnectorEffect.SAFE_MUTATION
            and (known.name.startswith("events.") or known.name == "attachments.add")
            and _event_mutation_is_outward(data)
        ):
            return ConnectorEffect.OUTWARD
        return known.effect

    def execute(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        write_idempotency_key: str | None = None,
    ) -> ConnectorAdapterResult:
        del write_idempotency_key
        known = _known_operation(operation)
        data = _input(known, input_value)
        if not isinstance(credential, ConnectorRuntimeCredential):
            raise ValidationError("connector runtime credential is invalid")
        if continuation is not None and known.mode is not ConnectorMode.READ:
            raise ValidationError("connector write operation cannot use a continuation")

        shape = _request_shape(known, data)
        query = shape.query
        if continuation is not None:
            query = _continuation_query(continuation, path=shape.path)
        try:
            response = transport.request(
                origin=_ORIGIN,
                method=shape.method,
                path=shape.path,
                credential=credential.credential,
                query=query,
                json_body=shape.json_body,
                headers=_headers(data, time_zone=shape.time_zone),
                expected_statuses=shape.expected_statuses,
                response_bound=shape.response_bound,
            )
        except ConnectorProviderError as exc:
            if known.name.endswith(".purge") and exc.status in _PERMANENT_DELETE_UNSUPPORTED:
                raise ConnectorProviderError(
                    origin=_ORIGIN,
                    status=exc.status,
                    code="permanent_delete_unsupported",
                    retry_after=exc.retry_after,
                ) from exc
            raise
        return _result(response, path=shape.path, mime=shape.mime)


def _known_operation(value: object) -> OperationSpec:
    if not isinstance(value, OperationSpec):
        raise ValidationError("connector operation is invalid")
    operation = _OPERATIONS.get(value.key)
    if operation is None or value != operation:
        raise ValidationError("connector operation is not in the Microsoft catalog")
    return operation


def _input(operation: OperationSpec, value: object) -> dict[str, object]:
    validated = operation.validate_input(value)
    if not isinstance(validated, dict):
        raise ValidationError("connector operation input is invalid")
    return validated


def _request_shape(operation: OperationSpec, data: dict[str, object]) -> _RequestShape:
    if operation.provider == "outlook_mail":
        return _mail_shape(operation.name, data)
    if operation.provider == "outlook_calendar":
        return _calendar_shape(operation.name, data)
    raise ValidationError("connector operation is not implemented by Microsoft Graph")


def _mail_shape(name: str, data: dict[str, object]) -> _RequestShape:
    if name == "folders.list":
        parent = _optional_text(data, "parent_folder_id")
        path = f"{_ROOT}/mailFolders" if parent is None else f"{_folder_path(parent)}/childFolders"
        return _RequestShape(path, ConnectorMethod.GET, query=_folder_query(data))
    if name == "folders.get":
        return _RequestShape(_folder_path(_text(data, "folder_id")), ConnectorMethod.GET)
    if name == "messages.list":
        folder = _optional_text(data, "folder_id")
        path = f"{_folder_path(folder)}/messages" if folder is not None else f"{_ROOT}/messages"
        return _RequestShape(path, ConnectorMethod.GET, query=_message_query(data))
    if name == "messages.get":
        return _RequestShape(_message_path(_text(data, "message_id")), ConnectorMethod.GET)
    if name == "messages.mime":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/$value",
            ConnectorMethod.GET,
            mime=True,
            response_bound=192_000,
        )
    if name == "attachments.list":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments",
            ConnectorMethod.GET,
        )
    if name == "attachments.get":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.GET,
        )
    if name == "folders.create":
        parent = _optional_text(data, "parent_folder_id")
        path = f"{_ROOT}/mailFolders" if parent is None else f"{_folder_path(parent)}/childFolders"
        return _RequestShape(
            path,
            ConnectorMethod.POST,
            json_body={"displayName": _text(data, "display_name")},
            expected_statuses=frozenset({201}),
        )
    if name == "folders.update":
        return _RequestShape(
            _folder_path(_text(data, "folder_id")),
            ConnectorMethod.PATCH,
            json_body=_only(data, {"display_name": "displayName"}),
        )
    if name == "folders.move":
        return _move_shape(
            _folder_path(_text(data, "folder_id")),
            _text(data, "destination_folder_id"),
        )
    if name == "folders.trash":
        return _RequestShape(
            _folder_path(_text(data, "folder_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "folders.restore":
        return _move_shape(
            _folder_path(_text(data, "folder_id")),
            _optional_text(data, "parent_folder_id") or "msgfolderroot",
        )
    if name == "folders.purge":
        return _permanent_delete_shape(_folder_path(_text(data, "folder_id")))
    if name == "drafts.create":
        return _RequestShape(
            f"{_ROOT}/messages",
            ConnectorMethod.POST,
            json_body=_mail_message(data),
            expected_statuses=frozenset({201}),
        )
    if name == "drafts.update":
        return _RequestShape(
            _message_path(_text(data, "message_id")),
            ConnectorMethod.PATCH,
            json_body=_mail_message(data),
        )
    if name == "drafts.reply":
        return _draft_reply_shape(data, "createReply")
    if name == "drafts.reply_all":
        return _draft_reply_shape(data, "createReplyAll")
    if name == "drafts.forward":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/createForward",
            ConnectorMethod.POST,
            json_body={
                "comment": _optional_text(data, "comment") or "",
                "toRecipients": _graph_recipients(data["to_recipients"]),
            },
            expected_statuses=frozenset({201}),
        )
    if name == "attachments.add":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments",
            ConnectorMethod.POST,
            json_body=_graph_attachment(data["attachment"]),
            expected_statuses=frozenset({201}),
        )
    if name == "attachments.delete":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "drafts.send":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/send",
            ConnectorMethod.POST,
            expected_statuses=frozenset({202}),
        )
    if name == "messages.update":
        return _RequestShape(
            _message_path(_text(data, "message_id")),
            ConnectorMethod.PATCH,
            json_body=_message_update_body(data),
        )
    if name == "messages.copy":
        return _copy_shape(
            _message_path(_text(data, "message_id")),
            _text(data, "destination_folder_id"),
        )
    if name == "messages.move":
        return _move_shape(
            _message_path(_text(data, "message_id")),
            _text(data, "destination_folder_id"),
        )
    if name == "messages.trash":
        return _RequestShape(
            _message_path(_text(data, "message_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "messages.restore":
        return _move_shape(
            _message_path(_text(data, "message_id")),
            _optional_text(data, "parent_folder_id") or "inbox",
        )
    if name == "messages.purge":
        return _permanent_delete_shape(_message_path(_text(data, "message_id")))
    raise ValidationError("Microsoft Graph mail operation is not implemented")


def _calendar_shape(name: str, data: dict[str, object]) -> _RequestShape:
    if name == "calendars.list":
        return _RequestShape(f"{_ROOT}/calendars", ConnectorMethod.GET, query=_calendar_query(data))
    if name == "calendars.get":
        return _RequestShape(_calendar_path(_text(data, "calendar_id")), ConnectorMethod.GET)
    if name == "events.list":
        return _RequestShape(
            f"{_calendar_path(_text(data, 'calendar_id'))}/events",
            ConnectorMethod.GET,
            query=_event_query(data),
        )
    if name == "events.get":
        return _RequestShape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id")),
            ConnectorMethod.GET,
        )
    if name == "events.window":
        return _window_shape(data, "calendarView")
    if name == "events.instances":
        return _window_shape(data, "instances", event_id=_text(data, "event_id"))
    if name == "freebusy.query":
        time_zone = _time_zone(data)
        return _RequestShape(
            f"{_ROOT}/calendar/getSchedule",
            ConnectorMethod.POST,
            json_body={
                "endTime": _graph_time({"date_time": data["end"], "time_zone": time_zone}),
                "schedules": _strings(data["attendees"]),
                "startTime": _graph_time({"date_time": data["start"], "time_zone": time_zone}),
            },
            time_zone=_optional_text(data, "time_zone"),
        )
    if name == "attachments.list":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments",
            ConnectorMethod.GET,
        )
    if name == "attachments.get":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.GET,
        )
    if name == "calendars.create":
        return _RequestShape(
            f"{_ROOT}/calendars",
            ConnectorMethod.POST,
            json_body={"name": _text(data, "name")},
            expected_statuses=frozenset({201}),
        )
    if name == "calendars.update":
        return _RequestShape(
            _calendar_path(_text(data, "calendar_id")),
            ConnectorMethod.PATCH,
            json_body=_only(data, {"color": "color", "name": "name"}),
        )
    if name == "calendars.delete":
        return _RequestShape(
            _calendar_path(_text(data, "calendar_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "calendars.purge":
        return _permanent_delete_shape(_calendar_path(_text(data, "calendar_id")))
    if name == "events.create":
        return _RequestShape(
            f"{_calendar_path(_text(data, 'calendar_id'))}/events",
            ConnectorMethod.POST,
            json_body=_event_body(data),
            expected_statuses=frozenset({201}),
        )
    if name == "events.update":
        return _RequestShape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id")),
            ConnectorMethod.PATCH,
            json_body=_event_body(data),
        )
    if name == "events.delete":
        return _RequestShape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "events.cancel":
        return _event_response_shape(data, "cancel")
    if name == "events.accept":
        return _event_response_shape(data, "accept")
    if name == "events.tentative":
        return _event_response_shape(data, "tentativelyAccept")
    if name == "events.decline":
        return _event_response_shape(data, "decline")
    if name == "events.forward":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/forward",
            ConnectorMethod.POST,
            json_body={
                "comment": _optional_text(data, "comment") or "",
                "toRecipients": _graph_recipients(data["recipients"]),
            },
            expected_statuses=frozenset({202}),
        )
    if name == "attachments.add":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments",
            ConnectorMethod.POST,
            json_body=_graph_attachment(data["attachment"]),
            expected_statuses=frozenset({201}),
        )
    if name == "attachments.delete":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "events.purge":
        return _permanent_delete_shape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id"))
        )
    raise ValidationError("Microsoft Graph calendar operation is not implemented")


def _folder_path(folder_id: str | None) -> str:
    if folder_id is None:
        return f"{_ROOT}/mailFolders"
    return f"{_ROOT}/mailFolders/{_segment(folder_id)}"


def _message_path(message_id: str) -> str:
    return f"{_ROOT}/messages/{_segment(message_id)}"


def _calendar_path(calendar_id: str) -> str:
    if calendar_id == _PRIMARY_CALENDAR_ALIAS:
        return f"{_ROOT}/calendar"
    return f"{_ROOT}/calendars/{_segment(calendar_id)}"


def _event_path(calendar_id: str, event_id: str) -> str:
    return f"{_calendar_path(calendar_id)}/events/{_segment(event_id)}"


def _segment(value: str) -> str:
    return quote(value, safe="")


def _move_shape(path: str, destination_id: str) -> _RequestShape:
    return _RequestShape(
        f"{path}/move",
        ConnectorMethod.POST,
        json_body={"destinationId": destination_id},
        expected_statuses=frozenset({201}),
    )


def _copy_shape(path: str, destination_id: str) -> _RequestShape:
    return _RequestShape(
        f"{path}/copy",
        ConnectorMethod.POST,
        json_body={"destinationId": destination_id},
        expected_statuses=frozenset({201}),
    )


def _permanent_delete_shape(path: str) -> _RequestShape:
    return _RequestShape(
        f"{path}/permanentDelete",
        ConnectorMethod.POST,
        expected_statuses=frozenset({204}),
    )


def _draft_reply_shape(data: dict[str, object], action: str) -> _RequestShape:
    return _RequestShape(
        f"{_message_path(_text(data, 'message_id'))}/{action}",
        ConnectorMethod.POST,
        json_body={"comment": _optional_text(data, "comment") or ""},
        expected_statuses=frozenset({201}),
    )


def _event_response_shape(data: dict[str, object], action: str) -> _RequestShape:
    return _RequestShape(
        f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/{action}",
        ConnectorMethod.POST,
        json_body={"comment": _optional_text(data, "comment") or ""},
        expected_statuses=frozenset({202}),
    )


def _window_shape(
    data: dict[str, object],
    suffix: str,
    event_id: str | None = None,
) -> _RequestShape:
    calendar_id = _text(data, "calendar_id")
    if event_id is None:
        path = f"{_calendar_path(calendar_id)}/{suffix}"
    else:
        path = f"{_event_path(calendar_id, event_id)}/{suffix}"
    return _RequestShape(
        path,
        ConnectorMethod.GET,
        query=(
            ("startDateTime", _text(data, "start")),
            ("endDateTime", _text(data, "end")),
        ),
        time_zone=_optional_text(data, "time_zone"),
    )


def _folder_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return _list_query(data, {"display_name": "displayName"})


def _message_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    query = list(
        _list_query(
            data,
            {
                "last_modified_at": "lastModifiedDateTime",
                "received_at": "receivedDateTime",
                "sent_at": "sentDateTime",
                "subject": "subject",
            },
        )
    )
    if "is_read" in data:
        query.append(("$filter", f"isRead eq {str(data['is_read']).lower()}"))
    return tuple(query)


def _calendar_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return _list_query(data, {"last_modified_at": "lastModifiedDateTime", "name": "name"})


def _event_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return _list_query(
        data,
        {
            "end_at": "end/dateTime",
            "last_modified_at": "lastModifiedDateTime",
            "start_at": "start/dateTime",
            "subject": "subject",
        },
    )


def _list_query(
    data: dict[str, object], order_names: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "page_size" in data:
        query.append(("$top", str(_integer(data["page_size"]))))
    if "search" in data:
        query.append(("$search", _text(data, "search")))
    if "order_by" in data:
        order = order_names[_text(data, "order_by")]
        direction = _optional_text(data, "sort_direction") or "ascending"
        query.append(("$orderby", f"{order} {'asc' if direction == 'ascending' else 'desc'}"))
    return tuple(query)


def _headers(data: dict[str, object], *, time_zone: str | None) -> dict[str, str]:
    prefer = 'IdType="ImmutableId"'
    if time_zone is not None:
        prefer += f', outlook.timezone="{_preference_time_zone(time_zone)}"'
    headers = {"Prefer": prefer}
    change_key = _optional_text(data, "change_key")
    if change_key is not None:
        headers["If-Match"] = change_key
    return headers


def _preference_time_zone(value: str) -> str:
    if not value or any(character in value for character in ('"', ",", "\r", "\n")):
        raise ValidationError("connector time zone is invalid")
    return value


def _mail_message(data: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {}
    if "to_recipients" in data:
        body["toRecipients"] = _graph_recipients(data["to_recipients"])
    if "cc_recipients" in data:
        body["ccRecipients"] = _graph_recipients(data["cc_recipients"])
    if "bcc_recipients" in data:
        body["bccRecipients"] = _graph_recipients(data["bcc_recipients"])
    if "subject" in data:
        body["subject"] = _text(data, "subject")
    if "body" in data:
        body["body"] = _graph_body(data["body"])
    if "importance" in data:
        body["importance"] = _text(data, "importance")
    if "categories" in data:
        body["categories"] = _strings(data["categories"])
    return body


def _message_update_body(data: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {}
    if "is_read" in data:
        body["isRead"] = _boolean(data["is_read"])
    if "categories" in data:
        body["categories"] = _strings(data["categories"])
    if "importance" in data:
        body["importance"] = _text(data, "importance")
    if "follow_up" in data:
        body["flag"] = {"flagStatus": _FOLLOW_UP_STATUS[_text(data, "follow_up")]}
    return body


def _event_body(data: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {}
    if "transaction_id" in data:
        body["transactionId"] = _text(data, "transaction_id")
    if "subject" in data:
        body["subject"] = _text(data, "subject")
    if "body" in data:
        body["body"] = _graph_body(data["body"])
    if "start" in data:
        body["start"] = _graph_time(data["start"])
    if "end" in data:
        body["end"] = _graph_time(data["end"])
    if "location" in data:
        body["location"] = {"displayName": _text(data, "location")}
    if "attendees" in data:
        body["attendees"] = _graph_attendees(data["attendees"])
    if "recurrence" in data:
        body["recurrence"] = _graph_recurrence(data["recurrence"])
    if "categories" in data:
        body["categories"] = _strings(data["categories"])
    if "importance" in data:
        body["importance"] = _text(data, "importance")
    if "is_all_day" in data:
        body["isAllDay"] = _boolean(data["is_all_day"])
    if "is_reminder_on" in data:
        body["isReminderOn"] = _boolean(data["is_reminder_on"])
    if "reminder_minutes_before_start" in data:
        body["reminderMinutesBeforeStart"] = _integer(data["reminder_minutes_before_start"])
    if "response_requested" in data:
        body["responseRequested"] = _boolean(data["response_requested"])
    if "sensitivity" in data:
        body["sensitivity"] = _text(data, "sensitivity")
    if "show_as" in data:
        body["showAs"] = _SHOW_AS[_text(data, "show_as")]
    return body


def _only(data: dict[str, object], names: Mapping[str, str]) -> dict[str, object]:
    return {target: _text(data, source) for source, target in names.items() if source in data}


def _graph_body(value: object) -> dict[str, object]:
    body = _mapping(value)
    content_type = _text(body, "content_type")
    return {
        "content": _text(body, "content"),
        "contentType": "Text" if content_type == "text" else "HTML",
    }


def _graph_time(value: object) -> dict[str, object]:
    time = _mapping(value)
    return {"dateTime": _text(time, "date_time"), "timeZone": _text(time, "time_zone")}


def _graph_recipients(value: object) -> list[dict[str, object]]:
    recipients: list[dict[str, object]] = []
    for item in _items(value):
        recipient = _mapping(item)
        address: dict[str, object] = {"address": _text(recipient, "email")}
        name = _optional_text(recipient, "name")
        if name is not None:
            address["name"] = name
        recipients.append({"emailAddress": address})
    return recipients


def _graph_attendees(value: object) -> list[dict[str, object]]:
    attendees: list[dict[str, object]] = []
    for item in _items(value):
        attendee = _mapping(item)
        address: dict[str, object] = {"address": _text(attendee, "email")}
        name = _optional_text(attendee, "name")
        if name is not None:
            address["name"] = name
        attendees.append({"emailAddress": address, "type": _text(attendee, "type")})
    return attendees


def _graph_attachment(value: object) -> dict[str, object]:
    attachment = _mapping(value)
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "contentBytes": _text(attachment, "content_base64"),
        "contentType": _text(attachment, "content_type"),
        "name": _text(attachment, "name"),
    }


def _graph_recurrence(value: object) -> dict[str, object]:
    recurrence = _mapping(value)
    pattern = _mapping(recurrence["pattern"])
    event_range = _mapping(recurrence["range"])
    graph_pattern: dict[str, object] = {
        "interval": _integer(pattern["interval"]),
        "type": _text(pattern, "type"),
    }
    graph_range: dict[str, object] = {
        "startDate": _text(event_range, "start_date"),
        "type": _text(event_range, "type"),
    }
    for source, target in (
        ("day_of_month", "dayOfMonth"),
        ("index", "index"),
        ("month", "month"),
    ):
        if source in pattern:
            graph_pattern[target] = pattern[source]
    if "days_of_week" in pattern:
        graph_pattern["daysOfWeek"] = _strings(pattern["days_of_week"])
    if "end_date" in event_range:
        graph_range["endDate"] = _text(event_range, "end_date")
    if "number_of_occurrences" in event_range:
        graph_range["numberOfOccurrences"] = _integer(event_range["number_of_occurrences"])
    return {"pattern": graph_pattern, "range": graph_range}


def _event_mutation_is_outward(data: dict[str, object]) -> bool:
    calendar_id = _optional_text(data, "calendar_id")
    if calendar_id != _PRIMARY_CALENDAR_ALIAS:
        return True
    attendees = data.get("attendees")
    return isinstance(attendees, list) and bool(attendees)


def _result(response: ConnectorResponse, *, path: str, mime: bool) -> ConnectorAdapterResult:
    if mime:
        return ConnectorAdapterResult(
            {"content_base64": base64.b64encode(response.body).decode("ascii")}
        )
    value = response.json()
    if not isinstance(value, Mapping):
        return ConnectorAdapterResult({} if value is None else value)
    payload: dict[str, object] = {}
    next_link: object | None = None
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConnectorProviderError(
                origin=_ORIGIN,
                status=response.status,
                code="invalid_json_response",
            )
        if key == "@odata.nextLink":
            next_link = item
        else:
            payload[key] = item
    continuation = _next_link(next_link, path=path, status=response.status)
    return ConnectorAdapterResult(payload, continuation=continuation)


def _next_link(value: object | None, *, path: str, status: int) -> object | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 16_384:
        raise ConnectorProviderError(origin=_ORIGIN, status=status, code="invalid_next_link")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=status,
            code="invalid_next_link",
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _GRAPH_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or parsed.path != path
    ):
        raise ConnectorProviderError(origin=_ORIGIN, status=status, code="invalid_next_link")
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=128,
        )
    except ValueError as exc:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=status,
            code="invalid_next_link",
        ) from exc
    if not pairs or any(key not in _CONTINUATION_KEYS for key, _ in pairs):
        raise ConnectorProviderError(origin=_ORIGIN, status=status, code="invalid_next_link")
    return {"path": path, "query": [[key, item] for key, item in pairs]}


def _continuation_query(value: object, *, path: str) -> tuple[tuple[str, str], ...]:
    continuation = _mapping(value)
    if set(continuation) != {"path", "query"} or continuation.get("path") != path:
        raise ValidationError("connector continuation does not match the fixed Graph route")
    query = _items(continuation["query"])
    if not query or len(query) > 128:
        raise ValidationError("connector continuation is invalid")
    pairs: list[tuple[str, str]] = []
    for item in query:
        pair = _items(item)
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], str):
            raise ValidationError("connector continuation is invalid")
        if pair[0] not in _CONTINUATION_KEYS:
            raise ValidationError("connector continuation is invalid")
        pairs.append((pair[0], pair[1]))
    return tuple(pairs)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValidationError("connector operation input is invalid")
    return value


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValidationError("connector operation input is invalid")
    return value


def _text(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise ValidationError("connector operation input is invalid")
    return value


def _optional_text(data: Mapping[str, object], name: str) -> str | None:
    if name not in data:
        return None
    return _text(data, name)


def _strings(value: object) -> list[str]:
    values = _items(value)
    if any(not isinstance(item, str) for item in values):
        raise ValidationError("connector operation input is invalid")
    return [item for item in values if isinstance(item, str)]


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValidationError("connector operation input is invalid")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValidationError("connector operation input is invalid")
    return value


def _time_zone(data: dict[str, object]) -> str:
    return _optional_text(data, "time_zone") or "UTC"
