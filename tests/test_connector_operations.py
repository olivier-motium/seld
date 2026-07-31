from __future__ import annotations

from collections import Counter

from continuity_kernel.connector_contract import ConnectorMode
from continuity_kernel.connector_operations import (
    ALL_CONNECTOR_OPERATIONS,
    CONNECTOR_TOOL_NAMES,
    OPERATION_CATALOG,
    connector_tool_definitions,
)


def test_catalog_is_exactly_seven_provider_pairs_and_178_operations() -> None:
    assert Counter(
        (operation.provider, operation.mode) for operation in ALL_CONNECTOR_OPERATIONS
    ) == {
        ("discord", ConnectorMode.READ): 11,
        ("discord", ConnectorMode.WRITE): 21,
        ("gmail", ConnectorMode.READ): 8,
        ("gmail", ConnectorMode.WRITE): 15,
        ("google_calendar", ConnectorMode.READ): 6,
        ("google_calendar", ConnectorMode.WRITE): 8,
        ("google_drive", ConnectorMode.READ): 9,
        ("google_drive", ConnectorMode.WRITE): 18,
        ("outlook_calendar", ConnectorMode.READ): 9,
        ("outlook_calendar", ConnectorMode.WRITE): 15,
        ("outlook_mail", ConnectorMode.READ): 7,
        ("outlook_mail", ConnectorMode.WRITE): 19,
        ("slack", ConnectorMode.READ): 12,
        ("slack", ConnectorMode.WRITE): 20,
    }
    assert len(ALL_CONNECTOR_OPERATIONS) == 178
    assert OPERATION_CATALOG.providers() == (
        "discord",
        "gmail",
        "google_calendar",
        "google_drive",
        "outlook_calendar",
        "outlook_mail",
        "slack",
    )


def test_connector_profile_exposes_only_the_fourteen_closed_tools() -> None:
    expected_names = {
        "gsv_discord_read",
        "gsv_discord_write",
        "gsv_gmail_read",
        "gsv_gmail_write",
        "gsv_google_calendar_read",
        "gsv_google_calendar_write",
        "gsv_google_drive_read",
        "gsv_google_drive_write",
        "gsv_outlook_calendar_read",
        "gsv_outlook_calendar_write",
        "gsv_outlook_mail_read",
        "gsv_outlook_mail_write",
        "gsv_slack_read",
        "gsv_slack_write",
    }
    assert expected_names == CONNECTOR_TOOL_NAMES
    tools = connector_tool_definitions()
    assert {tool["name"] for tool in tools} == CONNECTOR_TOOL_NAMES
    assert len(tools) == 14
    for tool in tools:
        annotations = tool["annotations"]
        assert isinstance(annotations, dict)
        assert annotations["openWorldHint"] is True
        read_only = str(tool["name"]).endswith("_read")
        assert annotations["readOnlyHint"] is read_only
        assert annotations["destructiveHint"] is not read_only


def test_every_tool_branch_has_only_typed_operation_envelope_fields() -> None:
    for tool in connector_tool_definitions():
        schema = tool["inputSchema"]
        assert isinstance(schema, dict)
        branches = schema["oneOf"]
        assert isinstance(branches, list)
        assert branches
        for branch in branches:
            properties = branch["properties"]
            expected = {"connection_id", "input", "operation"}
            expected.add("cursor" if str(tool["name"]).endswith("_read") else "confirmation_token")
            assert set(properties) == expected
            assert branch["additionalProperties"] is False
