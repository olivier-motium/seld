from __future__ import annotations

from pathlib import Path

import pytest

import continuity_kernel.mcp_server as mcp_server
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault


def test_connector_reader_mcp_is_finite_and_not_a_credentialed_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Connector MCP test")
    tool = next(item for item in mcp_server.TOOLS if item["name"] == "gsv_connector_source_read")
    schema = tool["inputSchema"]
    assert set(schema["properties"]) == {"connection_id", "limit", "source"}
    assert schema["properties"]["source"]["enum"] == [
        "gmail",
        "google_calendar",
        "google_drive",
        "outlook_calendar",
        "outlook_mail",
        "slack",
    ]

    observed: dict[str, object] = {}

    def read(
        observed_vault: Vault,
        *,
        connection_id: str,
        source_id: str,
        limit: int,
        timeout_seconds: float,
    ) -> dict[str, object]:
        observed.update(
            {
                "connection_id": connection_id,
                "limit": limit,
                "source": source_id,
                "timeout": timeout_seconds,
                "vault": observed_vault,
            }
        )
        return {"result": "explicit_empty", "items": []}

    monkeypatch.setattr(mcp_server, "read_connector_source", read)
    result = mcp_server._call(
        "gsv_connector_source_read",
        {
            "connection_id": "con-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "source": "gmail",
            "limit": 3,
        },
        vault=vault,
    )
    assert result == {"result": "explicit_empty", "items": []}
    assert observed == {
        "connection_id": "con-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "limit": 3,
        "source": "gmail",
        "timeout": 15.0,
        "vault": vault,
    }

    mcp_server._call(
        "gsv_connector_source_read",
        {
            "connection_id": "con-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "source": "slack",
            "limit": 25,
        },
        vault=vault,
    )
    assert observed["timeout"] == 45.0

    with pytest.raises(ValidationError, match="unknown field url"):
        mcp_server._call(
            "gsv_connector_source_read",
            {
                "connection_id": "con-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "source": "gmail",
                "url": "https://attacker.example/",
            },
            vault=vault,
        )
