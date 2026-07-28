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
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from continuity_kernel.errors import ConflictError, ValidationError


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


def _snapshot(metadata: os.stat_result) -> RegularFileSnapshot:
    return RegularFileSnapshot(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        modified_ns=int(metadata.st_mtime_ns),
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
        try:
            self._validate_root_identity()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._directory_binding is not None:
            raise ValidationError("cannot close pinned local storage with an active binding")
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
            try:
                listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                self._confirm_leaf_absent(parent_parts, parent, name, label=label)
                if missing_ok:
                    return None
                raise ValidationError(f"{label} is missing beneath the pinned root") from None
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
                raise ValidationError(f"{label} must be a regular file, not a link")
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
            return content
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError(f"could not read {label} beneath the pinned root: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)

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

    @contextmanager
    def bind_directory(
        self,
        relative: Path | str,
    ) -> Iterator[tuple[int, int]]:
        """Keep one descendant directory identity for a multi-file transaction."""

        parts = _relative_parts(relative)
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

        descriptor = self._open_directory_from_root(parts)
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
        self._validate_root_identity()
        parent, name = self._open_parent(parts)
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
                raise ValidationError(f"could not lock pinned local storage: {exc}") from exc
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
                yield
                self._validate_file_lock_identity(
                    parent,
                    name,
                    descriptor,
                    expected_identity=lock_identity,
                )
                self._validate_directory_path(parent_parts, parent)
            finally:
                _release(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)

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
        binding = self._directory_binding
        if binding is not None and parts[: len(binding.parts)] == binding.parts:
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
                    os.close(child)
                    raise ValidationError("pinned storage component is not a directory")
                os.close(current)
                current = child
            return current
        except OSError as exc:
            os.close(current)
            if isinstance(exc, FileNotFoundError):
                raise
            raise ValidationError(f"could not traverse pinned local storage: {exc}") from exc
        except Exception:
            os.close(current)
            raise


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


@contextmanager
def exclusive_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Acquire a bounded advisory lock on one stable, named regular file."""

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
