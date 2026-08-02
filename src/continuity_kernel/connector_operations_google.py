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
_DRIVE_MEET_READONLY: Final = "https://www.googleapis.com/auth/drive.meet.readonly"
_DRIVE_FILE: Final = "https://www.googleapis.com/auth/drive.file"
_DRIVE: Final = "https://www.googleapis.com/auth/drive"
_MAX_DRIVE_BYTE_OFFSET: Final = 5 * 1024**4
# The fixed transport permits 128 query items; metadata reads also send `format`.
_GMAIL_MAX_METADATA_HEADER_NAMES: Final = 127


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


def _drive_resource_key() -> dict[str, object]:
    return {
        "maxLength": 512,
        "minLength": 1,
        "pattern": "^[A-Za-z0-9_-]+$",
        "type": "string",
    }


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
    return _array(_text(512), maximum=64, minimum=1)


def _attachment() -> dict[str, object]:
    metadata: dict[str, object] = {"filename": _text(512), "mime_type": _text(256)}
    source_fields: dict[str, object] = {
        **metadata,
        "content_base64": _text(240_000),
        "local_file": _local_file(),
    }
    schema = _object(source_fields)
    schema["oneOf"] = [
        _object(
            {**metadata, "content_base64": source_fields["content_base64"]},
            required=("content_base64", "filename", "mime_type"),
        ),
        _object(
            {**metadata, "local_file": source_fields["local_file"]},
            required=("filename", "local_file", "mime_type"),
        ),
    ]
    return schema


def _gmail_attachment_delivery() -> dict[str, object]:
    return {"enum": ["artifact", "inline_chunk"], "type": "string"}


def _gmail_attachment_content() -> dict[str, object]:
    identifiers: dict[str, object] = {"attachment_id": _id(), "message_id": _id()}
    schema = _object(
        {
            **identifiers,
            "delivery": _gmail_attachment_delivery(),
        },
        required=("attachment_id", "message_id"),
    )
    schema["oneOf"] = [
        _object(identifiers, required=("attachment_id", "message_id")),
        _object(
            {**identifiers, "delivery": {"const": "artifact", "type": "string"}},
            required=("attachment_id", "delivery", "message_id"),
        ),
        _object(
            {**identifiers, "delivery": {"const": "inline_chunk", "type": "string"}},
            required=("attachment_id", "delivery", "message_id"),
        ),
    ]
    return schema


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


def _gmail_send() -> dict[str, object]:
    fields = _mail_fields()
    local_file = _local_file()
    schema = _object({**fields, "local_file": local_file})
    schema["oneOf"] = [
        _object(fields, required=("to",)),
        _object(
            {name: field for name, field in fields.items() if name != "to"},
            required=("cc",),
        ),
        _object(
            {name: field for name, field in fields.items() if name not in {"cc", "to"}},
            required=("bcc",),
        ),
        _object({"local_file": local_file}, required=("local_file",)),
    ]
    return schema


def _gmail_migration(*, importing: bool) -> dict[str, object]:
    fields: dict[str, object] = {
        "deleted": {"type": "boolean"},
        "internal_date_source": {
            "enum": ["dateHeader", "receivedTime"],
            "type": "string",
        },
        "label_ids": _array(_id(), maximum=100, minimum=1),
        "local_file": _local_file(),
    }
    if importing:
        fields.update(
            {
                "never_mark_spam": {"type": "boolean"},
                "process_for_calendar": {"type": "boolean"},
            }
        )
    return _object(fields, required=("local_file",))


def _mail_list() -> dict[str, object]:
    return _object(
        {
            "include_spam_trash": {"type": "boolean"},
            "label_ids": _array(_id(), maximum=64),
            "page_size": {"maximum": 500, "minimum": 1, "type": "integer"},
            "query": _text(8_192, minimum=0),
        }
    )


def _draft_list() -> dict[str, object]:
    return _object(
        {
            "include_spam_trash": {"type": "boolean"},
            "page_size": {"maximum": 500, "minimum": 1, "type": "integer"},
            "query": _text(8_192, minimum=0),
        }
    )


def _gmail_message_get() -> dict[str, object]:
    identifier = {"message_id": _id()}
    schema = _object(
        {
            **identifier,
            "format": {"enum": ["full", "metadata", "minimal", "raw"], "type": "string"},
            "metadata_header_names": _array(_text(998), maximum=_GMAIL_MAX_METADATA_HEADER_NAMES),
        },
        required=("message_id",),
    )
    schema["oneOf"] = [
        _object(
            {
                **identifier,
                "format": {"enum": ["full", "minimal", "raw"], "type": "string"},
            },
            required=("message_id",),
        ),
        _object(
            {
                **identifier,
                "format": {"const": "metadata", "type": "string"},
                "metadata_header_names": _array(
                    _text(998), maximum=_GMAIL_MAX_METADATA_HEADER_NAMES
                ),
            },
            required=("format", "message_id"),
        ),
    ]
    return schema


def _gmail_modify(identifier_name: str, identifier: dict[str, object]) -> dict[str, object]:
    properties: dict[str, object] = {
        "add_label_ids": _array(_id(), maximum=100, minimum=1),
        identifier_name: identifier,
        "remove_label_ids": _array(_id(), maximum=100, minimum=1),
    }
    schema = _object(properties, required=(identifier_name,))
    schema["oneOf"] = [
        _object(properties, required=("add_label_ids", identifier_name)),
        _object(
            {name: field for name, field in properties.items() if name != "add_label_ids"},
            required=(identifier_name, "remove_label_ids"),
        ),
    ]
    return schema


def _gmail_thread_get() -> dict[str, object]:
    identifier = {"thread_id": _id()}
    schema = _object(
        {
            **identifier,
            "format": {"enum": ["full", "metadata", "minimal"], "type": "string"},
            "metadata_header_names": _array(_text(998), maximum=_GMAIL_MAX_METADATA_HEADER_NAMES),
        },
        required=("thread_id",),
    )
    schema["oneOf"] = [
        _object(
            {
                **identifier,
                "format": {"enum": ["full", "minimal"], "type": "string"},
            },
            required=("thread_id",),
        ),
        _object(
            {
                **identifier,
                "format": {"const": "metadata", "type": "string"},
                "metadata_header_names": _array(
                    _text(998), maximum=_GMAIL_MAX_METADATA_HEADER_NAMES
                ),
            },
            required=("format", "thread_id"),
        ),
    ]
    return schema


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


def _event_attendee() -> dict[str, object]:
    return _object(
        {
            "display_name": _text(1_024, minimum=0),
            "email": _text(512),
            "optional": {"type": "boolean"},
            "response_status": {
                "enum": ["accepted", "declined", "needsAction", "tentative"],
                "type": "string",
            },
        },
        required=("email",),
    )


def _drive_attachment_reference() -> dict[str, object]:
    return _object({"file_id": _id()}, required=("file_id",))


def _local_file() -> dict[str, object]:
    return _object(
        {
            "grant_id": _text(128),
            "relative_path": _text(16 * 1024),
        },
        required=("grant_id", "relative_path"),
    )


def _event_reminder() -> dict[str, object]:
    return _object(
        {
            "delivery": {"enum": ["email", "popup"], "type": "string"},
            "minutes": {"maximum": 40_320, "minimum": 0, "type": "integer"},
        },
        required=("delivery", "minutes"),
    )


def _event_reminders() -> dict[str, object]:
    return {
        "oneOf": [
            _object(
                {"use_default": {"const": True, "type": "boolean"}},
                required=("use_default",),
            ),
            _object(
                {
                    "overrides": _array(_event_reminder(), maximum=5),
                    "use_default": {"const": False, "type": "boolean"},
                },
                required=("use_default",),
            ),
        ]
    }


def _event_fields() -> dict[str, object]:
    return {
        "attendee_emails": _email_addresses(),
        "attendees": _array(_event_attendee(), maximum=64),
        "description": _text(200_000, minimum=0),
        "drive_attachments": _array(_drive_attachment_reference(), maximum=25),
        "end": _event_time(),
        "event_id": _id(),
        "guests_can_invite_others": {"type": "boolean"},
        "guests_can_modify": {"type": "boolean"},
        "guests_can_see_other_guests": {"type": "boolean"},
        "location": _text(4_096, minimum=0),
        "recurrence": _array(_text(2_048), maximum=32),
        "reminders": _event_reminders(),
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


def _calendar_list() -> dict[str, object]:
    return _object(
        {
            "min_access_role": {
                "enum": [
                    "freeBusyReader",
                    "owner",
                    "reader",
                    "writer",
                    "writerWithoutPrivateAccess",
                ],
                "type": "string",
            },
            "page_size": {"maximum": 250, "minimum": 1, "type": "integer"},
            "show_deleted": {"type": "boolean"},
            "show_hidden": {"type": "boolean"},
            "show_own_organization_only": {"type": "boolean"},
        }
    )


def _calendar_event_list() -> dict[str, object]:
    return _object(
        {
            "calendar_id": _id(),
            "event_types": _array(
                {
                    "enum": [
                        "birthday",
                        "default",
                        "focusTime",
                        "fromGmail",
                        "outOfOffice",
                        "workingLocation",
                    ],
                    "type": "string",
                },
                maximum=6,
                minimum=1,
            ),
            "i_cal_uid": _text(1_024),
            "max_attendees": {"maximum": 1_000, "minimum": 1, "type": "integer"},
            "order_by": {"enum": ["startTime", "updated"], "type": "string"},
            "page_size": {"maximum": 2_500, "minimum": 1, "type": "integer"},
            "query": _text(8_192, minimum=0),
            "show_deleted": {"type": "boolean"},
            "single_events": {"type": "boolean"},
            "time_max": _text(64),
            "time_min": _text(64),
            "time_zone": _text(128),
            "updated_min": _text(64),
        },
        required=("calendar_id",),
    )


def _calendar_instance_list() -> dict[str, object]:
    return _object(
        {
            "calendar_id": _id(),
            "event_id": _id(),
            "max_attendees": {"maximum": 1_000, "minimum": 1, "type": "integer"},
            "page_size": {"maximum": 2_500, "minimum": 1, "type": "integer"},
            "time_max": _text(64),
            "time_min": _text(64),
            "time_zone": _text(128),
        },
        required=("calendar_id", "event_id"),
    )


def _app_property(*, allow_deletion: bool) -> dict[str, object]:
    value: dict[str, object] = _text(124)
    if allow_deletion:
        value = {"oneOf": [value, {"type": "null"}]}
    return _object({"key": _text(124), "value": value}, required=("key", "value"))


def _file_metadata_fields(
    *,
    allow_parent_id: bool,
    allow_property_deletion: bool,
) -> dict[str, object]:
    fields: dict[str, object] = {
        "app_properties": _array(
            _app_property(allow_deletion=allow_property_deletion),
            maximum=30,
            minimum=1,
        ),
        "description": _text(32_768, minimum=0),
        "mime_type": _text(256),
        "name": _text(1_024),
        "supports_all_drives": {"type": "boolean"},
    }
    if allow_parent_id:
        fields["parent_id"] = _id()
    return fields


def _quoted_file_content() -> dict[str, object]:
    return _object(
        {"mime_type": _text(256), "value": _text(200_000, minimum=0)},
    )


def _drive_file_mutation_schema(
    required: tuple[str, ...],
    *,
    leading_fields: dict[str, object] | None = None,
    allow_parent_id: bool,
    allow_property_deletion: bool,
) -> dict[str, object]:
    metadata_fields = {
        **(leading_fields or {}),
        **_file_metadata_fields(
            allow_parent_id=allow_parent_id,
            allow_property_deletion=allow_property_deletion,
        ),
    }
    source_fields = {
        **metadata_fields,
        "content_base64": _text(240_000),
        "local_file": _local_file(),
    }
    schema = _object(source_fields, required=required)
    schema["oneOf"] = [
        _object(
            {**metadata_fields, "content_base64": source_fields["content_base64"]},
            required=(*required, "content_base64"),
        ),
        _object(
            {**metadata_fields, "local_file": source_fields["local_file"]},
            required=(*required, "local_file"),
        ),
        _object(metadata_fields, required=required),
    ]
    return schema


def _drive_delivery() -> dict[str, object]:
    return {"enum": ["artifact", "inline_chunk"], "type": "string"}


def _drive_content_schema(
    properties: dict[str, object],
    required: tuple[str, ...],
    *,
    inline_fields: dict[str, object] | None = None,
) -> dict[str, object]:
    inline_fields = {} if inline_fields is None else inline_fields
    artifact_fields = {
        **properties,
        "delivery": {"const": "artifact", "type": "string"},
        "filename": _text(512),
        "mime_type": _text(256),
    }
    inline_properties = {
        **properties,
        "delivery": {"const": "inline_chunk", "type": "string"},
        **inline_fields,
    }
    schema = _object(
        {
            **properties,
            "delivery": _drive_delivery(),
            "filename": _text(512),
            "mime_type": _text(256),
            **inline_fields,
        },
        required=required,
    )
    schema["oneOf"] = [
        _object(artifact_fields, required=required),
        _object(inline_properties, required=(*required, "delivery")),
    ]
    return schema


def _drive_list() -> dict[str, object]:
    return _object(
        {
            "corpora": {"enum": ["allDrives", "domain", "drive", "user"], "type": "string"},
            "drive_id": _id(),
            "include_items_from_all_drives": {"type": "boolean"},
            "include_trashed": {"type": "boolean"},
            "mime_type": _text(256),
            "order_by": _array(
                {
                    "enum": [
                        "createdTime",
                        "createdTime desc",
                        "folder",
                        "folder desc",
                        "modifiedByMeTime",
                        "modifiedByMeTime desc",
                        "modifiedTime",
                        "modifiedTime desc",
                        "name",
                        "name desc",
                        "name_natural",
                        "name_natural desc",
                        "quotaBytesUsed",
                        "quotaBytesUsed desc",
                        "recency",
                        "recency desc",
                        "sharedWithMeTime",
                        "sharedWithMeTime desc",
                        "starred",
                        "starred desc",
                        "viewedByMeTime",
                        "viewedByMeTime desc",
                    ],
                    "type": "string",
                },
                maximum=10,
                minimum=1,
            ),
            "page_size": {"maximum": 1_000, "minimum": 1, "type": "integer"},
            "parent_id": _id(),
            "query": _text(8_192, minimum=0),
            "spaces": _array(
                {"enum": ["appDataFolder", "drive"], "type": "string"},
                maximum=2,
                minimum=1,
            ),
            "supports_all_drives": {"type": "boolean"},
        }
    )


def _shared_drive_list() -> dict[str, object]:
    return _object(
        {
            "page_size": {"maximum": 100, "minimum": 1, "type": "integer"},
            "query": _text(8_192, minimum=0),
        }
    )


def _permission_create_schema() -> dict[str, object]:
    common: dict[str, object] = {
        "file_id": _id(),
        "permission_type": {
            "enum": ["user", "group", "domain", "anyone"],
            "type": "string",
        },
        "role": {
            "enum": ["reader", "commenter", "writer", "fileOrganizer", "organizer"],
            "type": "string",
        },
        "supports_all_drives": {"type": "boolean"},
    }
    schema = _object(
        {
            **common,
            "allow_file_discovery": {"type": "boolean"},
            "domain": _text(253),
            "email_address": _text(512),
            "expiration_time": _text(64),
            "notification_message": _text(16_384, minimum=0),
            "send_notification_email": {"type": "boolean"},
        },
        required=("file_id", "permission_type", "role"),
    )
    user_group_base = {
        **common,
        "email_address": _text(512),
        "expiration_time": _text(64),
        "notification_message": _text(16_384, minimum=0),
        "send_notification_email": {"type": "boolean"},
    }
    schema["oneOf"] = [
        _object(
            {**user_group_base, "permission_type": {"const": "user", "type": "string"}},
            required=("email_address", "file_id", "permission_type", "role"),
        ),
        _object(
            {**user_group_base, "permission_type": {"const": "group", "type": "string"}},
            required=("email_address", "file_id", "permission_type", "role"),
        ),
        _object(
            {
                **common,
                "allow_file_discovery": {"type": "boolean"},
                "domain": _text(253),
                "permission_type": {"const": "domain", "type": "string"},
            },
            required=("domain", "file_id", "permission_type", "role"),
        ),
        _object(
            {
                **common,
                "allow_file_discovery": {"type": "boolean"},
                "permission_type": {"const": "anyone", "type": "string"},
            },
            required=("file_id", "permission_type", "role"),
        ),
    ]
    return schema


def _comment_create_schema() -> dict[str, object]:
    return _object(
        {
            "anchor": _text(32_768, minimum=0),
            "content": _text(200_000),
            "file_id": _id(),
            "quoted_file_content": _quoted_file_content(),
        },
        required=("content", "file_id"),
    )


def _reply_create_schema() -> dict[str, object]:
    identifiers: dict[str, object] = {"comment_id": _id(), "file_id": _id()}
    content: dict[str, object] = {**identifiers, "content": _text(200_000)}
    action: dict[str, object] = {
        **identifiers,
        "action": {"enum": ["reopen", "resolve"], "type": "string"},
    }
    both: dict[str, object] = {
        **content,
        "action": {"enum": ["reopen", "resolve"], "type": "string"},
    }
    schema = _object(
        {
            **identifiers,
            "action": {"enum": ["reopen", "resolve"], "type": "string"},
            "content": _text(200_000),
        },
        required=("comment_id", "file_id"),
    )
    schema["oneOf"] = [
        _object(content, required=("comment_id", "content", "file_id")),
        _object(action, required=("action", "comment_id", "file_id")),
        _object(both, required=("action", "comment_id", "content", "file_id")),
    ]
    return schema


def _drive_move_schema() -> dict[str, object]:
    identifiers = {
        "file_id": _id(),
        "supports_all_drives": {"type": "boolean"},
    }
    add = {**identifiers, "add_parent_ids": _array(_id(), maximum=1, minimum=1)}
    remove = {**identifiers, "remove_parent_ids": _array(_id(), maximum=1, minimum=1)}
    both = {
        **add,
        "remove_parent_ids": _array(_id(), maximum=1, minimum=1),
    }
    schema = _object(
        {
            **identifiers,
            "add_parent_ids": _array(_id(), maximum=1, minimum=1),
            "remove_parent_ids": _array(_id(), maximum=1, minimum=1),
        },
        required=("file_id",),
    )
    schema["oneOf"] = [
        _object(add, required=("add_parent_ids", "file_id")),
        _object(remove, required=("file_id", "remove_parent_ids")),
        _object(both, required=("add_parent_ids", "file_id", "remove_parent_ids")),
    ]
    return schema


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
_DRIVE_COMMENT_READ_SCOPES: Final = _scopes(
    _DRIVE_READONLY,
    _DRIVE_FILE,
    _DRIVE_MEET_READONLY,
    _DRIVE,
)
_DRIVE_WRITE_SCOPES: Final = _scopes(_DRIVE_FILE, _DRIVE)


GOOGLE_OPERATIONS: Final[tuple[OperationSpec, ...]] = (
    _operation(
        "gmail",
        ConnectorMode.READ,
        "profile.get",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object({}),
    ),
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
        _gmail_message_get(),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "attachments.get",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _gmail_attachment_content(),
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
        _gmail_thread_get(),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "drafts.list",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _draft_list(),
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
        ConnectorMode.READ,
        "labels.get",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object({"label_id": _id()}, required=("label_id",)),
    ),
    _operation(
        "gmail",
        ConnectorMode.READ,
        "history.list",
        ConnectorEffect.READ,
        _GMAIL_READ_SCOPES,
        _object(
            {
                "history_types": _array(
                    {
                        "enum": [
                            "labelAdded",
                            "labelRemoved",
                            "messageAdded",
                            "messageDeleted",
                        ],
                        "type": "string",
                    },
                    maximum=4,
                ),
                "label_id": _id(),
                "page_size": {"maximum": 500, "minimum": 1, "type": "integer"},
                "start_history_id": {
                    "maxLength": 20,
                    "minLength": 1,
                    "pattern": "^[0-9]+$",
                    "type": "string",
                },
            },
            required=("start_history_id",),
        ),
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
        "messages.send",
        ConnectorEffect.OUTWARD,
        _GMAIL_WRITE_SCOPES,
        _gmail_send(),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.insert",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _gmail_migration(importing=False),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.import",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _gmail_migration(importing=True),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.modify",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _gmail_modify("message_id", _id()),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.batch_modify",
        ConnectorEffect.SAFE_MUTATION,
        _GMAIL_WRITE_SCOPES,
        _gmail_modify("message_ids", _array(_id(), maximum=1_000, minimum=1)),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.batch_purge",
        ConnectorEffect.PERMANENT,
        _GMAIL_PURGE_SCOPES,
        _object(
            {"message_ids": _array(_id(), maximum=1_000, minimum=1)},
            required=("message_ids",),
        ),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "messages.trash",
        ConnectorEffect.SAFE_MUTATION,
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
        _gmail_modify("thread_id", _id()),
    ),
    _operation(
        "gmail",
        ConnectorMode.WRITE,
        "threads.trash",
        ConnectorEffect.SAFE_MUTATION,
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
        ConnectorEffect.PERMANENT,
        _GMAIL_WRITE_SCOPES,
        _object({"label_id": _id()}, required=("label_id",)),
    ),
    _operation(
        "google_calendar",
        ConnectorMode.READ,
        "calendars.list",
        ConnectorEffect.READ,
        _CALENDAR_LIST_SCOPES,
        _calendar_list(),
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
        _calendar_event_list(),
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
        _calendar_instance_list(),
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
        "drives.list",
        ConnectorEffect.READ,
        _scopes(_DRIVE_READONLY, _DRIVE),
        _shared_drive_list(),
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
        _object(
            {"file_id": _id(), "supports_all_drives": {"type": "boolean"}},
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "files.content",
        ConnectorEffect.READ,
        _DRIVE_CONTENT_SCOPES,
        _drive_content_schema(
            {
                "file_id": _id(),
                "supports_all_drives": {"type": "boolean"},
            },
            ("file_id",),
            inline_fields={
                "byte_offset": {
                    "maximum": _MAX_DRIVE_BYTE_OFFSET,
                    "minimum": 0,
                    "type": "integer",
                },
                "max_chunk_size": {"maximum": 240_000, "minimum": 1, "type": "integer"},
            },
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "files.download",
        ConnectorEffect.READ,
        _DRIVE_CONTENT_SCOPES,
        _drive_content_schema(
            {
                "file_id": _id(),
                "mime_type": _text(256),
                "resource_key": _drive_resource_key(),
                "revision_id": _id(),
            },
            ("file_id",),
            inline_fields={
                "byte_offset": {
                    "maximum": _MAX_DRIVE_BYTE_OFFSET,
                    "minimum": 0,
                    "type": "integer",
                },
                "max_chunk_size": {"maximum": 240_000, "minimum": 1, "type": "integer"},
            },
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "files.export",
        ConnectorEffect.READ,
        _DRIVE_CONTENT_SCOPES,
        _drive_content_schema(
            {"export_mime_type": _text(256), "file_id": _id()},
            ("export_mime_type", "file_id"),
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
                "supports_all_drives": {"type": "boolean"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.READ,
        "comments.list",
        ConnectorEffect.READ,
        _DRIVE_COMMENT_READ_SCOPES,
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
        _DRIVE_COMMENT_READ_SCOPES,
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
                "page_size": {"maximum": 1_000, "minimum": 1, "type": "integer"},
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
        _drive_content_schema(
            {
                "file_id": _id(),
                "revision_id": _id(),
            },
            ("file_id", "revision_id"),
            inline_fields={
                "byte_offset": {
                    "maximum": _MAX_DRIVE_BYTE_OFFSET,
                    "minimum": 0,
                    "type": "integer",
                },
                "max_chunk_size": {"maximum": 240_000, "minimum": 1, "type": "integer"},
            },
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.create",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _drive_file_mutation_schema(
            ("mime_type", "name"),
            allow_parent_id=True,
            allow_property_deletion=False,
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.update",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _drive_file_mutation_schema(
            ("file_id",),
            leading_fields={"file_id": _id()},
            allow_parent_id=False,
            allow_property_deletion=True,
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.copy",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "file_id": _id(),
                **_file_metadata_fields(
                    allow_parent_id=True,
                    allow_property_deletion=True,
                ),
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.move",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _drive_move_schema(),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.trash",
        ConnectorEffect.DESTRUCTIVE,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "file_id": _id(),
                "supports_all_drives": {"type": "boolean"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.restore",
        ConnectorEffect.SAFE_MUTATION,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "file_id": _id(),
                "supports_all_drives": {"type": "boolean"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "files.purge",
        ConnectorEffect.PERMANENT,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "file_id": _id(),
                "supports_all_drives": {"type": "boolean"},
            },
            required=("file_id",),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "permissions.create",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _permission_create_schema(),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "permissions.update",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "file_id": _id(),
                "permission_id": _id(),
                "role": {
                    "enum": ["reader", "commenter", "writer", "fileOrganizer", "organizer"],
                    "type": "string",
                },
                "supports_all_drives": {"type": "boolean"},
            },
            required=("file_id", "permission_id", "role"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "permissions.delete",
        ConnectorEffect.DESTRUCTIVE,
        _DRIVE_WRITE_SCOPES,
        _object(
            {
                "file_id": _id(),
                "permission_id": _id(),
                "supports_all_drives": {"type": "boolean"},
            },
            required=("file_id", "permission_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "comments.create",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _comment_create_schema(),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "comments.update",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"comment_id": _id(), "content": _text(200_000), "file_id": _id()},
            required=("comment_id", "content", "file_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "comments.delete",
        ConnectorEffect.DESTRUCTIVE,
        _DRIVE_WRITE_SCOPES,
        _object(
            {"comment_id": _id(), "file_id": _id()},
            required=("comment_id", "file_id"),
        ),
    ),
    _operation(
        "google_drive",
        ConnectorMode.WRITE,
        "replies.create",
        ConnectorEffect.OUTWARD,
        _DRIVE_WRITE_SCOPES,
        _reply_create_schema(),
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
                "content": _text(200_000),
                "file_id": _id(),
                "reply_id": _id(),
            },
            required=("comment_id", "content", "file_id", "reply_id"),
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
                "file_id": _id(),
                "reply_id": _id(),
            },
            required=("comment_id", "file_id", "reply_id"),
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
        _object({"file_id": _id(), "revision_id": _id()}, required=("file_id", "revision_id")),
    ),
)
