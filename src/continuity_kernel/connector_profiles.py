"""Finite built-in connector authentication profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from continuity_kernel.connector_auth import CredentialKind
from continuity_kernel.errors import ValidationError


class ConnectorAccessTier(StrEnum):
    """The local authority the person intentionally selected."""

    READ = "read"
    FULL = "full"


@dataclass(frozen=True)
class ConnectorProfile:
    name: str
    provider: str
    source_ids: tuple[str, ...]
    credential_kind: CredentialKind
    read_scopes: tuple[str, ...] = ()
    full_scopes: tuple[str, ...] = ()
    supplemental_scopes: tuple[str, ...] = ()
    legacy_read_scopes: tuple[tuple[str, ...], ...] = ()
    exact_read_scopes: bool = False
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None

    @property
    def scopes(self) -> tuple[str, ...]:
        """Compatibility alias for the default, least-authority tier."""

        return self.read_scopes

    @property
    def allowed_scopes(self) -> frozenset[str]:
        return frozenset((*self.read_scopes, *self.full_scopes, *self.supplemental_scopes))

    def scopes_for(
        self,
        access: ConnectorAccessTier | str,
        *,
        include_supplemental: bool = False,
    ) -> tuple[str, ...]:
        try:
            tier = ConnectorAccessTier(access)
        except (TypeError, ValueError) as exc:
            raise ValidationError("connector access must be read or full") from exc
        selected = self.read_scopes if tier is ConnectorAccessTier.READ else self.full_scopes
        if not selected and self.credential_kind is CredentialKind.OAUTH2:
            raise ValidationError("connector profile does not support the selected access")
        if include_supplemental:
            if tier is not ConnectorAccessTier.FULL or not self.supplemental_scopes:
                raise ValidationError("connector profile has no supplemental full-access grant")
            selected = (*selected, *self.supplemental_scopes)
        return _unique(selected)

    def access_for_scopes(self, scopes: tuple[str, ...]) -> ConnectorAccessTier:
        configured = frozenset(scopes)
        full = frozenset(self.full_scopes)
        if full and configured in {
            full,
            frozenset((*self.full_scopes, *self.supplemental_scopes)),
        }:
            return ConnectorAccessTier.FULL
        read_options = (self.read_scopes, *self.legacy_read_scopes)
        read_sets = {frozenset(option) for option in read_options}
        if configured in read_sets or (
            configured
            and not self.exact_read_scopes
            and any(configured < option for option in read_sets)
        ):
            return ConnectorAccessTier.READ
        raise ValidationError("connector scopes do not match a built-in access tier")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provider": self.provider,
            "source_ids": list(self.source_ids),
            "credential_kind": self.credential_kind.value,
            "scopes": list(self.read_scopes),
            "access_tiers": {
                "read": list(self.read_scopes),
                "full": list(self.full_scopes),
            },
            "supplemental_scopes": list(self.supplemental_scopes),
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
            full_scopes=("seld.discord.full",),
        ),
        "google": ConnectorProfile(
            name="google",
            provider="google",
            source_ids=("gmail", "google_calendar", "google_drive"),
            credential_kind=CredentialKind.OAUTH2,
            read_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                "https://www.googleapis.com/auth/calendar.events.readonly",
                "https://www.googleapis.com/auth/calendar.freebusy",
                "https://www.googleapis.com/auth/drive.metadata.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ),
            full_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                "https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar.calendars",
                "https://www.googleapis.com/auth/calendar.freebusy",
                "https://www.googleapis.com/auth/drive",
            ),
            supplemental_scopes=("https://mail.google.com/",),
            legacy_read_scopes=(
                (
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/calendar.readonly",
                    "https://www.googleapis.com/auth/drive.metadata.readonly",
                ),
            ),
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        ),
        "microsoft": ConnectorProfile(
            name="microsoft",
            provider="microsoft",
            source_ids=("outlook_mail", "outlook_calendar"),
            credential_kind=CredentialKind.OAUTH2,
            read_scopes=(
                "offline_access",
                "User.Read",
                "Mail.Read",
                "Calendars.Read",
            ),
            full_scopes=(
                "offline_access",
                "User.Read",
                "Mail.ReadWrite",
                "Mail.Send",
                "Calendars.ReadWrite",
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
            read_scopes=(
                "users:read",
                "channels:read",
                "groups:read",
                "im:read",
                "mpim:read",
                "channels:history",
                "groups:history",
                "im:history",
                "mpim:history",
                "files:read",
                "reactions:read",
            ),
            full_scopes=(
                "users:read",
                "channels:read",
                "groups:read",
                "im:read",
                "mpim:read",
                "channels:history",
                "groups:history",
                "im:history",
                "mpim:history",
                "files:read",
                "reactions:read",
                "channels:write",
                "groups:write",
                "im:write",
                "mpim:write",
                "chat:write",
                "files:write",
                "reactions:write",
            ),
            legacy_read_scopes=(
                (
                    "channels:history",
                    "groups:history",
                    "mpim:history",
                    "im:history",
                ),
            ),
            exact_read_scopes=True,
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


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
