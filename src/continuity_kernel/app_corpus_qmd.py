"""Physically scoped QMD indexes for connected-app retrieval.

A QMD collection filter is not a sufficient authorization boundary: QMD gathers
candidates before it applies that filter.  This module materializes a dedicated
regular-file view and index for each connection before QMD sees a query.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from continuity_kernel import recall as recall_module
from continuity_kernel.atomic import PinnedPathRoot
from continuity_kernel.errors import ValidationError

MAX_RECORDS: Final = 250_000
MAX_DOCUMENT_BYTES: Final = 4 * 1024 * 1024
MAX_MANIFEST_BYTES: Final = 128 * 1024 * 1024
_MANIFEST_VERSION: Final = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONNECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DOCUMENT_PATH = re.compile(r"^documents/([0-9a-f]{64})(?:-([0-9a-f]{16}))?\.md$")
_INDEX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ScopedQMDRefreshError(ValidationError):
    """A scope did not become queryable; callers must keep QMD out of that search."""


@dataclass(frozen=True)
class QMDScopedRecord:
    key: str
    connection_id: str
    document_path: str
    digest: str


@dataclass(frozen=True)
class QMDScopedSnapshot:
    fingerprint: str
    records: tuple[QMDScopedRecord, ...]


@dataclass(frozen=True)
class QMDScopedBinding:
    """One ready physical QMD scope and its exact permitted record names."""

    snapshot_fingerprint: str
    scope_fingerprint: str
    connection_id: str
    index: str
    collection: str
    root: Path
    documents_root: Path
    manifest_path: Path
    record_names: Mapping[str, str]

    def command(self, executable: str, *arguments: str) -> tuple[str, ...]:
        """Build a QMD command which can address only this scope's index."""

        return (executable, "--index", self.index, *arguments)

    def record_key_for_reference(self, reference: str) -> str | None:
        """Map a QMD result only when its collection and manifest filename match."""

        relative = recall_module._qmd_relative(reference, collection=self.collection)
        if not relative:
            candidate = Path(reference)
            if not candidate.is_absolute():
                return None
            try:
                relative_path = candidate.resolve(strict=False).relative_to(self.documents_root)
                relative = relative_path.as_posix()
            except ValueError:
                return None
        if "/" in relative:
            return None
        return self.record_names.get(relative)

    def collection_binding_problem(
        self,
        executable: str,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        deadline: float,
        run_command: _CommandRunner,
    ) -> str | None:
        """Return a safe problem when QMD no longer addresses this physical scope."""

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "QMD scoped app corpus binding check timed out"
        result = run_command(
            self.command(executable, "collection", "show", self.collection),
            cwd=cwd,
            env=dict(environment),
            timeout_seconds=remaining,
            output_limit=128 * 1024,
        )
        if result.problem == "timeout":
            return "QMD scoped app corpus binding check timed out"
        if result.problem == "output_limit":
            return "QMD scoped app corpus binding check exceeded its output bound"
        if result.problem is not None:
            return "QMD scoped app corpus binding check is unavailable"
        if result.returncode:
            return f"QMD scoped app corpus binding check failed with exit code {result.returncode}"
        target = recall_module._qmd_collection_target(result.stdout, collection=self.collection)
        if target != self.documents_root:
            return "QMD scoped app corpus collection binding is unverified"
        return None


_CommandRunner = Callable[..., recall_module._CommandResult]


class ScopedQMDIndexManager:
    """Build per-connection QMD views beneath one pinned app-corpus store."""

    def __init__(
        self,
        store: PinnedPathRoot,
        index_root: Path | str,
        executable: Path | str,
        environment: Mapping[str, str],
        index: str,
        *,
        run_command: _CommandRunner = recall_module._run_command,
    ) -> None:
        if not isinstance(store, PinnedPathRoot):
            raise ValidationError("scoped QMD requires pinned app corpus storage")
        self.store = store
        self.index_root = Path(index_root).expanduser().resolve()
        if self.index_root != store.root:
            raise ValidationError("scoped QMD index root must match pinned app corpus storage")
        if not isinstance(executable, (str, Path)) or not str(executable):
            raise ValidationError("scoped QMD executable is invalid")
        self.executable = str(executable)
        if not isinstance(index, str) or _INDEX_NAME.fullmatch(index) is None:
            raise ValidationError("scoped QMD index name is invalid")
        self.index = index
        if not callable(run_command):
            raise ValidationError("scoped QMD command runner is invalid")
        self.run_command = run_command
        self.environment = _environment(environment)

    def refresh(
        self, snapshot: QMDScopedSnapshot, deadline: float
    ) -> tuple[QMDScopedBinding, ...]:
        """Return bindings only after every affected scope is fully indexed.

        An existing view whose scope fingerprint is unchanged is immediately
        reusable.  Its returned binding is nevertheless stamped with the new
        complete corpus fingerprint, so callers can compare it with current
        state before mapping a hit back to provider material.
        """

        _check_deadline(deadline)
        fingerprint, groups = _snapshot_groups(snapshot)
        bindings: list[QMDScopedBinding] = []
        for connection_id, records in groups:
            _check_deadline(deadline)
            bindings.append(
                self._refresh_scope(
                    snapshot_fingerprint=fingerprint,
                    connection_id=connection_id,
                    records=records,
                    deadline=deadline,
                )
            )
        return tuple(bindings)

    def _refresh_scope(
        self,
        *,
        snapshot_fingerprint: str,
        connection_id: str,
        records: tuple[QMDScopedRecord, ...],
        deadline: float,
    ) -> QMDScopedBinding:
        token = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:32]
        root_relative = f"qmd-scopes/{token}"
        documents_relative = f"{root_relative}/documents"
        manifest_relative = f"{root_relative}/manifest.json"
        scope_index = _scope_index(self.index, token)
        collection = f"seld-scope-{token}"
        scope_fingerprint = _scope_fingerprint(records)
        record_names = {Path(record.document_path).name: record.key for record in records}

        self.store.ensure_directory("cache")
        self.store.ensure_directory("config")
        self.store.ensure_directory("qmd-scopes")
        self.store.ensure_directory(root_relative)
        self.store.ensure_directory(documents_relative)
        manifest = _load_manifest(self.store, manifest_relative)
        expected = _manifest_binding(
            manifest,
            connection_id=connection_id,
            scope_fingerprint=scope_fingerprint,
            index=scope_index,
            collection=collection,
            documents_root=self.index_root / documents_relative,
            record_names=record_names,
        )

        # Validate every source and view before deeming an unchanged scope ready.
        sources = self._read_sources(records, deadline=deadline)
        view_current = self._view_matches(documents_relative, sources, deadline=deadline)
        if expected and view_current:
            return _binding(
                snapshot_fingerprint=snapshot_fingerprint,
                scope_fingerprint=scope_fingerprint,
                connection_id=connection_id,
                index=scope_index,
                collection=collection,
                root=self.index_root / root_relative,
                documents_root=self.index_root / documents_relative,
                manifest_path=self.index_root / manifest_relative,
                record_names=record_names,
            )

        self._materialize_view(documents_relative, sources, deadline=deadline)
        already_registered = _config_has_binding(
            self.store,
            index=scope_index,
            collection=collection,
            documents_root=self.index_root / documents_relative,
        )
        commands = (
            (self._command(scope_index, "update"), self._command(scope_index, "embed"))
            if already_registered
            else (
                self._command(
                    scope_index,
                    "collection",
                    "add",
                    str(self.index_root / documents_relative),
                    "--name",
                    collection,
                    "--mask",
                    "**/*.md",
                ),
                self._command(scope_index, "update"),
                self._command(scope_index, "embed"),
            )
        )
        for command in commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScopedQMDRefreshError("scoped app QMD refresh timed out")
            result = self.run_command(
                command,
                cwd=self.index_root,
                env=dict(self.environment),
                timeout_seconds=remaining,
                output_limit=recall_module.MAX_QMD_OUTPUT_BYTES,
            )
            if recall_module._command_problem(result, operation="scoped app corpus refresh"):
                raise ScopedQMDRefreshError("scoped app QMD refresh failed")

        manifest_value = {
            "version": _MANIFEST_VERSION,
            "connection_id": connection_id,
            "scope_fingerprint": scope_fingerprint,
            "index": scope_index,
            "collection": collection,
            "documents_root": str(self.index_root / documents_relative),
            "record_names": dict(sorted(record_names.items())),
        }
        encoded = (
            json.dumps(manifest_value, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValidationError("scoped QMD manifest exceeds its size bound")
        self.store.atomic_write(manifest_relative, encoded)
        return _binding(
            snapshot_fingerprint=snapshot_fingerprint,
            scope_fingerprint=scope_fingerprint,
            connection_id=connection_id,
            index=scope_index,
            collection=collection,
            root=self.index_root / root_relative,
            documents_root=self.index_root / documents_relative,
            manifest_path=self.index_root / manifest_relative,
            record_names=record_names,
        )

    def _read_sources(
        self, records: tuple[QMDScopedRecord, ...], *, deadline: float
    ) -> dict[str, bytes]:
        sources: dict[str, bytes] = {}
        for record in records:
            _check_deadline(deadline)
            content = self.store.read_regular_file(
                record.document_path,
                label="app corpus document",
                max_bytes=MAX_DOCUMENT_BYTES,
            )
            assert content is not None
            if hashlib.sha256(content).hexdigest() != record.digest:
                raise ValidationError(
                    "app corpus document digest changed before scoped QMD refresh"
                )
            sources[Path(record.document_path).name] = content
        return sources

    def _view_matches(
        self, documents_relative: str, sources: Mapping[str, bytes], *, deadline: float
    ) -> bool:
        for name, expected in sources.items():
            _check_deadline(deadline)
            actual = self.store.read_regular_file(
                f"{documents_relative}/{name}",
                label="scoped QMD document",
                max_bytes=MAX_DOCUMENT_BYTES,
                missing_ok=True,
            )
            if actual != expected:
                return False
        names = self.store.list_directory_entry_names(
            documents_relative, max_entries=MAX_RECORDS + 1, suffix=".md"
        )
        return set(names) == set(sources)

    def _materialize_view(
        self, documents_relative: str, sources: Mapping[str, bytes], *, deadline: float
    ) -> None:
        for name, content in sources.items():
            _check_deadline(deadline)
            relative = f"{documents_relative}/{name}"
            current = self.store.read_regular_file(
                relative,
                label="scoped QMD document",
                max_bytes=MAX_DOCUMENT_BYTES,
                missing_ok=True,
            )
            if current != content:
                self.store.atomic_write(relative, content)
        # Exact leaf removal needs the ancestor pinned for the complete check-and-unlink.
        with self.store.bind_directory(documents_relative):
            for name in self.store.list_directory_entry_names(
                documents_relative, max_entries=MAX_RECORDS + 1, suffix=".md"
            ):
                _check_deadline(deadline)
                if name in sources:
                    continue
                current = self.store.read_regular_file(
                    f"{documents_relative}/{name}",
                    label="scoped QMD document",
                    max_bytes=MAX_DOCUMENT_BYTES,
                    missing_ok=True,
                )
                if current is not None:
                    self.store.unlink_regular_file_if_exact(
                        f"{documents_relative}/{name}",
                        expected=current,
                        label="scoped QMD document",
                        max_bytes=MAX_DOCUMENT_BYTES,
                    )

    def _command(self, index: str, *arguments: str) -> tuple[str, ...]:
        return (self.executable, "--index", index, *arguments)


def _snapshot_groups(
    snapshot: QMDScopedSnapshot,
) -> tuple[str, tuple[tuple[str, tuple[QMDScopedRecord, ...]], ...]]:
    if (
        not isinstance(snapshot, QMDScopedSnapshot)
        or _DIGEST.fullmatch(snapshot.fingerprint) is None
    ):
        raise ValidationError("scoped QMD snapshot fingerprint is invalid")
    if len(snapshot.records) > MAX_RECORDS:
        raise ValidationError("scoped QMD snapshot exceeds its record limit")
    grouped: dict[str, list[QMDScopedRecord]] = {}
    keys: set[str] = set()
    paths: set[str] = set()
    for record in snapshot.records:
        if not isinstance(record, QMDScopedRecord):
            raise ValidationError("scoped QMD snapshot record is invalid")
        _validate_record(record)
        if record.key in keys or record.document_path in paths:
            raise ValidationError("scoped QMD snapshot contains duplicate records")
        keys.add(record.key)
        paths.add(record.document_path)
        grouped.setdefault(record.connection_id, []).append(record)
    return (
        snapshot.fingerprint,
        tuple(
            (connection_id, tuple(sorted(records, key=lambda record: record.key)))
            for connection_id, records in sorted(grouped.items())
        ),
    )


def _validate_record(record: QMDScopedRecord) -> None:
    if _DIGEST.fullmatch(record.key) is None or _DIGEST.fullmatch(record.digest) is None:
        raise ValidationError("scoped QMD record digest is invalid")
    if (
        not isinstance(record.connection_id, str)
        or _CONNECTION_ID.fullmatch(record.connection_id) is None
    ):
        raise ValidationError("scoped QMD connection ID is invalid")
    if not isinstance(record.document_path, str):
        raise ValidationError("scoped QMD document path is invalid")
    match = _DOCUMENT_PATH.fullmatch(record.document_path)
    if (
        match is None
        or match.group(1) != record.key
        or (match.group(2) is not None and match.group(2) != record.digest[:16])
    ):
        raise ValidationError("scoped QMD document path does not bind its record digest")


def _scope_fingerprint(records: tuple[QMDScopedRecord, ...]) -> str:
    rows = [(record.key, record.digest, record.document_path) for record in records]
    encoded = json.dumps(sorted(rows), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scope_index(index: str, token: str) -> str:
    # Keep the scope token opaque and leave space for the suffix within QMD's 64-byte index limit.
    return f"{index[:24]}-scope-{token}"


def _environment(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValidationError("scoped QMD environment is invalid")
    clean: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise ValidationError("scoped QMD environment is invalid")
        if not isinstance(item, str) or "\x00" in item:
            raise ValidationError("scoped QMD environment is invalid")
        clean[key] = item
    return MappingProxyType(clean)


def _load_manifest(store: PinnedPathRoot, relative: str) -> Mapping[str, object] | None:
    encoded = store.read_regular_file(
        relative,
        label="scoped QMD manifest",
        max_bytes=MAX_MANIFEST_BYTES,
        missing_ok=True,
    )
    if encoded is None:
        return None
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _manifest_binding(
    manifest: Mapping[str, object] | None,
    *,
    connection_id: str,
    scope_fingerprint: str,
    index: str,
    collection: str,
    documents_root: Path,
    record_names: Mapping[str, str],
) -> bool:
    if manifest is None:
        return False
    names = manifest.get("record_names")
    return (
        manifest.get("version") == _MANIFEST_VERSION
        and manifest.get("connection_id") == connection_id
        and manifest.get("scope_fingerprint") == scope_fingerprint
        and manifest.get("index") == index
        and manifest.get("collection") == collection
        and manifest.get("documents_root") == str(documents_root)
        and isinstance(names, dict)
        and names == dict(record_names)
    )


def _config_has_binding(
    store: PinnedPathRoot,
    *,
    index: str,
    collection: str,
    documents_root: Path,
) -> bool:
    """Check QMD's generated collection entry without calling into its index."""

    encoded = store.read_regular_file(
        f"config/{index}.yml",
        label="scoped QMD configuration",
        max_bytes=1_024 * 1024,
        missing_ok=True,
    )
    if encoded is None:
        return False
    try:
        lines = encoded.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
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
        matched = re.fullmatch(r"    (path|pattern):\s*(.*)", line)
        if matched is not None:
            values[matched.group(1)] = _yaml_scalar(matched.group(2))
    return (
        found
        and values.get("path") == str(documents_root)
        and values.get("pattern") == "**/*.md"
    )


def _yaml_scalar(value: str) -> str:
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


def _binding(
    *,
    snapshot_fingerprint: str,
    scope_fingerprint: str,
    connection_id: str,
    index: str,
    collection: str,
    root: Path,
    documents_root: Path,
    manifest_path: Path,
    record_names: Mapping[str, str],
) -> QMDScopedBinding:
    return QMDScopedBinding(
        snapshot_fingerprint=snapshot_fingerprint,
        scope_fingerprint=scope_fingerprint,
        connection_id=connection_id,
        index=index,
        collection=collection,
        root=root,
        documents_root=documents_root,
        manifest_path=manifest_path,
        record_names=MappingProxyType(dict(sorted(record_names.items()))),
    )


def _check_deadline(deadline: float) -> None:
    if not isinstance(deadline, (float, int)) or isinstance(deadline, bool):
        raise ValidationError("scoped QMD deadline is invalid")
    if time.monotonic() >= deadline:
        raise ScopedQMDRefreshError("scoped app QMD refresh timed out")
