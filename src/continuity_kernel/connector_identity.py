"""Transient, provider-verified connector identities."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from continuity_kernel.connector_transport import (
    ConnectorCredential,
    ConnectorMethod,
    ConnectorOrigin,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError

GOOGLE_ISSUER: Final = "https://accounts.google.com"
MAX_IDENTITY_RESPONSE_BYTES: Final = 64 * 1024

_PROVIDERS: Final = frozenset({"google", "microsoft", "slack", "discord"})
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_IDENTITY_TEXT_BYTES: Final = 4 * 1024
_MAX_LABEL_BYTES: Final = 2 * 1024


@dataclass(frozen=True, repr=False)
class ConnectorIdentity:
    """A provider identity held only for the current operation."""

    provider: str
    fingerprint: str
    display_label: str
    portable_label: str

    def __post_init__(self) -> None:
        provider = _provider(self.provider)
        if (
            not isinstance(self.fingerprint, str)
            or _FINGERPRINT.fullmatch(self.fingerprint) is None
        ):
            raise ValidationError("connector identity fingerprint is invalid")
        _required_text(self.display_label, "connector identity display label", _MAX_LABEL_BYTES)
        expected_portable = _portable_label(provider, self.fingerprint)
        if self.portable_label != expected_portable:
            raise ValidationError("connector identity portable label is invalid")
        object.__setattr__(self, "provider", provider)

    @property
    def label(self) -> str:
        """Compatibility spelling for callers that use a generic label field."""

        return self.display_label

    @property
    def identity_fingerprint(self) -> str:
        return self.fingerprint

    def __repr__(self) -> str:
        return (
            "ConnectorIdentity("
            f"provider={self.provider!r}, fingerprint={self.fingerprint!r}, "
            f"portable_label={self.portable_label!r})"
        )


class ConnectorIdentityVerifier:
    """Verify one exact provider identity through the pinned transport."""

    def __init__(self, transport: ConnectorTransport | None = None) -> None:
        self.transport = transport or ConnectorTransport()

    def verify(
        self,
        provider: str,
        credential: ConnectorCredential,
    ) -> ConnectorIdentity:
        clean_provider = _provider(provider)
        if not isinstance(credential, ConnectorCredential):
            raise ValidationError("connector credential is invalid")
        if clean_provider == "google":
            return self._verify_google(credential)
        if clean_provider == "microsoft":
            return self._verify_microsoft(credential)
        if clean_provider == "slack":
            return self._verify_slack(credential)
        return self._verify_discord(credential)

    def _get(
        self,
        *,
        origin: ConnectorOrigin,
        path: str,
        credential: ConnectorCredential,
        query: tuple[tuple[str, str], ...] = (),
    ) -> dict[str, object]:
        response = self.transport.request(
            origin=origin,
            method=ConnectorMethod.GET,
            path=path,
            credential=credential,
            query=query,
            response_bound=MAX_IDENTITY_RESPONSE_BYTES,
        )
        payload = response.json()
        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise ValidationError("provider identity response is invalid")
        return cast(dict[str, object], payload)

    def _verify_google(self, credential: ConnectorCredential) -> ConnectorIdentity:
        payload = self._get(
            origin=ConnectorOrigin.GOOGLE_OIDC,
            path="/v1/userinfo",
            credential=credential,
        )
        if "iss" in payload and payload["iss"] != GOOGLE_ISSUER:
            raise ValidationError("Google identity issuer is invalid")
        subject = _required_identifier(payload, "sub", "Google subject")
        name = _optional_text(payload, "name", "Google display name")
        email = _optional_text(payload, "email", "Google email")
        return _identity(
            provider="google",
            immutable=(GOOGLE_ISSUER, subject),
            display_label=_display_label(name, email, "Google account"),
        )

    def _verify_microsoft(self, credential: ConnectorCredential) -> ConnectorIdentity:
        payload = self._get(
            origin=ConnectorOrigin.MICROSOFT_GRAPH,
            path="/v1.0/me",
            credential=credential,
            query=(("$select", "id,displayName,userPrincipalName,mail"),),
        )
        account_id = _required_identifier(payload, "id", "Microsoft account ID")
        display_name = _optional_text(payload, "displayName", "Microsoft display name")
        principal_name = _optional_text(
            payload,
            "userPrincipalName",
            "Microsoft user principal name",
        )
        mail = _optional_text(payload, "mail", "Microsoft mail address")
        return _identity(
            provider="microsoft",
            immutable=(account_id,),
            display_label=_display_label(
                display_name,
                principal_name or mail,
                "Microsoft account",
            ),
        )

    def _verify_slack(self, credential: ConnectorCredential) -> ConnectorIdentity:
        payload = self._get(
            origin=ConnectorOrigin.SLACK,
            path="/api/auth.test",
            credential=credential,
        )
        if payload.get("ok") is not True:
            raise ValidationError("Slack identity response is not successful")
        team_id = _required_identifier(payload, "team_id", "Slack workspace ID")
        user_id = _required_identifier(payload, "user_id", "Slack user ID")
        team = _optional_text(payload, "team", "Slack workspace name") or _optional_text(
            payload,
            "team_name",
            "Slack workspace name",
        )
        user = _optional_text(payload, "user", "Slack user name") or _optional_text(
            payload,
            "user_name",
            "Slack user name",
        )
        return _identity(
            provider="slack",
            immutable=(team_id, user_id),
            display_label=_display_label(user, team, "Slack account"),
        )

    def _verify_discord(self, credential: ConnectorCredential) -> ConnectorIdentity:
        payload = self._get(
            origin=ConnectorOrigin.DISCORD,
            path="/api/v10/users/@me",
            credential=credential,
        )
        if payload.get("bot") is not True:
            raise ValidationError("Discord identity response is not a bot")
        account_id = _required_identifier(payload, "id", "Discord bot ID")
        global_name = _optional_text(payload, "global_name", "Discord global name")
        username = _optional_text(payload, "username", "Discord username")
        return _identity(
            provider="discord",
            immutable=(account_id,),
            display_label=_display_label(
                global_name,
                username,
                "Discord bot",
            ),
        )


def verify_identity(
    provider: str,
    credential: ConnectorCredential,
    transport: ConnectorTransport | None = None,
) -> ConnectorIdentity:
    """Verify one provider identity without storing or logging its response."""

    return ConnectorIdentityVerifier(transport).verify(provider, credential)


def verify_connector_identity(
    provider: str,
    credential: ConnectorCredential,
    transport: ConnectorTransport | None = None,
) -> ConnectorIdentity:
    return verify_identity(provider, credential, transport)


def _identity(
    *, provider: str, immutable: tuple[str, ...], display_label: str
) -> ConnectorIdentity:
    material = "\0".join((provider, *immutable)).encode("utf-8")
    fingerprint = f"sha256:{hashlib.sha256(material).hexdigest()}"
    return ConnectorIdentity(
        provider=provider,
        fingerprint=fingerprint,
        display_label=display_label,
        portable_label=_portable_label(provider, fingerprint),
    )


def _portable_label(provider: str, fingerprint: str) -> str:
    return f"{provider}:{fingerprint[-12:]}"


def _provider(value: object) -> str:
    if not isinstance(value, str) or value.casefold() not in _PROVIDERS:
        raise ValidationError("connector identity provider is unsupported")
    return value.casefold()


def _required_identifier(payload: Mapping[str, object], key: str, label: str) -> str:
    value = _required_text(payload.get(key), label, _MAX_IDENTITY_TEXT_BYTES)
    if value != value.strip() or any(character.isspace() for character in value):
        raise ValidationError(f"{label} is invalid")
    return value


def _required_text(value: object, label: str, max_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValidationError(f"{label} is invalid")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{label} is too large")
    return value


def _optional_text(payload: Mapping[str, object], key: str, label: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return _required_text(value, label, _MAX_IDENTITY_TEXT_BYTES)


def _display_label(primary: str | None, secondary: str | None, fallback: str) -> str:
    if primary and secondary and primary != secondary:
        return _required_text(
            f"{primary} <{secondary}>",
            "connector identity display label",
            _MAX_LABEL_BYTES,
        )
    return _required_text(
        primary or secondary or fallback,
        "connector identity display label",
        _MAX_LABEL_BYTES,
    )
