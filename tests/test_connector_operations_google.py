from __future__ import annotations

from collections.abc import Mapping

import pytest

from continuity_kernel.connector_contract import ConnectorEffect, ConnectorMode, OperationCatalog
from continuity_kernel.connector_operations_google import GOOGLE_OPERATIONS
from continuity_kernel.errors import ValidationError

_EXPECTED_NAMES = {
    "gmail": {
        "messages.list",
        "messages.get",
        "attachments.get",
        "threads.list",
        "threads.get",
        "drafts.list",
        "drafts.get",
        "labels.list",
        "drafts.create",
        "drafts.update",
        "drafts.delete",
        "drafts.send",
        "messages.modify",
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


def _operation(provider: str, mode: ConnectorMode, name: str):
    return _catalog().lookup(provider, mode, name)


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
    assert len(GOOGLE_OPERATIONS) == 64
    assert {
        provider: sum(item.provider == provider for item in GOOGLE_OPERATIONS) for provider in names
    } == {
        "gmail": 23,
        "google_calendar": 14,
        "google_drive": 27,
    }
    assert all(item.endpoint == item.name for item in GOOGLE_OPERATIONS)


def test_google_effects_and_gmail_purge_scope_are_explicit() -> None:
    expected_effects = {
        ("gmail", "drafts.create"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "drafts.update"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "drafts.delete"): ConnectorEffect.PERMANENT,
        ("gmail", "drafts.send"): ConnectorEffect.OUTWARD,
        ("gmail", "messages.modify"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.trash"): ConnectorEffect.DESTRUCTIVE,
        ("gmail", "messages.restore"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "messages.purge"): ConnectorEffect.PERMANENT,
        ("gmail", "threads.modify"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "threads.trash"): ConnectorEffect.DESTRUCTIVE,
        ("gmail", "threads.restore"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "threads.purge"): ConnectorEffect.PERMANENT,
        ("gmail", "labels.create"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "labels.update"): ConnectorEffect.SAFE_MUTATION,
        ("gmail", "labels.delete"): ConnectorEffect.DESTRUCTIVE,
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

    for name in ("messages.purge", "threads.purge"):
        purge = _operation("gmail", ConnectorMode.WRITE, name)
        assert purge.required_scopes == (frozenset({"https://mail.google.com/"}),)
        assert purge.scope_grant_satisfies(["https://mail.google.com/"])
        assert not purge.scope_grant_satisfies(["https://www.googleapis.com/auth/gmail.modify"])


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
        "attendee_emails": ["guest@example.test"],
        "calendar_id": "primary",
        "description": "Planning meeting",
        "end": {"date_time": "2026-08-01T10:00:00+02:00", "time_zone": "Europe/Brussels"},
        "event_id": "client-event-01",
        "location": "Studio",
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"],
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
        "parent_ids": ["folder-1"],
    }
    assert (
        catalog.validate_input("google_drive", ConnectorMode.WRITE, "files.create", drive) == drive
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
        for variant in schema["oneOf"]:
            assert set(variant["properties"]) == expected


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


def test_gmail_normal_message_bodies_allow_the_documented_bound() -> None:
    operation = _operation("gmail", ConnectorMode.WRITE, "drafts.create")
    body = "x" * 200_000
    assert operation.validate_input({"text_body": body}) == {"text_body": body}
    with pytest.raises(ValidationError):
        operation.validate_input({"text_body": body + "x"})
