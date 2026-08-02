from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from continuity_kernel.connector_contract import (
    ConnectorEffect,
    ConnectorMode,
    OperationCatalog,
    OperationSpec,
)
from continuity_kernel.connector_operations_google import GOOGLE_OPERATIONS
from continuity_kernel.errors import ValidationError

_EXPECTED_NAMES = {
    "gmail": {
        "profile.get",
        "messages.list",
        "messages.get",
        "attachments.get",
        "threads.list",
        "threads.get",
        "drafts.list",
        "drafts.get",
        "labels.list",
        "labels.get",
        "history.list",
        "drafts.create",
        "drafts.update",
        "drafts.delete",
        "drafts.send",
        "messages.send",
        "messages.modify",
        "messages.batch_modify",
        "messages.batch_purge",
        "messages.import",
        "messages.insert",
        "messages.trash",
        "messages.restore",
        "messages.purge",
        "threads.modify",
        "threads.trash",
        "threads.restore",
        "threads.purge",
        "labels.create",
        "labels.update",
        "labels.delete",
        "settings.auto_forwarding.get",
        "settings.filters.create",
        "settings.filters.delete",
        "settings.filters.get",
        "settings.filters.list",
        "settings.forwarding_addresses.get",
        "settings.forwarding_addresses.list",
        "settings.imap.get",
        "settings.imap.update",
        "settings.language.get",
        "settings.language.update",
        "settings.pop.get",
        "settings.pop.update",
        "settings.send_as.get",
        "settings.send_as.list",
        "settings.send_as.patch",
        "settings.vacation.get",
        "settings.vacation.update",
    },
    "google_calendar": {
        "calendars.list",
        "calendars.get",
        "events.list",
        "events.get",
        "events.instances",
        "freebusy.query",
        "calendars.create",
        "calendars.update",
        "calendars.delete",
        "events.create",
        "events.update",
        "events.move",
        "events.respond",
        "events.delete",
    },
    "google_drive": {
        "drives.list",
        "files.content",
        "files.list",
        "files.get",
        "files.download",
        "files.export",
        "permissions.list",
        "comments.list",
        "replies.list",
        "revisions.list",
        "revisions.download",
        "files.create",
        "files.update",
        "files.copy",
        "files.move",
        "files.trash",
        "files.restore",
        "files.purge",
        "permissions.create",
        "permissions.update",
        "permissions.delete",
        "comments.create",
        "comments.update",
        "comments.delete",
        "replies.create",
        "replies.update",
        "replies.delete",
        "revisions.keep",
        "revisions.delete",
    },
}


def _catalog() -> OperationCatalog:
    return OperationCatalog(GOOGLE_OPERATIONS)


def _operation(provider: str, mode: ConnectorMode, name: str) -> OperationSpec:
    return _catalog().lookup(provider, mode, name)


def _expected_event() -> dict[str, object]:
    return {
        "end": None,
        "etag": "etag",
        "eventType": "default",
        "id": "event",
        "organizer": None,
        "start": None,
        "status": "confirmed",
        "summary": None,
    }


def _expected_calendar(calendar_id: str) -> dict[str, object]:
    return {
        "accessRole": "owner",
        "id": calendar_id,
        "primary": False,
        "summary": "Calendar",
    }


def test_google_provider_mode_partitions_and_exact_operation_surface() -> None:
    partitions = {(item.provider, item.mode) for item in GOOGLE_OPERATIONS}
    assert partitions == {
        ("gmail", ConnectorMode.READ),
        ("gmail", ConnectorMode.WRITE),
        ("google_calendar", ConnectorMode.READ),
        ("google_calendar", ConnectorMode.WRITE),
        ("google_drive", ConnectorMode.READ),
        ("google_drive", ConnectorMode.WRITE),
    }
    assert all(
        any((item.provider, item.mode) == partition for item in GOOGLE_OPERATIONS)
        for partition in partitions
    )

    names = {
        provider: {item.name for item in GOOGLE_OPERATIONS if item.provider == provider}
        for provider in _EXPECTED_NAMES
    }
    assert names == _EXPECTED_NAMES
    assert len(GOOGLE_OPERATIONS) == 92
    assert {
        provider: sum(item.provider == provider for item in GOOGLE_OPERATIONS) for provider in names
    } == {
        "gmail": 49,
        "google_calendar": 14,
        "google_drive": 29,
    }
    assert all(item.endpoint == item.name for item in GOOGLE_OPERATIONS)


def test_google_effects_and_gmail_purge_scope_are_explicit() -> None:
    expected_effects = {
        ("gmail", "drafts.create"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "drafts.update"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "drafts.delete"): ConnectorEffect.PERMANENT,
        ("gmail", "drafts.send"): ConnectorEffect.OUTWARD,
        ("gmail", "messages.send"): ConnectorEffect.OUTWARD,
        ("gmail", "messages.modify"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.batch_modify"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.batch_purge"): ConnectorEffect.PERMANENT,
        ("gmail", "messages.import"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.insert"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.trash"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.restore"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.purge"): ConnectorEffect.PERMANENT,
        ("gmail", "threads.modify"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "threads.trash"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "threads.restore"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "threads.purge"): ConnectorEffect.PERMANENT,
        ("gmail", "labels.create"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "labels.update"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "labels.delete"): ConnectorEffect.PERMANENT,
        ("gmail", "settings.filters.create"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "settings.filters.delete"): ConnectorEffect.PERMANENT,
        ("gmail", "settings.imap.update"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "settings.language.update"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "settings.pop.update"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "settings.send_as.patch"): ConnectorEffect.OUTWARD,
        ("gmail", "settings.vacation.update"): ConnectorEffect.SAFE_MUTATION,
        ("google_calendar", "calendars.create"): ConnectorEffect.SAFE_MUTATION,
        ("google_calendar", "calendars.update"): ConnectorEffect.SAFE_MUTATION,
        ("google_calendar", "calendars.delete"): ConnectorEffect.PERMANENT,
        ("google_calendar", "events.create"): ConnectorEffect.SAFE_MUTATION,
        ("google_calendar", "events.update"): ConnectorEffect.SAFE_MUTATION,
        ("google_calendar", "events.move"): ConnectorEffect.SAFE_MUTATION,
        ("google_calendar", "events.respond"): ConnectorEffect.OUTWARD,
        ("google_calendar", "events.delete"): ConnectorEffect.DESTRUCTIVE,
        ("google_drive", "files.create"): ConnectorEffect.SAFE_MUTATION,
        ("google_drive", "files.update"): ConnectorEffect.SAFE_MUTATION,
        ("google_drive", "files.copy"): ConnectorEffect.SAFE_MUTATION,
        ("google_drive", "files.move"): ConnectorEffect.SAFE_MUTATION,
        ("google_drive", "files.trash"): ConnectorEffect.DESTRUCTIVE,
        ("google_drive", "files.restore"): ConnectorEffect.SAFE_MUTATION,
        ("google_drive", "files.purge"): ConnectorEffect.PERMANENT,
        ("google_drive", "permissions.create"): ConnectorEffect.OUTWARD,
        ("google_drive", "permissions.update"): ConnectorEffect.OUTWARD,
        ("google_drive", "permissions.delete"): ConnectorEffect.DESTRUCTIVE,
        ("google_drive", "comments.create"): ConnectorEffect.OUTWARD,
        ("google_drive", "comments.update"): ConnectorEffect.OUTWARD,
        ("google_drive", "comments.delete"): ConnectorEffect.DESTRUCTIVE,
        ("google_drive", "replies.create"): ConnectorEffect.OUTWARD,
        ("google_drive", "replies.update"): ConnectorEffect.OUTWARD,
        ("google_drive", "replies.delete"): ConnectorEffect.DESTRUCTIVE,
        ("google_drive", "revisions.keep"): ConnectorEffect.SAFE_MUTATION,
        ("google_drive", "revisions.delete"): ConnectorEffect.PERMANENT,
    }
    writes = {
        (item.provider, item.name): item.effect
        for item in GOOGLE_OPERATIONS
        if item.mode is ConnectorMode.WRITE
    }
    assert writes == expected_effects
    assert all(
        item.effect is ConnectorEffect.READ
        for item in GOOGLE_OPERATIONS
        if item.mode is ConnectorMode.READ
    )

    for name in ("messages.batch_purge", "messages.purge", "threads.purge"):
        purge = _operation("gmail", ConnectorMode.WRITE, name)
        assert purge.required_scopes == (frozenset({"https://mail.google.com/"}),)
        assert purge.scope_grant_satisfies(["https://mail.google.com/"])
        assert not purge.scope_grant_satisfies(["https://www.googleapis.com/auth/gmail.modify"])

    for item in GOOGLE_OPERATIONS:
        if (
            item.provider == "gmail"
            and item.mode is ConnectorMode.WRITE
            and item.name.startswith("settings.")
        ):
            assert item.required_scopes == (
                frozenset({"https://www.googleapis.com/auth/gmail.settings.basic"}),
            )
            assert not item.scope_grant_satisfies(
                ["https://mail.google.com/", "https://www.googleapis.com/auth/gmail.modify"]
            )


def test_calendar_reads_accept_legacy_readonly_and_metadata_uses_resource_scope() -> None:
    legacy = ["https://www.googleapis.com/auth/calendar.readonly"]
    for name in (
        "calendars.list",
        "calendars.get",
        "events.list",
        "events.get",
        "events.instances",
        "freebusy.query",
    ):
        assert _operation("google_calendar", ConnectorMode.READ, name).scope_grant_satisfies(legacy)

    metadata = _operation("google_calendar", ConnectorMode.READ, "calendars.get")
    assert metadata.scope_grant_satisfies(
        ["https://www.googleapis.com/auth/calendar.calendars.readonly"]
    )
    assert not metadata.scope_grant_satisfies(
        ["https://www.googleapis.com/auth/calendar.calendarlist.readonly"]
    )


def test_gmail_settings_schemas_are_closed_and_require_explicit_final_states() -> None:
    imap = _operation("gmail", ConnectorMode.WRITE, "settings.imap.update")
    validated = imap.validate_input(
        {
            "auto_expunge": False,
            "enabled": True,
            "expunge_behavior": "trash",
            "max_folder_size": 5_000,
        }
    )
    assert isinstance(validated, dict)
    assert validated["max_folder_size"] == 5_000
    for invalid_imap in (
        {"enabled": True},
        {
            "auto_expunge": False,
            "enabled": True,
            "expunge_behavior": "deleteSometimes",
            "max_folder_size": 0,
        },
        {
            "auto_expunge": False,
            "enabled": True,
            "expunge_behavior": "trash",
            "max_folder_size": 500,
        },
    ):
        with pytest.raises(ValidationError):
            imap.validate_input(invalid_imap)

    pop = _operation("gmail", ConnectorMode.WRITE, "settings.pop.update")
    assert pop.validate_input({"access_window": "fromNowOn", "disposition": "leaveInInbox"}) == {
        "access_window": "fromNowOn",
        "disposition": "leaveInInbox",
    }
    with pytest.raises(ValidationError):
        pop.validate_input({"access_window": "allMail"})

    vacation = _operation("gmail", ConnectorMode.WRITE, "settings.vacation.update")
    vacation_state = {
        "enable_auto_reply": True,
        "end_time": None,
        "response_body_html": "",
        "response_body_plain_text": "Back Monday",
        "response_subject": "Away",
        "restrict_to_contacts": False,
        "restrict_to_domain": False,
        "start_time": None,
    }
    assert vacation.validate_input(vacation_state) == vacation_state
    with pytest.raises(ValidationError):
        vacation.validate_input({"response_subject": "Away"})
    with pytest.raises(ValidationError):
        vacation.validate_input({**vacation_state, "provider_field": True})
    with pytest.raises(ValidationError):
        vacation.validate_input(
            {key: value for key, value in vacation_state.items() if key != "end_time"}
        )

    send_as = _operation("gmail", ConnectorMode.WRITE, "settings.send_as.patch")
    with pytest.raises(ValidationError):
        send_as.validate_input({"is_default": False, "send_as_email": "me@example.test"})
    with pytest.raises(ValidationError):
        send_as.validate_input(
            {"send_as_email": "me@example.test", "smtp_password": "must-never-enter"}
        )

    delete_filter = _operation("gmail", ConnectorMode.WRITE, "settings.filters.delete")
    expected_filter = {
        "action": {"addLabelIds": ["STARRED"]},
        "criteria": {"from": "sender@example.test"},
        "id": "filter-1",
    }
    assert delete_filter.validate_input(
        {"expected_filter": expected_filter, "filter_id": "filter-1"}
    ) == {"expected_filter": expected_filter, "filter_id": "filter-1"}
    for invalid_filter in (
        {"filter_id": "filter-1"},
        {
            "expected_filter": {
                **expected_filter,
                "action": {"providerFutureAction": True},
            },
            "filter_id": "filter-1",
        },
    ):
        with pytest.raises(ValidationError):
            delete_filter.validate_input(invalid_filter)


def test_gmail_history_uses_a_bounded_decimal_uint64_cursor() -> None:
    history = _operation("gmail", ConnectorMode.READ, "history.list")
    maximum = {
        "history_types": ["messageAdded", "labelRemoved"],
        "label_id": "INBOX",
        "page_size": 500,
        "start_history_id": "18446744073709551615",
    }
    assert history.validate_input(maximum) == maximum

    for invalid in (
        {},
        {"start_history_id": "-1"},
        {"start_history_id": "1.5"},
        {"page_size": 501, "start_history_id": "1"},
    ):
        with pytest.raises(ValidationError):
            history.validate_input(invalid)


def test_gmail_metadata_headers_require_metadata_format_and_drafts_reject_label_filters() -> None:
    requests = (
        ("messages.get", "message_id"),
        ("threads.get", "thread_id"),
    )
    for name, identifier in requests:
        operation = _operation("gmail", ConnectorMode.READ, name)
        values = {
            "format": "metadata",
            identifier: "resource",
            "metadata_header_names": ["Subject", "From"],
        }
        assert operation.validate_input(values) == values
        with pytest.raises(ValidationError):
            operation.validate_input(
                {
                    "format": "full",
                    identifier: "resource",
                    "metadata_header_names": ["Subject"],
                }
            )

    drafts = _operation("gmail", ConnectorMode.READ, "drafts.list")
    with pytest.raises(ValidationError):
        drafts.validate_input({"label_ids": ["INBOX"]})

    messages = _operation("gmail", ConnectorMode.READ, "messages.get")
    assert messages.validate_input({"format": "raw", "message_id": "message"}) == {
        "format": "raw",
        "message_id": "message",
    }


def test_gmail_batch_operations_enforce_provider_bounds() -> None:
    modify = _operation("gmail", ConnectorMode.WRITE, "messages.batch_modify")
    purge = _operation("gmail", ConnectorMode.WRITE, "messages.batch_purge")
    maximum = [f"message-{index}" for index in range(1_000)]
    assert modify.validate_input({"add_label_ids": ["TRASH"], "message_ids": maximum}) == {
        "add_label_ids": ["TRASH"],
        "message_ids": maximum,
    }
    assert purge.validate_input({"message_ids": maximum}) == {"message_ids": maximum}

    with pytest.raises(ValidationError):
        modify.validate_input({"message_ids": ["message"]})
    for label_field in ("add_label_ids", "remove_label_ids"):
        with pytest.raises(ValidationError):
            modify.validate_input({label_field: [], "message_ids": ["message"]})

    for operation in (modify, purge):
        with pytest.raises(ValidationError):
            operation.validate_input({"message_ids": []})
        with pytest.raises(ValidationError):
            operation.validate_input({"message_ids": [*maximum, "one-too-many"]})


def test_gmail_message_and_thread_modify_accept_the_provider_label_limit() -> None:
    label_ids = [f"Label_{index}" for index in range(100)]
    cases = (
        ("messages.modify", "message_id", "add_label_ids"),
        ("threads.modify", "thread_id", "remove_label_ids"),
    )
    for name, identifier, label_field in cases:
        operation = _operation("gmail", ConnectorMode.WRITE, name)
        maximum = {identifier: "resource", label_field: label_ids}
        assert operation.validate_input(maximum) == maximum
        with pytest.raises(ValidationError):
            operation.validate_input({identifier: "resource"})
        with pytest.raises(ValidationError):
            operation.validate_input({identifier: "resource", label_field: []})
        with pytest.raises(ValidationError):
            operation.validate_input({identifier: "resource", label_field: [*label_ids, "extra"]})


def test_gmail_send_requires_at_least_one_nonempty_recipient_group() -> None:
    send = _operation("gmail", ConnectorMode.WRITE, "messages.send")
    for recipients in (
        {"to": ["to@example.test"]},
        {"cc": ["cc@example.test"]},
        {"bcc": ["bcc@example.test"]},
        {"bcc": ["bcc@example.test"], "cc": ["cc@example.test"]},
        {"cc": ["cc@example.test"], "to": ["to@example.test"]},
    ):
        values = {**recipients, "text_body": "Hello"}
        assert send.validate_input(values) == values

    local_file = {"grant_id": "grant", "relative_path": "message.eml"}
    assert send.validate_input({"local_file": local_file}) == {"local_file": local_file}

    for invalid in (
        {},
        {"text_body": "Hello"},
        {"text_body": "Hello", "to": []},
        {"cc": [], "text_body": "Hello"},
        {"bcc": [], "text_body": "Hello"},
        {"local_file": local_file, "to": ["recipient@example.test"]},
        {"local_file": local_file, "subject": "Caller-supplied override"},
    ):
        with pytest.raises(ValidationError):
            send.validate_input(invalid)


def test_gmail_insert_and_import_expose_exact_migration_controls() -> None:
    local_file = {"grant_id": "grant", "relative_path": "message.eml"}
    labels = [f"Label_{index}" for index in range(100)]
    insert = _operation("gmail", ConnectorMode.WRITE, "messages.insert")
    importing = _operation("gmail", ConnectorMode.WRITE, "messages.import")

    assert insert.validate_input(
        {
            "deleted": True,
            "internal_date_source": "receivedTime",
            "label_ids": labels,
            "local_file": local_file,
        }
    ) == {
        "deleted": True,
        "internal_date_source": "receivedTime",
        "label_ids": labels,
        "local_file": local_file,
    }
    assert importing.validate_input(
        {
            "internal_date_source": "dateHeader",
            "local_file": local_file,
            "never_mark_spam": True,
            "process_for_calendar": True,
        }
    ) == {
        "internal_date_source": "dateHeader",
        "local_file": local_file,
        "never_mark_spam": True,
        "process_for_calendar": True,
    }

    for operation, invalid in (
        (insert, {}),
        (insert, {"local_file": local_file, "never_mark_spam": True}),
        (insert, {"local_file": local_file, "process_for_calendar": True}),
        (insert, {"internal_date_source": "other", "local_file": local_file}),
        (insert, {"label_ids": [], "local_file": local_file}),
        (insert, {"label_ids": [*labels, "extra"], "local_file": local_file}),
        (importing, {"local_file": local_file, "unknown": True}),
    ):
        with pytest.raises(ValidationError):
            operation.validate_input(invalid)


def test_google_catalog_validates_representative_rich_inputs() -> None:
    catalog = _catalog()
    gmail = {
        "attachments": [
            {
                "content_base64": "YWdlbnQtc2FmZS1ub3Rl",
                "filename": "note.txt",
                "mime_type": "text/plain",
            }
        ],
        "cc": ["reviewer@example.test"],
        "html_body": "<p>Hello</p>",
        "reply_to_message_id": "message-1",
        "subject": "Bounded draft",
        "text_body": "Hello",
        "thread_id": "thread-1",
        "to": ["recipient@example.test"],
    }
    assert catalog.validate_input("gmail", ConnectorMode.WRITE, "drafts.create", gmail) == gmail

    event = {
        "attendees": [
            {
                "display_name": "Guest",
                "email": "guest@example.test",
                "optional": True,
            }
        ],
        "calendar_id": "primary",
        "description": "Planning meeting",
        "end": {"date_time": "2026-08-01T10:00:00+02:00", "time_zone": "Europe/Brussels"},
        "event_id": "123e4567e89b12d3a456426614174000",
        "guests_can_invite_others": False,
        "guests_can_modify": False,
        "guests_can_see_other_guests": True,
        "location": "Studio",
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"],
        "reminders": {
            "overrides": [{"delivery": "popup", "minutes": 10}],
            "use_default": False,
        },
        "send_updates": "externalOnly",
        "start": {"date_time": "2026-08-01T09:00:00+02:00", "time_zone": "Europe/Brussels"},
        "summary": "Weekly plan",
        "visibility": "private",
    }
    assert (
        catalog.validate_input("google_calendar", ConnectorMode.WRITE, "events.create", event)
        == event
    )

    all_day = {
        "calendar_id": "primary",
        "end": {"date": "2026-08-02", "time_zone": "Europe/Brussels"},
        "etag": "etag-1",
        "event_id": "client-event-01",
        "start": {"date": "2026-08-01", "time_zone": "Europe/Brussels"},
    }
    assert (
        catalog.validate_input("google_calendar", ConnectorMode.WRITE, "events.update", all_day)
        == all_day
    )

    drive = {
        "app_properties": [{"key": "source", "value": "seld"}],
        "content_base64": "c21hbGwtY29udGVudA==",
        "description": "An explicit small upload",
        "mime_type": "text/plain",
        "name": "plan.txt",
        "parent_id": "folder-1",
    }
    assert (
        catalog.validate_input("google_drive", ConnectorMode.WRITE, "files.create", drive) == drive
    )


def test_calendar_schemas_match_provider_time_id_and_notification_contracts() -> None:
    catalog = _catalog()
    all_day = {
        "calendar_id": "primary",
        "end": {"date": "2026-08-03"},
        "start": {"date": "2026-08-02"},
    }
    assert (
        catalog.validate_input("google_calendar", ConnectorMode.WRITE, "events.create", all_day)
        == all_day
    )

    offset_timed = {
        "calendar_id": "primary",
        "end": {"date_time": "2026-08-02T11:00:00+02:00"},
        "event_id": "123e4567e89b12d3a456426614174000",
        "start": {"date_time": "2026-08-02T10:00:00+02:00"},
    }
    assert (
        catalog.validate_input(
            "google_calendar", ConnectorMode.WRITE, "events.create", offset_timed
        )
        == offset_timed
    )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "google_calendar",
            ConnectorMode.WRITE,
            "events.create",
            {**offset_timed, "event_id": "client-event-01"},
        )

    freebusy = {
        "calendar_ids": [f"calendar-{index}" for index in range(50)],
        "time_max": "2026-08-03T00:00:00Z",
        "time_min": "2026-08-02T00:00:00Z",
    }
    assert (
        catalog.validate_input("google_calendar", ConnectorMode.READ, "freebusy.query", freebusy)
        == freebusy
    )
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "google_calendar",
            ConnectorMode.READ,
            "freebusy.query",
            {**freebusy, "calendar_ids": [*freebusy["calendar_ids"], "one-too-many"]},
        )

    move = {
        "calendar_id": "source",
        "destination_calendar_id": "destination",
        "etag": "etag",
        "event_id": "event",
        "expected_destination_calendar": _expected_calendar("destination"),
        "expected_event": _expected_event(),
        "send_updates": "all",
    }
    assert (
        catalog.validate_input("google_calendar", ConnectorMode.WRITE, "events.move", move) == move
    )
    for missing in ("etag", "expected_destination_calendar", "expected_event", "send_updates"):
        with pytest.raises(ValidationError):
            catalog.validate_input(
                "google_calendar",
                ConnectorMode.WRITE,
                "events.move",
                {key: value for key, value in move.items() if key != missing},
            )

    long_summary = "s" * 5_000
    bounded_provider_snapshot = {
        **move,
        "expected_destination_calendar": {
            **_expected_calendar("destination"),
            "summary": None,
        },
        "expected_event": {**_expected_event(), "summary": long_summary},
    }
    assert (
        catalog.validate_input(
            "google_calendar",
            ConnectorMode.WRITE,
            "events.move",
            bounded_provider_snapshot,
        )
        == bounded_provider_snapshot
    )


def test_gmail_attachment_delivery_and_typed_local_file_sources_are_explicit() -> None:
    attachment = _operation("gmail", ConnectorMode.READ, "attachments.get")
    identifiers = {"attachment_id": "attachment", "message_id": "message"}
    assert attachment.validate_input(identifiers) == identifiers
    assert attachment.validate_input({**identifiers, "delivery": "artifact"}) == {
        **identifiers,
        "delivery": "artifact",
    }
    assert attachment.validate_input({**identifiers, "delivery": "inline_chunk"}) == {
        **identifiers,
        "delivery": "inline_chunk",
    }
    with pytest.raises(ValidationError):
        attachment.validate_input({**identifiers, "delivery": "inline_chunk", "byte_offset": 1})

    draft = _operation("gmail", ConnectorMode.WRITE, "drafts.create")
    local = {
        "attachments": [
            {
                "filename": "note.txt",
                "local_file": {"grant_id": "grant", "relative_path": "note.txt"},
                "mime_type": "text/plain",
            }
        ],
        "text_body": "body",
    }
    assert draft.validate_input(local) == local
    with pytest.raises(ValidationError):
        draft.validate_input({"attachments": [{"filename": "note.txt", "mime_type": "text/plain"}]})
    with pytest.raises(ValidationError):
        draft.validate_input(
            {
                "attachments": [
                    {
                        "content_base64": "YQ==",
                        "filename": "note.txt",
                        "local_file": {"grant_id": "grant", "relative_path": "note.txt"},
                        "mime_type": "text/plain",
                    }
                ]
            }
        )


def test_google_inputs_reject_unknown_and_proxy_like_fields() -> None:
    operation = _operation("gmail", ConnectorMode.WRITE, "drafts.create")
    valid = {"text_body": "bounded", "to": ["recipient@example.test"]}
    assert operation.validate_input(valid) == valid
    for field in (
        "url",
        "proxy_url",
        "method",
        "headers",
        "token",
        "nextLink",
        "authorization",
        "host",
        "cursor",
    ):
        with pytest.raises(ValidationError):
            operation.validate_input({**valid, field: "untrusted"})


def _schema_property_names(schema: Mapping[str, object]) -> set[str]:
    names: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            names.add(str(name))
            if isinstance(child, Mapping):
                names.update(_schema_property_names(child))
    items = schema.get("items")
    if isinstance(items, Mapping):
        names.update(_schema_property_names(items))
    variants = schema.get("oneOf")
    if isinstance(variants, tuple):
        for variant in variants:
            if isinstance(variant, Mapping):
                names.update(_schema_property_names(variant))
    return names


def test_google_schemas_and_tool_envelopes_keep_transport_sealed() -> None:
    forbidden = ("url", "method", "header", "token", "nextlink", "authorization", "host", "cursor")
    for operation in GOOGLE_OPERATIONS:
        for name in _schema_property_names(operation.input_schema):
            normalized = "".join(character for character in name.casefold() if character.isalnum())
            assert normalized not in {"header", "headers", "url", "urls"}
            assert not any(normalized.endswith(suffix) for suffix in forbidden)

    catalog = _catalog()
    for provider, mode in {(item.provider, item.mode) for item in GOOGLE_OPERATIONS}:
        schema = catalog.tool_input_schema(provider, mode)
        expected = {"connection_id", "input", "operation"}
        expected.add("cursor" if mode is ConnectorMode.READ else "confirmation_token")
        variants = cast(list[Mapping[str, object]], schema["oneOf"])
        for variant in variants:
            properties = variant["properties"]
            assert isinstance(properties, Mapping)
            assert set(properties) == expected


def test_drive_metadata_scope_does_not_satisfy_content_or_write_operations() -> None:
    metadata_scope = ["https://www.googleapis.com/auth/drive.metadata.readonly"]
    assert _operation("google_drive", ConnectorMode.READ, "files.list").scope_grant_satisfies(
        metadata_scope
    )
    assert not _operation(
        "google_drive", ConnectorMode.READ, "files.download"
    ).scope_grant_satisfies(metadata_scope)
    assert not _operation(
        "google_drive", ConnectorMode.WRITE, "files.create"
    ).scope_grant_satisfies(metadata_scope)


def test_drive_discovery_scope_alternatives_match_provider_methods() -> None:
    readonly = "https://www.googleapis.com/auth/drive.readonly"
    file_scope = "https://www.googleapis.com/auth/drive.file"
    meet_scope = "https://www.googleapis.com/auth/drive.meet.readonly"
    drive = "https://www.googleapis.com/auth/drive"
    metadata = "https://www.googleapis.com/auth/drive.metadata.readonly"

    assert _operation("google_drive", ConnectorMode.READ, "drives.list").required_scopes == (
        frozenset({readonly}),
        frozenset({drive}),
    )
    assert not _operation("google_drive", ConnectorMode.READ, "drives.list").scope_grant_satisfies(
        [file_scope]
    )
    for name in ("comments.list", "replies.list"):
        operation = _operation("google_drive", ConnectorMode.READ, name)
        assert operation.scope_grant_satisfies([readonly])
        assert operation.scope_grant_satisfies([file_scope])
        assert operation.scope_grant_satisfies([meet_scope])
        assert operation.scope_grant_satisfies([drive])
        assert not operation.scope_grant_satisfies([metadata])


def test_google_catalog_uses_provider_page_limits_and_drive_capacity() -> None:
    limits = {
        ("gmail", "messages.list"): 500,
        ("gmail", "threads.list"): 500,
        ("gmail", "drafts.list"): 500,
        ("google_calendar", "calendars.list"): 250,
        ("google_calendar", "events.list"): 2_500,
        ("google_calendar", "events.instances"): 2_500,
        ("google_drive", "drives.list"): 100,
        ("google_drive", "files.list"): 1_000,
        ("google_drive", "permissions.list"): 100,
        ("google_drive", "comments.list"): 100,
        ("google_drive", "replies.list"): 100,
        ("google_drive", "revisions.list"): 1_000,
    }
    for (provider, name), limit in limits.items():
        schema = _operation(provider, ConnectorMode.READ, name).input_schema
        properties = schema["properties"]
        assert isinstance(properties, Mapping)
        page_size = properties["page_size"]
        assert isinstance(page_size, Mapping)
        assert page_size["maximum"] == limit

    for name in ("files.content", "files.download", "revisions.download"):
        schema = _operation("google_drive", ConnectorMode.READ, name).input_schema
        properties = schema["properties"]
        assert isinstance(properties, Mapping)
        byte_offset = properties["byte_offset"]
        assert isinstance(byte_offset, Mapping)
        assert byte_offset["maximum"] == 5 * 1024**4
        required = {
            "delivery": "inline_chunk",
            "file_id": "file",
            "byte_offset": 5 * 1024**4,
        }
        if name == "revisions.download":
            required["revision_id"] = "revision"
        operation = _operation("google_drive", ConnectorMode.READ, name)
        assert operation.validate_input(required) == required
        with pytest.raises(ValidationError):
            operation.validate_input({**required, "byte_offset": 5 * 1024**4 + 1})


def test_drive_file_sources_are_closed_while_metadata_only_mutations_remain_valid() -> None:
    create = _operation("google_drive", ConnectorMode.WRITE, "files.create")
    update = _operation("google_drive", ConnectorMode.WRITE, "files.update")
    copy = _operation("google_drive", ConnectorMode.WRITE, "files.copy")
    create_metadata = {"mime_type": "text/plain", "name": "note.txt"}
    update_metadata = {"file_id": "file", "name": "renamed.txt"}
    local_file = {"grant_id": "grant", "relative_path": "note.txt"}

    assert create.validate_input(create_metadata) == create_metadata
    assert update.validate_input(update_metadata) == update_metadata
    assert create.validate_input({**create_metadata, "content_base64": "aGVsbG8="}) == {
        **create_metadata,
        "content_base64": "aGVsbG8=",
    }
    assert create.validate_input({**create_metadata, "local_file": local_file}) == {
        **create_metadata,
        "local_file": local_file,
    }
    assert update.validate_input({**update_metadata, "local_file": local_file}) == {
        **update_metadata,
        "local_file": local_file,
    }
    for operation, values in (
        (
            create,
            {
                **create_metadata,
                "content_base64": "aGVsbG8=",
                "local_file": local_file,
            },
        ),
        (
            update,
            {
                **update_metadata,
                "content_base64": "aGVsbG8=",
                "local_file": local_file,
            },
        ),
    ):
        with pytest.raises(ValidationError):
            operation.validate_input(values)
    with pytest.raises(ValidationError):
        create.validate_input({**create_metadata, "local_file": {"grant_id": "grant"}})
    with pytest.raises(ValidationError):
        create.validate_input({**create_metadata, "local_file": {**local_file, "extra": "nope"}})
    assert create.validate_input({**create_metadata, "parent_id": "folder"}) == {
        **create_metadata,
        "parent_id": "folder",
    }
    assert copy.validate_input({"file_id": "file", "parent_id": "folder"}) == {
        "file_id": "file",
        "parent_id": "folder",
    }
    for invalid in (
        {**update_metadata, "parent_id": "folder"},
        {**update_metadata, "parent_ids": ["folder"]},
    ):
        with pytest.raises(ValidationError):
            update.validate_input(invalid)
    for invalid_copy in (
        {"file_id": "file", "content_base64": "aA=="},
        {"file_id": "file", "local_file": local_file},
    ):
        with pytest.raises(ValidationError):
            copy.validate_input(invalid_copy)

    deleted_property = {
        "app_properties": [{"key": "obsolete", "value": None}],
        "file_id": "file",
    }
    assert update.validate_input(deleted_property) == deleted_property
    assert copy.validate_input(deleted_property) == deleted_property
    with pytest.raises(ValidationError):
        update.validate_input({"app_properties": [], "file_id": "file"})
    with pytest.raises(ValidationError):
        create.validate_input(
            {
                **create_metadata,
                "app_properties": [{"key": "obsolete", "value": None}],
            }
        )


def test_drive_closed_comment_reply_permission_and_move_shapes() -> None:
    catalog = _catalog()
    assert catalog.validate_input(
        "google_drive",
        ConnectorMode.WRITE,
        "comments.create",
        {
            "anchor": '{"r":{"a":1}}',
            "content": "note",
            "file_id": "file",
            "quoted_file_content": {"mime_type": "text/plain", "value": "quote"},
        },
    )
    for quote in (
        {"mime_type": "text/plain"},
        {"value": "quote"},
        {"mime_type": "text/plain", "value": "quote"},
    ):
        value = {
            "content": "note",
            "file_id": "file",
            "quoted_file_content": quote,
        }
        assert (
            catalog.validate_input(
                "google_drive",
                ConnectorMode.WRITE,
                "comments.create",
                value,
            )
            == value
        )
    for invalid in (
        {"comment_id": "comment", "content": "note", "etag": "invented", "file_id": "file"},
        {
            "content": "note",
            "file_id": "file",
            "quoted_file_content": {"mime_type": "text/plain"},
        },
    ):
        with pytest.raises(ValidationError):
            catalog.validate_input("google_drive", ConnectorMode.WRITE, "comments.update", invalid)

    reply = _operation("google_drive", ConnectorMode.WRITE, "replies.create")
    for reply_value in (
        {"comment_id": "comment", "content": "note", "file_id": "file"},
        {"action": "resolve", "comment_id": "comment", "file_id": "file"},
        {
            "action": "reopen",
            "comment_id": "comment",
            "content": "note",
            "file_id": "file",
        },
    ):
        assert reply.validate_input(reply_value) == reply_value
    with pytest.raises(ValidationError):
        reply.validate_input({"comment_id": "comment", "file_id": "file"})

    permission = _operation("google_drive", ConnectorMode.WRITE, "permissions.create")
    assert permission.validate_input(
        {
            "allow_file_discovery": True,
            "domain": "example.test",
            "file_id": "file",
            "permission_type": "domain",
            "role": "reader",
        }
    )
    assert permission.validate_input(
        {
            "email_address": "person@example.test",
            "expiration_time": "2026-12-31T00:00:00Z",
            "file_id": "file",
            "notification_message": "Review this",
            "permission_type": "user",
            "role": "reader",
            "send_notification_email": True,
        }
    )
    with pytest.raises(ValidationError):
        permission.validate_input(
            {
                "domain": "example.test",
                "file_id": "file",
                "permission_type": "anyone",
                "role": "reader",
            }
        )
    for invalid_move in (
        {"file_id": "file"},
        {"add_parent_ids": [], "file_id": "file"},
        {"add_parent_ids": ["one", "two"], "file_id": "file"},
        {"file_id": "file", "remove_parent_ids": ["one", "two"]},
    ):
        with pytest.raises(ValidationError):
            catalog.validate_input("google_drive", ConnectorMode.WRITE, "files.move", invalid_move)


def test_drive_content_delivery_defaults_to_artifact_and_inline_is_explicitly_bounded() -> None:
    for name, base in (
        ("files.content", {"file_id": "file"}),
        ("files.download", {"file_id": "file"}),
        ("revisions.download", {"file_id": "file", "revision_id": "revision"}),
        ("files.export", {"export_mime_type": "text/plain", "file_id": "file"}),
    ):
        operation = _operation("google_drive", ConnectorMode.READ, name)
        properties = operation.input_schema["properties"]
        assert isinstance(properties, Mapping)
        delivery = properties["delivery"]
        assert isinstance(delivery, Mapping)
        assert delivery["enum"] == ("artifact", "inline_chunk")
        assert operation.validate_input(base) == base
        assert operation.validate_input({**base, "delivery": "artifact"}) == {
            **base,
            "delivery": "artifact",
        }
        inline: dict[str, object] = {**base, "delivery": "inline_chunk"}
        if name != "files.export":
            inline.update({"byte_offset": 5, "max_chunk_size": 3})
        assert operation.validate_input(inline) == inline

    download = _operation("google_drive", ConnectorMode.READ, "files.download")
    with pytest.raises(ValidationError):
        download.validate_input({"delivery": "artifact", "file_id": "file", "byte_offset": 1})
    with pytest.raises(ValidationError):
        download.validate_input({"delivery": "inline_chunk", "file_id": "file", "filename": "x"})
    with pytest.raises(ValidationError):
        download.validate_input({"byte_offset": 1, "file_id": "file"})


def test_drive_lro_download_exposes_only_documented_safe_inputs() -> None:
    download = _operation("google_drive", ConnectorMode.READ, "files.download")
    value = {
        "delivery": "artifact",
        "file_id": "file_1",
        "filename": "clip.mp4",
        "mime_type": "video/mp4",
        "resource_key": "0-safe_key",
        "revision_id": "revision-1",
    }
    assert download.validate_input(value) == value
    properties = download.input_schema["properties"]
    assert isinstance(properties, Mapping)
    assert "supports_all_drives" not in properties
    for resource_key in ("key,other/secret", "key/other", "key value", "key\nvalue"):
        with pytest.raises(ValidationError):
            download.validate_input({"file_id": "file", "resource_key": resource_key})


def test_drive_shared_drive_inputs_are_finite_without_admin_or_ownership_transfer() -> None:
    catalog = _catalog()
    file_list = {
        "corpora": "drive",
        "drive_id": "shared-drive",
        "include_items_from_all_drives": True,
        "order_by": ["modifiedTime desc", "name"],
        "page_size": 1_000,
        "spaces": ["drive"],
        "supports_all_drives": True,
    }
    assert (
        catalog.validate_input("google_drive", ConnectorMode.READ, "files.list", file_list)
        == file_list
    )
    assert catalog.validate_input(
        "google_drive",
        ConnectorMode.WRITE,
        "permissions.create",
        {
            "file_id": "shared-drive",
            "email_address": "owner@example.test",
            "permission_type": "user",
            "role": "organizer",
            "supports_all_drives": True,
        },
    ) == {
        "file_id": "shared-drive",
        "email_address": "owner@example.test",
        "permission_type": "user",
        "role": "organizer",
        "supports_all_drives": True,
    }
    with pytest.raises(ValidationError):
        catalog.validate_input(
            "google_drive",
            ConnectorMode.WRITE,
            "permissions.create",
            {"file_id": "file", "permission_type": "user", "role": "owner"},
        )
    for field in ("transfer_ownership", "use_domain_admin_access"):
        with pytest.raises(ValidationError):
            catalog.validate_input(
                "google_drive",
                ConnectorMode.WRITE,
                "permissions.create",
                {
                    "file_id": "file",
                    "permission_type": "user",
                    "role": "reader",
                    field: True,
                },
            )


def test_gmail_normal_message_bodies_allow_the_documented_bound() -> None:
    operation = _operation("gmail", ConnectorMode.WRITE, "drafts.create")
    body = "x" * 200_000
    assert operation.validate_input({"text_body": body}) == {"text_body": body}
    with pytest.raises(ValidationError):
        operation.validate_input({"text_body": body + "x"})


def test_separate_calendar_connection_does_not_advertise_unreachable_drive_attachments() -> None:
    operation = _operation("google_calendar", ConnectorMode.WRITE, "events.create")
    event_time = {
        "date_time": "2026-08-01T09:00:00+02:00",
        "time_zone": "Europe/Brussels",
    }
    base = {"calendar_id": "primary", "end": event_time, "start": event_time}
    with pytest.raises(ValidationError, match="unknown field"):
        operation.validate_input({**base, "drive_attachments": [{"file_id": "file"}]})
