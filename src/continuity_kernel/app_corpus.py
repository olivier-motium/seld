"""Host-local, content-bearing search corpus for explicitly connected apps.

The Seld vault remains content-free.  This store is keyed to one vault path and
keeps provider material, cursors, QMD state, and fallback search snapshots in a
private directory beneath the local GSV data directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from continuity_kernel import recall as recall_module
from continuity_kernel.app_corpus_microsoft_delta import (
    rewind_message_page_for_materialization_retry,
)
from continuity_kernel.atomic import PinnedPathRoot
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ValidationError
from continuity_kernel.slack_channel_access import (
    SlackChannelAccessPolicy,
    load_slack_channel_access,
)

MAX_DOCUMENTS = 250_000
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
MAX_QUERY_CHARACTERS = 1_000
MAX_RESULTS = 20
MAX_STATE_BYTES = 256 * 1024 * 1024
_INDEX_NAME = "seld-apps"
_STATE_VERSION = 1
_OBJECT_ID = re.compile(r"^.{1,1024}$", re.DOTALL)
_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT = "gmail_legacy_message_gap_recovery_epoch"
_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH = "legacy_message_gap_recovery_epoch"
_OUTLOOK_DELTA_MATERIALIZATION_RECOVERY_CHECKPOINT = "outlook_delta_materialization_recovery_epoch"


@dataclass(frozen=True)
class AppCorpusDocument:
    """One provider object normalized for host-local retrieval."""

    connection_id: str
    provider: str
    object_id: str
    revision: str
    fetched_at: str
    source_ref: str
    title: str
    text: str
    metadata: Mapping[str, Any]
    freshness: Mapping[str, Any]
    deleted: bool = False
    source_event_at: str | None = None


@dataclass(frozen=True)
class AppCorpusSyncResult:
    """A bounded adapter page; only a complete page advances the success cursor."""

    documents: tuple[AppCorpusDocument, ...]
    checkpoint: str | None
    scanned: int
    complete: bool
    freshness: Mapping[str, Any]


class AppCorpusAdapter(Protocol):
    def sync(
        self,
        connection_id: str,
        *,
        checkpoint: str | None = None,
        limit: int = 100,
    ) -> AppCorpusSyncResult: ...


@dataclass(frozen=True)
class AppCorpusStatus:
    available: bool
    complete: bool
    connections: tuple[dict[str, Any], ...]
    document_count: int
    index_root: str
    ready: bool
    reason: str | None
    total_bytes: int


@dataclass(frozen=True)
class AppCorpusSync:
    complete: bool
    document_count: int
    freshness: Mapping[str, Any]
    indexed: bool
    reason: str | None
    scanned: int


@dataclass(frozen=True)
class AppCorpusHit:
    connection_id: str
    fetched_at: str
    object_id: str
    provider: str
    revision: str
    score: float
    snippet: str
    source_ref: str
    title: str
    source_event_at: str | None = None


@dataclass(frozen=True)
class AppCorpusSearch:
    backend: str
    complete: bool
    hits: tuple[AppCorpusHit, ...]
    reason: str | None


@dataclass(frozen=True)
class AppCorpusScope:
    adapter: str
    connection_id: str
    settings: Mapping[str, Any]


class AppCorpusCompanion:
    """Secure local corpus and isolated QMD collection for one exact vault."""

    def __init__(
        self,
        vault_root: Path | str,
        *,
        executable: Path | str = "qmd",
        index: str = _INDEX_NAME,
        index_root: Path | str | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).expanduser().resolve()
        if not self.vault_root.is_dir():
            raise ValidationError("app corpus requires an ordinary Seld vault directory")
        self.executable = recall_module._executable(executable)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", index):
            raise ValidationError(
                "QMD index name must use letters, numbers, dots, dashes, or underscores"
            )
        self.index = index
        if index_root is None:
            identity = hashlib.sha256(str(self.vault_root).encode("utf-8")).hexdigest()[:24]
            index_root = data_dir() / "app-corpus" / identity
        self.index_root = Path(index_root).expanduser().resolve()
        try:
            self.index_root.relative_to(self.vault_root)
        except ValueError:
            pass
        else:
            raise ValidationError("app corpus storage must stay outside the Seld vault")
        self.collection = (
            "seld-apps-"
            + hashlib.sha256(
                f"{self.vault_root}\0{self.index_root}\0{self.index}".encode()
            ).hexdigest()[:24]
        )

    @property
    def documents_root(self) -> Path:
        return self.index_root / "documents"

    @property
    def state_path(self) -> Path:
        return self.index_root / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.index_root / "locks/app-corpus.lock"

    def status(self, *, timeout_seconds: int = 10) -> AppCorpusStatus:
        _timeout(timeout_seconds, maximum=60, label="app corpus status")
        self._prepare_root()
        # State is published with one atomic replacement.  A read-only snapshot
        # must not wait behind a long provider or QMD writer.
        with _open_store(self.index_root) as store:
            state = _load_state(store)
            documents = _visible_documents(_documents(state))
            qmd = recall_module._resolved_executable(self.executable) is not None
            scopes = _scopes(state)
            ready = qmd and _all_scoped_qmd_current(state, documents)
            reason = None
            complete = bool(scopes) and all(
                _connection_complete(_connections(state).get(scope_id, {})) for scope_id in scopes
            )
            if not scopes:
                reason = "no app sources are configured"
            elif not qmd:
                reason = "QMD executable is unavailable; exact app search remains available"
            elif not ready:
                reason = "app corpus QMD index needs a refresh; exact app search remains available"
            return AppCorpusStatus(
                available=qmd,
                complete=complete,
                connections=tuple(
                    _connection_status(key, value, documents, state)
                    for key, value in _connections(state).items()
                ),
                document_count=len(documents),
                index_root=str(self.index_root),
                ready=ready,
                reason=reason,
                total_bytes=sum(_document_size(value) for value in documents.values()),
            )

    def configure(
        self, connection_id: str, *, adapter: str, settings: Mapping[str, Any] | None = None
    ) -> AppCorpusScope:
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise ValidationError("app corpus connection ID must be a non-empty string")
        if not isinstance(adapter, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,127}", adapter):
            raise ValidationError("app corpus adapter must be a supported source name")
        self._prepare_root()
        with (
            _open_store(self.index_root) as store,
            store.exclusive_file_lock("locks/app-corpus.lock"),
        ):
            state = _load_state(store)
            scopes = _scopes(state)
            existing = scopes.get(connection_id)
            if (
                settings is None
                and isinstance(existing, Mapping)
                and existing.get("adapter") == adapter
            ):
                configured_settings = _json_mapping(
                    existing.get("settings", {}), "app corpus scope settings"
                )
            else:
                configured_settings = _json_mapping(settings or {}, "app corpus scope settings")
            scopes[connection_id] = {
                "adapter": adapter,
                "connection_id": connection_id,
                "settings": configured_settings,
            }
            state["scopes"] = scopes
            _write_state(store, state)
        return AppCorpusScope(
            adapter=adapter,
            connection_id=connection_id,
            settings=configured_settings,
        )

    def scopes(self) -> tuple[AppCorpusScope, ...]:
        self._prepare_root()
        # This only reads an atomically published state snapshot.  In particular,
        # provider setup must remain available while a QMD refresh holds the
        # corpus writer lock.
        with _open_store(self.index_root) as store:
            return tuple(
                AppCorpusScope(
                    adapter=_string_record(value, "adapter"),
                    connection_id=key,
                    settings=_json_mapping(value.get("settings", {}), "app corpus scope settings"),
                )
                for key, value in sorted(_scopes(_load_state(store)).items())
            )

    def record_failure(self, connection_id: str, *, freshness: Mapping[str, Any]) -> None:
        """Keep the last material intact while making an unsuccessful cycle visible."""

        clean_freshness = _json_mapping(freshness, "app corpus freshness")
        if clean_freshness.get("status") not in {"partial", "error", "refused"}:
            raise ValidationError(
                "app corpus failure freshness must state partial, error, or refused"
            )
        self._prepare_root()
        with (
            _open_store(self.index_root) as store,
            store.exclusive_file_lock("locks/app-corpus.lock"),
        ):
            state = _load_state(store)
            connections = _connections(state)
            prior = connections.get(connection_id, {})
            entry = {
                "complete_checkpoint": _complete_checkpoint(prior),
                "freshness": clean_freshness,
                "last_scanned": prior.get("last_scanned", 0),
                "resume_checkpoint": _resume_checkpoint(prior),
                "updated_at": _now(),
            }
            recovery_epoch = _gmail_legacy_message_gap_recovery_epoch(prior)
            if recovery_epoch is not None:
                entry[_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT] = recovery_epoch
            outlook_recovery_epoch = _outlook_delta_materialization_recovery_epoch(prior)
            if outlook_recovery_epoch is not None:
                entry[_OUTLOOK_DELTA_MATERIALIZATION_RECOVERY_CHECKPOINT] = outlook_recovery_epoch
            connections[connection_id] = entry
            state["connections"] = connections
            _write_state(store, state)

    def sync_all(
        self,
        adapters: Mapping[str, AppCorpusAdapter],
        *,
        limit: int = 100,
        timeout_seconds: int = 120,
    ) -> tuple[dict[str, Any], ...]:
        """Run only explicitly configured connections; one failure never erases prior content."""

        refresh_timeout = _timeout(timeout_seconds, maximum=3_600, label="app corpus sync-all")
        provider_timeout = min(refresh_timeout, 600)
        results: list[dict[str, Any]] = []
        for scope in self.scopes():
            adapter = adapters.get(scope.adapter)
            if adapter is None:
                freshness = {"status": "refused", "detail": "configured adapter is unavailable"}
                self.record_failure(scope.connection_id, freshness=freshness)
                results.append({"connection_id": scope.connection_id, "freshness": freshness})
                continue
            try:
                result = self.sync(
                    adapter,
                    scope.connection_id,
                    limit=limit,
                    timeout_seconds=provider_timeout,
                    refresh=False,
                )
                results.append({"connection_id": scope.connection_id, **asdict(result)})
            except ValidationError as exc:
                freshness = {"status": "error", "detail": str(exc)[:512]}
                self.record_failure(scope.connection_id, freshness=freshness)
                results.append({"connection_id": scope.connection_id, "freshness": freshness})
        if results:
            results.append({"refresh": asdict(self.refresh(timeout_seconds=refresh_timeout))})
        return tuple(results)

    def sync(
        self,
        adapter: AppCorpusAdapter,
        connection_id: str,
        *,
        limit: int = 100,
        timeout_seconds: int = 120,
        refresh: bool = False,
    ) -> AppCorpusSync:
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise ValidationError("app corpus connection ID must be a non-empty string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValidationError("app corpus sync limit must be between 1 and 1000")
        timeout = _timeout(timeout_seconds, maximum=600, label="app corpus sync")
        self._prepare_root()
        deadline = time.monotonic() + timeout
        with _open_store(self.index_root) as store:
            with store.exclusive_file_lock(
                self._connection_lock_path(connection_id), timeout=_remaining(deadline)
            ):
                with store.exclusive_file_lock(
                    "locks/app-corpus.lock", timeout=_remaining(deadline)
                ):
                    state = _load_state(store)
                    prior = _connections(state).get(connection_id, {})
                    checkpoint = _resume_checkpoint(prior) or _complete_checkpoint(prior)
                    checkpoint, gmail_recovery_epoch = (
                        _gmail_legacy_message_gap_recovery_checkpoint(
                            _documents(state), connection_id, prior, checkpoint
                        )
                    )
                    checkpoint, outlook_recovery_epoch = (
                        _outlook_delta_materialization_recovery_checkpoint(prior, checkpoint)
                    )
                    expected = _sync_snapshot_token(state, connection_id)
                # Provider reads can take minutes.  The connection lock serializes their
                # cursor while the global state lock remains available to search and refresh.
                result = adapter.sync(connection_id, checkpoint=checkpoint, limit=limit)
                _check_deadline(deadline)
                _validate_sync_result(result, connection_id)
                with store.exclusive_file_lock(
                    "locks/app-corpus.lock", timeout=_remaining(deadline)
                ):
                    state = _load_state(store)
                    if _sync_snapshot_token(state, connection_id) != expected:
                        raise ValidationError(
                            "app corpus connection changed during provider read; retry the sync"
                        )
                    committed = self._commit_sync_unlocked(
                        store,
                        state,
                        connection_id,
                        prior,
                        result,
                        deadline,
                        gmail_recovery_epoch,
                        outlook_recovery_epoch,
                    )
            if not refresh:
                return committed
            refreshed = self._refresh_with_store(store, deadline)
            return replace(committed, indexed=refreshed.indexed, reason=refreshed.reason)

    def _commit_sync_unlocked(
        self,
        store: PinnedPathRoot,
        state: dict[str, Any],
        connection_id: str,
        prior: Mapping[str, Any],
        result: AppCorpusSyncResult,
        deadline: float,
        gmail_recovery_epoch: int | None,
        outlook_recovery_epoch: int | None,
    ) -> AppCorpusSync:
        documents = _documents(state)
        connections = _connections(state)
        freshness = _json_mapping(result.freshness, "app corpus freshness")
        freshness["sync_kind"] = (
            "incremental" if _complete_checkpoint(prior) is not None else "backfill"
        )
        proposed_documents = dict(documents)
        writes: list[tuple[str, bytes]] = []
        removals = False
        for document in result.documents:
            _check_deadline(deadline)
            key = _document_key(document.connection_id, document.object_id)
            if document.deleted:
                removals = proposed_documents.pop(key, None) is not None or removals
                immutable_prefix = "outlook-immutable:"
                immutable_parent_id = (
                    document.object_id.removeprefix(immutable_prefix)
                    if document.provider == "outlook_mail"
                    and document.object_id.startswith(immutable_prefix)
                    else None
                )
                if immutable_parent_id:
                    for candidate_key, candidate in tuple(proposed_documents.items()):
                        metadata = (
                            candidate.get("metadata") if isinstance(candidate, Mapping) else None
                        )
                        if (
                            isinstance(candidate, Mapping)
                            and candidate.get("connection_id") == document.connection_id
                            and isinstance(metadata, Mapping)
                            and metadata.get("parent_message_id") == immutable_parent_id
                        ):
                            proposed_documents.pop(candidate_key)
                            removals = True
                continue
            payload = _document_payload(document)
            encoded = payload.encode("utf-8")
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise ValidationError("app corpus document exceeds its size bound")
            if key not in proposed_documents and len(proposed_documents) >= MAX_DOCUMENTS:
                raise ValidationError("app corpus document limit reached")
            digest = hashlib.sha256(encoded).hexdigest()
            # Content-addressed leaves make each published state point to immutable bytes.
            path = f"documents/{key}-{digest[:16]}.md"
            record = {
                "connection_id": document.connection_id,
                "digest": digest,
                "fetched_at": document.fetched_at,
                "object_id": document.object_id,
                "path": path,
                "provider": document.provider,
                "revision": document.revision,
                "source_event_at": _source_event_at(document),
                "source_ref": document.source_ref,
                "size": len(encoded),
                "title": document.title,
                "metadata": _json_mapping(document.metadata, "app corpus document metadata"),
            }
            existing = proposed_documents.get(key)
            # Preserve the first release's flat key leaf when its bytes are unchanged.
            existing_path = _stored_path(existing) if isinstance(existing, dict) else None
            if isinstance(existing, dict) and existing.get("digest") == digest and existing_path:
                record["path"] = existing_path
            proposed_documents[key] = record
            if (
                not isinstance(existing, dict)
                or existing.get("digest") != record["digest"]
                or _stored_path(existing) != record["path"]
            ):
                writes.append((path, encoded))
            removals = (isinstance(existing, dict) and existing.get("digest") != digest) or removals
        if sum(_document_size(value) for value in proposed_documents.values()) > MAX_TOTAL_BYTES:
            raise ValidationError("app corpus total content limit reached")
        entry = {
            "freshness": freshness,
            "last_scanned": result.scanned,
            "updated_at": _now(),
        }
        if result.complete:
            entry["complete_checkpoint"] = result.checkpoint
            entry["resume_checkpoint"] = None
        else:
            entry["complete_checkpoint"] = _complete_checkpoint(prior)
            entry["resume_checkpoint"] = result.checkpoint or _resume_checkpoint(prior)
        if gmail_recovery_epoch is not None:
            entry[_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT] = gmail_recovery_epoch
        if outlook_recovery_epoch is not None:
            entry[_OUTLOOK_DELTA_MATERIALIZATION_RECOVERY_CHECKPOINT] = outlook_recovery_epoch
        for path, encoded in writes:
            _check_deadline(deadline)
            store.atomic_write(path, encoded)
        content_changed = bool(writes or removals)
        state["documents"] = proposed_documents
        connections[connection_id] = entry
        if content_changed:
            state["index_fingerprint"] = None
        _write_state(store, state)
        indexed, reason = (
            (True, None)
            if not content_changed
            and _stored_index_fingerprint(state)
            == _fingerprint(_visible_documents(proposed_documents))
            else (False, "app corpus QMD refresh is pending")
        )
        return AppCorpusSync(
            complete=result.complete,
            document_count=len(proposed_documents),
            freshness=freshness,
            indexed=indexed,
            reason=reason,
            scanned=result.scanned,
        )

    def refresh(self, *, timeout_seconds: int = 120) -> AppCorpusSync:
        # Routine refreshes keep the short default.  An explicit first per-source
        # embedding pass may need longer than a page-sized provider sync.
        timeout = _timeout(timeout_seconds, maximum=3_600, label="app corpus refresh")
        self._prepare_root()
        deadline = time.monotonic() + timeout
        with _open_store(self.index_root) as store:
            return self._refresh_with_store(store, deadline)

    def search(
        self,
        query: str,
        *,
        connection_id: str | None = None,
        limit: int = 8,
        provider: str | None = None,
        timeout_seconds: int = 20,
    ) -> AppCorpusSearch:
        clean, terms = _query(query)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
            raise ValidationError(f"app corpus result limit must be between 1 and {MAX_RESULTS}")
        timeout = _timeout(timeout_seconds, maximum=60, label="app corpus search")
        self._prepare_root()
        deadline = time.monotonic() + timeout
        with _open_store(self.index_root) as store:
            # State is atomically published.  Copy that committed snapshot before doing
            # any potentially slow QMD or exact-text work without the global lock.
            state = _load_state(store)
            all_documents = dict(_documents(state))
            visible_documents = _visible_documents(all_documents)
            scopes = _scopes(state)
            complete = bool(scopes) and all(
                _connection_complete(_connections(state).get(scope_id, {})) for scope_id in scopes
            )
            qmd_bindings = {
                connection: binding
                for connection in {
                    _string_record(record, "connection_id") for record in visible_documents.values()
                }
                if (binding := _current_qmd_binding(state, visible_documents, connection))
                is not None
            }
            documents = _filtered_documents(
                visible_documents, connection_id=connection_id, provider=provider
            )
            selected_connections = {
                _string_record(record, "connection_id") for record in documents.values()
            }
            current = bool(selected_connections) and selected_connections.issubset(qmd_bindings)
            hits: tuple[AppCorpusHit, ...] | None
            if recall_module._resolved_executable(self.executable) is None:
                hits = _lexical_hits(
                    store,
                    self,
                    documents,
                    terms=terms,
                    limit=limit,
                    deadline=deadline,
                )
                reason = "QMD executable is unavailable; local lexical app search was used"
                backend = "local"
            elif not current:
                hits = _lexical_hits(
                    store,
                    self,
                    documents,
                    terms=terms,
                    limit=limit,
                    deadline=deadline,
                )
                reason = "app corpus QMD index is not current; local lexical app search was used"
                backend = "local"
            else:
                hits = _scoped_qmd_hits(
                    store,
                    self,
                    documents,
                    qmd_bindings,
                    query=clean,
                    terms=terms,
                    limit=limit,
                    deadline=deadline,
                )
                if hits is None:
                    hits = _lexical_hits(
                        store,
                        self,
                        documents,
                        terms=terms,
                        limit=limit,
                        deadline=deadline,
                    )
                    reason = "QMD app search failed; local lexical app search was used"
                    backend = "local"
                else:
                    reason = None
                    backend = "qmd"
            assert hits is not None
            return AppCorpusSearch(backend, complete, hits, reason)

    def read(self, connection_id: str, object_id: str) -> AppCorpusDocument | None:
        self._prepare_root()
        with (
            _open_store(self.index_root) as store,
            store.exclusive_file_lock("locks/app-corpus.lock"),
        ):
            state = _load_state(store)
            record = _documents(state).get(_document_key(connection_id, object_id))
            if not isinstance(record, dict):
                return None
            if not _visible_documents({_document_key(connection_id, object_id): record}):
                return None
            path = _stored_path(record)
            if path is None:
                raise ValidationError("app corpus document state is invalid")
            content = store.read_regular_file(
                path, label="app corpus document", max_bytes=MAX_DOCUMENT_BYTES
            )
            if content is None:
                raise ValidationError("app corpus document disappeared before it was read")
            text = _strip_document_header(content.decode("utf-8"))
            return AppCorpusDocument(
                connection_id=_string_record(record, "connection_id"),
                provider=_string_record(record, "provider"),
                object_id=_string_record(record, "object_id"),
                revision=_string_record(record, "revision"),
                fetched_at=_string_record(record, "fetched_at"),
                source_ref=_string_record(record, "source_ref"),
                title=_string_record(record, "title"),
                text=text,
                metadata=_metadata_record(record),
                freshness={},
                source_event_at=_stored_source_event_at(record),
            )

    def _refresh_with_store(self, store: PinnedPathRoot, deadline: float) -> AppCorpusSync:
        """Refresh one immutable state snapshot without holding the corpus state lock."""

        with store.exclusive_file_lock(
            "locks/app-corpus-refresh.lock", timeout=_remaining(deadline)
        ):
            with store.exclusive_file_lock("locks/app-corpus.lock", timeout=_remaining(deadline)):
                state = _load_state(store)
                documents = _visible_documents(_documents(state))
                complete = all(
                    _connection_complete(value) for value in _connections(state).values()
                )
                if _all_scoped_qmd_current(state, documents):
                    return AppCorpusSync(
                        complete=complete,
                        document_count=len(documents),
                        freshness={},
                        indexed=True,
                        reason=None,
                        scanned=0,
                    )
                _cleanup_orphan_documents(store, documents, deadline=deadline)
            if recall_module._resolved_executable(self.executable) is None:
                return self._refresh_result_after_unavailable_qmd(store, deadline)
            from continuity_kernel.app_corpus_qmd import (
                ScopedQMDIndexManager,
                ScopedQMDRefreshError,
            )

            manager = ScopedQMDIndexManager(
                store,
                self.index_root,
                self.executable,
                self._environment(),
                self.index,
                run_command=recall_module._run_command,
            )
            failures = False
            changed_during_refresh = False
            connection_counts: dict[str, int] = {}
            for record in documents.values():
                connection_id = _string_record(record, "connection_id")
                connection_counts[connection_id] = connection_counts.get(connection_id, 0) + 1
            # Give short sources a chance to become semantic-searchable before a
            # large source consumes the shared refresh deadline.
            connection_ids = sorted(
                connection_counts,
                key=lambda connection_id: (connection_counts[connection_id], connection_id),
            )
            for connection_id in connection_ids:
                snapshot = _scope_qmd_snapshot(documents, connection_id)
                if snapshot is None:
                    continue
                try:
                    bindings = manager.refresh(snapshot, deadline)
                except (ScopedQMDRefreshError, ValidationError):
                    failures = True
                    # A source-level QMD failure must not prevent another scope
                    # from becoming available while there is time left.
                    if time.monotonic() >= deadline:
                        break
                    continue
                if len(bindings) != 1:
                    failures = True
                    break
                binding = bindings[0]
                if (
                    binding.connection_id != connection_id
                    or binding.snapshot_fingerprint != snapshot.fingerprint
                    or binding.scope_fingerprint != snapshot.fingerprint
                ):
                    failures = True
                    break
                with store.exclusive_file_lock(
                    "locks/app-corpus.lock", timeout=_remaining(deadline)
                ):
                    state = _load_state(store)
                    current_documents = _visible_documents(_documents(state))
                    if (
                        _scope_qmd_fingerprint(current_documents, connection_id)
                        != snapshot.fingerprint
                    ):
                        changed_during_refresh = True
                        continue
                    bindings_state = _qmd_bindings(state)
                    value = _binding_state(binding)
                    if bindings_state.get(connection_id) != value:
                        bindings_state[connection_id] = value
                        # Older corpus state predates qmd_bindings.  In that
                        # migration case _qmd_bindings supplies a new mapping,
                        # so publish it back onto the state before writing.
                        state["qmd_bindings"] = bindings_state
                        _write_state(store, state)
            with store.exclusive_file_lock("locks/app-corpus.lock", timeout=_remaining(deadline)):
                state = _load_state(store)
                current_documents = _visible_documents(_documents(state))
                ready = _all_scoped_qmd_current(state, current_documents)
                current_fingerprint = _fingerprint(current_documents)
                if (_stored_index_fingerprint(state) != current_fingerprint and ready) or (
                    _stored_index_fingerprint(state) is not None and not ready
                ):
                    state["index_fingerprint"] = current_fingerprint if ready else None
                    _write_state(store, state)
                reason = None
                if failures:
                    reason = (
                        "one or more app source QMD indexes are pending; "
                        "exact app search remains available"
                    )
                elif changed_during_refresh or not ready:
                    reason = (
                        "app corpus changed while QMD was refreshing; "
                        "exact app search remains available"
                    )
                return AppCorpusSync(
                    complete=all(
                        _connection_complete(value) for value in _connections(state).values()
                    ),
                    document_count=len(current_documents),
                    freshness={},
                    indexed=ready,
                    reason=reason,
                    scanned=0,
                )

    def _refresh_result_after_unavailable_qmd(
        self, store: PinnedPathRoot, deadline: float
    ) -> AppCorpusSync:
        with store.exclusive_file_lock("locks/app-corpus.lock", timeout=_remaining(deadline)):
            state = _load_state(store)
            return AppCorpusSync(
                complete=all(_connection_complete(value) for value in _connections(state).values()),
                document_count=len(_visible_documents(_documents(state))),
                freshness={},
                indexed=False,
                reason="QMD executable is unavailable; exact app search remains available",
                scanned=0,
            )

    def _command(self, *arguments: str) -> tuple[str, ...]:
        return (self.executable, "--index", self.index, *arguments)

    def _collection_binding(self) -> dict[str, str]:
        return {
            "collection": self.collection,
            "documents_root": str(self.documents_root),
            "mask": "**/*.md",
        }

    def _connection_lock_path(self, connection_id: str) -> str:
        identity = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()
        return f"locks/connection-{identity}.lock"

    def _environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "LANG", "LC_ALL", "SystemRoot", "TMPDIR", "WINDIR"}
        }
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        environment["XDG_CACHE_HOME"] = str(self.index_root / "cache")
        environment["QMD_CONFIG_DIR"] = str(self.index_root / "config")
        return environment

    def _prepare_root(self) -> None:
        self.index_root.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.index_root.parent.chmod(0o700)
        self.index_root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.index_root.chmod(0o700)
        with _open_store(self.index_root) as store:
            store.ensure_directory("documents")
            store.ensure_directory("locks")


def _load_state(store: PinnedPathRoot) -> dict[str, Any]:
    encoded = store.read_regular_file(
        "state.json", label="app corpus state", max_bytes=MAX_STATE_BYTES, missing_ok=True
    )
    if encoded is None:
        return {
            "version": _STATE_VERSION,
            "documents": {},
            "connections": {},
            "index_fingerprint": None,
            "qmd_bindings": {},
            "scopes": {},
        }
    try:
        state = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("app corpus state is invalid") from exc
    if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
        raise ValidationError("app corpus state version is unsupported")
    _documents(state)
    _connections(state)
    _qmd_bindings(state)
    _scopes(state)
    return state


def _write_state(store: PinnedPathRoot, state: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise ValidationError("app corpus state exceeds its size bound")
    store.atomic_write("state.json", encoded)


def _documents(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    value = state.get("documents")
    if not isinstance(value, dict):
        raise ValidationError("app corpus document state is invalid")
    if any(not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()):
        raise ValidationError("app corpus document state is invalid")
    return value


def _connections(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    value = state.get("connections")
    if not isinstance(value, dict):
        raise ValidationError("app corpus connection state is invalid")
    if any(not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()):
        raise ValidationError("app corpus connection state is invalid")
    return value


def _scopes(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    value = state.get("scopes", {})
    if not isinstance(value, dict):
        raise ValidationError("app corpus scope state is invalid")
    if any(not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()):
        raise ValidationError("app corpus scope state is invalid")
    return value


def _validate_sync_result(result: AppCorpusSyncResult, connection_id: str) -> None:
    if not isinstance(result, AppCorpusSyncResult):
        raise ValidationError("app corpus adapter returned an invalid sync result")
    if (
        not isinstance(result.scanned, int)
        or isinstance(result.scanned, bool)
        or result.scanned < 0
    ):
        raise ValidationError("app corpus adapter returned an invalid scanned count")
    if not isinstance(result.complete, bool):
        raise ValidationError("app corpus adapter returned an invalid completion state")
    if result.checkpoint is not None and (
        not isinstance(result.checkpoint, str)
        or len(result.checkpoint) > MAX_CHECKPOINT_BYTES
        or len(result.checkpoint.encode("utf-8")) > MAX_CHECKPOINT_BYTES
    ):
        raise ValidationError("app corpus adapter returned an invalid checkpoint")
    _json_mapping(result.freshness, "app corpus freshness")
    for document in result.documents:
        if not isinstance(document, AppCorpusDocument):
            raise ValidationError("app corpus adapter returned an invalid document")
        if document.connection_id != connection_id:
            raise ValidationError("app corpus adapter returned a document for another connection")
        for value, label in (
            (document.provider, "provider"),
            (document.object_id, "object ID"),
            (document.revision, "revision"),
            (document.fetched_at, "fetched time"),
            (document.source_ref, "source reference"),
            (document.title, "title"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 8_192:
                raise ValidationError(f"app corpus document has an invalid {label}")
        if _OBJECT_ID.fullmatch(document.object_id) is None:
            raise ValidationError("app corpus document has an invalid object ID")
        if not isinstance(document.text, str) or "\x00" in document.text:
            raise ValidationError("app corpus document text is invalid")
        if document.source_event_at is not None and (
            not isinstance(document.source_event_at, str)
            or not document.source_event_at.strip()
            or len(document.source_event_at) > 8_192
        ):
            raise ValidationError("app corpus document has an invalid source event time")
        _json_mapping(document.metadata, "app corpus document metadata")
        _json_mapping(document.freshness, "app corpus document freshness")


def _document_payload(document: AppCorpusDocument) -> str:
    # The header makes documents intelligible to QMD without leaking identifier data into paths.
    return f"# {document.title}\n\n{document.text}"


def _document_key(connection_id: str, object_id: str) -> str:
    return hashlib.sha256(f"{connection_id}\0{object_id}".encode()).hexdigest()


def _stored_path(value: Mapping[str, Any]) -> str | None:
    path = value.get("path")
    if not isinstance(path, str) or not re.fullmatch(
        r"documents/[0-9a-f]{64}(?:-[0-9a-f]{16})?\.md", path
    ):
        return None
    return path


def _document_size(value: Mapping[str, Any]) -> int:
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValidationError("app corpus document state is invalid")
    return size


def _fingerprint(documents: Mapping[str, Mapping[str, Any]]) -> str:
    rows = [(key, value.get("digest")) for key, value in documents.items()]
    return hashlib.sha256(
        json.dumps(sorted(rows), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stored_index_fingerprint(state: Mapping[str, Any]) -> str | None:
    value = state.get("index_fingerprint")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _sync_snapshot_token(state: Mapping[str, Any], connection_id: str) -> str:
    """Bind a provider read to the exact cursor and configured scope it started with."""

    connection = _connections(state).get(connection_id, {})
    scope = _scopes(state).get(connection_id)
    value = {
        "complete_checkpoint": _complete_checkpoint(connection),
        _GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT: _gmail_legacy_message_gap_recovery_epoch(
            connection
        ),
        _OUTLOOK_DELTA_MATERIALIZATION_RECOVERY_CHECKPOINT: (
            _outlook_delta_materialization_recovery_epoch(connection)
        ),
        "resume_checkpoint": _resume_checkpoint(connection),
        "scope": scope,
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _source_event_at(document: AppCorpusDocument) -> str | None:
    if document.source_event_at is not None:
        return document.source_event_at
    for key in (
        "source_event_at",
        "sent_at",
        "received_at",
        "start_at",
        "created_at",
        "modified_at",
    ):
        value = document.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _stored_source_event_at(record: Mapping[str, Any]) -> str | None:
    value = record.get("source_event_at")
    return value if isinstance(value, str) and value.strip() else None


def _stored_collection_binding(state: Mapping[str, Any]) -> dict[str, str] | None:
    value = state.get("collection_binding")
    if not isinstance(value, Mapping):
        return None
    collection = value.get("collection")
    documents_root = value.get("documents_root")
    mask = value.get("mask")
    if (
        not isinstance(collection, str)
        or not isinstance(documents_root, str)
        or not isinstance(mask, str)
    ):
        return None
    return {
        "collection": collection,
        "documents_root": documents_root,
        "mask": mask,
    }


def _qmd_bindings(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    value = state.get("qmd_bindings", {})
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()
    ):
        raise ValidationError("app corpus QMD binding state is invalid")
    return value


def _scope_qmd_fingerprint(
    documents: Mapping[str, Mapping[str, Any]], connection_id: str
) -> str | None:
    rows: list[tuple[str, str, str]] = []
    for key, record in documents.items():
        if record.get("connection_id") != connection_id:
            continue
        path = _stored_path(record)
        digest = record.get("digest")
        if (
            path is None
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValidationError("app corpus document state is invalid")
        rows.append((key, digest, path))
    if not rows:
        return None
    return hashlib.sha256(
        json.dumps(sorted(rows), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _current_qmd_binding(
    state: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]], connection_id: str
) -> dict[str, str] | None:
    expected = _scope_qmd_fingerprint(documents, connection_id)
    value = _qmd_bindings(state).get(connection_id)
    if expected is None or not isinstance(value, Mapping):
        return None
    snapshot = value.get("snapshot_fingerprint")
    scope = value.get("scope_fingerprint")
    index = value.get("index")
    collection = value.get("collection")
    documents_root = value.get("documents_root")
    if (
        not isinstance(snapshot, str)
        or not isinstance(scope, str)
        or not isinstance(index, str)
        or not isinstance(collection, str)
        or not isinstance(documents_root, str)
    ):
        return None
    if snapshot != expected or scope != expected:
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", index) is None:
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", collection) is None:
        return None
    return {
        "collection": collection,
        "index": index,
        "scope_fingerprint": scope,
        "snapshot_fingerprint": snapshot,
        "documents_root": documents_root,
    }


def _all_scoped_qmd_current(
    state: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]]
) -> bool:
    connection_ids = {_string_record(record, "connection_id") for record in documents.values()}
    return bool(connection_ids) and all(
        _current_qmd_binding(state, documents, connection_id) is not None
        for connection_id in connection_ids
    )


def _scope_qmd_snapshot(
    documents: Mapping[str, Mapping[str, Any]], connection_id: str
) -> Any | None:
    fingerprint = _scope_qmd_fingerprint(documents, connection_id)
    if fingerprint is None:
        return None
    from continuity_kernel.app_corpus_qmd import QMDScopedRecord, QMDScopedSnapshot

    records = tuple(
        QMDScopedRecord(
            key=key,
            connection_id=connection_id,
            document_path=_stored_path(record) or "",
            digest=_string_record(record, "digest"),
        )
        for key, record in sorted(documents.items())
        if record.get("connection_id") == connection_id
    )
    return QMDScopedSnapshot(fingerprint=fingerprint, records=records)


def _binding_state(binding: Any) -> dict[str, str]:
    value = {
        "snapshot_fingerprint": getattr(binding, "snapshot_fingerprint", None),
        "scope_fingerprint": getattr(binding, "scope_fingerprint", None),
        "index": getattr(binding, "index", None),
        "collection": getattr(binding, "collection", None),
        "documents_root": str(getattr(binding, "documents_root", "")),
    }
    if any(not isinstance(item, str) for item in value.values()):
        raise ValidationError("scoped app QMD binding is invalid")
    return {key: item for key, item in value.items() if isinstance(item, str)}


def _config_has_collection_binding(
    store: PinnedPathRoot, binding: Mapping[str, str], *, index: str
) -> bool:
    """Read QMD's local YAML without starting QMD or touching its index."""

    encoded = store.read_regular_file(
        f"config/{index}.yml",
        label="app corpus QMD configuration",
        max_bytes=1_024 * 1_024,
        missing_ok=True,
    )
    if encoded is None:
        return False
    try:
        lines = encoded.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    collection = binding["collection"]
    found = False
    values: dict[str, str] = {}
    for line in lines:
        if re.fullmatch(rf"  {re.escape(collection)}:\s*", line):
            found = True
            continue
        if found and line.startswith("  ") and not line.startswith("    "):
            break
        if not found:
            continue
        match = re.fullmatch(r"    (path|pattern):\s*(.*)", line)
        if match is not None:
            values[match.group(1)] = _yaml_scalar(match.group(2))
    return (
        found
        and values.get("path") == binding["documents_root"]
        and values.get("pattern") == binding["mask"]
    )


def _yaml_scalar(value: str) -> str:
    """Decode the small scalar subset emitted by QMD's YAML writer."""

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, str) else value
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _complete_checkpoint(value: Mapping[str, Any]) -> str | None:
    checkpoint = value.get("complete_checkpoint")
    return checkpoint if isinstance(checkpoint, str) else None


def _resume_checkpoint(value: Mapping[str, Any]) -> str | None:
    checkpoint = value.get("resume_checkpoint")
    return checkpoint if isinstance(checkpoint, str) else None


def _gmail_legacy_message_gap_recovery_checkpoint(
    documents: Mapping[str, Mapping[str, Any]],
    connection_id: str,
    prior: Mapping[str, Any],
    checkpoint: str | None,
) -> tuple[str | None, int | None]:
    """Request one Gmail baseline only for the legacy body-gap document shape."""

    recovery_epoch = _gmail_legacy_message_gap_recovery_epoch(prior)
    if recovery_epoch is not None:
        return checkpoint, recovery_epoch
    if not any(
        _is_legacy_gmail_message_gap(record, connection_id) for record in documents.values()
    ):
        return checkpoint, None
    if checkpoint is None:
        return checkpoint, None
    try:
        decoded = json.loads(checkpoint)
    except json.JSONDecodeError:
        return checkpoint, None
    if not isinstance(decoded, dict) or decoded.get("provider") != "google":
        return checkpoint, None
    existing = _gmail_legacy_message_gap_recovery_epoch(decoded)
    gmail = decoded.get("gmail")
    if existing is None and isinstance(gmail, Mapping):
        existing = _positive_int(gmail.get(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_EPOCH))
    if existing is not None:
        return checkpoint, existing
    decoded[_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT] = 1
    encoded = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
        return checkpoint, None
    return encoded, 1


def _gmail_legacy_message_gap_recovery_epoch(value: Mapping[str, Any]) -> int | None:
    return _positive_int(value.get(_GMAIL_LEGACY_MESSAGE_GAP_RECOVERY_CHECKPOINT))


def _outlook_delta_materialization_recovery_checkpoint(
    prior: Mapping[str, Any], checkpoint: str | None
) -> tuple[str | None, int | None]:
    """Rewind one legacy Microsoft detail-read failure only once."""

    recovery_epoch = _outlook_delta_materialization_recovery_epoch(prior)
    if recovery_epoch is not None:
        return checkpoint, recovery_epoch
    freshness = prior.get("freshness")
    if not isinstance(freshness, Mapping) or freshness.get("status") != "error":
        return checkpoint, None
    if checkpoint is None:
        return checkpoint, None
    try:
        decoded = json.loads(checkpoint)
    except json.JSONDecodeError:
        return checkpoint, None
    if not isinstance(decoded, dict) or decoded.get("provider") != "microsoft":
        return checkpoint, None
    mail = decoded.get("mail")
    if not isinstance(mail, dict):
        return checkpoint, None
    delta_checkpoint = mail.get("delta_checkpoint")
    if not isinstance(delta_checkpoint, str):
        return checkpoint, None
    try:
        mail["delta_checkpoint"] = rewind_message_page_for_materialization_retry(delta_checkpoint)
    except ValidationError:
        return checkpoint, None
    decoded[_OUTLOOK_DELTA_MATERIALIZATION_RECOVERY_CHECKPOINT] = 1
    encoded = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
        return checkpoint, None
    return encoded, 1


def _outlook_delta_materialization_recovery_epoch(value: Mapping[str, Any]) -> int | None:
    return _positive_int(value.get(_OUTLOOK_DELTA_MATERIALIZATION_RECOVERY_CHECKPOINT))


def _is_legacy_gmail_message_gap(record: Mapping[str, Any], connection_id: str) -> bool:
    if record.get("connection_id") != connection_id or record.get("provider") != "gmail":
        return False
    object_id = record.get("object_id")
    if not isinstance(object_id, str) or not object_id.startswith("gmail:"):
        return False
    message_id = object_id.removeprefix("gmail:")
    metadata = record.get("metadata")
    return (
        bool(message_id)
        and record.get("source_ref") == f"gmail:message:{message_id}"
        and isinstance(metadata, Mapping)
        and metadata.get("extraction_status") == "gap"
    )


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _connection_complete(value: Mapping[str, Any]) -> bool:
    freshness = value.get("freshness")
    return (
        isinstance(freshness, dict)
        and freshness.get("status") == "complete"
        and not _resume_checkpoint(value)
    )


def _connection_status(
    connection_id: str,
    value: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "connection_id": connection_id,
        "extraction_status": _extraction_status_counts(documents, connection_id),
        "has_completed_snapshot": _complete_checkpoint(value) is not None,
        "has_resume": _resume_checkpoint(value) is not None,
        "media_coverage": (
            _media_coverage_counts(documents, connection_id)
            if _scopes(state).get(connection_id, {}).get("adapter") == "whatsapp"
            else {}
        ),
        "qmd_ready": _current_qmd_binding(state, documents, connection_id) is not None,
        "freshness": value.get("freshness", {}),
        "last_scanned": value.get("last_scanned", 0),
        "updated_at": value.get("updated_at"),
    }


def _extraction_status_counts(
    documents: Mapping[str, Mapping[str, Any]], connection_id: str
) -> dict[str, int]:
    counts = {"gap": 0, "partial": 0}
    for record in documents.values():
        if record.get("connection_id") != connection_id:
            continue
        metadata = record.get("metadata")
        status = metadata.get("extraction_status") if isinstance(metadata, Mapping) else None
        if status in counts:
            counts[status] += 1
    return counts


def _media_coverage_counts(
    documents: Mapping[str, Mapping[str, Any]], connection_id: str
) -> dict[str, dict[str, int]]:
    """Report WhatsApp media coverage from persisted metadata without reopening content."""

    coverage: dict[str, dict[str, int]] = {}
    for record in documents.values():
        if record.get("connection_id") != connection_id:
            continue
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        media_type = _media_coverage_type(metadata.get("media_type"))
        if media_type is None:
            continue
        counts = coverage.setdefault(media_type, {"total": 0})
        counts["total"] += 1
        status = metadata.get("media_extraction_status")
        if media_type == "audio":
            if metadata.get("transcript_source") == "local_relay":
                counts["local_relay_transcripts"] = counts.get("local_relay_transcripts", 0) + 1
            elif status == "transcript_unavailable" or status is None:
                counts["transcript_unavailable"] = counts.get("transcript_unavailable", 0) + 1
        elif status in {"local_text", "partial_text"}:
            counts[str(status)] = counts.get(str(status), 0) + 1
        elif status == "not_extracted" or status is None:
            counts["not_extracted"] = counts.get("not_extracted", 0) + 1
    return {media_type: coverage[media_type] for media_type in sorted(coverage)}


def _media_coverage_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if not normalized or normalized in {"none", "text"}:
        return None
    if normalized in {"audio", "ptt", "voice"} or normalized.startswith("audio/"):
        return "audio"
    return normalized.split("/", 1)[0]


def _json_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    try:
        converted = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must contain JSON values") from exc
    if not isinstance(converted, dict):
        raise ValidationError(f"{label} must be an object")
    return converted


def _query(value: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise ValidationError("app corpus query must be text")
    clean = " ".join(value.split())
    if not clean or len(clean) > MAX_QUERY_CHARACTERS or "\x00" in clean:
        raise ValidationError(
            f"app corpus query must contain 1 to {MAX_QUERY_CHARACTERS} characters"
        )
    terms = tuple(
        dict.fromkeys(word.casefold() for word in re.findall(r"[^\W_]+", clean, re.UNICODE))
    )
    if not terms:
        raise ValidationError("app corpus query must contain at least one word")
    return clean, terms


def _fallback_hits(
    store: PinnedPathRoot,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    terms: tuple[str, ...],
    limit: int,
    deadline: float,
) -> tuple[AppCorpusHit, ...]:
    matches: list[tuple[int, str, AppCorpusHit]] = []
    for _key, record in documents.items():
        _check_deadline(deadline)
        path = _stored_path(record)
        if path is None:
            raise ValidationError("app corpus document state is invalid")
        encoded = store.read_regular_file(
            path, label="app corpus document", max_bytes=MAX_DOCUMENT_BYTES
        )
        if encoded is None:
            raise ValidationError("app corpus document disappeared before it was searched")
        text = encoded.decode("utf-8")
        folded = text.casefold()
        counts = tuple(folded.count(term) for term in terms)
        if not counts or any(count == 0 for count in counts):
            continue
        score = float(sum(counts))
        matches.append(
            (
                sum(counts),
                _string_record(record, "fetched_at"),
                AppCorpusHit(
                    connection_id=_string_record(record, "connection_id"),
                    fetched_at=_string_record(record, "fetched_at"),
                    object_id=_string_record(record, "object_id"),
                    provider=_string_record(record, "provider"),
                    revision=_string_record(record, "revision"),
                    score=score if math.isfinite(score) else 0.0,
                    snippet=_snippet(_strip_document_header(text), terms),
                    source_ref=_string_record(record, "source_ref"),
                    title=_string_record(record, "title"),
                    source_event_at=_stored_source_event_at(record),
                ),
            )
        )
    matches.sort(key=lambda item: (item[1], item[2].object_id), reverse=True)
    matches.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[2] for item in matches[:limit])


def _lexical_hits(
    store: PinnedPathRoot,
    companion: AppCorpusCompanion,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    terms: tuple[str, ...],
    limit: int,
    deadline: float,
) -> tuple[AppCorpusHit, ...]:
    """Return FTS metadata candidates and read only the selected document bodies."""

    from continuity_kernel import app_corpus_lexical

    matches = app_corpus_lexical.search(
        store,
        companion.index_root,
        documents,
        terms=terms,
        limit=limit,
        deadline=deadline,
    )
    if matches is None:
        raise ValidationError(
            "app corpus lexical index is unavailable or still building; retry the search"
        )
    hits: list[AppCorpusHit] = []
    for match in matches:
        _check_deadline(deadline)
        record = documents.get(match.key)
        if record is None:
            continue
        path = _stored_path(record)
        if path is None:
            continue
        content = store.read_regular_file(
            path,
            label="app corpus document",
            max_bytes=MAX_DOCUMENT_BYTES,
            missing_ok=True,
        )
        if content is None or hashlib.sha256(content).hexdigest() != record.get("digest"):
            continue
        text = _strip_document_header(content.decode("utf-8"))
        snippet_source = (
            text
            if all(term in text.casefold() for term in terms)
            else f"{_string_record(record, 'title')}\n{match.metadata_text}"
        )
        hits.append(
            AppCorpusHit(
                connection_id=_string_record(record, "connection_id"),
                fetched_at=_string_record(record, "fetched_at"),
                object_id=_string_record(record, "object_id"),
                provider=_string_record(record, "provider"),
                revision=_string_record(record, "revision"),
                score=match.score,
                snippet=_snippet(snippet_source, terms),
                source_ref=_string_record(record, "source_ref"),
                title=_string_record(record, "title"),
                source_event_at=_stored_source_event_at(record),
            )
        )
    return tuple(hits)


def _filtered_documents(
    documents: Mapping[str, Mapping[str, Any]],
    *,
    connection_id: str | None,
    provider: str | None,
) -> dict[str, Mapping[str, Any]]:
    if connection_id is not None and (not isinstance(connection_id, str) or not connection_id):
        raise ValidationError("app corpus connection filter must be text")
    if provider is not None and (not isinstance(provider, str) or not provider):
        raise ValidationError("app corpus provider filter must be text")
    return {
        key: value
        for key, value in documents.items()
        if (connection_id is None or value.get("connection_id") == connection_id)
        and (provider is None or value.get("provider") == provider)
    }


def _visible_documents(
    documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Return records visible under the current local Slack channel policy.

    Existing corpus leaves are intentionally left in place.  This filter is the
    read boundary for direct reads, lexical search, and every QMD physical view.
    """

    policies: dict[str, SlackChannelAccessPolicy | None] = {}
    visible: dict[str, Mapping[str, Any]] = {}
    for key, record in documents.items():
        if record.get("provider") != "slack":
            visible[key] = record
            continue
        connection_id = record.get("connection_id")
        if not isinstance(connection_id, str):
            # A malformed Slack record must not escape a policy boundary.
            continue
        policy = policies.get(connection_id)
        if connection_id not in policies:
            policy = load_slack_channel_access(connection_id)
            policies[connection_id] = policy
        if policy is None or policy.allows_document(record.get("metadata")):
            visible[key] = record
    return visible


def _scoped_qmd_hits(
    store: PinnedPathRoot,
    companion: AppCorpusCompanion,
    documents: Mapping[str, Mapping[str, Any]],
    bindings: Mapping[str, Mapping[str, str]],
    *,
    query: str,
    terms: tuple[str, ...],
    limit: int,
    deadline: float,
) -> tuple[AppCorpusHit, ...] | None:
    """Query only physical, current connection scopes and map results back by digest."""

    from continuity_kernel.app_corpus_qmd import QMDScopedBinding

    combined: list[AppCorpusHit] = []
    seen: set[tuple[str, str]] = set()
    for connection_id, binding_state in sorted(bindings.items()):
        scoped = {
            key: record
            for key, record in documents.items()
            if record.get("connection_id") == connection_id
        }
        if not scoped:
            continue
        record_names = {
            Path(path).name: key
            for key, record in scoped.items()
            if (path := _stored_path(record)) is not None
        }
        documents_root = Path(binding_state["documents_root"])
        binding = QMDScopedBinding(
            snapshot_fingerprint=binding_state["snapshot_fingerprint"],
            scope_fingerprint=binding_state["scope_fingerprint"],
            connection_id=connection_id,
            index=binding_state["index"],
            collection=binding_state["collection"],
            root=documents_root.parent,
            documents_root=documents_root,
            manifest_path=documents_root.parent / "manifest.json",
            record_names=record_names,
        )
        if (
            binding.collection_binding_problem(
                companion.executable,
                cwd=companion.index_root,
                environment=companion._environment(),
                deadline=deadline,
                run_command=recall_module._run_command,
            )
            is not None
        ):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        result = recall_module._run_command(
            binding.command(
                companion.executable,
                "query",
                f"lex: {query}\nvec: {query}",
                "-n",
                str(min(200, max(limit, limit * 20))),
                "--json",
                "-c",
                binding.collection,
                "--no-rerank",
            ),
            cwd=companion.index_root,
            env=companion._environment(),
            timeout_seconds=remaining,
            output_limit=recall_module.MAX_QMD_OUTPUT_BYTES,
        )
        if recall_module._command_problem(result, operation="scoped app corpus search") is not None:
            return None
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        rows = (
            payload
            if isinstance(payload, list)
            else payload.get("results")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            reference = next(
                (str(row[key]) for key in ("file", "path", "url", "uri") if row.get(key)), ""
            )
            key = binding.record_key_for_reference(reference)
            record = scoped.get(key) if key is not None else None
            path = _stored_path(record) if isinstance(record, Mapping) else None
            if (
                record is None
                or path is None
                or record.get("connection_id") != connection_id
                or record_names.get(Path(path).name) != key
            ):
                continue
            content = store.read_regular_file(
                path,
                label="app corpus document",
                max_bytes=MAX_DOCUMENT_BYTES,
                missing_ok=True,
            )
            if content is None or hashlib.sha256(content).hexdigest() != record.get("digest"):
                continue
            identity = (connection_id, _string_record(record, "object_id"))
            if identity in seen:
                continue
            seen.add(identity)
            raw_score = row.get("score", 0.0)
            score = (
                float(raw_score)
                if isinstance(raw_score, (int, float)) and math.isfinite(float(raw_score))
                else 0.0
            )
            combined.append(
                AppCorpusHit(
                    connection_id=connection_id,
                    fetched_at=_string_record(record, "fetched_at"),
                    object_id=_string_record(record, "object_id"),
                    provider=_string_record(record, "provider"),
                    revision=_string_record(record, "revision"),
                    score=score,
                    snippet=_snippet(_strip_document_header(content.decode("utf-8")), terms),
                    source_ref=_string_record(record, "source_ref"),
                    title=_string_record(record, "title"),
                    source_event_at=_stored_source_event_at(record),
                )
            )
    combined.sort(key=lambda hit: (hit.score, hit.fetched_at, hit.object_id), reverse=True)
    return tuple(combined[:limit])


def _qmd_hits(
    store: PinnedPathRoot,
    companion: AppCorpusCompanion,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    query: str,
    terms: tuple[str, ...],
    limit: int,
    deadline: float,
) -> tuple[AppCorpusHit, ...] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    result = recall_module._run_command(
        companion._command(
            "query",
            f"lex: {query}\nvec: {query}",
            "-n",
            str(min(200, max(limit, limit * 20))),
            "--json",
            "-c",
            companion.collection,
            "--no-rerank",
        ),
        cwd=companion.index_root,
        env=companion._environment(),
        timeout_seconds=remaining,
        output_limit=recall_module.MAX_QMD_OUTPUT_BYTES,
    )
    if recall_module._command_problem(result, operation="app corpus search") is not None:
        return None
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    rows = (
        payload
        if isinstance(payload, list)
        else payload.get("results")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(rows, list):
        return None
    paths = {
        path.removeprefix("documents/"): record
        for record in documents.values()
        if (path := _stored_path(record)) is not None
    }
    hits: list[AppCorpusHit] = []
    seen: set[str] = set()
    for row in rows:
        if len(hits) >= limit:
            break
        if not isinstance(row, dict):
            continue
        reference = next(
            (str(row[key]) for key in ("file", "path", "url", "uri") if row.get(key)), ""
        )
        relative = recall_module._qmd_relative(reference, collection=companion.collection)
        if not relative:
            candidate = Path(reference).name
            relative = (
                candidate if re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{16}\.md", candidate) else ""
            )
        record = paths.get(relative)
        if record is None or relative in seen:
            continue
        seen.add(relative)
        path = _stored_path(record)
        assert path is not None
        content = store.read_regular_file(
            path, label="app corpus document", max_bytes=MAX_DOCUMENT_BYTES
        )
        if content is None:
            raise ValidationError("app corpus document disappeared before it was searched")
        raw_score = row.get("score", 0.0)
        score = (
            float(raw_score)
            if isinstance(raw_score, (int, float)) and math.isfinite(float(raw_score))
            else 0.0
        )
        hits.append(
            AppCorpusHit(
                connection_id=_string_record(record, "connection_id"),
                fetched_at=_string_record(record, "fetched_at"),
                object_id=_string_record(record, "object_id"),
                provider=_string_record(record, "provider"),
                revision=_string_record(record, "revision"),
                score=score,
                snippet=_snippet(_strip_document_header(content.decode("utf-8")), terms),
                source_ref=_string_record(record, "source_ref"),
                title=_string_record(record, "title"),
                source_event_at=_stored_source_event_at(record),
            )
        )
    return tuple(hits)


def _snippet(value: str, terms: tuple[str, ...]) -> str:
    folded = value.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    start = max(0, min(positions, default=0) - 120)
    clean = " ".join(value[start : start + 520].split())
    return clean if len(clean) <= 420 else clean[:419].rstrip() + "…"


def _strip_document_header(value: str) -> str:
    return value.split("\n\n", 1)[1] if value.startswith("# ") and "\n\n" in value else value


def _string_record(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValidationError("app corpus document state is invalid")
    return item


def _metadata_record(value: Mapping[str, Any]) -> dict[str, Any]:
    return _json_mapping(value.get("metadata", {}), "app corpus document metadata")


def _unlink_if_present(store: PinnedPathRoot, relative: str) -> None:
    try:
        expected = store.read_regular_file(
            relative, label="app corpus document", max_bytes=MAX_DOCUMENT_BYTES, missing_ok=True
        )
        if expected is not None:
            store.unlink_regular_file_if_exact(
                relative,
                expected=expected,
                label="app corpus document",
                max_bytes=MAX_DOCUMENT_BYTES,
            )
    except FileNotFoundError:
        return


def _cleanup_orphan_documents(
    store: PinnedPathRoot,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    deadline: float,
) -> None:
    """Remove only stale content-addressed leaves before QMD sees the documents directory."""

    current = {
        Path(path).name
        for record in documents.values()
        if (path := _stored_path(record)) is not None
    }
    with store.bind_directory("documents"):
        for name in store.list_directory_entry_names(
            "documents", max_entries=MAX_DOCUMENTS * 2, suffix=".md"
        ):
            _check_deadline(deadline)
            if name in current or re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{16}\.md", name) is None:
                continue
            _unlink_if_present(store, f"documents/{name}")


def _timeout(value: int, *, maximum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValidationError(f"{label} timeout must be between 1 and {maximum} seconds")
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValidationError("app corpus operation timed out")
    return remaining


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ValidationError("app corpus operation timed out")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def _open_store(root: Path) -> Iterator[PinnedPathRoot]:
    store = PinnedPathRoot(root)
    try:
        yield store
    finally:
        store.close()
