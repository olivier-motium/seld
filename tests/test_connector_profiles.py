from __future__ import annotations

from continuity_kernel.connector_profiles import list_profiles


def test_builtin_connector_profiles_are_finite_and_exact() -> None:
    assert list_profiles() == [
        {
            "name": "discord",
            "provider": "discord",
            "source_ids": ["discord"],
            "credential_kind": "bearer",
            "scopes": [],
            "authorization_endpoint": None,
            "token_endpoint": None,
        },
        {
            "name": "google",
            "provider": "google",
            "source_ids": ["gmail", "google_calendar", "google_drive"],
            "credential_kind": "oauth2",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/drive.metadata.readonly",
            ],
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
        },
        {
            "name": "microsoft",
            "provider": "microsoft",
            "source_ids": ["outlook_mail", "outlook_calendar"],
            "credential_kind": "oauth2",
            "scopes": [
                "offline_access",
                "User.Read",
                "Mail.Read",
                "Calendars.Read",
            ],
            "authorization_endpoint": (
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            ),
            "token_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        },
        {
            "name": "slack",
            "provider": "slack",
            "source_ids": ["slack"],
            "credential_kind": "oauth2",
            "scopes": [
                "channels:history",
                "groups:history",
                "mpim:history",
                "im:history",
            ],
            "authorization_endpoint": "https://slack.com/oauth/v2_user/authorize",
            "token_endpoint": "https://slack.com/api/oauth.v2.user.access",
        },
    ]
