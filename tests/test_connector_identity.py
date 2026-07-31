from __future__ import annotations

import hashlib
import io
from email.message import Message
from typing import cast
from urllib.request import Request

import pytest

from continuity_kernel.connector_identity import (
    GOOGLE_ISSUER,
    ConnectorIdentityVerifier,
)
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ResponseLike,
)
from continuity_kernel.errors import ContinuityError, ValidationError


class _Response:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.headers = Message()
        self._body = io.BytesIO(body)

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        del args


def _credential(scheme: AuthorizationScheme = AuthorizationScheme.BEARER) -> ConnectorCredential:
    return ConnectorCredential(scheme=scheme, secret="provider-token")


def _verifier(body: bytes, captured: list[Request]) -> ConnectorIdentityVerifier:
    def open_request(request: Request, timeout: float) -> ResponseLike:
        del timeout
        captured.append(request)
        return cast(ResponseLike, _Response(body))

    from continuity_kernel.connector_transport import ConnectorTransport

    return ConnectorIdentityVerifier(ConnectorTransport(opener=open_request))


def test_google_identity_uses_fixed_issuer_sub_fingerprint_and_hides_display_label() -> None:
    captured: list[Request] = []
    identity = _verifier(
        b'{"iss":"https://accounts.google.com","sub":"google-sub",'
        b'"name":"Ada Lovelace","email":"ada@example.test"}',
        captured,
    ).verify("google", _credential())

    expected = hashlib.sha256(f"google\0{GOOGLE_ISSUER}\0google-sub".encode()).hexdigest()
    assert identity.provider == "google"
    assert identity.fingerprint == f"sha256:{expected}"
    assert identity.display_label == "Ada Lovelace <ada@example.test>"
    assert identity.portable_label == f"google:{expected[-12:]}"
    assert "Ada Lovelace" not in repr(identity)
    assert captured[0].full_url == "https://openidconnect.googleapis.com/v1/userinfo"


def test_google_fingerprint_ignores_changeable_display_fields() -> None:
    first = _verifier(b'{"sub":"same-sub","name":"First Name"}', []).verify("google", _credential())
    second = _verifier(b'{"sub":"same-sub","name":"Changed Name"}', []).verify(
        "google", _credential()
    )

    assert first.fingerprint == second.fingerprint
    assert first.display_label != second.display_label


def test_microsoft_identity_uses_immutable_id_and_exact_select_query() -> None:
    captured: list[Request] = []
    identity = _verifier(
        b'{"id":"opaque-account","displayName":"Olivier","userPrincipalName":"o@example.test"}',
        captured,
    ).verify("microsoft", _credential())

    assert identity.provider == "microsoft"
    assert identity.display_label == "Olivier <o@example.test>"
    assert captured[0].full_url == (
        "https://graph.microsoft.com/v1.0/me?%24select=id%2CdisplayName%2CuserPrincipalName%2Cmail"
    )


def test_slack_requires_success_and_workspace_user_identity() -> None:
    captured: list[Request] = []
    identity = _verifier(
        b'{"ok":true,"team_id":"T123","user_id":"U456","team":"Seld", "user":"Olivier"}',
        captured,
    ).verify("slack", _credential())

    assert identity.display_label == "Olivier <Seld>"
    assert captured[0].full_url == "https://slack.com/api/auth.test"
    assert identity.portable_label.startswith("slack:")


def test_discord_requires_bot_identity_and_uses_bot_authentication() -> None:
    captured: list[Request] = []
    identity = _verifier(
        b'{"id":"987654321","bot":true,"username":"seld-bot"}',
        captured,
    ).verify("discord", _credential(AuthorizationScheme.BOT))

    assert identity.display_label == "seld-bot"
    assert captured[0].full_url == "https://discord.com/api/v10/users/@me"
    assert captured[0].get_header("Authorization") == "Bot provider-token"


def test_identity_rejects_bad_provider_flags_missing_ids_and_oversized_bodies() -> None:
    with pytest.raises(ValidationError, match="successful"):
        _verifier(b'{"ok":false,"team_id":"T1","user_id":"U1"}', []).verify("slack", _credential())
    with pytest.raises(ValidationError, match="account ID"):
        _verifier(b'{"displayName":"no id"}', []).verify("microsoft", _credential())
    with pytest.raises(ValidationError, match="issuer"):
        _verifier(b'{"iss":"https://attacker.invalid","sub":"subject"}', []).verify(
            "google", _credential()
        )
    with pytest.raises(ContinuityError, match="exceeds"):
        _verifier(b"{" + b"a" * (64 * 1024) + b"}", []).verify("google", _credential())
