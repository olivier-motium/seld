"""Declarative Microsoft connector operation catalog."""

from __future__ import annotations

from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationSpec


def _object(properties: dict[str, object], *, required: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
        "type": "object",
    }


def _string(
    max_length: int,
    *,
    min_length: int = 0,
    pattern: str | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {"maxLength": max_length, "type": "string"}
    if min_length:
        schema["minLength"] = min_length
    if pattern is not None:
        schema["pattern"] = pattern
    return schema


def _array(
    items: dict[str, object], *, max_items: int = 64, min_items: int = 0
) -> dict[str, object]:
    schema: dict[str, object] = {"items": items, "maxItems": max_items, "type": "array"}
    if min_items:
        schema["minItems"] = min_items
    return schema


def _enum(*values: str) -> dict[str, object]:
    return {"enum": list(values), "type": "string"}


def _operation(
    provider: str,
    mode: ConnectorMode,
    name: str,
    effect: ConnectorEffect,
    scopes: tuple[frozenset[str], ...],
    input_schema: dict[str, object],
) -> OperationSpec:
    return OperationSpec(
        provider=provider,
        mode=mode,
        name=name,
        effect=effect,
        endpoint=name,
        required_scopes=scopes,
        input_schema=input_schema,
    )


_ID = _string(1_024, min_length=1)
_CHANGE_KEY = _string(2_048, min_length=1)
_SHORT_TEXT = _string(1_024)
_NAME = _string(256, min_length=1)
_BODY_TEXT = _string(200_000)
_DATE_TIME = _string(128, min_length=1)
_DATE = _string(32, min_length=1)
_TIME_ZONE = _string(128, min_length=1)
_EMAIL = _string(320, min_length=3, pattern=r"^[^@\s]+@[^@\s]+$")
_PAGE_SIZE = {"maximum": 100, "minimum": 1, "type": "integer"}
_IMPORTANCE = _enum("low", "normal", "high")
_BODY = _object(
    {
        "content": _BODY_TEXT,
        "content_type": _enum("text", "html"),
    },
    required=("content", "content_type"),
)
_RECIPIENT = _object(
    {
        "email": _EMAIL,
        "name": _string(256),
    },
    required=("email",),
)
_RECIPIENTS = _array(_RECIPIENT, max_items=100)
_ATTENDEES = _array(
    _object(
        {
            "email": _EMAIL,
            "name": _string(256),
            "type": _enum("required", "optional", "resource"),
        },
        required=("email", "type"),
    ),
    max_items=100,
)
_CATEGORIES = _array(_string(128, min_length=1), max_items=32)
_ATTACHMENT = _object(
    {
        "content_base64": _string(
            240_000,
            min_length=1,
            pattern=r"^[A-Za-z0-9+/]+={0,2}$",
        ),
        "content_type": _string(255, min_length=1),
        "name": _NAME,
    },
    required=("name", "content_type", "content_base64"),
)
_EVENT_TIME = _object(
    {
        "date_time": _DATE_TIME,
        "time_zone": _TIME_ZONE,
    },
    required=("date_time", "time_zone"),
)
_RECURRENCE = _object(
    {
        "pattern": _object(
            {
                "day_of_month": {"maximum": 31, "minimum": 1, "type": "integer"},
                "days_of_week": _array(
                    _enum(
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    ),
                    max_items=7,
                ),
                "index": _enum("first", "second", "third", "fourth", "last"),
                "interval": {"maximum": 365, "minimum": 1, "type": "integer"},
                "month": {"maximum": 12, "minimum": 1, "type": "integer"},
                "type": _enum(
                    "daily",
                    "weekly",
                    "absolute_monthly",
                    "relative_monthly",
                    "absolute_yearly",
                    "relative_yearly",
                ),
            },
            required=("type", "interval"),
        ),
        "range": _object(
            {
                "end_date": _DATE,
                "number_of_occurrences": {"maximum": 999, "minimum": 1, "type": "integer"},
                "start_date": _DATE,
                "type": _enum("no_end", "end_date", "numbered"),
            },
            required=("type", "start_date"),
        ),
    },
    required=("pattern", "range"),
)

_MAIL_READ_SCOPES = (frozenset({"Mail.Read"}), frozenset({"Mail.ReadWrite"}))
_MAIL_WRITE_SCOPES = (frozenset({"Mail.ReadWrite"}),)
_MAIL_SEND_SCOPES = (frozenset({"Mail.ReadWrite", "Mail.Send"}),)
_CALENDAR_READ_SCOPES = (frozenset({"Calendars.Read"}), frozenset({"Calendars.ReadWrite"}))
_CALENDAR_WRITE_SCOPES = (frozenset({"Calendars.ReadWrite"}),)

_FOLDERS_LIST = _object(
    {
        "order_by": _enum("display_name"),
        "page_size": _PAGE_SIZE,
        "parent_folder_id": _ID,
        "search": _string(4_096),
    }
)
_MESSAGES_LIST = _object(
    {
        "folder_id": _ID,
        "is_read": {"type": "boolean"},
        "order_by": _enum("received_at", "sent_at", "subject", "last_modified_at"),
        "page_size": _PAGE_SIZE,
        "search": _string(4_096),
        "sort_direction": _enum("ascending", "descending"),
    }
)
_CALENDARS_LIST = _object(
    {
        "order_by": _enum("name", "last_modified_at"),
        "page_size": _PAGE_SIZE,
        "search": _string(4_096),
    }
)
_EVENTS_LIST = _object(
    {
        "calendar_id": _ID,
        "order_by": _enum("start_at", "end_at", "subject", "last_modified_at"),
        "page_size": _PAGE_SIZE,
        "search": _string(4_096),
        "sort_direction": _enum("ascending", "descending"),
    },
    required=("calendar_id",),
)


MICROSOFT_OPERATIONS: tuple[OperationSpec, ...] = (
    _operation(
        "outlook_mail",
        ConnectorMode.READ,
        "folders.list",
        ConnectorEffect.READ,
        _MAIL_READ_SCOPES,
        _FOLDERS_LIST,
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.READ,
        "folders.get",
        ConnectorEffect.READ,
        _MAIL_READ_SCOPES,
        _object({"folder_id": _ID}, required=("folder_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.READ,
        "messages.list",
        ConnectorEffect.READ,
        _MAIL_READ_SCOPES,
        _MESSAGES_LIST,
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.READ,
        "messages.get",
        ConnectorEffect.READ,
        _MAIL_READ_SCOPES,
        _object({"message_id": _ID}, required=("message_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.READ,
        "messages.mime",
        ConnectorEffect.READ,
        _MAIL_READ_SCOPES,
        _object({"message_id": _ID}, required=("message_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.READ,
        "attachments.list",
        ConnectorEffect.READ,
        _MAIL_READ_SCOPES,
        _object({"message_id": _ID}, required=("message_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.READ,
        "attachments.get",
        ConnectorEffect.READ,
        _MAIL_READ_SCOPES,
        _object(
            {"attachment_id": _ID, "message_id": _ID},
            required=("message_id", "attachment_id"),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "folders.create",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"display_name": _NAME, "parent_folder_id": _ID},
            required=("display_name",),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "folders.update",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"change_key": _CHANGE_KEY, "display_name": _NAME, "folder_id": _ID},
            required=("folder_id",),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "folders.move",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {
                "change_key": _CHANGE_KEY,
                "destination_folder_id": _ID,
                "folder_id": _ID,
            },
            required=("folder_id", "destination_folder_id"),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "folders.trash",
        ConnectorEffect.DESTRUCTIVE,
        _MAIL_WRITE_SCOPES,
        _object({"change_key": _CHANGE_KEY, "folder_id": _ID}, required=("folder_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "folders.restore",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"change_key": _CHANGE_KEY, "folder_id": _ID, "parent_folder_id": _ID},
            required=("folder_id",),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "folders.purge",
        ConnectorEffect.PERMANENT,
        _MAIL_WRITE_SCOPES,
        _object({"change_key": _CHANGE_KEY, "folder_id": _ID}, required=("folder_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.create",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {
                "bcc_recipients": _RECIPIENTS,
                "body": _BODY,
                "categories": _CATEGORIES,
                "cc_recipients": _RECIPIENTS,
                "importance": _IMPORTANCE,
                "subject": _string(4_096),
                "to_recipients": _RECIPIENTS,
            }
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.update",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {
                "bcc_recipients": _RECIPIENTS,
                "body": _BODY,
                "categories": _CATEGORIES,
                "cc_recipients": _RECIPIENTS,
                "change_key": _CHANGE_KEY,
                "importance": _IMPORTANCE,
                "message_id": _ID,
                "subject": _string(4_096),
                "to_recipients": _RECIPIENTS,
            },
            required=("message_id",),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.reply",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"comment": _BODY_TEXT, "message_id": _ID},
            required=("message_id",),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.reply_all",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"comment": _BODY_TEXT, "message_id": _ID},
            required=("message_id",),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.forward",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"comment": _BODY_TEXT, "message_id": _ID, "to_recipients": _RECIPIENTS},
            required=("message_id", "to_recipients"),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "attachments.add",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"attachment": _ATTACHMENT, "change_key": _CHANGE_KEY, "message_id": _ID},
            required=("message_id", "attachment"),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "attachments.delete",
        ConnectorEffect.DESTRUCTIVE,
        _MAIL_WRITE_SCOPES,
        _object(
            {"attachment_id": _ID, "change_key": _CHANGE_KEY, "message_id": _ID},
            required=("message_id", "attachment_id"),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.send",
        ConnectorEffect.OUTWARD,
        _MAIL_SEND_SCOPES,
        _object({"change_key": _CHANGE_KEY, "message_id": _ID}, required=("message_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "messages.copy",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {
                "change_key": _CHANGE_KEY,
                "destination_folder_id": _ID,
                "message_id": _ID,
            },
            required=("message_id", "destination_folder_id"),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "messages.move",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {
                "change_key": _CHANGE_KEY,
                "destination_folder_id": _ID,
                "message_id": _ID,
            },
            required=("message_id", "destination_folder_id"),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "messages.trash",
        ConnectorEffect.DESTRUCTIVE,
        _MAIL_WRITE_SCOPES,
        _object({"change_key": _CHANGE_KEY, "message_id": _ID}, required=("message_id",)),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "messages.restore",
        ConnectorEffect.SAFE_MUTATION,
        _MAIL_WRITE_SCOPES,
        _object(
            {"change_key": _CHANGE_KEY, "message_id": _ID, "parent_folder_id": _ID},
            required=("message_id",),
        ),
    ),
    _operation(
        "outlook_mail",
        ConnectorMode.WRITE,
        "messages.purge",
        ConnectorEffect.PERMANENT,
        _MAIL_WRITE_SCOPES,
        _object({"change_key": _CHANGE_KEY, "message_id": _ID}, required=("message_id",)),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "calendars.list",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _CALENDARS_LIST,
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "calendars.get",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _object({"calendar_id": _ID}, required=("calendar_id",)),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "events.list",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _EVENTS_LIST,
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "events.get",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _object(
            {"calendar_id": _ID, "event_id": _ID},
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "events.window",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _object(
            {
                "calendar_id": _ID,
                "end": _DATE_TIME,
                "start": _DATE_TIME,
                "time_zone": _TIME_ZONE,
            },
            required=("calendar_id", "start", "end"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "events.instances",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _object(
            {
                "calendar_id": _ID,
                "end": _DATE_TIME,
                "event_id": _ID,
                "start": _DATE_TIME,
                "time_zone": _TIME_ZONE,
            },
            required=("calendar_id", "event_id", "start", "end"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "freebusy.query",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _object(
            {
                "attendees": _array(_EMAIL, max_items=100, min_items=1),
                "end": _DATE_TIME,
                "start": _DATE_TIME,
                "time_zone": _TIME_ZONE,
            },
            required=("attendees", "start", "end"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "attachments.list",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _object(
            {"calendar_id": _ID, "event_id": _ID},
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.READ,
        "attachments.get",
        ConnectorEffect.READ,
        _CALENDAR_READ_SCOPES,
        _object(
            {"attachment_id": _ID, "calendar_id": _ID, "event_id": _ID},
            required=("calendar_id", "event_id", "attachment_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "calendars.create",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_WRITE_SCOPES,
        _object({"name": _NAME}, required=("name",)),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "calendars.update",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "change_key": _CHANGE_KEY,
                "color": _enum(
                    "auto",
                    "light_blue",
                    "light_green",
                    "light_orange",
                    "light_gray",
                    "light_yellow",
                    "light_teal",
                    "light_pink",
                    "light_brown",
                    "light_red",
                    "max_color",
                ),
                "calendar_id": _ID,
                "name": _NAME,
            },
            required=("calendar_id",),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "calendars.delete",
        ConnectorEffect.DESTRUCTIVE,
        _CALENDAR_WRITE_SCOPES,
        _object({"calendar_id": _ID, "change_key": _CHANGE_KEY}, required=("calendar_id",)),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "calendars.purge",
        ConnectorEffect.PERMANENT,
        _CALENDAR_WRITE_SCOPES,
        _object({"calendar_id": _ID, "change_key": _CHANGE_KEY}, required=("calendar_id",)),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.create",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "attendees": _ATTENDEES,
                "body": _BODY,
                "calendar_id": _ID,
                "categories": _CATEGORIES,
                "end": _EVENT_TIME,
                "is_reminder_on": {"type": "boolean"},
                "location": _SHORT_TEXT,
                "recurrence": _RECURRENCE,
                "reminder_minutes_before_start": {
                    "maximum": 10_080,
                    "minimum": 0,
                    "type": "integer",
                },
                "start": _EVENT_TIME,
                "subject": _string(4_096),
                "transaction_id": _string(256, min_length=1),
            },
            required=("calendar_id", "transaction_id", "subject", "body", "start", "end"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.update",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "attendees": _ATTENDEES,
                "body": _BODY,
                "calendar_id": _ID,
                "categories": _CATEGORIES,
                "change_key": _CHANGE_KEY,
                "end": _EVENT_TIME,
                "event_id": _ID,
                "is_reminder_on": {"type": "boolean"},
                "location": _SHORT_TEXT,
                "recurrence": _RECURRENCE,
                "reminder_minutes_before_start": {
                    "maximum": 10_080,
                    "minimum": 0,
                    "type": "integer",
                },
                "start": _EVENT_TIME,
                "subject": _string(4_096),
            },
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.delete",
        ConnectorEffect.DESTRUCTIVE,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {"calendar_id": _ID, "change_key": _CHANGE_KEY, "event_id": _ID},
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.cancel",
        ConnectorEffect.DESTRUCTIVE,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _ID,
                "change_key": _CHANGE_KEY,
                "comment": _BODY_TEXT,
                "event_id": _ID,
            },
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.accept",
        ConnectorEffect.OUTWARD,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _ID,
                "change_key": _CHANGE_KEY,
                "comment": _BODY_TEXT,
                "event_id": _ID,
            },
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.tentative",
        ConnectorEffect.OUTWARD,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _ID,
                "change_key": _CHANGE_KEY,
                "comment": _BODY_TEXT,
                "event_id": _ID,
            },
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.decline",
        ConnectorEffect.OUTWARD,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _ID,
                "change_key": _CHANGE_KEY,
                "comment": _BODY_TEXT,
                "event_id": _ID,
            },
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.forward",
        ConnectorEffect.OUTWARD,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _ID,
                "change_key": _CHANGE_KEY,
                "comment": _BODY_TEXT,
                "event_id": _ID,
                "recipients": _RECIPIENTS,
            },
            required=("calendar_id", "event_id", "recipients"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "attachments.add",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "attachment": _ATTACHMENT,
                "calendar_id": _ID,
                "change_key": _CHANGE_KEY,
                "event_id": _ID,
            },
            required=("calendar_id", "event_id", "attachment"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "attachments.delete",
        ConnectorEffect.DESTRUCTIVE,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {
                "attachment_id": _ID,
                "calendar_id": _ID,
                "change_key": _CHANGE_KEY,
                "event_id": _ID,
            },
            required=("calendar_id", "event_id", "attachment_id"),
        ),
    ),
    _operation(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.purge",
        ConnectorEffect.PERMANENT,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {"calendar_id": _ID, "change_key": _CHANGE_KEY, "event_id": _ID},
            required=("calendar_id", "event_id"),
        ),
    ),
)
