"""Fixed Slack and Discord execution adapters for the collaboration catalog."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Final, cast
from urllib.parse import quote

from continuity_kernel.connector_adapter import (
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_contract import ConnectorEffect, OperationCatalog, OperationSpec
from continuity_kernel.connector_operations_collaboration import COLLABORATION_OPERATIONS
from continuity_kernel.connector_transfer import (
    MAX_ARTIFACT_BYTES,
    ConnectorInputPath,
    PreparedUpload,
    sanitize_artifact_name,
)
from continuity_kernel.connector_transport import (
    MAX_STREAM_CHUNK_BYTES,
    AuthorizationScheme,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorOutcomeUnknown,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ContinuityError, ValidationError

__all__ = ["CollaborationConnectorAdapter", "SlackUploadOutcomeUnknown"]


_PROVIDERS: Final = frozenset({"discord", "slack"})
OPERATION_CATALOG: Final = OperationCatalog(COLLABORATION_OPERATIONS)
_COLLABORATION_BY_KEY: Final = {operation.key: operation for operation in COLLABORATION_OPERATIONS}
_MAX_BINARY_BYTES: Final = 180_000
_SLACK_MAX_LOCAL_FILE_BYTES: Final = 1_000_000_000
_DISCORD_DEFAULT_LOCAL_FILE_BYTES: Final = 10 * 1024**2
_DISCORD_TIER_2_LOCAL_FILE_BYTES: Final = 50 * 1024**2
_DISCORD_TIER_3_LOCAL_FILE_BYTES: Final = 100 * 1024**2
_LOCAL_FILE_LIMIT_MARKER: Final = "opaque-local-file"
_LOCAL_FILE_PLACEHOLDER: Final = {
    "grant_id": "prepared",
    "relative_path": "prepared",
}
_LOCAL_UPLOAD_OPERATIONS: Final = frozenset(
    {
        ("discord", "attachments.add"),
        ("slack", "files.upload"),
    }
)
_MULTIPART_BOUNDARY_ATTEMPTS: Final = 8
_MULTIPART_BOUNDARY_BYTES: Final = 16
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
_DISCORD_MESSAGE_CONTENT_FLAGS: Final = (1 << 18) | (1 << 19)


class SlackUploadOutcomeUnknown(ConnectorOutcomeUnknown):
    """Completion may have committed; expose only the allocated ID and safe recovery."""

    def __init__(self, file_id: str) -> None:
        safe_file_id = _required_slack_id(file_id, "Slack file identifier")
        self.file_id = safe_file_id
        self.recovery_action = "inspect_file_before_retry"
        self.may_allocate_replacement = False
        super().__init__(
            f"Slack upload completion is unresolved for allocated file {safe_file_id}; "
            "inspect it with files.get before retrying and do not allocate a replacement"
        )


class CollaborationConnectorAdapter:
    """Execute only explicit Slack and Discord collaboration operations."""

    @property
    def providers(self) -> frozenset[str]:
        return _PROVIDERS

    def max_local_file_bytes(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        path: ConnectorInputPath,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> int:
        """Return the provider policy ceiling for one sanitized local-file input."""

        expected = (
            _COLLABORATION_BY_KEY.get(operation.key)
            if isinstance(operation, OperationSpec)
            else None
        )
        if expected is None or operation != expected:
            raise ValidationError("collaboration operation is not in the collaboration catalog")
        if not (
            (operation.provider, operation.name) in _LOCAL_UPLOAD_OPERATIONS
            and path == ("local_file",)
            and isinstance(input_value, Mapping)
            and input_value.get("local_file") == _LOCAL_FILE_LIMIT_MARKER
        ):
            raise ValidationError("collaboration local-file upload path is not permitted")
        self._validate_runtime(operation, credential)
        if operation.provider == "slack":
            return _SLACK_MAX_LOCAL_FILE_BYTES
        return self._discord_local_file_limit(input_value, credential, transport)

    def classify_effect(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorEffect:
        self._validated_input(operation, input_value, transfer=transfer)
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
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorAdapterResult:
        value = self._validated_input(operation, input_value, transfer=transfer)
        self._validate_runtime(operation, credential)
        if operation.provider == "slack":
            return self._execute_slack(
                operation,
                value,
                continuation,
                credential,
                transport,
                write_idempotency_key=write_idempotency_key,
                transfer=transfer,
            )
        return self._execute_discord(
            operation,
            value,
            continuation,
            credential,
            transport,
            write_idempotency_key=write_idempotency_key,
            transfer=transfer,
        )

    def _validated_input(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        transfer: ConnectorTransferContext | None,
    ) -> dict[str, object]:
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
        validation_input = _prepared_validation_input(operation, input_value, transfer=transfer)
        value = canonical.validate_input(validation_input)
        if not isinstance(value, dict):
            raise ValidationError("connector operation input is invalid")
        _validate_collaboration_semantics(
            canonical,
            value,
            input_value=input_value,
            transfer=transfer,
        )
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

    def _discord_local_file_limit(
        self,
        input_value: Mapping[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> int:
        channel_id = _required_discord_id(
            input_value.get("channel_id"),
            "Discord channel identifier",
        )
        channel = self._discord_request(
            transport,
            credential,
            ConnectorMethod.GET,
            f"/api/v10/channels/{channel_id}",
        ).payload
        if not isinstance(channel, Mapping):
            raise ConnectorProviderError(
                origin=ConnectorOrigin.DISCORD,
                status=200,
                code="invalid_channel_response",
            )
        raw_guild_id = channel.get("guild_id")
        if raw_guild_id is None:
            return _DISCORD_DEFAULT_LOCAL_FILE_BYTES
        guild_id = _required_discord_id(raw_guild_id, "Discord guild identifier")
        guild = self._discord_request(
            transport,
            credential,
            ConnectorMethod.GET,
            f"/api/v10/guilds/{guild_id}",
        ).payload
        if not isinstance(guild, Mapping):
            raise ConnectorProviderError(
                origin=ConnectorOrigin.DISCORD,
                status=200,
                code="invalid_guild_response",
            )
        tier = guild.get("premium_tier")
        if type(tier) is not int or tier < 0:
            raise ValidationError("Discord guild premium tier is invalid")
        if tier >= 3:
            return _DISCORD_TIER_3_LOCAL_FILE_BYTES
        if tier == 2:
            return _DISCORD_TIER_2_LOCAL_FILE_BYTES
        return _DISCORD_DEFAULT_LOCAL_FILE_BYTES

    def _execute_slack(
        self,
        operation: OperationSpec,
        value: dict[str, object],
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        *,
        write_idempotency_key: str | None,
        transfer: ConnectorTransferContext | None,
    ) -> ConnectorAdapterResult:
        name = operation.name
        if name == "files.upload":
            _reject_continuation(continuation)
            return self._slack_upload(value, credential, transport, transfer=transfer)
        if name == "files.download":
            _reject_continuation(continuation)
            return self._slack_download(value, credential, transport, transfer=transfer)

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
                "/api/conversations.replies",
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
        if name in {"threads.delete", "threads.update"}:
            self._slack_assert_thread_member(value, credential, transport)
        route = _SLACK_WRITE_ROUTES.get(name)
        if route is None:
            raise ValidationError("Slack connector operation has no fixed route")
        method, path = route
        body = _slack_write_body(
            name,
            value,
            write_idempotency_key=write_idempotency_key,
        )
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

    def _slack_assert_thread_member(
        self,
        value: Mapping[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> None:
        target = _query_text(value["ts"])
        parent = _query_text(value["thread_ts"])
        result = self._slack_request(
            transport,
            credential,
            ConnectorMethod.GET,
            "/api/conversations.replies",
            query=(
                ("channel", _query_text(value["channel"])),
                ("ts", parent),
                ("oldest", target),
                ("latest", target),
                ("inclusive", "true"),
                ("limit", "1"),
            ),
        )
        payload = result.payload
        messages = payload.get("messages") if isinstance(payload, Mapping) else None
        if not isinstance(messages, list) or len(messages) > 1:
            raise ValidationError("Slack thread membership response is invalid")
        for message in messages:
            if not isinstance(message, Mapping) or message.get("ts") != target:
                continue
            if target == parent or message.get("thread_ts") == parent:
                return
        raise ValidationError("Slack message is not a member of the approved thread")

    def _slack_upload(
        self,
        value: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        *,
        transfer: ConnectorTransferContext | None,
    ) -> ConnectorAdapterResult:
        source = _upload_source(value, transfer)
        upload = source if isinstance(source, PreparedUpload) else None
        content_length = upload.size if upload is not None else len(cast(bytes, source))
        start = transport.request(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.POST,
            path="/api/files.getUploadURLExternal",
            credential=credential.credential,
            json_body={"filename": value["filename"], "length": content_length},
            expected_statuses=frozenset({200}),
        )
        start_payload, _ = _slack_payload(start)
        upload_location = _required_text(start_payload.get("upload_url"), "Slack upload location")
        external_file_id = _required_slack_id(start_payload.get("file_id"), "Slack file identifier")
        if upload is not None:
            transport.request_stream(
                origin=ConnectorOrigin.SLACK,
                method=ConnectorMethod.POST,
                source=upload,
                content_length=upload.size,
                location=upload_location,
                credential=None,
                content_type="application/octet-stream",
                expected_statuses=frozenset({200, 201, 204}),
            )
        else:
            transport.request_provider_location(
                origin=ConnectorOrigin.SLACK,
                method=ConnectorMethod.POST,
                location=upload_location,
                credential=None,
                body=cast(bytes, source),
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
        try:
            completed = self._slack_request(
                transport,
                credential,
                ConnectorMethod.POST,
                "/api/files.completeUploadExternal",
                body=complete_body,
            )
        except ConnectorOutcomeUnknown:
            return self._slack_reconcile_or_raise(
                external_file_id,
                value,
                credential,
                transport,
            )
        try:
            _slack_completed_file(completed.payload, external_file_id)
        except ConnectorProviderError as exc:
            if exc.code != "upload_completion_mismatch":
                raise
            return self._slack_reconcile_or_raise(
                external_file_id,
                value,
                credential,
                transport,
            )
        return completed

    def _slack_reconcile_or_raise(
        self,
        file_id: str,
        value: Mapping[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        if self._slack_reconcile_upload(file_id, value, credential, transport):
            return ConnectorAdapterResult(
                {
                    "file_id": file_id,
                    "reconciliation": "confirmed",
                }
            )
        raise SlackUploadOutcomeUnknown(file_id) from None

    def _slack_reconcile_upload(
        self,
        file_id: str,
        value: Mapping[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> bool:
        try:
            result = self._slack_request(
                transport,
                credential,
                ConnectorMethod.GET,
                "/api/files.info",
                query=(("file", file_id),),
            )
            file_value = result.payload.get("file") if isinstance(result.payload, Mapping) else None
            return isinstance(file_value, Mapping) and _slack_file_matches_completion(
                file_value,
                file_id=file_id,
                channel=_query_text(value["channel"]),
                thread_ts=(_query_text(value["thread_ts"]) if "thread_ts" in value else None),
            )
        except ContinuityError:
            return False

    def _slack_download(
        self,
        value: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        *,
        transfer: ConnectorTransferContext | None,
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
        delivery = _download_delivery(value)
        if delivery == "artifact":
            if transfer is None:
                raise ValidationError("Slack artifact delivery requires transfer context")
            filename = sanitize_artifact_name(
                _required_text(file_value.get("name"), "Slack file name")
            )
            media_type = _artifact_media_type(file_value.get("mimetype"), "Slack MIME type")
            size = _required_download_size(file_value.get("size"), "Slack file size")
            writer = transfer.artifacts.start(
                filename,
                media_type=media_type,
                expected_size=size,
            )
            try:
                downloaded = transport.download_stream(
                    origin=ConnectorOrigin.SLACK,
                    sink=writer,
                    location=location,
                    credential=credential.credential,
                    expected_length=size,
                    expected_statuses=frozenset({200}),
                    max_bytes=MAX_ARTIFACT_BYTES,
                )
            except Exception:
                writer.abort()
                raise
            if downloaded.artifact is None:
                writer.abort()
                raise ValidationError("Slack artifact delivery produced no receipt")
            return ConnectorAdapterResult(
                {
                    "bytes": downloaded.bytes_transferred,
                    "delivery": "artifact",
                    "file_id": value["file_id"],
                },
                artifact=downloaded.artifact,
            )
        inline_download = transport.request_provider_location(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.GET,
            location=location,
            credential=credential.credential,
            expected_statuses=frozenset({200}),
            response_bound=_MAX_BINARY_BYTES,
        )
        return ConnectorAdapterResult(
            {
                "content_base64": base64.b64encode(inline_download.body).decode("ascii"),
                "delivery": "inline_chunk",
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
        transfer: ConnectorTransferContext | None,
        write_idempotency_key: str | None,
    ) -> ConnectorAdapterResult:
        _reject_continuation(continuation)
        bot = self._discord_identity(credential, transport)
        if operation.name == "identity.get":
            application = self._discord_application(credential, transport)
            return ConnectorAdapterResult(_discord_identity_report(bot, application))
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
            return self._discord_download_attachment(
                value,
                credential,
                transport,
                transfer=transfer,
            )
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
            return self._discord_add_attachment(
                value,
                message,
                credential,
                transport,
                transfer=transfer,
            )
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

    def _discord_application(
        self,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> dict[str, object]:
        result = self._discord_request(
            transport,
            credential,
            ConnectorMethod.GET,
            "/api/v10/oauth2/applications/@me",
        )
        if not isinstance(result.payload, dict):
            raise ConnectorProviderError(
                origin=ConnectorOrigin.DISCORD,
                status=200,
                code="invalid_application_response",
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
        *,
        transfer: ConnectorTransferContext | None,
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
        delivery = _download_delivery(value)
        if delivery == "artifact":
            if transfer is None:
                raise ValidationError("Discord artifact delivery requires transfer context")
            filename = sanitize_artifact_name(
                _required_text(attachment.get("filename"), "Discord attachment filename")
            )
            media_type = _artifact_media_type(
                attachment.get("content_type"),
                "Discord attachment media type",
            )
            size = _required_download_size(attachment.get("size"), "Discord attachment size")
            writer = transfer.artifacts.start(
                filename,
                media_type=media_type,
                expected_size=size,
            )
            try:
                downloaded = transport.download_stream(
                    origin=ConnectorOrigin.DISCORD,
                    sink=writer,
                    location=location,
                    credential=None,
                    expected_length=size,
                    expected_statuses=frozenset({200}),
                    max_bytes=MAX_ARTIFACT_BYTES,
                )
            except Exception:
                writer.abort()
                raise
            if downloaded.artifact is None:
                writer.abort()
                raise ValidationError("Discord artifact delivery produced no receipt")
            return ConnectorAdapterResult(
                {
                    "attachment_id": value["attachment_id"],
                    "bytes": downloaded.bytes_transferred,
                    "delivery": "artifact",
                },
                artifact=downloaded.artifact,
            )
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
                "delivery": "inline_chunk",
            }
        )

    def _discord_add_attachment(
        self,
        value: dict[str, object],
        message: dict[str, object],
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        *,
        transfer: ConnectorTransferContext | None,
    ) -> ConnectorAdapterResult:
        source = _upload_source(value, transfer)
        attachments = _existing_attachments(message)
        new_attachment: dict[str, object] = {"filename": value["filename"], "id": 0}
        if "description" in value:
            new_attachment["description"] = value["description"]
        attachments.append(new_attachment)
        payload = {"attachments": attachments}
        if isinstance(source, PreparedUpload):
            multipart = _DiscordMultipartUpload(payload, value["filename"], source)
            response = transport.request_stream(
                origin=ConnectorOrigin.DISCORD,
                method=ConnectorMethod.PATCH,
                source=multipart,
                content_length=multipart.size,
                path=(f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}"),
                credential=credential.credential,
                content_type=multipart.content_type,
                expected_statuses=frozenset({200}),
            )
            return ConnectorAdapterResult(_discord_stream_payload(response))
        body, content_type = _multipart_body(
            payload,
            value["filename"],
            source,
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


def _slack_write_body(
    name: str,
    value: dict[str, object],
    *,
    write_idempotency_key: str | None,
) -> dict[str, object]:
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
        body = _selected(
            value,
            (
                "attachments",
                "blocks",
                "channel",
                "reply_broadcast",
                "text",
                "thread_ts",
                "unfurl_links",
                "unfurl_media",
            ),
        )
        body["client_msg_id"] = _slack_idempotency_key(write_idempotency_key)
        return body
    if name in {"messages.update", "threads.update"}:
        return _selected(
            value,
            (
                "attachments",
                "blocks",
                "channel",
                "text",
                "ts",
            ),
        )
    if name in {"messages.delete", "threads.delete"}:
        return _selected(value, ("channel", "ts"))
    if name == "files.delete":
        return {"file": value["file_id"]}
    if name in {"reactions.add", "reactions.remove"}:
        return _selected(value, ("channel", "emoji", "timestamp"))
    raise ValidationError("Slack connector operation has no fixed body")


def _slack_idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 128
        or not value.isascii()
        or any(not (character.isalnum() or character in "_-") for character in value)
    ):
        raise ValidationError("Slack message creation requires confirmed idempotency")
    return value


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
    query = [
        ("channel", _query_text(value["channel"])),
        ("ts", _query_text(value["thread_ts"])),
    ]
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
    if "count" in value:
        query.append(("count", _query_text(value["count"])))
    if "oldest" in value:
        query.append(("ts_from", _query_text(value["oldest"])))
    if "latest" in value:
        query.append(("ts_to", _query_text(value["latest"])))
    if "page" in value:
        query.append(("page", _query_text(value["page"])))
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


def _slack_completed_file(payload: object, file_id: str) -> Mapping[str, object]:
    files = payload.get("files") if isinstance(payload, Mapping) else None
    if not isinstance(files, list) or not 1 <= len(files) <= 128:
        raise ConnectorProviderError(
            origin=ConnectorOrigin.SLACK,
            status=200,
            code="upload_completion_mismatch",
        )
    matches = [
        file_value
        for file_value in files
        if isinstance(file_value, Mapping) and file_value.get("id") == file_id
    ]
    if len(matches) != 1:
        raise ConnectorProviderError(
            origin=ConnectorOrigin.SLACK,
            status=200,
            code="upload_completion_mismatch",
        )
    return matches[0]


def _slack_file_matches_completion(
    file_value: Mapping[str, object],
    *,
    file_id: str,
    channel: str,
    thread_ts: str | None,
) -> bool:
    if file_value.get("id") != file_id:
        return False
    channel_bound = any(
        isinstance(channels, list) and channel in channels
        for channels in (
            file_value.get("channels"),
            file_value.get("groups"),
            file_value.get("ims"),
        )
    )
    shares = file_value.get("shares")
    if isinstance(shares, Mapping):
        for visibility in shares.values():
            if not isinstance(visibility, Mapping):
                continue
            records = visibility.get(channel)
            if not isinstance(records, list):
                continue
            channel_bound = True
            if thread_ts is None:
                return True
            if any(
                isinstance(record, Mapping) and record.get("thread_ts") == thread_ts
                for record in records
            ):
                return True
    return channel_bound and thread_ts is None


def _discord_payload(response: ConnectorResponse) -> object:
    if not response.body:
        return {}
    return response.json()


def _discord_stream_payload(response: ConnectorStreamResponse) -> object:
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
    body = _selected(value, ("content", "embeds", "message_reference", "poll"))
    body["allowed_mentions"] = _discord_allowed_mentions(value.get("allowed_mentions"))
    if "components" in value:
        body["components"] = _discord_components(value["components"])
    if nonce is not None:
        if not isinstance(nonce, str) or not nonce or len(nonce) > 128:
            raise ValidationError("Discord message idempotency key is invalid")
        body["enforce_nonce"] = True
        body["nonce"] = nonce
    return body


def _discord_allowed_mentions(value: object) -> dict[str, object]:
    if value is None:
        return {"parse": []}
    if not isinstance(value, Mapping):
        raise ValidationError("Discord allowed mentions are invalid")
    shaped = {str(key): item for key, item in value.items()}
    if "parse" not in shaped and "roles" not in shaped and "users" not in shaped:
        shaped["parse"] = []
    return shaped


def _discord_components(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValidationError("Discord message components are invalid")
    rows: list[dict[str, object]] = []
    for row in value:
        if not isinstance(row, Mapping) or row.get("type") != "action_row":
            raise ValidationError("Discord message components are invalid")
        items = row.get("components")
        if not isinstance(items, list):
            raise ValidationError("Discord message components are invalid")
        buttons: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, Mapping) or item.get("type") != "link_button":
                raise ValidationError("Discord message components are invalid")
            button = _selected(item, ("disabled", "emoji", "label"))
            button["style"] = 5
            button["type"] = 2
            button["url"] = item["destination"]
            buttons.append(button)
        rows.append({"components": buttons, "type": 1})
    return rows


def _discord_reaction_path(value: Mapping[str, object]) -> str:
    emoji = _query_text(value["emoji"])
    return (
        f"/api/v10/channels/{value['channel_id']}/messages/{value['message_id']}"
        f"/reactions/{quote(emoji, safe='')}"
    )


def _discord_identity_report(
    bot: Mapping[str, object], application: Mapping[str, object]
) -> dict[str, object]:
    report = {str(key): item for key, item in bot.items()}
    flags = _discord_application_flags(application)
    if flags is None:
        status = "unknown"
    elif flags & _DISCORD_MESSAGE_CONTENT_FLAGS:
        status = "available"
    else:
        status = "redacted"
    report["message_content_readback"] = {
        "source": "current_application_flags",
        "status": status,
    }
    return report


def _discord_application_flags(application: Mapping[str, object]) -> int | None:
    flags = application.get("flags")
    if type(flags) is int and flags >= 0:
        return flags
    flags_new = application.get("flags_new")
    if isinstance(flags_new, str) and flags_new.isascii() and flags_new.isdigit():
        try:
            parsed = int(flags_new)
        except ValueError:
            return None
        if parsed >= 0:
            return parsed
    return None


def _prepared_validation_input(
    operation: OperationSpec,
    input_value: object,
    *,
    transfer: ConnectorTransferContext | None,
) -> object:
    if (
        (operation.provider, operation.name) not in _LOCAL_UPLOAD_OPERATIONS
        or not isinstance(input_value, Mapping)
        or "content_base64" in input_value
        or "local_file" in input_value
        or transfer is None
        or ("local_file",) not in transfer.uploads
    ):
        return input_value
    prepared = dict(input_value)
    prepared["local_file"] = _LOCAL_FILE_PLACEHOLDER
    return prepared


def _validate_transfer_source(
    operation: OperationSpec,
    input_value: object,
    transfer: ConnectorTransferContext | None,
) -> None:
    upload_operation = (operation.provider, operation.name) in _LOCAL_UPLOAD_OPERATIONS
    uploads = frozenset() if transfer is None else frozenset(transfer.uploads)
    if not upload_operation:
        if uploads:
            raise ValidationError("collaboration operation received unexpected prepared uploads")
        return
    if not isinstance(input_value, Mapping):
        raise ValidationError("collaboration upload input is invalid")
    expected_path = ("local_file",)
    if uploads and uploads != {expected_path}:
        raise ValidationError("collaboration upload has an unexpected prepared-file binding")
    has_inline = "content_base64" in input_value
    has_selector = "local_file" in input_value
    has_prepared = expected_path in uploads
    if has_selector:
        raise ValidationError(
            "collaboration local-file selector must be prepared by the connector runtime"
        )
    if has_inline and has_prepared:
        raise ValidationError("collaboration upload has multiple binary sources")
    if not has_inline and not has_prepared:
        raise ValidationError("collaboration upload has no prepared or inline binary source")
    if has_prepared and transfer is not None and transfer.upload(expected_path).size == 0:
        raise ValidationError("collaboration prepared upload cannot be empty")


def _validate_collaboration_semantics(
    operation: OperationSpec,
    value: Mapping[str, object],
    *,
    input_value: object,
    transfer: ConnectorTransferContext | None,
) -> None:
    _validate_transfer_source(operation, input_value, transfer)
    if operation.provider == "slack":
        if operation.name in {
            "messages.create",
            "messages.update",
            "threads.reply",
            "threads.update",
        }:
            _validate_slack_message(value)
        return

    if operation.name == "guilds.list":
        _require_at_most_one(value, ("after", "before"), "Discord guild cursor")
    elif operation.name == "messages.list":
        _require_at_most_one(
            value,
            ("after", "around", "before"),
            "Discord message cursor",
        )
    elif operation.name in {"messages.create", "messages.update"}:
        _validate_discord_message(operation.name, value)


def _validate_slack_message(value: Mapping[str, object]) -> None:
    if not any(field in value for field in ("attachments", "blocks", "text")):
        raise ValidationError("Slack messages require text, blocks, or attachments")
    blocks = value.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if (
                isinstance(block, Mapping)
                and block.get("type") == "section"
                and "text" not in block
                and "fields" not in block
            ):
                raise ValidationError("Slack section blocks require text or fields")
    attachments = value.get("attachments")
    if isinstance(attachments, list):
        visible_fields = frozenset({"fallback", "fields", "footer", "pretext", "text", "title"})
        for attachment in attachments:
            if not isinstance(attachment, Mapping) or not visible_fields & set(attachment):
                raise ValidationError("Slack attachments require visible content")
    if value.get("reply_broadcast") is True and "thread_ts" not in value:
        raise ValidationError("Slack reply broadcast requires a thread timestamp")


def _validate_discord_message(name: str, value: Mapping[str, object]) -> None:
    content_fields = ("components", "content", "embeds", "poll")
    if not any(field in value for field in content_fields):
        raise ValidationError("Discord messages require content, embeds, poll, or components")
    if name == "messages.update" and "poll" in value:
        raise ValidationError("Discord polls cannot be edited")

    embeds = value.get("embeds")
    if isinstance(embeds, list):
        total_characters = 0
        for embed in embeds:
            if not isinstance(embed, Mapping):
                raise ValidationError("Discord embeds are invalid")
            if not any(
                field in embed for field in ("author", "description", "fields", "footer", "title")
            ):
                raise ValidationError("Discord embeds require visible content")
            total_characters += _discord_embed_characters(embed)
        if total_characters > 6_000:
            raise ValidationError("Discord embed text exceeds the combined message bound")

    allowed_mentions = value.get("allowed_mentions")
    if isinstance(allowed_mentions, Mapping):
        parse = allowed_mentions.get("parse")
        if isinstance(parse, list):
            parsed = cast(list[str], parse)
            if len(set(parsed)) != len(parsed):
                raise ValidationError("Discord allowed mention types must be unique")
            users = allowed_mentions.get("users")
            roles = allowed_mentions.get("roles")
            if "users" in parsed and isinstance(users, list) and users:
                raise ValidationError("Discord parsed users overlap with explicit user mentions")
            if "roles" in parsed and isinstance(roles, list) and roles:
                raise ValidationError("Discord parsed roles overlap with explicit role mentions")


def _discord_embed_characters(embed: Mapping[str, object]) -> int:
    total = 0
    for field in ("description", "title"):
        value = embed.get(field)
        if isinstance(value, str):
            total += len(value)
    for field in ("author", "footer"):
        value = embed.get(field)
        if isinstance(value, Mapping):
            text = value.get("name" if field == "author" else "text")
            if isinstance(text, str):
                total += len(text)
    fields = embed.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if isinstance(field, Mapping):
                for key in ("name", "value"):
                    text = field.get(key)
                    if isinstance(text, str):
                        total += len(text)
    return total


def _require_at_most_one(value: Mapping[str, object], fields: Sequence[str], label: str) -> None:
    if sum(field in value for field in fields) > 1:
        raise ValidationError(f"{label} fields are mutually exclusive")


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


class _DiscordMultipartUpload(Iterable[bytes]):
    """Exact-length Discord multipart body whose file bytes remain streamed."""

    def __init__(self, payload: object, filename: object, upload: PreparedUpload) -> None:
        if not isinstance(upload, PreparedUpload):
            raise ValidationError("Discord prepared attachment is invalid")
        safe_filename = _discord_attachment_filename(filename)
        payload_json = _multipart_payload(payload)
        boundary = _multipart_boundary(payload_json, safe_filename.encode("utf-8"))
        prefix, suffix = _multipart_framing(payload_json, safe_filename, boundary)
        if len(prefix) > MAX_STREAM_CHUNK_BYTES or len(suffix) > MAX_STREAM_CHUNK_BYTES:
            raise ValidationError("Discord multipart metadata exceeds its stream chunk bound")
        self._prefix = prefix
        self._suffix = suffix
        self._upload = upload
        self.size = len(prefix) + upload.size + len(suffix)
        self.content_type = f"multipart/form-data; boundary={boundary}"

    def __iter__(self) -> Iterator[bytes]:
        yield self._prefix
        yield from self._upload.iter_chunks()
        yield self._suffix


def _multipart_body(payload: object, filename: object, content: bytes) -> tuple[bytes, str]:
    if not isinstance(content, bytes):
        raise ValidationError("Discord attachment content is invalid")
    safe_filename = _discord_attachment_filename(filename)
    payload_json = _multipart_payload(payload)
    boundary = _multipart_boundary(payload_json, safe_filename.encode("utf-8"), content)
    prefix, suffix = _multipart_framing(payload_json, safe_filename, boundary)
    return prefix + content + suffix, f"multipart/form-data; boundary={boundary}"


def _discord_attachment_filename(filename: object) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or any(
            character in {'"', "\\"} or ord(character) < 0x20 or ord(character) == 0x7F
            for character in filename
        )
    ):
        raise ValidationError("Discord attachment filename is invalid")
    return filename


def _multipart_payload(payload: object) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise ValidationError("Discord attachment metadata is invalid") from exc


def _multipart_boundary(*metadata: bytes) -> str:
    for _ in range(_MULTIPART_BOUNDARY_ATTEMPTS):
        candidate = f"seld-discord-attachment-{secrets.token_hex(_MULTIPART_BOUNDARY_BYTES)}"
        candidate_bytes = candidate.encode("ascii")
        if all(candidate_bytes not in item for item in metadata):
            return candidate
    raise ValidationError("Discord attachment multipart boundary could not be made safe")


def _multipart_framing(
    payload_json: bytes,
    filename: str,
    boundary: str,
) -> tuple[bytes, bytes]:
    prefix = (
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
    )
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    return prefix, suffix


def _upload_source(
    value: Mapping[str, object],
    transfer: ConnectorTransferContext | None,
) -> bytes | PreparedUpload:
    if "content_base64" in value:
        return _decode_base64(value["content_base64"])
    if transfer is None:
        raise ValidationError("collaboration prepared upload requires transfer context")
    return transfer.upload(("local_file",))


def _download_delivery(value: Mapping[str, object]) -> str:
    delivery = value.get("delivery", "artifact")
    if delivery not in {"artifact", "inline_chunk"}:
        raise ValidationError("collaboration file delivery is invalid")
    return delivery


def _artifact_media_type(value: object, label: str) -> str:
    if value is None or value == "":
        return "application/octet-stream"
    if (
        not isinstance(value, str)
        or len(value) > 255
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or "/" not in value
    ):
        raise ValidationError(f"{label} is invalid")
    return value


def _required_download_size(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ARTIFACT_BYTES:
        raise ValidationError(f"{label} is invalid")
    return value


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


def _required_slack_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isascii()
        or not value.isalnum()
    ):
        raise ValidationError(f"{label} is invalid")
    return value


def _required_discord_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 20
        or not value.isascii()
        or not value.isdigit()
    ):
        raise ValidationError(f"{label} is invalid")
    return value


def _reject_continuation(continuation: object | None) -> None:
    if continuation is not None:
        raise ValidationError("connector operation does not accept a continuation")
