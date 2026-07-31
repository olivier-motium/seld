from __future__ import annotations

import io
from collections.abc import Callable
from email.message import Message
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorOutcomeUnknown,
    ConnectorProviderError,
    ConnectorResponse,
    ConnectorTransport,
    ResponseLike,
)
from continuity_kernel.errors import ContinuityError, ValidationError


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        headers_value = Message()
        for name, value in (headers or {}).items():
            headers_value[name] = value
        self.headers: object = headers_value
        self._body = io.BytesIO(body)

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        del args


def _credential(
    scheme: AuthorizationScheme = AuthorizationScheme.BEARER,
) -> ConnectorCredential:
    return ConnectorCredential(scheme=scheme, secret="top-secret-provider-token")


def test_fixed_origin_request_builds_authorization_internally_and_filters_headers() -> None:
    captured: list[tuple[Request, float]] = []

    def open_request(request: Request, timeout: float) -> ResponseLike:
        captured.append((request, timeout))
        return _Response(
            b'{"messages":[]}',
            headers={"ETag": '"one"', "Set-Cookie": "must-not-escape"},
        )

    transport = ConnectorTransport(opener=open_request)
    response = transport.request(
        origin=ConnectorOrigin.GMAIL,
        method=ConnectorMethod.GET,
        path="/gmail/v1/users/me/messages",
        credential=_credential(),
        query=(("maxResults", "50"), ("q", "from:alice@example.com")),
    )

    assert len(captured) == 1
    request, timeout = captured[0]
    assert request.full_url == (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        "?maxResults=50&q=from%3Aalice%40example.com"
    )
    assert request.get_header("Authorization") == "Bearer top-secret-provider-token"
    assert timeout == 30.0
    assert response.headers == {"etag": '"one"'}
    assert response.json() == {"messages": []}
    assert "top-secret-provider-token" not in repr(_credential())
    assert "top-secret-provider-token" not in repr(response)


def test_google_oidc_origin_is_pinned_for_userinfo_requests() -> None:
    captured: list[Request] = []

    def open_request(request: Request, timeout: float) -> ResponseLike:
        del timeout
        captured.append(request)
        return _Response(b'{"sub":"subject"}')

    response = ConnectorTransport(opener=open_request).request(
        origin=ConnectorOrigin.GOOGLE_OIDC,
        method=ConnectorMethod.GET,
        path="/v1/userinfo",
        credential=_credential(),
    )

    assert response.json() == {"sub": "subject"}
    assert captured[0].full_url == "https://openidconnect.googleapis.com/v1/userinfo"
    assert captured[0].get_header("Authorization") == "Bearer top-secret-provider-token"


def test_transport_rejects_caller_transport_escape_and_wrong_auth_scheme() -> None:
    transport = ConnectorTransport(opener=lambda request, timeout: _Response(b"{}"))
    with pytest.raises(ValidationError, match="path"):
        transport.request(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path="https://attacker.invalid/steal",
            credential=_credential(),
        )
    with pytest.raises(ValidationError, match="bot authorization"):
        transport.request(
            origin=ConnectorOrigin.DISCORD,
            method=ConnectorMethod.GET,
            path="/api/v10/users/@me",
            credential=_credential(),
        )
    with pytest.raises(ValidationError, match="bearer authorization"):
        transport.request(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.GET,
            path="/api/auth.test",
            credential=_credential(AuthorizationScheme.BOT),
        )


def test_transport_allows_only_well_formed_byte_range_headers() -> None:
    captured: list[Request] = []

    def open_request(request: Request, timeout: float) -> ResponseLike:
        del timeout
        captured.append(request)
        return _Response(b"partial", status=206, headers={"Content-Range": "bytes 5-11/12"})

    transport = ConnectorTransport(opener=open_request)
    response = transport.request(
        origin=ConnectorOrigin.GOOGLE,
        method=ConnectorMethod.GET,
        path="/drive/v3/files/file",
        credential=_credential(),
        headers={"Range": "bytes=5-11"},
        expected_statuses=frozenset({206}),
    )
    assert captured[0].get_header("Range") == "bytes=5-11"
    assert response.headers["content-range"] == "bytes 5-11/12"

    with pytest.raises(ValidationError, match="range"):
        transport.request(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path="/drive/v3/files/file",
            credential=_credential(),
            headers={"Range": "bytes=11-5"},
        )
    with pytest.raises(ValidationError, match="header"):
        transport.request(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path="/drive/v3/files/file",
            credential=_credential(),
            headers={"X-Forwarded-Host": "attacker.invalid"},
        )


def test_provider_location_is_response_bound_to_an_exact_https_host() -> None:
    captured: list[Request] = []

    def open_request(request: Request, timeout: float) -> ResponseLike:
        del timeout
        captured.append(request)
        return _Response(b"uploaded", status=200)

    transport = ConnectorTransport(opener=open_request)
    response = transport.request_provider_location(
        origin=ConnectorOrigin.SLACK,
        method=ConnectorMethod.POST,
        location="https://files.slack.com/upload/v1/opaque?ticket=one",
        credential=None,
        body=b"content",
        content_type="application/octet-stream",
    )
    assert response.body == b"uploaded"
    assert captured[0].get_header("Authorization") is None

    with pytest.raises(ValidationError, match="pinned origin"):
        transport.request_provider_location(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.POST,
            location="https://files.slack.com.attacker.invalid/upload",
            credential=None,
            body=b"content",
        )


def test_provider_error_is_bounded_and_mutation_transport_is_never_retried() -> None:
    headers = Message()
    headers["Retry-After"] = "30"

    def provider_error(request: Request, timeout: float) -> ResponseLike:
        del request, timeout
        raise HTTPError(
            "https://slack.com/api/chat.postMessage",
            429,
            "rate limited",
            headers,
            io.BytesIO(b'{"ok":false,"error":"ratelimited"}'),
        )

    with pytest.raises(ConnectorProviderError) as caught:
        ConnectorTransport(opener=provider_error).request(
            origin=ConnectorOrigin.SLACK,
            method=ConnectorMethod.POST,
            path="/api/chat.postMessage",
            credential=_credential(),
            json_body={"channel": "C1", "text": "hello"},
            expected_statuses=frozenset({200}),
        )
    assert caught.value.status == 429
    assert caught.value.code == "ratelimited"
    assert caught.value.retry_after == "30"

    calls = 0

    def fail_after_dispatch(request: Request, timeout: float) -> ResponseLike:
        nonlocal calls
        del request, timeout
        calls += 1
        raise URLError("connection reset")

    with pytest.raises(ConnectorOutcomeUnknown, match="not retried"):
        ConnectorTransport(opener=fail_after_dispatch).request(
            origin=ConnectorOrigin.MICROSOFT_GRAPH,
            method=ConnectorMethod.POST,
            path="/v1.0/me/sendMail",
            credential=_credential(),
            json_body={"message": {}},
            expected_statuses=frozenset({202}),
        )
    assert calls == 1


def test_read_transport_failure_is_retryable_but_response_bounds_fail_closed() -> None:
    def unavailable(request: Request, timeout: float) -> ResponseLike:
        del request, timeout
        raise URLError("offline")

    with pytest.raises(ContinuityError, match="transport failed"):
        ConnectorTransport(opener=unavailable).request(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path="/drive/v3/files",
            credential=_credential(),
        )

    transport = ConnectorTransport(opener=lambda request, timeout: _Response(b"12345"))
    with pytest.raises(ContinuityError, match="exceeds"):
        transport.request(
            origin=ConnectorOrigin.GOOGLE,
            method=ConnectorMethod.GET,
            path="/drive/v3/files",
            credential=_credential(),
            response_bound=4,
        )


def test_response_json_rejects_duplicate_keys_without_echoing_body() -> None:
    response = ConnectorResponse(
        origin=ConnectorOrigin.DISCORD,
        status=200,
        headers={},
        body=b'{"id":"one","id":"two"}',
    )
    with pytest.raises(ConnectorProviderError) as caught:
        response.json()
    assert caught.value.origin is ConnectorOrigin.DISCORD
    assert caught.value.code == "invalid_json_response"


def test_test_opener_signature_remains_exact() -> None:
    opener = cast(
        Callable[[Request, float], ResponseLike], lambda request, timeout: _Response(b"{}")
    )
    assert ConnectorTransport(opener=opener)
