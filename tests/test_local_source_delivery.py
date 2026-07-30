from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel import (
    apple_messages as apple_adapter,
)
from continuity_kernel import (
    codex_integration,
    config,
    mcp_server,
)
from continuity_kernel import (
    whatsapp as whatsapp_adapter,
)
from continuity_kernel.atomic import PinnedPathRoot
from continuity_kernel.errors import ConflictError, ContinuityError, NotFoundError, ValidationError
from continuity_kernel.local_source_delivery import (
    LocalSourceDelivery,
    _Binding,
    _Checkpoint,
)
from continuity_kernel.source_state import ABSENT_SOURCE_REVISION
from continuity_kernel.vault import Vault

_POSIX_STORAGE = pytest.mark.skipif(
    os.name == "nt", reason="local source delivery requires POSIX pinned storage"
)

APPLE_TEST_TIMESTAMP = 800_000_000


def _apple_store(root: Path, *, old_body: str = "already covered") -> Path:
    root.mkdir(parents=True)
    database = root / "chat.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE message (date INTEGER, is_from_me INTEGER, text TEXT, "
            "attributedBody BLOB, item_type INTEGER DEFAULT 0, "
            "associated_message_type INTEGER DEFAULT 0, "
            "cache_has_attachments INTEGER DEFAULT 0)"
        )
        connection.execute("INSERT INTO message VALUES (0, 0, ?, NULL, 0, 0, 0)", (old_body,))
    return database


def _append_apple(
    database: Path,
    body: str,
    *,
    timestamp: int = APPLE_TEST_TIMESTAMP,
    item_type: int = 0,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message VALUES (?, 0, ?, NULL, ?, 0, 0)",
            (timestamp, body, item_type),
        )


def _replace_apple(database: Path, *, new_body: str) -> None:
    replacement = database.with_name("replacement.db")
    _apple_store(replacement.parent / "replacement-root", old_body="already covered")
    source = replacement.parent / "replacement-root" / "chat.db"
    _append_apple(source, new_body)
    os.replace(source, database)


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
            "INSERT INTO chats VALUES ('private-route@s.whatsapp.net', 'dm', 'Planning', ?)",
            (int(datetime.now(UTC).timestamp()),),
        )
    (root / "HEARTBEAT").write_text(
        datetime.now(UTC).isoformat().replace("+00:00", "Z") + "\n",
        encoding="utf-8",
    )
    return database


def _append_whatsapp(database: Path, body: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO messages ("
            "chat_jid, chat_name, msg_id, sender_jid, sender_name, ts, from_me, "
            "text, display_text, direct_path, media_key"
            ") VALUES (?, 'Planning', ?, ?, 'Synthetic sender', ?, 0, ?, ?, ?, ?)",
            (
                "private-route@s.whatsapp.net",
                "provider-message-id",
                "private-sender@s.whatsapp.net",
                int(datetime.now(UTC).timestamp()),
                body,
                body,
                "/private/provider/media",
                b"provider-media-key",
            ),
        )


def _runtime(root: Path) -> Path:
    runtime = root / "wacli"
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o755)
    return runtime


def _runner(runtime: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        assert isinstance(command, list)
        if command[:3] == ["/usr/bin/pgrep", "-x", "wacli"]:
            return subprocess.CompletedProcess(command, 0, stdout="71\n", stderr="")
        if command[:2] == ["/bin/launchctl", "print"]:
            output = f"state = running\nprogram = {runtime}\nsync\n--follow\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        raise AssertionError(f"unexpected status command: {command}")

    return run


def _selected_vault(tmp_path: Path, *sources: str) -> tuple[Vault, dict[str, Any]]:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Local delivery")
    selected = vault.select_sources(
        expected_revision=ABSENT_SOURCE_REVISION,
        sources=tuple(sources),
    )
    return vault, selected


def _state_path() -> Path:
    paths = list((config.data_dir() / "local-source-delivery/checkpoints").glob("*.json"))
    assert len(paths) == 1
    return paths[0]


def _result_ref(vault: Vault, identifier: str) -> str:
    try:
        task = vault.get_task(identifier)
    except NotFoundError:
        task = vault.create_task(
            identifier=identifier,
            title=f"Synthetic result {identifier}",
            outcome="Retain one exact local-source test disposition.",
            status="doing",
            next_actor="agent",
            next_action="Verify the local-source acknowledgement.",
            observed_at=datetime.now(UTC),
        )
    return f"task:{task.identifier}@{task.revision}"


def _ack(
    delivery: LocalSourceDelivery,
    poll: dict[str, Any],
    *,
    disposition: str = "accepted",
    result_refs: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    delivery_info = cast(dict[str, Any], poll["delivery"])
    exact_refs = result_refs or (_result_ref(delivery.vault, "local-proof"),)
    return delivery.acknowledge(
        cast(str, poll["source"]),
        token=cast(str, delivery_info["token"]),
        expected_source_revision=cast(str, delivery_info["source_revision"]),
        disposition=disposition,
        result_refs=exact_refs,
        actor_ref="codex-task:local-source-proof",
        account_binding="local-account:confirmed",
    )


def test_whatsapp_service_label_env_is_validated_and_explicit_value_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    monkeypatch.setenv("GSV_WHATSAPP_SERVICE_LABEL", "ai.example.custom-sync")

    from_environment = LocalSourceDelivery(vault)
    explicit = LocalSourceDelivery(
        vault,
        whatsapp_service_label="ai.example.explicit-sync",
    )

    assert from_environment.whatsapp_service_label == "ai.example.custom-sync"
    assert explicit.whatsapp_service_label == "ai.example.explicit-sync"

    monkeypatch.setenv("GSV_WHATSAPP_SERVICE_LABEL", "bad label with spaces")
    with pytest.raises(ValidationError, match="service label"):
        LocalSourceDelivery(vault)
    assert (
        LocalSourceDelivery(
            vault,
            whatsapp_service_label="ai.example.explicit-still-wins",
        ).whatsapp_service_label
        == "ai.example.explicit-still-wins"
    )


@_POSIX_STORAGE
def test_forward_baseline_discard_replay_and_semantic_ack_are_content_free(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store, old_body="old body must never replay")
    delivery = LocalSourceDelivery(vault, store_root=store)

    baseline = delivery.baseline("apple_messages")
    assert baseline["baseline_scope"] == "forward-only"
    assert baseline["messages"] is None
    assert "cursor" not in baseline
    _append_apple(database, "new transient body")

    first = delivery.poll("apple_messages", limit=1)
    replay = LocalSourceDelivery(vault, store_root=store).poll("apple_messages", limit=99)
    assert first == replay
    assert first["messages"][0]["body"] == "new transient body"
    assert "old body must never replay" not in json.dumps(first)
    state_path = _state_path()
    state_before = state_path.read_text(encoding="ascii")
    assert "new transient body" not in state_before
    assert vault.get_source_snapshot().observation("apple_messages") is None

    acknowledged = _ack(delivery, first, disposition="rejected")
    assert acknowledged["sequence"] == 1
    assert acknowledged["disposition"] == "rejected"
    state_after = state_path.read_text(encoding="ascii")
    ledger = (vault.root / "SOURCES.md").read_text(encoding="utf-8")
    for body in ("old body must never replay", "new transient body"):
        assert body not in state_after
        assert body not in ledger
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700

    exact_retry = _ack(LocalSourceDelivery(vault, store_root=store), first, disposition="rejected")
    assert exact_retry["already_acknowledged"] is True
    empty = LocalSourceDelivery(vault, store_root=store).poll("apple_messages")
    assert empty["messages"] == []
    empty_result = _ack(
        LocalSourceDelivery(vault, store_root=store),
        empty,
        result_refs=(_result_ref(vault, "empty-proof"),),
    )
    assert empty_result["sequence"] == 2
    empty_observation = vault.get_source_snapshot().observation("apple_messages")
    assert empty_observation is not None
    assert empty_observation.result.value == "explicit_empty"


@_POSIX_STORAGE
def test_ack_rejects_noncanonical_result_reference_before_any_persistence(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "transient evidence")
    poll = delivery.poll("apple_messages")
    before = _state_path().read_bytes()
    marker = "sk_live_synthetic_result_reference"

    with pytest.raises(ValidationError, match="reference"):
        delivery.acknowledge(
            "apple_messages",
            token=cast(str, poll["delivery"]["token"]),
            expected_source_revision=cast(str, poll["delivery"]["source_revision"]),
            disposition="accepted",
            result_refs=(marker,),
            actor_ref="codex-task:local-source-proof",
            account_binding="local-account:confirmed",
        )

    assert _state_path().read_bytes() == before
    assert marker.encode() not in _state_path().read_bytes()
    assert vault.get_source_snapshot().observation("apple_messages") is None


@_POSIX_STORAGE
def test_result_ref_source_receipt_and_checkpoint_share_one_vault_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    task = vault.create_task(
        identifier="lock-consistent-result",
        title="Lock-consistent result",
        outcome="The source receipt commits against one exact canonical revision.",
        status="doing",
        next_actor="agent",
        next_action="Commit the local-source disposition.",
    )
    result_ref = f"task:{task.identifier}@{task.revision}"
    normal = LocalSourceDelivery(vault, store_root=store)
    normal.baseline("apple_messages")
    _append_apple(database, "concurrent canonical mutation")
    poll = normal.poll("apple_messages")
    authoritative_validation = threading.Event()
    mutation_started = threading.Event()
    mutation_finished = threading.Event()
    updated: dict[str, object] = {}
    validation_count = 0
    original_resolver = vault._resolve_canonical_result_ref

    def resolve_with_race(value: str) -> str:
        nonlocal validation_count
        resolved = original_resolver(value)
        validation_count += 1
        if validation_count == 2:
            authoritative_validation.set()
            assert mutation_started.wait(timeout=5)
        return resolved

    monkeypatch.setattr(vault, "_resolve_canonical_result_ref", resolve_with_race)

    def mutate_task() -> None:
        assert authoritative_validation.wait(timeout=5)
        mutation_started.set()
        updated["task"] = vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            next_action="Continue after the committed source receipt.",
            note="Concurrent mutation completed only after receipt and checkpoint publication.",
        )
        mutation_finished.set()

    worker = threading.Thread(target=mutate_task, daemon=True)
    worker.start()

    def assert_commit_window(_receipt: dict[str, Any]) -> None:
        assert mutation_started.is_set()
        assert not mutation_finished.is_set()
        assert vault.get_task(task.identifier).revision == task.revision
        assert vault.get_source_snapshot().observation("apple_messages") is not None
        assert json.loads(_state_path().read_text(encoding="ascii"))["pending_token"] is not None

    acknowledgement = _ack(
        LocalSourceDelivery(
            vault,
            store_root=store,
            after_source_commit=assert_commit_window,
        ),
        poll,
        result_refs=(result_ref,),
    )
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert mutation_finished.is_set()
    changed = cast(Any, updated["task"])
    assert changed.revision != task.revision
    fresh_vault = Vault(vault.root)
    fresh_status = LocalSourceDelivery(fresh_vault, store_root=store).status("apple_messages")
    assert acknowledgement["result_refs"] == [result_ref]
    assert fresh_status["last_receipt"]["result_refs"] == [result_ref]
    assert fresh_status["pending"] is False
    assert fresh_vault.get_task(task.identifier).revision == changed.revision


@_POSIX_STORAGE
def test_receipt_commit_precedes_checkpoint_and_exact_retry_recovers(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    normal = LocalSourceDelivery(vault, store_root=store)
    normal.baseline("apple_messages")
    _append_apple(database, "crash-bound body")
    poll = normal.poll("apple_messages")
    before_state = json.loads(_state_path().read_text(encoding="ascii"))

    class InjectedCrash(RuntimeError):
        pass

    def crash_after_receipt(_receipt: dict[str, Any]) -> None:
        during = json.loads(_state_path().read_text(encoding="ascii"))
        assert during["checkpoint_digest"] == before_state["checkpoint_digest"]
        assert during["pending_token"] == before_state["pending_token"]
        assert vault.get_source_snapshot().observation("apple_messages") is not None
        raise InjectedCrash

    crashing = LocalSourceDelivery(
        vault,
        store_root=store,
        after_source_commit=crash_after_receipt,
    )
    with pytest.raises(InjectedCrash):
        _ack(crashing, poll)
    assert LocalSourceDelivery(vault, store_root=store).status("apple_messages")["pending"]

    recovered = _ack(LocalSourceDelivery(vault, store_root=store), poll)
    assert recovered["sequence"] == 1
    assert recovered["already_acknowledged"] is False
    events = [
        json.loads(line)
        for line in (vault.root / "journal/events.jsonl").read_text().splitlines()
        if line
    ]
    source_events = [event for event in events if event["operation"] == "source.observe"]
    assert len(source_events) == 1
    assert _ack(LocalSourceDelivery(vault, store_root=store), poll)["already_acknowledged"]


@_POSIX_STORAGE
def test_receipt_recovery_requires_exact_semantics_even_after_result_record_advances(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    result_task = vault.create_task(
        identifier="crash-result",
        title="Crash result",
        outcome="Retain the exact semantic receipt across recovery.",
        status="doing",
        next_actor="agent",
        next_action="Commit the source disposition.",
    )
    alternate_task = vault.create_task(
        identifier="alternate-crash-result",
        title="Alternate crash result",
        outcome="Provide a different valid result reference for the recovery boundary.",
        status="doing",
        next_actor="agent",
        next_action="Remain distinct from the committed receipt.",
    )
    result_ref = f"task:{result_task.identifier}@{result_task.revision}"
    alternate_ref = f"task:{alternate_task.identifier}@{alternate_task.revision}"
    _append_apple(database, "receipt semantics survive a checkpoint crash")
    poll = delivery.poll("apple_messages")
    delivery_info = cast(dict[str, Any], poll["delivery"])

    class InjectedCrash(RuntimeError):
        pass

    crashing = LocalSourceDelivery(
        vault,
        store_root=store,
        after_source_commit=lambda _receipt: (_ for _ in ()).throw(InjectedCrash),
    )
    with pytest.raises(InjectedCrash):
        crashing.acknowledge(
            "apple_messages",
            token=cast(str, delivery_info["token"]),
            expected_source_revision=cast(str, delivery_info["source_revision"]),
            disposition="accepted",
            result_refs=(result_ref,),
            actor_ref="codex-task:original-receipt",
            account_binding="local-account:original",
        )

    advanced = vault.update_task(
        result_task.identifier,
        expected_revision=result_task.revision,
        next_action="Advance after the source receipt was durably committed.",
        note="The old result ref remains valid only for exact crash recovery.",
    )
    assert advanced.revision != result_task.revision

    changed_attempts = (
        ("rejected", (result_ref,), "codex-task:original-receipt", "local-account:original"),
        ("accepted", (alternate_ref,), "codex-task:original-receipt", "local-account:original"),
        ("accepted", (result_ref,), "codex-task:changed-receipt", "local-account:original"),
        ("accepted", (result_ref,), "codex-task:original-receipt", "local-account:changed"),
    )
    for disposition, refs, actor_ref, account_binding in changed_attempts:
        with pytest.raises(ConflictError, match="exact prepared receipt"):
            LocalSourceDelivery(vault, store_root=store).acknowledge(
                "apple_messages",
                token=cast(str, delivery_info["token"]),
                expected_source_revision=cast(str, delivery_info["source_revision"]),
                disposition=disposition,
                result_refs=refs,
                actor_ref=actor_ref,
                account_binding=account_binding,
            )
        assert LocalSourceDelivery(vault, store_root=store).status("apple_messages")["pending"]

    recovered = LocalSourceDelivery(Vault(vault.root), store_root=store).acknowledge(
        "apple_messages",
        token=cast(str, delivery_info["token"]),
        expected_source_revision=cast(str, delivery_info["source_revision"]),
        disposition="accepted",
        result_refs=(result_ref,),
        actor_ref="codex-task:original-receipt",
        account_binding="local-account:original",
    )

    assert recovered["already_acknowledged"] is False
    assert recovered["result_refs"] == [result_ref]
    assert not LocalSourceDelivery(Vault(vault.root), store_root=store).status("apple_messages")[
        "pending"
    ]


@_POSIX_STORAGE
def test_receipt_recovery_survives_an_unrelated_later_source_commit(tmp_path: Path) -> None:
    vault, selected = _selected_vault(tmp_path, "apple_messages", "whatsapp")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "receipt survives unrelated coverage")
    poll = delivery.poll("apple_messages")

    class InjectedCrash(RuntimeError):
        pass

    def crash_after_receipt(receipt: dict[str, Any]) -> None:
        assert receipt["revision"] != selected["revision"]
        raise InjectedCrash

    with pytest.raises(InjectedCrash):
        _ack(
            LocalSourceDelivery(
                vault,
                store_root=store,
                after_source_commit=crash_after_receipt,
            ),
            poll,
        )
    after_receipt = vault.get_source_snapshot()
    unrelated = vault.record_source_observation(
        expected_revision=after_receipt.revision,
        source_id="whatsapp",
        actor_ref="another-task",
        result="failure",
        error_code="timeout",
    )

    recovered = _ack(LocalSourceDelivery(vault, store_root=store), poll)

    assert recovered["sequence"] == 1
    assert recovered["source_revision"] == after_receipt.revision
    assert recovered["source_revision"] != unrelated["revision"]
    assert not LocalSourceDelivery(vault, store_root=store).status("apple_messages")["pending"]


@_POSIX_STORAGE
def test_receipt_recovery_survives_later_same_source_observation_and_large_journal(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "receipt survives later same-source observation")
    poll = delivery.poll("apple_messages")

    class InjectedCrash(RuntimeError):
        pass

    with pytest.raises(InjectedCrash):
        _ack(
            LocalSourceDelivery(
                vault,
                store_root=store,
                after_source_commit=lambda _receipt: (_ for _ in ()).throw(InjectedCrash),
            ),
            poll,
        )
    committed = vault.get_source_snapshot()
    later = vault.record_source_observation(
        expected_revision=committed.revision,
        source_id="apple_messages",
        actor_ref="later-same-source-task",
        result="failure",
        error_code="timeout",
    )
    journal = vault.root / "journal/events.jsonl"
    filler = (json.dumps({"operation": "test.noop", "padding": "x" * 900}) + "\n").encode()
    with journal.open("ab") as stream:
        stream.write(filler * ((33 * 1024 * 1024 // len(filler)) + 1))
    assert journal.stat().st_size > 32 * 1024 * 1024

    recovered = _ack(LocalSourceDelivery(vault, store_root=store), poll)

    assert recovered["sequence"] == 1
    assert recovered["source_revision"] == committed.revision
    assert recovered["source_revision"] != later["revision"]
    assert not LocalSourceDelivery(vault, store_root=store).status("apple_messages")["pending"]


@_POSIX_STORAGE
def test_uncommitted_attempt_rebases_after_source_state_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, selected = _selected_vault(tmp_path, "apple_messages", "whatsapp")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "marker without a canonical commit")
    poll = delivery.poll("apple_messages")
    original_record = vault.record_source_observation

    class InjectedFailure(RuntimeError):
        pass

    monkeypatch.setattr(
        vault,
        "record_source_observation",
        lambda **_kwargs: (_ for _ in ()).throw(InjectedFailure),
    )
    with pytest.raises(InjectedFailure):
        _ack(delivery, poll)
    markers = list((config.data_dir() / "local-source-delivery/checkpoints/commits").glob("*.json"))
    assert markers == []

    monkeypatch.setattr(vault, "record_source_observation", original_record)
    changed = original_record(
        expected_revision=selected["revision"],
        source_id="whatsapp",
        actor_ref="another-task",
        result="failure",
        error_code="timeout",
    )
    assert changed["revision"] != poll["delivery"]["source_revision"]

    acknowledged = _ack(LocalSourceDelivery(Vault(vault.root), store_root=store), poll)
    assert acknowledged["sequence"] == 1
    assert acknowledged["source_revision"] not in {
        poll["delivery"]["source_revision"],
        changed["revision"],
    }
    assert delivery.status("apple_messages")["pending"] is False


@_POSIX_STORAGE
def test_invisible_partial_page_preserves_prefix_horizon_until_visible_continuation(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(
        database,
        "filtered system row",
        timestamp=APPLE_TEST_TIMESTAMP + 200,
        item_type=1,
    )
    _append_apple(
        database,
        "older visible body",
        timestamp=APPLE_TEST_TIMESTAMP + 100,
    )

    partial = delivery.poll("apple_messages", limit=1)

    assert partial["messages"] == []
    assert partial["complete"] is False
    first_ack = _ack(
        delivery,
        partial,
        result_refs=(_result_ref(vault, "filtered-prefix"),),
    )
    first_observation = vault.get_source_snapshot().observation("apple_messages")
    assert first_ack["sequence"] == 1
    assert first_observation is not None
    assert first_observation.completeness is not None
    assert first_observation.completeness.value == "partial"

    continuation = LocalSourceDelivery(vault, store_root=store).poll("apple_messages", limit=1)
    assert continuation["messages"][0]["body"] == "older visible body"
    assert continuation["complete"] is True
    second_ack = _ack(
        LocalSourceDelivery(vault, store_root=store),
        continuation,
        result_refs=(_result_ref(vault, "visible-continuation"),),
    )
    final_observation = vault.get_source_snapshot().observation("apple_messages")

    assert second_ack["sequence"] == 2
    assert final_observation is not None
    assert final_observation.completeness is not None
    assert final_observation.completeness.value == "complete"
    assert final_observation.covered_through == first_observation.covered_through


@_POSIX_STORAGE
def test_unrelated_source_revision_rebases_verified_delivery_without_replay(
    tmp_path: Path,
) -> None:
    vault, selected = _selected_vault(tmp_path, "apple_messages", "whatsapp")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "stale source body")
    poll = delivery.poll("apple_messages")
    before = delivery.status("apple_messages")

    changed = vault.record_source_observation(
        expected_revision=selected["revision"],
        source_id="whatsapp",
        actor_ref="another-task",
        result="failure",
        error_code="timeout",
    )
    assert changed["revision"] != poll["delivery"]["source_revision"]
    acknowledged = _ack(LocalSourceDelivery(Vault(vault.root), store_root=store), poll)
    after = LocalSourceDelivery(Vault(vault.root), store_root=store).status("apple_messages")
    snapshot = Vault(vault.root).get_source_snapshot()
    assert before["sequence"] == 0
    assert acknowledged["sequence"] == after["sequence"] == 1
    assert acknowledged["source_revision"] not in {
        poll["delivery"]["source_revision"],
        changed["revision"],
    }
    assert after["pending"] is False
    assert snapshot.observation("apple_messages") is not None
    assert snapshot.observation("whatsapp") is not None


@pytest.mark.parametrize(("mutation", "message"), [("content", "content"), ("store", "store")])
@_POSIX_STORAGE
def test_changed_content_or_store_fails_before_source_receipt(
    tmp_path: Path, mutation: str, message: str
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "prepared body")
    poll = delivery.poll("apple_messages")
    if mutation == "content":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE message SET text = 'changed body' WHERE ROWID = 2")
    else:
        _replace_apple(database, new_body="prepared body")

    with pytest.raises(ContinuityError, match=message):
        _ack(delivery, poll)
    assert delivery.status("apple_messages")["sequence"] == 0
    assert vault.get_source_snapshot().observation("apple_messages") is None


@pytest.mark.parametrize("swapped", ("authority", "checkpoints"))
@_POSIX_STORAGE
def test_checkpoint_operation_rejects_ancestor_or_checkpoint_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped: str,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store_root = tmp_path / "Messages"
    _apple_store(store_root)
    delivery = LocalSourceDelivery(vault, store_root=store_root)
    delivery.baseline("apple_messages")
    authority = config.data_dir() / "local-source-delivery"
    target = authority if swapped == "authority" else authority / "checkpoints"
    replacement = target.with_name(f"{target.name}-replacement")
    moved = target.with_name(f"{target.name}-moved")
    original_read = delivery._read_state
    swapped_once = False

    def swap_before_read(
        pinned: PinnedPathRoot,
        binding: _Binding,
        *,
        required: bool,
    ) -> tuple[_Checkpoint | None, bytes | None]:
        nonlocal swapped_once
        if not swapped_once:
            target.rename(moved)
            replacement.mkdir(mode=0o700)
            replacement.rename(target)
            swapped_once = True
        return original_read(pinned, binding, required=required)

    monkeypatch.setattr(delivery, "_read_state", swap_before_read)

    with pytest.raises(ValidationError, match="pinned local storage"):
        delivery.poll("apple_messages")
    assert list(target.glob("*.json")) == []
    assert list(moved.rglob("*.json"))


@_POSIX_STORAGE
def test_explicit_rebaseline_preserves_receipt_and_pending_history(tmp_path: Path) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "first accepted delivery")
    accepted = delivery.poll("apple_messages")
    _ack(delivery, accepted, result_refs=(_result_ref(vault, "first-delivery"),))
    _append_apple(database, "pending before replacement", timestamp=APPLE_TEST_TIMESTAMP + 1)
    pending = delivery.poll("apple_messages")
    before = delivery.status("apple_messages")
    before_bytes = _state_path().read_bytes()
    _replace_apple(database, new_body="replacement baseline")

    with pytest.raises(ContinuityError, match="store"):
        _ack(delivery, pending, result_refs=(_result_ref(vault, "pending-delivery"),))
    repaired = delivery.rebaseline(
        "apple_messages",
        expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
        expected_sequence=cast(int, before["sequence"]),
        disposition="forward_only_reset",
    )

    assert repaired["baseline_scope"] == "forward-only"
    assert repaired["messages"] is None
    assert repaired["sequence"] == cast(int, before["sequence"]) + 1
    assert repaired["already_rebaselined"] is False
    assert repaired["pending_delivery_discarded"] is True
    assert repaired["source_health"] == "needs_reproof"
    assert "source_revision" not in repaired
    status = delivery.status("apple_messages")
    assert status["pending"] is False
    assert status["source_health"] == "needs_reproof"
    assert status["last_receipt"] == before["last_receipt"]
    assert status["last_reset"]["previous_checkpoint_digest"] == before["checkpoint_digest"]
    assert status["last_reset"]["pending_delivery_discarded"] is True
    assert (
        status["last_reset"]["previous_store_identity"]
        != status["last_reset"]["replacement_store_identity"]
    )
    history = list(
        (config.data_dir() / "local-source-delivery/checkpoints/history").rglob("*.json")
    )
    assert len(history) == 1
    assert history[0].read_bytes() == before_bytes
    archived = json.loads(history[0].read_text(encoding="ascii"))
    assert archived["pending_token"] is not None
    assert archived["last_receipt"] is not None
    retry = delivery.rebaseline(
        "apple_messages",
        expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
        expected_sequence=cast(int, before["sequence"]),
        disposition="forward_only_reset",
    )
    assert retry["already_rebaselined"] is True


@_POSIX_STORAGE
def test_rebaseline_rejects_new_messages_from_the_same_store(tmp_path: Path) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "ordinary unread message")
    delivery.poll("apple_messages")
    before = delivery.status("apple_messages")

    with pytest.raises(ConflictError, match="store identity has not changed"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition="forward_only_reset",
        )

    after = delivery.status("apple_messages")
    assert after["pending"] is True
    assert after["sequence"] == before["sequence"]
    assert after["checkpoint_digest"] == before["checkpoint_digest"]
    history = config.data_dir() / "local-source-delivery/checkpoints/history"
    assert list(history.rglob("*.json")) == []


@_POSIX_STORAGE
def test_rebaseline_requires_exact_cas_and_explicit_forward_only_disposition(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    before = delivery.status("apple_messages")
    _replace_apple(database, new_body="replacement baseline")

    with pytest.raises(ConflictError, match="checkpoint changed"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=1,
            disposition="forward_only_reset",
        )
    with pytest.raises(ValidationError, match="forward_only_reset"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=0,
            disposition="accepted",
        )
    assert delivery.status("apple_messages") == before


@_POSIX_STORAGE
def test_rebaseline_crash_after_history_keeps_old_checkpoint_and_retry_completes(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    before = delivery.status("apple_messages")
    before_bytes = _state_path().read_bytes()
    _replace_apple(database, new_body="replacement baseline")

    class InjectedCrash(RuntimeError):
        pass

    crashing = LocalSourceDelivery(
        vault,
        store_root=store,
        after_rebaseline_history=lambda _receipt: (_ for _ in ()).throw(InjectedCrash),
    )
    with pytest.raises(InjectedCrash):
        crashing.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition="forward_only_reset",
        )
    assert _state_path().read_bytes() == before_bytes

    recovered = delivery.rebaseline(
        "apple_messages",
        expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
        expected_sequence=cast(int, before["sequence"]),
        disposition="forward_only_reset",
    )
    assert recovered["already_rebaselined"] is False
    assert recovered["sequence"] == cast(int, before["sequence"]) + 1


@_POSIX_STORAGE
def test_apple_checkpoint_adoption_preserves_the_exact_prefix_without_a_gap(
    tmp_path: Path,
) -> None:
    vault, selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store, old_body="covered before cutover")
    prior_cursor = apple_adapter.inspect_apple_messages(store_root=store).cursor()
    assert prior_cursor is not None
    _append_apple(database, "arrived during cutover")
    delivery = LocalSourceDelivery(vault, store_root=store)

    adopted = delivery.adopt_checkpoint(
        "apple_messages",
        prior_cursor=prior_cursor,
        expected_source_revision=cast(str, selected["revision"]),
        disposition="adopt_verified_prefix",
    )
    pending = delivery.poll("apple_messages")

    assert adopted["baseline_scope"] == "verified-prefix"
    assert adopted["messages"] is None
    assert "cursor" not in adopted
    assert [message["body"] for message in pending["messages"]] == ["arrived during cutover"]
    status = delivery.status("apple_messages")
    assert status["adoption"]["disposition"] == "adopt_verified_prefix"
    durable = _state_path().read_text(encoding="ascii")
    assert "covered before cutover" not in durable
    assert "arrived during cutover" not in durable


@_POSIX_STORAGE
def test_whatsapp_checkpoint_adoption_preserves_the_exact_prefix_without_a_gap(
    tmp_path: Path,
) -> None:
    vault, selected = _selected_vault(tmp_path, "whatsapp")
    store = tmp_path / "wacli-store"
    database = _whatsapp_store(store)
    runtime = _runtime(tmp_path)
    runner = _runner(runtime)
    _append_whatsapp(database, "covered before cutover")
    prior_cursor = whatsapp_adapter.inspect_whatsapp(
        store_root=store,
        runtime=runtime,
        runner=runner,
    ).cursor()
    assert prior_cursor is not None
    _append_whatsapp(database, "arrived during cutover")
    delivery = LocalSourceDelivery(
        vault,
        store_root=store,
        whatsapp_runtime=runtime,
        whatsapp_runner=runner,
    )

    adopted = delivery.adopt_checkpoint(
        "whatsapp",
        prior_cursor=prior_cursor,
        expected_source_revision=cast(str, selected["revision"]),
        disposition="adopt_verified_prefix",
    )
    pending = delivery.poll("whatsapp")

    assert adopted["baseline_scope"] == "verified-prefix"
    assert [message["body"] for message in pending["messages"]] == ["arrived during cutover"]
    assert delivery.status("whatsapp")["adoption"]["disposition"] == "adopt_verified_prefix"


@_POSIX_STORAGE
def test_checkpoint_adoption_rejects_unproven_prefix_store_and_cas(
    tmp_path: Path,
) -> None:
    vault, selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    prior_cursor = apple_adapter.inspect_apple_messages(store_root=store).cursor()
    assert prior_cursor is not None
    delivery = LocalSourceDelivery(vault, store_root=store)
    tampered = json.loads(prior_cursor)
    tampered["messages"] = 0

    with pytest.raises(ValidationError, match="adopt_verified_prefix"):
        delivery.adopt_checkpoint(
            "apple_messages",
            prior_cursor=prior_cursor,
            expected_source_revision=cast(str, selected["revision"]),
            disposition="accepted",
        )
    with pytest.raises(ConflictError, match="source state changed"):
        delivery.adopt_checkpoint(
            "apple_messages",
            prior_cursor=prior_cursor,
            expected_source_revision="absent",
            disposition="adopt_verified_prefix",
        )
    with pytest.raises(ContinuityError, match="prefix is not present exactly"):
        delivery.adopt_checkpoint(
            "apple_messages",
            prior_cursor=json.dumps(tampered, separators=(",", ":"), sort_keys=True),
            expected_source_revision=cast(str, selected["revision"]),
            disposition="adopt_verified_prefix",
        )
    assert delivery.status("apple_messages")["initialized"] is False

    _replace_apple(database, new_body="different store")
    with pytest.raises(ContinuityError, match="store changed"):
        delivery.adopt_checkpoint(
            "apple_messages",
            prior_cursor=prior_cursor,
            expected_source_revision=cast(str, selected["revision"]),
            disposition="adopt_verified_prefix",
        )
    assert delivery.status("apple_messages")["initialized"] is False


@_POSIX_STORAGE
def test_token_is_bound_to_exact_vault_root_host_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _selected = _selected_vault(tmp_path / "one", "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    _append_apple(database, "bound body")
    poll = delivery.poll("apple_messages")
    token = poll["delivery"]["token"]

    other, _other_selected = _selected_vault(tmp_path / "two", "apple_messages")
    with pytest.raises(ConflictError, match="another vault or host"):
        LocalSourceDelivery(other, store_root=store).acknowledge(
            "apple_messages",
            token=token,
            expected_source_revision=poll["delivery"]["source_revision"],
            disposition="accepted",
            result_refs=("task:root-proof@revision",),
            actor_ref="actor",
            account_binding="account",
        )

    monkeypatch.setattr(
        config,
        "local_host_id",
        lambda *, create=False: "ffffffff-ffff-4fff-8fff-ffffffffffff",
    )
    with pytest.raises(ConflictError, match="another vault or host"):
        _ack(delivery, poll)

    corrupted = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(ValidationError, match="token is invalid"):
        delivery.acknowledge(
            "apple_messages",
            token=corrupted,
            expected_source_revision=poll["delivery"]["source_revision"],
            disposition="accepted",
            result_refs=("task:token-proof@revision",),
            actor_ref="actor",
            account_binding="account",
        )


@_POSIX_STORAGE
def test_whatsapp_delivery_uses_external_companion_without_routing_leaks(tmp_path: Path) -> None:
    vault, _selected = _selected_vault(tmp_path, "whatsapp")
    store = tmp_path / "wacli-store"
    database = _whatsapp_store(store)
    runtime = _runtime(tmp_path)
    delivery = LocalSourceDelivery(
        vault,
        store_root=store,
        whatsapp_runtime=runtime,
        whatsapp_runner=_runner(runtime),
    )
    delivery.baseline("whatsapp")
    _append_whatsapp(database, "bounded WhatsApp body")
    poll = delivery.poll("whatsapp")
    assert poll["messages"][0]["body"] == "bounded WhatsApp body"
    result = _ack(delivery, poll)
    assert result["sequence"] == 1
    durable = _state_path().read_text(encoding="ascii") + (vault.root / "SOURCES.md").read_text(
        encoding="utf-8"
    )
    for private in (
        "bounded WhatsApp body",
        "private-route@s.whatsapp.net",
        "private-sender@s.whatsapp.net",
        "provider-message-id",
        "/private/provider/media",
        "provider-media-key",
    ):
        assert private not in durable


def _exchange(
    process: subprocess.Popen[str],
    method: str,
    request_id: int,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr else "MCP server exited"
    return cast(dict[str, Any], json.loads(line))


@_POSIX_STORAGE
def test_fresh_cli_custom_store_is_visible_to_generated_manifest_mcp_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    isolated_home = tmp_path / "home"
    _apple_store(isolated_home / "Library/Messages", old_body="wrong synthetic store")
    monkeypatch.setenv("HOME", str(isolated_home))
    environment = os.environ.copy()
    command = [
        sys.executable,
        "-m",
        "continuity_kernel",
        "--json",
        "--vault",
        str(vault.root),
        "local-source",
        "baseline",
        "--source",
        "apple_messages",
        "--store-root",
        str(store),
    ]
    baseline = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    assert json.loads(baseline.stdout)["result"]["baseline_scope"] == "forward-only"
    _append_apple(database, "fresh MCP custom-store item")

    contents, _manifest = codex_integration._marketplace_contents(
        vault.root,
        runtime=(sys.executable, ["-m", "continuity_kernel"]),
    )
    encoded_mcp = contents["plugins/gsv/.mcp.json"]
    assert isinstance(encoded_mcp, bytes)
    server = json.loads(encoded_mcp)["mcpServers"]["gsv"]
    provider_environment = environment.copy()
    for name in (
        codex_integration.GSV_DATA_DIR_ENV,
        "GSV_VAULT",
        whatsapp_adapter.SERVICE_LABEL_ENV,
    ):
        provider_environment.pop(name, None)
    provider_environment.update(server["env"])
    process = subprocess.Popen(
        [server["command"], *server["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=provider_environment,
    )
    try:
        _exchange(process, "initialize", 1)
        response = _exchange(
            process,
            "tools/call",
            2,
            {"name": "gsv_local_source_status", "arguments": {"source": "apple_messages"}},
        )
        polled = _exchange(
            process,
            "tools/call",
            3,
            {"name": "gsv_local_source_poll", "arguments": {"source": "apple_messages"}},
        )
    finally:
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    status = response["result"]["structuredContent"]
    assert status["initialized"] is True
    assert status["sequence"] == 0
    assert status["pending"] is False
    assert polled["result"]["isError"] is False
    delivery = polled["result"]["structuredContent"]
    assert delivery["delivery"]["items_observed"] == 1
    assert [message["body"] for message in delivery["messages"]] == ["fresh MCP custom-store item"]
    assert str(store) not in repr(status)
    assert str(store) not in repr(delivery)
    assert server["env"][codex_integration.GSV_DATA_DIR_ENV] == str(config.data_dir())


@_POSIX_STORAGE
def test_host_local_adapter_binding_rejects_wrong_or_replaced_store_root(
    tmp_path: Path,
) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    _apple_store(store)
    LocalSourceDelivery(vault, store_root=store).baseline("apple_messages")

    fresh = LocalSourceDelivery(vault).status("apple_messages")
    bindings = list(
        (config.data_dir() / "local-source-delivery/checkpoints/adapter-bindings").glob(
            "apple_messages-*.json"
        )
    )
    assert fresh["source_health"] == "current"
    assert str(store) not in repr(fresh)
    assert len(bindings) == 1
    assert stat.S_IMODE(bindings[0].stat().st_mode) == 0o600
    assert not bindings[0].is_relative_to(vault.root)
    binding_payload = json.loads(bindings[0].read_text(encoding="ascii"))
    assert set(binding_payload) == {
        "binding_key",
        "format_version",
        "host_digest",
        "source",
        "store_device",
        "store_inode",
        "store_root",
        "store_root_digest",
        "vault_id",
        "vault_root_digest",
    }
    assert "replacement store" not in bindings[0].read_text(encoding="ascii")

    wrong = tmp_path / "WrongMessages"
    _apple_store(wrong)
    with pytest.raises(ConflictError, match="different host-local store root"):
        LocalSourceDelivery(vault, store_root=wrong).status("apple_messages")

    store.rename(tmp_path / "Messages-before-replacement")
    _apple_store(store, old_body="replacement store")
    with pytest.raises(ConflictError, match="store root identity changed"):
        LocalSourceDelivery(vault).status("apple_messages")


@_POSIX_STORAGE
def test_raw_cursor_adoption_is_internal_and_fresh_cli_delivers_only_the_gap(
    tmp_path: Path,
) -> None:
    vault, selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store, old_body="covered before CLI cutover")
    prior_cursor = apple_adapter.inspect_apple_messages(store_root=store).cursor()
    assert prior_cursor is not None
    _append_apple(database, "gap after CLI cutover")
    environment = os.environ.copy()
    base = [
        sys.executable,
        "-m",
        "continuity_kernel",
        "--json",
        "--vault",
        str(vault.root),
        "local-source",
    ]

    rejected = subprocess.run(
        [
            *base,
            "adopt-checkpoint",
            "--source",
            "apple_messages",
            "--store-root",
            str(store),
            "--prior-cursor",
            prior_cursor,
            "--expected-source-revision",
            cast(str, selected["revision"]),
            "--disposition",
            "adopt_verified_prefix",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    adopted = LocalSourceDelivery(vault, store_root=store).adopt_checkpoint(
        "apple_messages",
        prior_cursor=prior_cursor,
        expected_source_revision=cast(str, selected["revision"]),
        disposition="adopt_verified_prefix",
    )
    polled = subprocess.run(
        [
            *base,
            "poll",
            "--source",
            "apple_messages",
            "--store-root",
            str(store),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert rejected.returncode == 2
    assert "invalid choice: 'adopt-checkpoint'" in rejected.stderr
    assert adopted["baseline_scope"] == "verified-prefix"
    messages = json.loads(polled.stdout)["result"]["messages"]
    assert [message["body"] for message in messages] == ["gap after CLI cutover"]


@_POSIX_STORAGE
def test_fresh_cli_rebaseline_requires_exact_replacement_disposition(tmp_path: Path) -> None:
    vault, _selected = _selected_vault(tmp_path, "apple_messages")
    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")
    before = delivery.status("apple_messages")
    _replace_apple(database, new_body="replacement through CLI")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault.root),
            "local-source",
            "rebaseline",
            "--source",
            "apple_messages",
            "--store-root",
            str(store),
            "--expected-checkpoint-digest",
            cast(str, before["checkpoint_digest"]),
            "--expected-sequence",
            str(before["sequence"]),
            "--disposition",
            "forward_only_reset",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=10,
    )

    result = json.loads(completed.stdout)["result"]
    assert result["source_health"] == "needs_reproof"
    assert result["sequence"] == cast(int, before["sequence"]) + 1


def test_mcp_local_source_contract_marks_poll_and_ack_as_mutating() -> None:
    tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
    assert tools["gsv_local_source_status"]["annotations"]["readOnlyHint"] is True
    assert tools["gsv_local_source_poll"]["annotations"]["readOnlyHint"] is False
    assert tools["gsv_local_source_baseline"]["annotations"]["readOnlyHint"] is False
    assert tools["gsv_local_source_staged_status"]["annotations"]["readOnlyHint"] is True
    assert tools["gsv_local_source_adopt_staged"]["annotations"]["readOnlyHint"] is False
    assert tools["gsv_local_source_rebaseline"]["annotations"]["readOnlyHint"] is False
    assert tools["gsv_local_source_acknowledge"]["annotations"]["readOnlyHint"] is False
    assert "untrusted evidence" in tools["gsv_local_source_poll"]["description"]
    assert "host-local pending token" in tools["gsv_local_source_poll"]["description"]
    assert "sends" in tools["gsv_local_source_poll"]["description"]
    assert "vault-staged" in tools["gsv_local_source_adopt_staged"]["description"]
    assert (
        "Raw cursors are never returned" in tools["gsv_local_source_staged_status"]["description"]
    )
    assert "prior_cursor" not in tools["gsv_local_source_adopt_staged"]["inputSchema"]["properties"]
    assert "needing reproof" in tools["gsv_local_source_rebaseline"]["description"]
