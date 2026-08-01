"""Finite built-in connector authentication profiles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


CONNECTOR_PROFILES: Final[Mapping[str, ConnectorProfile]] = MappingProxyType(
    {
        "discord": PROFILES["discord"],
        "slack": PROFILES["slack"],
        "gmail": ConnectorProfile(
            name="gmail",
            provider="google",
            source_ids=("gmail",),
            credential_kind=CredentialKind.OAUTH2,
            read_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
            ),
            full_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.modify",
            ),
            supplemental_scopes=("https://mail.google.com/",),
            legacy_read_scopes=(("https://www.googleapis.com/auth/gmail.readonly",),),
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        ),
        "google_calendar": ConnectorProfile(
            name="google_calendar",
            provider="google",
            source_ids=("google_calendar",),
            credential_kind=CredentialKind.OAUTH2,
            read_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                "https://www.googleapis.com/auth/calendar.events.readonly",
                "https://www.googleapis.com/auth/calendar.freebusy",
            ),
            full_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                "https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar.calendars",
                "https://www.googleapis.com/auth/calendar.freebusy",
            ),
            legacy_read_scopes=(("https://www.googleapis.com/auth/calendar.readonly",),),
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        ),
        "google_drive": ConnectorProfile(
            name="google_drive",
            provider="google",
            source_ids=("google_drive",),
            credential_kind=CredentialKind.OAUTH2,
            read_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/drive.metadata.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ),
            full_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/drive",
            ),
            legacy_read_scopes=(("https://www.googleapis.com/auth/drive.metadata.readonly",),),
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        ),
        "outlook_mail": ConnectorProfile(
            name="outlook_mail",
            provider="microsoft",
            source_ids=("outlook_mail",),
            credential_kind=CredentialKind.OAUTH2,
            read_scopes=("offline_access", "User.Read", "Mail.Read"),
            full_scopes=("offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send"),
            legacy_read_scopes=(("Mail.Read",),),
            authorization_endpoint=(
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            ),
            token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        ),
        "outlook_calendar": ConnectorProfile(
            name="outlook_calendar",
            provider="microsoft",
            source_ids=("outlook_calendar",),
            credential_kind=CredentialKind.OAUTH2,
            read_scopes=("offline_access", "User.Read", "Calendars.Read"),
            full_scopes=("offline_access", "User.Read", "Calendars.ReadWrite"),
            legacy_read_scopes=(("Calendars.Read",),),
            authorization_endpoint=(
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            ),
            token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        ),
    }
)


def get_profile(name: str) -> ConnectorProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValidationError("connector profile is unsupported") from exc


def get_connector_profile(name: str) -> ConnectorProfile:
    if not isinstance(name, str):
        raise ValidationError("connector profile is unsupported")
    try:
        return CONNECTOR_PROFILES[name]
    except KeyError as exc:
        raise ValidationError("connector profile is unsupported") from exc


def get_profile_for_connection(
    provider: str,
    source_ids: Sequence[str],
    scopes: tuple[str, ...] | None = None,
) -> ConnectorProfile:
    if (
        not isinstance(provider, str)
        or isinstance(source_ids, str)
        or not isinstance(source_ids, Sequence)
        or not source_ids
        or any(not isinstance(source_id, str) or not source_id for source_id in source_ids)
    ):
        raise ValidationError("connector connection profile is unsupported")
    exact_source_ids = tuple(sorted(source_ids))
    if len(set(exact_source_ids)) != len(exact_source_ids):
        raise ValidationError("connector connection profile is unsupported")
    logical: ConnectorProfile | None = None
    for profile in CONNECTOR_PROFILES.values():
        if profile.provider == provider and tuple(sorted(profile.source_ids)) == exact_source_ids:
            logical = profile
            break
    if logical is not None:
        if scopes is None:
            return logical
        try:
            logical.access_for_scopes(scopes)
        except ValidationError:
            pass
        else:
            return logical
    try:
        aggregate = PROFILES[provider]
    except KeyError as exc:
        raise ValidationError("connector connection profile is unsupported") from exc
    if scopes is not None:
        try:
            aggregate.access_for_scopes(scopes)
        except ValidationError:
            pass
        else:
            # Compatibility for pre-logical records, whose source subset and
            # provider-wide grant were stored independently.
            return aggregate
    if tuple(sorted(aggregate.source_ids)) != exact_source_ids:
        raise ValidationError("connector connection profile is unsupported")
    return aggregate


def list_profiles() -> list[dict[str, object]]:
    return [PROFILES[name].to_public_dict() for name in sorted(PROFILES)]


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
