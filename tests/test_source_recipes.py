from __future__ import annotations

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.pulse import MECHANICAL_PULSE_PROFILE
from continuity_kernel.source_recipes import (
    RECIPE_SET_VERSION,
    RECIPES,
    get_recipe,
    list_recipes,
)


def test_supported_source_set_is_explicit_and_versioned() -> None:
    assert set(RECIPES) == {
        "codex_activity",
        "github",
        "gmail",
        "google_calendar",
        "gsv",
        "local_files",
        "outlook_calendar",
        "outlook_mail",
        "screen_context",
        "slack",
        "teams",
        "whatsapp",
    }
    assert all(recipe.recipe_version == RECIPE_SET_VERSION for recipe in RECIPES.values())
    assert all(recipe.read_limit > 0 for recipe in RECIPES.values())
    assert all(recipe.proof_ttl.total_seconds() > 0 for recipe in RECIPES.values())


def test_whatsapp_is_the_only_experimental_recipe() -> None:
    assert [source for source, recipe in RECIPES.items() if recipe.experimental] == ["whatsapp"]
    assert "read-only" in RECIPES["whatsapp"].label


def test_source_zero_has_long_lived_local_capabilities() -> None:
    source_zero = get_recipe("gsv")

    assert source_zero.identity_capability == "gsv.vault.identity"
    assert source_zero.read_capability == "gsv.context.bounded_read"
    assert source_zero.proof_ttl.days == 30


def test_pulse_profile_structurally_forbids_computer_use_and_mutation() -> None:
    assert "computer_use" in MECHANICAL_PULSE_PROFILE.forbidden
    assert "external_action" in MECHANICAL_PULSE_PROFILE.forbidden
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
