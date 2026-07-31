"""Bounded, non-persistent state and artifact primitives for connector transfers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Final

from continuity_kernel.config import data_dir
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.local_files import MAX_FILE_TRANSFER_BYTES, LocalFileRef

DEFAULT_TRANSFER_TTL_SECONDS: Final = 10 * 60
MAX_TRANSFER_TTL_SECONDS: Final = 15 * 60
MAX_TRANSFER_HANDLES: Final = 4_096
MAX_TRANSFER_STATE_BYTES: Final = 64 * 1024
MAX_ARTIFACT_BYTES: Final = MAX_FILE_TRANSFER_BYTES
ARTIFACT_CHUNK_BYTES: Final = 1024 * 1024
DEFAULT_ARTIFACT_TTL_SECONDS: Final = 24 * 60 * 60
MAX_ARTIFACT_TTL_SECONDS: Final = 7 * 24 * 60 * 60

_HANDLE = re.compile(r"^tr1\.[A-Za-z0-9_-]{43}$")
_ARTIFACT_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_PART_NAME = re.compile(r"^\.seld-artifact-[A-Za-z0-9_-]+\.part$")
_STAGE_NAME = re.compile(r"^\.seld-upload-[A-Za-z0-9_-]+\.stage$")
_FINAL_NAME = re.compile(r"^(art_[A-Za-z0-9_-]{24})--([0-9]{1,13})--([A-Za-z0-9._-]{1,160})$")
_SECURE_ARTIFACT_STORE_SUPPORTED: Final = (
    os.name != "nt"
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.link in os.supports_dir_fd
    and os.link in os.supports_follow_symlinks
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
)

Clock = Callable[[], float]


@dataclass(frozen=True, repr=False)
class _TransferEntry:
    kind: str
    payload: bytes
    binding_digest: str
    expires_at: float


class TransferStore:
    """Process-local TTL storage for opaque provider continuation handles."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        max_entries: int = MAX_TRANSFER_HANDLES,
    ) -> None:
        if clock is not None and not callable(clock):
            raise ValidationError("transfer store clock is invalid")
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_TRANSFER_HANDLES:
            raise ValidationError("transfer store capacity is invalid")
        self._clock = clock or time.monotonic
        self._max_entries = max_entries
        self._entries: dict[str, _TransferEntry] = {}
        self._lock = RLock()

    def issue(
        self,
        value: object,
        *,
        binding: object,
        ttl_seconds: int = DEFAULT_TRANSFER_TTL_SECONDS,
    ) -> str:
        """Store a continuation and return a random handle containing no state."""

        now = self._now()
        ttl = _ttl(ttl_seconds)
        kind, payload = _freeze_transfer_value(value)
        binding_digest = _binding_digest(binding)
        handle = f"tr1.{secrets.token_urlsafe(32)}"
        with self._lock:
            self._prune(now)
            if len(self._entries) >= self._max_entries:
                raise ConflictError("transfer store capacity is full; wait for state to expire")
            self._entries[handle] = _TransferEntry(
                kind=kind,
                payload=payload,
                binding_digest=binding_digest,
                expires_at=now + ttl,
            )
        return handle

    def consume(self, handle: object, *, binding: object) -> object:
        """Consume one correctly bound handle exactly once."""

        clean_handle = _handle(handle)
        expected_binding = _binding_digest(binding)
        now = self._now()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(clean_handle)
            if entry is None:
                raise ConflictError("transfer handle is unavailable, expired, or already consumed")
            if entry.binding_digest != expected_binding:
                raise ConflictError("transfer handle binding does not match")
            del self._entries[clean_handle]
            return _thaw_transfer_value(entry.kind, entry.payload)

    def prune(self) -> int:
        """Remove expired process-local entries and return the number removed."""

        with self._lock:
            before = len(self._entries)
            self._prune(self._now())
            return before - len(self._entries)

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise ValidationError("transfer store clock is unavailable") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("transfer store clock returned an invalid time")
        result = float(value)
        if not math.isfinite(result):
            raise ValidationError("transfer store clock returned a non-finite time")
        return result

    def _prune(self, now: float) -> None:
        for handle, entry in tuple(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[handle]


@dataclass(frozen=True, repr=False)
class ArtifactReceipt:
    """A privacy-safe receipt for one completed local artifact."""

    artifact_id: str
    filename: str
    media_type: str | None
    size: int
    sha256: str
    expires_at: float
    _path: Path = field(repr=False, compare=False)

    def __repr__(self) -> str:
        return (
            "ArtifactReceipt(artifact_id=<redacted>, filename=<redacted>, "
            f"media_type={self.media_type!r}, size={self.size!r}, sha256=<redacted>, "
            "expires_at=<redacted>, path=<redacted>)"
        )

    @property
    def name(self) -> str:
        """Compatibility alias for the user-facing filename."""

        return self.filename

    @property
    def path(self) -> Path:
        """Return the transient owner-only artifact path to the local caller."""

        return self._path

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "bytes": self.size,
            "expires_at": datetime.fromtimestamp(self.expires_at, UTC).isoformat(),
            "filename": self.filename,
            "media_type": self.media_type,
            "path": str(self._path),
            "sha256": self.sha256,
        }


class PreparedUpload:
    """An immutable, anonymous, process-local snapshot ready for provider upload."""

    def __init__(
        self,
        *,
        filename: str,
        media_type: str | None,
        size: int,
        sha256: str,
        descriptor: int,
    ) -> None:
        self.filename = filename
        self.media_type = media_type
        self.size = size
        self.sha256 = sha256
        self._descriptor = descriptor
        self._closed = False
        self._lock = RLock()
        self._finalizer = weakref.finalize(self, _close_quietly, descriptor)

    def __repr__(self) -> str:
        return (
            "PreparedUpload(filename=<redacted>, "
            f"media_type={self.media_type!r}, size={self.size!r}, sha256=<redacted>)"
        )

    def binding(self) -> dict[str, object]:
        """Return the content identity safe to bind into a confirmation preview."""

        return {
            "bytes": self.size,
            "filename": self.filename,
            "media_type": self.media_type,
            "sha256": self.sha256,
        }

    def iter_chunks(
        self,
        *,
        offset: int = 0,
        length: int | None = None,
        chunk_size: int = ARTIFACT_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        """Read an exact slice from the anonymous snapshot without sharing its descriptor."""

        if type(offset) is not int or not 0 <= offset <= self.size:
            raise ValidationError("prepared upload offset is invalid")
        remaining = self.size - offset if length is None else length
        if type(remaining) is not int or not 0 <= remaining <= self.size - offset:
            raise ValidationError("prepared upload length is invalid")
        if type(chunk_size) is not int or not 1 <= chunk_size <= ARTIFACT_CHUNK_BYTES:
            raise ValidationError("prepared upload chunk size is invalid")
        with self._lock:
            if self._closed or self._descriptor < 0:
                raise ValidationError("prepared upload is closed")
            try:
                descriptor = os.dup(self._descriptor)
            except OSError as exc:
                raise ValidationError("prepared upload is unavailable") from exc
        try:
            os.lseek(descriptor, offset, os.SEEK_SET)
            while remaining:
                block = os.read(descriptor, min(chunk_size, remaining))
                if not block:
                    raise ValidationError("prepared upload ended before its captured length")
                remaining -= len(block)
                yield block
        except OSError as exc:
            raise ValidationError("prepared upload could not be read") from exc
        finally:
            os.close(descriptor)

    def close(self) -> None:
        with self._lock:
            if self._descriptor >= 0:
                self._finalizer()
                self._descriptor = -1
            self._closed = True

    def __enter__(self) -> PreparedUpload:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


@dataclass
class _PendingArtifact:
    artifact_id: str
    filename: str
    media_type: str | None
    part_name: str
    final_name: str | None
    expires_at: float
    ttl_seconds: int
    writer: weakref.ReferenceType[ArtifactWriter] | None = None


class ArtifactWriter:
    """Bounded streaming writer used by :class:`ArtifactStore`."""

    def __init__(
        self,
        store: ArtifactStore,
        pending_id: str,
        pending: _PendingArtifact,
        descriptor: int,
        expected_size: int | None,
    ) -> None:
        self._store = store
        self._pending_id = pending_id
        self._pending = pending
        self._descriptor = descriptor
        self._expected_size = expected_size
        self._size = 0
        self._digest = hashlib.sha256()
        self._closed = False
        self._finalizer = weakref.finalize(
            self,
            store._abandon_writer,
            pending_id,
            pending,
            descriptor,
        )
        pending.writer = weakref.ref(self)

    def __repr__(self) -> str:
        return "ArtifactWriter(name=<redacted>, state=<redacted>)"

    @property
    def size(self) -> int:
        return self._size

    def write(self, content: bytes) -> int:
        if self._closed:
            raise ValidationError("artifact writer is closed")
        if not isinstance(content, bytes):
            raise ValidationError("artifact chunk must be bytes")
        if self._size + len(content) > self._store.max_bytes:
            self.abort()
            raise ValidationError("artifact exceeds its size bound")
        try:
            _write_all(self._descriptor, content)
        except OSError as exc:
            self.abort()
            raise ValidationError("artifact could not be written") from exc
        self._size += len(content)
        self._digest.update(content)
        return len(content)

    def finish(self) -> ArtifactReceipt:
        if self._closed:
            raise ValidationError("artifact writer is closed")
        if self._expected_size is not None and self._size != self._expected_size:
            self.abort()
            raise ValidationError("artifact size does not match its declared length")
        try:
            os.fsync(self._descriptor)
            os.fchmod(self._descriptor, 0o600)
            os.close(self._descriptor)
            self._descriptor = -1
            self._finalizer.detach()
            expires_at = self._store._publish(self._pending)
        except Exception as exc:
            self.abort()
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("artifact could not be completed") from exc
        self._closed = True
        self._store._finished(self._pending_id)
        if self._pending.final_name is None:
            raise ValidationError("artifact publication did not produce a final path")
        return ArtifactReceipt(
            artifact_id=self._pending.artifact_id,
            filename=self._pending.filename,
            media_type=self._pending.media_type,
            size=self._size,
            sha256=self._digest.hexdigest(),
            expires_at=expires_at,
            _path=self._store.root / self._pending.final_name,
        )

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._descriptor >= 0:
            with suppress(OSError):
                os.close(self._descriptor)
            self._descriptor = -1
        self._finalizer.detach()
        self._store._abort(self._pending_id, self._pending)

    def __enter__(self) -> ArtifactWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del traceback
        if exc_type is None:
            self.finish()
        else:
            self.abort()


class ArtifactStore:
    """Owner-only local artifact directory with atomic bounded completion."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        max_bytes: int = MAX_ARTIFACT_BYTES,
        clock: Clock | None = None,
    ) -> None:
        if not _SECURE_ARTIFACT_STORE_SUPPORTED:
            raise ValidationError("secure connector artifact storage is unavailable on this host")
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_ARTIFACT_BYTES:
            raise ValidationError("artifact store size bound is invalid")
        if clock is not None and not callable(clock):
            raise ValidationError("artifact store clock is invalid")
        self.root = (
            Path(root) if root is not None else data_dir() / "connector-artifacts"
        ).expanduser()
        if not self.root.is_absolute():
            raise ValidationError("artifact store requires an absolute root")
        self.max_bytes = max_bytes
        self._clock = clock or time.time
        self._lock = RLock()
        self._pending: dict[str, _PendingArtifact] = {}
        self._completed: dict[str, float] = {}
        self._active_stages: set[str] = set()
        self._root_fd = -1
        self._root_identity: tuple[int, int] | None = None
        self._open_root()
        self.cleanup()

    @staticmethod
    def supported() -> bool:
        """Report whether this host can enforce descriptor-pinned artifact storage."""

        return _SECURE_ARTIFACT_STORE_SUPPORTED

    def start(
        self,
        name: str,
        *,
        media_type: str | None = None,
        expected_size: int | None = None,
        ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
    ) -> ArtifactWriter:
        clean_name = sanitize_artifact_name(name)
        clean_media_type = _media_type(media_type)
        if expected_size is not None and (
            type(expected_size) is not int or not 0 <= expected_size <= self.max_bytes
        ):
            raise ValidationError("artifact expected size is invalid")
        now = self._now()
        ttl = _artifact_ttl(ttl_seconds)
        with self._lock:
            self._ensure_root()
            self._prune_locked(now)
            artifact_id = f"art_{secrets.token_urlsafe(18)}"
            pending_id = secrets.token_urlsafe(24)
            part_name = f".seld-artifact-{secrets.token_urlsafe(24)}.part"
            pending = _PendingArtifact(
                artifact_id=artifact_id,
                filename=clean_name,
                media_type=clean_media_type,
                part_name=part_name,
                final_name=None,
                expires_at=now + ttl,
                ttl_seconds=ttl,
            )
            try:
                descriptor = os.open(
                    part_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=self._root_fd,
                )
            except OSError as exc:
                raise ValidationError("artifact temporary file could not be created") from exc
            self._pending[pending_id] = pending
            return ArtifactWriter(self, pending_id, pending, descriptor, expected_size)

    def prepare_upload(
        self,
        reference: LocalFileRef,
        *,
        filename: str | None = None,
        media_type: str | None = None,
        max_bytes: int | None = None,
    ) -> PreparedUpload:
        """Copy a granted file into an anonymous immutable snapshot before preview."""

        if not isinstance(reference, LocalFileRef):
            raise ValidationError("prepared upload requires a local file reference")
        bound = self.max_bytes if max_bytes is None else max_bytes
        if type(bound) is not int or not 0 <= bound <= self.max_bytes:
            raise ValidationError("prepared upload size bound is invalid")
        if reference.size > bound:
            raise ValidationError("local file exceeds this provider operation's size limit")
        clean_name = sanitize_artifact_name(filename or Path(reference.relative_path).name)
        clean_media_type = _media_type(media_type)
        stage_name = f".seld-upload-{secrets.token_urlsafe(24)}.stage"
        root_descriptor = -1
        descriptor = -1
        read_descriptor = -1
        digest = hashlib.sha256()
        total = 0
        with self._lock:
            self._ensure_root()
            try:
                root_descriptor = os.dup(self._root_fd)
                descriptor = os.open(
                    stage_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=root_descriptor,
                )
            except OSError as exc:
                if root_descriptor >= 0:
                    with suppress(OSError):
                        os.close(root_descriptor)
                raise ValidationError("prepared upload snapshot could not be created") from exc
            self._active_stages.add(stage_name)
        try:
            for block in reference.iter_chunks(chunk_size=ARTIFACT_CHUNK_BYTES):
                total += len(block)
                if total > bound:
                    raise ValidationError(
                        "local file exceeds this provider operation's size limit"
                    )
                _write_all(descriptor, block)
                digest.update(block)
            if total != reference.size or digest.hexdigest() != reference.sha256:
                raise ValidationError("local file changed while its upload was prepared")
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            descriptor = -1
            read_descriptor = os.open(
                stage_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            metadata = os.fstat(read_descriptor)
            if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_size) != total:
                raise ValidationError("prepared upload snapshot is invalid")
            os.unlink(stage_name, dir_fd=root_descriptor)
        except Exception:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            if read_descriptor >= 0:
                with suppress(OSError):
                    os.close(read_descriptor)
                read_descriptor = -1
            with suppress(FileNotFoundError, OSError):
                os.unlink(stage_name, dir_fd=root_descriptor)
            raise
        finally:
            with self._lock:
                self._active_stages.discard(stage_name)
            if root_descriptor >= 0:
                with suppress(OSError):
                    os.close(root_descriptor)
        return PreparedUpload(
            filename=clean_name,
            media_type=clean_media_type,
            size=total,
            sha256=digest.hexdigest(),
            descriptor=read_descriptor,
        )

    def cleanup(self) -> int:
        """Remove expired pending and completed artifacts."""

        with self._lock:
            self._ensure_root()
            now = self._now()
            removed = self._prune_locked(now)
            return removed + self._prune_orphan_parts_locked(now)

    def close(self) -> None:
        with self._lock:
            for pending_id, pending in tuple(self._pending.items()):
                writer = pending.writer() if pending.writer is not None else None
                if writer is not None:
                    writer.abort()
                else:
                    self._unlink_part(pending.part_name)
                self._pending.pop(pending_id, None)
            if self._root_fd >= 0:
                os.close(self._root_fd)
                self._root_fd = -1

    def _open_root(self) -> None:
        if not os.path.lexists(self.root):
            self.root.mkdir(parents=True, mode=0o700)
            if os.name != "nt":
                self.root.chmod(0o700)
        try:
            metadata = os.lstat(self.root)
        except OSError as exc:
            raise ValidationError("artifact store root is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077)
        ):
            raise ValidationError("artifact store root must be an owner-only directory")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(self.root, flags)
        except OSError as exc:
            raise ValidationError("artifact store root could not be pinned") from exc
        opened = os.fstat(self._root_fd)
        if not stat.S_ISDIR(opened.st_mode):
            self.close()
            raise ValidationError("artifact store root is not a directory")
        self._root_identity = (int(opened.st_dev), int(opened.st_ino))

    def _ensure_root(self) -> None:
        if self._root_fd < 0 or self._root_identity is None:
            raise ValidationError("artifact store is closed")
        try:
            opened = os.fstat(self._root_fd)
            listed = os.lstat(self.root)
        except OSError as exc:
            raise ValidationError("artifact store root changed") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(listed.st_mode)
            or stat.S_ISLNK(listed.st_mode)
            or (int(opened.st_dev), int(opened.st_ino)) != self._root_identity
            or (int(listed.st_dev), int(listed.st_ino)) != self._root_identity
            or (os.name != "nt" and stat.S_IMODE(listed.st_mode) & 0o077)
        ):
            raise ValidationError("artifact store root changed")

    def _publish(self, pending: _PendingArtifact) -> float:
        with self._lock:
            self._ensure_root()
            expires_at = float(int(self._now() + pending.ttl_seconds))
            final_name = f"{pending.artifact_id}--{int(expires_at)}--{pending.filename}"
            if _FINAL_NAME.fullmatch(final_name) is None:
                raise ValidationError("artifact destination name is invalid")
            try:
                existing = os.stat(final_name, dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise ValidationError("artifact destination could not be inspected") from exc
            if existing is not None:
                raise ConflictError("artifact destination already exists")
            try:
                os.link(
                    pending.part_name,
                    final_name,
                    src_dir_fd=self._root_fd,
                    dst_dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                os.unlink(pending.part_name, dir_fd=self._root_fd)
                os.fsync(self._root_fd)
            except OSError as exc:
                raise ValidationError("artifact could not be published atomically") from exc
            pending.final_name = final_name
            pending.expires_at = expires_at
            self._completed[final_name] = expires_at
            return expires_at

    def _finished(self, pending_id: str) -> None:
        with self._lock:
            self._pending.pop(pending_id, None)

    def _abort(self, pending_id: str, pending: _PendingArtifact) -> None:
        with self._lock:
            self._unlink_part(pending.part_name)
            self._pending.pop(pending_id, None)

    def _abandon_writer(
        self,
        pending_id: str,
        pending: _PendingArtifact,
        descriptor: int,
    ) -> None:
        _close_quietly(descriptor)
        with self._lock:
            if self._pending.get(pending_id) is not pending:
                return
            self._unlink_part(pending.part_name)
            self._pending.pop(pending_id, None)

    def _unlink_part(self, part_name: str) -> None:
        with suppress(FileNotFoundError):
            os.unlink(part_name, dir_fd=self._root_fd)

    def _prune_orphan_parts_locked(self, now: float) -> int:
        tracked = {pending.part_name for pending in self._pending.values()} | self._active_stages
        removed = 0
        try:
            names = os.listdir(self._root_fd)
        except OSError as exc:
            raise ValidationError("artifact store entries could not be inspected") from exc
        for name in names:
            if not isinstance(name, str) or name in tracked:
                continue
            if _PART_NAME.fullmatch(name) is None and _STAGE_NAME.fullmatch(name) is None:
                continue
            try:
                metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                if (
                    stat.S_ISREG(metadata.st_mode)
                    and float(metadata.st_mtime) + MAX_ARTIFACT_TTL_SECONDS <= now
                ):
                    os.unlink(name, dir_fd=self._root_fd)
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValidationError("expired artifact cleanup failed") from exc
        return removed

    def _prune_locked(self, now: float) -> int:
        removed = 0
        for pending_id, pending in tuple(self._pending.items()):
            if pending.expires_at > now:
                continue
            writer = pending.writer() if pending.writer is not None else None
            if writer is not None:
                continue
            self._unlink_part(pending.part_name)
            self._pending.pop(pending_id, None)
            removed += 1
        return removed + self._prune_completed_files_locked(now)

    def _prune_completed_files_locked(self, now: float) -> int:
        removed = 0
        try:
            names = os.listdir(self._root_fd)
        except OSError as exc:
            raise ValidationError("artifact store entries could not be inspected") from exc
        for name in names:
            if not isinstance(name, str):
                continue
            match = _FINAL_NAME.fullmatch(name)
            if match is None or float(int(match.group(2))) > now:
                continue
            try:
                metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise ValidationError("expired artifact is not a regular file")
                os.unlink(name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ValidationError("expired artifact cleanup failed") from exc
            self._completed.pop(name, None)
            removed += 1
        return removed

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise ValidationError("artifact store clock is unavailable") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("artifact store clock returned an invalid time")
        result = float(value)
        if not math.isfinite(result):
            raise ValidationError("artifact store clock returned a non-finite time")
        return result


def sanitize_artifact_name(value: object) -> str:
    """Turn an untrusted display name into one flat, bounded artifact name."""

    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValidationError("artifact name is invalid")
    if "\x00" in value or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ValidationError("artifact name is invalid")
    name = re.split(r"[/\\]", value)[-1]
    name = _ARTIFACT_NAME.sub("_", name)
    name = re.sub(r"\.{2,}", "_", name).strip(".")
    if not name or name in {".", ".."}:
        raise ValidationError("artifact name is invalid")
    if len(name) > 160:
        name = name[:160]
    if name.startswith(".seld-artifact-") or name.endswith(".part"):
        name = f"artifact_{name.lstrip('.')}"
    return name or "artifact"


def _handle(value: object) -> str:
    if not isinstance(value, str) or _HANDLE.fullmatch(value) is None:
        raise ValidationError("transfer handle is invalid")
    return value


def _ttl(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_TRANSFER_TTL_SECONDS:
        raise ValidationError("transfer TTL is invalid")
    return value


def _artifact_ttl(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ARTIFACT_TTL_SECONDS:
        raise ValidationError("artifact TTL is invalid")
    return value


def _media_type(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or "/" not in value
    ):
        raise ValidationError("artifact media type is invalid")
    return value


def _freeze_transfer_value(value: object) -> tuple[str, bytes]:
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("transfer state is not valid UTF-8 text") from exc
        if len(encoded) > MAX_TRANSFER_STATE_BYTES:
            raise ValidationError("transfer state exceeds its in-memory bound")
        return "text", encoded
    if isinstance(value, (bytes, bytearray)):
        encoded = bytes(value)
        if len(encoded) > MAX_TRANSFER_STATE_BYTES:
            raise ValidationError("transfer state exceeds its in-memory bound")
        return "bytes", encoded
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
        try:
            encoded = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (RecursionError, TypeError, UnicodeError, ValueError) as exc:
            raise ValidationError("transfer state is not bounded JSON-compatible data") from exc
        if len(encoded) > MAX_TRANSFER_STATE_BYTES:
            raise ValidationError("transfer state exceeds its in-memory bound")
        return "json", encoded
    raise ValidationError("transfer state is not a supported in-memory value")


def _thaw_transfer_value(kind: str, payload: bytes) -> object:
    if kind == "bytes":
        return bytes(payload)
    if kind == "text":
        return payload.decode("utf-8")
    if kind == "json":
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("stored transfer state is invalid") from exc
    raise ValidationError("stored transfer state kind is invalid")


def _binding_digest(value: object) -> str:
    if isinstance(value, bytes):
        encoded = value
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        try:
            encoded = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise ValidationError("transfer binding is invalid") from exc
    if not encoded or len(encoded) > MAX_TRANSFER_STATE_BYTES:
        raise ValidationError("transfer binding is invalid")
    return hashlib.sha256(b"seld-transfer-binding\0" + encoded).hexdigest()


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short artifact write")
        view = view[written:]


def _close_quietly(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
