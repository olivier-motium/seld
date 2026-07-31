"""Fixed Slack and Discord execution adapters for the collaboration catalog."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from typing import Final, cast
from urllib.parse import quote

from continuity_kernel.connector_adapter import ConnectorAdapterResult, ConnectorRuntimeCredential
from continuity_kernel.connector_contract import ConnectorEffect, OperationCatalog, OperationSpec
from continuity_kernel.connector_operations_collaboration import COLLABORATION_OPERATIONS
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError

__all__ = ["CollaborationConnectorAdapter"]


_PROVIDERS: Final = frozenset({"discord", "slack"})
OPERATION_CATALOG: Final = OperationCatalog(COLLABORATION_OPERATIONS)
_MAX_BINARY_BYTES: Final = 180_000
_SLACK_CHANNEL_TYPES: Final = {
    "channel": "public_channel",
    "group": "private_channel",
    "im": "im",
    "mpim": "mpim",
}
_DISCORD_CHANNEL_TYPES: Final = {
    "announcement": 5,
    "forum": 15,
    "text": 0,
    "voice": 2,
}
_DISCORD_THREAD_TYPES: Final = {"private": 12, "public": 11}


class CollaborationConnectorAdapter:
    """Execute only explicit Slack and Discord collaboration operations."""

    @property
    def providers(self) -> frozenset[str]:
        return _PROVIDERS

    def classify_effect(self, operation: OperationSpec, input_value: object) -> ConnectorEffect:
        self._validated_input(operation, input_value)
        return operation.effect

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
        value = self._validated_input(operation, input_value)
        self._validate_runtime(operation, credential)
        if operation.provider == "slack":
            return self._execute_slack(operation, value, continuation, credential, transport)
        return self._execute_discord(
            operation,
            value,
            continuation,
            credential,
            transport,
            write_idempotency_key=write_idempotency_key,
        )

    def _validated_input(self, operation: OperationSpec, input_value: object) -> dict[str, object]:
        if not isinstance(operation, OperationSpec):
            raise ValidationError("connector operation is outside the collaboration catalog")
        try:
            canonical = OPERATION_CATALOG.lookup(operation.provider, operation.mode, operation.name)
        except ValidationError as exc:
            raise ValidationError(
                "connector operation is outside the collaboration catalog"
            ) from exc
        if operation != canonical:
            raise ValidationError(
                "connector operation differs from the canonical collaboration catalog"
            )
        value = canonical.validate_input(input_value)
        if not isinstance(value, dict):
            raise ValidationError("connector operation input is invalid")
        return value

    def _validate_runtime(
        self,
        operation: OperationSpec,
        credential: ConnectorRuntimeCredential,
    ) -> None:
        if not isinstance(credential, ConnectorRuntimeCredential):
            raise ValidationError("connector runtime credential is invalid")
        if not operation.scope_grant_satisfies(credential.granted_scopes):
            raise ValidationError("connector runtime credential lacks the required scopes")
        if operation.provider == "slack":
            if credential.credential.scheme is not AuthorizationScheme.BEARER:
                raise ValidationError("Slack connectors require bearer authorization")
        elif credential.credential.scheme is not AuthorizationScheme.BOT:
            raise ValidationError("Discord connectors require bot authorization")

    def _execute_slack(
        self,
        operation: OperationSpec,
        value: dict[str, object],
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        name = operation.name
        if name == "files.upload":
            _reject_continuation(continuation)
            return self._slack_upload(value, credential, transport)
        if name == "files.download":
            _reject_continuation(continuation)
            return self._slack_download(value, credential, transport)

        if name == "identity.get":
            _reject_continuation(continuation)
            return self._slack_request(transport, credential, ConnectorMethod.GET, "/api/auth.test")
        if name == "users.list":
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/users.list",
                query=_slack_users_list_query(value, continuation),
            )
        if name == "users.get":
            _reject_continuation(continuation)
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/users.info",
                query=_slack_user_query(value),
            )
        if name == "conversations.list":
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/conversations.list",
                query=_slack_conversations_list_query(value, continuation),
            )
        if name == "conversations.get":
            _reject_continuation(continuation)
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/conversations.info",
                query=_slack_conversation_query(value),
            )
        if name == "messages.list":
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/conversations.history",
                query=_slack_messages_list_query(value, continuation),
            )
        if name == "messages.get":
            _reject_continuation(continuation)
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/conversations.replies",
                query=_slack_message_query(value),
            )
        if name == "threads.list":
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/conversations.history",
                query=_slack_threads_list_query(value, continuation),
            )
        if name == "files.list":
            _reject_continuation(continuation)
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/files.list",
                query=_slack_files_list_query(value),
            )
        if name == "files.get":
            _reject_continuation(continuation)
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/files.info",
                query=_slack_file_query(value),
            )
        if name == "reactions.list":
            _reject_continuation(continuation)
            return self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/reactions.get",
                query=_slack_reactions_query(value),
            )

        _reject_continuation(continuation)
        route = _SLACK_WRITE_ROUTES.get(name)
        if route is None:
            raise ValidationError("Slack connector operation has no fixed route")
        method, path = route
        body = _slack_write_body(name, value)
        return self._slack_request(transport, credential, method, path, body=body)

    def _slack_request(
        self,
        transport: ConnectorTransport,
        credential: ConnectorRuntimeCredential,
        method: ConnectorMethod,
        path: str,
        *,
        query: tuple[tuple[str, str], ...] = (),
        body: object | None = None,
    ) -> ConnectorAdapterResult:
        response = transport.request(
            origin=ConnectorOrigin.SLACK,
            method=method,
            path=path,
            credential=credential.credential,
            query=query,
            json_body=body,
            expected_statuses=frozenset({200}),
        )
        payload, continuation = _slack_payload(response)
        return ConnectorAdapterResult(payload, continuation=continuation)

    def _slack_upload(
        self,
        value: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        content = _decode_base64(value["content_base64"])
        start = transport.request(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.POST,
            path="/api/files.getUploadURLExternal",
            credential=credential.credential,
            json_body={"filename": value["filename"], "length": len(content)},
            expected_statuses=frozenset({200}),
        )
        start_payload, _ = _slack_payload(start)
        upload_location = _required_text(start_payload.get("upload_url"), "Slack upload location")
        external_file_id = _required_text(start_payload.get("file_id"), "Slack file identifier")
        transport.request_provider_location(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.POST,
            location=upload_location,
            credential=None,
            body=content,
            content_type="application/octet-stream",
            expected_statuses=frozenset({200, 201, 204}),
            response_bound=_MAX_BINARY_BYTES,
        )
        file_detail: dict[str, object] = {"id": external_file_id}
        if "title" in value:
            file_detail["title"] = value["title"]
        complete_body: dict[str, object] = {
            "channel_id": value["channel"],
            "files": [file_detail],
        }
        if "thread_ts" in value:
            complete_body["thread_ts"] = value["thread_ts"]
        return self._slack_request(
            transport,
            credential,
            ConnectorMethod.POST,
            "/api/files.completeUploadExternal",
            body=complete_body,
        )

    def _slack_download(
        self,
        value: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        info = transport.request(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.GET,
            path="/api/files.info",
            credential=credential.credential,
            query=(("file", _query_text(value["file_id"])),),
            expected_statuses=frozenset({200}),
        )
        info_payload, _ = _slack_payload(info)
        file_value = info_payload.get("file")
        if not isinstance(file_value, Mapping):
            raise ConnectorProviderError(
                origin=ConnectorOrigin.SLACK,
                status=info.status,
                code="invalid_file_response",
            )
        location = _required_text(file_value.get("url_private_download"), "Slack private download")
        downloaded = transport.request_provider_location(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.GET,
            location=location,
            credential=credential.credential,
            expected_statuses=frozenset({200}),
            response_bound=_MAX_BINARY_BYTES,
        )
        return ConnectorAdapterResult(
            {
                "content_base64": base64.b64encode(downloaded.body).decode("ascii"),
                "file_id": value["file_id"],
            }
        )

    def _execute_discord(
        self,
        operation: OperationSpec,
        value: dict[str, object],
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        *,
        write_idempotency_key: str | None,
    ) -> ConnectorAdapterResult:
        _reject_continuation(continuation)
        bot = self._discord_identity(credential, transport)
        if operation.name == "identity.get":
            return ConnectorAdapterResult(bot)
        bot_id = _required_text(bot.get("id"), "Discord bot identifier")
        name = operation.name

        if name == "guilds.list":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/v10/users/@me/guilds",
                query=_discord_guilds_list_query(value),
            )
        if name == "channels.list":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                f"/api/v10/guilds/{value['guild_id']}/channels",
            )
        if name == "channels.get":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                f"/api/v10/channels/{value['channel_id']}",
            )
        if name == "threads.active":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                f"/api/v10/guilds/{value['guild_id']}/threads/active",
            )
        if name in {"threads.archived_private", "threads.archived_public"}:
            visibility = "private" if name.endswith("private") else "public"
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                f"/api/v10/channels/{value['channel_id']}/threads/archived/{visibility}",
                query=_discord_archived_threads_query(value),
            )
        if name == "messages.list":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                f"/api/v10/channels/{value['channel_id']}/messages",
                query=_discord_messages_list_query(value),
            )
        if name == "messages.get":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}",
            )
        if name == "attachments.get":
            return self._discord_download_attachment(value, credential, transport)
        if name == "reactions.list":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.GET,
                _discord_reaction_path(value),
                query=_discord_reactions_list_query(value),
            )
        if name == "dms.open":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.POST,
                "/api/v10/users/@me/channels",
                body={"recipient_id": value["user_id"]},
            )
        if name == "dms.close":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.DELETE,
                f"/api/v10/channels/{value['channel_id']}",
                statuses=frozenset({200}),
            )
        if name == "channels.create":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.POST,
                f"/api/v10/guilds/{value['guild_id']}/channels",
                body=_discord_channel_body(value, create=True),
            )
        if name == "channels.update":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.PATCH,
                f"/api/v10/channels/{value['channel_id']}",
                body=_discord_channel_body(value, create=False),
            )
        if name == "channels.delete":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.DELETE,
                f"/api/v10/channels/{value['channel_id']}",
                statuses=frozenset({200, 204}),
            )
        if name == "threads.create_from_message":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.POST,
                f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}/threads",
                body=_discord_thread_body(value, from_message=True),
            )
        if name == "threads.create":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.POST,
                f"/api/v10/channels/{value['channel_id']}/threads",
                body=_discord_thread_body(value, from_message=False),
            )
        if name in {"threads.join", "threads.leave"}:
            method = ConnectorMethod.PUT if name == "threads.join" else ConnectorMethod.DELETE
            return self._discord_request(
                transport,
                credential,
                method,
                f"/api/v10/channels/{value['thread_id']}/thread-members/@me",
                statuses=frozenset({204}),
            )
        if name == "threads.update":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.PATCH,
                f"/api/v10/channels/{value['thread_id']}",
                body=_discord_thread_body(value, from_message=False),
            )
        if name in {"threads.archive", "threads.restore"}:
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.PATCH,
                f"/api/v10/channels/{value['thread_id']}",
                body={"archived": name == "threads.archive"},
            )
        if name == "threads.delete":
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.DELETE,
                f"/api/v10/channels/{value['thread_id']}",
                statuses=frozenset({200, 204}),
            )
        if name == "messages.create":
            if write_idempotency_key is None:
                raise ValidationError("Discord message creation requires confirmed idempotency")
            result = self._discord_request(
                transport,
                credential,
                ConnectorMethod.POST,
                f"/api/v10/channels/{value['channel_id']}/messages",
                body=_discord_message_body(value, nonce=write_idempotency_key),
            )
            _reject_empty_message_content(result.payload, value)
            return result
        if name == "messages.update":
            self._discord_owned_message(value, bot_id, credential, transport)
            result = self._discord_request(
                transport,
                credential,
                ConnectorMethod.PATCH,
                f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}",
                body=_discord_message_body(value),
            )
            _reject_empty_message_content(result.payload, value)
            return result
        if name == "messages.delete":
            self._discord_owned_message(value, bot_id, credential, transport)
            return self._discord_request(
                transport,
                credential,
                ConnectorMethod.DELETE,
                f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}",
                statuses=frozenset({204}),
            )
        if name == "attachments.add":
            message = self._discord_owned_message(value, bot_id, credential, transport)
            return self._discord_add_attachment(value, message, credential, transport)
        if name == "attachments.update":
            message = self._discord_owned_message(value, bot_id, credential, transport)
            return self._discord_update_attachment(
                value, message, credential, transport, remove=False
            )
        if name == "attachments.remove":
            message = self._discord_owned_message(value, bot_id, credential, transport)
            return self._discord_update_attachment(
                value, message, credential, transport, remove=True
            )
        if name in {"reactions.add", "reactions.remove"}:
            method = ConnectorMethod.PUT if name == "reactions.add" else ConnectorMethod.DELETE
            return self._discord_request(
                transport,
                credential,
                method,
                _discord_reaction_path(value) + "/@me",
                statuses=frozenset({204}),
            )
        raise ValidationError("Discord connector operation has no fixed route")

    def _discord_identity(
        self,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> dict[str, object]:
        result = self._discord_request(
            transport,
            credential,
            ConnectorMethod.GET,
            "/api/v10/users/@me",
        )
        if not isinstance(result.payload, dict) or result.payload.get("bot") is not True:
            raise ConnectorProviderError(
                origin=ConnectorOrigin.DISCORD,
                status=200,
                code="bot_identity_required",
            )
        return result.payload

    def _discord_owned_message(
        self,
        value: dict[str, object],
        bot_id: str,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> dict[str, object]:
        result = self._discord_request(
            transport,
            credential,
            ConnectorMethod.GET,
            f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}",
        )
        if not isinstance(result.payload, dict):
            raise ConnectorProviderError(
                origin=ConnectorOrigin.DISCORD,
                status=200,
                code="invalid_message_response",
            )
        author = result.payload.get("author")
        if not isinstance(author, Mapping) or author.get("id") != bot_id:
            raise ValidationError("Discord message is not authored by the current bot")
        return result.payload

    def _discord_download_attachment(
        self,
        value: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        message = self._discord_request(
            transport,
            credential,
            ConnectorMethod.GET,
            f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}",
        )
        if not isinstance(message.payload, dict):
            raise ConnectorProviderError(
                origin=ConnectorOrigin.DISCORD,
                status=200,
                code="invalid_message_response",
            )
        attachment = _attachment_by_id(message.payload, value["attachment_id"])
        location = _required_text(attachment.get("url"), "Discord attachment location")
        response = transport.request_provider_location(
            origin=ConnectorOrigin.DISCORD,
            method=ConnectorMethod.GET,
            location=location,
            credential=None,
            expected_statuses=frozenset({200}),
            response_bound=_MAX_BINARY_BYTES,
        )
        return ConnectorAdapterResult(
            {
                "attachment_id": value["attachment_id"],
                "content_base64": base64.b64encode(response.body).decode("ascii"),
            }
        )

    def _discord_add_attachment(
        self,
        value: dict[str, object],
        message: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        content = _decode_base64(value["content_base64"])
        attachments = _existing_attachments(message)
        new_attachment: dict[str, object] = {"filename": value["filename"], "id": 0}
        if "description" in value:
            new_attachment["description"] = value["description"]
        attachments.append(new_attachment)
        body, content_type = _multipart_body(
            {"attachments": attachments}, value["filename"], content
        )
        return self._discord_request(
            transport,
            credential,
            ConnectorMethod.PATCH,
            f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}",
            raw_body=body,
            content_type=content_type,
        )

    def _discord_update_attachment(
        self,
        value: dict[str, object],
        message: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        *,
        remove: bool,
    ) -> ConnectorAdapterResult:
        target = _query_text(value["attachment_id"])
        attachments = _existing_attachments(message)
        matching = [attachment for attachment in attachments if attachment["id"] == target]
        if len(matching) != 1:
            raise ValidationError("Discord attachment is not part of the target message")
        if remove:
            shaped = [attachment for attachment in attachments if attachment["id"] != target]
        else:
            shaped = []
            for attachment in attachments:
                updated = dict(attachment)
                if attachment["id"] == target and "description" in value:
                    updated["description"] = value["description"]
                shaped.append(updated)
        return self._discord_request(
            transport,
            credential,
            ConnectorMethod.PATCH,
            f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}",
            body={"attachments": shaped},
        )

    def _discord_request(
        self,
        transport: ConnectorTransport,
        credential: ConnectorRuntimeCredential,
        method: ConnectorMethod,
        path: str,
        *,
        query: tuple[tuple[str, str], ...] = (),
        body: object | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        statuses: frozenset[int] = frozenset({200}),
    ) -> ConnectorAdapterResult:
        response = transport.request(
            origin=ConnectorOrigin.DISCORD,
            method=method,
            path=path,
            credential=credential.credential,
            query=query,
            json_body=body,
            body=raw_body,
            content_type=content_type,
            expected_statuses=statuses,
        )
        return ConnectorAdapterResult(_discord_payload(response))


_SLACK_WRITE_ROUTES: Final = {
    "dms.open": (ConnectorMethod.POST, "/api/conversations.open"),
    "dms.close": (ConnectorMethod.POST, "/api/conversations.close"),
    "channels.join": (ConnectorMethod.POST, "/api/conversations.join"),
    "channels.leave": (ConnectorMethod.POST, "/api/conversations.leave"),
    "channels.create": (ConnectorMethod.POST, "/api/conversations.create"),
    "channels.rename": (ConnectorMethod.POST, "/api/conversations.rename"),
    "channels.topic": (ConnectorMethod.POST, "/api/conversations.setTopic"),
    "channels.purpose": (ConnectorMethod.POST, "/api/conversations.setPurpose"),
    "channels.archive": (ConnectorMethod.POST, "/api/conversations.archive"),
    "channels.restore": (ConnectorMethod.POST, "/api/conversations.unarchive"),
    "messages.create": (ConnectorMethod.POST, "/api/chat.postMessage"),
    "messages.update": (ConnectorMethod.POST, "/api/chat.update"),
    "messages.delete": (ConnectorMethod.POST, "/api/chat.delete"),
    "threads.reply": (ConnectorMethod.POST, "/api/chat.postMessage"),
    "threads.update": (ConnectorMethod.POST, "/api/chat.update"),
    "threads.delete": (ConnectorMethod.POST, "/api/chat.delete"),
    "files.delete": (ConnectorMethod.POST, "/api/files.delete"),
    "reactions.add": (ConnectorMethod.POST, "/api/reactions.add"),
    "reactions.remove": (ConnectorMethod.POST, "/api/reactions.remove"),
}


def _slack_write_body(name: str, value: dict[str, object]) -> dict[str, object]:
    if name == "dms.open":
        return {"users": ",".join(cast(list[str], value["users"]))}
    if name in {
        "dms.close",
        "channels.join",
        "channels.leave",
        "channels.archive",
        "channels.restore",
    }:
        return {"channel": value["channel"]}
    if name == "channels.create":
        return _selected(value, ("is_private", "name"))
    if name == "channels.rename":
        return _selected(value, ("channel", "name"))
    if name == "channels.topic":
        return _selected(value, ("channel", "topic"))
    if name == "channels.purpose":
        return _selected(value, ("channel", "purpose"))
    if name in {"messages.create", "threads.reply"}:
        return _selected(
            value,
            (
                "blocks",
                "channel",
                "client_msg_id",
                "reply_broadcast",
                "text",
                "thread_ts",
                "unfurl_links",
                "unfurl_media",
            ),
        )
    if name in {"messages.update", "threads.update"}:
        return _selected(
            value,
            (
                "blocks",
                "channel",
                "client_msg_id",
                "reply_broadcast",
                "text",
                "ts",
                "unfurl_links",
                "unfurl_media",
            ),
        )
    if name in {"messages.delete", "threads.delete"}:
        return _selected(value, ("channel", "ts"))
    if name == "files.delete":
        return {"file": value["file_id"]}
    if name in {"reactions.add", "reactions.remove"}:
        return _selected(value, ("channel", "emoji", "timestamp"))
    raise ValidationError("Slack connector operation has no fixed body")


def _slack_users_list_query(
    value: Mapping[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    return _with_slack_cursor(query, continuation)


def _slack_user_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return (("user", _query_text(value["user"])),)


def _slack_conversations_list_query(
    value: Mapping[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    if "channel_types" in value:
        types = cast(list[str], value["channel_types"])
        query.append(("types", ",".join(_SLACK_CHANNEL_TYPES[item] for item in types)))
    return _with_slack_cursor(query, continuation)


def _slack_conversation_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return (("channel", _query_text(value["channel"])),)


def _slack_messages_list_query(
    value: Mapping[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query = [("channel", _query_text(value["channel"]))]
    if "latest" in value:
        query.append(("latest", _query_text(value["latest"])))
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    if "oldest" in value:
        query.append(("oldest", _query_text(value["oldest"])))
    return _with_slack_cursor(query, continuation)


def _slack_message_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return (
        ("channel", _query_text(value["channel"])),
        ("ts", _query_text(value["ts"])),
        ("limit", "1"),
    )


def _slack_threads_list_query(
    value: Mapping[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query = [("channel", _query_text(value["channel"]))]
    if "latest" in value:
        query.append(("latest", _query_text(value["latest"])))
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    if "oldest" in value:
        query.append(("oldest", _query_text(value["oldest"])))
    return _with_slack_cursor(query, continuation)


def _slack_files_list_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "channel" in value:
        query.append(("channel", _query_text(value["channel"])))
    if "limit" in value:
        query.append(("count", _query_text(value["limit"])))
    if "oldest" in value:
        query.append(("ts_from", _query_text(value["oldest"])))
    if "latest" in value:
        query.append(("ts_to", _query_text(value["latest"])))
    return tuple(query)


def _slack_file_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return (("file", _query_text(value["file_id"])),)


def _slack_reactions_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return (
        ("channel", _query_text(value["channel"])),
        ("timestamp", _query_text(value["timestamp"])),
        ("full", "true"),
    )


def _with_slack_cursor(
    query: list[tuple[str, str]], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    cursor = _slack_cursor(continuation)
    if cursor is not None:
        query.append(("cursor", cursor))
    return tuple(query)


def _slack_cursor(continuation: object | None) -> str | None:
    if continuation is None:
        return None
    if not isinstance(continuation, Mapping) or set(continuation) != {"next_cursor"}:
        raise ValidationError("Slack continuation is invalid")
    cursor = continuation["next_cursor"]
    if not isinstance(cursor, str) or not cursor or len(cursor) > 8_192:
        raise ValidationError("Slack continuation is invalid")
    return cursor


def _slack_payload(response: ConnectorResponse) -> tuple[dict[str, object], object | None]:
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ConnectorProviderError(
            origin=ConnectorOrigin.SLACK,
            status=response.status,
            code="invalid_slack_response",
        )
    if payload.get("ok") is not True:
        error = payload.get("error")
        code = error if isinstance(error, str) and error else "slack_operation_failed"
        raise ConnectorProviderError(
            origin=ConnectorOrigin.SLACK, status=response.status, code=code
        )
    shaped = {str(key): item for key, item in payload.items()}
    continuation: object | None = None
    metadata = shaped.get("response_metadata")
    if isinstance(metadata, Mapping):
        metadata_shaped = {str(key): item for key, item in metadata.items() if key != "next_cursor"}
        cursor = metadata.get("next_cursor")
        if isinstance(cursor, str) and cursor:
            continuation = {"next_cursor": cursor}
        if metadata_shaped:
            shaped["response_metadata"] = metadata_shaped
        else:
            shaped.pop("response_metadata", None)
    return shaped, continuation


def _discord_payload(response: ConnectorResponse) -> object:
    if not response.body:
        return {}
    return response.json()


def _discord_guilds_list_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "after" in value:
        query.append(("after", _query_text(value["after"])))
    if "before" in value:
        query.append(("before", _query_text(value["before"])))
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    return tuple(query)


def _discord_archived_threads_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "before" in value:
        query.append(("before", _query_text(value["before"])))
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    return tuple(query)


def _discord_messages_list_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "after" in value:
        query.append(("after", _query_text(value["after"])))
    if "around" in value:
        query.append(("around", _query_text(value["around"])))
    if "before" in value:
        query.append(("before", _query_text(value["before"])))
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    return tuple(query)


def _discord_reactions_list_query(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "after" in value:
        query.append(("after", _query_text(value["after"])))
    if "limit" in value:
        query.append(("limit", _query_text(value["limit"])))
    return tuple(query)


def _discord_channel_body(value: Mapping[str, object], *, create: bool) -> dict[str, object]:
    body = _selected(value, ("name", "nsfw", "parent_id", "position", "topic"))
    if "slowmode_seconds" in value:
        body["rate_limit_per_user"] = value["slowmode_seconds"]
    if create:
        channel_type = value.get("type")
        if not isinstance(channel_type, str):
            raise ValidationError("Discord channel type is invalid")
        body["type"] = _DISCORD_CHANNEL_TYPES[channel_type]
    return body


def _discord_thread_body(value: Mapping[str, object], *, from_message: bool) -> dict[str, object]:
    body = _selected(value, ("name",))
    if "archive_duration" in value:
        body["auto_archive_duration"] = value["archive_duration"]
    if not from_message and "type" in value:
        thread_type = value["type"]
        if not isinstance(thread_type, str):
            raise ValidationError("Discord thread type is invalid")
        body["type"] = _DISCORD_THREAD_TYPES[thread_type]
    return body


def _discord_message_body(
    value: Mapping[str, object], *, nonce: str | None = None
) -> dict[str, object]:
    body = _selected(value, ("content", "embeds", "message_reference"))
    if nonce is not None:
        if not isinstance(nonce, str) or not nonce or len(nonce) > 128:
            raise ValidationError("Discord message idempotency key is invalid")
        body["enforce_nonce"] = True
        body["nonce"] = nonce
    return body


def _discord_reaction_path(value: Mapping[str, object]) -> str:
    emoji = _query_text(value["emoji"])
    return (
        f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}"
        f"/reactions/{quote(emoji, safe='')}"
    )


def _reject_empty_message_content(payload: object, value: Mapping[str, object]) -> None:
    if value.get("content") and isinstance(payload, Mapping) and payload.get("content") == "":
        raise ConnectorProviderError(
            origin=ConnectorOrigin.DISCORD,
            status=200,
            code="message_content_unavailable",
        )


def _attachment_by_id(message: Mapping[str, object], attachment_id: object) -> Mapping[str, object]:
    target = _query_text(attachment_id)
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        raise ValidationError("Discord message attachment list is invalid")
    for attachment in attachments:
        if isinstance(attachment, Mapping) and attachment.get("id") == target:
            return attachment
    raise ValidationError("Discord attachment is not part of the target message")


def _existing_attachments(message: Mapping[str, object]) -> list[dict[str, object]]:
    attachments = message.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 128:
        raise ValidationError("Discord message attachment list is invalid")
    shaped: list[dict[str, object]] = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            raise ValidationError("Discord message attachment list is invalid")
        attachment_id = attachment.get("id")
        if not isinstance(attachment_id, str) or not attachment_id:
            raise ValidationError("Discord message attachment list is invalid")
        shaped.append({"id": attachment_id})
    return shaped


def _multipart_body(payload: object, filename: object, content: bytes) -> tuple[bytes, str]:
    if not isinstance(filename, str) or not filename or "\r" in filename or "\n" in filename:
        raise ValidationError("Discord attachment filename is invalid")
    boundary = "seld-discord-attachment-v1"
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    parts = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="payload_json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode("ascii")
        + payload_json
        + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode("ascii")
    )
    return parts, f"multipart/form-data; boundary={boundary}"


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValidationError("connector file content is invalid")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, binascii.Error) as exc:
        raise ValidationError("connector file content is not base64") from exc
    if len(decoded) > _MAX_BINARY_BYTES:
        raise ValidationError("connector file content exceeds its binary bound")
    return decoded


def _selected(value: Mapping[str, object], fields: Sequence[str]) -> dict[str, object]:
    return {field: value[field] for field in fields if field in value}


def _query_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    raise ValidationError("connector query value is invalid")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise ValidationError(f"{label} is invalid")
    return value


def _reject_continuation(continuation: object | None) -> None:
    if continuation is not None:
        raise ValidationError("connector operation does not accept a continuation")
