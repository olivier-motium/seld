"""Versioned logical source recipes used by deterministic readiness checks.

Recipes describe capabilities rather than provider bodies or unstable display
labels.  The resident AI binds those capabilities to the exact tools exposed
in a fresh ChatGPT task and records a content-free fingerprint of that mapping.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Final

from continuity_kernel.errors import ValidationError


@dataclass(frozen=True)
class SourceRecipe:
    source: str
    label: str
    recipe_version: str
    identity_capability: str | None
    read_capability: str
    read_limit: int
    proof_ttl: timedelta
    max_future_coverage: timedelta

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["proof_ttl_seconds"] = int(self.proof_ttl.total_seconds())
        payload["max_future_coverage_seconds"] = int(self.max_future_coverage.total_seconds())
        del payload["proof_ttl"]
        del payload["max_future_coverage"]
        return payload


def _recipe(
    source: str,
    label: str,
    *,
    version: str,
    identity: str | None,
    read: str,
    limit: int = 25,
    ttl_hours: int = 24,
    future_coverage_hours: int = 0,
) -> SourceRecipe:
    return SourceRecipe(
        source=source,
        label=label,
        recipe_version=version,
        identity_capability=identity,
        read_capability=read,
        read_limit=limit,
        proof_ttl=timedelta(hours=ttl_hours),
        max_future_coverage=timedelta(
            hours=future_coverage_hours,
            minutes=1 if future_coverage_hours == 0 else 0,
        ),
    )


RECIPES: Final = {
    recipe.source: recipe
    for recipe in (
        _recipe(
            "gsv",
            "Seld on this computer",
            version="1",
            identity="gsv.vault.identity",
            read="gsv.context.bounded_read",
            ttl_hours=24 * 30,
        ),
        _recipe(
            "codex_activity",
            "ChatGPT activity",
            version="1",
            identity="codex.account.identity",
            read="codex.activity.bounded_read",
            ttl_hours=24,
        ),
        _recipe(
            "gmail",
            "Gmail",
            version="1",
            identity="google.mail.identity",
            read="google.mail.recent_read",
        ),
        _recipe(
            "google_calendar",
            "Google Calendar",
            version="1",
            identity="google.calendar.identity",
            read="google.calendar.window_read",
            future_coverage_hours=24 * 60,
        ),
        _recipe(
            "google_drive",
            "Google Drive",
            version="1",
            identity="google.drive.identity",
            read="google.drive.recent_read",
        ),
        _recipe(
            "google_sheets",
            "Google Sheets",
            version="1",
            identity="google.drive.identity",
            read="google.sheets.recent_read",
        ),
        _recipe(
            "outlook_mail",
            "Outlook Mail",
            version="1",
            identity="microsoft.mail.identity",
            read="microsoft.mail.recent_read",
        ),
        _recipe(
            "outlook_calendar",
            "Outlook Calendar",
            version="1",
            identity="microsoft.calendar.identity",
            read="microsoft.calendar.window_read",
            future_coverage_hours=24 * 60,
        ),
        _recipe(
            "slack",
            "Slack",
            version="2",
            identity="slack.workspace.identity",
            read="slack.messages.recent_read",
            ttl_hours=6,
        ),
        _recipe(
            "teams",
            "Microsoft Teams",
            version="1",
            identity="microsoft.teams.identity",
            read="microsoft.teams.recent_read",
        ),
        _recipe(
            "github",
            "GitHub",
            version="1",
            identity="github.account.identity",
            read="github.activity.recent_read",
        ),
        _recipe(
            "asana",
            "Asana",
            version="1",
            identity="asana.workspace.identity",
            read="asana.work.recent_read",
        ),
        _recipe(
            "atlassian",
            "Atlassian",
            version="1",
            identity="atlassian.site.identity",
            read="atlassian.work.recent_read",
        ),
        _recipe(
            "box",
            "Box",
            version="1",
            identity="box.account.identity",
            read="box.files.recent_read",
        ),
        _recipe(
            "figma",
            "Figma",
            version="1",
            identity="figma.account.identity",
            read="figma.files.recent_read",
        ),
        _recipe(
            "notion",
            "Notion",
            version="1",
            identity="notion.workspace.identity",
            read="notion.pages.recent_read",
        ),
        _recipe(
            "sharepoint",
            "SharePoint",
            version="1",
            identity="microsoft.sharepoint.identity",
            read="microsoft.sharepoint.recent_read",
        ),
        _recipe(
            "local_files",
            "Files you choose",
            version="1",
            identity=None,
            read="local.files.bounded_read",
            limit=100,
            ttl_hours=24 * 7,
        ),
        _recipe(
            "apple_messages",
            "Apple Messages",
            version="1",
            identity="apple.messages.identity",
            read="apple.messages.recent_read",
            ttl_hours=6,
        ),
        _recipe(
            "shopify",
            "Shopify",
            version="1",
            identity="shopify.store.identity",
            read="shopify.orders.recent_read",
            limit=50,
            ttl_hours=6,
        ),
        _recipe(
            "instagram",
            "Instagram",
            version="1",
            identity="meta.instagram.identity",
            read="meta.instagram.activity.recent_read",
            ttl_hours=6,
        ),
        _recipe(
            "screen_context",
            "Optional screen context",
            version="1",
            identity=None,
            read="local.screen.derived_context_read",
            ttl_hours=6,
        ),
        _recipe(
            "whatsapp",
            "WhatsApp",
            version="2",
            identity="wacli.account.identity",
            read="wacli.messages.recent_read",
            ttl_hours=6,
        ),
        _recipe(
            "discord",
            "Discord",
            version="2",
            identity="discord.account.identity",
            read="discord.messages.recent_read",
            ttl_hours=6,
        ),
    )
}


def get_recipe(source: str) -> SourceRecipe:
    try:
        return RECIPES[source]
    except KeyError as exc:
        raise ValidationError(f"unsupported source recipe: {source}") from exc


def list_recipes() -> list[dict[str, Any]]:
    return [RECIPES[source].to_dict() for source in sorted(RECIPES)]
