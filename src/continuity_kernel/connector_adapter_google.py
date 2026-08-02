"""Fixed Google provider adapter for the bounded connector operation catalog."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from io import BytesIO
from typing import Final, NoReturn, cast
from urllib.parse import urlsplit

from continuity_kernel.connector_adapter import (
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_contract import ConnectorEffect, OperationSpec
from continuity_kernel.connector_gmail_transfer import (
    GMAIL_MIGRATION_UPLOAD_MAX_BYTES,
    GMAIL_UPLOAD_MAX_BYTES,
    GmailMessagePartBodyDecoder,
    GmailMimeAttachment,
    GmailMimeUpload,
    gmail_raw_message_preview,
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
    ConnectorOutcomeUnknown,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError

_GOOGLE_PROVIDERS: Final = frozenset({"gmail", "google_calendar", "google_drive"})
_GOOGLE_BY_KEY: Final = {operation.key: operation for operation in GOOGLE_OPERATIONS}
_GMAIL_PURGE_SCOPE: Final = "https://mail.google.com/"
_GMAIL_MAX_HISTORY_ID: Final = 2**64 - 1
_JSON_STATUSES: Final = frozenset({200, 201, 202})
_DELETE_STATUSES: Final = frozenset({200, 202, 204})
_MAX_CONTENT_BYTES: Final = 180_000
_LOCAL_FILE_LIMIT_MARKER: Final = "opaque-local-file"
_GMAIL_ATTACHMENT_UPLOAD_OPERATIONS: Final = frozenset(
    {"drafts.create", "drafts.update", "messages.send"}
)
_GMAIL_RAW_UPLOAD_OPERATIONS: Final = frozenset(
    {"messages.import", "messages.insert", "messages.send"}
)
_GMAIL_LOCAL_UPLOAD_OPERATIONS: Final = (
    _GMAIL_ATTACHMENT_UPLOAD_OPERATIONS | _GMAIL_RAW_UPLOAD_OPERATIONS
)
_DRIVE_LOCAL_UPLOAD_OPERATIONS: Final = frozenset({"files.create", "files.update"})
_GMAIL_MAX_RAW_ATTACHMENT_BYTES: Final = (GMAIL_UPLOAD_MAX_BYTES * 3) // 4
_GMAIL_RESUMABLE_INIT_STATUSES: Final = frozenset({200})
_GMAIL_RESUMABLE_FINAL_STATUSES: Final = frozenset({200, 201})
_GMAIL_WRITE_RECEIPT_FIELDS: Final = frozenset(
    {"historyId", "id", "labelIds", "sizeEstimate", "threadId"}
)
_GMAIL_USERS_ME_BASE: Final = "/gmail/v1/users/me"
_GMAIL_SIMPLE_SETTINGS_READ_PATHS: Final = {
    "settings.auto_forwarding.get": "autoForwarding",
    "settings.filters.list": "filters",
    "settings.forwarding_addresses.list": "forwardingAddresses",
    "settings.imap.get": "imap",
    "settings.language.get": "language",
    "settings.pop.get": "pop",
    "settings.vacation.get": "vacation",
}
_GMAIL_SEND_AS_FIELDS: Final = frozenset(
    {
        "displayName",
        "isDefault",
        "isPrimary",
        "replyToAddress",
        "sendAsEmail",
        "signature",
        "treatAsAlias",
        "verificationStatus",
    }
)
_GMAIL_SEND_AS_BOOLEAN_FIELDS: Final = frozenset({"isDefault", "isPrimary", "treatAsAlias"})
_GMAIL_SEND_AS_STRING_FIELDS: Final = _GMAIL_SEND_AS_FIELDS - _GMAIL_SEND_AS_BOOLEAN_FIELDS
_GMAIL_SEND_AS_FIELDS_QUERY: Final = ",".join(sorted(_GMAIL_SEND_AS_FIELDS))
_GMAIL_SEND_AS_LIST_FIELDS_QUERY: Final = f"sendAs({_GMAIL_SEND_AS_FIELDS_QUERY})"
_GMAIL_FILTER_FIELDS: Final = frozenset({"action", "criteria", "id"})
# These provider-shape allowlists deliberately mirror the closed camelCase
# snapshot schema in connector_operations_google. Drift fails closed in tests.
_GMAIL_FILTER_CRITERIA_FIELDS: Final = frozenset(
    {
        "excludeChats",
        "from",
        "hasAttachment",
        "negatedQuery",
        "query",
        "size",
        "sizeComparison",
        "subject",
        "to",
    }
)
_GMAIL_FILTER_ACTION_FIELDS: Final = frozenset({"addLabelIds", "forward", "removeLabelIds"})
_DRIVE_MAX_LOCAL_FILE_BYTES: Final = 5 * 1024**4
_DRIVE_RESUMABLE_INIT_STATUSES: Final = frozenset({200})
_DRIVE_RESUMABLE_FINAL_STATUSES: Final = frozenset({200, 201})
_DRIVE_RESUMABLE_STREAM_STATUSES: Final = _DRIVE_RESUMABLE_FINAL_STATUSES | frozenset({308})
_DRIVE_RESUMABLE_MAX_ATTEMPTS: Final = 8
_DRIVE_RESUMABLE_MAX_NO_PROGRESS: Final = 2
_DRIVE_RESUMABLE_MAX_RESTARTS: Final = 1
_CONTENT_RANGE: Final = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_DRIVE_DOWNLOAD_OPERATION_NAME: Final = re.compile(r"^(?:operations/)?([A-Za-z0-9._~-]{1,480})$")
_DRIVE_RESOURCE_COMPONENT: Final = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_DRIVE_DOWNLOAD_RESPONSE_TYPE: Final = (
    "type.googleapis.com/google.apps.drive.v3.DownloadFileResponse"
)
_DRIVE_DOWNLOAD_RETRY_SECONDS: Final = 10
_DRIVE_DOWNLOAD_ERROR_STATUS: Final = {
    1: (499, "cancelled"),
    2: (500, "unknown"),
    3: (400, "invalid_argument"),
    4: (504, "deadline_exceeded"),
    5: (404, "not_found"),
    6: (409, "already_exists"),
    7: (403, "permission_denied"),
    8: (429, "resource_exhausted"),
    9: (400, "failed_precondition"),
    10: (409, "aborted"),
    11: (400, "out_of_range"),
    12: (501, "unimplemented"),
    13: (500, "internal"),
    14: (503, "unavailable"),
    15: (500, "data_loss"),
    16: (401, "unauthenticated"),
}
_MESSAGE_ID: Final = re.compile(r"^<[^<>\s]+@[^<>\s]+>$")
_MESSAGE_ID_REFERENCE: Final = re.compile(r"<[^<>\s]+@[^<>\s]+>")
_PAGINATION_FIELDS: Final = frozenset({"nextPageToken", "nextSyncToken", "nextLink", "nextPage"})
_UPLOAD_LOCATION_FIELDS: Final = frozenset({"location"})
_UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_EXISTING_CALENDAR_EVENT_MUTATIONS: Final = frozenset(
    {"events.delete", "events.move", "events.respond", "events.update"}
)
_CALENDAR_RECURRENCE_LINE: Final = re.compile(
    r"^(?:RRULE|EXRULE|RDATE|EXDATE)(?:;[^:\r\n]*)?:[^\r\n]+$"
)
_CALENDAR_RFC3339: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})?$"
)
_CALENDAR_MAX_EXTENDED_PROPERTIES: Final = 300
_CALENDAR_MAX_EXTENDED_PROPERTY_BYTES: Final = 32_768
_CALENDAR_STATUS_PROPERTY_FIELDS: Final = frozenset(
    {
        "focus_time_properties",
        "out_of_office_properties",
        "working_location_properties",
    }
)
_CALENDAR_EVENT_SYNC_FILTER_FIELDS: Final = frozenset(
    {
        "i_cal_uid",
        "order_by",
        "private_extended_properties",
        "query",
        "shared_extended_properties",
        "time_max",
        "time_min",
        "updated_min",
    }
)
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
            and operation.name in _GMAIL_RAW_UPLOAD_OPERATIONS
            and _gmail_raw_local_file_shape(input_value, path)
        ):
            return (
                GMAIL_UPLOAD_MAX_BYTES
                if operation.name == "messages.send"
                else GMAIL_MIGRATION_UPLOAD_MAX_BYTES
            )
        if (
            operation.provider == "gmail"
            and operation.name in _GMAIL_ATTACHMENT_UPLOAD_OPERATIONS
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

    def prepared_confirmation_preview(
        self,
        operation: OperationSpec,
        preview_value: object,
        *,
        transfer: ConnectorTransferContext,
    ) -> object | None:
        """Describe one immutable raw Gmail upload without provider access."""

        if not isinstance(operation, OperationSpec):
            raise ValidationError("Google operation is invalid")
        expected = _GOOGLE_BY_KEY.get(operation.key)
        if expected is None or operation != expected:
            raise ValidationError("Google operation is not in the Google catalog")
        if (
            operation.provider != "gmail"
            or operation.name not in _GMAIL_RAW_UPLOAD_OPERATIONS
            or ("local_file",) not in transfer.uploads
        ):
            return None
        if not isinstance(preview_value, Mapping):
            raise ValidationError("Gmail raw-message preview input is invalid")

        source = _gmail_effective_internal_date_source(operation.name, preview_value)
        raw_message = gmail_raw_message_preview(
            transfer.upload(("local_file",)),
            strict_send=operation.name == "messages.send",
            require_date=source == "dateHeader",
        )
        return {
            "gmail_raw_message": {
                **raw_message,
                "consequences": _gmail_raw_upload_consequences(operation.name, preview_value),
                "effective_internal_date_source": source,
                "media_type": "message/rfc822",
            }
        }

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
            and operation.name in _GMAIL_RAW_UPLOAD_OPERATIONS
            and transfer is not None
            and ("local_file",) in transfer.uploads
        ):
            source = _gmail_effective_internal_date_source(operation.name, values)
            gmail_raw_message_preview(
                transfer.upload(("local_file",)),
                strict_send=operation.name == "messages.send",
                require_date=source == "dateHeader",
            )
            if values.get("deleted") is True:
                return ConnectorEffect.PERMANENT
            if values.get("process_for_calendar") is True:
                return ConnectorEffect.OUTWARD
            return operation.effect
        if operation.provider == "gmail" and operation.name == "messages.send":
            reply_context = None
            if "reply_to_message_id" in values or "thread_id" in values:
                if credential is None or transport is None:
                    raise ValidationError(
                        "Google effect preflight requires credential and transport"
                    )
                reply_context = _gmail_reply_context(values, credential, transport)
            if transfer is not None and _gmail_has_local_attachment(values, transfer):
                _gmail_mime_upload(values, transfer, reply_context=reply_context)
            return operation.effect
        if (
            operation.provider == "gmail"
            and operation.name in _GMAIL_ATTACHMENT_UPLOAD_OPERATIONS
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
        if operation.provider == "gmail" and operation.name == "settings.filters.create":
            return _gmail_filter_effect(values)
        if operation.provider == "gmail" and operation.name == "settings.filters.delete":
            if credential is None or transport is None:
                raise ValidationError(
                    "Gmail filter deletion preflight requires credential and transport"
                )
            _gmail_filter_delete_snapshot(values, credential, transport)
            return operation.effect
        if operation.provider == "gmail" and operation.name == "settings.imap.update":
            return _gmail_imap_effect(values)
        if operation.provider == "gmail" and operation.name == "settings.pop.update":
            return _gmail_pop_effect(values)
        if operation.provider == "gmail" and operation.name == "settings.vacation.update":
            _validate_gmail_vacation(values)
            return (
                ConnectorEffect.OUTWARD
                if values.get("enable_auto_reply") is True
                else operation.effect
            )
        if operation.provider == "gmail" and operation.name == "settings.send_as.patch":
            _validate_gmail_send_as_patch(values)
            if credential is None or transport is None:
                raise ValidationError("Gmail send-as preflight requires credential and transport")
            _gmail_primary_send_as(values, credential, transport)
            return operation.effect
        if operation.provider != "google_calendar":
            return operation.effect
        if operation.name == "calendars.update":
            _calendar_body(values, require_change=True)
            return ConnectorEffect.OUTWARD
        if operation.name == "calendars.delete":
            _validate_secondary_calendar_target(values)
            if credential is not None and transport is not None:
                _calendar_delete_preflight(values, credential, transport)
            return operation.effect
        if operation.name == "events.create":
            _calendar_event_body(values, is_create=True, require_change=False)
            status_effect = _calendar_status_create_effect(values)
            if status_effect is not None:
                return status_effect
            return (
                ConnectorEffect.OUTWARD
                if _calendar_has_external_effect(values)
                else operation.effect
            )
        if operation.name == "events.move":
            _validate_calendar_move(values)
        if operation.name in _EXISTING_CALENDAR_EVENT_MUTATIONS:
            if operation.name == "events.update":
                _calendar_event_body(values, is_create=False, require_change=True)
            if credential is None or transport is None:
                return _calendar_event_effect(
                    operation.name,
                    values,
                    None,
                    catalog_effect=operation.effect,
                )
            event = _calendar_event_mutation_preflight(values, credential, transport)
            return _calendar_event_effect(
                operation.name,
                values,
                event,
                catalog_effect=operation.effect,
            )
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
        if (
            operation.provider == "gmail"
            and operation.name in _GMAIL_RAW_UPLOAD_OPERATIONS
            and "local_file" in values
        ):
            if transfer is None or ("local_file",) not in transfer.uploads:
                raise ValidationError(
                    "Gmail raw-message local_file requires its matching prepared transfer"
                )
            if not write_idempotency_key:
                raise ValidationError("Gmail raw-message uploads require a fresh confirmation")
        if not operation.scope_grant_satisfies(credential.granted_scopes):
            raise ValidationError("connector credential does not satisfy the operation scope")
        if (
            operation.provider == "google_calendar"
            and operation.name in _EXISTING_CALENDAR_EVENT_MUTATIONS
        ):
            if operation.name == "events.move":
                _validate_calendar_move(values)
            if operation.name == "events.update":
                _calendar_event_body(values, is_create=False, require_change=True)
            event = None
            if operation.name != "events.respond":
                event = _calendar_event_mutation_preflight(values, credential, transport)
            effect = _calendar_event_effect(
                operation.name,
                values,
                event,
                catalog_effect=operation.effect,
            )
            if effect is not ConnectorEffect.SAFE_MUTATION and write_idempotency_key is None:
                raise ValidationError(
                    "the Google Calendar event changed or affects other people; request a "
                    "fresh confirmation preview"
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
        or operation.name not in _GMAIL_LOCAL_UPLOAD_OPERATIONS
        or not isinstance(input_value, Mapping)
    ):
        return input_value
    result: dict[str, object] = dict(input_value)
    changed = False
    if (
        operation.name in _GMAIL_RAW_UPLOAD_OPERATIONS
        and "local_file" not in result
        and transfer is not None
        and ("local_file",) in transfer.uploads
    ):
        result["local_file"] = {
            "grant_id": "prepared",
            "relative_path": "prepared",
        }
        changed = True

    raw_attachments = result.get("attachments")
    if not isinstance(raw_attachments, list):
        return result if changed else input_value
    attachments: list[object] = []
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


def _gmail_raw_local_file_shape(input_value: object, path: ConnectorInputPath) -> bool:
    return (
        path == ("local_file",)
        and isinstance(input_value, Mapping)
        and input_value.get("local_file") == _LOCAL_FILE_LIMIT_MARKER
    )


def _gmail_effective_internal_date_source(
    operation_name: str,
    values: Mapping[str, object],
) -> str | None:
    if operation_name == "messages.send":
        return None
    default = "dateHeader" if operation_name == "messages.import" else "receivedTime"
    source = values.get("internal_date_source", default)
    if not isinstance(source, str) or source not in {"dateHeader", "receivedTime"}:
        raise ValidationError("Gmail internal-date source is invalid")
    return source


def _gmail_raw_upload_consequences(
    operation_name: str,
    values: Mapping[str, object],
) -> list[str]:
    if operation_name == "messages.send":
        return ["Gmail will send the exact RFC822 message to every To, Cc, and Bcc recipient"]
    consequences = [
        "Gmail will store the exact RFC822 message without sending its listed recipients"
    ]
    if values.get("deleted") is True:
        consequences.append(
            "The message will be permanently deleted rather than moved to Trash; this is "
            "Google Workspace only and remains visible only to Google Vault administrators"
        )
    if values.get("process_for_calendar") is True:
        consequences.append(
            "Gmail may extract meeting data and add it to the connected user's Google Calendar"
        )
    if values.get("never_mark_spam") is True:
        consequences.append(
            "Gmail will ignore its spam-classifier decision for this import; Gmail's import "
            "path performs no SPF checks"
        )
    return consequences


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
    base = _GMAIL_USERS_ME_BASE
    name = operation.name
    if name in {"messages.list", "threads.list"}:
        resource = name.split(".", 1)[0]
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/{resource}",
            credential=credential,
            query=_gmail_list_query(values, continuation),
        )
    if name == "drafts.list":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/drafts",
            credential=credential,
            query=_gmail_draft_list_query(values, continuation),
        )
    if name == "history.list":
        try:
            return _json_request(
                transport,
                origin=ConnectorOrigin.GMAIL,
                method=ConnectorMethod.GET,
                path=f"{base}/history",
                credential=credential,
                query=_gmail_history_query(values, continuation),
            )
        except ConnectorProviderError as exc:
            if exc.origin is ConnectorOrigin.GMAIL and exc.status == 404:
                raise ConnectorProviderError(
                    origin=exc.origin,
                    status=exc.status,
                    code="full_sync_required",
                    retry_after=exc.retry_after,
                ) from exc
            raise
    _reject_continuation(continuation)
    settings_path = _GMAIL_SIMPLE_SETTINGS_READ_PATHS.get(name)
    if settings_path is not None:
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/settings/{settings_path}",
            credential=credential,
        )
    if name == "settings.filters.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/settings/filters/{_segment(_required(values, 'filter_id'))}",
            credential=credential,
        )
    if name == "settings.forwarding_addresses.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=(
                f"{base}/settings/forwardingAddresses/"
                f"{_segment(_required(values, 'forwarding_email'))}"
            ),
            credential=credential,
        )
    if name == "settings.send_as.list":
        return _gmail_send_as_result(
            _json_request(
                transport,
                origin=ConnectorOrigin.GMAIL,
                method=ConnectorMethod.GET,
                path=f"{base}/settings/sendAs",
                credential=credential,
                query=(("fields", _GMAIL_SEND_AS_LIST_FIELDS_QUERY),),
            ),
            collection=True,
        )
    if name == "settings.send_as.get":
        return _gmail_send_as_result(
            _json_request(
                transport,
                origin=ConnectorOrigin.GMAIL,
                method=ConnectorMethod.GET,
                path=f"{base}/settings/sendAs/{_segment(_required(values, 'send_as_email'))}",
                credential=credential,
                query=(("fields", _GMAIL_SEND_AS_FIELDS_QUERY),),
            ),
            collection=False,
        )
    if name == "profile.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/profile",
            credential=credential,
        )
    if name == "messages.get" and values.get("format") == "raw":
        return _gmail_raw_message_request(
            transport,
            credential=credential,
            values=values,
            transfer=transfer,
        )
    if name == "messages.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/messages/{_segment(_required(values, 'message_id'))}",
            credential=credential,
            query=_gmail_get_query(values),
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
            query=_gmail_get_query(values),
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
    if name == "labels.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{base}/labels/{_segment(_required(values, 'label_id'))}",
            credential=credential,
        )
    if (
        name in _GMAIL_RAW_UPLOAD_OPERATIONS
        and transfer is not None
        and ("local_file",) in transfer.uploads
    ):
        return _gmail_raw_message_upload(
            transport,
            operation_name=name,
            credential=credential,
            values=values,
            upload=transfer.upload(("local_file",)),
        )
    if name in {"drafts.create", "drafts.update", "messages.send"}:
        draft_path = f"{base}/drafts"
        method = ConnectorMethod.POST
        if name == "drafts.update":
            method = ConnectorMethod.PUT
            draft_path += f"/{_segment(_required(values, 'draft_id'))}"
        elif name == "messages.send":
            draft_path = f"{base}/messages/send"
        reply_context = _gmail_reply_context(values, credential, transport)
        if _gmail_has_local_attachment(values, transfer):
            if transfer is None:
                raise ValidationError("Gmail local-file upload requires transfer context")
            metadata: dict[str, object]
            if name == "messages.send":
                metadata = {}
                if reply_context is not None:
                    metadata["threadId"] = reply_context.thread_id
            else:
                message_metadata: dict[str, object] = {}
                if reply_context is not None:
                    message_metadata["threadId"] = reply_context.thread_id
                metadata = {"message": message_metadata}
            return _gmail_resumable_message_upload(
                transport,
                method=method,
                upload_path=draft_path,
                credential=credential,
                values=values,
                reply_context=reply_context,
                transfer=transfer,
                metadata=metadata,
                privacy_safe_receipt=name == "messages.send",
            )
        message = _gmail_message(values, reply_context=reply_context)
        json_body = message if name == "messages.send" else {"message": message}
        if name == "messages.send":
            response = transport.request(
                origin=ConnectorOrigin.GMAIL,
                method=method,
                path=draft_path,
                credential=credential.credential,
                json_body=json_body,
                expected_statuses=_JSON_STATUSES,
            )
            return _gmail_message_write_receipt(response, action="sent")
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=method,
            path=draft_path,
            credential=credential,
            json_body=json_body,
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
    if name == "messages.batch_modify":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.POST,
            path=f"{base}/messages/batchModify",
            credential=credential,
            json_body=_selected(
                values,
                {
                    "add_label_ids": "addLabelIds",
                    "message_ids": "ids",
                    "remove_label_ids": "removeLabelIds",
                },
            ),
            expected_statuses=_DELETE_STATUSES,
        )
    if name == "messages.batch_purge":
        if operation.required_scopes != (frozenset({_GMAIL_PURGE_SCOPE}),):
            raise ValidationError("Gmail batch purge must require the full Gmail scope")
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.POST,
            path=f"{base}/messages/batchDelete",
            credential=credential,
            json_body={"ids": values["message_ids"]},
            expected_statuses=_DELETE_STATUSES,
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
    if name == "settings.filters.create":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.POST,
            path=f"{base}/settings/filters",
            credential=credential,
            json_body=_gmail_filter_body(values),
            expected_statuses=_JSON_STATUSES,
        )
    if name == "settings.filters.delete":
        _gmail_filter_delete_snapshot(values, credential, transport)
        return _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.DELETE,
            path=f"{base}/settings/filters/{_segment(_required(values, 'filter_id'))}",
            credential=credential,
            expected_statuses=_DELETE_STATUSES,
        )
    if name == "settings.imap.update":
        return _gmail_setting_update(
            transport,
            credential=credential,
            path=f"{base}/settings/imap",
            body=_selected(
                values,
                {
                    "auto_expunge": "autoExpunge",
                    "enabled": "enabled",
                    "expunge_behavior": "expungeBehavior",
                    "max_folder_size": "maxFolderSize",
                },
            ),
        )
    if name == "settings.language.update":
        return _gmail_setting_update(
            transport,
            credential=credential,
            path=f"{base}/settings/language",
            body={"displayLanguage": _required(values, "display_language")},
        )
    if name == "settings.pop.update":
        return _gmail_setting_update(
            transport,
            credential=credential,
            path=f"{base}/settings/pop",
            body=_selected(
                values,
                {"access_window": "accessWindow", "disposition": "disposition"},
            ),
        )
    if name == "settings.vacation.update":
        _validate_gmail_vacation(values)
        return _gmail_setting_update(
            transport,
            credential=credential,
            path=f"{base}/settings/vacation",
            body=_gmail_vacation_body(values),
        )
    if name == "settings.send_as.patch":
        _validate_gmail_send_as_patch(values)
        _gmail_primary_send_as(values, credential, transport)
        return _gmail_send_as_result(
            _json_request(
                transport,
                origin=ConnectorOrigin.GMAIL,
                method=ConnectorMethod.PATCH,
                path=f"{base}/settings/sendAs/{_segment(_required(values, 'send_as_email'))}",
                credential=credential,
                query=(("fields", _GMAIL_SEND_AS_FIELDS_QUERY),),
                json_body=_selected(
                    values,
                    {
                        "display_name": "displayName",
                        "is_default": "isDefault",
                        "reply_to_address": "replyToAddress",
                        "signature": "signature",
                    },
                ),
                expected_statuses=_JSON_STATUSES,
            ),
            collection=False,
        )
    raise ValidationError("Gmail operation has no fixed route")


def _gmail_setting_update(
    transport: ConnectorTransport,
    *,
    credential: ConnectorRuntimeCredential,
    path: str,
    body: Mapping[str, object],
) -> ConnectorAdapterResult:
    return _json_request(
        transport,
        origin=ConnectorOrigin.GMAIL,
        method=ConnectorMethod.PUT,
        path=path,
        credential=credential,
        json_body=dict(body),
        expected_statuses=_JSON_STATUSES,
    )


def _gmail_filter_parts(
    values: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    criteria = values.get("criteria")
    action = values.get("action")
    if not isinstance(criteria, Mapping) or not criteria:
        raise ValidationError("Gmail filter requires at least one matching criterion")
    if not isinstance(action, Mapping) or not action:
        raise ValidationError("Gmail filter requires at least one action")
    if ("size" in criteria) is not ("size_comparison" in criteria):
        raise ValidationError("Gmail filter size and comparison must be provided together")
    add_labels = action.get("add_label_ids", [])
    remove_labels = action.get("remove_label_ids", [])
    if not isinstance(add_labels, list) or not isinstance(remove_labels, list):
        raise ValidationError("Gmail filter label actions are invalid")
    if set(_strings(add_labels)) & set(_strings(remove_labels)):
        raise ValidationError("Gmail filter cannot add and remove the same label")
    return criteria, action


def _gmail_filter_body(values: Mapping[str, object]) -> dict[str, object]:
    criteria, action = _gmail_filter_parts(values)
    return {
        "action": _selected(
            action,
            {
                "add_label_ids": "addLabelIds",
                "forward": "forward",
                "remove_label_ids": "removeLabelIds",
            },
        ),
        "criteria": _selected(
            criteria,
            {
                "exclude_chats": "excludeChats",
                "from": "from",
                "has_attachment": "hasAttachment",
                "negated_query": "negatedQuery",
                "query": "query",
                "size": "size",
                "size_comparison": "sizeComparison",
                "subject": "subject",
                "to": "to",
            },
        ),
    }


def _gmail_filter_effect(values: Mapping[str, object]) -> ConnectorEffect:
    _criteria, action = _gmail_filter_parts(values)
    add_labels = set(_strings(action.get("add_label_ids", [])))
    remove_labels = set(_strings(action.get("remove_label_ids", [])))
    if add_labels.intersection({"SPAM", "TRASH"}) or "INBOX" in remove_labels:
        return ConnectorEffect.DESTRUCTIVE
    if "forward" in action:
        return ConnectorEffect.OUTWARD
    return ConnectorEffect.SAFE_MUTATION


def _gmail_filter_delete_snapshot(
    values: Mapping[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> None:
    filter_id = _required(values, "filter_id")
    expected = _gmail_filter_snapshot_resource(
        values.get("expected_filter"),
        context="Gmail expected filter",
    )
    if expected["id"] != filter_id:
        raise ValidationError("Gmail expected filter ID does not match the deletion target")
    current = _gmail_filter_snapshot_resource(
        _provider_mapping(
            _json_request(
                transport,
                origin=ConnectorOrigin.GMAIL,
                method=ConnectorMethod.GET,
                path=f"{_GMAIL_USERS_ME_BASE}/settings/filters/{_segment(filter_id)}",
                credential=credential,
            ),
            context="Gmail filter deletion preflight",
        ),
        context="Gmail current filter",
    )
    if current != expected:
        raise ConflictError("Gmail filter changed; read it again before deleting it")


def _gmail_filter_snapshot_resource(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or key not in _GMAIL_FILTER_FIELDS for key in value
    ):
        raise ValidationError(f"{context} contains unsupported fields")
    identifier = value.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ValidationError(f"{context} has no usable ID")
    return {
        "action": _gmail_filter_snapshot_part(
            value.get("action"),
            allowed=_GMAIL_FILTER_ACTION_FIELDS,
            context=f"{context} action",
        ),
        "criteria": _gmail_filter_snapshot_part(
            value.get("criteria"),
            allowed=_GMAIL_FILTER_CRITERIA_FIELDS,
            context=f"{context} criteria",
        ),
        "id": identifier,
    }


def _gmail_filter_snapshot_part(
    value: object,
    *,
    allowed: frozenset[str],
    context: str,
) -> dict[str, object]:
    if (
        not isinstance(value, Mapping)
        or not value
        or any(not isinstance(key, str) or key not in allowed for key in value)
    ):
        raise ValidationError(f"{context} is invalid")
    return {key: value[key] for key in sorted(allowed) if key in value}


def _gmail_send_as_result(
    result: ConnectorAdapterResult,
    *,
    collection: bool,
) -> ConnectorAdapterResult:
    payload = _provider_mapping(result, context="Gmail send-as response")
    if not collection:
        return ConnectorAdapterResult(_gmail_send_as_resource(payload))
    raw_resources = payload.get("sendAs", [])
    if not isinstance(raw_resources, list):
        raise ValidationError("Gmail send-as list returned an invalid resource collection")
    return ConnectorAdapterResult(
        {"sendAs": [_gmail_send_as_resource(resource) for resource in raw_resources]}
    )


def _gmail_send_as_resource(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError("Gmail send-as response returned an invalid resource")
    email = value.get("sendAsEmail")
    if not isinstance(email, str) or not email:
        raise ValidationError("Gmail send-as response has no usable email address")
    result: dict[str, object] = {}
    for key in sorted(_GMAIL_SEND_AS_FIELDS):
        if key not in value:
            continue
        field = value[key]
        if key in _GMAIL_SEND_AS_BOOLEAN_FIELDS:
            if type(field) is not bool:
                raise ValidationError(f"Gmail send-as response has an invalid {key} field")
        elif key in _GMAIL_SEND_AS_STRING_FIELDS and not isinstance(field, str):
            raise ValidationError(f"Gmail send-as response has an invalid {key} field")
        result[key] = field
    return result


def _gmail_imap_effect(values: Mapping[str, object]) -> ConnectorEffect:
    if values.get("expunge_behavior") == "deleteForever":
        return ConnectorEffect.PERMANENT
    if values.get("enabled") is True:
        return ConnectorEffect.OUTWARD
    return ConnectorEffect.SAFE_MUTATION


def _gmail_pop_effect(values: Mapping[str, object]) -> ConnectorEffect:
    if values.get("disposition") in {"archive", "trash"}:
        return ConnectorEffect.DESTRUCTIVE
    if values.get("access_window") != "disabled":
        return ConnectorEffect.OUTWARD
    return ConnectorEffect.SAFE_MUTATION


def _validate_gmail_vacation(values: Mapping[str, object]) -> None:
    start = values.get("start_time")
    end = values.get("end_time")
    maximum_epoch = 2**63 - 1
    for label, value in (("start", start), ("end", end)):
        if value is not None and (
            not isinstance(value, str)
            or not value.isdecimal()
            or not 0 <= int(value) <= maximum_epoch
        ):
            raise ValidationError(f"Gmail vacation {label} time is invalid")
    if isinstance(start, str) and isinstance(end, str) and int(start) >= int(end):
        raise ValidationError("Gmail vacation start time must precede end time")
    if values.get("enable_auto_reply") is True and not any(
        isinstance(values.get(name), str) and bool(values[name])
        for name in (
            "response_body_html",
            "response_body_plain_text",
            "response_subject",
        )
    ):
        raise ValidationError("Gmail vacation replies require a non-empty subject or body")


def _gmail_vacation_body(values: Mapping[str, object]) -> dict[str, object]:
    body = _selected(
        values,
        {
            "enable_auto_reply": "enableAutoReply",
            "end_time": "endTime",
            "response_body_html": "responseBodyHtml",
            "response_body_plain_text": "responseBodyPlainText",
            "response_subject": "responseSubject",
            "restrict_to_contacts": "restrictToContacts",
            "restrict_to_domain": "restrictToDomain",
            "start_time": "startTime",
        },
    )
    # Explicit null means no schedule boundary. Gmail expresses that final state
    # by omitting the optional field from the whole VacationSettings PUT body.
    return {key: value for key, value in body.items() if value is not None}


def _validate_gmail_send_as_patch(values: Mapping[str, object]) -> None:
    if not set(values) - {"send_as_email"}:
        raise ValidationError("Gmail send-as patch requires at least one setting change")


def _gmail_primary_send_as(
    values: Mapping[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> None:
    requested = _required(values, "send_as_email")
    current = _provider_mapping(
        _json_request(
            transport,
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.GET,
            path=f"{_GMAIL_USERS_ME_BASE}/settings/sendAs/{_segment(requested)}",
            credential=credential,
            query=(("fields", "isPrimary,sendAsEmail"),),
        ),
        context="Gmail send-as effect preflight",
    )
    actual = current.get("sendAsEmail")
    if not isinstance(actual, str) or actual.casefold() != requested.casefold():
        raise ValidationError("Gmail send-as preflight returned a different alias")
    if current.get("isPrimary") is not True:
        raise ValidationError(
            "Gmail user OAuth can update only the primary send-as alias; custom aliases require "
            "a separately authorized Workspace administrator connection"
        )


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
        return _calendar_sync_request(
            transport,
            method=ConnectorMethod.GET,
            path=f"{base}/users/me/calendarList",
            credential=credential,
            query=_calendar_list_query(values, continuation),
            continuation=continuation,
            allow_sync_cursor=_calendar_list_sync_compatible(values),
        )
    if name == "events.list":
        return _calendar_sync_request(
            transport,
            method=ConnectorMethod.GET,
            path=f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events",
            credential=credential,
            query=_calendar_event_list_query(values, continuation),
            continuation=continuation,
            allow_sync_cursor=_calendar_event_sync_compatible(values),
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
            allow_sync_cursor=False,
        )
    _reject_continuation(continuation)
    if name == "colors.get":
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/colors",
            credential=credential,
        )
    if name == "calendars.get":
        calendar_id = _required(values, "calendar_id")
        result = _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"{base}/calendars/{_segment(calendar_id)}",
            credential=credential,
            query=(
                (
                    "fields",
                    "conferenceProperties,description,etag,id,kind,location,summary,timeZone",
                ),
            ),
        )
        calendar = _provider_mapping(result, context="Google Calendar metadata read")
        if calendar.get("kind") != "calendar#calendar":
            raise ValidationError("Google Calendar metadata read returned a different resource")
        if (
            calendar_id != "primary"
            and _provider_text(calendar, "id", context="Google Calendar metadata read")
            != calendar_id
        ):
            raise ValidationError("Google Calendar metadata read returned a different calendar")
        _safe_header(_provider_text(calendar, "etag", context="Google Calendar metadata read"))
        return result
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
        _validate_calendar_time_range(values, context="Google Calendar free/busy")
        calendar_ids = _strings(_required_value(values, "calendar_ids"))
        if len(set(calendar_ids)) != len(calendar_ids):
            raise ValidationError("Google Calendar free/busy calendar IDs must be unique")
        freebusy_body: dict[str, object] = {
            "items": [{"id": value} for value in calendar_ids],
            "timeMax": _required(values, "time_max"),
            "timeMin": _required(values, "time_min"),
        }
        if "time_zone" in values:
            freebusy_body["timeZone"] = _required(values, "time_zone")
        return _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.POST,
            path=f"{base}/freeBusy",
            credential=credential,
            json_body=freebusy_body,
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
        body = _calendar_body(values, require_change=name == "calendars.update")
        return _calendar_write_request(
            transport,
            method=method,
            path=path,
            credential=credential,
            json_body=body,
            headers=headers,
            expected_statuses=_JSON_STATUSES,
        )
    if name == "calendars.delete":
        _validate_secondary_calendar_target(values)
        _calendar_delete_preflight(values, credential, transport)
        return _calendar_write_request(
            transport,
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
        event_body = _calendar_event_body(
            values,
            is_create=name == "events.create",
            require_change=name == "events.update",
        )
        query = list(_optional_query(values, {"send_updates": "sendUpdates"}))
        if "meet_request_id" in values:
            query.append(("conferenceDataVersion", "1"))
        if "attachments" in values:
            query.append(("supportsAttachments", "true"))
        request = _calendar_write_request if name == "events.update" else _json_request
        return request(
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
        _validate_calendar_move(values)
        return _calendar_write_request(
            transport,
            method=ConnectorMethod.POST,
            path=(
                f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
                f"{_segment(_required(values, 'event_id'))}/move"
            ),
            credential=credential,
            query=_calendar_move_query(values),
            headers=_etag_headers(values),
            expected_statuses=_JSON_STATUSES,
        )
    if name == "events.respond":
        event_path = (
            f"{base}/calendars/{_segment(_required(values, 'calendar_id'))}/events/"
            f"{_segment(_required(values, 'event_id'))}"
        )
        event = _calendar_event_mutation_preflight(values, credential, transport)
        attendee, etag = _calendar_self_attendee(event, values)
        return _calendar_write_request(
            transport,
            method=ConnectorMethod.PATCH,
            path=event_path,
            credential=credential,
            query=_optional_query(values, {"send_updates": "sendUpdates"}),
            json_body={"attendees": [attendee], "attendeesOmitted": True},
            headers={"If-Match": etag},
            expected_statuses=_JSON_STATUSES,
        )
    if name == "events.delete":
        return _calendar_write_request(
            transport,
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
    if name == "files.download":
        return _drive_download(
            transport,
            credential=credential,
            continuation=continuation,
            values=values,
            transfer=transfer,
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
    if name in {"files.content", "revisions.download"}:
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
    allow_sync_cursor: bool = True,
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
    return _json_result(response.json(), allow_sync_cursor=allow_sync_cursor)


def _calendar_sync_request(
    transport: ConnectorTransport,
    *,
    method: ConnectorMethod,
    path: str,
    credential: ConnectorRuntimeCredential,
    query: Sequence[tuple[str, str]],
    continuation: object | None,
    allow_sync_cursor: bool,
) -> ConnectorAdapterResult:
    try:
        response = transport.request(
            origin=ConnectorOrigin.GOOGLE,
            method=method,
            path=path,
            credential=credential.credential,
            query=query,
            expected_statuses=frozenset({200}),
        )
    except ConnectorProviderError as exc:
        if exc.origin is ConnectorOrigin.GOOGLE and exc.status == 410:
            raise ConnectorProviderError(
                origin=exc.origin,
                status=exc.status,
                code="full_sync_required",
                retry_after=exc.retry_after,
            ) from exc
        raise
    return _json_result(
        response.json(),
        active_sync_token=_calendar_sync_token(continuation),
        allow_sync_cursor=allow_sync_cursor,
    )


def _calendar_write_request(
    transport: ConnectorTransport,
    *,
    method: ConnectorMethod,
    path: str,
    credential: ConnectorRuntimeCredential,
    query: Sequence[tuple[str, str]] = (),
    json_body: object | None = None,
    headers: Mapping[str, str] | None = None,
    expected_statuses: frozenset[int] = frozenset({200}),
    origin: ConnectorOrigin = ConnectorOrigin.GOOGLE,
) -> ConnectorAdapterResult:
    try:
        return _json_request(
            transport,
            origin=origin,
            method=method,
            path=path,
            credential=credential,
            query=query,
            json_body=json_body,
            headers=headers,
            expected_statuses=expected_statuses,
        )
    except ConnectorProviderError as exc:
        if exc.origin is ConnectorOrigin.GOOGLE and exc.status == 412:
            raise ConnectorProviderError(
                origin=exc.origin,
                status=exc.status,
                code="resource_changed_reread_required",
                retry_after=exc.retry_after,
            ) from exc
        raise


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
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/zip": ".zip",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "text/csv": ".csv",
        "text/plain": ".txt",
        "video/mp4": ".mp4",
    }.get(media_type, "")
    if export:
        return f"drive-export-{file_id}{suffix}"
    revision_id = values.get("revision_id")
    if revision_id is not None:
        return f"drive-file-{file_id}-revision-{_text(revision_id)}{suffix}"
    return f"drive-file-{file_id}{suffix}"


def _drive_download(
    transport: ConnectorTransport,
    *,
    credential: ConnectorRuntimeCredential,
    continuation: object | None,
    values: dict[str, object],
    transfer: ConnectorTransferContext | None,
) -> ConnectorAdapterResult:
    if _drive_delivery(values) == "artifact" and transfer is None:
        raise ValidationError("Google Drive artifact delivery requires transfer context")
    operation_name: str | None = None
    resource_key = _drive_input_resource_key(values)
    if continuation is not None:
        operation_name, continued_resource_key = _drive_download_continuation(continuation)
        if (
            resource_key is not None
            and continued_resource_key is not None
            and resource_key != continued_resource_key
        ):
            raise ValidationError("Google Drive download continuation does not match its input")
        resource_key = continued_resource_key or resource_key
        try:
            operation = transport.request(
                origin=ConnectorOrigin.GOOGLE,
                method=ConnectorMethod.GET,
                path=f"/drive/v3/operations/{_segment(operation_name)}",
                credential=credential.credential,
                headers=_drive_resource_key_headers(values, resource_key),
            ).json()
        except ConnectorProviderError as exc:
            if exc.status in {403, 404}:
                raise ContinuityError(
                    "Google Drive download operation is no longer pollable; "
                    "start files.download again"
                ) from exc
            raise
    else:
        try:
            operation = transport.request(
                origin=ConnectorOrigin.GOOGLE,
                method=ConnectorMethod.POST,
                path=f"/drive/v3/files/{_segment(_required(values, 'file_id'))}/download",
                credential=credential.credential,
                query=_optional_query(
                    values,
                    {"mime_type": "mimeType", "revision_id": "revisionId"},
                ),
                body=b"",
                headers=_drive_resource_key_headers(values, resource_key),
                expected_statuses=frozenset({200}),
            ).json()
        except ConnectorOutcomeUnknown as exc:
            raise ContinuityError(
                "Google Drive download operation start failed; retrying is safe"
            ) from exc
    return _drive_download_operation_result(
        operation,
        expected_name=operation_name,
        resource_key=resource_key,
        transport=transport,
        transfer=transfer,
        values=values,
    )


def _drive_download_operation_result(
    operation: object,
    *,
    expected_name: str | None,
    resource_key: str | None,
    transport: ConnectorTransport,
    transfer: ConnectorTransferContext | None,
    values: dict[str, object],
) -> ConnectorAdapterResult:
    if not isinstance(operation, Mapping):
        raise ContinuityError("Google Drive returned an invalid download operation")
    name = _drive_download_operation_name(operation.get("name"))
    if expected_name is not None and name != expected_name:
        raise ContinuityError("Google Drive download operation identity changed")
    resource_key = _drive_download_resource_key(
        operation.get("metadata"),
        current=resource_key,
    )
    done = operation.get("done")
    if done is not None and not isinstance(done, bool):
        raise ContinuityError("Google Drive returned an invalid download operation state")
    has_error = "error" in operation
    has_response = "response" in operation
    if done is not True:
        if has_error or has_response:
            raise ContinuityError("Google Drive returned an invalid pending download operation")
        continuation: dict[str, object] = {"operation_name": name}
        if resource_key is not None:
            continuation["resource_key"] = resource_key
        return ConnectorAdapterResult(
            {"retry_after_seconds": _DRIVE_DOWNLOAD_RETRY_SECONDS, "status": "pending"},
            continuation=continuation,
        )
    if has_error == has_response:
        raise ContinuityError("Google Drive returned an invalid completed download operation")
    if has_error:
        _raise_drive_download_error(operation["error"])
    response = operation["response"]
    if not isinstance(response, Mapping):
        raise ContinuityError("Google Drive returned an invalid download response")
    if response.get("@type") != _DRIVE_DOWNLOAD_RESPONSE_TYPE:
        raise ContinuityError("Google Drive returned an unexpected download response type")
    location = response.get("downloadUri")
    partial_allowed = response.get("partialDownloadAllowed", False)
    if not isinstance(location, str) or not location or len(location) > 16_384:
        raise ContinuityError("Google Drive download response omitted its content location")
    if not isinstance(partial_allowed, bool):
        raise ContinuityError("Google Drive returned an invalid partial-download state")
    return _drive_download_location(
        transport,
        location=location,
        partial_allowed=partial_allowed,
        resource_key=resource_key,
        transfer=transfer,
        values=values,
    )


def _drive_download_location(
    transport: ConnectorTransport,
    *,
    location: str,
    partial_allowed: bool,
    resource_key: str | None,
    transfer: ConnectorTransferContext | None,
    values: dict[str, object],
) -> ConnectorAdapterResult:
    resource_binding = _drive_resource_key_binding(values, resource_key)
    if _drive_delivery(values) == "artifact":
        if transfer is None:
            raise ValidationError("Google Drive artifact delivery requires transfer context")
        writer = transfer.artifacts.start(
            _drive_artifact_filename(values, export=False),
            media_type=_drive_artifact_media_type(values, export=False),
        )
        downloaded = transport.download_stream(
            origin=ConnectorOrigin.GOOGLE,
            sink=writer,
            location=location,
            credential=None,
            max_bytes=MAX_ARTIFACT_BYTES,
            google_drive_resource_key=resource_binding,
        )
        if downloaded.artifact is None:
            raise ValidationError("Google Drive artifact delivery produced no receipt")
        return ConnectorAdapterResult(
            {"bytes": downloaded.bytes_transferred, "delivery": "artifact"},
            artifact=downloaded.artifact,
        )
    if not partial_allowed:
        raise ValidationError(
            "Google Drive download does not support inline chunks; use artifact delivery"
        )
    byte_offset = _integer(values.get("byte_offset", 0))
    maximum = _integer(values.get("max_chunk_size", _MAX_CONTENT_BYTES))
    if maximum < 1:
        raise ValidationError("Google Drive content chunk bound is invalid")
    sink = BytesIO()
    downloaded = transport.download_stream(
        origin=ConnectorOrigin.GOOGLE,
        sink=sink,
        location=location,
        credential=None,
        range_start=byte_offset,
        range_end=byte_offset + maximum - 1,
        expected_statuses=frozenset({206}),
        max_bytes=maximum,
        google_drive_resource_key=resource_binding,
    )
    content_range, next_byte_offset = _drive_stream_content_range(
        downloaded,
        byte_offset=byte_offset,
    )
    return ConnectorAdapterResult(
        {
            "byte_offset": byte_offset,
            "content_base64": base64.b64encode(sink.getvalue()).decode("ascii"),
            "content_range": content_range,
            "next_byte_offset": next_byte_offset,
        }
    )


def _drive_download_continuation(value: object) -> tuple[str, str | None]:
    if not isinstance(value, Mapping) or set(value) not in (
        {"operation_name"},
        {"operation_name", "resource_key"},
    ):
        raise ValidationError("Google Drive download continuation is invalid")
    try:
        name = _drive_download_operation_name(value.get("operation_name"))
    except ContinuityError as exc:
        raise ValidationError("Google Drive download continuation is invalid") from exc
    resource_key = value.get("resource_key")
    if resource_key is not None:
        resource_key = _drive_resource_component(resource_key, label="resource key")
    return name, resource_key


def _drive_download_operation_name(value: object) -> str:
    if not isinstance(value, str):
        raise ContinuityError("Google Drive download operation omitted its identity")
    match = _DRIVE_DOWNLOAD_OPERATION_NAME.fullmatch(value)
    if match is None:
        raise ContinuityError("Google Drive download operation identity is invalid")
    return match.group(1)


def _drive_input_resource_key(values: Mapping[str, object]) -> str | None:
    value = values.get("resource_key")
    if value is None:
        return None
    return _drive_resource_component(value, label="resource key")


def _drive_download_resource_key(
    metadata: object,
    *,
    current: str | None,
) -> str | None:
    if metadata is None:
        return current
    if not isinstance(metadata, Mapping):
        raise ContinuityError("Google Drive download operation metadata is invalid")
    returned = metadata.get("resourceKey")
    if returned is None:
        return current
    try:
        returned_key = _drive_resource_component(returned, label="provider resource key")
    except ValidationError as exc:
        raise ContinuityError("Google Drive download resource key is invalid") from exc
    if current is not None and returned_key != current:
        raise ContinuityError("Google Drive download resource key changed")
    return returned_key


def _drive_resource_component(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DRIVE_RESOURCE_COMPONENT.fullmatch(value) is None:
        raise ValidationError(f"Google Drive {label} is invalid")
    return value


def _drive_resource_key_binding(
    values: Mapping[str, object],
    resource_key: str | None,
) -> tuple[str, str] | None:
    if resource_key is None:
        return None
    return (
        _drive_resource_component(_required(values, "file_id"), label="file ID"),
        _drive_resource_component(resource_key, label="resource key"),
    )


def _drive_resource_key_headers(
    values: Mapping[str, object],
    resource_key: str | None,
) -> dict[str, str] | None:
    binding = _drive_resource_key_binding(values, resource_key)
    if binding is None:
        return None
    return {"X-Goog-Drive-Resource-Keys": f"{binding[0]}/{binding[1]}"}


def _raise_drive_download_error(value: object) -> NoReturn:
    if not isinstance(value, Mapping):
        raise ContinuityError("Google Drive returned an invalid download error")
    code = value.get("code")
    if type(code) is not int or code not in _DRIVE_DOWNLOAD_ERROR_STATUS:
        raise ContinuityError("Google Drive returned an invalid download error code")
    status, label = _DRIVE_DOWNLOAD_ERROR_STATUS[code]
    raise ConnectorProviderError(
        origin=ConnectorOrigin.GOOGLE,
        status=status,
        code=f"download_{label}",
    )


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


def _drive_stream_content_range(
    response: ConnectorStreamResponse,
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
    start, end, _total = (int(part) for part in match.groups())
    if start != byte_offset or end < start:
        raise ValidationError("Google Drive ranged download does not match its requested offset")
    return content_range, end + 1


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
    inline_content = None if content_base64 is None else _decode_base64(content_base64)
    total_length = upload.size if upload is not None else len(cast(bytes, inline_content))
    media_type = (
        _mime_type(upload.media_type or mime_type) if upload is not None else _mime_type(mime_type)
    )

    def initiate() -> str:
        initiated = transport.request(
            origin=ConnectorOrigin.GOOGLE,
            method=method,
            path=path,
            credential=credential.credential,
            query=query,
            json_body=metadata,
            headers={
                "X-Upload-Content-Length": str(total_length),
                "X-Upload-Content-Type": media_type,
            },
            expected_statuses=_DRIVE_RESUMABLE_INIT_STATUSES,
        )
        return _response_location(initiated.headers)

    location = initiate()
    offset = 0
    attempts = 0
    no_progress = 0
    restarts = 0
    unresolved_final_dispatch = False
    while True:
        attempts = _drive_resumable_attempt(attempts)
        try:
            if upload is not None:
                response = transport.request_stream(
                    origin=ConnectorOrigin.GOOGLE,
                    method=ConnectorMethod.PUT,
                    source=upload,
                    content_length=total_length - offset,
                    location=location,
                    credential=None,
                    content_type=media_type,
                    byte_offset=offset,
                    total_length=None if total_length == 0 else total_length,
                    expected_statuses=_DRIVE_RESUMABLE_STREAM_STATUSES,
                )
            else:
                assert inline_content is not None
                response = transport.request_stream(
                    origin=ConnectorOrigin.GOOGLE,
                    method=ConnectorMethod.PUT,
                    source=(inline_content,),
                    content_length=total_length,
                    location=location,
                    credential=None,
                    content_type=media_type,
                    total_length=None if total_length == 0 else total_length,
                    expected_statuses=_DRIVE_RESUMABLE_STREAM_STATUSES,
                )
            unresolved_final_dispatch = False
        except ConnectorProviderError as exc:
            if exc.status == 404:
                location, restarts = _drive_resumable_restart(
                    initiate,
                    restarts=restarts,
                    unresolved_final_dispatch=unresolved_final_dispatch,
                )
                offset = 0
                no_progress = 0
                continue
            if 200 <= exc.status < 300:
                raise ConnectorOutcomeUnknown(
                    "Google Drive upload returned an unsupported success status; "
                    "upload outcome is unknown"
                ) from exc
            if exc.status < 500:
                raise
            unresolved_final_dispatch = True
            attempts = _drive_resumable_attempt(attempts)
            response = _drive_resumable_probe(transport, location, total_length)
        except ConnectorOutcomeUnknown:
            unresolved_final_dispatch = True
            attempts = _drive_resumable_attempt(attempts)
            response = _drive_resumable_probe(transport, location, total_length)

        while True:
            if response.status in _DRIVE_RESUMABLE_FINAL_STATUSES:
                try:
                    payload = response.json()
                except ConnectorProviderError as exc:
                    raise ConnectorOutcomeUnknown(
                        "Google Drive upload completed but its result could not be decoded; "
                        "the upload was not retried"
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise ConnectorOutcomeUnknown(
                        "Google Drive upload completed but its result could not be decoded; "
                        "the upload was not retried"
                    )
                return _json_result(payload, strip_upload_location=True)
            if response.status == 404:
                raise ConnectorOutcomeUnknown(
                    "Google Drive upload session disappeared after an ambiguous final dispatch; "
                    "the upload was not restarted"
                )
            if response.status != 308 or response.next_offset is None:
                raise ConnectorOutcomeUnknown(
                    "Google Drive resumable upload returned invalid recovery state; "
                    "upload outcome is unknown"
                )
            next_offset = response.next_offset
            if not offset <= next_offset <= total_length:
                raise ConnectorOutcomeUnknown(
                    "Google Drive resumable upload acknowledgement regressed; "
                    "upload outcome is unknown"
                )
            if upload is None and 0 < next_offset < total_length:
                raise ConnectorOutcomeUnknown(
                    "Google Drive inline upload was not finalized; retry with a fresh confirmation"
                )
            if next_offset == offset:
                no_progress += 1
                if no_progress >= _DRIVE_RESUMABLE_MAX_NO_PROGRESS:
                    raise ConnectorOutcomeUnknown(
                        "Google Drive resumable upload made no progress within its retry bound"
                    )
            else:
                offset = next_offset
                no_progress = 0
            if offset < total_length or total_length == 0:
                unresolved_final_dispatch = False
                break
            unresolved_final_dispatch = True
            attempts = _drive_resumable_attempt(attempts)
            response = _drive_resumable_probe(transport, location, total_length)


def _drive_resumable_attempt(attempts: int) -> int:
    if attempts >= _DRIVE_RESUMABLE_MAX_ATTEMPTS:
        raise ConnectorOutcomeUnknown(
            "Google Drive resumable upload exceeded its bounded recovery attempts"
        )
    return attempts + 1


def _drive_resumable_probe(
    transport: ConnectorTransport,
    location: str,
    total_length: int,
) -> ConnectorStreamResponse:
    try:
        return transport.probe_resumable_upload(
            origin=ConnectorOrigin.GOOGLE,
            location=location,
            total_length=total_length,
        )
    except ContinuityError as exc:
        raise ConnectorOutcomeUnknown(
            "Google Drive upload status could not be resolved; upload outcome remains unknown"
        ) from exc


def _drive_resumable_restart(
    initiate: Callable[[], str],
    *,
    restarts: int,
    unresolved_final_dispatch: bool,
) -> tuple[str, int]:
    if unresolved_final_dispatch:
        raise ConnectorOutcomeUnknown(
            "Google Drive upload session disappeared after an ambiguous final dispatch; "
            "the upload was not restarted"
        )
    if restarts >= _DRIVE_RESUMABLE_MAX_RESTARTS:
        raise ConnectorOutcomeUnknown("Google Drive upload session expired more than once")
    return initiate(), restarts + 1


def _json_result(
    payload: object,
    *,
    strip_upload_location: bool = False,
    active_sync_token: str | None = None,
    allow_sync_cursor: bool = True,
) -> ConnectorAdapterResult:
    if not isinstance(payload, Mapping):
        return ConnectorAdapterResult(payload)
    continuation: object | None = None
    if isinstance(payload.get("nextPageToken"), str):
        continuation = (
            {
                "pageToken": payload["nextPageToken"],
                "syncToken": active_sync_token,
            }
            if active_sync_token is not None
            else payload["nextPageToken"]
        )
    elif allow_sync_cursor and isinstance(payload.get("nextSyncToken"), str):
        continuation = {"syncToken": payload["nextSyncToken"]}
    return ConnectorAdapterResult(
        _strip_provider_state(payload, strip_upload_location=strip_upload_location),
        continuation=continuation,
    )


def _gmail_message_write_result(payload: object) -> ConnectorAdapterResult:
    """Return only provider identifiers needed to reconcile a completed Gmail write."""

    if not isinstance(payload, Mapping):
        return ConnectorAdapterResult({})
    receipt: dict[str, object] = {}
    for name in _GMAIL_WRITE_RECEIPT_FIELDS:
        value = payload.get(name)
        if name == "sizeEstimate":
            if type(value) is int and 0 <= value <= 2**63 - 1:
                receipt[name] = value
            continue
        if name == "labelIds":
            if (
                isinstance(value, list)
                and len(value) <= 1_000
                and all(isinstance(item, str) and 0 < len(item) <= 1_024 for item in value)
            ):
                receipt[name] = value
            continue
        if isinstance(value, str) and 0 < len(value) <= 2_048:
            receipt[name] = value
    return ConnectorAdapterResult(receipt)


def _gmail_message_write_receipt(
    response: ConnectorResponse | ConnectorStreamResponse,
    *,
    action: str,
) -> ConnectorAdapterResult:
    """Parse one irreversible Gmail write receipt without inviting a duplicate retry."""

    unusable_receipt = (
        "Gmail accepted the message write but returned no usable receipt; it may already "
        f"have been {action}, so do not retry automatically and reconcile provider state "
        "before any manual retry"
    )
    try:
        payload = response.json()
    except ConnectorProviderError as exc:
        raise ConnectorOutcomeUnknown(unusable_receipt) from exc
    if not isinstance(payload, Mapping):
        raise ConnectorOutcomeUnknown(unusable_receipt)
    receipt = _gmail_message_write_result(payload)
    if not isinstance(receipt.payload, Mapping) or "id" not in receipt.payload:
        raise ConnectorOutcomeUnknown(unusable_receipt)
    return receipt


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


def _gmail_draft_list_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    query = list(_optional_query(values, {"include_spam_trash": "includeSpamTrash", "query": "q"}))
    if "page_size" in values:
        query.append(("maxResults", str(_integer(values["page_size"]))))
    query.extend(_continuation_query(continuation))
    return tuple(query)


def _gmail_get_query(values: dict[str, object]) -> tuple[tuple[str, str], ...]:
    query = list(_optional_query(values, {"format": "format"}))
    if "metadata_header_names" in values:
        query.extend(
            ("metadataHeaders", _safe_header(name))
            for name in _strings(values["metadata_header_names"])
        )
    return tuple(query)


def _gmail_history_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    start_history_id = _required(values, "start_history_id")
    if int(start_history_id) > _GMAIL_MAX_HISTORY_ID:
        raise ValidationError("Gmail history ID exceeds the provider uint64 bound")
    query: list[tuple[str, str]] = [("startHistoryId", start_history_id)]
    if "label_id" in values:
        query.append(("labelId", _required(values, "label_id")))
    if "page_size" in values:
        query.append(("maxResults", str(_integer(values["page_size"]))))
    if "history_types" in values:
        history_types = _strings(values["history_types"])
        if len(set(history_types)) != len(history_types):
            raise ValidationError("Gmail history types must be unique")
        query.extend(("historyTypes", history_type) for history_type in history_types)
    query.extend(_continuation_query(continuation))
    return tuple(query)


def _calendar_list_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    continuation_query = _calendar_continuation_query(continuation)
    if _calendar_sync_token(continuation) is not None:
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
    _validate_calendar_time_range(values, context="Google Calendar event list")
    if "updated_min" in values:
        _calendar_rfc3339(values["updated_min"], context="Google Calendar updated-min")
    continuation_query = _calendar_continuation_query(continuation)
    if _calendar_sync_token(continuation) is not None:
        if any(field in values for field in _CALENDAR_EVENT_SYNC_FILTER_FIELDS):
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
                "show_hidden_invitations": "showHiddenInvitations",
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
    query.extend(_calendar_extended_property_filter_query(values))
    query.extend(continuation_query)
    return tuple(query)


def _calendar_extended_property_filter_query(
    values: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    rendered: list[tuple[str, str]] = []
    for source, target in (
        ("private_extended_properties", "privateExtendedProperty"),
        ("shared_extended_properties", "sharedExtendedProperty"),
    ):
        if source not in values:
            continue
        items = _calendar_extended_property_items(
            values[source],
            context="Google Calendar extended-property filter",
            allow_deletion=False,
            allow_repeated_keys=True,
        )
        if any("=" in key for key, _value in items):
            raise ValidationError(
                "Google Calendar extended-property filter keys cannot contain equals signs"
            )
        rendered.extend((target, f"{key}={value}") for key, value in items)
    return tuple(rendered)


def _calendar_instance_query(
    values: dict[str, object], continuation: object | None
) -> tuple[tuple[str, str], ...]:
    continuation_query = _continuation_query(continuation)
    if continuation_query and continuation_query[0][0] == "syncToken":
        raise ValidationError("Google Calendar event instances do not support sync tokens")
    _validate_calendar_time_range(values, context="Google Calendar event instances")
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


def _calendar_continuation_query(
    continuation: object | None,
) -> tuple[tuple[str, str], ...]:
    if (
        isinstance(continuation, Mapping)
        and set(continuation) == {"pageToken", "syncToken"}
        and isinstance(continuation["pageToken"], str)
        and continuation["pageToken"]
        and isinstance(continuation["syncToken"], str)
        and continuation["syncToken"]
    ):
        return (
            ("syncToken", continuation["syncToken"]),
            ("pageToken", continuation["pageToken"]),
        )
    return _continuation_query(continuation)


def _calendar_sync_token(continuation: object | None) -> str | None:
    if isinstance(continuation, Mapping):
        value = continuation.get("syncToken")
        if isinstance(value, str) and value:
            return value
    return None


def _calendar_list_sync_compatible(values: Mapping[str, object]) -> bool:
    if any(field in values for field in ("min_access_role", "show_own_organization_only")):
        return False
    return not any(values.get(field) is False for field in ("show_deleted", "show_hidden"))


def _calendar_event_sync_compatible(values: Mapping[str, object]) -> bool:
    return (
        not any(field in values for field in _CALENDAR_EVENT_SYNC_FILTER_FIELDS)
        and values.get("show_deleted") is not False
    )


def _reject_continuation(continuation: object | None) -> None:
    if continuation is not None:
        raise ValidationError("connector operation does not accept a continuation")


def _calendar_body(values: Mapping[str, object], *, require_change: bool) -> dict[str, object]:
    body = _selected(
        values,
        {
            "description": "description",
            "location": "location",
            "summary": "summary",
            "time_zone": "timeZone",
        },
    )
    if require_change and not body:
        raise ValidationError("Google Calendar update requires at least one changed field")
    return body


def _calendar_event_body(
    values: Mapping[str, object], *, is_create: bool, require_change: bool
) -> dict[str, object]:
    body = _selected(
        values,
        {
            "color_id": "colorId",
            "description": "description",
            "guests_can_invite_others": "guestsCanInviteOthers",
            "guests_can_modify": "guestsCanModify",
            "guests_can_see_other_guests": "guestsCanSeeOtherGuests",
            "location": "location",
            "recurrence": "recurrence",
            "summary": "summary",
            "transparency": "transparency",
            "visibility": "visibility",
        },
    )
    body.update(_calendar_status_event_body(values, is_create=is_create))
    if is_create and "event_id" in values:
        body["id"] = _text(values["event_id"])
    if "attendee_emails" in values and "attendees" in values:
        raise ValidationError("Google Calendar attendees are specified twice")
    if "attendee_emails" in values:
        emails = _strings(values["attendee_emails"])
        _validate_unique_calendar_attendees(emails)
        body["attendees"] = [{"email": value} for value in emails]
    if "attendees" in values:
        attendees = _mappings(values["attendees"])
        _validate_unique_calendar_attendees(
            [_required(attendee, "email") for attendee in attendees]
        )
        body["attendees"] = [
            _selected(
                attendee,
                {
                    "additional_guests": "additionalGuests",
                    "display_name": "displayName",
                    "email": "email",
                    "optional": "optional",
                },
            )
            for attendee in attendees
        ]
    if "attachments" in values:
        body["attachments"] = _calendar_event_attachments(values["attachments"])
    if "meet_request_id" in values:
        body["conferenceData"] = {
            "createRequest": {
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
                "requestId": _required(values, "meet_request_id"),
            }
        }
    extended_properties = _calendar_event_extended_properties(
        values,
        allow_deletion=not is_create,
    )
    if extended_properties:
        body["extendedProperties"] = extended_properties
    if "attendees" in body and "send_updates" not in values:
        raise ValidationError(
            "Google Calendar attendee changes require an explicit send-updates choice"
        )
    if "recurrence" in values:
        recurrence = _strings(values["recurrence"])
        if len(set(recurrence)) != len(recurrence):
            raise ValidationError("Google Calendar recurrence lines must be unique")
        if any(_CALENDAR_RECURRENCE_LINE.fullmatch(line) is None for line in recurrence):
            raise ValidationError(
                "Google Calendar recurrence must use RRULE, EXRULE, RDATE, or EXDATE lines"
            )
    if "reminders" in values:
        body["reminders"] = _calendar_reminders(values["reminders"])
    for source, target in (("start", "start"), ("end", "end")):
        if source in values:
            body[target] = _calendar_time(values[source])
    _validate_calendar_event_times(values)
    if is_create:
        _validate_calendar_status_event_create(values)
    if require_change and not body:
        raise ValidationError("Google Calendar event update requires at least one changed field")
    return body


def _calendar_status_event_body(
    values: Mapping[str, object], *, is_create: bool
) -> dict[str, object]:
    event_type = values.get("event_type")
    present = [field for field in _CALENDAR_STATUS_PROPERTY_FIELDS if field in values]
    if len(present) > 1:
        raise ValidationError("Google Calendar status properties must describe one event type")
    if not is_create and event_type is not None:
        raise ValidationError("Google Calendar event type cannot be changed")
    if is_create and event_type is None:
        if present:
            raise ValidationError("Google Calendar status properties require a matching event type")
        return {}
    if not is_create:
        if present and not _mapping(values[present[0]]):
            raise ValidationError(
                "Google Calendar status-properties update requires at least one changed field"
            )
        return _calendar_status_property_body(values)
    if not isinstance(event_type, str) or event_type not in {
        "focusTime",
        "outOfOffice",
        "workingLocation",
    }:
        raise ValidationError("Google Calendar event type is not creatable")
    expected = {
        "focusTime": "focus_time_properties",
        "outOfOffice": "out_of_office_properties",
        "workingLocation": "working_location_properties",
    }[event_type]
    if present != [expected]:
        raise ValidationError("Google Calendar event type requires its matching properties")
    body = {"eventType": event_type, **_calendar_status_property_body(values)}
    if event_type == "focusTime":
        focus_properties = cast(dict[str, object], body["focusTimeProperties"])
        focus_properties.setdefault("autoDeclineMode", "declineNone")
        focus_properties.setdefault("chatStatus", "available")
        if values.get("transparency", "opaque") != "opaque":
            raise ValidationError(f"Google Calendar {event_type} events must be opaque")
        body["transparency"] = "opaque"
    elif event_type == "outOfOffice":
        out_of_office_properties = cast(dict[str, object], body["outOfOfficeProperties"])
        out_of_office_properties.setdefault("autoDeclineMode", "declineNone")
        if values.get("transparency", "opaque") != "opaque":
            raise ValidationError(f"Google Calendar {event_type} events must be opaque")
        body["transparency"] = "opaque"
    else:
        if values.get("transparency", "transparent") != "transparent":
            raise ValidationError("Google Calendar working-location events must be transparent")
        if values.get("visibility", "public") != "public":
            raise ValidationError("Google Calendar working-location events must be public")
        body["transparency"] = "transparent"
        body["visibility"] = "public"
    return body


def _calendar_status_property_body(values: Mapping[str, object]) -> dict[str, object]:
    if "focus_time_properties" in values:
        properties = _mapping(values["focus_time_properties"])
        return {
            "focusTimeProperties": _selected(
                properties,
                {
                    "auto_decline_mode": "autoDeclineMode",
                    "chat_status": "chatStatus",
                    "decline_message": "declineMessage",
                },
            )
        }
    if "out_of_office_properties" in values:
        properties = _mapping(values["out_of_office_properties"])
        return {
            "outOfOfficeProperties": _selected(
                properties,
                {
                    "auto_decline_mode": "autoDeclineMode",
                    "decline_message": "declineMessage",
                },
            )
        }
    if "working_location_properties" in values:
        return {
            "workingLocationProperties": _calendar_working_location_properties(
                values["working_location_properties"]
            )
        }
    return {}


def _calendar_working_location_properties(value: object) -> dict[str, object]:
    properties = _mapping(value)
    location_type = _required(properties, "type")
    result: dict[str, object] = {"type": location_type}
    if location_type == "customLocation":
        if "office_location" in properties:
            raise ValidationError("Google Calendar working location fields do not match its type")
        if "custom_location" in properties:
            result["customLocation"] = _selected(
                _mapping(properties["custom_location"]),
                {"label": "label"},
            )
        return result
    if location_type == "officeLocation":
        if "custom_location" in properties:
            raise ValidationError("Google Calendar working location fields do not match its type")
        if "office_location" in properties:
            result["officeLocation"] = _selected(
                _mapping(properties["office_location"]),
                {
                    "building_id": "buildingId",
                    "desk_id": "deskId",
                    "floor_id": "floorId",
                    "floor_section_id": "floorSectionId",
                    "label": "label",
                },
            )
        return result
    if location_type == "homeOffice":
        if "custom_location" in properties or "office_location" in properties:
            raise ValidationError("Google Calendar working location fields do not match its type")
        return result
    raise ValidationError("Google Calendar working location type is invalid")


def _validate_calendar_status_event_create(values: Mapping[str, object]) -> None:
    event_type = values.get("event_type")
    if event_type in {"focusTime", "outOfOffice"}:
        if (
            _calendar_time_kind(values["start"]) != "date_time"
            or _calendar_time_kind(values["end"]) != "date_time"
        ):
            raise ValidationError(f"Google Calendar {event_type} events must be timed")
        if event_type == "focusTime":
            properties = _mapping(values["focus_time_properties"])
            if properties.get("chat_status") == "doNotDisturb":
                _validate_calendar_focus_chat_duration(values["start"], values["end"])
    elif event_type == "workingLocation":
        _validate_calendar_working_location_times(values["start"], values["end"])


def _calendar_event_attachments(value: object) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    seen: set[str] = set()
    for attachment in _mappings(value):
        file_url = _required(attachment, "file_url")
        _validate_calendar_attachment_url(file_url)
        if file_url in seen:
            raise ValidationError("Google Calendar attachment URLs must be unique")
        seen.add(file_url)
        attachments.append({"fileUrl": file_url})
    return attachments


def _validate_calendar_attachment_url(value: str) -> None:
    if any(character.isspace() for character in value):
        raise ValidationError("Google Calendar attachment URL must use absolute HTTP or HTTPS")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValidationError(
            "Google Calendar attachment URL must use absolute HTTP or HTTPS"
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValidationError("Google Calendar attachment URL must use absolute HTTP or HTTPS")


def _calendar_event_extended_properties(
    values: Mapping[str, object],
    *,
    allow_deletion: bool,
) -> dict[str, dict[str, str | None]]:
    result: dict[str, dict[str, str | None]] = {}
    all_items: list[tuple[str, str | None]] = []
    for source, target in (
        ("private_extended_properties", "private"),
        ("shared_extended_properties", "shared"),
    ):
        if source not in values:
            continue
        items = _calendar_extended_property_items(
            values[source],
            context="Google Calendar extended properties",
            allow_deletion=allow_deletion,
            allow_repeated_keys=False,
        )
        all_items.extend(items)
        result[target] = {key: value for key, value in items}
    _validate_calendar_extended_property_collection(all_items)
    return result


def _calendar_extended_property_items(
    value: object,
    *,
    context: str,
    allow_deletion: bool,
    allow_repeated_keys: bool,
) -> list[tuple[str, str | None]]:
    items: list[tuple[str, str | None]] = []
    seen: set[object] = set()
    for item in _mappings(value):
        key = _required(item, "key")
        raw_value = _required_value(item, "value")
        if raw_value is None:
            if not allow_deletion:
                raise ValidationError(f"{context} cannot delete a property")
            rendered_value = None
        else:
            rendered_value = _text(raw_value)
        identity: object = (key, rendered_value) if allow_repeated_keys else key
        if identity in seen:
            raise ValidationError(f"{context} must not contain duplicate properties")
        seen.add(identity)
        items.append((key, rendered_value))
    return items


def _validate_calendar_extended_property_collection(
    items: Sequence[tuple[str, str | None]],
) -> None:
    if len(items) > _CALENDAR_MAX_EXTENDED_PROPERTIES:
        raise ValidationError("Google Calendar supports at most 300 extended properties")
    encoded_bytes = sum(
        len(key.encode("utf-8")) + (len(value.encode("utf-8")) if isinstance(value, str) else 0)
        for key, value in items
    )
    if encoded_bytes > _CALENDAR_MAX_EXTENDED_PROPERTY_BYTES:
        raise ValidationError("Google Calendar extended properties exceed their 32 KiB bound")


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
        date_time = _text(value["date_time"])
        _calendar_rfc3339(
            date_time,
            context="Google Calendar event date-time",
            require_offset="time_zone" not in value,
        )
        result = {"dateTime": date_time}
    else:
        calendar_date = _text(value["date"])
        _calendar_date(calendar_date, context="Google Calendar event date")
        result = {"date": calendar_date}
    if "time_zone" in value:
        result["timeZone"] = _text(value["time_zone"])
    return result


def _validate_unique_calendar_attendees(emails: Sequence[str]) -> None:
    normalized = [email.casefold() for email in emails]
    if len(set(normalized)) != len(normalized):
        raise ValidationError("Google Calendar attendee emails must be unique")


def _calendar_date(value: object, *, context: str) -> date:
    rendered = _text(value)
    try:
        parsed = date.fromisoformat(rendered)
    except ValueError as exc:
        raise ValidationError(f"{context} must be an ISO 8601 date") from exc
    if parsed.isoformat() != rendered:
        raise ValidationError(f"{context} must be an ISO 8601 date")
    return parsed


def _calendar_rfc3339(value: object, *, context: str, require_offset: bool = True) -> datetime:
    rendered = _text(value)
    if _CALENDAR_RFC3339.fullmatch(rendered) is None:
        raise ValidationError(f"{context} must be an RFC3339 date-time")
    normalized = f"{rendered[:-1]}+00:00" if rendered.endswith(("Z", "z")) else rendered
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"{context} must be an RFC3339 date-time") from exc
    if require_offset and parsed.utcoffset() is None:
        raise ValidationError(f"{context} must include an offset or an explicit time zone")
    return parsed


def _calendar_time_kind(value: object) -> str:
    if not isinstance(value, Mapping):
        raise ValidationError("Google Calendar event time is invalid")
    return "date_time" if "date_time" in value or "dateTime" in value else "date"


def _validate_calendar_working_location_times(start: object, end: object) -> None:
    start_kind = _calendar_time_kind(start)
    end_kind = _calendar_time_kind(end)
    if start_kind != end_kind:
        raise ValidationError(
            "Google Calendar working-location start and end must use the same time kind"
        )
    if start_kind != "date":
        _validate_calendar_timed_order(start, end)
        return
    assert isinstance(start, Mapping) and isinstance(end, Mapping)
    start_date = _calendar_date(start.get("date"), context="Google Calendar working-location start")
    end_date = _calendar_date(end.get("date"), context="Google Calendar working-location end")
    if end_date - start_date != timedelta(days=1):
        raise ValidationError(
            "Google Calendar all-day working-location events must span exactly one day"
        )


def _validate_calendar_focus_chat_duration(start: object, end: object) -> None:
    duration = _calendar_timed_duration(start, end)
    if duration is not None and duration >= timedelta(hours=24):
        raise ValidationError(
            "Google Calendar focus-time Chat do-not-disturb requires a block under 24 hours"
        )


def _validate_calendar_timed_order(start: object, end: object) -> None:
    duration = _calendar_timed_duration(start, end)
    if duration is not None and duration <= timedelta(0):
        raise ValidationError("Google Calendar event end must follow its start")


def _calendar_timed_duration(start: object, end: object) -> timedelta | None:
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise ValidationError("Google Calendar event time is invalid")
    start_text = start.get("date_time", start.get("dateTime"))
    end_text = end.get("date_time", end.get("dateTime"))
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        raise ValidationError("Google Calendar event requires timed bounds")
    start_zone = start.get("time_zone", start.get("timeZone"))
    end_zone = end.get("time_zone", end.get("timeZone"))
    start_value = _calendar_rfc3339(
        start_text,
        context="Google Calendar event start",
        require_offset=start_zone is None,
    )
    end_value = _calendar_rfc3339(
        end_text,
        context="Google Calendar event end",
        require_offset=end_zone is None,
    )
    if start_value.utcoffset() is not None and end_value.utcoffset() is not None:
        return end_value - start_value
    return None


def _validate_calendar_event_times(values: Mapping[str, object]) -> None:
    start = values.get("start")
    end = values.get("end")
    if start is not None:
        _calendar_time(start)
    if end is not None:
        _calendar_time(end)
    if start is None or end is None:
        return
    start_kind = _calendar_time_kind(start)
    end_kind = _calendar_time_kind(end)
    if start_kind != end_kind:
        raise ValidationError("Google Calendar event start and end must use the same time kind")
    assert isinstance(start, Mapping) and isinstance(end, Mapping)
    if values.get("recurrence"):
        _validate_calendar_recurrence_time_zones(start, end)
    if start_kind == "date":
        start_value = _calendar_date(start["date"], context="Google Calendar event start")
        end_value = _calendar_date(end["date"], context="Google Calendar event end")
        if end_value <= start_value:
            raise ValidationError(
                "Google Calendar all-day end date is exclusive and must follow the start date"
            )
        return
    start_value = _calendar_rfc3339(
        start["date_time"],
        context="Google Calendar event start",
        require_offset="time_zone" not in start,
    )
    end_value = _calendar_rfc3339(
        end["date_time"],
        context="Google Calendar event end",
        require_offset="time_zone" not in end,
    )
    comparable = (start_value.utcoffset() is not None and end_value.utcoffset() is not None) or (
        start_value.utcoffset() is None
        and end_value.utcoffset() is None
        and start.get("time_zone") == end.get("time_zone")
    )
    if comparable and end_value <= start_value:
        raise ValidationError("Google Calendar event end must follow its start")


def _validate_calendar_time_range(values: Mapping[str, object], *, context: str) -> None:
    start = values.get("time_min")
    end = values.get("time_max")
    start_value = (
        _calendar_rfc3339(start, context=f"{context} time-min") if start is not None else None
    )
    end_value = _calendar_rfc3339(end, context=f"{context} time-max") if end is not None else None
    if start_value is not None and end_value is not None and end_value <= start_value:
        raise ValidationError(f"{context} time-max must follow time-min")


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
    if "attendee_emails" in values or "attendees" in values:
        return True
    calendar_id = values.get("calendar_id")
    return isinstance(calendar_id, str) and calendar_id != "primary"


def _calendar_status_create_effect(values: Mapping[str, object]) -> ConnectorEffect | None:
    event_type = values.get("event_type")
    if event_type == "workingLocation":
        return ConnectorEffect.OUTWARD
    if event_type in {"focusTime", "outOfOffice"} and values.get("visibility") == "public":
        return ConnectorEffect.OUTWARD
    if event_type == "focusTime":
        properties = _mapping(values.get("focus_time_properties"))
        decline_mode = properties.get("auto_decline_mode", "declineNone")
        if (
            decline_mode not in {None, "declineNone"}
            or "chat_status" in properties
            or ("decline_message" in properties and decline_mode != "declineNone")
        ):
            return ConnectorEffect.OUTWARD
    if event_type == "outOfOffice":
        properties = _mapping(values.get("out_of_office_properties"))
        decline_mode = properties.get("auto_decline_mode", "declineNone")
        if decline_mode not in {None, "declineNone"} or (
            "decline_message" in properties and decline_mode != "declineNone"
        ):
            return ConnectorEffect.OUTWARD
    return None


def _calendar_event_effect(
    name: str,
    values: Mapping[str, object],
    event: Mapping[str, object] | None,
    *,
    catalog_effect: ConnectorEffect,
) -> ConnectorEffect:
    if name in {"events.move", "events.respond"}:
        return ConnectorEffect.OUTWARD
    if name == "events.delete":
        return ConnectorEffect.DESTRUCTIVE
    if name != "events.update":
        return catalog_effect
    if "recurrence" in values:
        return ConnectorEffect.DESTRUCTIVE
    if _calendar_extended_property_deletion(values):
        return ConnectorEffect.DESTRUCTIVE
    if "attachments" in values:
        if event is None:
            return ConnectorEffect.DESTRUCTIVE
        current_attachments, attachments_complete = _calendar_event_attachment_urls(event)
        requested_attachments = {
            _required(attachment, "file_url") for attachment in _mappings(values["attachments"])
        }
        if not attachments_complete or not current_attachments.issubset(requested_attachments):
            return ConnectorEffect.DESTRUCTIVE
    if "meet_request_id" in values and (event is None or _calendar_event_has_conference(event)):
        return ConnectorEffect.DESTRUCTIVE
    if "attendee_emails" in values or "attendees" in values:
        if event is None:
            return ConnectorEffect.OUTWARD
        current, current_complete = _calendar_event_attendee_emails(event)
        requested = _calendar_requested_attendee_emails(values)
        if not current_complete or not current.issubset(requested):
            return ConnectorEffect.DESTRUCTIVE
        current_guests, current_guests_complete = _calendar_event_additional_guests(event)
        requested_guests = _calendar_requested_additional_guests(values)
        if not current_guests_complete or any(
            requested_guests.get(email, 0) < count for email, count in current_guests.items()
        ):
            return ConnectorEffect.DESTRUCTIVE
        return ConnectorEffect.OUTWARD
    status_effect = _calendar_status_update_effect(values, event)
    if status_effect is not None:
        return status_effect
    local_fields = {"calendar_id", "etag", "event_id", "reminders", "send_updates"}
    if set(values).issubset(local_fields):
        return ConnectorEffect.SAFE_MUTATION
    if event is None or _calendar_event_is_shared(event) or values.get("calendar_id") != "primary":
        return ConnectorEffect.OUTWARD
    return catalog_effect


def _calendar_status_update_effect(
    values: Mapping[str, object], event: Mapping[str, object] | None
) -> ConnectorEffect | None:
    event_type = event.get("eventType") if isinstance(event, Mapping) else None
    if event_type is None:
        if "working_location_properties" in values:
            return ConnectorEffect.OUTWARD
        if "focus_time_properties" in values:
            properties = _mapping(values["focus_time_properties"])
            decline_mode = properties.get("auto_decline_mode")
            if (
                decline_mode not in {None, "declineNone"}
                or "chat_status" in properties
                or ("decline_message" in properties and decline_mode != "declineNone")
            ):
                return ConnectorEffect.OUTWARD
        if "out_of_office_properties" in values:
            properties = _mapping(values["out_of_office_properties"])
            decline_mode = properties.get("auto_decline_mode")
            if decline_mode not in {None, "declineNone"} or (
                "decline_message" in properties and decline_mode != "declineNone"
            ):
                return ConnectorEffect.OUTWARD
        return None
    assert event is not None
    if event_type == "workingLocation":
        locally_safe = {
            "calendar_id",
            "color_id",
            "etag",
            "event_id",
            "private_extended_properties",
            "reminders",
        }
        if not set(values).issubset(locally_safe):
            return ConnectorEffect.OUTWARD
        return None
    if event_type not in {"focusTime", "outOfOffice"}:
        return None
    if values.get("visibility") == "public":
        return ConnectorEffect.OUTWARD
    submitted, current = _calendar_status_property_state(values, event, event_type)
    resulting_mode = submitted.get("auto_decline_mode", current.get("autoDeclineMode"))
    time_changed = "start" in values or "end" in values
    if event_type == "focusTime":
        resulting_chat_status = submitted.get("chat_status", current.get("chatStatus"))
        if "chat_status" in submitted or (time_changed and resulting_chat_status != "available"):
            return ConnectorEffect.OUTWARD
    if "decline_message" in submitted and resulting_mode != "declineNone":
        return ConnectorEffect.OUTWARD
    if submitted.get("auto_decline_mode") not in {None, "declineNone"}:
        return ConnectorEffect.OUTWARD
    if time_changed and resulting_mode != "declineNone":
        return ConnectorEffect.OUTWARD
    return None


def _calendar_status_property_state(
    values: Mapping[str, object],
    event: Mapping[str, object],
    event_type: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    source = "focus_time_properties" if event_type == "focusTime" else "out_of_office_properties"
    provider_source = (
        "focusTimeProperties" if event_type == "focusTime" else "outOfOfficeProperties"
    )
    submitted = _mapping(values[source]) if source in values else {}
    current_raw = event.get(provider_source)
    current = current_raw if isinstance(current_raw, Mapping) else {}
    return submitted, current


def _calendar_event_mutation_preflight(
    values: Mapping[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> Mapping[str, object]:
    path = (
        "/calendar/v3/calendars/"
        f"{_segment(_required(values, 'calendar_id'))}/events/"
        f"{_segment(_required(values, 'event_id'))}"
    )
    event = _provider_mapping(
        _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=path,
            credential=credential,
            query=(("fields", _calendar_event_preflight_fields(values)),),
        ),
        context="Google Calendar event effect preflight",
    )
    current_etag = _safe_header(
        _provider_text(event, "etag", context="Google Calendar event effect preflight")
    )
    if current_etag != _safe_header(_required(values, "etag")):
        raise ConflictError("Google Calendar event changed; read it again before changing it")
    if "expected_event" in values:
        expected = _calendar_event_snapshot(
            values["expected_event"],
            context="Google Calendar expected event",
        )
        if expected["id"] != _required(values, "event_id"):
            raise ValidationError("Google Calendar expected event ID does not match the target")
        if expected["etag"] != _safe_header(_required(values, "etag")):
            raise ValidationError("Google Calendar expected event ETag does not match the target")
        current = _calendar_event_snapshot(event, context="Google Calendar current event")
        if current != expected:
            raise ValidationError(
                "Google Calendar expected event snapshot does not match the current event; "
                "normalize the reviewed read to the documented confirmation shape"
            )
    _validate_calendar_status_event_update(values, event)
    if values.get("recurrence"):
        _validate_calendar_recurrence_time_zones(
            values.get("start", event.get("start")),
            values.get("end", event.get("end")),
        )
    if "response_status" in values:
        _calendar_self_attendee(event, values)
    if "destination_calendar_id" in values and event.get("eventType") != "default":
        raise ValidationError("Google Calendar can move only default events")
    if "destination_calendar_id" in values:
        _calendar_move_destination_preflight(values, credential, transport)
    return event


def _calendar_event_preflight_fields(values: Mapping[str, object]) -> str:
    attendee_fields = (
        "attendees(additionalGuests,comment,email,self)"
        if "attendee_emails" in values or "attendees" in values
        else "attendees(comment,email,self)"
    )
    fields = [attendee_fields]
    if "attachments" in values:
        fields.insert(0, "attachments(fileUrl)")
    if "meet_request_id" in values:
        fields.extend(
            (
                "conferenceData(conferenceId,conferenceSolution(key(type)),"
                "createRequest(requestId,status(statusCode)),"
                "entryPoints(entryPointType,uri))",
                "hangoutLink",
            )
        )
    fields.extend(
        (
            "focusTimeProperties(autoDeclineMode,chatStatus,declineMessage)",
            "outOfOfficeProperties(autoDeclineMode,declineMessage)",
            "workingLocationProperties(customLocation(label),homeOffice,"
            "officeLocation(buildingId,deskId,floorId,floorSectionId,label),type)",
        )
    )
    fields.extend(
        (
            "end(date,dateTime,timeZone)",
            "etag",
            "eventType",
            "id",
            "organizer(displayName,email,self)",
            "start(date,dateTime,timeZone)",
            "status",
            "summary",
        )
    )
    return ",".join(fields)


def _validate_calendar_status_event_update(
    values: Mapping[str, object], event: Mapping[str, object]
) -> None:
    event_type = _provider_text(
        event,
        "eventType",
        context="Google Calendar event effect preflight",
    )
    present = [field for field in _CALENDAR_STATUS_PROPERTY_FIELDS if field in values]
    expected_property = {
        "focusTime": "focus_time_properties",
        "outOfOffice": "out_of_office_properties",
        "workingLocation": "working_location_properties",
    }.get(event_type)
    if present and present != [expected_property]:
        raise ValidationError(
            "Google Calendar status properties do not match the current event type"
        )
    if event_type in {"focusTime", "outOfOffice"}:
        if values.get("transparency", "opaque") != "opaque":
            raise ValidationError(f"Google Calendar {event_type} events must remain opaque")
        if "start" in values or "end" in values:
            start = values.get("start", event.get("start"))
            end = values.get("end", event.get("end"))
            if _calendar_time_kind(start) != "date_time" or _calendar_time_kind(end) != "date_time":
                raise ValidationError(f"Google Calendar {event_type} events must be timed")
            _validate_calendar_timed_order(start, end)
        if event_type == "focusTime":
            submitted, current = _calendar_status_property_state(values, event, event_type)
            chat_status = submitted.get("chat_status", current.get("chatStatus"))
            if chat_status == "doNotDisturb" and (
                "chat_status" in submitted or "start" in values or "end" in values
            ):
                _validate_calendar_focus_chat_duration(
                    values.get("start", event.get("start")),
                    values.get("end", event.get("end")),
                )
    elif event_type == "workingLocation":
        if values.get("transparency", "transparent") != "transparent":
            raise ValidationError("Google Calendar working-location events must remain transparent")
        if values.get("visibility", "public") != "public":
            raise ValidationError("Google Calendar working-location events must remain public")
        if "start" in values or "end" in values:
            _validate_calendar_working_location_times(
                values.get("start", event.get("start")),
                values.get("end", event.get("end")),
            )


def _validate_calendar_recurrence_time_zones(start: object, end: object) -> None:
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise ValidationError(
            "Google Calendar recurring events require one matching explicit time zone"
        )
    start_zone = start.get("time_zone", start.get("timeZone"))
    end_zone = end.get("time_zone", end.get("timeZone"))
    if not isinstance(start_zone, str) or not start_zone or start_zone != end_zone:
        raise ValidationError(
            "Google Calendar recurring events require one matching explicit time zone"
        )


def _calendar_event_snapshot(value: object, *, context: str) -> dict[str, object]:
    fields = {"end", "etag", "eventType", "id", "organizer", "start", "status", "summary"}
    if not isinstance(value, Mapping):
        raise ValidationError(f"{context} is invalid")
    selected = {key: value.get(key) for key in fields}
    identifier = _provider_text(selected, "id", context=context)
    etag = _safe_header(_provider_text(selected, "etag", context=context))
    event_type = _provider_text(selected, "eventType", context=context)
    status = _provider_text(selected, "status", context=context)
    summary = selected["summary"]
    if summary is not None and not isinstance(summary, str):
        raise ValidationError(f"{context} has an invalid summary")
    return {
        "end": _calendar_provider_time_snapshot(selected["end"], context=f"{context} end"),
        "etag": etag,
        "eventType": event_type,
        "id": identifier,
        "organizer": _calendar_organizer_snapshot(
            selected["organizer"],
            context=f"{context} organizer",
        ),
        "start": _calendar_provider_time_snapshot(
            selected["start"],
            context=f"{context} start",
        ),
        "status": status,
        "summary": summary,
    }


def _calendar_provider_time_snapshot(value: object, *, context: str) -> object:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError(f"{context} is invalid")
    allowed = {"date", "dateTime", "timeZone"}
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise ValidationError(f"{context} contains unsupported fields")
    if ("date" in value) == ("dateTime" in value):
        raise ValidationError(f"{context} must contain exactly one time value")
    if "date" in value:
        result: dict[str, object] = {
            "date": _calendar_date(value["date"], context=context).isoformat()
        }
    else:
        rendered = _text(value["dateTime"])
        _calendar_rfc3339(
            rendered,
            context=context,
            require_offset="timeZone" not in value,
        )
        result = {"dateTime": rendered}
    if "timeZone" in value:
        result["timeZone"] = _text(value["timeZone"])
    return result


def _calendar_organizer_snapshot(value: object, *, context: str) -> object:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError(f"{context} is invalid")
    allowed = {"displayName", "email", "self"}
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise ValidationError(f"{context} contains unsupported fields")
    result: dict[str, object] = {"email": _provider_text(value, "email", context=context)}
    display_name = value.get("displayName")
    if display_name is not None:
        if not isinstance(display_name, str):
            raise ValidationError(f"{context} has an invalid display name")
        result["displayName"] = display_name
    self_value = value.get("self")
    if self_value is not None:
        if type(self_value) is not bool:
            raise ValidationError(f"{context} has an invalid self marker")
        result["self"] = self_value
    return result


def _calendar_event_attendee_emails(event: Mapping[str, object]) -> tuple[set[str], bool]:
    attendees = event.get("attendees", [])
    if not isinstance(attendees, list):
        raise ValidationError("Google Calendar event preflight returned invalid attendees")
    emails: set[str] = set()
    complete = True
    for attendee in attendees:
        if not isinstance(attendee, Mapping):
            raise ValidationError("Google Calendar event preflight returned invalid attendees")
        email = attendee.get("email")
        if isinstance(email, str) and email:
            emails.add(email.casefold())
        else:
            complete = False
    return emails, complete


def _calendar_event_additional_guests(
    event: Mapping[str, object],
) -> tuple[dict[str, int], bool]:
    attendees = event.get("attendees", [])
    if not isinstance(attendees, list):
        raise ValidationError("Google Calendar event preflight returned invalid attendees")
    counts: dict[str, int] = {}
    complete = True
    for attendee in attendees:
        if not isinstance(attendee, Mapping):
            raise ValidationError("Google Calendar event preflight returned invalid attendees")
        email = attendee.get("email")
        if not isinstance(email, str) or not email:
            complete = False
            continue
        normalized = email.casefold()
        if normalized in counts:
            complete = False
            continue
        count = attendee.get("additionalGuests", 0)
        if type(count) is not int or count < 0:
            raise ValidationError(
                "Google Calendar event preflight returned invalid additional guests"
            )
        counts[normalized] = count
    return counts, complete


def _calendar_requested_attendee_emails(values: Mapping[str, object]) -> set[str]:
    if "attendee_emails" in values:
        return {email.casefold() for email in _strings(values["attendee_emails"])}
    return {
        _required(attendee, "email").casefold()
        for attendee in _mappings(_required_value(values, "attendees"))
    }


def _calendar_requested_additional_guests(values: Mapping[str, object]) -> dict[str, int]:
    if "attendee_emails" in values:
        return {email.casefold(): 0 for email in _strings(values["attendee_emails"])}
    result: dict[str, int] = {}
    for attendee in _mappings(_required_value(values, "attendees")):
        count = cast(int, attendee.get("additional_guests", 0))
        result[_required(attendee, "email").casefold()] = count
    return result


def _calendar_event_attachment_urls(
    event: Mapping[str, object],
) -> tuple[set[str], bool]:
    raw_attachments = event.get("attachments", [])
    if not isinstance(raw_attachments, list):
        raise ValidationError("Google Calendar event preflight returned invalid attachments")
    urls: set[str] = set()
    complete = True
    for attachment in raw_attachments:
        if not isinstance(attachment, Mapping):
            raise ValidationError("Google Calendar event preflight returned invalid attachments")
        file_url = attachment.get("fileUrl")
        if not isinstance(file_url, str) or not file_url or file_url in urls:
            complete = False
            continue
        urls.add(file_url)
    return urls, complete


def _calendar_event_has_conference(event: Mapping[str, object]) -> bool:
    hangout_link = event.get("hangoutLink")
    if hangout_link is not None:
        if not isinstance(hangout_link, str):
            raise ValidationError(
                "Google Calendar event preflight returned invalid conference data"
            )
        if hangout_link:
            return True
    conference = event.get("conferenceData")
    if conference is None:
        return False
    if not isinstance(conference, Mapping):
        raise ValidationError("Google Calendar event preflight returned invalid conference data")
    return bool(conference)


def _calendar_extended_property_deletion(values: Mapping[str, object]) -> bool:
    for name in ("private_extended_properties", "shared_extended_properties"):
        if name not in values:
            continue
        for item in _mappings(values[name]):
            if item.get("value") is None:
                return True
    return False


def _validate_calendar_move(values: Mapping[str, object]) -> None:
    if _required(values, "calendar_id") == _required(values, "destination_calendar_id"):
        raise ValidationError("Google Calendar move requires a different destination calendar")


def _validate_secondary_calendar_target(values: Mapping[str, object]) -> None:
    if _required(values, "calendar_id") == "primary":
        raise ValidationError("Google Calendar cannot delete the primary calendar")


def _calendar_delete_preflight(
    values: Mapping[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> None:
    calendar_id = _required(values, "calendar_id")
    calendar = _provider_mapping(
        _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"/calendar/v3/users/me/calendarList/{_segment(calendar_id)}",
            credential=credential,
            query=(("fields", "accessRole,id,primary,summary"),),
        ),
        context="Google Calendar deletion preflight",
    )
    if _provider_text(calendar, "id", context="Google Calendar deletion preflight") != calendar_id:
        raise ValidationError("Google Calendar deletion preflight returned a different calendar")
    expected = _calendar_list_entry_snapshot(
        values["expected_calendar"],
        context="Google Calendar expected deletion target",
    )
    current = _calendar_list_entry_snapshot(
        calendar,
        context="Google Calendar current deletion target",
    )
    if expected["id"] != calendar_id:
        raise ValidationError("Google Calendar expected calendar ID does not match the target")
    if current != expected:
        raise ConflictError("Google Calendar changed; read it again before deleting it")
    if calendar.get("primary") is True:
        raise ValidationError("Google Calendar cannot delete the primary calendar")
    if calendar.get("accessRole") != "owner":
        raise ValidationError("Google Calendar can delete only an owned secondary calendar")


def _calendar_move_destination_preflight(
    values: Mapping[str, object],
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> None:
    destination = _required(values, "destination_calendar_id")
    calendar = _provider_mapping(
        _json_request(
            transport,
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path=f"/calendar/v3/users/me/calendarList/{_segment(destination)}",
            credential=credential,
            query=(("fields", "accessRole,id,primary,summary"),),
        ),
        context="Google Calendar move destination preflight",
    )
    expected = _calendar_list_entry_snapshot(
        values["expected_destination_calendar"],
        context="Google Calendar expected move destination",
    )
    current = _calendar_list_entry_snapshot(
        calendar,
        context="Google Calendar current move destination",
    )
    if destination != "primary" and expected["id"] != destination:
        raise ValidationError("Google Calendar expected destination ID does not match the target")
    if current != expected:
        raise ConflictError("Google Calendar destination changed; read it again before moving")
    if current["accessRole"] not in {"owner", "writer", "writerWithoutPrivateAccess"}:
        raise ValidationError("Google Calendar move destination is not writable")


def _calendar_list_entry_snapshot(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{context} is invalid")
    primary = value.get("primary", False)
    if type(primary) is not bool:
        raise ValidationError(f"{context} has an invalid primary marker")
    summary = value.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValidationError(f"{context} has an invalid summary")
    return {
        "accessRole": _provider_text(value, "accessRole", context=context),
        "id": _provider_text(value, "id", context=context),
        "primary": primary,
        "summary": summary,
    }


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
            path=f"{_GMAIL_USERS_ME_BASE}/messages/{_segment(provider_message_id)}",
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
        f"{_GMAIL_USERS_ME_BASE}/messages/{_segment(_required(values, 'message_id'))}/attachments/"
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


def _gmail_raw_message_request(
    transport: ConnectorTransport,
    *,
    credential: ConnectorRuntimeCredential,
    values: Mapping[str, object],
    transfer: ConnectorTransferContext | None,
) -> ConnectorAdapterResult:
    if transfer is None:
        raise ValidationError("Gmail raw-message artifact delivery requires transfer context")
    writer = transfer.artifacts.start("gmail-message.eml", media_type="message/rfc822")
    decoder = GmailMessagePartBodyDecoder(writer=writer, encoded_field="raw")
    try:
        response = transport.download_stream(
            origin=ConnectorOrigin.GMAIL,
            sink=decoder,
            path=(f"{_GMAIL_USERS_ME_BASE}/messages/{_segment(_required(values, 'message_id'))}"),
            query=(("format", "raw"), ("fields", "raw")),
            credential=credential.credential,
            max_bytes=MAX_ARTIFACT_BYTES,
        )
    except Exception:
        decoder.abort()
        raise
    if response.artifact is None:
        decoder.abort()
        raise ValidationError("Gmail raw-message artifact delivery produced no receipt")
    return ConnectorAdapterResult(
        {"bytes": decoder.decoded_size, "delivery": "artifact"},
        artifact=response.artifact,
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


def _gmail_resumable_message_upload(
    transport: ConnectorTransport,
    *,
    method: ConnectorMethod,
    upload_path: str,
    credential: ConnectorRuntimeCredential,
    values: Mapping[str, object],
    reply_context: _GmailReplyContext | None,
    transfer: ConnectorTransferContext,
    metadata: Mapping[str, object],
    privacy_safe_receipt: bool,
) -> ConnectorAdapterResult:
    mime = _gmail_mime_upload(values, transfer, reply_context=reply_context)
    location = _gmail_resumable_upload_location(
        transport,
        method=method,
        upload_path=upload_path,
        credential=credential,
        query=(("uploadType", "resumable"),),
        metadata=metadata,
        size=mime.size,
    )
    uploaded = transport.request_stream(
        origin=ConnectorOrigin.GMAIL,
        method=ConnectorMethod.PUT,
        source=mime,
        content_length=mime.size,
        location=location,
        credential=None,
        content_type="message/rfc822",
        expected_statuses=_GMAIL_RESUMABLE_FINAL_STATUSES,
    )
    if privacy_safe_receipt:
        return _gmail_message_write_receipt(uploaded, action="sent")
    payload = uploaded.json()
    return _json_result(payload, strip_upload_location=True)


def _gmail_raw_message_upload(
    transport: ConnectorTransport,
    *,
    operation_name: str,
    credential: ConnectorRuntimeCredential,
    values: Mapping[str, object],
    upload: PreparedUpload,
) -> ConnectorAdapterResult:
    source = _gmail_effective_internal_date_source(operation_name, values)
    gmail_raw_message_preview(
        upload,
        strict_send=operation_name == "messages.send",
        require_date=source == "dateHeader",
    )
    upload_path = {
        "messages.import": f"{_GMAIL_USERS_ME_BASE}/messages/import",
        "messages.insert": f"{_GMAIL_USERS_ME_BASE}/messages",
        "messages.send": f"{_GMAIL_USERS_ME_BASE}/messages/send",
    }.get(operation_name)
    if upload_path is None:  # pragma: no cover - guarded by the finite operation set
        raise ValidationError("Gmail raw-message operation is invalid")
    metadata: dict[str, object] = {}
    if "label_ids" in values:
        metadata["labelIds"] = _strings(values["label_ids"])
    query: list[tuple[str, str]] = [("uploadType", "resumable")]
    query.extend(
        _optional_query(
            dict(values),
            {
                "internal_date_source": "internalDateSource",
                "never_mark_spam": "neverMarkSpam",
                "process_for_calendar": "processForCalendar",
                "deleted": "deleted",
            },
        )
    )
    try:
        location = _gmail_resumable_upload_location(
            transport,
            method=ConnectorMethod.POST,
            upload_path=upload_path,
            credential=credential,
            query=tuple(query),
            metadata=metadata,
            size=upload.size,
        )
    except ConnectorOutcomeUnknown as exc:
        raise ConnectorOutcomeUnknown(
            "Gmail raw-message upload session could not be confirmed; no message bytes were "
            "sent, so request a fresh confirmation before retrying"
        ) from exc
    try:
        uploaded = transport.request_stream(
            origin=ConnectorOrigin.GMAIL,
            method=ConnectorMethod.PUT,
            source=upload,
            content_length=upload.size,
            location=location,
            credential=None,
            content_type="message/rfc822",
            expected_statuses=_GMAIL_RESUMABLE_FINAL_STATUSES,
        )
    except ConnectorOutcomeUnknown as exc:
        action = "sent" if operation_name == "messages.send" else "created"
        raise ConnectorOutcomeUnknown(
            f"Gmail raw-message outcome is unknown and may already have been {action}; "
            "do not retry automatically, and reconcile provider state before any manual retry"
        ) from exc
    action = "sent" if operation_name == "messages.send" else "created"
    return _gmail_message_write_receipt(uploaded, action=action)


def _gmail_resumable_upload_location(
    transport: ConnectorTransport,
    *,
    method: ConnectorMethod,
    upload_path: str,
    credential: ConnectorRuntimeCredential,
    query: Sequence[tuple[str, str]],
    metadata: Mapping[str, object],
    size: int,
) -> str:
    initiated = transport.request(
        origin=ConnectorOrigin.GMAIL,
        method=method,
        path=f"/upload{upload_path}",
        credential=credential.credential,
        query=query,
        json_body=dict(metadata),
        headers={
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "message/rfc822",
        },
        expected_statuses=_GMAIL_RESUMABLE_INIT_STATUSES,
    )
    return _response_location(initiated.headers)


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
    return {"If-Match": _safe_header(_required(values, "etag"))}


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


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError("connector operation object is invalid")
    return value


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
        raise ValidationError("connector provider header value is invalid")
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
