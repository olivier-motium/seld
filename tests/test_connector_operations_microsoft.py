from __future__ import annotations

import pytest

from continuity_kernel.connector_contract import (
    ConnectorEffect,
    ConnectorMode,
    OperationCatalog,
    OperationSpec,
    validate_json,
)
from continuity_kernel.connector_operations_microsoft import MICROSOFT_OPERATIONS
from continuity_kernel.errors import ValidationError

MAIL_NAMES = frozenset(
    {
        "folders.list",
        "folders.get",
        "messages.list",
        "messages.get",
        "messages.mime",
        "attachments.list",
        "attachments.get",
        "folders.create",
        "folders.update",
        "folders.move",
        "folders.trash",
        "folders.restore",
        "folders.purge",
        "drafts.create",
        "drafts.update",
        "drafts.reply",
        "drafts.reply_all",
        "drafts.forward",
        "attachments.add",
        "attachments.delete",
        "drafts.send",
        "messages.update",
        "messages.copy",
        "messages.move",
        "messages.trash",
        "messages.restore",
        "messages.purge",
    }
)
CALENDAR_NAMES = frozenset(
    {
        "calendars.list",
        "calendars.get",
        "events.list",
        "events.get",
        "events.window",
        "events.instances",
        "freebusy.query",
        "attachments.list",
        "attachments.get",
        "calendars.create",
        "calendars.update",
        "calendars.delete",
        "calendars.purge",
        "events.create",
        "events.update",
        "events.delete",
        "events.cancel",
        "events.accept",
        "events.tentative",
        "events.decline",
        "events.forward",
        "attachments.add",
        "attachments.delete",
        "events.purge",
    }
)


@pytest.fixture
def catalog() -> OperationCatalog:
    return OperationCatalog(MICROSOFT_OPERATIONS)


def _mail_draft(body: str = "Hello") -> dict[str, object]:
    return {
        "body": {"content": body, "content_type": "text"},
        "subject": "Hello from the catalog",
        "to_recipients": [{"email": "ada@example.test", "name": "Ada"}],
    }


def _event(body: str = "Planning") -> dict[str, object]:
    return {
        "attendees": [{"email": "ada@example.test", "type": "required"}],
        "body": {"content": body, "content_type": "html"},
        "calendar_id": "calendar-1",
        "end": {"date_time": "2026-08-01T10:00:00", "time_zone": "Europe/Brussels"},
        "start": {"date_time": "2026-08-01T09:00:00", "time_zone": "Europe/Brussels"},
        "subject": "Planning",
        "transaction_id": "client-created-1",
    }


def test_microsoft_catalog_has_the_exact_mail_and_calendar_operation_sets() -> None:
    mail = tuple(
        operation for operation in MICROSOFT_OPERATIONS if operation.provider == "outlook_mail"
    )
    calendar = tuple(
        operation for operation in MICROSOFT_OPERATIONS if operation.provider == "outlook_calendar"
    )

    assert len(MICROSOFT_OPERATIONS) == 51
    assert len(mail) == 27
    assert len(calendar) == 24
    assert {operation.name for operation in mail} == MAIL_NAMES
    assert {operation.name for operation in calendar} == CALENDAR_NAMES
    assert all(operation.endpoint == operation.name for operation in MICROSOFT_OPERATIONS)


def test_effects_and_scope_alternatives_are_bounded_to_the_named_operation() -> None:
    catalog = OperationCatalog(MICROSOFT_OPERATIONS)
    mail_read = catalog.lookup("outlook_mail", ConnectorMode.READ, "messages.get")
    mail_send = catalog.lookup("outlook_mail", ConnectorMode.WRITE, "drafts.send")
    folder_trash = catalog.lookup("outlook_mail", ConnectorMode.WRITE, "folders.trash")
    calendar_accept = catalog.lookup("outlook_calendar", ConnectorMode.WRITE, "events.accept")
    calendar_purge = catalog.lookup("outlook_calendar", ConnectorMode.WRITE, "events.purge")

    assert mail_read.effect is ConnectorEffect.READ
    assert mail_read.required_scopes == (
        frozenset({"Mail.Read"}),
        frozenset({"Mail.ReadWrite"}),
    )
    assert folder_trash.effect is ConnectorEffect.DESTRUCTIVE
    assert mail_send.effect is ConnectorEffect.OUTWARD
    assert mail_send.required_scopes == (frozenset({"Mail.ReadWrite", "Mail.Send"}),)
    assert calendar_accept.effect is ConnectorEffect.OUTWARD
    assert calendar_purge.effect is ConnectorEffect.PERMANENT
    assert catalog.lookup("outlook_calendar", ConnectorMode.READ, "events.get").required_scopes == (
        frozenset({"Calendars.Read"}),
        frozenset({"Calendars.ReadWrite"}),
    )


def test_representative_message_event_and_attachment_inputs_are_exact(
    catalog: OperationCatalog,
) -> None:
    draft = catalog.validate_input(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.create",
        _mail_draft(),
    )
    attachment = catalog.validate_input(
        "outlook_mail",
        ConnectorMode.WRITE,
        "attachments.add",
        {
            "attachment": {
                "content_base64": "aGVsbG8=",
                "content_type": "text/plain",
                "name": "hello.txt",
            },
            "change_key": "provider-version-1",
            "message_id": "message-1",
        },
    )
    event = catalog.validate_input(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.create",
        {
            **_event(),
            "importance": "high",
            "is_all_day": False,
            "response_requested": False,
            "sensitivity": "private",
            "show_as": "working_elsewhere",
        },
    )
    message_update = catalog.validate_input(
        "outlook_mail",
        ConnectorMode.WRITE,
        "messages.update",
        {
            "categories": ["Follow up"],
            "follow_up": "flagged",
            "importance": "high",
            "is_read": True,
            "message_id": "message-1",
        },
    )

    assert draft["body"] == {"content": "Hello", "content_type": "text"}
    assert attachment["attachment"]["name"] == "hello.txt"
    assert event["start"]["time_zone"] == "Europe/Brussels"
    assert event["show_as"] == "working_elsewhere"
    assert message_update["follow_up"] == "flagged"


def test_message_page_size_and_follow_up_status_are_bounded(catalog: OperationCatalog) -> None:
    assert catalog.validate_input(
        "outlook_mail",
        ConnectorMode.READ,
        "messages.list",
        {"page_size": 1_000},
    ) == {"page_size": 1_000}
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_mail",
            ConnectorMode.READ,
            "messages.list",
            {"page_size": 1_001},
        )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_mail",
            ConnectorMode.WRITE,
            "messages.update",
            {"follow_up": "later", "message_id": "message-1"},
        )


def test_unknown_and_proxy_like_input_fields_fail(catalog: OperationCatalog) -> None:
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_mail",
            ConnectorMode.WRITE,
            "drafts.create",
            {**_mail_draft(), "method": "POST"},
        )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.create",
            {**_event(), "callback_url": "https://proxy.invalid"},
        )
    with pytest.raises(ValidationError):
        OperationSpec(
            provider="outlook_mail",
            mode=ConnectorMode.READ,
            name="messages.proxy",
            effect=ConnectorEffect.READ,
            endpoint="messages.proxy",
            required_scopes=(frozenset({"Mail.Read"}),),
            input_schema={
                "additionalProperties": False,
                "properties": {"proxy_url": {"type": "string"}},
                "required": [],
                "type": "object",
            },
        )


def test_tool_envelopes_are_sealed_to_connection_operation_input_and_control_state(
    catalog: OperationCatalog,
) -> None:
    read_schema = catalog.tool_input_schema("outlook_mail", ConnectorMode.READ)
    write_schema = catalog.tool_input_schema("outlook_calendar", ConnectorMode.WRITE)
    read_call = {
        "connection_id": "con-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "cursor": "v1.payload.mac",
        "input": {"message_id": "message-1"},
        "operation": "messages.get",
    }
    write_call = {
        "confirmation_token": "v1.payload.mac",
        "connection_id": "con-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "input": _event(),
        "operation": "events.create",
    }

    assert validate_json(read_call, read_schema) == read_call
    assert validate_json(write_call, write_schema) == write_call
    assert all(
        set(variant["properties"]) <= {"connection_id", "cursor", "input", "operation"}
        for variant in read_schema["oneOf"]
    )
    assert all(
        set(variant["properties"]) <= {"confirmation_token", "connection_id", "input", "operation"}
        for variant in write_schema["oneOf"]
    )
    with pytest.raises(ValidationError):
        validate_json({**read_call, "url": "https://proxy.invalid"}, read_schema)
    with pytest.raises(ValidationError):
        validate_json({**write_call, "cursor": "v1.payload.mac"}, write_schema)


def test_scope_checks_and_large_message_and_event_bodies(catalog: OperationCatalog) -> None:
    send = catalog.lookup("outlook_mail", ConnectorMode.WRITE, "drafts.send")
    write = catalog.lookup("outlook_mail", ConnectorMode.WRITE, "drafts.update")
    calendar_write = catalog.lookup("outlook_calendar", ConnectorMode.WRITE, "events.update")
    large_body = "x" * 200_000

    assert not send.scope_grant_satisfies({"Mail.Send"})
    assert send.scope_grant_satisfies({"Mail.ReadWrite", "Mail.Send"})
    assert not write.scope_grant_satisfies({"Mail.Read"})
    assert not calendar_write.scope_grant_satisfies({"Calendars.Read"})
    assert catalog.validate_input(
        "outlook_mail",
        ConnectorMode.WRITE,
        "drafts.create",
        _mail_draft(large_body),
    )
    assert catalog.validate_input(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.create",
        _event(large_body),
    )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_mail",
            ConnectorMode.WRITE,
            "drafts.create",
            _mail_draft("x" * 200_001),
        )
