"""Finite built-in connector authentication profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from continuity_kernel.connector_auth import CredentialKind
from continuity_kernel.errors import ValidationError


@dataclass(frozen=True)
class ConnectorProfile:
    name: str
    provider: str
    source_ids: tuple[str, ...]
    credential_kind: CredentialKind
    scopes: tuple[str, ...] = ()
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provider": self.provider,
            "source_ids": list(self.source_ids),
            "credential_kind": self.credential_kind.value,
            "scopes": list(self.scopes),
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
        }


PROFILES: Final[Mapping[str, ConnectorProfile]] = MappingProxyType(
    {
        "discord": ConnectorProfile(
            name="discord",
            provider="discord",
            source_ids=("discord",),
            credential_kind=CredentialKind.BEARER,
        ),
        "google": ConnectorProfile(
            name="google",
            provider="google",
            source_ids=("gmail", "google_calendar", "google_drive"),
            credential_kind=CredentialKind.OAUTH2,
            scopes=(
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/drive.metadata.readonly",
            ),
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        ),
        "microsoft": ConnectorProfile(
            name="microsoft",
            provider="microsoft",
            source_ids=("outlook_mail", "outlook_calendar"),
            credential_kind=CredentialKind.OAUTH2,
            scopes=(
                "offline_access",
                "User.Read",
                "Mail.Read",
                "Calendars.Read",
            ),
            authorization_endpoint=(
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            ),
            token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        ),
        "slack": ConnectorProfile(
            name="slack",
            provider="slack",
            source_ids=("slack",),
            credential_kind=CredentialKind.OAUTH2,
            scopes=(
                "channels:history",
                "groups:history",
                "mpim:history",
                "im:history",
            ),
            authorization_endpoint="https://slack.com/oauth/v2_user/authorize",
            token_endpoint="https://slack.com/api/oauth.v2.user.access",
        ),
    }
)


def get_profile(name: str) -> ConnectorProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValidationError("connector profile is unsupported") from exc


def list_profiles() -> list[dict[str, object]]:
    return [PROFILES[name].to_public_dict() for name in sorted(PROFILES)]
