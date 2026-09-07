from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from continuity_kernel.app_corpus import AppCorpusCompanion, AppCorpusDocument, AppCorpusSyncResult
from continuity_kernel.app_corpus_microsoft_delta import (
    MAX_FOLDERS,
    MicrosoftMailDeltaSync,
    clear_continuations,
    rewind_message_page_for_materialization_retry,
)
from continuity_kernel.vault import Vault


@dataclass
class _Runtime:
    responses: list[dict[str, object]]
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
        return self.responses.pop(0)


def _response(result: object, *, continuation: object | None = None) -> dict[str, object]:
    value: dict[str, object] = {"result": result, "status": "ok"}
    if continuation is not None:
        value["continuation"] = continuation
    return value


def test_delta_sync_resumes_all_folders_and_releases_removals_only_at_round_end() -> None:
    runtime = _Runtime(
        [
            _response({"value": [{"id": "folder-a"}]}, continuation={"path": "folders-p2"}),
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": "folder-b"}],
                }
            ),
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-a/messages/delta?$deltatoken=a",
                    "value": [{"id": "immutable-a"}],
                }
            ),
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-b/messages/delta?$deltatoken=b",
                    "value": [{"@removed": {"reason": "deleted"}, "id": "immutable-b"}],
                }
            ),
        ]
    )
    sync = MicrosoftMailDeltaSync(runtime)  # type: ignore[arg-type]

    folders_first = sync.sync("outlook", limit=50)
    folders_last = sync.sync("outlook", checkpoint=folders_first.checkpoint, limit=50)
    first_messages = sync.sync("outlook", checkpoint=folders_last.checkpoint, limit=50)
    complete = sync.sync("outlook", checkpoint=first_messages.checkpoint, limit=50)

    assert folders_first.complete is False
    assert folders_last.complete is False
    assert first_messages.changes[0].message_id == "immutable-a"
    assert first_messages.removals == ()
    assert complete.complete is True
    assert complete.removals[0].message_id == "immutable-b"
    assert runtime.calls == [
        (
            "gsv_outlook_mail_read",
            {"connection_id": "outlook", "input": {"page_size": 50}, "operation": "folders.delta"},
        ),
        (
            "gsv_outlook_mail_read",
            {"connection_id": "outlook", "input": {"page_size": 50}, "operation": "folders.delta"},
        ),
        (
            "gsv_outlook_mail_read",
            {
                "connection_id": "outlook",
                "input": {"folder_id": "folder-a", "page_size": 50},
                "operation": "messages.delta",
            },
        ),
        (
            "gsv_outlook_mail_read",
            {
                "connection_id": "outlook",
                "input": {"folder_id": "folder-b", "page_size": 50},
                "operation": "messages.delta",
            },
        ),
    ]
    assert runtime.continuations == [None, {"path": "folders-p2"}, None, None]


def test_completed_checkpoint_reuses_opaque_delta_links_in_a_fresh_runtime() -> None:
    initial = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": "folder-a"}],
                }
            ),
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-a/messages/delta?$deltatoken=a",
                    "value": [],
                }
            ),
        ]
    )
    first = MicrosoftMailDeltaSync(initial).sync("outlook", limit=10)  # type: ignore[arg-type]
    complete = MicrosoftMailDeltaSync(initial).sync(  # type: ignore[arg-type]
        "outlook", checkpoint=first.checkpoint, limit=10
    )

    resumed_runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders-2",
                    "value": [],
                }
            ),
        ]
    )
    resumed = MicrosoftMailDeltaSync(resumed_runtime).sync(  # type: ignore[arg-type]
        "outlook", checkpoint=complete.checkpoint, limit=10
    )

    assert resumed.complete is False
    assert resumed_runtime.calls[0][1]["input"] == {
        "delta_link": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
        "page_size": 10,
    }


def test_continuation_reset_preserves_completed_delta_links() -> None:
    checkpoint = json.dumps(
        {
            "candidates": {},
            "coverage_gaps": [],
            "folder_continuation": {"path": "stale-folder-page"},
            "folder_delta_link": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
            "folder_index": 0,
            "folder_order": ["folder-a"],
            "folders": {
                "folder-a": {
                    "continuation": {"path": "stale-message-page"},
                    "delta_link": "https://graph.microsoft.com/v1.0/me/mailFolders/folder-a/messages/delta?$deltatoken=a",
                }
            },
            "phase": "messages",
            "v": 1,
        }
    )

    saved = json.loads(clear_continuations(checkpoint))

    assert "folder_continuation" not in saved
    assert "continuation" not in saved["folders"]["folder-a"]
    assert saved["folder_delta_link"].endswith("folders")
    assert saved["folders"]["folder-a"]["delta_link"].endswith("a")


def test_materialization_retry_rewinds_the_current_and_previous_message_folders() -> None:
    checkpoint = json.dumps(
        {
            "candidates": {"deleted": {"folder_id": "folder-b", "revision": "r1"}},
            "coverage_gaps": ["A retained coverage gap"],
            "folder_delta_link": "folders-delta",
            "folder_index": 2,
            "folder_order": ["folder-a", "folder-b", "folder-c"],
            "folders": {
                "folder-a": {"delta_link": "messages-a"},
                "folder-b": {"continuation": {"path": "page-b"}, "delta_link": "messages-b"},
                "folder-c": {"continuation": {"path": "page-c"}, "delta_link": "messages-c"},
            },
            "phase": "messages",
            "v": 1,
        }
    )

    saved = json.loads(rewind_message_page_for_materialization_retry(checkpoint))

    assert saved["phase"] == "messages"
    assert saved["folder_index"] == 1
    assert saved["folders"]["folder-a"]["delta_link"] == "messages-a"
    assert saved["folders"]["folder-b"] == {}
    assert saved["folders"]["folder-c"] == {}
    assert saved["candidates"] == {"deleted": {"folder_id": "folder-b", "revision": "r1"}}
    assert saved["coverage_gaps"] == ["A retained coverage gap"]


def test_folder_cap_is_visible_and_never_truncates_the_checkpoint() -> None:
    runtime = _Runtime(
        [
            _response(
                {
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=folders",
                    "value": [{"id": f"folder-{index}"} for index in range(MAX_FOLDERS + 1)],
                }
            )
        ]
    )

    result = MicrosoftMailDeltaSync(runtime).sync("outlook", limit=1_000)  # type: ignore[arg-type]
    saved = json.loads(result.checkpoint)

    assert len(saved["folders"]) == MAX_FOLDERS
    assert any("first 128" in detail for detail in result.coverage_gaps)


def test_immutable_outlook_tombstone_cascades_matching_attachment_documents(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corpus")
    companion = AppCorpusCompanion(
        vault.root,
        executable=tmp_path / "missing-qmd",
        index_root=tmp_path / "corpus",
    )

    class Adapter:
        deleted = False

        def sync(self, connection_id, *, checkpoint=None, limit=100):
            documents = (
                AppCorpusDocument(
                    connection_id=connection_id,
                    provider="outlook_mail",
                    object_id="outlook-immutable:immutable-parent",
                    revision="r1",
                    fetched_at="2026-09-07T08:00:00Z",
                    source_ref="outlook:message:immutable-parent",
                    title="Immutable parent",
                    text="message",
                    metadata={"identity_format": "immutable"},
                    freshness={"status": "complete"},
                    deleted=self.deleted,
                ),
            )
            if not self.deleted:
                documents += (
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="outlook_mail",
                        object_id="outlook-immutable-attachment:immutable-parent:attachment-1",
                        revision="r1",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="outlook:attachment:immutable-parent:attachment-1",
                        title="Matching attachment",
                        text="attachment",
                        metadata={"parent_message_id": "immutable-parent"},
                        freshness={"status": "complete"},
                    ),
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="outlook_mail",
                        object_id="outlook:immutable-parent",
                        revision="legacy",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="outlook:message:legacy-parent",
                        title="Legacy message",
                        text="legacy",
                        metadata={},
                        freshness={"status": "complete"},
                    ),
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="outlook_mail",
                        object_id="outlook-immutable-attachment:other-parent:attachment-1",
                        revision="r1",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="outlook:attachment:other-parent:attachment-1",
                        title="Other attachment",
                        text="other attachment",
                        metadata={"parent_message_id": "other-parent"},
                        freshness={"status": "complete"},
                    ),
                )
            return AppCorpusSyncResult(
                documents=documents,
                checkpoint="checkpoint",
                scanned=len(documents),
                complete=True,
                freshness={"status": "complete"},
            )

    adapter = Adapter()
    companion.sync(adapter, "outlook-connection")

    class OtherConnectionAdapter:
        def sync(self, connection_id, *, checkpoint=None, limit=100):
            return AppCorpusSyncResult(
                documents=(
                    AppCorpusDocument(
                        connection_id=connection_id,
                        provider="outlook_mail",
                        object_id="outlook-immutable-attachment:immutable-parent:attachment-2",
                        revision="r1",
                        fetched_at="2026-09-07T08:00:00Z",
                        source_ref="outlook:attachment:immutable-parent:attachment-2",
                        title="Other connection attachment",
                        text="other connection",
                        metadata={"parent_message_id": "immutable-parent"},
                        freshness={"status": "complete"},
                    ),
                ),
                checkpoint="checkpoint",
                scanned=1,
                complete=True,
                freshness={"status": "complete"},
            )

    companion.sync(OtherConnectionAdapter(), "other-connection")
    adapter.deleted = True

    result = companion.sync(adapter, "outlook-connection")

    assert result.document_count == 3
    assert companion.read(
        "outlook-connection", "outlook-immutable:immutable-parent"
    ) is None
    assert companion.read(
        "outlook-connection", "outlook-immutable-attachment:immutable-parent:attachment-1"
    ) is None
    assert companion.read("outlook-connection", "outlook:immutable-parent") is not None
    assert companion.read(
        "outlook-connection", "outlook-immutable-attachment:other-parent:attachment-1"
    ) is not None
    assert companion.read(
        "other-connection", "outlook-immutable-attachment:immutable-parent:attachment-2"
    ) is not None
