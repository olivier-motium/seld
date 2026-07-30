"""Cross-platform locking and crash-safe local file primitives."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import os
import secrets
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from continuity_kernel.errors import (
    ConflictError,
    MutationCommittedError,
    ValidationError,
)


class _PosixLock(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


class _WindowsLock(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, length: int) -> None: ...


class _LockTarget(Protocol):
    @property
    def name(self) -> Any: ...

    def fileno(self) -> int: ...

    def seek(self, offset: int) -> Any: ...


@dataclass(frozen=True)
class _DescriptorLockTarget:
    descriptor: int
    name: str

    def fileno(self) -> int:
        return self.descriptor

    def seek(self, offset: int) -> int:
        return os.lseek(self.descriptor, offset, os.SEEK_SET)


@dataclass
class _DirectoryBinding:
    parts: tuple[str, ...]
    descriptor: int
    identity: tuple[int, int]
    depth: int = 1


@dataclass
class _RegularFileWatch:
    parts: tuple[str, ...]
    parent_descriptor: int
    descriptor: int
    identity: tuple[int, int]
    depth: int = 1


class AppendOutcome(StrEnum):
    RESTORED = "restored"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class DurableAppendError(OSError):
    """An append failed with an explicit durable-state outcome."""

    outcome: AppendOutcome

    def __init__(self, message: str, *, outcome: AppendOutcome):
        super().__init__(message)
        self.outcome = outcome


class _PinnedLockTeardownError(ValidationError):
    """A lock validated on entry but failed validation or cleanup after its body."""


class PublishOutcome(StrEnum):
    UNPUBLISHED = "unpublished"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class DurablePublishError(OSError):
    """A no-clobber publication failed with an explicit visible-state outcome."""

    outcome: PublishOutcome

    def __init__(self, message: str, *, outcome: PublishOutcome):
        super().__init__(message)
        self.outcome = outcome


PINNED_PATH_ROOT_SUPPORTED = (
    os.name != "nt" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
)
_POSIX_OS = cast(Any, os)


@dataclass(frozen=True)
class RegularFileSnapshot:
    """Stable metadata captured from an already-open regular file."""

    device: int
    inode: int
    size: int
    modified_ns: int
    mode: int


def _snapshot(metadata: os.stat_result) -> RegularFileSnapshot:
    return RegularFileSnapshot(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        modified_ns=int(metadata.st_mtime_ns),
        mode=int(metadata.st_mode),
    )


def _same_file(left: RegularFileSnapshot, right: RegularFileSnapshot) -> bool:
    if os.name != "nt":
        return (left.device, left.inode) == (right.device, right.inode)
    # CPython can expose different Windows file-index encodings for path and
    # descriptor stat calls. Size and timestamp are checked here and again
    # after the bounded read; lstat also rejects reparse-point swaps.
    return (
        left.device == right.device
        and left.size == right.size
        and left.modified_ns == right.modified_ns
    )


class PinnedPathRoot:
    """Directory-fd anchored reads, writes, and locks beneath one local root.

    The root descriptor stays open for the whole operation. Every descendant
    directory is opened one component at a time with ``O_NOFOLLOW`` so a
    transient ancestor rename or symlink cannot redirect an operation. Each
    instance belongs to one serialized operation and must not be shared across
    threads: root-lock ownership follows the instance's open file description.
    """

    def __init__(self, root: Path):
        if not PINNED_PATH_ROOT_SUPPORTED:
            raise ValidationError("secure directory-pinned storage is unavailable on this platform")
        expanded = root.expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        try:
            canonical_parent = expanded.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValidationError(
                f"could not resolve local storage root parent: {root}: {exc}"
            ) from exc
        root_name = expanded.name
        if not root_name:
            raise ValidationError("secure directory-pinned storage requires a named root")
        canonical_root = canonical_parent / root_name
        flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
        parent_descriptor = -1
        root_descriptor = -1
        try:
            parent_descriptor = os.open(canonical_parent, flags)
            listed = os.stat(root_name, dir_fd=parent_descriptor, follow_symlinks=False)
            root_descriptor = os.open(root_name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            if root_descriptor >= 0:
                os.close(root_descriptor)
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
            raise ValidationError(f"could not pin local storage root: {root}: {exc}") from exc
        parent_metadata = os.fstat(parent_descriptor)
        metadata = os.fstat(root_descriptor)
        listed_snapshot = _snapshot(listed)
        opened_snapshot = _snapshot(metadata)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or not stat.S_ISDIR(listed.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or not _same_file(listed_snapshot, opened_snapshot)
        ):
            os.close(root_descriptor)
            os.close(parent_descriptor)
            raise ValidationError(f"local storage root is not a stable directory: {root}")
        self.root = canonical_root
        self._root_parent_descriptor = parent_descriptor
        self._root_parent_identity = (
            int(parent_metadata.st_dev),
            int(parent_metadata.st_ino),
        )
        self._root_name = root_name
        self._root_descriptor = root_descriptor
        self._root_identity = (int(metadata.st_dev), int(metadata.st_ino))
        self._directory_binding: _DirectoryBinding | None = None
        self._directory_watches: dict[tuple[str, ...], _DirectoryBinding] = {}
        self._file_watches: dict[tuple[str, ...], _RegularFileWatch] = {}
        self._retained_directories: dict[tuple[str, ...], _DirectoryBinding] = {}
        self._retained_files: dict[tuple[str, ...], _RegularFileWatch] = {}
        try:
            self._validate_root_identity()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._directory_binding is not None or self._directory_watches or self._file_watches:
            raise ValidationError("cannot close pinned local storage with an active binding")
        for watch in self._retained_files.values():
            with suppress(OSError):
                os.close(watch.descriptor)
            with suppress(OSError):
                os.close(watch.parent_descriptor)
        self._retained_files.clear()
        for binding in self._retained_directories.values():
            with suppress(OSError):
                os.close(binding.descriptor)
        self._retained_directories.clear()
        descriptor = self._root_descriptor
        self._root_descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)
        parent_descriptor = self._root_parent_descriptor
        self._root_parent_descriptor = -1
        if parent_descriptor >= 0:
            os.close(parent_descriptor)

    def _validate_root_identity(self) -> None:
        """Prove that the pinned directory is still the canonical root path."""

        if self._root_descriptor < 0 or self._root_parent_descriptor < 0:
            raise ValidationError("pinned local storage root is closed")
        try:
            opened = os.fstat(self._root_descriptor)
            parent_opened = os.fstat(self._root_parent_descriptor)
            parent_entry = os.stat(
                self._root_name,
                dir_fd=self._root_parent_descriptor,
                follow_symlinks=False,
            )
            canonical_parent = os.lstat(self.root.parent)
            canonical = os.lstat(self.root)
        except OSError as exc:
            raise ValidationError(
                f"pinned local storage root no longer has its canonical path: {self.root}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(parent_opened.st_mode)
            or not stat.S_ISDIR(parent_entry.st_mode)
            or not stat.S_ISDIR(canonical_parent.st_mode)
            or not stat.S_ISDIR(canonical.st_mode)
            or (int(opened.st_dev), int(opened.st_ino)) != self._root_identity
            or (int(parent_opened.st_dev), int(parent_opened.st_ino)) != self._root_parent_identity
            or (int(canonical_parent.st_dev), int(canonical_parent.st_ino))
            != self._root_parent_identity
            or (int(parent_entry.st_dev), int(parent_entry.st_ino)) != self._root_identity
            or (int(canonical.st_dev), int(canonical.st_ino)) != self._root_identity
        ):
            raise ValidationError(
                f"pinned local storage root changed identity at its canonical path: {self.root}"
            )

    def directory_exists(self, relative: Path | str) -> bool:
        parts = _relative_parts(relative)
        self._validate_root_identity()
        try:
            descriptor = self._open_directory(parts)
        except FileNotFoundError:
            self._confirm_directory_absent(parts)
            return False
        try:
            self._validate_directory_path(parts, descriptor)
            return True
        finally:
            os.close(descriptor)

    def count_directory_entries(
        self,
        relative: Path | str,
        *,
        suffix: str | None = None,
        stop_at: int | None = None,
    ) -> int:
        """Count bounded matching entries through an already-pinned directory descriptor."""

        if suffix is not None and (not suffix or "/" in suffix or "\\" in suffix):
            raise ValidationError("directory entry suffix must be one non-empty filename suffix")
        if stop_at is not None and (
            not isinstance(stop_at, int) or isinstance(stop_at, bool) or stop_at <= 0
        ):
            raise ValidationError("directory entry count bound must be a positive integer")
        parts = _relative_parts(relative)
        self._validate_root_identity()
        try:
            descriptor = self._open_directory(parts)
        except OSError as exc:
            raise ValidationError(
                f"could not open directory beneath the pinned root: {self.root}: {exc}"
            ) from exc
        try:
            self._validate_directory_path(parts, descriptor)
            count = 0
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        if suffix is not None and not entry.name.endswith(suffix):
                            continue
                        count += 1
                        if stop_at is not None and count >= stop_at:
                            break
            except OSError as exc:
                raise ValidationError(
                    f"could not count directory entries beneath the pinned root: {self.root}: {exc}"
                ) from exc
            self._validate_directory_path(parts, descriptor)
            return count
        finally:
            os.close(descriptor)

    def list_directory_entry_names(
        self,
        relative: Path | str,
        *,
        max_entries: int,
        suffix: str | None = None,
    ) -> tuple[str, ...]:
        """List bounded names through a pinned descendant directory descriptor."""

        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries <= 0:
            raise ValidationError("directory entry list bound must be a positive integer")
        if suffix is not None and (not suffix or "/" in suffix or "\\" in suffix):
            raise ValidationError("directory entry suffix must be one non-empty filename suffix")
        parts = _relative_parts(relative)
        self._validate_root_identity()
        try:
            descriptor = self._open_directory(parts)
        except OSError as exc:
            raise ValidationError(
                f"could not open directory beneath the pinned root: {self.root}: {exc}"
            ) from exc
        try:
            self._validate_directory_path(parts, descriptor)
            names: list[str] = []
            seen = 0
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > max_entries:
                            raise ValidationError(
                                "directory contains more entries than the supported bound"
                            )
                        if suffix is not None and not entry.name.endswith(suffix):
                            continue
                        names.append(entry.name)
            except ValidationError:
                raise
            except OSError as exc:
                raise ValidationError(
                    f"could not list directory entries beneath the pinned root: {self.root}: {exc}"
                ) from exc
            self._validate_directory_path(parts, descriptor)
            return tuple(sorted(names))
        finally:
            os.close(descriptor)

    def ensure_directory(self, relative: Path | str, *, mode: int = 0o700) -> None:
        parts = _relative_parts(relative)
        self._validate_root_identity()
        descriptor = self._open_directory(parts, create=True, mode=mode)
        try:
            _POSIX_OS.fchmod(descriptor, mode)
            os.fsync(descriptor)
            try:
                self._validate_directory_path(parts, descriptor)
            except ValidationError as exc:
                raise DurablePublishError(
                    f"directory state may have changed outside its canonical path: {self.root}",
                    outcome=PublishOutcome.UNKNOWN,
                ) from exc
        finally:
            os.close(descriptor)

    def read_regular_file(
        self,
        relative: Path | str,
        *,
        label: str,
        max_bytes: int,
        missing_ok: bool = False,
        retain: bool = False,
    ) -> bytes | None:
        parts = _relative_parts(relative)
        parent_parts = parts[:-1]
        self._validate_root_identity()
        try:
            parent, name = self._open_parent(parts)
        except FileNotFoundError:
            self._confirm_directory_absent(parent_parts)
            if missing_ok:
                return None
            raise ValidationError(f"{label} is missing beneath the pinned root") from None
        descriptor = -1
        try:
            retained = self._retained_files.get(parts)
            if retained is not None:
                self._validate_regular_file_watch(retained, label=label)
            try:
                listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                self._confirm_leaf_absent(parent_parts, parent, name, label=label)
                if missing_ok:
                    if retain:
                        self._retain_missing_parent(parent_parts, parent)
                    return None
                raise ValidationError(f"{label} is missing beneath the pinned root") from None
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
                raise ValidationError(f"{label} must be a regular file, not a link")
            if (
                retained is not None
                and (
                    int(listed.st_dev),
                    int(listed.st_ino),
                )
                != retained.identity
            ):
                raise ValidationError(f"{label} changed identity after its authoritative read")
            if listed.st_size > max_bytes:
                raise ValidationError(f"{label} exceeds its size bound")
            flags = os.O_RDONLY | _POSIX_OS.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(name, flags, dir_fd=parent)
            opened = os.fstat(descriptor)
            opened_snapshot = _snapshot(opened)
            if not stat.S_ISREG(opened.st_mode) or not _same_file(
                _snapshot(listed), opened_snapshot
            ):
                raise ValidationError(f"{label} changed while it was opened")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                block = os.read(descriptor, min(64 * 1024, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            content = b"".join(chunks)
            if len(content) > max_bytes:
                raise ValidationError(f"{label} exceeds its size bound")
            finished = _snapshot(os.fstat(descriptor))
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or finished != opened_snapshot
                or not _same_file(_snapshot(current), opened_snapshot)
            ):
                raise ValidationError(f"{label} changed while it was read")
            self._validate_directory_path(parent_parts, parent)
            if retain:
                self._retain_regular_file(parts, parent, descriptor, opened_snapshot)
            return content
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError(f"could not read {label} beneath the pinned root: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)

    def retain_directory(self, relative: Path | str) -> tuple[int, int]:
        """Keep one authoritative descendant directory identity until this root closes."""

        candidate = Path(relative)
        parts = (
            ()
            if not candidate.is_absolute() and candidate == Path(".")
            else _relative_parts(relative)
        )
        retained = self._retained_directories.get(parts)
        if retained is not None:
            self._validate_directory_path(parts, retained.descriptor)
            return retained.identity
        descriptor = self._open_directory(parts)
        try:
            self._validate_directory_path(parts, descriptor)
            metadata = os.fstat(descriptor)
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            self._retained_directories[parts] = _DirectoryBinding(
                parts=parts,
                descriptor=os.dup(descriptor),
                identity=identity,
            )
            return identity
        finally:
            os.close(descriptor)

    @contextmanager
    def open_regular_file_descriptor(
        self,
        relative: Path | str,
        *,
        label: str,
        writable: bool = False,
    ) -> Iterator[int]:
        """Yield one no-follow descriptor and validate the same named inode on exit."""

        parts = _relative_parts(relative)
        parent, name = self._open_parent(parts)
        descriptor = -1
        try:
            self._validate_directory_path(parts[:-1], parent)
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
                raise ValidationError(f"{label} must be a regular file, not a link")
            flags = (os.O_RDWR if writable else os.O_RDONLY) | _POSIX_OS.O_NOFOLLOW
            descriptor = os.open(name, flags, dir_fd=parent)
            opened = os.fstat(descriptor)
            identity = (int(opened.st_dev), int(opened.st_ino))
            if (
                not stat.S_ISREG(opened.st_mode)
                or (int(listed.st_dev), int(listed.st_ino)) != identity
            ):
                raise ValidationError(f"{label} changed while it was opened")
            yield descriptor
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or (int(current.st_dev), int(current.st_ino)) != identity
            ):
                raise ValidationError(f"{label} changed while its descriptor was active")
            self._validate_directory_path(parts[:-1], parent)
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError(
                f"could not access {label} beneath the pinned root: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)

    def remove_retained_empty_directory(self, relative: Path | str, *, label: str) -> None:
        """Remove only an empty directory retained by earlier transaction reads."""

        parts = _relative_parts(relative)
        retained = self._retained_directories.pop(parts, None)
        if retained is None:
            raise ValidationError(f"{label} was not retained by this pinned transaction")
        parent = -1
        try:
            self._validate_directory_path(parts, retained.descriptor)
            parent, name = self._open_parent(parts)
            self._validate_directory_path(parts[:-1], parent)
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                stat.S_ISLNK(listed.st_mode)
                or not stat.S_ISDIR(listed.st_mode)
                or (int(listed.st_dev), int(listed.st_ino)) != retained.identity
            ):
                raise ConflictError(f"{label} changed and was preserved")
            os.rmdir(name, dir_fd=parent)
            os.fsync(parent)
            self._validate_directory_path(parts[:-1], parent)
        finally:
            if parent >= 0:
                os.close(parent)
            os.close(retained.descriptor)

    def _retain_regular_file(
        self,
        parts: tuple[str, ...],
        parent: int,
        descriptor: int,
        opened: RegularFileSnapshot,
    ) -> None:
        watch: _RegularFileWatch | None = None
        directory_binding: _DirectoryBinding | None = None
        if parts not in self._retained_files:
            watch = self._duplicate_regular_file_watch(
                parts,
                parent,
                descriptor,
                identity=(opened.device, opened.inode),
            )
        parent_parts = parts[:-1]
        try:
            if parent_parts not in self._retained_directories:
                metadata = os.fstat(parent)
                directory_binding = _DirectoryBinding(
                    parts=parent_parts,
                    descriptor=os.dup(parent),
                    identity=(int(metadata.st_dev), int(metadata.st_ino)),
                )
        except Exception:
            self._close_retained_file(watch)
            raise
        if watch is not None:
            self._retained_files[parts] = watch
        if directory_binding is not None:
            self._retained_directories[parent_parts] = directory_binding

    def _retain_missing_parent(self, parts: tuple[str, ...], parent: int) -> None:
        if parts in self._retained_directories:
            self._validate_directory_path(parts, self._retained_directories[parts].descriptor)
            return
        metadata = os.fstat(parent)
        self._retained_directories[parts] = _DirectoryBinding(
            parts=parts,
            descriptor=os.dup(parent),
            identity=(int(metadata.st_dev), int(metadata.st_ino)),
        )

    def _take_retained_file(
        self,
        parts: tuple[str, ...],
        *,
        label: str,
        validate_canonical: bool = True,
    ) -> _RegularFileWatch | None:
        retained = self._retained_files.pop(parts, None)
        if retained is not None:
            try:
                opened = os.fstat(retained.descriptor)
                listed = os.stat(
                    retained.parts[-1],
                    dir_fd=retained.parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not stat.S_ISREG(listed.st_mode)
                    or (int(opened.st_dev), int(opened.st_ino)) != retained.identity
                    or (int(listed.st_dev), int(listed.st_ino)) != retained.identity
                ):
                    raise ValidationError(f"{label} changed identity while it was retained")
                if validate_canonical:
                    self._validate_directory_path(
                        retained.parts[:-1],
                        retained.parent_descriptor,
                    )
            except Exception:
                with suppress(OSError):
                    os.close(retained.descriptor)
                with suppress(OSError):
                    os.close(retained.parent_descriptor)
                raise
        return retained

    def _retain_named_regular_file(
        self,
        parts: tuple[str, ...],
        parent: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        label: str,
    ) -> None:
        flags = os.O_RDONLY | _POSIX_OS.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            identity = (int(opened.st_dev), int(opened.st_ino))
            if not stat.S_ISREG(opened.st_mode) or identity != expected_identity:
                raise DurablePublishError(
                    f"{label} changed before its committed identity could be retained",
                    outcome=PublishOutcome.UNKNOWN,
                )
            self._validate_directory_path(parts[:-1], parent)
            self._retain_missing_parent(parts[:-1], parent)
            replacement = self._duplicate_regular_file_watch(
                parts,
                parent,
                descriptor,
                identity=identity,
            )
            previous = self._retained_files.get(parts)
            self._retained_files[parts] = replacement
            self._close_retained_file(previous)
        finally:
            os.close(descriptor)

    @staticmethod
    def _duplicate_regular_file_watch(
        parts: tuple[str, ...],
        parent: int,
        descriptor: int,
        *,
        identity: tuple[int, int],
    ) -> _RegularFileWatch:
        parent_duplicate = -1
        descriptor_duplicate = -1
        try:
            parent_duplicate = os.dup(parent)
            descriptor_duplicate = os.dup(descriptor)
            watch = _RegularFileWatch(
                parts=parts,
                parent_descriptor=parent_duplicate,
                descriptor=descriptor_duplicate,
                identity=identity,
            )
            parent_duplicate = -1
            descriptor_duplicate = -1
            return watch
        finally:
            if descriptor_duplicate >= 0:
                owned = descriptor_duplicate
                descriptor_duplicate = -1
                with suppress(OSError):
                    os.close(owned)
            if parent_duplicate >= 0:
                owned = parent_duplicate
                parent_duplicate = -1
                with suppress(OSError):
                    os.close(owned)

    @staticmethod
    def _close_retained_file(watch: _RegularFileWatch | None) -> None:
        if watch is None:
            return
        with suppress(OSError):
            os.close(watch.descriptor)
        with suppress(OSError):
            os.close(watch.parent_descriptor)

    def atomic_write(
        self,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        parts = _relative_parts(relative)
        parent_parts = parts[:-1]
        self._validate_root_identity()
        parent, name = self._open_parent(parts)
        parent_metadata = os.fstat(parent)
        parent_identity = (int(parent_metadata.st_dev), int(parent_metadata.st_ino))
        descriptor = -1
        published_identity: tuple[int, int] | None = None
        temp_name = f".{name}.tmp-{secrets.token_hex(16)}"
        try:
            self._validate_directory_path(parent_parts, parent)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _POSIX_OS.O_NOFOLLOW
            descriptor = os.open(temp_name, flags, mode, dir_fd=parent)
            _write_all(descriptor, content)
            os.fsync(descriptor)
            published = os.fstat(descriptor)
            published_identity = (int(published.st_dev), int(published.st_ino))
            os.close(descriptor)
            descriptor = -1
            os.rename(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            assert published_identity is not None
            try:
                os.fsync(parent)
            except OSError as exc:
                # A visible inode is not necessarily visible at the canonical
                # path when a descendant directory or the root was renamed.
                # Prove the exact path before calling the publication committed.
                self._verify_published_path(
                    parts,
                    expected_content=content,
                    parent_identity=parent_identity,
                    published_identity=published_identity,
                )
                raise DurablePublishError(
                    f"replacement is visible beneath {self.root}, but directory durability "
                    "is unconfirmed",
                    outcome=PublishOutcome.COMMITTED,
                ) from exc
            self._verify_published_path(
                parts,
                expected_content=content,
                parent_identity=parent_identity,
                published_identity=published_identity,
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=parent)
            os.close(parent)

    def append_durable(self, relative: Path | str, content: bytes, *, label: str) -> None:
        """Append through pinned ancestry or report restored, committed, or unknown state."""

        parts = _relative_parts(relative)
        parent_parts = parts[:-1]
        descriptor = -1
        parent = -1
        original_size: int | None = None
        identity: tuple[int, int] | None = None
        committed = False
        try:
            self._validate_root_identity()
            watch = self._file_watches.get(parts)
            if watch is not None:
                self._validate_regular_file_watch(watch, label=label)
                parent = os.dup(watch.parent_descriptor)
                descriptor = os.dup(watch.descriptor)
                name = parts[-1]
            else:
                parent, name = self._open_parent(parts)
                self._validate_directory_path(parent_parts, parent)
                listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
                    raise ValidationError(f"{label} must be a regular file, not a link")
                flags = os.O_RDWR | os.O_APPEND | _POSIX_OS.O_NOFOLLOW
                descriptor = os.open(name, flags, dir_fd=parent)
            opened = os.fstat(descriptor)
            identity = (int(opened.st_dev), int(opened.st_ino))
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (int(current.st_dev), int(current.st_ino)) != identity
            ):
                raise ValidationError(f"{label} changed while it was opened")
            original_size = int(opened.st_size)
            _write_all(descriptor, content)
            os.fsync(descriptor)
            finished = os.fstat(descriptor)
            if int(finished.st_size) != original_size + len(content):
                raise OSError(f"{label} changed while it was appended")
            appended = _POSIX_OS.pread(descriptor, len(content), original_size)
            if appended != content:
                raise OSError(f"{label} append bytes changed before verification")
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or (int(current.st_dev), int(current.st_ino)) != identity
            ):
                raise OSError(f"{label} changed before append completion")
            self._validate_directory_path(parent_parts, parent)
            committed = True
        except DurableAppendError:
            raise
        except Exception as exc:
            if descriptor >= 0 and original_size is not None and identity is not None:
                try:
                    _restore_exact_append(
                        descriptor,
                        original_size=original_size,
                        content=content,
                        identity=identity,
                    )
                except Exception as rollback_exc:
                    raise DurableAppendError(
                        f"{label} append failed and exact rollback is unknown",
                        outcome=AppendOutcome.UNKNOWN,
                    ) from rollback_exc
            raise DurableAppendError(
                f"{label} append failed and the previous bytes were restored",
                outcome=AppendOutcome.RESTORED,
            ) from exc
        finally:
            if not committed:
                if descriptor >= 0:
                    with suppress(OSError):
                        os.close(descriptor)
                    descriptor = -1
                if parent >= 0:
                    with suppress(OSError):
                        os.close(parent)
                    parent = -1

        try:
            closing_descriptor = descriptor
            descriptor = -1
            os.close(closing_descriptor)
            closing_parent = parent
            parent = -1
            os.close(closing_parent)
        except Exception as exc:
            raise DurableAppendError(
                f"{label} append was synchronized but descriptor cleanup failed",
                outcome=AppendOutcome.COMMITTED,
            ) from exc
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            if parent >= 0:
                with suppress(OSError):
                    os.close(parent)

    def compare_and_swap_regular_file(
        self,
        relative: Path | str,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int,
        mode: int = 0o600,
    ) -> None:
        """Replace one exact file state beneath an active directory binding.

        The binding is intentionally required: a byte comparison followed by a
        path-based reopen would otherwise allow a renamed descendant directory
        to redirect the replacement into a different logical store.
        """

        parts = _relative_parts(relative)
        binding = self._directory_binding
        if binding is None or parts[: len(binding.parts)] != binding.parts:
            raise ValidationError("compare-and-swap requires an active pinned-parent binding")
        try:
            current = self.read_regular_file(
                relative,
                label=label,
                max_bytes=max_bytes,
                missing_ok=True,
            )
            if current != expected:
                raise ConflictError(f"{label} changed before compare-and-swap publication")
            self._validate_directory_path(binding.parts, binding.descriptor)
            self.atomic_write(relative, replacement, mode=mode)
        except ConflictError:
            raise
        except DurablePublishError:
            raise
        except ValidationError as exc:
            raise DurablePublishError(
                f"{label} compare-and-swap lost its pinned canonical parent",
                outcome=PublishOutcome.UNKNOWN,
            ) from exc

    def replace_regular_file_if_exact(
        self,
        relative: Path | str,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int,
        mode: int = 0o600,
    ) -> None:
        """Replace one bound leaf without clobbering a concurrent foreign leaf.

        The current name is atomically moved to an unguessable quarantine under
        the already-open parent descriptor before its bytes are checked.  The
        replacement is then published with a kernel no-replace move.  All
        destructive operations therefore stay beneath the pinned parent even
        if its canonical path is concurrently replaced.
        """

        self._replace_regular_file_if_exact(
            relative,
            expected=expected,
            replacement=replacement,
            label=label,
            max_bytes=max_bytes,
            mode=mode,
            validate_canonical=True,
        )

    def exchange_regular_file_if_exact(
        self,
        relative: Path | str,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int,
        mode: int = 0o600,
    ) -> None:
        """Atomically exchange an existing exact leaf without a public absence window.

        Signal queues use this variant because a hard process death must leave a
        complete old-or-new JSONL file at the public name. Expected-absent
        publication retains the ordinary kernel no-replace path.
        """

        if expected is None:
            self.replace_regular_file_if_exact(
                relative,
                expected=None,
                replacement=replacement,
                label=label,
                max_bytes=max_bytes,
                mode=mode,
            )
            return
        if len(expected) > max_bytes or len(replacement) > max_bytes:
            raise ValidationError(f"{label} exceeds its size bound")
        parts, parent = self._bound_leaf(relative)
        name = parts[-1]
        retained: _RegularFileWatch | None = None
        stage_name = ""
        stage_identity: tuple[int, int] | None = None
        exchanged = False
        committed = False
        try:
            retained = self._take_retained_file(parts, label=label)
            current, current_identity = _read_regular_file_at(
                parent,
                name,
                label=label,
                max_bytes=max_bytes,
            )
            if current != expected or (
                retained is not None and current_identity != retained.identity
            ):
                raise ConflictError(f"{label} changed before atomic exchange and was preserved")
            stage_name, stage_identity = _stage_regular_file_at(
                parent,
                name=name,
                content=replacement,
                mode=mode,
            )
            self._validate_directory_path(parts[:-1], parent)
            _exchange_regular_files_at(parent, stage_name, name)
            exchanged = True
            os.fsync(parent)
            assert stage_identity is not None
            _verify_regular_file_at(
                parent,
                name,
                expected=replacement,
                identity=stage_identity,
                label=label,
                max_bytes=max_bytes,
            )
            displaced, displaced_identity = _read_regular_file_at(
                parent,
                stage_name,
                label=f"{label} displaced state",
                max_bytes=max_bytes,
            )
            if displaced != expected or displaced_identity != current_identity:
                raise ConflictError(
                    f"{label} changed during atomic exchange; the competing state was preserved"
                )
            self._validate_directory_path(parts[:-1], parent)
            self._retain_named_regular_file(
                parts,
                parent,
                name,
                expected_identity=stage_identity,
                label=label,
            )
            try:
                _unlink_private_if_identity_at(
                    parent,
                    stage_name,
                    displaced_identity,
                    label=f"{label} displaced state",
                )
            except Exception as exc:
                committed = True
                raise DurablePublishError(
                    f"{label} is committed but displaced-state cleanup is unconfirmed",
                    outcome=PublishOutcome.COMMITTED,
                ) from exc
            stage_name = ""
            exchanged = False
            committed = True
        except Exception as exc:
            if committed or (
                isinstance(exc, DurablePublishError) and exc.outcome is PublishOutcome.COMMITTED
            ):
                raise
            if exchanged:
                assert stage_identity is not None and stage_name
                try:
                    _rollback_regular_file_exchange_at(
                        parent,
                        name=name,
                        stage_name=stage_name,
                        published_identity=stage_identity,
                        label=label,
                    )
                    exchanged = False
                except Exception as rollback_exc:
                    raise DurablePublishError(
                        f"{label} atomic exchange failed and rollback is unknown",
                        outcome=PublishOutcome.UNKNOWN,
                    ) from rollback_exc
            if isinstance(exc, ConflictError):
                raise
            if isinstance(exc, DurablePublishError):
                raise
            if isinstance(exc, ValidationError):
                raise DurablePublishError(
                    f"{label} atomic exchange lost its pinned canonical parent",
                    outcome=PublishOutcome.UNPUBLISHED,
                ) from exc
            raise DurablePublishError(
                f"{label} atomic exchange failed; the exact prior state was restored",
                outcome=PublishOutcome.UNPUBLISHED,
            ) from exc
        finally:
            primary_error = sys.exc_info()[1]
            cleanup_error: Exception | None = None
            if stage_name and not exchanged and stage_identity is not None:
                try:
                    _unlink_private_if_identity_at(
                        parent,
                        stage_name,
                        stage_identity,
                        label=f"{label} exchange stage",
                        missing_ok=True,
                    )
                except Exception as exc:
                    cleanup_error = exc
            self._close_retained_file(retained)
            owned_parent = parent
            parent = -1
            try:
                os.close(owned_parent)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if primary_error is None and cleanup_error is not None:
                raise DurablePublishError(
                    f"{label} is committed but descriptor cleanup is unconfirmed",
                    outcome=(PublishOutcome.COMMITTED if committed else PublishOutcome.UNKNOWN),
                ) from cleanup_error

    def rollback_regular_file_if_exact(
        self,
        relative: Path | str,
        *,
        expected: bytes,
        replacement: bytes | None,
        label: str,
        max_bytes: int,
        mode: int = 0o600,
    ) -> None:
        """Restore exact prior bytes through an already-held transaction binding.

        This deliberately does not reopen or require the canonical parent path:
        it is only for rollback after a later transaction step failed. Exact
        bytes and the active binding keep a substituted foreign tree unreachable.
        """

        if replacement is None:
            self._unlink_regular_file_if_exact(
                relative,
                expected=expected,
                label=label,
                max_bytes=max_bytes,
                validate_canonical=False,
            )
            return
        self._replace_regular_file_if_exact(
            relative,
            expected=expected,
            replacement=replacement,
            label=label,
            max_bytes=max_bytes,
            mode=mode,
            validate_canonical=False,
        )

    def _replace_regular_file_if_exact(
        self,
        relative: Path | str,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int,
        mode: int,
        validate_canonical: bool,
    ) -> None:
        """Implement exact replacement with optional detached-binding rollback mode."""

        if len(replacement) > max_bytes or (expected is not None and len(expected) > max_bytes):
            raise ValidationError(f"{label} exceeds its size bound")
        parts, parent = self._bound_leaf(relative, validate_canonical=validate_canonical)
        retained: _RegularFileWatch | None = None
        name = parts[-1]
        stage_name = ""
        stage_identity: tuple[int, int] | None = None
        quarantine_name: str | None = None
        quarantine_identity: tuple[int, int] | None = None
        published = False
        try:
            retained = self._take_retained_file(
                parts,
                label=label,
                validate_canonical=validate_canonical,
            )
            stage_name, stage_identity = _stage_regular_file_at(
                parent,
                name=name,
                content=replacement,
                mode=mode,
            )
            if expected is not None:
                quarantine_name = f".{name}.seld-quarantine-{secrets.token_hex(16)}"
                try:
                    _move_no_replace_at(parent, name, parent, quarantine_name)
                except FileNotFoundError as exc:
                    raise ConflictError(f"{label} changed before quarantine") from exc
                try:
                    quarantined, quarantine_identity = _read_regular_file_at(
                        parent,
                        quarantine_name,
                        label=label,
                        max_bytes=max_bytes,
                    )
                except (OSError, ValidationError) as exc:
                    _restore_quarantine_at(parent, quarantine_name, name, label=label)
                    quarantine_name = None
                    raise ConflictError(
                        f"{label} changed before quarantine and was preserved"
                    ) from exc
                if quarantined != expected or (
                    retained is not None and quarantine_identity != retained.identity
                ):
                    _restore_quarantine_at(parent, quarantine_name, name, label=label)
                    quarantine_name = None
                    raise ConflictError(
                        f"{label} changed identity before quarantine and was preserved"
                    )
            try:
                _move_no_replace_at(parent, stage_name, parent, name)
                stage_name = ""
                published = True
            except OSError as exc:
                assert stage_identity is not None
                try:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    source_missing = False
                    try:
                        os.stat(stage_name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        source_missing = True
                    if (
                        stat.S_ISREG(current.st_mode)
                        and (int(current.st_dev), int(current.st_ino)) == stage_identity
                        and source_missing
                    ):
                        stage_name = ""
                        published = True
                except OSError:
                    pass
                if quarantine_name is not None:
                    if published:
                        raise
                    _restore_quarantine_at(parent, quarantine_name, name, label=label)
                    quarantine_name = None
                if isinstance(exc, FileExistsError):
                    raise ConflictError(f"{label} changed before no-clobber publication") from exc
                raise
            assert stage_identity is not None
            _verify_regular_file_at(
                parent,
                name,
                expected=replacement,
                identity=stage_identity,
                label=label,
                max_bytes=max_bytes,
            )
            if validate_canonical:
                try:
                    self._validate_directory_path(parts[:-1], parent)
                    self._retain_named_regular_file(
                        parts,
                        parent,
                        name,
                        expected_identity=stage_identity,
                        label=label,
                    )
                except ValidationError as exc:
                    try:
                        _rollback_regular_file_replacement_at(
                            parent,
                            name=name,
                            published_identity=stage_identity,
                            quarantine_name=quarantine_name,
                            quarantine_identity=quarantine_identity,
                            expected=expected,
                            label=label,
                            max_bytes=max_bytes,
                        )
                    except Exception as rollback_exc:
                        raise DurablePublishError(
                            f"{label} parent changed and exact publication rollback is unknown",
                            outcome=PublishOutcome.UNKNOWN,
                        ) from rollback_exc
                    published = False
                    quarantine_name = None
                    raise DurablePublishError(
                        f"{label} parent changed; the exact prior state was restored",
                        outcome=PublishOutcome.UNPUBLISHED,
                    ) from exc
            if quarantine_name is not None:
                assert quarantine_identity is not None
                try:
                    _unlink_private_if_identity_at(
                        parent,
                        quarantine_name,
                        quarantine_identity,
                        label=f"{label} quarantine",
                    )
                except Exception as exc:
                    raise DurablePublishError(
                        f"{label} is committed but exact quarantine cleanup is unconfirmed",
                        outcome=PublishOutcome.COMMITTED,
                    ) from exc
                quarantine_name = None
        except Exception as exc:
            if isinstance(exc, DurablePublishError) and exc.outcome is PublishOutcome.COMMITTED:
                raise
            if isinstance(exc, ConflictError) and not published:
                # A foreign public leaf can prevent quarantine restoration.
                # Both identities remain preserved; retrying the same restore
                # would only obscure the caller-visible CAS conflict.
                raise
            try:
                if published:
                    assert stage_identity is not None
                    _rollback_regular_file_replacement_at(
                        parent,
                        name=name,
                        published_identity=stage_identity,
                        quarantine_name=quarantine_name,
                        quarantine_identity=quarantine_identity,
                        expected=expected,
                        label=label,
                        max_bytes=max_bytes,
                    )
                    published = False
                    quarantine_name = None
                elif quarantine_name is not None:
                    _restore_quarantine_at(parent, quarantine_name, name, label=label)
                    quarantine_name = None
            except Exception as rollback_exc:
                raise DurablePublishError(
                    f"{label} failed and exact prior-state restoration is unknown",
                    outcome=PublishOutcome.UNKNOWN,
                ) from rollback_exc
            if isinstance(exc, (ConflictError, DurablePublishError, ValidationError)):
                raise
            raise DurablePublishError(
                f"{label} failed before commit; the exact prior state was restored",
                outcome=PublishOutcome.UNPUBLISHED,
            ) from exc
        finally:
            primary_error = sys.exc_info()[1]
            cleanup_error: Exception | None = None
            if stage_name:
                assert stage_identity is not None
                try:
                    _unlink_private_if_identity_at(
                        parent,
                        stage_name,
                        stage_identity,
                        label=f"{label} stage",
                        missing_ok=True,
                    )
                except Exception as exc:
                    cleanup_error = exc
            self._close_retained_file(retained)
            owned_parent = parent
            parent = -1
            try:
                os.close(owned_parent)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if primary_error is None and cleanup_error is not None:
                raise DurablePublishError(
                    f"{label} descriptor cleanup failed after exact publication",
                    outcome=(PublishOutcome.COMMITTED if published else PublishOutcome.UNKNOWN),
                ) from cleanup_error

    def unlink_regular_file_if_exact(
        self,
        relative: Path | str,
        *,
        expected: bytes,
        label: str,
        max_bytes: int,
    ) -> None:
        """Remove only the exact bound leaf by first moving it out of its public name."""

        self._unlink_regular_file_if_exact(
            relative,
            expected=expected,
            label=label,
            max_bytes=max_bytes,
            validate_canonical=True,
        )

    def unlink_private_regular_file_if_exact(
        self,
        relative: Path | str,
        *,
        expected: bytes,
        label: str,
        max_bytes: int,
    ) -> None:
        """Directly remove one exact private transaction artifact.

        Private stages and markers are safe in either old-or-absent state after
        a hard stop.  Avoiding a public-name quarantine keeps their cleanup
        idempotent across process death.
        """

        if len(expected) > max_bytes:
            raise ValidationError(f"{label} exceeds its size bound")
        parts, parent = self._bound_leaf(relative)
        retained: _RegularFileWatch | None = None
        committed = False
        try:
            retained = self._take_retained_file(parts, label=label)
            try:
                content, identity = _read_regular_file_at(
                    parent,
                    parts[-1],
                    label=label,
                    max_bytes=max_bytes,
                )
            except (OSError, ValidationError) as exc:
                raise ConflictError(f"{label} changed before removal and was preserved") from exc
            if content != expected or (retained is not None and identity != retained.identity):
                raise ConflictError(f"{label} changed before removal and was preserved")
            self._validate_directory_path(parts[:-1], parent)
            try:
                _unlink_private_if_identity_at(
                    parent,
                    parts[-1],
                    identity,
                    label=label,
                )
                committed = True
            except ConflictError:
                raise
            except OSError as exc:
                try:
                    os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    committed = True
                    raise DurablePublishError(
                        f"{label} removal is visible but directory durability is unconfirmed",
                        outcome=PublishOutcome.COMMITTED,
                    ) from exc
                raise DurablePublishError(
                    f"{label} removal has an unknown durable outcome",
                    outcome=PublishOutcome.UNKNOWN,
                ) from exc
        finally:
            primary_error = sys.exc_info()[1]
            self._close_retained_file(retained)
            try:
                os.close(parent)
            except OSError as exc:
                if primary_error is None:
                    raise DurablePublishError(
                        f"{label} removal committed but descriptor cleanup is unconfirmed",
                        outcome=(PublishOutcome.COMMITTED if committed else PublishOutcome.UNKNOWN),
                    ) from exc

    def _unlink_regular_file_if_exact(
        self,
        relative: Path | str,
        *,
        expected: bytes,
        label: str,
        max_bytes: int,
        validate_canonical: bool,
    ) -> None:
        """Implement exact removal with optional detached-binding rollback mode."""

        if len(expected) > max_bytes:
            raise ValidationError(f"{label} exceeds its size bound")
        parts, parent = self._bound_leaf(relative, validate_canonical=validate_canonical)
        retained: _RegularFileWatch | None = None
        name = parts[-1]
        quarantine_name = f".{name}.seld-quarantine-{secrets.token_hex(16)}"
        committed = False
        try:
            retained = self._take_retained_file(
                parts,
                label=label,
                validate_canonical=validate_canonical,
            )
            try:
                _move_no_replace_at(parent, name, parent, quarantine_name)
            except FileNotFoundError as exc:
                raise ConflictError(f"{label} changed before removal and was preserved") from exc
            try:
                quarantined, identity = _read_regular_file_at(
                    parent,
                    quarantine_name,
                    label=label,
                    max_bytes=max_bytes,
                )
            except (OSError, ValidationError) as exc:
                _restore_quarantine_at(parent, quarantine_name, name, label=label)
                quarantine_name = ""
                raise ConflictError(f"{label} changed before removal and was preserved") from exc
            if quarantined != expected or (retained is not None and identity != retained.identity):
                _restore_quarantine_at(parent, quarantine_name, name, label=label)
                quarantine_name = ""
                raise ConflictError(f"{label} changed before removal and was preserved")
            if validate_canonical:
                try:
                    self._validate_directory_path(parts[:-1], parent)
                except ValidationError as exc:
                    try:
                        _restore_quarantine_at(parent, quarantine_name, name, label=label)
                        _verify_regular_file_at(
                            parent,
                            name,
                            expected=expected,
                            identity=identity,
                            label=label,
                            max_bytes=max_bytes,
                        )
                    except Exception as rollback_exc:
                        raise DurablePublishError(
                            f"{label} parent changed and exact removal rollback is unknown",
                            outcome=PublishOutcome.UNKNOWN,
                        ) from rollback_exc
                    quarantine_name = ""
                    raise ValidationError(
                        f"{label} parent changed at its canonical path; the exact file was restored"
                    ) from exc
            _unlink_private_if_identity_at(
                parent,
                quarantine_name,
                identity,
                label=f"{label} quarantine",
            )
            quarantine_name = ""
            committed = True
        finally:
            primary_error = sys.exc_info()[1]
            self._close_retained_file(retained)
            cleanup_error: OSError | None = None
            owned_parent = parent
            parent = -1
            try:
                os.close(owned_parent)
            except OSError as exc:
                cleanup_error = exc
            if primary_error is None and cleanup_error is not None:
                raise DurablePublishError(
                    f"{label} removal committed but descriptor cleanup is unconfirmed",
                    outcome=(PublishOutcome.COMMITTED if committed else PublishOutcome.UNKNOWN),
                ) from cleanup_error

    def _bound_leaf(
        self,
        relative: Path | str,
        *,
        validate_canonical: bool = True,
    ) -> tuple[tuple[str, ...], int]:
        parts = _relative_parts(relative)
        binding = self._directory_binding
        if binding is None or parts[: len(binding.parts)] != binding.parts:
            raise ValidationError("exact leaf mutation requires a pinned ancestor binding")
        if validate_canonical:
            self._validate_directory_path(binding.parts, binding.descriptor)
        parent = self._open_directory(parts[:-1])
        if validate_canonical:
            try:
                self._validate_directory_path(parts[:-1], parent)
            except Exception:
                os.close(parent)
                raise
        return parts, parent

    def publish_directory_no_replace_if_exact(
        self,
        source: Path | str,
        target: Path | str,
        *,
        expected_identity: tuple[int, int],
        label: str,
    ) -> None:
        """Move one exact staged directory to an absent sibling under a pinned parent."""

        source_parts = _relative_parts(source)
        target_parts = _relative_parts(target)
        binding = self._directory_binding
        if (
            binding is None
            or source_parts[:-1] != binding.parts
            or target_parts[:-1] != binding.parts
            or source_parts[-1] == target_parts[-1]
        ):
            raise ValidationError(
                "directory publication requires two leaves under one pinned-parent binding"
            )
        self._validate_directory_path(binding.parts, binding.descriptor)
        parent = os.dup(binding.descriptor)
        source_name = source_parts[-1]
        target_name = target_parts[-1]
        moved = False
        try:
            metadata = os.stat(source_name, dir_fd=parent, follow_symlinks=False)
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or identity != expected_identity
            ):
                raise ConflictError(f"{label} changed before publication and was preserved")
            try:
                _move_no_replace_at(parent, source_name, parent, target_name)
            except FileExistsError as exc:
                raise ConflictError(f"{label} target appeared and was preserved") from exc
            moved = True
            published = os.stat(target_name, dir_fd=parent, follow_symlinks=False)
            if (
                stat.S_ISLNK(published.st_mode)
                or not stat.S_ISDIR(published.st_mode)
                or (int(published.st_dev), int(published.st_ino)) != expected_identity
            ):
                raise DurablePublishError(
                    f"{label} publication changed before verification",
                    outcome=PublishOutcome.UNKNOWN,
                )
            try:
                self._validate_directory_path(binding.parts, parent)
            except ValidationError as exc:
                _rollback_directory_publication_at(
                    parent,
                    source_name=source_name,
                    target_name=target_name,
                    expected_identity=expected_identity,
                    label=label,
                )
                moved = False
                raise DurablePublishError(
                    f"{label} parent changed; the exact stage was restored",
                    outcome=PublishOutcome.UNPUBLISHED,
                ) from exc
        except Exception:
            if moved:
                try:
                    _rollback_directory_publication_at(
                        parent,
                        source_name=source_name,
                        target_name=target_name,
                        expected_identity=expected_identity,
                        label=label,
                    )
                except Exception as rollback_exc:
                    raise DurablePublishError(
                        f"{label} publication failed and exact stage rollback is unknown",
                        outcome=PublishOutcome.UNKNOWN,
                    ) from rollback_exc
            raise
        finally:
            os.close(parent)

    @contextmanager
    def bind_directory(
        self,
        relative: Path | str,
        *,
        create: bool = False,
    ) -> Iterator[tuple[int, int]]:
        """Keep one descendant directory identity for a multi-file transaction."""

        candidate = Path(relative)
        parts = (
            ()
            if not candidate.is_absolute() and candidate == Path(".")
            else _relative_parts(relative)
        )
        active = self._directory_binding
        if active is not None:
            if active.parts != parts:
                raise ValidationError("only one pinned directory binding may be active")
            self._validate_directory_path(parts, active.descriptor)
            active.depth += 1
            try:
                yield active.identity
            finally:
                active.depth -= 1
            return

        descriptor = self._open_directory(parts, create=create)
        metadata = os.fstat(descriptor)
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        binding = _DirectoryBinding(parts=parts, descriptor=descriptor, identity=identity)
        self._directory_binding = binding
        try:
            self._validate_directory_path(parts, descriptor)
            try:
                yield identity
            except BaseException:
                raise
            else:
                self._validate_directory_path(parts, descriptor)
        finally:
            self._directory_binding = None
            os.close(descriptor)

    @contextmanager
    def watch_directory(
        self,
        relative: Path | str,
        *,
        create: bool = False,
    ) -> Iterator[tuple[int, int]]:
        """Keep an additional directory identity for one multi-tree transaction."""

        candidate = Path(relative)
        parts = (
            ()
            if not candidate.is_absolute() and candidate == Path(".")
            else _relative_parts(relative)
        )
        active = self._directory_watches.get(parts)
        if active is not None:
            self._validate_directory_path(parts, active.descriptor)
            active.depth += 1
            try:
                yield active.identity
            finally:
                active.depth -= 1
            return
        if self._directory_binding is not None and self._directory_binding.parts == parts:
            raise ValidationError("directory is already the active mutation binding")

        descriptor = self._open_directory(parts, create=create)
        metadata = os.fstat(descriptor)
        identity = (int(metadata.st_dev), int(metadata.st_ino))
        binding = _DirectoryBinding(parts=parts, descriptor=descriptor, identity=identity)
        self._directory_watches[parts] = binding
        try:
            self._validate_directory_path(parts, descriptor)
            try:
                yield identity
            except BaseException:
                raise
            else:
                self._validate_directory_path(parts, descriptor)
        finally:
            del self._directory_watches[parts]
            os.close(descriptor)

    @contextmanager
    def watch_regular_file(
        self,
        relative: Path | str,
        *,
        label: str,
    ) -> Iterator[tuple[int, int]]:
        """Pin one existing regular-file identity for a later transaction step."""

        parts = _relative_parts(relative)
        active = self._file_watches.get(parts)
        if active is not None:
            self._validate_regular_file_watch(active, label=label)
            active.depth += 1
            try:
                yield active.identity
            finally:
                active.depth -= 1
            return

        parent, name = self._open_parent(parts)
        descriptor = -1
        try:
            self._validate_directory_path(parts[:-1], parent)
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
                raise ValidationError(f"{label} must be a regular file, not a link")
            flags = os.O_RDWR | os.O_APPEND | _POSIX_OS.O_NOFOLLOW
            descriptor = os.open(name, flags, dir_fd=parent)
            opened = os.fstat(descriptor)
            identity = (int(opened.st_dev), int(opened.st_ino))
            if (
                not stat.S_ISREG(opened.st_mode)
                or (int(listed.st_dev), int(listed.st_ino)) != identity
            ):
                raise ValidationError(f"{label} changed while it was pinned")
            watch = _RegularFileWatch(
                parts=parts,
                parent_descriptor=parent,
                descriptor=descriptor,
                identity=identity,
            )
            self._file_watches[parts] = watch
            try:
                self._validate_regular_file_watch(watch, label=label)
                try:
                    yield identity
                except BaseException:
                    raise
                else:
                    self._validate_regular_file_watch(watch, label=label)
            finally:
                del self._file_watches[parts]
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)

    def _validate_regular_file_watch(
        self,
        watch: _RegularFileWatch,
        *,
        label: str,
    ) -> None:
        try:
            opened = os.fstat(watch.descriptor)
            listed = os.stat(
                watch.parts[-1],
                dir_fd=watch.parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValidationError(f"{label} changed identity while it was pinned") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(listed.st_mode)
            or (int(opened.st_dev), int(opened.st_ino)) != watch.identity
            or (int(listed.st_dev), int(listed.st_ino)) != watch.identity
        ):
            raise ValidationError(f"{label} changed identity while it was pinned")
        self._validate_directory_path(watch.parts[:-1], watch.parent_descriptor)

    def validate_bound_directory(self) -> None:
        """Prove that the active transaction binding still has its canonical path."""

        binding = self._directory_binding
        if binding is None:
            raise ValidationError("pinned directory validation requires an active binding")
        self._validate_directory_path(binding.parts, binding.descriptor)

    def _verify_published_path(
        self,
        relative: Path | str | tuple[str, ...],
        *,
        expected_content: bytes,
        parent_identity: tuple[int, int],
        published_identity: tuple[int, int],
    ) -> None:
        """Reject success when the canonical path or bytes changed during publication."""

        canonical_parent = -1
        descriptor = -1
        final_parent = -1
        try:
            self._validate_root_identity()
            canonical_parent, name = self._open_parent_from_root(relative)
            metadata = os.fstat(canonical_parent)
            if (int(metadata.st_dev), int(metadata.st_ino)) != parent_identity:
                raise OSError("canonical publication parent changed")
            flags = os.O_RDONLY | _POSIX_OS.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(name, flags, dir_fd=canonical_parent)
            published = os.fstat(descriptor)
            opened_snapshot = _snapshot(published)
            if (
                not stat.S_ISREG(published.st_mode)
                or (
                    int(published.st_dev),
                    int(published.st_ino),
                )
                != published_identity
            ):
                raise OSError("canonical publication target changed")
            chunks: list[bytes] = []
            remaining = len(expected_content) + 1
            while remaining:
                block = os.read(descriptor, min(64 * 1024, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            if b"".join(chunks) != expected_content:
                raise OSError("canonical publication content changed")
            finished = _snapshot(os.fstat(descriptor))
            current = os.stat(name, dir_fd=canonical_parent, follow_symlinks=False)
            if finished != opened_snapshot or _snapshot(current) != opened_snapshot:
                raise OSError("canonical publication target changed while verified")
            final_parent, final_name = self._open_parent_from_root(relative)
            final_parent_metadata = os.fstat(final_parent)
            final_target = os.stat(final_name, dir_fd=final_parent, follow_symlinks=False)
            if (
                int(final_parent_metadata.st_dev),
                int(final_parent_metadata.st_ino),
            ) != parent_identity or _snapshot(final_target) != opened_snapshot:
                raise OSError("canonical publication path changed after verification")
            self._validate_root_identity()
        except (OSError, ValidationError) as exc:
            raise DurablePublishError(
                f"replacement was written beneath {self.root}, but its canonical path changed "
                "during publication",
                outcome=PublishOutcome.UNKNOWN,
            ) from exc
        finally:
            if final_parent >= 0:
                os.close(final_parent)
            if descriptor >= 0:
                os.close(descriptor)
            if canonical_parent >= 0:
                os.close(canonical_parent)

    @contextmanager
    def exclusive_root_lock(self, *, timeout: float = 10.0) -> Iterator[None]:
        """Lock this already-pinned root without inventing a file-path identity."""

        self._validate_root_identity()
        descriptor = os.dup(self._root_descriptor)
        handle = _DescriptorLockTarget(descriptor, f"{self.root} (pinned control lock)")
        try:
            _acquire(handle, timeout=timeout)
            try:
                self._validate_root_identity()
                yield
                self._validate_root_identity()
            finally:
                _release(handle)
        finally:
            os.close(descriptor)

    @contextmanager
    def exclusive_file_lock(
        self,
        relative: Path | str,
        *,
        timeout: float = 10.0,
    ) -> Iterator[None]:
        """Lock one regular file without following a replaceable parent path."""

        parts = _relative_parts(relative)
        parent_parts = parts[:-1]
        display_path = self.root.joinpath(*parts)
        display_parent = self.root.joinpath(*parent_parts)
        self._validate_root_identity()
        try:
            parent, name = self._open_parent(parts)
        except (OSError, ValidationError) as exc:
            raise ValidationError(
                f"lock parent must be one stable real directory: {display_parent}: {exc}"
            ) from exc
        descriptor = -1
        try:
            try:
                self._validate_directory_path(parent_parts, parent)
                flags = os.O_RDWR | os.O_CREAT | _POSIX_OS.O_NOFOLLOW
                descriptor = os.open(name, flags, 0o600, dir_fd=parent)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValidationError("pinned lock target must be a regular file")
                lock_identity = (int(metadata.st_dev), int(metadata.st_ino))
                self._validate_file_lock_identity(
                    parent,
                    name,
                    descriptor,
                    expected_identity=lock_identity,
                )
                self._validate_directory_path(parent_parts, parent)
                if metadata.st_size == 0:
                    _write_all(descriptor, b"0")
                    os.fsync(descriptor)
                    os.fsync(parent)
                    self._validate_directory_path(parent_parts, parent)
            except ValidationError:
                raise
            except OSError as exc:
                raise ValidationError(
                    f"lock target must be one stable regular file: {display_path}: {exc}"
                ) from exc
            handle = _DescriptorLockTarget(
                descriptor,
                f"{self.root}/{Path(relative).as_posix()} (pinned file lock)",
            )
            try:
                _acquire(handle, timeout=timeout)
            except OSError as exc:
                raise ValidationError(f"could not lock pinned local storage: {exc}") from exc
            try:
                self._validate_root_identity()
                self._validate_directory_path(parent_parts, parent)
                self._validate_file_lock_identity(
                    parent,
                    name,
                    descriptor,
                    expected_identity=lock_identity,
                )
                try:
                    yield
                except BaseException:
                    raise
                else:
                    try:
                        self._validate_file_lock_identity(
                            parent,
                            name,
                            descriptor,
                            expected_identity=lock_identity,
                        )
                        self._validate_directory_path(parent_parts, parent)
                    except (OSError, ValidationError) as exc:
                        raise _PinnedLockTeardownError(str(exc)) from exc
            finally:
                primary_error = sys.exc_info()[1]
                try:
                    _release(handle)
                except OSError as exc:
                    if primary_error is None:
                        raise _PinnedLockTeardownError(
                            "pinned lock release failed after the protected operation"
                        ) from exc
        finally:
            primary_error = sys.exc_info()[1]
            cleanup_error: OSError | None = None
            if descriptor >= 0:
                owned = descriptor
                descriptor = -1
                try:
                    os.close(owned)
                except OSError as exc:
                    cleanup_error = exc
            owned_parent = parent
            parent = -1
            try:
                os.close(owned_parent)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if primary_error is None and cleanup_error is not None:
                raise _PinnedLockTeardownError(
                    "pinned lock descriptor cleanup failed after the protected operation"
                ) from cleanup_error

    @staticmethod
    def _validate_file_lock_identity(
        parent: int,
        name: str,
        descriptor: int,
        *,
        expected_identity: tuple[int, int],
    ) -> None:
        """Prove the locked descriptor is still the named file in its pinned parent."""

        try:
            opened = os.fstat(descriptor)
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise ValidationError(
                "pinned lock target changed identity beneath the pinned root"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(listed.st_mode)
            or (int(opened.st_dev), int(opened.st_ino)) != expected_identity
            or (int(listed.st_dev), int(listed.st_ino)) != expected_identity
        ):
            raise ValidationError("pinned lock target changed identity beneath the pinned root")

    def _validate_directory_path(self, parts: tuple[str, ...], descriptor: int) -> None:
        """Prove one held directory is still reachable by its no-follow path."""

        self._validate_root_identity()
        canonical = -1
        try:
            canonical = self._open_directory_from_root(parts)
            held = os.fstat(descriptor)
            visible = os.fstat(canonical)
        except (OSError, ValidationError) as exc:
            raise ValidationError(
                "pinned local storage directory changed identity at its canonical path"
            ) from exc
        finally:
            if canonical >= 0:
                os.close(canonical)
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or (int(held.st_dev), int(held.st_ino)) != (int(visible.st_dev), int(visible.st_ino))
        ):
            raise ValidationError(
                "pinned local storage directory changed identity at its canonical path"
            )
        self._validate_root_identity()

    def _confirm_directory_absent(self, parts: tuple[str, ...]) -> None:
        """Re-walk one missing descendant before returning an absence result."""

        self._validate_root_identity()
        descriptor = -1
        try:
            descriptor = self._open_directory(parts)
        except FileNotFoundError:
            self._validate_root_identity()
            return
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        raise ValidationError("pinned storage path appeared while its absence was checked")

    def _confirm_leaf_absent(
        self,
        parent_parts: tuple[str, ...],
        parent: int,
        name: str,
        *,
        label: str,
    ) -> None:
        """Prove a missing leaf still has the same reachable parent and is absent."""

        self._validate_directory_path(parent_parts, parent)
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            self._validate_directory_path(parent_parts, parent)
            return
        except OSError as exc:
            raise ValidationError(f"could not confirm missing {label}: {exc}") from exc
        raise ValidationError(f"{label} appeared while its absence was checked")

    def _open_parent(
        self,
        relative: Path | str | tuple[str, ...],
        *,
        create_parent: bool = False,
    ) -> tuple[int, str]:
        parts = relative if isinstance(relative, tuple) else _relative_parts(relative)
        if len(parts) == 1:
            self._validate_root_identity()
            return os.dup(self._root_descriptor), parts[0]
        return (
            self._open_directory(parts[:-1], create=create_parent),
            parts[-1],
        )

    def _open_parent_from_root(
        self,
        relative: Path | str | tuple[str, ...],
    ) -> tuple[int, str]:
        parts = relative if isinstance(relative, tuple) else _relative_parts(relative)
        if len(parts) == 1:
            self._validate_root_identity()
            return os.dup(self._root_descriptor), parts[0]
        return self._open_directory_from_root(parts[:-1]), parts[-1]

    def _open_directory(
        self,
        parts: tuple[str, ...],
        *,
        create: bool = False,
        mode: int = 0o700,
    ) -> int:
        candidates = (
            *self._retained_directories.values(),
            *self._directory_watches.values(),
        )
        if self._directory_binding is not None:
            candidates = (*candidates, self._directory_binding)
        bindings = tuple(
            binding for binding in candidates if parts[: len(binding.parts)] == binding.parts
        )
        binding = max(bindings, key=lambda item: len(item.parts), default=None)
        if binding is not None:
            current = os.dup(binding.descriptor)
            remaining_parts = parts[len(binding.parts) :]
        else:
            current = os.dup(self._root_descriptor)
            remaining_parts = parts
        return self._open_directory_from_descriptor(
            current,
            remaining_parts,
            create=create,
            mode=mode,
        )

    def _open_directory_from_root(
        self,
        parts: tuple[str, ...],
        *,
        create: bool = False,
        mode: int = 0o700,
    ) -> int:
        return self._open_directory_from_descriptor(
            os.dup(self._root_descriptor),
            parts,
            create=create,
            mode=mode,
        )

    def _open_directory_from_descriptor(
        self,
        current: int,
        parts: tuple[str, ...],
        *,
        create: bool,
        mode: int,
    ) -> int:
        flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
        child = -1
        try:
            for part in parts:
                if create:
                    created = False
                    try:
                        os.mkdir(part, mode, dir_fd=current)
                        created = True
                    except FileExistsError:
                        pass
                    if created:
                        # Persist the new directory entry before relying on it for
                        # any canonical publication beneath this pinned parent.
                        os.fsync(current)
                child = os.open(part, flags, dir_fd=current)
                metadata = os.fstat(child)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValidationError("pinned storage component is not a directory")
                previous = current
                current = -1
                os.close(previous)
                current = child
                child = -1
            result = current
            current = -1
            return result
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise ValidationError(f"could not traverse pinned local storage: {exc}") from exc
        finally:
            if child >= 0:
                owned = child
                child = -1
                with suppress(OSError):
                    os.close(owned)
            if current >= 0:
                owned = current
                current = -1
                with suppress(OSError):
                    os.close(owned)


def _relative_parts(relative: Path | str) -> tuple[str, ...]:
    path = Path(relative)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("pinned storage path must be a safe relative path")
    return parts


@contextmanager
def open_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int | None = None,
) -> Iterator[tuple[int, RegularFileSnapshot]]:
    """Open one stable regular file without following links or special files."""

    try:
        listed = os.lstat(path)
    except OSError as exc:
        raise ValidationError(f"could not inspect {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(listed.st_mode):
        raise ValidationError(f"{label} cannot be a symbolic link: {path}")
    if not stat.S_ISREG(listed.st_mode):
        raise ValidationError(f"{label} must be a regular file: {path}")
    if max_bytes is not None and listed.st_size > max_bytes:
        raise ValidationError(f"{label} exceeds its size bound: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValidationError(f"could not open {label}: {path}: {exc}") from exc
        opened = os.fstat(descriptor)
        listed_snapshot = _snapshot(listed)
        opened_snapshot = _snapshot(opened)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(listed_snapshot, opened_snapshot):
            raise ValidationError(f"{label} changed while it was opened: {path}")
        yield descriptor, opened_snapshot
        finished = _snapshot(os.fstat(descriptor))
        current = os.lstat(path)
        if (
            not stat.S_ISREG(current.st_mode)
            or finished != opened_snapshot
            or not _same_file(_snapshot(current), opened_snapshot)
        ):
            raise ValidationError(f"{label} changed while it was read: {path}")
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError(f"could not read {label}: {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def read_regular_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    """Read a stable regular file through the canonical bounded binary path."""

    chunks: list[bytes] = []
    remaining = max_bytes + 1
    with open_regular_file(path, label=label, max_bytes=max_bytes) as (descriptor, _):
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise ValidationError(f"{label} exceeds its size bound: {path}")
    return content


def sha256_regular_file(path: Path, *, label: str, max_bytes: int | None = None) -> str:
    """Hash one stable regular file through the same no-follow open boundary."""

    digest = hashlib.sha256()
    total = 0
    with open_regular_file(path, label=label, max_bytes=max_bytes) as (descriptor, _):
        while block := os.read(descriptor, 1024 * 1024):
            total += len(block)
            if max_bytes is not None and total > max_bytes:
                raise ValidationError(f"{label} exceeds its size bound: {path}")
            digest.update(block)
    return digest.hexdigest()


def portable_relative(path: Path, root: Path) -> str:
    """Serialize an API/archive path with stable POSIX separators on every OS."""

    return path.relative_to(root).as_posix()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class _PinnedStoreTransaction:
    root: Path
    store: PinnedPathRoot
    mutation_committed: bool = False


_ACTIVE_PINNED_STORE: ContextVar[_PinnedStoreTransaction | None] = ContextVar(
    "continuity_kernel_active_pinned_store",
    default=None,
)


def active_pinned_path_root(root: Path) -> PinnedPathRoot | None:
    """Return the root-pinned transaction established by an outer vault lock."""

    active = _ACTIVE_PINNED_STORE.get()
    if active is None:
        return None
    expected = Path(os.path.abspath(root.expanduser()))
    return active.store if active.root == expected else None


def mark_active_pinned_transaction_committed(root: Path) -> None:
    """Record that the active vault mutation and its audit event are durable."""

    active = _ACTIVE_PINNED_STORE.get()
    expected = Path(os.path.abspath(root.expanduser()))
    if active is not None and active.root == expected:
        active.mutation_committed = True


def _vault_lock_target(path: Path) -> tuple[Path, Path] | None:
    lexical = Path(os.path.abspath(path.expanduser()))
    if lexical.parent.name != "locks" or lexical.parent.parent.name != ".gsv":
        return None
    root = lexical.parent.parent.parent
    return root, lexical.relative_to(root)


@contextmanager
def exclusive_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Acquire a bounded advisory lock on one stable, named regular file."""

    pinned_target = _vault_lock_target(path) if PINNED_PATH_ROOT_SUPPORTED else None
    if pinned_target is not None:
        root, relative = pinned_target
        active = _ACTIVE_PINNED_STORE.get()
        if active is not None and active.root == root:
            with active.store.exclusive_file_lock(relative, timeout=timeout):
                yield
            return
        store = PinnedPathRoot(root)
        transaction = _PinnedStoreTransaction(root=root, store=store)
        token = _ACTIVE_PINNED_STORE.set(transaction)
        operation_error: BaseException | None = None
        cleanup_error: Exception | None = None
        try:
            try:
                try:
                    if not store.directory_exists(".gsv/locks"):
                        store.ensure_directory(".gsv/locks")
                except (OSError, ValidationError) as exc:
                    raise ValidationError(
                        f"lock parent must be one stable real directory: "
                        f"{root / relative.parent}: {exc}"
                    ) from exc
                with store.exclusive_file_lock(relative, timeout=timeout):
                    yield
            except BaseException as exc:
                operation_error = exc
        finally:
            try:
                _ACTIVE_PINNED_STORE.reset(token)
            except Exception as exc:
                cleanup_error = exc
            try:
                store.close()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if operation_error is not None:
            if (
                transaction.mutation_committed
                and isinstance(operation_error, Exception)
                and isinstance(operation_error, _PinnedLockTeardownError)
            ):
                raise MutationCommittedError(
                    "vault mutation and audit event were committed, but lock teardown could "
                    "not confirm the pinned lock path; reload before retrying"
                ) from operation_error
            raise operation_error.with_traceback(operation_error.__traceback__)
        if cleanup_error is not None:
            if transaction.mutation_committed:
                raise MutationCommittedError(
                    "vault mutation and audit event were committed, but pinned transaction "
                    "cleanup failed; reload before retrying"
                ) from cleanup_error
            raise cleanup_error
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    parent_snapshot = _validate_lock_parent(path.parent)
    parent_descriptor = -1
    descriptor = -1
    try:
        if PINNED_PATH_ROOT_SUPPORTED:
            parent_flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
            try:
                parent_descriptor = os.open(path.parent, parent_flags)
            except OSError as exc:
                raise ValidationError(
                    f"lock parent must be one stable real directory: {path.parent}: {exc}"
                ) from exc
            opened_parent = os.fstat(parent_descriptor)
            if (
                not stat.S_ISDIR(opened_parent.st_mode)
                or (int(opened_parent.st_dev), int(opened_parent.st_ino)) != parent_snapshot
            ):
                raise ValidationError(
                    f"lock parent changed identity while it was opened: {path.parent}"
                )
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        for attempt in range(3):
            try:
                if parent_descriptor >= 0:
                    descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
                else:
                    descriptor = os.open(path, flags, 0o600)
                break
            except FileNotFoundError as exc:
                # macOS can report ENOENT to one of two simultaneous O_CREAT
                # opens of the same absent leaf. Retry only while the already-
                # pinned parent still has the exact canonical identity.
                if attempt == 2 or _validate_lock_parent(path.parent) != parent_snapshot:
                    raise ValidationError(
                        f"lock target must be one stable regular file: {path}: {exc}"
                    ) from exc
            except OSError as exc:
                raise ValidationError(
                    f"lock target must be one stable regular file: {path}: {exc}"
                ) from exc
        opened = _validate_named_lock_target(
            path,
            descriptor,
            parent_descriptor=parent_descriptor,
            parent_snapshot=parent_snapshot,
        )
        if opened.st_size == 0:
            _write_all(descriptor, b"0")
            _validate_named_lock_target(
                path,
                descriptor,
                parent_descriptor=parent_descriptor,
                parent_snapshot=parent_snapshot,
            )
        handle = _DescriptorLockTarget(descriptor, str(path))
        _acquire(handle, timeout=timeout)
        try:
            _validate_named_lock_target(
                path,
                descriptor,
                parent_descriptor=parent_descriptor,
                parent_snapshot=parent_snapshot,
            )
            yield
            _validate_named_lock_target(
                path,
                descriptor,
                parent_descriptor=parent_descriptor,
                parent_snapshot=parent_snapshot,
            )
        finally:
            _release(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _validate_lock_parent(path: Path) -> tuple[int, int]:
    """Reject a linked, reparse-point, or non-directory immediate lock parent."""

    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValidationError(f"could not inspect lock parent: {path}: {exc}") from exc
    file_attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(file_attributes & reparse_flag)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValidationError(f"lock parent must be one stable real directory: {path}")
    return int(metadata.st_dev), int(metadata.st_ino)


def _validate_named_lock_target(
    path: Path,
    descriptor: int,
    *,
    parent_descriptor: int,
    parent_snapshot: tuple[int, int],
) -> os.stat_result:
    """Reject links, reparse points, special files, and replaced lock inodes."""

    try:
        opened = os.fstat(descriptor)
        current_parent = _validate_lock_parent(path.parent)
        if parent_descriptor >= 0:
            held_parent = os.fstat(parent_descriptor)
            listed = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        else:
            held_parent = None
            listed = os.lstat(path)
    except OSError as exc:
        raise ValidationError(f"lock target changed identity: {path}: {exc}") from exc
    if current_parent != parent_snapshot or (
        held_parent is not None
        and (
            not stat.S_ISDIR(held_parent.st_mode)
            or (int(held_parent.st_dev), int(held_parent.st_ino)) != parent_snapshot
        )
    ):
        raise ValidationError(f"lock parent changed identity: {path.parent}")
    file_attributes = int(getattr(listed, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if (
        stat.S_ISLNK(listed.st_mode)
        or bool(file_attributes & reparse_flag)
        or not stat.S_ISREG(listed.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or not _same_file(_snapshot(listed), _snapshot(opened))
    ):
        raise ValidationError(f"lock target must be one stable regular file: {path}")
    return opened


def atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Durably replace one file without exposing a partially written version."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temp = Path(raw_temp)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, mode)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        durable_replace(temp, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temp.unlink()


def durable_replace(source: Path, target: Path) -> None:
    """Replace a path and persist the containing directory entry where supported."""

    os.replace(source, target)
    _fsync_directory(target.parent)


def durable_publish_new(source: Path, target: Path) -> tuple[int, int]:
    """Publish one staged regular file without replacing any existing path."""

    if source.parent.resolve() != target.parent.resolve():
        raise OSError(errno.EXDEV, "no-clobber publication requires one directory")
    source_metadata = os.lstat(source)
    if not stat.S_ISREG(source_metadata.st_mode):
        raise OSError(errno.EINVAL, "no-clobber publication source must be a regular file")
    identity = (source_metadata.st_dev, source_metadata.st_ino)
    try:
        os.link(source, target, follow_symlinks=False)
    except (NotImplementedError, OSError) as exc:
        error = exc.errno if isinstance(exc, OSError) else errno.ENOTSUP
        windows_error = getattr(exc, "winerror", None)
        fallback_errors = {
            errno.EXDEV,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EPERM,
            getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        }
        if error not in fallback_errors and windows_error not in {1, 50}:
            raise
        return _publish_new_by_move(source, target, identity=identity)
    try:
        _fsync_directory(target.parent)
    except OSError as exc:
        if not (
            _path_matches_identity(source, identity) and _path_matches_identity(target, identity)
        ):
            raise DurablePublishError(
                f"no-clobber publication has an unknown outcome for {target}; the linked "
                "source or target changed before directory durability was confirmed",
                outcome=PublishOutcome.UNKNOWN,
            ) from exc
        raise DurablePublishError(
            f"target is visible at {target}, but publication durability is unconfirmed; "
            f"the staged file remains at {source}",
            outcome=PublishOutcome.COMMITTED,
        ) from exc
    try:
        source.unlink()
    except OSError as exc:
        if not (
            _path_matches_identity(source, identity) and _path_matches_identity(target, identity)
        ):
            raise DurablePublishError(
                f"no-clobber publication has an unknown outcome for {target}; the linked "
                "source or target changed during staged-file cleanup",
                outcome=PublishOutcome.UNKNOWN,
            ) from exc
        raise DurablePublishError(
            f"target is committed at {target}, but the staged file remains at {source}: {exc}",
            outcome=PublishOutcome.COMMITTED,
        ) from exc
    try:
        _fsync_directory(target.parent)
    except OSError as exc:
        if not (_path_absent(source) and _path_matches_identity(target, identity)):
            raise DurablePublishError(
                f"no-clobber publication has an unknown outcome for {target}; the target "
                "changed while staged-file cleanup durability was confirmed",
                outcome=PublishOutcome.UNKNOWN,
            ) from exc
        raise DurablePublishError(
            f"target is committed at {target}, but staged-file cleanup durability is unconfirmed",
            outcome=PublishOutcome.COMMITTED,
        ) from exc
    if not (_path_absent(source) and _path_matches_identity(target, identity)):
        raise DurablePublishError(
            f"no-clobber publication has an unknown outcome for {target}; the target changed "
            "before publication completion was observed",
            outcome=PublishOutcome.UNKNOWN,
        )
    return identity


def _stage_regular_file_at(
    parent: int,
    *,
    name: str,
    content: bytes,
    mode: int,
) -> tuple[str, tuple[int, int]]:
    stage_name = f".{name}.seld-stage-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _POSIX_OS.O_NOFOLLOW
    descriptor = os.open(stage_name, flags, mode, dir_fd=parent)
    metadata = os.fstat(descriptor)
    identity = (int(metadata.st_dev), int(metadata.st_ino))
    try:
        if os.name != "nt":
            _POSIX_OS.fchmod(descriptor, mode)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        finished = os.fstat(descriptor)
        current = os.stat(stage_name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(finished.st_mode)
            or (int(finished.st_dev), int(finished.st_ino)) != identity
            or (int(current.st_dev), int(current.st_ino)) != identity
        ):
            raise OSError(f"staged file changed before publication: {stage_name}")
    except Exception:
        _unlink_private_if_identity_at(
            parent,
            stage_name,
            identity,
            label="staged file",
            missing_ok=True,
        )
        raise
    finally:
        os.close(descriptor)
    return stage_name, identity


def _read_regular_file_at(
    parent: int,
    name: str,
    *,
    label: str,
    max_bytes: int,
) -> tuple[bytes, tuple[int, int]]:
    listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise ValidationError(f"{label} must be a regular file, not a link")
    if listed.st_size > max_bytes:
        raise ValidationError(f"{label} exceeds its size bound")
    flags = os.O_RDONLY | _POSIX_OS.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        opened_snapshot = _snapshot(opened)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(_snapshot(listed), opened_snapshot):
            raise ValidationError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        content = b"".join(chunks)
        if len(content) > max_bytes:
            raise ValidationError(f"{label} exceeds its size bound")
        finished = _snapshot(os.fstat(descriptor))
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if finished != opened_snapshot or not _same_file(_snapshot(current), opened_snapshot):
            raise ValidationError(f"{label} changed while it was read")
        return content, (opened_snapshot.device, opened_snapshot.inode)
    finally:
        os.close(descriptor)


def _verify_regular_file_at(
    parent: int,
    name: str,
    *,
    expected: bytes,
    identity: tuple[int, int],
    label: str,
    max_bytes: int,
) -> None:
    content, current_identity = _read_regular_file_at(
        parent,
        name,
        label=label,
        max_bytes=max_bytes,
    )
    if content != expected or current_identity != identity:
        raise DurablePublishError(
            f"{label} publication changed before it could be verified",
            outcome=PublishOutcome.UNKNOWN,
        )


def _unlink_private_if_identity_at(
    parent: int,
    name: str,
    identity: tuple[int, int],
    *,
    label: str,
    missing_ok: bool = False,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if (int(current.st_dev), int(current.st_ino)) != identity:
        raise ConflictError(f"{label} changed and was preserved")
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)


def _restore_quarantine_at(parent: int, quarantine: str, name: str, *, label: str) -> None:
    try:
        _move_no_replace_at(parent, quarantine, parent, name)
    except FileExistsError as exc:
        raise ConflictError(
            f"{label} changed during the operation; both states were preserved"
        ) from exc


def _rollback_regular_file_replacement_at(
    parent: int,
    *,
    name: str,
    published_identity: tuple[int, int],
    quarantine_name: str | None,
    quarantine_identity: tuple[int, int] | None,
    expected: bytes | None,
    label: str,
    max_bytes: int,
) -> None:
    """Restore the exact pre-publication leaf through one held parent descriptor."""

    if quarantine_name is None:
        _unlink_private_if_identity_at(
            parent,
            name,
            published_identity,
            label=f"{label} unpublished replacement",
        )
        return

    assert quarantine_identity is not None and expected is not None
    rollback_name = f".{name}.seld-rollback-{secrets.token_hex(16)}"
    _move_no_replace_at(parent, name, parent, rollback_name)
    _, rollback_identity = _read_regular_file_at(
        parent,
        rollback_name,
        label=f"{label} unpublished replacement",
        max_bytes=max_bytes,
    )
    if rollback_identity != published_identity:
        raise DurablePublishError(
            f"{label} unpublished replacement changed during rollback",
            outcome=PublishOutcome.UNKNOWN,
        )
    _restore_quarantine_at(parent, quarantine_name, name, label=label)
    _verify_regular_file_at(
        parent,
        name,
        expected=expected,
        identity=quarantine_identity,
        label=label,
        max_bytes=max_bytes,
    )
    _unlink_private_if_identity_at(
        parent,
        rollback_name,
        published_identity,
        label=f"{label} unpublished replacement",
    )


def _rollback_directory_publication_at(
    parent: int,
    *,
    source_name: str,
    target_name: str,
    expected_identity: tuple[int, int],
    label: str,
) -> None:
    try:
        _move_no_replace_at(parent, target_name, parent, source_name)
    except OSError as exc:
        raise DurablePublishError(
            f"{label} exact stage and competing state were preserved, but rollback is unknown",
            outcome=PublishOutcome.UNKNOWN,
        ) from exc
    restored = os.stat(source_name, dir_fd=parent, follow_symlinks=False)
    if (
        stat.S_ISLNK(restored.st_mode)
        or not stat.S_ISDIR(restored.st_mode)
        or (int(restored.st_dev), int(restored.st_ino)) != expected_identity
    ):
        raise DurablePublishError(
            f"{label} exact stage identity changed during rollback",
            outcome=PublishOutcome.UNKNOWN,
        )


def _exchange_regular_files_at(parent: int, left_name: str, right_name: str) -> None:
    """Atomically exchange two names beneath one held directory descriptor."""

    if sys.platform == "darwin":
        library: Any = ctypes.CDLL(None, use_errno=True)
        rename_swap = library.renameatx_np
        rename_swap.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_swap.restype = ctypes.c_int
        result = rename_swap(
            ctypes.c_int(parent),
            ctypes.c_char_p(os.fsencode(left_name)),
            ctypes.c_int(parent),
            ctypes.c_char_p(os.fsencode(right_name)),
            ctypes.c_uint(0x00000002),
        )
        if result != 0:
            _raise_move_error(ctypes.get_errno(), Path(right_name))
        return
    if sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename_exchange = getattr(library, "renameat2", None)
        if rename_exchange is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable for atomic exchange")
        rename_exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exchange.restype = ctypes.c_int
        result = rename_exchange(
            ctypes.c_int(parent),
            ctypes.c_char_p(os.fsencode(left_name)),
            ctypes.c_int(parent),
            ctypes.c_char_p(os.fsencode(right_name)),
            ctypes.c_uint(0x00000002),
        )
        if result != 0:
            _raise_move_error(ctypes.get_errno(), Path(right_name))
        return
    raise OSError(errno.ENOTSUP, "dir-fd atomic exchange is unavailable")


def _rollback_regular_file_exchange_at(
    parent: int,
    *,
    name: str,
    stage_name: str,
    published_identity: tuple[int, int],
    label: str,
) -> None:
    published = os.stat(name, dir_fd=parent, follow_symlinks=False)
    displaced = os.stat(stage_name, dir_fd=parent, follow_symlinks=False)
    if (
        stat.S_ISLNK(published.st_mode)
        or not stat.S_ISREG(published.st_mode)
        or (int(published.st_dev), int(published.st_ino)) != published_identity
        or stat.S_ISLNK(displaced.st_mode)
        or not stat.S_ISREG(displaced.st_mode)
    ):
        raise DurablePublishError(
            f"{label} exchange identities changed before rollback",
            outcome=PublishOutcome.UNKNOWN,
        )
    displaced_identity = (int(displaced.st_dev), int(displaced.st_ino))
    _exchange_regular_files_at(parent, stage_name, name)
    os.fsync(parent)
    restored = os.stat(name, dir_fd=parent, follow_symlinks=False)
    staged = os.stat(stage_name, dir_fd=parent, follow_symlinks=False)
    if (int(restored.st_dev), int(restored.st_ino)) != displaced_identity or (
        int(staged.st_dev),
        int(staged.st_ino),
    ) != published_identity:
        raise DurablePublishError(
            f"{label} exact exchange rollback changed identity",
            outcome=PublishOutcome.UNKNOWN,
        )


def _move_no_replace_at(
    source_parent: int,
    source_name: str,
    target_parent: int,
    target_name: str,
) -> None:
    if sys.platform == "darwin":
        library: Any = ctypes.CDLL(None, use_errno=True)
        rename_exclusive = library.renameatx_np
        rename_exclusive.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            ctypes.c_int(source_parent),
            ctypes.c_char_p(os.fsencode(source_name)),
            ctypes.c_int(target_parent),
            ctypes.c_char_p(os.fsencode(target_name)),
            ctypes.c_uint(0x00000004),
        )
        if result != 0:
            _raise_move_error(ctypes.get_errno(), Path(target_name))
    elif sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename_no_replace = getattr(library, "renameat2", None)
        if rename_no_replace is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable for no-replace move")
        rename_no_replace.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_no_replace.restype = ctypes.c_int
        result = rename_no_replace(
            ctypes.c_int(source_parent),
            ctypes.c_char_p(os.fsencode(source_name)),
            ctypes.c_int(target_parent),
            ctypes.c_char_p(os.fsencode(target_name)),
            ctypes.c_uint(1),
        )
        if result != 0:
            _raise_move_error(ctypes.get_errno(), Path(target_name))
    else:
        raise OSError(errno.ENOTSUP, "dir-fd no-replace move is unavailable")
    os.fsync(source_parent)
    if target_parent != source_parent:
        os.fsync(target_parent)


def move_no_replace(source: Path, target: Path) -> None:
    """Atomically move one staged path without replacing an existing target."""

    if source.parent.resolve() != target.parent.resolve():
        raise OSError(errno.EXDEV, "no-replace move requires one directory")
    if sys.platform == "darwin":
        library: Any = ctypes.CDLL(None, use_errno=True)
        rename_exclusive = library.renamex_np
        rename_exclusive.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            ctypes.c_char_p(os.fsencode(source)),
            ctypes.c_char_p(os.fsencode(target)),
            ctypes.c_uint(0x00000004),
        )
        if result != 0:
            _raise_move_error(ctypes.get_errno(), target)
    elif sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename_no_replace = getattr(library, "renameat2", None)
        if rename_no_replace is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable for no-replace move")
        rename_no_replace.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_no_replace.restype = ctypes.c_int
        result = rename_no_replace(
            ctypes.c_int(-100),
            ctypes.c_char_p(os.fsencode(source)),
            ctypes.c_int(-100),
            ctypes.c_char_p(os.fsencode(target)),
            ctypes.c_uint(1),
        )
        if result != 0:
            _raise_move_error(ctypes.get_errno(), target)
    elif os.name == "nt":
        _move_windows_path_new(source, target)
    else:
        raise OSError(errno.ENOTSUP, "no atomic no-replace move primitive is available")
    if os.name != "nt":
        _fsync_directory(target.parent)


def _publish_new_by_move(
    source: Path,
    target: Path,
    *,
    identity: tuple[int, int],
) -> tuple[int, int]:
    try:
        move_no_replace(source, target)
    except FileExistsError:
        raise
    except OSError as exc:
        source_matches = _path_matches_identity(source, identity)
        target_matches = _path_matches_identity(target, identity)
        if target_matches and not source_matches:
            raise DurablePublishError(
                f"target is visible at {target}, but publication durability is unconfirmed; "
                f"the staged path was consumed: {exc}",
                outcome=PublishOutcome.COMMITTED,
            ) from exc
        target_absent = _path_absent(target)
        if source_matches and target_absent:
            detail = (
                "filesystem does not support atomic no-clobber publication"
                if exc.errno in _UNSUPPORTED_MOVE_ERRORS
                else "atomic no-clobber publication failed before the target became visible"
            )
            raise DurablePublishError(
                f"{detail} for {target}: {exc}",
                outcome=PublishOutcome.UNPUBLISHED,
            ) from exc
        raise DurablePublishError(
            f"atomic no-clobber publication has an unknown outcome for {target}; inspect "
            f"{source} and {target} before retrying: {exc}",
            outcome=PublishOutcome.UNKNOWN,
        ) from exc
    return identity


_UNSUPPORTED_MOVE_ERRORS = {
    errno.ENOSYS,
    errno.ENOTSUP,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
}


def _raise_move_error(error: int, target: Path) -> None:
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, "publish target already exists", str(target))
    raise OSError(error, "could not move path without replacement", str(target))


def _windows_move_kernel32() -> Any:
    loader = ctypes.__dict__.get("WinDLL")
    if not callable(loader):
        raise OSError(errno.ENOTSUP, "Windows atomic move APIs are unavailable")
    kernel32 = loader("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    return kernel32


def _windows_last_error() -> int:
    getter = ctypes.__dict__.get("get_last_error")
    if not callable(getter):  # pragma: no cover - only reachable on a broken Windows runtime
        return errno.EIO
    return int(getter())


def _move_windows_path_new(source: Path, target: Path) -> None:
    kernel32 = _windows_move_kernel32()
    moved = kernel32.MoveFileExW(str(source), str(target), 0x00000008)
    if moved:
        return
    error = _windows_last_error()
    if error in {80, 183}:
        raise FileExistsError(error, "publish target already exists", str(target))
    if error in {1, 50}:
        error = errno.ENOTSUP
    raise OSError(error, "could not move path without replacement", str(target))


def _path_matches_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return (metadata.st_dev, metadata.st_ino) == identity


def _path_absent(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _restore_exact_append(
    descriptor: int,
    *,
    original_size: int,
    content: bytes,
    identity: tuple[int, int],
) -> None:
    """Truncate only when every byte after the original end belongs to this append."""

    current = os.fstat(descriptor)
    if (
        not stat.S_ISREG(current.st_mode)
        or (int(current.st_dev), int(current.st_ino)) != identity
        or current.st_size < original_size
        or current.st_size > original_size + len(content)
    ):
        raise OSError("append target changed before exact rollback")
    appended_size = int(current.st_size) - original_size
    if _POSIX_OS.pread(descriptor, appended_size, original_size) != content[:appended_size]:
        raise OSError("append tail changed before exact rollback")
    os.ftruncate(descriptor, original_size)
    os.fsync(descriptor)
    if os.fstat(descriptor).st_size != original_size:
        raise OSError("append target size changed during exact rollback")


def append_durable(path: Path, content: bytes) -> None:
    """Append to an existing file or report restored, committed, or unknown state."""

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
    except Exception as exc:
        raise DurableAppendError(
            "could not open the existing append target", outcome=AppendOutcome.RESTORED
        ) from exc
    try:
        original_size = os.fstat(descriptor).st_size
    except Exception as exc:
        with suppress(OSError):
            os.close(descriptor)
        raise DurableAppendError(
            "could not inspect the append target", outcome=AppendOutcome.RESTORED
        ) from exc
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    except Exception as exc:
        try:
            os.ftruncate(descriptor, original_size)
            os.fsync(descriptor)
        except Exception as rollback_exc:
            with suppress(OSError):
                os.close(descriptor)
            raise DurableAppendError(
                "append failed and the previous bytes could not be restored",
                outcome=AppendOutcome.UNKNOWN,
            ) from rollback_exc
        with suppress(OSError):
            os.close(descriptor)
        raise DurableAppendError(
            "append failed and the previous bytes were restored",
            outcome=AppendOutcome.RESTORED,
        ) from exc
    try:
        os.close(descriptor)
    except Exception as exc:
        raise DurableAppendError(
            "append was synchronized but descriptor close failed",
            outcome=AppendOutcome.COMMITTED,
        ) from exc


def durable_unlink(path: Path) -> None:
    """Remove one file and persist the containing directory entry where supported."""

    path.unlink()
    _fsync_directory(path.parent)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _acquire(handle: _LockTarget, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                backend = cast(_WindowsLock, importlib.import_module("msvcrt"))
                handle.seek(0)
                backend.locking(handle.fileno(), backend.LK_NBLCK, 1)
            else:
                backend_posix = cast(_PosixLock, importlib.import_module("fcntl"))
                backend_posix.flock(handle.fileno(), backend_posix.LOCK_EX | backend_posix.LOCK_NB)
            return
        except OSError as exc:
            if not _contention(exc):
                raise
            if time.monotonic() >= deadline:
                raise ConflictError(f"timed out waiting for lock: {handle.name}") from exc
            time.sleep(0.025)


def _release(handle: _LockTarget) -> None:
    if os.name == "nt":
        backend = cast(_WindowsLock, importlib.import_module("msvcrt"))
        handle.seek(0)
        backend.locking(handle.fileno(), backend.LK_UNLCK, 1)
    else:
        backend_posix = cast(_PosixLock, importlib.import_module("fcntl"))
        backend_posix.flock(handle.fileno(), backend_posix.LOCK_UN)


def _contention(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EBUSY, errno.EDEADLK} or getattr(
        exc, "winerror", None
    ) in {33, 36, 158}


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
