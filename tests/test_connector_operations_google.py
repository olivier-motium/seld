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
        "drives.list",
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
    assert len(GOOGLE_OPERATIONS) == 65
    assert {
        provider: sum(item.provider == provider for item in GOOGLE_OPERATIONS) for provider in names
    } == {
        "gmail": 23,
        "google_calendar": 14,
        "google_drive": 28,
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
        "attendees": [
            {
                "display_name": "Guest",
                "email": "guest@example.test",
                "optional": True,
                "response_status": "needsAction",
            }
        ],
        "calendar_id": "primary",
        "description": "Planning meeting",
        "drive_attachments": [{"file_id": "drive-file-1"}],
        "end": {"date_time": "2026-08-01T10:00:00+02:00", "time_zone": "Europe/Brussels"},
        "event_id": "client-event-01",
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
        "parent_ids": ["folder-1"],
    }
    assert (
        catalog.validate_input("google_drive", ConnectorMode.WRITE, "files.create", drive) == drive
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

    for name in ("files.download", "revisions.download"):
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
    create_metadata = {"mime_type": "text/plain", "name": "note.txt"}
    update_metadata = {"etag": "etag", "file_id": "file"}
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


def test_drive_content_delivery_defaults_to_artifact_and_inline_is_explicitly_bounded() -> None:
    for name, base in (
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
            "permission_type": "user",
            "role": "organizer",
            "supports_all_drives": True,
        },
    ) == {
        "file_id": "shared-drive",
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


def test_calendar_drive_attachment_references_are_closed_and_bounded() -> None:
    operation = _operation("google_calendar", ConnectorMode.WRITE, "events.create")
    event_time = {
        "date_time": "2026-08-01T09:00:00+02:00",
        "time_zone": "Europe/Brussels",
    }
    base = {"calendar_id": "primary", "end": event_time, "start": event_time}
    attachments = [{"file_id": f"file-{index}"} for index in range(25)]
    assert operation.validate_input({**base, "drive_attachments": attachments}) == {
        **base,
        "drive_attachments": attachments,
    }
    with pytest.raises(ValidationError):
        operation.validate_input(
            {**base, "drive_attachments": [*attachments, {"file_id": "file-26"}]}
        )
    for untrusted in (
        {"file_id": "file", "file_url": "https://attacker.example/file"},
        {"file_id": "file", "title": "caller-controlled"},
    ):
        with pytest.raises(ValidationError):
            operation.validate_input({**base, "drive_attachments": [untrusted]})
