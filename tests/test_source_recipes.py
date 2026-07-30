from __future__ import annotations

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.source_recipes import (
    RECIPES,
    get_recipe,
    list_recipes,
)


def test_supported_source_set_is_explicit_and_versioned() -> None:
    assert set(RECIPES) == {
        "apple_messages",
        "asana",
        "atlassian",
        "box",
        "codex_activity",
        "discord",
        "figma",
        "github",
        "gmail",
        "google_calendar",
        "google_drive",
        "google_sheets",
        "gsv",
        "instagram",
        "local_files",
        "notion",
        "outlook_calendar",
        "outlook_mail",
        "screen_context",
        "sharepoint",
        "shopify",
        "slack",
        "teams",
        "whatsapp",
    }
    assert {recipe.recipe_version for recipe in RECIPES.values()} == {"1"}
    assert all(recipe.read_limit > 0 for recipe in RECIPES.values())
    assert all(recipe.proof_ttl.total_seconds() > 0 for recipe in RECIPES.values())


def test_promoted_source_catalog_uses_capabilities_not_dead_status_flags() -> None:
    assert all("experimental" not in recipe.to_dict() for recipe in RECIPES.values())
    assert all("interactive_only" not in recipe.to_dict() for recipe in RECIPES.values())
    assert all("pulse_capability" not in recipe.to_dict() for recipe in RECIPES.values())
    assert RECIPES["whatsapp"].label == "WhatsApp"


def test_consumer_source_ecosystem_has_bounded_read_recipes() -> None:
    for source in (
        "apple_messages",
        "asana",
        "atlassian",
        "box",
        "discord",
        "figma",
        "gmail",
        "google_calendar",
        "google_drive",
        "google_sheets",
        "instagram",
        "notion",
        "outlook_calendar",
        "outlook_mail",
        "shopify",
        "sharepoint",
        "slack",
        "teams",
        "whatsapp",
    ):
        recipe = RECIPES[source]
        assert recipe.identity_capability
        assert recipe.read_capability.endswith(".recent_read") or recipe.read_capability.endswith(
            ".window_read"
        )


def test_source_zero_has_long_lived_local_capabilities() -> None:
    source_zero = get_recipe("gsv")

    assert source_zero.identity_capability == "gsv.vault.identity"
    assert source_zero.read_capability == "gsv.context.bounded_read"
    assert source_zero.proof_ttl.days == 30


def test_source_recipes_expose_reads_not_sends() -> None:
    assert all("send" not in recipe.read_capability for recipe in RECIPES.values())


def test_serialized_recipes_do_not_expose_timedelta_or_tool_names() -> None:
    payload = list_recipes()

    assert payload == sorted(payload, key=lambda item: item["source"])
    assert all("proof_ttl" not in item for item in payload)
    assert all(item["proof_ttl_seconds"] > 0 for item in payload)
    assert all("tool" not in key for item in payload for key in item)


def test_unknown_source_fails_closed() -> None:
    with pytest.raises(ValidationError, match="unsupported source recipe"):
        get_recipe("personal_bank")
