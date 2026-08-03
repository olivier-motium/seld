"""Finite built-in connector authentication profiles."""

from __future__ import annotations

import os
import re
import shlex
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
    legacy_full_scopes: tuple[tuple[str, ...], ...] = ()
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None

    @property
    def allowed_scopes(self) -> frozenset[str]:
        legacy = tuple(
            scope
            for bundle in (*self.legacy_read_scopes, *self.legacy_full_scopes)
            for scope in bundle
        )
        return frozenset((*self.read_scopes, *self.full_scopes, *self.supplemental_scopes, *legacy))

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
        full_sets = {
            frozenset(option) for option in (self.full_scopes, *self.legacy_full_scopes) if option
        }
        full_with_supplemental = {
            frozenset((*option, *self.supplemental_scopes))
            for option in (self.full_scopes, *self.legacy_full_scopes)
            if option and self.supplemental_scopes
        }
        if configured in full_sets | full_with_supplemental:
            return ConnectorAccessTier.FULL
        read_options = (self.read_scopes, *self.legacy_read_scopes)
        read_sets = {frozenset(option) for option in read_options if option}
        if configured in read_sets:
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


def connector_connect_command(
    profile: ConnectorProfile,
    scopes: Sequence[str],
    *,
    new_account: bool = False,
    connection_id: str | None = None,
    access: ConnectorAccessTier | None = None,
    include_permanent_delete: bool | None = None,
    alias: str | None = None,
    browser_mode: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """Build the canonical guided-onboarding command for one profile."""

    if profile.name not in CONNECTOR_PROFILES:
        return "gsv-auth status"
    selected_access = access or (
        ConnectorAccessTier.FULL
        if profile.name == "discord"
        else profile.access_for_scopes(tuple(scopes))
    )
    arguments = [
        "gsv",
        "connectors",
        "connect",
        profile.name,
        "--access",
        selected_access.value,
    ]
    has_permanent_delete_scope = any(scope in scopes for scope in profile.supplemental_scopes)
    if profile.name == "gmail" and (
        include_permanent_delete
        if include_permanent_delete is not None
        else has_permanent_delete_scope
    ):
        arguments.append("--with-permanent-delete")
    if new_account:
        arguments.append("--new-account")
    if connection_id is not None:
        arguments.extend(("--connection-id", connection_id))
    if alias is not None:
        arguments.append(f"--alias={alias}")
    if timeout_seconds is not None:
        arguments.extend(("--timeout", format(timeout_seconds, "g")))
    arguments.extend(connector_browser_options(browser_mode))
    return format_connector_command(arguments)


def connector_browser_options(browser_mode: str | None) -> tuple[str, ...]:
    """Return the explicit CLI flags for one browser mode."""

    if browser_mode is None:
        return ()
    if browser_mode == "manual":
        return ("--no-browser",)
    if browser_mode in {"default", "firefox"}:
        return ("--browser", browser_mode)
    raise ValidationError("connector browser mode is invalid")


def format_connector_command(arguments: Sequence[str]) -> str:
    """Format a copyable command for POSIX shells or Windows PowerShell."""

    if not arguments or any(
        not isinstance(argument, str) or not argument for argument in arguments
    ):
        raise ValidationError("connector command arguments are invalid")
    if os.name == "nt":
        return " ".join(_powershell_argument(argument) for argument in arguments)
    return shlex.join(arguments)


def _powershell_argument(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def validate_connector_alias(value: str | None) -> str | None:
    """Validate and normalize a privacy-safe local connector label."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("connector alias is invalid")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValidationError("connector alias is invalid") from exc
    if encoded_length > 256 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ValidationError("connector alias is invalid")
    return value.strip()


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
                "https://www.googleapis.com/auth/calendar.calendars.readonly",
                "https://www.googleapis.com/auth/calendar.events.readonly",
                "https://www.googleapis.com/auth/calendar.freebusy",
                "https://www.googleapis.com/auth/drive.metadata.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ),
            full_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.settings.basic",
                "https://www.googleapis.com/auth/calendar.calendarlist",
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
                (
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                    "https://www.googleapis.com/auth/calendar.events.readonly",
                    "https://www.googleapis.com/auth/calendar.freebusy",
                    "https://www.googleapis.com/auth/drive.metadata.readonly",
                    "https://www.googleapis.com/auth/drive.readonly",
                ),
            ),
            legacy_full_scopes=(
                (
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                    "https://www.googleapis.com/auth/calendar.events",
                    "https://www.googleapis.com/auth/calendar.calendars",
                    "https://www.googleapis.com/auth/calendar.freebusy",
                    "https://www.googleapis.com/auth/drive",
                ),
                (
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/gmail.settings.basic",
                    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                    "https://www.googleapis.com/auth/calendar.events",
                    "https://www.googleapis.com/auth/calendar.calendars",
                    "https://www.googleapis.com/auth/calendar.freebusy",
                    "https://www.googleapis.com/auth/drive",
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
                "https://www.googleapis.com/auth/gmail.settings.basic",
            ),
            supplemental_scopes=("https://mail.google.com/",),
            legacy_read_scopes=(("https://www.googleapis.com/auth/gmail.readonly",),),
            legacy_full_scopes=(
                (
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/gmail.modify",
                ),
            ),
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
                "https://www.googleapis.com/auth/calendar.calendars.readonly",
                "https://www.googleapis.com/auth/calendar.events.readonly",
                "https://www.googleapis.com/auth/calendar.freebusy",
            ),
            full_scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/calendar.calendarlist",
                "https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar.calendars",
                "https://www.googleapis.com/auth/calendar.freebusy",
            ),
            legacy_read_scopes=(
                ("https://www.googleapis.com/auth/calendar.readonly",),
                (
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                    "https://www.googleapis.com/auth/calendar.events.readonly",
                    "https://www.googleapis.com/auth/calendar.freebusy",
                ),
            ),
            legacy_full_scopes=(
                (
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
                    "https://www.googleapis.com/auth/calendar.events",
                    "https://www.googleapis.com/auth/calendar.calendars",
                    "https://www.googleapis.com/auth/calendar.freebusy",
                ),
            ),
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
