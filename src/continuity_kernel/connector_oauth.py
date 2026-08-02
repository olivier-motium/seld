"""OAuth 2.1 native-client boundary; excludes persistence, OIDC, and servers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from continuity_kernel.errors import ContinuityError

_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
_PKCE_ALLOWED: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_MAX_RESPONSE_BYTES: Final = 1_048_576
_SLACK_REFRESH_ENDPOINT: Final = "https://slack.com/api/oauth.v2.access"
_RESERVED_AUTH_PARAMETERS: Final = frozenset(
    "client_id code_challenge code_challenge_method redirect_uri response_type scope state".split()  # noqa: SIM905
)


class OAuthTokenType(StrEnum):
    BEARER = "Bearer"


class OAuthDialect(StrEnum):
    STANDARD = "standard"
    GOOGLE = "google"
    MICROSOFT = "microsoft"
    SLACK_USER = "slack_user"


class ConnectorOAuthError(ContinuityError): ...


class OAuthConfigurationError(ConnectorOAuthError): ...


class OAuthCallbackError(ConnectorOAuthError): ...


class OAuthAuthorizationRejectedError(OAuthCallbackError):
    def __init__(self, error: str) -> None:
        super().__init__(f"OAuth authorization was rejected ({error})")


class OAuthStateMismatchError(OAuthCallbackError): ...


class OAuthTransportError(ConnectorOAuthError): ...


class OAuthTokenEndpointError(ConnectorOAuthError):
    def __init__(self, *, error: str, description: str | None, status_code: int) -> None:
        self.error = error
        self.description = description
        self.status_code = status_code
        super().__init__(f"OAuth token request rejected ({error}, HTTP {status_code})")


@dataclass(frozen=True)
class PKCEPair:
    code_verifier: str
    code_challenge: str


@dataclass(frozen=True)
class OAuthClientConfig:
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()
    dialect: OAuthDialect = OAuthDialect.STANDARD

    def __post_init__(self) -> None:
        _validate_https_endpoint(self.authorization_endpoint, "authorization endpoint")
        _validate_https_endpoint(self.token_endpoint, "token endpoint")
        if not self.client_id:
            raise OAuthConfigurationError("OAuth client_id must not be empty")
        _validate_loopback_redirect(self.redirect_uri)
        _validate_scopes(self.scopes)
        if self.dialect is OAuthDialect.GOOGLE:
            redirect = urlsplit(self.redirect_uri)
            if redirect.hostname not in {"127.0.0.1", "::1"} or redirect.path:
                raise OAuthConfigurationError(
                    "Google OAuth requires an exact loopback IP redirect without a path"
                )
        elif self.dialect is OAuthDialect.SLACK_USER:
            redirect = urlsplit(self.redirect_uri)
            if (
                redirect.hostname != "localhost"
                or redirect.port in {None, 0}
                or redirect.path != "/oauth/callback"
            ):
                raise OAuthConfigurationError(
                    "Slack OAuth requires an exact registered localhost callback with a fixed port"
                )


@dataclass(frozen=True, repr=False)
class OAuthTokenSet:
    access_token: str
    token_type: OAuthTokenType
    refresh_token: str | None
    expires_in_seconds: int | None
    scopes: tuple[str, ...] | None


class FormPoster(Protocol):
    def __call__(
        self,
        endpoint: str,
        fields: dict[str, str],
        *,
        timeout_seconds: float,
    ) -> tuple[int, bytes]: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def generate_pkce_pair() -> PKCEPair:
    return pkce_pair_from_verifier(secrets.token_urlsafe(64))


def pkce_pair_from_verifier(code_verifier: str) -> PKCEPair:
    _validate_code_verifier(code_verifier)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PKCEPair(code_verifier=code_verifier, code_challenge=challenge)


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorization_url(config: OAuthClientConfig, *, state: str, pkce: PKCEPair) -> str:
    if not state:
        raise OAuthConfigurationError("OAuth state must not be empty")
    expected = pkce_pair_from_verifier(pkce.code_verifier)
    if not hmac.compare_digest(pkce.code_challenge, expected.code_challenge):
        raise OAuthConfigurationError("OAuth PKCE challenge is not the S256 verifier digest")

    endpoint = urlsplit(config.authorization_endpoint)
    existing = parse_qsl(endpoint.query, keep_blank_values=True)
    if any(name in _RESERVED_AUTH_PARAMETERS for name, _value in existing):
        raise OAuthConfigurationError("authorization endpoint contains reserved OAuth parameters")
    parameters = [
        *existing,
        ("response_type", "code"),
        ("client_id", config.client_id),
        ("redirect_uri", config.redirect_uri),
        ("state", state),
        ("code_challenge", pkce.code_challenge),
        ("code_challenge_method", "S256"),
    ]
    if config.scopes:
        separator = "," if config.dialect is OAuthDialect.SLACK_USER else " "
        parameters.append(("scope", separator.join(config.scopes)))
    if config.dialect is OAuthDialect.GOOGLE:
        if any(name in {"access_type", "prompt"} for name, _value in existing):
            raise OAuthConfigurationError(
                "Google authorization endpoint contains reserved provider parameters"
            )
        parameters.extend(
            (
                ("access_type", "offline"),
                ("prompt", "consent select_account"),
            )
        )
    elif config.dialect is OAuthDialect.MICROSOFT:
        if any(name == "prompt" for name, _value in existing):
            raise OAuthConfigurationError(
                "Microsoft authorization endpoint contains reserved provider parameters"
            )
        parameters.append(("prompt", "select_account"))
    return urlunsplit(endpoint._replace(query=urlencode(parameters)))


def validate_authorization_callback(
    config: OAuthClientConfig, *, callback_url: str, expected_state: str
) -> str:
    if not expected_state:
        raise OAuthCallbackError("expected OAuth state must not be empty")
    expected = urlsplit(config.redirect_uri)
    received = urlsplit(callback_url)
    if received.fragment:
        raise OAuthCallbackError("OAuth callback must not contain a fragment")
    received_target = (received.scheme, received.netloc, received.path)
    expected_target = (expected.scheme, expected.netloc, expected.path)
    if received_target != expected_target:
        raise OAuthCallbackError("OAuth callback target does not match the redirect URI")

    state_values = [
        value
        for name, value in parse_qsl(received.query, keep_blank_values=True)
        if name == "state"
    ]
    if len(state_values) != 1 or not hmac.compare_digest(state_values[0], expected_state):
        raise OAuthStateMismatchError("OAuth callback state does not match")
    parameters = _singular_parameters(received.query)
    error = parameters.get("error")
    if error is not None:
        if not error:
            raise OAuthCallbackError("OAuth callback contains an empty error")
        if error == "access_denied":
            raise OAuthAuthorizationRejectedError(error)
        raise OAuthCallbackError(f"OAuth authorization failed ({error})")
    code = parameters.get("code")
    if not code:
        raise OAuthCallbackError("OAuth callback does not contain an authorization code")
    return code


def exchange_authorization_code(
    config: OAuthClientConfig,
    *,
    authorization_code: str,
    code_verifier: str,
    timeout_seconds: float = 15.0,
    post_form: FormPoster | None = None,
) -> OAuthTokenSet:
    if not authorization_code:
        raise OAuthConfigurationError("authorization code must not be empty")
    _validate_code_verifier(code_verifier)
    token_set = _request_token(
        config,
        {
            "grant_type": "authorization_code",
            "client_id": config.client_id,
            "code": authorization_code,
            "redirect_uri": config.redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout_seconds=timeout_seconds,
        post_form=post_form,
        preserved_refresh_token=None,
        allow_slack_authed_user_scope=True,
    )
    if config.dialect is OAuthDialect.SLACK_USER and (
        (token_set.refresh_token is None) != (token_set.expires_in_seconds is None)
    ):
        raise OAuthTransportError(
            "Slack OAuth token response must pair refresh_token with expires_in"
        )
    return token_set


def refresh_access_token(
    config: OAuthClientConfig,
    *,
    refresh_token: str,
    scopes: tuple[str, ...] | None = None,
    timeout_seconds: float = 15.0,
    post_form: FormPoster | None = None,
) -> OAuthTokenSet:
    if not refresh_token:
        raise OAuthConfigurationError("refresh token must not be empty")
    fields = {
        "grant_type": "refresh_token",
        "client_id": config.client_id,
        "refresh_token": refresh_token,
    }
    if scopes is not None and config.dialect is OAuthDialect.STANDARD:
        _validate_scopes(scopes)
        if scopes:
            fields["scope"] = " ".join(scopes)
    request_config = config
    preserved_refresh_token: str | None = refresh_token
    if config.dialect is OAuthDialect.SLACK_USER:
        request_config = replace(config, token_endpoint=_SLACK_REFRESH_ENDPOINT)
        preserved_refresh_token = None
    token_set = _request_token(
        request_config,
        fields,
        timeout_seconds=timeout_seconds,
        post_form=post_form,
        preserved_refresh_token=preserved_refresh_token,
        allow_slack_authed_user_scope=False,
    )
    if config.dialect is OAuthDialect.SLACK_USER:
        if token_set.refresh_token is None or token_set.refresh_token == refresh_token:
            raise OAuthTransportError(
                "Slack OAuth refresh response has no new replacement refresh_token"
            )
        if token_set.expires_in_seconds is None or token_set.expires_in_seconds <= 0:
            raise OAuthTransportError("Slack OAuth refresh response has no usable expires_in")
        if not token_set.scopes:
            raise OAuthTransportError("Slack OAuth refresh response has no usable scope")
    return token_set


def _request_token(
    config: OAuthClientConfig,
    fields: dict[str, str],
    *,
    timeout_seconds: float,
    post_form: FormPoster | None,
    preserved_refresh_token: str | None,
    allow_slack_authed_user_scope: bool,
) -> OAuthTokenSet:
    if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
        raise OAuthConfigurationError("OAuth timeout must be a positive finite number")
    status, body = (post_form or _post_form)(
        config.token_endpoint, fields, timeout_seconds=timeout_seconds
    )
    try:
        payload = _decode_json_object(body)
    except OAuthTransportError as exc:
        if not 200 <= status < 300:
            raise OAuthTokenEndpointError(
                error="token_endpoint_error", description=None, status_code=status
            ) from exc
        raise
    slack_rejected = config.dialect is OAuthDialect.SLACK_USER and payload.get("ok") is not True
    if not 200 <= status < 300 or "error" in payload or slack_rejected:
        error = payload.get("error")
        description = payload.get("error_description")
        raise OAuthTokenEndpointError(
            error=error if isinstance(error, str) and error else "token_endpoint_error",
            description=description if isinstance(description, str) else None,
            status_code=status,
        )
    return _parse_token_set(
        payload,
        preserved_refresh_token,
        config.dialect,
        configured_scopes=config.scopes,
        allow_slack_authed_user_scope=allow_slack_authed_user_scope,
    )


def _post_form(
    endpoint: str, fields: dict[str, str], *, timeout_seconds: float
) -> tuple[int, bytes]:
    request = Request(
        endpoint,
        data=urlencode(fields).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=timeout_seconds) as response:
            return response.status, _read_bounded(response)
    except HTTPError as exc:
        return exc.code, _read_bounded(exc)
    except (URLError, TimeoutError, OSError) as exc:
        raise OAuthTransportError("OAuth token endpoint is unavailable") from exc


def _parse_token_set(
    payload: dict[str, object],
    preserved_refresh_token: str | None,
    dialect: OAuthDialect,
    *,
    configured_scopes: tuple[str, ...],
    allow_slack_authed_user_scope: bool,
) -> OAuthTokenSet:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthTransportError("OAuth token response has no usable access_token")
    token_type = payload.get("token_type")
    expected_token_type = "user" if dialect is OAuthDialect.SLACK_USER else "bearer"
    if not isinstance(token_type, str) or token_type.casefold() != expected_token_type:
        raise OAuthTransportError("OAuth token response has unsupported token_type")

    if "refresh_token" not in payload:
        refresh_token = preserved_refresh_token
    else:
        refresh_value = payload["refresh_token"]
        if not isinstance(refresh_value, str) or not refresh_value:
            raise OAuthTransportError("OAuth token response has an invalid refresh_token")
        refresh_token = refresh_value
    if dialect is OAuthDialect.GOOGLE and refresh_token is None:
        raise OAuthTransportError("Google OAuth response has no usable refresh_token")
    if (
        dialect is OAuthDialect.MICROSOFT
        and preserved_refresh_token is None
        and _has_scope(configured_scopes, "offline_access")
        and refresh_token is None
    ):
        raise OAuthTransportError("Microsoft OAuth response has no usable refresh_token")

    expires_value = payload.get("expires_in")
    if expires_value is None:
        expires_in = None
    elif isinstance(expires_value, bool) or not isinstance(expires_value, int) or expires_value < 0:
        raise OAuthTransportError("OAuth token response has an invalid expires_in")
    else:
        expires_in = expires_value
    scope_value = payload.get("scope")
    if scope_value is None and dialect is OAuthDialect.SLACK_USER and allow_slack_authed_user_scope:
        authed_user = payload.get("authed_user")
        if isinstance(authed_user, dict):
            scope_value = authed_user.get("scope")
    if scope_value is None:
        if preserved_refresh_token is None and dialect is OAuthDialect.GOOGLE:
            raise OAuthTransportError("Google OAuth token response has no usable scope")
        if preserved_refresh_token is None and dialect is OAuthDialect.SLACK_USER:
            raise OAuthTransportError("Slack OAuth token response has no usable user scope")
        if preserved_refresh_token is None and dialect is OAuthDialect.MICROSOFT:
            scopes = canonicalize_microsoft_access_scopes(
                tuple(scope for scope in configured_scopes if scope.casefold() != "offline_access")
            )
        else:
            scopes = None
    elif isinstance(scope_value, str):
        if dialect is OAuthDialect.SLACK_USER:
            scopes = tuple(item.strip() for item in scope_value.split(","))
        else:
            scopes = tuple(scope_value.split())
        _validate_scopes(scopes)
        if (
            not scopes
            and preserved_refresh_token is None
            and dialect
            in {
                OAuthDialect.GOOGLE,
                OAuthDialect.SLACK_USER,
            }
        ):
            raise OAuthTransportError("OAuth token response has no usable scope")
        if dialect is OAuthDialect.GOOGLE:
            scopes = canonicalize_google_scopes(scopes)
        if dialect is OAuthDialect.MICROSOFT:
            scopes = canonicalize_microsoft_access_scopes(scopes)
    else:
        raise OAuthTransportError("OAuth token response has an invalid scope")
    return OAuthTokenSet(
        access_token=access_token,
        token_type=OAuthTokenType.BEARER,
        refresh_token=refresh_token,
        expires_in_seconds=expires_in,
        scopes=scopes,
    )


def _decode_json_object(body: bytes) -> dict[str, object]:
    if len(body) > _MAX_RESPONSE_BYTES:
        raise OAuthTransportError("OAuth token response exceeds the size limit")
    try:
        decoded: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthTransportError("OAuth token response is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise OAuthTransportError("OAuth token response must be a JSON object")
    return cast(dict[str, object], decoded)


def _read_bounded(response: object) -> bytes:
    read = getattr(response, "read", None)
    if not callable(read):
        raise OAuthTransportError("OAuth token response is unreadable")
    body = cast(bytes, read(_MAX_RESPONSE_BYTES + 1))
    if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES:
        raise OAuthTransportError("OAuth token response exceeds the size limit")
    return body


def _validate_https_endpoint(endpoint: str, label: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OAuthConfigurationError(f"OAuth {label} must be an HTTPS URL without credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise OAuthConfigurationError(f"OAuth {label} has an invalid port") from exc


def _validate_loopback_redirect(redirect_uri: str) -> None:
    parsed = urlsplit(redirect_uri)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OAuthConfigurationError("OAuth redirect URI has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise OAuthConfigurationError(
            "OAuth redirect URI must be an exact HTTP loopback URI with an explicit port"
        )


def _singular_parameters(query: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name in values:
            error_type = OAuthStateMismatchError if name == "state" else OAuthCallbackError
            raise error_type(f"OAuth callback contains duplicate {name}")
        values[name] = value
    return values


def _validate_code_verifier(code_verifier: str) -> None:
    if not 43 <= len(code_verifier) <= 128 or any(
        character not in _PKCE_ALLOWED for character in code_verifier
    ):
        raise OAuthConfigurationError("PKCE verifier must be 43-128 RFC 7636 characters")


def _validate_scopes(scopes: tuple[str, ...]) -> None:
    if any(not scope or any(character.isspace() for character in scope) for scope in scopes):
        raise OAuthConfigurationError("OAuth scopes must be non-empty strings without whitespace")
    if len(set(scopes)) != len(scopes):
        raise OAuthConfigurationError("OAuth scopes must not contain duplicates")


_GOOGLE_USERINFO_EMAIL_SCOPE: Final = "https://www.googleapis.com/auth/userinfo.email"


def canonicalize_google_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """Return Google grants in the built-in profile's canonical spelling."""

    _validate_scopes(scopes)
    result = tuple("email" if scope == _GOOGLE_USERINFO_EMAIL_SCOPE else scope for scope in scopes)
    _validate_scopes(result)
    return result


_MICROSOFT_SCOPE_PREFIX: Final = "https://graph.microsoft.com/"
_MICROSOFT_CANONICAL_SCOPES: Final = {
    "offline_access": "offline_access",
    "user.read": "User.Read",
    "mail.read": "Mail.Read",
    "mail.readwrite": "Mail.ReadWrite",
    "mail.send": "Mail.Send",
    "calendars.read": "Calendars.Read",
    "calendars.readwrite": "Calendars.ReadWrite",
}


def canonicalize_microsoft_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """Return Microsoft grants in the built-in profile's canonical spelling."""

    canonical: list[str] = []
    prefix = _MICROSOFT_SCOPE_PREFIX.casefold()
    for scope in scopes:
        candidate = scope
        if candidate.casefold().startswith(prefix):
            candidate = candidate[len(_MICROSOFT_SCOPE_PREFIX) :]
        canonical.append(_MICROSOFT_CANONICAL_SCOPES.get(candidate.casefold(), candidate))
    result = tuple(canonical)
    _validate_scopes(result)
    return result


def canonicalize_microsoft_access_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        scope
        for scope in canonicalize_microsoft_scopes(scopes)
        if scope.casefold() != "offline_access"
    )


def _has_scope(scopes: tuple[str, ...], expected: str) -> bool:
    return any(scope.casefold() == expected.casefold() for scope in scopes)
