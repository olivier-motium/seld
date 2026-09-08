"""Read-only app-content corpus adapters built on the connector runtime.

The adapters deliberately stay on the public connector read surface.  They do
not resolve credentials, make provider writes, or retain provider payloads in
their checkpoints.  A checkpoint is a bounded progress marker for the local
corpus only; documents remain the only content-bearing output.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Final

from continuity_kernel.app_corpus import AppCorpusDocument, AppCorpusSyncResult
from continuity_kernel.app_corpus_microsoft_calendar_delta import (
    MicrosoftCalendarDeltaSync,
    MicrosoftCalendarDeltaWindow,
    calendar_delta_checkpoint_matches_window,
    validate_calendar_delta_window,
)
from continuity_kernel.app_corpus_microsoft_calendar_delta import (
    clear_calendar_delta_continuation as _clear_microsoft_calendar_delta_continuation,
)
from continuity_kernel.app_corpus_microsoft_delta import (
    MAX_CHECKPOINT_CHARS as _MICROSOFT_DELTA_CHECKPOINT_CHARS,
)
from continuity_kernel.app_corpus_microsoft_delta import (
    MicrosoftMailDeltaSync,
)
from continuity_kernel.app_corpus_microsoft_delta import (
    clear_continuations as _clear_microsoft_delta_continuations,
)
from continuity_kernel.app_corpus_text import ExtractionResult, extract_text
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_transport import ConnectorOrigin, ConnectorProviderError
from continuity_kernel.errors import ContinuityError, ValidationError
from continuity_kernel.slack_channel_access import load_slack_channel_access

_CHECKPOINT_VERSION: Final = 1
_MAX_CHECKPOINT_CHARS: Final = 12_000
_MAX_DOCUMENT_TEXT_CHARS: Final = 240_000
_MAX_METADATA_TEXT_CHARS: Final = 2_000
_MAX_PAGE_ITEMS: Final = 50
_MAX_CALENDARS: Final = 128
_MAX_SLACK_CHANNELS: Final = 128
_MAX_SLACK_SEARCH_PAGES: Final = 100
_MAX_ATTACHMENT_BYTES: Final = 16 * 1024 * 1024
_INLINE_ATTACHMENT_BYTES: Final = 1 * 1024 * 1024
_MAX_GMAIL_RAW_MESSAGE_BYTES: Final = 16 * 1024 * 1024
_GOOGLE_DOCUMENT_MIME: Final = "application/vnd.google-apps.document"
_DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME: Final = "application/pdf"
_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT: Final = "gmail_legacy_message_gap_recovery_epoch"
_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH: Final = "legacy_message_gap_recovery_epoch"
_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_STARTED: Final = "legacy_message_gap_recovery_started"


class _PlainText(HTMLParser):
    """Small dependency-free HTML-to-text converter for read-only corpus text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"head", "script", "style", "template", "title"}:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag in {"br", "div", "li", "p", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"head", "script", "style", "template", "title"} and self.hidden_depth:
            self.hidden_depth -= 1
            return
        if self.hidden_depth:
            return
        if tag in {"div", "li", "p", "tr"}:
            self.parts.append("\n")

    def text(self, *, bounded: bool = True) -> str:
        value = "".join(self.parts)
        return _bounded_text(value) if bounded else value


@dataclass(frozen=True)
class _Page:
    payload: Mapping[str, Any]
    continuation: object | None
    artifact: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _Attachment:
    attachment_id: str
    filename: str
    mime_type: str | None
    size: int | None


@dataclass(frozen=True)
class _RawGmailAttachment:
    content: bytes
    filename: str
    mime_type: str
    part_index: int


@dataclass(frozen=True)
class _RawGmailMessage:
    attachment_count: int
    attachments: tuple[_RawGmailAttachment, ...]
    attachments_truncated: bool
    body: str
    body_truncated: bool
    sender: str | None
    subject: str | None


class _CorpusProviderAdapter:
    """Common safe runtime call and checkpoint handling for one provider family."""

    provider_name: str

    def __init__(self, runtime: ConnectorRuntime) -> None:
        self._runtime = runtime

    def sync(
        self,
        connection_id: str,
        *,
        checkpoint: str | None = None,
        limit: int = 100,
    ) -> AppCorpusSyncResult:
        if not isinstance(connection_id, str) or not connection_id:
            raise ValidationError("app corpus connection ID is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValidationError("app corpus sync limit is invalid")

        now = _now()
        try:
            state = self._state(checkpoint)
        except ValidationError:
            return AppCorpusSyncResult(
                documents=(),
                checkpoint=_encode_checkpoint(self._initial_state()),
                scanned=0,
                complete=False,
                freshness=_freshness(
                    "error", "checkpoint was invalid; a bounded scan will restart"
                ),
            )

        try:
            documents, scanned, done = self._sync_round(
                connection_id, state=state, limit=limit, fetched_at=now
            )
        except Exception as exc:  # Connector failures must become visible corpus health.
            if _continuation_failure(exc):
                _clear_continuations(state)
            status = "refused" if _refused(exc) else "error"
            return AppCorpusSyncResult(
                documents=(),
                checkpoint=_encode_checkpoint(state),
                scanned=0,
                complete=False,
                freshness=_freshness(status, _failure_detail(exc)),
            )

        omissions = _omission_count(state)
        coverage_gaps = _coverage_gaps(state)
        if done:
            complete_state = {
                "provider": self.provider_name,
                "round": "complete",
                "since": now,
                "v": _CHECKPOINT_VERSION,
                **self._completion_checkpoint(state),
            }
            return AppCorpusSyncResult(
                documents=tuple(documents),
                checkpoint=_encode_checkpoint(complete_state),
                scanned=scanned,
                complete=True,
                freshness=_freshness(
                    "partial" if omissions or coverage_gaps else "complete",
                    _completion_detail(omissions, coverage_gaps),
                ),
            )
        return AppCorpusSyncResult(
            documents=tuple(documents),
            checkpoint=_encode_checkpoint(state),
            scanned=scanned,
            complete=False,
            freshness=_freshness("partial", _partial_detail(omissions, coverage_gaps)),
        )

    def _state(self, checkpoint: str | None) -> dict[str, Any]:
        if checkpoint is None:
            return self._initial_state()
        decoded = _decode_checkpoint(checkpoint, provider=self.provider_name)
        if decoded.get("round") == "complete":
            # A complete pass becomes a bounded recent-change pass on the next run.
            prior_since = decoded.get("since")
            state = self._initial_state()
            state["incremental_since"] = prior_since if isinstance(prior_since, str) else _now()
            self._restore_completion_checkpoint(state, decoded)
            return state
        return decoded

    def _completion_checkpoint(self, state: Mapping[str, Any]) -> dict[str, object]:
        del state
        return {}

    def _restore_completion_checkpoint(
        self, state: dict[str, Any], checkpoint: Mapping[str, Any]
    ) -> None:
        del state, checkpoint

    def _call(
        self,
        tool: str,
        connection_id: str,
        operation: str,
        input_value: Mapping[str, Any],
        *,
        continuation: object | None = None,
    ) -> _Page:
        values: dict[str, object] = {
            "connection_id": connection_id,
            "input": dict(input_value),
            "operation": operation,
        }
        response = self._runtime.call_app_corpus_read(
            tool,
            values,
            continuation=continuation,
        )
        if not isinstance(response, Mapping):
            raise ValidationError("connector read returned an invalid response")
        if response.get("status") not in {"ok", "completed_state_changed"}:
            raise ValidationError("connector read did not complete")
        payload = response.get("result")
        if not isinstance(payload, Mapping):
            raise ValidationError("connector read returned an invalid payload")
        next_continuation = response.get("continuation")
        artifact = response.get("artifact")
        if artifact is not None and not isinstance(artifact, Mapping):
            raise ValidationError("connector read returned an invalid artifact receipt")
        return _Page(
            payload=payload,
            continuation=next_continuation,
            artifact=None if artifact is None else _mapping(artifact),
        )

    def _initial_state(self) -> dict[str, Any]:
        raise NotImplementedError

    def _sync_round(
        self,
        connection_id: str,
        *,
        state: dict[str, Any],
        limit: int,
        fetched_at: str,
    ) -> tuple[list[AppCorpusDocument], int, bool]:
        raise NotImplementedError


class GoogleAppCorpusAdapter(_CorpusProviderAdapter):
    """Gmail, Google Calendar, and text-exportable Google Drive documents."""

    provider_name = "google"

    def __init__(
        self,
        runtime: ConnectorRuntime,
        *,
        sources: frozenset[str] | None = None,
    ) -> None:
        super().__init__(runtime)
        selected = (
            frozenset({"gmail", "google_calendar", "google_drive"}) if sources is None else sources
        )
        if not selected or not selected <= {"gmail", "google_calendar", "google_drive"}:
            raise ValidationError("Google app corpus sources are invalid")
        self._sources = selected

    def _initial_state(self) -> dict[str, Any]:
        return {
            "calendar": {"calendar_ids": [], "index": 0, "listed": False},
            "drive": {},
            "gmail": {},
            "provider": self.provider_name,
            "round": "running",
            "v": _CHECKPOINT_VERSION,
        }

    def _completion_checkpoint(self, state: Mapping[str, Any]) -> dict[str, object]:
        checkpoint: dict[str, object] = {}
        gmail = _mapping(state.get("gmail"))
        history_id = _gmail_history_id(gmail.get("history_id"))
        if history_id is not None and gmail.get("history_anchor_captured") is True:
            checkpoint.update({"gmail_history_anchor": True, "gmail_history_id": history_id})
        drive = _mapping(state.get("drive"))
        change_id = _drive_change_id(drive.get("change_id"))
        if change_id is not None and drive.get("change_anchor_captured") is True:
            checkpoint.update({"drive_change_anchor": True, "drive_change_id": change_id})
        return checkpoint

    def _restore_completion_checkpoint(
        self, state: dict[str, Any], checkpoint: Mapping[str, Any]
    ) -> None:
        history_id = _gmail_history_id(checkpoint.get("gmail_history_id"))
        gmail = _mapping_state(state, "gmail")
        if history_id is not None and checkpoint.get("gmail_history_anchor") is True:
            gmail["history_anchor_captured"] = True
            gmail["history_id"] = history_id
        elif history_id is not None:
            _record_coverage_gap(
                gmail,
                "Gmail checkpoint cursor has no fixed history anchor; a full scan will restart",
            )
            state.pop("incremental_since", None)

        recovery_epoch = _positive_int(
            checkpoint.get(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT), default=0
        )
        if recovery_epoch:
            gmail[_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH] = recovery_epoch

        if "google_drive" not in self._sources:
            return
        drive = _mapping_state(state, "drive")
        change_id = _drive_change_id(checkpoint.get("drive_change_id"))
        if change_id is not None and checkpoint.get("drive_change_anchor") is True:
            drive["change_anchor_captured"] = True
            drive["change_id"] = change_id
            return
        drive["legacy_checkpoint"] = True

    def _sync_round(
        self,
        connection_id: str,
        *,
        state: dict[str, Any],
        limit: int,
        fetched_at: str,
    ) -> tuple[list[AppCorpusDocument], int, bool]:
        gmail = _mapping_state(state, "gmail")
        calendar = _mapping_state(state, "calendar")
        drive = _mapping_state(state, "drive")
        page_size = _per_source_limit(limit, 3)
        incremental_since = _optional_text(state.get("incremental_since"))
        documents: list[AppCorpusDocument] = []
        scanned = 0

        recovery_epoch = _positive_int(
            state.get(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT), default=0
        )
        if recovery_epoch:
            state.pop(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT, None)
            gmail[_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH] = recovery_epoch
        recovery_epoch = _positive_int(
            gmail.get(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH), default=0
        )
        if (
            "gmail" in self._sources
            and recovery_epoch
            and gmail.get(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_STARTED) is not True
        ):
            # A legacy release could replace an existing message body with a
            # provider-gap document after its cursor had moved past that message.
            # Reuse the normal baseline paging path so successful detail reads
            # overwrite those same object keys.  The corpus retains every old
            # document until its source sends a replacement or deletion.
            gmail.clear()
            gmail[_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH] = recovery_epoch
            state.pop("incremental_since", None)
            incremental_since = None

        if "gmail" in self._sources and _gmail_needs_backfill_restart(gmail):
            gmail.clear()
            _record_coverage_gap(
                gmail,
                "Gmail backfill checkpoint had no fixed history anchor; "
                "the scan restarted from the beginning",
            )
        if "gmail" not in self._sources:
            gmail["done"] = True
        elif not gmail.get("done"):
            history_anchor_id = _gmail_history_id(gmail.get("history_anchor_id"))
            if incremental_since is not None and history_anchor_id is None:
                history_anchor_id = _gmail_history_id(gmail.get("history_id"))
                if history_anchor_id is not None:
                    # A history crawl can span sync calls.  Its start cursor must
                    # remain the one that created its continuation.
                    gmail["history_anchor_id"] = history_anchor_id
            if incremental_since is not None and history_anchor_id is not None:
                try:
                    gmail["retry_current_page"] = False
                    gmail.pop("retry_category", None)
                    page = self._call(
                        "gsv_gmail_read",
                        connection_id,
                        "history.list",
                        {
                            "history_types": ["messageAdded", "messageDeleted"],
                            "page_size": page_size,
                            "start_history_id": history_anchor_id,
                        },
                        continuation=gmail.get("history_continuation"),
                    )
                except Exception as exc:
                    if _gmail_history_expired(exc):
                        gmail.pop("history_continuation", None)
                        gmail.pop("history_anchor_id", None)
                        gmail.pop("history_id", None)
                        state.pop("incremental_since", None)
                        _record_coverage_gap(
                            gmail,
                            "Gmail history expired; a full scan is required "
                            "before deletions are current",
                        )
                    raise
                history = _items(page.payload, "history")
                scanned += len(history)
                documents.extend(
                    self._gmail_history_documents(connection_id, history, fetched_at, gmail)
                )
                if gmail.pop("retry_current_page", False):
                    _record_coverage_gap(
                        gmail,
                        "Gmail message details are retrying after "
                        f"{_gmail_detail_retry_reason(gmail)}",
                    )
                elif page.continuation is None:
                    final_history_id = _gmail_history_id(page.payload.get("historyId"))
                    if final_history_id is None:
                        _record_coverage_gap(
                            gmail,
                            "Gmail history ended without a numeric cursor; "
                            "the next scan will repeat this range",
                        )
                    else:
                        # The connector returns the latest history ID only after
                        # the final page.  Detail reads are deliberately unable
                        # to advance this cursor.
                        gmail["history_id"] = final_history_id
                        _clear_coverage_gaps(gmail, prefix="Gmail history")
                        _clear_coverage_gaps(gmail, prefix="Gmail message details")
                    gmail.pop("history_anchor_id", None)
                    gmail.pop("history_continuation", None)
                    gmail["done"] = True
                else:
                    gmail["history_continuation"] = page.continuation
            else:
                history_anchor_id = _gmail_history_id(gmail.get("history_anchor_id"))
                if history_anchor_id is None:
                    profile = self._call("gsv_gmail_read", connection_id, "profile.get", {}).payload
                    history_anchor_id = _gmail_history_id(profile.get("historyId"))
                    if history_anchor_id is None:
                        _record_coverage_gap(
                            gmail,
                            "Gmail profile had no numeric history cursor; "
                            "deletions are not yet observable",
                        )
                    else:
                        gmail["history_anchor_captured"] = True
                        gmail["history_anchor_id"] = history_anchor_id
                query: dict[str, Any] = {"page_size": page_size}
                if incremental_since is not None:
                    query["query"] = f"after:{_gmail_overlap_date(incremental_since)}"
                gmail["retry_current_page"] = False
                gmail.pop("retry_category", None)
                page = self._call(
                    "gsv_gmail_read",
                    connection_id,
                    "messages.list",
                    query,
                    continuation=gmail.get("continuation"),
                )
                messages = _items(page.payload, "messages")
                scanned += len(messages)
                if recovery_epoch:
                    # A detail failure below must retry this provider page, not
                    # restart the full recovery baseline on every sync.
                    gmail[_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_STARTED] = True
                documents.extend(
                    self._gmail_message_documents(
                        connection_id, messages[:page_size], fetched_at, gmail
                    )
                )
                if gmail.pop("retry_current_page", False):
                    _record_coverage_gap(
                        gmail,
                        "Gmail message details are retrying after "
                        f"{_gmail_detail_retry_reason(gmail)}",
                    )
                else:
                    _finish_page(gmail, page.continuation)
                    _clear_coverage_gaps(gmail, prefix="Gmail message details")
                if gmail.get("done"):
                    if history_anchor_id is None:
                        _record_coverage_gap(
                            gmail,
                            "Gmail deletions are not observable until a numeric "
                            "history cursor is read",
                        )
                    else:
                        gmail["history_id"] = history_anchor_id
                        gmail.pop("history_anchor_id", None)
                        _clear_coverage_gaps(gmail, prefix="Gmail deletions")
                        _clear_coverage_gaps(gmail, prefix="Gmail backfill checkpoint")
                if gmail.get("done"):
                    gmail.pop(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH, None)
                    gmail.pop(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_STARTED, None)

        if "google_calendar" not in self._sources:
            calendar["done"] = True
        elif not calendar.get("done"):
            if not calendar.get("listed"):
                page = self._call(
                    "gsv_google_calendar_read",
                    connection_id,
                    "calendars.list",
                    {"page_size": page_size},
                    continuation=calendar.get("list_continuation"),
                )
                items = _items(page.payload, "items")
                scanned += len(items)
                known = _string_list(calendar.get("calendar_ids"), maximum=_MAX_CALENDARS)
                for item in items:
                    calendar_id = _optional_text(item.get("id"))
                    if calendar_id is not None and calendar_id not in known:
                        if len(known) >= _MAX_CALENDARS:
                            raise ValidationError(
                                "Google Calendar list exceeds the local sync bound"
                            )
                        known.append(calendar_id)
                calendar["calendar_ids"] = known
                if _google_sync_complete(page.continuation):
                    calendar["listed"] = True
                    calendar.pop("list_continuation", None)
                else:
                    calendar["list_continuation"] = page.continuation
            else:
                calendar_ids = _string_list(calendar.get("calendar_ids"), maximum=_MAX_CALENDARS)
                index = _nonnegative_int(calendar.get("index"))
                if index >= len(calendar_ids):
                    calendar["done"] = True
                else:
                    calendar_id = calendar_ids[index]
                    event_input: dict[str, Any] = {
                        "calendar_id": calendar_id,
                        "page_size": page_size,
                        "show_deleted": True,
                    }
                    if incremental_since is not None:
                        event_input["updated_min"] = _overlap_timestamp(incremental_since)
                    page = self._call(
                        "gsv_google_calendar_read",
                        connection_id,
                        "events.list",
                        event_input,
                        continuation=calendar.get("event_continuation"),
                    )
                    items = _items(page.payload, "items")
                    scanned += len(items)
                    documents.extend(
                        _google_event_document(connection_id, calendar_id, item, fetched_at)
                        for item in items
                    )
                    if _google_sync_complete(page.continuation):
                        calendar.pop("event_continuation", None)
                        calendar["index"] = index + 1
                        if index + 1 >= len(calendar_ids):
                            calendar["done"] = True
                    else:
                        calendar["event_continuation"] = page.continuation

        if "google_drive" in self._sources:
            _record_coverage_gap(
                drive,
                "Google Drive shared drives are not enrolled in this My Drive-only scan",
            )
            if _drive_needs_change_bootstrap(drive):
                drive.clear()
                drive["full_scan"] = True
                _record_coverage_gap(
                    drive,
                    "Google Drive shared drives are not enrolled in this My Drive-only scan",
                )
                _record_coverage_gap(
                    drive,
                    "Google Drive permanent deletions are not reconciled until the change scan "
                    "completes",
                )
        if "google_drive" not in self._sources:
            drive["done"] = True
        elif not drive.get("done"):
            phase = _optional_text(drive.get("phase"))
            if phase not in {"files", "changes"}:
                prior_change_id = _drive_change_id(drive.get("change_id"))
                if prior_change_id is not None:
                    drive["change_anchor"] = prior_change_id
                    drive["phase"] = "changes"
                else:
                    anchor_page = self._call(
                        "gsv_google_drive_read",
                        connection_id,
                        "changes.get_start_page_token",
                        {},
                    )
                    anchor = _drive_change_id(anchor_page.payload.get("startPageToken"))
                    if anchor is None:
                        _record_coverage_gap(
                            drive,
                            "Google Drive did not provide a change cursor; "
                            "permanent deletions are not current",
                        )
                        raise ValidationError("Google Drive start page token is invalid")
                    drive["change_anchor"] = anchor
                    drive["change_anchor_captured"] = True
                    drive["phase"] = "files"
                    _record_coverage_gap(
                        drive,
                        "Google Drive permanent deletions are not reconciled until "
                        "the change scan completes",
                    )
                phase = _optional_text(drive.get("phase"))

            if phase == "files":
                drive_input: dict[str, Any] = {
                    "include_trashed": True,
                    "order_by": ["modifiedTime desc"],
                    "page_size": page_size,
                }
                if incremental_since is not None and drive.get("full_scan") is not True:
                    drive_input["query"] = (
                        f"modifiedTime > '{_overlap_timestamp(incremental_since)}'"
                    )
                page = self._call(
                    "gsv_google_drive_read",
                    connection_id,
                    "files.list",
                    drive_input,
                    continuation=drive.get("continuation"),
                )
                files = _items(page.payload, "files")
                scanned += len(files)
                for file in files[:page_size]:
                    try:
                        document = self._drive_document(connection_id, file, fetched_at)
                    except Exception as exc:
                        document = _drive_gap_document(
                            connection_id, file, fetched_at, reason=_failure_detail(exc)
                        )
                    if document is not None:
                        documents.append(document)
                        if document.metadata.get("extraction_status") == "gap":
                            _record_omission(drive)
                if page.continuation is None:
                    drive.pop("continuation", None)
                    drive.pop("full_scan", None)
                    drive["phase"] = "changes"
                else:
                    drive["continuation"] = page.continuation
            elif phase == "changes":
                anchor = _drive_change_id(drive.get("change_anchor"))
                if anchor is None:
                    raise ValidationError("Google Drive change scan has no fixed anchor")
                page = self._call(
                    "gsv_google_drive_read",
                    connection_id,
                    "changes.list",
                    {"page_size": page_size, "start_change_id": anchor},
                    continuation=drive.get("changes_continuation"),
                )
                changes = _items(page.payload, "changes")
                scanned += len(changes)
                documents.extend(
                    self._drive_change_documents(
                        connection_id, changes[:page_size], fetched_at, drive
                    )
                )
                if page.continuation is not None:
                    drive["changes_continuation"] = page.continuation
                else:
                    final_change_id = _drive_change_id(page.payload.get("newStartPageToken"))
                    drive.pop("changes_continuation", None)
                    if final_change_id is None:
                        _record_coverage_gap(
                            drive,
                            "Google Drive change scan ended without a final cursor; "
                            "permanent deletions are not current",
                        )
                    else:
                        drive["change_id"] = final_change_id
                        drive.pop("change_anchor", None)
                        drive["done"] = True
                        _clear_coverage_gaps(drive, prefix="Google Drive permanent deletions")
                        _clear_coverage_gaps(
                            drive, prefix="Google Drive did not provide a change cursor"
                        )
                        _clear_coverage_gaps(
                            drive, prefix="Google Drive change scan ended without a final cursor"
                        )

        done = bool(gmail.get("done")) and bool(calendar.get("done")) and bool(drive.get("done"))
        return documents, scanned, done

    def _drive_document(
        self, connection_id: str, file: Mapping[str, Any], fetched_at: str
    ) -> AppCorpusDocument | None:
        file_id = _required_identifier(file, "id", "Google Drive file")
        mime_type = _optional_text(file.get("mimeType"))
        metadata = _drive_metadata(file, mime_type=mime_type)
        common = {
            "connection_id": connection_id,
            "provider": "google_drive",
            "object_id": f"drive:{file_id}",
            "revision": _revision(file, "version", "modifiedTime", fallback=file_id),
            "fetched_at": fetched_at,
            "source_ref": f"google-drive:file:{file_id}",
            "title": _title(file.get("name"), "Google Drive document"),
        }
        if file.get("trashed") is True:
            return _document(
                **common,
                text="",
                metadata={**metadata, "trashed": True},
                deleted=True,
            )
        if mime_type == _GOOGLE_DOCUMENT_MIME:
            export = self._call(
                "gsv_google_drive_read",
                connection_id,
                "files.export",
                {"delivery": "inline_chunk", "export_mime_type": "text/plain", "file_id": file_id},
            ).payload
            encoded = _optional_text(export.get("content_base64"))
            if encoded is None:
                raise ValidationError("Google Drive text export had no content")
            return _document(
                **common,
                text=_decode_base64_text(encoded),
                metadata=metadata,
            )
        if mime_type not in _artifact_extractable_mimes():
            return None
        size = _provider_size(file.get("size"))
        if size is None:
            return _provider_gap_document(
                **common,
                metadata=metadata,
                reason="file was not read because its provider size is unavailable",
            )
        if size > _MAX_ATTACHMENT_BYTES:
            return _provider_gap_document(
                **common,
                metadata=metadata,
                reason="file was not read because it exceeds the artifact read bound",
            )
        content_input: dict[str, Any] = {
            "delivery": "artifact",
            "file_id": file_id,
            "filename": _title(file.get("name"), "Google Drive document"),
            "mime_type": mime_type,
            "resource_key": _optional_text(file.get("resourceKey")),
        }
        page = self._call(
            "gsv_google_drive_read",
            connection_id,
            "files.content",
            {key: value for key, value in content_input.items() if value is not None},
        )
        artifact = page.artifact or {}
        path = _optional_text(artifact.get("path"))
        if path is None:
            raise ValidationError("Google Drive file artifact is unavailable")
        extraction = extract_text(Path(path), mime_type)
        if extraction.status == "ok":
            return _document(
                **common,
                text=extraction.text,
                metadata={**metadata, "extraction_status": "ok"},
            )
        return _provider_gap_document(
            **common,
            metadata={
                **metadata,
                "extraction_omissions": ",".join(extraction.omissions[:12]),
                "extraction_status": extraction.status,
            },
            reason=extraction.reason or "file text extraction was incomplete",
        )

    def _drive_change_documents(
        self,
        connection_id: str,
        changes: list[Mapping[str, Any]],
        fetched_at: str,
        drive: dict[str, Any],
    ) -> list[AppCorpusDocument]:
        documents: list[AppCorpusDocument] = []
        for change in changes:
            file_id = _required_identifier(change, "fileId", "Google Drive change")
            if change.get("removed") is True:
                documents.append(
                    _drive_removed_document(connection_id, file_id, change, fetched_at)
                )
                continue
            file = _mapping(change.get("file"))
            if not file:
                documents.append(
                    _provider_gap_document(
                        connection_id=connection_id,
                        provider="google_drive",
                        object_id=f"drive:{file_id}",
                        revision=_revision(change, "time", fallback=file_id),
                        fetched_at=fetched_at,
                        source_ref=f"google-drive:file:{file_id}",
                        title="Google Drive file",
                        metadata={},
                        reason="Google Drive change had no file metadata",
                    )
                )
                _record_omission(drive)
                continue
            if _required_identifier(file, "id", "Google Drive changed file") != file_id:
                raise ValidationError("Google Drive change returned a different file")
            try:
                document = self._drive_document(connection_id, file, fetched_at)
            except Exception as exc:
                document = _drive_gap_document(
                    connection_id, file, fetched_at, reason=_failure_detail(exc)
                )
            if document is not None:
                documents.append(document)
                if document.metadata.get("extraction_status") == "gap":
                    _record_omission(drive)
        return documents

    def _gmail_message_documents(
        self,
        connection_id: str,
        messages: list[Mapping[str, Any]],
        fetched_at: str,
        gmail: dict[str, Any],
    ) -> list[AppCorpusDocument]:
        documents: list[AppCorpusDocument] = []
        for summary in messages:
            message_id = _required_identifier(summary, "id", "Gmail message")
            try:
                try:
                    detail = self._call(
                        "gsv_gmail_read",
                        connection_id,
                        "messages.get",
                        {"format": "full", "message_id": message_id},
                    ).payload
                except ValidationError as exc:
                    if not _gmail_full_message_exceeds_json_bound(exc):
                        raise
                    metadata = self._call(
                        "gsv_gmail_read",
                        connection_id,
                        "messages.get",
                        {
                            "format": "metadata",
                            "message_id": message_id,
                            "metadata_header_names": ["From", "Subject"],
                        },
                    ).payload
                    raw = self._call(
                        "gsv_gmail_read",
                        connection_id,
                        "messages.get",
                        {"format": "raw", "message_id": message_id},
                    )
                    documents.extend(
                        self._gmail_raw_documents(
                            connection_id,
                            metadata,
                            raw.artifact,
                            fetched_at,
                            fallback_id=message_id,
                        )
                    )
                else:
                    documents.extend(
                        self._gmail_documents(
                            connection_id, detail, fetched_at, fallback_id=message_id
                        )
                    )
            except ConnectorProviderError as exc:
                if exc.origin is ConnectorOrigin.GMAIL and exc.status == 404:
                    # A message may disappear between a list and its detail read.
                    # Do not infer a corpus deletion from that race, but let the
                    # bounded scan advance instead of retrying this page forever.
                    _record_omission(gmail)
                    _record_coverage_gap(
                        gmail,
                        "Gmail listed message could not be read; "
                        "any prior corpus content was retained",
                    )
                    continue
                _record_gmail_detail_retry(gmail, exc)
            except Exception as exc:
                # A summary page cannot replace a previously indexed message body.
                # Keep its provider cursor fixed and retry this same page on the next sync.
                _record_gmail_detail_retry(gmail, exc)
        return documents

    def _gmail_history_documents(
        self,
        connection_id: str,
        history: list[Mapping[str, Any]],
        fetched_at: str,
        gmail: dict[str, Any],
    ) -> list[AppCorpusDocument]:
        documents: list[AppCorpusDocument] = []
        for record in history:
            revision = _gmail_history_id(record.get("id")) or "unknown"
            for deleted in _items(record, "messagesDeleted"):
                message_id = _optional_text(_mapping(deleted.get("message")).get("id"))
                if message_id is not None:
                    documents.append(
                        _gmail_deleted_document(connection_id, message_id, revision, fetched_at)
                    )
            additions: list[Mapping[str, Any]] = []
            for added in _items(record, "messagesAdded"):
                message_id = _optional_text(_mapping(added.get("message")).get("id"))
                if message_id is not None:
                    additions.append({"id": message_id})
            documents.extend(
                self._gmail_message_documents(connection_id, additions, fetched_at, gmail)
            )
        return documents

    def _gmail_raw_documents(
        self,
        connection_id: str,
        metadata: Mapping[str, Any],
        artifact: Mapping[str, Any] | None,
        fetched_at: str,
        *,
        fallback_id: str,
    ) -> list[AppCorpusDocument]:
        path = _optional_text((artifact or {}).get("path"))
        if path is None:
            raise ValidationError("Gmail raw message artifact is unavailable")
        parsed = _parse_gmail_raw_message(Path(path))
        message_id = _optional_text(metadata.get("id")) or fallback_id
        revision = _revision(metadata, "historyId", "internalDate", fallback=message_id)
        received_at = _gmail_internal_date(metadata.get("internalDate"))
        attachment_names = tuple(attachment.filename for attachment in parsed.attachments)
        body = parsed.body
        if attachment_names:
            body = _bounded_text(body + "\n\nAttachments: " + ", ".join(attachment_names))
        metadata_payload = metadata.get("payload")
        document_metadata = _metadata(
            attachment_count=parsed.attachment_count,
            labels=_joined_strings(metadata.get("labelIds")),
            received_at=received_at,
            sender=parsed.sender or _gmail_header(metadata_payload, "from"),
            thread_id=_optional_text(metadata.get("threadId")),
        )
        omissions = _gmail_raw_extraction_omissions(parsed)
        if omissions:
            document_metadata = {
                **document_metadata,
                "extraction_omissions": ",".join(omissions),
                "extraction_status": "partial",
            }
        documents = [
            AppCorpusDocument(
                connection_id=connection_id,
                provider="gmail",
                object_id=f"gmail:{message_id}",
                revision=revision,
                fetched_at=fetched_at,
                source_ref=f"gmail:message:{message_id}",
                title=_title(
                    parsed.subject or _gmail_header(metadata_payload, "subject"), "Gmail message"
                ),
                text=body,
                metadata=document_metadata,
                freshness=_freshness(
                    "partial" if omissions else "complete",
                    "; ".join(omissions) if omissions else "provider object was read",
                ),
            )
        ]
        for attachment in parsed.attachments:
            documents.append(
                _gmail_raw_attachment_document(
                    connection_id,
                    message_id,
                    revision,
                    attachment,
                    fetched_at,
                    received_at=received_at,
                )
            )
        return documents

    def _gmail_documents(
        self,
        connection_id: str,
        value: Mapping[str, Any],
        fetched_at: str,
        *,
        fallback_id: str,
    ) -> list[AppCorpusDocument]:
        message_id = _optional_text(value.get("id")) or fallback_id
        revision = _revision(value, "historyId", "internalDate", fallback=message_id)
        received_at = _gmail_internal_date(value.get("internalDate"))
        documents = [_gmail_document(connection_id, value, fetched_at, fallback_id=fallback_id)]
        for attachment in _gmail_attachments(value.get("payload")):
            documents.append(
                self._gmail_attachment_document(
                    connection_id,
                    message_id,
                    revision,
                    attachment,
                    fetched_at,
                    received_at=received_at,
                )
            )
        return documents

    def _gmail_attachment_document(
        self,
        connection_id: str,
        message_id: str,
        revision: str,
        attachment: _Attachment,
        fetched_at: str,
        *,
        received_at: str | None,
    ) -> AppCorpusDocument:
        source_ref = f"gmail:attachment:{message_id}:{attachment.attachment_id}"
        base_metadata = _metadata(
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            parent_message_id=message_id,
            received_at=received_at,
            size=attachment.size,
        )
        if attachment.size is None or attachment.size > _INLINE_ATTACHMENT_BYTES:
            return _attachment_gap_document(
                connection_id=connection_id,
                provider="gmail",
                object_id=f"gmail-attachment:{message_id}:{attachment.attachment_id}",
                revision=revision,
                fetched_at=fetched_at,
                source_ref=source_ref,
                title=attachment.filename,
                metadata=base_metadata,
                reason="attachment was not read because it exceeds the inline read bound",
            )
        if attachment.mime_type not in _artifact_extractable_mimes():
            return _attachment_gap_document(
                connection_id=connection_id,
                provider="gmail",
                object_id=f"gmail-attachment:{message_id}:{attachment.attachment_id}",
                revision=revision,
                fetched_at=fetched_at,
                source_ref=source_ref,
                title=attachment.filename,
                metadata=base_metadata,
                reason="attachment type needs a local artifact extraction route",
            )
        try:
            page = self._call(
                "gsv_gmail_read",
                connection_id,
                "attachments.get",
                {
                    "attachment_id": attachment.attachment_id,
                    "delivery": "inline_chunk",
                    "message_id": message_id,
                },
            )
            encoded = _optional_text(page.payload.get("content_base64"))
            if encoded is None:
                raise ValidationError("Gmail attachment had no content")
            raw = _decode_base64_bytes(encoded, maximum=_INLINE_ATTACHMENT_BYTES)
            if attachment.mime_type in _inline_text_mimes():
                text = _bounded_text(raw.decode("utf-8", errors="replace"))
                if attachment.mime_type == "text/html":
                    text = _html_to_text(text)
                return _document(
                    connection_id=connection_id,
                    provider="gmail",
                    object_id=f"gmail-attachment:{message_id}:{attachment.attachment_id}",
                    revision=revision,
                    fetched_at=fetched_at,
                    source_ref=source_ref,
                    title=attachment.filename,
                    text=text,
                    metadata=base_metadata,
                )
            extraction = _extract_inline_attachment(raw, attachment)
            if extraction.status == "ok":
                return _document(
                    connection_id=connection_id,
                    provider="gmail",
                    object_id=f"gmail-attachment:{message_id}:{attachment.attachment_id}",
                    revision=revision,
                    fetched_at=fetched_at,
                    source_ref=source_ref,
                    title=attachment.filename,
                    text=extraction.text,
                    metadata={**base_metadata, "extraction_status": extraction.status},
                )
            return _attachment_gap_document(
                connection_id=connection_id,
                provider="gmail",
                object_id=f"gmail-attachment:{message_id}:{attachment.attachment_id}",
                revision=revision,
                fetched_at=fetched_at,
                source_ref=source_ref,
                title=attachment.filename,
                metadata={
                    **base_metadata,
                    "extraction_omissions": ",".join(extraction.omissions[:12]),
                    "extraction_status": extraction.status,
                },
                reason=extraction.reason or "attachment text extraction was incomplete",
            )
        except Exception as exc:
            return _attachment_gap_document(
                connection_id=connection_id,
                provider="gmail",
                object_id=f"gmail-attachment:{message_id}:{attachment.attachment_id}",
                revision=revision,
                fetched_at=fetched_at,
                source_ref=source_ref,
                title=attachment.filename,
                metadata=base_metadata,
                reason=_failure_detail(exc),
            )


class MicrosoftAppCorpusAdapter(_CorpusProviderAdapter):
    """Outlook mail and calendar content through Microsoft Graph connector reads."""

    provider_name = "microsoft"

    def __init__(
        self,
        runtime: ConnectorRuntime,
        *,
        sources: frozenset[str] | None = None,
        calendar_delta_window: MicrosoftCalendarDeltaWindow | None = None,
    ) -> None:
        super().__init__(runtime)
        selected = frozenset({"outlook_mail", "outlook_calendar"}) if sources is None else sources
        if not selected or not selected <= {"outlook_mail", "outlook_calendar"}:
            raise ValidationError("Microsoft app corpus sources are invalid")
        self._sources = selected
        self._calendar_delta_window = (
            None
            if calendar_delta_window is None
            else validate_calendar_delta_window(
                calendar_delta_window.start, calendar_delta_window.end
            )
        )

    def _initial_state(self) -> dict[str, Any]:
        return {
            "calendar": {"calendar_ids": [], "index": 0, "listed": False},
            "mail": {},
            "provider": self.provider_name,
            "round": "running",
            "v": _CHECKPOINT_VERSION,
        }

    def _completion_checkpoint(self, state: Mapping[str, Any]) -> dict[str, object]:
        checkpoint: dict[str, object] = {}
        if "outlook_mail" in self._sources:
            mail = _mapping(state.get("mail"))
            mail_checkpoint = _optional_text(mail.get("delta_checkpoint"))
            if mail_checkpoint is not None:
                checkpoint["mail_delta_checkpoint"] = mail_checkpoint
        if "outlook_calendar" in self._sources and self._calendar_delta_window is not None:
            calendar = _mapping(state.get("calendar"))
            calendar_checkpoint = _optional_text(calendar.get("delta_checkpoint"))
            primary_calendar_id = _optional_text(calendar.get("primary_calendar_id"))
            if primary_calendar_id is not None and calendar_delta_checkpoint_matches_window(
                calendar_checkpoint, self._calendar_delta_window
            ):
                checkpoint["calendar_delta_checkpoint"] = calendar_checkpoint
                checkpoint["calendar_delta_primary_calendar_id"] = primary_calendar_id
        return checkpoint

    def _restore_completion_checkpoint(
        self, state: dict[str, Any], checkpoint: Mapping[str, Any]
    ) -> None:
        value = _optional_text(checkpoint.get("mail_delta_checkpoint"))
        if value is not None:
            _mapping_state(state, "mail")["delta_checkpoint"] = value
        if self._calendar_delta_window is None:
            return
        calendar_checkpoint = _optional_text(checkpoint.get("calendar_delta_checkpoint"))
        primary_calendar_id = _optional_text(checkpoint.get("calendar_delta_primary_calendar_id"))
        if primary_calendar_id is not None and calendar_delta_checkpoint_matches_window(
            calendar_checkpoint, self._calendar_delta_window
        ):
            calendar = _mapping_state(state, "calendar")
            calendar["delta_checkpoint"] = calendar_checkpoint
            calendar["primary_calendar_id"] = primary_calendar_id

    def _sync_round(
        self,
        connection_id: str,
        *,
        state: dict[str, Any],
        limit: int,
        fetched_at: str,
    ) -> tuple[list[AppCorpusDocument], int, bool]:
        mail = _mapping_state(state, "mail")
        calendar = _mapping_state(state, "calendar")
        page_size = _per_source_limit(limit, 2)
        documents: list[AppCorpusDocument] = []
        scanned = 0

        if "outlook_mail" not in self._sources:
            mail["done"] = True
        elif not mail.get("done"):
            delta = MicrosoftMailDeltaSync(self._runtime).sync(
                connection_id,
                checkpoint=_optional_text(mail.get("delta_checkpoint")),
                limit=page_size,
            )
            scanned += delta.scanned
            for change in delta.changes:
                try:
                    detail = self._call(
                        "gsv_outlook_mail_read",
                        connection_id,
                        "messages.get",
                        {"body_format": "html", "message_id": change.message_id},
                    ).payload
                except ConnectorProviderError as exc:
                    if exc.status != 404:
                        raise
                    documents.append(
                        _outlook_delta_deleted_document(
                            connection_id,
                            change.message_id,
                            _revision(
                                change.value,
                                "changeKey",
                                "lastModifiedDateTime",
                                fallback=change.message_id,
                            ),
                            fetched_at,
                        )
                    )
                    continue
                documents.extend(
                    self._outlook_documents(
                        connection_id,
                        detail,
                        fetched_at,
                        fallback_id=change.message_id,
                        immutable_ids=True,
                    )
                )
            for removal in delta.removals:
                try:
                    detail = self._call(
                        "gsv_outlook_mail_read",
                        connection_id,
                        "messages.get",
                        {"body_format": "html", "message_id": removal.message_id},
                    ).payload
                except ConnectorProviderError as exc:
                    if exc.status != 404:
                        _record_coverage_gap(
                            mail,
                            "Outlook removal could not be confirmed; its corpus record "
                            "was retained",
                        )
                        continue
                    documents.append(
                        _outlook_delta_deleted_document(
                            connection_id,
                            removal.message_id,
                            removal.revision,
                            fetched_at,
                        )
                    )
                    continue
                documents.extend(
                    self._outlook_documents(
                        connection_id,
                        detail,
                        fetched_at,
                        fallback_id=removal.message_id,
                        immutable_ids=True,
                    )
                )
            mail["delta_checkpoint"] = delta.checkpoint
            _clear_coverage_gaps(
                mail,
                prefix=(
                    "Outlook Mail permanent deletions are not observable without a Graph delta read"
                ),
            )
            for detail in delta.coverage_gaps:
                _record_coverage_gap(mail, detail)
            mail["done"] = delta.complete

        if "outlook_calendar" not in self._sources:
            calendar["done"] = True
        elif not calendar.get("done"):
            self._record_calendar_delta_coverage(calendar)
            if not calendar.get("listed"):
                page = self._call(
                    "gsv_outlook_calendar_read",
                    connection_id,
                    "calendars.list",
                    {"page_size": page_size},
                    continuation=calendar.get("list_continuation"),
                )
                items = _items(page.payload, "value")
                scanned += len(items)
                known = _string_list(calendar.get("calendar_ids"), maximum=_MAX_CALENDARS)
                for item in items:
                    calendar_id = _optional_text(item.get("id"))
                    if calendar_id is not None and calendar_id not in known:
                        if len(known) >= _MAX_CALENDARS:
                            raise ValidationError(
                                "Outlook calendar list exceeds the local sync bound"
                            )
                        known.append(calendar_id)
                    if item.get("isDefaultCalendar") is True and calendar_id is not None:
                        calendar["primary_calendar_id"] = calendar_id
                calendar["calendar_ids"] = known
                if page.continuation is None:
                    calendar["listed"] = True
                    calendar.pop("list_continuation", None)
                else:
                    calendar["list_continuation"] = page.continuation
            else:
                calendar_ids = _string_list(calendar.get("calendar_ids"), maximum=_MAX_CALENDARS)
                index = _nonnegative_int(calendar.get("index"))
                primary_calendar_id = _optional_text(calendar.get("primary_calendar_id"))
                if (
                    index >= len(calendar_ids)
                    and self._calendar_delta_window is not None
                    and primary_calendar_id is None
                ):
                    primary = self._call(
                        "gsv_outlook_calendar_read",
                        connection_id,
                        "calendars.get",
                        {"calendar_id": "primary"},
                    ).payload
                    primary_calendar_id = _optional_text(primary.get("id"))
                    if primary_calendar_id is not None:
                        calendar["primary_calendar_id"] = primary_calendar_id
                if index < len(calendar_ids):
                    calendar_id = calendar_ids[index]
                    page = self._call(
                        "gsv_outlook_calendar_read",
                        connection_id,
                        "events.list",
                        {
                            "calendar_id": calendar_id,
                            "order_by": "last_modified_at",
                            "page_size": page_size,
                            "sort_direction": "descending",
                        },
                        continuation=calendar.get("event_continuation"),
                    )
                    items = _items(page.payload, "value")
                    scanned += len(items)
                    documents.extend(
                        _outlook_event_document(connection_id, calendar_id, item, fetched_at)
                        for item in items
                    )
                    if page.continuation is None:
                        calendar.pop("event_continuation", None)
                        calendar["index"] = index + 1
                        if self._calendar_delta_window is None and index + 1 >= len(calendar_ids):
                            calendar["done"] = True
                    else:
                        calendar["event_continuation"] = page.continuation
                elif self._calendar_delta_window is None:
                    calendar["done"] = True
                elif primary_calendar_id is None:
                    _record_coverage_gap(
                        calendar,
                        "Outlook Calendar primary calendar was not identified; permanent "
                        "deletions remain unobservable",
                    )
                    calendar["done"] = True
                else:
                    _record_coverage_gap(
                        calendar,
                        "Outlook Calendar permanent deletions in the configured primary-calendar "
                        "window are not current until its Graph delta cursor finishes",
                    )
                    delta = MicrosoftCalendarDeltaSync(
                        self._runtime, window=self._calendar_delta_window
                    ).sync(
                        connection_id,
                        checkpoint=_optional_text(calendar.get("delta_checkpoint")),
                        limit=page_size,
                    )
                    scanned += delta.scanned
                    for change in delta.changes:
                        if not change.removed:
                            documents.append(
                                _outlook_event_document(
                                    connection_id,
                                    primary_calendar_id,
                                    change.value,
                                    fetched_at,
                                )
                            )
                            continue
                        try:
                            detail = self._call(
                                "gsv_outlook_calendar_read",
                                connection_id,
                                "events.get",
                                {"calendar_id": "primary", "event_id": change.event_id},
                            ).payload
                        except ConnectorProviderError as exc:
                            if exc.status != 404:
                                raise
                            documents.append(
                                _outlook_calendar_delta_deleted_document(
                                    connection_id,
                                    primary_calendar_id,
                                    change.event_id,
                                    _revision(
                                        change.value,
                                        "changeKey",
                                        "lastModifiedDateTime",
                                        fallback=change.event_id,
                                    ),
                                    fetched_at,
                                )
                            )
                            continue
                        documents.append(
                            _outlook_event_document(
                                connection_id,
                                primary_calendar_id,
                                detail,
                                fetched_at,
                            )
                        )
                    calendar["delta_checkpoint"] = delta.checkpoint
                    calendar["done"] = delta.complete
                    if delta.complete:
                        _clear_coverage_gaps(
                            calendar,
                            prefix=(
                                "Outlook Calendar permanent deletions in the configured "
                                "primary-calendar window"
                            ),
                        )

        return documents, scanned, bool(mail.get("done")) and bool(calendar.get("done"))

    def _record_calendar_delta_coverage(self, calendar: dict[str, Any]) -> None:
        if self._calendar_delta_window is None:
            _record_coverage_gap(
                calendar,
                "Outlook Calendar permanent deletions are not observable; "
                "cancellations are preserved",
            )
            return
        _clear_coverage_gaps(
            calendar,
            prefix="Outlook Calendar permanent deletions are not observable",
        )
        _record_coverage_gap(
            calendar,
            "Outlook Calendar permanent deletions are observed only for the primary "
            f"calendar from {self._calendar_delta_window.start} through "
            f"{self._calendar_delta_window.end}; secondary calendars and events outside "
            "that window are retained",
        )

    def _outlook_documents(
        self,
        connection_id: str,
        value: Mapping[str, Any],
        fetched_at: str,
        *,
        fallback_id: str,
        immutable_ids: bool = False,
    ) -> list[AppCorpusDocument]:
        message_id = _optional_text(value.get("id")) or fallback_id
        revision = _revision(value, "changeKey", "lastModifiedDateTime", fallback=message_id)
        documents = [
            _outlook_document(
                connection_id,
                value,
                fetched_at,
                fallback_id=fallback_id,
                immutable_ids=immutable_ids,
            )
        ]
        if value.get("hasAttachments") is not True:
            return documents
        try:
            page = self._call(
                "gsv_outlook_mail_read",
                connection_id,
                "attachments.list",
                {"message_id": message_id},
            )
            attachments = _outlook_attachments(page.payload)
        except Exception as exc:
            documents.append(
                _attachment_gap_document(
                    connection_id=connection_id,
                    provider="outlook_mail",
                    object_id=f"outlook-attachments:{message_id}",
                    revision=revision,
                    fetched_at=fetched_at,
                    source_ref=f"outlook:attachments:{message_id}",
                    title="Outlook attachments",
                    metadata={"parent_message_id": message_id},
                    reason=_failure_detail(exc),
                )
            )
            return documents
        for attachment in attachments:
            documents.append(
                self._outlook_attachment_document(
                    connection_id,
                    message_id,
                    revision,
                    attachment,
                    fetched_at,
                    immutable_ids=immutable_ids,
                )
            )
        return documents

    def _outlook_attachment_document(
        self,
        connection_id: str,
        message_id: str,
        revision: str,
        attachment: _Attachment,
        fetched_at: str,
        *,
        immutable_ids: bool,
    ) -> AppCorpusDocument:
        prefix = "outlook-immutable-attachment" if immutable_ids else "outlook-attachment"
        object_id = f"{prefix}:{message_id}:{attachment.attachment_id}"
        source_ref = f"outlook:attachment:{message_id}:{attachment.attachment_id}"
        metadata = _metadata(
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            parent_message_id=message_id,
            size=attachment.size,
        )
        if attachment.size is None or attachment.size > _MAX_ATTACHMENT_BYTES:
            return _attachment_gap_document(
                connection_id=connection_id,
                provider="outlook_mail",
                object_id=object_id,
                revision=revision,
                fetched_at=fetched_at,
                source_ref=source_ref,
                title=attachment.filename,
                metadata=metadata,
                reason="attachment was not read because it exceeds the artifact read bound",
            )
        try:
            page = self._call(
                "gsv_outlook_mail_read",
                connection_id,
                "attachments.get",
                {
                    "attachment_id": attachment.attachment_id,
                    "delivery": "artifact",
                    "message_id": message_id,
                },
            )
            artifact = page.artifact or {}
            path = _optional_text(artifact.get("path"))
            if path is None:
                raise ValidationError("Outlook attachment artifact is unavailable")
            extraction = extract_text(Path(path), attachment.mime_type)
            if extraction.status == "ok":
                return _document(
                    connection_id=connection_id,
                    provider="outlook_mail",
                    object_id=object_id,
                    revision=revision,
                    fetched_at=fetched_at,
                    source_ref=source_ref,
                    title=attachment.filename,
                    text=extraction.text,
                    metadata={**metadata, "extraction_status": extraction.status},
                )
            reason = extraction.reason or "attachment text extraction was incomplete"
            return _attachment_gap_document(
                connection_id=connection_id,
                provider="outlook_mail",
                object_id=object_id,
                revision=revision,
                fetched_at=fetched_at,
                source_ref=source_ref,
                title=attachment.filename,
                metadata={
                    **metadata,
                    "extraction_omissions": ",".join(extraction.omissions[:12]),
                    "extraction_status": extraction.status,
                },
                reason=reason,
            )
        except Exception as exc:
            return _attachment_gap_document(
                connection_id=connection_id,
                provider="outlook_mail",
                object_id=object_id,
                revision=revision,
                fetched_at=fetched_at,
                source_ref=source_ref,
                title=attachment.filename,
                metadata=metadata,
                reason=_failure_detail(exc),
            )


class SlackAppCorpusAdapter(_CorpusProviderAdapter):
    """Authenticated Slack search plus channel-history reads discovered from that search."""

    provider_name = "slack"

    def _initial_state(self) -> dict[str, Any]:
        return {
            "history": {"channels": [], "index": 0},
            "provider": self.provider_name,
            "round": "running",
            "search": {"done": False, "page": 1},
            "v": _CHECKPOINT_VERSION,
        }

    def _sync_round(
        self,
        connection_id: str,
        *,
        state: dict[str, Any],
        limit: int,
        fetched_at: str,
    ) -> tuple[list[AppCorpusDocument], int, bool]:
        self._apply_channel_policy(connection_id, state)
        search = _mapping_state(state, "search")
        history = _mapping_state(state, "history")
        _record_coverage_gap(
            history,
            "Slack edits and deletions are not observable through search.messages or messages.list",
        )
        page_size = _per_source_limit(limit, 1)
        documents: list[AppCorpusDocument] = []
        scanned = 0

        if not search.get("done"):
            page_number = _positive_int(search.get("page"), default=1)
            since = _optional_text(state.get("incremental_since"))
            query_date = _slack_overlap_date(since) if since is not None else "2000-01-01"
            page = self._call(
                "gsv_slack_read",
                connection_id,
                "search.messages",
                {
                    "count": page_size,
                    "page": page_number,
                    "query": f"after:{query_date}",
                    "sort": "timestamp",
                    "sort_direction": "descending",
                },
            )
            messages = _slack_search_items(page.payload)
            scanned += len(messages)
            documents.extend(_slack_document(connection_id, item, fetched_at) for item in messages)
            channels = _string_list(history.get("channels"), maximum=_MAX_SLACK_CHANNELS)
            for message in messages:
                channel_id = _slack_channel_id(message)
                if channel_id is not None and channel_id not in channels:
                    if len(channels) >= _MAX_SLACK_CHANNELS:
                        raise ValidationError(
                            "Slack channel discovery exceeds the local sync bound"
                        )
                    channels.append(channel_id)
            history["channels"] = channels
            reported_pages = _slack_page_count(page.payload)
            pages = min(reported_pages, _MAX_SLACK_SEARCH_PAGES)
            if page_number >= pages:
                search["done"] = True
                search.pop("page", None)
                if reported_pages > pages:
                    _record_coverage_gap(
                        history,
                        "Slack search reached the local page bound; "
                        "older matching messages were not indexed",
                    )
            else:
                search["page"] = page_number + 1
        else:
            channels = _string_list(history.get("channels"), maximum=_MAX_SLACK_CHANNELS)
            index = _nonnegative_int(history.get("index"))
            if index >= len(channels):
                history["done"] = True
            else:
                channel_id = channels[index]
                page = self._call(
                    "gsv_slack_read",
                    connection_id,
                    "messages.list",
                    {"channel": channel_id, "limit": page_size},
                    continuation=history.get("continuation"),
                )
                messages = _items(page.payload, "messages")
                for message in messages:
                    reported_channel_id = _slack_channel_id(message)
                    if reported_channel_id is not None and reported_channel_id != channel_id:
                        raise ValidationError(
                            "Slack history response escaped its requested channel"
                        )
                scanned += len(messages)
                documents.extend(
                    _slack_document(
                        connection_id,
                        {**item, "channel_id": channel_id},
                        fetched_at,
                    )
                    for item in messages
                )
                if page.continuation is None:
                    history.pop("continuation", None)
                    history["index"] = index + 1
                    if index + 1 >= len(channels):
                        history["done"] = True
                else:
                    history["continuation"] = page.continuation

        return documents, scanned, bool(search.get("done")) and bool(history.get("done"))

    def _apply_channel_policy(self, connection_id: str, state: dict[str, Any]) -> None:
        """Replace any broad Slack checkpoint with the current fixed channel list."""

        policy = load_slack_channel_access(connection_id)
        if policy is None or state.get("channel_policy") == policy.fingerprint:
            return
        state.clear()
        state.update(self._initial_state())
        state["channel_policy"] = policy.fingerprint
        state["history"] = {"channels": sorted(policy.channels), "index": 0}
        state["search"] = {"done": True}


def default_app_corpus_adapters(runtime: ConnectorRuntime) -> dict[str, _CorpusProviderAdapter]:
    """Return source-ID adapters for the existing account-aware connector runtime."""

    gmail = GoogleAppCorpusAdapter(runtime, sources=frozenset({"gmail"}))
    google_calendar = GoogleAppCorpusAdapter(runtime, sources=frozenset({"google_calendar"}))
    google_drive = GoogleAppCorpusAdapter(runtime, sources=frozenset({"google_drive"}))
    outlook_mail = MicrosoftAppCorpusAdapter(runtime, sources=frozenset({"outlook_mail"}))
    outlook_calendar = MicrosoftAppCorpusAdapter(runtime, sources=frozenset({"outlook_calendar"}))
    slack = SlackAppCorpusAdapter(runtime)
    return {
        "gmail": gmail,
        "google": gmail,
        "google_calendar": google_calendar,
        "google_drive": google_drive,
        "microsoft": outlook_mail,
        "outlook": outlook_mail,
        "outlook_calendar": outlook_calendar,
        "outlook_mail": outlook_mail,
        "slack": slack,
    }


def _gmail_document(
    connection_id: str,
    value: Mapping[str, Any],
    fetched_at: str,
    *,
    fallback_id: str,
) -> AppCorpusDocument:
    message_id = _optional_text(value.get("id")) or fallback_id
    payload = value.get("payload")
    body, attachment_names = _gmail_body(payload)
    subject = _gmail_header(payload, "subject")
    sender = _gmail_header(payload, "from")
    if attachment_names:
        body = _bounded_text(body + "\n\nAttachments: " + ", ".join(attachment_names))
    return _document(
        connection_id=connection_id,
        provider="gmail",
        object_id=f"gmail:{message_id}",
        revision=_revision(value, "historyId", "internalDate", fallback=message_id),
        fetched_at=fetched_at,
        source_ref=f"gmail:message:{message_id}",
        title=_title(subject, "Gmail message"),
        text=body,
        metadata=_metadata(
            attachment_count=len(attachment_names),
            labels=_joined_strings(value.get("labelIds")),
            received_at=_gmail_internal_date(value.get("internalDate")),
            sender=sender,
            thread_id=_optional_text(value.get("threadId")),
        ),
    )


def _gmail_raw_attachment_document(
    connection_id: str,
    message_id: str,
    revision: str,
    attachment: _RawGmailAttachment,
    fetched_at: str,
    *,
    received_at: str | None,
) -> AppCorpusDocument:
    attachment_id = f"raw-{attachment.part_index}"
    source_ref = f"gmail:attachment:{message_id}:{attachment_id}"
    base_metadata = _metadata(
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        parent_message_id=message_id,
        received_at=received_at,
        size=len(attachment.content),
    )
    common = {
        "connection_id": connection_id,
        "provider": "gmail",
        "object_id": f"gmail-attachment:{message_id}:{attachment_id}",
        "revision": revision,
        "fetched_at": fetched_at,
        "source_ref": source_ref,
        "title": attachment.filename,
    }
    if len(attachment.content) > _INLINE_ATTACHMENT_BYTES:
        return _attachment_gap_document(
            **common,
            metadata=base_metadata,
            reason="attachment was not read because it exceeds the inline read bound",
        )
    if attachment.mime_type not in _artifact_extractable_mimes():
        return _attachment_gap_document(
            **common,
            metadata=base_metadata,
            reason="attachment type needs a local artifact extraction route",
        )
    try:
        if attachment.mime_type in _inline_text_mimes():
            text = _bounded_text(_decode_mime_text(attachment.content, charset=None))
            if attachment.mime_type == "text/html":
                text = _html_to_text(text)
            return _document(**common, text=text, metadata=base_metadata)
        extraction = _extract_inline_attachment(
            attachment.content,
            _Attachment(
                attachment_id=attachment_id,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size=len(attachment.content),
            ),
        )
        if extraction.status == "ok":
            return _document(
                **common,
                text=extraction.text,
                metadata={**base_metadata, "extraction_status": extraction.status},
            )
        return _attachment_gap_document(
            **common,
            metadata={
                **base_metadata,
                "extraction_omissions": ",".join(extraction.omissions[:12]),
                "extraction_status": extraction.status,
            },
            reason=extraction.reason or "attachment text extraction was incomplete",
        )
    except Exception as exc:
        return _attachment_gap_document(
            **common,
            metadata=base_metadata,
            reason=_failure_detail(exc),
        )


def _gmail_deleted_document(
    connection_id: str, message_id: str, revision: str, fetched_at: str
) -> AppCorpusDocument:
    return _document(
        connection_id=connection_id,
        provider="gmail",
        object_id=f"gmail:{message_id}",
        revision=revision,
        fetched_at=fetched_at,
        source_ref=f"gmail:message:{message_id}",
        title="Deleted Gmail message",
        text="",
        metadata={"history_id": revision},
        deleted=True,
    )


def _outlook_document(
    connection_id: str,
    value: Mapping[str, Any],
    fetched_at: str,
    *,
    fallback_id: str,
    immutable_ids: bool = False,
) -> AppCorpusDocument:
    message_id = _optional_text(value.get("id")) or fallback_id
    body = _mapping(value.get("body"))
    content = _optional_text(body.get("content")) or _optional_text(value.get("bodyPreview")) or ""
    content_type = (_optional_text(body.get("contentType")) or "").casefold()
    text = _html_to_text(content) if content_type == "html" else _bounded_text(content)
    return _document(
        connection_id=connection_id,
        provider="outlook_mail",
        object_id=(f"outlook-immutable:{message_id}" if immutable_ids else f"outlook:{message_id}"),
        revision=_revision(value, "changeKey", "lastModifiedDateTime", fallback=message_id),
        fetched_at=fetched_at,
        source_ref=f"outlook:message:{message_id}",
        title=_title(value.get("subject"), "Outlook message"),
        text=text,
        metadata=_metadata(
            attachment_count=1 if value.get("hasAttachments") is True else 0,
            conversation_id=_optional_text(value.get("conversationId")),
            received_at=_optional_text(value.get("receivedDateTime")),
            sender=_graph_address(value.get("from")),
            web_link=_optional_text(value.get("webLink")),
            identity_format="immutable" if immutable_ids else None,
        ),
    )


def _outlook_delta_deleted_document(
    connection_id: str,
    message_id: str,
    revision: str,
    fetched_at: str,
) -> AppCorpusDocument:
    """Delete only the typed immutable record; prior mutable-ID records stay unverified."""

    return _document(
        connection_id=connection_id,
        provider="outlook_mail",
        object_id=f"outlook-immutable:{message_id}",
        revision=revision,
        fetched_at=fetched_at,
        source_ref=f"outlook:message:{message_id}",
        title="Deleted Outlook message",
        text="",
        metadata={"identity_format": "immutable"},
        deleted=True,
    )


def _google_event_document(
    connection_id: str, calendar_id: str, value: Mapping[str, Any], fetched_at: str
) -> AppCorpusDocument:
    event_id = _required_identifier(value, "id", "Google Calendar event")
    deleted = value.get("status") == "cancelled"
    text = _event_text(
        _optional_text(value.get("description")),
        _optional_text(value.get("location")),
        _calendar_time(value.get("start")),
        _calendar_time(value.get("end")),
    )
    return _document(
        connection_id=connection_id,
        provider="google_calendar",
        object_id=f"google-calendar:{calendar_id}:{event_id}",
        revision=_revision(value, "etag", "updated", fallback=event_id),
        fetched_at=fetched_at,
        source_ref=f"google-calendar:event:{calendar_id}:{event_id}",
        title=_title(value.get("summary"), "Google Calendar event"),
        text=text,
        metadata=_metadata(
            calendar_id=calendar_id,
            start_at=_calendar_time(value.get("start")),
            status=_optional_text(value.get("status")),
            updated_at=_optional_text(value.get("updated")),
        ),
        deleted=deleted,
    )


def _outlook_event_document(
    connection_id: str, calendar_id: str, value: Mapping[str, Any], fetched_at: str
) -> AppCorpusDocument:
    event_id = _required_identifier(value, "id", "Outlook calendar event")
    body = _mapping(value.get("body"))
    content = _optional_text(body.get("content")) or ""
    body_type = (_optional_text(body.get("contentType")) or "").casefold()
    text = _event_text(
        _html_to_text(content) if body_type == "html" else content,
        _optional_text(value.get("location", {}).get("displayName"))
        if isinstance(value.get("location"), Mapping)
        else None,
        _calendar_time(value.get("start")),
        _calendar_time(value.get("end")),
    )
    return _document(
        connection_id=connection_id,
        provider="outlook_calendar",
        object_id=f"outlook-calendar:{calendar_id}:{event_id}",
        revision=_revision(value, "changeKey", "lastModifiedDateTime", fallback=event_id),
        fetched_at=fetched_at,
        source_ref=f"outlook-calendar:event:{calendar_id}:{event_id}",
        title=_title(value.get("subject"), "Outlook calendar event"),
        text=text,
        metadata=_metadata(
            calendar_id=calendar_id,
            cancelled=value.get("isCancelled") is True,
            start_at=_calendar_time(value.get("start")),
            updated_at=_optional_text(value.get("lastModifiedDateTime")),
        ),
        deleted=value.get("isCancelled") is True,
    )


def _outlook_calendar_delta_deleted_document(
    connection_id: str,
    calendar_id: str,
    event_id: str,
    revision: str,
    fetched_at: str,
) -> AppCorpusDocument:
    return _document(
        connection_id=connection_id,
        provider="outlook_calendar",
        object_id=f"outlook-calendar:{calendar_id}:{event_id}",
        revision=revision,
        fetched_at=fetched_at,
        source_ref=f"outlook-calendar:event:{calendar_id}:{event_id}",
        title="Outlook calendar event",
        text="",
        metadata=_metadata(calendar_id=calendar_id, permanent_deletion=True),
        deleted=True,
    )


def _slack_document(
    connection_id: str, value: Mapping[str, Any], fetched_at: str
) -> AppCorpusDocument:
    timestamp = _required_identifier(value, "ts", "Slack message")
    channel_id = _slack_channel_id(value) or "unknown"
    text = _bounded_text(_optional_text(value.get("text")) or "")
    edited = _mapping(value.get("edited"))
    revision = _optional_text(edited.get("ts")) or timestamp
    return _document(
        connection_id=connection_id,
        provider="slack",
        object_id=f"slack:{channel_id}:{timestamp}",
        revision=revision,
        fetched_at=fetched_at,
        source_ref=f"slack:message:{channel_id}:{timestamp}",
        title=_title(text.split("\n", 1)[0], "Slack message"),
        text=text,
        metadata=_metadata(
            channel_id=channel_id,
            sent_at=_slack_timestamp(timestamp),
            subtype=_optional_text(value.get("subtype")),
            thread_ts=_optional_text(value.get("thread_ts")),
            user_id=_optional_text(value.get("user")),
        ),
    )


def _document(
    *,
    connection_id: str,
    provider: str,
    object_id: str,
    revision: str,
    fetched_at: str,
    source_ref: str,
    title: str,
    text: str,
    metadata: Mapping[str, Any],
    deleted: bool = False,
) -> AppCorpusDocument:
    return AppCorpusDocument(
        connection_id=connection_id,
        provider=provider,
        object_id=object_id,
        revision=revision,
        fetched_at=fetched_at,
        source_ref=source_ref,
        title=title,
        text=_bounded_text(text),
        metadata=dict(metadata),
        freshness=_freshness("complete", "provider object was read"),
        deleted=deleted,
    )


def _gmail_body(value: object) -> tuple[str, list[str]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []

    def visit(part: object) -> None:
        mapping = _mapping(part)
        mime_type = (_optional_text(mapping.get("mimeType")) or "").casefold()
        body = _mapping(mapping.get("body"))
        data = _optional_text(body.get("data"))
        filename = _optional_text(mapping.get("filename"))
        if filename is not None and _optional_text(body.get("attachmentId")) is not None:
            attachments.append(filename)
        if data is not None:
            decoded = _decode_base64_text(data)
            if mime_type == "text/plain":
                plain.append(decoded)
            elif mime_type == "text/html":
                html.append(decoded)
        for child in _items(mapping, "parts"):
            visit(child)

    visit(value)
    body = "\n\n".join(plain) if plain else "\n\n".join(_html_to_text(item) for item in html)
    return _bounded_text(body), _unique_limited(attachments, maximum=24)


def _parse_gmail_raw_message(path: Path) -> _RawGmailMessage:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_GMAIL_RAW_MESSAGE_BYTES:
            raise ValidationError("Gmail raw message exceeds the corpus artifact read bound")
        with path.open("rb") as stream:
            message = BytesParser(policy=policy.default).parse(stream)
    except OSError as exc:
        raise ValidationError("Gmail raw message artifact cannot be read") from exc
    plain: list[str] = []
    html: list[str] = []
    attachments: list[_RawGmailAttachment] = []
    attachment_count = 0
    for part_index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        mime_type = part.get_content_type().casefold()
        filename = _mime_filename(part)
        if filename is not None:
            attachment_count += 1
            if len(attachments) < 24:
                attachments.append(
                    _RawGmailAttachment(
                        content=_mime_part_bytes(part),
                        filename=filename,
                        mime_type=mime_type,
                        part_index=part_index,
                    )
                )
            continue
        if mime_type == "text/plain":
            plain.append(
                _decode_mime_text(
                    _mime_part_bytes(part), charset=part.get_content_charset(), bounded=False
                )
            )
        elif mime_type == "text/html":
            html.append(
                _decode_mime_text(
                    _mime_part_bytes(part), charset=part.get_content_charset(), bounded=False
                )
            )
    visible_body = (
        "\n\n".join(plain) if plain else "\n\n".join(_visible_html_to_text(item) for item in html)
    )
    body, body_truncated = _bounded_document_text(visible_body)
    return _RawGmailMessage(
        attachment_count=attachment_count,
        attachments=tuple(attachments),
        attachments_truncated=attachment_count > len(attachments),
        body=body,
        body_truncated=body_truncated,
        sender=_mime_header(message, "from"),
        subject=_mime_header(message, "subject"),
    )


def _mime_filename(part: Message) -> str | None:
    value = part.get_filename()
    if not isinstance(value, str):
        return None
    clean = _bounded_text(value)
    return clean or None


def _mime_part_bytes(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Gmail raw message part cannot be decoded") from exc
    if payload is None:
        return b""
    if not isinstance(payload, bytes):
        raise ValidationError("Gmail raw message part is invalid")
    return payload


def _decode_mime_text(content: bytes, *, charset: str | None, bounded: bool = True) -> str:
    try:
        decoded = content.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeError):
        decoded = content.decode("utf-8", errors="replace")
    return _bounded_text(decoded) if bounded else decoded


def _mime_header(message: Message, name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    clean = _bounded_text(str(value))
    return clean or None


def _gmail_raw_extraction_omissions(message: _RawGmailMessage) -> tuple[str, ...]:
    omissions: list[str] = []
    if message.body_truncated:
        omissions.append("visible message text was truncated at the corpus text bound")
    if message.attachments_truncated:
        omissions.append("only the first 24 named attachments were materialized")
    return tuple(omissions)


def _gmail_attachments(value: object) -> list[_Attachment]:
    result: list[_Attachment] = []

    def visit(part: object) -> None:
        mapping = _mapping(part)
        body = _mapping(mapping.get("body"))
        attachment_id = _optional_text(body.get("attachmentId"))
        filename = _optional_text(mapping.get("filename"))
        if attachment_id is not None and filename is not None:
            raw_size = body.get("size")
            size = raw_size if type(raw_size) is int and raw_size >= 0 else None
            result.append(
                _Attachment(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime_type=_optional_text(mapping.get("mimeType")),
                    size=size,
                )
            )
        for child in _items(mapping, "parts"):
            visit(child)

    visit(value)
    return result[:24]


def _outlook_attachments(value: Mapping[str, Any]) -> list[_Attachment]:
    result: list[_Attachment] = []
    for item in _items(value, "value")[:24]:
        attachment_id = _required_identifier(item, "id", "Outlook attachment")
        filename = _title(item.get("name"), "Outlook attachment")
        raw_size = item.get("size")
        size = raw_size if type(raw_size) is int and raw_size >= 0 else None
        result.append(
            _Attachment(
                attachment_id=attachment_id,
                filename=filename,
                mime_type=_optional_text(item.get("contentType")),
                size=size,
            )
        )
    return result


def _inline_text_mimes() -> frozenset[str]:
    return frozenset(
        {
            "application/json",
            "application/xml",
            "application/yaml",
            "text/calendar",
            "text/csv",
            "text/html",
            "text/markdown",
            "text/plain",
            "text/tab-separated-values",
            "text/xml",
        }
    )


def _artifact_extractable_mimes() -> frozenset[str]:
    return _inline_text_mimes() | frozenset({_DOCX_MIME, _PDF_MIME})


def _extract_inline_attachment(raw: bytes, attachment: _Attachment) -> ExtractionResult:
    mime_type = attachment.mime_type
    if mime_type not in {_DOCX_MIME, _PDF_MIME}:
        raise ValidationError("attachment type needs a local artifact extraction route")
    suffix = ".docx" if mime_type == _DOCX_MIME else ".pdf"
    path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb", prefix="seld-app-corpus-", suffix=suffix, delete=False
        ) as stream:
            path = Path(stream.name)
            stream.write(raw)
        return extract_text(path, mime_type)
    finally:
        if path is not None:
            with suppress(OSError):
                path.unlink(missing_ok=True)


def _drive_metadata(file: Mapping[str, Any], *, mime_type: str | None) -> dict[str, object]:
    return _metadata(
        description=_optional_text(file.get("description")),
        mime_type=mime_type,
        modified_at=_optional_text(file.get("modifiedTime")),
        size=_provider_size(file.get("size")),
        web_link=_optional_text(file.get("webViewLink")),
    )


def _drive_gap_document(
    connection_id: str,
    file: Mapping[str, Any],
    fetched_at: str,
    *,
    reason: str,
) -> AppCorpusDocument:
    file_id = _required_identifier(file, "id", "Google Drive file")
    return _provider_gap_document(
        connection_id=connection_id,
        provider="google_drive",
        object_id=f"drive:{file_id}",
        revision=_revision(file, "version", "modifiedTime", fallback=file_id),
        fetched_at=fetched_at,
        source_ref=f"google-drive:file:{file_id}",
        title=_title(file.get("name"), "Google Drive document"),
        metadata=_drive_metadata(file, mime_type=_optional_text(file.get("mimeType"))),
        reason=reason,
    )


def _drive_removed_document(
    connection_id: str,
    file_id: str,
    change: Mapping[str, Any],
    fetched_at: str,
) -> AppCorpusDocument:
    return _document(
        connection_id=connection_id,
        provider="google_drive",
        object_id=f"drive:{file_id}",
        revision=_revision(change, "time", fallback=file_id),
        fetched_at=fetched_at,
        source_ref=f"google-drive:file:{file_id}",
        title="Unavailable Google Drive file",
        text="",
        metadata=_metadata(change_time=_optional_text(change.get("time"))),
        deleted=True,
    )


def _attachment_gap_document(
    *,
    connection_id: str,
    provider: str,
    object_id: str,
    revision: str,
    fetched_at: str,
    source_ref: str,
    title: str,
    metadata: Mapping[str, Any],
    reason: str,
) -> AppCorpusDocument:
    return _provider_gap_document(
        connection_id=connection_id,
        provider=provider,
        object_id=object_id,
        revision=revision,
        fetched_at=fetched_at,
        source_ref=source_ref,
        title=title,
        metadata=metadata,
        reason=reason,
    )


def _provider_gap_document(
    *,
    connection_id: str,
    provider: str,
    object_id: str,
    revision: str,
    fetched_at: str,
    source_ref: str,
    title: str,
    metadata: Mapping[str, Any],
    reason: str,
) -> AppCorpusDocument:
    return AppCorpusDocument(
        connection_id=connection_id,
        provider=provider,
        object_id=object_id,
        revision=revision,
        fetched_at=fetched_at,
        source_ref=source_ref,
        title=_title(title, "Provider object"),
        text=f"Provider text is unavailable: {reason}",
        metadata={**metadata, "extraction_status": "gap"},
        freshness=_freshness("partial", reason),
    )


def _gmail_header(payload: object, name: str) -> str | None:
    for header in _items(_mapping(payload), "headers"):
        header_name = _optional_text(header.get("name"))
        if header_name is not None and header_name.casefold() == name:
            return _optional_text(header.get("value"))
    return None


def _items(value: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = value.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("connector collection response is invalid")
    result: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValidationError("connector collection item is invalid")
        result.append({str(name): value for name, value in item.items() if isinstance(name, str)})
    return result


def _slack_search_items(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    messages = _mapping(value.get("messages"))
    return _items(messages, "matches")


def _slack_page_count(value: Mapping[str, Any]) -> int:
    paging = _mapping(value.get("messages")).get("paging")
    if not isinstance(paging, Mapping):
        return 1
    pages = paging.get("pages")
    if type(pages) is not int or pages < 1:
        return 1
    return pages


def _slack_channel_id(value: Mapping[str, Any]) -> str | None:
    direct = _optional_text(value.get("channel_id"))
    if direct is not None:
        return direct
    channel = value.get("channel")
    return _optional_text(_mapping(channel).get("id"))


def _finish_page(state: dict[str, Any], continuation: object | None) -> None:
    if continuation is None:
        state["done"] = True
        state.pop("continuation", None)
    else:
        state["continuation"] = continuation


def _google_sync_complete(continuation: object | None) -> bool:
    return continuation is None or (
        isinstance(continuation, Mapping)
        and set(continuation) == {"syncToken"}
        and isinstance(continuation.get("syncToken"), str)
        and bool(continuation["syncToken"])
    )


def _record_omission(state: dict[str, Any]) -> None:
    state["omissions"] = _nonnegative_int(state.get("omissions")) + 1


def _record_coverage_gap(state: dict[str, Any], detail: str) -> None:
    existing = state.get("coverage_gaps")
    gaps = (
        []
        if not isinstance(existing, list)
        else [item for item in existing if isinstance(item, str)]
    )
    if detail not in gaps and len(gaps) < 8:
        gaps.append(detail)
    state["coverage_gaps"] = gaps


def _clear_coverage_gaps(state: dict[str, Any], *, prefix: str) -> None:
    existing = state.get("coverage_gaps")
    if isinstance(existing, list):
        state["coverage_gaps"] = [
            item for item in existing if isinstance(item, str) and not item.startswith(prefix)
        ]


def _coverage_gaps(state: Mapping[str, Any]) -> tuple[str, ...]:
    gaps: list[str] = []
    for value in state.values():
        if not isinstance(value, Mapping):
            continue
        entries = value.get("coverage_gaps")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str) and entry and entry not in gaps:
                gaps.append(entry)
    return tuple(gaps)


def _omission_count(state: Mapping[str, Any]) -> int:
    total = 0
    for value in state.values():
        if isinstance(value, Mapping):
            total += _nonnegative_int(value.get("omissions"))
    return total


def _completion_detail(omissions: int, coverage_gaps: tuple[str, ...]) -> str:
    detail = "bounded provider scan reached its current end"
    if omissions:
        detail += f" with {omissions} unavailable objects"
    return _with_coverage_gaps(detail, coverage_gaps)


def _partial_detail(omissions: int, coverage_gaps: tuple[str, ...]) -> str:
    detail = "more provider content remains within the saved checkpoint"
    if omissions:
        detail = f"more provider content remains; {omissions} objects were unavailable"
    return _with_coverage_gaps(detail, coverage_gaps)


def _with_coverage_gaps(detail: str, coverage_gaps: tuple[str, ...]) -> str:
    if not coverage_gaps:
        return detail
    return detail + "; coverage gaps: " + "; ".join(coverage_gaps)


def _mapping_state(state: dict[str, Any], key: str) -> dict[str, Any]:
    current = state.get(key)
    if not isinstance(current, dict):
        current = {}
        state[key] = current
    return current


def _decode_checkpoint(value: str, *, provider: str) -> dict[str, Any]:
    maximum = (
        _MICROSOFT_DELTA_CHECKPOINT_CHARS if provider == "microsoft" else _MAX_CHECKPOINT_CHARS
    )
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError("app corpus checkpoint is invalid")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError("app corpus checkpoint is invalid") from exc
    if (
        not isinstance(decoded, dict)
        or decoded.get("v") != _CHECKPOINT_VERSION
        or decoded.get("provider") != provider
        or decoded.get("round") not in {"running", "complete"}
    ):
        raise ValidationError("app corpus checkpoint is invalid")
    return decoded


def _encode_checkpoint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    maximum = (
        _MICROSOFT_DELTA_CHECKPOINT_CHARS
        if value.get("provider") == "microsoft"
        else _MAX_CHECKPOINT_CHARS
    )
    if len(encoded) > maximum:
        raise ValidationError("app corpus checkpoint exceeds its local bound")
    return encoded


def _clear_continuations(value: object) -> None:
    if isinstance(value, dict):
        value.pop("continuation", None)
        value.pop("changes_continuation", None)
        value.pop("list_continuation", None)
        value.pop("event_continuation", None)
        checkpoint = value.get("delta_checkpoint")
        if isinstance(checkpoint, str):
            try:
                clear = (
                    _clear_microsoft_calendar_delta_continuation
                    if "primary_calendar_id" in value
                    else _clear_microsoft_delta_continuations
                )
                value["delta_checkpoint"] = clear(checkpoint)
            except ValidationError:
                value.pop("delta_checkpoint", None)
        for child in value.values():
            _clear_continuations(child)
    elif isinstance(value, list):
        for child in value:
            _clear_continuations(child)


def _continuation_failure(error: Exception) -> bool:
    text = str(error).casefold()
    return "continuation" in text or "cursor" in text


def _gmail_history_expired(error: Exception) -> bool:
    return getattr(error, "code", None) == "full_sync_required"


def _record_gmail_detail_retry(gmail: dict[str, Any], error: Exception) -> None:
    """Retain the page cursor and one content-free reason for its retry."""

    gmail["retry_current_page"] = True
    gmail["retry_category"] = _gmail_detail_retry_category(error)


def _gmail_full_message_exceeds_json_bound(error: ValidationError) -> bool:
    """Use raw MIME only for the connector's exact bounded-result failure."""

    return str(error) == "JSON string is invalid or too large"


def _gmail_detail_retry_reason(gmail: dict[str, Any]) -> str:
    return _optional_text(gmail.pop("retry_category", None)) or "a provider read failure"


def _gmail_detail_retry_category(value: object) -> str:
    if isinstance(value, ConnectorProviderError):
        return f"provider HTTP {value.status}"
    if isinstance(value, ValidationError):
        return "invalid provider result"
    if isinstance(value, ContinuityError):
        return "connector failure"
    return "unexpected local failure"


def _refused(error: Exception) -> bool:
    if not isinstance(error, (ContinuityError, ValidationError)):
        return False
    text = str(error).casefold()
    return any(
        word in text for word in ("authorize", "credential", "scope", "verified", "connection")
    )


def _failure_detail(error: Exception) -> str:
    if _continuation_failure(error):
        return "saved provider continuation was invalid; the bounded scan will restart"
    if _refused(error):
        return "connector read was refused; connection health or granted scopes need repair"
    return "connector provider read failed; the saved checkpoint remains retryable"


def _freshness(status: str, detail: str) -> dict[str, str]:
    return {"detail": detail, "status": status}


def _metadata(**values: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, str):
            result[key] = value[:_MAX_METADATA_TEXT_CHARS]
        elif type(value) in {bool, int, float}:
            result[key] = value
    return result


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean if clean else None


def _provider_size(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _gmail_history_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.isdecimal() or len(value) > 20:
        return None
    return value


def _drive_change_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean if clean and len(clean) <= 4_096 else None


def _drive_needs_change_bootstrap(drive: Mapping[str, Any]) -> bool:
    return drive.get("legacy_checkpoint") is True or (
        drive.get("change_anchor_captured") is not True
        and (
            drive.get("continuation") is not None
            or drive.get("done") is True
            or _drive_change_id(drive.get("change_id")) is not None
        )
    )


def _gmail_internal_date(value: object) -> str | None:
    if type(value) is int:
        milliseconds = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 16:
        milliseconds = int(value)
    else:
        return None
    if milliseconds < 0:
        return None
    try:
        stamp = datetime.fromtimestamp(milliseconds // 1_000, UTC).replace(
            microsecond=(milliseconds % 1_000) * 1_000
        )
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _slack_timestamp(value: object) -> str | None:
    raw = _optional_text(value)
    if raw is None:
        return None
    seconds_text, separator, fraction = raw.partition(".")
    if (
        not seconds_text.isascii()
        or not seconds_text.isdecimal()
        or len(seconds_text) > 14
        or (separator and (not fraction.isascii() or not fraction.isdecimal() or len(fraction) > 9))
    ):
        return None
    try:
        stamp = datetime.fromtimestamp(int(seconds_text), UTC)
    except (OverflowError, OSError, ValueError):
        return None
    if separator:
        stamp = stamp.replace(microsecond=int(fraction[:6].ljust(6, "0")))
        return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _gmail_needs_backfill_restart(gmail: Mapping[str, Any]) -> bool:
    """Recognize an interrupted pre-anchor Gmail scan from checkpoint version one."""
    return "history_anchor_captured" not in gmail and (
        gmail.get("continuation") is not None
        or gmail.get("done") is True
        or _gmail_history_id(gmail.get("history_id")) is not None
    )


def _required_identifier(value: Mapping[str, Any], key: str, label: str) -> str:
    identifier = _optional_text(value.get(key))
    if identifier is None:
        raise ValidationError(f"{label} has no identifier")
    return identifier


def _revision(value: Mapping[str, Any], *keys: str, fallback: str) -> str:
    for key in keys:
        item = _optional_text(value.get(key))
        if item is not None:
            return item
    return fallback


def _title(value: object, fallback: str) -> str:
    title = _bounded_text(value if isinstance(value, str) else "")
    return title.replace("\n", " ")[:8_192] or fallback


def _bounded_text(value: str) -> str:
    clean = value.replace("\x00", "").strip()
    return clean[:_MAX_DOCUMENT_TEXT_CHARS]


def _html_to_text(value: str) -> str:
    parser = _PlainText()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return _bounded_text(value)
    return parser.text()


def _visible_html_to_text(value: str) -> str:
    parser = _PlainText()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return value
    return parser.text(bounded=False)


def _bounded_document_text(value: str) -> tuple[str, bool]:
    clean = value.replace("\x00", "").strip()
    return clean[:_MAX_DOCUMENT_TEXT_CHARS], len(clean) > _MAX_DOCUMENT_TEXT_CHARS


def _decode_base64_text(value: str) -> str:
    return _bounded_text(_decode_base64_bytes(value).decode("utf-8", errors="replace"))


def _decode_base64_bytes(value: str, *, maximum: int | None = None) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValidationError("provider text content is not valid base64") from exc
    if maximum is not None and len(decoded) > maximum:
        raise ValidationError("provider content exceeds its read bound")
    return decoded


def _event_text(
    description: str | None, location: str | None, start: str | None, end: str | None
) -> str:
    lines = []
    if description:
        lines.append(description)
    if location:
        lines.append(f"Location: {location}")
    if start:
        lines.append(f"Start: {start}")
    if end:
        lines.append(f"End: {end}")
    return _bounded_text("\n".join(lines))


def _calendar_time(value: object) -> str | None:
    mapping = _mapping(value)
    return (
        _optional_text(mapping.get("dateTime"))
        or _optional_text(mapping.get("date_time"))
        or _optional_text(mapping.get("date"))
    )


def _graph_address(value: object) -> str | None:
    email_address = _mapping(_mapping(value).get("emailAddress"))
    return _optional_text(email_address.get("address"))


def _joined_strings(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    strings = [item for item in value if isinstance(item, str) and item]
    return ", ".join(strings[:24]) or None


def _unique_limited(values: list[str], *, maximum: int) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
        if len(result) >= maximum:
            break
    return result


def _string_list(value: object, *, maximum: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise ValidationError("app corpus checkpoint collection is invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 1_024:
            raise ValidationError("app corpus checkpoint collection is invalid")
        result.append(item)
    return result


def _nonnegative_int(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    return 0


def _positive_int(value: object, *, default: int) -> int:
    return value if type(value) is int and value > 0 else default


def _per_source_limit(limit: int, source_count: int) -> int:
    return max(1, min(_MAX_PAGE_ITEMS, limit // source_count or 1))


def _overlap_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return (parsed.astimezone(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")


def _gmail_overlap_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return "2000/01/01"
    return (parsed - timedelta(days=2)).strftime("%Y/%m/%d")


def _slack_overlap_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return "2000-01-01"
    return (parsed - timedelta(days=2)).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
