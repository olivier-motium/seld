"""Closed Slack and Discord operation specifications for collaboration connectors."""

from __future__ import annotations

from typing import Final

from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationSpec


def _object(
    properties: dict[str, object],
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
        "type": "object",
    }


def _text(
    maximum: int,
    *,
    minimum: int = 0,
    pattern: str | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "maxLength": maximum,
        "minLength": minimum,
        "type": "string",
    }
    if pattern is not None:
        schema["pattern"] = pattern
    return schema


def _array(
    items: dict[str, object],
    maximum: int,
    *,
    minimum: int = 0,
) -> dict[str, object]:
    return {
        "items": items,
        "maxItems": maximum,
        "minItems": minimum,
        "type": "array",
    }


def _local_file() -> dict[str, object]:
    return _object(
        {
            "grant_id": _text(128, minimum=1),
            "relative_path": _text(16 * 1024, minimum=1),
        },
        ("grant_id", "relative_path"),
    )


def _binary_upload(
    metadata: dict[str, object],
    required: tuple[str, ...],
) -> dict[str, object]:
    source_fields = {
        **metadata,
        "content_base64": _BASE64,
        "local_file": _local_file(),
    }
    schema = _object(source_fields)
    schema["oneOf"] = [
        _object(
            {**metadata, "content_base64": source_fields["content_base64"]},
            (*required, "content_base64"),
        ),
        _object(
            {**metadata, "local_file": source_fields["local_file"]},
            (*required, "local_file"),
        ),
    ]
    return schema


def _binary_delivery(
    identifiers: dict[str, object],
    required: tuple[str, ...],
) -> dict[str, object]:
    return _object(
        {
            **identifiers,
            "delivery": {
                "enum": ["artifact", "inline_chunk"],
                "type": "string",
            },
        },
        required,
    )


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


_EMPTY = _object({})
_BOOLEAN: Final = {"type": "boolean"}
_LIMIT: Final = {"maximum": 100, "minimum": 1, "type": "integer"}
_POSITION: Final = {"maximum": 10_000, "minimum": 0, "type": "integer"}
_PAGE: Final = {"maximum": 1_000_000, "minimum": 1, "type": "integer"}
_SLACK_FILE_COUNT: Final = {"maximum": 1_000, "minimum": 1, "type": "integer"}
_SLACK_ID: Final = _text(128, minimum=1, pattern=r"^[A-Za-z0-9]+$")
_SLACK_TIMESTAMP: Final = _text(
    32,
    minimum=3,
    pattern=r"^[0-9]{1,20}\.[0-9]{1,9}$",
)
_SNOWFLAKE: Final = _text(20, minimum=1, pattern=r"^[0-9]{1,20}$")
_ISO8601: Final = _text(
    35,
    minimum=20,
    pattern=(
        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
    ),
)
_EMOJI: Final = _text(128, minimum=1)
_BASE64: Final = _text(
    240_000,
    minimum=4,
    pattern=r"^[A-Za-z0-9+/]*={0,2}$",
)

_SLACK_READ: Final = (
    frozenset({"channels:read"}),
    frozenset({"groups:read"}),
    frozenset({"im:read"}),
    frozenset({"mpim:read"}),
)
_SLACK_HISTORY: Final = (
    frozenset({"channels:history"}),
    frozenset({"groups:history"}),
    frozenset({"im:history"}),
    frozenset({"mpim:history"}),
)
_SLACK_WRITE: Final = (
    frozenset({"channels:write"}),
    frozenset({"groups:write"}),
    frozenset({"im:write"}),
    frozenset({"mpim:write"}),
)
_SLACK_USERS: Final = (frozenset({"users:read"}),)
_SLACK_CHAT: Final = (frozenset({"chat:write"}),)
_SLACK_CHAT_HISTORY: Final = tuple(
    frozenset({"chat:write"}) | history for history in _SLACK_HISTORY
)
_SLACK_FILES_READ: Final = (frozenset({"files:read"}),)
_SLACK_FILES_WRITE: Final = (frozenset({"files:write"}),)
_SLACK_FILES_UPLOAD: Final = (frozenset({"files:read", "files:write"}),)
_SLACK_REACTIONS_READ: Final = (frozenset({"reactions:read"}),)
_SLACK_REACTIONS_WRITE: Final = (frozenset({"reactions:write"}),)
_DISCORD_BOT: Final[tuple[frozenset[str], ...]] = (frozenset(),)

_SLACK_TEXT = _text(40_000, minimum=1)
_SLACK_REPLY_LIMIT: Final = {"maximum": 1_000, "minimum": 1, "type": "integer"}
_SLACK_BLOCK_TEXT = _text(3_000, minimum=1)
_SLACK_PLAIN_TEXT_ELEMENT = _object(
    {
        "emoji": _BOOLEAN,
        "text": _SLACK_BLOCK_TEXT,
        "type": {"const": "plain_text", "type": "string"},
    },
    ("text", "type"),
)
_SLACK_MRKDWN_TEXT_ELEMENT = _object(
    {
        "text": _SLACK_BLOCK_TEXT,
        "type": {"const": "mrkdwn", "type": "string"},
        "verbatim": _BOOLEAN,
    },
    ("text", "type"),
)
_SLACK_BLOCK_TEXT_ELEMENT: Final[dict[str, object]] = {
    "oneOf": [_SLACK_PLAIN_TEXT_ELEMENT, _SLACK_MRKDWN_TEXT_ELEMENT]
}
_SLACK_SECTION_FIELD_TEXT: Final[dict[str, object]] = {
    "oneOf": [
        _object(
            {
                "text": _text(2_000, minimum=1),
                "type": {"const": "plain_text", "type": "string"},
            },
            ("text", "type"),
        ),
        _object(
            {
                "text": _text(2_000, minimum=1),
                "type": {"const": "mrkdwn", "type": "string"},
                "verbatim": _BOOLEAN,
            },
            ("text", "type"),
        ),
    ]
}
_SLACK_BLOCK_ITEM: Final[dict[str, object]] = {
    "oneOf": [
        _object(
            {
                "block_id": _text(255, minimum=1),
                "fields": _array(_SLACK_SECTION_FIELD_TEXT, 10, minimum=1),
                "text": _SLACK_BLOCK_TEXT_ELEMENT,
                "type": {"const": "section", "type": "string"},
            },
            ("type",),
        ),
        _object(
            {
                "block_id": _text(255, minimum=1),
                "elements": _array(_SLACK_BLOCK_TEXT_ELEMENT, 10, minimum=1),
                "type": {"const": "context", "type": "string"},
            },
            ("elements", "type"),
        ),
        _object(
            {
                "block_id": _text(255, minimum=1),
                "type": {"const": "divider", "type": "string"},
            },
            ("type",),
        ),
        _object(
            {
                "block_id": _text(255, minimum=1),
                "text": _object(
                    {
                        "emoji": _BOOLEAN,
                        "text": _text(150, minimum=1),
                        "type": {"const": "plain_text", "type": "string"},
                    },
                    ("text", "type"),
                ),
                "type": {"const": "header", "type": "string"},
            },
            ("text", "type"),
        ),
    ]
}
_SLACK_BLOCKS = _array(_SLACK_BLOCK_ITEM, 50, minimum=1)
_SLACK_UPDATE_BLOCKS = _array(_SLACK_BLOCK_ITEM, 50)
_SLACK_ATTACHMENT_FIELD = _object(
    {
        "short": _BOOLEAN,
        "title": _text(255, minimum=1),
        "value": _text(2_000, minimum=1),
    },
    ("title", "value"),
)
_SLACK_ATTACHMENT = _object(
    {
        "color": _text(
            7,
            minimum=3,
            pattern=r"^(?:#[0-9A-Fa-f]{6}|good|warning|danger)$",
        ),
        "fallback": _text(2_000, minimum=1),
        "fields": _array(_SLACK_ATTACHMENT_FIELD, 10, minimum=1),
        "footer": _text(300, minimum=1),
        "mrkdwn_in": _array(
            {"enum": ["fallback", "fields", "pretext", "text"], "type": "string"},
            4,
            minimum=1,
        ),
        "pretext": _text(2_000, minimum=1),
        "text": _text(3_000, minimum=1),
        "title": _text(255, minimum=1),
    }
)
_SLACK_ATTACHMENTS = _array(_SLACK_ATTACHMENT, 100, minimum=1)
_SLACK_UPDATE_ATTACHMENTS = _array(_SLACK_ATTACHMENT, 100)
_SLACK_MESSAGE_FIELDS: Final = {
    "attachments": _SLACK_ATTACHMENTS,
    "blocks": _SLACK_BLOCKS,
    "reply_broadcast": _BOOLEAN,
    "text": _SLACK_TEXT,
    "thread_ts": _SLACK_TIMESTAMP,
    "unfurl_links": _BOOLEAN,
    "unfurl_media": _BOOLEAN,
}
_SLACK_MESSAGE_UPDATE_FIELDS: Final = {
    "attachments": _SLACK_UPDATE_ATTACHMENTS,
    "blocks": _SLACK_UPDATE_BLOCKS,
    "text": _text(40_000),
}
_DISCORD_EMBED_FIELD = _object(
    {
        "inline": _BOOLEAN,
        "name": _text(256, minimum=1),
        "value": _text(1_024, minimum=1),
    },
    ("name", "value"),
)
_DISCORD_EMBED = _object(
    {
        "author": _object({"name": _text(256, minimum=1)}, ("name",)),
        "color": {"maximum": 16_777_215, "minimum": 0, "type": "integer"},
        "description": _text(4_096, minimum=1),
        "fields": _array(_DISCORD_EMBED_FIELD, 25, minimum=1),
        "footer": _object({"text": _text(2_048, minimum=1)}, ("text",)),
        "timestamp": _ISO8601,
        "title": _text(256, minimum=1),
    }
)
_DISCORD_ALLOWED_MENTIONS = _object(
    {
        "parse": _array(
            {"enum": ["everyone", "roles", "users"], "type": "string"},
            3,
        ),
        "replied_user": _BOOLEAN,
        "roles": _array(_SNOWFLAKE, 100),
        "users": _array(_SNOWFLAKE, 100),
    }
)
_DISCORD_PARTIAL_EMOJI: Final = {
    "oneOf": [
        _object({"id": _SNOWFLAKE}, ("id",)),
        _object({"name": _text(64, minimum=1)}, ("name",)),
    ]
}
_DISCORD_LINK_BUTTON = _object(
    {
        "destination": _text(2_048, minimum=9, pattern=r"^https://[^\s]+$"),
        "disabled": _BOOLEAN,
        "emoji": _DISCORD_PARTIAL_EMOJI,
        "label": _text(80, minimum=1),
        "type": {"const": "link_button", "type": "string"},
    },
    ("destination", "label", "type"),
)
_DISCORD_ACTION_ROW = _object(
    {
        "components": _array(_DISCORD_LINK_BUTTON, 5, minimum=1),
        "type": {"const": "action_row", "type": "string"},
    },
    ("components", "type"),
)
_DISCORD_COMPONENTS = _array(_DISCORD_ACTION_ROW, 5, minimum=1)
_DISCORD_UPDATE_COMPONENTS = _array(_DISCORD_ACTION_ROW, 5)
_DISCORD_POLL_MEDIA = _object({"text": _text(300, minimum=1)}, ("text",))
_DISCORD_POLL_ANSWER = _object(
    {
        "poll_media": _object(
            {
                "emoji": _DISCORD_PARTIAL_EMOJI,
                "text": _text(55, minimum=1),
            },
            ("text",),
        )
    },
    ("poll_media",),
)
_DISCORD_POLL = _object(
    {
        "allow_multiselect": _BOOLEAN,
        "answers": _array(_DISCORD_POLL_ANSWER, 10, minimum=2),
        "duration": {"maximum": 768, "minimum": 1, "type": "integer"},
        "layout_type": {"const": 1, "type": "integer"},
        "question": _DISCORD_POLL_MEDIA,
    },
    ("answers", "question"),
)
_DISCORD_MESSAGE_CREATE_FIELDS: Final = {
    "allowed_mentions": _DISCORD_ALLOWED_MENTIONS,
    "components": _DISCORD_COMPONENTS,
    "content": _text(2_000, minimum=1),
    "embeds": _array(_DISCORD_EMBED, 10, minimum=1),
    "message_reference": _object(
        {
            "channel_id": _SNOWFLAKE,
            "fail_if_not_exists": _BOOLEAN,
            "guild_id": _SNOWFLAKE,
            "message_id": _SNOWFLAKE,
        },
        ("message_id",),
    ),
    "poll": _DISCORD_POLL,
}
_DISCORD_MESSAGE_UPDATE_FIELDS: Final = {
    "allowed_mentions": _DISCORD_ALLOWED_MENTIONS,
    "components": _DISCORD_UPDATE_COMPONENTS,
    "content": _text(2_000),
    "embeds": _array(_DISCORD_EMBED, 10),
}


COLLABORATION_OPERATIONS: Final[tuple[OperationSpec, ...]] = (
    _operation(
        "slack", ConnectorMode.READ, "identity.get", ConnectorEffect.READ, _SLACK_USERS, _EMPTY
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "users.list",
        ConnectorEffect.READ,
        _SLACK_USERS,
        _object({"limit": _LIMIT}),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "users.get",
        ConnectorEffect.READ,
        _SLACK_USERS,
        _object({"user": _SLACK_ID}, ("user",)),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "conversations.list",
        ConnectorEffect.READ,
        _SLACK_READ,
        _object(
            {
                "channel_types": _array(
                    {"enum": ["channel", "group", "im", "mpim"], "type": "string"},
                    4,
                    minimum=1,
                ),
                "limit": _LIMIT,
            }
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "conversations.get",
        ConnectorEffect.READ,
        _SLACK_READ,
        _object({"channel": _SLACK_ID}, ("channel",)),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "messages.list",
        ConnectorEffect.READ,
        _SLACK_HISTORY,
        _object(
            {
                "channel": _SLACK_ID,
                "latest": _SLACK_TIMESTAMP,
                "limit": _LIMIT,
                "oldest": _SLACK_TIMESTAMP,
            },
            ("channel",),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "messages.get",
        ConnectorEffect.READ,
        _SLACK_HISTORY,
        _object({"channel": _SLACK_ID, "ts": _SLACK_TIMESTAMP}, ("channel", "ts")),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "threads.list",
        ConnectorEffect.READ,
        _SLACK_HISTORY,
        _object(
            {
                "channel": _SLACK_ID,
                "latest": _SLACK_TIMESTAMP,
                "limit": _SLACK_REPLY_LIMIT,
                "oldest": _SLACK_TIMESTAMP,
                "thread_ts": _SLACK_TIMESTAMP,
            },
            ("channel", "thread_ts"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "files.list",
        ConnectorEffect.READ,
        _SLACK_FILES_READ,
        _object(
            {
                "channel": _SLACK_ID,
                "count": _SLACK_FILE_COUNT,
                "latest": _SLACK_TIMESTAMP,
                "oldest": _SLACK_TIMESTAMP,
                "page": _PAGE,
            }
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "files.get",
        ConnectorEffect.READ,
        _SLACK_FILES_READ,
        _object({"file_id": _SLACK_ID}, ("file_id",)),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "files.download",
        ConnectorEffect.READ,
        _SLACK_FILES_READ,
        _binary_delivery({"file_id": _SLACK_ID}, ("file_id",)),
    ),
    _operation(
        "slack",
        ConnectorMode.READ,
        "reactions.list",
        ConnectorEffect.READ,
        _SLACK_REACTIONS_READ,
        _object({"channel": _SLACK_ID, "timestamp": _SLACK_TIMESTAMP}, ("channel", "timestamp")),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "dms.open",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object({"users": _array(_SLACK_ID, 8, minimum=1)}, ("users",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "dms.close",
        ConnectorEffect.DESTRUCTIVE,
        _SLACK_WRITE,
        _object({"channel": _SLACK_ID}, ("channel",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.join",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object({"channel": _SLACK_ID}, ("channel",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.leave",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object({"channel": _SLACK_ID}, ("channel",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.create",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object(
            {
                "is_private": _BOOLEAN,
                "name": _text(80, minimum=1, pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$"),
            },
            ("name",),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.rename",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object(
            {
                "channel": _SLACK_ID,
                "name": _text(80, minimum=1, pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$"),
            },
            ("channel", "name"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.topic",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object({"channel": _SLACK_ID, "topic": _text(250, minimum=1)}, ("channel", "topic")),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.purpose",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object(
            {"channel": _SLACK_ID, "purpose": _text(250, minimum=1)},
            ("channel", "purpose"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.archive",
        ConnectorEffect.DESTRUCTIVE,
        _SLACK_WRITE,
        _object({"channel": _SLACK_ID}, ("channel",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "channels.restore",
        ConnectorEffect.OUTWARD,
        _SLACK_WRITE,
        _object({"channel": _SLACK_ID}, ("channel",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "messages.create",
        ConnectorEffect.OUTWARD,
        _SLACK_CHAT,
        _object({"channel": _SLACK_ID, **_SLACK_MESSAGE_FIELDS}, ("channel",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "messages.update",
        ConnectorEffect.OUTWARD,
        _SLACK_CHAT,
        _object(
            {"channel": _SLACK_ID, "ts": _SLACK_TIMESTAMP, **_SLACK_MESSAGE_UPDATE_FIELDS},
            ("channel", "ts"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "messages.delete",
        ConnectorEffect.PERMANENT,
        _SLACK_CHAT,
        _object({"channel": _SLACK_ID, "ts": _SLACK_TIMESTAMP}, ("channel", "ts")),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "threads.reply",
        ConnectorEffect.OUTWARD,
        _SLACK_CHAT,
        _object(
            {"channel": _SLACK_ID, **_SLACK_MESSAGE_FIELDS},
            ("channel", "thread_ts"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "threads.update",
        ConnectorEffect.OUTWARD,
        _SLACK_CHAT_HISTORY,
        _object(
            {
                "channel": _SLACK_ID,
                "thread_ts": _SLACK_TIMESTAMP,
                "ts": _SLACK_TIMESTAMP,
                **_SLACK_MESSAGE_UPDATE_FIELDS,
            },
            ("channel", "thread_ts", "ts"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "threads.delete",
        ConnectorEffect.PERMANENT,
        _SLACK_CHAT_HISTORY,
        _object(
            {"channel": _SLACK_ID, "thread_ts": _SLACK_TIMESTAMP, "ts": _SLACK_TIMESTAMP},
            ("channel", "thread_ts", "ts"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "files.upload",
        ConnectorEffect.OUTWARD,
        _SLACK_FILES_UPLOAD,
        _binary_upload(
            {
                "channel": _SLACK_ID,
                "filename": _text(255, minimum=1),
                "thread_ts": _SLACK_TIMESTAMP,
                "title": _text(255, minimum=1),
            },
            ("channel", "filename"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "files.delete",
        ConnectorEffect.PERMANENT,
        _SLACK_FILES_WRITE,
        _object({"file_id": _SLACK_ID}, ("file_id",)),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "reactions.add",
        ConnectorEffect.OUTWARD,
        _SLACK_REACTIONS_WRITE,
        _object(
            {"channel": _SLACK_ID, "emoji": _EMOJI, "timestamp": _SLACK_TIMESTAMP},
            ("channel", "emoji", "timestamp"),
        ),
    ),
    _operation(
        "slack",
        ConnectorMode.WRITE,
        "reactions.remove",
        ConnectorEffect.OUTWARD,
        _SLACK_REACTIONS_WRITE,
        _object(
            {"channel": _SLACK_ID, "emoji": _EMOJI, "timestamp": _SLACK_TIMESTAMP},
            ("channel", "emoji", "timestamp"),
        ),
    ),
    _operation(
        "discord", ConnectorMode.READ, "identity.get", ConnectorEffect.READ, _DISCORD_BOT, _EMPTY
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "guilds.list",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object({"after": _SNOWFLAKE, "before": _SNOWFLAKE, "limit": _LIMIT}),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "channels.list",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object({"guild_id": _SNOWFLAKE}, ("guild_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "channels.get",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object({"channel_id": _SNOWFLAKE}, ("channel_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "threads.active",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object({"guild_id": _SNOWFLAKE}, ("guild_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "threads.archived_public",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object({"before": _ISO8601, "channel_id": _SNOWFLAKE, "limit": _LIMIT}, ("channel_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "threads.archived_private",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object({"before": _ISO8601, "channel_id": _SNOWFLAKE, "limit": _LIMIT}, ("channel_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "messages.list",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object(
            {
                "after": _SNOWFLAKE,
                "around": _SNOWFLAKE,
                "before": _SNOWFLAKE,
                "channel_id": _SNOWFLAKE,
                "limit": _LIMIT,
            },
            ("channel_id",),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "messages.get",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object({"channel_id": _SNOWFLAKE, "message_id": _SNOWFLAKE}, ("channel_id", "message_id")),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "attachments.get",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _binary_delivery(
            {
                "attachment_id": _SNOWFLAKE,
                "channel_id": _SNOWFLAKE,
                "message_id": _SNOWFLAKE,
            },
            ("attachment_id", "channel_id", "message_id"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.READ,
        "reactions.list",
        ConnectorEffect.READ,
        _DISCORD_BOT,
        _object(
            {
                "after": _SNOWFLAKE,
                "channel_id": _SNOWFLAKE,
                "emoji": _EMOJI,
                "limit": _LIMIT,
                "message_id": _SNOWFLAKE,
            },
            ("channel_id", "emoji", "message_id"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "dms.open",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object({"user_id": _SNOWFLAKE}, ("user_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "dms.close",
        ConnectorEffect.DESTRUCTIVE,
        _DISCORD_BOT,
        _object({"channel_id": _SNOWFLAKE}, ("channel_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "channels.create",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {
                "guild_id": _SNOWFLAKE,
                "name": _text(100, minimum=1),
                "nsfw": _BOOLEAN,
                "parent_id": _SNOWFLAKE,
                "position": _POSITION,
                "slowmode_seconds": {"maximum": 21_600, "minimum": 0, "type": "integer"},
                "topic": _text(1_024, minimum=1),
                "type": {"enum": ["announcement", "forum", "text", "voice"], "type": "string"},
            },
            ("guild_id", "name", "type"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "channels.update",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {
                "channel_id": _SNOWFLAKE,
                "name": _text(100, minimum=1),
                "nsfw": _BOOLEAN,
                "parent_id": _SNOWFLAKE,
                "position": _POSITION,
                "slowmode_seconds": {"maximum": 21_600, "minimum": 0, "type": "integer"},
                "topic": _text(1_024, minimum=1),
            },
            ("channel_id",),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "channels.delete",
        ConnectorEffect.PERMANENT,
        _DISCORD_BOT,
        _object({"channel_id": _SNOWFLAKE}, ("channel_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.create_from_message",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {
                "archive_duration": {"enum": [60, 1_440, 4_320, 10_080], "type": "integer"},
                "channel_id": _SNOWFLAKE,
                "message_id": _SNOWFLAKE,
                "name": _text(100, minimum=1),
            },
            ("channel_id", "message_id", "name"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.create",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {
                "archive_duration": {"enum": [60, 1_440, 4_320, 10_080], "type": "integer"},
                "channel_id": _SNOWFLAKE,
                "name": _text(100, minimum=1),
                "type": {"enum": ["private", "public"], "type": "string"},
            },
            ("channel_id", "name", "type"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.join",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object({"thread_id": _SNOWFLAKE}, ("thread_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.leave",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object({"thread_id": _SNOWFLAKE}, ("thread_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.update",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {
                "archive_duration": {"enum": [60, 1_440, 4_320, 10_080], "type": "integer"},
                "name": _text(100, minimum=1),
                "thread_id": _SNOWFLAKE,
            },
            ("thread_id",),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.archive",
        ConnectorEffect.DESTRUCTIVE,
        _DISCORD_BOT,
        _object({"thread_id": _SNOWFLAKE}, ("thread_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.restore",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object({"thread_id": _SNOWFLAKE}, ("thread_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "threads.delete",
        ConnectorEffect.PERMANENT,
        _DISCORD_BOT,
        _object({"thread_id": _SNOWFLAKE}, ("thread_id",)),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "messages.create",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {"channel_id": _SNOWFLAKE, **_DISCORD_MESSAGE_CREATE_FIELDS},
            ("channel_id",),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "messages.update",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {
                "channel_id": _SNOWFLAKE,
                "message_id": _SNOWFLAKE,
                **_DISCORD_MESSAGE_UPDATE_FIELDS,
            },
            ("channel_id", "message_id"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "messages.delete",
        ConnectorEffect.PERMANENT,
        _DISCORD_BOT,
        _object({"channel_id": _SNOWFLAKE, "message_id": _SNOWFLAKE}, ("channel_id", "message_id")),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "attachments.add",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _binary_upload(
            {
                "channel_id": _SNOWFLAKE,
                "description": _text(1_024, minimum=1),
                "filename": _text(255, minimum=1),
                "message_id": _SNOWFLAKE,
            },
            ("channel_id", "filename", "message_id"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "attachments.update",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {
                "attachment_id": _SNOWFLAKE,
                "channel_id": _SNOWFLAKE,
                "description": _text(1_024, minimum=1),
                "message_id": _SNOWFLAKE,
            },
            ("attachment_id", "channel_id", "message_id"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "attachments.remove",
        ConnectorEffect.PERMANENT,
        _DISCORD_BOT,
        _object(
            {
                "attachment_id": _SNOWFLAKE,
                "channel_id": _SNOWFLAKE,
                "message_id": _SNOWFLAKE,
            },
            ("attachment_id", "channel_id", "message_id"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "reactions.add",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {"channel_id": _SNOWFLAKE, "emoji": _EMOJI, "message_id": _SNOWFLAKE},
            ("channel_id", "emoji", "message_id"),
        ),
    ),
    _operation(
        "discord",
        ConnectorMode.WRITE,
        "reactions.remove",
        ConnectorEffect.OUTWARD,
        _DISCORD_BOT,
        _object(
            {"channel_id": _SNOWFLAKE, "emoji": _EMOJI, "message_id": _SNOWFLAKE},
            ("channel_id", "emoji", "message_id"),
        ),
    ),
)
