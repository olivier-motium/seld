"""Cross-platform locking and crash-safe local file primitives."""

from __future__ import annotations

import errno
import hashlib
import importlib
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from enum import StrEnum
from pathlib import Path
from typing import IO, Protocol, cast

from continuity_kernel.errors import ConflictError


class _PosixLock(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


class _WindowsLock(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, length: int) -> None: ...


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
    """Acquire an advisory lock with a bounded wait on every supported OS."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        _acquire(handle, timeout=timeout)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()


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


def append_durable(path: Path, content: bytes) -> None:
    """Append to an existing file or report restored, committed, or unknown state."""

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
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


def _acquire(handle: IO[bytes], *, timeout: float) -> None:
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


def _release(handle: IO[bytes]) -> None:
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
