"""Disposable QMD recall with an exact Markdown fallback.

Canonical Seld records remain ordinary Markdown in the vault.  This module
builds a host-local snapshot for QMD so the indexer never has to traverse the
canonical tree itself, and it keeps every cache, fingerprint, and index byte
outside the vault.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final
from urllib.parse import unquote, urlsplit

from continuity_kernel.atomic import exclusive_lock
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ValidationError
from continuity_kernel.resident_context import validate_imported_resident_content

MAX_DOCUMENTS: Final = 20_000
MAX_DISCOVERY_ENTRIES: Final = 100_000
MAX_DOCUMENT_BYTES: Final = 4 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 128 * 1024 * 1024
MAX_QMD_OUTPUT_BYTES: Final = 1024 * 1024
MAX_QUERY_CHARACTERS: Final = 1_000
MAX_RESULTS: Final = 20
MAX_REFRESH_TIMEOUT_SECONDS: Final = 600
MAX_SEARCH_TIMEOUT_SECONDS: Final = 60
_INDEX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_QUERY_TERM = re.compile(r"[^\W_]+", re.UNICODE)
_DEFAULT_QMD_EXECUTABLES: Final = (
    Path("/opt/homebrew/bin/qmd"),
    Path("/usr/local/bin/qmd"),
)
_ROOT_MARKDOWN: Final = (
    "MIND.md",
    "NOW.md",
    "DIRECTION.md",
    "PORTFOLIO.md",
)
_MARKDOWN_TREES: Final = (
    "tasks",
    "entities",
    "threads",
    "context/resident",
    "journal",
)
_RESIDENT_CONTEXT: Final = PurePosixPath("context/resident")
_LEGACY_RESIDENT_CONTROL: Final = PurePosixPath("context/resident/control")


@dataclass(frozen=True)
class RecallDocument:
    relative_path: str
    digest: str
    modified_at: str
    size: int


@dataclass(frozen=True)
class SkippedDocument:
    relative_path: str
    reason: str


@dataclass(frozen=True)
class RecallDiscovery:
    documents: tuple[RecallDocument, ...]
    fingerprint: str
    skipped: tuple[SkippedDocument, ...]

    @property
    def complete(self) -> bool:
        return not self.skipped


@dataclass(frozen=True)
class RecallPlan:
    collection: str
    commands: tuple[tuple[str, ...], ...]
    discovery: RecallDiscovery
    environment: tuple[tuple[str, str], ...]
    executable: str
    index: str
    index_root: str
    snapshot_root: str


@dataclass(frozen=True)
class RecallStatus:
    available: bool
    current: bool
    discovery: RecallDiscovery
    index_root: str
    ready: bool
    reason: str | None


@dataclass(frozen=True)
class RecallRefresh:
    changed: bool
    discovery: RecallDiscovery
    reason: str | None
    updated: bool


@dataclass(frozen=True)
class RecallHit:
    modified_at: str
    relative_path: str
    score: float
    snippet: str
    title: str


@dataclass(frozen=True)
class RecallSearch:
    backend: str
    complete: bool
    hits: tuple[RecallHit, ...]
    reason: str | None
    skipped: tuple[SkippedDocument, ...]


@dataclass(frozen=True)
class _DocumentSnapshot:
    content: bytes
    document: RecallDocument


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    problem: str | None = None


class _RecallDeadlineExceeded(Exception):
    pass


class RecallCompanion:
    """Host-local QMD lifecycle for one exact vault path."""

    def __init__(
        self,
        vault_root: Path | str,
        *,
        executable: Path | str = "qmd",
        index: str = "seld",
        index_root: Path | str | None = None,
    ):
        self.vault_root = _absolute_path(vault_root, "vault root")
        self.executable = _executable(executable)
        if _INDEX_NAME.fullmatch(index) is None:
            raise ValidationError(
                "QMD index name must use letters, numbers, dots, dashes, or underscores"
            )
        self.index = index
        if index_root is None:
            identity = hashlib.sha256(str(self.vault_root).encode("utf-8")).hexdigest()[:24]
            self.index_root = _absolute_path(data_dir() / "recall" / identity, "QMD index root")
        else:
            self.index_root = _absolute_path(index_root, "QMD index root")
        _require_disjoint_roots(self.vault_root, self.index_root)
        collection_identity = hashlib.sha256(
            f"{self.vault_root}\0{self.index_root}\0{self.index}".encode()
        ).hexdigest()[:24]
        self.collection = f"seld-vault-{collection_identity}"

    @property
    def snapshot_root(self) -> Path:
        return self.index_root / "documents"

    @property
    def state_path(self) -> Path:
        return self.index_root / f"content-fingerprint-{self.index}.json"

    @property
    def lock_path(self) -> Path:
        return self.index_root / "locks/recall.lock"

    def discover(self) -> RecallDiscovery:
        snapshots, discovery = _discover(self.vault_root)
        del snapshots
        return discovery

    def plan(self) -> RecallPlan:
        discovery = self.discover()
        remove = self._command("collection", "remove", self.collection)
        add = self._command(
            "collection",
            "add",
            str(self.snapshot_root),
            "--name",
            self.collection,
            "--mask",
            "**/*.md",
        )
        return RecallPlan(
            collection=self.collection,
            commands=(
                remove,
                add,
                self._command("update"),
                self._command("embed", "-f"),
            ),
            discovery=discovery,
            environment=tuple(sorted(self._environment().items())),
            executable=self.executable,
            index=self.index,
            index_root=str(self.index_root),
            snapshot_root=str(self.snapshot_root),
        )

    def status(self, *, timeout_seconds: int = 10) -> RecallStatus:
        timeout = _bounded_timeout(timeout_seconds, maximum=60, label="QMD status")
        deadline = time.monotonic() + timeout
        with exclusive_lock(self.lock_path, timeout=float(timeout)):
            return self._status_unlocked(deadline)

    def _status_unlocked(self, deadline: float) -> RecallStatus:
        try:
            _, discovery = _discover(self.vault_root, deadline=deadline)
        except _RecallDeadlineExceeded:
            discovery = _deadline_discovery()
            return RecallStatus(
                _resolved_executable(self.executable) is not None,
                False,
                discovery,
                str(self.index_root),
                False,
                "QMD status timed out; exact Markdown recall remains available",
            )
        current = (
            _stored_fingerprint(self.index_root, self.state_path.name) == discovery.fingerprint
        )
        if _resolved_executable(self.executable) is None:
            return RecallStatus(
                False,
                current,
                discovery,
                str(self.index_root),
                False,
                "QMD executable is unavailable; exact Markdown recall remains available",
            )
        if not current or not _index_directory_exists(self.index_root, self.snapshot_root.name):
            return RecallStatus(
                True,
                current,
                discovery,
                str(self.index_root),
                False,
                "QMD recall needs a refresh; exact Markdown recall remains available",
            )
        bound, problem = self._collection_binding(deadline, operation="status")
        if bound:
            command_timeout = _remaining_timeout(deadline)
            if command_timeout is None:
                problem = "QMD status timed out; exact Markdown recall remains available"
            else:
                result = _run_command(
                    self._command("status"),
                    cwd=self.index_root,
                    env=self._environment(),
                    timeout_seconds=command_timeout,
                    output_limit=128 * 1024,
                )
                problem = _command_problem(result, operation="status")
        return RecallStatus(
            True,
            current,
            discovery,
            str(self.index_root),
            problem is None,
            problem,
        )

    def refresh(self, *, timeout_seconds: int = 120) -> RecallRefresh:
        timeout = _bounded_timeout(
            timeout_seconds,
            maximum=MAX_REFRESH_TIMEOUT_SECONDS,
            label="QMD refresh",
        )
        deadline = time.monotonic() + timeout
        with exclusive_lock(self.lock_path, timeout=float(timeout)):
            return self._refresh_unlocked(deadline)

    def _refresh_unlocked(self, deadline: float) -> RecallRefresh:
        try:
            snapshots, discovery = _discover(self.vault_root, deadline=deadline)
        except _RecallDeadlineExceeded:
            return RecallRefresh(
                True,
                _deadline_discovery(),
                "QMD refresh timed out; exact Markdown recall remains available",
                False,
            )
        prior = _stored_fingerprint(self.index_root, self.state_path.name)
        if _resolved_executable(self.executable) is None:
            return RecallRefresh(
                prior != discovery.fingerprint,
                discovery,
                "QMD executable is unavailable; exact Markdown recall remains available",
                False,
            )
        if prior == discovery.fingerprint and _index_directory_exists(
            self.index_root, self.snapshot_root.name
        ):
            bound, _problem = self._collection_binding(deadline, operation="refresh")
            if bound:
                return RecallRefresh(False, discovery, None, False)
        self._prepare_runtime()
        try:
            _publish_snapshot(self.snapshot_root, snapshots, deadline=deadline)
        except _RecallDeadlineExceeded:
            return RecallRefresh(
                True,
                discovery,
                "QMD refresh timed out; exact Markdown recall remains available",
                False,
            )
        command_timeout = _remaining_timeout(deadline)
        if command_timeout is None:
            return RecallRefresh(
                True,
                discovery,
                "QMD refresh timed out; exact Markdown recall remains available",
                False,
            )
        removed = _run_command(
            self._command("collection", "remove", self.collection),
            cwd=self.index_root,
            env=self._environment(),
            timeout_seconds=command_timeout,
            output_limit=128 * 1024,
        )
        if removed.problem is not None:
            return RecallRefresh(
                True,
                discovery,
                _command_problem(removed, operation="refresh"),
                False,
            )
        command_timeout = _remaining_timeout(deadline)
        if command_timeout is None:
            return RecallRefresh(
                True,
                discovery,
                "QMD refresh timed out; exact Markdown recall remains available",
                False,
            )
        added = _run_command(
            self._command(
                "collection",
                "add",
                str(self.snapshot_root),
                "--name",
                self.collection,
                "--mask",
                "**/*.md",
            ),
            cwd=self.index_root,
            env=self._environment(),
            timeout_seconds=command_timeout,
            output_limit=128 * 1024,
        )
        problem = _command_problem(added, operation="refresh")
        if problem is not None:
            return RecallRefresh(True, discovery, problem, False)
        bound, problem = self._collection_binding(deadline, operation="refresh")
        if not bound:
            return RecallRefresh(True, discovery, problem, False)
        for command in (self._command("update"), self._command("embed")):
            command_timeout = _remaining_timeout(deadline)
            if command_timeout is None:
                return RecallRefresh(
                    True,
                    discovery,
                    "QMD refresh timed out; exact Markdown recall remains available",
                    False,
                )
            result = _run_command(
                command,
                cwd=self.index_root,
                env=self._environment(),
                timeout_seconds=command_timeout,
                output_limit=MAX_QMD_OUTPUT_BYTES,
            )
            problem = _command_problem(result, operation="refresh")
            if problem is not None:
                return RecallRefresh(True, discovery, problem, False)
        _write_fingerprint(self.index_root, self.state_path.name, discovery.fingerprint)
        return RecallRefresh(True, discovery, None, True)

    def rebuild(self, *, timeout_seconds: int = 600) -> RecallRefresh:
        timeout = _bounded_timeout(
            timeout_seconds,
            maximum=MAX_REFRESH_TIMEOUT_SECONDS,
            label="QMD rebuild",
        )
        deadline = time.monotonic() + timeout
        with exclusive_lock(self.lock_path, timeout=float(timeout)):
            return self._rebuild_unlocked(deadline)

    def _rebuild_unlocked(self, deadline: float) -> RecallRefresh:
        try:
            snapshots, discovery = _discover(self.vault_root, deadline=deadline)
        except _RecallDeadlineExceeded:
            return RecallRefresh(
                True,
                _deadline_discovery(),
                "QMD rebuild timed out; exact Markdown recall remains available",
                False,
            )
        if _resolved_executable(self.executable) is None:
            return RecallRefresh(
                True,
                discovery,
                "QMD executable is unavailable; exact Markdown recall remains available",
                False,
            )
        self._prepare_runtime()
        try:
            _publish_snapshot(self.snapshot_root, snapshots, deadline=deadline)
        except _RecallDeadlineExceeded:
            return RecallRefresh(
                True,
                discovery,
                "QMD rebuild timed out; exact Markdown recall remains available",
                False,
            )
        commands = (
            self._command("collection", "remove", self.collection),
            self._command(
                "collection",
                "add",
                str(self.snapshot_root),
                "--name",
                self.collection,
                "--mask",
                "**/*.md",
            ),
            self._command("update"),
            self._command("embed", "-f"),
        )
        for position, command in enumerate(commands):
            command_timeout = _remaining_timeout(deadline)
            if command_timeout is None:
                return RecallRefresh(
                    True,
                    discovery,
                    "QMD rebuild timed out; exact Markdown recall remains available",
                    False,
                )
            result = _run_command(
                command,
                cwd=self.index_root,
                env=self._environment(),
                timeout_seconds=command_timeout,
                output_limit=MAX_QMD_OUTPUT_BYTES,
            )
            if position == 0 and result.problem is None:
                continue
            problem = _command_problem(result, operation="rebuild")
            if problem is not None:
                return RecallRefresh(True, discovery, problem, False)
            if position == 1:
                bound, problem = self._collection_binding(deadline, operation="rebuild")
                if not bound:
                    return RecallRefresh(True, discovery, problem, False)
        _write_fingerprint(self.index_root, self.state_path.name, discovery.fingerprint)
        return RecallRefresh(True, discovery, None, True)

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        timeout_seconds: int = 20,
    ) -> RecallSearch:
        clean_query, terms = _search_query(query)
        if not 1 <= limit <= MAX_RESULTS:
            raise ValidationError(f"recall result limit must be between 1 and {MAX_RESULTS}")
        timeout = _bounded_timeout(
            timeout_seconds,
            maximum=MAX_SEARCH_TIMEOUT_SECONDS,
            label="QMD search",
        )
        deadline = time.monotonic() + timeout
        with exclusive_lock(self.lock_path, timeout=float(timeout)):
            return self._search_unlocked(
                clean_query,
                terms=terms,
                limit=limit,
                deadline=deadline,
            )

    def _search_unlocked(
        self,
        clean_query: str,
        *,
        terms: tuple[str, ...],
        limit: int,
        deadline: float,
    ) -> RecallSearch:
        try:
            snapshots, discovery = _discover(self.vault_root, deadline=deadline)
        except _RecallDeadlineExceeded:
            return RecallSearch(
                "markdown",
                False,
                (),
                "QMD search timed out before recall coverage could be established",
                _deadline_discovery().skipped,
            )
        qmd_reason: str | None = None
        if (
            _resolved_executable(self.executable) is not None
            and _index_directory_exists(self.index_root, self.snapshot_root.name)
            and _stored_fingerprint(self.index_root, self.state_path.name) == discovery.fingerprint
        ):
            bound, binding_problem = self._collection_binding(deadline, operation="search")
            if not bound:
                qmd_reason = binding_problem
            else:
                command_timeout = _remaining_timeout(deadline)
                if command_timeout is None:
                    return RecallSearch(
                        "markdown",
                        discovery.complete,
                        _fallback_hits(snapshots, terms=terms, limit=limit),
                        "QMD search timed out; exact Markdown recall was used",
                        discovery.skipped,
                    )
                result = _run_command(
                    self._command(
                        "query",
                        clean_query,
                        "-n",
                        str(limit),
                        "--json",
                        "-c",
                        self.collection,
                    ),
                    cwd=self.index_root,
                    env=self._environment(),
                    timeout_seconds=command_timeout,
                    output_limit=MAX_QMD_OUTPUT_BYTES,
                )
                qmd_reason = _command_problem(result, operation="search")
                if qmd_reason is None:
                    hits = _qmd_hits(
                        result.stdout,
                        snapshots=snapshots,
                        terms=terms,
                        limit=limit,
                        collection=self.collection,
                    )
                    if hits is not None:
                        return RecallSearch(
                            "qmd",
                            discovery.complete,
                            hits,
                            None,
                            discovery.skipped,
                        )
                    qmd_reason = "QMD returned an unreadable result; exact Markdown recall was used"
        elif _resolved_executable(self.executable) is None:
            qmd_reason = "QMD executable is unavailable; exact Markdown recall was used"
        else:
            qmd_reason = "QMD recall is not current; exact Markdown recall was used"
        return RecallSearch(
            "markdown",
            discovery.complete,
            _fallback_hits(snapshots, terms=terms, limit=limit),
            qmd_reason,
            discovery.skipped,
        )

    def _command(self, *arguments: str) -> tuple[str, ...]:
        return (self.executable, "--index", self.index, *arguments)

    def _collection_binding(
        self,
        deadline: float,
        *,
        operation: str,
    ) -> tuple[bool, str | None]:
        command_timeout = _remaining_timeout(deadline)
        if command_timeout is None:
            return False, f"QMD {operation} timed out; exact Markdown recall remains available"
        result = _run_command(
            self._command("collection", "show", self.collection),
            cwd=self.index_root,
            env=self._environment(),
            timeout_seconds=command_timeout,
            output_limit=128 * 1024,
        )
        problem = _command_problem(result, operation=operation)
        if problem is not None:
            return False, problem
        target = _qmd_collection_target(result.stdout, collection=self.collection)
        if target != self.snapshot_root:
            return (
                False,
                f"QMD {operation} collection binding is unverified; "
                "exact Markdown recall remains available",
            )
        return True, None

    def _environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "LANG", "LC_ALL", "SystemRoot", "TMPDIR", "WINDIR"}
        }
        if os.name == "nt":
            environment["PATH"] = os.defpath
        else:
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        environment["XDG_CACHE_HOME"] = str(self.index_root / "cache")
        environment["QMD_CONFIG_DIR"] = str(self.index_root / "config")
        return environment

    def _prepare_runtime(self) -> None:
        self.index_root.mkdir(parents=True, exist_ok=True)
        with _pinned_index_root(self.index_root) as descriptor:
            if os.name != "nt":
                os.fchmod(descriptor, 0o700)
            for name in ("cache", "config"):
                child = _open_index_directory(descriptor, (name,), create=True)
                try:
                    if os.name != "nt":
                        os.fchmod(child, 0o700)
                finally:
                    os.close(child)


def _discover(
    vault_root: Path,
    *,
    deadline: float | None = None,
) -> tuple[tuple[_DocumentSnapshot, ...], RecallDiscovery]:
    _check_deadline(deadline)
    try:
        root_metadata = os.lstat(vault_root)
    except OSError as exc:
        raise ValidationError("could not inspect the Seld vault for recall") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise ValidationError("Seld recall requires an ordinary vault directory")

    candidates: set[Path] = set()
    excluded: set[str] = set()
    entry_count = [0]
    for name in _ROOT_MARKDOWN:
        _check_deadline(deadline)
        path = vault_root / name
        if os.path.lexists(path):
            candidates.add(path)
    for relative in _MARKDOWN_TREES:
        _check_deadline(deadline)
        directory = vault_root / relative
        if os.path.lexists(directory):
            _walk_markdown(
                directory,
                candidates,
                vault_root=vault_root,
                deadline=deadline,
                entry_count=entry_count,
                excluded=excluded,
            )
    if len(candidates) > MAX_DOCUMENTS:
        raise ValidationError(f"Seld recall is limited to {MAX_DOCUMENTS} Markdown documents")

    snapshots: list[_DocumentSnapshot] = []
    skipped = [
        SkippedDocument(relative, "legacy_resident_control") for relative in sorted(excluded)
    ]
    total = 0
    for path in sorted(candidates, key=lambda item: item.relative_to(vault_root).as_posix()):
        _check_deadline(deadline)
        relative = path.relative_to(vault_root).as_posix()
        relative_path = PurePosixPath(relative)
        if _is_relative_posix(relative_path, _LEGACY_RESIDENT_CONTROL):
            skipped.append(SkippedDocument(relative, "legacy_resident_control"))
            continue
        try:
            content, metadata = _read_stable_markdown(
                path,
                vault_root=vault_root,
                deadline=deadline,
            )
        except _Skipped as exc:
            skipped.append(SkippedDocument(relative, exc.reason))
            continue
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValidationError(f"Seld recall is limited to {MAX_TOTAL_BYTES} Markdown bytes")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(SkippedDocument(relative, "invalid_utf8"))
            continue
        if _is_relative_posix(relative_path, _RESIDENT_CONTEXT):
            try:
                validate_imported_resident_content(
                    content,
                    label=f"imported resident recall document {relative}",
                )
            except ValidationError:
                skipped.append(SkippedDocument(relative, "privacy_quarantine"))
                continue
        digest = hashlib.sha256(content).hexdigest()
        document = RecallDocument(
            relative_path=relative,
            digest=digest,
            modified_at=datetime.fromtimestamp(metadata.st_mtime, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            size=len(content),
        )
        snapshots.append(_DocumentSnapshot(content, document))

    fingerprint = hashlib.sha256()
    for snapshot in snapshots:
        fingerprint.update(snapshot.document.relative_path.encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(snapshot.document.digest.encode("ascii"))
        fingerprint.update(b"\0")
    discovery = RecallDiscovery(
        tuple(snapshot.document for snapshot in snapshots),
        fingerprint.hexdigest(),
        tuple(skipped),
    )
    return tuple(snapshots), discovery


def _is_relative_posix(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or path.parts[: len(root.parts)] == root.parts


def _walk_markdown(
    directory: Path,
    candidates: set[Path],
    *,
    vault_root: Path,
    deadline: float | None,
    entry_count: list[int],
    excluded: set[str],
) -> None:
    if os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd:
        _walk_markdown_pinned(
            directory,
            candidates,
            vault_root=vault_root,
            deadline=deadline,
            entry_count=entry_count,
            excluded=excluded,
        )
        return
    try:
        root_metadata = os.lstat(directory)
    except OSError as exc:
        relative = directory.relative_to(vault_root).as_posix()
        raise ValidationError(f"could not inspect recall directory: {relative}") from exc
    if stat.S_ISLNK(root_metadata.st_mode):
        return
    if not stat.S_ISDIR(root_metadata.st_mode):
        relative = directory.relative_to(vault_root).as_posix()
        raise ValidationError(f"recall directory is not an ordinary directory: {relative}")
    pending = [directory]
    while pending:
        _check_deadline(deadline)
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            relative = current.relative_to(vault_root).as_posix()
            raise ValidationError(f"could not inspect recall directory: {relative}") from exc
        for entry in entries:
            _check_deadline(deadline)
            path = Path(entry.path)
            relative_path = PurePosixPath(*path.relative_to(vault_root).parts)
            if _is_relative_posix(relative_path, _LEGACY_RESIDENT_CONTROL):
                excluded.add(_LEGACY_RESIDENT_CONTROL.as_posix())
                continue
            _count_discovery_entry(entry_count)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                relative = path.relative_to(vault_root).as_posix()
                raise ValidationError(f"could not inspect recall path: {relative}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode) and path.suffix.casefold() == ".md":
                candidates.add(path)
                if len(candidates) > MAX_DOCUMENTS:
                    raise ValidationError(
                        f"Seld recall is limited to {MAX_DOCUMENTS} Markdown documents"
                    )


def _walk_markdown_pinned(
    directory: Path,
    candidates: set[Path],
    *,
    vault_root: Path,
    deadline: float | None,
    entry_count: list[int],
    excluded: set[str],
) -> None:
    try:
        relative = directory.relative_to(vault_root)
    except ValueError as exc:
        raise ValidationError("recall directory is outside the Seld vault") from exc
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = -1
    initial_descriptor = -1
    pending: list[tuple[int, PurePosixPath]] = []
    try:
        root_descriptor = os.open(vault_root, directory_flags)
        root_identity = _directory_identity(os.fstat(root_descriptor))
        current = os.dup(root_descriptor)
        try:
            for component in relative.parts:
                next_descriptor = os.open(component, directory_flags, dir_fd=current)
                os.close(current)
                current = next_descriptor
            initial_descriptor = current
        except BaseException:
            os.close(current)
            raise
        pending.append((initial_descriptor, PurePosixPath(*relative.parts)))
        initial_descriptor = -1
        while pending:
            _check_deadline(deadline)
            descriptor, logical = pending.pop()
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        _check_deadline(deadline)
                        child_logical = logical / entry.name
                        if _is_relative_posix(child_logical, _LEGACY_RESIDENT_CONTROL):
                            excluded.add(_LEGACY_RESIDENT_CONTROL.as_posix())
                            continue
                        _count_discovery_entry(entry_count)
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            candidate = (logical / entry.name).as_posix()
                            raise ValidationError(
                                f"could not inspect recall path: {candidate}"
                            ) from exc
                        if stat.S_ISLNK(metadata.st_mode):
                            continue
                        if stat.S_ISDIR(metadata.st_mode):
                            child = os.open(entry.name, directory_flags, dir_fd=descriptor)
                            opened = os.fstat(child)
                            if _directory_identity(opened) != _directory_identity(metadata):
                                os.close(child)
                                raise ValidationError(
                                    "recall directory changed while opened: "
                                    f"{child_logical.as_posix()}"
                                )
                            pending.append((child, child_logical))
                        elif (
                            stat.S_ISREG(metadata.st_mode)
                            and PurePosixPath(entry.name).suffix.casefold() == ".md"
                        ):
                            candidates.add(vault_root / Path(*child_logical.parts))
                            if len(candidates) > MAX_DOCUMENTS:
                                raise ValidationError(
                                    f"Seld recall is limited to {MAX_DOCUMENTS} Markdown documents"
                                )
            except OSError as exc:
                raise ValidationError(
                    f"could not inspect recall directory: {logical.as_posix()}"
                ) from exc
            finally:
                os.close(descriptor)
        current_root = os.lstat(vault_root)
        if _directory_identity(current_root) != root_identity:
            raise ValidationError("Seld vault changed identity during recall discovery")
    except ValidationError:
        raise
    except OSError as exc:
        relative_text = PurePosixPath(*relative.parts).as_posix()
        raise ValidationError(f"could not inspect recall directory: {relative_text}") from exc
    finally:
        if initial_descriptor >= 0:
            os.close(initial_descriptor)
        for descriptor, _ in pending:
            os.close(descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("recall path is not an ordinary directory")
    return int(metadata.st_dev), int(metadata.st_ino)


class _Skipped(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _read_stable_markdown(
    path: Path,
    *,
    vault_root: Path,
    deadline: float | None,
) -> tuple[bytes, os.stat_result]:
    _check_deadline(deadline)
    if os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd:
        return _read_descriptor_pinned_markdown(
            path,
            vault_root=vault_root,
            deadline=deadline,
        )
    return _read_path_markdown(path, vault_root=vault_root, deadline=deadline)


def _read_descriptor_pinned_markdown(
    path: Path,
    *,
    vault_root: Path,
    deadline: float | None,
) -> tuple[bytes, os.stat_result]:
    try:
        relative = path.relative_to(vault_root)
    except ValueError as exc:
        raise _Skipped("outside_vault") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise _Skipped("outside_vault")
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        current = os.open(vault_root, directory_flags)
        descriptors.append(current)
        root_metadata = os.fstat(current)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise _Skipped("unavailable")
        for component in relative.parts[:-1]:
            _check_deadline(deadline)
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise _Skipped("unavailable")
        name = relative.parts[-1]
        before = os.stat(name, dir_fd=current, follow_symlinks=False)
        return _read_open_markdown(
            path,
            before=before,
            opener=lambda flags: os.open(name, flags, dir_fd=current),
            final_stat=lambda: os.stat(name, dir_fd=current, follow_symlinks=False),
            deadline=deadline,
        )
    except _Skipped:
        raise
    except OSError as exc:
        raise _Skipped("unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _read_path_markdown(
    path: Path,
    *,
    vault_root: Path,
    deadline: float | None,
) -> tuple[bytes, os.stat_result]:
    _require_ordinary_ancestors(path, vault_root=vault_root)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise _Skipped("unavailable") from exc
    result = _read_open_markdown(
        path,
        before=before,
        opener=lambda flags: os.open(path, flags),
        final_stat=lambda: os.lstat(path),
        deadline=deadline,
    )
    _require_ordinary_ancestors(path, vault_root=vault_root)
    return result


def _read_open_markdown(
    path: Path,
    *,
    before: os.stat_result,
    opener: Callable[[int], int],
    final_stat: Callable[[], os.stat_result],
    deadline: float | None,
) -> tuple[bytes, os.stat_result]:
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise _Skipped("not_regular")
    if _is_placeholder(path, before):
        raise _Skipped("cloud_placeholder")
    if before.st_size > MAX_DOCUMENT_BYTES:
        raise _Skipped("too_large")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = opener(flags)
    except OSError as exc:
        raise _Skipped("unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if _file_snapshot(opened) != _file_snapshot(before) or _is_placeholder(path, opened):
            raise _Skipped("changed_during_read")
        chunks: list[bytes] = []
        size = 0
        while True:
            _check_deadline(deadline)
            chunk = os.read(descriptor, min(64 * 1024, MAX_DOCUMENT_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_DOCUMENT_BYTES:
                raise _Skipped("too_large")
        finished = os.fstat(descriptor)
        if _file_snapshot(finished) != _file_snapshot(opened) or _is_placeholder(path, finished):
            raise _Skipped("changed_during_read")
        named = final_stat()
        if _file_snapshot(named) != _file_snapshot(finished) or stat.S_ISLNK(named.st_mode):
            raise _Skipped("changed_during_read")
        return b"".join(chunks), finished
    finally:
        os.close(descriptor)


def _require_ordinary_ancestors(path: Path, *, vault_root: Path) -> None:
    try:
        relative = path.relative_to(vault_root)
    except ValueError as exc:
        raise _Skipped("outside_vault") from exc
    current = vault_root
    for component in relative.parts[:-1]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise _Skipped("unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise _Skipped("unavailable")


def _file_snapshot(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _is_placeholder(path: Path, metadata: os.stat_result) -> bool:
    if path.name.casefold().endswith(".icloud"):
        return True
    windows_attributes = int(getattr(metadata, "st_file_attributes", 0))
    windows_offline = 0x1000 | 0x400000
    macos_flags = int(getattr(metadata, "st_flags", 0))
    macos_dataless = 0x40000000
    return bool(windows_attributes & windows_offline or macos_flags & macos_dataless)


def _publish_snapshot(
    root: Path,
    snapshots: tuple[_DocumentSnapshot, ...],
    *,
    deadline: float | None,
) -> None:
    if root.name != "documents":
        raise ValidationError("QMD document snapshot has an unexpected path")
    stage_name = f".documents-stage-{uuid.uuid4().hex}"
    stage_exists = False
    with _pinned_index_root(root.parent) as index_descriptor:
        try:
            os.mkdir(stage_name, 0o700, dir_fd=index_descriptor)
            stage_exists = True
            stage_descriptor = _open_index_directory(index_descriptor, (stage_name,))
            try:
                for snapshot in snapshots:
                    _check_deadline(deadline)
                    parts = _snapshot_parts(snapshot.document.relative_path)
                    _atomic_write_at(stage_descriptor, parts, snapshot.content)
            finally:
                os.close(stage_descriptor)
            _check_deadline(deadline)
            if _entry_exists_at(index_descriptor, root.name):
                metadata = os.stat(root.name, dir_fd=index_descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise ValidationError("QMD document snapshot must be an ordinary directory")
                _remove_tree_at(index_descriptor, root.name, deadline=deadline)
            _check_deadline(deadline)
            os.rename(
                stage_name,
                root.name,
                src_dir_fd=index_descriptor,
                dst_dir_fd=index_descriptor,
            )
            os.fsync(index_descriptor)
            stage_exists = False
        finally:
            if stage_exists and _entry_exists_at(index_descriptor, stage_name):
                _remove_tree_at(index_descriptor, stage_name, deadline=None)


def _stored_fingerprint(index_root: Path, name: str) -> str | None:
    encoded = _read_index_file(index_root, name, max_bytes=4_096)
    if encoded is None:
        return None
    try:
        payload = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeError):
        return None
    value = payload.get("fingerprint") if isinstance(payload, dict) else None
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def _write_fingerprint(index_root: Path, name: str, fingerprint: str) -> None:
    payload = {
        "fingerprint": fingerprint,
        "refreshed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    with _pinned_index_root(index_root) as descriptor:
        _atomic_write_at(descriptor, (name,), encoded)


def _index_directory_exists(index_root: Path, name: str) -> bool:
    if not os.path.lexists(index_root):
        return False
    with _pinned_index_root(index_root) as descriptor:
        if not _entry_exists_at(descriptor, name):
            return False
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValidationError("QMD index directory must be an ordinary directory")
        return True


def _read_index_file(index_root: Path, name: str, *, max_bytes: int) -> bytes | None:
    if not os.path.lexists(index_root):
        return None
    with _pinned_index_root(index_root) as descriptor:
        if not _entry_exists_at(descriptor, name):
            return None
        listed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(listed.st_mode)
            or stat.S_ISLNK(listed.st_mode)
            or listed.st_size > max_bytes
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(name, flags, dir_fd=descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if _file_snapshot(opened) != _file_snapshot(listed):
                raise ValidationError("QMD recall fingerprint changed while opened")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                block = os.read(file_descriptor, min(4_096, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            encoded = b"".join(chunks)
            if len(encoded) > max_bytes or _file_snapshot(os.fstat(file_descriptor)) != (
                _file_snapshot(opened)
            ):
                raise ValidationError("QMD recall fingerprint changed while read")
            return encoded
        finally:
            os.close(file_descriptor)


@contextmanager
def _pinned_index_root(root: Path) -> Iterator[int]:
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise ValidationError("secure QMD index storage is unavailable on this platform")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = os.open(root.parent, flags)
    root_descriptor = -1
    try:
        listed = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        root_descriptor = os.open(root.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(root_descriptor)
        identity = _directory_identity(opened)
        if _directory_identity(listed) != identity:
            raise ValidationError("QMD index root changed while it was pinned")
        yield root_descriptor
        current = os.stat(root.name, dir_fd=parent_descriptor, follow_symlinks=False)
        try:
            current_identity = _directory_identity(current)
        except ValidationError as exc:
            raise ValidationError("QMD index root changed during the operation") from exc
        if current_identity != identity:
            raise ValidationError("QMD index root changed during the operation")
    except OSError as exc:
        raise ValidationError("QMD index root could not be pinned safely") from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _open_index_directory(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    create: bool = False,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.dup(root_descriptor)
    try:
        for name in parts:
            _safe_component(name)
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=current)
            listed = os.stat(name, dir_fd=current, follow_symlinks=False)
            child = os.open(name, flags, dir_fd=current)
            opened = os.fstat(child)
            if _directory_identity(listed) != _directory_identity(opened):
                os.close(child)
                raise ValidationError("QMD index directory changed while opened")
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _atomic_write_at(root_descriptor: int, parts: tuple[str, ...], content: bytes) -> None:
    if not parts:
        raise ValidationError("QMD snapshot target is invalid")
    parent = _open_index_directory(root_descriptor, parts[:-1], create=True)
    name = _safe_component(parts[-1])
    temporary = f".{name}.tmp-{uuid.uuid4().hex}"
    descriptor = -1
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short QMD snapshot write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def _remove_tree_at(parent: int, name: str, *, deadline: float | None) -> None:
    _safe_component(name)
    _check_deadline(deadline)
    listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(listed.st_mode) or stat.S_ISLNK(listed.st_mode):
        raise ValidationError("QMD snapshot cleanup refused a non-directory target")
    descriptor = _open_index_directory(parent, (name,))
    try:
        with os.scandir(descriptor) as entries:
            names = [entry.name for entry in entries]
        for child_name in names:
            _check_deadline(deadline)
            child = os.stat(child_name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(child.st_mode) and not stat.S_ISLNK(child.st_mode):
                _remove_tree_at(descriptor, child_name, deadline=deadline)
            else:
                os.unlink(child_name, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent)


def _entry_exists_at(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _snapshot_parts(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts:
        raise ValidationError("QMD snapshot path is invalid")
    return tuple(_safe_component(part) for part in path.parts)


def _safe_component(value: str) -> str:
    if value in {"", ".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValidationError("QMD snapshot path contains an unsafe component")
    return value


def _fallback_hits(
    snapshots: tuple[_DocumentSnapshot, ...],
    *,
    terms: tuple[str, ...],
    limit: int,
) -> tuple[RecallHit, ...]:
    matches: list[tuple[int, int, RecallHit]] = []
    for snapshot in snapshots:
        text = snapshot.content.decode("utf-8")
        folded = text.casefold()
        counts = tuple(folded.count(term) for term in terms)
        if not counts or any(count == 0 for count in counts):
            continue
        modified_ns = int(
            datetime.fromisoformat(snapshot.document.modified_at.replace("Z", "+00:00")).timestamp()
            * 1_000_000_000
        )
        score = float(sum(counts))
        hit = RecallHit(
            modified_at=snapshot.document.modified_at,
            relative_path=snapshot.document.relative_path,
            score=score,
            snippet=_keyword_snippet(text, terms),
            title=_document_title(text, snapshot.document.relative_path),
        )
        matches.append((sum(counts), modified_ns, hit))
    matches.sort(key=lambda item: (-item[0], -item[1], item[2].relative_path))
    return tuple(item[2] for item in matches[:limit])


def _qmd_hits(
    encoded: bytes,
    *,
    snapshots: tuple[_DocumentSnapshot, ...],
    terms: tuple[str, ...],
    limit: int,
    collection: str,
) -> tuple[RecallHit, ...] | None:
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
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
    known = {snapshot.document.relative_path: snapshot for snapshot in snapshots}
    folded: dict[str, _DocumentSnapshot | None] = {}
    for relative, snapshot in known.items():
        key = relative.casefold()
        folded[key] = snapshot if key not in folded else None
    hits: list[RecallHit] = []
    seen: set[str] = set()
    for row in rows:
        if len(hits) >= limit:
            break
        if not isinstance(row, dict):
            continue
        reference = next(
            (str(row[key]) for key in ("file", "path", "url", "uri") if row.get(key)),
            "",
        )
        relative = _qmd_relative(reference, collection=collection)
        matched = known.get(relative) or folded.get(relative.casefold())
        if matched is None:
            continue
        relative = matched.document.relative_path
        if relative in seen:
            continue
        seen.add(relative)
        text = matched.content.decode("utf-8")
        raw_score = row.get("score", 0.0)
        score = (
            float(raw_score)
            if isinstance(raw_score, (int, float))
            and not isinstance(raw_score, bool)
            and math.isfinite(float(raw_score))
            else 0.0
        )
        hits.append(
            RecallHit(
                modified_at=matched.document.modified_at,
                relative_path=relative,
                score=score,
                snippet=_keyword_snippet(text, terms),
                title=_document_title(text, relative),
            )
        )
    return tuple(hits)


def _qmd_relative(reference: str, *, collection: str) -> str:
    if not reference:
        return ""
    parsed = urlsplit(reference)
    if parsed.scheme == "qmd":
        if parsed.netloc != collection:
            return ""
        candidate = unquote(parsed.path).lstrip("/")
    else:
        candidate = unquote(reference).replace("\\", "/")
        marker = f"/{collection}/"
        if marker in candidate:
            candidate = candidate.split(marker, 1)[1]
        elif candidate.startswith(f"{collection}/"):
            candidate = candidate[len(collection) + 1 :]
        else:
            return ""
    path = PurePosixPath(candidate)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _qmd_collection_target(encoded: bytes, *, collection: str) -> Path | None:
    try:
        lines = encoded.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    headers = [line for line in lines if line.startswith("Collection: ")]
    paths = [line for line in lines if line.startswith("  Path:     ")]
    if headers != [f"Collection: {collection}"] or len(paths) != 1:
        return None
    value = paths[0][len("  Path:     ") :]
    if not value or value != value.strip() or "\x00" in value:
        return None
    try:
        return _absolute_path(value, "QMD collection target")
    except ValidationError:
        return None


def _document_title(text: str, relative: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean.startswith("# "):
            return _plain_excerpt(clean[2:], maximum=180)
    return PurePosixPath(relative).stem.replace("-", " ").replace("_", " ").strip() or "Memory"


def _keyword_snippet(text: str, terms: tuple[str, ...]) -> str:
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    start = max(0, min(positions, default=0) - 120)
    return _plain_excerpt(text[start : start + 520])


def _plain_excerpt(value: str, *, maximum: int = 420) -> str:
    clean = " ".join(value.split())
    if len(clean) <= maximum:
        return clean
    return clean[: maximum - 1].rstrip() + "…"


def _search_query(value: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise ValidationError("recall query must be text")
    clean = " ".join(value.split())
    if not clean or len(clean) > MAX_QUERY_CHARACTERS or "\x00" in clean:
        raise ValidationError(f"recall query must contain 1 to {MAX_QUERY_CHARACTERS} characters")
    terms = tuple(dict.fromkeys(match.group(0).casefold() for match in _QUERY_TERM.finditer(clean)))
    if not terms:
        raise ValidationError("recall query must contain at least one word")
    return clean, terms


def _command_problem(result: _CommandResult, *, operation: str) -> str | None:
    if result.problem == "timeout":
        return f"QMD {operation} timed out; exact Markdown recall remains available"
    if result.problem == "output_limit":
        return f"QMD {operation} exceeded its output bound; exact Markdown recall remains available"
    if result.problem is not None:
        return f"QMD {operation} is unavailable; exact Markdown recall remains available"
    if result.returncode:
        return (
            f"QMD {operation} failed with exit code {result.returncode}; "
            "exact Markdown recall remains available"
        )
    return None


def _run_command(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    output_limit: int,
) -> _CommandResult:
    """Run one local command with hard wall-clock and captured-output bounds."""

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            ),
        )
    except OSError:
        return _CommandResult(127, b"", b"", "unavailable")

    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    total = 0
    output_lock = threading.Lock()
    overflow = threading.Event()

    def drain(name: str, stream: BinaryIO) -> None:
        nonlocal total
        while True:
            chunk = stream.read(16 * 1024)
            if not chunk:
                return
            with output_lock:
                remaining = max(0, output_limit - total)
                if remaining:
                    chunks[name].append(chunk[:remaining])
                    total += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    overflow.set()
                    return

    assert process.stdout is not None and process.stderr is not None
    readers = (
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_seconds
    problem: str | None = None
    while process.poll() is None:
        if overflow.is_set():
            problem = "output_limit"
            _stop_process(process)
            break
        if time.monotonic() >= deadline:
            problem = "timeout"
            _stop_process(process)
            break
        time.sleep(0.01)
    for reader in readers:
        reader.join(timeout=1.0)
    if overflow.is_set() and problem is None:
        problem = "output_limit"
    if any(reader.is_alive() for reader in readers):
        problem = problem or "unavailable"
        _stop_process(process)
    return _CommandResult(
        process.returncode if process.returncode is not None else 1,
        b"".join(chunks["stdout"]),
        b"".join(chunks["stderr"]),
        problem,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        _stop_process_group(process)
        return
    try:
        process.terminate()
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            break
        time.sleep(0.01)
    with suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=1.0)


def _absolute_path(value: Path | str, label: str) -> Path:
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError(f"invalid {label}") from exc


def _executable(value: Path | str) -> str:
    clean = str(value).strip()
    if not clean or "\x00" in clean or "\n" in clean or "\r" in clean:
        raise ValidationError("QMD executable is invalid")
    if clean == "qmd":
        for candidate in _DEFAULT_QMD_EXECUTABLES:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve(strict=True))
        return str(_DEFAULT_QMD_EXECUTABLES[0])
    path = Path(clean).expanduser()
    if not path.is_absolute():
        raise ValidationError("QMD executable must be an exact absolute path")
    return str(path.resolve(strict=path.exists()))


def _resolved_executable(value: str) -> str | None:
    path = Path(value)
    return value if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None


def _require_disjoint_roots(vault_root: Path, index_root: Path) -> None:
    if _is_relative_to(index_root, vault_root) or _is_relative_to(vault_root, index_root):
        raise ValidationError("QMD index storage must be outside and separate from the Seld vault")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _bounded_timeout(value: int, *, maximum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValidationError(f"{label} timeout must be between 1 and {maximum} seconds")
    return value


def _remaining_timeout(deadline: float) -> float | None:
    remaining = deadline - time.monotonic()
    return remaining if remaining > 0 else None


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _RecallDeadlineExceeded


def _count_discovery_entry(entry_count: list[int]) -> None:
    entry_count[0] += 1
    if entry_count[0] > MAX_DISCOVERY_ENTRIES:
        raise ValidationError(
            f"Seld recall is limited to {MAX_DISCOVERY_ENTRIES} filesystem entries"
        )


def _deadline_discovery() -> RecallDiscovery:
    return RecallDiscovery(
        (),
        hashlib.sha256(b"").hexdigest(),
        (SkippedDocument("recall", "deadline_exceeded"),),
    )
