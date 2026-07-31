"""Fixed Google provider adapter for the bounded connector operation catalog."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from email.policy import SMTP
from typing import Final

from continuity_kernel.connector_adapter import (
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
)
from continuity_kernel.connector_contract import ConnectorEffect, OperationSpec
from continuity_kernel.connector_operations_google import GOOGLE_OPERATIONS
from continuity_kernel.connector_transport import (
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError

_GOOGLE_PROVIDERS: Final = frozenset({"gmail", "google_calendar", "google_drive"})
_GOOGLE_BY_KEY: Final = {operation.key: operation for operation in GOOGLE_OPERATIONS}
_GMAIL_PURGE_SCOPE: Final = "https://mail.google.com/"
_JSON_STATUSES: Final = frozenset({200, 201, 202})
_DELETE_STATUSES: Final = frozenset({200, 202, 204})
_MAX_CONTENT_BYTES: Final = 180_000
_CONTENT_RANGE: Final = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_PAGINATION_FIELDS: Final = frozenset({"nextPageToken", "nextSyncToken", "nextLink", "nextPage"})
_UPLOAD_FIELDS: Final = frozenset({"uploadUrl", "resumableUploadUrl", "location"})
_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


class GoogleConnectorAdapter:
    """Execute only cataloged Google operations through fixed provider routes."""

    @property
    def providers(self) -> frozenset[str]:
        return _GOOGLE_PROVIDERS

    def classify_effect(self, operation: OperationSpec, input_value: object) -> ConnectorEffect:
        operation, values = _known_operation(operation, input_value)
        if operation.provider != "google_calendar" or operation.name not in {
            "events.create",
            "events.update",
            "events.move",
        }:
            return operation.effect
        if _calendar_has_external_effect(values):
            return ConnectorEffect.OUTWARD
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
        del write_idempotency_key
        operation, values = _known_operation(operation, input_value)
        if not isinstance(credential, ConnectorRuntimeCredential):
            raise ValidationError("connector runtime credential is invalid")
        if not operation.scope_grant_satisfies(credential.granted_scopes):
            raise ValidationError("connector credential does not satisfy the operation scope")
        if operation.provider == "gmail":
            return _execute_gmail(operation, values, continuation, credential, transport)
        if operation.provider == "google_calendar":
            return _execute_calendar(operation, values, continuation, credential, transport)
        if operation.provider == "google_drive":
            return _execute_drive(operation, values, continuation, credential, transport)
        raise ValidationError("connector operation is not handled by Google")


def _known_operation(
    operation: object, input_value: object
) -> tuple[OperationSpec, dict[str, object]]:
    if not isinstance(operation, OperationSpec):
        raise ValidationError("connector operation is invalid")
    expected = _GOOGLE_BY_KEY.get(operation.key)
    if expected is None or operation != expected:
        raise ValidationError("connector operation is not in the Google catalog")
    validated = operation.validate_input(input_value)
    if not isinstance(validated, dict):
        raise ValidationError("connector operation input is invalid")
    return operation, validated


def _execute_gmail(
    operation: OperationSpec,
    values: dict[str, object],
    continuation: object | None,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> ConnectorAdapterResult:
    base = "/gmail/v1/users/me"
    name = operation.name
    if name in {"messages.list", "threads.list", "drafts.list"}:
        resource = name.split(".", 1)[0]
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/{resource}",
            credential=credential,
            query=_gmail_list_query(values, continuation),
        )
    _reject_continuation(continuation)
    if name == "messages.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/messages/{_segment(_required(values, 'message_id'))}",
            credential=credential,
            query=_optional_query(values, {"format": "format"}),
        )
    if name == "attachments.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=(
                f"{base}/messages/{_segment(_required(values, 'message_id'))}/attachments/"
                f"{_segment(_required(values, 'attachment_id'))}"
            ),
            credential=credential,
        )
    if name == "threads.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/threads/{_segment(_required(values, 'thread_id'))}",
            credential=credential,
            query=_optional_query(values, {"format": "format"}),
        )
    if name == "drafts.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/drafts/{_segment(_required(values, 'draft_id'))}",
            credential=credential,
            query=_optional_query(values, {"format": "format"}),
        )
    if name == "labels.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/labels",
            credential=credential,
        )
    if name in {"drafts.create", "drafts.update"}:
        draft_path = f"{base}/drafts"
        method = ConnectorMethod.POST
        if name == "drafts.update":
            method = ConnectorMethod.PUT
            draft_path += f"/{_segment(_required(values, 'draft_id'))}"
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=method,
            path=draft_path,
            credential=credential,
            json_body={"message": _gmail_message(values)},
            expected_statuses=_JSON_STATUSES,
        )
    if name == "drafts.delete":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.DELETE,
            path=f"{base}/drafts/{_segment(_required(values, 'draft_id'))}",
            credential=credential,
            expected_statuses=_DELETE_STATUSES,
        )
    if name == "drafts.send":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.POST,
            path=f"{base}/drafts/send",
            credential=credential,
            json_body={"id": _required(values, "draft_id")},
            expected_statuses=_JSON_STATUSES,
        )
    if name in {"messages.modify", "threads.modify"}:
        resource, identifier = _gmail_resource_identifier(values, name)
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.POST,
            path=f"{base}/{resource}/{_segment(identifier)}/modify",
            credential=credential,
            json_body=_selected(
                values,
                {"add_label_ids": "addLabelIds", "remove_label_ids": "removeLabelIds"},
            ),
            expected_statuses=_JSON_STATUSES,
        )
    if name in {
        "messages.trash",
        "messages.restore",
        "messages.purge",
        "threads.trash",
        "threads.restore",
        "threads.purge",
    }:
        resource, identifier = _gmail_resource_identifier(values, name)
        action = name.rsplit(".", 1)[1]
        if action == "restore":
            action = "untrash"
        if action == "purge":
            if operation.required_scopes != (frozenset({_GMAIL_PURGE_SCOPE}),):
                raise ValidationError("Gmail purge must require the full Gmail scope")
            return _json_request(
                transport,
                origin=ConnectorOrigin.GMAIL,
                method=ConnectorMethod.DELETE,
                path=f"{base}/{resource}/{_segment(identifier)}",
                credential=credential,
                expected_statuses=_DELETE_STATUSES,
            )
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.POST,
            path=f"{base}/{resource}/{_segment(identifier)}/{action}",
            credential=credential,
            expected_statuses=_JSON_STATUSES,
        )
    if name == "labels.create":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.POST,
            path=f"{base}/labels",
            credential=credential,
            json_body=_selected(
                values,
                {
                    "label_list_visibility": "labelListVisibility",
                    "message_list_visibility": "messageListVisibility",
                    "name": "name",
                },
            ),
            expected_statuses=_JSON_STATUSES,
        )
    if name == "labels.update":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.PATCH,
            path=f"{base}/labels/{_segment(_required(values, 'label_id'))}",
            credential=credential,
            json_body=_selected(
                values,
                {
                    "label_list_visibility": "labelListVisibility",
                    "message_list_visibility": "messageListVisibility",
                    "name": "name",
                },
            ),
            expected_statuses=_JSON_STATUSES,
        )
    if name == "labels.delete":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.DELETE,
            path=f"{base}/labels/{_segment(_required(values, 'label_id'))}",
            credential=credential,
            expected_statuses=_DELETE_STATUSES,
        )
    raise ValidationError("Gmail operation has no fixed route")


def _execute_calendar(
    operation: OperationSpec,
    values: dict[str, object],
    continuation: object | None,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> ConnectorAdapterResult:
    base = "/calendar/v3"
    name = operation.name
    if name == "calendars.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/users/me/calendarList",
            credential=credential,
            query=_calendar_list_query(values, continuation),
        )
    if name == "events.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events",
            credential=credential,
            query=_calendar_event_list_query(values, continuation),
        )
    if name == "events.instances":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=(
                f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
                f"{_segment(_required(values, 'event_id'))}/instances"
            ),
            credential=credential,
            query=_calendar_instance_query(values, continuation),
        )
    _reject_continuation(continuation)
    if name == "calendars.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}",
            credential=credential,
        )
    if name == "events.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=(
                f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
                f"{_segment(_required(values, 'event_id'))}"
            ),
            credential=credential,
        )
    if name == "freebusy.query":
        body = {
            "items": [{"id": value} for value in _strings(_required_value(values, "calendar_ids"))],
            "timeMax": _required(values, "time_max"),
            "timeMin": _required(values, "time_min"),
            "timeZone": _required(values, "time_zone"),
        }
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.POST,
            path=f"{base}/freeBusy",
            credential=credential,
            json_body=body,
            expected_statuses=_JSON_STATUSES,
        )
    if name in {"calendars.create", "calendars.update"}:
        path = f"{base}/calendars"
        method = ConnectorMethod.POST
        headers: dict[str, str] | None = None
        if name == "calendars.update":
            method = ConnectorMethod.PATCH
            path += f"/{_segment(_required(values, 'calendar_id'))}"
            headers = _etag_headers(values)
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=method,
            path=path,
            credential=credential,
            json_body=_calendar_fields(values),
            headers=headers,
            expected_statuses=_JSON_STATUSES,
        )
    if name == "calendars.delete":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.DELETE,
            path=f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}",
            credential=credential,
            headers=_etag_headers(values),
            expected_statuses=_DELETE_STATUSES,
        )
    if name in {"events.create", "events.update"}:
        path = f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events"
        method = ConnectorMethod.POST
        headers = None
        if name == "events.update":
            method = ConnectorMethod.PATCH
            path += f"/{_segment(_required(values, 'event_id'))}"
            headers = _etag_headers(values)
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=method,
            path=path,
            credential=credential,
            query=_optional_query(values, {"send_updates": "sendUpdates"}),
            json_body=_calendar_event_body(values, include_client_id=name == "events.create"),
            headers=headers,
            expected_statuses=_JSON_STATUSES,
        )
    if name == "events.move":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.POST,
            path=(
                f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
                f"{_segment(_required(values, 'event_id'))}/move"
            ),
            credential=credential,
            query=_calendar_move_query(values),
            headers=_etag_headers(values) if "etag" in values else None,
            expected_statuses=_JSON_STATUSES,
        )
    if name == "events.respond":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PATCH,
            path=(
                f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
                f"{_segment(_required(values, 'event_id'))}"
            ),
            credential=credential,
            query=_optional_query(values, {"send_updates": "sendUpdates"}),
            json_body={
                "attendees": [
                    {"self": True, "responseStatus": _required(values, "response_status")}
                ]
            },
            expected_statuses=_JSON_STATUSES,
        )
    if name == "events.delete":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.DELETE,
            path=(
                f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
                f"{_segment(_required(values, 'event_id'))}"
            ),
            credential=credential,
            query=_optional_query(values, {"send_updates": "sendUpdates"}),
            headers=_etag_headers(values),
            expected_statuses=_DELETE_STATUSES,
        )
    raise ValidationError("Google Calendar operation has no fixed route")


def _execute_drive(
    operation: OperationSpec,
    values: dict[str, object],
    continuation: object | None,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> ConnectorAdapterResult:
    base = "/drive/v3"
    name = operation.name
    if name == "drives.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/drives",
            credential=credential,
            query=_shared_drive_list_query(values, continuation),
        )
    if name == "files.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/files",
            credential=credential,
            query=_drive_file_list_query(values, continuation),
        )
    if name in {"permissions.list", "comments.list", "revisions.list"}:
        resource = name.split(".", 1)[0]
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}/{resource}",
            credential=credential,
            query=(
                _with_supports_all_drives(values, _drive_page_query(values, continuation))
                if name == "permissions.list"
                else _drive_page_query(values, continuation)
            ),
        )
    if name == "replies.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=(
                f"{base}/files/{_segment(_required(values, 'file_id'))}/comments/"
                f"{_segment(_required(values, 'comment_id'))}/replies"
            ),
            credential=credential,
            query=_drive_page_query(values, continuation),
        )
    _reject_continuation(continuation)
    if name == "files.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}",
            credential=credential,
            query=_with_supports_all_drives(values),
        )
    if name in {"files.download", "revisions.download"}:
        path = f"{base}/files/{_segment(_required(values, 'file_id'))}"
        if name == "revisions.download":
            path += f"/revisions/{_segment(_required(values, 'revision_id'))}"
        return _drive_content_request(
            transport,
            path=path,
            credential=credential,
            byte_offset=_integer(values.get("byte_offset", 0)),
            maximum=_integer(values.get("max_chunk_size", _MAX_CONTENT_BYTES)),
            query=_with_supports_all_drives(values, (("alt", "media"),)),
            range_download=True,
        )
    if name == "files.export":
        return _drive_content_request(
            transport,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}/export",
            credential=credential,
            byte_offset=0,
            maximum=_MAX_CONTENT_BYTES,
            query=(("mimeType", _required(values, "export_mime_type")),),
            range_download=False,
        )
    if name in {"files.create", "files.update"}:
        file_id = values.get("file_id")
        method = ConnectorMethod.POST
        metadata_path = f"{base}/files"
        upload_path = "/upload/drive/v3/files"
        headers: dict[str, str] | None = None
        if name == "files.update":
            method = ConnectorMethod.PATCH
            identifier = _segment(file_id)
            metadata_path += f"/{identifier}"
            upload_path += f"/{identifier}"
            headers = _etag_headers(values)
        metadata = _drive_file_metadata(values)
        content = values.get("content_base64")
        if content is not None:
            return _drive_resumable_upload(
                transport,
                method=method,
                path=upload_path,
                credential=credential,
                metadata=metadata,
                content_base64=_text(content),
                mime_type=_text(values.get("mime_type", "application/octet-stream")),
                headers=headers,
                query=_with_supports_all_drives(values, (("uploadType", "resumable"),)),
            )
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=method,
            path=metadata_path,
            credential=credential,
            query=_with_supports_all_drives(values),
            json_body=metadata,
            headers=headers,
            expected_statuses=_JSON_STATUSES,
        )
    if name == "files.copy":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.POST,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}/copy",
            credential=credential,
            query=_with_supports_all_drives(values),
            json_body=_drive_file_metadata(values),
            expected_statuses=_JSON_STATUSES,
        )
    if name == "files.move":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PATCH,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}",
            credential=credential,
            query=_with_supports_all_drives(values, _drive_move_query(values)),
            headers=_etag_headers(values) if "etag" in values else None,
            expected_statuses=_JSON_STATUSES,
        )
    if name in {"files.trash", "files.restore"}:
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PATCH,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}",
            credential=credential,
            query=_with_supports_all_drives(values),
            json_body={"trashed": name == "files.trash"},
            headers=_etag_headers(values),
            expected_statuses=_JSON_STATUSES,
        )
    if name == "files.purge":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.DELETE,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}",
            credential=credential,
            query=_with_supports_all_drives(values),
            headers=_etag_headers(values),
            expected_statuses=_DELETE_STATUSES,
        )
    if name in {"permissions.create", "permissions.update", "permissions.delete"}:
        return _drive_permission_request(name, values, credential, transport)
    if name in {"comments.create", "comments.update", "comments.delete"}:
        return _drive_comment_request(name, values, credential, transport)
    if name in {"replies.create", "replies.update", "replies.delete"}:
        return _drive_reply_request(name, values, credential, transport)
    if name in {"revisions.keep", "revisions.delete"}:
        path = (
            f"{base}/files/{_segment(_required(values, 'file_id'))}/revisions/"
            f"{_segment(_required(values, 'revision_id'))}"
        )
        if name == "revisions.keep":
            return _json_request(
                transport,
                origin=ConnectorOrigin.GOOGLE,
                method=ConnectorMethod.PATCH,
                path=path,
                credential=credential,
                json_body={"keepForever": True},
                expected_statuses=_JSON_STATUSES,
            )
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.DELETE,
            path=path,
            credential=credential,
            headers=_etag_headers(values),
            expected_statuses=_DELETE_STATUSES,
        )
    raise ValidationError("Google Drive operation has no fixed route")


def _drive_permission_request(
    name: str,
    values: dict[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> ConnectorAdapterResult:
    base = f"/drive/v3/files/{_segment(_required(values, 'file_id'))}/permissions"
    if name == "permissions.create":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.POST,
            path=base,
            credential=credential,
            query=_with_supports_all_drives(
                values,
                _optional_query(
                    values,
                    {
                        "notification_message": "emailMessage",
                        "send_notification_email": "sendNotificationEmail",
                    },
                ),
            ),
            json_body=_selected(
                values,
                {
                    "domain": "domain",
                    "email_address": "emailAddress",
                    "permission_type": "type",
                    "role": "role",
                },
            ),
            expected_statuses=_JSON_STATUSES,
        )
    path = f"{base}/{_segment(_required(values, 'permission_id'))}"
    if name == "permissions.update":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PATCH,
            path=path,
            credential=credential,
            query=_with_supports_all_drives(values),
            json_body={"role": _required(values, "role")},
            headers=_etag_headers(values),
            expected_statuses=_JSON_STATUSES,
        )
    return _json_request(
        transport,
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.DELETE,
        path=path,
        credential=credential,
        query=_with_supports_all_drives(values),
        headers=_etag_headers(values),
        expected_statuses=_DELETE_STATUSES,
    )


def _drive_comment_request(
    name: str,
    values: dict[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> ConnectorAdapterResult:
    base = f"/drive/v3/files/{_segment(_required(values, 'file_id'))}/comments"
    if name == "comments.create":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.POST,
            path=base,
            credential=credential,
            json_body=_drive_comment_body(values),
            expected_statuses=_JSON_STATUSES,
        )
    path = f"{base}/{_segment(_required(values, 'comment_id'))}"
    if name == "comments.update":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PATCH,
            path=path,
            credential=credential,
            json_body=_drive_comment_body(values),
            headers=_etag_headers(values),
            expected_statuses=_JSON_STATUSES,
        )
    return _json_request(
        transport,
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.DELETE,
        path=path,
        credential=credential,
        headers=_etag_headers(values),
        expected_statuses=_DELETE_STATUSES,
    )


def _drive_reply_request(
    name: str,
    values: dict[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> ConnectorAdapterResult:
    base = (
        f"/drive/v3/files/{_segment(_required(values, 'file_id'))}/comments/"
        f"{_segment(_required(values, 'comment_id'))}/replies"
    )
    if name == "replies.create":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.POST,
            path=base,
            credential=credential,
            json_body=_drive_comment_body(values),
            expected_statuses=_JSON_STATUSES,
        )
    path = f"{base}/{_segment(_required(values, 'reply_id'))}"
    if name == "replies.update":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PATCH,
            path=path,
            credential=credential,
            json_body=_drive_comment_body(values),
            headers=_etag_headers(values),
            expected_statuses=_JSON_STATUSES,
        )
    return _json_request(
        transport,
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.DELETE,
        path=path,
        credential=credential,
        headers=_etag_headers(values),
        expected_statuses=_DELETE_STATUSES,
    )


def _json_request(
    transport: ConnectorTransport,
    *,
    origin: ConnectorOrigin,
    method: ConnectorMethod,
    path: str,
    credential: ConnectorRuntimeCredential,
    query: Sequence[tuple[str, str]] = (),
    json_body: object | None = None,
    headers: Mapping[str, str] | None = None,
    expected_statuses: frozenset[int] = frozenset({200}),
) -> ConnectorAdapterResult:
    response = transport.request(
        origin=origin,
        method=method,
        path=path,
        credential=credential.credential,
        query=query,
        json_body=json_body,
        headers=headers,
        expected_statuses=expected_statuses,
    )
    return _json_result(response.json())


def _drive_content_request(
    transport: ConnectorTransport,
    *,
    path: str,
    credential: ConnectorRuntimeCredential,
    byte_offset: int,
    maximum: int,
    query: Sequence[tuple[str, str]],
    range_download: bool,
) -> ConnectorAdapterResult:
    if maximum < 1:
        raise ValidationError("Google Drive content chunk bound is invalid")
    headers: Mapping[str, str] | None = None
    expected_statuses = frozenset({200})
    if range_download:
        last_byte = byte_offset + maximum - 1
        headers = {"Range": f"bytes={byte_offset}-{last_byte}"}
        expected_statuses = frozenset({206})
    elif byte_offset != 0:
        raise ValidationError("Google Drive exports do not support byte offsets")
    response = transport.request(
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.GET,
        path=path,
        credential=credential.credential,
        query=query,
        headers=headers,
        expected_statuses=expected_statuses,
        response_bound=min(maximum, _MAX_CONTENT_BYTES),
    )
    payload: dict[str, object] = {
        "byte_offset": byte_offset,
        "content_base64": base64.b64encode(response.body).decode("ascii"),
    }
    if range_download:
        content_range, next_byte_offset = _drive_content_range(
            response,
            byte_offset=byte_offset,
        )
        payload["content_range"] = content_range
        payload["next_byte_offset"] = next_byte_offset
    return ConnectorAdapterResult(payload)


def _drive_content_range(
    response: ConnectorResponse,
    *,
    byte_offset: int,
) -> tuple[str, int]:
    if response.status != 206:
        raise ValidationError("Google Drive ranged download did not return partial content")
    content_range = _response_header(response.headers, "content-range")
    if content_range is None:
        raise ValidationError("Google Drive ranged download has no content range")
    match = _CONTENT_RANGE.fullmatch(content_range)
    if match is None:
        raise ValidationError("Google Drive ranged download content range is invalid")
    start, end, total = (int(value) for value in match.groups())
    if start != byte_offset or end < start or end - start + 1 != len(response.body) or total <= end:
        raise ValidationError("Google Drive ranged download content range does not match content")
    return content_range, end + 1


def _drive_resumable_upload(
    transport: ConnectorTransport,
    *,
    method: ConnectorMethod,
    path: str,
    credential: ConnectorRuntimeCredential,
    metadata: dict[str, object],
    content_base64: str,
    mime_type: str,
    headers: Mapping[str, str] | None,
    query: Sequence[tuple[str, str]],
) -> ConnectorAdapterResult:
    initiated = transport.request(
        origin=ConnectorOrigin.GOOGLE,
        method=method,
        path=path,
        credential=credential.credential,
        query=query,
        json_body=metadata,
        headers=headers,
        expected_statuses=_JSON_STATUSES,
    )
    location = _response_location(initiated.headers)
    uploaded = transport.request_provider_location(
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.PUT,
        location=location,
        credential=credential.credential,
        body=_decode_base64(content_base64),
        content_type=_mime_type(mime_type),
        headers=headers,
        expected_statuses=_JSON_STATUSES,
    )
    return _json_result(uploaded.json())


def _json_result(payload: object) -> ConnectorAdapterResult:
    if not isinstance(payload, Mapping):
        return ConnectorAdapterResult(payload)
    continuation: object | None = None
    if isinstance(payload.get("nextPageToken"), str):
        continuation = payload["nextPageToken"]
    elif isinstance(payload.get("nextSyncToken"), str):
        continuation = {"syncToken": payload["nextSyncToken"]}
    return ConnectorAdapterResult(_strip_provider_state(payload), continuation=continuation)


def _strip_provider_state(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_provider_state(item)
            for key, item in value.items()
            if isinstance(key, str) and key not in _PAGINATION_FIELDS | _UPLOAD_FIELDS
        }
    if isinstance(value, list):
        return [_strip_provider_state(item) for item in value]
    return value


def _gmail_list_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query = list(_optional_query(values, {"include_spam_trash": "includeSpamTrash", "query": "q"}))
    if "page_size" in values:
        query.append(("maxResults", str(_integer(values["page_size"]))))
    if "label_ids" in values:
        query.extend(("labelIds", label) for label in _strings(values["label_ids"]))
    query.extend(_continuation_query(continuation))
    return tuple(query)


def _calendar_list_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    continuation_query = _continuation_query(continuation)
    if continuation_query and continuation_query[0][0] == "syncToken":
        if any(field in values for field in ("min_access_role", "show_own_organization_only")):
            raise ValidationError("Google Calendar sync cannot combine calendar list filters")
        visibility_flags = ("show_deleted", "show_hidden")
        if any(values.get(field) is False for field in visibility_flags if field in values):
            raise ValidationError("Google Calendar sync cannot hide changed calendar entries")
    query = list(
        _optional_query(
            values,
            {
                "min_access_role": "minAccessRole",
                "page_size": "maxResults",
                "show_deleted": "showDeleted",
                "show_hidden": "showHidden",
                "show_own_organization_only": "showOwnOrganizationOnly",
            },
        )
    )
    query.extend(continuation_query)
    return tuple(query)


def _calendar_event_list_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    if values.get("order_by") == "startTime" and values.get("single_events") is not True:
        raise ValidationError("Google Calendar start-time ordering requires single events")
    continuation_query = _continuation_query(continuation)
    if continuation_query and continuation_query[0][0] == "syncToken":
        filters = {
            "event_types",
            "i_cal_uid",
            "max_attendees",
            "order_by",
            "query",
            "single_events",
            "time_max",
            "time_min",
            "time_zone",
            "updated_min",
        }
        if any(field in values for field in filters):
            raise ValidationError("Google Calendar sync cannot combine event list filters")
        if values.get("show_deleted") is False:
            raise ValidationError("Google Calendar sync must include deleted events")
    query = list(
        _optional_query(
            values,
            {
                "i_cal_uid": "iCalUID",
                "max_attendees": "maxAttendees",
                "order_by": "orderBy",
                "page_size": "maxResults",
                "query": "q",
                "show_deleted": "showDeleted",
                "single_events": "singleEvents",
                "time_max": "timeMax",
                "time_min": "timeMin",
                "time_zone": "timeZone",
                "updated_min": "updatedMin",
            },
        )
    )
    if "event_types" in values:
        query.extend(("eventTypes", item) for item in _strings(values["event_types"]))
    query.extend(continuation_query)
    return tuple(query)


def _calendar_instance_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    continuation_query = _continuation_query(continuation)
    if continuation_query and continuation_query[0][0] == "syncToken":
        raise ValidationError("Google Calendar event instances do not support sync tokens")
    query = list(
        _optional_query(
            values,
            {
                "max_attendees": "maxAttendees",
                "page_size": "maxResults",
                "time_max": "timeMax",
                "time_min": "timeMin",
                "time_zone": "timeZone",
            },
        )
    )
    query.extend(continuation_query)
    return tuple(query)


def _calendar_move_query(values: dict[str, object]) -> tuple[tuple[str, str], ...]:
    query = [("destination", _required(values, "destination_calendar_id"))]
    query.extend(_optional_query(values, {"send_updates": "sendUpdates"}))
    return tuple(query)


def _drive_file_list_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    _validate_shared_drive_list(values)
    clauses: list[str] = []
    if "query" in values:
        clauses.append(_text(values["query"]))
    if "parent_id" in values:
        clauses.append(f"'{_drive_query_literal(_text(values['parent_id']))}' in parents")
    if "mime_type" in values:
        clauses.append(f"mimeType = '{_drive_query_literal(_text(values['mime_type']))}'")
    if values.get("include_trashed") is False:
        clauses.append("trashed = false")
    query = list(
        _optional_query(
            values,
            {
                "corpora": "corpora",
                "drive_id": "driveId",
                "include_items_from_all_drives": "includeItemsFromAllDrives",
                "page_size": "pageSize",
                "supports_all_drives": "supportsAllDrives",
            },
        )
    )
    if clauses:
        query.append(("q", " and ".join(clauses)))
    if "order_by" in values:
        query.append(("orderBy", ",".join(_strings(values["order_by"]))))
    if "spaces" in values:
        query.append(("spaces", ",".join(_strings(values["spaces"]))))
    query.extend(_continuation_query(continuation))
    return tuple(query)


def _shared_drive_list_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query = list(_optional_query(values, {"page_size": "pageSize", "query": "q"}))
    query.extend(_continuation_query(continuation))
    return tuple(query)


def _validate_shared_drive_list(values: Mapping[str, object]) -> None:
    corpora = values.get("corpora")
    has_drive_id = "drive_id" in values
    shared_corpora = corpora in {"allDrives", "drive"}
    shared_items = values.get("include_items_from_all_drives") is True
    if corpora == "drive" and not has_drive_id:
        raise ValidationError("Google Drive shared-drive corpus requires a drive ID")
    if has_drive_id and corpora != "drive":
        raise ValidationError("Google Drive drive ID requires the shared-drive corpus")
    if (shared_corpora or shared_items) and values.get("supports_all_drives") is not True:
        raise ValidationError("Google Drive shared-drive search requires supports-all-drives")


def _with_supports_all_drives(
    values: Mapping[str, object], query: Sequence[tuple[str, str]] = ()
) -> tuple[tuple[str, str], ...]:
    result = tuple(query)
    if values.get("supports_all_drives") is True:
        return (*result, ("supportsAllDrives", "true"))
    return result


def _drive_page_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query = list(_optional_query(values, {"page_size": "pageSize"}))
    query.extend(_continuation_query(continuation))
    return tuple(query)


def _drive_move_query(values: dict[str, object]) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "add_parent_ids" in values:
        query.append(("addParents", ",".join(_strings(values["add_parent_ids"]))))
    if "remove_parent_ids" in values:
        query.append(("removeParents", ",".join(_strings(values["remove_parent_ids"]))))
    return tuple(query)


def _optional_query(
    values: dict[str, object], fields: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    for source, target in fields.items():
        if source not in values:
            continue
        value = values[source]
        if type(value) is bool:
            rendered = "true" if value else "false"
        elif type(value) is int:
            rendered = str(value)
        else:
            rendered = _text(value)
        query.append((target, rendered))
    return tuple(query)


def _continuation_query(continuation: object | None) -> tuple[tuple[str, str], ...]:
    if continuation is None:
        return ()
    if isinstance(continuation, str) and continuation:
        return (("pageToken", continuation),)
    if (
        isinstance(continuation, Mapping)
        and set(continuation) == {"syncToken"}
        and isinstance(continuation["syncToken"], str)
        and continuation["syncToken"]
    ):
        return (("syncToken", continuation["syncToken"]),)
    raise ValidationError("connector continuation is invalid")


def _reject_continuation(continuation: object | None) -> None:
    if continuation is not None:
        raise ValidationError("connector operation does not accept a continuation")


def _calendar_fields(values: dict[str, object]) -> dict[str, object]:
    return _selected(
        values,
        {
            "description": "description",
            "location": "location",
            "summary": "summary",
            "time_zone": "timeZone",
        },
    )


def _calendar_event_body(
    values: dict[str, object], *, include_client_id: bool
) -> dict[str, object]:
    body = _selected(
        values,
        {
            "description": "description",
            "guests_can_invite_others": "guestsCanInviteOthers",
            "guests_can_modify": "guestsCanModify",
            "guests_can_see_other_guests": "guestsCanSeeOtherGuests",
            "location": "location",
            "recurrence": "recurrence",
            "summary": "summary",
            "visibility": "visibility",
        },
    )
    if include_client_id and "event_id" in values:
        body["id"] = _text(values["event_id"])
    if "attendee_emails" in values and "attendees" in values:
        raise ValidationError("Google Calendar attendees are specified twice")
    if "attendee_emails" in values:
        body["attendees"] = [{"email": value} for value in _strings(values["attendee_emails"])]
    if "attendees" in values:
        body["attendees"] = [
            _selected(
                attendee,
                {
                    "display_name": "displayName",
                    "email": "email",
                    "optional": "optional",
                    "response_status": "responseStatus",
                },
            )
            for attendee in _mappings(values["attendees"])
        ]
    if "reminders" in values:
        body["reminders"] = _calendar_reminders(values["reminders"])
    for source, target in (("start", "start"), ("end", "end")):
        if source in values:
            body[target] = _calendar_time(values[source])
    return body


def _calendar_time(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValidationError("Google Calendar event time is invalid")
    if "date_time" in value:
        return {"dateTime": _text(value["date_time"]), "timeZone": _text(value["time_zone"])}
    return {"date": _text(value["date"]), "timeZone": _text(value["time_zone"])}


def _calendar_reminders(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError("Google Calendar reminders are invalid")
    use_default = value.get("use_default")
    if type(use_default) is not bool:
        raise ValidationError("Google Calendar reminder default is invalid")
    if use_default:
        return {"useDefault": True}
    result: dict[str, object] = {"useDefault": False}
    if "overrides" in value:
        result["overrides"] = [
            _selected(reminder, {"delivery": "method", "minutes": "minutes"})
            for reminder in _mappings(value["overrides"])
        ]
    return result


def _calendar_has_external_effect(values: Mapping[str, object]) -> bool:
    if "attendee_emails" in values or "attendees" in values or "send_updates" in values:
        return True
    calendar_ids = [values.get("calendar_id"), values.get("destination_calendar_id")]
    return any(isinstance(value, str) and value != "primary" for value in calendar_ids)


def _drive_file_metadata(values: dict[str, object]) -> dict[str, object]:
    metadata = _selected(
        values,
        {
            "description": "description",
            "mime_type": "mimeType",
            "name": "name",
            "parent_ids": "parents",
        },
    )
    if "app_properties" in values:
        properties: dict[str, str] = {}
        for item in _mappings(values["app_properties"]):
            key = _text(item.get("key"))
            if key in properties:
                raise ValidationError("Google Drive app property keys must be unique")
            properties[key] = _text(item.get("value"))
        metadata["appProperties"] = properties
    return metadata


def _drive_comment_body(values: dict[str, object]) -> dict[str, object]:
    return _selected(
        values,
        {"content": "content", "quoted_file_content": "quotedFileContent"},
    )


def _gmail_message(values: dict[str, object]) -> dict[str, object]:
    message = EmailMessage()
    for source, header in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
        if source in values:
            message[header] = ", ".join(_safe_header(value) for value in _strings(values[source]))
    if "subject" in values:
        message["Subject"] = _safe_header(_text(values["subject"]))
    if "reply_to_message_id" in values:
        message["In-Reply-To"] = f"<{_safe_header(_text(values['reply_to_message_id']))}>"
    message.set_content(_text(values.get("text_body", "")))
    if "html_body" in values:
        message.add_alternative(_text(values["html_body"]), subtype="html")
    for attachment in _mappings(values.get("attachments", [])):
        mime_type = _mime_type(_text(attachment.get("mime_type")))
        main_type, sub_type = mime_type.split("/", 1)
        message.add_attachment(
            _decode_base64(_text(attachment.get("content_base64"))),
            maintype=main_type,
            subtype=sub_type,
            filename=_safe_header(_text(attachment.get("filename"))),
        )
    raw = base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode("ascii").rstrip("=")
    result: dict[str, object] = {"raw": raw}
    if "thread_id" in values:
        result["threadId"] = _text(values["thread_id"])
    return result


def _gmail_resource_identifier(values: dict[str, object], name: str) -> tuple[str, str]:
    if name.startswith("messages."):
        return "messages", _required(values, "message_id")
    return "threads", _required(values, "thread_id")


def _etag_headers(values: Mapping[str, object]) -> dict[str, str]:
    return {"If-Match": _required(values, "etag")}


def _selected(values: Mapping[str, object], fields: Mapping[str, str]) -> dict[str, object]:
    return {target: values[source] for source, target in fields.items() if source in values}


def _response_header(headers: Mapping[str, str], name: str) -> str | None:
    for candidate, value in headers.items():
        if candidate.casefold() == name.casefold() and isinstance(value, str):
            return value
    return None


def _response_location(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        if name.casefold() == "location" and isinstance(value, str) and value:
            return value
    raise ValidationError("Google Drive upload response has no provider location")


def _required(values: Mapping[str, object], name: str) -> str:
    return _text(_required_value(values, name))


def _required_value(values: Mapping[str, object], name: str) -> object:
    if name not in values:
        raise ValidationError("connector operation input is missing a required value")
    return values[name]


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("connector operation text is invalid")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValidationError("connector operation integer is invalid")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError("connector operation text array is invalid")
    return [_text(item) for item in value]


def _mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValidationError("connector operation object array is invalid")
    return [item for item in value if isinstance(item, Mapping)]


def _segment(value: object) -> str:
    text = _text(value)
    encoded: list[str] = []
    for byte in text.encode("utf-8"):
        character = chr(byte)
        encoded.append(character if character in _UNRESERVED else f"%{byte:02X}")
    return "".join(encoded)


def _drive_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _safe_header(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValidationError("connector mail header value is invalid")
    return value


def _mime_type(value: str) -> str:
    if value.count("/") != 1 or any(character.isspace() for character in value):
        raise ValidationError("connector MIME type is invalid")
    main_type, sub_type = value.split("/", 1)
    if not main_type or not sub_type or any(not character.isprintable() for character in value):
        raise ValidationError("connector MIME type is invalid")
    return value


def _decode_base64(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValidationError("connector base64 content is invalid") from exc
