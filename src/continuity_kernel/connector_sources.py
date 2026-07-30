"""Read-only Google Workspace and Microsoft Graph connector source readers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import MappingProxyType
from typing import Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.connector_auth import (
    ConnectionHealth,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_http import (
    ConnectorHTTPError,
    ConnectorRedirectError,
    ConnectorResponseError,
    ConnectorTimeoutError,
    ConnectorTransportError,
    JsonGetter,
)
from continuity_kernel.connector_http import (
    get_json as http_get_json,
)
from continuity_kernel.connector_identifiers import ConnectionId, parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenEndpointError, OAuthTransportError
from continuity_kernel.errors import ConflictError, NotFoundError, SetupError, ValidationError
from continuity_kernel.records import format_time
from continuity_kernel.source_state import SourceCompleteness
from continuity_kernel.vault import Vault

SUPPORTED_SOURCE_IDS: Final = frozenset(
    {
        "gmail",
        "google_calendar",
        "google_drive",
        "outlook_mail",
        "outlook_calendar",
    }
)
_SOURCE_PROVIDERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gmail": "google",
        "google_calendar": "google",
        "google_drive": "google",
        "outlook_mail": "microsoft",
        "outlook_calendar": "microsoft",
    }
)
_OAUTH_ENDPOINTS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "google": (
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
        ),
        "microsoft": (
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        ),
    }
)
_GOOGLE_GMAIL_BASE: Final = "https://gmail.googleapis.com/gmail/v1/users/me"
_GOOGLE_CALENDAR_BASE: Final = "https://www.googleapis.com/calendar/v3"
_GOOGLE_DRIVE_BASE: Final = "https://www.googleapis.com/drive/v3"
_GRAPH_BASE: Final = "https://graph.microsoft.com/v1.0"
_MAX_LIMIT: Final = 25
_MAX_PROVIDER_ID_BYTES: Final = 2_048
_MAX_TRANSIENT_BYTES: Final = 8_192
_MAX_SUBJECT_BYTES: Final = 4_096
_MAX_SNIPPET_BYTES: Final = 8_192
_MAX_SHORT_TEXT_BYTES: Final = 2_048
_CALENDAR_WINDOW: Final = timedelta(days=60)
_TOOL_NAMESPACE: Final = "seld-connector-http-readonly-v1"
_ACCOUNT_NAMESPACE: Final = "seld-connector-account-v1"
_EVIDENCE_NAMESPACE: Final = "seld-connector-evidence-v1"


@dataclass(frozen=True)
class _ReadResult:
    items: list[dict[str, object]]
    account_binding: str
    completeness: SourceCompleteness
    covered_through: str
    evidence_refs: list[str]


@dataclass(frozen=True)
class _OperationBudget:
    deadline: float

    def remaining(self) -> float:
        seconds = self.deadline - monotonic()
        if seconds <= 0:
            raise ConnectorTimeoutError("connector operation timed out")
        return seconds


def read_connector_source(
    vault: Vault,
    *,
    connection_id: str,
    source_id: str,
    limit: int = 5,
    observed_at: datetime | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, object]:
    """Read one selected source through one explicit OAuth connection.

    The function only reads vault metadata and provider data.  It returns a
    source receipt for a later caller to submit to ``Vault.record_source_observation``;
    it never writes that receipt itself.
    """

    clean_connection_id, source_revision, connection_revision = _preflight(
        vault,
        connection_id=connection_id,
        source_id=source_id,
    )
    _validate_limit(limit)
    _validate_timeout(timeout_seconds)
    if observed_at is not None and (observed_at.tzinfo is None or observed_at.utcoffset() is None):
        raise ValidationError("connector observation time must be timezone-aware")
    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    tool_binding = _digest(_TOOL_NAMESPACE, source_id)
    budget = _OperationBudget(monotonic() + timeout_seconds)

    auth_manager = ConnectorAuthManager(vault)
    try:
        access_token = auth_manager.resolve_oauth_access_token(
            clean_connection_id,
            expected_connection_revision=connection_revision,
            minimum_validity_seconds=60,
            timeout_seconds=budget.remaining(),
            observed_at=observed,
        )
    except NotFoundError:
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "auth_required",
        )
    except OAuthTokenEndpointError as exc:
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            _token_error_code(exc),
        )
    except OAuthTransportError:
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "provider_unavailable",
        )
    except (SetupError, ValidationError):
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "auth_required",
        )

    def getter(url: str, headers: Mapping[str, str], _timeout_seconds: float) -> object:
        return http_get_json(url, headers, budget.remaining())

    try:
        if _SOURCE_PROVIDERS[source_id] == "google":
            result = _read_google(
                source_id,
                access_token=access_token,
                getter=getter,
                limit=limit,
                observed_at=observed,
                timeout_seconds=budget.remaining(),
            )
        else:
            result = _read_microsoft(
                source_id,
                access_token=access_token,
                getter=getter,
                limit=limit,
                observed_at=observed,
                timeout_seconds=budget.remaining(),
            )
    except ConnectorHTTPError as exc:
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            _http_error_code(exc.status_code),
        )
    except HTTPError as exc:
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            _http_error_code(exc.code),
        )
    except (ConnectorTimeoutError, TimeoutError):
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "timeout",
        )
    except (ConnectorRedirectError, ConnectorResponseError):
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "read_failed",
        )
    except ConnectorTransportError:
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "provider_unavailable",
        )
    except (URLError, OSError):
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "provider_unavailable",
        )
    except ValidationError:
        return _failure(
            source_id,
            source_revision,
            clean_connection_id,
            tool_binding,
            "read_failed",
        )

    if vault.get_source_snapshot().revision != source_revision:
        raise ConflictError("source selection changed during connector read")
    if vault.get_connection_snapshot().revision != connection_revision:
        raise ConflictError("connection changed during connector read")
    return _success(
        source_id,
        source_revision,
        clean_connection_id,
        tool_binding,
        result,
    )


def _preflight(
    vault: Vault,
    *,
    connection_id: str,
    source_id: str,
) -> tuple[ConnectionId, str, str]:
    expected_provider = _SOURCE_PROVIDERS.get(source_id)
    if expected_provider is None:
        raise ValidationError("connector source is unsupported")
    clean_connection_id = parse_connection_id(connection_id)
    source_snapshot = vault.get_source_snapshot()
    if source_id not in source_snapshot.selected_sources:
        raise ConflictError("connector source is not selected")
    connection_snapshot = vault.get_connection_snapshot()
    connection = connection_snapshot.connection(clean_connection_id)
    if connection is None:
        raise NotFoundError("connection was not found")
    if connection.provider != expected_provider:
        raise ValidationError("connection provider does not match source")
    if source_id not in connection.source_ids:
        raise ValidationError("connection does not authorize source")
    if connection.health in {
        ConnectionHealth.REAUTHORIZATION_REQUIRED,
        ConnectionHealth.REVOKED,
    }:
        raise ValidationError("connection health does not permit source reads")
    if connection.credential_kind is not CredentialKind.OAUTH2:
        raise ValidationError("connection credential kind is not OAuth")
    expected_endpoints = _OAUTH_ENDPOINTS[expected_provider]
    actual_endpoints = (
        connection.client.authorization_endpoint,
        connection.client.token_endpoint,
    )
    if actual_endpoints != expected_endpoints:
        raise ValidationError("connection OAuth endpoints do not match the built-in provider")
    return clean_connection_id, source_snapshot.revision, connection_snapshot.revision


def _read_google(
    source_id: str,
    *,
    access_token: str,
    getter: JsonGetter,
    limit: int,
    observed_at: datetime,
    timeout_seconds: float,
) -> _ReadResult:
    if source_id == "gmail":
        return _read_gmail(
            access_token=access_token,
            getter=getter,
            limit=limit,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )
    if source_id == "google_calendar":
        return _read_google_calendar(
            access_token=access_token,
            getter=getter,
            limit=limit,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )
    if source_id == "google_drive":
        return _read_google_drive(
            source_id,
            access_token=access_token,
            getter=getter,
            limit=limit,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )
    raise ValidationError("connector source is unsupported")


def _read_gmail(
    *,
    access_token: str,
    getter: JsonGetter,
    limit: int,
    observed_at: datetime,
    timeout_seconds: float,
) -> _ReadResult:
    identity = _request_json(
        getter,
        f"{_GOOGLE_GMAIL_BASE}/profile",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    account_identifier = _required_text(identity.get("emailAddress"), "Gmail account", 512)
    account_binding = _digest(_ACCOUNT_NAMESPACE, f"google:gmail:{account_identifier}")
    listing = _request_json(
        getter,
        f"{_GOOGLE_GMAIL_BASE}/messages?{_query((('maxResults', str(limit)),))}",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    messages = _object_items(listing, "messages", limit, optional=True)
    next_page = _page_marker(listing.get("nextPageToken"), "Gmail page marker")
    items: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    for message in messages:
        message_id = _provider_id(message.get("id"), "Gmail message ID")
        detail_query = _query(
            (
                ("format", "metadata"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Date"),
            )
        )
        detail = _request_json(
            getter,
            f"{_GOOGLE_GMAIL_BASE}/messages/{quote(message_id, safe='')}?{detail_query}",
            access_token=access_token,
            timeout_seconds=timeout_seconds,
        )
        detail_id = _provider_id(detail.get("id"), "Gmail message ID")
        if detail_id != message_id:
            raise ValidationError("Gmail message identity changed during read")
        payload = _required_object(detail.get("payload"), "Gmail message payload")
        headers = payload.get("headers")
        if not isinstance(headers, list):
            raise ValidationError("Gmail message headers are invalid")
        values: dict[str, str] = {}
        for header_value in headers:
            header = _required_object(header_value, "Gmail message header")
            name = _required_text(header.get("name"), "Gmail header name", 128)
            value = _required_text(header.get("value"), "Gmail header value", _MAX_SHORT_TEXT_BYTES)
            values[name.casefold()] = value
        snippet = _optional_text(detail.get("snippet"), "Gmail snippet", _MAX_SNIPPET_BYTES)
        evidence_refs.append(_digest(_EVIDENCE_NAMESPACE, f"google:gmail:message:{message_id}"))
        items.append(
            {
                "subject": _bounded_or_empty(
                    values.get("subject"), "Gmail subject", _MAX_SUBJECT_BYTES
                ),
                "from": _bounded_or_empty(
                    values.get("from"), "Gmail sender", _MAX_SHORT_TEXT_BYTES
                ),
                "date": _bounded_or_empty(values.get("date"), "Gmail date", _MAX_SHORT_TEXT_BYTES),
                "snippet": snippet,
            }
        )
    completeness = _page_completeness(next_page)
    return _ReadResult(
        items=items,
        account_binding=account_binding,
        completeness=completeness,
        covered_through=format_time(observed_at),
        evidence_refs=evidence_refs,
    )


def _read_google_calendar(
    *,
    access_token: str,
    getter: JsonGetter,
    limit: int,
    observed_at: datetime,
    timeout_seconds: float,
) -> _ReadResult:
    primary = _request_json(
        getter,
        f"{_GOOGLE_CALENDAR_BASE}/calendars/primary",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    calendar_id = _provider_id(primary.get("id"), "Google Calendar ID")
    account_binding = _digest(_ACCOUNT_NAMESPACE, f"google:calendar:{calendar_id}")
    window_end = observed_at + _CALENDAR_WINDOW
    event_query = _query(
        (
            ("timeMin", format_time(observed_at)),
            ("timeMax", format_time(window_end)),
            ("singleEvents", "true"),
            ("orderBy", "startTime"),
            ("maxResults", str(limit)),
        )
    )
    events = _request_json(
        getter,
        f"{_GOOGLE_CALENDAR_BASE}/calendars/primary/events?{event_query}",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    event_values = _object_items(events, "items", limit, optional=True)
    next_page = _page_marker(events.get("nextPageToken"), "Google Calendar page marker")
    items: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    for event in event_values:
        event_id = _provider_id(event.get("id"), "Google Calendar event ID")
        start = _calendar_time(event.get("start"), "Google Calendar event start")
        end = _calendar_time(event.get("end"), "Google Calendar event end")
        organizer = _calendar_person(event.get("organizer"), "Google Calendar organizer")
        evidence_refs.append(_digest(_EVIDENCE_NAMESPACE, f"google:calendar:event:{event_id}"))
        items.append(
            {
                "summary": _optional_text(
                    event.get("summary"), "Google Calendar summary", _MAX_SUBJECT_BYTES
                ),
                "start": start,
                "end": end,
                "updated": _required_text(
                    event.get("updated"), "Google Calendar updated", _MAX_SHORT_TEXT_BYTES
                ),
                "organizer": organizer,
            }
        )
    completeness = _page_completeness(next_page)
    return _ReadResult(
        items=items,
        account_binding=account_binding,
        completeness=completeness,
        covered_through=format_time(window_end),
        evidence_refs=evidence_refs,
    )


def _read_google_drive(
    source_id: str,
    *,
    access_token: str,
    getter: JsonGetter,
    limit: int,
    observed_at: datetime,
    timeout_seconds: float,
) -> _ReadResult:
    about_query = _query((("fields", "user(permissionId,emailAddress,displayName)"),))
    about = _request_json(
        getter,
        f"{_GOOGLE_DRIVE_BASE}/about?{about_query}",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    user = _required_object(about.get("user"), "Google Drive user")
    account_identifier = _first_identity(user, "Google Drive account")
    account_binding = _digest(_ACCOUNT_NAMESPACE, f"google:drive:{account_identifier}")
    query = "trashed = false"
    file_query = _query(
        (
            ("q", query),
            ("orderBy", "modifiedTime desc"),
            ("pageSize", str(limit)),
            (
                "fields",
                "nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime)",
            ),
        )
    )
    files = _request_json(
        getter,
        f"{_GOOGLE_DRIVE_BASE}/files?{file_query}",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    file_values = _object_items(files, "files", limit, optional=True)
    next_page = _page_marker(files.get("nextPageToken"), "Google Drive page marker")
    incomplete_search = files.get("incompleteSearch", False)
    if not isinstance(incomplete_search, bool):
        raise ValidationError("Google Drive incomplete-search flag is invalid")
    items: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    for file_value in file_values:
        file_id = _provider_id(file_value.get("id"), "Google Drive file ID")
        evidence_refs.append(_digest(_EVIDENCE_NAMESPACE, f"google:{source_id}:file:{file_id}"))
        items.append(
            {
                "name": _required_text(
                    file_value.get("name"), "Google Drive file name", _MAX_SUBJECT_BYTES
                ),
                "mimeType": _required_text(
                    file_value.get("mimeType"), "Google Drive MIME type", _MAX_SHORT_TEXT_BYTES
                ),
                "modifiedTime": _required_text(
                    file_value.get("modifiedTime"),
                    "Google Drive modified time",
                    _MAX_SHORT_TEXT_BYTES,
                ),
            }
        )
    completeness = _page_completeness(next_page, incomplete=incomplete_search)
    return _ReadResult(
        items=items,
        account_binding=account_binding,
        completeness=completeness,
        covered_through=format_time(observed_at),
        evidence_refs=evidence_refs,
    )


def _read_microsoft(
    source_id: str,
    *,
    access_token: str,
    getter: JsonGetter,
    limit: int,
    observed_at: datetime,
    timeout_seconds: float,
) -> _ReadResult:
    identity = _request_json(
        getter,
        f"{_GRAPH_BASE}/me?{_query((('$select', 'id,displayName,userPrincipalName,mail'),))}",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    account_id = _provider_id(identity.get("id"), "Microsoft account ID")
    account_binding = _digest(_ACCOUNT_NAMESPACE, f"microsoft:account:{account_id}")
    if source_id == "outlook_mail":
        return _read_outlook_mail(
            account_binding,
            access_token=access_token,
            getter=getter,
            limit=limit,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )
    if source_id == "outlook_calendar":
        return _read_outlook_calendar(
            account_binding,
            access_token=access_token,
            getter=getter,
            limit=limit,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )
    raise ValidationError("connector source is unsupported")


def _read_outlook_mail(
    account_binding: str,
    *,
    access_token: str,
    getter: JsonGetter,
    limit: int,
    observed_at: datetime,
    timeout_seconds: float,
) -> _ReadResult:
    message_query = _query(
        (
            ("$top", str(limit)),
            ("$select", "id,subject,receivedDateTime,from,isRead"),
            ("$orderby", "receivedDateTime desc"),
        )
    )
    payload = _request_json(
        getter,
        f"{_GRAPH_BASE}/me/messages?{message_query}",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    values = _object_items(payload, "value", limit)
    next_page = _page_marker(payload.get("@odata.nextLink"), "Microsoft page marker")
    items: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    for message in values:
        message_id = _provider_id(message.get("id"), "Outlook message ID")
        is_read = message.get("isRead")
        if not isinstance(is_read, bool):
            raise ValidationError("Outlook read state is invalid")
        evidence_refs.append(
            _digest(_EVIDENCE_NAMESPACE, f"microsoft:outlook_mail:message:{message_id}")
        )
        items.append(
            {
                "subject": _optional_text(
                    message.get("subject"), "Outlook subject", _MAX_SUBJECT_BYTES
                ),
                "received": _required_text(
                    message.get("receivedDateTime"), "Outlook received time", _MAX_SHORT_TEXT_BYTES
                ),
                "from": _graph_person(message.get("from"), "Outlook sender"),
                "isRead": is_read,
            }
        )
    completeness = _page_completeness(next_page)
    return _ReadResult(
        items=items,
        account_binding=account_binding,
        completeness=completeness,
        covered_through=format_time(observed_at),
        evidence_refs=evidence_refs,
    )


def _read_outlook_calendar(
    account_binding: str,
    *,
    access_token: str,
    getter: JsonGetter,
    limit: int,
    observed_at: datetime,
    timeout_seconds: float,
) -> _ReadResult:
    window_end = observed_at + _CALENDAR_WINDOW
    event_query = _query(
        (
            ("startDateTime", format_time(observed_at)),
            ("endDateTime", format_time(window_end)),
            ("$top", str(limit)),
        )
    )
    payload = _request_json(
        getter,
        f"{_GRAPH_BASE}/me/calendar/calendarView?{event_query}",
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
    values = _object_items(payload, "value", limit)
    next_page = _page_marker(payload.get("@odata.nextLink"), "Microsoft page marker")
    items: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    for event in values:
        event_id = _provider_id(event.get("id"), "Outlook event ID")
        evidence_refs.append(
            _digest(_EVIDENCE_NAMESPACE, f"microsoft:outlook_calendar:event:{event_id}")
        )
        items.append(
            {
                "subject": _optional_text(
                    event.get("subject"), "Outlook event subject", _MAX_SUBJECT_BYTES
                ),
                "start": _graph_date(event.get("start"), "Outlook event start"),
                "end": _graph_date(event.get("end"), "Outlook event end"),
                "organizer": _graph_person(event.get("organizer"), "Outlook organizer"),
                "lastModified": _required_text(
                    event.get("lastModifiedDateTime"),
                    "Outlook event modification time",
                    _MAX_SHORT_TEXT_BYTES,
                ),
            }
        )
    completeness = _page_completeness(next_page)
    return _ReadResult(
        items=items,
        account_binding=account_binding,
        completeness=completeness,
        covered_through=format_time(window_end),
        evidence_refs=evidence_refs,
    )


def _request_json(
    getter: JsonGetter,
    url: str,
    *,
    access_token: str,
    timeout_seconds: float,
) -> dict[str, object]:
    value = getter(
        url,
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        timeout_seconds,
    )
    return _required_object(value, "provider JSON response")


def _object_items(
    payload: Mapping[str, object],
    key: str,
    limit: int,
    *,
    optional: bool = False,
) -> list[dict[str, object]]:
    raw = payload.get(key)
    if raw is None and optional:
        return []
    if not isinstance(raw, list) or len(raw) > limit:
        raise ValidationError("provider item list exceeds its bound")
    return [_required_object(item, "provider item") for item in raw]


def _required_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{label} is invalid")
    return cast(dict[str, object], value)


def _required_text(value: object, label: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{label} is invalid")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{label} is too large")
    return value


def _optional_text(value: object, label: str, max_bytes: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or "\x00" in value:
        raise ValidationError(f"{label} is invalid")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{label} is too large")
    return value


def _bounded_or_empty(value: str | None, label: str, max_bytes: int) -> str:
    return _optional_text(value, label, max_bytes)


def _provider_id(value: object, label: str) -> str:
    identifier = _required_text(value, label, _MAX_PROVIDER_ID_BYTES)
    if any(character in identifier for character in "/?#"):
        raise ValidationError(f"{label} is invalid")
    return identifier


def _first_identity(value: Mapping[str, object], label: str) -> str:
    for key in ("permissionId", "emailAddress", "id"):
        candidate = value.get(key)
        if candidate is not None:
            return _provider_id(candidate, label)
    raise ValidationError(f"{label} is invalid")


def _calendar_time(value: object, label: str) -> dict[str, str]:
    payload = _required_object(value, label)
    date_time = payload.get("dateTime")
    date = payload.get("date")
    if date_time is not None:
        projected = {
            "dateTime": _required_text(date_time, label, _MAX_SHORT_TEXT_BYTES),
        }
        time_zone = payload.get("timeZone")
        if time_zone is not None:
            projected["timeZone"] = _required_text(
                time_zone, f"{label} timezone", _MAX_SHORT_TEXT_BYTES
            )
        return projected
    if date is not None:
        return {"date": _required_text(date, label, _MAX_SHORT_TEXT_BYTES)}
    raise ValidationError(f"{label} is invalid")


def _calendar_person(value: object, label: str) -> str:
    if value is None:
        return ""
    payload = _required_object(value, label)
    for key in ("email", "displayName"):
        candidate = payload.get(key)
        if candidate is not None:
            return _required_text(candidate, label, _MAX_SHORT_TEXT_BYTES)
    raise ValidationError(f"{label} is invalid")


def _graph_person(value: object, label: str) -> str:
    if value is None:
        return ""
    payload = _required_object(value, label)
    email_address = payload.get("emailAddress")
    if email_address is None:
        raise ValidationError(f"{label} is invalid")
    address = _required_object(email_address, label)
    for key in ("address", "name"):
        candidate = address.get(key)
        if candidate is not None:
            return _required_text(candidate, label, _MAX_SHORT_TEXT_BYTES)
    raise ValidationError(f"{label} is invalid")


def _graph_date(value: object, label: str) -> dict[str, str]:
    payload = _required_object(value, label)
    return {
        "dateTime": _required_text(payload.get("dateTime"), label, _MAX_SHORT_TEXT_BYTES),
        "timeZone": _required_text(
            payload.get("timeZone"), f"{label} timezone", _MAX_SHORT_TEXT_BYTES
        ),
    }


def _page_marker(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, _MAX_TRANSIENT_BYTES)


def _page_completeness(marker: str | None, *, incomplete: bool = False) -> SourceCompleteness:
    if marker is None and not incomplete:
        return SourceCompleteness.COMPLETE
    return SourceCompleteness.PARTIAL


def _query(parameters: tuple[tuple[str, str], ...]) -> str:
    encoded: list[str] = []
    for name, value in parameters:
        encoded_name = quote(name, safe="$")
        encoded_value = quote(value, safe=",/$'():")
        encoded.append(f"{encoded_name}={encoded_value}")
    return "&".join(encoded)


def _digest(namespace: str, value: str) -> str:
    encoded = namespace.encode("utf-8") + b"\0" + value.encode("utf-8")
    return f"sha256:{sha256_bytes(encoded)}"


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise ValidationError("connector limit must be between 1 and 25")


def _validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValidationError("connector timeout must be a positive finite number")


def _http_error_code(status_code: int) -> str:
    if status_code == 401:
        return "auth_expired"
    if status_code == 403:
        return "permission_denied"
    if status_code == 429:
        return "rate_limited"
    if status_code in {408, 504}:
        return "timeout"
    if 500 <= status_code <= 599:
        return "provider_unavailable"
    return "read_failed"


def _token_error_code(error: OAuthTokenEndpointError) -> str:
    if error.error == "invalid_grant" or error.status_code in {401, 403}:
        return "auth_expired"
    if error.status_code in {408, 504}:
        return "timeout"
    if error.status_code == 429:
        return "rate_limited"
    if 500 <= error.status_code <= 599:
        return "provider_unavailable"
    return "auth_required"


def _success(
    source_id: str,
    source_revision: str,
    connection_id: ConnectionId,
    tool_binding: str,
    result: _ReadResult,
) -> dict[str, object]:
    outcome = "success" if result.items else "explicit_empty"
    record: dict[str, object] = {
        "source": source_id,
        "result": outcome,
        "coveredThrough": result.covered_through,
        "completeness": result.completeness.value,
        "accountBinding": result.account_binding,
        "toolBinding": tool_binding,
        "evidenceRefs": result.evidence_refs,
    }
    return {
        "source": source_id,
        "sourceRevision": source_revision,
        "connectionId": str(connection_id),
        "result": outcome,
        "items": result.items,
        "record": record,
    }


def _failure(
    source_id: str,
    source_revision: str,
    connection_id: ConnectionId,
    tool_binding: str,
    error_code: str,
) -> dict[str, object]:
    return {
        "source": source_id,
        "sourceRevision": source_revision,
        "connectionId": str(connection_id),
        "result": "failure",
        "items": [],
        "errorCode": error_code,
        "record": {
            "source": source_id,
            "result": "failure",
            "errorCode": error_code,
            "toolBinding": tool_binding,
        },
    }
