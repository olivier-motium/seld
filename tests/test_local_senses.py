from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from continuity_kernel import apple_messages, whatsapp
from continuity_kernel.errors import ContinuityError, ValidationError

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
PRIVATE_ROUTE = "route-447199900000@s.whatsapp.net"
PRIVATE_SENDER = "sender-447199900001@s.whatsapp.net"
PRIVATE_MESSAGE_ID = "provider-message-id-44"
PRIVATE_MEDIA_PATH = "/private/provider/media/object"
PRIVATE_MEDIA_KEY = b"provider-media-key"


def _apple_store(root: Path) -> Path:
    root.mkdir(parents=True)
    database = root / "chat.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE message (date INTEGER, is_from_me INTEGER, text TEXT, "
            "attributedBody BLOB, item_type INTEGER DEFAULT 0, "
            "associated_message_type INTEGER DEFAULT 0, "
            "cache_has_attachments INTEGER DEFAULT 0)"
        )
        connection.commit()
    return database


def _append_apple(database: Path, *, timestamp: int, body: str) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO message VALUES (?, 0, ?, NULL, 0, 0, 0)",
            (timestamp, body),
        )
        connection.commit()


def _replace_apple(database: Path, *, body: str) -> None:
    replacement = database.with_name("replacement.db")
    connection = sqlite3.connect(replacement)
    try:
        connection.execute(
            "CREATE TABLE message (date INTEGER, is_from_me INTEGER, text TEXT, "
            "attributedBody BLOB, item_type INTEGER DEFAULT 0, "
            "associated_message_type INTEGER DEFAULT 0, "
            "cache_has_attachments INTEGER DEFAULT 0)"
        )
        connection.execute("INSERT INTO message VALUES (0, 0, ?, NULL, 0, 0, 0)", (body,))
        connection.commit()
    finally:
        connection.close()
    os.replace(replacement, database)


def _whatsapp_store(root: Path) -> Path:
    root.mkdir(parents=True)
    database = root / "wacli.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE chats (jid TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "name TEXT, last_message_ts INTEGER)"
        )
        connection.execute(
            "CREATE TABLE messages ("
            "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_jid TEXT NOT NULL, chat_name TEXT, msg_id TEXT NOT NULL, "
            "sender_jid TEXT, sender_name TEXT, ts INTEGER NOT NULL, "
            "from_me INTEGER NOT NULL, text TEXT, display_text TEXT, "
            "media_type TEXT, media_caption TEXT, direct_path TEXT, media_key BLOB, "
            "revoked INTEGER NOT NULL DEFAULT 0, "
            "deleted_for_me INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "INSERT INTO chats VALUES (?, 'dm', 'Planning', ?)",
            (PRIVATE_ROUTE, int(NOW.timestamp())),
        )
    (root / "HEARTBEAT").write_text(
        (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z") + "\n",
        encoding="utf-8",
    )
    return database


def _append_whatsapp(database: Path, *, body: str, seconds: int = 0) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO messages ("
            "chat_jid, chat_name, msg_id, sender_jid, sender_name, ts, from_me, "
            "text, display_text, media_type, direct_path, media_key"
            ") VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (
                PRIVATE_ROUTE,
                "Planning",
                f"{PRIVATE_MESSAGE_ID}-{seconds}",
                PRIVATE_SENDER,
                "Synthetic Sender",
                int(NOW.timestamp()) + seconds,
                body,
                body,
                "document",
                PRIVATE_MEDIA_PATH,
                PRIVATE_MEDIA_KEY,
            ),
        )


def _replace_whatsapp(database: Path, *, body: str) -> None:
    replacement_root = database.parent / "replacement"
    replacement = _whatsapp_store(replacement_root)
    _append_whatsapp(replacement, body=body)
    os.replace(replacement, database)


def _runtime(root: Path) -> Path:
    runtime = root / "wacli"
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o755)
    return runtime


def _runner(
    runtime: Path, calls: list[tuple[str, ...]]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        assert isinstance(command, list)
        normalized = tuple(str(part) for part in command)
        calls.append(normalized)
        if normalized[:3] == ("/usr/bin/pgrep", "-x", "wacli"):
            return subprocess.CompletedProcess(command, 0, stdout="71\n", stderr="")
        if normalized[:2] == ("/bin/launchctl", "print"):
            output = f"state = running\nprogram = {runtime}\nsync\n--follow\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        raise AssertionError(f"unexpected passive status command: {normalized}")

    return run


def _decoded_token(token: str) -> str:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")


def _tamper(token: str) -> str:
    replacement = "A" if token[0] != "A" else "B"
    return replacement + token[1:]


@pytest.mark.parametrize("uid_primitive", (None, "not-callable"))
def test_whatsapp_service_probe_fails_closed_without_callable_posix_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uid_primitive: object | None,
) -> None:
    store = tmp_path / "wacli-store"
    _whatsapp_store(store)
    runtime_root = tmp_path / "bin"
    runtime_root.mkdir()
    runtime = _runtime(runtime_root)
    calls: list[tuple[str, ...]] = []
    if uid_primitive is None:
        monkeypatch.delattr(os, "getuid", raising=False)
    else:
        monkeypatch.setattr(os, "getuid", uid_primitive, raising=False)

    status = whatsapp.inspect_whatsapp(
        runtime=runtime,
        store_root=store,
        runner=_runner(runtime, calls),
        observed_at=NOW,
    )

    assert status.available is False
    assert status.sync_service_running is False
    assert status.error is not None
    assert status.error.startswith("standalone sync service is not running")
    assert not any(call[:2] == ("/bin/launchctl", "print") for call in calls)


def test_apple_messages_forward_baseline_replays_until_verified_ack(tmp_path: Path) -> None:
    store = tmp_path / "Messages"
    database = _apple_store(store)
    status = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW)
    baseline = status.cursor()
    assert status.available and baseline is not None
    assert json.loads(baseline)["rowid"] == 0

    private_body = "transient-apple-body-" + "x" * apple_messages.MAX_BODY_CHARS
    _append_apple(database, timestamp=1_000_000_000, body=private_body)
    _append_apple(database, timestamp=2_000_000_000, body="second transient body")
    post_append_status = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW)
    assert private_body not in json.dumps(post_append_status.to_dict())
    first = apple_messages.read_apple_messages_delta(
        cursor=baseline, store_root=store, limit=1, observed_at=NOW
    )
    replay = apple_messages.read_apple_messages_delta(
        cursor=baseline, store_root=store, limit=1, observed_at=NOW
    )

    assert first == replay
    assert not first.complete
    assert len(first.messages) == 1
    assert first.messages[0].body_truncated
    assert len(first.messages[0].body or "") == apple_messages.MAX_BODY_CHARS
    assert private_body not in first.cursor

    project = tmp_path / "state"
    project.mkdir()
    token = apple_messages.create_apple_messages_ack_token(
        project_root=project, previous_cursor=baseline, delta=first
    )
    assert "transient-apple-body" not in _decoded_token(token)
    with pytest.raises(ValidationError, match="another state root"):
        apple_messages.verify_apple_messages_ack_token(
            token=token,
            project_root=tmp_path / "other",
            current_cursor=baseline,
            store_root=store,
            observed_at=NOW,
        )
    with pytest.raises(ValidationError, match="token is invalid"):
        apple_messages.verify_apple_messages_ack_token(
            token=_tamper(token),
            project_root=project,
            current_cursor=baseline,
            store_root=store,
            observed_at=NOW,
        )

    acknowledgement, already_covered = apple_messages.verify_apple_messages_ack_token(
        token=token,
        project_root=project,
        current_cursor=baseline,
        store_root=store,
        observed_at=NOW,
    )
    assert not already_covered
    repeated, already_covered = apple_messages.verify_apple_messages_ack_token(
        token=token,
        project_root=project,
        current_cursor=acknowledgement.cursor,
        store_root=store,
        observed_at=NOW,
    )
    assert already_covered and repeated == acknowledgement


def test_apple_attributed_body_is_bounded_inside_sqlite(tmp_path: Path) -> None:
    store = tmp_path / "Messages"
    database = _apple_store(store)
    oversized = b"NSString\0" + b"x" * (apple_messages.MAX_ATTRIBUTED_BODY_BYTES * 8)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message VALUES (?, 0, NULL, ?, 0, 0, 0)",
            (1_000_000_000, oversized),
        )
    status = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW)
    assert status.schema is not None and status.generation is not None

    rows = apple_messages._delta_rows(
        database,
        after_rowid=0,
        limit=2,
        expected_schema=status.schema,
        expected_generation=status.generation,
    )

    assert len(rows) == 1
    assert len(rows[0]["attributed_body"]) == apple_messages.MAX_ATTRIBUTED_BODY_BYTES + 1


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("content", "delivered content changed"),
        ("prefix", "delivery prefix changed"),
        ("generation", "delivery store changed"),
    ],
)
def test_apple_messages_ack_fails_closed_when_store_changes(
    tmp_path: Path, mutation: str, error: str
) -> None:
    store = tmp_path / mutation / "Messages"
    database = _apple_store(store)
    baseline = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW).cursor()
    assert baseline is not None
    _append_apple(database, timestamp=1_000_000_000, body="prepared apple body")
    delta = apple_messages.read_apple_messages_delta(
        cursor=baseline, store_root=store, observed_at=NOW
    )
    project = tmp_path / mutation / "state"
    project.mkdir()
    token = apple_messages.create_apple_messages_ack_token(
        project_root=project, previous_cursor=baseline, delta=delta
    )

    if mutation == "content":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE message SET text = 'changed' WHERE ROWID = 1")
    elif mutation == "prefix":
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM message WHERE ROWID = 1")
    else:
        _replace_apple(database, body="prepared apple body")

    with pytest.raises(ContinuityError, match=error):
        apple_messages.verify_apple_messages_ack_token(
            token=token,
            project_root=project,
            current_cursor=baseline,
            store_root=store,
            observed_at=NOW,
        )


def test_apple_messages_status_is_sanitized_and_exposes_no_write_route(tmp_path: Path) -> None:
    status = apple_messages.inspect_apple_messages(
        store_root=tmp_path / "private-person-store", observed_at=NOW
    )
    assert not status.available
    assert status.error == "Apple Messages store is unavailable"
    assert "private-person-store" not in json.dumps(status.to_dict())
    assert apple_messages.SOURCE_ID == "apple_messages"
    assert not any(
        word in name.lower()
        for name in apple_messages.__all__
        for word in ("send", "write", "mutate", "delete")
    )


@pytest.mark.skipif(os.name == "nt", reason="the standalone WhatsApp service uses launchd")
def test_whatsapp_forward_baseline_replays_until_verified_ack(tmp_path: Path) -> None:
    store = tmp_path / "wacli-store"
    database = _whatsapp_store(store)
    runtime = _runtime(tmp_path)
    calls: list[tuple[str, ...]] = []
    runner = _runner(runtime, calls)
    status = whatsapp.inspect_whatsapp(
        store_root=store, runtime=runtime, observed_at=NOW, runner=runner
    )
    baseline = status.cursor()
    assert status.available and baseline is not None
    assert json.loads(baseline)["rowid"] == 0

    private_body = "transient-whatsapp-body-" + "x" * whatsapp.MAX_BODY_CHARS
    _append_whatsapp(database, body=private_body, seconds=1)
    _append_whatsapp(database, body="second transient body", seconds=2)
    post_append_status = whatsapp.inspect_whatsapp(
        store_root=store, runtime=runtime, observed_at=NOW, runner=runner
    )
    assert private_body not in json.dumps(post_append_status.to_dict())
    first = whatsapp.read_whatsapp_delta(
        cursor=baseline,
        store_root=store,
        runtime=runtime,
        runner=runner,
        observed_at=NOW,
        limit=1,
    )
    replay = whatsapp.read_whatsapp_delta(
        cursor=baseline,
        store_root=store,
        runtime=runtime,
        runner=runner,
        observed_at=NOW,
        limit=1,
    )

    assert first == replay
    assert not first.complete
    assert len(first.messages) == 1
    assert first.messages[0].body_truncated
    assert len(first.messages[0].body or "") == whatsapp.MAX_BODY_CHARS
    transient = json.dumps(first.to_dict())
    durable = json.dumps(status.to_dict()) + first.cursor
    token_project = tmp_path / "state"
    token_project.mkdir()
    token = whatsapp.create_whatsapp_ack_token(
        project_root=token_project, previous_cursor=baseline, delta=first
    )
    durable += _decoded_token(token)
    for provider_secret in (
        PRIVATE_ROUTE,
        PRIVATE_SENDER,
        PRIVATE_MESSAGE_ID,
        PRIVATE_MEDIA_PATH,
        PRIVATE_MEDIA_KEY.decode(),
    ):
        assert provider_secret not in transient
        assert provider_secret not in durable
    assert "transient-whatsapp-body" not in durable

    with pytest.raises(ValidationError, match="another state root"):
        whatsapp.verify_whatsapp_ack_token(
            token=token,
            project_root=tmp_path / "other",
            current_cursor=baseline,
            store_root=store,
            runtime=runtime,
            runner=runner,
            observed_at=NOW,
        )
    with pytest.raises(ValidationError, match="token is invalid"):
        whatsapp.verify_whatsapp_ack_token(
            token=_tamper(token),
            project_root=token_project,
            current_cursor=baseline,
            store_root=store,
            runtime=runtime,
            runner=runner,
            observed_at=NOW,
        )

    acknowledgement, already_covered = whatsapp.verify_whatsapp_ack_token(
        token=token,
        project_root=token_project,
        current_cursor=baseline,
        store_root=store,
        runtime=runtime,
        runner=runner,
        observed_at=NOW,
    )
    assert not already_covered
    repeated, already_covered = whatsapp.verify_whatsapp_ack_token(
        token=token,
        project_root=token_project,
        current_cursor=acknowledgement.cursor,
        store_root=store,
        runtime=runtime,
        runner=runner,
        observed_at=NOW,
    )
    assert already_covered and repeated == acknowledgement
    assert calls
    assert {command[0] for command in calls} <= {"/usr/bin/pgrep", "/bin/launchctl"}
    assert all(command[0] != str(runtime) for command in calls)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("content", "delivered content changed"),
        ("prefix", "delivery prefix changed"),
        ("generation", "delivery store changed"),
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="the standalone WhatsApp service uses launchd")
def test_whatsapp_ack_fails_closed_when_store_changes(
    tmp_path: Path, mutation: str, error: str
) -> None:
    store = tmp_path / mutation / "wacli-store"
    database = _whatsapp_store(store)
    runtime = _runtime(tmp_path / mutation)
    calls: list[tuple[str, ...]] = []
    runner = _runner(runtime, calls)
    baseline = whatsapp.inspect_whatsapp(
        store_root=store, runtime=runtime, runner=runner, observed_at=NOW
    ).cursor()
    assert baseline is not None
    _append_whatsapp(database, body="prepared WhatsApp body")
    delta = whatsapp.read_whatsapp_delta(
        cursor=baseline,
        store_root=store,
        runtime=runtime,
        runner=runner,
        observed_at=NOW,
    )
    project = tmp_path / mutation / "state"
    project.mkdir()
    token = whatsapp.create_whatsapp_ack_token(
        project_root=project, previous_cursor=baseline, delta=delta
    )

    if mutation == "content":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE messages SET display_text = 'changed' WHERE rowid = 1")
    elif mutation == "prefix":
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM messages WHERE rowid = 1")
    else:
        _replace_whatsapp(database, body="prepared WhatsApp body")

    with pytest.raises(ContinuityError, match=error):
        whatsapp.verify_whatsapp_ack_token(
            token=token,
            project_root=project,
            current_cursor=baseline,
            store_root=store,
            runtime=runtime,
            runner=runner,
            observed_at=NOW,
        )


def test_whatsapp_status_is_sanitized_and_has_no_provider_mutation_route(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    calls: list[tuple[str, ...]] = []
    status = whatsapp.inspect_whatsapp(
        store_root=tmp_path / "private-person-store",
        runtime=runtime,
        observed_at=NOW,
        runner=_runner(runtime, calls),
    )
    assert not status.available
    assert status.error == "standalone WhatsApp store is unavailable"
    assert "private-person-store" not in json.dumps(status.to_dict())
    assert whatsapp.SOURCE_ID == "whatsapp"
    assert whatsapp.DEFAULT_SERVICE_LABEL == "ai.seld.wacli-sync"
    assert not any(
        word in name.lower()
        for name in whatsapp.__all__
        for word in ("send", "write", "mutate", "delete", "sync")
    )


@pytest.mark.parametrize(
    "kind",
    (
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Windows symlink setup needs elevated privileges"
            ),
        ),
        "directory",
    ),
)
def test_apple_messages_rejects_nonregular_database(tmp_path: Path, kind: str) -> None:
    store = tmp_path / "Messages"
    store.mkdir()
    database = store / "chat.db"
    if kind == "symlink":
        target = _apple_store(tmp_path / "target")
        database.symlink_to(target)
    else:
        database.mkdir()

    status = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW)

    assert status.available is False
    assert status.database_present is False
    assert status.error == "Apple Messages store is unavailable"


@pytest.mark.skipif(os.name == "nt", reason="Windows locks an open provider database")
def test_apple_messages_detects_database_entry_swap_during_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "Messages"
    database = _apple_store(store)
    replacement = _apple_store(tmp_path / "replacement")
    original_columns = apple_messages._columns
    swapped = False

    def swap_after_open(connection: sqlite3.Connection, table: str) -> dict[str, str]:
        nonlocal swapped
        columns = original_columns(connection, table)
        if not swapped:
            moved = database.with_name("chat.db-moved")
            database.rename(moved)
            os.replace(replacement, database)
            swapped = True
        return columns

    monkeypatch.setattr(apple_messages, "_columns", swap_after_open)

    status = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW)

    assert status.available is False
    assert status.error is not None and "changed during" in status.error


@pytest.mark.skipif(os.name == "nt", reason="Windows locks an open provider database")
def test_apple_messages_connect_time_aba_cannot_redirect_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "Messages"
    database = _apple_store(store)
    baseline = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW).cursor()
    assert baseline is not None
    _append_apple(database, timestamp=1_000_000_000, body="BENIGN-ORIGINAL")
    replacement = _apple_store(tmp_path / "outside")
    _append_apple(replacement, timestamp=1_000_000_000, body="OUTSIDE-PRIVATE-BODY")
    original_connect = sqlite3.connect
    connect_calls = 0

    def transient_swap(
        database_path: str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls != 2:
            return original_connect(database_path, uri=uri, timeout=timeout)
        held = database.with_name("chat.db-held")
        database.rename(held)
        os.replace(replacement, database)
        try:
            return original_connect(database_path, uri=uri, timeout=timeout)
        finally:
            os.replace(database, replacement)
            held.rename(database)

    monkeypatch.setattr(sqlite3, "connect", transient_swap)

    delta = apple_messages.read_apple_messages_delta(
        cursor=baseline,
        store_root=store,
        observed_at=NOW,
    )

    assert [message.body for message in delta.messages] == ["BENIGN-ORIGINAL"]
    with original_connect(database) as connection:
        assert connection.execute("SELECT text FROM message").fetchone()[0] == "BENIGN-ORIGINAL"


def test_apple_messages_snapshot_includes_a_stable_committed_wal(tmp_path: Path) -> None:
    store = tmp_path / "Messages"
    database = _apple_store(store)
    baseline = apple_messages.inspect_apple_messages(store_root=store, observed_at=NOW).cursor()
    assert baseline is not None
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT INTO message VALUES (?, 0, ?, NULL, 0, 0, 0)",
            (1_000_000_000, "COMMITTED-IN-WAL"),
        )
        writer.commit()
        assert database.with_name("chat.db-wal").is_file()

        delta = apple_messages.read_apple_messages_delta(
            cursor=baseline,
            store_root=store,
            observed_at=NOW,
        )
    finally:
        writer.close()

    assert [message.body for message in delta.messages] == ["COMMITTED-IN-WAL"]


@pytest.mark.parametrize(
    "kind",
    (
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(
                os.name == "nt", reason="Windows symlink setup needs elevated privileges"
            ),
        ),
        "directory",
    ),
)
def test_whatsapp_rejects_nonregular_database(tmp_path: Path, kind: str) -> None:
    store = tmp_path / "wacli-store"
    store.mkdir()
    database = store / "wacli.db"
    if kind == "symlink":
        target = _whatsapp_store(tmp_path / "target")
        database.symlink_to(target)
    else:
        database.mkdir()
    (store / "HEARTBEAT").write_text(NOW.isoformat().replace("+00:00", "Z") + "\n")
    runtime = _runtime(tmp_path)

    status = whatsapp.inspect_whatsapp(
        store_root=store,
        runtime=runtime,
        observed_at=NOW,
        runner=_runner(runtime, []),
    )

    assert status.available is False
    assert status.database_present is False
    assert status.error == "standalone WhatsApp store is unavailable"


@pytest.mark.parametrize("heartbeat_kind", ("symlink", "oversized"))
@pytest.mark.skipif(os.name == "nt", reason="the standalone WhatsApp service uses launchd")
def test_whatsapp_heartbeat_is_bounded_and_never_follows_links(
    tmp_path: Path, heartbeat_kind: str
) -> None:
    store = tmp_path / "wacli-store"
    _whatsapp_store(store)
    heartbeat = store / "HEARTBEAT"
    heartbeat.unlink()
    if heartbeat_kind == "symlink":
        target = tmp_path / "external-heartbeat"
        target.write_text(NOW.isoformat().replace("+00:00", "Z") + "\n")
        heartbeat.symlink_to(target)
    else:
        heartbeat.write_text("x" * 257)
    runtime = _runtime(tmp_path)

    status = whatsapp.inspect_whatsapp(
        store_root=store,
        runtime=runtime,
        observed_at=NOW,
        runner=_runner(runtime, []),
    )

    assert status.available is False
    assert status.error == "standalone sync heartbeat is unreadable"


@pytest.mark.skipif(os.name == "nt", reason="Windows locks an open provider database")
def test_whatsapp_detects_database_entry_swap_during_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "wacli-store"
    database = _whatsapp_store(store)
    replacement = _whatsapp_store(tmp_path / "replacement")
    runtime = _runtime(tmp_path)
    original_columns = whatsapp._columns
    swapped = False

    def swap_after_open(connection: sqlite3.Connection, table: str) -> dict[str, str]:
        nonlocal swapped
        columns = original_columns(connection, table)
        if not swapped:
            moved = database.with_name("wacli.db-moved")
            database.rename(moved)
            os.replace(replacement, database)
            swapped = True
        return columns

    monkeypatch.setattr(whatsapp, "_columns", swap_after_open)

    status = whatsapp.inspect_whatsapp(
        store_root=store,
        runtime=runtime,
        observed_at=NOW,
        runner=_runner(runtime, []),
    )

    assert status.available is False
    assert status.error is not None and "changed during" in status.error


@pytest.mark.skipif(os.name == "nt", reason="the standalone WhatsApp service uses launchd")
def test_whatsapp_connect_time_aba_cannot_redirect_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "wacli-store"
    database = _whatsapp_store(store)
    runtime = _runtime(tmp_path)
    runner = _runner(runtime, [])
    baseline = whatsapp.inspect_whatsapp(
        store_root=store,
        runtime=runtime,
        observed_at=NOW,
        runner=runner,
    ).cursor()
    assert baseline is not None
    _append_whatsapp(database, body="BENIGN-ORIGINAL")
    replacement = _whatsapp_store(tmp_path / "outside")
    _append_whatsapp(replacement, body="OUTSIDE-PRIVATE-BODY")
    original_connect = sqlite3.connect
    connect_calls = 0

    def transient_swap(
        database_path: str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls != 2:
            return original_connect(database_path, uri=uri, timeout=timeout)
        held = database.with_name("wacli.db-held")
        database.rename(held)
        os.replace(replacement, database)
        try:
            return original_connect(database_path, uri=uri, timeout=timeout)
        finally:
            os.replace(database, replacement)
            held.rename(database)

    monkeypatch.setattr(sqlite3, "connect", transient_swap)

    delta = whatsapp.read_whatsapp_delta(
        cursor=baseline,
        store_root=store,
        runtime=runtime,
        runner=runner,
        observed_at=NOW,
    )

    assert [message.body for message in delta.messages] == ["BENIGN-ORIGINAL"]
    with original_connect(database) as connection:
        assert connection.execute("SELECT text FROM messages").fetchone()[0] == "BENIGN-ORIGINAL"
