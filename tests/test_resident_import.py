from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import continuity_kernel.atomic as atomic_module
import continuity_kernel.update as self_update
from continuity_kernel import apple_messages, cli, resident_import
from continuity_kernel.atomic import sha256_bytes
from continuity_kernel.direction import direction_aim, direction_dict, new_direction
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError
from continuity_kernel.local_source_delivery import (
    VERIFIED_PREFIX_ADOPTION,
    LocalSourceDelivery,
)
from continuity_kernel.portfolio import new_portfolio, portfolio_dict, portfolio_item
from continuity_kernel.records import (
    EntityMergeAbsorption,
    EntityRelationship,
    TaskEntityLink,
    WorkThreadEntityLink,
    WorkThreadTaskLink,
    new_entity,
    new_task,
    new_thread,
    record_dict,
)
from continuity_kernel.resident_signals import (
    SETTLED_EVENT_KEYS_RELATIVE,
    ResidentSignalStore,
    settled_event_key_count,
)
from continuity_kernel.vault import Vault

_POSIX_IMPORT = pytest.mark.skipif(
    os.name == "nt", reason="resident import publication requires POSIX pinned storage"
)

OBSERVED = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
TIMESTAMP = "2026-07-29T10:00:00.000000Z"
SIGNAL_A = "019f0000-0000-7000-8000-000000000101"
SIGNAL_B = "019f0000-0000-7000-8000-000000000102"
ACK_A = "019f0000-0000-7000-8000-000000000111"
ARCHIVED_EVENT_KEY = "fixture:archived-import"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _json_digest(value: object) -> str:
    return sha256_bytes(_json_bytes(value))


def _drop_revision(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "revision"}


def _file(path: str, content: bytes, *, executable: bool = False) -> dict[str, Any]:
    return {
        "bytes_base64": base64.b64encode(content).decode("ascii"),
        "executable": executable,
        "path": path,
        "sha256": sha256_bytes(content),
    }


def _jsonl(values: list[dict[str, object]]) -> bytes:
    return b"".join(_json_bytes(value) for value in values)


def _signal(identifier: str) -> dict[str, object]:
    return {
        "envelope": {"summary": f"Signal {identifier[-1]} is waiting."},
        "event_key": f"fixture:{identifier}",
        "input_id": identifier,
        "kind": "task-checkpoint",
        "observed_at": TIMESTAMP,
        "ref": "task:resident-outcome",
    }


def _ack(input_id: str = SIGNAL_A) -> dict[str, object]:
    return {
        "acknowledged_at": TIMESTAMP,
        "acknowledgement_id": ACK_A,
        "consumer": "resident-mind",
        "input_id": input_id,
    }


def _private_records() -> dict[str, object]:
    owner = new_entity(
        identifier="person:resident-owner",
        title="Resident owner",
        entity_type="person",
        summary="The person whose local Mind this vault serves.",
        refs=("context:resident-owner",),
        history=(f"{TIMESTAMP} — Retained during the public cutover.",),
        observed_at=OBSERVED,
    )
    project = new_entity(
        identifier="project:resident-mind",
        title="Resident Mind",
        entity_type="project",
        summary="The explicit local continuity system.",
        refs=("context:resident-mind",),
        relationships=(
            EntityRelationship(
                predicate="serves",
                target=owner.identifier,
                status="current",
                recorded_at=TIMESTAMP,
                valid_from="2026-07-29T09:00:00.000000Z",
                valid_to=None,
                refs=("context:resident-owner",),
            ),
        ),
        merge_absorptions=(
            EntityMergeAbsorption(
                source_id="project:old-resident-mind",
                source_updated_at="2026-07-29T08:00:00.000000Z",
                merged_at="2026-07-29T09:00:00.000000Z",
            ),
        ),
        history=(f"{TIMESTAMP} — Preserved with canonical relationships.",),
        observed_at=OBSERVED,
    )
    task = new_task(
        identifier="resident-outcome",
        title="Resident outcome",
        outcome="Carry the exact outcome into public Seld.",
        status="doing",
        next_actor="agent",
        next_action="Prove fresh-process visibility.",
        rank=1,
        active_thread_id="019f0000-0000-7000-8000-000000000001",
        refs=("context:resident-mind",),
        project="Seld",
        entity_links=(TaskEntityLink(role="project", entity_id=project.identifier),),
        workspace="/synthetic/seld",
        attention_at="2026-07-29",
        due="2026-07-30",
        history=(f"{TIMESTAMP} — Continued on the public stack.",),
        observed_at=OBSERVED,
    )
    thread = new_thread(
        identifier="thread:resident-cutover",
        title="Resident cutover",
        purpose="Move to the public stack without losing context.",
        summary="The import remains staged until parity passes.",
        status="active",
        next_move="Run the isolated onboarding rehearsal.",
        focus_task_id=task.identifier,
        task_links=(WorkThreadTaskLink(position=10, task_id=task.identifier),),
        entity_links=(
            WorkThreadEntityLink(role="project", entity_id=project.identifier),
            WorkThreadEntityLink(role="owner", entity_id=owner.identifier),
        ),
        refs=("context:resident-mind",),
        closure_condition="The public stack passes parity and rollback proof.",
        next_actor="agent",
        history=(f"{TIMESTAMP} — Continued with exact task ownership.",),
        observed_at=OBSERVED,
    )
    direction = new_direction(
        status="provisional",
        current_chapter="Use one resident stack without losing continuity.",
        aims=(
            direction_aim(
                identifier="resident-parity",
                title="Resident parity",
                desired_state="The public stack retains the complete working Mind.",
            ),
        ),
        constraints=("Do not lose any authored context.",),
        tensions=("Complete the cutover without weakening rollback.",),
        refs=("context:resident-mind",),
        source_observed_at=TIMESTAMP,
        recorded_at=TIMESTAMP,
        recheck_at="2026-08-29T10:00:00.000000Z",
        history=(f"{TIMESTAMP} — Re-expressed through stable aims.",),
        observed_at=OBSERVED,
    )
    portfolio = new_portfolio(
        summary="One current outcome remains under deliberate execution.",
        direction_revision=direction.revision,
        items=(
            portfolio_item(
                task_id_value=task.identifier,
                task_revision=task.revision,
                stance="agent-can-carry",
                reason="The Mind can complete the bounded local migration.",
                work_thread_id=thread.identifier,
                work_thread_revision=thread.revision,
                direction_aim_ids=("resident-parity",),
                source_position=1,
                source_task_updated_at=task.updated_at,
                source_thread_updated_at=thread.updated_at,
            ),
        ),
        source_direction_updated_at=direction.updated_at,
        refs=("context:resident-mind",),
        source_observed_at=TIMESTAMP,
        recorded_at=TIMESTAMP,
        review_after="2026-08-05T10:00:00.000000Z",
        history=(f"{TIMESTAMP} — Preserved exact source anchors.",),
        observed_at=OBSERVED,
    )

    task_value = _drop_revision(record_dict(task))
    task_value["thread_ids"] = task_value.pop("codex_episode_ids")
    task_value["next_actor"] = "agent"

    def private_entity(value: Any) -> dict[str, Any]:
        result = _drop_revision(record_dict(value))
        result["name"] = result.pop("title")
        result["entity_kind"] = result.pop("entity_type")
        result["sources"] = result.pop("refs")
        return result

    thread_value = _drop_revision(record_dict(thread))
    thread_value["current_summary"] = thread_value.pop("summary")
    thread_value["recorded_at"] = thread_value.pop("created_at")
    thread_value["next_actor"] = "agent"
    thread_value.pop("task_ids")
    thread_value.pop("entity_ids")

    direction_value = _drop_revision(direction_dict(direction))
    direction_value["desired_outcomes"] = [item.desired_state for item in direction.aims]

    portfolio_value = portfolio_dict(portfolio)
    private_items = []
    for item in portfolio_value["items"]:
        private_items.append(
            {
                "direction_aim_ids": item["direction_aim_ids"],
                "position": item["source_position"],
                "rationale": item["reason"],
                "stance": "agent-can-carry",
                "task_id": item["task_id"],
                "task_updated_at": item["source_task_updated_at"],
                "unaligned_reason": item["unaligned_reason"],
                "work_thread_id": item["work_thread_id"],
                "work_thread_updated_at": item["source_thread_updated_at"],
            }
        )
    private_portfolio = {
        "direction_updated_at": portfolio.source_direction_updated_at,
        "format_version": 2,
        "history": list(portfolio.history),
        "items": private_items,
        "observed_at": portfolio.observed_at,
        "recorded_at": portfolio.recorded_at,
        "refs": list(portfolio.refs),
        "review_after": portfolio.review_after,
        "review_thread_id": "thread:life-portfolio-review",
        "summary": portfolio.summary,
        "updated_at": portfolio.updated_at,
    }
    return {
        "direction": direction_value,
        "entities": [private_entity(owner), private_entity(project)],
        "portfolio": private_portfolio,
        "tasks": [task_value],
        "threads": [thread_value],
    }


def _payload() -> dict[str, Any]:
    context = b"# Durable context\n\nThis authored note survives the import.\n"
    journal = b"# Resident journal\n\n2026-07-29 - The cutover was prepared.\n"
    inputs = _jsonl([_signal(SIGNAL_A), _signal(SIGNAL_B)])
    acks = _jsonl([_ack()])
    return {
        "context_files": [
            _file("context/resident/notes/durable-context.md", context),
            _file("journal/resident.md", journal),
        ],
        "documents": {
            "MIND.md": "# Mind\n\nPreserve context and act from evidence.\n",
            "NOW.md": "# Now\n\nThe public cutover is being rehearsed.\n",
        },
        "records": _private_records(),
        "selected_sources": [
            "chronicle",
            "codex",
            "gmail",
            "local-markdown",
            "qmd",
        ],
        "signal_files": [
            _file(".gsv/signals/acks.jsonl", acks),
            _file(".gsv/signals/inputs.jsonl", inputs),
        ],
    }


def _compacted_signal_files(root: Path, *, include_index: bool) -> list[dict[str, Any]]:
    root.mkdir()
    store = ResidentSignalStore(root)
    signal, created = store.append_result(
        kind="task-checkpoint",
        ref="task:resident-outcome",
        event_key=ARCHIVED_EVENT_KEY,
        envelope={"summary": "The archived import event was handled."},
        observed_at=OBSERVED,
    )
    assert created is True
    store.acknowledge(
        [signal.input_id],
        expected_revision=store.list().revision,
        consumer="resident-mind",
        acknowledged_at=OBSERVED,
    )
    compacted = store.compact(retain_recent=0, observed_at=OBSERVED)
    assert compacted.archived_signals == 1

    files = []
    for path in sorted(store.root.rglob("*.jsonl")):
        relative = path.relative_to(root).as_posix()
        if relative == SETTLED_EVENT_KEYS_RELATIVE and not include_index:
            continue
        files.append(_file(relative, path.read_bytes()))
    return files


def _manifest(
    payload: dict[str, Any] | None = None,
    *,
    schema_version: int = 1,
) -> dict[str, Any]:
    value = payload or _payload()
    records = value["records"]
    assert isinstance(records, dict)
    counts = {
        "context_files": len(value["context_files"]),
        "entities": len(records["entities"]),
        "signal_files": len(value["signal_files"]),
        "tasks": len(records["tasks"]),
        "threads": len(records["threads"]),
    }
    if schema_version == 2:
        counts["local_source_checkpoints"] = len(value["local_source_checkpoints"])
    return {
        "counts": counts,
        "payload": value,
        "payload_sha256": _json_digest(value),
        "schema_version": schema_version,
        "section_sha256": {key: _json_digest(value[key]) for key in sorted(value)},
        "source_format": "sbrain-resident-v2",
    }


def _write_export(path: Path, manifest: dict[str, Any] | None = None) -> Path:
    path.write_bytes(_json_bytes(manifest or _manifest()))
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _available_update() -> dict[str, object]:
    return {"state": "current", "transaction": None}


@_POSIX_IMPORT
def test_exact_manifest_import_preserves_rich_semantics_files_sources_and_signals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "status", _available_update)
    manifest = _manifest()
    export = _write_export(tmp_path / "resident.json", manifest)
    target = tmp_path / "public-vault"

    inspected = resident_import.inspect_resident_export(export, target)
    applied = resident_import.apply_resident_export(
        export,
        target,
        expected_plan_revision=inspected["plan_revision"],
    )

    assert applied["parity"]["matched"] is True
    assert applied["counts"]["signals_inputs"] == 2
    assert applied["counts"]["signals_acknowledged"] == 1
    assert applied["counts"]["signals_pending"] == 1
    assert applied["companions"] == ["qmd"]
    assert applied["source_zero"] == "gsv"
    assert applied["selected_sources"] == [
        "codex_activity",
        "gmail",
        "gsv",
        "local_files",
        "screen_context",
    ]
    assert {tuple(item.values()) for item in inspected["source_mapping"]} >= {
        ("chronicle", "screen_context"),
        ("codex", "codex_activity"),
    }
    assert applied["source_observations_copied"] == 0
    migrated = Vault(target)
    assert migrated.doctor().healthy
    task = migrated.get_task("resident-outcome")
    assert task.next_actor == "agent"
    assert task.project == "Seld"
    assert task.codex_episode_ids == ("019f0000-0000-7000-8000-000000000001",)
    entity = migrated.get_entity("project:resident-mind")
    assert entity.refs == ("context:resident-mind",)
    assert entity.relationships[0].target == "person:resident-owner"
    assert migrated.get_thread("thread:resident-cutover").task_links[0].position == 10
    assert migrated.get_direction().constraints == ("Do not lose any authored context.",)
    assert migrated.get_portfolio().items[0].source_position == 1
    assert migrated.get_source_snapshot().observations == ()
    assert "gsv" in migrated.get_source_snapshot().selected_sources
    payload = manifest["payload"]
    assert isinstance(payload, dict)
    for entry in [*payload["context_files"], *payload["signal_files"]]:
        assert isinstance(entry, dict)
        assert (target / entry["path"]).read_bytes() == base64.b64decode(
            entry["bytes_base64"], validate=True
        )

    signals = ResidentSignalStore(target).list()
    assert [item.input_id for item in signals.signals] == [SIGNAL_B]
    signals_store = ResidentSignalStore(target)
    signals_store.acknowledge(
        [SIGNAL_B],
        expected_revision=signals.revision,
        consumer="resident-mind",
        acknowledged_at=OBSERVED,
    )
    assert ResidentSignalStore(target).list().signals == ()

    backup = Path(migrated.create_backup(tmp_path / "public-vault.zip")["backup"])
    restored = tmp_path / "restored"
    Vault.restore_backup(backup, restored)
    assert (restored / "context/resident/notes/durable-context.md").read_bytes() == (
        target / "context/resident/notes/durable-context.md"
    ).read_bytes()


@_POSIX_IMPORT
def test_import_derives_compacted_event_index_and_suppresses_replay_in_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "status", _available_update)
    payload = _payload()
    payload["signal_files"] = _compacted_signal_files(
        tmp_path / "private-signal-export",
        include_index=False,
    )
    export = _write_export(tmp_path / "resident.json", _manifest(payload))
    target = tmp_path / "public-vault"

    inspected = resident_import.inspect_resident_export(export, target)
    applied = resident_import.apply_resident_export(
        export,
        target,
        expected_plan_revision=inspected["plan_revision"],
    )

    assert inspected["counts"]["signals_settled_event_keys"] == 1
    assert applied["parity"]["matched"] is True
    assert (target / SETTLED_EVENT_KEYS_RELATIVE).is_file()
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; from pathlib import Path; "
                "from continuity_kernel.resident_signals import ResidentSignalStore; "
                "store=ResidentSignalStore(Path(sys.argv[1])); "
                "_signal,created=store.append_result("
                "kind='task-checkpoint', ref='task:resident-outcome', "
                f"event_key={ARCHIVED_EVENT_KEY!r}, "
                "envelope={'summary':'The archived import event was handled.'}); "
                "status=store.status(); "
                "print(json.dumps({'created':created,'inputs':status.inputs,"
                "'pending':status.pending},sort_keys=True))"
            ),
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"created": False, "inputs": 0, "pending": 0}


@_POSIX_IMPORT
def test_import_stages_an_empty_replay_ledger_when_archives_have_no_event_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "status", _available_update)
    payload = _payload()
    files = _compacted_signal_files(tmp_path / "unkeyed-signal-export", include_index=False)
    position = next(
        index
        for index, item in enumerate(files)
        if str(item["path"]).startswith(".gsv/signals/archive/inputs-")
    )
    archived = json.loads(base64.b64decode(str(files[position]["bytes_base64"]), validate=True))
    archived["event_key"] = None
    files[position] = _file(str(files[position]["path"]), _jsonl([archived]))
    payload["signal_files"] = files
    export = _write_export(tmp_path / "unkeyed.json", _manifest(payload))
    target = tmp_path / "public-vault"

    inspected = resident_import.inspect_resident_export(export, target)
    applied = resident_import.apply_resident_export(
        export,
        target,
        expected_plan_revision=inspected["plan_revision"],
    )

    assert inspected["counts"]["signals_settled_event_keys"] == 0
    assert applied["parity"]["matched"] is True
    index = (target / SETTLED_EVENT_KEYS_RELATIVE).read_bytes()
    assert index
    assert settled_event_key_count(index) == 0
    assert Vault(target).doctor().healthy is True


def test_import_rejects_malformed_archive_conflicting_index_and_nearby_active_path(
    tmp_path: Path,
) -> None:
    malformed_payload = _payload()
    malformed_files = _compacted_signal_files(
        tmp_path / "malformed-source",
        include_index=False,
    )
    archive_input = next(
        index
        for index, item in enumerate(malformed_files)
        if str(item["path"]).startswith(".gsv/signals/archive/inputs-")
    )
    malformed_files[archive_input] = _file(
        str(malformed_files[archive_input]["path"]),
        _jsonl([{"provider_body": "unsupported archive shape"}]),
    )
    malformed_payload["signal_files"] = malformed_files
    malformed_export = _write_export(
        tmp_path / "malformed.json",
        _manifest(malformed_payload),
    )
    with pytest.raises(ValidationError, match="signal input has an unsupported shape"):
        resident_import.inspect_resident_export(
            malformed_export,
            tmp_path / "malformed-target",
        )

    conflict_payload = _payload()
    conflict_files = _compacted_signal_files(
        tmp_path / "conflict-source",
        include_index=True,
    )
    index_position = next(
        index
        for index, item in enumerate(conflict_files)
        if item["path"] == SETTLED_EVENT_KEYS_RELATIVE
    )
    index_entry = conflict_files[index_position]
    receipts = [
        json.loads(line)
        for line in base64.b64decode(str(index_entry["bytes_base64"]), validate=True).splitlines()
    ]
    receipts[1]["signal_digest"] = "0" * 64
    receipt_bytes = _jsonl(receipts[1:])
    receipts[0]["receipts_sha256"] = sha256_bytes(receipt_bytes)
    conflict_files[index_position] = _file(
        SETTLED_EVENT_KEYS_RELATIVE,
        _jsonl(receipts),
    )
    conflict_payload["signal_files"] = conflict_files
    conflict_export = _write_export(
        tmp_path / "conflicting-index.json",
        _manifest(conflict_payload),
    )
    with pytest.raises(ValidationError, match="settled event-key ledger differs"):
        resident_import.inspect_resident_export(
            conflict_export,
            tmp_path / "conflict-target",
        )

    nearby_payload = _payload()
    nearby_payload["signal_files"].append(_file(".gsv/signals/event-keys-copy.jsonl", b""))
    nearby_export = _write_export(tmp_path / "nearby-path.json", _manifest(nearby_payload))
    with pytest.raises(ValidationError, match="outside the import allowlist"):
        resident_import.inspect_resident_export(nearby_export, tmp_path / "nearby-target")


def _import_v2_local_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Vault, Path, Path]:
    monkeypatch.setattr(self_update, "status", _available_update)
    store = tmp_path / "Messages"
    store.mkdir()
    database = store / "chat.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE message (date INTEGER, is_from_me INTEGER, text TEXT, "
            "attributedBody BLOB, item_type INTEGER DEFAULT 0, "
            "associated_message_type INTEGER DEFAULT 0, "
            "cache_has_attachments INTEGER DEFAULT 0)"
        )
        connection.execute("INSERT INTO message VALUES (0, 0, 'already covered', NULL, 0, 0, 0)")
    prior_status = apple_messages.inspect_apple_messages(store_root=store, observed_at=OBSERVED)
    prior_cursor = prior_status.cursor()
    assert prior_cursor is not None

    payload = _payload()
    payload["selected_sources"].append("imessage")
    payload["local_source_checkpoints"] = [
        {
            "covered_through": prior_status.newest_message_at,
            "cursor": prior_cursor,
            "cursor_sha256": sha256_bytes(prior_cursor.encode("utf-8")),
            "observed_at": prior_status.observed_at,
            "source": "apple_messages",
        }
    ]
    export = _write_export(
        tmp_path / "resident-v2.json",
        _manifest(payload, schema_version=2),
    )
    target = tmp_path / "public-vault"
    inspected = resident_import.inspect_resident_export(export, target)
    applied = resident_import.apply_resident_export(
        export,
        target,
        expected_plan_revision=inspected["plan_revision"],
    )

    migration_path = target / ".gsv/migrations/local-source-checkpoints.json"
    assert applied["local_source_checkpoints_staged"] == 1
    assert applied["local_source_checkpoint_sources"] == ["apple_messages"]
    assert applied["source_zero"] == "gsv"
    assert migration_path.stat().st_mode & 0o077 == 0
    assert b"already covered" not in migration_path.read_bytes()
    migrated = Vault(target)
    assert migrated.get_source_snapshot().observations == ()
    assert "gsv" in migrated.get_source_snapshot().selected_sources
    return migrated, store, database


def test_absent_staged_local_checkpoint_status_needs_no_pinned_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "portable-vault")
    vault.initialize(name="Portable staged status")
    monkeypatch.setattr(resident_import, "PINNED_PATH_ROOT_SUPPORTED", False)
    monkeypatch.setenv("GSV_WHATSAPP_SERVICE_LABEL", "invalid label")

    status = resident_import.staged_local_source_checkpoint_status(vault)

    assert status == {
        "checkpoints": [],
        "migration_revision": resident_import.ABSENT_LOCAL_SOURCE_MIGRATION_REVISION,
        "source_revision": vault.get_source_snapshot().revision,
        "vault_id": vault.identity()["vault_id"],
    }
    assert not (vault.root / ".gsv/migrations/local-source-checkpoints.json").exists()


@_POSIX_IMPORT
@pytest.mark.parametrize("migration_directory_present", [False, True])
def test_absent_staged_local_checkpoint_status_needs_no_delivery_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration_directory_present: bool,
) -> None:
    vault = Vault(tmp_path / "portable-vault")
    vault.initialize(name="Portable staged status")
    if migration_directory_present:
        (vault.root / ".gsv/migrations").mkdir(parents=True)
    monkeypatch.setenv("GSV_WHATSAPP_SERVICE_LABEL", "invalid label")

    status = resident_import.staged_local_source_checkpoint_status(vault)

    assert status == {
        "checkpoints": [],
        "migration_revision": resident_import.ABSENT_LOCAL_SOURCE_MIGRATION_REVISION,
        "source_revision": vault.get_source_snapshot().revision,
        "vault_id": vault.identity()["vault_id"],
    }


@_POSIX_IMPORT
def test_v2_import_consumes_staged_prefix_and_recovers_after_host_adoption_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated, store, database = _import_v2_local_checkpoint(tmp_path, monkeypatch)
    delivery = LocalSourceDelivery(migrated, store_root=store)
    staged = resident_import.staged_local_source_checkpoint_status(
        migrated,
        delivery=delivery,
    )
    assert len(staged["checkpoints"]) == 1
    assert staged["checkpoints"][0]["source"] == "apple_messages"
    assert staged["checkpoints"][0]["state"] == "pending"
    assert staged["checkpoints"][0]["adopted_at"] is None
    assert "cursor" not in staged["checkpoints"][0]

    with pytest.raises(ConflictError, match="migration changed"):
        resident_import.adopt_staged_local_source_checkpoint(
            migrated,
            source="apple_messages",
            expected_migration_revision="0" * 64,
            expected_source_revision=staged["source_revision"],
            disposition=VERIFIED_PREFIX_ADOPTION,
            delivery=delivery,
        )

    changed_sources = migrated.select_sources(
        expected_revision=staged["source_revision"],
        sources=migrated.get_source_snapshot().selected_sources,
    )
    with pytest.raises(ConflictError, match="source state changed"):
        resident_import.adopt_staged_local_source_checkpoint(
            migrated,
            source="apple_messages",
            expected_migration_revision=staged["migration_revision"],
            expected_source_revision=staged["source_revision"],
            disposition=VERIFIED_PREFIX_ADOPTION,
            delivery=delivery,
        )
    staged = resident_import.staged_local_source_checkpoint_status(
        migrated,
        delivery=delivery,
    )
    assert staged["source_revision"] == changed_sources["revision"]

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message VALUES (800000000, 0, 'cutover gap', NULL, 0, 0, 0)"
        )

    class InjectedCrash(RuntimeError):
        pass

    with pytest.raises(InjectedCrash):
        resident_import.adopt_staged_local_source_checkpoint(
            migrated,
            source="apple_messages",
            expected_migration_revision=staged["migration_revision"],
            expected_source_revision=staged["source_revision"],
            disposition=VERIFIED_PREFIX_ADOPTION,
            delivery=delivery,
            _after_host_adoption=lambda _result: (_ for _ in ()).throw(InjectedCrash),
        )

    restarted_vault = Vault(migrated.root)
    restarted_delivery = LocalSourceDelivery(restarted_vault, store_root=store)
    interrupted = resident_import.staged_local_source_checkpoint_status(
        restarted_vault,
        delivery=restarted_delivery,
    )
    assert interrupted["checkpoints"][0]["state"] == "completion_pending"
    adoption = resident_import.adopt_staged_local_source_checkpoint(
        restarted_vault,
        source="apple_messages",
        expected_migration_revision=interrupted["migration_revision"],
        expected_source_revision=interrupted["source_revision"],
        disposition=VERIFIED_PREFIX_ADOPTION,
        delivery=restarted_delivery,
    )
    assert adoption["baseline_scope"] == "verified-prefix"
    assert adoption["already_adopted"] is True
    completed = resident_import.staged_local_source_checkpoint_status(
        Vault(migrated.root),
        delivery=LocalSourceDelivery(Vault(migrated.root), store_root=store),
    )
    assert completed["checkpoints"][0]["state"] == "adopted"
    assert completed["migration_revision"] == adoption["migration_revision"]

    poll = LocalSourceDelivery(Vault(migrated.root), store_root=store).poll("apple_messages")
    assert [message["body"] for message in poll["messages"]] == ["cutover gap"]

    migration_path = migrated.root / ".gsv/migrations/local-source-checkpoints.json"
    backup = Path(migrated.create_backup(tmp_path / "public-vault-v2.zip")["backup"])
    restored = tmp_path / "restored-v2"
    Vault.restore_backup(backup, restored)
    assert (
        restored / ".gsv/migrations/local-source-checkpoints.json"
    ).read_bytes() == migration_path.read_bytes()
    restored_status = resident_import.staged_local_source_checkpoint_status(
        Vault(restored),
        delivery=LocalSourceDelivery(Vault(restored), store_root=store),
    )
    assert restored_status["checkpoints"][0]["state"] == "needs_reproof"


@_POSIX_IMPORT
def test_staged_local_checkpoint_rejects_store_and_vault_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated, store, database = _import_v2_local_checkpoint(tmp_path, monkeypatch)
    staged = resident_import.staged_local_source_checkpoint_status(
        migrated,
        delivery=LocalSourceDelivery(migrated, store_root=store),
    )

    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(replacement) as connection:
        connection.execute(
            "CREATE TABLE message (date INTEGER, is_from_me INTEGER, text TEXT, "
            "attributedBody BLOB, item_type INTEGER DEFAULT 0, "
            "associated_message_type INTEGER DEFAULT 0, "
            "cache_has_attachments INTEGER DEFAULT 0)"
        )
        connection.execute(
            "INSERT INTO message VALUES (0, 0, 'same aggregate, new store', NULL, 0, 0, 0)"
        )
    os.replace(replacement, database)
    with pytest.raises((ConflictError, ContinuityError), match=r"store changed|checkpoint"):
        resident_import.adopt_staged_local_source_checkpoint(
            migrated,
            source="apple_messages",
            expected_migration_revision=staged["migration_revision"],
            expected_source_revision=staged["source_revision"],
            disposition=VERIFIED_PREFIX_ADOPTION,
            delivery=LocalSourceDelivery(migrated, store_root=store),
        )

    foreign = Vault(tmp_path / "foreign")
    foreign.initialize(name="Foreign")
    foreign.select_sources(
        expected_revision="absent",
        sources=("apple_messages", "gsv"),
    )
    foreign_migration = foreign.root / ".gsv/migrations/local-source-checkpoints.json"
    foreign_migration.parent.mkdir(parents=True, exist_ok=True)
    foreign_migration.write_bytes(
        (migrated.root / ".gsv/migrations/local-source-checkpoints.json").read_bytes()
    )
    if os.name != "nt":
        foreign_migration.chmod(0o600)
    with pytest.raises(ConflictError, match="another vault"):
        resident_import.staged_local_source_checkpoint_status(foreign)


def test_inspection_is_read_only_and_stale_plan_fails_before_parent_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "status", _available_update)
    export = _write_export(tmp_path / "resident.json")
    target = tmp_path / "missing" / "vault"
    inspected = resident_import.inspect_resident_export(export, target)
    assert not target.parent.exists()
    manifest = _manifest()
    payload = manifest["payload"]
    assert isinstance(payload, dict)
    documents = payload["documents"]
    assert isinstance(documents, dict)
    documents["NOW.md"] += "Changed.\n"
    _write_export(export, _manifest(payload))

    with pytest.raises(ConflictError, match="plan changed"):
        resident_import.apply_resident_export(
            export,
            target,
            expected_plan_revision=inspected["plan_revision"],
        )
    assert not target.parent.exists()


@pytest.mark.parametrize("occupied", ["file", "directory", "symlink"])
def test_import_requires_strictly_absent_target(tmp_path: Path, occupied: str) -> None:
    export = _write_export(tmp_path / "resident.json")
    target = tmp_path / "target"
    if occupied == "file":
        target.write_text("keep", encoding="utf-8")
    elif occupied == "directory":
        target.mkdir()
    else:
        if os.name == "nt":
            pytest.skip("symlink setup requires elevated Windows privileges")
        outside = tmp_path / "outside"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConflictError, match="must be absent"):
        resident_import.inspect_resident_export(export, target)


def test_manifest_payload_section_and_count_hashes_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["counts"]["tasks"] = 99
    export = _write_export(tmp_path / "count.json", manifest)
    with pytest.raises(ValidationError, match="counts do not match"):
        resident_import.inspect_resident_export(export, tmp_path / "target-a")

    manifest = _manifest()
    manifest["payload_sha256"] = "0" * 64
    export = _write_export(tmp_path / "payload.json", manifest)
    with pytest.raises(ValidationError, match="payload hash"):
        resident_import.inspect_resident_export(export, tmp_path / "target-b")

    manifest = _manifest()
    manifest["section_sha256"]["records"] = "0" * 64
    export = _write_export(tmp_path / "section.json", manifest)
    with pytest.raises(ValidationError, match="records hash"):
        resident_import.inspect_resident_export(export, tmp_path / "target-c")


def test_unknown_source_and_private_shape_fail_closed(tmp_path: Path) -> None:
    payload = _payload()
    payload["selected_sources"].append("unknown-provider")
    export = _write_export(tmp_path / "source.json", _manifest(payload))
    with pytest.raises(ValidationError, match="no public semantic mapping"):
        resident_import.inspect_resident_export(export, tmp_path / "target-a")

    payload = _payload()
    payload["records"]["tasks"][0]["provider_session"] = "must-not-copy"
    export = _write_export(tmp_path / "shape.json", _manifest(payload))
    with pytest.raises(ValidationError, match="unsupported shape"):
        resident_import.inspect_resident_export(export, tmp_path / "target-b")


def test_machine_local_task_bindings_cannot_travel_as_resident_context(tmp_path: Path) -> None:
    for name in ("PULSE", "RESIDENT"):
        payload = _payload()
        payload["context_files"].append(
            _file(f"context/resident/control/{name}", b"019f0000-0000-7000-8000-000000000001\n")
        )
        export = _write_export(tmp_path / f"{name.lower()}.json", _manifest(payload))

        with pytest.raises(ValidationError, match="machine-local task bindings"):
            resident_import.inspect_resident_export(export, tmp_path / f"target-{name.lower()}")


def test_signal_ack_targets_paths_hashes_and_modes_are_validated(tmp_path: Path) -> None:
    payload = _payload()
    payload["signal_files"][0] = _file(
        ".gsv/signals/acks.jsonl", _jsonl([_ack("019f0000-0000-7000-8000-000000000999")])
    )
    export = _write_export(tmp_path / "ack.json", _manifest(payload))
    with pytest.raises(ValidationError, match="unknown signal"):
        resident_import.inspect_resident_export(export, tmp_path / "target-a")

    payload = _payload()
    payload["signal_files"][0]["path"] = "../escape.jsonl"
    export = _write_export(tmp_path / "path.json", _manifest(payload))
    with pytest.raises(ValidationError, match=r"not portable|unsafe|allowlist"):
        resident_import.inspect_resident_export(export, tmp_path / "target-b")

    payload = _payload()
    payload["signal_files"][0]["executable"] = True
    export = _write_export(tmp_path / "mode.json", _manifest(payload))
    with pytest.raises(ValidationError, match="cannot be executable"):
        resident_import.inspect_resident_export(export, tmp_path / "target-c")

    payload = _payload()
    payload["signal_files"][0]["sha256"] = "0" * 64
    export = _write_export(tmp_path / "hash.json", _manifest(payload))
    with pytest.raises(ValidationError, match="hash does not match"):
        resident_import.inspect_resident_export(export, tmp_path / "target-d")


def test_signal_queue_uses_its_own_larger_file_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = _jsonl([_signal(SIGNAL_A)])
    assert len(encoded) > 32
    monkeypatch.setattr(resident_import, "MAX_PORTABLE_FILE_BYTES", 32)
    monkeypatch.setattr(resident_import, "MAX_SIGNAL_FILE_BYTES", 1_024)
    signal = _file(".gsv/signals/inputs.jsonl", encoded)

    imported = resident_import._portable_files([signal], kind="signal")
    assert imported[0].content == encoded
    with pytest.raises(ValidationError, match="size bound"):
        resident_import._portable_files(
            [_file("context/resident/example.md", encoded)],
            kind="context",
        )


def test_root_export_and_portable_file_counts_fail_closed_at_their_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    export = _write_export(tmp_path / "bounded.json", _manifest(payload))
    monkeypatch.setattr(resident_import, "MAX_EXPORT_BYTES", export.stat().st_size - 1)

    with pytest.raises(ValidationError, match="resident export exceeds its size bound"):
        resident_import.inspect_resident_export(export, tmp_path / "bounded-target")

    monkeypatch.setattr(resident_import, "MAX_PORTABLE_FILES", 1)
    with pytest.raises(ValidationError, match="context export has too many files"):
        resident_import._portable_files(payload["context_files"], kind="context")
    with pytest.raises(ValidationError, match="signal export has too many files"):
        resident_import._portable_files(payload["signal_files"], kind="signal")


def test_binary_context_is_retained_inert_but_concrete_secret_signatures_block_import(
    tmp_path: Path,
) -> None:
    binary = b"\x89PNG\r\n\x1a\n\x00\xff\x80"
    binary_payload = _payload()
    binary_payload["context_files"].append(_file("context/resident/assets/example.png", binary))
    binary_export = _write_export(tmp_path / "binary.json", _manifest(binary_payload))
    inspected = resident_import.inspect_resident_export(binary_export, tmp_path / "binary-target")
    assert inspected["apply_ready"] is True

    placeholder_payload = _payload()
    placeholder_payload["context_files"].append(
        _file(
            "context/resident/skills/example/reference.md",
            b"Configure the client with `api_key: $POSTHOG_API_KEY`.\n",
        )
    )
    placeholder_export = _write_export(
        tmp_path / "placeholder.json",
        _manifest(placeholder_payload),
    )
    assert (
        resident_import.inspect_resident_export(
            placeholder_export,
            tmp_path / "placeholder-target",
        )["apply_ready"]
        is True
    )

    ambiguous_payload = _payload()
    ambiguous_payload["context_files"].append(
        _file(
            "context/resident/skills/example/ambiguous.md",
            b"Configure the client with `api_key: POSTHOG_API_KEY`.\n",
        )
    )
    ambiguous_export = _write_export(
        tmp_path / "ambiguous-placeholder.json",
        _manifest(ambiguous_payload),
    )
    with pytest.raises(ValidationError, match="quarantined before import"):
        resident_import.inspect_resident_export(
            ambiguous_export,
            tmp_path / "ambiguous-placeholder-target",
        )

    payload = _payload()
    flagged = b"api_key=gh" + b"p_abcdefghijklmnopqrstuvwxyz0123456789\n"
    payload["context_files"].append(_file("context/resident/notes/ordinary-note.md", flagged))
    export = _write_export(tmp_path / "secret-context.json", _manifest(payload))
    with pytest.raises(ValidationError, match="quarantined before import"):
        resident_import.inspect_resident_export(export, tmp_path / "context-target")

    token_payload = _payload()
    token_payload["context_files"].append(
        _file(
            "context/resident/notes/ordinary-token-note.md",
            b"sk-" + b"proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789\n",
        )
    )
    token_export = _write_export(
        tmp_path / "openai-token.json",
        _manifest(token_payload),
    )
    with pytest.raises(ValidationError, match=r"quarantined before import.*openai-token"):
        resident_import.inspect_resident_export(token_export, tmp_path / "token-target")

    pending_payload = _payload()
    pending_payload["signal_files"][1] = _file(
        ".gsv/signals/inputs.jsonl",
        _jsonl(
            [
                {
                    **_signal(SIGNAL_A),
                    "envelope": {"summary": "postgres://user:secret@db:5432/app"},
                },
                _signal(SIGNAL_B),
            ]
        ),
    )
    pending_export = _write_export(
        tmp_path / "secret-pending.json",
        _manifest(pending_payload),
    )
    with pytest.raises(ValidationError, match="quarantined before import"):
        resident_import.inspect_resident_export(pending_export, tmp_path / "pending-target")


def test_direction_redundancy_and_review_thread_are_validated(tmp_path: Path) -> None:
    payload = _payload()
    payload["records"]["direction"]["desired_outcomes"] = ["Different meaning"]
    export = _write_export(tmp_path / "direction.json", _manifest(payload))
    with pytest.raises(ValidationError, match="do not equal"):
        resident_import.inspect_resident_export(export, tmp_path / "target-a")

    payload = _payload()
    payload["records"]["portfolio"]["review_thread_id"] = "thread:other"
    export = _write_export(tmp_path / "portfolio.json", _manifest(payload))
    with pytest.raises(ValidationError, match="thread:life-portfolio-review"):
        resident_import.inspect_resident_export(export, tmp_path / "target-b")


@_POSIX_IMPORT
def test_apply_refuses_update_before_staging_and_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export = _write_export(tmp_path / "resident.json")
    target = tmp_path / "target"
    inspected = resident_import.inspect_resident_export(export, target)
    monkeypatch.setattr(
        self_update,
        "status",
        lambda: {"state": "interrupted", "transaction": {"phase": "candidate_installed"}},
    )
    with pytest.raises(ConflictError, match="self-update"):
        resident_import.apply_resident_export(
            export, target, expected_plan_revision=inspected["plan_revision"]
        )
    assert list(tmp_path.glob(".target.tmp-resident-import-*")) == []

    states = iter(
        [
            {"state": "current", "transaction": None},
            {"state": "interrupted", "transaction": {"phase": "candidate_installed"}},
        ]
    )
    monkeypatch.setattr(self_update, "status", lambda: next(states))
    with pytest.raises(ConflictError, match="self-update"):
        resident_import.apply_resident_export(
            export, target, expected_plan_revision=inspected["plan_revision"]
        )
    stages = list(tmp_path.glob(".target.tmp-resident-import-*"))
    assert len(stages) == 1
    assert Vault(stages[0]).doctor().healthy


@_POSIX_IMPORT
def test_publish_race_never_replaces_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "status", _available_update)
    export = _write_export(tmp_path / "resident.json")
    target = tmp_path / "target"
    inspected = resident_import.inspect_resident_export(export, target)

    actual_publish = atomic_module.PinnedPathRoot.publish_directory_no_replace_if_exact

    def competing_publish(
        store: atomic_module.PinnedPathRoot,
        source: Path | str,
        destination: Path | str,
        *,
        expected_identity: tuple[int, int],
        label: str,
    ) -> None:
        target.mkdir()
        (target / "keep.txt").write_text("competitor", encoding="utf-8")
        actual_publish(
            store,
            source,
            destination,
            expected_identity=expected_identity,
            label=label,
        )

    monkeypatch.setattr(
        atomic_module.PinnedPathRoot,
        "publish_directory_no_replace_if_exact",
        competing_publish,
    )
    with pytest.raises(ConflictError, match="appeared"):
        resident_import.apply_resident_export(
            export, target, expected_plan_revision=inspected["plan_revision"]
        )
    assert (target / "keep.txt").read_text(encoding="utf-8") == "competitor"


@_POSIX_IMPORT
def test_publish_parent_swap_preserves_verified_stage_and_foreign_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(self_update, "status", _available_update)
    export = _write_export(tmp_path / "resident.json")
    target = tmp_path / "publish-parent" / "target"
    inspected = resident_import.inspect_resident_export(export, target)
    detached_parent = tmp_path / "detached-publish-parent"
    actual_move = atomic_module._move_no_replace_at
    swapped_stage_name: list[str] = []

    def swap_parent_at_publish(
        source_parent: int,
        source_name: str,
        target_parent: int,
        target_name: str,
    ) -> None:
        if not swapped_stage_name and target_name == target.name:
            target.parent.rename(detached_parent)
            target.parent.mkdir()
            foreign_stage = target.parent / source_name
            foreign_stage.mkdir()
            (foreign_stage / "keep.txt").write_text("foreign stage", encoding="utf-8")
            swapped_stage_name.append(source_name)
        actual_move(source_parent, source_name, target_parent, target_name)

    monkeypatch.setattr(atomic_module, "_move_no_replace_at", swap_parent_at_publish)
    with pytest.raises(ContinuityError, match="publication"):
        resident_import.apply_resident_export(
            export,
            target,
            expected_plan_revision=inspected["plan_revision"],
        )

    assert len(swapped_stage_name) == 1
    stage_name = swapped_stage_name[0]
    foreign_stage = target.parent / stage_name
    restored_stage = detached_parent / stage_name
    assert (foreign_stage / "keep.txt").read_text(encoding="utf-8") == "foreign stage"
    assert not target.exists()
    assert not (detached_parent / target.name).exists()
    assert Vault(restored_stage).doctor().healthy


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode has no Windows equivalent")
def test_import_requires_owner_only_export(tmp_path: Path) -> None:
    export = _write_export(tmp_path / "resident.json")
    export.chmod(0o644)
    with pytest.raises(ValidationError, match="owner-only"):
        resident_import.inspect_resident_export(export, tmp_path / "target")


@_POSIX_IMPORT
def test_cli_migration_round_trip_needs_no_existing_config_or_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(self_update, "status", _available_update)
    export = _write_export(tmp_path / "resident.json")
    target = tmp_path / "imported"

    assert cli.main(["--json", "migration", "inspect", str(export), str(target)]) == 0
    inspected = json.loads(capsys.readouterr().out)["result"]
    assert target.exists() is False

    assert (
        cli.main(
            [
                "--json",
                "migration",
                "apply",
                str(export),
                str(target),
                "--expected-plan-revision",
                inspected["plan_revision"],
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)["result"]

    assert applied["published"] is True
    assert applied["parity"]["matched"] is True
    assert Vault(target).doctor().healthy is True
