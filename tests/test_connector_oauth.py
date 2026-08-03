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
    OAuthAuthorizationRejectedError,
    OAuthCallbackError,
    OAuthClientConfig,
    OAuthConfigurationError,
    OAuthDialect,
    OAuthStateMismatchError,
    OAuthTokenEndpointError,
    OAuthTokenSet,
    OAuthTokenType,
    OAuthTransportError,
    PKCEPair,
    build_authorization_url,
    canonicalize_google_scopes,
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
        "http://example.test:49152/oauth/callback",
        "http://127.0.0.1/oauth/callback",
        "http://127.0.0.1:49152/oauth/callback?fixed=query",
    ],
)
def test_configuration_requires_an_exact_loopback_redirect(redirect_uri: str) -> None:
    with pytest.raises(OAuthConfigurationError, match="loopback URI"):
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


@pytest.mark.parametrize("callback_host", ["127.0.0.1", "::1"])
def test_localhost_redirect_listens_on_both_loopback_families(callback_host: str) -> None:
    if callback_host == "::1" and not socket.has_ipv6:
        pytest.skip("IPv6 is unavailable on this host")
    listener = BoundLoopbackCallback.bind(
        host="localhost",
        port=0,
        path="/oauth/callback",
    )
    config = OAuthClientConfig(
        authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.user.access",
        client_id="seld-public-client",
        redirect_uri=listener.redirect_uri,
        dialect=OAuthDialect.SLACK_USER,
    )
    attempt = begin_authorization(config)
    listener.configure(config, attempt)
    redirect = urlsplit(listener.redirect_uri)
    assert redirect.hostname == "localhost"
    assert redirect.port is not None

    def callback() -> None:
        connection = http.client.HTTPConnection(callback_host, redirect.port, timeout=5)
        connection.request(
            "GET",
            f"{redirect.path}?code=localhost-code&state={attempt.state}",
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()

    thread = threading.Thread(target=callback)
    thread.start()
    try:
        assert listener.wait_for_code(timeout_seconds=5) == "localhost-code"
    finally:
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "invalid_query",
    [
        "code=rejected-one&code=rejected-two&state=wrong-state",
        "code=rejected-one&code=rejected-two",
        "code=rejected-one&code=rejected-two&state=wrong-state&state=expected",
    ],
)
def test_state_mismatch_does_not_latch_listener_before_valid_callback(
    invalid_query: str,
) -> None:
    listener = BoundLoopbackCallback.bind(host="127.0.0.1", port=0, path="/oauth/callback")
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
    responses: list[int] = []

    def callback() -> None:
        for query in (invalid_query, f"code=valid-code&state={attempt.state}"):
            connection = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=5)
            connection.request("GET", f"{redirect.path}?{query}")
            response = connection.getresponse()
            responses.append(response.status)
            response.read()
            connection.close()

    thread = threading.Thread(target=callback)
    thread.start()
    try:
        assert listener.wait_for_code(timeout_seconds=5) == "valid-code"
    finally:
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert responses == [400, 200]


def test_valid_state_duplicate_code_latches_listener_as_terminal() -> None:
    listener = BoundLoopbackCallback.bind(host="127.0.0.1", port=0, path="/oauth/callback")
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
    responses: list[int] = []

    def callback() -> None:
        connection = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=5)
        connection.request(
            "GET",
            f"{redirect.path}?code=one&code=two&state={attempt.state}",
        )
        response = connection.getresponse()
        responses.append(response.status)
        response.read()
        connection.close()

    thread = threading.Thread(target=callback)
    thread.start()
    try:
        with pytest.raises(OAuthCallbackError, match="duplicate code"):
            listener.wait_for_code(timeout_seconds=5)
    finally:
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert responses == [400]


def test_fixed_oauth_callback_port_collision_fails_cleanly() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    port = int(occupied.getsockname()[1])
    try:
        with pytest.raises(ValidationError, match="registered port"):
            BoundLoopbackCallback.bind(
                host="localhost",
                port=port,
                path="/oauth/callback",
            )
    finally:
        occupied.close()


def test_callback_returns_code_only_after_exact_target_and_state_validation(
    oauth_config: OAuthClientConfig,
) -> None:
    result = validate_authorization_callback(
        oauth_config,
        callback_url=("http://127.0.0.1:49152/oauth/callback?code=one-time-code&state=expected"),
        expected_state="expected",
    )

    assert result == "one-time-code"


def test_missing_or_mismatched_state_uses_a_specific_callback_error(
    oauth_config: OAuthClientConfig,
) -> None:
    for callback_url in (
        "http://127.0.0.1:49152/oauth/callback?code=code",
        "http://127.0.0.1:49152/oauth/callback?code=code&state=wrong",
        "http://127.0.0.1:49152/oauth/callback?code=code&state=wrong&state=expected",
        "http://127.0.0.1:49152/oauth/callback?code=one&code=two",
        "http://127.0.0.1:49152/oauth/callback?code=one&code=two&state=wrong",
        ("http://127.0.0.1:49152/oauth/callback?code=one&code=two&state=wrong&state=expected"),
    ):
        with pytest.raises(OAuthStateMismatchError):
            validate_authorization_callback(
                oauth_config,
                callback_url=callback_url,
                expected_state="expected",
            )


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


def test_access_denied_callback_has_a_specific_recoverable_error(
    oauth_config: OAuthClientConfig,
) -> None:
    with pytest.raises(
        OAuthAuthorizationRejectedError,
        match=r"OAuth authorization was rejected \(access_denied\)",
    ):
        validate_authorization_callback(
            oauth_config,
            callback_url=(
                "http://127.0.0.1:49152/oauth/callback?error=access_denied&state=expected"
            ),
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


def test_initial_scope_omission_is_not_globally_replaced() -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://accounts.example/authorize",
        token_endpoint="https://accounts.example/token",
        client_id="seld-public-client",
        redirect_uri="http://127.0.0.1:49152/oauth/callback",
        scopes=("messages.read",),
    )

    result = exchange_authorization_code(
        config,
        authorization_code="one-time-code",
        code_verifier="v" * 64,
        post_form=RecordingTransport(
            (200, b'{"access_token":"new-access-token","token_type":"Bearer"}')
        ),
    )

    assert result.scopes is None


def test_google_authorization_requires_a_refreshable_offline_grant() -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        client_id="public-google-client",
        redirect_uri="http://127.0.0.1:49152",
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        dialect=OAuthDialect.GOOGLE,
        client_secret="desktop-client-secret",
    )
    pkce = pkce_pair_from_verifier("v" * 64)
    query = parse_qs(urlsplit(build_authorization_url(config, state="expected", pkce=pkce)).query)

    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent select_account"]
    with pytest.raises(OAuthTransportError, match="refresh_token"):
        transport = RecordingTransport(
            (
                200,
                b'{"access_token":"temporary","token_type":"Bearer",'
                b'"scope":"https://www.googleapis.com/auth/gmail.readonly",'
                b'"expires_in":3600}',
            )
        )
        exchange_authorization_code(
            config,
            authorization_code="one-time-code",
            code_verifier=pkce.code_verifier,
            post_form=transport,
        )
    assert transport.fields["client_secret"] == "desktop-client-secret"
    assert "desktop-client-secret" not in repr(config)

    refresh_transport = RecordingTransport(token_response())
    refresh_access_token(
        config,
        refresh_token="existing-refresh-token",
        post_form=refresh_transport,
    )
    assert refresh_transport.fields["client_secret"] == "desktop-client-secret"


def test_google_userinfo_email_scope_is_canonicalized_during_token_parsing() -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        client_id="public-google-client",
        redirect_uri="http://127.0.0.1:49152",
        scopes=("openid", "email", "https://www.googleapis.com/auth/gmail.readonly"),
        dialect=OAuthDialect.GOOGLE,
    )
    token_set = exchange_authorization_code(
        config,
        authorization_code="one-time-code",
        code_verifier="v" * 64,
        post_form=RecordingTransport(
            (
                200,
                json.dumps(
                    {
                        "access_token": "google-access",
                        "refresh_token": "google-refresh",
                        "token_type": "Bearer",
                        "scope": (
                            "openid https://www.googleapis.com/auth/userinfo.email "
                            "https://www.googleapis.com/auth/gmail.readonly"
                        ),
                    }
                ).encode(),
            )
        ),
    )
    credential = credential_from_token_set(
        token_set,
        issued_at=datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
    )

    assert token_set.scopes == (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    assert credential.scopes == token_set.scopes
    assert canonicalize_google_scopes(("userinfo.email",)) == ("userinfo.email",)


def test_microsoft_authorization_uses_only_select_account() -> None:
    config = OAuthClientConfig(
        authorization_endpoint=("https://login.microsoftonline.com/common/oauth2/v2.0/authorize"),
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        client_id="public-microsoft-client",
        redirect_uri="http://127.0.0.1:49152/oauth/callback",
        scopes=("offline_access", "User.Read", "Mail.Read"),
        dialect=OAuthDialect.MICROSOFT,
    )
    query = parse_qs(
        urlsplit(
            build_authorization_url(
                config,
                state="expected",
                pkce=pkce_pair_from_verifier("v" * 64),
            )
        ).query
    )

    assert query["prompt"] == ["select_account"]
    assert "consent select_account" not in query["prompt"]

    response = RecordingTransport(
        (
            200,
            json.dumps(
                {
                    "access_token": "microsoft-access",
                    "refresh_token": "microsoft-refresh",
                    "token_type": "Bearer",
                    "scope": (
                        "openid profile email "
                        "HTTPS://GRAPH.MICROSOFT.COM/user.read "
                        "https://graph.microsoft.com/MAIL.READ offline_access"
                    ),
                }
            ).encode(),
        )
    )
    result = exchange_authorization_code(
        config,
        authorization_code="one-time-code",
        code_verifier="v" * 64,
        post_form=response,
    )

    assert result.scopes == ("User.Read", "Mail.Read")


def test_microsoft_initial_scope_and_refresh_rules_are_provider_exact() -> None:
    config = OAuthClientConfig(
        authorization_endpoint=("https://login.microsoftonline.com/common/oauth2/v2.0/authorize"),
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        client_id="public-microsoft-client",
        redirect_uri="http://127.0.0.1:49152/oauth/callback",
        scopes=("offline_access", "User.Read", "Mail.Read"),
        dialect=OAuthDialect.MICROSOFT,
    )
    token_response_without_scope = RecordingTransport(
        (
            200,
            b'{"access_token":"microsoft-access","token_type":"Bearer",'
            b'"refresh_token":"microsoft-refresh"}',
        )
    )

    result = exchange_authorization_code(
        config,
        authorization_code="one-time-code",
        code_verifier="v" * 64,
        post_form=token_response_without_scope,
    )

    assert result.scopes == ("User.Read", "Mail.Read")
    assert result.refresh_token == "microsoft-refresh"

    without_offline_access = OAuthClientConfig(
        authorization_endpoint=config.authorization_endpoint,
        token_endpoint=config.token_endpoint,
        client_id=config.client_id,
        redirect_uri=config.redirect_uri,
        scopes=("User.Read", "Mail.Read"),
        dialect=OAuthDialect.MICROSOFT,
    )
    result_without_refresh = exchange_authorization_code(
        without_offline_access,
        authorization_code="one-time-code",
        code_verifier="v" * 64,
        post_form=RecordingTransport(
            (200, b'{"access_token":"microsoft-access","token_type":"Bearer"}')
        ),
    )
    assert result_without_refresh.scopes == ("User.Read", "Mail.Read")
    assert result_without_refresh.refresh_token is None

    with pytest.raises(OAuthTransportError, match="refresh_token"):
        exchange_authorization_code(
            config,
            authorization_code="one-time-code",
            code_verifier="v" * 64,
            post_form=RecordingTransport(
                (200, b'{"access_token":"microsoft-access","token_type":"Bearer"}')
            ),
        )


def test_google_initial_grant_requires_a_returned_scope() -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        client_id="public-google-client",
        redirect_uri="http://127.0.0.1:49152",
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        dialect=OAuthDialect.GOOGLE,
    )

    with pytest.raises(OAuthTransportError, match="usable scope"):
        exchange_authorization_code(
            config,
            authorization_code="one-time-code",
            code_verifier="v" * 64,
            post_form=RecordingTransport(
                (
                    200,
                    b'{"access_token":"temporary","token_type":"Bearer",'
                    b'"refresh_token":"refresh","expires_in":3600}',
                )
            ),
        )


def test_slack_user_authorization_and_token_response_use_the_official_dialect() -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.user.access",
        client_id="123.456",
        redirect_uri="http://localhost:49152/oauth/callback",
        scopes=("channels:history", "groups:history"),
        dialect=OAuthDialect.SLACK_USER,
    )
    pkce = pkce_pair_from_verifier("v" * 64)
    query = parse_qs(urlsplit(build_authorization_url(config, state="expected", pkce=pkce)).query)
    assert query["scope"] == ["channels:history,groups:history"]

    transport = RecordingTransport(
        (
            200,
            json.dumps(
                {
                    "ok": True,
                    "access_token": "xoxp-portable-user-token",
                    "token_type": "Bearer",
                    "refresh_token": "xoxe-portable-refresh",
                    "expires_in": 43200,
                    "authed_user": {"scope": "channels:history,groups:history"},
                }
            ).encode(),
        )
    )
    result = exchange_authorization_code(
        config,
        authorization_code="one-time-code",
        code_verifier=pkce.code_verifier,
        post_form=transport,
    )

    assert result.token_type is OAuthTokenType.BEARER
    assert result.scopes == ("channels:history", "groups:history")
    assert result.refresh_token == "xoxe-portable-refresh"

    for incomplete_rotation in (
        b'{"ok":true,"access_token":"xoxp-user","token_type":"user",'
        b'"refresh_token":"xoxe-refresh","authed_user":{"scope":"channels:history"}}',
        b'{"ok":true,"access_token":"xoxp-user","token_type":"user",'
        b'"expires_in":43200,"authed_user":{"scope":"channels:history"}}',
    ):
        with pytest.raises(OAuthTransportError, match="pair refresh_token with expires_in"):
            exchange_authorization_code(
                config,
                authorization_code="one-time-code",
                code_verifier=pkce.code_verifier,
                post_form=RecordingTransport((200, incomplete_rotation)),
            )

    with pytest.raises(OAuthTransportError, match="user scope"):
        exchange_authorization_code(
            config,
            authorization_code="one-time-code",
            code_verifier=pkce.code_verifier,
            post_form=RecordingTransport(
                (200, b'{"ok":true,"access_token":"xoxp-user","token_type":"user"}')
            ),
        )

    with pytest.raises(OAuthConfigurationError, match="fixed port"):
        OAuthClientConfig(
            authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
            token_endpoint="https://slack.com/api/oauth.v2.user.access",
            client_id="123.456",
            redirect_uri="http://localhost:0/oauth/callback",
            dialect=OAuthDialect.SLACK_USER,
        )


def test_slack_user_refresh_uses_rotation_endpoint_and_exact_rotating_shape() -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.user.access",
        client_id="123.456",
        redirect_uri="http://localhost:49152/oauth/callback",
        scopes=("channels:history",),
        dialect=OAuthDialect.SLACK_USER,
    )
    successful = RecordingTransport(
        (
            200,
            b'{"ok":true,"access_token":"fresh","token_type":"user",'
            b'"refresh_token":"xoxe-next","expires_in":43200,'
            b'"scope":"channels:history"}',
        )
    )
    refreshed = refresh_access_token(
        config,
        refresh_token="xoxe-refresh",
        scopes=("channels:history",),
        post_form=successful,
    )
    assert successful.endpoint == "https://slack.com/api/oauth.v2.access"
    assert successful.fields == {
        "client_id": "123.456",
        "grant_type": "refresh_token",
        "refresh_token": "xoxe-refresh",
    }
    assert refreshed.refresh_token == "xoxe-next"
    assert refreshed.expires_in_seconds == 43200
    assert refreshed.scopes == ("channels:history",)

    with pytest.raises(OAuthTokenEndpointError) as failure:
        refresh_access_token(
            config,
            refresh_token="invalid",
            post_form=RecordingTransport((200, b'{"ok":false,"error":"invalid_refresh_token"}')),
        )
    assert failure.value.error == "invalid_refresh_token"


def test_slack_user_refresh_rejects_authed_user_scope_without_top_level_scope() -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.user.access",
        client_id="123.456",
        redirect_uri="http://localhost:49152/oauth/callback",
        scopes=("channels:history",),
        dialect=OAuthDialect.SLACK_USER,
    )

    with pytest.raises(OAuthTransportError, match="user scope"):
        refresh_access_token(
            config,
            refresh_token="xoxe-refresh",
            scopes=("channels:history",),
            post_form=RecordingTransport(
                (
                    200,
                    b'{"ok":true,"access_token":"fresh","token_type":"user",'
                    b'"refresh_token":"xoxe-next","expires_in":43200,'
                    b'"authed_user":{"scope":"channels:history"}}',
                )
            ),
        )


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            b'{"ok":true,"access_token":"fresh","token_type":"user",'
            b'"expires_in":43200,"scope":"channels:history"}',
            "replacement refresh_token",
        ),
        (
            b'{"ok":true,"access_token":"fresh","token_type":"user",'
            b'"refresh_token":"xoxe-consumed","expires_in":43200,'
            b'"scope":"channels:history"}',
            "replacement refresh_token",
        ),
        (
            b'{"ok":true,"access_token":"fresh","token_type":"user",'
            b'"refresh_token":"xoxe-next","scope":"channels:history"}',
            "expires_in",
        ),
        (
            b'{"ok":true,"access_token":"fresh","token_type":"user",'
            b'"refresh_token":"xoxe-next","expires_in":43200}',
            "scope",
        ),
    ],
)
def test_slack_user_refresh_rejects_incomplete_rotating_response(
    response: bytes,
    message: str,
) -> None:
    config = OAuthClientConfig(
        authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.user.access",
        client_id="123.456",
        redirect_uri="http://localhost:49152/oauth/callback",
        scopes=("channels:history",),
        dialect=OAuthDialect.SLACK_USER,
    )

    with pytest.raises(OAuthTransportError, match=message):
        refresh_access_token(
            config,
            refresh_token="xoxe-consumed",
            scopes=("channels:history",),
            post_form=RecordingTransport((200, response)),
        )


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


def test_oauth_expiry_rejects_the_first_second_beyond_datetime_capacity() -> None:
    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    remaining = datetime.max.replace(tzinfo=UTC) - issued_at
    first_overflowing_second = remaining.days * 86_400 + remaining.seconds + 1

    with pytest.raises(ValidationError, match="supported time range"):
        credential_from_token_set(
            OAuthTokenSet(
                access_token="bounded-access",
                token_type=OAuthTokenType.BEARER,
                refresh_token="bounded-refresh",
                expires_in_seconds=first_overflowing_second,
                scopes=("messages.read",),
            ),
            issued_at=issued_at,
        )
