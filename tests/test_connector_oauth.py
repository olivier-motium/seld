from __future__ import annotations

import http.client
import json
import socket
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from continuity_kernel.connector_credentials import OAuthCredential, credential_from_token_set
from continuity_kernel.connector_oauth import (
    OAuthCallbackError,
    OAuthClientConfig,
    OAuthConfigurationError,
    OAuthTokenEndpointError,
    OAuthTokenSet,
    OAuthTokenType,
    OAuthTransportError,
    PKCEPair,
    build_authorization_url,
    exchange_authorization_code,
    generate_pkce_pair,
    generate_state,
    pkce_pair_from_verifier,
    refresh_access_token,
    validate_authorization_callback,
)
from continuity_kernel.connector_oauth_loopback import BoundLoopbackCallback, begin_authorization
from continuity_kernel.errors import ValidationError


@dataclass
class RecordingTransport:
    response: tuple[int, bytes]
    endpoint: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None

    def __call__(
        self,
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        self.endpoint = endpoint
        self.fields = fields
        self.timeout_seconds = timeout_seconds
        return self.response


@pytest.fixture
def oauth_config() -> OAuthClientConfig:
    return OAuthClientConfig(
        authorization_endpoint="https://accounts.example/authorize?prompt=consent",
        token_endpoint="https://accounts.example/token",
        client_id="seld-public-client",
        redirect_uri="http://127.0.0.1:49152/oauth/callback",
        scopes=("messages.read", "profile"),
    )


def token_response(**overrides: object) -> tuple[int, bytes]:
    payload: dict[str, object] = {
        "access_token": "new-access-token",
        "token_type": "Bearer",
        "refresh_token": "new-refresh-token",
        "expires_in": 3600,
        "scope": "messages.read profile",
    }
    payload.update(overrides)
    return 200, json.dumps(payload).encode()


def test_pkce_s256_matches_rfc_7636_example() -> None:
    pair = pkce_pair_from_verifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")

    assert pair.code_challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_pkce_has_valid_entropy_and_no_padding() -> None:
    pair = generate_pkce_pair()

    assert 43 <= len(pair.code_verifier) <= 128
    assert len(pair.code_challenge) == 43
    assert "=" not in pair.code_challenge


def test_authorization_request_has_state_pkce_and_existing_provider_query(
    oauth_config: OAuthClientConfig,
) -> None:
    state = generate_state()
    pkce = generate_pkce_pair()
    url = build_authorization_url(oauth_config, state=state, pkce=pkce)
    query = parse_qs(urlsplit(url).query)

    assert query == {
        "client_id": ["seld-public-client"],
        "code_challenge": [pkce.code_challenge],
        "code_challenge_method": ["S256"],
        "prompt": ["consent"],
        "redirect_uri": ["http://127.0.0.1:49152/oauth/callback"],
        "response_type": ["code"],
        "scope": ["messages.read profile"],
        "state": [state],
    }


def test_authorization_url_rejects_a_mismatched_pkce_challenge(
    oauth_config: OAuthClientConfig,
) -> None:
    verifier = "a" * 43

    with pytest.raises(OAuthConfigurationError, match="S256 verifier digest"):
        build_authorization_url(
            oauth_config,
            state="expected-state",
            pkce=PKCEPair(code_verifier=verifier, code_challenge="wrong"),
        )


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://127.0.0.1:49152/oauth/callback",
        "http://localhost:49152/oauth/callback",
        "http://127.0.0.1/oauth/callback",
        "http://127.0.0.1:49152/oauth/callback?fixed=query",
    ],
)
def test_configuration_requires_an_exact_loopback_ip_redirect(redirect_uri: str) -> None:
    with pytest.raises(OAuthConfigurationError, match="loopback IP URI"):
        OAuthClientConfig(
            authorization_endpoint="https://accounts.example/authorize",
            token_endpoint="https://accounts.example/token",
            client_id="client",
            redirect_uri=redirect_uri,
        )


def test_ipv6_loopback_callback_binds_and_completes_when_supported() -> None:
    if not socket.has_ipv6:
        pytest.skip("IPv6 is unavailable on this host")
    listener = BoundLoopbackCallback.bind(host="::1", port=0, path="/oauth/callback")
    config = OAuthClientConfig(
        authorization_endpoint="https://accounts.example/authorize",
        token_endpoint="https://accounts.example/token",
        client_id="seld-public-client",
        redirect_uri=listener.redirect_uri,
    )
    attempt = begin_authorization(config)
    listener.configure(config, attempt)
    redirect = urlsplit(listener.redirect_uri)
    assert redirect.port is not None

    def callback() -> None:
        connection = http.client.HTTPConnection("::1", redirect.port, timeout=5)
        connection.request(
            "GET",
            f"{redirect.path}?code=ipv6-code&state={attempt.state}",
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()

    thread = threading.Thread(target=callback)
    thread.start()
    try:
        assert listener.wait_for_code(timeout_seconds=5) == "ipv6-code"
    finally:
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_callback_returns_code_only_after_exact_target_and_state_validation(
    oauth_config: OAuthClientConfig,
) -> None:
    result = validate_authorization_callback(
        oauth_config,
        callback_url=("http://127.0.0.1:49152/oauth/callback?code=one-time-code&state=expected"),
        expected_state="expected",
    )

    assert result == "one-time-code"


@pytest.mark.parametrize(
    ("callback_url", "message"),
    [
        (
            "http://127.0.0.1:49153/oauth/callback?code=code&state=expected",
            "target does not match",
        ),
        (
            "http://127.0.0.1:49152/other?code=code&state=expected",
            "target does not match",
        ),
        (
            "http://127.0.0.1:49152/oauth/callback?code=code&state=wrong",
            "state does not match",
        ),
        (
            "http://127.0.0.1:49152/oauth/callback?code=one&code=two&state=expected",
            "duplicate code",
        ),
        (
            "http://127.0.0.1:49152/oauth/callback?error=access_denied&state=expected",
            "access_denied",
        ),
    ],
)
def test_callback_fails_closed(
    oauth_config: OAuthClientConfig,
    callback_url: str,
    message: str,
) -> None:
    with pytest.raises(OAuthCallbackError, match=message):
        validate_authorization_callback(
            oauth_config,
            callback_url=callback_url,
            expected_state="expected",
        )


def test_authorization_code_exchange_posts_the_exact_public_client_form(
    oauth_config: OAuthClientConfig,
) -> None:
    transport = RecordingTransport(token_response())
    verifier = "v" * 64

    result = exchange_authorization_code(
        oauth_config,
        authorization_code="one-time-code",
        code_verifier=verifier,
        timeout_seconds=7.5,
        post_form=transport,
    )

    assert transport.endpoint == "https://accounts.example/token"
    assert transport.timeout_seconds == 7.5
    assert transport.fields == {
        "client_id": "seld-public-client",
        "code": "one-time-code",
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": "http://127.0.0.1:49152/oauth/callback",
    }
    assert result.access_token == "new-access-token"
    assert result.token_type is OAuthTokenType.BEARER
    assert result.refresh_token == "new-refresh-token"
    assert result.expires_in_seconds == 3600
    assert result.scopes == ("messages.read", "profile")


def test_refresh_preserves_old_refresh_token_only_when_provider_omits_it(
    oauth_config: OAuthClientConfig,
) -> None:
    response = token_response()
    payload = json.loads(response[1])
    del payload["refresh_token"]
    transport = RecordingTransport((200, json.dumps(payload).encode()))

    result = refresh_access_token(
        oauth_config,
        refresh_token="old-refresh-token",
        scopes=("messages.read",),
        post_form=transport,
    )

    assert transport.fields == {
        "client_id": "seld-public-client",
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh-token",
        "scope": "messages.read",
    }
    assert result.refresh_token == "old-refresh-token"


def test_refresh_uses_rotated_refresh_token(oauth_config: OAuthClientConfig) -> None:
    result = refresh_access_token(
        oauth_config,
        refresh_token="old-refresh-token",
        post_form=RecordingTransport(token_response(refresh_token="rotated-refresh-token")),
    )

    assert result.refresh_token == "rotated-refresh-token"


def test_refresh_rejects_present_but_empty_replacement(oauth_config: OAuthClientConfig) -> None:
    with pytest.raises(OAuthTransportError, match="invalid refresh_token"):
        refresh_access_token(
            oauth_config,
            refresh_token="old-refresh-token",
            post_form=RecordingTransport(token_response(refresh_token="")),
        )


def test_token_endpoint_error_is_typed_without_exposing_description_in_message(
    oauth_config: OAuthClientConfig,
) -> None:
    transport = RecordingTransport(
        (
            400,
            json.dumps(
                {"error": "invalid_grant", "error_description": "sensitive provider detail"}
            ).encode(),
        )
    )

    with pytest.raises(OAuthTokenEndpointError) as failure:
        exchange_authorization_code(
            oauth_config,
            authorization_code="expired-code",
            code_verifier="v" * 64,
            post_form=transport,
        )

    assert failure.value.error == "invalid_grant"
    assert failure.value.description == "sensitive provider detail"
    assert "sensitive provider detail" not in str(failure.value)


@pytest.mark.parametrize(
    "response",
    [
        (200, b"not-json"),
        (200, b"[]"),
        token_response(access_token=""),
        token_response(token_type="MAC"),
        token_response(expires_in=-1),
    ],
)
def test_invalid_token_responses_fail_closed(
    oauth_config: OAuthClientConfig,
    response: tuple[int, bytes],
) -> None:
    with pytest.raises(OAuthTransportError):
        exchange_authorization_code(
            oauth_config,
            authorization_code="code",
            code_verifier="v" * 64,
            post_form=RecordingTransport(response),
        )


def test_persisted_oauth_credential_has_absolute_expiry_and_strict_shape() -> None:
    issued_at = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
    credential = credential_from_token_set(
        OAuthTokenSet(
            access_token="persisted-access",
            token_type=OAuthTokenType.BEARER,
            refresh_token="persisted-refresh",
            expires_in_seconds=3600,
            scopes=("messages.read",),
        ),
        issued_at=issued_at,
    )

    assert OAuthCredential.from_bytes(credential.to_bytes()) == credential
    assert credential.expires_at == datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    assert "persisted-access" not in repr(credential)
    malformed = json.loads(credential.to_bytes())
    malformed["provider_payload"] = "forbidden"
    with pytest.raises(ValidationError, match="unsupported shape"):
        OAuthCredential.from_bytes(json.dumps(malformed).encode())
