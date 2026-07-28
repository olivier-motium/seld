"""Versioned logical source recipes used by deterministic readiness checks.

Recipes describe capabilities rather than provider bodies or unstable display
labels.  A host adapter must bind those capabilities to the exact tools exposed
in a fresh Codex task and include that mapping in the attestation fingerprint.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Final

from continuity_kernel.errors import ValidationError

RECIPE_SET_VERSION: Final = "2026-07-24.1"


@dataclass(frozen=True)
class SourceRecipe:
    source: str
    label: str
    recipe_version: str
    identity_capability: str | None
    read_capability: str
    pulse_capability: str
    read_limit: int
    proof_ttl: timedelta
    experimental: bool = False
    interactive_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["proof_ttl_seconds"] = int(self.proof_ttl.total_seconds())
        del payload["proof_ttl"]
        return payload


def _recipe(
    source: str,
    label: str,
    *,
    identity: str | None,
    read: str,
    pulse: str | None = None,
    limit: int = 25,
    ttl_hours: int = 24,
    experimental: bool = False,
    interactive_only: bool = False,
) -> SourceRecipe:
    return SourceRecipe(
        source=source,
        label=label,
        recipe_version=RECIPE_SET_VERSION,
        identity_capability=identity,
        read_capability=read,
        pulse_capability=pulse or read,
        read_limit=limit,
        proof_ttl=timedelta(hours=ttl_hours),
        experimental=experimental,
        interactive_only=interactive_only,
    )


RECIPES: Final = {
    recipe.source: recipe
    for recipe in (
        _recipe(
            "gsv",
            "GSV on this computer",
            identity="gsv.vault.identity",
            read="gsv.context.bounded_read",
            ttl_hours=24 * 30,
        ),
        _recipe(
            "codex_activity",
            "Codex activity",
            identity="codex.account.identity",
            read="codex.activity.bounded_read",
            ttl_hours=24,
        ),
        _recipe(
            "gmail",
            "Gmail",
            identity="google.mail.identity",
            read="google.mail.recent_read",
        ),
        _recipe(
            "google_calendar",
            "Google Calendar",
            identity="google.calendar.identity",
            read="google.calendar.window_read",
        ),
        _recipe(
            "outlook_mail",
            "Outlook Mail",
            identity="microsoft.mail.identity",
            read="microsoft.mail.recent_read",
        ),
        _recipe(
            "outlook_calendar",
            "Outlook Calendar",
            identity="microsoft.calendar.identity",
            read="microsoft.calendar.window_read",
        ),
        _recipe(
            "slack",
            "Slack",
            identity="slack.workspace.identity",
            read="slack.messages.recent_read",
        ),
        _recipe(
            "teams",
            "Microsoft Teams",
            identity="microsoft.teams.identity",
            read="microsoft.teams.recent_read",
        ),
        _recipe(
            "github",
            "GitHub",
            identity="github.account.identity",
            read="github.activity.recent_read",
        ),
        _recipe(
            "local_files",
            "Files you choose",
            identity=None,
            read="local.files.bounded_read",
            limit=100,
            ttl_hours=24 * 7,
        ),
        _recipe(
            "screen_context",
            "Optional screen context",
            identity=None,
            read="local.screen.derived_context_read",
            ttl_hours=6,
        ),
        _recipe(
            "whatsapp",
            "WhatsApp (experimental, read-only)",
            identity="wacli.account.identity",
            read="wacli.messages.recent_read",
            experimental=True,
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
