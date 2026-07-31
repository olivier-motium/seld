"""Provider-locked HTTP transport for interactive connector adapters."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import IO, Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from continuity_kernel.errors import ContinuityError, ValidationError

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
MAX_TIMEOUT_SECONDS: Final = 120.0
MAX_REQUEST_BODY_BYTES: Final = 16 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES: Final = 16 * 1024 * 1024
MAX_ERROR_BODY_BYTES: Final = 64 * 1024
MAX_QUERY_ITEMS: Final = 128
MAX_HEADER_ITEMS: Final = 24

_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_HEADER_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{0,8192}$")
_RANGE = re.compile(r"^bytes=([0-9]+)-([0-9]+)$")
_ALLOWED_HEADERS: Final = frozenset(
    {
        "accept",
        "content-type",
        "if-match",
        "if-none-match",
        "prefer",
        "range",
        "x-goog-if-generation-match",
        "x-goog-if-metageneration-match",
    }
)
_SAFE_RESPONSE_HEADERS: Final = frozenset(
    {
        "content-length",
        "content-range",
        "content-type",
        "etag",
        "location",
        "preference-applied",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-slack-req-id",
    }
)


class ConnectorOrigin(StrEnum):
    GMAIL = "gmail"
    GOOGLE = "google"
    GOOGLE_OIDC = "google_oidc"
    MICROSOFT_GRAPH = "microsoft_graph"
    SLACK = "slack"
    DISCORD = "discord"


class ConnectorMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class AuthorizationScheme(StrEnum):
    BEARER = "Bearer"
    BOT = "Bot"


_ORIGIN_BASES: Final[Mapping[ConnectorOrigin, str]] = MappingProxyType(
    {
        ConnectorOrigin.GMAIL: "https://gmail.googleapis.com",
        ConnectorOrigin.GOOGLE: "https://www.googleapis.com",
        ConnectorOrigin.GOOGLE_OIDC: "https://openidconnect.googleapis.com",
        ConnectorOrigin.MICROSOFT_GRAPH: "https://graph.microsoft.com",
        ConnectorOrigin.SLACK: "https://slack.com",
        ConnectorOrigin.DISCORD: "https://discord.com",
    }
)
_DYNAMIC_HOSTS: Final[Mapping[ConnectorOrigin, frozenset[str]]] = MappingProxyType(
    {
        ConnectorOrigin.GMAIL: frozenset({"gmail.googleapis.com"}),
        ConnectorOrigin.GOOGLE: frozenset({"www.googleapis.com", "content.googleapis.com"}),
        ConnectorOrigin.GOOGLE_OIDC: frozenset({"openidconnect.googleapis.com"}),
        ConnectorOrigin.MICROSOFT_GRAPH: frozenset({"graph.microsoft.com"}),
        ConnectorOrigin.SLACK: frozenset({"files.slack.com", "slack.com"}),
        ConnectorOrigin.DISCORD: frozenset({"cdn.discordapp.com", "discord.com"}),
    }
)


class ConnectorProviderError(ContinuityError):
    """The provider returned a bounded non-success response."""

    def __init__(
        self,
        *,
        origin: ConnectorOrigin,
        status: int,
        code: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        self.origin = origin
        self.status = status
        self.code = code
        self.retry_after = retry_after
        detail = f"provider {origin.value} returned HTTP {status}"
        if code is not None:
            detail += f" ({code})"
        if retry_after is not None:
            detail += f"; retry after {retry_after}"
        super().__init__(detail)


class ConnectorOutcomeUnknown(ContinuityError):
    """A mutation transport failed after dispatch, so retrying could duplicate it."""


@dataclass(frozen=True, repr=False)
class ConnectorCredential:
    scheme: AuthorizationScheme
    secret: str

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, AuthorizationScheme):
            raise ValidationError("connector authorization scheme is invalid")
        if (
            not isinstance(self.secret, str)
            or not self.secret
            or len(self.secret) > 16_384
            or "\x00" in self.secret
            or "\r" in self.secret
            or "\n" in self.secret
        ):
            raise ValidationError("connector credential is invalid")

    def __repr__(self) -> str:
        return f"ConnectorCredential(scheme={self.scheme!r}, secret=<redacted>)"


@dataclass(frozen=True, repr=False)
class ConnectorResponse:
    origin: ConnectorOrigin
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __repr__(self) -> str:
        return (
            f"ConnectorResponse(origin={self.origin!r}, status={self.status!r}, "
            f"headers={dict(self.headers)!r}, body=<{len(self.body)} bytes>)"
        )

    def json(self) -> object:
        if not self.body:
            return None
        try:
            return json.loads(
                self.body.decode("utf-8"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (RecursionError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ConnectorProviderError(
                origin=self.origin,
                status=self.status,
                code="invalid_json_response",
            ) from exc


class ResponseLike(Protocol):
    status: int
    headers: object

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> ResponseLike: ...

    def __exit__(self, *args: object) -> None: ...


OpenRequest = Callable[[Request, float], ResponseLike]


class HeadersLike(Protocol):
    def items(self) -> Iterable[tuple[object, object]]: ...

    def get(self, name: str, default: object = None) -> object: ...


class ConnectorTransport:
    """Issue one bounded request to a fixed provider origin without retries."""

    def __init__(self, *, opener: OpenRequest | None = None) -> None:
        self._opener = opener or _open_without_redirects

    def request(
        self,
        *,
        origin: ConnectorOrigin,
        method: ConnectorMethod,
        path: str,
        credential: ConnectorCredential,
        query: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        expected_statuses: frozenset[int] = frozenset({200}),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        response_bound: int = MAX_RESPONSE_BODY_BYTES,
    ) -> ConnectorResponse:
        base = _ORIGIN_BASES[_origin(origin)]
        if not isinstance(credential, ConnectorCredential):
            raise ValidationError("connector credential is invalid")
        clean_path = _relative_path(path)
        return self._send(
            origin=origin,
            method=method,
            target=base + clean_path,
            credential=credential,
            query=query,
            json_body=json_body,
            body=body,
            content_type=content_type,
            headers=headers,
            expected_statuses=expected_statuses,
            timeout_seconds=timeout_seconds,
            response_bound=response_bound,
        )

    def request_provider_location(
        self,
        *,
        origin: ConnectorOrigin,
        method: ConnectorMethod,
        location: str,
        credential: ConnectorCredential | None,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        expected_statuses: frozenset[int] = frozenset({200}),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        response_bound: int = MAX_RESPONSE_BODY_BYTES,
    ) -> ConnectorResponse:
        """Follow only a response-derived HTTPS location on a provider-owned host."""

        target = _provider_location(origin, location)
        return self._send(
            origin=origin,
            method=method,
            target=target,
            credential=credential,
            query=(),
            json_body=None,
            body=body,
            content_type=content_type,
            headers=headers,
            expected_statuses=expected_statuses,
            timeout_seconds=timeout_seconds,
            response_bound=response_bound,
        )

    def _send(
        self,
        *,
        origin: ConnectorOrigin,
        method: ConnectorMethod,
        target: str,
        credential: ConnectorCredential | None,
        query: Sequence[tuple[str, str]],
        json_body: object | None,
        body: bytes | None,
        content_type: str | None,
        headers: Mapping[str, str] | None,
        expected_statuses: frozenset[int],
        timeout_seconds: float,
        response_bound: int,
    ) -> ConnectorResponse:
        _validate_request_policy(origin, method, credential)
        encoded_query = _query(query)
        if encoded_query:
            separator = "&" if "?" in target else "?"
            target += separator + encoded_query
        encoded_body, request_headers = _request_body(
            json_body=json_body,
            body=body,
            content_type=content_type,
            headers=headers,
        )
        if credential is not None:
            request_headers["Authorization"] = f"{credential.scheme.value} {credential.secret}"
        request_headers.setdefault("Accept", "application/json")
        expected = _expected_statuses(expected_statuses)
        timeout = _timeout(timeout_seconds)
        bound = _response_bound(response_bound)
        request = Request(target, data=encoded_body, headers=request_headers, method=method.value)
        try:
            with self._opener(request, timeout) as response:
                payload = _bounded_read(response, bound)
                response_headers = _safe_headers(response.headers)
                status = response.status
        except HTTPError as exc:
            error_body = _bounded_error_read(exc)
            raise ConnectorProviderError(
                origin=origin,
                status=exc.code,
                code=_provider_error_code(error_body),
                retry_after=_header(exc.headers, "Retry-After"),
            ) from exc
        except (OSError, TimeoutError, URLError) as exc:
            if method is ConnectorMethod.GET:
                raise ContinuityError(f"provider {origin.value} transport failed") from exc
            raise ConnectorOutcomeUnknown(
                "provider mutation transport failed; outcome is unknown and was not retried"
            ) from exc
        if status not in expected:
            raise ConnectorProviderError(
                origin=origin,
                status=status,
                code=_provider_error_code(payload),
                retry_after=response_headers.get("retry-after"),
            )
        return ConnectorResponse(
            origin=origin,
            status=status,
            headers=MappingProxyType(response_headers),
            body=payload,
        )


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: IO[bytes],
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> Request | None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _open_without_redirects(request: Request, timeout: float) -> ResponseLike:
    return cast(ResponseLike, build_opener(_RejectRedirects()).open(request, timeout=timeout))


def _origin(value: object) -> ConnectorOrigin:
    if not isinstance(value, ConnectorOrigin):
        raise ValidationError("connector origin is invalid")
    return value


def _relative_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 8_192
        or _PATH.fullmatch(value) is None
        or value.startswith("//")
        or any(segment in {".", ".."} for segment in value.split("/"))
    ):
        raise ValidationError("connector provider path is invalid")
    return value


def _provider_location(origin: ConnectorOrigin, value: object) -> str:
    if not isinstance(value, str) or len(value) > 16_384:
        raise ValidationError("connector provider location is invalid")
    parsed = urlsplit(value)
    host = parsed.hostname.casefold() if parsed.hostname is not None else None
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("connector provider location is invalid") from exc
    if (
        parsed.scheme != "https"
        or host not in _DYNAMIC_HOSTS[_origin(origin)]
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValidationError("connector provider location is outside its pinned origin")
    canonical = SplitResult("https", host, parsed.path, parsed.query, "")
    return urlunsplit(canonical)


def _validate_request_policy(
    origin: ConnectorOrigin,
    method: ConnectorMethod,
    credential: ConnectorCredential | None,
) -> None:
    _origin(origin)
    if not isinstance(method, ConnectorMethod):
        raise ValidationError("connector method is invalid")
    if credential is None:
        return
    if not isinstance(credential, ConnectorCredential):
        raise ValidationError("connector credential is invalid")
    if origin is ConnectorOrigin.DISCORD:
        if credential.scheme is not AuthorizationScheme.BOT:
            raise ValidationError("Discord connectors require bot authorization")
    elif credential.scheme is not AuthorizationScheme.BEARER:
        raise ValidationError("OAuth connectors require bearer authorization")


def _query(values: Sequence[tuple[str, str]]) -> str:
    if not isinstance(values, Sequence) or len(values) > MAX_QUERY_ITEMS:
        raise ValidationError("connector query exceeds its item bound")
    parsed: list[tuple[str, str]] = []
    for item in values:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
            or not item[0]
            or len(item[0]) > 256
            or len(item[1]) > 16_384
            or "\x00" in item[0]
            or "\x00" in item[1]
        ):
            raise ValidationError("connector query item is invalid")
        parsed.append(item)
    return urlencode(parsed, doseq=True)


def _request_body(
    *,
    json_body: object | None,
    body: bytes | None,
    content_type: str | None,
    headers: Mapping[str, str] | None,
) -> tuple[bytes | None, dict[str, str]]:
    if json_body is not None and body is not None:
        raise ValidationError("connector request has conflicting body encodings")
    clean_headers = _headers(headers or {})
    if json_body is not None:
        try:
            encoded = json.dumps(
                json_body,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise ValidationError("connector JSON body is invalid") from exc
        clean_headers.setdefault("Content-Type", "application/json")
    elif body is not None:
        if not isinstance(body, bytes):
            raise ValidationError("connector request body is invalid")
        encoded = body
        if content_type is not None:
            _header_value(content_type)
            clean_headers.setdefault("Content-Type", content_type)
    else:
        encoded = None
        if content_type is not None:
            raise ValidationError("connector content type requires a body")
    if encoded is not None and len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise ValidationError("connector request body exceeds its bound")
    return encoded, clean_headers


def _headers(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping) or len(values) > MAX_HEADER_ITEMS:
        raise ValidationError("connector headers exceed their item bound")
    result: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str):
            raise ValidationError("connector header is not allowed")
        normalized_name = name.casefold()
        if normalized_name not in _ALLOWED_HEADERS:
            raise ValidationError("connector header is not allowed")
        if not isinstance(value, str):
            raise ValidationError("connector header value is invalid")
        _header_value(value)
        if normalized_name == "range":
            match = _RANGE.fullmatch(value)
            if match is None or int(match.group(1)) > int(match.group(2)):
                raise ValidationError("connector range header is invalid")
        canonical = "-".join(part.capitalize() for part in name.split("-"))
        result[canonical] = value
    return result


def _header_value(value: str) -> None:
    if _HEADER_VALUE.fullmatch(value) is None:
        raise ValidationError("connector header value is invalid")


def _expected_statuses(values: object) -> frozenset[int]:
    if (
        not isinstance(values, frozenset)
        or not values
        or len(values) > 16
        or any(type(status) is not int or not 100 <= status <= 599 for status in values)
    ):
        raise ValidationError("connector expected statuses are invalid")
    return values


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= MAX_TIMEOUT_SECONDS
    ):
        raise ValidationError("connector timeout is invalid")
    return float(value)


def _response_bound(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RESPONSE_BODY_BYTES:
        raise ValidationError("connector response bound is invalid")
    return value


def _bounded_read(response: ResponseLike, bound: int) -> bytes:
    payload = response.read(bound + 1)
    if len(payload) > bound:
        raise ContinuityError("provider response exceeds its configured bound")
    return payload


def _bounded_error_read(response: HTTPError) -> bytes:
    try:
        return response.read(MAX_ERROR_BODY_BYTES + 1)[:MAX_ERROR_BODY_BYTES]
    except OSError:
        return b""


def _safe_headers(values: object) -> dict[str, str]:
    if not hasattr(values, "items"):
        return {}
    result: dict[str, str] = {}
    for name, value in cast(HeadersLike, values).items():
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.casefold() in _SAFE_RESPONSE_HEADERS
        ):
            result[name.casefold()] = value[:8_192]
    return result


def _header(values: object, name: str) -> str | None:
    if hasattr(values, "get"):
        value = cast(HeadersLike, values).get(name)
        if isinstance(value, str):
            return value[:8_192]
    return None


def _provider_error_code(body: bytes) -> str | None:
    if not body:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return None
    candidates: list[object] = []
    if isinstance(value, dict):
        candidates.extend((value.get("error"), value.get("code")))
        nested = value.get("error")
        if isinstance(nested, dict):
            candidates.extend((nested.get("code"), nested.get("status")))
    for candidate in candidates:
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", candidate):
            return candidate
    return None


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
