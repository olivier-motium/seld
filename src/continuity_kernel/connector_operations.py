"""Finite interactive connector operation catalog and MCP tool bindings."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from continuity_kernel.connector_contract import ConnectorMode, OperationCatalog
from continuity_kernel.connector_operations_collaboration import COLLABORATION_OPERATIONS
from continuity_kernel.connector_operations_google import GOOGLE_OPERATIONS
from continuity_kernel.connector_operations_microsoft import MICROSOFT_OPERATIONS

CONNECTOR_PROFILE: Final = "connectors"

ALL_CONNECTOR_OPERATIONS: Final = (
    GOOGLE_OPERATIONS + MICROSOFT_OPERATIONS + COLLABORATION_OPERATIONS
)
OPERATION_CATALOG: Final = OperationCatalog(ALL_CONNECTOR_OPERATIONS)

_TOOL_STEMS: Final = MappingProxyType(
    {
        "discord": "discord",
        "gmail": "gmail",
        "google_calendar": "google_calendar",
        "google_drive": "google_drive",
        "outlook_calendar": "outlook_calendar",
        "outlook_mail": "outlook_mail",
        "slack": "slack",
    }
)

CONNECTOR_TOOL_BINDINGS: Final = MappingProxyType(
    {
        f"gsv_{stem}_{mode.value}": (provider, mode)
        for provider, stem in _TOOL_STEMS.items()
        for mode in (ConnectorMode.READ, ConnectorMode.WRITE)
    }
)
CONNECTOR_TOOL_NAMES: Final = frozenset(CONNECTOR_TOOL_BINDINGS)


def connector_tool_definitions() -> list[dict[str, object]]:
    """Return the exact fourteen provider read/write tools for the connector profile."""

    tools: list[dict[str, object]] = []
    for name, (provider, mode) in CONNECTOR_TOOL_BINDINGS.items():
        read_only = mode is ConnectorMode.READ
        tools.append(
            {
                "annotations": {
                    "destructiveHint": not read_only,
                    "idempotentHint": read_only,
                    "openWorldHint": True,
                    "readOnlyHint": read_only,
                },
                "description": _tool_description(provider, mode),
                "inputSchema": OPERATION_CATALOG.tool_input_schema(provider, mode),
                "name": name,
            }
        )
    return tools


def _tool_description(provider: str, mode: ConnectorMode) -> str:
    label = provider.replace("_", " ").title()
    if mode is ConnectorMode.READ:
        return (
            f"Read {label} through one exact Seld connection and a closed operation schema. "
            "Continuation state is accepted only as a short-lived sealed cursor."
        )
    return (
        f"Change {label} through one exact Seld connection and a closed operation schema. "
        "Outward, destructive, and permanent effects require an exact short-lived confirmation."
    )
