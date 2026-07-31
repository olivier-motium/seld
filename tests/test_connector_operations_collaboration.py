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
                "client_msg_id": "client-123",
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
    ("provider", "mode", "name", "value"),
    [
        ("slack", ConnectorMode.READ, "messages.list", {"channel": "C123", "cursor": "opaque"}),
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


def test_rich_slack_message_text_fits_the_explicit_bound() -> None:
    text = "x" * 40_000
    operation = _operation("slack", ConnectorMode.WRITE, "messages.create")

    assert _validated_input(operation, {"channel": "C123", "text": text})["text"] == text
    with pytest.raises(ValidationError):
        operation.validate_input({"channel": "C123", "text": text + "x"})
