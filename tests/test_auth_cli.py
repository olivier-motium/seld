from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from continuity_kernel import auth_cli
from continuity_kernel.connector_auth import CredentialKind
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault


class BinaryInput:
    def __init__(self, value: bytes) -> None:
        self.buffer = io.BytesIO(value)

    def isatty(self) -> bool:
        return False


def test_gsv_auth_add_and_stdin_credential_path_never_serializes_the_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Auth CLI")
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    parser = auth_cli._parser()
    added = auth_cli._dispatch(
        parser.parse_args(
            [
                "add",
                "--provider",
                "github",
                "--source",
                "github",
                "--kind",
                "api_key",
                "--label",
                "Synthetic",
            ]
        ),
        manager,
    )
    connections = added["connections"]
    assert isinstance(connections, list)
    first_connection = connections[0]
    assert isinstance(first_connection, dict)
    connection_id = first_connection["connection_id"]
    assert isinstance(connection_id, str)
    sentinel = b"cli-secret-from-stdin"
    monkeypatch.setattr(sys, "stdin", BinaryInput(sentinel))

    result = auth_cli._dispatch(
        parser.parse_args(["credential", connection_id]),
        manager,
    )

    connection = result["connection"]
    assert isinstance(connection, dict)
    assert connection["host_credential"] == "available"
    assert sentinel.decode() not in json.dumps(result, sort_keys=True)
    assert sentinel not in (vault.root / "CONNECTIONS.md").read_bytes()
    with pytest.raises(SystemExit):
        parser.parse_args(["credential", connection_id, "--token", sentinel.decode()])


def test_profile_add_creates_exact_google_oauth_metadata(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Google profile")
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    parser = auth_cli._parser()

    auth_cli._dispatch(
        parser.parse_args(
            [
                "add",
                "--profile",
                "google",
                "--client-id",
                "public-google-client",
                "--redirect-uri",
                "http://127.0.0.1:0",
                "--label",
                "Personal Google",
            ]
        ),
        manager,
    )

    connection = vault.get_connection_snapshot().connections[0]
    assert connection.provider == "google"
    assert connection.source_ids == ("gmail", "google_calendar", "google_drive")
    assert connection.credential_kind is CredentialKind.OAUTH2
    assert connection.scopes == (
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    assert connection.client.identifier == "public-google-client"
    assert connection.client.redirect_uris == ("http://127.0.0.1:0",)
    assert connection.client.authorization_endpoint == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert connection.client.token_endpoint == "https://oauth2.googleapis.com/token"


def test_discord_profile_rejects_oauth_fields_and_keeps_external_custody(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Discord profile")
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    parser = auth_cli._parser()

    with pytest.raises(ValidationError, match="OAuth client arguments"):
        auth_cli._dispatch(
            parser.parse_args(["add", "--profile", "discord", "--client-id", "not-allowed"]),
            manager,
        )
    auth_cli._dispatch(parser.parse_args(["add", "--profile", "discord"]), manager)

    connection = vault.get_connection_snapshot().connections[0]
    assert connection.provider == "discord"
    assert connection.source_ids == ("discord",)
    assert connection.credential_kind is CredentialKind.BEARER
    assert connection.client.identifier is None


def test_manual_oauth_requires_a_built_in_profile(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Manual OAuth")
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    parser = auth_cli._parser()

    with pytest.raises(ValidationError, match="use a built-in profile"):
        auth_cli._dispatch(
            parser.parse_args(
                [
                    "add",
                    "--provider",
                    "example",
                    "--source",
                    "example",
                    "--kind",
                    "oauth2",
                    "--client-id",
                    "client",
                    "--redirect-uri",
                    "http://127.0.0.1:49152/callback",
                    "--authorization-endpoint",
                    "https://example.test/authorize",
                    "--token-endpoint",
                    "https://example.test/token",
                ]
            ),
            manager,
        )


def test_profile_owned_fields_cannot_be_overridden(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Profile override")
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    parser = auth_cli._parser()

    with pytest.raises(ValidationError, match="cannot be overridden"):
        auth_cli._dispatch(
            parser.parse_args(
                [
                    "add",
                    "--profile",
                    "slack",
                    "--scope",
                    "chat:write",
                    "--client-id",
                    "public-slack-client",
                    "--redirect-uri",
                    "http://127.0.0.1:49152/oauth/callback",
                ]
            ),
            manager,
        )

    with pytest.raises(ValidationError, match="exact registered"):
        auth_cli._dispatch(
            parser.parse_args(
                [
                    "add",
                    "--profile",
                    "slack",
                    "--client-id",
                    "public-slack-client",
                    "--redirect-uri",
                    "http://localhost:0/oauth/callback",
                ]
            ),
            manager,
        )

    with pytest.raises(ValidationError, match="Google profile requires"):
        auth_cli._dispatch(
            parser.parse_args(
                [
                    "add",
                    "--profile",
                    "google",
                    "--client-id",
                    "public-google-client",
                    "--redirect-uri",
                    "http://localhost:49152/oauth/callback",
                ]
            ),
            manager,
        )
