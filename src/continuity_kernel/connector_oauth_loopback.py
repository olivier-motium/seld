"""One-shot native OAuth loopback callback; never a resident server."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from continuity_kernel.connector_oauth import (
    OAuthCallbackError,
    OAuthClientConfig,
    build_authorization_url,
    generate_pkce_pair,
    generate_state,
    validate_authorization_callback,
)
from continuity_kernel.errors import ValidationError

_SUCCESS = b"Seld received the authorization response. You can close this tab.\n"
_REJECTED = b"Seld rejected this callback. Return to the terminal.\n"
_NOT_FOUND = b"Not found.\n"


@dataclass(frozen=True, repr=False)
class OAuthAuthorizationAttempt:
    authorization_url: str
    state: str
    code_verifier: str

    def __repr__(self) -> str:
        return "OAuthAuthorizationAttempt(authorization_url=<redacted>, state=<redacted>)"


class _OneShotServer(HTTPServer):
    expected_path: str
    config: OAuthClientConfig
    attempt: OAuthAuthorizationAttempt
    authorization_code: str | None = None
    callback_error: OAuthCallbackError | None = None


class _IPv6OneShotServer(_OneShotServer):
    address_family = socket.AF_INET6


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _OneShotServer

    def do_GET(self) -> None:
        received = urlsplit(self.path)
        if received.path != self.server.expected_path or received.fragment:
            self._reply(404, _NOT_FOUND)
            return
        callback_url = self.server.config.redirect_uri + (
            f"?{received.query}" if received.query else ""
        )
        try:
            self.server.authorization_code = validate_authorization_callback(
                self.server.config,
                callback_url=callback_url,
                expected_state=self.server.attempt.state,
            )
        except OAuthCallbackError as exc:
            self.server.callback_error = exc
            self._reply(400, _REJECTED)
            return
        self._reply(200, _SUCCESS)

    def log_message(self, _format: str, *args: object) -> None:
        del args

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class BoundLoopbackCallback:
    """A pre-bound callback whose origin is never derived from request headers."""

    def __init__(self, server: _OneShotServer, *, host: str, path: str) -> None:
        self._server = server
        self._host = host
        self._path = path
        self._closed = False

    @classmethod
    def bind(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        path: str = "/oauth/callback",
    ) -> BoundLoopbackCallback:
        if host not in {"127.0.0.1", "::1"}:
            raise ValidationError("OAuth callback host must be a loopback IP")
        if not path.startswith("/") or "?" in path or "#" in path or "\x00" in path:
            raise ValidationError("OAuth callback path is invalid")
        server_type = _IPv6OneShotServer if host == "::1" else _OneShotServer
        server = server_type((host, port), _CallbackHandler)
        server.expected_path = path
        return cls(server, host=host, path=path)

    @property
    def redirect_uri(self) -> str:
        address = self._server.server_address
        port = int(address[1])
        host = f"[{self._host}]" if self._host == "::1" else self._host
        return f"http://{host}:{port}{self._path}"

    def configure(
        self,
        config: OAuthClientConfig,
        attempt: OAuthAuthorizationAttempt,
    ) -> None:
        if config.redirect_uri != self.redirect_uri:
            raise ValidationError("OAuth configuration is not bound to this callback listener")
        self._server.config = config
        self._server.attempt = attempt

    def wait_for_code(self, *, timeout_seconds: float) -> str:
        if timeout_seconds <= 0:
            raise ValidationError("OAuth callback timeout must be positive")
        deadline = time.monotonic() + timeout_seconds
        try:
            while self._server.authorization_code is None and self._server.callback_error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OAuthCallbackError("OAuth callback timed out")
                self._server.timeout = remaining
                self._server.handle_request()
            if self._server.callback_error is not None:
                raise self._server.callback_error
            assert self._server.authorization_code is not None
            return self._server.authorization_code
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._server.server_close()


def begin_authorization(config: OAuthClientConfig) -> OAuthAuthorizationAttempt:
    state = generate_state()
    pkce = generate_pkce_pair()
    return OAuthAuthorizationAttempt(
        authorization_url=build_authorization_url(config, state=state, pkce=pkce),
        state=state,
        code_verifier=pkce.code_verifier,
    )
