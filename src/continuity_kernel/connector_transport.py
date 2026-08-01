"""Provider-locked HTTP transport for interactive connector adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import IO, Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from continuity_kernel.connector_transfer import ArtifactReceipt, PreparedUpload
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.local_files import MAX_FILE_TRANSFER_BYTES

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
MAX_TIMEOUT_SECONDS: Final = 120.0
MAX_REQUEST_BODY_BYTES: Final = 16 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES: Final = 16 * 1024 * 1024
MAX_ERROR_BODY_BYTES: Final = 64 * 1024
MAX_STREAM_BODY_BYTES: Final = MAX_FILE_TRANSFER_BYTES
MAX_STREAM_CHUNK_BYTES: Final = 1024 * 1024
MAX_QUERY_ITEMS: Final = 128
MAX_HEADER_ITEMS: Final = 24
SLACK_AUTH_FAILURE_CODES: Final = frozenset(
    {
        "account_inactive",
        "invalid_auth",
        "not_authed",
        "token_expired",
        "token_revoked",
    }
)

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
        "x-upload-content-length",
        "x-upload-content-type",
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
        "range",
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
        ConnectorOrigin.MICROSOFT_GRAPH: frozenset({"graph.microsoft.com", "outlook.office.com"}),
        ConnectorOrigin.SLACK: frozenset({"files.slack.com", "slack.com"}),
        ConnectorOrigin.DISCORD: frozenset({"cdn.discordapp.com", "discord.com"}),
    }
)
_DYNAMIC_HOST_SUFFIXES: Final[Mapping[ConnectorOrigin, tuple[str, ...]]] = MappingProxyType(
    {
        ConnectorOrigin.GMAIL: (),
        ConnectorOrigin.GOOGLE: (".googleusercontent.com",),
        ConnectorOrigin.GOOGLE_OIDC: (),
        ConnectorOrigin.MICROSOFT_GRAPH: (),
        ConnectorOrigin.SLACK: (),
        ConnectorOrigin.DISCORD: (".discordapp.com",),
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
        safe_headers = {
            name: ("<redacted>" if name.casefold() == "location" else value)
            for name, value in self.headers.items()
        }
        return (
            f"ConnectorResponse(origin={self.origin!r}, status={self.status!r}, "
            f"headers={safe_headers!r}, body=<{len(self.body)} bytes>)"
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


@dataclass(frozen=True, repr=False)
class ConnectorStreamResponse:
    """Response metadata for a transfer whose body was never materialized."""

    origin: ConnectorOrigin
    status: int
    headers: Mapping[str, str]
    bytes_transferred: int
    sha256: str | None
    next_offset: int | None = None
    control_body: bytes = b""
    artifact: ArtifactReceipt | None = None

    @property
    def body(self) -> bytes:
        """Return only the bounded provider control response, never transferred bytes."""

        return self.control_body

    def json(self) -> object:
        if not self.control_body:
            return None
        try:
            return json.loads(
                self.control_body.decode("utf-8"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (RecursionError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ConnectorProviderError(
                origin=self.origin,
                status=self.status,
                code="invalid_json_response",
            ) from exc

    @property
    def bytes_sent(self) -> int:
        return self.bytes_transferred

    @property
    def bytes_received(self) -> int:
        return self.bytes_transferred

    def __repr__(self) -> str:
        safe_headers = {
            name: ("<redacted>" if name.casefold() == "location" else value)
            for name, value in self.headers.items()
        }
        return (
            f"ConnectorStreamResponse(origin={self.origin!r}, status={self.status!r}, "
            f"headers={safe_headers!r}, bytes_transferred={self.bytes_transferred!r}, "
            f"next_offset={self.next_offset!r}, "
            f"control_body=<{len(self.control_body)} bytes>, artifact={self.artifact!r}, "
            "sha256=<redacted>)"
        )


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

    def request_stream(
        self,
        *,
        origin: ConnectorOrigin,
        method: ConnectorMethod,
        source: Iterable[bytes] | PreparedUpload | None = None,
        content_length: int | None = None,
        path: str | None = None,
        location: str | None = None,
        query: Sequence[tuple[str, str]] = (),
        credential: ConnectorCredential | None = None,
        content_type: str | None = None,
        byte_offset: int = 0,
        total_length: int | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 201, 202, 204, 308}),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> ConnectorStreamResponse:
        """Upload an adapter-owned stream with an exact generated byte length.

        The destination is either a validated relative provider path or a
        response-derived provider location. No caller headers, raw URL, or
        token are accepted by this boundary.
        """

        target = _stream_target(origin, path=path, location=location)
        target = _stream_query(target, query=query, location=location)
        _validate_upload_stream_credential(path=path, location=location, credential=credential)
        _validate_request_policy(origin, method, credential)
        if source is None:
            raise ValidationError("stream source is required")
        if isinstance(source, PreparedUpload):
            length = _prepared_upload_length(
                source,
                content_length=content_length,
                byte_offset=byte_offset,
                total_length=total_length,
            )
        else:
            if byte_offset != 0:
                raise ValidationError("resumed uploads require an immutable prepared upload")
            length = _stream_length(source, content_length)
        headers = _stream_upload_headers(
            length=length,
            content_type=content_type,
            byte_offset=byte_offset,
            total_length=total_length,
        )
        expected = _expected_statuses(expected_statuses)
        timeout = _timeout(timeout_seconds)
        body = _ExactBody(source, expected_length=length, byte_offset=byte_offset)
        if credential is not None:
            headers["Authorization"] = f"{credential.scheme.value} {credential.secret}"
        request = Request(target, data=body, headers=headers, method=method.value)
        response_headers: dict[str, str] = {}
        control_body = b""
        status = 0
        try:
            with self._opener(request, timeout) as response:
                status = _response_status(response)
                response_headers = _safe_headers(response.headers)
                if status not in expected:
                    error_body = _bounded_stream_error_read(response)
                    raise ConnectorProviderError(
                        origin=origin,
                        status=status,
                        code=_provider_error_code(error_body),
                        retry_after=response_headers.get("retry-after"),
                    )
                control_body = _bounded_read(response, MAX_RESPONSE_BODY_BYTES)
            body.verify_complete()
        except HTTPError as exc:
            if exc.code in expected:
                status = exc.code
                response_headers = _safe_headers(exc.headers)
                control_body = _bounded_read(cast(ResponseLike, exc), MAX_RESPONSE_BODY_BYTES)
                try:
                    body.verify_complete()
                except ValidationError as verification_error:
                    raise ConnectorOutcomeUnknown(
                        "provider responded before the complete upload; outcome is unknown"
                    ) from verification_error
            else:
                raise ConnectorProviderError(
                    origin=origin,
                    status=exc.code,
                    code=_provider_error_code(_bounded_error_read(exc)),
                    retry_after=_header(exc.headers, "Retry-After"),
                ) from exc
        except ValidationError as exc:
            if body.total:
                raise ConnectorOutcomeUnknown(
                    "provider stream source failed after dispatch; outcome is unknown"
                ) from exc
            raise
        except (OSError, TimeoutError, URLError) as exc:
            raise ConnectorOutcomeUnknown(
                "provider stream transport failed; outcome is unknown and was not retried"
            ) from exc
        next_offset = _resume_next_offset(
            status=status,
            headers=response_headers,
            byte_offset=byte_offset,
            content_length=length,
        )
        return ConnectorStreamResponse(
            origin=origin,
            status=status,
            headers=MappingProxyType(response_headers),
            bytes_transferred=body.total,
            sha256=body.hexdigest,
            next_offset=next_offset,
            control_body=control_body,
        )

    def download_stream(
        self,
        *,
        origin: ConnectorOrigin,
        sink: object,
        path: str | None = None,
        location: str | None = None,
        query: Sequence[tuple[str, str]] = (),
        credential: ConnectorCredential | None = None,
        range_start: int | None = None,
        range_end: int | None = None,
        expected_length: int | None = None,
        expected_statuses: frozenset[int] = frozenset({200, 206}),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = MAX_STREAM_BODY_BYTES,
    ) -> ConnectorStreamResponse:
        """Stream a provider response into an adapter-owned sink.

        The sink is aborted on every failure when it exposes ``abort``. A
        sink exposing ``finish`` is completed only after status, length, range,
        and byte-count checks have passed.
        """

        target = _stream_target(origin, path=path, location=location)
        target = _stream_query(target, query=query, location=location)
        _validate_download_stream_credential(
            origin=origin,
            path=path,
            location=location,
            credential=credential,
        )
        _validate_request_policy(origin, ConnectorMethod.GET, credential)
        _stream_bound(max_bytes)
        expected = _expected_statuses(expected_statuses)
        request_headers: dict[str, str] = {"Accept": "application/octet-stream"}
        expected_from_range = _download_range_header(
            request_headers,
            range_start=range_start,
            range_end=range_end,
        )
        expected_count = expected_length
        if expected_count is None:
            expected_count = expected_from_range
        if expected_count is not None:
            _stream_length_value(expected_count, label="expected download length")
            if expected_count > max_bytes:
                raise ValidationError("expected download length exceeds its operation bound")
        if credential is not None:
            request_headers["Authorization"] = f"{credential.scheme.value} {credential.secret}"
        timeout = _timeout(timeout_seconds)
        response_headers: dict[str, str] = {}
        status = 0
        digest = hashlib.sha256()
        total = 0
        try:
            redirected = False
            while True:
                request = Request(target, headers=request_headers, method="GET")
                try:
                    response = self._opener(request, timeout)
                except HTTPError as exc:
                    redirect_headers = _safe_headers(exc.headers)
                    redirected_target = _credentialless_download_redirect(
                        origin,
                        from_fixed_path=path is not None,
                        already_redirected=redirected,
                        status=exc.code,
                        headers=redirect_headers,
                    )
                    if redirected_target is None:
                        raise
                    with suppress(Exception):
                        exc.close()
                    target = redirected_target
                    request_headers.pop("Authorization", None)
                    redirected = True
                    continue
                break
            with response:
                status = _response_status(response)
                response_headers = _safe_headers(response.headers)
                if status not in expected:
                    error_body = _bounded_stream_error_read(response)
                    raise ConnectorProviderError(
                        origin=origin,
                        status=status,
                        code=_provider_error_code(error_body),
                        retry_after=response_headers.get("retry-after"),
                    )
                _validate_download_status(
                    status,
                    range_requested=range_start is not None,
                )
                declared = _declared_content_length(response_headers)
                if declared is not None:
                    if declared > max_bytes:
                        raise ValidationError(
                            "provider download Content-Length exceeds its operation bound"
                        )
                    if expected_count is not None and declared != expected_count:
                        raise ValidationError("provider download Content-Length is unexpected")
                    expected_count = declared
                _validate_download_content_range(
                    response_headers,
                    range_start=range_start,
                    range_end=range_end,
                    expected_count=expected_count,
                )
                while True:
                    block = response.read(MAX_STREAM_CHUNK_BYTES)
                    if not isinstance(block, bytes):
                        raise ValidationError("provider download returned a non-binary chunk")
                    if len(block) > MAX_STREAM_CHUNK_BYTES:
                        raise ValidationError("provider download chunk exceeds its size bound")
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise ValidationError("provider download exceeds its size bound")
                    _write_sink(sink, block)
                    digest.update(block)
                if expected_count is not None and total != expected_count:
                    raise ValidationError("provider download byte count does not match its length")
            artifact = _finish_sink(sink)
        except HTTPError as exc:
            _abort_sink(sink)
            raise ConnectorProviderError(
                origin=origin,
                status=exc.code,
                code=_provider_error_code(_bounded_error_read(exc)),
                retry_after=_header(exc.headers, "Retry-After"),
            ) from exc
        except (OSError, TimeoutError, URLError) as exc:
            _abort_sink(sink)
            raise ContinuityError(f"provider {origin.value} download transport failed") from exc
        except Exception:
            _abort_sink(sink)
            raise
        return ConnectorStreamResponse(
            origin=origin,
            status=status,
            headers=MappingProxyType(response_headers),
            bytes_transferred=total,
            sha256=digest.hexdigest(),
            artifact=artifact,
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


class _ExactBody:
    """File-like adapter that counts an iterable without buffering the payload."""

    def __init__(
        self,
        source: Iterable[bytes] | PreparedUpload,
        *,
        expected_length: int,
        byte_offset: int = 0,
    ) -> None:
        self._source = (
            source.iter_chunks(offset=byte_offset, length=expected_length)
            if isinstance(source, PreparedUpload)
            else iter(source)
        )
        self._expected_length = expected_length
        self._buffer = bytearray()
        self._exhausted = False
        self._accepted = 0
        self.total = 0
        self._digest = hashlib.sha256()

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    def read(self, amount: int = -1) -> bytes:
        if amount == 0:
            return b""
        if self._exhausted and not self._buffer:
            return b""
        requested = MAX_STREAM_CHUNK_BYTES if amount <= 0 else min(amount, MAX_STREAM_CHUNK_BYTES)
        while len(self._buffer) < requested and not self._exhausted:
            if self._accepted == self._expected_length:
                self._exhausted = True
                break
            try:
                chunk = next(self._source)
            except StopIteration:
                self._exhausted = True
                raise ValidationError(
                    "stream source ended before its declared Content-Length"
                ) from None
            if not isinstance(chunk, bytes):
                raise ValidationError("stream source returned a non-binary chunk")
            if not chunk:
                raise ValidationError("stream source returned an empty chunk")
            if len(chunk) > MAX_STREAM_CHUNK_BYTES:
                raise ValidationError("stream source chunk exceeds its size bound")
            if self._accepted + len(chunk) > self._expected_length:
                raise ValidationError("stream source exceeds its declared Content-Length")
            self._accepted += len(chunk)
            self._buffer.extend(chunk)
        if not self._buffer:
            return b""
        block = bytes(self._buffer[:requested])
        del self._buffer[: len(block)]
        self.total += len(block)
        self._digest.update(block)
        return block

    def verify_complete(self) -> None:
        if (
            self._buffer
            or self._accepted != self._expected_length
            or self.total != self._expected_length
        ):
            raise ValidationError("stream source byte count does not match Content-Length")


def _stream_target(
    origin: ConnectorOrigin,
    *,
    path: str | None,
    location: str | None,
) -> str:
    if (path is None) == (location is None):
        raise ValidationError("stream target must be one provider path or location")
    if location is not None:
        return _provider_location(origin, location)
    assert path is not None
    return _ORIGIN_BASES[_origin(origin)] + _relative_path(path)


def _stream_query(
    target: str,
    *,
    query: Sequence[tuple[str, str]],
    location: str | None,
) -> str:
    if location is not None and query:
        raise ValidationError("response-derived stream locations do not accept extra query items")
    encoded = _query(query)
    return f"{target}?{encoded}" if encoded else target


def _validate_upload_stream_credential(
    *,
    path: str | None,
    location: str | None,
    credential: ConnectorCredential | None,
) -> None:
    if path is not None and credential is None:
        raise ValidationError("fixed provider upload paths require a credential")
    if location is not None and credential is not None:
        raise ValidationError("response-derived transfer locations do not accept credentials")


def _validate_download_stream_credential(
    *,
    origin: ConnectorOrigin,
    path: str | None,
    location: str | None,
    credential: ConnectorCredential | None,
) -> None:
    if path is not None and credential is None:
        raise ValidationError("fixed provider download paths require a credential")
    if location is None:
        return
    if origin is ConnectorOrigin.SLACK:
        if credential is None:
            raise ValidationError("Slack private file downloads require a credential")
        return
    if credential is not None:
        raise ValidationError("signed provider download locations do not accept credentials")


def _stream_length(
    source: Iterable[bytes] | PreparedUpload,
    value: int | None,
) -> int:
    if value is None and isinstance(source, PreparedUpload):
        value = source.size
    if value is None:
        raise ValidationError("stream Content-Length is required")
    return _stream_length_value(value, label="stream Content-Length")


def _prepared_upload_length(
    source: PreparedUpload,
    *,
    content_length: int | None,
    byte_offset: int,
    total_length: int | None,
) -> int:
    if type(byte_offset) is not int or not 0 <= byte_offset <= source.size:
        raise ValidationError("prepared upload byte offset is invalid")
    if total_length is not None and total_length != source.size:
        raise ValidationError("prepared upload total length does not match its snapshot")
    length = (
        source.size - byte_offset
        if content_length is None
        else _stream_length_value(
            content_length,
            label="stream Content-Length",
        )
    )
    if byte_offset + length > source.size:
        raise ValidationError("prepared upload range exceeds its snapshot")
    return length


def _resume_next_offset(
    *,
    status: int,
    headers: Mapping[str, str],
    byte_offset: int,
    content_length: int,
) -> int | None:
    if status != 308:
        return None
    acknowledged = headers.get("range")
    match = _RANGE.fullmatch(acknowledged) if acknowledged is not None else None
    expected_end = byte_offset + content_length - 1
    if (
        content_length == 0
        or match is None
        or int(match.group(1)) != 0
        or int(match.group(2)) != expected_end
    ):
        raise ValidationError("provider resumable acknowledgement does not match the sent range")
    return expected_end + 1


def _stream_length_value(value: object, *, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_STREAM_BODY_BYTES:
        raise ValidationError(f"{label} is invalid")
    return value


def _stream_bound(value: object) -> int:
    return _stream_length_value(value, label="stream size bound")


def _stream_upload_headers(
    *,
    length: int,
    content_type: str | None,
    byte_offset: int,
    total_length: int | None,
) -> dict[str, str]:
    if type(byte_offset) is not int or byte_offset < 0:
        raise ValidationError("stream byte offset is invalid")
    headers = {
        "Accept": "application/json",
        "Content-Length": str(length),
    }
    if content_type is not None:
        _header_value(content_type)
        headers["Content-Type"] = content_type
    if total_length is not None:
        _stream_length_value(total_length, label="upload total length")
        if total_length < byte_offset + length:
            raise ValidationError("upload range exceeds its total length")
        if length == 0:
            raise ValidationError("upload range cannot contain an empty chunk")
        headers["Content-Range"] = f"bytes {byte_offset}-{byte_offset + length - 1}/{total_length}"
    elif byte_offset != 0:
        raise ValidationError("non-zero upload offset requires a total length")
    return headers


def _validate_download_status(status: int, *, range_requested: bool) -> None:
    if range_requested and status != 206:
        raise ValidationError("provider ignored the requested download range")
    if not range_requested and status == 206:
        raise ValidationError("provider returned an unsolicited partial download")


def _download_range_header(
    headers: dict[str, str],
    *,
    range_start: int | None,
    range_end: int | None,
) -> int | None:
    if (range_start is None) != (range_end is None):
        raise ValidationError("download range requires both endpoints")
    if range_start is None or range_end is None:
        return None
    if (
        type(range_start) is not int
        or type(range_end) is not int
        or range_start < 0
        or range_end < range_start
        or range_end - range_start + 1 > MAX_STREAM_BODY_BYTES
    ):
        raise ValidationError("download range is invalid")
    headers["Range"] = f"bytes={range_start}-{range_end}"
    return range_end - range_start + 1


def _declared_content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("content-length")
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValidationError("provider download Content-Length is invalid")
    return _stream_length_value(int(value), label="provider download Content-Length")


def _validate_download_content_range(
    headers: Mapping[str, str],
    *,
    range_start: int | None,
    range_end: int | None,
    expected_count: int | None,
) -> None:
    value = headers.get("content-range")
    if range_start is None or range_end is None:
        return
    if value is None:
        raise ValidationError("provider ranged download has no Content-Range")
    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)", value)
    if match is None:
        raise ValidationError("provider Content-Range is invalid")
    start, end, total = match.groups()
    start_value = int(start)
    end_value = int(end)
    if (
        start_value != range_start
        or end_value != range_end
        or end_value < start_value
        or (expected_count is not None and end_value - start_value + 1 != expected_count)
        or (total != "*" and int(total) <= end_value)
    ):
        raise ValidationError("provider Content-Range does not match the requested range")


def _response_status(response: ResponseLike) -> int:
    status = response.status
    if type(status) is not int or not 100 <= status <= 599:
        raise ContinuityError("provider response status is invalid")
    return status


def _bounded_stream_error_read(response: ResponseLike) -> bytes:
    try:
        return response.read(MAX_ERROR_BODY_BYTES + 1)[:MAX_ERROR_BODY_BYTES]
    except OSError:
        return b""


def _write_sink(sink: object, block: bytes) -> None:
    writer = getattr(sink, "write", None)
    if not callable(writer):
        raise ValidationError("download sink is not writable")
    result = writer(block)
    if result is not None and result != len(block):
        raise ValidationError("download sink wrote an unexpected byte count")


def _finish_sink(sink: object) -> ArtifactReceipt | None:
    finish = getattr(sink, "finish", None)
    if finish is None:
        return None
    if not callable(finish):
        raise ValidationError("download sink completion is invalid")
    result = finish()
    if result is None or isinstance(result, ArtifactReceipt):
        return result
    raise ValidationError("download sink returned an invalid completion receipt")


def _abort_sink(sink: object) -> None:
    abort = getattr(sink, "abort", None)
    if callable(abort):
        with suppress(Exception):
            abort()


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
    clean_origin = _origin(origin)
    host_allowed = host in _DYNAMIC_HOSTS[clean_origin] or (
        host is not None
        and any(
            host.endswith(suffix) and host != suffix[1:]
            for suffix in _DYNAMIC_HOST_SUFFIXES[clean_origin]
        )
    )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("connector provider location is invalid") from exc
    if (
        parsed.scheme != "https"
        or not host_allowed
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValidationError("connector provider location is outside its pinned origin")
    assert host is not None
    canonical = SplitResult("https", host, parsed.path, parsed.query, "")
    return urlunsplit(canonical)


def _credentialless_download_redirect(
    origin: ConnectorOrigin,
    *,
    from_fixed_path: bool,
    already_redirected: bool,
    status: int,
    headers: Mapping[str, str],
) -> str | None:
    if (
        not from_fixed_path
        or already_redirected
        or status not in {302, 303, 307, 308}
        or origin not in {ConnectorOrigin.GOOGLE, ConnectorOrigin.MICROSOFT_GRAPH}
    ):
        return None
    location = headers.get("location")
    if location is None:
        raise ValidationError("provider download redirect has no safe Location")
    return _provider_location(origin, location)


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
        normalized = name.casefold() if isinstance(name, str) else None
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and normalized in _SAFE_RESPONSE_HEADERS
        ):
            assert normalized is not None
            if normalized in result:
                raise ValidationError("provider response contains a duplicate safety header")
            result[normalized] = value[:8_192]
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
