"""Descriptor-pinned, owner-only snapshots for passive local SQLite reads."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Final

from continuity_kernel.errors import ValidationError

MAX_DATABASE_BYTES: Final = 32 * 1024 * 1024 * 1024
MAX_WAL_BYTES: Final = 2 * 1024 * 1024 * 1024

SQLiteFileIdentity = tuple[int, int, int, int, str]


@contextmanager
def pinned_sqlite_snapshot(
    database: Path,
    *,
    label: str,
) -> Iterator[tuple[Path, SQLiteFileIdentity, bool]]:
    """Copy or clone the exact opened main/WAL files into one private snapshot.

    The provider pathname is never handed to SQLite. A live writer may make a
    snapshot attempt fail, but it cannot redirect the read to another inode.
    """

    main_descriptor, main_identity = _open_regular(
        database,
        label=label,
        max_bytes=MAX_DATABASE_BYTES,
        required=True,
    )
    assert main_descriptor is not None and main_identity is not None
    wal_path = database.with_name(database.name + "-wal")
    wal_descriptor: int | None = None
    wal_identity: SQLiteFileIdentity | None = None
    wal_was_absent = False
    try:
        wal_descriptor, wal_identity = _open_regular(
            wal_path,
            label=f"{label} WAL",
            max_bytes=MAX_WAL_BYTES,
            required=False,
        )
        wal_was_absent = wal_descriptor is None
        _reject_rollback_journal(database, label=label)
        with tempfile.TemporaryDirectory(prefix="seld-sqlite-snapshot-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            snapshot = root / database.name
            _copy_or_clone(main_descriptor, snapshot, max_bytes=MAX_DATABASE_BYTES)
            if wal_descriptor is not None:
                _copy_or_clone(
                    wal_descriptor,
                    snapshot.with_name(snapshot.name + "-wal"),
                    max_bytes=MAX_WAL_BYTES,
                )
            yield snapshot, main_identity, wal_descriptor is None
        _validate_opened(database, main_descriptor, main_identity, label=label)
        if wal_descriptor is not None and wal_identity is not None:
            _validate_opened(wal_path, wal_descriptor, wal_identity, label=f"{label} WAL")
        elif wal_was_absent and os.path.lexists(wal_path):
            raise ValidationError(f"{label} WAL appeared during the snapshot")
        _reject_rollback_journal(database, label=label)
    finally:
        if wal_descriptor is not None:
            os.close(wal_descriptor)
        os.close(main_descriptor)


def _open_regular(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    required: bool,
) -> tuple[int | None, SQLiteFileIdentity | None]:
    try:
        listed = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise ValidationError(f"{label} is missing") from None
        return None, None
    except OSError as exc:
        raise ValidationError(f"{label} is unreadable") from exc
    if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise ValidationError(f"{label} must be a regular file, not a link")
    if listed.st_size > max_bytes:
        raise ValidationError(f"{label} exceeds its snapshot size bound")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValidationError(f"{label} could not be opened") from exc
    opened = os.fstat(descriptor)
    opened_identity = _identity(opened)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_size > max_bytes
        or opened_identity[:2] != _identity(listed)[:2]
    ):
        os.close(descriptor)
        raise ValidationError(f"{label} changed while it was opened")
    return descriptor, opened_identity


def _validate_opened(
    path: Path,
    descriptor: int,
    expected: SQLiteFileIdentity,
    *,
    label: str,
) -> None:
    try:
        opened = os.fstat(descriptor)
        listed = os.lstat(path)
    except OSError as exc:
        raise ValidationError(f"{label} disappeared during the snapshot") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(listed.st_mode)
        or _identity(opened) != expected
        or _identity(listed) != expected
    ):
        raise ValidationError(f"{label} changed during the snapshot")


def _reject_rollback_journal(database: Path, *, label: str) -> None:
    journal = database.with_name(database.name + "-journal")
    if os.path.lexists(journal):
        raise ValidationError(f"{label} has an active rollback journal")


def _copy_or_clone(source: int, destination: Path, *, max_bytes: int) -> None:
    metadata = os.fstat(source)
    if metadata.st_size > max_bytes:
        raise ValidationError("SQLite snapshot input exceeds its size bound")
    if sys.platform == "darwin" and _clone_descriptor(source, destination):
        destination.chmod(0o600)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        return
    target = -1
    try:
        target = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.lseek(source, 0, os.SEEK_SET)
        remaining = metadata.st_size
        while remaining:
            block = os.read(source, min(1024 * 1024, remaining))
            if not block:
                raise ValidationError("SQLite snapshot input ended before its declared size")
            view = memoryview(block)
            while view:
                written = os.write(target, view)
                view = view[written:]
            remaining -= len(block)
        if os.read(source, 1):
            raise ValidationError("SQLite snapshot input grew past its declared size")
        os.fsync(target)
    finally:
        if target >= 0:
            os.close(target)


def _clone_descriptor(source: int, destination: Path) -> bool:
    directory = -1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        clone = libc.fclonefileat
        clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        clone.restype = ctypes.c_int
        directory = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        if clone(source, directory, os.fsencode(destination.name), 0) == 0:
            return True
    except (AttributeError, OSError):
        pass
    finally:
        if directory >= 0:
            os.close(directory)
    with suppress(FileNotFoundError):
        destination.unlink()
    return False


def _identity(metadata: os.stat_result) -> SQLiteFileIdentity:
    birthtime = getattr(metadata, "st_birthtime", None)
    birthtime_token = float(birthtime).hex() if birthtime is not None else "unavailable"
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        birthtime_token,
    )


__all__ = ["SQLiteFileIdentity", "pinned_sqlite_snapshot"]
