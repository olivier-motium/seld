"""Small, fixed-destination HTTPS JSON GET boundary for connector readers."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from continuity_kernel.errors import ContinuityError, ValidationError

MAX_RESPONSE_BYTES: Final = 1024 * 1024
_ALLOWED_HOSTS: Final = frozenset(
    {"gmail.googleapis.com", "www.googleapis.com", "graph.microsoft.com", "slack.com"}
)
_GMAIL_PROFILE: Final = "/gmail/v1/users/me/profile"
_GMAIL_MESSAGES: Final = "/gmail/v1/users/me/messages"
_GMAIL_MESSAGE_PREFIX: Final = "/gmail/v1/users/me/messages/"
_GOOGLE_CALENDAR_PRIMARY: Final = "/calendar/v3/users/me/calendarList/primary"
_GOOGLE_CALENDAR_EVENTS: Final = "/calendar/v3/calendars/primary/events"
_GOOGLE_DRIVE_ABOUT: Final = "/drive/v3/about"
_GOOGLE_DRIVE_FILES: Final = "/drive/v3/files"
_GRAPH_ME: Final = "/v1.0/me"
_GRAPH_MESSAGES: Final = "/v1.0/me/messages"
_GRAPH_CALENDAR_VIEW: Final = "/v1.0/me/calendar/calendarView"
_SLACK_AUTH_TEST: Final = "/api/auth.test"
_SLACK_CONVERSATIONS_HISTORY: Final = "/api/conversations.history"

JsonGetter = Callable[[str, Mapping[str, str], float], object]


class ConnectorHTTPError(ContinuityError):
    """A provider HTTP response with no retained response body."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("connector provider returned an HTTP failure")


class ConnectorRedirectError(ContinuityError):
    """The fixed connector boundary rejected a redirect."""


class ConnectorTimeoutError(ContinuityError):
    """The fixed connector boundary timed out before receiving JSON."""


class ConnectorTransportError(ContinuityError):
    """The fixed connector boundary could not complete a request."""


class ConnectorResponseError(ContinuityError):
    """The provider response exceeded the bound or was not a JSON object."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None

    def http_error_301(self, *args: object, **kwargs: object) -> object:
        raise ConnectorRedirectError("connector redirects are not allowed")

    def http_error_302(self, *args: object, **kwargs: object) -> object:
        raise ConnectorRedirectError("connector redirects are not allowed")

    def http_error_303(self, *args: object, **kwargs: object) -> object:
        raise ConnectorRedirectError("connector redirects are not allowed")

    def http_error_307(self, *args: object, **kwargs: object) -> object:
        raise ConnectorRedirectError("connector redirects are not allowed")

    def http_error_308(self, *args: object, **kwargs: object) -> object:
        raise ConnectorRedirectError("connector redirects are not allowed")


def get_json(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> object:
    """Perform one allowlisted HTTPS GET and decode one bounded JSON object."""

    _validate_timeout(timeout_seconds)
    expected_query_keys = _validate_url(url)
    _validate_headers(headers)
    query_keys = {name for name, _value in parse_qsl(urlsplit(url).query, keep_blank_values=True)}
    if query_keys != expected_query_keys:
        raise ValidationError("connector query shape is not allowed")
    try:
        request = Request(
            url,
            headers=dict(headers),
            method="GET",
        )
    except (TypeError, ValueError) as exc:
        raise ConnectorTransportError("connector request could not be created") from exc
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=timeout_seconds) as response:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ConnectorHTTPError(status if isinstance(status, int) else 0)
            return _decode_json_object(_read_bounded(response))
    except ConnectorHTTPError:
        raise
    except (ConnectorRedirectError, ConnectorResponseError, ConnectorTimeoutError):
        raise
    except HTTPError as exc:
        status = exc.code if isinstance(exc.code, int) else 0
        exc.close()
        raise ConnectorHTTPError(status) from None
    except TimeoutError as exc:
        raise ConnectorTimeoutError("connector request timed out") from exc
    except (URLError, OSError) as exc:
        raise ConnectorTransportError("connector request was unavailable") from exc


def _validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValidationError("connector timeout must be a positive finite number")


def _validate_headers(headers: Mapping[str, str]) -> None:
    try:
        header_names = set(headers)
    except (TypeError, ValueError) as exc:
        raise ValidationError("connector headers are not allowed") from exc
    if header_names != {"Accept", "Authorization"}:
        raise ValidationError("connector headers are not allowed")
    authorization = headers.get("Authorization")
    accept = headers.get("Accept")
    if (
        not isinstance(authorization, str)
        or not authorization.startswith("Bearer ")
        or len(authorization) <= len("Bearer ")
        or any(character in authorization for character in "\r\n")
        or accept != "application/json"
    ):
        raise ValidationError("connector headers are not allowed")


def _validate_url(url: str) -> frozenset[str]:
    if not isinstance(url, str):
        raise ValidationError("connector URL is not allowed")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("connector URL is not allowed") from exc
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path
    ):
        raise ValidationError("connector URL is not allowed")
    query_keys = _allowed_query_keys(host, parsed.path)
    if query_keys is None:
        raise ValidationError("connector URL is not allowed")
    return query_keys


def _allowed_query_keys(host: str, path: str) -> frozenset[str] | None:
    if host == "gmail.googleapis.com":
        if path == _GMAIL_PROFILE:
            return frozenset()
        if path == _GMAIL_MESSAGES:
            return frozenset({"maxResults"})
        if path.startswith(_GMAIL_MESSAGE_PREFIX):
            segment = path[len(_GMAIL_MESSAGE_PREFIX) :]
            if _valid_path_segment(segment):
                return frozenset({"format", "metadataHeaders"})
        return None
    if host == "www.googleapis.com":
        if path == _GOOGLE_CALENDAR_PRIMARY:
            return frozenset()
        if path == _GOOGLE_CALENDAR_EVENTS:
            return frozenset({"timeMin", "timeMax", "singleEvents", "orderBy", "maxResults"})
        if path == _GOOGLE_DRIVE_ABOUT:
            return frozenset({"fields"})
        if path == _GOOGLE_DRIVE_FILES:
            return frozenset({"q", "orderBy", "pageSize", "fields"})
        return None
    if host == "graph.microsoft.com":
        if path == _GRAPH_ME:
            return frozenset({"$select"})
        if path == _GRAPH_MESSAGES:
            return frozenset({"$top", "$select", "$orderby"})
        if path == _GRAPH_CALENDAR_VIEW:
            return frozenset({"startDateTime", "endDateTime", "$top"})
    if host == "slack.com":
        if path == _SLACK_AUTH_TEST:
            return frozenset()
        if path == _SLACK_CONVERSATIONS_HISTORY:
            return frozenset({"channel", "limit"})
    return None


def _valid_path_segment(value: str) -> bool:
    if not value or "/" in value:
        return False
    try:
        decoded = unquote(value)
    except (TypeError, ValueError):
        return False
    return bool(decoded) and "/" not in decoded and "\x00" not in decoded


def _read_bounded(response: object) -> bytes:
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise ConnectorResponseError("connector response is unreadable")
    body = cast(bytes, cast(Callable[[int], object], reader)(MAX_RESPONSE_BYTES + 1))
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise ConnectorResponseError("connector response exceeds its size bound")
    return body


def _decode_json_object(body: bytes) -> dict[str, object]:
    try:
        decoded: object = cast(object, json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorResponseError("connector response is not valid JSON") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ConnectorResponseError("connector response must be a JSON object")
    return cast(dict[str, object], decoded)
