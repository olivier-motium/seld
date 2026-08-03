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
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock, Timer
from typing import Any, Final, Protocol, cast

from continuity_kernel.config import data_dir
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.local_files import (
    MAX_FILE_TRANSFER_BYTES,
    LocalFileAuthorityUse,
    LocalFileRef,
    LocalFileTransferCandidate,
)

DEFAULT_TRANSFER_TTL_SECONDS: Final = 10 * 60
MAX_TRANSFER_TTL_SECONDS: Final = 15 * 60
MAX_TRANSFER_HANDLES: Final = 4_096
MAX_TRANSFER_STATE_BYTES: Final = 64 * 1024
MAX_ARTIFACT_BYTES: Final = MAX_FILE_TRANSFER_BYTES
ARTIFACT_CHUNK_BYTES: Final = 1024 * 1024
DEFAULT_ARTIFACT_TTL_SECONDS: Final = 24 * 60 * 60
MAX_ARTIFACT_TTL_SECONDS: Final = 7 * 24 * 60 * 60
_POSIX_OS = cast(Any, os)

_HANDLE = re.compile(r"^tr1\.[A-Za-z0-9_-]{43}$")
_ARTIFACT_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_PART_NAME = re.compile(r"^\.seld-artifact-[A-Za-z0-9_-]+\.part$")
_STAGE_NAME = re.compile(r"^\.seld-upload-[A-Za-z0-9_-]+\.stage$")
_FINAL_NAME = re.compile(r"^(art_[A-Za-z0-9_-]{24})--([0-9]{1,13})--([A-Za-z0-9._-]{1,160})$")
_SECURE_ARTIFACT_STORE_SUPPORTED: Final = os.name == "nt" or (
    hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.link in os.supports_dir_fd
    and os.link in os.supports_follow_symlinks
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
)
_BINARY_OPEN: Final = getattr(os, "O_BINARY", 0)
_NOINHERIT_OPEN: Final = getattr(os, "O_NOINHERIT", 0)
_TEMPORARY_OPEN: Final = getattr(os, "O_TEMPORARY", 0)

Clock = Callable[[], float]
ConnectorInputPath = tuple[str | int, ...]


class ScheduledCall(Protocol):
    def cancel(self) -> None: ...


Scheduler = Callable[[float, Callable[[], None]], ScheduledCall]


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
    """A privacy-safe receipt for one owner-only host-cache artifact."""

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
            "cleanup": "after_expiry_while_runtime_active_or_on_next_start",
            "expires_at": datetime.fromtimestamp(self.expires_at, UTC).isoformat(),
            "filename": self.filename,
            "media_type": self.media_type,
            "path": str(self._path),
            "sha256": self.sha256,
            "storage": "owner_only_transient_host_cache",
            "transient": True,
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
        _set_binary_mode(descriptor)
        self.filename = filename
        self.media_type = media_type
        self.size = size
        self.sha256 = sha256
        self._descriptor = descriptor
        self._authority: LocalFileAuthorityUse | None = None
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
            authority = self._authority
            try:
                descriptor = os.dup(self._descriptor)
            except OSError as exc:
                raise ValidationError("prepared upload is unavailable") from exc
        try:
            os.lseek(descriptor, offset, os.SEEK_SET)
            while remaining:
                if authority is None:
                    block = os.read(descriptor, min(chunk_size, remaining))
                    if not block:
                        raise ValidationError("prepared upload ended before its captured length")
                    remaining -= len(block)
                    yield block
                    continue
                with authority.delivery():
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
            authority = self._authority
            self._authority = None
            if self._descriptor >= 0:
                self._finalizer()
                self._descriptor = -1
            self._closed = True
        if authority is not None:
            authority.close()

    def bind_authority(self, authority: LocalFileAuthorityUse) -> None:
        if not isinstance(authority, LocalFileAuthorityUse):
            raise ValidationError("prepared upload authority is invalid")
        with self._lock:
            if self._closed:
                authority.close()
                raise ConflictError("prepared upload was invalidated before it could be used")
            if self._authority is not None and self._authority is not authority:
                raise ConflictError("prepared upload authority is already bound")
            self._authority = authority

    def __enter__(self) -> PreparedUpload:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


@dataclass(frozen=True, repr=False)
class PreparedUploadBinding:
    """One anonymous upload bound to its exact caller selector and input position."""

    path: ConnectorInputPath
    selector_digest: str
    grant_id: str
    upload: PreparedUpload

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, tuple)
            or not self.path
            or any(
                (not isinstance(part, str) or not part or "\x00" in part)
                if not isinstance(part, int) or isinstance(part, bool)
                else part < 0
                for part in self.path
            )
        ):
            raise ValidationError("prepared upload input path is invalid")
        if (
            not isinstance(self.selector_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.selector_digest) is None
        ):
            raise ValidationError("prepared upload selector digest is invalid")
        if (
            not isinstance(self.grant_id, str)
            or not self.grant_id
            or len(self.grant_id) > 128
            or "\x00" in self.grant_id
        ):
            raise ValidationError("prepared upload grant binding is invalid")
        if not isinstance(self.upload, PreparedUpload):
            raise ValidationError("prepared upload binding has no immutable snapshot")

    def __repr__(self) -> str:
        return (
            "PreparedUploadBinding(path=<redacted>, selector_digest=<redacted>, "
            "grant_id=<redacted>, upload=<redacted>)"
        )


class PreparedUploadReservation:
    """One atomic aggregate-byte reservation released with its prepared snapshot."""

    def __init__(self, cache: PreparedUploadCache, size: int) -> None:
        self._cache = cache
        self.size = size
        self._released = False
        self._lock = RLock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._cache._release_bytes(self.size)

    def _belongs_to(self, cache: PreparedUploadCache) -> bool:
        with self._lock:
            return self._cache is cache and not self._released


class PreparedUploadBundle:
    """A closeable set of snapshots for one exact connector mutation."""

    def __init__(
        self,
        bindings: Sequence[PreparedUploadBinding],
        *,
        reservations: Sequence[PreparedUploadReservation] = (),
    ) -> None:
        if (
            isinstance(bindings, (str, bytes, bytearray))
            or not isinstance(bindings, Sequence)
            or not bindings
            or len(bindings) > 128
            or any(not isinstance(binding, PreparedUploadBinding) for binding in bindings)
        ):
            raise ValidationError("prepared upload bundle is invalid")
        exact = tuple(bindings)
        if len({binding.path for binding in exact}) != len(exact):
            raise ValidationError("prepared upload bundle contains duplicate input paths")
        if (
            isinstance(reservations, (str, bytes, bytearray))
            or not isinstance(reservations, Sequence)
            or any(
                not isinstance(reservation, PreparedUploadReservation)
                for reservation in reservations
            )
        ):
            raise ValidationError("prepared upload reservations are invalid")
        self.bindings = exact
        self._reservations = tuple(reservations)
        self._closed = False
        self._lock = RLock()

    def __repr__(self) -> str:
        return (
            f"PreparedUploadBundle(bindings=<{len(self.bindings)} redacted>, closed={self._closed})"
        )

    @property
    def uploads(self) -> Mapping[ConnectorInputPath, PreparedUpload]:
        return {binding.path: binding.upload for binding in self.bindings}

    @property
    def size(self) -> int:
        return sum(binding.upload.size for binding in self.bindings)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def bind_authority(self, authority: LocalFileAuthorityUse) -> None:
        with self._lock:
            if self._closed:
                authority.close()
                raise ConflictError("prepared upload bundle was already invalidated")
            for binding in self.bindings:
                binding.upload.bind_authority(authority)

    def _ensure_reserved_by(self, cache: PreparedUploadCache) -> None:
        with self._lock:
            if self._closed:
                raise ConflictError("prepared upload bundle is closed")
            if self._reservations:
                if sum(reservation.size for reservation in self._reservations) != self.size or any(
                    not reservation._belongs_to(cache) for reservation in self._reservations
                ):
                    raise ValidationError("prepared upload byte reservation is invalid")
                return
            self._reservations = (cache.reserve(self.size),)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for binding in self.bindings:
                binding.upload.close()
            reservations = self._reservations
            self._reservations = ()
        for reservation in reservations:
            reservation.release()


@dataclass(repr=False)
class _PreparedUploadEntry:
    bundle: PreparedUploadBundle
    expires_at: float
    active: int = 0


class PreparedUploadCache:
    """Process-local ownership for snapshots awaiting a sealed confirmation."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        scheduler: Scheduler | None = None,
        max_entries: int = MAX_TRANSFER_HANDLES,
        max_snapshot_bytes: int = MAX_FILE_TRANSFER_BYTES,
    ) -> None:
        if clock is not None and not callable(clock):
            raise ValidationError("prepared upload cache clock is invalid")
        if scheduler is not None and not callable(scheduler):
            raise ValidationError("prepared upload cache scheduler is invalid")
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_TRANSFER_HANDLES:
            raise ValidationError("prepared upload cache capacity is invalid")
        if (
            type(max_snapshot_bytes) is not int
            or not 0 <= max_snapshot_bytes <= MAX_FILE_TRANSFER_BYTES
        ):
            raise ValidationError("prepared upload cache byte capacity is invalid")
        self._clock = clock or time.monotonic
        self._scheduler = scheduler or _schedule_timer
        self._max_entries = max_entries
        self._max_snapshot_bytes = max_snapshot_bytes
        self._entries: dict[str, _PreparedUploadEntry] = {}
        self._live_bytes = 0
        self._timer: ScheduledCall | None = None
        self._timer_generation = 0
        self._closed = False
        self._lock = RLock()

    def put(
        self,
        confirmation_token: object,
        bundle: PreparedUploadBundle,
        *,
        ttl_seconds: int = DEFAULT_TRANSFER_TTL_SECONDS,
    ) -> None:
        """Take ownership of snapshots under a hash of one confirmation token."""

        if not isinstance(bundle, PreparedUploadBundle):
            raise ValidationError("prepared upload cache value is invalid")
        bundle._ensure_reserved_by(self)
        key = _confirmation_key(confirmation_token)
        now = self._now()
        ttl = _ttl(ttl_seconds)
        with self._lock:
            self._require_open()
            self._prune_locked(now)
            if key in self._entries:
                raise ConflictError("prepared uploads already exist for this confirmation")
            if len(self._entries) >= self._max_entries:
                raise ConflictError(
                    "prepared upload capacity is full; wait for an approval to expire"
                )
            self._entries[key] = _PreparedUploadEntry(bundle=bundle, expires_at=now + ttl)
            self._reschedule_locked(now)

    def reserve(self, size: int) -> PreparedUploadReservation:
        """Atomically reserve aggregate snapshot bytes before copying source content."""

        if type(size) is not int or not 0 <= size <= MAX_FILE_TRANSFER_BYTES:
            raise ValidationError("prepared upload reservation size is invalid")
        with self._lock:
            self._require_open()
            now = self._now()
            self._prune_locked(now)
            if self._live_bytes + size > self._max_snapshot_bytes:
                raise ConflictError(
                    "prepared upload byte capacity is full; wait for an approval to expire"
                )
            self._live_bytes += size
            return PreparedUploadReservation(self, size)

    def peek(self, confirmation_token: object) -> PreparedUploadBundle | None:
        """Borrow a current bundle without consuming it so mismatches remain retryable."""

        key = _confirmation_key(confirmation_token)
        with self._lock:
            self._require_open()
            now = self._now()
            self._prune_locked(now)
            self._reschedule_locked(now)
            entry = self._entries.get(key)
            return None if entry is None else entry.bundle

    @contextmanager
    def hold(self, confirmation_token: object) -> Iterator[PreparedUploadBundle | None]:
        """Pin a current cache entry while a confirmation is rebound and consumed."""

        key = _confirmation_key(confirmation_token)
        with self._lock:
            self._require_open()
            now = self._now()
            self._prune_locked(now)
            entry = self._entries.get(key)
            if entry is not None:
                entry.active += 1
            self._reschedule_locked(now)
        try:
            yield None if entry is None else entry.bundle
        finally:
            if entry is not None:
                with self._lock:
                    current = self._entries.get(key)
                    if current is entry:
                        entry.active -= 1
                        now = self._now()
                        self._prune_locked(now)
                        self._reschedule_locked(now)

    def take(
        self,
        confirmation_token: object,
        *,
        expected: PreparedUploadBundle,
    ) -> PreparedUploadBundle:
        """Atomically transfer ownership after the sealed confirmation was spent."""

        key = _confirmation_key(confirmation_token)
        with self._lock:
            self._require_open()
            now = self._now()
            self._prune_locked(now)
            entry = self._entries.get(key)
            if entry is None or entry.bundle is not expected:
                self._reschedule_locked(now)
                raise ConflictError(
                    "prepared upload is unavailable or expired; request a fresh preview"
                )
            del self._entries[key]
            self._reschedule_locked(now)
            return entry.bundle

    def discard(self, confirmation_token: object) -> bool:
        """Close and remove a bundle whose authority can no longer be used."""

        key = _confirmation_key(confirmation_token)
        with self._lock:
            self._require_open()
            entry = self._entries.pop(key, None)
            self._reschedule_locked(self._now())
        if entry is None:
            return False
        entry.bundle.close()
        return True

    def prune(self) -> int:
        with self._lock:
            self._require_open()
            now = self._now()
            removed = self._prune_locked(now)
            self._reschedule_locked(now)
            return removed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            entries = tuple(self._entries.values())
            self._entries.clear()
            self._timer_generation += 1
            timer = self._timer
            self._timer = None
            self._closed = True
        if timer is not None:
            timer.cancel()
        for entry in entries:
            entry.bundle.close()

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise ValidationError("prepared upload cache clock is unavailable") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("prepared upload cache clock returned an invalid time")
        result = float(value)
        if not math.isfinite(result):
            raise ValidationError("prepared upload cache clock returned a non-finite time")
        return result

    def _require_open(self) -> None:
        if self._closed:
            raise ValidationError("prepared upload cache is closed")

    def _prune_locked(self, now: float) -> int:
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.bundle.closed or (entry.active == 0 and entry.expires_at <= now)
        ]
        entries = [self._entries.pop(key) for key in expired]
        for entry in entries:
            entry.bundle.close()
        return len(entries)

    def _release_bytes(self, size: int) -> None:
        with self._lock:
            self._live_bytes -= size
            if self._live_bytes < 0:
                self._live_bytes = 0

    def _reschedule_locked(self, now: float) -> None:
        self._timer_generation += 1
        generation = self._timer_generation
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()
        if not self._entries or self._closed:
            return
        expiries = [
            entry.expires_at
            for entry in self._entries.values()
            if entry.active == 0 and not entry.bundle.closed
        ]
        if not expiries:
            return
        expires_at = min(expiries)
        self._timer = self._scheduler(
            max(0.0, expires_at - now),
            lambda: self._expire_scheduled(generation),
        )

    def _expire_scheduled(self, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._timer_generation:
                return
            self._timer = None
            try:
                now = self._now()
            except ValidationError:
                return
            self._prune_locked(now)
            self._reschedule_locked(now)


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
            if os.name != "nt":
                _POSIX_OS.fchmod(self._descriptor, 0o600)
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
        scheduler: Scheduler | None = None,
    ) -> None:
        if not _SECURE_ARTIFACT_STORE_SUPPORTED:
            raise ValidationError("secure connector artifact storage is unavailable on this host")
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_ARTIFACT_BYTES:
            raise ValidationError("artifact store size bound is invalid")
        if clock is not None and not callable(clock):
            raise ValidationError("artifact store clock is invalid")
        if scheduler is not None and not callable(scheduler):
            raise ValidationError("artifact store scheduler is invalid")
        self.root = (
            Path(root) if root is not None else data_dir() / "connector-artifacts"
        ).expanduser()
        if not self.root.is_absolute():
            raise ValidationError("artifact store requires an absolute root")
        self.max_bytes = max_bytes
        self._clock = clock or time.time
        self._scheduler = scheduler or _schedule_timer
        self._lock = RLock()
        self._pending: dict[str, _PendingArtifact] = {}
        self._completed: dict[str, float] = {}
        self._active_stages: set[str] = set()
        self._timer: ScheduledCall | None = None
        self._timer_generation = 0
        self._closed = False
        self._root_fd = -1
        self._root_identity: tuple[int, int] | None = None
        self._open_root()
        self.cleanup()

    @staticmethod
    def supported() -> bool:
        """Report whether this host can enforce private atomic artifact storage."""

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
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | _BINARY_OPEN
                    | _NOINHERIT_OPEN
                )
                if os.name == "nt":
                    descriptor = os.open(self.root / part_name, flags, 0o600)
                else:
                    descriptor = os.open(part_name, flags, 0o600, dir_fd=self._root_fd)
            except OSError as exc:
                raise ValidationError("artifact temporary file could not be created") from exc
            self._pending[pending_id] = pending
            self._reschedule_locked(now)
            return ArtifactWriter(self, pending_id, pending, descriptor, expected_size)

    def prepare_upload(
        self,
        reference: LocalFileRef | LocalFileTransferCandidate,
        *,
        authority: LocalFileAuthorityUse | None = None,
        filename: str | None = None,
        media_type: str | None = None,
        max_bytes: int | None = None,
    ) -> PreparedUpload:
        """Copy a granted file into an anonymous immutable snapshot before preview."""

        if not isinstance(reference, (LocalFileRef, LocalFileTransferCandidate)):
            raise ValidationError("prepared upload requires an inspected local file")
        if isinstance(reference, LocalFileTransferCandidate) and authority is None:
            raise ValidationError("prepared upload candidate requires transfer authority")
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
                flags = (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | _BINARY_OPEN
                    | _NOINHERIT_OPEN
                )
                if os.name == "nt":
                    descriptor = os.open(self.root / stage_name, flags, 0o600)
                else:
                    root_descriptor = os.dup(self._root_fd)
                    descriptor = os.open(stage_name, flags, 0o600, dir_fd=root_descriptor)
            except OSError as exc:
                if root_descriptor >= 0:
                    with suppress(OSError):
                        os.close(root_descriptor)
                raise ValidationError("prepared upload snapshot could not be created") from exc
            self._active_stages.add(stage_name)
        try:
            if isinstance(reference, LocalFileTransferCandidate):
                if authority is None:
                    raise ValidationError("prepared upload candidate requires transfer authority")
                chunks = reference.iter_chunks(
                    chunk_size=ARTIFACT_CHUNK_BYTES,
                    authority=authority,
                )
            else:
                chunks = reference.iter_chunks(
                    chunk_size=ARTIFACT_CHUNK_BYTES,
                    authority=authority,
                )
            for block in chunks:
                total += len(block)
                if total > bound:
                    raise ValidationError("local file exceeds this provider operation's size limit")
                _write_all(descriptor, block)
                digest.update(block)
            if total != reference.size or (
                isinstance(reference, LocalFileRef) and digest.hexdigest() != reference.sha256
            ):
                raise ValidationError("local file changed while its upload was prepared")
            os.fsync(descriptor)
            if os.name != "nt":
                _POSIX_OS.fchmod(descriptor, 0o600)
            os.close(descriptor)
            descriptor = -1
            read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | _BINARY_OPEN | _NOINHERIT_OPEN
            if os.name == "nt":
                read_descriptor = os.open(
                    self.root / stage_name,
                    read_flags | _TEMPORARY_OPEN,
                )
            else:
                read_descriptor = os.open(stage_name, read_flags, dir_fd=root_descriptor)
            metadata = os.fstat(read_descriptor)
            if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_size) != total:
                raise ValidationError("prepared upload snapshot is invalid")
            if os.name != "nt":
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
                if os.name == "nt":
                    os.unlink(self.root / stage_name)
                else:
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
            removed += self._prune_orphan_parts_locked(now)
            self._reschedule_locked(now)
            return removed

    def discard(self, receipt: ArtifactReceipt) -> None:
        """Delete one exact completed receipt still owned by this store."""

        if not isinstance(receipt, ArtifactReceipt):
            raise ValidationError("artifact receipt is invalid")
        final_name = receipt.path.name
        match = _FINAL_NAME.fullmatch(final_name)
        now = self._now()
        with self._lock:
            self._ensure_root()
            if (
                match is None
                or receipt.path != self.root / final_name
                or match.group(1) != receipt.artifact_id
                or float(int(match.group(2))) != receipt.expires_at
                or self._completed.get(final_name) != receipt.expires_at
            ):
                raise ValidationError("artifact receipt does not belong to this store")
            try:
                metadata = self._entry_stat(final_name)
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise ValidationError("artifact receipt does not name a regular file")
                self._entry_unlink(final_name)
                if os.name != "nt":
                    os.fsync(self._root_fd)
            except FileNotFoundError as exc:
                raise ValidationError("artifact receipt is no longer available") from exc
            except OSError as exc:
                raise ValidationError("artifact receipt could not be discarded") from exc
            self._completed.pop(final_name, None)
            self._reschedule_locked(now)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._timer_generation += 1
            timer = self._timer
            self._timer = None
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
        if timer is not None:
            timer.cancel()

    def _open_root(self) -> None:
        if not os.path.lexists(self.root):
            self.root.mkdir(parents=True, mode=0o700)
        if os.name == "nt":
            try:
                _harden_windows_directory(self.root)
            except OSError as exc:
                raise ValidationError("artifact store root could not be made private") from exc
        else:
            self.root.chmod(0o700)
        try:
            metadata = os.lstat(self.root)
        except OSError as exc:
            raise ValidationError("artifact store root is unavailable") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or _is_windows_reparse_point(metadata)
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077)
        ):
            raise ValidationError("artifact store root must be an owner-only directory")
        if os.name == "nt":
            self._root_identity = (int(metadata.st_dev), int(metadata.st_ino))
            return
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
        if self._closed or self._root_identity is None or (os.name != "nt" and self._root_fd < 0):
            raise ValidationError("artifact store is closed")
        try:
            listed = os.lstat(self.root)
            opened = listed if os.name == "nt" else os.fstat(self._root_fd)
        except OSError as exc:
            raise ValidationError("artifact store root changed") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(listed.st_mode)
            or stat.S_ISLNK(listed.st_mode)
            or _is_windows_reparse_point(listed)
            or (int(opened.st_dev), int(opened.st_ino)) != self._root_identity
            or (int(listed.st_dev), int(listed.st_ino)) != self._root_identity
            or (os.name != "nt" and stat.S_IMODE(listed.st_mode) & 0o077)
        ):
            raise ValidationError("artifact store root changed")

    def _publish(self, pending: _PendingArtifact) -> float:
        with self._lock:
            self._ensure_root()
            now = self._now()
            expires_at = float(int(now + pending.ttl_seconds))
            final_name = f"{pending.artifact_id}--{int(expires_at)}--{pending.filename}"
            if _FINAL_NAME.fullmatch(final_name) is None:
                raise ValidationError("artifact destination name is invalid")
            try:
                existing = self._entry_stat(final_name)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise ValidationError("artifact destination could not be inspected") from exc
            if existing is not None:
                raise ConflictError("artifact destination already exists")
            try:
                if os.name == "nt":
                    os.rename(self.root / pending.part_name, self.root / final_name)
                else:
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
            self._reschedule_locked(now)
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
            self._entry_unlink(part_name)

    def _prune_orphan_parts_locked(self, now: float) -> int:
        tracked = {pending.part_name for pending in self._pending.values()} | self._active_stages
        removed = 0
        try:
            names = self._entry_names()
        except OSError as exc:
            raise ValidationError("artifact store entries could not be inspected") from exc
        for name in names:
            if not isinstance(name, str) or name in tracked:
                continue
            if _PART_NAME.fullmatch(name) is None and _STAGE_NAME.fullmatch(name) is None:
                continue
            try:
                metadata = self._entry_stat(name)
                max_age = (
                    DEFAULT_TRANSFER_TTL_SECONDS
                    if _STAGE_NAME.fullmatch(name) is not None
                    else MAX_ARTIFACT_TTL_SECONDS
                )
                if stat.S_ISREG(metadata.st_mode) and float(metadata.st_mtime) + max_age <= now:
                    self._entry_unlink(name)
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
        current: set[str] = set()
        try:
            names = self._entry_names()
        except OSError as exc:
            raise ValidationError("artifact store entries could not be inspected") from exc
        for name in names:
            if not isinstance(name, str):
                continue
            match = _FINAL_NAME.fullmatch(name)
            if match is None:
                continue
            expires_at = float(int(match.group(2)))
            if expires_at > now:
                self._completed[name] = expires_at
                current.add(name)
                continue
            try:
                metadata = self._entry_stat(name)
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise ValidationError("expired artifact is not a regular file")
                self._entry_unlink(name)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ValidationError("expired artifact cleanup failed") from exc
            self._completed.pop(name, None)
            removed += 1
        for name in tuple(self._completed):
            if name not in current:
                self._completed.pop(name, None)
        return removed

    def _entry_names(self) -> list[str]:
        return os.listdir(self.root if os.name == "nt" else self._root_fd)

    def _entry_stat(self, name: str) -> os.stat_result:
        if os.name == "nt":
            return os.lstat(self.root / name)
        return os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)

    def _entry_unlink(self, name: str) -> None:
        if os.name == "nt":
            os.unlink(self.root / name)
        else:
            os.unlink(name, dir_fd=self._root_fd)

    def _reschedule_locked(self, now: float) -> None:
        self._timer_generation += 1
        generation = self._timer_generation
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()
        if self._closed or not self._completed:
            return
        expires_at = min(self._completed.values())
        self._timer = self._scheduler(
            max(0.0, expires_at - now),
            lambda: self._expire_scheduled(generation),
        )

    def _expire_scheduled(self, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._timer_generation:
                return
            self._timer = None
            try:
                now = self._now()
                self._ensure_root()
                self._prune_locked(now)
                self._reschedule_locked(now)
            except ValidationError:
                return

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


class ScopedArtifactWriter:
    """Artifact writer whose completed receipt is owned by one adapter call."""

    def __init__(self, scope: ConnectorArtifactScope, writer: ArtifactWriter) -> None:
        self._scope = scope
        self._writer = writer
        self._closed = False

    @property
    def size(self) -> int:
        return self._writer.size

    def write(self, content: bytes) -> int:
        return self._writer.write(content)

    def finish(self) -> ArtifactReceipt:
        if self._closed:
            raise ValidationError("scoped artifact writer is closed")
        receipt = self._writer.finish()
        self._closed = True
        self._scope._finished(self, receipt)
        return receipt

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.abort()
        self._scope._aborted(self)

    def __enter__(self) -> ScopedArtifactWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc, traceback
        if exc_type is None:
            self.finish()
        else:
            self.abort()


class ConnectorArtifactScope:
    """Per-call facade that retains only the exact receipt returned by its adapter."""

    def __init__(self, store: ArtifactStore) -> None:
        if not isinstance(store, ArtifactStore):
            raise ValidationError("connector artifact scope requires an artifact store")
        self._store = store
        self._writers: set[ScopedArtifactWriter] = set()
        self._receipts: dict[int, ArtifactReceipt] = {}
        self._closed = False
        self._lock = RLock()

    def start(
        self,
        name: str,
        *,
        media_type: str | None = None,
        expected_size: int | None = None,
        ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
    ) -> ScopedArtifactWriter:
        with self._lock:
            if self._closed:
                raise ValidationError("connector artifact scope is closed")
            writer = ScopedArtifactWriter(
                self,
                self._store.start(
                    name,
                    media_type=media_type,
                    expected_size=expected_size,
                    ttl_seconds=ttl_seconds,
                ),
            )
            self._writers.add(writer)
            return writer

    def complete(self, returned: ArtifactReceipt | None) -> None:
        """Close the call, rejecting foreign receipts and deleting all unreturned output."""

        with self._lock:
            if self._closed:
                raise ValidationError("connector artifact scope is closed")
            owned = returned is None or self._receipts.get(id(returned)) is returned
            writers = tuple(self._writers)
            receipts = tuple(self._receipts.values())
            self._writers.clear()
            self._receipts.clear()
            self._closed = True
        for writer in writers:
            writer.abort()
        for receipt in receipts:
            if receipt is not returned:
                self._store.discard(receipt)
        if not owned:
            raise ValidationError("connector adapter returned a foreign artifact receipt")

    def close(self) -> None:
        if self._closed:
            return
        self.complete(None)

    def _finished(self, writer: ScopedArtifactWriter, receipt: ArtifactReceipt) -> None:
        with self._lock:
            if self._closed or writer not in self._writers:
                self._store.discard(receipt)
                raise ValidationError("connector artifact scope closed before completion")
            self._writers.remove(writer)
            self._receipts[id(receipt)] = receipt

    def _aborted(self, writer: ScopedArtifactWriter) -> None:
        with self._lock:
            self._writers.discard(writer)


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


def _confirmation_key(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 8_192 or not value.isascii():
        raise ValidationError("prepared upload confirmation token is invalid")
    return hashlib.sha256(b"seld-prepared-upload\0" + value.encode("ascii")).hexdigest()


def _schedule_timer(delay_seconds: float, callback: Callable[[], None]) -> ScheduledCall:
    timer = Timer(delay_seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


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


def _set_binary_mode(descriptor: int) -> None:
    if os.name != "nt":
        return
    try:
        import msvcrt

        cast(Any, msvcrt).setmode(descriptor, _BINARY_OPEN)
    except (ImportError, OSError) as exc:
        raise ValidationError("prepared upload could not enter binary mode") from exc


def _is_windows_reparse_point(metadata: os.stat_result) -> bool:
    return os.name == "nt" and bool(int(getattr(metadata, "st_file_attributes", 0)) & 0x400)


def _harden_windows_directory(path: Path) -> None:
    """Install a protected inheritable DACL for the current Windows user."""

    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    loader = cast(Any, getattr(ctypes, "WinDLL", None))
    win_error = cast(Any, getattr(ctypes, "WinError", OSError))
    get_last_error = cast(Any, getattr(ctypes, "get_last_error", lambda: 0))
    if loader is None:
        raise OSError("Windows security APIs are unavailable")
    advapi32 = loader("advapi32", use_last_error=True)
    kernel32 = loader("kernel32", use_last_error=True)

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_token_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    convert_sid.restype = wintypes.BOOL
    convert_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_descriptor.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert_descriptor.restype = wintypes.BOOL
    set_file_security = advapi32.SetFileSecurityW
    set_file_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    set_file_security.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not open_process_token(get_current_process(), 0x0008, ctypes.byref(token)):
            raise win_error(get_last_error())
        required = wintypes.DWORD()
        get_token_information(token, 1, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise win_error(get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token,
            1,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise win_error(get_last_error())
        user = _TokenUser.from_buffer(buffer)
        if not convert_sid(user.user.sid, ctypes.byref(sid_text)):
            raise win_error(get_last_error())
        sid = sid_text.value
        if not sid:
            raise OSError("Windows user SID is unavailable")
        sddl = f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        descriptor_size = wintypes.DWORD()
        if not convert_descriptor(
            sddl,
            1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            raise win_error(get_last_error())
        if not set_file_security(str(path), 0x00000004, descriptor):
            raise win_error(get_last_error())
    finally:
        if descriptor.value:
            local_free(descriptor)
        if sid_text:
            local_free(ctypes.cast(sid_text, ctypes.c_void_p))
        if token:
            close_handle(token)


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
