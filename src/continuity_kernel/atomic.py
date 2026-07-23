"""Cross-platform locking and crash-safe local file primitives."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from ctypes import wintypes
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Protocol, cast

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
        raise DurablePublishError(
            f"target is visible at {target}, but publication durability is unconfirmed; "
            f"the staged file remains at {source}",
            outcome=PublishOutcome.COMMITTED,
        ) from exc
    try:
        source.unlink()
    except OSError as exc:
        raise DurablePublishError(
            f"target is committed at {target}, but the staged file remains at {source}: {exc}",
            outcome=PublishOutcome.COMMITTED,
        ) from exc
    try:
        _fsync_directory(target.parent)
    except OSError as exc:
        raise DurablePublishError(
            f"target is committed at {target}, but staged-file cleanup durability is unconfirmed",
            outcome=PublishOutcome.COMMITTED,
        ) from exc
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
