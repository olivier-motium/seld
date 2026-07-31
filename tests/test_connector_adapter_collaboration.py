from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import cast

import pytest

from continuity_kernel.connector_adapter import ConnectorRuntimeCredential
from continuity_kernel.connector_adapter_collaboration import CollaborationConnectorAdapter
from continuity_kernel.connector_contract import ConnectorMode, OperationSpec
from continuity_kernel.connector_operations_collaboration import COLLABORATION_OPERATIONS
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


@dataclass
class _FakeTransport:
    bot: bool = True
    message_author: str = "bot-1"
    message_content: str = "confirmed"
    slack_cursor: str | None = None
    slack_error: str | None = None
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
                return _response(
                    origin,
                    {
                        "ok": True,
                        "file": {"url_private_download": "https://files.slack.com/private"},
                    },
                )
            metadata: dict[str, object] = {}
            if self.slack_cursor is not None:
                metadata["next_cursor"] = self.slack_cursor
                self.slack_cursor = None
            response: dict[str, object] = {"ok": True}
            if metadata:
                response["response_metadata"] = metadata
            return _response(origin, response)
        if path == "/api/v10/users/@me":
            return _response(origin, {"bot": self.bot, "id": "bot-1"})
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
        ("slack", "threads.list"): {"channel": "C123", "limit": 50},
        ("slack", "files.list"): {"limit": 50},
        ("slack", "files.get"): {"file_id": "F123"},
        ("slack", "files.download"): {"file_id": "F123"},
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
        ("discord", "attachments.get"): {**common_discord, "attachment_id": "3"},
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
                if operation.provider == "discord" and operation.name == "messages.create"
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
    assert downloaded.payload == {"content_base64": "ZG93bmxvYWRlZA==", "file_id": "F123"}


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
