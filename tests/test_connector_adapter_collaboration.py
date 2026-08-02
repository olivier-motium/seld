from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import cast

import pytest

import continuity_kernel.connector_adapter_collaboration as collaboration_module
from continuity_kernel.connector_adapter import ConnectorRuntimeCredential
from continuity_kernel.connector_adapter_collaboration import (
    CollaborationConnectorAdapter,
    SlackUploadOutcomeUnknown,
)
from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationSpec
from continuity_kernel.connector_operations_collaboration import COLLABORATION_OPERATIONS
from continuity_kernel.connector_session import ConnectorSession
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorOutcomeUnknown,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError


@dataclass
class _FakeTransport:
    application_flags: int | None = 1 << 19
    bot: bool = True
    discord_channel_guild_id: str | None = "323456789012345678"
    discord_premium_tier: int = 0
    message_author: str = "bot-1"
    message_content: str = "confirmed"
    slack_cursor: str | None = None
    slack_error: str | None = None
    slack_complete_outcome_unknown: bool = False
    slack_complete_files: list[dict[str, object]] = field(default_factory=lambda: [{"id": "F1"}])
    slack_info_file: dict[str, object] | None = field(
        default_factory=lambda: cast(
            dict[str, object],
            {
                "channels": ["C123"],
                "id": "F1",
                "shares": {
                    "public": {
                        "C123": [{"thread_ts": "1712345678.000001"}],
                    }
                },
                "url_private_download": "https://files.slack.com/private",
            },
        )
    )
    slack_thread_messages: list[dict[str, object]] = field(
        default_factory=lambda: [
            {"ts": "1712345678.000001"},
            {"thread_ts": "1712345678.000001", "ts": "1712345678.000002"},
        ]
    )
    requests: list[dict[str, object]] = field(default_factory=list)
    locations: list[dict[str, object]] = field(default_factory=list)

    def request(self, **kwargs: object) -> ConnectorResponse:
        self.requests.append(kwargs)
        origin = cast(ConnectorOrigin, kwargs["origin"])
        path = cast(str, kwargs["path"])
        method = cast(ConnectorMethod, kwargs["method"])
        if origin is ConnectorOrigin.SLACK:
            if self.slack_error is not None:
                return _response(origin, {"error": self.slack_error, "ok": False})
            if path == "/api/files.getUploadURLExternal":
                return _response(
                    origin,
                    {"ok": True, "file_id": "F1", "upload_url": "https://files.slack.com/upload"},
                )
            if path == "/api/files.info":
                if self.slack_info_file is None:
                    return _response(origin, {"error": "file_not_found", "ok": False})
                return _response(
                    origin,
                    {
                        "ok": True,
                        "file": self.slack_info_file,
                    },
                )
            if path == "/api/files.completeUploadExternal":
                if self.slack_complete_outcome_unknown:
                    raise ConnectorOutcomeUnknown("completion response was lost")
                return _response(origin, {"files": self.slack_complete_files, "ok": True})
            metadata: dict[str, object] = {}
            if self.slack_cursor is not None:
                metadata["next_cursor"] = self.slack_cursor
                self.slack_cursor = None
            response: dict[str, object] = {"ok": True}
            if path == "/api/conversations.replies":
                query = cast(tuple[tuple[str, str], ...], kwargs.get("query", ()))
                oldest = next((item for key, item in query if key == "oldest"), None)
                response["messages"] = [
                    message
                    for message in self.slack_thread_messages
                    if oldest is None or message.get("ts") == oldest
                ]
            if metadata:
                response["response_metadata"] = metadata
            return _response(origin, response)
        if path == "/api/v10/users/@me":
            return _response(origin, {"bot": self.bot, "id": "bot-1"})
        if path == "/api/v10/oauth2/applications/@me":
            payload: dict[str, object] = {"id": "application-1"}
            if self.application_flags is not None:
                payload["flags"] = self.application_flags
            return _response(origin, payload)
        if path == "/api/v10/channels/123456789012345678" and method is ConnectorMethod.GET:
            payload = {"id": "123456789012345678"}
            if self.discord_channel_guild_id is not None:
                payload["guild_id"] = self.discord_channel_guild_id
            return _response(origin, payload)
        if (
            self.discord_channel_guild_id is not None
            and path == f"/api/v10/guilds/{self.discord_channel_guild_id}"
            and method is ConnectorMethod.GET
        ):
            return _response(origin, {"premium_tier": self.discord_premium_tier})
        if "/messages/" in path and method is ConnectorMethod.GET:
            return _response(
                origin,
                {
                    "attachments": [
                        {"id": "3", "url": "https://cdn.discordapp.com/attachments/3"},
                    ],
                    "author": {"id": self.message_author},
                    "content": self.message_content,
                },
            )
        if path.endswith("/messages") and method is ConnectorMethod.POST:
            return _response(origin, {"content": self.message_content})
        if "/messages/" in path and method is ConnectorMethod.PATCH:
            return _response(origin, {"content": self.message_content})
        if (
            path.startswith("/api/v10/channels/")
            and "/messages/" not in path
            and method is ConnectorMethod.DELETE
        ):
            return _response(origin, {"id": "closed-channel", "type": 1})
        return _response(origin, {})

    def request_provider_location(self, **kwargs: object) -> ConnectorResponse:
        self.locations.append(kwargs)
        origin = cast(ConnectorOrigin, kwargs["origin"])
        return ConnectorResponse(
            origin=origin,
            status=200,
            headers=MappingProxyType({}),
            body=b"downloaded",
        )


def _response(origin: ConnectorOrigin, body: object) -> ConnectorResponse:
    return ConnectorResponse(
        origin=origin,
        status=200,
        headers=MappingProxyType({}),
        body=json.dumps(body).encode("utf-8"),
    )


def _credential(
    operation: OperationSpec, *, scheme: AuthorizationScheme | None = None
) -> ConnectorRuntimeCredential:
    authorization = scheme or (
        AuthorizationScheme.BEARER if operation.provider == "slack" else AuthorizationScheme.BOT
    )
    scopes = tuple(sorted(operation.required_scopes[0]))
    return ConnectorRuntimeCredential(
        credential=ConnectorCredential(scheme=authorization, secret="test-secret"),
        granted_scopes=scopes,
        version=1,
    )


def _input(operation: OperationSpec) -> dict[str, object]:
    thread_reply: dict[str, object] = {
        "channel": "C123",
        "text": "Hello",
        "thread_ts": "1712345678.000001",
    }
    thread_update: dict[str, object] = {**thread_reply, "ts": "1712345678.000002"}
    thread_delete: dict[str, object] = {
        "channel": "C123",
        "thread_ts": "1712345678.000001",
        "ts": "1712345678.000002",
    }
    common_discord: dict[str, object] = {
        "channel_id": "123456789012345678",
        "message_id": "223456789012345678",
    }
    values: dict[tuple[str, str], dict[str, object]] = {
        ("slack", "identity.get"): {},
        ("slack", "users.list"): {"limit": 50},
        ("slack", "users.get"): {"user": "U123"},
        ("slack", "conversations.list"): {"channel_types": ["channel"], "limit": 50},
        ("slack", "conversations.get"): {"channel": "C123"},
        ("slack", "messages.list"): {"channel": "C123", "limit": 50},
        ("slack", "messages.get"): {"channel": "C123", "ts": "1712345678.000001"},
        ("slack", "threads.list"): {
            "channel": "C123",
            "limit": 50,
            "thread_ts": "1712345678.000001",
        },
        ("slack", "files.list"): {"count": 50, "page": 1},
        ("slack", "files.get"): {"file_id": "F123"},
        ("slack", "files.download"): {"delivery": "inline_chunk", "file_id": "F123"},
        ("slack", "reactions.list"): {"channel": "C123", "timestamp": "1712345678.000001"},
        ("slack", "dms.open"): {"users": ["U123"]},
        ("slack", "dms.close"): {"channel": "D123"},
        ("slack", "channels.join"): {"channel": "C123"},
        ("slack", "channels.leave"): {"channel": "C123"},
        ("slack", "channels.create"): {"name": "project-chat"},
        ("slack", "channels.rename"): {"channel": "C123", "name": "project-chat"},
        ("slack", "channels.topic"): {"channel": "C123", "topic": "Current work"},
        ("slack", "channels.purpose"): {"channel": "C123", "purpose": "Coordinate work"},
        ("slack", "channels.archive"): {"channel": "C123"},
        ("slack", "channels.restore"): {"channel": "C123"},
        ("slack", "messages.create"): {"channel": "C123", "text": "Hello"},
        ("slack", "messages.update"): {
            "channel": "C123",
            "text": "Hello",
            "ts": "1712345678.000001",
        },
        ("slack", "messages.delete"): {"channel": "C123", "ts": "1712345678.000001"},
        ("slack", "threads.reply"): thread_reply,
        ("slack", "threads.update"): thread_update,
        ("slack", "threads.delete"): thread_delete,
        ("slack", "files.upload"): {
            "channel": "C123",
            "content_base64": "aGVsbG8=",
            "filename": "hello.txt",
        },
        ("slack", "files.delete"): {"file_id": "F123"},
        ("slack", "reactions.add"): {
            "channel": "C123",
            "emoji": "wave",
            "timestamp": "1712345678.000001",
        },
        ("slack", "reactions.remove"): {
            "channel": "C123",
            "emoji": "wave",
            "timestamp": "1712345678.000001",
        },
        ("discord", "identity.get"): {},
        ("discord", "guilds.list"): {"limit": 50},
        ("discord", "channels.list"): {"guild_id": "123456789012345678"},
        ("discord", "channels.get"): {"channel_id": "123456789012345678"},
        ("discord", "threads.active"): {"guild_id": "123456789012345678"},
        ("discord", "threads.archived_public"): {"channel_id": "123456789012345678", "limit": 50},
        ("discord", "threads.archived_private"): {"channel_id": "123456789012345678", "limit": 50},
        ("discord", "messages.list"): {"channel_id": "123456789012345678", "limit": 50},
        ("discord", "messages.get"): common_discord,
        ("discord", "attachments.get"): {
            **common_discord,
            "attachment_id": "3",
            "delivery": "inline_chunk",
        },
        ("discord", "reactions.list"): {**common_discord, "emoji": "wave", "limit": 50},
        ("discord", "dms.open"): {"user_id": "123456789012345678"},
        ("discord", "dms.close"): {"channel_id": "123456789012345678"},
        ("discord", "channels.create"): {
            "guild_id": "123456789012345678",
            "name": "chat",
            "type": "text",
        },
        ("discord", "channels.update"): {"channel_id": "123456789012345678", "name": "chat"},
        ("discord", "channels.delete"): {"channel_id": "123456789012345678"},
        ("discord", "threads.create_from_message"): {**common_discord, "name": "A thread"},
        ("discord", "threads.create"): {
            "channel_id": "123456789012345678",
            "name": "A thread",
            "type": "public",
        },
        ("discord", "threads.join"): {"thread_id": "123456789012345678"},
        ("discord", "threads.leave"): {"thread_id": "123456789012345678"},
        ("discord", "threads.update"): {"thread_id": "123456789012345678", "name": "A thread"},
        ("discord", "threads.archive"): {"thread_id": "123456789012345678"},
        ("discord", "threads.restore"): {"thread_id": "123456789012345678"},
        ("discord", "threads.delete"): {"thread_id": "123456789012345678"},
        ("discord", "messages.create"): {
            "channel_id": "123456789012345678",
            "content": "Hello",
        },
        ("discord", "messages.update"): {**common_discord, "content": "Hello"},
        ("discord", "messages.delete"): common_discord,
        ("discord", "attachments.add"): {
            **common_discord,
            "content_base64": "aGVsbG8=",
            "filename": "hello.txt",
        },
        ("discord", "attachments.update"): {
            **common_discord,
            "attachment_id": "3",
            "description": "updated",
        },
        ("discord", "attachments.remove"): {**common_discord, "attachment_id": "3"},
        ("discord", "reactions.add"): {**common_discord, "emoji": "wave"},
        ("discord", "reactions.remove"): {**common_discord, "emoji": "wave"},
    }
    return values[(operation.provider, operation.name)]


def _operation(provider: str, mode: ConnectorMode, name: str) -> OperationSpec:
    return next(
        operation
        for operation in COLLABORATION_OPERATIONS
        if operation.provider == provider and operation.mode is mode and operation.name == name
    )


def test_adapter_executes_every_catalog_operation_through_fixed_provider_routes() -> None:
    adapter = CollaborationConnectorAdapter()
    transport = _FakeTransport()

    for operation in COLLABORATION_OPERATIONS:
        result = adapter.execute(
            operation,
            _input(operation),
            continuation=None,
            credential=_credential(operation),
            transport=cast(ConnectorTransport, transport),
            write_idempotency_key=(
                "confirmed-write-id"
                if (operation.provider == "discord" and operation.name == "messages.create")
                or (
                    operation.provider == "slack"
                    and operation.name in {"messages.create", "threads.reply"}
                )
                else None
            ),
        )
        assert result.payload is not None

    assert (
        len(
            {
                (operation.provider, operation.mode, operation.name)
                for operation in COLLABORATION_OPERATIONS
            }
        )
        == 64
    )
    assert all(cast(str, request["path"]).startswith("/api/") for request in transport.requests)
    assert all("url" not in request and "headers" not in request for request in transport.requests)


def test_adapter_requires_the_exact_canonical_operation_and_rejects_unknown_query_keys() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("slack", ConnectorMode.READ, "users.list")
    forged = replace(
        operation,
        input_schema={
            "additionalProperties": False,
            "properties": {"forged_query": {"maxLength": 8, "type": "string"}},
            "required": ["forged_query"],
            "type": "object",
        },
    )
    transport = _FakeTransport()

    with pytest.raises(ValidationError, match="canonical collaboration catalog"):
        adapter.execute(
            forged,
            {"forged_query": "unsafe"},
            continuation=None,
            credential=_credential(operation),
            transport=cast(ConnectorTransport, transport),
        )
    with pytest.raises(ValidationError):
        adapter.execute(
            operation,
            {"limit": 10, "query": "unsafe"},
            continuation=None,
            credential=_credential(operation),
            transport=cast(ConnectorTransport, transport),
        )

    assert not transport.requests


def test_local_file_limit_hook_accepts_only_the_two_sanitized_upload_shapes() -> None:
    adapter = CollaborationConnectorAdapter()
    slack = _operation("slack", ConnectorMode.WRITE, "files.upload")
    discord = _operation("discord", ConnectorMode.WRITE, "attachments.add")
    slack_transport = _FakeTransport()

    assert (
        adapter.max_local_file_bytes(
            slack,
            {
                "channel": "C123",
                "filename": "report.bin",
                "local_file": "opaque-local-file",
            },
            path=("local_file",),
            credential=_credential(slack),
            transport=cast(ConnectorTransport, slack_transport),
        )
        == 1_000_000_000
    )
    assert slack_transport.requests == []

    discord_input = {
        "channel_id": "123456789012345678",
        "filename": "report.bin",
        "local_file": "opaque-local-file",
        "message_id": "223456789012345678",
    }
    for tier, expected in ((0, 10), (1, 10), (2, 50), (3, 100), (4, 100)):
        transport = _FakeTransport(discord_premium_tier=tier)
        assert (
            adapter.max_local_file_bytes(
                discord,
                discord_input,
                path=("local_file",),
                credential=_credential(discord),
                transport=cast(ConnectorTransport, transport),
            )
            == expected * 1024**2
        )
        assert [request["path"] for request in transport.requests] == [
            "/api/v10/channels/123456789012345678",
            "/api/v10/guilds/323456789012345678",
        ]
        assert all(request["method"] is ConnectorMethod.GET for request in transport.requests)

    dm_transport = _FakeTransport(discord_channel_guild_id=None)
    assert (
        adapter.max_local_file_bytes(
            discord,
            discord_input,
            path=("local_file",),
            credential=_credential(discord),
            transport=cast(ConnectorTransport, dm_transport),
        )
        == 10 * 1024**2
    )
    assert [request["path"] for request in dm_transport.requests] == [
        "/api/v10/channels/123456789012345678"
    ]

    malformed_transport = _FakeTransport(discord_premium_tier=-1)
    with pytest.raises(ValidationError, match="premium tier"):
        adapter.max_local_file_bytes(
            discord,
            discord_input,
            path=("local_file",),
            credential=_credential(discord),
            transport=cast(ConnectorTransport, malformed_transport),
        )
    wrong_auth_transport = _FakeTransport()
    with pytest.raises(ValidationError, match="bot authorization"):
        adapter.max_local_file_bytes(
            discord,
            discord_input,
            path=("local_file",),
            credential=_credential(discord, scheme=AuthorizationScheme.BEARER),
            transport=cast(ConnectorTransport, wrong_auth_transport),
        )
    assert wrong_auth_transport.requests == []
    for operation, value, path in (
        (slack, {"local_file": "wrong-marker"}, ("local_file",)),
        (slack, {"local_file": "opaque-local-file"}, ("nested", "local_file")),
        (
            _operation("slack", ConnectorMode.READ, "files.download"),
            {"local_file": "opaque-local-file"},
            ("local_file",),
        ),
    ):
        with pytest.raises(ValidationError, match="not permitted"):
            adapter.max_local_file_bytes(
                operation,
                value,
                path=path,
                credential=_credential(operation),
                transport=cast(ConnectorTransport, _FakeTransport()),
            )


def test_slack_pagination_extracts_the_provider_cursor_and_never_exposes_it_in_payload() -> None:
    adapter = CollaborationConnectorAdapter()
    transport = _FakeTransport(slack_cursor="provider-cursor")
    operation = _operation("slack", ConnectorMode.READ, "users.list")

    first = adapter.execute(
        operation,
        {"limit": 100},
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
    )
    second = adapter.execute(
        operation,
        {"limit": 100},
        continuation=first.continuation,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
    )

    assert first.continuation == {"next_cursor": "provider-cursor"}
    assert isinstance(first.payload, Mapping)
    assert "response_metadata" not in first.payload
    assert ("cursor", "provider-cursor") in cast(
        tuple[tuple[str, str], ...], transport.requests[1]["query"]
    )
    assert second.continuation is None


def test_slack_thread_replies_use_the_required_parent_and_sealed_cursor_lane() -> None:
    adapter = CollaborationConnectorAdapter()
    transport = _FakeTransport(slack_cursor="provider-replies-cursor")
    operation = _operation("slack", ConnectorMode.READ, "threads.list")
    value = {
        "channel": "C123",
        "limit": 15,
        "thread_ts": "1712345678.000001",
    }

    first = adapter.execute(
        operation,
        value,
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
    )
    adapter.execute(
        operation,
        value,
        continuation=first.continuation,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
    )

    assert transport.requests[0]["path"] == "/api/conversations.replies"
    assert transport.requests[1]["path"] == "/api/conversations.replies"
    assert transport.requests[0]["query"] == (
        ("channel", "C123"),
        ("ts", "1712345678.000001"),
        ("limit", "15"),
    )
    assert ("cursor", "provider-replies-cursor") in cast(
        tuple[tuple[str, str], ...], transport.requests[1]["query"]
    )


def test_slack_files_list_uses_provider_page_and_count_without_cursor_fiction() -> None:
    adapter = CollaborationConnectorAdapter()
    transport = _FakeTransport()
    operation = _operation("slack", ConnectorMode.READ, "files.list")

    adapter.execute(
        operation,
        {"channel": "C123", "count": 200, "page": 3},
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
    )

    assert transport.requests[-1]["query"] == (
        ("channel", "C123"),
        ("count", "200"),
        ("page", "3"),
    )
    with pytest.raises(ValidationError, match="does not accept a continuation"):
        adapter.execute(
            operation,
            {"count": 50, "page": 2},
            continuation={"next_cursor": "not-supported"},
            credential=_credential(operation),
            transport=cast(ConnectorTransport, transport),
        )


@pytest.mark.parametrize("name", ["threads.update", "threads.delete"])
def test_slack_thread_mutations_require_membership_in_the_confirmed_parent(name: str) -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("slack", ConnectorMode.WRITE, name)
    transport = _FakeTransport(
        slack_thread_messages=[{"thread_ts": "1712345678.999999", "ts": "1712345678.000002"}]
    )

    with pytest.raises(ValidationError, match="approved thread"):
        adapter.execute(
            operation,
            _input(operation),
            continuation=None,
            credential=_credential(operation),
            transport=cast(ConnectorTransport, transport),
        )

    assert transport.requests[-1]["path"] == "/api/conversations.replies"
    assert transport.requests[-1]["query"] == (
        ("channel", "C123"),
        ("ts", "1712345678.000001"),
        ("oldest", "1712345678.000002"),
        ("latest", "1712345678.000002"),
        ("inclusive", "true"),
        ("limit", "1"),
    )
    assert not any(
        request["path"] in {"/api/chat.delete", "/api/chat.update"}
        for request in transport.requests
    )


def test_slack_rich_messages_need_no_text_and_keep_thread_routing_fields() -> None:
    adapter = CollaborationConnectorAdapter()
    transport = _FakeTransport()
    create = _operation("slack", ConnectorMode.WRITE, "messages.create")
    update = _operation("slack", ConnectorMode.WRITE, "messages.update")
    reply = _operation("slack", ConnectorMode.WRITE, "threads.reply")
    blocks = [
        {
            "text": {"emoji": True, "text": "Status", "type": "plain_text"},
            "type": "header",
        },
        {
            "fields": [{"text": "*Owner*\nOlivier", "type": "mrkdwn"}],
            "type": "section",
        },
    ]
    attachments = [{"color": "good", "fallback": "Ready", "text": "Ready to ship"}]

    adapter.execute(
        create,
        {"attachments": attachments, "channel": "C123"},
        continuation=None,
        credential=_credential(create),
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key="runtime-create-key",
    )
    adapter.execute(
        update,
        {
            "blocks": blocks,
            "channel": "C123",
            "ts": "1712345678.000002",
        },
        continuation=None,
        credential=_credential(update),
        transport=cast(ConnectorTransport, transport),
    )
    adapter.execute(
        reply,
        {
            "blocks": blocks,
            "channel": "C123",
            "reply_broadcast": True,
            "thread_ts": "1712345678.000001",
        },
        continuation=None,
        credential=_credential(reply),
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key="runtime-thread-key",
    )

    assert transport.requests[-3]["json_body"] == {
        "attachments": attachments,
        "channel": "C123",
        "client_msg_id": "runtime-create-key",
    }
    assert transport.requests[-2]["json_body"] == {
        "blocks": blocks,
        "channel": "C123",
        "ts": "1712345678.000002",
    }
    assert transport.requests[-1]["json_body"] == {
        "blocks": blocks,
        "channel": "C123",
        "client_msg_id": "runtime-thread-key",
        "reply_broadcast": True,
        "thread_ts": "1712345678.000001",
    }
    with pytest.raises(ValidationError, match="text, blocks, or attachments"):
        adapter.execute(
            create,
            {"channel": "C123"},
            continuation=None,
            credential=_credential(create),
            transport=cast(ConnectorTransport, transport),
        )


def test_slack_message_idempotency_comes_from_the_consumed_runtime_confirmation() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("slack", ConnectorMode.WRITE, "messages.create")
    credential = _credential(operation)
    value = {"channel": "C123", "text": "One confirmed message"}
    session = ConnectorSession(secret=b"s" * 32, clock=lambda: 1_000.0)
    token = session.issue_confirmation(
        provider="slack",
        operation="messages.create",
        connection_id="con-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        effect=ConnectorEffect.OUTWARD,
        authorization_tier="full",
        granted_scopes=credential.granted_scopes,
        mutation=value,
        connection_version=1,
        credential_version=credential.version,
    )
    runtime_key = session.consume_confirmation(
        token,
        provider="slack",
        operation="messages.create",
        connection_id="con-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        effect=ConnectorEffect.OUTWARD,
        authorization_tier="full",
        granted_scopes=credential.granted_scopes,
        mutation=value,
        connection_version=1,
        credential_version=credential.version,
    )
    transport = _FakeTransport()

    adapter.execute(
        operation,
        value,
        continuation=None,
        credential=credential,
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key=runtime_key,
    )

    assert (
        cast(Mapping[str, object], transport.requests[-1]["json_body"])["client_msg_id"]
        == runtime_key
    )
    with pytest.raises(ValidationError, match="confirmed idempotency"):
        adapter.execute(
            operation,
            value,
            continuation=None,
            credential=credential,
            transport=cast(ConnectorTransport, _FakeTransport()),
        )


def test_message_updates_preserve_exact_empty_removal_fields() -> None:
    adapter = CollaborationConnectorAdapter()
    slack = _operation("slack", ConnectorMode.WRITE, "messages.update")
    discord = _operation("discord", ConnectorMode.WRITE, "messages.update")
    transport = _FakeTransport()

    adapter.execute(
        slack,
        {
            "attachments": [],
            "blocks": [],
            "channel": "C123",
            "text": "",
            "ts": "1712345678.000002",
        },
        continuation=None,
        credential=_credential(slack),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.requests[-1]["json_body"] == {
        "attachments": [],
        "blocks": [],
        "channel": "C123",
        "text": "",
        "ts": "1712345678.000002",
    }

    adapter.execute(
        discord,
        {
            "channel_id": "123456789012345678",
            "components": [],
            "content": "",
            "embeds": [],
            "message_id": "223456789012345678",
        },
        continuation=None,
        credential=_credential(discord),
        transport=cast(ConnectorTransport, transport),
    )
    assert transport.requests[-1]["json_body"] == {
        "allowed_mentions": {"parse": []},
        "components": [],
        "content": "",
        "embeds": [],
    }


def test_slack_ok_false_is_a_central_provider_error_even_on_http_200() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("slack", ConnectorMode.READ, "users.list")
    transport = _FakeTransport(slack_error="not_authed")

    with pytest.raises(ConnectorProviderError, match="not_authed"):
        adapter.execute(
            operation,
            {"limit": 10},
            continuation=None,
            credential=_credential(operation),
            transport=cast(ConnectorTransport, transport),
        )

    assert len(transport.requests) == 1


def test_discord_dms_close_accepts_the_documented_channel_response() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("discord", ConnectorMode.WRITE, "dms.close")
    transport = _FakeTransport()

    result = adapter.execute(
        operation,
        _input(operation),
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
    )

    assert result.payload == {"id": "closed-channel", "type": 1}
    assert transport.requests[-1]["expected_statuses"] == frozenset({200})


def test_slack_external_upload_and_private_download_use_only_response_bound_locations() -> None:
    adapter = CollaborationConnectorAdapter()
    upload = _operation("slack", ConnectorMode.WRITE, "files.upload")
    download = _operation("slack", ConnectorMode.READ, "files.download")
    transport = _FakeTransport()

    adapter.execute(
        upload,
        _input(upload),
        continuation=None,
        credential=_credential(upload),
        transport=cast(ConnectorTransport, transport),
    )
    downloaded = adapter.execute(
        download,
        _input(download),
        continuation=None,
        credential=_credential(download),
        transport=cast(ConnectorTransport, transport),
    )

    assert transport.locations[0]["credential"] is None
    assert transport.locations[0]["location"] == "https://files.slack.com/upload"
    assert transport.locations[1]["credential"] == _credential(download).credential
    assert transport.locations[1]["location"] == "https://files.slack.com/private"
    assert downloaded.payload == {
        "content_base64": "ZG93bmxvYWRlZA==",
        "delivery": "inline_chunk",
        "file_id": "F123",
    }


def test_slack_upload_verifies_completion_and_reconciles_unknown_outcome_once() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("slack", ConnectorMode.WRITE, "files.upload")
    credential = _credential(operation)

    mismatch = _FakeTransport(
        slack_complete_files=[{"id": "F-other"}],
        slack_info_file=None,
    )
    with pytest.raises(SlackUploadOutcomeUnknown):
        adapter.execute(
            operation,
            _input(operation),
            continuation=None,
            credential=credential,
            transport=cast(ConnectorTransport, mismatch),
        )
    assert [request["path"] for request in mismatch.requests].count(
        "/api/files.completeUploadExternal"
    ) == 1
    assert [request["path"] for request in mismatch.requests].count("/api/files.info") == 1

    reconciled_transport = _FakeTransport(slack_complete_outcome_unknown=True)
    reconciled = adapter.execute(
        operation,
        {**_input(operation), "thread_ts": "1712345678.000001"},
        continuation=None,
        credential=credential,
        transport=cast(ConnectorTransport, reconciled_transport),
    )
    assert reconciled.payload == {"file_id": "F1", "reconciliation": "confirmed"}
    assert [request["path"] for request in reconciled_transport.requests].count(
        "/api/files.getUploadURLExternal"
    ) == 1
    assert [request["path"] for request in reconciled_transport.requests].count(
        "/api/files.completeUploadExternal"
    ) == 1
    assert [request["path"] for request in reconciled_transport.requests].count(
        "/api/files.info"
    ) == 1


def test_slack_upload_unresolved_completion_exposes_only_safe_recovery_identity() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("slack", ConnectorMode.WRITE, "files.upload")
    transport = _FakeTransport(
        slack_complete_outcome_unknown=True,
        slack_info_file={
            "channels": ["C123"],
            "id": "F1",
            "shares": {"public": {"C123": [{"thread_ts": "1712345678.999999"}]}},
        },
    )

    with pytest.raises(SlackUploadOutcomeUnknown) as caught:
        adapter.execute(
            operation,
            {**_input(operation), "thread_ts": "1712345678.000001"},
            continuation=None,
            credential=_credential(operation),
            transport=cast(ConnectorTransport, transport),
        )

    assert caught.value.file_id == "F1"
    assert caught.value.recovery_action == "inspect_file_before_retry"
    assert caught.value.may_allocate_replacement is False
    assert "files.get" in str(caught.value)
    assert [request["path"] for request in transport.requests].count(
        "/api/files.getUploadURLExternal"
    ) == 1
    assert [request["path"] for request in transport.requests].count(
        "/api/files.completeUploadExternal"
    ) == 1


def test_discord_requires_a_verified_bot_and_enforces_bot_authorship_before_mutation() -> None:
    adapter = CollaborationConnectorAdapter()
    create = _operation("discord", ConnectorMode.WRITE, "messages.create")
    update = _operation("discord", ConnectorMode.WRITE, "messages.update")
    bearer_transport = _FakeTransport()
    with pytest.raises(ValidationError, match="bot authorization"):
        adapter.execute(
            create,
            _input(create),
            continuation=None,
            credential=_credential(create, scheme=AuthorizationScheme.BEARER),
            transport=cast(ConnectorTransport, bearer_transport),
        )
    assert not bearer_transport.requests

    with pytest.raises(ConnectorProviderError, match="bot_identity_required"):
        adapter.execute(
            create,
            _input(create),
            continuation=None,
            credential=_credential(create),
            transport=cast(ConnectorTransport, _FakeTransport(bot=False)),
        )

    author_transport = _FakeTransport(message_author="someone-else")
    with pytest.raises(ValidationError, match="not authored"):
        adapter.execute(
            update,
            _input(update),
            continuation=None,
            credential=_credential(update),
            transport=cast(ConnectorTransport, author_transport),
        )
    assert not any(
        request["method"] is ConnectorMethod.PATCH for request in author_transport.requests
    )


def test_discord_message_nonce_is_enforced_and_empty_content_is_detected() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("discord", ConnectorMode.WRITE, "messages.create")
    transport = _FakeTransport()

    adapter.execute(
        operation,
        _input(operation),
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key="confirmed-write-id",
    )
    body = cast(Mapping[str, object], transport.requests[-1]["json_body"])
    assert body["enforce_nonce"] is True
    assert body["nonce"] == "confirmed-write-id"

    with pytest.raises(ConnectorProviderError, match="message_content_unavailable"):
        adapter.execute(
            operation,
            _input(operation),
            continuation=None,
            credential=_credential(operation),
            transport=cast(ConnectorTransport, _FakeTransport(message_content="")),
            write_idempotency_key="another-confirmed-id",
        )


def test_discord_messages_suppress_mentions_by_default_and_shape_safe_rich_content() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("discord", ConnectorMode.WRITE, "messages.create")
    transport = _FakeTransport(message_content="")
    poll = {
        "answers": [
            {"poll_media": {"text": "Now"}},
            {"poll_media": {"emoji": {"name": "🕐"}, "text": "Later"}},
        ],
        "duration": 24,
        "question": {"text": "When should we ship?"},
    }
    components = [
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
    ]

    adapter.execute(
        operation,
        {
            "channel_id": "123456789012345678",
            "components": components,
            "poll": poll,
        },
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key="rich-message-id",
    )

    body = cast(Mapping[str, object], transport.requests[-1]["json_body"])
    assert body["allowed_mentions"] == {"parse": []}
    assert body["poll"] == poll
    assert body["components"] == [
        {
            "components": [
                {
                    "label": "View status",
                    "style": 5,
                    "type": 2,
                    "url": "https://example.com/status",
                }
            ],
            "type": 1,
        }
    ]

    update = _operation("discord", ConnectorMode.WRITE, "messages.update")
    adapter.execute(
        update,
        {
            "channel_id": "123456789012345678",
            "embeds": [{"description": "Updated without plain-text content"}],
            "message_id": "223456789012345678",
        },
        continuation=None,
        credential=_credential(update),
        transport=cast(ConnectorTransport, transport),
    )
    update_body = cast(Mapping[str, object], transport.requests[-1]["json_body"])
    assert update_body == {
        "allowed_mentions": {"parse": []},
        "embeds": [{"description": "Updated without plain-text content"}],
    }


def test_discord_explicit_mentions_and_pagination_are_semantically_closed() -> None:
    adapter = CollaborationConnectorAdapter()
    message = _operation("discord", ConnectorMode.WRITE, "messages.create")
    listing = _operation("discord", ConnectorMode.READ, "messages.list")
    guilds = _operation("discord", ConnectorMode.READ, "guilds.list")
    transport = _FakeTransport()

    adapter.execute(
        message,
        {
            "allowed_mentions": {"replied_user": False, "users": ["123"]},
            "channel_id": "123456789012345678",
            "content": "Hello <@123>",
        },
        continuation=None,
        credential=_credential(message),
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key="mention-message-id",
    )
    assert cast(Mapping[str, object], transport.requests[-1]["json_body"])["allowed_mentions"] == {
        "replied_user": False,
        "users": ["123"],
    }

    adapter.execute(
        message,
        {
            "allowed_mentions": {
                "parse": ["everyone"],
                "roles": ["456"],
                "users": ["123"],
            },
            "channel_id": "123456789012345678",
            "content": "Hello @everyone <@123> <@&456>",
        },
        continuation=None,
        credential=_credential(message),
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key="non-overlap-message-id",
    )
    assert cast(Mapping[str, object], transport.requests[-1]["json_body"])["allowed_mentions"] == {
        "parse": ["everyone"],
        "roles": ["456"],
        "users": ["123"],
    }

    adapter.execute(
        message,
        {
            "allowed_mentions": {"parse": ["users"], "users": []},
            "channel_id": "123456789012345678",
            "content": "No explicit user IDs",
        },
        continuation=None,
        credential=_credential(message),
        transport=cast(ConnectorTransport, transport),
        write_idempotency_key="empty-overlap-message-id",
    )

    with pytest.raises(ValidationError, match="overlap"):
        adapter.execute(
            message,
            {
                "allowed_mentions": {"parse": ["users"], "users": ["123"]},
                "channel_id": "123456789012345678",
                "content": "Hello <@123>",
            },
            continuation=None,
            credential=_credential(message),
            transport=cast(ConnectorTransport, transport),
            write_idempotency_key="invalid-mention-message-id",
        )
    with pytest.raises(ValidationError, match="message cursor"):
        adapter.execute(
            listing,
            {
                "after": "123",
                "before": "456",
                "channel_id": "123456789012345678",
            },
            continuation=None,
            credential=_credential(listing),
            transport=cast(ConnectorTransport, transport),
        )
    with pytest.raises(ValidationError, match="guild cursor"):
        adapter.execute(
            guilds,
            {"after": "123", "before": "456"},
            continuation=None,
            credential=_credential(guilds),
            transport=cast(ConnectorTransport, transport),
        )


def test_discord_identity_reports_message_content_redaction_from_application_flags() -> None:
    adapter = CollaborationConnectorAdapter()
    operation = _operation("discord", ConnectorMode.READ, "identity.get")

    redacted = adapter.execute(
        operation,
        {},
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, _FakeTransport(application_flags=0)),
    )
    available = adapter.execute(
        operation,
        {},
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, _FakeTransport(application_flags=1 << 19)),
    )
    unknown = adapter.execute(
        operation,
        {},
        continuation=None,
        credential=_credential(operation),
        transport=cast(ConnectorTransport, _FakeTransport(application_flags=None)),
    )

    assert cast(Mapping[str, object], redacted.payload)["message_content_readback"] == {
        "source": "current_application_flags",
        "status": "redacted",
    }
    assert cast(Mapping[str, object], available.payload)["message_content_readback"] == {
        "source": "current_application_flags",
        "status": "available",
    }
    assert cast(Mapping[str, object], unknown.payload)["message_content_readback"] == {
        "source": "current_application_flags",
        "status": "unknown",
    }


def test_discord_attachment_multipart_and_raw_transport_fields_are_rejected() -> None:
    adapter = CollaborationConnectorAdapter()
    attachment = _operation("discord", ConnectorMode.WRITE, "attachments.add")
    message = _operation("discord", ConnectorMode.WRITE, "messages.create")
    transport = _FakeTransport()

    adapter.execute(
        attachment,
        _input(attachment),
        continuation=None,
        credential=_credential(attachment),
        transport=cast(ConnectorTransport, transport),
    )
    upload_request = transport.requests[-1]
    assert cast(str, upload_request["content_type"]).startswith("multipart/form-data; boundary=")
    assert b"hello" in cast(bytes, upload_request["body"])
    assert upload_request["json_body"] is None

    with pytest.raises(ValidationError):
        adapter.execute(
            message,
            {**_input(message), "url": "https://untrusted.example"},
            continuation=None,
            credential=_credential(message),
            transport=cast(ConnectorTransport, transport),
        )


def test_discord_multipart_boundary_retries_after_payload_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = iter(("collision", "safe"))
    monkeypatch.setattr(
        secrets,
        "token_hex",
        lambda _length: next(candidates),
    )

    body, content_type = collaboration_module._multipart_body(
        {"content": "seld-discord-attachment-collision"},
        "é.txt",
        b"attachment:seld-discord-attachment-collision",
    )

    assert content_type.endswith("seld-discord-attachment-safe")
    assert b"seld-discord-attachment-collision" in body
    assert b"seld-discord-attachment-safe" in body
    assert 'filename="é.txt"'.encode() in body


def test_discord_multipart_boundary_fails_closed_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "seld-discord-attachment-collision"
    attempts = 0

    def collision(_length: int) -> str:
        nonlocal attempts
        attempts += 1
        return "collision"

    monkeypatch.setattr(secrets, "token_hex", collision)
    with pytest.raises(ValidationError, match="boundary could not be made safe"):
        collaboration_module._multipart_body({"content": candidate}, "hello.txt", b"attachment")
    assert attempts == collaboration_module._MULTIPART_BOUNDARY_ATTEMPTS


@pytest.mark.parametrize(
    "filename",
    [
        'bad"name.txt',
        "bad\\name.txt",
        "bad\rname.txt",
        "bad\nname.txt",
        "bad\x00name.txt",
        "bad\x1fname.txt",
        "bad\x7fname.txt",
    ],
)
def test_discord_multipart_rejects_filename_header_injection_and_controls(
    filename: str,
) -> None:
    with pytest.raises(ValidationError, match="filename is invalid"):
        collaboration_module._multipart_body({"attachments": []}, filename, b"attachment")
