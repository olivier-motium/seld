from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from continuity_kernel import auth_cli
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_secrets import InMemorySecretStore
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
