from __future__ import annotations

from collections.abc import Mapping

import pytest

from continuity_kernel.connector_contract import (
    ConnectorEffect,
    ConnectorMode,
    OperationCatalog,
    OperationSpec,
    validate_json,
)
from continuity_kernel.connector_operations_collaboration import COLLABORATION_OPERATIONS
from continuity_kernel.errors import ValidationError

CATALOG = OperationCatalog(COLLABORATION_OPERATIONS)
CONNECTION_ID = "con-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SEALED = "v1.payload.mac"


def _operation(provider: str, mode: ConnectorMode, name: str) -> OperationSpec:
    return CATALOG.lookup(provider, mode, name)


def _validated_input(operation: OperationSpec, value: object) -> dict[str, object]:
    validated = operation.validate_input(value)
    assert isinstance(validated, dict)
    return validated


def _schema_fields(schema: object) -> set[str]:
    if not isinstance(schema, Mapping):
        return set()
    fields = set(schema.get("properties", {}))
    for child in schema.values():
        if isinstance(child, Mapping):
            fields.update(_schema_fields(child))
        elif isinstance(child, (list, tuple)):
            for item in child:
                fields.update(_schema_fields(item))
    return fields


def test_catalog_has_the_exact_collaboration_operation_surface() -> None:
    slack = [operation for operation in COLLABORATION_OPERATIONS if operation.provider == "slack"]
    discord = [
        operation for operation in COLLABORATION_OPERATIONS if operation.provider == "discord"
    ]

    assert len(slack) == 32
    assert len(discord) == 32
    assert len(COLLABORATION_OPERATIONS) == 64
    assert {operation.endpoint for operation in COLLABORATION_OPERATIONS} == {
        operation.name for operation in COLLABORATION_OPERATIONS
    }


def test_effects_and_scopes_preserve_recoverable_and_permanent_boundaries() -> None:
    assert (
        _operation("slack", ConnectorMode.WRITE, "channels.archive").effect
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        _operation("slack", ConnectorMode.WRITE, "channels.restore").effect
        is ConnectorEffect.OUTWARD
    )
    assert (
        _operation("slack", ConnectorMode.WRITE, "messages.delete").effect
        is ConnectorEffect.PERMANENT
    )
    assert (
        _operation("slack", ConnectorMode.WRITE, "files.delete").effect is ConnectorEffect.PERMANENT
    )
    assert (
        _operation("discord", ConnectorMode.WRITE, "dms.close").effect
        is ConnectorEffect.DESTRUCTIVE
    )
    assert (
        _operation("discord", ConnectorMode.WRITE, "channels.delete").effect
        is ConnectorEffect.PERMANENT
    )
    assert (
        _operation("discord", ConnectorMode.WRITE, "attachments.remove").effect
        is ConnectorEffect.PERMANENT
    )
    assert _operation("slack", ConnectorMode.READ, "users.get").required_scopes == (
        frozenset({"users:read"}),
    )
    assert "users:read.email" not in {
        scope
        for operation in COLLABORATION_OPERATIONS
        for alternative in operation.required_scopes
        for scope in alternative
    }


def test_discord_is_bot_only_and_never_exposes_permission_overwrites() -> None:
    discord = [
        operation for operation in COLLABORATION_OPERATIONS if operation.provider == "discord"
    ]

    assert all(operation.required_scopes == (frozenset(),) for operation in discord)
    assert all(
        "permission_overwrites" not in _schema_fields(operation.input_schema)
        for operation in discord
    )
    forbidden = {"authorization", "cursor", "host", "method", "token", "url", "webhook"}
    assert not forbidden & {
        field.casefold()
        for operation in discord
        for field in _schema_fields(operation.input_schema)
    }


def test_representative_message_thread_and_file_inputs_are_exactly_validated() -> None:
    slack_message = _operation("slack", ConnectorMode.WRITE, "messages.create")
    assert (
        _validated_input(
            slack_message,
            {
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "Hello *team*"}},
                    {"type": "divider"},
                    {
                        "type": "context",
                        "elements": [{"type": "plain_text", "text": "Visible to all"}],
                    },
                ],
                "channel": "C123",
                "text": "Hello team",
                "unfurl_links": False,
            },
        )["text"]
        == "Hello team"
    )
    assert (
        _validated_input(
            _operation("slack", ConnectorMode.WRITE, "threads.reply"),
            {"channel": "C123", "text": "A thread reply", "thread_ts": "1712345678.000001"},
        )["thread_ts"]
        == "1712345678.000001"
    )
    assert (
        _validated_input(
            _operation("slack", ConnectorMode.WRITE, "files.upload"),
            {
                "channel": "C123",
                "content_base64": "aGVsbG8=",
                "filename": "hello.txt",
                "thread_ts": "1712345678.000001",
                "title": "Hello",
            },
        )["filename"]
        == "hello.txt"
    )
    assert (
        _validated_input(
            _operation("discord", ConnectorMode.WRITE, "messages.create"),
            {
                "channel_id": "123456789012345678",
                "content": "Hello Discord",
                "embeds": [
                    {"title": "A bounded embed", "fields": [{"name": "One", "value": "Two"}]}
                ],
                "message_reference": {"message_id": "223456789012345678"},
            },
        )["content"]
        == "Hello Discord"
    )
    assert (
        _validated_input(
            _operation("discord", ConnectorMode.WRITE, "threads.create"),
            {
                "archive_duration": 1_440,
                "channel_id": "123456789012345678",
                "name": "Design discussion",
                "type": "public",
            },
        )["type"]
        == "public"
    )
    assert (
        _validated_input(
            _operation("discord", ConnectorMode.WRITE, "attachments.add"),
            {
                "channel_id": "123456789012345678",
                "content_base64": "aGVsbG8=",
                "filename": "hello.txt",
                "message_id": "223456789012345678",
            },
        )["content_base64"]
        == "aGVsbG8="
    )


@pytest.mark.parametrize(
    ("provider", "name", "metadata"),
    [
        ("slack", "files.upload", {"channel": "C123", "filename": "report.pdf"}),
        (
            "discord",
            "attachments.add",
            {
                "channel_id": "123456789012345678",
                "filename": "report.pdf",
                "message_id": "223456789012345678",
            },
        ),
    ],
)
def test_binary_uploads_require_exactly_one_inline_or_local_file_source(
    provider: str,
    name: str,
    metadata: dict[str, object],
) -> None:
    operation = _operation(provider, ConnectorMode.WRITE, name)
    selector = {
        "grant_id": "grant-1",
        "relative_path": "exports/report.pdf",
    }

    assert (
        _validated_input(operation, {**metadata, "local_file": selector})["local_file"] == selector
    )
    assert (
        _validated_input(operation, {**metadata, "content_base64": "cGRm"})["content_base64"]
        == "cGRm"
    )
    with pytest.raises(ValidationError, match="oneOf"):
        operation.validate_input(metadata)
    with pytest.raises(ValidationError, match="oneOf"):
        operation.validate_input(
            {
                **metadata,
                "content_base64": "cGRm",
                "local_file": selector,
            }
        )


@pytest.mark.parametrize(
    ("provider", "name", "identifiers"),
    [
        ("slack", "files.download", {"file_id": "F123"}),
        (
            "discord",
            "attachments.get",
            {
                "attachment_id": "323456789012345678",
                "channel_id": "123456789012345678",
                "message_id": "223456789012345678",
            },
        ),
    ],
)
def test_binary_downloads_default_to_artifacts_and_keep_explicit_inline_compatibility(
    provider: str,
    name: str,
    identifiers: dict[str, object],
) -> None:
    operation = _operation(provider, ConnectorMode.READ, name)

    assert _validated_input(operation, identifiers) == identifiers
    assert _validated_input(operation, {**identifiers, "delivery": "artifact"})["delivery"] == (
        "artifact"
    )
    assert (
        _validated_input(operation, {**identifiers, "delivery": "inline_chunk"})["delivery"]
        == "inline_chunk"
    )
    with pytest.raises(ValidationError):
        operation.validate_input({**identifiers, "delivery": "provider_url"})


@pytest.mark.parametrize(
    ("provider", "mode", "name", "value"),
    [
        ("slack", ConnectorMode.READ, "messages.list", {"channel": "C123", "cursor": "opaque"}),
        (
            "slack",
            ConnectorMode.WRITE,
            "messages.create",
            {"channel": "C123", "client_msg_id": "caller-controlled", "text": "unsafe"},
        ),
        (
            "slack",
            ConnectorMode.WRITE,
            "files.upload",
            {
                "channel": "C123",
                "content_base64": "aGVsbG8=",
                "filename": "x",
                "upload_url": "https://example.test",
            },
        ),
        (
            "discord",
            ConnectorMode.WRITE,
            "channels.create",
            {"guild_id": "123", "name": "safe", "type": "text", "permission_overwrites": []},
        ),
        (
            "discord",
            ConnectorMode.READ,
            "messages.get",
            {"channel_id": "123", "message_id": "456", "proxy_url": "https://example.test"},
        ),
    ],
)
def test_proxy_and_unknown_fields_fail(
    provider: str,
    mode: ConnectorMode,
    name: str,
    value: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _operation(provider, mode, name).validate_input(value)


@pytest.mark.parametrize(
    ("provider", "name", "value"),
    [
        ("slack", "users.list", {"query": "staff"}),
        ("slack", "conversations.list", {"query": "project"}),
        ("slack", "messages.list", {"channel": "C123", "query": "hello"}),
        ("slack", "files.list", {"query": "report"}),
        ("discord", "channels.list", {"guild_id": "123", "limit": 10}),
        ("discord", "threads.active", {"guild_id": "123", "limit": 10}),
    ],
)
def test_catalog_rejects_unsupported_list_search_and_pagination_inputs(
    provider: str,
    name: str,
    value: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _operation(provider, ConnectorMode.READ, name).validate_input(value)


def test_tool_envelopes_expose_only_sealed_boundary_fields() -> None:
    read_schema = CATALOG.tool_input_schema("slack", ConnectorMode.READ)
    write_schema = CATALOG.tool_input_schema("discord", ConnectorMode.WRITE)
    read_call = {
        "connection_id": CONNECTION_ID,
        "cursor": SEALED,
        "input": {"channel": "C123"},
        "operation": "messages.list",
    }
    write_call = {
        "confirmation_token": SEALED,
        "connection_id": CONNECTION_ID,
        "input": {"channel_id": "123", "content": "Hello"},
        "operation": "messages.create",
    }

    assert validate_json(read_call, read_schema) == read_call
    assert validate_json(write_call, write_schema) == write_call
    for field in ("provider", "mode", "method", "url", "headers", "token"):
        with pytest.raises(ValidationError):
            validate_json({**read_call, field: "arbitrary"}, read_schema)
    with pytest.raises(ValidationError):
        validate_json({**write_call, "cursor": SEALED}, write_schema)


def test_slack_history_scope_alternatives_require_one_complete_history_grant() -> None:
    history = _operation("slack", ConnectorMode.READ, "messages.list")

    assert history.scope_grant_satisfies({"channels:history"})
    assert history.scope_grant_satisfies({"groups:history", "chat:write"})
    assert history.scope_grant_satisfies({"im:history"})
    assert history.scope_grant_satisfies({"mpim:history"})
    assert not history.scope_grant_satisfies({"channels:read"})
    assert not history.scope_grant_satisfies({"channels:read", "groups:read"})

    thread_update = _operation("slack", ConnectorMode.WRITE, "threads.update")
    assert thread_update.scope_grant_satisfies({"channels:history", "chat:write"})
    assert not thread_update.scope_grant_satisfies({"chat:write"})

    upload = _operation("slack", ConnectorMode.WRITE, "files.upload")
    assert upload.scope_grant_satisfies({"files:read", "files:write"})
    assert not upload.scope_grant_satisfies({"files:write"})


def test_rich_slack_message_text_fits_the_explicit_bound() -> None:
    text = "x" * 40_000
    operation = _operation("slack", ConnectorMode.WRITE, "messages.create")

    assert _validated_input(operation, {"channel": "C123", "text": text})["text"] == text
    with pytest.raises(ValidationError):
        operation.validate_input({"channel": "C123", "text": text + "x"})


def test_thread_file_and_archived_pagination_match_provider_shapes() -> None:
    thread = _operation("slack", ConnectorMode.READ, "threads.list")
    files = _operation("slack", ConnectorMode.READ, "files.list")
    archived = _operation("discord", ConnectorMode.READ, "threads.archived_public")

    assert (
        _validated_input(
            thread,
            {"channel": "C123", "limit": 15, "thread_ts": "1712345678.000001"},
        )["thread_ts"]
        == "1712345678.000001"
    )
    with pytest.raises(ValidationError):
        thread.validate_input({"channel": "C123", "limit": 15})
    assert _validated_input(files, {"count": 200, "page": 3}) == {
        "count": 200,
        "page": 3,
    }
    with pytest.raises(ValidationError):
        files.validate_input({"limit": 200})
    assert (
        _validated_input(
            archived,
            {"before": "2026-07-31T12:34:56.123Z", "channel_id": "123"},
        )["before"]
        == "2026-07-31T12:34:56.123Z"
    )
    with pytest.raises(ValidationError):
        archived.validate_input({"before": "123", "channel_id": "123"})


def test_discord_catalog_accepts_closed_poll_and_noninteractive_link_components() -> None:
    operation = _operation("discord", ConnectorMode.WRITE, "messages.create")
    value = {
        "allowed_mentions": {"parse": []},
        "channel_id": "123456789012345678",
        "components": [
            {
                "components": [
                    {
                        "destination": "https://example.com/status",
                        "label": "View status",
                        "type": "link_button",
                    }
                ],
                "type": "action_row",
            }
        ],
        "poll": {
            "answers": [
                {"poll_media": {"text": "Now"}},
                {"poll_media": {"text": "Later"}},
            ],
            "question": {"text": "When?"},
        },
    }

    assert _validated_input(operation, value)["poll"] == value["poll"]
    with pytest.raises(ValidationError):
        operation.validate_input(
            {
                "channel_id": "123456789012345678",
                "components": [
                    {
                        "components": [
                            {
                                "custom_id": "requires-an-inbound-receiver",
                                "label": "Click",
                                "type": "button",
                            }
                        ],
                        "type": "action_row",
                    }
                ],
            }
        )


def test_message_edit_schemas_allow_exact_removal_without_weakening_create() -> None:
    slack_create = _operation("slack", ConnectorMode.WRITE, "messages.create")
    slack_update = _operation("slack", ConnectorMode.WRITE, "messages.update")
    discord_create = _operation("discord", ConnectorMode.WRITE, "messages.create")
    discord_update = _operation("discord", ConnectorMode.WRITE, "messages.update")

    assert (
        _validated_input(
            slack_update,
            {
                "attachments": [],
                "blocks": [],
                "channel": "C123",
                "text": "",
                "ts": "1712345678.000001",
            },
        )["blocks"]
        == []
    )
    for field, empty in (("attachments", []), ("blocks", []), ("text", "")):
        with pytest.raises(ValidationError):
            slack_create.validate_input({"channel": "C123", field: empty})

    assert (
        _validated_input(
            discord_update,
            {
                "channel_id": "123",
                "components": [],
                "content": "",
                "embeds": [],
                "message_id": "456",
            },
        )["content"]
        == ""
    )
    for field, empty in (("components", []), ("content", ""), ("embeds", [])):
        with pytest.raises(ValidationError):
            discord_create.validate_input({"channel_id": "123", field: empty})
