from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from continuity_kernel.app_corpus import AppCorpusCompanion
from continuity_kernel.app_corpus_providers import (
    GoogleAppCorpusAdapter,
    MicrosoftAppCorpusAdapter,
    SlackAppCorpusAdapter,
    default_app_corpus_adapters,
)
from continuity_kernel.connector_transport import ConnectorOrigin, ConnectorProviderError
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault


@dataclass
class _Runtime:
    responses: list[dict[str, object] | Exception]
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    continuations: list[object | None] = field(default_factory=list)

    def call_app_corpus_read(
        self,
        name: str,
        values: Mapping[str, object],
        *,
        continuation: object | None = None,
    ) -> dict[str, object]:
        self.calls.append((name, dict(values)))
        self.continuations.append(continuation)
        if not self.responses:
            raise AssertionError(f"unexpected connector call: {name} {values!r}")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _response(result: object, *, continuation: object | None = None) -> dict[str, object]:
    response: dict[str, object] = {"result": result, "status": "ok"}
    if continuation is not None:
        response["continuation"] = continuation
    return response


def test_google_sync_pages_gmail_and_extracts_plain_text_body() -> None:
    runtime = _Runtime(
        [
            _response({"historyId": "10"}),
            _response({"messages": [{"id": "m-1"}]}, continuation={"pageToken": "next"}),
            _response(
                {
                    "historyId": "h-1",
                    "id": "m-1",
                    "payload": {
                        "headers": [{"name": "Subject", "value": "Planning"}],
                        "mimeType": "text/html",
                        "body": {
                            "data": base64.urlsafe_b64encode(
                                b"<p>Build <b>the plan</b></p>"
                            ).decode()
                        },
                    },
                    "threadId": "t-1",
                }
            ),
            _response({"items": []}),
            _response({"startPageToken": "drive-start"}),
            _response({"files": []}),
            _response({"messages": [{"id": "m-2"}]}),
            _response(
                {
                    "historyId": "h-2",
                    "id": "m-2",
                    "payload": {
                        "headers": [{"name": "Subject", "value": "Done"}],
                        "mimeType": "text/plain",
                        "body": {"data": base64.urlsafe_b64encode(b"Second page").decode()},
                    },
                }
            ),
            _response({"changes": [], "newStartPageToken": "drive-next"}),
        ]
    )
    adapter = GoogleAppCorpusAdapter(runtime)  # type: ignore[arg-type]

    first = adapter.sync("connection-1", limit=3)

    assert first.complete is False
    assert first.freshness["status"] == "partial"
    assert first.documents[0].object_id == "gmail:m-1"
    assert first.documents[0].text == "Build the plan"
    assert first.documents[0].source_ref == "gmail:message:m-1"
    assert first.documents[0].revision == "h-1"
    assert runtime.calls[0] == (
        "gsv_gmail_read",
        {
            "connection_id": "connection-1",
            "input": {},
            "operation": "profile.get",
        },
    )
    assert runtime.calls[1] == (
        "gsv_gmail_read",
        {
            "connection_id": "connection-1",
            "input": {"page_size": 1},
            "operation": "messages.list",
        },
    )

    second = adapter.sync("connection-1", checkpoint=first.checkpoint, limit=3)

    assert second.complete is True
    assert second.documents[0].object_id == "gmail:m-2"
    assert runtime.continuations[6] == {"pageToken": "next"}


def test_source_scoped_gmail_checkpoint_resumes_in_a_fresh_runtime() -> None:
    first_runtime = _Runtime(
        [
            _response({"historyId": "41"}),
            _response({"messages": [{"id": "m-1"}]}, continuation={"pageToken": "provider-next"}),
            _response(
                {
                    "historyId": "h-1",
                    "id": "m-1",
                    "payload": {"mimeType": "text/plain", "body": {"data": "b25l"}},
                }
            ),
        ]
    )
    first_adapter = default_app_corpus_adapters(first_runtime)["gmail"]  # type: ignore[arg-type]

    first = first_adapter.sync("gmail-connection", limit=10)

    second_runtime = _Runtime(
        [
            _response({"messages": [{"id": "m-2"}]}),
            _response(
                {
                    "historyId": "h-2",
                    "id": "m-2",
                    "payload": {"mimeType": "text/plain", "body": {"data": "dHdv"}},
                }
            ),
        ]
    )
    second_adapter = default_app_corpus_adapters(second_runtime)["gmail"]  # type: ignore[arg-type]

    second = second_adapter.sync("gmail-connection", checkpoint=first.checkpoint, limit=10)

    assert first.complete is False
    assert second.complete is True
    saved_first = json.loads(first.checkpoint)
    saved_second = json.loads(second.checkpoint)
    assert saved_first["gmail"]["history_anchor_id"] == "41"
    assert saved_second["gmail_history_id"] == "41"
    assert second_runtime.continuations == [{"pageToken": "provider-next"}, None]
    assert all("cursor" not in values for _name, values in second_runtime.calls)


def test_gmail_history_preserves_incremental_additions_and_deletions() -> None:
    first_runtime = _Runtime(
        [
            _response({"historyId": "41"}),
            _response({"messages": [{"id": "m-1"}]}),
            _response(
                {
                    "historyId": "41",
                    "id": "m-1",
                    "payload": {"mimeType": "text/plain", "body": {"data": "b25l"}},
                }
            ),
        ]
    )
    first_adapter = GoogleAppCorpusAdapter(first_runtime, sources=frozenset({"gmail"}))  # type: ignore[arg-type]
    first = first_adapter.sync("gmail-connection", limit=10)

    second_runtime = _Runtime(
        [
            _response(
                {
                    "history": [
                        {
                            "id": "42",
                            "messagesAdded": [{"message": {"id": "m-2"}}],
                            "messagesDeleted": [{"message": {"id": "m-1"}}],
                        }
                    ],
                    "historyId": "43",
                }
            ),
            _response(
                {
                    "historyId": "43",
                    "id": "m-2",
                    "payload": {"mimeType": "text/plain", "body": {"data": "dHdv"}},
                }
            ),
        ]
    )
    second_adapter = GoogleAppCorpusAdapter(second_runtime, sources=frozenset({"gmail"}))  # type: ignore[arg-type]
    second = second_adapter.sync("gmail-connection", checkpoint=first.checkpoint, limit=10)

    deleted = next(document for document in second.documents if document.object_id == "gmail:m-1")
    added = next(document for document in second.documents if document.object_id == "gmail:m-2")
    assert deleted.deleted is True
    assert added.text == "two"
    assert second.freshness["status"] == "complete"
    assert second_runtime.calls[0][1]["operation"] == "history.list"
    assert second_runtime.calls[0][1]["input"]["start_history_id"] == "41"


def test_gmail_history_cursor_ignores_newer_message_details_until_final_page() -> None:
    initial = GoogleAppCorpusAdapter(
        _Runtime(
            [
                _response({"historyId": "41"}),
                _response({"messages": [{"id": "m-1"}]}),
                _response(
                    {
                        "historyId": "41",
                        "id": "m-1",
                        "payload": {"mimeType": "text/plain", "body": {"data": "b25l"}},
                    }
                ),
            ]
        ),
        sources=frozenset({"gmail"}),
    )
    first = initial.sync("gmail-connection", limit=10)
    runtime = _Runtime(
        [
            _response(
                {
                    "history": [{"id": "42", "messagesAdded": [{"message": {"id": "m-2"}}]}],
                    "historyId": "43",
                },
                continuation={"pageToken": "next-history"},
            ),
            _response(
                {
                    "historyId": "99",
                    "id": "m-2",
                    "payload": {"mimeType": "text/plain", "body": {"data": "dHdv"}},
                }
            ),
            _response({"history": [], "historyId": "43"}),
        ]
    )
    adapter = GoogleAppCorpusAdapter(runtime, sources=frozenset({"gmail"}))  # type: ignore[arg-type]

    middle = adapter.sync("gmail-connection", checkpoint=first.checkpoint, limit=10)
    final = adapter.sync("gmail-connection", checkpoint=middle.checkpoint, limit=10)

    assert middle.complete is False
    assert final.complete is True
    assert [
        values["input"]["start_history_id"]
        for _name, values in runtime.calls
        if values["operation"] == "history.list"
    ] == ["41", "41"]
    assert json.loads(final.checkpoint)["gmail_history_id"] == "43"


def test_gmail_detail_failure_keeps_prior_body_and_retries_its_provider_page(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    corpus = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )
    initial = GoogleAppCorpusAdapter(
        _Runtime(
            [
                _response({"historyId": "41"}),
                _response({"messages": [{"id": "m-1"}]}),
                _response(
                    {
                        "historyId": "41",
                        "id": "m-1",
                        "payload": {"mimeType": "text/plain", "body": {"data": "b2xk"}},
                    }
                ),
            ]
        ),
        sources=frozenset({"gmail"}),
    )
    corpus.sync(initial, "gmail-connection")

    failed_runtime = _Runtime(
        [
            _response(
                {
                    "history": [{"id": "42", "messagesAdded": [{"message": {"id": "m-1"}}]}],
                    "historyId": "42",
                }
            ),
            ValidationError("transient Gmail provider read failure"),
        ]
    )
    failed = corpus.sync(
        GoogleAppCorpusAdapter(failed_runtime, sources=frozenset({"gmail"})),
        "gmail-connection",
    )

    preserved = corpus.read("gmail-connection", "gmail:m-1")
    assert failed.complete is False
    assert failed.document_count == 1
    assert failed.freshness["status"] == "partial"
    assert preserved is not None
    assert preserved.text == "old"
    assert failed_runtime.calls[0][1]["operation"] == "history.list"
    assert failed_runtime.calls[0][1]["input"]["start_history_id"] == "41"

    retry_runtime = _Runtime(
        [
            _response(
                {
                    "history": [{"id": "42", "messagesAdded": [{"message": {"id": "m-1"}}]}],
                    "historyId": "42",
                }
            ),
            _response(
                {
                    "historyId": "42",
                    "id": "m-1",
                    "payload": {"mimeType": "text/plain", "body": {"data": "bmV3"}},
                }
            ),
        ]
    )
    retried = corpus.sync(
        GoogleAppCorpusAdapter(retry_runtime, sources=frozenset({"gmail"})),
        "gmail-connection",
    )

    updated = corpus.read("gmail-connection", "gmail:m-1")
    assert retried.complete is True
    assert updated is not None
    assert updated.text == "new"
    assert retry_runtime.calls[0][1]["operation"] == "history.list"
    assert retry_runtime.calls[0][1]["input"]["start_history_id"] == "41"


def test_gmail_legacy_message_gap_recovery_restarts_one_baseline_without_rewinding_retry(
) -> None:
    checkpoint = json.dumps(
        {
            "gmail_history_anchor": True,
            "gmail_history_id": "99",
            "gmail_legacy_message_gap_recovery_epoch": 1,
            "provider": "google",
            "round": "complete",
            "since": "2026-09-07T09:00:00Z",
            "v": 1,
        }
    )
    first_runtime = _Runtime(
        [
            _response({"historyId": "41"}),
            _response({"messages": [{"id": "m-1"}]}, continuation={"pageToken": "next"}),
            _response(
                {
                    "historyId": "41",
                    "id": "m-1",
                    "payload": {"mimeType": "text/plain", "body": {"data": "b25l"}},
                }
            ),
        ]
    )
    first = GoogleAppCorpusAdapter(first_runtime, sources=frozenset({"gmail"})).sync(
        "gmail-connection", checkpoint=checkpoint, limit=10
    )  # type: ignore[arg-type]

    saved_first = json.loads(first.checkpoint)
    assert first.complete is False
    assert first.documents[0].object_id == "gmail:m-1"
    assert [values["operation"] for _name, values in first_runtime.calls] == [
        "profile.get",
        "messages.list",
        "messages.get",
    ]
    assert "query" not in first_runtime.calls[1][1]["input"]
    assert "gmail_legacy_message_gap_recovery_epoch" not in saved_first
    assert saved_first["gmail"]["legacy_message_gap_recovery_epoch"] == 1
    assert saved_first["gmail"]["legacy_message_gap_recovery_started"] is True
    assert "incremental_since" not in saved_first

    second_runtime = _Runtime(
        [
            _response({"messages": [{"id": "m-2"}]}),
            _response(
                {
                    "historyId": "41",
                    "id": "m-2",
                    "payload": {"mimeType": "text/plain", "body": {"data": "dHdv"}},
                }
            ),
        ]
    )
    second = GoogleAppCorpusAdapter(second_runtime, sources=frozenset({"gmail"})).sync(
        "gmail-connection", checkpoint=first.checkpoint, limit=10
    )  # type: ignore[arg-type]

    saved_second = json.loads(second.checkpoint)
    assert second.complete is True
    assert second.documents[0].object_id == "gmail:m-2"
    assert [values["operation"] for _name, values in second_runtime.calls] == [
        "messages.list",
        "messages.get",
    ]
    assert second_runtime.continuations[0] == {"pageToken": "next"}
    assert saved_second["gmail_history_id"] == "41"
    assert "gmail_legacy_message_gap_recovery_epoch" not in saved_second


def test_unanchored_gmail_backfill_checkpoint_restarts_without_its_old_continuation() -> None:
    checkpoint = json.dumps(
        {
            "calendar": {"calendar_ids": [], "index": 0, "listed": False},
            "drive": {},
            "gmail": {"continuation": {"pageToken": "old"}, "history_id": "99"},
            "provider": "google",
            "round": "running",
            "v": 1,
        }
    )
    runtime = _Runtime(
        [
            _response({"historyId": "41"}),
            _response({"messages": [{"id": "m-1"}]}, continuation={"pageToken": "new"}),
            _response(
                {
                    "historyId": "99",
                    "id": "m-1",
                    "payload": {"mimeType": "text/plain", "body": {"data": "b25l"}},
                }
            ),
        ]
    )

    result = GoogleAppCorpusAdapter(runtime, sources=frozenset({"gmail"})).sync(
        "gmail-connection", checkpoint=checkpoint, limit=10
    )  # type: ignore[arg-type]

    assert result.complete is False
    assert "fixed history anchor" in result.freshness["detail"]
    assert runtime.calls[0][1]["operation"] == "profile.get"
    assert runtime.continuations[1] is None
    assert json.loads(result.checkpoint)["gmail"]["history_anchor_id"] == "41"


def test_legacy_completed_gmail_subscan_is_not_treated_as_a_deletion_cursor() -> None:
    checkpoint = json.dumps(
        {
            "calendar": {"calendar_ids": [], "index": 0, "listed": False},
            "drive": {},
            "gmail": {"done": True, "history_id": "99"},
            "provider": "google",
            "round": "running",
            "v": 1,
        }
    )
    runtime = _Runtime([_response({"historyId": "41"}), _response({"messages": []})])

    result = GoogleAppCorpusAdapter(runtime, sources=frozenset({"gmail"})).sync(
        "gmail-connection", checkpoint=checkpoint, limit=10
    )  # type: ignore[arg-type]

    assert result.complete is True
    assert result.freshness["status"] == "complete"
    assert runtime.calls[0][1]["operation"] == "profile.get"
    saved = json.loads(result.checkpoint)
    assert saved["gmail_history_anchor"] is True
    assert saved["gmail_history_id"] == "41"


def test_expired_gmail_history_forces_a_full_rescan() -> None:
    first_runtime = _Runtime(
        [
            _response({"historyId": "41"}),
            _response({"messages": [{"id": "m-1"}]}),
            _response(
                {
                    "historyId": "41",
                    "id": "m-1",
                    "payload": {"mimeType": "text/plain", "body": {"data": "b25l"}},
                }
            ),
        ]
    )
    first = GoogleAppCorpusAdapter(first_runtime, sources=frozenset({"gmail"})).sync(
        "gmail-connection", limit=10
    )  # type: ignore[arg-type]
    expired_runtime = _Runtime(
        [
            ConnectorProviderError(
                origin=ConnectorOrigin.GMAIL,
                status=404,
                code="full_sync_required",
            )
        ]
    )

    result = GoogleAppCorpusAdapter(expired_runtime, sources=frozenset({"gmail"})).sync(
        "gmail-connection", checkpoint=first.checkpoint, limit=10
    )  # type: ignore[arg-type]

    saved = json.loads(result.checkpoint)
    assert result.complete is False
    assert saved["gmail"].get("history_id") is None
    assert "incremental_since" not in saved


def test_drive_trash_is_preserved_and_permanent_deletion_gap_stays_visible() -> None:
    runtime = _Runtime(
        [
            _response({"startPageToken": "drive-start"}),
            _response(
                {
                    "files": [
                        {
                            "id": "file-1",
                            "mimeType": "text/plain",
                            "name": "Deleted notes",
                            "trashed": True,
                            "version": "9",
                        }
                    ]
                }
            ),
        ]
    )
    result = GoogleAppCorpusAdapter(runtime, sources=frozenset({"google_drive"})).sync(
        "drive-connection", limit=10
    )  # type: ignore[arg-type]

    assert result.documents[0].object_id == "drive:file-1"
    assert result.documents[0].deleted is True
    assert result.freshness["status"] == "partial"
    assert "permanent deletions" in result.freshness["detail"]
    assert runtime.calls[1][1]["input"]["include_trashed"] is True


def test_drive_change_cursor_reconciles_removals_and_only_commits_the_final_cursor() -> None:
    runtime = _Runtime(
        [
            _response({"startPageToken": "drive-start"}),
            _response({"files": []}),
            _response(
                {
                    "changes": [
                        {
                            "fileId": "removed-file",
                            "removed": True,
                            "time": "2026-09-07T10:00:00Z",
                        }
                    ]
                },
                continuation={"pageToken": "more-changes"},
            ),
            _response(
                {
                    "changes": [
                        {
                            "fileId": "doc-1",
                            "file": {
                                "id": "doc-1",
                                "mimeType": "application/vnd.google-apps.document",
                                "name": "Updated roadmap",
                                "version": "2",
                            },
                        }
                    ],
                    "newStartPageToken": "drive-next",
                }
            ),
            _response(
                {
                    "content_base64": base64.b64encode(b"Updated document").decode(),
                    "delivery": "inline_chunk",
                }
            ),
        ]
    )
    adapter = GoogleAppCorpusAdapter(runtime, sources=frozenset({"google_drive"}))  # type: ignore[arg-type]

    bootstrap = adapter.sync("drive-connection", limit=10)
    middle = adapter.sync("drive-connection", checkpoint=bootstrap.checkpoint, limit=10)
    final = adapter.sync("drive-connection", checkpoint=middle.checkpoint, limit=10)

    assert bootstrap.complete is False
    assert middle.complete is False
    assert middle.documents[0].object_id == "drive:removed-file"
    assert middle.documents[0].deleted is True
    assert middle.documents[0].title == "Unavailable Google Drive file"
    assert final.complete is True
    assert final.documents[0].text == "Updated document"
    assert final.freshness["status"] == "partial"
    assert "shared drives" in final.freshness["detail"]
    assert runtime.calls[2][1]["input"]["start_change_id"] == "drive-start"
    assert runtime.calls[3][1]["input"]["start_change_id"] == "drive-start"
    assert runtime.continuations[3] == {"pageToken": "more-changes"}
    saved = json.loads(final.checkpoint)
    assert saved["drive_change_id"] == "drive-next"
    assert saved["drive_change_anchor"] is True
    assert "change_anchor" not in saved

    next_runtime = _Runtime(
        [
            _response(
                {
                    "changes": [
                        {
                            "fileId": "next-removed-file",
                            "removed": True,
                            "time": "2026-09-07T11:00:00Z",
                        }
                    ],
                    "newStartPageToken": "drive-after-next",
                }
            )
        ]
    )
    next_result = GoogleAppCorpusAdapter(next_runtime, sources=frozenset({"google_drive"})).sync(
        "drive-connection", checkpoint=final.checkpoint, limit=10
    )  # type: ignore[arg-type]

    assert next_result.complete is True
    assert next_result.documents[0].object_id == "drive:next-removed-file"
    assert next_result.documents[0].deleted is True
    assert next_runtime.calls[0][1]["operation"] == "changes.list"
    assert next_runtime.calls[0][1]["input"]["start_change_id"] == "drive-next"


def test_legacy_drive_checkpoint_restarts_a_full_scan_until_the_change_cursor_is_final() -> None:
    checkpoint = json.dumps(
        {
            "provider": "google",
            "round": "complete",
            "since": "2026-09-07T09:00:00Z",
            "v": 1,
        }
    )
    runtime = _Runtime([_response({"startPageToken": "drive-start"}), _response({"files": []})])

    result = GoogleAppCorpusAdapter(runtime, sources=frozenset({"google_drive"})).sync(
        "drive-connection", checkpoint=checkpoint, limit=10
    )  # type: ignore[arg-type]

    saved = json.loads(result.checkpoint)
    assert result.complete is False
    assert "permanent deletions" in result.freshness["detail"]
    assert saved["drive"]["change_anchor"] == "drive-start"
    assert "change_id" not in saved["drive"]
    assert runtime.calls[0][1]["operation"] == "changes.get_start_page_token"
    assert runtime.calls[1][1]["operation"] == "files.list"
    assert "query" not in runtime.calls[1][1]["input"]


def test_google_calendar_keeps_event_occurrence_separate_from_last_update() -> None:
    runtime = _Runtime(
        [
            _response({"items": [{"id": "primary"}]}),
            _response(
                {
                    "items": [
                        {
                            "id": "nimble-intro",
                            "start": {"dateTime": "2026-09-08T09:00:00+02:00"},
                            "summary": "Nimble Intro engineering",
                            "updated": "2026-08-29T15:30:00Z",
                        }
                    ]
                }
            ),
        ]
    )
    adapter = GoogleAppCorpusAdapter(runtime, sources=frozenset({"google_calendar"}))  # type: ignore[arg-type]

    first = adapter.sync("calendar-connection", limit=10)
    second = adapter.sync("calendar-connection", checkpoint=first.checkpoint, limit=10)

    assert second.complete is True
    assert second.documents[0].metadata["start_at"] == "2026-09-08T09:00:00+02:00"
    assert second.documents[0].metadata["updated_at"] == "2026-08-29T15:30:00Z"


def test_google_exports_text_drive_docs_without_downloading_other_files() -> None:
    runtime = _Runtime(
        [
            _response({"historyId": "41"}),
            _response({"messages": []}),
            _response({"items": []}),
            _response({"startPageToken": "drive-start"}),
            _response(
                {
                    "files": [
                        {
                            "id": "doc-1",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-09-07T10:00:00Z",
                            "name": "Roadmap",
                            "version": "7",
                        },
                        {"id": "image-1", "mimeType": "image/png", "name": "Diagram"},
                    ]
                }
            ),
            _response(
                {
                    "content_base64": base64.b64encode(b"A bounded Drive export").decode(),
                    "delivery": "inline_chunk",
                }
            ),
        ]
    )
    adapter = GoogleAppCorpusAdapter(runtime)  # type: ignore[arg-type]

    result = adapter.sync("connection-1", limit=6)

    drive = next(document for document in result.documents if document.provider == "google_drive")
    assert drive.object_id == "drive:doc-1"
    assert drive.text == "A bounded Drive export"
    assert drive.metadata["mime_type"] == "application/vnd.google-apps.document"
    assert drive.metadata["modified_at"] == "2026-09-07T10:00:00Z"
    assert [name for name, _values in runtime.calls].count("gsv_google_drive_read") == 3


def test_google_sync_indexes_bounded_readable_gmail_attachments_separately() -> None:
    runtime = _Runtime(
        [
            _response({"historyId": "41"}),
            _response({"messages": [{"id": "m-attachment"}]}),
            _response(
                {
                    "historyId": "h-attachment",
                    "internalDate": "1712345678001",
                    "id": "m-attachment",
                    "payload": {
                        "mimeType": "multipart/mixed",
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": base64.b64encode(b"Email body").decode()},
                            },
                            {
                                "filename": "notes.txt",
                                "mimeType": "text/plain",
                                "body": {"attachmentId": "a-1", "size": 16},
                            },
                        ],
                    },
                }
            ),
            _response(
                {
                    "content_base64": base64.b64encode(b"Attachment text").decode(),
                    "delivery": "inline_chunk",
                }
            ),
            _response({"items": []}),
            _response({"startPageToken": "drive-start"}),
            _response({"files": []}),
        ]
    )

    result = GoogleAppCorpusAdapter(runtime).sync("connection-1", limit=3)  # type: ignore[arg-type]

    attachment = next(
        document
        for document in result.documents
        if document.object_id.startswith("gmail-attachment:")
    )
    assert attachment.source_ref == "gmail:attachment:m-attachment:a-1"
    assert attachment.text == "Attachment text"
    assert attachment.metadata["parent_message_id"] == "m-attachment"
    assert attachment.metadata["received_at"] == "2024-04-05T19:34:38.001Z"
    message = next(
        document for document in result.documents if document.object_id == "gmail:m-attachment"
    )
    assert message.metadata["received_at"] == "2024-04-05T19:34:38.001Z"
    assert runtime.calls[3][1]["operation"] == "attachments.get"


def test_microsoft_sync_gets_full_message_body_then_calendar_events() -> None:
    runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": "folder-1"}],
                }
            ),
            _response({"value": [{"id": "calendar-1"}]}),
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-1/messages/delta?$deltatoken=messages",
                    "value": [{"id": "mail-1"}],
                }
            ),
            _response(
                {
                    "body": {"content": "<p>Full <em>mail</em> body</p>", "contentType": "html"},
                    "changeKey": "mail-v1",
                    "id": "mail-1",
                    "subject": "Quarterly plan",
                }
            ),
            _response(
                {
                    "value": [
                        {
                            "body": {"content": "<p>Discuss scope</p>", "contentType": "html"},
                            "changeKey": "event-v1",
                            "id": "event-1",
                            "start": {"dateTime": "2026-09-07T09:00:00Z"},
                            "subject": "Planning",
                        }
                    ]
                }
            ),
        ]
    )
    adapter = MicrosoftAppCorpusAdapter(runtime)  # type: ignore[arg-type]

    first = adapter.sync("connection-2", limit=4)
    second = adapter.sync("connection-2", checkpoint=first.checkpoint, limit=4)

    mail = next(document for document in second.documents if document.provider == "outlook_mail")
    event = next(
        document for document in second.documents if document.provider == "outlook_calendar"
    )
    assert mail.object_id == "outlook-immutable:mail-1"
    assert mail.text == "Full mail body"
    assert mail.revision == "mail-v1"
    assert event.object_id == "outlook-calendar:calendar-1:event-1"
    assert event.text == "Discuss scope\nStart: 2026-09-07T09:00:00Z"
    assert event.metadata["start_at"] == "2026-09-07T09:00:00Z"
    assert second.complete is True
    assert all(not name.endswith("_write") for name, _values in runtime.calls)


def test_microsoft_completed_checkpoint_reuses_graph_delta_links() -> None:
    first_runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [],
                }
            )
        ]
    )
    first_adapter = MicrosoftAppCorpusAdapter(first_runtime, sources=frozenset({"outlook_mail"}))  # type: ignore[arg-type]
    first = first_adapter.sync("connection-2", limit=4)

    second_runtime = _Runtime([])
    second_adapter = MicrosoftAppCorpusAdapter(second_runtime, sources=frozenset({"outlook_mail"}))  # type: ignore[arg-type]
    complete = second_adapter.sync("connection-2", checkpoint=first.checkpoint, limit=4)

    third_runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders-2",
                    "value": [],
                }
            )
        ]
    )
    third_adapter = MicrosoftAppCorpusAdapter(third_runtime, sources=frozenset({"outlook_mail"}))  # type: ignore[arg-type]
    third_adapter.sync("connection-2", checkpoint=complete.checkpoint, limit=4)

    third_input = third_runtime.calls[0][1]["input"]
    assert third_input == {
        "delta_link": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders"
    }
    assert complete.freshness["status"] == "complete"


def test_microsoft_delta_read_clears_only_the_superseded_legacy_deletion_gap() -> None:
    legacy_gap = "Outlook Mail permanent deletions are not observable without a Graph delta read"
    delta_checkpoint = json.dumps(
        {
            "candidates": {},
            "coverage_gaps": [],
            "folder_delta_link": None,
            "folder_index": 0,
            "folder_order": [],
            "folders": {},
            "phase": "folders",
            "v": 1,
        }
    )
    checkpoint = json.dumps(
        {
            "calendar": {"calendar_ids": [], "index": 0, "listed": False},
            "mail": {
                "coverage_gaps": [legacy_gap, "A current Outlook coverage gap"],
                "delta_checkpoint": delta_checkpoint,
            },
            "provider": "microsoft",
            "round": "running",
            "v": 1,
        }
    )
    runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [],
                }
            )
        ]
    )

    result = MicrosoftAppCorpusAdapter(
        runtime, sources=frozenset({"outlook_mail"})
    ).sync("connection-2", checkpoint=checkpoint, limit=4)

    assert legacy_gap not in result.freshness.get("detail", "")
    assert "A current Outlook coverage gap" in result.freshness.get("detail", "")


def test_microsoft_delta_tombstones_only_the_typed_immutable_record_after_404() -> None:
    runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": "folder-1"}],
                }
            ),
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-1/messages/delta?$deltatoken=messages",
                    "value": [{"@removed": {"reason": "deleted"}, "id": "immutable-deleted"}],
                }
            ),
            ConnectorProviderError(
                origin=ConnectorOrigin.MICROSOFT_GRAPH,
                status=404,
                code="not_found",
            ),
        ]
    )
    adapter = MicrosoftAppCorpusAdapter(runtime, sources=frozenset({"outlook_mail"}))  # type: ignore[arg-type]

    first = adapter.sync("connection-2", limit=4)
    result = adapter.sync("connection-2", checkpoint=first.checkpoint, limit=4)

    assert result.complete is True
    assert [(document.object_id, document.deleted) for document in result.documents] == [
        ("outlook-immutable:immutable-deleted", True)
    ]
    assert "separately indexed attachments" not in result.freshness.get("detail", "")


def test_microsoft_delta_change_404_tombstones_and_advances_the_cursor() -> None:
    first_runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": "folder-1"}],
                }
            )
        ]
    )
    first = MicrosoftAppCorpusAdapter(
        first_runtime, sources=frozenset({"outlook_mail"})
    ).sync("connection-2", limit=4)  # type: ignore[arg-type]
    runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-1/messages/delta?$deltatoken=messages",
                    "value": [{"changeKey": "delta-v1", "id": "immutable-deleted"}],
                }
            ),
            ConnectorProviderError(
                origin=ConnectorOrigin.MICROSOFT_GRAPH,
                status=404,
                code="not_found",
            ),
        ]
    )

    result = MicrosoftAppCorpusAdapter(
        runtime, sources=frozenset({"outlook_mail"})
    ).sync("connection-2", checkpoint=first.checkpoint, limit=4)  # type: ignore[arg-type]

    assert result.complete is True
    assert [
        (document.object_id, document.revision, document.deleted) for document in result.documents
    ] == [("outlook-immutable:immutable-deleted", "delta-v1", True)]


def test_microsoft_detail_failure_keeps_the_prior_delta_checkpoint_for_retry() -> None:
    first_runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": "folder-1"}],
                }
            )
        ]
    )
    first = MicrosoftAppCorpusAdapter(
        first_runtime, sources=frozenset({"outlook_mail"})
    ).sync("connection-2", limit=4)  # type: ignore[arg-type]
    prior_delta_checkpoint = json.loads(first.checkpoint)["mail"]["delta_checkpoint"]
    failed_runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-1/messages/delta?$deltatoken=messages",
                    "value": [{"id": "mail-1"}],
                }
            ),
            ConnectorProviderError(
                origin=ConnectorOrigin.MICROSOFT_GRAPH,
                status=503,
                code="temporarily_unavailable",
            ),
        ]
    )

    failed = MicrosoftAppCorpusAdapter(
        failed_runtime, sources=frozenset({"outlook_mail"})
    ).sync("connection-2", checkpoint=first.checkpoint, limit=4)  # type: ignore[arg-type]

    assert failed.documents == ()
    assert failed.complete is False
    assert failed.freshness["status"] == "error"
    assert json.loads(failed.checkpoint)["mail"]["delta_checkpoint"] == prior_delta_checkpoint

    retry_runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-1/messages/delta?$deltatoken=messages",
                    "value": [{"id": "mail-1"}],
                }
            ),
            _response(
                {
                    "body": {"content": "Recovered mail body", "contentType": "text"},
                    "changeKey": "mail-v1",
                    "id": "mail-1",
                    "subject": "Recovered mail",
                }
            ),
        ]
    )
    retried = MicrosoftAppCorpusAdapter(
        retry_runtime, sources=frozenset({"outlook_mail"})
    ).sync("connection-2", checkpoint=failed.checkpoint, limit=4)  # type: ignore[arg-type]

    assert retried.complete is True
    assert retried.documents[0].object_id == "outlook-immutable:mail-1"
    assert retried.documents[0].text == "Recovered mail body"


def test_microsoft_sync_extracts_bounded_attachment_artifact(tmp_path: object) -> None:
    path = tmp_path / "notes.txt"  # type: ignore[operator]
    path.write_text("Attachment content", encoding="utf-8")  # type: ignore[union-attr]
    runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": "folder-1"}],
                }
            ),
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-1/messages/delta?$deltatoken=messages",
                    "value": [{"id": "mail-attachment"}],
                }
            ),
            _response(
                {
                    "changeKey": "mail-v1",
                    "hasAttachments": True,
                    "id": "mail-attachment",
                    "subject": "Attachment mail",
                }
            ),
            _response(
                {
                    "value": [
                        {
                            "contentType": "text/plain",
                            "id": "a-1",
                            "name": "notes.txt",
                            "size": 18,
                        }
                    ]
                }
            ),
            {
                "artifact": {"path": str(path)},
                "result": {"delivery": "artifact"},
                "status": "ok",
            },
        ]
    )

    first = MicrosoftAppCorpusAdapter(runtime, sources=frozenset({"outlook_mail"})).sync(  # type: ignore[arg-type]
        "connection-2", limit=4
    )
    result = MicrosoftAppCorpusAdapter(runtime, sources=frozenset({"outlook_mail"})).sync(  # type: ignore[arg-type]
        "connection-2", checkpoint=first.checkpoint, limit=4
    )

    attachment = next(
        document
        for document in result.documents
        if document.object_id.startswith("outlook-immutable-attachment:")
    )
    assert attachment.text == "Attachment content"
    assert attachment.source_ref == "outlook:attachment:mail-attachment:a-1"
    assert attachment.metadata["extraction_status"] == "ok"


def test_slack_sync_uses_search_then_authenticated_channel_history() -> None:
    runtime = _Runtime(
        [
            _response(
                {
                    "messages": {
                        "matches": [
                            {
                                "channel": {"id": "C1"},
                                "text": "Search result text",
                                "ts": "1712345678.000001",
                            }
                        ],
                        "paging": {"pages": 1},
                    }
                }
            ),
            _response(
                {
                    "messages": [
                        {"channel_id": "C1", "text": "History message", "ts": "1712345679.000001"}
                    ]
                }
            ),
        ]
    )
    adapter = SlackAppCorpusAdapter(runtime)  # type: ignore[arg-type]

    first = adapter.sync("connection-3", limit=5)
    second = adapter.sync("connection-3", checkpoint=first.checkpoint, limit=5)

    assert first.documents[0].source_ref == "slack:message:C1:1712345678.000001"
    assert first.documents[0].metadata["sent_at"] == "2024-04-05T19:34:38.000001Z"
    assert second.documents[0].text == "History message"
    assert second.documents[0].metadata["sent_at"] == "2024-04-05T19:34:39.000001Z"
    assert second.complete is True
    assert second.freshness["status"] == "partial"
    assert "edits and deletions" in second.freshness["detail"]
    assert [name for name, _values in runtime.calls] == ["gsv_slack_read", "gsv_slack_read"]
    assert runtime.calls[1][1]["operation"] == "messages.list"


def test_slack_search_keeps_current_documents_when_the_provider_exceeds_local_page_bound() -> None:
    page = _response(
        {
            "messages": {
                "matches": [
                    {
                        "channel": {"id": "C1"},
                        "text": "Search result text",
                        "ts": "1712345678.000001",
                    }
                ],
                "paging": {"pages": 101},
            }
        }
    )
    runtime = _Runtime([page, page])
    adapter = SlackAppCorpusAdapter(runtime)  # type: ignore[arg-type]

    first = adapter.sync("connection-3", limit=50)
    first_checkpoint = json.loads(first.checkpoint)
    assert first.documents[0].source_ref == "slack:message:C1:1712345678.000001"
    assert first.freshness["status"] == "partial"
    assert first_checkpoint["search"]["page"] == 2

    first_checkpoint["search"]["page"] = 100
    bounded = adapter.sync("connection-3", checkpoint=json.dumps(first_checkpoint), limit=50)
    bounded_checkpoint = json.loads(bounded.checkpoint)

    assert bounded.documents[0].source_ref == "slack:message:C1:1712345678.000001"
    assert bounded_checkpoint["search"]["done"] is True
    assert "page" not in bounded_checkpoint["search"]
    assert "local page bound" in bounded.freshness["detail"]


def test_refused_connector_read_is_visible_and_never_claims_completion() -> None:
    runtime = _Runtime(
        [ValidationError("connector credential does not satisfy the operation scope")]
    )

    result = GoogleAppCorpusAdapter(runtime).sync("connection-4")  # type: ignore[arg-type]

    assert result.complete is False
    assert result.checkpoint is not None
    assert result.freshness["status"] == "refused"
    assert "credential" not in result.freshness["detail"]


def test_default_mapping_shares_one_adapter_per_provider_family() -> None:
    runtime = _Runtime([_response({"historyId": "41"}), _response({"messages": []})])

    adapters = default_app_corpus_adapters(runtime)  # type: ignore[arg-type]

    gmail = adapters["gmail"].sync("gmail-connection")

    assert gmail.complete is True
    assert [name for name, _values in runtime.calls] == ["gsv_gmail_read", "gsv_gmail_read"]
    assert adapters["gmail"] is not adapters["google_drive"]
    assert adapters["microsoft"] is not adapters["outlook_calendar"]
    assert isinstance(adapters["slack"], SlackAppCorpusAdapter)
