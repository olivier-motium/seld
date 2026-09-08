"""Fast local lexical retrieval while a scoped QMD vector index is pending.

The index is built once from one selected physical scope.  Later queries use
the index and read only the selected documents before returning a result.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from continuity_kernel.atomic import PinnedPathRoot
from continuity_kernel.errors import ValidationError

_INDEX_VERSION: Final = "3"
_MAX_DATABASE_BYTES: Final = 4 * 1024 * 1024 * 1024
_MAX_METADATA_CHARS: Final = 32 * 1024
_BATCH_SIZE: Final = 500


@dataclass(frozen=True)
class LexicalMatch:
    key: str
    metadata_text: str
    score: float


def search(
    store: PinnedPathRoot,
    index_root: Path,
    selected_documents: Mapping[str, Mapping[str, Any]],
    *,
    terms: tuple[str, ...],
    limit: int,
    deadline: float,
) -> tuple[LexicalMatch, ...] | None:
    """Return physical-scope lexical candidates, or ``None`` if FTS is unavailable.

    Candidate ranking happens only within one connection.  Results are returned
    round-robin across connections so scores from separate source indexes are
    never treated as one global ranking.
    """

    if not terms or limit < 1:
        raise ValidationError("app corpus lexical query is invalid")
    try:
        with store.exclusive_file_lock(
            "locks/app-corpus-lexical.lock", timeout=_remaining(deadline)
        ):
            scope_ids = sorted(
                {str(row.get("connection_id")) for row in selected_documents.values()}
            )
            scope_key = hashlib.sha256("\0".join(scope_ids).encode()).hexdigest()
            database = _database_path(store, index_root, scope_key)
            connection = sqlite3.connect(database, timeout=_remaining(deadline))
            try:
                _configure(connection)
                fingerprint = _fingerprint(selected_documents, deadline=deadline)
                if _stored_fingerprint(connection) != fingerprint:
                    _rebuild(
                        store,
                        connection,
                        selected_documents,
                        fingerprint=fingerprint,
                        deadline=deadline,
                    )
                matches = _matches(
                    connection,
                    selected_documents,
                    terms=terms,
                    limit=limit,
                    deadline=deadline,
                )
                _check_database_size(database)
                return matches
            finally:
                connection.close()
    except (sqlite3.Error, OSError, ValidationError):
        return None


def _database_path(store: PinnedPathRoot, index_root: Path, scope_key: str) -> Path:
    store.ensure_directory("lexical")
    path = index_root / "lexical" / f"{scope_key}.sqlite"
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return path
    if not os.path.isfile(path) or os.path.islink(path):
        raise ValidationError("app corpus lexical index must be a regular file")
    if metadata.st_size > _MAX_DATABASE_BYTES:
        raise ValidationError("app corpus lexical index exceeds its size bound")
    return path


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE IF NOT EXISTS lexical_meta (key TEXT PRIMARY KEY, value TEXT)")
    schema = connection.execute(
        "SELECT value FROM lexical_meta WHERE key = 'schema_version'"
    ).fetchone()
    if schema is None or schema[0] != _INDEX_VERSION:
        connection.execute("DROP TABLE IF EXISTS lexical_documents")
        connection.execute("DROP TABLE IF EXISTS lexical_catalog")
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS lexical_documents USING fts5("
        "document_key UNINDEXED, connection_id UNINDEXED, provider UNINDEXED, "
        "fetched_at UNINDEXED, object_id UNINDEXED, revision UNINDEXED, "
        "source_ref UNINDEXED, source_event_at UNINDEXED, digest UNINDEXED, "
        "title, metadata, body, tokenize='unicode61 remove_diacritics 2')"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS lexical_catalog (document_key TEXT PRIMARY KEY, "
        "row_id INTEGER NOT NULL, fingerprint TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT OR REPLACE INTO lexical_meta(key, value) VALUES ('schema_version', ?)",
        (_INDEX_VERSION,),
    )
    connection.commit()


def _stored_fingerprint(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT value FROM lexical_meta WHERE key = 'fingerprint'").fetchone()
    return row[0] if row and isinstance(row[0], str) else None


def _fingerprint(documents: Mapping[str, Mapping[str, Any]], *, deadline: float) -> str:
    digest = hashlib.sha256(_INDEX_VERSION.encode("ascii"))
    for number, (key, record) in enumerate(sorted(documents.items()), start=1):
        if number % _BATCH_SIZE == 0:
            _check_deadline(deadline)
        row = (
            key,
            record.get("connection_id"),
            record.get("provider"),
            # A read timestamp does not change searchable content. Hits use the
            # current corpus record, so a rescan must not reindex unchanged text.
            record.get("object_id"),
            record.get("revision"),
            record.get("source_ref"),
            record.get("source_event_at"),
            record.get("digest"),
            record.get("title"),
            record.get("metadata"),
        )
        digest.update(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _rebuild(
    store: PinnedPathRoot,
    connection: sqlite3.Connection,
    documents: Mapping[str, Mapping[str, Any]],
    *,
    fingerprint: str,
    deadline: float,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = {
            key: (row_id, digest)
            for key, row_id, digest in connection.execute(
                "SELECT document_key, row_id, fingerprint FROM lexical_catalog"
            )
        }
        for number, (key, record) in enumerate(sorted(documents.items()), start=1):
            if number % _BATCH_SIZE == 0:
                _check_deadline(deadline)
            record_fingerprint = _fingerprint({key: record}, deadline=deadline)
            previous = existing.pop(key, None)
            if previous is not None and previous[1] == record_fingerprint:
                continue
            if previous is not None:
                connection.execute("DELETE FROM lexical_documents WHERE rowid = ?", (previous[0],))
            cursor = connection.execute(
                "INSERT INTO lexical_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                _row(store, key, record),
            )
            connection.execute(
                "INSERT OR REPLACE INTO lexical_catalog VALUES (?,?,?)",
                (key, cursor.lastrowid, record_fingerprint),
            )
        for key, (row_id, _) in existing.items():
            connection.execute("DELETE FROM lexical_documents WHERE rowid = ?", (row_id,))
            connection.execute("DELETE FROM lexical_catalog WHERE document_key = ?", (key,))
        connection.execute(
            "INSERT OR REPLACE INTO lexical_meta(key, value) VALUES ('fingerprint', ?)",
            (fingerprint,),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _row(
    store: PinnedPathRoot, key: str, record: Mapping[str, Any]
) -> tuple[str, str, str, str, str, str, str, str, str, str, str, str]:
    connection_id = _required_text(record, "connection_id")
    provider = _required_text(record, "provider")
    fetched_at = _required_text(record, "fetched_at")
    object_id = _required_text(record, "object_id")
    revision = _required_text(record, "revision")
    source_ref = _required_text(record, "source_ref")
    digest = _required_text(record, "digest")
    title = _required_text(record, "title")
    source_event_at = record.get("source_event_at")
    if source_event_at is not None and not isinstance(source_event_at, str):
        raise ValidationError("app corpus lexical state is invalid")
    metadata = record.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValidationError("app corpus lexical state is invalid")
    metadata_text = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(metadata_text) > _MAX_METADATA_CHARS:
        metadata_text = metadata_text[:_MAX_METADATA_CHARS]
    path = record.get("path")
    if not isinstance(path, str):
        raise ValidationError("app corpus lexical state is invalid")
    content = store.read_regular_file(
        path, label="app corpus lexical document", max_bytes=4 * 1024 * 1024
    )
    if content is None or hashlib.sha256(content).hexdigest() != digest:
        raise ValidationError("app corpus lexical document changed before indexing")
    try:
        body = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("app corpus lexical document is not text") from exc
    return (
        key,
        connection_id,
        provider,
        fetched_at,
        object_id,
        revision,
        source_ref,
        source_event_at or "",
        digest,
        title,
        metadata_text,
        body,
    )


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValidationError("app corpus lexical state is invalid")
    return value


def _matches(
    connection: sqlite3.Connection,
    selected_documents: Mapping[str, Mapping[str, Any]],
    *,
    terms: tuple[str, ...],
    limit: int,
    deadline: float,
) -> tuple[LexicalMatch, ...]:
    expression = " AND ".join(f'"{term}"' for term in terms)
    per_connection: dict[str, list[LexicalMatch]] = {}
    connection_ids = sorted(
        {
            connection_id
            for record in selected_documents.values()
            if isinstance(connection_id := record.get("connection_id"), str)
        }
    )
    for connection_id in connection_ids:
        _check_deadline(deadline)
        rows = connection.execute(
            "SELECT document_key, metadata, bm25(lexical_documents) "
            "FROM lexical_documents WHERE lexical_documents MATCH ? AND connection_id = ? "
            "ORDER BY bm25(lexical_documents) LIMIT ?",
            (expression, connection_id, max(limit, min(100, limit * 20))),
        ).fetchall()
        matches: list[LexicalMatch] = []
        for key, metadata_text, rank in rows:
            if (
                not isinstance(key, str)
                or key not in selected_documents
                or not isinstance(metadata_text, str)
            ):
                continue
            score = -float(rank) if isinstance(rank, (int, float)) else 0.0
            matches.append(
                LexicalMatch(key=key, metadata_text=metadata_text, score=max(0.0, score))
            )
        if matches:
            per_connection[connection_id] = matches
    results: list[LexicalMatch] = []
    while len(results) < limit and per_connection:
        for connection_id in tuple(sorted(per_connection)):
            candidates = per_connection[connection_id]
            results.append(candidates.pop(0))
            if not candidates:
                del per_connection[connection_id]
            if len(results) == limit:
                break
    return tuple(results)


def _check_database_size(path: Path) -> None:
    metadata = os.lstat(path)
    if not os.path.isfile(path) or os.path.islink(path) or metadata.st_size > _MAX_DATABASE_BYTES:
        raise ValidationError("app corpus lexical index is invalid")
    if os.name != "nt":
        os.chmod(path, 0o600)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValidationError("app corpus lexical search timed out")
    return remaining


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ValidationError("app corpus lexical search timed out")
