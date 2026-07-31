from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuity_kernel.connector_client_registration import (
    CLIENT_REGISTRATION_SCHEMA_VERSION,
    load_public_client_registration,
    load_public_client_registrations,
)
from continuity_kernel.errors import SetupError, ValidationError


def _encoded(providers: dict[str, object]) -> bytes:
    return json.dumps(
        {"schema_version": CLIENT_REGISTRATION_SCHEMA_VERSION, "providers": providers}
    ).encode()


def _all_providers() -> dict[str, object]:
    return {
        "google": {
            "client_id": "google-public.apps.example",
            "redirect_template": "http://127.0.0.1:0",
        },
        "microsoft": {
            "client_id": "microsoft-public",
            "redirect_template": "http://localhost:0/oauth/callback",
        },
        "slack": {
            "client_id": "slack-public",
            "redirect_template": "http://localhost:43127/oauth/callback",
        },
    }


def test_loader_reads_public_ids_from_injected_bytes_and_never_needs_user_id_input() -> None:
    registrations = load_public_client_registrations(source_bytes=_encoded(_all_providers()))

    assert registrations["google"].client_id == "google-public.apps.example"
    assert registrations["microsoft"].redirect_uri_template.endswith("/oauth/callback")
    assert registrations["slack"].redirect_uri == "http://localhost:43127/oauth/callback"
    assert (
        load_public_client_registration("google", source_bytes=_encoded(_all_providers()))
        == (registrations["google"])
    )


def test_loader_accepts_an_explicit_injected_path(tmp_path: Path) -> None:
    source = tmp_path / "connector_clients.json"
    source.write_bytes(_encoded({"google": _all_providers()["google"]}))

    registration = load_public_client_registration("google", source_path=source)

    assert registration.client_id == "google-public.apps.example"


def test_missing_provider_is_a_friendly_setup_error_without_writing_anything(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "not-created.json"
    with pytest.raises(
        SetupError,
        match=r"sign-in is unavailable in this build.*nothing was saved",
    ):
        load_public_client_registration(
            "slack",
            source_bytes=_encoded(
                {"google": _all_providers()["google"], "microsoft": _all_providers()["microsoft"]}
            ),
        )
    assert not missing.exists()


@pytest.mark.parametrize(
    ("provider", "redirect_template"),
    [
        ("google", "http://localhost:0"),
        ("google", "http://127.0.0.1:0/"),
        ("microsoft", "http://127.0.0.1:0/oauth/callback"),
        ("microsoft", "http://127.0.0.1:7000/oauth/callback"),
        ("slack", "http://localhost:0/oauth/callback"),
        ("slack", "http://127.0.0.1:43127/oauth/callback"),
    ],
)
def test_provider_redirect_templates_are_validated(provider: str, redirect_template: str) -> None:
    provider_entry = {"client_id": "public-client", "redirect_template": redirect_template}
    with pytest.raises(ValidationError, match="redirect template"):
        load_public_client_registration(provider, source_bytes=_encoded({provider: provider_entry}))


def test_loader_is_strict_bounded_and_rejects_secrets_or_unknown_shapes() -> None:
    with pytest.raises(ValidationError, match="client secrets"):
        load_public_client_registration(
            "google",
            source_bytes=_encoded(
                {
                    "google": {
                        "client_id": "public-client",
                        "redirect_template": "http://127.0.0.1:0",
                        "client_secret": "must-not-be-accepted",
                    }
                }
            ),
        )
    with pytest.raises(ValidationError, match="schema version"):
        load_public_client_registration(
            "google",
            source_bytes=json.dumps({"schema_version": 2, "providers": {}}).encode(),
        )
    with pytest.raises(ValidationError, match="size bound"):
        load_public_client_registration("google", source_bytes=b"{" + b" " * (64 * 1024) + b"}")
    with pytest.raises(ValidationError, match="valid JSON"):
        load_public_client_registration("google", source_bytes=b"not-json")
