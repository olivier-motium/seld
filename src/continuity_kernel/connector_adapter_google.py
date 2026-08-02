"""Fixed Google provider adapter for the bounded connector operation catalog."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Final, cast
from urllib.parse import urlsplit

from continuity_kernel.connector_adapter import (
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_contract import ConnectorEffect, OperationSpec
from continuity_kernel.connector_gmail_transfer import (
    GMAIL_UPLOAD_MAX_BYTES,
    GmailMessagePartBodyDecoder,
    GmailMimeAttachment,
    GmailMimeUpload,
)
from continuity_kernel.connector_operations_google import GOOGLE_OPERATIONS
from continuity_kernel.connector_transfer import (
    MAX_ARTIFACT_BYTES,
    ConnectorInputPath,
    PreparedUpload,
)
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
_LOCAL_FILE_LIMIT_MARKER: Final = "opaque-local-file"
_GMAIL_LOCAL_UPLOAD_OPERATIONS: Final = frozenset({"drafts.create", "drafts.update"})
_DRIVE_LOCAL_UPLOAD_OPERATIONS: Final = frozenset({"files.create", "files.update"})
_GMAIL_MAX_RAW_ATTACHMENT_BYTES: Final = (GMAIL_UPLOAD_MAX_BYTES * 3) // 4
_DRIVE_MAX_LOCAL_FILE_BYTES: Final = 5 * 1024**4
_CONTENT_RANGE: Final = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_MESSAGE_ID: Final = re.compile(r"^<[^<>\s]+@[^<>\s]+>$")
_MESSAGE_ID_REFERENCE: Final = re.compile(r"<[^<>\s]+@[^<>\s]+>")
_PAGINATION_FIELDS: Final = frozenset({"nextPageToken", "nextSyncToken", "nextLink", "nextPage"})
_UPLOAD_LOCATION_FIELDS: Final = frozenset({"location"})
_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_DRIVE_ATTACHMENT_SCOPES: Final = frozenset(
    {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
    }
)
_EXISTING_CALENDAR_EVENT_MUTATIONS: Final = frozenset({"events.move", "events.update"})
_DRIVE_COMMENT_RESOURCE_FIELDS: Final = (
    "id,kind,createdTime,modifiedTime,author(displayName,emailAddress,kind,me,"
    "permissionId,photoLink),content,htmlContent,deleted,resolved,anchor,"
    "quotedFileContent(mimeType,value),mentionedEmailAddresses,assigneeEmailAddress"
)
_DRIVE_REPLY_RESOURCE_FIELDS: Final = (
    "id,kind,createdTime,modifiedTime,author(displayName,emailAddress,kind,me,permissionId,"
    "photoLink),content,htmlContent,deleted,action,mentionedEmailAddresses,"
    "assigneeEmailAddress"
)
_DRIVE_COMMENT_LIST_FIELDS: Final = f"nextPageToken,comments({_DRIVE_COMMENT_RESOURCE_FIELDS})"
_DRIVE_REPLY_LIST_FIELDS: Final = f"nextPageToken,replies({_DRIVE_REPLY_RESOURCE_FIELDS})"


@dataclass(frozen=True)
class _GmailReplyContext:
    message_id: str
    references: tuple[str, ...]
    subject: str
    thread_id: str


class GoogleConnectorAdapter:
    """Execute only cataloged Google operations through fixed provider routes."""

    @property
    def providers(self) -> frozenset[str]:
        return _GOOGLE_PROVIDERS

    def max_local_file_bytes(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        path: ConnectorInputPath,
    ) -> int:
        """Return a provider-native cap for one sanitized local-file input."""

        if not isinstance(operation, OperationSpec):
            raise ValidationError("Google operation is invalid")
        expected = _GOOGLE_BY_KEY.get(operation.key)
        if expected is None or operation != expected:
            raise ValidationError("Google operation is not in the Google catalog")
        if (
            operation.provider == "gmail"
            and operation.name in _GMAIL_LOCAL_UPLOAD_OPERATIONS
            and _gmail_local_file_shape(input_value, path)
        ):
            return _GMAIL_MAX_RAW_ATTACHMENT_BYTES
        if (
            operation.provider == "google_drive"
            and operation.name in _DRIVE_LOCAL_UPLOAD_OPERATIONS
            and _drive_local_file_shape(input_value, path)
        ):
            return _DRIVE_MAX_LOCAL_FILE_BYTES
        raise ValidationError("Google local-file upload path is not permitted")

    def classify_effect(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        credential: ConnectorRuntimeCredential | None = None,
        transport: ConnectorTransport | None = None,
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorEffect:
        operation, values = _known_operation(
            operation,
            input_value,
            allow_prepared_gmail=True,
            transfer=transfer,
        )
        if (credential is None) is not (transport is None):
            raise ValidationError("Google effect preflight requires credential and transport")
        if (
            operation.provider == "gmail"
            and operation.name in _GMAIL_LOCAL_UPLOAD_OPERATIONS
            and transfer is not None
            and _gmail_has_local_attachment(values, transfer)
        ):
            reply_context = None
            if "reply_to_message_id" in values:
                if credential is None or transport is None:
                    raise ValidationError(
                        "Google effect preflight requires credential and transport"
                    )
                reply_context = _gmail_reply_context(values, credential, transport)
            _gmail_mime_upload(values, transfer, reply_context=reply_context)
            return operation.effect
        if (
            operation.provider == "google_drive"
            and operation.name in _DRIVE_LOCAL_UPLOAD_OPERATIONS
            and _drive_has_binary_upload(values, transfer)
        ):
            return ConnectorEffect.OUTWARD
        if operation.provider != "google_calendar":
            return operation.effect
        if operation.name == "calendars.update" and values.get("calendar_id") != "primary":
            return ConnectorEffect.OUTWARD
        if operation.name == "events.create":
            return (
                ConnectorEffect.OUTWARD
                if _calendar_has_external_effect(values)
                else operation.effect
            )
        if operation.name in _EXISTING_CALENDAR_EVENT_MUTATIONS:
            if _calendar_has_external_effect(values):
                return ConnectorEffect.OUTWARD
            if credential is None or transport is None:
                return ConnectorEffect.OUTWARD
            event = _calendar_event_effect_preflight(values, credential, transport)
            if _calendar_event_is_shared(event):
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
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorAdapterResult:
        operation, values = _known_operation(operation, input_value, transfer=transfer)
        if not isinstance(credential, ConnectorRuntimeCredential):
            raise ValidationError("connector runtime credential is invalid")
        if (
            operation.provider == "google_drive"
            and operation.name in _DRIVE_LOCAL_UPLOAD_OPERATIONS
            and _drive_has_binary_upload(values, transfer)
            and not write_idempotency_key
        ):
            raise ValidationError(
                "Google Drive binary uploads require a fresh outward confirmation"
            )
        if not operation.scope_grant_satisfies(credential.granted_scopes):
            raise ValidationError("connector credential does not satisfy the operation scope")
        if (
            operation.provider == "google_calendar"
            and operation.name in _EXISTING_CALENDAR_EVENT_MUTATIONS
        ):
            event = _calendar_event_effect_preflight(values, credential, transport)
            if _calendar_event_is_shared(event) and write_idempotency_key is None:
                raise ValidationError(
                    "the Google Calendar event is shared; request a fresh outward "
                    "confirmation preview"
                )
        if operation.provider == "gmail":
            return _execute_gmail(
                operation,
                values,
                continuation,
                credential,
                transport,
                transfer=transfer,
            )
        if operation.provider == "google_calendar":
            return _execute_calendar(operation, values, continuation, credential, transport)
        if operation.provider == "google_drive":
            return _execute_drive(
                operation,
                values,
                continuation,
                credential,
                transport,
                transfer=transfer,
            )
        raise ValidationError("connector operation is not handled by Google")


def _known_operation(
    operation: object,
    input_value: object,
    *,
    allow_prepared_gmail: bool = False,
    transfer: ConnectorTransferContext | None = None,
) -> tuple[OperationSpec, dict[str, object]]:
    if not isinstance(operation, OperationSpec):
        raise ValidationError("connector operation is invalid")
    expected = _GOOGLE_BY_KEY.get(operation.key)
    if expected is None or operation != expected:
        raise ValidationError("connector operation is not in the Google catalog")
    validation_input = _gmail_prepared_validation_input(
        operation,
        input_value,
        allow_placeholder=allow_prepared_gmail,
        transfer=transfer,
    )
    validated = operation.validate_input(validation_input)
    if not isinstance(validated, dict):
        raise ValidationError("connector operation input is invalid")
    return operation, validated


def _gmail_prepared_validation_input(
    operation: OperationSpec,
    input_value: object,
    *,
    allow_placeholder: bool,
    transfer: ConnectorTransferContext | None,
) -> object:
    if (
        operation.provider != "gmail"
        or operation.name not in {"drafts.create", "drafts.update"}
        or not isinstance(input_value, Mapping)
    ):
        return input_value
    raw_attachments = input_value.get("attachments")
    if not isinstance(raw_attachments, list):
        return input_value
    attachments: list[object] = []
    changed = False
    for index, item in enumerate(raw_attachments):
        if not isinstance(item, Mapping):
            attachments.append(item)
            continue
        if "content_base64" in item or "local_file" in item:
            attachments.append(item)
            continue
        bound = transfer is not None and ("attachments", index, "local_file") in transfer.uploads
        if not allow_placeholder and not bound:
            attachments.append(item)
            continue
        prepared = dict(item)
        prepared["local_file"] = {
            "grant_id": "prepared",
            "relative_path": "prepared",
        }
        attachments.append(prepared)
        changed = True
    if not changed:
        return input_value
    result = dict(input_value)
    result["attachments"] = attachments
    return result


def _gmail_local_file_shape(input_value: object, path: ConnectorInputPath) -> bool:
    if (
        not isinstance(path, tuple)
        or len(path) != 3
        or path[0] != "attachments"
        or type(path[1]) is not int
        or path[1] < 0
        or path[2] != "local_file"
        or not isinstance(input_value, Mapping)
    ):
        return False
    attachments = input_value.get("attachments")
    if not isinstance(attachments, list) or path[1] >= len(attachments):
        return False
    attachment = attachments[path[1]]
    return (
        isinstance(attachment, Mapping) and attachment.get("local_file") == _LOCAL_FILE_LIMIT_MARKER
    )


def _drive_local_file_shape(input_value: object, path: ConnectorInputPath) -> bool:
    return (
        path == ("local_file",)
        and isinstance(input_value, Mapping)
        and input_value.get("local_file") == _LOCAL_FILE_LIMIT_MARKER
    )


def _execute_gmail(
    operation: OperationSpec,
    values: dict[str, object],
    continuation: object | None,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
    *,
    transfer: ConnectorTransferContext | None,
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
        return _gmail_attachment_request(
            transport,
            credential=credential,
            values=values,
            transfer=transfer,
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
        reply_context = _gmail_reply_context(values, credential, transport)
        if _gmail_has_local_attachment(values, transfer):
            if transfer is None:
                raise ValidationError("Gmail local-file upload requires transfer context")
            return _gmail_resumable_draft_upload(
                transport,
                method=method,
                draft_path=draft_path,
                credential=credential,
                values=values,
                reply_context=reply_context,
                transfer=transfer,
            )
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=method,
            path=draft_path,
            credential=credential,
            json_body={"message": _gmail_message(values, reply_context=reply_context)},
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
        event_body = _calendar_event_body(values, include_client_id=name == "events.create")
        query = list(_optional_query(values, {"send_updates": "sendUpdates"}))
        if "drive_attachments" in values:
            event_body["attachments"] = _calendar_drive_attachments(values, credential, transport)
            query.append(("supportsAttachments", "true"))
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=method,
            path=path,
            credential=credential,
            query=tuple(query),
            json_body=event_body,
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
        event_path = (
            f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
            f"{_segment(_required(values, 'event_id'))}"
        )
        event = _provider_mapping(
            _json_request(
                transport,
                origin=ConnectorOrigin.GOOGLE,
                method=ConnectorMethod.GET,
                path=event_path,
                credential=credential,
                query=(("maxAttendees", "1"), ("fields", "attendees,etag")),
            ),
            context="Google Calendar RSVP preflight",
        )
        attendee, etag = _calendar_self_attendee(event, values)
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PATCH,
            path=event_path,
            credential=credential,
            query=_optional_query(values, {"send_updates": "sendUpdates"}),
            json_body={"attendees": [attendee], "attendeesOmitted": True},
            headers={"If-Match": etag},
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
    *,
    transfer: ConnectorTransferContext | None,
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
    if name in {"permissions.list", "revisions.list"}:
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
    if name == "comments.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}/comments",
            credential=credential,
            query=_drive_page_query(
                values,
                continuation,
                fields=_DRIVE_COMMENT_LIST_FIELDS,
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
            query=_drive_page_query(
                values,
                continuation,
                fields=_DRIVE_REPLY_LIST_FIELDS,
            ),
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
            query=_with_supports_all_drives(values, (("alt", "media"),)),
            range_download=True,
            values=values,
            transfer=transfer,
        )
    if name == "files.export":
        return _drive_content_request(
            transport,
            path=f"{base}/files/{_segment(_required(values, 'file_id'))}/export",
            credential=credential,
            query=(("mimeType", _required(values, "export_mime_type")),),
            range_download=False,
            values=values,
            transfer=transfer,
        )
    if name in {"files.create", "files.update"}:
        file_id = values.get("file_id")
        method = ConnectorMethod.POST
        metadata_path = f"{base}/files"
        upload_path = "/upload/drive/v3/files"
        if name == "files.update":
            method = ConnectorMethod.PATCH
            identifier = _segment(file_id)
            metadata_path += f"/{identifier}"
            upload_path += f"/{identifier}"
        metadata = _drive_file_metadata(values)
        content = values.get("content_base64")
        upload = _drive_local_upload(values, transfer)
        if name == "files.update" and not metadata and content is None and upload is None:
            raise ValidationError("Google Drive file update requires a metadata or content change")
        if content is not None or upload is not None:
            return _drive_resumable_upload(
                transport,
                method=method,
                path=upload_path,
                credential=credential,
                metadata=metadata,
                content_base64=None if content is None else _text(content),
                upload=upload,
                mime_type=_text(values.get("mime_type", "application/octet-stream")),
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
                    "allow_file_discovery": "allowFileDiscovery",
                    "domain": "domain",
                    "email_address": "emailAddress",
                    "expiration_time": "expirationTime",
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
            expected_statuses=_JSON_STATUSES,
        )
    return _json_request(
        transport,
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.DELETE,
        path=path,
        credential=credential,
        query=_with_supports_all_drives(values),
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
            query=(("fields", _DRIVE_COMMENT_RESOURCE_FIELDS),),
            json_body=_drive_comment_create_body(values),
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
            query=(("fields", _DRIVE_COMMENT_RESOURCE_FIELDS),),
            json_body={"content": _required(values, "content")},
            expected_statuses=_JSON_STATUSES,
        )
    return _json_request(
        transport,
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.DELETE,
        path=path,
        credential=credential,
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
            query=(("fields", _DRIVE_REPLY_RESOURCE_FIELDS),),
            json_body=_drive_reply_body(values),
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
            query=(("fields", _DRIVE_REPLY_RESOURCE_FIELDS),),
            json_body={"content": _required(values, "content")},
            expected_statuses=_JSON_STATUSES,
        )
    return _json_request(
        transport,
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.DELETE,
        path=path,
        credential=credential,
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


def _drive_delivery(values: Mapping[str, object]) -> str:
    delivery = values.get("delivery", "artifact")
    if not isinstance(delivery, str) or delivery not in {"artifact", "inline_chunk"}:
        raise ValidationError("Google Drive delivery is invalid")
    return delivery


def _drive_artifact_media_type(values: Mapping[str, object], *, export: bool) -> str:
    value = values.get("mime_type")
    if value is None and export:
        value = values.get("export_mime_type")
    if value is None:
        value = "application/octet-stream"
    return _mime_type(_text(value))


def _drive_artifact_filename(values: Mapping[str, object], *, export: bool) -> str:
    filename = values.get("filename")
    if filename is not None:
        return _text(filename)
    file_id = _required(values, "file_id")
    media_type = _drive_artifact_media_type(values, export=export)
    suffix = {
        "application/json": ".json",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "text/csv": ".csv",
        "text/plain": ".txt",
    }.get(media_type, "")
    if export:
        return f"drive-export-{file_id}{suffix}"
    revision_id = values.get("revision_id")
    if revision_id is not None:
        return f"drive-file-{file_id}-revision-{_text(revision_id)}{suffix}"
    return f"drive-file-{file_id}{suffix}"


def _drive_content_request(
    transport: ConnectorTransport,
    *,
    path: str,
    credential: ConnectorRuntimeCredential,
    query: Sequence[tuple[str, str]],
    range_download: bool,
    values: dict[str, object],
    transfer: ConnectorTransferContext | None,
) -> ConnectorAdapterResult:
    delivery = _drive_delivery(values)
    if delivery == "artifact":
        if transfer is None:
            raise ValidationError("Google Drive artifact delivery requires transfer context")
        media_type = _drive_artifact_media_type(values, export=not range_download)
        writer = transfer.artifacts.start(
            _drive_artifact_filename(values, export=not range_download),
            media_type=media_type,
        )
        artifact_response = transport.download_stream(
            origin=ConnectorOrigin.GOOGLE,
            sink=writer,
            path=path,
            credential=credential.credential,
            query=query,
            max_bytes=MAX_ARTIFACT_BYTES,
        )
        if artifact_response.artifact is None:
            raise ValidationError("Google Drive artifact delivery produced no receipt")
        return ConnectorAdapterResult(
            {"bytes": artifact_response.bytes_transferred, "delivery": "artifact"},
            artifact=artifact_response.artifact,
        )

    byte_offset = _integer(values.get("byte_offset", 0))
    maximum = _integer(values.get("max_chunk_size", _MAX_CONTENT_BYTES))
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


def _drive_local_upload(
    values: Mapping[str, object], transfer: ConnectorTransferContext | None
) -> PreparedUpload | None:
    has_bound_upload = transfer is not None and ("local_file",) in transfer.uploads
    if "local_file" not in values and not has_bound_upload:
        return None
    if transfer is None:
        raise ValidationError("Google Drive local-file upload requires transfer context")
    if values.get("content_base64") is not None:
        raise ValidationError("Google Drive upload has multiple binary sources")
    return transfer.upload(("local_file",))


def _drive_has_binary_upload(
    values: Mapping[str, object], transfer: ConnectorTransferContext | None
) -> bool:
    return (
        "content_base64" in values
        or "local_file" in values
        or (transfer is not None and ("local_file",) in transfer.uploads)
    )


def _drive_resumable_upload(
    transport: ConnectorTransport,
    *,
    method: ConnectorMethod,
    path: str,
    credential: ConnectorRuntimeCredential,
    metadata: dict[str, object],
    content_base64: str | None,
    upload: PreparedUpload | None,
    mime_type: str,
    query: Sequence[tuple[str, str]],
) -> ConnectorAdapterResult:
    if (content_base64 is None) == (upload is None):
        raise ValidationError("Google Drive upload requires exactly one binary source")
    initiated = transport.request(
        origin=ConnectorOrigin.GOOGLE,
        method=method,
        path=path,
        credential=credential.credential,
        query=query,
        json_body=metadata,
        expected_statuses=_JSON_STATUSES,
    )
    location = _response_location(initiated.headers)
    if upload is not None:
        streamed = transport.request_stream(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PUT,
            source=upload,
            content_length=upload.size,
            location=location,
            credential=None,
            content_type=_mime_type(upload.media_type or mime_type),
            expected_statuses=_JSON_STATUSES,
        )
        return _json_result(streamed.json(), strip_upload_location=True)
    else:
        assert content_base64 is not None
        uploaded = transport.request_provider_location(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.PUT,
            location=location,
            credential=None,
            body=_decode_base64(content_base64),
            content_type=_mime_type(mime_type),
            expected_statuses=_JSON_STATUSES,
        )
        return _json_result(uploaded.json(), strip_upload_location=True)


def _json_result(payload: object, *, strip_upload_location: bool = False) -> ConnectorAdapterResult:
    if not isinstance(payload, Mapping):
        return ConnectorAdapterResult(payload)
    continuation: object | None = None
    if isinstance(payload.get("nextPageToken"), str):
        continuation = payload["nextPageToken"]
    elif isinstance(payload.get("nextSyncToken"), str):
        continuation = {"syncToken": payload["nextSyncToken"]}
    return ConnectorAdapterResult(
        _strip_provider_state(payload, strip_upload_location=strip_upload_location),
        continuation=continuation,
    )


def _provider_mapping(result: ConnectorAdapterResult, *, context: str) -> Mapping[str, object]:
    if not isinstance(result.payload, Mapping):
        raise ValidationError(f"{context} returned an invalid resource")
    return result.payload


def _strip_provider_state(value: object, *, strip_upload_location: bool = False) -> object:
    if not isinstance(value, Mapping):
        return value
    blocked = _PAGINATION_FIELDS
    if strip_upload_location:
        blocked |= _UPLOAD_LOCATION_FIELDS
    return {
        str(key): item for key, item in value.items() if isinstance(key, str) and key not in blocked
    }


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
    if shared_corpora and not shared_items:
        raise ValidationError(
            "Google Drive shared-drive corpus requires include-items-from-all-drives"
        )
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
    values: dict[str, object],
    continuation: object | None,
    *,
    fields: str | None = None,
) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if fields is not None:
        query.append(("fields", fields))
    query.extend(_optional_query(values, {"page_size": "pageSize"}))
    query.extend(_continuation_query(continuation))
    return tuple(query)


def _drive_move_query(values: dict[str, object]) -> tuple[tuple[str, str], ...]:
    add_parent_ids = _strings(values["add_parent_ids"]) if "add_parent_ids" in values else []
    remove_parent_ids = (
        _strings(values["remove_parent_ids"]) if "remove_parent_ids" in values else []
    )
    if not add_parent_ids and not remove_parent_ids:
        raise ValidationError("Google Drive move requires a parent change")
    if set(add_parent_ids) & set(remove_parent_ids):
        raise ValidationError("Google Drive move cannot add and remove the same parent")
    query: list[tuple[str, str]] = []
    if add_parent_ids:
        query.append(("addParents", ",".join(add_parent_ids)))
    if remove_parent_ids:
        query.append(("removeParents", ",".join(remove_parent_ids)))
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


def _calendar_drive_attachments(
    values: dict[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> list[dict[str, str]]:
    references = _mappings(_required_value(values, "drive_attachments"))
    if references and not _DRIVE_ATTACHMENT_SCOPES.intersection(credential.granted_scopes):
        raise ValidationError(
            "Google Calendar Drive attachments require Drive access on this Google connection"
        )
    attachments: list[dict[str, str]] = []
    seen: set[str] = set()
    for reference in references:
        file_id = _required(reference, "file_id")
        if file_id in seen:
            raise ValidationError("Google Calendar Drive attachment IDs must be unique")
        seen.add(file_id)
        file = _provider_mapping(
            _json_request(
                transport,
                origin=ConnectorOrigin.GOOGLE,
                method=ConnectorMethod.GET,
                path=f"/drive/v3/files/{_segment(file_id)}",
                credential=credential,
                query=(
                    ("fields", "id,mimeType,name,webViewLink"),
                    ("supportsAllDrives", "true"),
                ),
            ),
            context="Google Drive attachment preflight",
        )
        if _provider_text(file, "id", context="Google Drive attachment preflight") != file_id:
            raise ValidationError("Google Drive attachment preflight returned a different file")
        attachments.append(
            {
                "fileUrl": _drive_web_view_link(file),
                "mimeType": _mime_type(
                    _provider_text(file, "mimeType", context="Google Drive attachment preflight")
                ),
                "title": _provider_text(
                    file,
                    "name",
                    context="Google Drive attachment preflight",
                    allow_empty=True,
                ),
            }
        )
    return attachments


def _drive_web_view_link(file: Mapping[str, object]) -> str:
    link = _provider_text(file, "webViewLink", context="Google Drive attachment preflight")
    if len(link) > 8_192:
        raise ValidationError("Google Drive attachment preflight returned an invalid web link")
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(
            "Google Drive attachment preflight returned an invalid web link"
        ) from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or not (hostname == "google.com" or hostname.endswith(".google.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValidationError("Google Drive attachment preflight returned an invalid web link")
    return link


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


def _calendar_self_attendee(
    event: Mapping[str, object], values: Mapping[str, object]
) -> tuple[dict[str, object], str]:
    etag = _safe_header(_provider_text(event, "etag", context="Google Calendar RSVP preflight"))
    attendees = _mappings(event.get("attendees"))
    participants = [attendee for attendee in attendees if attendee.get("self") is True]
    if len(participants) != 1:
        raise ValidationError("Google Calendar RSVP preflight did not identify one self attendee")
    participant = participants[0]
    attendee: dict[str, object] = {
        "email": _provider_text(
            participant,
            "email",
            context="Google Calendar RSVP preflight",
        ),
        "responseStatus": _required(values, "response_status"),
    }
    if "comment" in values:
        attendee["comment"] = _text(values["comment"])
    elif isinstance(participant.get("comment"), str):
        attendee["comment"] = participant["comment"]
    return attendee, etag


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
    if (
        "attendee_emails" in values
        or "attendees" in values
        or "drive_attachments" in values
        or "send_updates" in values
    ):
        return True
    calendar_ids = [values.get("calendar_id"), values.get("destination_calendar_id")]
    return any(isinstance(value, str) and value != "primary" for value in calendar_ids)


def _calendar_event_effect_preflight(
    values: Mapping[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> Mapping[str, object]:
    path = (
        "/calendar/v3/calendars/"
        f"{_segment(_required(values, 'calendar_id'))}/events/"
        f"{_segment(_required(values, 'event_id'))}"
    )
    return _provider_mapping(
        _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=path,
            credential=credential,
            query=(("fields", "attendees,organizer"),),
        ),
        context="Google Calendar event effect preflight",
    )


def _calendar_event_is_shared(event: Mapping[str, object]) -> bool:
    attendees = event.get("attendees", [])
    if not isinstance(attendees, list):
        raise ValidationError("Google Calendar event effect preflight returned invalid attendees")
    for attendee in attendees:
        if not isinstance(attendee, Mapping):
            raise ValidationError(
                "Google Calendar event effect preflight returned invalid attendees"
            )
        if attendee.get("self") is not True:
            return True
    organizer = event.get("organizer")
    if organizer is None:
        return False
    if not isinstance(organizer, Mapping):
        raise ValidationError("Google Calendar event effect preflight returned invalid organizer")
    return organizer.get("self") is not True


def _drive_file_metadata(values: dict[str, object]) -> dict[str, object]:
    metadata = _selected(
        values,
        {
            "description": "description",
            "mime_type": "mimeType",
            "name": "name",
        },
    )
    if "parent_id" in values:
        metadata["parents"] = [_required(values, "parent_id")]
    if "app_properties" in values:
        properties: dict[str, object] = {}
        for item in _mappings(values["app_properties"]):
            key = _text(item.get("key"))
            if key in properties:
                raise ValidationError("Google Drive app property keys must be unique")
            value = item.get("value")
            encoded_value_length = 0
            if value is not None:
                value = _text(value)
                encoded_value_length = len(value.encode("utf-8"))
            if len(key.encode("utf-8")) + encoded_value_length > 124:
                raise ValidationError(
                    "Google Drive app property key and value exceed 124 UTF-8 bytes"
                )
            properties[key] = value
        metadata["appProperties"] = properties
    return metadata


def _drive_comment_create_body(values: dict[str, object]) -> dict[str, object]:
    body = _selected(values, {"anchor": "anchor", "content": "content"})
    if "quoted_file_content" in values:
        quoted = cast(Mapping[str, object], values["quoted_file_content"])
        body["quotedFileContent"] = _selected(
            quoted,
            {"mime_type": "mimeType", "value": "value"},
        )
    return body


def _drive_reply_body(values: dict[str, object]) -> dict[str, object]:
    return _selected(values, {"action": "action", "content": "content"})


def _gmail_reply_context(
    values: dict[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> _GmailReplyContext | None:
    if "reply_to_message_id" not in values:
        if "thread_id" in values:
            raise ValidationError("Gmail thread ID is derived from reply_to_message_id")
        return None
    provider_message_id = _required(values, "reply_to_message_id")
    message = _provider_mapping(
        _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"/gmail/v1/users/me/messages/{_segment(provider_message_id)}",
            credential=credential,
            query=(
                ("format", "metadata"),
                ("metadataHeaders", "Message-ID"),
                ("metadataHeaders", "References"),
                ("metadataHeaders", "Subject"),
            ),
        ),
        context="Gmail reply preflight",
    )
    thread_id = _provider_text(message, "threadId", context="Gmail reply preflight")
    if "thread_id" in values and _required(values, "thread_id") != thread_id:
        raise ValidationError("Gmail reply thread does not match the original message")
    headers = _gmail_metadata_headers(message)
    message_id_values = headers.get("message-id", ())
    if len(message_id_values) != 1:
        raise ValidationError("Gmail reply preflight did not return one RFC Message-ID")
    message_id = _rfc_message_id(message_id_values[0])
    references: list[str] = []
    for value in headers.get("references", ()):
        if len(value) > 8_192:
            raise ValidationError("Gmail reply References header is invalid")
        parsed = _MESSAGE_ID_REFERENCE.findall(_safe_header(value))
        if _MESSAGE_ID_REFERENCE.sub("", value).strip() or len(parsed) > 100:
            raise ValidationError("Gmail reply References header is invalid")
        for reference in parsed:
            normalized = _rfc_message_id(reference)
            if normalized not in references:
                references.append(normalized)
    if message_id not in references:
        references.append(message_id)
    subject_values = headers.get("subject", ())
    if len(subject_values) > 1:
        raise ValidationError("Gmail reply preflight returned multiple Subject headers")
    original_subject = subject_values[0] if subject_values else ""
    subject = _gmail_reply_subject(original_subject)
    if "subject" in values and _text(values["subject"]) != subject:
        raise ValidationError("Gmail reply Subject must match the original message")
    return _GmailReplyContext(
        message_id=message_id,
        references=tuple(references),
        subject=subject,
        thread_id=thread_id,
    )


def _gmail_metadata_headers(message: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise ValidationError("Gmail reply preflight returned invalid metadata")
    raw_headers = payload.get("headers")
    if not isinstance(raw_headers, list):
        raise ValidationError("Gmail reply preflight returned invalid metadata")
    selected: dict[str, list[str]] = {}
    wanted = {"message-id", "references", "subject"}
    for raw_header in raw_headers:
        if not isinstance(raw_header, Mapping):
            raise ValidationError("Gmail reply preflight returned invalid metadata")
        name = raw_header.get("name")
        if not isinstance(name, str) or name.casefold() not in wanted:
            continue
        value = raw_header.get("value")
        if not isinstance(value, str):
            raise ValidationError("Gmail reply preflight returned invalid metadata")
        selected.setdefault(name.casefold(), []).append(value)
    return {name: tuple(values) for name, values in selected.items()}


def _rfc_message_id(value: str) -> str:
    safe = _safe_header(value).strip()
    if len(safe) > 998 or _MESSAGE_ID.fullmatch(safe) is None:
        raise ValidationError("Gmail reply Message-ID is invalid")
    return safe


def _gmail_reply_subject(value: str) -> str:
    subject = _safe_header(value).strip()
    if len(subject) > 998:
        raise ValidationError("Gmail reply Subject is invalid")
    return subject


def _gmail_attachment_request(
    transport: ConnectorTransport,
    *,
    credential: ConnectorRuntimeCredential,
    values: Mapping[str, object],
    transfer: ConnectorTransferContext | None,
) -> ConnectorAdapterResult:
    path = (
        f"/gmail/v1/users/me/messages/{_segment(_required(values, 'message_id'))}/attachments/"
        f"{_segment(_required(values, 'attachment_id'))}"
    )
    delivery = values.get("delivery", "artifact")
    if delivery == "artifact":
        if transfer is None:
            raise ValidationError("Gmail attachment artifact delivery requires transfer context")
        writer = transfer.artifacts.start(
            "gmail-attachment.bin",
            media_type="application/octet-stream",
        )
        decoder = GmailMessagePartBodyDecoder(writer=writer)
        try:
            response = transport.download_stream(
                origin=ConnectorOrigin.GMAIL,
                sink=decoder,
                path=path,
                credential=credential.credential,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        except Exception:
            decoder.abort()
            raise
        if response.artifact is None:
            decoder.abort()
            raise ValidationError("Gmail attachment artifact delivery produced no receipt")
        return ConnectorAdapterResult(
            {"bytes": decoder.decoded_size, "delivery": "artifact"},
            artifact=response.artifact,
        )

    if delivery != "inline_chunk":
        raise ValidationError("Gmail attachment delivery is invalid")
    decoder = GmailMessagePartBodyDecoder()
    try:
        transport.download_stream(
            origin=ConnectorOrigin.GMAIL,
            sink=decoder,
            path=path,
            credential=credential.credential,
            max_bytes=MAX_ARTIFACT_BYTES,
        )
    except Exception:
        decoder.abort()
        raise
    content = decoder.inline_content
    return ConnectorAdapterResult(
        {
            "content_base64": base64.b64encode(content).decode("ascii"),
            "delivery": "inline_chunk",
        }
    )


def _gmail_has_local_attachment(
    values: Mapping[str, object], transfer: ConnectorTransferContext | None
) -> bool:
    for index, attachment in enumerate(_mappings(values.get("attachments", []))):
        if "local_file" in attachment:
            return True
        if transfer is not None and ("attachments", index, "local_file") in transfer.uploads:
            return True
    return False


def _gmail_attachment_uploads(
    values: Mapping[str, object], transfer: ConnectorTransferContext | None
) -> tuple[GmailMimeAttachment, ...]:
    attachments: list[GmailMimeAttachment] = []
    for index, attachment in enumerate(_mappings(values.get("attachments", []))):
        filename = _safe_header(_text(attachment.get("filename")))
        mime_type = _mime_type(_text(attachment.get("mime_type")))
        path = ("attachments", index, "local_file")
        has_marker = "local_file" in attachment
        has_bound_upload = transfer is not None and path in transfer.uploads
        if has_marker or has_bound_upload:
            if "content_base64" in attachment:
                raise ValidationError("Gmail attachment has multiple binary sources")
            if transfer is None:
                raise ValidationError("Gmail local-file upload requires transfer context")
            attachments.append(
                GmailMimeAttachment(
                    filename=filename,
                    mime_type=mime_type,
                    upload=transfer.upload(path),
                )
            )
            continue
        if "content_base64" not in attachment:
            raise ValidationError("Gmail attachment has no supported binary source")
        attachments.append(
            GmailMimeAttachment(
                filename=filename,
                mime_type=mime_type,
                inline_content=_decode_base64(_text(attachment["content_base64"])),
            )
        )
    return tuple(attachments)


def _gmail_mime_upload(
    values: Mapping[str, object],
    transfer: ConnectorTransferContext,
    *,
    reply_context: _GmailReplyContext | None,
) -> GmailMimeUpload:
    return GmailMimeUpload(
        headers=tuple(_gmail_header_message(values, reply_context=reply_context).items()),
        body=_gmail_body_bytes(values),
        attachments=_gmail_attachment_uploads(values, transfer),
    )


def _gmail_resumable_draft_upload(
    transport: ConnectorTransport,
    *,
    method: ConnectorMethod,
    draft_path: str,
    credential: ConnectorRuntimeCredential,
    values: Mapping[str, object],
    reply_context: _GmailReplyContext | None,
    transfer: ConnectorTransferContext,
) -> ConnectorAdapterResult:
    mime = _gmail_mime_upload(values, transfer, reply_context=reply_context)
    metadata: dict[str, object] = {"message": {}}
    if reply_context is not None:
        metadata["message"] = {"threadId": reply_context.thread_id}
    initiated = transport.request(
        origin=ConnectorOrigin.GMAIL,
        method=method,
        path=f"/upload{draft_path}",
        credential=credential.credential,
        query=(("uploadType", "resumable"),),
        json_body=metadata,
        headers={
            "X-Upload-Content-Length": str(mime.size),
            "X-Upload-Content-Type": "message/rfc822",
        },
        expected_statuses=_JSON_STATUSES,
    )
    location = _response_location(initiated.headers)
    uploaded = transport.request_stream(
        origin=ConnectorOrigin.GMAIL,
        method=ConnectorMethod.PUT,
        source=mime,
        content_length=mime.size,
        location=location,
        credential=None,
        content_type="message/rfc822",
        expected_statuses=_JSON_STATUSES,
    )
    return _json_result(uploaded.json(), strip_upload_location=True)


def _gmail_header_message(
    values: Mapping[str, object], *, reply_context: _GmailReplyContext | None
) -> EmailMessage:
    message = EmailMessage()
    for source, header in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
        if source in values:
            message[header] = ", ".join(_safe_header(value) for value in _strings(values[source]))
    if reply_context is not None:
        message["Subject"] = reply_context.subject
        message["In-Reply-To"] = reply_context.message_id
        message["References"] = " ".join(reply_context.references)
    elif "subject" in values:
        message["Subject"] = _safe_header(_text(values["subject"]))
    return message


def _gmail_body_bytes(values: Mapping[str, object]) -> bytes:
    message = EmailMessage()
    message.set_content(_text(values.get("text_body", "")))
    if "html_body" in values:
        message.add_alternative(_text(values["html_body"]), subtype="html")
    return message.as_bytes(policy=SMTP)


def _gmail_message(
    values: dict[str, object], *, reply_context: _GmailReplyContext | None
) -> dict[str, object]:
    message = _gmail_header_message(values, reply_context=reply_context)
    message.set_content(_text(values.get("text_body", "")))
    if "html_body" in values:
        message.add_alternative(_text(values["html_body"]), subtype="html")
    for attachment in _mappings(values.get("attachments", [])):
        if "content_base64" not in attachment:
            raise ValidationError("Gmail local-file upload requires transfer context")
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
    if reply_context is not None:
        result["threadId"] = reply_context.thread_id
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
    raise ValidationError("Google upload response has no provider location")


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


def _provider_text(
    values: Mapping[str, object],
    name: str,
    *,
    context: str,
    allow_empty: bool = False,
) -> str:
    value = values.get(name)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValidationError(f"{context} omitted required provider metadata")
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
