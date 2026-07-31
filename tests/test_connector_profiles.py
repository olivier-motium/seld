from __future__ import annotations

import pytest

from continuity_kernel.connector_profiles import (
    ConnectorAccessTier,
    get_profile,
    list_profiles,
)
from continuity_kernel.errors import ValidationError


def test_builtin_connector_profiles_are_finite_and_expose_both_access_tiers() -> None:
    profiles = list_profiles()
    assert [profile["name"] for profile in profiles] == [
        "discord",
        "google",
        "microsoft",
        "slack",
    ]
    assert all(
        set(profile)
        == {
            "access_tiers",
            "authorization_endpoint",
            "credential_kind",
            "name",
            "provider",
            "scopes",
            "source_ids",
            "supplemental_scopes",
            "token_endpoint",
        }
        for profile in profiles
    )

    google = get_profile("google")
    assert google.scopes_for("read") == (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    )
    assert google.scopes_for("full") == (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.calendars",
        "https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/drive",
    )
    assert google.scopes_for("full", include_supplemental=True)[-1] == ("https://mail.google.com/")

    microsoft = get_profile("microsoft")
    assert microsoft.scopes_for("read") == (
        "offline_access",
        "User.Read",
        "Mail.Read",
        "Calendars.Read",
    )
    assert microsoft.scopes_for("full") == (
        "offline_access",
        "User.Read",
        "Mail.ReadWrite",
        "Mail.Send",
        "Calendars.ReadWrite",
    )

    slack = get_profile("slack")
    assert {
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
    } == set(slack.scopes_for("read"))
    assert {
        "channels:write",
        "groups:write",
        "im:write",
        "mpim:write",
        "chat:write",
        "files:write",
        "reactions:write",
    } < set(slack.scopes_for("full"))

    discord = get_profile("discord")
    assert discord.scopes_for("read") == ()
    assert discord.scopes_for("full") == ("seld.discord.full",)


def test_access_tier_classification_preserves_legacy_read_connections() -> None:
    google = get_profile("google")
    assert (
        google.access_for_scopes(("https://www.googleapis.com/auth/gmail.readonly",))
        is ConnectorAccessTier.READ
    )
    assert google.access_for_scopes(google.full_scopes) is ConnectorAccessTier.FULL
    assert (
        google.access_for_scopes(google.scopes_for("full", include_supplemental=True))
        is ConnectorAccessTier.FULL
    )

    slack = get_profile("slack")
    assert slack.access_for_scopes(slack.legacy_read_scopes[0]) is ConnectorAccessTier.READ
    with pytest.raises(ValidationError, match="built-in access tier"):
        slack.access_for_scopes(("channels:history",))
    with pytest.raises(ValidationError, match="built-in access tier"):
        slack.access_for_scopes((*slack.read_scopes, "admin"))


def test_supplemental_grant_is_only_available_on_supported_full_profiles() -> None:
    with pytest.raises(ValidationError, match="supplemental full-access"):
        get_profile("google").scopes_for("read", include_supplemental=True)
    with pytest.raises(ValidationError, match="no supplemental"):
        get_profile("microsoft").scopes_for("full", include_supplemental=True)
