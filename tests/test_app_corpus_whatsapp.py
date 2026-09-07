from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from continuity_kernel import app_corpus_whatsapp
from continuity_kernel.app_corpus_whatsapp import (
    WhatsAppAppCorpusAdapter,
    whatsapp_app_capabilities,
)

ACCOUNT = "sha256:" + "a" * 64
AT = int(datetime(2026, 9, 7, 10, 0, tzinfo=UTC).timestamp())


def test_downloaded_attachment_requires_matching_bytes_and_respects_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    database = _store(root)
    attachment = tmp_path / "download.txt"
    attachment.write_text("The document contains the approved agenda.")
    _append(database, message_id="attachment", text="Agenda", timestamp=AT)
    with closing(sqlite3.connect(database)) as connection:
        for column, kind in (
            ("local_path", "TEXT"), ("file_sha256", "BLOB"),
            ("filename", "TEXT"), ("mime_type", "TEXT"),
        ):
            connection.execute(f"ALTER TABLE messages ADD COLUMN {column} {kind}")
        connection.execute(
            "UPDATE messages SET local_path=?, file_sha256=?, filename=?, mime_type=?",
            (str(attachment), hashlib.sha256(attachment.read_bytes()).digest(),
             "download.txt", "text/plain; charset=utf-8"),
        )
        connection.commit()
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=root)
    document = adapter.sync("wa-test").documents[0]
    assert "approved agenda" in document.text
    assert document.metadata["media_extraction_status"] == "local_text"
    attachment.write_text("Replacement bytes must not be indexed.")
    document = adapter.sync("wa-test").documents[0]
    assert "Replacement" not in document.text
    assert document.metadata["extraction_reason"] == "attachment digest mismatch"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE messages SET deleted_at=?", (AT + 1,))
        connection.commit()
    document = adapter.sync("wa-test").documents[0]
    assert document.deleted and not document.text


def _store(root: Path) -> Path:
    root.mkdir(parents=True)
    database = root / "wacli.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE messages ("
            "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_jid TEXT NOT NULL, chat_name TEXT, msg_id TEXT NOT NULL, "
            "ts INTEGER NOT NULL, from_me INTEGER NOT NULL, text TEXT, display_text TEXT, "
            "media_caption TEXT, media_type TEXT, edited_ts INTEGER NOT NULL DEFAULT 0, "
            "revoked INTEGER NOT NULL DEFAULT 0, deleted_for_me INTEGER NOT NULL DEFAULT 0, "
            "deleted_at INTEGER, payload_purged_at INTEGER)"
        )
        connection.commit()
    return database


def _append(
    database: Path,
    *,
    message_id: str,
    text: str,
    timestamp: int,
    display_text: str | None = None,
    media_caption: str | None = None,
    media_type: str = "document",
) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO messages (chat_jid, chat_name, msg_id, ts, from_me, text, display_text, "
            "media_caption, media_type) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (
                "15551234567@s.whatsapp.net",
                "Planning",
                message_id,
                timestamp,
                text,
                display_text,
                media_caption,
                media_type,
            ),
        )
        connection.commit()


def _voice_envelope(root: Path, *, rowid: int, timestamp: int, transcript: str) -> None:
    at = datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")
    digest = hashlib.sha256(f"{rowid}:{at}:{transcript}".encode()).hexdigest()[:32]
    path = root / "envoy" / "inbox" / "voice.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "id: wbm-test\n"
        "from: service-whatsapp-chief-relay\n"
        "to: envoy\n"
        "kind: request\n"
        f'event-key: "whatsapp-envoy-inbound-{digest}"\n'
        "expects-report-event-key: ignored\n"
        "---\n\n"
        f"{transcript}\n",
        encoding="utf-8",
    )


def _due(checkpoint: str) -> str:
    value = json.loads(checkpoint)
    value["phase"] = "steady"
    value["rescan_due_at"] = "2020-01-01T00:00:00Z"
    value["rescan_rowid"] = 0
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def test_whatsapp_paginates_and_defers_old_row_review_until_tomorrow(tmp_path: Path) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="first message", timestamp=AT)
    _append(
        database,
        message_id="message-2",
        text="body text",
        display_text="display text",
        media_caption="caption text",
        timestamp=AT + 1,
    )
    _append(database, message_id="message-3", text="third message", timestamp=AT + 2)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)

    first = adapter.sync("local-whatsapp", limit=2)

    assert first.complete is False
    assert first.scanned == 2
    assert len(first.documents) == 2
    assert "first message" not in (first.checkpoint or "")
    assert "message-1" not in (first.checkpoint or "")
    second = first.documents[1]
    assert second.text == "body text\n\ndisplay text\n\ncaption text"
    assert second.source_ref.endswith("/message-2?at=2026-09-07T10%3A00%3A01Z")
    assert second.metadata == {
        "conversation": "Planning",
        "direction": "inbound",
        "extraction_status": "partial",
        "media_extraction_status": "not_extracted",
        "media_type": "document",
        "sent_at": "2026-09-07T10:00:01Z",
    }

    second_page = adapter.sync("local-whatsapp", checkpoint=first.checkpoint, limit=2)
    assert second_page.complete is True
    assert second_page.scanned == 1
    assert [item.text for item in second_page.documents] == ["third message"]

    steady = adapter.sync("local-whatsapp", checkpoint=second_page.checkpoint, limit=2)
    assert steady.complete is True
    assert steady.scanned == 0
    assert steady.documents == ()
    assert steady.freshness["status"] == "complete"


def test_whatsapp_rolling_rescan_reissues_old_edit_and_explicit_tombstone(tmp_path: Path) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="before edit", timestamp=AT)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)

    initial = adapter.sync("local-whatsapp", limit=10)
    review = adapter.sync("local-whatsapp", checkpoint=initial.checkpoint, limit=10)
    assert initial.complete is True
    assert review.complete is True
    assert review.scanned == 0

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE messages SET text = ?, edited_ts = ? WHERE msg_id = ?",
            ("after edit", AT + 10, "message-1"),
        )
        connection.commit()

    edited = adapter.sync("local-whatsapp", checkpoint=_due(review.checkpoint or ""), limit=10)
    assert edited.complete is True
    assert [item.text for item in edited.documents] == ["after edit"]
    assert edited.documents[0].revision == "whatsapp-message:2026-09-07T10:00:10Z"

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE messages SET deleted_for_me = 1, deleted_at = ? WHERE msg_id = ?",
            (AT + 20, "message-1"),
        )
        connection.commit()

    tombstone = adapter.sync("local-whatsapp", checkpoint=_due(edited.checkpoint or ""), limit=10)
    assert tombstone.complete is True
    assert len(tombstone.documents) == 1
    assert tombstone.documents[0].deleted is True
    assert tombstone.documents[0].text == ""


def test_whatsapp_explicit_recheck_starts_one_existing_row_pass_without_rewinding(
    tmp_path: Path,
) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="first", timestamp=AT)
    _append(database, message_id="message-2", text="second", timestamp=AT + 1)
    _append(database, message_id="message-3", text="third", timestamp=AT + 2)
    normal = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)
    checkpoint = normal.sync("local-whatsapp", limit=10).checkpoint
    assert checkpoint is not None

    rechecking = WhatsAppAppCorpusAdapter(
        account_fingerprint=ACCOUNT,
        store_root=database.parent,
        recheck_existing=True,
    )
    first = rechecking.sync("local-whatsapp", checkpoint=checkpoint, limit=1)
    assert first.complete is False
    assert [document.text for document in first.documents] == ["first"]
    assert json.loads(first.checkpoint or "")["rescan_rowid"] == 1

    # Retaining the flag must not restart an active pass at row 1.
    second = rechecking.sync("local-whatsapp", checkpoint=first.checkpoint, limit=1)
    assert second.complete is False
    assert [document.text for document in second.documents] == ["second"]
    assert json.loads(second.checkpoint or "")["rescan_rowid"] == 2


def test_whatsapp_refuses_replaced_store_without_credential_database(
    tmp_path: Path, monkeypatch
) -> None:
    original = _store(tmp_path / "wacli")
    _append(original, message_id="message-1", text="original", timestamp=AT)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=original.parent)
    inspected: list[Path] = []
    original_snapshot = app_corpus_whatsapp.pinned_sqlite_snapshot

    def pinned_snapshot(database: Path, *, label: str):
        inspected.append(database)
        return original_snapshot(database, label=label)

    monkeypatch.setattr(app_corpus_whatsapp, "pinned_sqlite_snapshot", pinned_snapshot)
    (original.parent / "session.db").mkdir()
    initial = adapter.sync("local-whatsapp", limit=10)
    assert initial.complete is True
    assert [path.name for path in inspected] == ["wacli.db"]

    replacement = _store(tmp_path / "replacement")
    _append(replacement, message_id="message-2", text="replacement", timestamp=AT + 1)
    os.replace(replacement, original)

    refused = adapter.sync("local-whatsapp", checkpoint=initial.checkpoint, limit=10)
    assert refused.complete is False
    assert refused.documents == ()
    assert refused.scanned == 0
    assert refused.freshness["status"] == "refused"


def test_whatsapp_checkpoint_rebases_an_additive_in_place_schema_migration(
    tmp_path: Path,
) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="before upgrade", timestamp=AT)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)

    initial = adapter.sync("local-whatsapp", limit=10)
    before = json.loads(initial.checkpoint or "")
    assert before["version"] == 2

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE messages ADD COLUMN reaction_count INTEGER")
        connection.commit()
    _append(database, message_id="message-2", text="after upgrade", timestamp=AT + 1)

    next_page = adapter.sync("local-whatsapp", checkpoint=initial.checkpoint, limit=10)
    after = json.loads(next_page.checkpoint or "")
    assert next_page.complete is True
    assert next_page.scanned == 1
    assert [document.text for document in next_page.documents] == ["after upgrade"]
    assert after["rowid"] == 2
    assert after["schema"] != before["schema"]
    assert after["generation"] != before["generation"]


def test_whatsapp_v1_checkpoint_crosses_the_documented_wacli_column_migration(
    tmp_path: Path,
) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="before upgrade", timestamp=AT)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)
    modern = json.loads(adapter.sync("local-whatsapp", limit=10).checkpoint or "")
    legacy = {
        key: value
        for key, value in modern.items()
        if key not in {"columns", "prefix_messages", "prefix_newest"}
    }
    legacy["version"] = 1

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE messages ADD COLUMN deletion_reason TEXT")
        connection.commit()
    _append(database, message_id="message-2", text="after upgrade", timestamp=AT + 1)

    next_page = adapter.sync(
        "local-whatsapp",
        checkpoint=json.dumps(legacy, separators=(",", ":"), sort_keys=True),
        limit=10,
    )
    checkpoint = json.loads(next_page.checkpoint or "")
    assert next_page.complete is True
    assert [document.text for document in next_page.documents] == ["after upgrade"]
    assert checkpoint["version"] == 2
    assert checkpoint["rowid"] == 2


def test_whatsapp_refuses_an_incompatible_in_place_schema_change(tmp_path: Path) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="before change", timestamp=AT)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)
    initial = adapter.sync("local-whatsapp", limit=10)

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE messages RENAME COLUMN text TO raw_text")
        connection.commit()

    refused = adapter.sync("local-whatsapp", checkpoint=initial.checkpoint, limit=10)
    assert refused.complete is False
    assert refused.scanned == 0
    assert refused.freshness["status"] == "refused"


def test_whatsapp_refuses_a_migrated_store_that_lost_its_cursor_prefix(tmp_path: Path) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="before upgrade", timestamp=AT)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)
    initial = adapter.sync("local-whatsapp", limit=10)

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE messages ADD COLUMN reaction_count INTEGER")
        connection.execute("DELETE FROM messages WHERE rowid = 1")
        connection.commit()

    refused = adapter.sync("local-whatsapp", checkpoint=initial.checkpoint, limit=10)
    assert refused.complete is False
    assert refused.scanned == 0
    assert refused.freshness["status"] == "refused"


def test_whatsapp_ends_legacy_immediate_rescan_without_replaying_old_rows(
    tmp_path: Path,
) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="message-1", text="already indexed", timestamp=AT)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)
    current = adapter.sync("local-whatsapp", limit=10)
    legacy = json.loads(current.checkpoint or "")
    legacy["phase"] = "rescan"
    legacy["rescan_due_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    legacy["rescan_rowid"] = 700

    repaired = adapter.sync(
        "local-whatsapp",
        checkpoint=json.dumps(legacy, separators=(",", ":"), sort_keys=True),
        limit=10,
    )

    assert repaired.complete is True
    assert repaired.scanned == 0
    assert repaired.documents == ()
    assert json.loads(repaired.checkpoint or "")["phase"] == "steady"


def test_whatsapp_uses_only_a_relay_transcript_bound_to_its_audio_row(tmp_path: Path) -> None:
    database = _store(tmp_path / "wacli")
    _append(
        database,
        message_id="voice-1",
        text="",
        timestamp=AT,
        media_type="audio",
    )
    relay_mail = tmp_path / "mail"
    transcript = (
        "[transcribed voice note (fallback transcript: tone and emphasis may be lost)] Book dentist"
    )
    _voice_envelope(relay_mail, rowid=1, timestamp=AT, transcript=transcript)
    unrelated = relay_mail / "envoy" / "processed" / "unrelated.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(
        "---\nfrom: service-whatsapp-chief-relay\nkind: request\n"
        "event-key: whatsapp-envoy-inbound-00000000000000000000000000000000\n---\n\n"
        f"{transcript}\n",
        encoding="utf-8",
    )
    adapter = WhatsAppAppCorpusAdapter(
        account_fingerprint=ACCOUNT,
        store_root=database.parent,
        voice_transcript_root=relay_mail,
    )

    result = adapter.sync("local-whatsapp", limit=10)

    assert result.complete is True
    assert result.documents[0].text == transcript
    assert result.documents[0].metadata["media_extraction_status"] == "local_transcript"
    assert result.documents[0].metadata["transcript_source"] == "local_relay"
    assert ":relay-transcript:" in result.documents[0].revision


def test_whatsapp_rejects_conflicting_relay_transcript_copies(tmp_path: Path) -> None:
    database = _store(tmp_path / "wacli")
    _append(
        database,
        message_id="voice-1",
        text="",
        timestamp=AT,
        media_type="audio",
    )
    relay_mail = tmp_path / "mail"
    transcript = (
        "[transcribed voice note (fallback transcript: tone and emphasis may be lost)] Book dentist"
    )
    _voice_envelope(relay_mail, rowid=1, timestamp=AT, transcript=transcript)
    copied = relay_mail / "envoy" / "processed" / "copy.md"
    copied.parent.mkdir(parents=True)
    source = (relay_mail / "envoy" / "inbox" / "voice.md").read_text(encoding="utf-8")
    copied.write_text(source.replace("Book dentist", "Different text"), encoding="utf-8")
    adapter = WhatsAppAppCorpusAdapter(
        account_fingerprint=ACCOUNT,
        store_root=database.parent,
        voice_transcript_root=relay_mail,
    )

    result = adapter.sync("local-whatsapp", limit=10)

    assert result.documents[0].text == ""
    assert result.documents[0].metadata["extraction_status"] == "gap"
    assert result.documents[0].metadata["media_extraction_status"] == "transcript_unavailable"
    assert "transcript_source" not in result.documents[0].metadata


def test_whatsapp_marks_nonvoice_media_as_partial_or_unavailable_without_reading_it(
    tmp_path: Path,
) -> None:
    database = _store(tmp_path / "wacli")
    _append(
        database,
        message_id="image-caption",
        text="",
        media_caption="The team photo",
        media_type="image",
        timestamp=AT,
    )
    _append(
        database,
        message_id="video-empty",
        text="",
        media_type="video",
        timestamp=AT + 1,
    )
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)

    result = adapter.sync("local-whatsapp", limit=10)

    caption, empty = result.documents
    assert caption.text == "The team photo"
    assert caption.metadata["extraction_status"] == "partial"
    assert caption.metadata["media_extraction_status"] == "not_extracted"
    assert empty.text == ""
    assert empty.metadata["extraction_status"] == "gap"
    assert empty.metadata["media_extraction_status"] == "not_extracted"


def test_whatsapp_native_capabilities_are_explicit_and_not_apps_call(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime" / "wacli"
    store = tmp_path / "store"

    capabilities = {
        item["name"]: item for item in whatsapp_app_capabilities(runtime=runtime, store_root=store)
    }

    read = capabilities["whatsapp.messages.show"]
    create = capabilities["whatsapp.messages.send_text"]
    update = capabilities["whatsapp.messages.edit_sent_text"]
    delete = capabilities["whatsapp.messages.delete"]
    purge = capabilities["whatsapp.messages.purge_tombstone"]
    assert read["command"] == [
        str(runtime.resolve()),
        "--store",
        str(store.resolve()),
        "--read-only",
        "--json",
        "messages",
        "show",
        "--chat",
        "<chat>",
        "--id",
        "<id>",
    ]
    assert read["approval_required"] is False
    assert read["input_schema"] == {
        "additionalProperties": False,
        "properties": {"chat": {"type": "string"}, "id": {"type": "string"}},
        "required": ["chat", "id"],
        "type": "object",
    }
    assert create["mode"] == "create"
    assert update["mode"] == "update"
    assert delete["mode"] == "delete"
    assert purge["effect"] == "local_delete"
    assert all(item["executor"] == "native_wacli_cli" for item in capabilities.values())
    assert all(item["apps_call_supported"] is False for item in capabilities.values())
    assert all(item["approval_required"] is True for item in (create, update, delete, purge))


def test_whatsapp_retention_replays_same_store_without_inferred_deletions(tmp_path: Path) -> None:
    database = _store(tmp_path / "wacli")
    _append(database, message_id="kept", text="kept", timestamp=AT)
    _append(database, message_id="removed", text="removed", timestamp=AT + 1)
    adapter = WhatsAppAppCorpusAdapter(account_fingerprint=ACCOUNT, store_root=database.parent)
    initial = adapter.sync("local-whatsapp", limit=10)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DELETE FROM messages WHERE rowid = 2")
        connection.commit()
    _append(database, message_id="new", text="new", timestamp=AT + 2)
    page = adapter.sync("local-whatsapp", checkpoint=initial.checkpoint, limit=1)
    assert [doc.text for doc in page.documents] == ["kept"]
    assert page.freshness["status"] == "partial"
    assert not page.complete
    next_page = adapter.sync("local-whatsapp", checkpoint=page.checkpoint, limit=10)
    assert [doc.text for doc in next_page.documents] == ["new"]
    assert next_page.complete
    assert next_page.freshness["status"] == "partial"
    assert all(not doc.deleted for doc in (*page.documents, *next_page.documents))
    assert json.loads(next_page.checkpoint)["continuity_gap"] is True
