"""Bounded declarative Google connector operation catalog."""

from __future__ import annotations

from typing import Final

from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationSpec

_GMAIL_READONLY: Final = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_MODIFY: Final = "https://www.googleapis.com/auth/gmail.modify"
_MAIL: Final = "https://mail.google.com/"

_CALENDAR_LIST_READONLY: Final = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
_CALENDAR_EVENTS_READONLY: Final = "https://www.googleapis.com/auth/calendar.events.readonly"
_CALENDAR_EVENTS: Final = "https://www.googleapis.com/auth/calendar.events"
_CALENDAR_FREEBUSY: Final = "https://www.googleapis.com/auth/calendar.freebusy"
_CALENDAR_CALENDARS: Final = "https://www.googleapis.com/auth/calendar.calendars"
_CALENDAR: Final = "https://www.googleapis.com/auth/calendar"

_DRIVE_METADATA_READONLY: Final = "https://www.googleapis.com/auth/drive.metadata.readonly"
_DRIVE_READONLY: Final = "https://www.googleapis.com/auth/drive.readonly"
_DRIVE_FILE: Final = "https://www.googleapis.com/auth/drive.file"
_DRIVE: Final = "https://www.googleapis.com/auth/drive"


def _object(properties: dict[str, object], *, required: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
        "type": "object",
    }


def _text(maximum: int = 4_096, *, minimum: int = 1) -> dict[str, object]:
    return {"maxLength": maximum, "minLength": minimum, "type": "string"}


def _id() -> dict[str, object]:
    return _text(512)


def _array(items: dict[str, object], *, maximum: int = 32, minimum: int = 0) -> dict[str, object]:
    return {"items": items, "maxItems": maximum, "minItems": minimum, "type": "array"}


def _scopes(*alternatives: str) -> tuple[frozenset[str], ...]:
    return tuple(frozenset({scope}) for scope in alternatives)


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


def _email_addresses() -> dict[str, object]:
    return _array(_text(512), maximum=64)


def _attachment() -> dict[str, object]:
    return _object(
        {
            "content_base64": _text(240_000),
            "filename": _text(512),
            "mime_type": _text(256),
        },
        required=("content_base64", "filename", "mime_type"),
    )


def _mail_fields() -> dict[str, object]:
    return {
        "attachments": _array(_attachment(), maximum=16),
        "bcc": _email_addresses(),
        "cc": _email_addresses(),
        "html_body": _text(200_000, minimum=0),
        "reply_to_message_id": _id(),
        "subject": _text(998, minimum=0),
        "text_body": _text(200_000, minimum=0),
        "thread_id": _id(),
        "to": _email_addresses(),
    }


def _mail_list() -> dict[str, object]:
    return _object(
        {
            "include_spam_trash": {"type": "boolean"},
            "label_ids": _array(_id(), maximum=64),
            "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
            "query": _text(8_192, minimum=0),
        }
    )


def _event_time() -> dict[str, object]:
    return {
        "oneOf": [
            _object(
                {"date": _text(32), "time_zone": _text(128)},
                required=("date", "time_zone"),
            ),
            _object(
                {"date_time": _text(64), "time_zone": _text(128)},
                required=("date_time", "time_zone"),
            ),
        ]
    }


def _event_fields() -> dict[str, object]:
    return {
        "attendee_emails": _email_addresses(),
        "description": _text(200_000, minimum=0),
        "end": _event_time(),
        "event_id": _id(),
        "location": _text(4_096, minimum=0),
        "recurrence": _array(_text(2_048), maximum=32),
        "send_updates": {
            "enum": ["all", "externalOnly", "none"],
            "type": "string",
        },
        "start": _event_time(),
        "summary": _text(4_096, minimum=0),
        "visibility": {
            "enum": ["default", "private", "public", "confidential"],
            "type": "string",
        },
    }


def _calendar_fields() -> dict[str, object]:
    return {
        "description": _text(16_384, minimum=0),
        "location": _text(4_096, minimum=0),
        "summary": _text(4_096, minimum=0),
        "time_zone": _text(128),
    }


def _app_property() -> dict[str, object]:
    return _object({"key": _text(124), "value": _text(4_096)}, required=("key", "value"))


def _file_fields() -> dict[str, object]:
    return {
        "app_properties": _array(_app_property(), maximum=64),
        "content_base64": _text(240_000),
        "description": _text(32_768, minimum=0),
        "mime_type": _text(256),
        "name": _text(1_024),
        "parent_ids": _array(_id(), maximum=32),
    }


def _drive_list() -> dict[str, object]:
    return _object(
        {
            "include_trashed": {"type": "boolean"},
            "mime_type": _text(256),
            "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
            "parent_id": _id(),
            "query": _text(8_192, minimum=0),
        }
    )


def _comment_fields() -> dict[str, object]:
    return {
        "content": _text(200_000),
        "quoted_file_content": _text(32_768, minimum=0),
    }


_GMAIL_READ_SCOPES: Final = _scopes(_GMAIL_READONLY, _GMAIL_MODIFY, _MAIL)
_GMAIL_WRITE_SCOPES: Final = _scopes(_GMAIL_MODIFY, _MAIL)
_GMAIL_PURGE_SCOPES: Final = _scopes(_MAIL)
_CALENDAR_LIST_SCOPES: Final = _scopes(_CALENDAR_LIST_READONLY, _CALENDAR)
_CALENDAR_EVENT_READ_SCOPES: Final = _scopes(
    _CALENDAR_EVENTS_READONLY,
    _CALENDAR_EVENTS,
    _CALENDAR,
)
_CALENDAR_FREEBUSY_SCOPES: Final = _scopes(_CALENDAR_FREEBUSY, _CALENDAR)
_CALENDAR_WRITE_SCOPES: Final = _scopes(_CALENDAR_CALENDARS, _CALENDAR)
_CALENDAR_EVENT_WRITE_SCOPES: Final = _scopes(_CALENDAR_EVENTS, _CALENDAR)
_DRIVE_METADATA_SCOPES: Final = _scopes(
    _DRIVE_METADATA_READONLY,
    _DRIVE_READONLY,
    _DRIVE_FILE,
    _DRIVE,
)
_DRIVE_CONTENT_SCOPES: Final = _scopes(_DRIVE_READONLY, _DRIVE_FILE, _DRIVE)
_DRIVE_WRITE_SCOPES: Final = _scopes(_DRIVE_FILE, _DRIVE)


GOOGLE_OPERATIONS: Final[tuple[OperationSpec, ...]] = (
    _operation(
        "gmail",
        ConnectorMode.READ,
        "messages.list",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _mail_list(),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "messages.get",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object(
            {
                "format": {"enum": ["full", "metadata", "minimal"], "type": "string"},
                "message_id": _id(),
            },
            required=("message_id",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "attachments.get",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object(
            {"attachment_id": _id(), "message_id": _id()},
            required=("attachment_id", "message_id"),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "threads.list",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _mail_list(),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "threads.get",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object(
            {
                "format": {"enum": ["full", "metadata", "minimal"], "type": "string"},
                "thread_id": _id(),
            },
            required=("thread_id",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "drafts.list",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _mail_list(),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "drafts.get",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object(
            {
                "draft_id": _id(),
                "format": {"enum": ["full", "metadata", "minimal"], "type": "string"},
            },
            required=("draft_id",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "labels.list",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object({}),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "drafts.create",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object(_mail_fields()),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "drafts.update",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object({"draft_id": _id(), **_mail_fields()}, required=("draft_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "drafts.delete",
        ConnectorEffect.PERMANENT,
        _GMAIL_WRITE_SCOPES,
        _object({"draft_id": _id()}, required=("draft_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "drafts.send",
        ConnectorEffect.OUTWARD,
        _GMAIL_WRITE_SCOPES,
        _object({"draft_id": _id()}, required=("draft_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.modify",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object(
            {
                "add_label_ids": _array(_id(), maximum=64),
                "message_id": _id(),
                "remove_label_ids": _array(_id(), maximum=64),
            },
            required=("message_id",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.trash",
        ConnectorEffect.DESTRUCTIVE,
        _GMAIL_WRITE_SCOPES,
        _object({"message_id": _id()}, required=("message_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.restore",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object({"message_id": _id()}, required=("message_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.purge",
        ConnectorEffect.PERMANENT,
        _GMAIL_PURGE_SCOPES,
        _object({"message_id": _id()}, required=("message_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "threads.modify",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object(
            {
                "add_label_ids": _array(_id(), maximum=64),
                "remove_label_ids": _array(_id(), maximum=64),
                "thread_id": _id(),
            },
            required=("thread_id",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "threads.trash",
        ConnectorEffect.DESTRUCTIVE,
        _GMAIL_WRITE_SCOPES,
        _object({"thread_id": _id()}, required=("thread_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "threads.restore",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object({"thread_id": _id()}, required=("thread_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "threads.purge",
        ConnectorEffect.PERMANENT,
        _GMAIL_PURGE_SCOPES,
        _object({"thread_id": _id()}, required=("thread_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "labels.create",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object(
            {
                "label_list_visibility": {"enum": ["labelHide", "labelShow"], "type": "string"},
                "message_list_visibility": {"enum": ["hide", "show"], "type": "string"},
                "name": _text(225),
            },
            required=("name",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "labels.update",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _object(
            {
                "label_id": _id(),
                "label_list_visibility": {"enum": ["labelHide", "labelShow"], "type": "string"},
                "message_list_visibility": {"enum": ["hide", "show"], "type": "string"},
                "name": _text(225),
            },
            required=("label_id",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "labels.delete",
        ConnectorEffect.DESTRUCTIVE,
        _GMAIL_WRITE_SCOPES,
        _object({"label_id": _id()}, required=("label_id",)),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.READ,
        "calendars.list",
        ConnectorEffect.READ,
        _CALENDAR_LIST_SCOPES,
        _object({"page_size": {"maximum": 100, "minimum": 1, "type": "integer"}}),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.READ,
        "calendars.get",
        ConnectorEffect.READ,
        _CALENDAR_LIST_SCOPES,
        _object({"calendar_id": _id()}, required=("calendar_id",)),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.READ,
        "events.list",
        ConnectorEffect.READ,
        _CALENDAR_EVENT_READ_SCOPES,
        _object(
            {
                "calendar_id": _id(),
                "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
                "query": _text(8_192, minimum=0),
                "single_events": {"type": "boolean"},
                "time_max": _text(64),
                "time_min": _text(64),
            },
            required=("calendar_id",),
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.READ,
        "events.get",
        ConnectorEffect.READ,
        _CALENDAR_EVENT_READ_SCOPES,
        _object({"calendar_id": _id(), "event_id": _id()}, required=("calendar_id", "event_id")),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.READ,
        "events.instances",
        ConnectorEffect.READ,
        _CALENDAR_EVENT_READ_SCOPES,
        _object(
            {
                "calendar_id": _id(),
                "event_id": _id(),
                "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
                "time_max": _text(64),
                "time_min": _text(64),
            },
            required=("calendar_id", "event_id"),
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.READ,
        "freebusy.query",
        ConnectorEffect.READ,
        _CALENDAR_FREEBUSY_SCOPES,
        _object(
            {
                "calendar_ids": _array(_id(), maximum=64, minimum=1),
                "time_max": _text(64),
                "time_min": _text(64),
                "time_zone": _text(128),
            },
            required=("calendar_ids", "time_max", "time_min", "time_zone"),
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "calendars.create",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_WRITE_SCOPES,
        _object(_calendar_fields(), required=("summary", "time_zone")),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "calendars.update",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_WRITE_SCOPES,
        _object(
            {"calendar_id": _id(), "etag": _text(1_024), **_calendar_fields()},
            required=("calendar_id", "etag"),
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "calendars.delete",
        ConnectorEffect.PERMANENT,
        _CALENDAR_WRITE_SCOPES,
        _object({"calendar_id": _id(), "etag": _text(1_024)}, required=("calendar_id", "etag")),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "events.create",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_EVENT_WRITE_SCOPES,
        _object(
            {"calendar_id": _id(), **_event_fields()}, required=("calendar_id", "start", "end")
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "events.update",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_EVENT_WRITE_SCOPES,
        _object(
            {"calendar_id": _id(), "etag": _text(1_024), **_event_fields()},
            required=("calendar_id", "event_id", "etag"),
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "events.move",
        ConnectorEffect.SAFE_MUTATION,
        _CALENDAR_EVENT_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _id(),
                "destination_calendar_id": _id(),
                "event_id": _id(),
                "etag": _text(1_024),
                "send_updates": {"enum": ["all", "externalOnly", "none"], "type": "string"},
            },
            required=("calendar_id", "destination_calendar_id", "event_id"),
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "events.respond",
        ConnectorEffect.OUTWARD,
        _CALENDAR_EVENT_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _id(),
                "comment": _text(16_384, minimum=0),
                "event_id": _id(),
                "response_status": {
                    "enum": ["accepted", "declined", "tentative"],
                    "type": "string",
                },
                "send_updates": {"enum": ["all", "externalOnly", "none"], "type": "string"},
            },
            required=("calendar_id", "event_id", "response_status"),
        ),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.WRITE,
        "events.delete",
        ConnectorEffect.DESTRUCTIVE,
        _CALENDAR_EVENT_WRITE_SCOPES,
        _object(
            {
                "calendar_id": _id(),
                "etag": _text(1_024),
                "event_id": _id(),
                "send_updates": {"enum": ["all", "externalOnly", "none"], "type": "string"},
            },
            required=("calendar_id", "event_id", "etag"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "files.list",
        ConnectorEffect.READ,
        _DRIVE_METADATA_SCOPES,
        _drive_list(),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "files.get",
        ConnectorEffect.READ,
        _DRIVE_METADATA_SCOPES,
        _object({"file_id": _id()}, required=("file_id",)),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "files.download",
        ConnectorEffect.READ,
        _DRIVE_CONTENT_SCOPES,
        _object(
            {
                "byte_offset": {"maximum": 2_147_483_647, "minimum": 0, "type": "integer"},
                "file_id": _id(),
                "max_chunk_size": {"maximum": 240_000, "minimum": 1, "type": "integer"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "files.export",
        ConnectorEffect.READ,
        _DRIVE_CONTENT_SCOPES,
        _object(
            {"export_mime_type": _text(256), "file_id": _id()},
            required=("export_mime_type", "file_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "permissions.list",
        ConnectorEffect.READ,
        _DRIVE_METADATA_SCOPES,
        _object(
            {
                "file_id": _id(),
                "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "comments.list",
        ConnectorEffect.READ,
        _DRIVE_METADATA_SCOPES,
        _object(
            {
                "file_id": _id(),
                "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "replies.list",
        ConnectorEffect.READ,
        _DRIVE_METADATA_SCOPES,
        _object(
            {
                "comment_id": _id(),
                "file_id": _id(),
                "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
            },
            required=("comment_id", "file_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "revisions.list",
        ConnectorEffect.READ,
        _DRIVE_METADATA_SCOPES,
        _object(
            {
                "file_id": _id(),
                "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "revisions.download",
        ConnectorEffect.READ,
        _DRIVE_CONTENT_SCOPES,
        _object(
            {
                "byte_offset": {"maximum": 2_147_483_647, "minimum": 0, "type": "integer"},
                "file_id": _id(),
                "max_chunk_size": {"maximum": 240_000, "minimum": 1, "type": "integer"},
                "revision_id": _id(),
            },
            required=("file_id", "revision_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.create",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object(_file_fields(), required=("mime_type", "name")),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.update",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"etag": _text(1_024), "file_id": _id(), **_file_fields()}, required=("etag", "file_id")
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.copy",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object({"file_id": _id(), **_file_fields()}, required=("file_id",)),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.move",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "add_parent_ids": _array(_id(), maximum=32),
                "etag": _text(1_024),
                "file_id": _id(),
                "remove_parent_ids": _array(_id(), maximum=32),
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.trash",
        ConnectorEffect.DESTRUCTIVE,
        _DRIVE_WRITE_SCOPES,
        _object({"etag": _text(1_024), "file_id": _id()}, required=("etag", "file_id")),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.restore",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object({"etag": _text(1_024), "file_id": _id()}, required=("etag", "file_id")),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.purge",
        ConnectorEffect.PERMANENT,
        _DRIVE_WRITE_SCOPES,
        _object({"etag": _text(1_024), "file_id": _id()}, required=("etag", "file_id")),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "permissions.create",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "domain": _text(253),
                "email_address": _text(512),
                "file_id": _id(),
                "notification_message": _text(16_384, minimum=0),
                "permission_type": {
                    "enum": ["user", "group", "domain", "anyone"],
                    "type": "string",
                },
                "role": {"enum": ["reader", "commenter", "writer"], "type": "string"},
                "send_notification_email": {"type": "boolean"},
            },
            required=("file_id", "permission_type", "role"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "permissions.update",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "etag": _text(1_024),
                "file_id": _id(),
                "permission_id": _id(),
                "role": {"enum": ["reader", "commenter", "writer"], "type": "string"},
            },
            required=("etag", "file_id", "permission_id", "role"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "permissions.delete",
        ConnectorEffect.DESTRUCTIVE,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"etag": _text(1_024), "file_id": _id(), "permission_id": _id()},
            required=("etag", "file_id", "permission_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "comments.create",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object({"file_id": _id(), **_comment_fields()}, required=("content", "file_id")),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "comments.update",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"comment_id": _id(), "etag": _text(1_024), "file_id": _id(), **_comment_fields()},
            required=("comment_id", "etag", "file_id", "content"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "comments.delete",
        ConnectorEffect.DESTRUCTIVE,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"comment_id": _id(), "etag": _text(1_024), "file_id": _id()},
            required=("comment_id", "etag", "file_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "replies.create",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"comment_id": _id(), "file_id": _id(), **_comment_fields()},
            required=("comment_id", "content", "file_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "replies.update",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "comment_id": _id(),
                "etag": _text(1_024),
                "file_id": _id(),
                "reply_id": _id(),
                **_comment_fields(),
            },
            required=("comment_id", "content", "etag", "file_id", "reply_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "replies.delete",
        ConnectorEffect.DESTRUCTIVE,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "comment_id": _id(),
                "etag": _text(1_024),
                "file_id": _id(),
                "reply_id": _id(),
            },
            required=("comment_id", "etag", "file_id", "reply_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "revisions.keep",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "file_id": _id(),
                "keep_forever": {"const": True, "type": "boolean"},
                "revision_id": _id(),
            },
            required=("file_id", "keep_forever", "revision_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "revisions.delete",
        ConnectorEffect.PERMANENT,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"etag": _text(1_024), "file_id": _id(), "revision_id": _id()},
            required=("etag", "file_id", "revision_id"),
        ),
    ),
)
