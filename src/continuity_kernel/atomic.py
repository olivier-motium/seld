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
        if os.name != "nt":
            os.fchmod(descriptor, mode)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
