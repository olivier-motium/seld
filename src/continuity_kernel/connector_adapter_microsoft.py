"""Fixed Microsoft Graph adapter for the bounded Outlook operation catalog."""

from __future__ import annotations

import base64
import html
import re
import secrets
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Final
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from continuity_kernel.connector_adapter import (
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
    ConnectorTransferContext,
)
from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationSpec
from continuity_kernel.connector_operations_microsoft import MICROSOFT_OPERATIONS
from continuity_kernel.connector_transfer import (
    ConnectorInputPath,
    PreparedUpload,
    sanitize_artifact_name,
)
from continuity_kernel.connector_transport import (
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorOutcomeUnknown,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError

_PRIMARY_CALENDAR_ALIAS: Final = "primary"
_ORIGIN: Final = ConnectorOrigin.MICROSOFT_GRAPH
_GRAPH_HOST: Final = "graph.microsoft.com"
_ROOT: Final = "/v1.0/me"
_PERMANENT_DELETE_UNSUPPORTED: Final = frozenset({404, 405, 501})
_CALENDAR_PERMANENT_DELETE_UNSUPPORTED: Final = frozenset({403, 404, 405, 501})
_RESTORE_TTL_SECONDS: Final = 24 * 60 * 60
_MAX_RESTORE_HANDLES: Final = 4_096
_EXISTING_EVENT_MUTATIONS: Final = frozenset(
    {"events.delete", "events.purge", "events.update", "attachments.add"}
)
_LOCAL_FILE_LIMIT_MARKER: Final = "opaque-local-file"
_OUTLOOK_UPLOAD_MAX_BYTES: Final = 150 * 1024**2
_OUTLOOK_SESSION_THRESHOLD_BYTES: Final = 3 * 1000**2
_OUTLOOK_UPLOAD_RANGE_BYTES: Final = 3 * 1024**2
_OUTLOOK_UPLOAD_HOST: Final = "outlook.office.com"
_ATTACHMENT_METADATA_SELECT: Final = (
    "id,lastModifiedDateTime,name,contentType,size,isInline,contentId,contentLocation"
)
_ATTACHMENT_METADATA_RESPONSE_BOUND: Final = 256 * 1024
_SESSION_RESPONSE_BOUND: Final = 64 * 1024
_NEXT_EXPECTED_RANGE: Final = re.compile(r"^(0|[1-9][0-9]*)(-)?$")
_ATTACHMENT_LOCATION_EXPRESSION: Final = re.compile(
    r"^attachments\('([^']+)'\)$",
    re.IGNORECASE,
)
_HTML_DIV_TAG: Final = re.compile(r"<(/?)div\b[^<>]*>", re.IGNORECASE)
_HTML_CLASS_ATTRIBUTE: Final = re.compile(
    r"\bclass\s*=\s*(['\"])(.*?)\1",
    re.IGNORECASE | re.DOTALL,
)
_MEETING_BLOB_CLASS: Final = "me-email-text"
_CONTINUATION_KEYS: Final = frozenset(
    {"$filter", "$orderby", "$search", "$skip", "$skiptoken", "$top"}
)
_WINDOW_CONTINUATION_KEYS: Final = frozenset({"endDateTime", "startDateTime"})
_OPERATIONS: Final = {operation.key: operation for operation in MICROSOFT_OPERATIONS}
_FOLLOW_UP_STATUS: Final = {
    "not_flagged": "notFlagged",
    "flagged": "flagged",
    "complete": "complete",
}
_SHOW_AS: Final = {
    "free": "free",
    "tentative": "tentative",
    "busy": "busy",
    "oof": "oof",
    "working_elsewhere": "workingElsewhere",
    "unknown": "unknown",
}
_ONLINE_MEETING_PROVIDERS: Final = {"teams_for_business": "teamsForBusiness"}


@dataclass(frozen=True)
class _RequestShape:
    path: str
    method: ConnectorMethod
    query: tuple[tuple[str, str], ...] = ()
    json_body: object | None = None
    expected_statuses: frozenset[int] = frozenset({200})
    time_zone: str | None = None
    continuation_window: tuple[tuple[str, str], tuple[str, str]] | None = None
    mime: bool = False
    metadata_only: bool = False
    response_bound: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class _RestoreRecord:
    expires_at: float
    message_id: str
    parent_folder_id: str
    subject_id: str


class _MessageRestoreStore:
    """Short-lived process-local bindings for recoverable Outlook message deletion."""

    def __init__(self) -> None:
        self._records: dict[str, _RestoreRecord] = {}
        self._lock = Lock()

    def issue(
        self,
        *,
        message_id: str,
        parent_folder_id: str,
        subject_id: str,
    ) -> str:
        now = monotonic()
        with self._lock:
            self._prune(now)
            if len(self._records) >= _MAX_RESTORE_HANDLES:
                raise ValidationError(
                    "Outlook restore capacity is full; restore or let an earlier handle expire"
                )
            while True:
                handle = "rst-" + secrets.token_urlsafe(32)
                if handle not in self._records:
                    break
            self._records[handle] = _RestoreRecord(
                expires_at=now + _RESTORE_TTL_SECONDS,
                message_id=message_id,
                parent_folder_id=parent_folder_id,
                subject_id=subject_id,
            )
            return handle

    def consume(
        self,
        handle: str,
        *,
        message_id: str,
        subject_id: str,
    ) -> _RestoreRecord:
        now = monotonic()
        with self._lock:
            self._prune(now)
            record = self._records.pop(handle, None)
            if record is None:
                raise ValidationError(
                    "Outlook restore handle is unavailable; inspect Deleted Items before retrying"
                )
            if record.message_id != message_id or not secrets.compare_digest(
                record.subject_id, subject_id
            ):
                self._records[handle] = record
                raise ValidationError(
                    "Outlook restore handle does not match this message or account"
                )
            return record

    def restore(self, handle: str, record: _RestoreRecord) -> None:
        with self._lock:
            if record.expires_at > monotonic():
                self._records.setdefault(handle, record)

    def discard(self, handle: str) -> None:
        with self._lock:
            self._records.pop(handle, None)

    def _prune(self, now: float) -> None:
        for handle, record in tuple(self._records.items()):
            if record.expires_at <= now:
                del self._records[handle]


class MicrosoftConnectorAdapter:
    """Translate only catalogued Outlook operations into pinned Graph requests."""

    def __init__(self) -> None:
        self._message_restores = _MessageRestoreStore()

    @property
    def providers(self) -> frozenset[str]:
        return frozenset({"outlook_mail", "outlook_calendar"})

    def max_local_file_bytes(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        path: ConnectorInputPath,
    ) -> int:
        """Return Microsoft's bounded attachment upload-session capacity."""

        expected = _OPERATIONS.get(operation.key) if isinstance(operation, OperationSpec) else None
        if expected is None or operation != expected:
            raise ValidationError("Microsoft operation is not in the Microsoft catalog")
        if (
            operation.provider in {"outlook_mail", "outlook_calendar"}
            and operation.name == "attachments.add"
            and _microsoft_local_file_shape(input_value, path)
        ):
            return _OUTLOOK_UPLOAD_MAX_BYTES
        raise ValidationError("Microsoft local-file upload path is not permitted")

    def classify_effect(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        credential: ConnectorRuntimeCredential | None = None,
        transport: ConnectorTransport | None = None,
        transfer: ConnectorTransferContext | None = None,
    ) -> ConnectorEffect:
        known, data = _known_operation(operation, input_value, transfer=transfer)
        if (credential is None) is not (transport is None):
            raise ValidationError("Microsoft effect preflight requires credential and transport")
        _preflight_deletable_calendar(
            known,
            data,
            credential=credential,
            transport=transport,
        )
        if known.provider == "outlook_calendar" and known.name in _EXISTING_EVENT_MUTATIONS:
            if credential is None or transport is None:
                if known.name in {"events.delete", "events.purge"}:
                    return known.effect
                return ConnectorEffect.OUTWARD
            event = _preflight_event(
                data,
                credential=credential,
                transport=transport,
                identity_mismatch_code=(
                    "event_calendar_binding_mismatch"
                    if known.name == "events.purge"
                    else "event_identity_mismatch"
                ),
            )
            _event_precondition_data(data, event)
            if known.name in {"events.delete", "events.purge"}:
                return known.effect
            return _existing_event_effect(data, event)
        if (
            known.provider == "outlook_calendar"
            and known.name == "events.create"
            and _event_mutation_is_outward(data)
        ):
            return ConnectorEffect.OUTWARD
        return known.effect

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
        known, data = _known_operation(operation, input_value, transfer=transfer)
        if not isinstance(credential, ConnectorRuntimeCredential):
            raise ValidationError("connector runtime credential is invalid")
        if continuation is not None and known.mode is not ConnectorMode.READ:
            raise ValidationError("connector write operation cannot use a continuation")
        _preflight_deletable_calendar(
            known,
            data,
            credential=credential,
            transport=transport,
        )

        if known.name.endswith(".purge"):
            return _execute_permanent_delete(
                known,
                data,
                credential=credential,
                transport=transport,
            )
        if known.provider == "outlook_mail" and known.name == "messages.trash":
            return self._trash_message(data, credential=credential, transport=transport)
        if known.provider == "outlook_mail" and known.name == "messages.restore":
            return self._restore_message(data, credential=credential, transport=transport)

        delivery = _optional_text(data, "delivery")
        if known.name == "messages.mime" and delivery == "artifact":
            _reject_continuation(continuation)
            return _download_message_artifact(
                data,
                credential=credential,
                transport=transport,
                transfer=transfer,
            )
        if known.name == "attachments.get" and delivery == "artifact":
            _reject_continuation(continuation)
            return _download_attachment_artifact(
                known,
                data,
                credential=credential,
                transport=transport,
                transfer=transfer,
            )

        event: Mapping[str, object] | None = None
        request_data = data
        if known.provider == "outlook_calendar" and known.name in _EXISTING_EVENT_MUTATIONS:
            event = _preflight_event(data, credential=credential, transport=transport)
            request_data = _event_precondition_data(data, event)
            if (
                known.name != "events.delete"
                and _existing_event_effect(data, event) is ConnectorEffect.OUTWARD
                and write_idempotency_key is None
            ):
                raise ValidationError(
                    "the Outlook event is shared; request a fresh outward confirmation preview"
                )

        if known.name == "attachments.add" and _has_local_attachment(data):
            if transfer is None:
                raise ValidationError("Outlook local-file upload requires transfer context")
            return _execute_attachment_add(
                known,
                request_data,
                credential=credential,
                transport=transport,
                upload=transfer.upload(("attachment", "local_file")),
            )
        shape = _request_shape(known, data)
        if known.provider == "outlook_calendar" and known.name == "events.update":
            assert event is not None
            shape = _event_update_shape(data, event)
        query = shape.query
        if continuation is not None:
            query = _continuation_query(
                continuation,
                path=shape.path,
                required_select=_ATTACHMENT_METADATA_SELECT if shape.metadata_only else None,
                required_window=shape.continuation_window,
            )
        try:
            response = transport.request(
                origin=_ORIGIN,
                method=shape.method,
                path=shape.path,
                credential=credential.credential,
                query=query,
                json_body=shape.json_body,
                headers=_headers(request_data, time_zone=shape.time_zone),
                expected_statuses=shape.expected_statuses,
                response_bound=shape.response_bound,
            )
        except ConnectorProviderError as exc:
            if exc.status == 412 and "change_key" in data:
                raise _stale_outlook_resource_error(known) from exc
            if (
                known.provider == "outlook_calendar"
                and known.name == "freebusy.query"
                and exc.status == 403
            ):
                raise ConnectorProviderError(
                    origin=_ORIGIN,
                    status=exc.status,
                    code="freebusy_unsupported_for_account_or_permission",
                    retry_after=exc.retry_after,
                ) from exc
            raise
        return _result(
            response,
            path=shape.path,
            mime=shape.mime,
            metadata_only=shape.metadata_only,
            continuation_window=shape.continuation_window,
            delivery=delivery if shape.mime else None,
        )

    def _trash_message(
        self,
        data: dict[str, object],
        *,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        message_id = _text(data, "message_id")
        message = _preflight_message(message_id, credential=credential, transport=transport)
        parent_folder_id = _required_provider_text(message, "parentFolderId", "parent folder")
        subject_id = _resolve_graph_subject(credential=credential, transport=transport)
        handle = self._message_restores.issue(
            message_id=message_id,
            parent_folder_id=parent_folder_id,
            subject_id=subject_id,
        )
        try:
            transport.request(
                origin=_ORIGIN,
                method=ConnectorMethod.DELETE,
                path=_message_path(message_id),
                credential=credential.credential,
                headers=_headers(data, time_zone=None),
                expected_statuses=frozenset({204}),
            )
        except ConnectorProviderError:
            self._message_restores.discard(handle)
            raise
        return ConnectorAdapterResult(
            {
                "message_id": message_id,
                "restore_handle": handle,
                "restore_status": "available_for_24_hours_or_until_process_restart",
                "restore_ttl_seconds": _RESTORE_TTL_SECONDS,
            }
        )

    def _restore_message(
        self,
        data: dict[str, object],
        *,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult:
        message_id = _text(data, "message_id")
        handle = _text(data, "restore_handle")
        headers = _headers(data, time_zone=None)
        subject_id = _resolve_graph_subject(credential=credential, transport=transport)
        record = self._message_restores.consume(
            handle,
            message_id=message_id,
            subject_id=subject_id,
        )
        shape = _move_shape(_message_path(message_id), record.parent_folder_id)
        try:
            response = transport.request(
                origin=_ORIGIN,
                method=shape.method,
                path=shape.path,
                credential=credential.credential,
                json_body=shape.json_body,
                headers=headers,
                expected_statuses=shape.expected_statuses,
            )
        except ConnectorProviderError:
            self._message_restores.restore(handle, record)
            raise
        return _result(response, path=shape.path, mime=False)


def _known_operation(
    value: object,
    input_value: object,
    *,
    transfer: ConnectorTransferContext | None,
) -> tuple[OperationSpec, dict[str, object]]:
    if not isinstance(value, OperationSpec):
        raise ValidationError("connector operation is invalid")
    operation = _OPERATIONS.get(value.key)
    if operation is None or value != operation:
        raise ValidationError("connector operation is not in the Microsoft catalog")
    validation_input = _prepared_validation_input(operation, input_value, transfer=transfer)
    validated = operation.validate_input(validation_input)
    if not isinstance(validated, dict):
        raise ValidationError("connector operation input is invalid")
    _validate_transfer_bindings(operation, validated, transfer)
    return operation, validated


def _prepared_validation_input(
    operation: OperationSpec,
    input_value: object,
    *,
    transfer: ConnectorTransferContext | None,
) -> object:
    expected_path = ("attachment", "local_file")
    if (
        operation.name != "attachments.add"
        or operation.provider not in {"outlook_mail", "outlook_calendar"}
        or not isinstance(input_value, Mapping)
        or not isinstance(input_value.get("attachment"), Mapping)
        or transfer is None
        or expected_path not in transfer.uploads
    ):
        return input_value
    attachment = input_value["attachment"]
    assert isinstance(attachment, Mapping)
    if "local_file" in attachment or "content_base64" in attachment:
        return input_value
    prepared_attachment = dict(attachment)
    prepared_attachment["local_file"] = {
        "grant_id": "prepared",
        "relative_path": "prepared",
    }
    result = dict(input_value)
    result["attachment"] = prepared_attachment
    return result


def _validate_transfer_bindings(
    operation: OperationSpec,
    data: Mapping[str, object],
    transfer: ConnectorTransferContext | None,
) -> None:
    uploads = {} if transfer is None else transfer.uploads
    expected_path: ConnectorInputPath | None = None
    if operation.name == "attachments.add" and operation.provider in {
        "outlook_mail",
        "outlook_calendar",
    }:
        expected_path = ("attachment", "local_file")
    if expected_path is None:
        if uploads:
            raise ValidationError("prepared upload binding does not match the Microsoft operation")
        return
    if any(path != expected_path for path in uploads):
        raise ValidationError("prepared upload binding does not match the Microsoft attachment")
    attachment = _mapping(data["attachment"])
    has_local_file = "local_file" in attachment
    has_inline = "content_base64" in attachment
    if has_local_file and expected_path not in uploads:
        raise ValidationError("Outlook local-file upload requires transfer context")
    if expected_path in uploads and has_inline:
        raise ValidationError("Outlook attachment has multiple binary sources")
    if expected_path in uploads and not has_local_file:
        raise ValidationError("prepared upload binding does not match the Microsoft attachment")


def _has_local_attachment(data: Mapping[str, object]) -> bool:
    attachment = _mapping(data["attachment"])
    return "local_file" in attachment


def _microsoft_local_file_shape(input_value: object, path: ConnectorInputPath) -> bool:
    if (
        path != ("attachment", "local_file")
        or not isinstance(input_value, Mapping)
        or not isinstance(input_value.get("attachment"), Mapping)
    ):
        return False
    attachment = input_value["attachment"]
    assert isinstance(attachment, Mapping)
    return attachment.get("local_file") == _LOCAL_FILE_LIMIT_MARKER


def _request_shape(operation: OperationSpec, data: dict[str, object]) -> _RequestShape:
    if operation.provider == "outlook_mail":
        return _mail_shape(operation.name, data)
    if operation.provider == "outlook_calendar":
        return _calendar_shape(operation.name, data)
    raise ValidationError("connector operation is not implemented by Microsoft Graph")


def _mail_shape(name: str, data: dict[str, object]) -> _RequestShape:
    if name == "folders.list":
        parent = _optional_text(data, "parent_folder_id")
        path = f"{_ROOT}/mailFolders" if parent is None else f"{_folder_path(parent)}/childFolders"
        return _RequestShape(path, ConnectorMethod.GET, query=_folder_query(data))
    if name == "folders.get":
        return _RequestShape(_folder_path(_text(data, "folder_id")), ConnectorMethod.GET)
    if name == "messages.list":
        folder = _optional_text(data, "folder_id")
        path = f"{_folder_path(folder)}/messages" if folder is not None else f"{_ROOT}/messages"
        return _RequestShape(path, ConnectorMethod.GET, query=_message_query(data))
    if name == "messages.get":
        return _RequestShape(_message_path(_text(data, "message_id")), ConnectorMethod.GET)
    if name == "messages.mime":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/$value",
            ConnectorMethod.GET,
            mime=True,
            response_bound=192_000,
        )
    if name == "attachments.list":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments",
            ConnectorMethod.GET,
            query=(("$select", _ATTACHMENT_METADATA_SELECT),),
            metadata_only=True,
            response_bound=_ATTACHMENT_METADATA_RESPONSE_BOUND,
        )
    if name == "attachments.get":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.GET,
            query=(("$select", _ATTACHMENT_METADATA_SELECT),),
            metadata_only=True,
            response_bound=_ATTACHMENT_METADATA_RESPONSE_BOUND,
        )
    if name == "folders.create":
        parent = _optional_text(data, "parent_folder_id")
        path = f"{_ROOT}/mailFolders" if parent is None else f"{_folder_path(parent)}/childFolders"
        return _RequestShape(
            path,
            ConnectorMethod.POST,
            json_body={"displayName": _text(data, "display_name")},
            expected_statuses=frozenset({201}),
        )
    if name == "folders.update":
        return _RequestShape(
            _folder_path(_text(data, "folder_id")),
            ConnectorMethod.PATCH,
            json_body=_only(data, {"display_name": "displayName"}),
        )
    if name == "folders.move":
        return _move_shape(
            _folder_path(_text(data, "folder_id")),
            _text(data, "destination_folder_id"),
        )
    if name == "folders.trash":
        return _RequestShape(
            _folder_path(_text(data, "folder_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "folders.restore":
        return _move_shape(
            _folder_path(_text(data, "folder_id")),
            _optional_text(data, "parent_folder_id") or "msgfolderroot",
        )
    if name == "folders.purge":
        return _permanent_delete_shape(_folder_path(_text(data, "folder_id")))
    if name == "drafts.create":
        return _RequestShape(
            f"{_ROOT}/messages",
            ConnectorMethod.POST,
            json_body=_mail_message(data),
            expected_statuses=frozenset({201}),
        )
    if name == "drafts.update":
        return _RequestShape(
            _message_path(_text(data, "message_id")),
            ConnectorMethod.PATCH,
            json_body=_mail_message(data),
        )
    if name == "drafts.reply":
        return _draft_reply_shape(data, "createReply")
    if name == "drafts.reply_all":
        return _draft_reply_shape(data, "createReplyAll")
    if name == "drafts.forward":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/createForward",
            ConnectorMethod.POST,
            json_body={
                "comment": _optional_text(data, "comment") or "",
                "toRecipients": _graph_recipients(data["to_recipients"]),
            },
            expected_statuses=frozenset({201}),
        )
    if name == "attachments.add":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments",
            ConnectorMethod.POST,
            json_body=_graph_attachment(data["attachment"]),
            expected_statuses=frozenset({201}),
        )
    if name == "attachments.delete":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "drafts.send":
        return _RequestShape(
            f"{_message_path(_text(data, 'message_id'))}/send",
            ConnectorMethod.POST,
            expected_statuses=frozenset({202}),
        )
    if name == "messages.update":
        return _RequestShape(
            _message_path(_text(data, "message_id")),
            ConnectorMethod.PATCH,
            json_body=_message_update_body(data),
        )
    if name == "messages.copy":
        return _copy_shape(
            _message_path(_text(data, "message_id")),
            _text(data, "destination_folder_id"),
        )
    if name == "messages.move":
        return _move_shape(
            _message_path(_text(data, "message_id")),
            _text(data, "destination_folder_id"),
        )
    if name == "messages.trash":
        return _RequestShape(
            _message_path(_text(data, "message_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "messages.restore":
        return _move_shape(
            _message_path(_text(data, "message_id")),
            _optional_text(data, "parent_folder_id") or "inbox",
        )
    if name == "messages.purge":
        return _permanent_delete_shape(_message_path(_text(data, "message_id")))
    raise ValidationError("Microsoft Graph mail operation is not implemented")


def _calendar_shape(name: str, data: dict[str, object]) -> _RequestShape:
    if name == "calendars.list":
        return _RequestShape(f"{_ROOT}/calendars", ConnectorMethod.GET, query=_calendar_query(data))
    if name == "calendars.get":
        return _RequestShape(_calendar_path(_text(data, "calendar_id")), ConnectorMethod.GET)
    if name == "events.list":
        return _RequestShape(
            f"{_calendar_path(_text(data, 'calendar_id'))}/events",
            ConnectorMethod.GET,
            query=_event_query(data),
            time_zone=_optional_text(data, "time_zone"),
        )
    if name == "events.get":
        return _RequestShape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id")),
            ConnectorMethod.GET,
            time_zone=_optional_text(data, "time_zone"),
        )
    if name == "events.window":
        return _window_shape(data, "calendarView")
    if name == "events.instances":
        return _window_shape(data, "instances", event_id=_text(data, "event_id"))
    if name == "freebusy.query":
        time_zone = _time_zone(data)
        schedule_body: dict[str, object] = {
            "endTime": _graph_time({"date_time": data["end"], "time_zone": time_zone}),
            "schedules": _strings(data["attendees"]),
            "startTime": _graph_time({"date_time": data["start"], "time_zone": time_zone}),
        }
        if "interval_minutes" in data:
            schedule_body["availabilityViewInterval"] = _integer(data["interval_minutes"])
        return _RequestShape(
            f"{_ROOT}/calendar/getSchedule",
            ConnectorMethod.POST,
            json_body=schedule_body,
            time_zone=_optional_text(data, "time_zone"),
        )
    if name == "attachments.list":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments",
            ConnectorMethod.GET,
            query=(("$select", _ATTACHMENT_METADATA_SELECT),),
            metadata_only=True,
            response_bound=_ATTACHMENT_METADATA_RESPONSE_BOUND,
        )
    if name == "attachments.get":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.GET,
            query=(("$select", _ATTACHMENT_METADATA_SELECT),),
            metadata_only=True,
            response_bound=_ATTACHMENT_METADATA_RESPONSE_BOUND,
        )
    if name == "calendars.create":
        return _RequestShape(
            f"{_ROOT}/calendars",
            ConnectorMethod.POST,
            json_body={"name": _text(data, "name")},
            expected_statuses=frozenset({201}),
        )
    if name == "calendars.update":
        return _RequestShape(
            _calendar_path(_text(data, "calendar_id")),
            ConnectorMethod.PATCH,
            json_body=_only(data, {"color": "color", "name": "name"}),
        )
    if name == "calendars.delete":
        return _RequestShape(
            _calendar_path(_text(data, "calendar_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "calendars.purge":
        return _permanent_delete_shape(_calendar_path(_text(data, "calendar_id")))
    if name == "events.create":
        return _RequestShape(
            f"{_calendar_path(_text(data, 'calendar_id'))}/events",
            ConnectorMethod.POST,
            json_body=_event_body(data),
            expected_statuses=frozenset({201}),
        )
    if name == "events.update":
        return _RequestShape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id")),
            ConnectorMethod.PATCH,
            json_body=_event_body(data),
        )
    if name == "events.delete":
        return _RequestShape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id")),
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "events.cancel":
        return _event_response_shape(data, "cancel")
    if name == "events.accept":
        return _event_response_shape(data, "accept")
    if name == "events.tentative":
        return _event_response_shape(data, "tentativelyAccept")
    if name == "events.decline":
        return _event_response_shape(data, "decline")
    if name == "events.forward":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/forward",
            ConnectorMethod.POST,
            json_body={
                "comment": _optional_text(data, "comment") or "",
                "toRecipients": _graph_recipients(data["recipients"]),
            },
            expected_statuses=frozenset({202}),
        )
    if name == "attachments.add":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments",
            ConnectorMethod.POST,
            json_body=_graph_attachment(data["attachment"]),
            expected_statuses=frozenset({201}),
        )
    if name == "attachments.delete":
        return _RequestShape(
            f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments/"
            f"{_segment(_text(data, 'attachment_id'))}",
            ConnectorMethod.DELETE,
            expected_statuses=frozenset({204}),
        )
    if name == "events.purge":
        return _permanent_delete_shape(
            _event_path(_text(data, "calendar_id"), _text(data, "event_id"))
        )
    raise ValidationError("Microsoft Graph calendar operation is not implemented")


def _folder_path(folder_id: str | None) -> str:
    if folder_id is None:
        return f"{_ROOT}/mailFolders"
    return f"{_ROOT}/mailFolders/{_segment(folder_id)}"


def _message_path(message_id: str) -> str:
    return f"{_ROOT}/messages/{_segment(message_id)}"


def _calendar_path(calendar_id: str) -> str:
    if calendar_id == _PRIMARY_CALENDAR_ALIAS:
        return f"{_ROOT}/calendar"
    return f"{_ROOT}/calendars/{_segment(calendar_id)}"


def _preflight_deletable_calendar(
    operation: OperationSpec,
    data: Mapping[str, object],
    *,
    credential: ConnectorRuntimeCredential | None,
    transport: ConnectorTransport | None,
) -> None:
    if operation.provider != "outlook_calendar" or operation.name not in {
        "calendars.delete",
        "calendars.purge",
    }:
        return
    calendar_id = _text(data, "calendar_id")
    if calendar_id == _PRIMARY_CALENDAR_ALIAS:
        raise ValidationError(
            "The Outlook primary calendar cannot be deleted or permanently purged."
        )
    if credential is None or transport is None:
        return
    response = transport.request(
        origin=_ORIGIN,
        method=ConnectorMethod.GET,
        path=_calendar_path(calendar_id),
        credential=credential.credential,
        query=(("$select", "id,isDefaultCalendar"),),
        headers=_headers({}, time_zone=None),
        expected_statuses=frozenset({200}),
    )
    calendar = _provider_mapping(response, "Outlook calendar delete preflight")
    if _required_provider_text(calendar, "id", "calendar ID") != calendar_id:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=response.status,
            code="calendar_identity_mismatch",
        )
    is_default = calendar.get("isDefaultCalendar")
    if type(is_default) is not bool:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=response.status,
            code="invalid_calendar_default_state",
        )
    if is_default:
        raise ValidationError(
            "The Outlook primary calendar cannot be deleted or permanently purged."
        )


def _event_path(calendar_id: str, event_id: str) -> str:
    return f"{_calendar_path(calendar_id)}/events/{_segment(event_id)}"


def _segment(value: str) -> str:
    return quote(value, safe="")


def _move_shape(path: str, destination_id: str) -> _RequestShape:
    return _RequestShape(
        f"{path}/move",
        ConnectorMethod.POST,
        json_body={"destinationId": destination_id},
        expected_statuses=frozenset({201}),
    )


def _copy_shape(path: str, destination_id: str) -> _RequestShape:
    return _RequestShape(
        f"{path}/copy",
        ConnectorMethod.POST,
        json_body={"destinationId": destination_id},
        expected_statuses=frozenset({201}),
    )


def _permanent_delete_shape(path: str) -> _RequestShape:
    return _RequestShape(
        f"{path}/permanentDelete",
        ConnectorMethod.POST,
        expected_statuses=frozenset({204}),
    )


def _execute_permanent_delete(
    operation: OperationSpec,
    data: dict[str, object],
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> ConnectorAdapterResult:
    if operation.provider == "outlook_calendar" and operation.name == "events.purge":
        event = _preflight_event(
            data,
            credential=credential,
            transport=transport,
            identity_mismatch_code="event_calendar_binding_mismatch",
        )
        _event_precondition_data(data, event)
    subject = _resolve_graph_subject(credential=credential, transport=transport)
    user_root = f"/v1.0/users/{_segment(subject)}"
    if operation.provider == "outlook_mail" and operation.name == "messages.purge":
        resource_path = f"{user_root}/messages/{_segment(_text(data, 'message_id'))}"
    elif operation.provider == "outlook_mail" and operation.name == "folders.purge":
        resource_path = f"{user_root}/mailFolders/{_segment(_text(data, 'folder_id'))}"
    elif operation.provider == "outlook_calendar" and operation.name == "calendars.purge":
        resource_path = f"{user_root}/calendars/{_segment(_text(data, 'calendar_id'))}"
    elif operation.provider == "outlook_calendar" and operation.name == "events.purge":
        resource_path = f"{user_root}/events/{_segment(_text(data, 'event_id'))}"
    else:
        raise ValidationError("Microsoft permanent-delete operation is not implemented")
    shape = _permanent_delete_shape(resource_path)
    try:
        response = transport.request(
            origin=_ORIGIN,
            method=shape.method,
            path=shape.path,
            credential=credential.credential,
            headers=_headers(data, time_zone=None),
            expected_statuses=shape.expected_statuses,
        )
    except ConnectorProviderError as exc:
        if exc.status == 412 and "change_key" in data:
            raise _stale_outlook_resource_error(operation) from exc
        if (
            operation.provider == "outlook_calendar"
            and operation.name == "calendars.purge"
            and exc.status in _CALENDAR_PERMANENT_DELETE_UNSUPPORTED
        ):
            raise ConnectorProviderError(
                origin=_ORIGIN,
                status=exc.status,
                code="calendar_permanent_delete_unsupported_for_account",
                retry_after=exc.retry_after,
            ) from exc
        if exc.status in _PERMANENT_DELETE_UNSUPPORTED:
            raise ConnectorProviderError(
                origin=_ORIGIN,
                status=exc.status,
                code="permanent_delete_unsupported",
                retry_after=exc.retry_after,
            ) from exc
        raise
    return _result(response, path=shape.path, mime=False)


def _resolve_graph_subject(
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> str:
    response = transport.request(
        origin=_ORIGIN,
        method=ConnectorMethod.GET,
        path=_ROOT,
        credential=credential.credential,
        query=(("$select", "id"),),
        headers=_headers({}, time_zone=None),
        expected_statuses=frozenset({200}),
    )
    subject = _required_provider_text(
        _provider_mapping(response, "Microsoft account subject"),
        "id",
        "Microsoft account subject",
    )
    if len(subject) > 1_024:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=response.status,
            code="invalid_account_subject",
        )
    return subject


def _draft_reply_shape(data: dict[str, object], action: str) -> _RequestShape:
    return _RequestShape(
        f"{_message_path(_text(data, 'message_id'))}/{action}",
        ConnectorMethod.POST,
        json_body={"comment": _optional_text(data, "comment") or ""},
        expected_statuses=frozenset({201}),
    )


def _event_response_shape(data: dict[str, object], action: str) -> _RequestShape:
    return _RequestShape(
        f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/{action}",
        ConnectorMethod.POST,
        json_body={"comment": _optional_text(data, "comment") or ""},
        expected_statuses=frozenset({202}),
    )


def _window_shape(
    data: dict[str, object],
    suffix: str,
    event_id: str | None = None,
) -> _RequestShape:
    calendar_id = _text(data, "calendar_id")
    start = _text(data, "start")
    end = _text(data, "end")
    if event_id is None:
        path = f"{_calendar_path(calendar_id)}/{suffix}"
    else:
        path = f"{_event_path(calendar_id, event_id)}/{suffix}"
    query: list[tuple[str, str]] = [
        ("startDateTime", start),
        ("endDateTime", end),
    ]
    if "page_size" in data:
        query.append(("$top", str(_integer(data["page_size"]))))
    return _RequestShape(
        path,
        ConnectorMethod.GET,
        query=tuple(query),
        time_zone=_optional_text(data, "time_zone"),
        continuation_window=(
            ("startDateTime", start),
            ("endDateTime", end),
        ),
    )


def _folder_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return _list_query(data, {"display_name": "displayName"})


def _message_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    query = list(
        _list_query(
            data,
            {
                "last_modified_at": "lastModifiedDateTime",
                "received_at": "receivedDateTime",
                "sent_at": "sentDateTime",
                "subject": "subject",
            },
        )
    )
    if "is_read" in data:
        query.append(("$filter", f"isRead eq {str(data['is_read']).lower()}"))
    return tuple(query)


def _calendar_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return _list_query(data, {"name": "name"})


def _event_query(data: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return _list_query(
        data,
        {
            "end_at": "end/dateTime",
            "last_modified_at": "lastModifiedDateTime",
            "start_at": "start/dateTime",
            "subject": "subject",
        },
    )


def _list_query(
    data: dict[str, object], order_names: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    query: list[tuple[str, str]] = []
    if "page_size" in data:
        query.append(("$top", str(_integer(data["page_size"]))))
    if "search" in data:
        query.append(("$search", _text(data, "search")))
    if "order_by" in data:
        order = order_names[_text(data, "order_by")]
        direction = _optional_text(data, "sort_direction") or "ascending"
        query.append(("$orderby", f"{order} {'asc' if direction == 'ascending' else 'desc'}"))
    return tuple(query)


def _headers(data: dict[str, object], *, time_zone: str | None) -> dict[str, str]:
    prefer = 'IdType="ImmutableId"'
    if time_zone is not None:
        prefer += f', outlook.timezone="{_preference_time_zone(time_zone)}"'
    headers = {"Prefer": prefer}
    change_key = _optional_text(data, "change_key")
    if change_key is not None:
        if any(character in change_key for character in ("\x00", "\r", "\n")):
            raise ValidationError("Outlook change key is invalid")
        headers["If-Match"] = change_key
    return headers


def _preference_time_zone(value: str) -> str:
    if not value or any(character in value for character in ('"', ",", "\r", "\n")):
        raise ValidationError("connector time zone is invalid")
    return value


def _mail_message(data: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {}
    if "to_recipients" in data:
        body["toRecipients"] = _graph_recipients(data["to_recipients"])
    if "cc_recipients" in data:
        body["ccRecipients"] = _graph_recipients(data["cc_recipients"])
    if "bcc_recipients" in data:
        body["bccRecipients"] = _graph_recipients(data["bcc_recipients"])
    if "subject" in data:
        body["subject"] = _text(data, "subject")
    if "body" in data:
        body["body"] = _graph_body(data["body"])
    if "importance" in data:
        body["importance"] = _text(data, "importance")
    if "categories" in data:
        body["categories"] = _strings(data["categories"])
    return body


def _message_update_body(data: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {}
    if "is_read" in data:
        body["isRead"] = _boolean(data["is_read"])
    if "categories" in data:
        body["categories"] = _strings(data["categories"])
    if "importance" in data:
        body["importance"] = _text(data, "importance")
    if "follow_up" in data:
        body["flag"] = {"flagStatus": _FOLLOW_UP_STATUS[_text(data, "follow_up")]}
    return body


def _event_update_shape(
    data: dict[str, object],
    existing: Mapping[str, object],
) -> _RequestShape:
    body = _event_body(data)
    if "body" in data and _event_is_online_meeting(existing):
        body["body"] = _preserve_online_meeting_body(data["body"], existing)
    return _RequestShape(
        _event_path(_text(data, "calendar_id"), _text(data, "event_id")),
        ConnectorMethod.PATCH,
        json_body=body,
    )


def _event_body(data: dict[str, object]) -> dict[str, object]:
    body: dict[str, object] = {}
    if "transaction_id" in data:
        body["transactionId"] = _text(data, "transaction_id")
    if "subject" in data:
        body["subject"] = _text(data, "subject")
    if "body" in data:
        body["body"] = _graph_body(data["body"])
    if "start" in data:
        body["start"] = _graph_time(data["start"])
    if "end" in data:
        body["end"] = _graph_time(data["end"])
    if "location" in data:
        body["location"] = {"displayName": _text(data, "location")}
    if "attendees" in data:
        body["attendees"] = _graph_attendees(data["attendees"])
    if "recurrence" in data:
        body["recurrence"] = _graph_recurrence(data["recurrence"])
    if "categories" in data:
        body["categories"] = _strings(data["categories"])
    if "importance" in data:
        body["importance"] = _text(data, "importance")
    if "is_all_day" in data:
        body["isAllDay"] = _boolean(data["is_all_day"])
    if "is_reminder_on" in data:
        body["isReminderOn"] = _boolean(data["is_reminder_on"])
    if "reminder_minutes_before_start" in data:
        body["reminderMinutesBeforeStart"] = _integer(data["reminder_minutes_before_start"])
    if "response_requested" in data:
        body["responseRequested"] = _boolean(data["response_requested"])
    if "is_online_meeting" in data:
        is_online_meeting = _boolean(data["is_online_meeting"])
        body["isOnlineMeeting"] = is_online_meeting
        if is_online_meeting:
            provider = _optional_text(data, "online_meeting_provider") or "teams_for_business"
            body["onlineMeetingProvider"] = _ONLINE_MEETING_PROVIDERS[provider]
    if "sensitivity" in data:
        body["sensitivity"] = _text(data, "sensitivity")
    if "show_as" in data:
        body["showAs"] = _SHOW_AS[_text(data, "show_as")]
    return body


def _only(data: dict[str, object], names: Mapping[str, str]) -> dict[str, object]:
    return {target: _text(data, source) for source, target in names.items() if source in data}


def _graph_body(value: object) -> dict[str, object]:
    body = _mapping(value)
    content_type = _text(body, "content_type")
    return {
        "content": _text(body, "content"),
        "contentType": "Text" if content_type == "text" else "HTML",
    }


def _graph_time(value: object) -> dict[str, object]:
    time = _mapping(value)
    return {"dateTime": _text(time, "date_time"), "timeZone": _text(time, "time_zone")}


def _graph_recipients(value: object) -> list[dict[str, object]]:
    recipients: list[dict[str, object]] = []
    for item in _items(value):
        recipient = _mapping(item)
        address: dict[str, object] = {"address": _text(recipient, "email")}
        name = _optional_text(recipient, "name")
        if name is not None:
            address["name"] = name
        recipients.append({"emailAddress": address})
    return recipients


def _graph_attendees(value: object) -> list[dict[str, object]]:
    attendees: list[dict[str, object]] = []
    for item in _items(value):
        attendee = _mapping(item)
        address: dict[str, object] = {"address": _text(attendee, "email")}
        name = _optional_text(attendee, "name")
        if name is not None:
            address["name"] = name
        attendees.append({"emailAddress": address, "type": _text(attendee, "type")})
    return attendees


def _graph_attachment(value: object) -> dict[str, object]:
    attachment = _mapping(value)
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "contentBytes": _text(attachment, "content_base64"),
        "contentType": _text(attachment, "content_type"),
        "name": _text(attachment, "name"),
    }


def _execute_attachment_add(
    operation: OperationSpec,
    data: Mapping[str, object],
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
    upload: PreparedUpload,
) -> ConnectorAdapterResult:
    attachment = _mapping(data["attachment"])
    if type(upload.size) is not int or not 0 <= upload.size <= _OUTLOOK_UPLOAD_MAX_BYTES:
        raise ValidationError("Outlook local file exceeds the 150 MiB attachment limit")
    path = _attachment_collection_path(operation, data)
    if upload.size < _OUTLOOK_SESSION_THRESHOLD_BYTES:
        snapshot = _prepared_bytes(upload)
        direct = dict(attachment)
        direct.pop("local_file", None)
        direct["content_base64"] = base64.b64encode(snapshot).decode("ascii")
        response = transport.request(
            origin=_ORIGIN,
            method=ConnectorMethod.POST,
            path=path,
            credential=credential.credential,
            json_body=_graph_attachment(direct),
            headers=_headers(dict(data), time_zone=None),
            expected_statuses=frozenset({201}),
        )
        return _result(response, path=path, mime=False, metadata_only=True)
    return _outlook_upload_session(
        _attachment_upload_session_path(operation, data),
        data,
        credential=credential,
        transport=transport,
        upload=upload,
    )


def _outlook_upload_session(
    session_path: str,
    data: Mapping[str, object],
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
    upload: PreparedUpload,
) -> ConnectorAdapterResult:
    attachment = _mapping(data["attachment"])
    item: dict[str, object] = {
        "attachmentType": "file",
        "name": _text(attachment, "name"),
        "size": upload.size,
    }
    content_type = _valid_media_type(attachment.get("content_type"))
    if content_type is not None:
        item["contentType"] = content_type
    initiated = transport.request(
        origin=_ORIGIN,
        method=ConnectorMethod.POST,
        path=session_path,
        credential=credential.credential,
        json_body={"AttachmentItem": item},
        headers=_headers(dict(data), time_zone=None),
        expected_statuses=frozenset({201}),
        response_bound=_SESSION_RESPONSE_BOUND,
    )
    session = _provider_mapping(initiated, "Outlook attachment upload session")
    upload_url = _safe_outlook_location(session.get("uploadUrl"), "upload URL")
    if session.get("nextExpectedRanges") != ["0-"]:
        raise ValidationError("Outlook attachment upload session must start at byte zero")

    offset = 0
    total = upload.size
    while offset < total:
        length = min(_OUTLOOK_UPLOAD_RANGE_BYTES, total - offset)
        response = transport.request_stream(
            origin=_ORIGIN,
            method=ConnectorMethod.PUT,
            source=upload,
            content_length=length,
            location=upload_url,
            credential=None,
            content_type="application/octet-stream",
            byte_offset=offset,
            total_length=total,
            expected_statuses=frozenset({200, 201}),
        )
        if response.status == 200:
            if offset + length == total:
                raise ValidationError("Outlook final attachment upload response must be HTTP 201")
            next_offset = _next_expected_range_start(response.json(), "intermediate upload")
            expected_offset = offset + length
            if next_offset != expected_offset:
                raise ValidationError(
                    "Outlook attachment upload session returned a divergent next byte; "
                    "the upload was not retried"
                )
            offset = next_offset
            continue
        if response.status != 201:
            raise ConnectorProviderError(
                origin=_ORIGIN,
                status=response.status,
                code="invalid_upload_session_status",
            )
        if offset + length != total:
            raise ConnectorOutcomeUnknown(
                "Outlook returned HTTP 201 before the final attachment range; the outcome is "
                "unknown and must not be retried. Inspect the Outlook item before continuing."
            )
        try:
            final_location = _safe_outlook_location(
                _response_header(response.headers, "location"),
                "final attachment location",
            )
        except ValidationError as exc:
            raise ConnectorOutcomeUnknown(
                "Outlook returned HTTP 201 without a trusted final attachment receipt; the "
                "outcome is unknown and must not be retried. Inspect the Outlook item before "
                "continuing."
            ) from exc
        payload: dict[str, object] = {"bytes": total, "status": "completed"}
        attachment_id = _attachment_id_from_location(final_location)
        if attachment_id is not None:
            payload["attachment_id"] = attachment_id
        return ConnectorAdapterResult(payload)
    raise ValidationError("Outlook attachment upload session did not produce a final response")


def _prepared_bytes(upload: PreparedUpload) -> bytes:
    content = bytearray()
    for block in upload.iter_chunks():
        content.extend(block)
    if len(content) != upload.size:
        raise ValidationError("prepared upload byte count does not match its snapshot")
    return bytes(content)


def _download_message_artifact(
    data: Mapping[str, object],
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
    transfer: ConnectorTransferContext | None,
) -> ConnectorAdapterResult:
    if transfer is None:
        raise ValidationError("Outlook MIME artifact delivery requires transfer context")
    writer = transfer.artifacts.start("outlook-message.eml", media_type="message/rfc822")
    path = f"{_message_path(_text(data, 'message_id'))}/$value"
    try:
        response = transport.download_stream(
            origin=_ORIGIN,
            sink=writer,
            path=path,
            credential=credential.credential,
            max_bytes=_OUTLOOK_UPLOAD_MAX_BYTES,
        )
    except Exception:
        writer.abort()
        raise
    if response.artifact is None:
        writer.abort()
        raise ValidationError("Outlook MIME artifact delivery produced no receipt")
    return ConnectorAdapterResult(
        {"bytes": response.bytes_transferred, "delivery": "artifact"},
        artifact=response.artifact,
    )


def _download_attachment_artifact(
    operation: OperationSpec,
    data: Mapping[str, object],
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
    transfer: ConnectorTransferContext | None,
) -> ConnectorAdapterResult:
    if transfer is None:
        raise ValidationError("Outlook attachment artifact delivery requires transfer context")
    attachment_path = _attachment_path(operation, data)
    metadata_response = transport.request(
        origin=_ORIGIN,
        method=ConnectorMethod.GET,
        path=attachment_path,
        credential=credential.credential,
        query=(("$select", _ATTACHMENT_METADATA_SELECT),),
        headers=_headers({}, time_zone=None),
        expected_statuses=frozenset({200}),
        response_bound=_ATTACHMENT_METADATA_RESPONSE_BOUND,
    )
    metadata = _provider_mapping(metadata_response, "Outlook attachment metadata")
    filename, media_type = _attachment_artifact_details(metadata)
    writer = transfer.artifacts.start(filename, media_type=media_type)
    try:
        response = transport.download_stream(
            origin=_ORIGIN,
            sink=writer,
            path=f"{attachment_path}/$value",
            credential=credential.credential,
            max_bytes=_OUTLOOK_UPLOAD_MAX_BYTES,
        )
    except Exception:
        writer.abort()
        raise
    if response.artifact is None:
        writer.abort()
        raise ValidationError("Outlook attachment artifact delivery produced no receipt")
    return ConnectorAdapterResult(
        {"bytes": response.bytes_transferred, "delivery": "artifact"},
        artifact=response.artifact,
    )


def _attachment_collection_path(operation: OperationSpec, data: Mapping[str, object]) -> str:
    if operation.provider == "outlook_mail":
        return f"{_message_path(_text(data, 'message_id'))}/attachments"
    if operation.provider == "outlook_calendar":
        return f"{_event_path(_text(data, 'calendar_id'), _text(data, 'event_id'))}/attachments"
    raise ValidationError("Microsoft attachment operation has an unsupported provider")


def _attachment_upload_session_path(
    operation: OperationSpec,
    data: Mapping[str, object],
) -> str:
    if operation.provider == "outlook_mail":
        return f"{_message_path(_text(data, 'message_id'))}/attachments/createUploadSession"
    if operation.provider == "outlook_calendar":
        event_id = _segment(_text(data, "event_id"))
        return f"{_ROOT}/events/{event_id}/attachments/createUploadSession"
    raise ValidationError("Microsoft attachment operation has an unsupported provider")


def _attachment_path(operation: OperationSpec, data: Mapping[str, object]) -> str:
    return (
        f"{_attachment_collection_path(operation, data)}/{_segment(_text(data, 'attachment_id'))}"
    )


def _attachment_artifact_details(metadata: Mapping[str, object]) -> tuple[str, str]:
    filename = "outlook-attachment.bin"
    raw_name = metadata.get("name")
    if isinstance(raw_name, str) and raw_name:
        with suppress(ValidationError):
            filename = sanitize_artifact_name(raw_name)
    media_type = _valid_media_type(metadata.get("contentType")) or "application/octet-stream"
    return filename, media_type


def _valid_media_type(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or "/" not in value
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        return None
    return value


def _safe_outlook_location(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValidationError(f"Outlook {label} is invalid or missing")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"Outlook {label} is invalid or missing") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != _OUTLOOK_UPLOAD_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValidationError(f"Outlook {label} is invalid or missing")
    return urlunsplit(("https", _OUTLOOK_UPLOAD_HOST, parsed.path, parsed.query, ""))


def _next_expected_range_start(
    value: object,
    label: str,
) -> int:
    if not isinstance(value, Mapping):
        raise ValidationError(f"Outlook {label} response is invalid")
    camel_case = "nextExpectedRanges" in value
    pascal_case = "NextExpectedRanges" in value
    if camel_case is pascal_case:
        raise ValidationError(f"Outlook {label} response has ambiguous nextExpectedRanges")
    ranges = value["nextExpectedRanges" if camel_case else "NextExpectedRanges"]
    if not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], str):
        raise ValidationError(f"Outlook {label} response has ambiguous nextExpectedRanges")
    match = _NEXT_EXPECTED_RANGE.fullmatch(ranges[0])
    if match is None:
        raise ValidationError(f"Outlook {label} response has invalid nextExpectedRanges")
    return int(match.group(1))


def _attachment_id_from_location(value: str) -> str | None:
    parsed = urlsplit(value)
    parts = parsed.path.split("/")
    for index, part in enumerate(parts):
        decoded = unquote(part)
        match = _ATTACHMENT_LOCATION_EXPRESSION.fullmatch(decoded)
        if match is not None:
            candidate = unquote(match.group(1))
            if _safe_provider_id(candidate):
                return candidate
        if decoded.casefold() != "attachments" or index + 1 >= len(parts):
            continue
        candidate = unquote(parts[index + 1])
        if _safe_provider_id(candidate):
            return candidate
    return None


def _safe_provider_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 1_024
        and not any(
            character in value
            for character in ("/", "\\", "?", "#", "'", "(", ")", "\x00", "\r", "\n")
        )
    )


def _response_header(headers: Mapping[str, str], name: str) -> str | None:
    for candidate, value in headers.items():
        if candidate.casefold() == name.casefold() and isinstance(value, str) and value:
            return value
    return None


def _reject_continuation(value: object | None) -> None:
    if value is not None:
        raise ValidationError("Outlook transfer operations do not support continuations")


def _graph_recurrence(value: object) -> dict[str, object]:
    recurrence = _mapping(value)
    pattern = _mapping(recurrence["pattern"])
    event_range = _mapping(recurrence["range"])
    graph_pattern: dict[str, object] = {
        "interval": _integer(pattern["interval"]),
        "type": {
            "absolute_monthly": "absoluteMonthly",
            "absolute_yearly": "absoluteYearly",
            "daily": "daily",
            "relative_monthly": "relativeMonthly",
            "relative_yearly": "relativeYearly",
            "weekly": "weekly",
        }[_text(pattern, "type")],
    }
    graph_range: dict[str, object] = {
        "startDate": _text(event_range, "start_date"),
        "type": {
            "end_date": "endDate",
            "no_end": "noEnd",
            "numbered": "numbered",
        }[_text(event_range, "type")],
    }
    for source, target in (
        ("day_of_month", "dayOfMonth"),
        ("index", "index"),
        ("month", "month"),
    ):
        if source in pattern:
            graph_pattern[target] = pattern[source]
    if "days_of_week" in pattern:
        graph_pattern["daysOfWeek"] = _strings(pattern["days_of_week"])
    if "first_day_of_week" in pattern:
        graph_pattern["firstDayOfWeek"] = _text(pattern, "first_day_of_week")
    if "end_date" in event_range:
        graph_range["endDate"] = _text(event_range, "end_date")
    if "number_of_occurrences" in event_range:
        graph_range["numberOfOccurrences"] = _integer(event_range["number_of_occurrences"])
    if "recurrence_time_zone" in event_range:
        graph_range["recurrenceTimeZone"] = _text(event_range, "recurrence_time_zone")
    return {"pattern": graph_pattern, "range": graph_range}


def _preflight_message(
    message_id: str,
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
) -> Mapping[str, object]:
    response = transport.request(
        origin=_ORIGIN,
        method=ConnectorMethod.GET,
        path=_message_path(message_id),
        credential=credential.credential,
        query=(("$select", "id,parentFolderId"),),
        headers=_headers({}, time_zone=None),
        expected_statuses=frozenset({200}),
    )
    message = _provider_mapping(response, "Outlook message restore preflight")
    if _required_provider_text(message, "id", "message ID") != message_id:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=response.status,
            code="message_identity_mismatch",
        )
    return message


def _preflight_event(
    data: dict[str, object],
    *,
    credential: ConnectorRuntimeCredential,
    transport: ConnectorTransport,
    identity_mismatch_code: str = "event_identity_mismatch",
) -> Mapping[str, object]:
    event_id = _text(data, "event_id")
    response = transport.request(
        origin=_ORIGIN,
        method=ConnectorMethod.GET,
        path=_event_path(_text(data, "calendar_id"), event_id),
        credential=credential.credential,
        query=(
            (
                "$select",
                "id,attendees,isOrganizer,body,isOnlineMeeting,onlineMeeting,"
                "onlineMeetingProvider,changeKey",
            ),
        ),
        headers=_headers({}, time_zone=None),
        expected_statuses=frozenset({200}),
    )
    event = _provider_mapping(response, "Outlook event preflight")
    if _required_provider_text(event, "id", "event ID") != event_id:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=response.status,
            code=identity_mismatch_code,
        )
    return event


def _existing_event_effect(
    data: dict[str, object],
    existing: Mapping[str, object],
) -> ConnectorEffect:
    if _optional_text(data, "calendar_id") != _PRIMARY_CALENDAR_ALIAS:
        return ConnectorEffect.OUTWARD
    attendees = existing.get("attendees")
    if not isinstance(attendees, list):
        raise ValidationError("Outlook event preflight did not return attendees")
    if attendees:
        return ConnectorEffect.OUTWARD
    is_organizer = existing.get("isOrganizer")
    if type(is_organizer) is not bool:
        raise ValidationError("Outlook event preflight did not return organizer state")
    if not is_organizer:
        return ConnectorEffect.OUTWARD
    requested_attendees = data.get("attendees")
    if isinstance(requested_attendees, list) and requested_attendees:
        return ConnectorEffect.OUTWARD
    return ConnectorEffect.SAFE_MUTATION


def _event_precondition_data(
    data: dict[str, object],
    existing: Mapping[str, object],
) -> dict[str, object]:
    provider_change_key = _required_provider_text(existing, "changeKey", "event change key")
    requested_change_key = _text(data, "change_key")
    if requested_change_key != provider_change_key:
        raise _stale_event_error()
    return data


def _stale_event_error() -> ValidationError:
    return ValidationError(
        "Outlook event changed; read it again and request a fresh preview with its latest "
        "change_key"
    )


def _stale_outlook_resource_error(operation: OperationSpec) -> ValidationError:
    if operation.provider == "outlook_calendar":
        resource = "calendar" if operation.name.startswith("calendars.") else "event"
    elif operation.provider == "outlook_mail":
        resource = "folder" if operation.name.startswith("folders.") else "message"
    else:
        raise ValidationError("Microsoft stale-write operation is invalid")
    return ValidationError(
        f"Outlook {resource} changed; read it again and request a fresh preview with its "
        "latest change_key"
    )


def _event_is_online_meeting(existing: Mapping[str, object]) -> bool:
    flag = existing.get("isOnlineMeeting")
    if type(flag) is not bool:
        raise ValidationError("Outlook event preflight did not return online-meeting state")
    return bool(flag)


def _preserve_online_meeting_body(
    requested_value: object,
    existing: Mapping[str, object],
) -> dict[str, object]:
    requested = _graph_body(requested_value)
    meeting_blob = _isolated_online_meeting_blob(existing)
    requested_content = _text(requested, "content")
    if _text(requested, "contentType") == "Text":
        requested_content = (
            html.escape(requested_content)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "<br>")
        )
    if meeting_blob not in requested_content:
        requested_content = f"{requested_content}<br><br>{meeting_blob}"
    return {"content": requested_content, "contentType": "HTML"}


def _isolated_online_meeting_blob(existing: Mapping[str, object]) -> str:
    existing_body = _mapping(existing.get("body"))
    existing_content = _required_provider_body(existing_body, "content")
    existing_type = _required_provider_text(
        existing_body,
        "contentType",
        "online-meeting body type",
    )
    online_meeting = _mapping(existing.get("onlineMeeting"))
    join_url = _required_provider_text(
        online_meeting,
        "joinUrl",
        "online-meeting join URL",
        max_length=16_384,
    )
    if existing_type.casefold() != "html":
        raise _unsafe_online_meeting_body()

    candidates: list[re.Match[str]] = []
    for tag in _HTML_DIV_TAG.finditer(existing_content):
        if tag.group(1):
            continue
        class_attributes = _HTML_CLASS_ATTRIBUTE.findall(tag.group(0))
        if any(_MEETING_BLOB_CLASS in value.split() for _quote, value in class_attributes):
            if len(class_attributes) != 1 or tag.group(0).rstrip().endswith("/>"):
                raise _unsafe_online_meeting_body()
            candidates.append(tag)
    if len(candidates) != 1:
        raise _unsafe_online_meeting_body()

    start = candidates[0]
    depth = 0
    end_offset: int | None = None
    for tag in _HTML_DIV_TAG.finditer(existing_content, start.start()):
        if tag.group(1):
            depth -= 1
            if depth == 0:
                end_offset = tag.end()
                break
            if depth < 0:
                raise _unsafe_online_meeting_body()
        elif not tag.group(0).rstrip().endswith("/>"):
            depth += 1
    if end_offset is None:
        raise _unsafe_online_meeting_body()
    meeting_blob = existing_content[start.start() : end_offset]
    if join_url not in html.unescape(meeting_blob):
        raise _unsafe_online_meeting_body()
    return meeting_blob


def _unsafe_online_meeting_body() -> ValidationError:
    return ValidationError(
        "Outlook did not expose a safely isolated online-meeting block, so Seld did not change "
        "the body. Update other event fields here, or edit the meeting body in Outlook."
    )


def _event_mutation_is_outward(data: dict[str, object]) -> bool:
    calendar_id = _optional_text(data, "calendar_id")
    if calendar_id != _PRIMARY_CALENDAR_ALIAS:
        return True
    attendees = data.get("attendees")
    return isinstance(attendees, list) and bool(attendees)


def _result(
    response: ConnectorResponse,
    *,
    path: str,
    mime: bool,
    metadata_only: bool = False,
    continuation_window: tuple[tuple[str, str], tuple[str, str]] | None = None,
    delivery: str | None = None,
) -> ConnectorAdapterResult:
    if mime:
        mime_payload: dict[str, object] = {
            "content_base64": base64.b64encode(response.body).decode("ascii")
        }
        if delivery is not None:
            mime_payload["delivery"] = delivery
        return ConnectorAdapterResult(mime_payload)
    value = response.json()
    if metadata_only:
        value = _without_content_bytes(value)
    if not isinstance(value, Mapping):
        return ConnectorAdapterResult({} if value is None else value)
    payload: dict[str, object] = {}
    next_link: object | None = None
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConnectorProviderError(
                origin=_ORIGIN,
                status=response.status,
                code="invalid_json_response",
            )
        if key == "@odata.nextLink":
            next_link = item
        else:
            payload[key] = item
    continuation = _next_link(
        next_link,
        path=path,
        status=response.status,
        required_select=_ATTACHMENT_METADATA_SELECT if metadata_only else None,
        required_window=continuation_window,
    )
    return ConnectorAdapterResult(payload, continuation=continuation)


def _without_content_bytes(value: object) -> object:
    if isinstance(value, list):
        return [_without_content_bytes(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    result: dict[object, object] = {}
    for key, item in value.items():
        if key == "contentBytes":
            continue
        result[key] = _without_content_bytes(item)
    return result


def _next_link(
    value: object | None,
    *,
    path: str,
    status: int,
    required_select: str | None = None,
    required_window: tuple[tuple[str, str], tuple[str, str]] | None = None,
) -> object | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 16_384:
        raise ConnectorProviderError(origin=_ORIGIN, status=status, code="invalid_next_link")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=status,
            code="invalid_next_link",
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != _GRAPH_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or parsed.path != path
    ):
        raise ConnectorProviderError(origin=_ORIGIN, status=status, code="invalid_next_link")
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=128,
        )
    except ValueError as exc:
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=status,
            code="invalid_next_link",
        ) from exc
    continuation_pairs: list[tuple[str, str]] = []
    select_seen = False
    allowed_keys = _CONTINUATION_KEYS
    if required_window is not None:
        allowed_keys = allowed_keys | _WINDOW_CONTINUATION_KEYS
    for key, item in pairs:
        if key == "$select" and required_select is not None:
            if select_seen or item != required_select:
                raise ConnectorProviderError(
                    origin=_ORIGIN,
                    status=status,
                    code="invalid_next_link",
                )
            select_seen = True
            continue
        if key not in allowed_keys:
            raise ConnectorProviderError(
                origin=_ORIGIN,
                status=status,
                code="invalid_next_link",
            )
        continuation_pairs.append((key, item))
    if not _has_continuation_progress(continuation_pairs):
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=status,
            code="invalid_next_link",
        )
    if required_window is not None and not _has_exact_required_window(
        continuation_pairs, required_window
    ):
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=status,
            code="invalid_next_link",
        )
    return {"path": path, "query": [[key, item] for key, item in continuation_pairs]}


def _continuation_query(
    value: object,
    *,
    path: str,
    required_select: str | None = None,
    required_window: tuple[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[tuple[str, str], ...]:
    continuation = _mapping(value)
    if set(continuation) != {"path", "query"} or continuation.get("path") != path:
        raise ValidationError("connector continuation does not match the fixed Graph route")
    query = _items(continuation["query"])
    if not query or len(query) > 128:
        raise ValidationError("connector continuation is invalid")
    pairs: list[tuple[str, str]] = []
    allowed_keys = _CONTINUATION_KEYS
    if required_window is not None:
        allowed_keys = allowed_keys | _WINDOW_CONTINUATION_KEYS
    for item in query:
        pair = _items(item)
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], str):
            raise ValidationError("connector continuation is invalid")
        if pair[0] not in allowed_keys:
            raise ValidationError("connector continuation is invalid")
        pairs.append((pair[0], pair[1]))
    if not _has_continuation_progress(pairs):
        raise ValidationError("connector continuation is invalid")
    if required_window is not None and not _has_exact_required_window(pairs, required_window):
        raise ValidationError("connector continuation is invalid")
    if required_select is None:
        return tuple(pairs)
    return (("$select", required_select), *pairs)


def _has_continuation_progress(pairs: list[tuple[str, str]]) -> bool:
    progress = [(key, item) for key, item in pairs if key in {"$skip", "$skiptoken"}]
    if len(progress) != 1:
        return False
    key, item = progress[0]
    return (key == "$skiptoken" and bool(item)) or (
        key == "$skip" and item.isascii() and item.isdecimal() and not item.startswith("0")
    )


def _has_exact_required_window(
    pairs: list[tuple[str, str]],
    required_window: tuple[tuple[str, str], tuple[str, str]],
) -> bool:
    return all(
        [item for key, item in pairs if key == required_key] == [required_value]
        for required_key, required_value in required_window
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValidationError("connector operation input is invalid")
    return value


def _provider_mapping(response: ConnectorResponse, label: str) -> Mapping[str, object]:
    value = response.json()
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ConnectorProviderError(
            origin=_ORIGIN,
            status=response.status,
            code=f"invalid_{label.casefold().replace(' ', '_')}",
        )
    return value


def _required_provider_text(
    data: Mapping[str, object],
    name: str,
    label: str,
    *,
    max_length: int = 2_048,
) -> str:
    value = data.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ValidationError(f"Outlook {label} is invalid")
    return value


def _required_provider_body(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or "\x00" in value:
        raise ValidationError("Outlook online-meeting body is invalid")
    return value


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValidationError("connector operation input is invalid")
    return value


def _text(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise ValidationError("connector operation input is invalid")
    return value


def _optional_text(data: Mapping[str, object], name: str) -> str | None:
    if name not in data:
        return None
    return _text(data, name)


def _strings(value: object) -> list[str]:
    values = _items(value)
    if any(not isinstance(item, str) for item in values):
        raise ValidationError("connector operation input is invalid")
    return [item for item in values if isinstance(item, str)]


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValidationError("connector operation input is invalid")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValidationError("connector operation input is invalid")
    return value


def _time_zone(data: dict[str, object]) -> str:
    return _optional_text(data, "time_zone") or "UTC"
