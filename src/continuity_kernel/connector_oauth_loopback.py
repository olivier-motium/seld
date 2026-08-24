"""One-shot native OAuth loopback callback; never a resident server."""

from __future__ import annotations

import selectors
import socket
import ssl
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from continuity_kernel.connector_oauth import (
    OAuthCallbackError,
    OAuthClientConfig,
    OAuthStateMismatchError,
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

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


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
            if not isinstance(exc, OAuthStateMismatchError):
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

    def __init__(
        self,
        servers: tuple[_OneShotServer, ...],
        *,
        host: str,
        path: str,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._servers = servers
        self._host = host
        self._path = path
        self._tls_context = tls_context
        self._closed = False

    @classmethod
    def bind(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        path: str = "/oauth/callback",
        tls_context: ssl.SSLContext | None = None,
    ) -> BoundLoopbackCallback:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValidationError("OAuth callback host must be loopback")
        if (path and not path.startswith("/")) or "?" in path or "#" in path or "\x00" in path:
            raise ValidationError("OAuth callback path is invalid")

        if host == "localhost":
            try:
                ipv4 = _OneShotServer(("127.0.0.1", port), _CallbackHandler)
            except OSError as exc:
                raise ValidationError("OAuth callback could not bind the registered port") from exc
            servers: tuple[_OneShotServer, ...] = (ipv4,)
            actual_port = int(ipv4.server_address[1])
            if socket.has_ipv6:
                try:
                    ipv6 = _IPv6OneShotServer(("::1", actual_port), _CallbackHandler)
                except OSError as exc:
                    ipv4.server_close()
                    raise ValidationError(
                        "OAuth localhost callback could not bind both loopback families"
                    ) from exc
                servers = (ipv4, ipv6)
        else:
            server_type = _IPv6OneShotServer if host == "::1" else _OneShotServer
            try:
                servers = (server_type((host, port), _CallbackHandler),)
            except OSError as exc:
                raise ValidationError("OAuth callback could not bind the loopback port") from exc

        if tls_context is not None:
            for server in servers:
                server.socket = tls_context.wrap_socket(server.socket, server_side=True)

        for server in servers:
            server.expected_path = path or "/"
        return cls(servers, host=host, path=path, tls_context=tls_context)

    @property
    def redirect_uri(self) -> str:
        address = self._servers[0].server_address
        port = int(address[1])
        host = f"[{self._host}]" if self._host == "::1" else self._host
        scheme = "https" if self._tls_context is not None else "http"
        return f"{scheme}://{host}:{port}{self._path}"

    def configure(
        self,
        config: OAuthClientConfig,
        attempt: OAuthAuthorizationAttempt,
    ) -> None:
        if config.redirect_uri != self.redirect_uri:
            raise ValidationError("OAuth configuration is not bound to this callback listener")
        for server in self._servers:
            server.config = config
            server.attempt = attempt

    def wait_for_code(self, *, timeout_seconds: float) -> str:
        if timeout_seconds <= 0:
            raise ValidationError("OAuth callback timeout must be positive")
        deadline = time.monotonic() + timeout_seconds
        try:
            with selectors.DefaultSelector() as selector:
                for server in self._servers:
                    selector.register(server, selectors.EVENT_READ, data=server)
                return self._wait_for_ready_callback(selector, deadline)
        finally:
            self.close()

    def _wait_for_ready_callback(
        self,
        selector: selectors.BaseSelector,
        deadline: float,
    ) -> str:
        while True:
            for server in self._servers:
                if server.callback_error is not None:
                    raise server.callback_error
                if server.authorization_code is not None:
                    return server.authorization_code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OAuthCallbackError("OAuth callback timed out")
            ready = selector.select(remaining)
            if not ready:
                raise OAuthCallbackError("OAuth callback timed out")
            for key, _events in ready:
                callback_server = key.data
                assert isinstance(callback_server, _OneShotServer)
                callback_server.timeout = 0
                callback_server.handle_request()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            for server in self._servers:
                server.server_close()


def begin_authorization(config: OAuthClientConfig) -> OAuthAuthorizationAttempt:
    state = generate_state()
    pkce = generate_pkce_pair()
    return OAuthAuthorizationAttempt(
        authorization_url=build_authorization_url(config, state=state, pkce=pkce),
        state=state,
        code_verifier=pkce.code_verifier,
    )
