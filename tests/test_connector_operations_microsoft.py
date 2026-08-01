from __future__ import annotations

from typing import cast

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

    draft_value = cast(dict[str, object], draft)
    attachment_value = cast(dict[str, object], attachment)
    event_value = cast(dict[str, object], event)
    update_value = cast(dict[str, object], message_update)
    assert draft_value["body"] == {"content": "Hello", "content_type": "text"}
    assert cast(dict[str, object], attachment_value["attachment"])["name"] == "hello.txt"
    assert cast(dict[str, object], event_value["start"])["time_zone"] == "Europe/Brussels"
    assert event_value["show_as"] == "working_elsewhere"
    assert update_value["follow_up"] == "flagged"


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


def test_message_list_schema_exposes_only_provider_valid_query_combinations(
    catalog: OperationCatalog,
) -> None:
    valid = (
        {},
        {"fields": ["id", "subject"], "folder_id": "inbox", "page_size": 1_000},
        {"is_read": False, "page_size": 25},
        {"order_by": "received_at"},
        {"order_by": "subject", "sort_direction": "descending"},
        {"search": "from:ada@example.test", "page_size": 50},
    )
    for value in valid:
        assert (
            catalog.validate_input(
                "outlook_mail",
                ConnectorMode.READ,
                "messages.list",
                value,
            )
            == value
        )

    invalid = (
        {"fields": ["internet_message_headers"]},
        {"sort_direction": "ascending"},
        {"is_read": True, "order_by": "received_at"},
        {"is_read": True, "search": "subject:planning"},
        {"order_by": "sent_at", "search": "subject:planning"},
        {"search": "subject:planning", "sort_direction": "descending"},
    )
    for value in invalid:
        with pytest.raises(ValidationError):
            catalog.validate_input(
                "outlook_mail",
                ConnectorMode.READ,
                "messages.list",
                value,
            )


def test_calendar_read_schemas_expose_only_supported_query_fields(
    catalog: OperationCatalog,
) -> None:
    calendars = catalog.lookup("outlook_calendar", ConnectorMode.READ, "calendars.list")
    events_list = catalog.lookup("outlook_calendar", ConnectorMode.READ, "events.list")
    events_get = catalog.lookup("outlook_calendar", ConnectorMode.READ, "events.get")
    window = catalog.lookup("outlook_calendar", ConnectorMode.READ, "events.window")
    instances = catalog.lookup("outlook_calendar", ConnectorMode.READ, "events.instances")

    calendar_properties = cast(dict[str, object], calendars.input_schema["properties"])
    calendar_order = cast(dict[str, object], calendar_properties["order_by"])
    assert "search" not in calendar_properties
    assert calendar_order["enum"] == ("name",)

    event_list_properties = cast(dict[str, object], events_list.input_schema["properties"])
    assert "search" not in event_list_properties
    assert "time_zone" in event_list_properties
    assert "time_zone" in cast(dict[str, object], events_get.input_schema["properties"])
    for operation in (window, instances):
        properties = cast(dict[str, object], operation.input_schema["properties"])
        page_size = cast(dict[str, object], properties["page_size"])
        assert page_size["minimum"] == 1
        assert page_size["maximum"] == 1_000


def test_freebusy_attendees_and_interval_are_provider_bounded(catalog: OperationCatalog) -> None:
    base = {
        "attendees": ["person@example.test"],
        "end": "2026-08-01T10:00:00",
        "start": "2026-08-01T09:00:00",
    }
    valid = catalog.validate_input(
        "outlook_calendar",
        ConnectorMode.READ,
        "freebusy.query",
        {**base, "interval_minutes": 30},
    )
    assert cast(dict[str, object], valid)["interval_minutes"] == 30
    maximum_schedules = [f"person-{index}@example.test" for index in range(20)]
    bounded = catalog.validate_input(
        "outlook_calendar",
        ConnectorMode.READ,
        "freebusy.query",
        {**base, "attendees": maximum_schedules},
    )
    assert cast(dict[str, object], bounded)["attendees"] == maximum_schedules
    for interval in (5, 1_440):
        boundary = catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.READ,
            "freebusy.query",
            {**base, "interval_minutes": interval},
        )
        assert cast(dict[str, object], boundary)["interval_minutes"] == interval
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.READ,
            "freebusy.query",
            {
                **base,
                "attendees": [f"person-{index}@example.test" for index in range(21)],
            },
        )
    for interval in (4, 1_441):
        with pytest.raises(ValidationError):
            catalog.validate_input(
                "outlook_calendar",
                ConnectorMode.READ,
                "freebusy.query",
                {**base, "interval_minutes": interval},
            )


def test_event_attendees_match_graphs_500_person_limit(catalog: OperationCatalog) -> None:
    attendees = [
        {"email": f"person-{index}@example.test", "type": "required"} for index in range(500)
    ]
    validated = catalog.validate_input(
        "outlook_calendar",
        ConnectorMode.WRITE,
        "events.create",
        {**_event(), "attendees": attendees},
    )
    assert len(cast(dict[str, object], validated)["attendees"]) == 500

    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.create",
            {
                **_event(),
                "attendees": [
                    *attendees,
                    {"email": "person-500@example.test", "type": "required"},
                ],
            },
        )


def test_existing_event_mutations_require_the_confirmed_change_key(
    catalog: OperationCatalog,
) -> None:
    update = {
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
        "subject": "Updated",
    }
    attachment = {
        "attachment": {
            "content_base64": "aGVsbG8=",
            "content_type": "text/plain",
            "name": "hello.txt",
        },
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
    }
    delete = {
        "calendar_id": "primary",
        "change_key": "event-version-1",
        "event_id": "event-1",
    }
    purge = dict(delete)

    assert (
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.update",
            update,
        )
        == update
    )
    assert (
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "attachments.add",
            attachment,
        )
        == attachment
    )
    assert (
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.delete",
            delete,
        )
        == delete
    )
    assert (
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.purge",
            purge,
        )
        == purge
    )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.update",
            {key: value for key, value in update.items() if key != "change_key"},
        )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "attachments.add",
            {key: value for key, value in attachment.items() if key != "change_key"},
        )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.delete",
            {key: value for key, value in delete.items() if key != "change_key"},
        )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.purge",
            {key: value for key, value in purge.items() if key != "change_key"},
        )


def test_recurrence_contract_enforces_each_graph_pattern_and_range_shape(
    catalog: OperationCatalog,
) -> None:
    base = _event()
    patterns = (
        {"type": "daily", "interval": 2},
        {
            "type": "weekly",
            "interval": 1,
            "days_of_week": ["monday"],
            "first_day_of_week": "monday",
        },
        {"type": "absolute_monthly", "interval": 1, "day_of_month": 15},
        {
            "type": "relative_monthly",
            "interval": 1,
            "days_of_week": ["thursday"],
            "index": "second",
        },
        {
            "type": "absolute_yearly",
            "interval": 1,
            "day_of_month": 15,
            "month": 3,
        },
        {
            "type": "relative_yearly",
            "interval": 1,
            "days_of_week": ["thursday"],
            "index": "second",
            "month": 11,
        },
    )
    ranges = (
        {"type": "no_end", "start_date": "2026-08-01"},
        {
            "type": "end_date",
            "start_date": "2026-08-01",
            "end_date": "2026-12-31",
            "recurrence_time_zone": "Europe/Brussels",
        },
        {
            "type": "numbered",
            "start_date": "2026-08-01",
            "number_of_occurrences": 10,
        },
    )

    for pattern in patterns:
        assert catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.create",
            {**base, "recurrence": {"pattern": pattern, "range": ranges[0]}},
        )
    for event_range in ranges:
        assert catalog.validate_input(
            "outlook_calendar",
            ConnectorMode.WRITE,
            "events.create",
            {**base, "recurrence": {"pattern": patterns[0], "range": event_range}},
        )

    invalid_patterns = (
        {"type": "weekly", "interval": 1, "days_of_week": ["monday"]},
        {"type": "absolute_monthly", "interval": 1},
        {
            "type": "relative_monthly",
            "interval": 1,
            "days_of_week": ["monday"],
        },
        {"type": "absolute_yearly", "interval": 1, "day_of_month": 1},
        {
            "type": "relative_yearly",
            "interval": 1,
            "days_of_week": ["monday"],
            "index": "first",
        },
    )
    invalid_ranges = (
        {"type": "end_date", "start_date": "2026-08-01"},
        {"type": "numbered", "start_date": "2026-08-01"},
        {"type": "no_end", "start_date": "2026-08-01", "end_date": "2026-09-01"},
    )
    for pattern in invalid_patterns:
        with pytest.raises(ValidationError):
            catalog.validate_input(
                "outlook_calendar",
                ConnectorMode.WRITE,
                "events.create",
                {**base, "recurrence": {"pattern": pattern, "range": ranges[0]}},
            )
    for event_range in invalid_ranges:
        with pytest.raises(ValidationError):
            catalog.validate_input(
                "outlook_calendar",
                ConnectorMode.WRITE,
                "events.create",
                {**base, "recurrence": {"pattern": patterns[0], "range": event_range}},
            )


def test_message_restore_requires_an_opaque_process_local_handle(
    catalog: OperationCatalog,
) -> None:
    assert catalog.validate_input(
        "outlook_mail",
        ConnectorMode.WRITE,
        "messages.restore",
        {"message_id": "message-1", "restore_handle": "rst-" + "A" * 43},
    )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "outlook_mail",
            ConnectorMode.WRITE,
            "messages.restore",
            {"message_id": "message-1", "parent_folder_id": "caller-chosen"},
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
    read_variants = cast(list[dict[str, object]], read_schema["oneOf"])
    write_variants = cast(list[dict[str, object]], write_schema["oneOf"])
    assert all(
        set(cast(dict[str, object], variant["properties"]))
        <= {"connection_id", "cursor", "input", "operation"}
        for variant in read_variants
    )
    assert all(
        set(cast(dict[str, object], variant["properties"]))
        <= {"confirmation_token", "connection_id", "input", "operation"}
        for variant in write_variants
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
