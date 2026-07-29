"""Strict, offline import of one private resident-Mind export into public Seld."""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from tempfile import mkdtemp as _tempfile_mkdtemp
from typing import Any, Final

import continuity_kernel.update as self_update
from continuity_kernel.atomic import (
    PINNED_PATH_ROOT_SUPPORTED,
    PinnedPathRoot,
    atomic_write,
    read_regular_file,
    sha256_bytes,
)
from continuity_kernel.direction import Direction, DirectionAim, parse_direction, render_direction
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    DegradedIntegrityError,
    MutationCommittedError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.local_source_delivery import (
    VERIFIED_PREFIX_ADOPTION,
    LocalSourceDelivery,
)
from continuity_kernel.portfolio import (
    Portfolio,
    PortfolioItem,
    PortfolioStance,
    parse_portfolio,
    render_portfolio,
)
from continuity_kernel.privacy import AwarenessDecision, screen_local_content
from continuity_kernel.records import (
    REVIEW_WORK_THREAD_ID,
    WINDOWS_RESERVED_NAMES,
    Actor,
    Entity,
    Task,
    WorkThread,
    entity_merge_absorptions_from_json,
    entity_relationships_from_json,
    parse_entity,
    parse_task,
    parse_thread,
    record_dict,
    render_entity,
    render_task,
    render_thread,
    task_entity_links_from_json,
    thread_entity_links_from_json,
    thread_task_links_from_json,
)
from continuity_kernel.resident_signals import (
    SETTLED_EVENT_KEYS_RELATIVE,
    ResidentSignalStore,
    build_settled_event_index,
    settled_event_key_count,
)
from continuity_kernel.source_state import (
    empty_source_snapshot,
    render_source_snapshot,
    select_sources,
)
from continuity_kernel.vault import Vault, doctor_dict
from continuity_kernel.vault_identity import parse_vault_manifest

EXPORT_KIND: Final = "sbrain-resident-v2"
EXPORT_VERSIONS: Final = frozenset({1, 2})
IMPORTER_VERSION: Final = 4
MAX_EXPORT_BYTES: Final = 128 * 1024 * 1024
MAX_PORTABLE_FILES: Final = 2_000
MAX_PORTABLE_FILE_BYTES: Final = 8 * 1024 * 1024
MAX_SIGNAL_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_DOCUMENT_BYTES: Final = 512 * 1024
_SHA256: Final = frozenset("0123456789abcdef")
_ROOT_KEYS: Final = frozenset(
    {"counts", "payload", "payload_sha256", "schema_version", "section_sha256", "source_format"}
)
_COUNT_KEYS_V1: Final = frozenset({"context_files", "entities", "signal_files", "tasks", "threads"})
_PAYLOAD_KEYS_V1: Final = frozenset(
    {"context_files", "documents", "records", "selected_sources", "signal_files"}
)
_COUNT_KEYS_V2: Final = _COUNT_KEYS_V1 | {"local_source_checkpoints"}
_PAYLOAD_KEYS_V2: Final = _PAYLOAD_KEYS_V1 | {"local_source_checkpoints"}
_RECORD_KEYS: Final = frozenset({"direction", "entities", "portfolio", "tasks", "threads"})
_DOCUMENT_KEYS: Final = frozenset({"MIND.md", "NOW.md"})
_FILE_KEYS: Final = frozenset({"bytes_base64", "executable", "path", "sha256"})
_LOCAL_SOURCE_CHECKPOINT_KEYS: Final = frozenset(
    {"covered_through", "cursor", "cursor_sha256", "observed_at", "source"}
)
_LOCAL_CURSOR_KEYS: Final = frozenset(
    {"version", "schema", "generation", "messages", "chats", "rowid", "newest"}
)
_LOCAL_SOURCE_IDS: Final = frozenset({"apple_messages", "whatsapp"})
_LOCAL_SOURCE_MIGRATION_PATH: Final = Path(".gsv/migrations/local-source-checkpoints.json")
_LOCAL_SOURCE_MIGRATION_DIRECTORY: Final = _LOCAL_SOURCE_MIGRATION_PATH.parent
_LOCAL_SOURCE_MIGRATION_LOCK: Final = Path(".gsv/locks/local-source-migration.lock")
_LOCAL_SOURCE_MIGRATION_MAX_BYTES: Final = 32 * 1024
_LOCAL_SOURCE_MIGRATION_VERSION: Final = 2
ABSENT_LOCAL_SOURCE_MIGRATION_REVISION: Final = "absent"
_LOCAL_SOURCE_MIGRATION_ROOT_KEYS: Final = frozenset({"checkpoints", "format_version", "vault_id"})
_LOCAL_SOURCE_MIGRATION_CHECKPOINT_KEYS: Final = _LOCAL_SOURCE_CHECKPOINT_KEYS | frozenset(
    {"adoption", "state"}
)
_LOCAL_SOURCE_MIGRATION_ADOPTION_KEYS: Final = frozenset(
    {"adopted_at", "checkpoint_digest", "disposition", "source_revision"}
)
_LOCAL_SOURCE_MIGRATION_STATES: Final = frozenset({"adopted", "pending"})
_FINGERPRINT_20: Final = frozenset("0123456789abcdef")
_TASK_KEYS: Final = frozenset(
    {
        "active_thread_id",
        "attention_at",
        "created_at",
        "due",
        "entity_links",
        "history",
        "identifier",
        "next_action",
        "next_actor",
        "outcome",
        "project",
        "rank",
        "refs",
        "state_changed_at",
        "status",
        "superseded_by",
        "thread_ids",
        "title",
        "updated_at",
        "waiting_on",
        "workspace",
    }
)
_ENTITY_KEYS: Final = frozenset(
    {
        "aliases",
        "created_at",
        "entity_kind",
        "history",
        "identifier",
        "merge_absorptions",
        "merged_at",
        "merged_from_updated_at",
        "merged_into",
        "name",
        "observed_at",
        "recheck_at",
        "relationships",
        "sources",
        "status",
        "summary",
        "updated_at",
    }
)
_THREAD_KEYS: Final = frozenset(
    {
        "closure_condition",
        "current_summary",
        "entity_links",
        "focus_task_id",
        "history",
        "identifier",
        "next_actor",
        "next_move",
        "observed_at",
        "purpose",
        "recheck_at",
        "recorded_at",
        "refs",
        "resolved_at",
        "state_changed_at",
        "status",
        "superseded_by",
        "task_links",
        "title",
        "updated_at",
        "waiting_on",
    }
)
_DIRECTION_KEYS: Final = frozenset(
    {
        "aims",
        "constraints",
        "current_chapter",
        "desired_outcomes",
        "format_version",
        "history",
        "observed_at",
        "recheck_at",
        "recorded_at",
        "refs",
        "status",
        "tensions",
        "updated_at",
    }
)
_PORTFOLIO_KEYS: Final = frozenset(
    {
        "direction_updated_at",
        "format_version",
        "history",
        "items",
        "observed_at",
        "recorded_at",
        "refs",
        "review_after",
        "review_thread_id",
        "summary",
        "updated_at",
    }
)
_PORTFOLIO_ITEM_KEYS: Final = frozenset(
    {
        "direction_aim_ids",
        "position",
        "rationale",
        "stance",
        "task_id",
        "task_updated_at",
        "unaligned_reason",
        "work_thread_id",
        "work_thread_updated_at",
    }
)
_SOURCE_ALIASES: Final = {
    "chronicle": "screen_context",
    "codex": "codex_activity",
    "github": "github",
    "gmail": "gmail",
    "imessage": "apple_messages",
    "local-markdown": "local_files",
    "microsoft-teams": "teams",
    "outlook-calendar": "outlook_calendar",
    "outlook-mail": "outlook_mail",
    "slack": "slack",
    "teams": "teams",
    "whatsapp": "whatsapp",
}
_SOURCE_COMPANIONS: Final = frozenset({"qmd"})
_ACTOR_ALIASES: Final[dict[str, Actor]] = {
    "agent": "agent",
    "external": "external",
    "human": "human",
}
_STANCE_ALIASES: Final[dict[str, PortfolioStance]] = {
    "agent-can-carry": "agent-can-carry",
    "keep-in-view": "keep-in-view",
    "needs-human": "needs-human",
    "reconsider": "reconsider",
}


@dataclass(frozen=True)
class _PortableFile:
    path: str
    content: bytes
    executable: bool
    sha256: str


@dataclass(frozen=True)
class _SignalCounts:
    inputs: int
    acknowledged: int
    pending: int
    archive_files: int


@dataclass(frozen=True)
class _LocalSourceCheckpoint:
    source: str
    cursor: str
    cursor_sha256: str
    observed_at: str
    covered_through: str | None


@dataclass(frozen=True)
class _LocalSourceMigrationAdoption:
    checkpoint_digest: str
    disposition: str
    source_revision: str
    adopted_at: str


@dataclass(frozen=True)
class _LocalSourceMigrationCheckpoint:
    checkpoint: _LocalSourceCheckpoint
    state: str
    adoption: _LocalSourceMigrationAdoption | None


@dataclass(frozen=True)
class _LocalSourceMigration:
    vault_id: str
    checkpoints: tuple[_LocalSourceMigrationCheckpoint, ...]


@dataclass(frozen=True)
class _ResidentExport:
    source_sha256: str
    payload_sha256: str
    mind: str
    now: str
    tasks: tuple[Task, ...]
    entities: tuple[Entity, ...]
    threads: tuple[WorkThread, ...]
    direction: Direction
    portfolio: Portfolio
    original_sources: tuple[str, ...]
    selected_sources: tuple[str, ...]
    source_mapping: tuple[tuple[str, str], ...]
    companions: tuple[str, ...]
    context_files: tuple[_PortableFile, ...]
    signal_files: tuple[_PortableFile, ...]
    settled_event_keys: bytes
    signal_counts: _SignalCounts
    local_source_checkpoints: tuple[_LocalSourceCheckpoint, ...]
    observed_at: datetime
    manifest_counts: dict[str, int]
    retained_screen_warnings: int


def inspect_resident_export(export_path: Path, target: Path) -> dict[str, Any]:
    """Validate one exact owner-only export and return a content-free plan."""

    source = _load_export(export_path)
    destination = _absent_target(target)
    return _inspection(source, destination)


def apply_resident_export(
    export_path: Path,
    target: Path,
    *,
    expected_plan_revision: str,
) -> dict[str, Any]:
    """Stage, prove, and no-replace publish one exact export into an absent target."""

    source = _load_export(export_path)
    destination = _absent_target(target)
    inspection = _inspection(source, destination)
    if inspection["plan_revision"] != expected_plan_revision:
        raise ConflictError(
            "resident import plan changed; inspect the exact export and target again"
        )
    _require_no_update_transaction()
    destination = _absent_target(destination, prepare_parent=True)
    try:
        stage = Path(
            _tempfile_mkdtemp(
                prefix=f".{destination.name}.tmp-resident-import-",
                dir=destination.parent,
            )
        )
    except OSError as exc:
        raise PersistenceError(
            f"could not allocate import stage beside {destination}: {exc}"
        ) from exc
    if os.name != "nt":
        stage.chmod(0o700)
    stage_identity = _directory_identity(stage)
    expected_vault_id: str | None = None
    expected_digest: str | None = None
    expected_parity: dict[str, Any] | None = None
    try:
        staged = Vault(stage)
        staged.initialize(name="Seld")
        _write_stage(staged, source)
        doctor = doctor_dict(staged.doctor())
        if doctor["healthy"] is not True:
            issue = doctor["issues"][0] if doctor["issues"] else {"message": "unknown issue"}
            raise ValidationError("resident import stage failed doctor: " + str(issue["message"]))
        expected_parity = _parity(source, staged)
        if expected_parity["matched"] is not True:
            raise ValidationError("resident import stage did not preserve the complete export")
        expected_vault_id = staged.identity()["vault_id"]
        expected_digest = staged.logical_digest()
        _require_no_update_transaction()
        _absent_target(destination)
        try:
            _publish_import_stage(
                stage,
                destination,
                expected_identity=stage_identity,
            )
        except ConflictError as exc:
            if "target appeared" in str(exc):
                raise ConflictError(
                    f"resident import target appeared and was left unchanged: {destination}"
                ) from exc
            raise
        except OSError as exc:
            _raise_publish_error(
                stage=stage,
                target=destination,
                stage_identity=stage_identity,
                vault_id=expected_vault_id,
                digest=expected_digest,
                cause=exc,
            )
    except (ConflictError, MutationCommittedError, DegradedIntegrityError):
        raise
    except Exception as exc:
        if _same_directory(stage, stage_identity):
            raise PersistenceError(
                f"resident import staging failed: {exc}; unpublished stage retained at {stage}"
            ) from exc
        raise DegradedIntegrityError(
            f"resident import stage identity changed at {stage}; inspect before retrying"
        ) from exc

    assert expected_vault_id is not None
    assert expected_digest is not None
    assert expected_parity is not None
    published = Vault(destination)
    if not _published_matches(
        destination,
        identity=stage_identity,
        vault_id=expected_vault_id,
        digest=expected_digest,
    ):
        raise DegradedIntegrityError(
            f"resident import target changed during readback: {destination}"
        )
    readback_parity = _parity(source, published)
    if readback_parity != expected_parity or readback_parity["matched"] is not True:
        raise DegradedIntegrityError(
            f"resident import parity changed after publication: {destination}"
        )
    final_doctor = doctor_dict(published.doctor())
    if final_doctor["healthy"] is not True:
        raise DegradedIntegrityError(
            f"resident import became unhealthy after publication: {destination}"
        )
    return {
        **inspection,
        "applied": True,
        "configuration_changed": False,
        "digest": expected_digest,
        "doctor": final_doctor,
        "parity": readback_parity,
        "provider_state_copied": False,
        "published": True,
        "local_source_checkpoints_staged": len(source.local_source_checkpoints),
        "source_observations_copied": 0,
        "vault_id": expected_vault_id,
    }


def staged_local_source_checkpoint_status(
    vault: Vault,
    *,
    delivery: LocalSourceDelivery | None = None,
) -> dict[str, Any]:
    """Read the portable migration handoff without returning its raw cursor."""

    local_delivery = delivery or LocalSourceDelivery(vault)
    _require_delivery_vault(vault, local_delivery)
    store = PinnedPathRoot(vault.root)
    try:
        with store.exclusive_file_lock(_LOCAL_SOURCE_MIGRATION_LOCK):
            snapshot = vault.get_source_snapshot()
            vault_id = _pinned_vault_id(store)
            if not store.directory_exists(_LOCAL_SOURCE_MIGRATION_DIRECTORY):
                return {
                    "checkpoints": [],
                    "migration_revision": ABSENT_LOCAL_SOURCE_MIGRATION_REVISION,
                    "source_revision": snapshot.revision,
                    "vault_id": vault_id,
                }
            encoded = store.read_regular_file(
                _LOCAL_SOURCE_MIGRATION_PATH,
                label="local source migration checkpoints",
                max_bytes=_LOCAL_SOURCE_MIGRATION_MAX_BYTES,
                missing_ok=True,
            )
            if encoded is None:
                return {
                    "checkpoints": [],
                    "migration_revision": ABSENT_LOCAL_SOURCE_MIGRATION_REVISION,
                    "source_revision": snapshot.revision,
                    "vault_id": vault_id,
                }
            migration = _parse_local_source_migration(
                encoded,
                expected_vault_id=vault_id,
            )
            checkpoints = []
            for item in migration.checkpoints:
                host_status = local_delivery.status(item.checkpoint.source)
                host_matches = _host_adoption_matches(item, host_status)
                if host_matches and item.state == "pending":
                    state = "completion_pending"
                elif host_matches:
                    state = "adopted"
                elif item.state == "adopted":
                    state = "needs_reproof"
                elif item.checkpoint.source not in snapshot.selected_sources:
                    state = "source_not_selected"
                elif host_status.get("initialized") is True:
                    state = "host_conflict"
                else:
                    state = "pending"
                checkpoints.append(
                    {
                        "adopted_at": (
                            item.adoption.adopted_at if item.adoption is not None else None
                        ),
                        "covered_through": item.checkpoint.covered_through,
                        "cursor_sha256": item.checkpoint.cursor_sha256,
                        "observed_at": item.checkpoint.observed_at,
                        "source": item.checkpoint.source,
                        "state": state,
                    }
                )
    finally:
        store.close()
    return {
        "checkpoints": checkpoints,
        "migration_revision": sha256_bytes(encoded),
        "source_revision": snapshot.revision,
        "vault_id": migration.vault_id,
    }


def adopt_staged_local_source_checkpoint(
    vault: Vault,
    *,
    source: str,
    expected_migration_revision: str,
    expected_source_revision: str,
    disposition: str,
    delivery: LocalSourceDelivery | None = None,
    _after_host_adoption: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Prove and adopt one checkpoint from this vault's exact staged handoff."""

    expected_revision = _digest(
        expected_migration_revision,
        "expected local source migration revision",
    )
    local_delivery = delivery or LocalSourceDelivery(vault)
    _require_delivery_vault(vault, local_delivery)
    if disposition != VERIFIED_PREFIX_ADOPTION:
        raise ValidationError(
            "staged local source adoption requires disposition adopt_verified_prefix"
        )

    store = PinnedPathRoot(vault.root)
    try:
        with (
            store.exclusive_file_lock(_LOCAL_SOURCE_MIGRATION_LOCK),
            store.bind_directory(_LOCAL_SOURCE_MIGRATION_DIRECTORY),
        ):
            encoded, migration = _read_local_source_migration_from_store(store)
            if sha256_bytes(encoded) != expected_revision:
                raise ConflictError(
                    "local source migration changed; reload its exact status before adoption"
                )
            item = _migration_checkpoint_for_source(migration, source)
            snapshot = vault.get_source_snapshot()
            if snapshot.revision != expected_source_revision:
                raise ConflictError("source state changed before staged local checkpoint adoption")
            adoption = local_delivery.adopt_checkpoint(
                source,
                prior_cursor=item.checkpoint.cursor,
                expected_source_revision=expected_source_revision,
                disposition=disposition,
            )
            if _after_host_adoption is not None:
                _after_host_adoption(adoption)
            current = vault.get_source_snapshot()
            if current.revision != expected_source_revision:
                raise ConflictError("source state changed during staged local checkpoint adoption")
            current_encoded, current_migration = _read_local_source_migration_from_store(store)
            if current_encoded != encoded or current_migration != migration:
                raise ConflictError("local source migration changed during checkpoint adoption")
            checkpoint_digest = str(adoption["checkpoint_digest"])
            expected_checkpoint_digest = f"sha256:{item.checkpoint.cursor_sha256}"
            if checkpoint_digest != expected_checkpoint_digest:
                raise ValidationError(
                    "host checkpoint digest differs from the staged migration checkpoint"
                )
            adopted_at = str(adoption["adopted_at"])
            replacement = _complete_local_source_migration(
                migration,
                source=source,
                adoption=_LocalSourceMigrationAdoption(
                    checkpoint_digest=checkpoint_digest,
                    disposition=disposition,
                    source_revision=expected_source_revision,
                    adopted_at=adopted_at,
                ),
            )
            replacement_bytes = _local_source_migration_state_bytes(replacement)
            store.compare_and_swap_regular_file(
                _LOCAL_SOURCE_MIGRATION_PATH,
                expected=encoded,
                replacement=replacement_bytes,
                label="local source migration checkpoints",
                max_bytes=_LOCAL_SOURCE_MIGRATION_MAX_BYTES,
                mode=0o600,
            )
    finally:
        store.close()

    return {
        "adopted": True,
        "already_adopted": bool(adoption.get("already_adopted", False)),
        "baseline_scope": "verified-prefix",
        "checkpoint_digest": checkpoint_digest,
        "disposition": disposition,
        "messages": None,
        "migration_revision": sha256_bytes(replacement_bytes),
        "source": source,
        "source_revision": expected_source_revision,
        "state": "adopted",
    }


def _load_export(path: Path) -> _ResidentExport:
    source_path = _leaf_path(path, label="resident export")
    _require_owner_only(source_path)
    encoded = read_regular_file(source_path, label="resident export", max_bytes=MAX_EXPORT_BYTES)
    _require_owner_only(source_path)
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("resident export must be valid UTF-8 JSON") from exc
    root = _object(decoded, "resident export", _ROOT_KEYS)
    schema_version = root["schema_version"]
    if (
        root["source_format"] != EXPORT_KIND
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in EXPORT_VERSIONS
    ):
        raise ValidationError("unsupported resident export format")
    payload_keys = _PAYLOAD_KEYS_V2 if schema_version == 2 else _PAYLOAD_KEYS_V1
    payload = _object(root["payload"], "resident payload", payload_keys)
    payload_sha = _digest(root["payload_sha256"], "resident payload SHA-256")
    if _json_digest(payload) != payload_sha:
        raise ValidationError("resident payload hash does not match")
    section_hashes = _object(root["section_sha256"], "resident section hashes", payload_keys)
    for section in sorted(payload_keys):
        if _json_digest(payload[section]) != _digest(
            section_hashes[section], f"resident {section} SHA-256"
        ):
            raise ValidationError(f"resident {section} hash does not match")
    count_keys = _COUNT_KEYS_V2 if schema_version == 2 else _COUNT_KEYS_V1
    counts = _manifest_counts(root["counts"], payload, count_keys=count_keys)

    documents = _object(payload["documents"], "resident documents", _DOCUMENT_KEYS)
    mind = _document(documents, "MIND.md")
    now = _document(documents, "NOW.md")
    context_files = _portable_files(payload["context_files"], kind="context")
    signal_files = _portable_files(payload["signal_files"], kind="signal")
    signal_counts, settled_event_keys = _validate_signal_files(signal_files)
    local_source_checkpoints = (
        _local_source_checkpoints(payload["local_source_checkpoints"])
        if schema_version == 2
        else ()
    )
    records = _object(payload["records"], "resident records", _RECORD_KEYS)
    tasks = _records(records["tasks"], "tasks", _task_from_private)
    entities = _records(records["entities"], "entities", _entity_from_private)
    threads = _records(records["threads"], "threads", _thread_from_private)
    _unique_ids(tasks, "task")
    _unique_ids(entities, "entity")
    _unique_ids(threads, "work-thread")
    direction = _direction_from_private(records["direction"])
    portfolio = _portfolio_from_private(
        records["portfolio"],
        tasks=tasks,
        threads=threads,
        direction=direction,
    )
    original_sources, selected_sources, source_mapping, companions = _sources(
        payload["selected_sources"]
    )
    observed_at = _latest_observed_at(tasks, entities, threads, direction, portfolio)
    selected_sources = select_sources(
        empty_source_snapshot(), selected_sources, observed_at=observed_at
    ).selected_sources

    retained_warnings = 0
    retained_warnings += len(_screen_durable_text(mind.encode("utf-8"), "MIND.md"))
    retained_warnings += len(_screen_durable_text(now.encode("utf-8"), "NOW.md"))
    for item in context_files:
        retained_warnings += len(_screen_durable_text(item.content, item.path))
    for item in signal_files:
        retained_warnings += len(_screen_durable_text(item.content, item.path))
    for item in tasks:
        retained_warnings += len(
            _screen_durable_text(render_task(item).encode("utf-8"), f"task {item.identifier}")
        )
    for item in entities:
        retained_warnings += len(
            _screen_durable_text(render_entity(item).encode("utf-8"), f"entity {item.identifier}")
        )
    for item in threads:
        retained_warnings += len(
            _screen_durable_text(
                render_thread(item).encode("utf-8"), f"work-thread {item.identifier}"
            )
        )
    retained_warnings += len(
        _screen_durable_text(render_direction(direction).encode("utf-8"), "Direction")
    )
    retained_warnings += len(
        _screen_durable_text(render_portfolio(portfolio).encode("utf-8"), "Portfolio")
    )

    return _ResidentExport(
        source_sha256=sha256_bytes(encoded),
        payload_sha256=payload_sha,
        mind=mind,
        now=now,
        tasks=tasks,
        entities=entities,
        threads=threads,
        direction=direction,
        portfolio=portfolio,
        original_sources=original_sources,
        selected_sources=selected_sources,
        source_mapping=source_mapping,
        companions=companions,
        context_files=context_files,
        signal_files=signal_files,
        settled_event_keys=settled_event_keys,
        signal_counts=signal_counts,
        local_source_checkpoints=local_source_checkpoints,
        observed_at=observed_at,
        manifest_counts=counts,
        retained_screen_warnings=retained_warnings,
    )


def _inspection(source: _ResidentExport, target: Path) -> dict[str, Any]:
    semantic_digest = _source_semantic_digest(source)
    plan = {
        "export_sha256": source.source_sha256,
        "importer_version": IMPORTER_VERSION,
        "local_source_checkpoint_sources": [
            item.source for item in source.local_source_checkpoints
        ],
        "semantic_digest": semantic_digest,
        "source_zero": "gsv",
        "target": str(target),
    }
    return {
        "apply_ready": True,
        "companions": list(source.companions),
        "counts": {
            **source.manifest_counts,
            "signals_acknowledged": source.signal_counts.acknowledged,
            "signals_archive_files": source.signal_counts.archive_files,
            "signals_inputs": source.signal_counts.inputs,
            "signals_pending": source.signal_counts.pending,
            "signals_settled_event_keys": settled_event_key_count(source.settled_event_keys),
        },
        "dependencies": [],
        "export_format": EXPORT_KIND,
        "export_sha256": source.source_sha256,
        "importer_version": IMPORTER_VERSION,
        "local_source_checkpoint_sources": [
            item.source for item in source.local_source_checkpoints
        ],
        "original_selected_sources": list(source.original_sources),
        "payload_sha256": source.payload_sha256,
        "plan_revision": sha256_bytes(_canonical_json(plan)),
        "retained_screen_warnings": source.retained_screen_warnings,
        "selected_sources": list(source.selected_sources),
        "semantic_digest": semantic_digest,
        "source_mapping": [
            {"from": source_id, "to": target_id} for source_id, target_id in source.source_mapping
        ],
        "source_zero": "gsv",
        "target": str(target),
        "target_state": "absent",
    }


def _write_stage(vault: Vault, source: _ResidentExport) -> None:
    atomic_write(vault.root / "MIND.md", source.mind.encode("utf-8"))
    atomic_write(vault.root / "NOW.md", source.now.encode("utf-8"))
    for task in source.tasks:
        _write_record(vault.root / "tasks", task.identifier, render_task(task))
    for entity in source.entities:
        _write_record(vault.root / "entities", entity.identifier, render_entity(entity))
    for thread in source.threads:
        _write_record(vault.root / "threads", thread.identifier, render_thread(thread))
    atomic_write(vault.root / "DIRECTION.md", render_direction(source.direction).encode("utf-8"))
    atomic_write(vault.root / "PORTFOLIO.md", render_portfolio(source.portfolio).encode("utf-8"))
    snapshot = select_sources(
        empty_source_snapshot(), source.selected_sources, observed_at=source.observed_at
    )
    atomic_write(vault.root / "SOURCES.md", render_source_snapshot(snapshot).encode("utf-8"))
    for item in (*source.context_files, *source.signal_files):
        target = vault.root / PurePosixPath(item.path)
        atomic_write(target, item.content)
        if os.name != "nt":
            target.chmod(0o700 if item.executable else 0o600)
    if (source.settled_event_keys or source.signal_counts.archive_files) and not any(
        item.path == SETTLED_EVENT_KEYS_RELATIVE for item in source.signal_files
    ):
        target = vault.root / SETTLED_EVENT_KEYS_RELATIVE
        atomic_write(target, source.settled_event_keys)
        if os.name != "nt":
            target.chmod(0o600)
    if source.local_source_checkpoints:
        target = vault.root / _LOCAL_SOURCE_MIGRATION_PATH
        atomic_write(
            target,
            _local_source_migration_bytes(
                source.local_source_checkpoints,
                vault_id=str(vault.identity()["vault_id"]),
            ),
        )
        if os.name != "nt":
            target.chmod(0o600)


def _parity(source: _ResidentExport, vault: Vault) -> dict[str, Any]:
    try:
        imported_checkpoints = _read_local_source_migration(vault)
        if imported_checkpoints != source.local_source_checkpoints:
            return {"matched": False, "reason": "local source migration checkpoint mismatch"}
        imported_signal_files = _read_portable_files(vault.root, source.signal_files)
        actual = _semantic_payload(
            mind=vault.read_document("MIND.md")["content"],
            now=vault.read_document("NOW.md")["content"],
            tasks=tuple(vault.list_tasks()),
            entities=tuple(vault.list_entities()),
            threads=tuple(vault.list_threads()),
            direction=parse_direction((vault.root / "DIRECTION.md").read_text(encoding="utf-8")),
            portfolio=parse_portfolio((vault.root / "PORTFOLIO.md").read_text(encoding="utf-8")),
            selected_sources=vault.get_source_snapshot().selected_sources,
            context_files=_read_portable_files(vault.root, source.context_files),
            signal_files=imported_signal_files,
            settled_event_keys=_read_settled_event_keys(
                vault.root,
                source,
                imported_signal_files,
            ),
            signal_counts=_signal_counts_at_root(vault.root, source.signal_files),
            source=source,
        )
        expected = _semantic_payload_from_source(source)
    except (OSError, UnicodeError, ContinuityError) as exc:
        return {"matched": False, "reason": str(exc)}
    actual_digest = sha256_bytes(_canonical_json(actual))
    expected_digest = sha256_bytes(_canonical_json(expected))
    return {
        "actual_semantic_digest": actual_digest,
        "expected_semantic_digest": expected_digest,
        "matched": actual == expected,
        "source_audit": {
            "companions": list(source.companions),
            "original_selected_sources": list(source.original_sources),
            "source_mapping": [list(item) for item in source.source_mapping],
            "source_zero": "gsv",
        },
    }


def _source_semantic_digest(source: _ResidentExport) -> str:
    return sha256_bytes(_canonical_json(_semantic_payload_from_source(source)))


def _semantic_payload_from_source(source: _ResidentExport) -> dict[str, Any]:
    return _semantic_payload(
        mind=source.mind,
        now=source.now,
        tasks=source.tasks,
        entities=source.entities,
        threads=source.threads,
        direction=source.direction,
        portfolio=source.portfolio,
        selected_sources=source.selected_sources,
        context_files=source.context_files,
        signal_files=source.signal_files,
        settled_event_keys=source.settled_event_keys,
        signal_counts=source.signal_counts,
        source=source,
    )


def _semantic_payload(
    *,
    mind: str,
    now: str,
    tasks: tuple[Task, ...],
    entities: tuple[Entity, ...],
    threads: tuple[WorkThread, ...],
    direction: Direction,
    portfolio: Portfolio,
    selected_sources: tuple[str, ...],
    context_files: tuple[_PortableFile, ...],
    signal_files: tuple[_PortableFile, ...],
    settled_event_keys: bytes,
    signal_counts: _SignalCounts,
    source: _ResidentExport,
) -> dict[str, Any]:
    return {
        "companions": list(source.companions),
        "context_files": [_file_metadata(item) for item in context_files],
        "direction": _without_revision(asdict(direction)),
        "documents": {"MIND.md": mind, "NOW.md": now},
        "entities": [
            _without_revision(record_dict(item))
            for item in sorted(entities, key=lambda record: record.identifier)
        ],
        "original_selected_sources": list(source.original_sources),
        "portfolio": _without_revision(asdict(portfolio)),
        "selected_sources": list(selected_sources),
        "signal_counts": asdict(signal_counts),
        "signal_files": [_file_metadata(item) for item in signal_files],
        "settled_event_keys": {
            "count": settled_event_key_count(settled_event_keys),
            "sha256": sha256_bytes(settled_event_keys),
        },
        "local_source_checkpoints": [
            {
                "covered_through": item.covered_through,
                "cursor_sha256": item.cursor_sha256,
                "observed_at": item.observed_at,
                "source": item.source,
            }
            for item in source.local_source_checkpoints
        ],
        "source_mapping": [list(item) for item in source.source_mapping],
        "tasks": [
            _without_revision(record_dict(item))
            for item in sorted(tasks, key=lambda record: record.identifier)
        ],
        "threads": [
            _without_revision(record_dict(item))
            for item in sorted(threads, key=lambda record: record.identifier)
        ],
    }


def _task_from_private(value: object) -> Task:
    raw = _object(value, "private task", _TASK_KEYS)
    candidate = Task(
        identifier=raw["identifier"],
        title=raw["title"],
        status=raw["status"],
        next_actor=_actor(raw["next_actor"]),
        outcome=raw["outcome"],
        next_action=raw["next_action"],
        waiting_on=raw["waiting_on"],
        rank=raw["rank"],
        active_thread_id=raw["active_thread_id"],
        refs=_string_tuple(raw["refs"], "task refs"),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        revision="",
        superseded_by=raw["superseded_by"],
        project=raw["project"],
        entity_links=task_entity_links_from_json(raw["entity_links"]),
        workspace=raw["workspace"],
        attention_at=raw["attention_at"],
        due=raw["due"],
        codex_episode_ids=_string_tuple(raw["thread_ids"], "task thread IDs"),
        state_changed_at=raw["state_changed_at"],
        history=_string_tuple(raw["history"], "task history"),
    )
    return parse_task(render_task(candidate))


def _entity_from_private(value: object) -> Entity:
    raw = _object(value, "private entity", _ENTITY_KEYS)
    candidate = Entity(
        identifier=raw["identifier"],
        title=raw["name"],
        entity_type=raw["entity_kind"],
        aliases=_string_tuple(raw["aliases"], "entity aliases"),
        summary=raw["summary"],
        refs=_string_tuple(raw["sources"], "entity sources"),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        revision="",
        status=raw["status"],
        relationships=entity_relationships_from_json(raw["relationships"]),
        observed_at=raw["observed_at"],
        recheck_at=raw["recheck_at"],
        merged_into=raw["merged_into"],
        merged_at=raw["merged_at"],
        merged_from_updated_at=raw["merged_from_updated_at"],
        merge_absorptions=entity_merge_absorptions_from_json(raw["merge_absorptions"]),
        history=_string_tuple(raw["history"], "entity history"),
    )
    return parse_entity(render_entity(candidate))


def _thread_from_private(value: object) -> WorkThread:
    raw = _object(value, "private WorkThread", _THREAD_KEYS)
    candidate = WorkThread(
        identifier=raw["identifier"],
        title=raw["title"],
        status=raw["status"],
        purpose=raw["purpose"],
        summary=raw["current_summary"],
        next_move=raw["next_move"],
        focus_task_id=raw["focus_task_id"],
        task_links=thread_task_links_from_json(raw["task_links"]),
        entity_links=thread_entity_links_from_json(raw["entity_links"]),
        refs=_string_tuple(raw["refs"], "WorkThread refs"),
        created_at=raw["recorded_at"],
        updated_at=raw["updated_at"],
        revision="",
        closure_condition=raw["closure_condition"],
        next_actor=_actor(raw["next_actor"]),
        waiting_on=raw["waiting_on"],
        superseded_by=raw["superseded_by"],
        observed_at=raw["observed_at"],
        state_changed_at=raw["state_changed_at"],
        recheck_at=raw["recheck_at"],
        resolved_at=raw["resolved_at"],
        history=_string_tuple(raw["history"], "WorkThread history"),
    )
    return parse_thread(render_thread(candidate))


def _direction_from_private(value: object) -> Direction:
    raw = _object(value, "private Direction", _DIRECTION_KEYS)
    aims_raw = _array(raw["aims"], "Direction aims")
    aims = tuple(
        DirectionAim(
            **_object(
                item,
                "Direction aim",
                frozenset({"desired_state", "identifier", "title"}),
            )
        )
        for item in aims_raw
    )
    desired = _string_tuple(raw["desired_outcomes"], "Direction desired outcomes")
    if desired != tuple(item.desired_state for item in aims):
        raise ValidationError("Direction desired_outcomes do not equal its authored aim states")
    candidate = Direction(
        status=raw["status"],
        current_chapter=raw["current_chapter"],
        aims=aims,
        updated_at=raw["updated_at"],
        revision="",
        constraints=_string_tuple(raw["constraints"], "Direction constraints"),
        tensions=_string_tuple(raw["tensions"], "Direction tensions"),
        refs=_string_tuple(raw["refs"], "Direction refs"),
        observed_at=raw["observed_at"],
        recorded_at=raw["recorded_at"],
        recheck_at=raw["recheck_at"],
        history=_string_tuple(raw["history"], "Direction history"),
        format_version=2,
    )
    return parse_direction(render_direction(candidate))


def _portfolio_from_private(
    value: object,
    *,
    tasks: tuple[Task, ...],
    threads: tuple[WorkThread, ...],
    direction: Direction,
) -> Portfolio:
    raw = _object(value, "private Portfolio", _PORTFOLIO_KEYS)
    if raw["review_thread_id"] != REVIEW_WORK_THREAD_ID:
        raise ValidationError(f"Portfolio review thread must be {REVIEW_WORK_THREAD_ID}")
    task_by_id = {item.identifier: item for item in tasks}
    thread_by_id = {item.identifier: item for item in threads}
    items: list[PortfolioItem] = []
    for value_item in _array(raw["items"], "Portfolio items"):
        item = _object(value_item, "Portfolio item", _PORTFOLIO_ITEM_KEYS)
        task = task_by_id.get(item["task_id"])
        if task is None:
            raise ValidationError(f"Portfolio task is missing: {item['task_id']}")
        thread_id = item["work_thread_id"]
        if thread_id is None:
            if item["work_thread_updated_at"] is not None:
                raise ValidationError("unthreaded Portfolio item has a thread timestamp")
            thread_revision = None
        else:
            thread = thread_by_id.get(thread_id)
            if thread is None:
                raise ValidationError(f"Portfolio WorkThread is missing: {thread_id}")
            thread_revision = thread.revision
        items.append(
            PortfolioItem(
                task_id=task.identifier,
                task_revision=task.revision,
                stance=_stance(item["stance"]),
                reason=item["rationale"],
                work_thread_id=thread_id,
                work_thread_revision=thread_revision,
                direction_aim_ids=_string_tuple(
                    item["direction_aim_ids"], "Portfolio Direction aim IDs"
                ),
                unaligned_reason=item["unaligned_reason"],
                source_position=item["position"],
                source_task_updated_at=item["task_updated_at"],
                source_thread_updated_at=item["work_thread_updated_at"],
            )
        )
    candidate = Portfolio(
        summary=raw["summary"],
        items=tuple(items),
        updated_at=raw["updated_at"],
        revision="",
        direction_revision=direction.revision,
        format_version=3,
        source_direction_updated_at=raw["direction_updated_at"],
        refs=_string_tuple(raw["refs"], "Portfolio refs"),
        observed_at=raw["observed_at"],
        recorded_at=raw["recorded_at"],
        review_after=raw["review_after"],
        history=_string_tuple(raw["history"], "Portfolio history"),
    )
    return parse_portfolio(render_portfolio(candidate))


def _sources(
    value: object,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    original = _string_tuple(value, "selected sources")
    if len(set(original)) != len(original):
        raise ValidationError("selected sources contain duplicates")
    # Seld itself is source zero: importing a resident Mind must keep the
    # signed local capability selected without fabricating any read receipt.
    mapped: list[str] = ["gsv"]
    mapping: list[tuple[str, str]] = []
    companions: list[str] = []
    for source in original:
        if source in _SOURCE_COMPANIONS:
            companions.append(source)
            continue
        target = _SOURCE_ALIASES.get(source)
        if target is None:
            raise ValidationError(f"private source has no public semantic mapping: {source}")
        mapping.append((source, target))
        if target not in mapped:
            mapped.append(target)
    return original, tuple(mapped), tuple(mapping), tuple(companions)


def _portable_files(value: object, *, kind: str) -> tuple[_PortableFile, ...]:
    rows = _array(value, f"resident {kind} files")
    if len(rows) > MAX_PORTABLE_FILES:
        raise ValidationError(f"resident {kind} export has too many files")
    result: list[_PortableFile] = []
    seen: set[str] = set()
    total = 0
    for row in rows:
        raw = _object(row, f"resident {kind} file", _FILE_KEYS)
        path = _portable_file_path(raw["path"], kind=kind)
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in seen:
            raise ValidationError(f"resident export has a path collision: {path}")
        seen.add(folded)
        encoded = raw["bytes_base64"]
        if not isinstance(encoded, str):
            raise ValidationError(f"resident file bytes must be base64 text: {path}")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(f"resident file has invalid base64: {path}") from exc
        file_limit = MAX_SIGNAL_FILE_BYTES if kind == "signal" else MAX_PORTABLE_FILE_BYTES
        if len(content) > file_limit:
            raise ValidationError(f"resident file exceeds its size bound: {path}")
        total += len(content)
        if total > MAX_EXPORT_BYTES:
            raise ValidationError(f"resident {kind} files exceed their total size bound")
        digest = _digest(raw["sha256"], f"resident file SHA-256: {path}")
        if sha256_bytes(content) != digest:
            raise ValidationError(f"resident file hash does not match: {path}")
        executable = raw["executable"]
        if not isinstance(executable, bool):
            raise ValidationError(f"resident file executable flag must be boolean: {path}")
        if kind == "signal" and executable:
            raise ValidationError("resident signal files cannot be executable")
        if os.name == "nt" and executable:
            raise ValidationError("executable resident context requires a POSIX target")
        result.append(_PortableFile(path, content, executable, digest))
    return tuple(result)


def _portable_file_path(value: object, *, kind: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ValidationError("resident export path is unsafe")
    path = PurePosixPath(value)
    for part in path.parts:
        normalized = unicodedata.normalize("NFC", part)
        if (
            part in {"", ".", ".."}
            or normalized != part
            or part.endswith((" ", "."))
            or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
            or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
        ):
            raise ValidationError(f"resident export path is not portable: {value}")
    if kind == "context":
        if path.parts[:3] == ("context", "resident", "control"):
            raise ValidationError(
                "resident export cannot import machine-local task bindings as context"
            )
        allowed = (
            path.parts[:2] == ("context", "resident")
            or path == PurePosixPath("journal/resident.md")
            or path.parts[:2] == ("journal", "resident-archive")
        )
    else:
        active = {
            ".gsv/signals/inputs.jsonl",
            ".gsv/signals/acks.jsonl",
            SETTLED_EVENT_KEYS_RELATIVE,
        }
        allowed = path.as_posix() in active or path.parts[:3] == (".gsv", "signals", "archive")
        allowed = allowed and path.suffix == ".jsonl"
    if not allowed:
        raise ValidationError(f"resident {kind} path is outside the import allowlist: {value}")
    return path.as_posix()


def _validate_signal_files(files: tuple[_PortableFile, ...]) -> tuple[_SignalCounts, bytes]:
    settled_event_keys = build_settled_event_index({item.path: item.content for item in files})
    with TemporaryDirectory(prefix="seld-signal-import-check-") as temporary:
        root = Path(temporary)
        for item in files:
            if item.path.startswith(".gsv/signals/archive/"):
                _validate_archive_jsonl(item)
            atomic_write(root / PurePosixPath(item.path), item.content)
        if (
            settled_event_keys
            or any(item.path.startswith(".gsv/signals/archive/") for item in files)
        ) and not any(item.path == SETTLED_EVENT_KEYS_RELATIVE for item in files):
            atomic_write(root / SETTLED_EVENT_KEYS_RELATIVE, settled_event_keys)
        return _signal_counts_at_root(root, files), settled_event_keys


def _signal_counts_at_root(root: Path, files: tuple[_PortableFile, ...]) -> _SignalCounts:
    active = {item.path for item in files if "/archive/" not in item.path}
    if not active:
        return _SignalCounts(0, 0, 0, sum("/archive/" in item.path for item in files))
    status = ResidentSignalStore(root).status()
    return _SignalCounts(
        inputs=status.inputs,
        acknowledged=status.acknowledged,
        pending=status.pending,
        archive_files=sum("/archive/" in item.path for item in files),
    )


def _read_settled_event_keys(
    root: Path,
    source: _ResidentExport,
    imported_signal_files: tuple[_PortableFile, ...],
) -> bytes:
    supplied = any(item.path == SETTLED_EVENT_KEYS_RELATIVE for item in source.signal_files)
    if supplied:
        return build_settled_event_index(
            {item.path: item.content for item in imported_signal_files}
        )
    if source.settled_event_keys or source.signal_counts.archive_files:
        return read_regular_file(
            root / SETTLED_EVENT_KEYS_RELATIVE,
            label="imported resident settled event index",
            max_bytes=MAX_SIGNAL_FILE_BYTES,
        )
    return build_settled_event_index({item.path: item.content for item in imported_signal_files})


def _validate_archive_jsonl(item: _PortableFile) -> None:
    for number, line in enumerate(item.content.splitlines(), 1):
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"invalid archived signal JSONL: {item.path}:{number}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"archived signal line must be an object: {item.path}:{number}")


def _local_source_checkpoints(value: object) -> tuple[_LocalSourceCheckpoint, ...]:
    checkpoints = tuple(
        _local_source_checkpoint(item) for item in _array(value, "local source checkpoints")
    )
    if len(checkpoints) > len(_LOCAL_SOURCE_IDS):
        raise ValidationError("resident export contains too many local source checkpoints")
    sources = [item.source for item in checkpoints]
    if len(sources) != len(set(sources)) or sources != sorted(sources):
        raise ValidationError("resident local source checkpoints must be unique and sorted")
    return checkpoints


def _local_source_checkpoint(value: object) -> _LocalSourceCheckpoint:
    raw = _object(
        value,
        "local source checkpoint",
        _LOCAL_SOURCE_CHECKPOINT_KEYS,
    )
    source = raw["source"]
    cursor = raw["cursor"]
    if not isinstance(source, str) or source not in _LOCAL_SOURCE_IDS:
        raise ValidationError("resident local source checkpoint has an unsupported source")
    if not isinstance(cursor, str) or not cursor or len(cursor) > 8_192:
        raise ValidationError("resident local source checkpoint cursor is invalid")
    try:
        cursor_value = json.loads(cursor)
    except json.JSONDecodeError as exc:
        raise ValidationError("resident local source checkpoint cursor is invalid") from exc
    cursor_object = _object(cursor_value, "local source cursor", _LOCAL_CURSOR_KEYS)
    version = cursor_object["version"]
    if type(version) is not int or version not in {1, 2}:
        raise ValidationError("resident local source checkpoint cursor is invalid")
    for key in ("messages", "chats", "rowid"):
        _nonnegative_int(cursor_object[key], f"local source cursor {key}")
    for key in ("schema", "generation"):
        fingerprint = cursor_object[key]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 20
            or any(character not in _FINGERPRINT_20 for character in fingerprint)
        ):
            raise ValidationError("resident local source checkpoint cursor is invalid")
    newest = cursor_object["newest"]
    if newest is not None:
        _timestamp(newest, "local source cursor newest")
    canonical_cursor = json.dumps(
        cursor_object,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if cursor != canonical_cursor:
        raise ValidationError("resident local source checkpoint cursor is not canonical")
    cursor_sha = _digest(raw["cursor_sha256"], "resident local source cursor SHA-256")
    if sha256_bytes(cursor.encode("utf-8")) != cursor_sha:
        raise ValidationError("resident local source checkpoint cursor hash does not match")
    observed_at = _timestamp(raw["observed_at"], "local source checkpoint observed_at")
    covered = raw["covered_through"]
    covered_through = (
        None if covered is None else _timestamp(covered, "local source checkpoint covered_through")
    )
    return _LocalSourceCheckpoint(
        source=source,
        cursor=cursor,
        cursor_sha256=cursor_sha,
        observed_at=observed_at,
        covered_through=covered_through,
    )


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise ValidationError(f"resident {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"resident {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"resident {label} must include a timezone")
    return value


def _local_source_migration_bytes(
    checkpoints: tuple[_LocalSourceCheckpoint, ...],
    *,
    vault_id: str,
) -> bytes:
    return _local_source_migration_state_bytes(
        _LocalSourceMigration(
            vault_id=vault_id,
            checkpoints=tuple(
                _LocalSourceMigrationCheckpoint(
                    checkpoint=item,
                    state="pending",
                    adoption=None,
                )
                for item in checkpoints
            ),
        )
    )


def _read_local_source_migration(vault: Vault) -> tuple[_LocalSourceCheckpoint, ...]:
    if not os.path.lexists(vault.root / _LOCAL_SOURCE_MIGRATION_PATH):
        return ()
    if not PINNED_PATH_ROOT_SUPPORTED:
        raise ValidationError(
            "secure staged local-source checkpoint migration is unavailable on this host"
        )
    store = PinnedPathRoot(vault.root)
    try:
        if not store.directory_exists(_LOCAL_SOURCE_MIGRATION_DIRECTORY):
            return ()
        encoded = store.read_regular_file(
            _LOCAL_SOURCE_MIGRATION_PATH,
            label="imported local source migration checkpoints",
            max_bytes=_LOCAL_SOURCE_MIGRATION_MAX_BYTES,
            missing_ok=True,
        )
        if encoded is None:
            return ()
        migration = _parse_local_source_migration(
            encoded,
            expected_vault_id=_pinned_vault_id(store),
        )
        return tuple(item.checkpoint for item in migration.checkpoints)
    finally:
        store.close()


def _read_local_source_migration_from_store(
    store: PinnedPathRoot,
) -> tuple[bytes, _LocalSourceMigration]:
    encoded = store.read_regular_file(
        _LOCAL_SOURCE_MIGRATION_PATH,
        label="local source migration checkpoints",
        max_bytes=_LOCAL_SOURCE_MIGRATION_MAX_BYTES,
    )
    assert encoded is not None
    return encoded, _parse_local_source_migration(
        encoded,
        expected_vault_id=_pinned_vault_id(store),
    )


def _parse_local_source_migration(
    encoded: bytes,
    *,
    expected_vault_id: str,
) -> _LocalSourceMigration:
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("imported local source migration checkpoints are invalid") from exc
    payload = _object(
        decoded,
        "imported local source migration checkpoints",
        _LOCAL_SOURCE_MIGRATION_ROOT_KEYS,
    )
    if payload["format_version"] != _LOCAL_SOURCE_MIGRATION_VERSION:
        raise ValidationError("imported local source migration checkpoints are invalid")
    vault_id = payload["vault_id"]
    if not isinstance(vault_id, str) or vault_id != expected_vault_id:
        raise ConflictError("local source migration belongs to another vault")
    checkpoints = tuple(
        _local_source_migration_checkpoint(item)
        for item in _array(payload["checkpoints"], "local source migration checkpoints")
    )
    sources = [item.checkpoint.source for item in checkpoints]
    if (
        len(checkpoints) > len(_LOCAL_SOURCE_IDS)
        or len(sources) != len(set(sources))
        or sources != sorted(sources)
    ):
        raise ValidationError(
            "imported local source migration checkpoints must be unique and sorted"
        )
    migration = _LocalSourceMigration(vault_id=vault_id, checkpoints=checkpoints)
    if encoded != _local_source_migration_state_bytes(migration):
        raise ValidationError("imported local source migration checkpoints are not canonical")
    return migration


def _pinned_vault_id(store: PinnedPathRoot) -> str:
    encoded = store.read_regular_file(
        Path(".gsv/manifest.json"),
        label="vault identity",
        max_bytes=64 * 1024,
    )
    assert encoded is not None
    return str(parse_vault_manifest(encoded)["vault_id"])


def _local_source_migration_checkpoint(value: object) -> _LocalSourceMigrationCheckpoint:
    raw = _object(
        value,
        "local source migration checkpoint",
        _LOCAL_SOURCE_MIGRATION_CHECKPOINT_KEYS,
    )
    checkpoint = _local_source_checkpoint({key: raw[key] for key in _LOCAL_SOURCE_CHECKPOINT_KEYS})
    state = raw["state"]
    if not isinstance(state, str) or state not in _LOCAL_SOURCE_MIGRATION_STATES:
        raise ValidationError("local source migration checkpoint state is invalid")
    adoption_value = raw["adoption"]
    if state == "pending":
        if adoption_value is not None:
            raise ValidationError("pending local source migration has an adoption receipt")
        adoption = None
    else:
        adoption_raw = _object(
            adoption_value,
            "local source migration adoption",
            _LOCAL_SOURCE_MIGRATION_ADOPTION_KEYS,
        )
        checkpoint_digest = adoption_raw["checkpoint_digest"]
        if checkpoint_digest != f"sha256:{checkpoint.cursor_sha256}":
            raise ValidationError("local source migration adoption digest is invalid")
        disposition = adoption_raw["disposition"]
        if disposition != VERIFIED_PREFIX_ADOPTION:
            raise ValidationError("local source migration adoption disposition is invalid")
        source_revision = adoption_raw["source_revision"]
        if source_revision != "absent":
            source_revision = _digest(
                source_revision,
                "local source migration adoption source revision",
            )
        adoption = _LocalSourceMigrationAdoption(
            checkpoint_digest=checkpoint_digest,
            disposition=disposition,
            source_revision=source_revision,
            adopted_at=_timestamp(
                adoption_raw["adopted_at"],
                "local source migration adoption time",
            ),
        )
    return _LocalSourceMigrationCheckpoint(
        checkpoint=checkpoint,
        state=state,
        adoption=adoption,
    )


def _local_source_migration_state_bytes(migration: _LocalSourceMigration) -> bytes:
    checkpoints: list[dict[str, Any]] = []
    for item in migration.checkpoints:
        checkpoints.append(
            {
                **asdict(item.checkpoint),
                "adoption": asdict(item.adoption) if item.adoption is not None else None,
                "state": item.state,
            }
        )
    return (
        _canonical_json(
            {
                "checkpoints": checkpoints,
                "format_version": _LOCAL_SOURCE_MIGRATION_VERSION,
                "vault_id": migration.vault_id,
            }
        )
        + b"\n"
    )


def _migration_checkpoint_for_source(
    migration: _LocalSourceMigration,
    source: str,
) -> _LocalSourceMigrationCheckpoint:
    matches = [item for item in migration.checkpoints if item.checkpoint.source == source]
    if len(matches) != 1:
        raise ValidationError("local source has no staged migration checkpoint")
    return matches[0]


def _complete_local_source_migration(
    migration: _LocalSourceMigration,
    *,
    source: str,
    adoption: _LocalSourceMigrationAdoption,
) -> _LocalSourceMigration:
    checkpoints = []
    found = False
    for item in migration.checkpoints:
        if item.checkpoint.source != source:
            checkpoints.append(item)
            continue
        found = True
        if (
            item.state == "adopted"
            and item.adoption != adoption
            and (
                item.adoption is None
                or item.adoption.checkpoint_digest != adoption.checkpoint_digest
                or item.adoption.disposition != adoption.disposition
            )
        ):
            raise ConflictError("local source migration already has a different adoption")
        checkpoints.append(
            _LocalSourceMigrationCheckpoint(
                checkpoint=item.checkpoint,
                state="adopted",
                adoption=adoption,
            )
        )
    if not found:
        raise ValidationError("local source has no staged migration checkpoint")
    return _LocalSourceMigration(
        vault_id=migration.vault_id,
        checkpoints=tuple(checkpoints),
    )


def _host_adoption_matches(
    item: _LocalSourceMigrationCheckpoint,
    host_status: dict[str, Any],
) -> bool:
    adoption = host_status.get("adoption")
    reset = host_status.get("last_reset")
    if isinstance(adoption, dict) and isinstance(reset, dict):
        adopted_at = adoption.get("adopted_at")
        reset_at = reset.get("reset_at")
        if isinstance(adopted_at, str) and isinstance(reset_at, str):
            try:
                if datetime.fromisoformat(
                    reset_at.replace("Z", "+00:00")
                ) >= datetime.fromisoformat(adopted_at.replace("Z", "+00:00")):
                    return False
            except ValueError:
                return False
    return bool(
        host_status.get("initialized") is True
        and isinstance(adoption, dict)
        and adoption.get("imported_checkpoint_digest") == f"sha256:{item.checkpoint.cursor_sha256}"
        and adoption.get("disposition") == VERIFIED_PREFIX_ADOPTION
    )


def _require_delivery_vault(vault: Vault, delivery: LocalSourceDelivery) -> None:
    if (
        delivery.vault.root.resolve() != vault.root.resolve()
        or delivery.vault.identity()["vault_id"] != vault.identity()["vault_id"]
    ):
        raise ValidationError("local source delivery belongs to another vault")


def _manifest_counts(
    value: object,
    payload: dict[str, Any],
    *,
    count_keys: frozenset[str],
) -> dict[str, int]:
    raw = _object(value, "resident counts", count_keys)
    counts = {key: _nonnegative_int(raw[key], f"resident count {key}") for key in count_keys}
    records = _object(payload["records"], "resident records", _RECORD_KEYS)
    expected = {
        "context_files": len(_array(payload["context_files"], "context files")),
        "entities": len(_array(records["entities"], "entities")),
        "signal_files": len(_array(payload["signal_files"], "signal files")),
        "tasks": len(_array(records["tasks"], "tasks")),
        "threads": len(_array(records["threads"], "threads")),
    }
    if "local_source_checkpoints" in count_keys:
        expected["local_source_checkpoints"] = len(
            _array(payload["local_source_checkpoints"], "local source checkpoints")
        )
    if counts != expected:
        raise ValidationError("resident manifest counts do not match its payload")
    return counts


def _read_portable_files(
    root: Path, expected: tuple[_PortableFile, ...]
) -> tuple[_PortableFile, ...]:
    result = []
    for item in expected:
        path = root / PurePosixPath(item.path)
        content = read_regular_file(
            path,
            label="imported resident file",
            max_bytes=(
                MAX_SIGNAL_FILE_BYTES
                if item.path.startswith(".gsv/signals/")
                else MAX_PORTABLE_FILE_BYTES
            ),
        )
        executable = bool(os.lstat(path).st_mode & stat.S_IXUSR) if os.name != "nt" else False
        result.append(_PortableFile(item.path, content, executable, sha256_bytes(content)))
    return tuple(result)


def _file_metadata(item: _PortableFile) -> dict[str, Any]:
    return {"executable": item.executable, "path": item.path, "sha256": item.sha256}


def _latest_observed_at(
    tasks: tuple[Task, ...],
    entities: tuple[Entity, ...],
    threads: tuple[WorkThread, ...],
    direction: Direction,
    portfolio: Portfolio,
) -> datetime:
    values = [
        *(item.updated_at for item in tasks),
        *(item.updated_at for item in entities),
        *(item.updated_at for item in threads),
        direction.updated_at,
        portfolio.updated_at,
    ]
    return max(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values).astimezone(
        UTC
    )


def _records(value: object, label: str, parser: Any) -> tuple[Any, ...]:
    return tuple(parser(item) for item in _array(value, label))


def _unique_ids(values: tuple[Any, ...], label: str) -> None:
    identifiers = [item.identifier for item in values]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError(f"resident export contains duplicate {label} IDs")


def _document(documents: dict[str, Any], name: str) -> str:
    value = documents[name]
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > MAX_DOCUMENT_BYTES
    ):
        raise ValidationError(f"resident document is empty or too large: {name}")
    return value


def _screen_durable_text(
    content: bytes,
    label: str,
) -> tuple[str, ...]:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        # Allowed binary assets are retained exactly but never treated as automatic
        # model-readable context by the import transaction.
        return ()
    size = 256 * 1024
    overlap = 512
    reasons: set[str] = set()
    for offset in range(0, len(content), size - overlap):
        assessment = screen_local_content(content[offset : offset + size])
        if assessment.decision is AwarenessDecision.QUARANTINE:
            reasons.update(assessment.reasons)
    # Generic entropy alone is not sufficient to drop owner-authored local context during a
    # byte-preserving migration (hashes, IDs, and code fixtures legitimately trigger it).
    # Every concrete credential signature fails closed before staging; no byte is silently
    # removed or copied into model-readable canon.
    concrete = reasons - {"high-entropy-token"}
    if concrete:
        reason = sorted(concrete)[0]
        raise ValidationError(
            f"resident export content is quarantined before import: {label} ({reason})"
        )
    return ("high-entropy-token",) if "high-entropy-token" in reasons else ()


def _write_record(directory: Path, identifier: str, content: str) -> None:
    atomic_write(directory / (identifier.replace(":", "--") + ".md"), content.encode("utf-8"))


def _without_revision(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "revision"}


def _absent_target(path: Path, *, prepare_parent: bool = False) -> Path:
    target = _leaf_path(path, label="resident import target")
    try:
        os.lstat(target)
    except FileNotFoundError:
        if prepare_parent:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PersistenceError(
                    f"could not prepare import parent: {target.parent}: {exc}"
                ) from exc
        return target
    except OSError as exc:
        raise ValidationError(f"could not inspect import target: {target}: {exc}") from exc
    raise ConflictError(f"resident import target must be absent: {target}")


def _leaf_path(path: Path, *, label: str) -> Path:
    try:
        expanded = path.expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        parent = expanded.parent.resolve()
        if expanded.name in {"", ".", ".."}:
            raise ValueError("missing leaf name")
        return parent / expanded.name
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError(f"invalid {label}: {path}: {exc}") from exc


def _require_owner_only(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValidationError(f"could not inspect export permissions: {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValidationError("resident export must be one regular file")
    if metadata.st_mode & 0o077:
        raise ValidationError("resident export must be owner-only (mode 0600 or stricter)")
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and metadata.st_uid != geteuid():
        raise ValidationError("resident export must be owned by the current user")


def _require_no_update_transaction() -> None:
    if self_update.status().get("state") == "interrupted":
        raise ConflictError("recover the unresolved Seld self-update before resident import")


def _publish_import_stage(
    stage: Path,
    target: Path,
    *,
    expected_identity: tuple[int, int],
) -> None:
    if stage.parent != target.parent:
        raise ValidationError("resident import stage and target must share one parent")
    parent = target.parent
    if not parent.name:
        raise ValidationError("resident import target requires one named parent directory")
    store = PinnedPathRoot(parent.parent)
    relative_parent = Path(parent.name)
    try:
        with store.bind_directory(relative_parent):
            store.publish_directory_no_replace_if_exact(
                relative_parent / stage.name,
                relative_parent / target.name,
                expected_identity=expected_identity,
                label="resident import",
            )
    finally:
        store.close()


def _raise_publish_error(
    *,
    stage: Path,
    target: Path,
    stage_identity: tuple[int, int],
    vault_id: str,
    digest: str,
    cause: OSError,
) -> None:
    if not stage.exists() and _published_matches(
        target, identity=stage_identity, vault_id=vault_id, digest=digest
    ):
        raise MutationCommittedError(
            f"resident import is visible at {target}, but publication durability is unknown"
        ) from cause
    if _same_directory(stage, stage_identity) and not os.path.lexists(target):
        raise PersistenceError(
            f"resident import was not published; stage retained at {stage}"
        ) from cause
    raise DegradedIntegrityError(
        f"resident import publication is unknown; inspect {stage} and {target}"
    ) from cause


def _published_matches(
    path: Path, *, identity: tuple[int, int], vault_id: str, digest: str
) -> bool:
    if not _same_directory(path, identity):
        return False
    try:
        vault = Vault(path)
        return vault.identity()["vault_id"] == vault_id and vault.logical_digest() == digest
    except (OSError, ContinuityError):
        return False


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise DegradedIntegrityError(f"could not pin import stage: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DegradedIntegrityError(f"import stage is not a stable directory: {path}")
    return int(metadata.st_dev), int(metadata.st_ino)


def _same_directory(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and (int(metadata.st_dev), int(metadata.st_ino)) == identity
    )


def _object(value: object, label: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError(f"{label} has an unsupported shape")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be an array")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{label} must be a string array")
    return tuple(value)


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{label} must be a non-negative integer")
    return value


def _actor(value: object) -> Actor | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _ACTOR_ALIASES:
        raise ValidationError(f"private next actor has no public semantic mapping: {value}")
    return _ACTOR_ALIASES[value]


def _stance(value: object) -> PortfolioStance:
    if not isinstance(value, str) or value not in _STANCE_ALIASES:
        raise ValidationError(f"private Portfolio stance has no public mapping: {value}")
    return _STANCE_ALIASES[value]


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256 for character in value)
    ):
        raise ValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_digest(value: object) -> str:
    return sha256_bytes(_canonical_json(value) + b"\n")


__all__ = [
    "adopt_staged_local_source_checkpoint",
    "apply_resident_export",
    "inspect_resident_export",
    "staged_local_source_checkpoint_status",
]
