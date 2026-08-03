from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import continuity_kernel.cli as cli
import continuity_kernel.mcp_server as mcp_server
from continuity_kernel.connector_operations import CONNECTOR_PROFILE, CONNECTOR_TOOL_NAMES
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def call_tool(self, name: str, values: dict[str, Any]) -> dict[str, object]:
        self.calls.append((name, values))
        return {"operation": values["operation"], "status": "synthetic-ok"}

    def close(self) -> None:
        self.closed = True


def test_connector_profile_advertises_exactly_fourteen_closed_tools() -> None:
    initialized = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize"},
        profile=CONNECTOR_PROFILE,
    )
    assert initialized is not None
    assert initialized["result"]["serverInfo"]["name"] == "gsv-connectors"
    assert "confirmation_required" in initialized["result"]["instructions"]

    response = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        profile=CONNECTOR_PROFILE,
    )
    assert response is not None
    tools = response["result"]["tools"]
    assert {tool["name"] for tool in tools} == CONNECTOR_TOOL_NAMES
    assert len(tools) == 14
    assert all(set(tool["inputSchema"]) == {"oneOf"} for tool in tools)
    assert all(tool["annotations"]["openWorldHint"] is True for tool in tools)

    default = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert default is not None
    assert not CONNECTOR_TOOL_NAMES.intersection(
        tool["name"] for tool in default["result"]["tools"]
    )


def test_connector_profile_dispatches_only_after_exact_schema_validation() -> None:
    runtime = _Runtime()
    valid = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "gsv_gmail_read",
                "arguments": {
                    "connection_id": "con-" + "a" * 32,
                    "input": {"page_size": 5},
                    "operation": "messages.list",
                },
            },
        },
        connector_runtime=cast(ConnectorRuntime, runtime),
        profile=CONNECTOR_PROFILE,
    )
    assert valid is not None
    assert valid["result"]["structuredContent"] == {
        "operation": "messages.list",
        "status": "synthetic-ok",
    }
    assert [name for name, _values in runtime.calls] == ["gsv_gmail_read"]

    invalid = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "gsv_gmail_read",
                "arguments": {
                    "connection_id": "con-" + "a" * 32,
                    "input": {"page_size": 5, "url": "https://attacker.invalid"},
                    "operation": "messages.list",
                },
            },
        },
        connector_runtime=cast(ConnectorRuntime, runtime),
        profile=CONNECTOR_PROFILE,
    )
    assert invalid is not None
    assert invalid["result"]["isError"] is True
    assert [name for name, _values in runtime.calls] == ["gsv_gmail_read"]


def test_connector_profile_rejects_vault_tools_and_event_binding() -> None:
    runtime = _Runtime()
    rejected = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "gsv_status", "arguments": {}},
        },
        connector_runtime=cast(ConnectorRuntime, runtime),
        profile=CONNECTOR_PROFILE,
    )
    assert rejected is not None
    assert rejected["result"]["isError"] is True
    assert runtime.calls == []

    with pytest.raises(ValidationError, match="does not accept an event"):
        mcp_server._handle(
            {"jsonrpc": "2.0", "id": 2, "method": "initialize"},
            profile=CONNECTOR_PROFILE,
            event_id="00000000-0000-0000-0000-000000000001",
        )


def test_cli_accepts_connector_profile_without_guided_review_event() -> None:
    args = cli._parser().parse_args(["mcp", "serve", "--profile", CONNECTOR_PROFILE])
    assert args.profile == CONNECTOR_PROFILE
    assert args.event_id is None


def test_connector_profile_requires_its_process_runtime(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="One-shot connector runtime")
    with pytest.raises(ValidationError, match="runtime is unavailable"):
        mcp_server._call(
            "gsv_gmail_read",
            {
                "connection_id": "con-" + "a" * 32,
                "input": {"page_size": 5},
                "operation": "messages.list",
            },
            vault=vault,
            profile=CONNECTOR_PROFILE,
        )


def test_connector_server_closes_process_runtime_on_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Connector server runtime")
    runtime = _Runtime()
    monkeypatch.setattr(mcp_server, "ConnectorRuntime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(mcp_server, "_bounded_lines", lambda _stream: iter(()))
    assert mcp_server.serve(vault, profile=CONNECTOR_PROFILE) == 0
    assert runtime.closed is True
