"""Host-local grants and bounded, privacy-screened file reads.

Grant roots live outside the portable vault in an owner-only host record. File
content is returned only to the current CLI or MCP caller and is never written
to the Seld vault or the grant record.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Condition, Event, RLock
from typing import Any, Final, cast

from continuity_kernel.atomic import atomic_write, exclusive_lock, read_regular_file, sha256_bytes
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ConflictError, NotFoundError, ValidationError
from continuity_kernel.privacy import (
    AwarenessDecision,
    assess_local_path,
    read_screened_local_content,
)
from continuity_kernel.records import format_time, stored_time
from continuity_kernel.source_recipes import get_recipe
from continuity_kernel.vault_identity import canonical_vault_id

LOCAL_FILE_SOURCE_ID: Final = "local_files"
LOCAL_FILE_READER_TOOL: Final = "gsv_local_file_read"
LOCAL_FILE_READ_CAPABILITY: Final = get_recipe(LOCAL_FILE_SOURCE_ID).read_capability
LOCAL_FILE_GRANT_FORMAT_VERSION: Final = 2
_LEGACY_LOCAL_FILE_GRANT_FORMAT_VERSION: Final = 1
MAX_LOCAL_PATH_BYTES: Final = 16 * 1024
MAX_LOCAL_GRANTS: Final = 128
MAX_LOCAL_GRANT_STORE_BYTES: Final = 512 * 1024
# Google Drive is the largest supported provider surface at 5 TiB.  Keep this
# independent from the 16 MiB JSON/control-plane limits: this lane is streamed
# from a descriptor-pinned file and is never materialized in a request object.
MAX_FILE_TRANSFER_BYTES: Final = 5 * 1024**4
FILE_TRANSFER_CHUNK_BYTES: Final = 1024 * 1024
_POSIX_OS = cast(Any, os)
_PINNED_FILE_TRANSFER_SUPPORTED: Final = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)
_GRANT_KEYS: Final = frozenset(
    {
        "created_at",
        "device",
        "grant_id",
        "inode",
        "root",
        "vault_device",
        "vault_id",
        "vault_inode",
        "vault_root",
    }
)
_LEGACY_GRANT_KEYS: Final = _GRANT_KEYS - {"vault_device", "vault_inode"}
_AUTHORITY_CANCEL_TIMEOUT_SECONDS: Final = 10.0
_AUTHORITY_LOCK = RLock()
_AUTHORITY_CONDITION = Condition(_AUTHORITY_LOCK)
_AUTHORITY_USES: dict[tuple[str, str, str], set[LocalFileAuthorityUse]] = {}


@dataclass(frozen=True)
class LocalFileGrant:
    grant_id: str
    vault_id: str
    vault_root: str
    vault_device: int
    vault_inode: int
    root: str
    device: int
    inode: int
    created_at: str

    def to_dict(self, *, current: bool | None = None) -> dict[str, Any]:
        """Return the minimal user-facing grant shape, never storage identity fields."""

        value: dict[str, Any] = {
            "created_at": self.created_at,
            "grant_id": self.grant_id,
            "selected_root": self.root,
        }
        if current is not None:
            value["current"] = current
        return value


@dataclass(frozen=True, repr=False)
class LocalFileTransferCandidate:
    """Metadata-only identity inspected before quota reservation and content reads."""

    grant_id: str
    relative_path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    _store: LocalFileGrantStore
    _root: str
    _relative: Path

    def __repr__(self) -> str:
        return (
            "LocalFileTransferCandidate(grant_id=<redacted>, relative_path=<redacted>, "
            f"device={self.device!r}, inode={self.inode!r}, size={self.size!r}, "
            f"modified_ns={self.modified_ns!r})"
        )

    def iter_chunks(
        self,
        *,
        authority: LocalFileAuthorityUse,
        chunk_size: int = FILE_TRANSFER_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        yield from self._store.iter_file_candidate(
            self,
            authority=authority,
            chunk_size=chunk_size,
        )


@dataclass(frozen=True, repr=False)
class LocalFileRef:
    """An internal, revalidatable reference to one granted regular file."""

    grant_id: str
    relative_path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str
    _store: LocalFileGrantStore
    _root: str
    _relative: Path

    def __repr__(self) -> str:
        return (
            "LocalFileRef(grant_id=<redacted>, relative_path=<redacted>, "
            f"device={self.device!r}, inode={self.inode!r}, size={self.size!r}, "
            f"modified_ns={self.modified_ns!r}, sha256=<redacted>)"
        )

    def revalidate(self) -> None:
        """Prove that the grant and the captured file identity still hold."""

        self._store.revalidate_file_ref(self)

    def iter_chunks(
        self,
        *,
        chunk_size: int = FILE_TRANSFER_CHUNK_BYTES,
        authority: LocalFileAuthorityUse | None = None,
    ) -> Iterator[bytes]:
        """Yield bounded chunks, optionally through one cancellable authority."""

        yield from self._store.iter_file_ref(
            self,
            chunk_size=chunk_size,
            authority=authority,
        )

    @property
    def mtime_ns(self) -> int:
        return self.modified_ns

    @property
    def content_length(self) -> int:
        return self.size


class LocalFileAuthorityUse:
    """One cancellable prepared-snapshot authority registered to exact grants."""

    def __init__(
        self,
        *,
        store: LocalFileGrantStore,
        grant_ids: tuple[str, ...],
        invalidator: Callable[[], None],
    ) -> None:
        self._store = store
        self._grant_ids = grant_ids
        self._invalidator = invalidator
        self._cancelled = Event()
        self._active_chunks = 0
        self._closed = False

    @contextmanager
    def chunk(self) -> Iterator[None]:
        """Authorize one local source read while holding the grant lock only for that read."""

        with _AUTHORITY_CONDITION:
            if self._closed or self._cancelled.is_set():
                raise ConflictError("local-file upload authority was revoked")
            self._active_chunks += 1
        try:
            with self._store._authority_chunk(self._grant_ids):
                if self._cancelled.is_set():
                    raise ConflictError("local-file upload authority was revoked")
                yield
        finally:
            with _AUTHORITY_CONDITION:
                self._active_chunks -= 1
                _AUTHORITY_CONDITION.notify_all()

    @contextmanager
    def delivery(self) -> Iterator[None]:
        """Count one delivered snapshot chunk without holding the grant-store lock."""

        with _AUTHORITY_CONDITION:
            if self._closed or self._cancelled.is_set():
                raise ConflictError("local-file upload authority was revoked")
            self._active_chunks += 1
        try:
            if self._cancelled.is_set():
                raise ConflictError("local-file upload authority was revoked")
            yield
        finally:
            with _AUTHORITY_CONDITION:
                self._active_chunks -= 1
                _AUTHORITY_CONDITION.notify_all()

    def close(self) -> None:
        with _AUTHORITY_CONDITION:
            if self._closed:
                return
            self._closed = True
            self._cancelled.set()
            for key in self._keys:
                uses = _AUTHORITY_USES.get(key)
                if uses is None:
                    continue
                uses.discard(self)
                if not uses:
                    _AUTHORITY_USES.pop(key, None)
            _AUTHORITY_CONDITION.notify_all()

    @property
    def _keys(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (self._store.vault_id, self._store.vault_root, grant_id) for grant_id in self._grant_ids
        )

    def _cancel(self) -> None:
        self._cancelled.set()
        self._invalidator()

    def _wait_quiescent(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with _AUTHORITY_CONDITION:
            while self._active_chunks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _AUTHORITY_CONDITION.wait(remaining)
            return True

    def _authorizes(self, store: LocalFileGrantStore, grant_id: str) -> bool:
        return self._store is store and grant_id in self._grant_ids


class LocalFileGrantStore:
    """Owner-only host grants bound to one exact logical vault and path."""

    def __init__(self, *, vault_root: Path | str, vault_id: str):
        root = Path(vault_root).expanduser()
        if not root.is_absolute():
            raise ValidationError("local-file grants require an absolute vault root")
        canonical_root, vault_identity = _real_directory(root, "vault root")
        self.vault_root = str(canonical_root)
        self.vault_device, self.vault_inode = vault_identity
        self.vault_id = canonical_vault_id(vault_id)
        self.storage_root = data_dir() / "local-file-authority"
        self.path = self.storage_root / "local-file-grants.json"
        self.lock_path = self.storage_root / "locks/local-file-grants.lock"

    def create(self, selected_root: Path | str) -> dict[str, Any]:
        root, identity = _grant_root(selected_root)
        with self._locked():
            grants = list(self._load())
            for grant in grants:
                if (
                    self._belongs(grant)
                    and grant.root == str(root)
                    and (grant.device, grant.inode) == identity
                ):
                    return {"created": False, "grant": grant.to_dict(current=True)}
            grants = [
                grant for grant in grants if not (self._belongs(grant) and grant.root == str(root))
            ]
            if len(grants) >= MAX_LOCAL_GRANTS:
                raise ValidationError("local-file grant limit reached; revoke an unused root")
            grant = LocalFileGrant(
                grant_id=str(uuid.uuid4()),
                vault_id=self.vault_id,
                vault_root=self.vault_root,
                vault_device=self.vault_device,
                vault_inode=self.vault_inode,
                root=str(root),
                device=identity[0],
                inode=identity[1],
                created_at=format_time(datetime.now(UTC)),
            )
            grants.append(grant)
            self._save(tuple(grants))
            return {"created": True, "grant": grant.to_dict(current=True)}

    def list(self) -> dict[str, Any]:
        with self._locked():
            grants = tuple(grant for grant in self._load() if self._belongs(grant))
            return {
                "grants": [
                    grant.to_dict(current=_root_matches(grant))
                    for grant in sorted(grants, key=lambda item: (item.root, item.grant_id))
                ],
            }

    def source_binding(self, *, require_current_grant: bool = False) -> str:
        """Return a privacy-safe binding to this vault's exact current grant set."""

        with self._locked():
            grants = tuple(
                sorted(
                    (grant for grant in self._load() if self._belongs(grant)),
                    key=lambda item: item.grant_id,
                )
            )
            entries = [
                {
                    "current": _root_matches(grant),
                    "device": grant.device,
                    "grant_id": grant.grant_id,
                    "inode": grant.inode,
                    "root": grant.root,
                }
                for grant in grants
            ]
            if require_current_grant and not any(entry["current"] for entry in entries):
                raise ValidationError(
                    "successful local_files coverage requires one current host-local grant"
                )
            encoded = json.dumps(
                {
                    "grants": entries,
                    "vault_id": self.vault_id,
                    "vault_device": self.vault_device,
                    "vault_inode": self.vault_inode,
                    "vault_root": self.vault_root,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return f"{LOCAL_FILE_READER_TOOL}:grant-set:{sha256_bytes(encoded)}"

    def revoke(self, grant_id: str) -> dict[str, Any]:
        clean_id = _grant_id(grant_id)
        cancelled: tuple[LocalFileAuthorityUse, ...] = ()
        with self._locked():
            grants = list(self._load())
            matched = next(
                (grant for grant in grants if grant.grant_id == clean_id and self._belongs(grant)),
                None,
            )
            if matched is None:
                raise NotFoundError("local-file grant not found for this vault")
            self._save(tuple(grant for grant in grants if grant.grant_id != clean_id))
            cancelled = self._cancel_authority((clean_id,))
        _await_authority_cancellation(cancelled)
        return {"grant": matched.to_dict(current=_root_matches(matched)), "revoked": True}

    def revoke_all(self) -> dict[str, Any]:
        """Remove every host grant for this exact vault identity and path."""

        cancelled: tuple[LocalFileAuthorityUse, ...] = ()
        with self._locked():
            grants = list(self._load())
            revoked_ids = tuple(grant.grant_id for grant in grants if self._belongs(grant))
            retained = tuple(grant for grant in grants if not self._belongs(grant))
            revoked = len(grants) - len(retained)
            if revoked:
                self._save(retained)
                cancelled = self._cancel_authority(revoked_ids)
        _await_authority_cancellation(cancelled)
        return {"revoked": revoked}

    def read(self, *, grant_id: str, relative_path: str) -> dict[str, Any]:
        """Read while holding the grant lock so completed revocation is final."""

        clean_id = _grant_id(grant_id)
        relative = _relative_path(relative_path)
        with self._locked():
            grant = next(
                (
                    item
                    for item in self._load()
                    if item.grant_id == clean_id and self._belongs(item)
                ),
                None,
            )
            if grant is None:
                raise NotFoundError("local-file grant not found for this vault")
            if not _root_matches(grant):
                raise ValidationError(
                    "local-file grant root changed; revoke it and grant the root again"
                )
            return _read_granted_file(grant=grant, relative=relative)

    def inspect_file_candidate(
        self,
        grant_id: str,
        relative_path: str,
        *,
        authority: LocalFileAuthorityUse,
        max_bytes: int,
    ) -> LocalFileTransferCandidate:
        """Inspect stable file metadata without reading source content."""

        clean_id = _grant_id(grant_id)
        relative = _relative_path(relative_path)
        bound = _file_transfer_bound(max_bytes)
        self._require_transfer_authority(authority, clean_id)
        with authority.chunk():
            grant = self._grant_for_file_ref(clean_id)
            with _open_granted_file(grant, relative) as descriptor:
                metadata = _stable_file_metadata(os.fstat(descriptor))
                if metadata[2] > bound:
                    raise ValidationError("local file exceeds this provider operation's size limit")
        return LocalFileTransferCandidate(
            grant_id=clean_id,
            relative_path=relative.as_posix(),
            device=metadata[0],
            inode=metadata[1],
            size=metadata[2],
            modified_ns=metadata[3],
            _store=self,
            _root=grant.root,
            _relative=relative,
        )

    def resolve_file_ref(
        self,
        grant_id: str,
        relative_path: str,
        *,
        max_bytes: int = MAX_FILE_TRANSFER_BYTES,
        authority: LocalFileAuthorityUse | None = None,
    ) -> LocalFileRef:
        """Resolve one internal binary reference through the current grant set."""

        clean_id = _grant_id(grant_id)
        relative = _relative_path(relative_path)
        _file_transfer_bound(max_bytes)
        if authority is None:
            with self._locked():
                grant = self._grant_for_file_ref(clean_id)
                with _open_granted_file(grant, relative) as descriptor:
                    metadata = _stable_file_metadata(os.fstat(descriptor))
                    if metadata[2] > max_bytes:
                        raise ValidationError("local file exceeds its transfer size bound")
                    digest = _hash_descriptor(
                        descriptor,
                        label="local file",
                        max_bytes=max_bytes,
                    )
                    current = _stable_file_metadata(os.fstat(descriptor))
                    if current != metadata:
                        raise ValidationError("local file changed while its reference was created")
        else:
            self._require_transfer_authority(authority, clean_id)
            with authority.chunk():
                grant = self._grant_for_file_ref(clean_id)
            with _open_granted_file(grant, relative) as descriptor:
                metadata = _stable_file_metadata(os.fstat(descriptor))
                if metadata[2] > max_bytes:
                    raise ValidationError("local file exceeds its transfer size bound")
                digest = _hash_descriptor_authorized(
                    descriptor,
                    authority=authority,
                    label="local file",
                    max_bytes=max_bytes,
                )
                with authority.chunk():
                    current = _stable_file_metadata(os.fstat(descriptor))
                    if current != metadata:
                        raise ValidationError("local file changed while its reference was created")
        return LocalFileRef(
            grant_id=clean_id,
            relative_path=relative.as_posix(),
            device=metadata[0],
            inode=metadata[1],
            size=metadata[2],
            modified_ns=metadata[3],
            sha256=digest,
            _store=self,
            _root=grant.root,
            _relative=relative,
        )

    def assert_transfer_authorized(self, grant_id: str, relative_path: str) -> None:
        """Recheck grant authority without reopening or replacing a prepared snapshot."""

        clean_id = _grant_id(grant_id)
        _relative_path(relative_path)
        with self._locked():
            self._grant_for_file_ref(clean_id)

    def register_transfer_authority(
        self,
        grant_ids: Sequence[str],
        *,
        invalidator: Callable[[], None],
    ) -> LocalFileAuthorityUse:
        """Atomically check grants and register cancellable prepared-snapshot use."""

        if (
            isinstance(grant_ids, (str, bytes, bytearray))
            or not isinstance(grant_ids, Sequence)
            or not grant_ids
            or len(grant_ids) > MAX_LOCAL_GRANTS
        ):
            raise ValidationError("local-file authority lease is invalid")
        clean_ids = tuple(_grant_id(grant_id) for grant_id in grant_ids)
        if len(set(clean_ids)) != len(clean_ids):
            raise ValidationError("local-file authority lease contains duplicate grants")
        if not callable(invalidator):
            raise ValidationError("local-file authority invalidator is invalid")
        with self._locked():
            for grant_id in clean_ids:
                self._grant_for_file_ref(grant_id)
            use = LocalFileAuthorityUse(
                store=self,
                grant_ids=clean_ids,
                invalidator=invalidator,
            )
            with _AUTHORITY_CONDITION:
                for key in use._keys:
                    _AUTHORITY_USES.setdefault(key, set()).add(use)
            return use

    def revalidate_file_ref(self, reference: LocalFileRef) -> None:
        """Recheck grant, path, identity, size, timestamp, and content digest."""

        if not isinstance(reference, LocalFileRef) or reference._store is not self:
            raise ValidationError("local file reference belongs to another grant store")
        relative = _relative_path(reference.relative_path)
        if relative != reference._relative or reference._root == "":
            raise ValidationError("local file reference is invalid")
        with self._locked():
            grant = self._grant_for_file_ref(reference.grant_id)
            if grant.root != reference._root:
                raise ValidationError("local file reference grant root changed")
            with _open_granted_file(grant, relative) as descriptor:
                metadata = _stable_file_metadata(os.fstat(descriptor))
                _match_file_ref(reference, metadata)
                digest = _hash_descriptor(
                    descriptor,
                    label="local file",
                    max_bytes=reference.size,
                )
                if digest != reference.sha256:
                    raise ValidationError("local file reference content changed")

    def iter_file_ref(
        self,
        reference: LocalFileRef,
        *,
        chunk_size: int = FILE_TRANSFER_CHUNK_BYTES,
        authority: LocalFileAuthorityUse | None = None,
    ) -> Iterator[bytes]:
        """Stream a reference without materializing its content in memory."""

        if not isinstance(reference, LocalFileRef) or reference._store is not self:
            raise ValidationError("local file reference belongs to another grant store")
        if type(chunk_size) is not int or not 1 <= chunk_size <= FILE_TRANSFER_CHUNK_BYTES:
            raise ValidationError("local file transfer chunk size is invalid")
        relative = _relative_path(reference.relative_path)
        if authority is None:
            with self._locked():
                grant = self._grant_for_file_ref(reference.grant_id)
                if grant.root != reference._root:
                    raise ValidationError("local file reference grant root changed")
            yield from _stream_file_ref(
                reference,
                grant=grant,
                relative=relative,
                chunk_size=chunk_size,
            )
            return

        self._require_transfer_authority(authority, reference.grant_id)
        with authority.chunk():
            grant = self._grant_for_file_ref(reference.grant_id)
            if grant.root != reference._root:
                raise ValidationError("local file reference grant root changed")
        yield from _stream_file_ref(
            reference,
            grant=grant,
            relative=relative,
            chunk_size=chunk_size,
            authority=authority,
        )

    def iter_file_candidate(
        self,
        candidate: LocalFileTransferCandidate,
        *,
        authority: LocalFileAuthorityUse,
        chunk_size: int = FILE_TRANSFER_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        """Read a metadata-inspected candidate once under chunk-bound authority."""

        if not isinstance(candidate, LocalFileTransferCandidate) or candidate._store is not self:
            raise ValidationError("local file candidate belongs to another grant store")
        if type(chunk_size) is not int or not 1 <= chunk_size <= FILE_TRANSFER_CHUNK_BYTES:
            raise ValidationError("local file transfer chunk size is invalid")
        self._require_transfer_authority(authority, candidate.grant_id)
        relative = _relative_path(candidate.relative_path)
        if relative != candidate._relative:
            raise ValidationError("local file candidate is invalid")
        with authority.chunk():
            grant = self._grant_for_file_ref(candidate.grant_id)
            if grant.root != candidate._root:
                raise ValidationError("local file candidate grant root changed")
        with _open_granted_file(grant, relative) as descriptor:
            metadata = _stable_file_metadata(os.fstat(descriptor))
            _match_file_candidate(candidate, metadata)
            total = 0
            while True:
                with authority.chunk():
                    block = os.read(descriptor, chunk_size)
                if not block:
                    break
                total += len(block)
                if total > candidate.size:
                    raise ValidationError("local file grew beyond its inspected identity")
                yield block
            with authority.chunk():
                current = _stable_file_metadata(os.fstat(descriptor))
                _match_file_candidate(candidate, current)
            if total != candidate.size:
                raise ValidationError("local file changed while its upload was prepared")

    def _require_transfer_authority(
        self,
        authority: LocalFileAuthorityUse,
        grant_id: str,
    ) -> None:
        if not isinstance(authority, LocalFileAuthorityUse) or not authority._authorizes(
            self,
            grant_id,
        ):
            raise ValidationError("local-file transfer authority does not match its reference")

    def _grant_for_file_ref(self, grant_id: str) -> LocalFileGrant:
        grant = next(
            (item for item in self._load() if item.grant_id == grant_id and self._belongs(item)),
            None,
        )
        if grant is None:
            raise NotFoundError("local-file grant not found for this vault")
        if not _root_matches(grant):
            raise ValidationError(
                "local-file grant root changed; revoke it and grant the root again"
            )
        return grant

    @contextmanager
    def _authority_chunk(self, grant_ids: tuple[str, ...]) -> Iterator[None]:
        with self._locked():
            for grant_id in grant_ids:
                self._grant_for_file_ref(grant_id)
            yield

    def _cancel_authority(
        self,
        grant_ids: tuple[str, ...],
    ) -> tuple[LocalFileAuthorityUse, ...]:
        keys = {(self.vault_id, self.vault_root, grant_id) for grant_id in grant_ids}
        with _AUTHORITY_CONDITION:
            uses = tuple({use for key in keys for use in _AUTHORITY_USES.get(key, set())})
            for use in uses:
                use._cancelled.set()
        for use in uses:
            with suppress(Exception):
                use._invalidator()
        return uses

    def _belongs(self, grant: LocalFileGrant) -> bool:
        return (
            grant.vault_id == self.vault_id
            and grant.vault_root == self.vault_root
            and (grant.vault_device, grant.vault_inode) == (self.vault_device, self.vault_inode)
            and _directory_matches(
                self.vault_root,
                expected=(self.vault_device, self.vault_inode),
            )
        )

    def _locked(self) -> Any:
        _prepare_private_storage(self.storage_root)
        return exclusive_lock(self.lock_path)

    def _load(self) -> tuple[LocalFileGrant, ...]:
        if not os.path.lexists(self.path):
            return ()
        _validate_private_file(self.path)
        encoded = read_regular_file(
            self.path,
            label="Seld local-file grants",
            max_bytes=MAX_LOCAL_GRANT_STORE_BYTES,
        )
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Seld local-file grant store is invalid") from exc
        format_version = payload.get("format_version") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"format_version", "grants"}
            or format_version
            not in {
                _LEGACY_LOCAL_FILE_GRANT_FORMAT_VERSION,
                LOCAL_FILE_GRANT_FORMAT_VERSION,
            }
            or not isinstance(payload.get("grants"), list)
            or len(payload["grants"]) > MAX_LOCAL_GRANTS
        ):
            raise ValidationError("Seld local-file grant store has an unsupported shape")
        if format_version == _LEGACY_LOCAL_FILE_GRANT_FORMAT_VERSION:
            # Version 1 did not bind authority to the vault directory object. Validate
            # its shape, then expose no authority; the next explicit grant rewrites
            # the host-local store in version 2.
            for value in payload["grants"]:
                _validate_legacy_grant(value)
            return ()
        grants = tuple(_grant_from_value(value) for value in payload["grants"])
        if len({grant.grant_id for grant in grants}) != len(grants):
            raise ValidationError("Seld local-file grant store contains duplicate IDs")
        return grants

    def _save(self, grants: tuple[LocalFileGrant, ...]) -> None:
        payload = {
            "format_version": LOCAL_FILE_GRANT_FORMAT_VERSION,
            "grants": [asdict(grant) for grant in sorted(grants, key=lambda item: item.grant_id)],
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_LOCAL_GRANT_STORE_BYTES:
            raise ValidationError("Seld local-file grant store exceeds its size bound")
        atomic_write(self.path, encoded, mode=0o600)
        _validate_private_file(self.path)


def validate_local_file_tool_binding(
    *,
    source_id: str,
    result: str,
    tool_binding: str | None,
) -> None:
    """Prevent local-file coverage from claiming an ambient filesystem tool."""

    if source_id != LOCAL_FILE_SOURCE_ID:
        return
    binding_required = result in {"success", "explicit_empty"}
    if (binding_required and tool_binding != LOCAL_FILE_READER_TOOL) or (
        tool_binding is not None and tool_binding != LOCAL_FILE_READER_TOOL
    ):
        raise ValidationError(
            f"{LOCAL_FILE_SOURCE_ID} observations require tool_binding {LOCAL_FILE_READER_TOOL!r}"
        )


def _await_authority_cancellation(uses: Sequence[LocalFileAuthorityUse]) -> None:
    for use in uses:
        if not use._wait_quiescent(_AUTHORITY_CANCEL_TIMEOUT_SECONDS):
            raise ConflictError(
                "local-file authority was revoked, but an in-flight chunk is still cancelling"
            )


def _file_transfer_bound(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_FILE_TRANSFER_BYTES:
        raise ValidationError("local file transfer size bound is invalid")
    return value


def _stable_file_metadata(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _match_file_ref(
    reference: LocalFileRef,
    metadata: tuple[int, int, int, int],
) -> None:
    if metadata != (
        reference.device,
        reference.inode,
        reference.size,
        reference.modified_ns,
    ):
        raise ValidationError("local file reference identity changed")


def _match_file_candidate(
    candidate: LocalFileTransferCandidate,
    metadata: tuple[int, int, int, int],
) -> None:
    if metadata != (
        candidate.device,
        candidate.inode,
        candidate.size,
        candidate.modified_ns,
    ):
        raise ValidationError("local file candidate identity changed")


@contextmanager
def _open_granted_file(grant: LocalFileGrant, relative: Path) -> Iterator[int]:
    """Open a granted file through no-follow descriptors and verify its ancestry."""

    if not _PINNED_FILE_TRANSFER_SUPPORTED:
        raise ValidationError("secure local file transfer is unavailable on this platform")
    parts = relative.parts
    if relative.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("local file transfer path is not safely relative")
    root = Path(grant.root)
    assessment = assess_local_path(relative, selected_root=root, content_requested=True)
    if assessment.relative_path != relative.as_posix():
        raise ValidationError("local file transfer path has symbolic-link ancestry")
    if (
        assessment.decision
        in {
            AwarenessDecision.EXCLUDE,
            AwarenessDecision.PLACEHOLDER,
        }
        or assessment.reason == "cloud-residency-unverified"
    ):
        raise ValidationError("local file is not eligible for binary transfer")

    directory_flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
    root_descriptor = -1
    directory_descriptors: list[int] = []
    directory_links: list[tuple[int, str, tuple[int, int]]] = []
    file_descriptor = -1
    try:
        try:
            root_descriptor = os.open(root, directory_flags)
        except OSError as exc:
            raise ValidationError("granted local file root changed before transfer") from exc
        opened_root = os.fstat(root_descriptor)
        if not stat.S_ISDIR(opened_root.st_mode) or _identity(opened_root) != (
            grant.device,
            grant.inode,
        ):
            raise ValidationError("granted local file root changed before transfer")

        parent = root_descriptor
        for component in parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=parent)
            except OSError as exc:
                raise ValidationError("local file transfer ancestry is unavailable") from exc
            child_metadata = os.fstat(child)
            if not stat.S_ISDIR(child_metadata.st_mode):
                os.close(child)
                raise ValidationError("local file transfer ancestry is not a directory")
            directory_links.append((parent, component, _identity(child_metadata)))
            directory_descriptors.append(child)
            parent = child

        name = parts[-1]
        try:
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise ValidationError("granted local file is unavailable") from exc
        if _is_cloud_placeholder_metadata(name, listed):
            raise ValidationError("local file is a cloud placeholder")
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            raise ValidationError("local file transfer requires a regular file, not a link")

        file_flags = os.O_RDONLY | _POSIX_OS.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            file_descriptor = os.open(name, file_flags, dir_fd=parent)
        except OSError as exc:
            raise ValidationError("local file changed before transfer") from exc
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(listed) != _identity(opened)
            or _is_cloud_placeholder_metadata(name, opened)
        ):
            raise ValidationError("local file changed before transfer")
        yield file_descriptor

        finished = os.fstat(file_descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(finished.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _stable_file_metadata(finished) != _stable_file_metadata(opened)
            or _identity(current) != _identity(opened)
            or _is_cloud_placeholder_metadata(name, finished)
            or _is_cloud_placeholder_metadata(name, current)
        ):
            raise ValidationError("local file changed while it was transferred")
        for link_parent, component, identity in directory_links:
            linked = os.stat(component, dir_fd=link_parent, follow_symlinks=False)
            if not stat.S_ISDIR(linked.st_mode) or _identity(linked) != identity:
                raise ValidationError("local file transfer ancestry changed while it ran")
        current_root = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(current_root.st_mode) or _identity(current_root) != (
            grant.device,
            grant.inode,
        ):
            raise ValidationError("granted local file root changed while it was transferred")
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("local file transfer could not verify its stable path") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _hash_descriptor(descriptor: int, *, label: str, max_bytes: int) -> str:
    digest = hashlib.sha256()
    total = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while block := os.read(descriptor, FILE_TRANSFER_CHUNK_BYTES):
        total += len(block)
        if total > max_bytes:
            raise ValidationError(f"{label} exceeds its transfer size bound")
        digest.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _hash_descriptor_authorized(
    descriptor: int,
    *,
    authority: LocalFileAuthorityUse,
    label: str,
    max_bytes: int,
) -> str:
    """Hash source bytes with revocation checked around every read boundary."""

    digest = hashlib.sha256()
    total = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        with authority.chunk():
            block = os.read(descriptor, FILE_TRANSFER_CHUNK_BYTES)
        if not block:
            break
        total += len(block)
        if total > max_bytes:
            raise ValidationError(f"{label} exceeds its transfer size bound")
        digest.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _stream_file_ref(
    reference: LocalFileRef,
    *,
    grant: LocalFileGrant,
    relative: Path,
    chunk_size: int,
    authority: LocalFileAuthorityUse | None = None,
) -> Iterator[bytes]:
    """Stream one stable file, checking a cancellable authority at every source read."""

    with _open_granted_file(grant, relative) as descriptor:
        metadata = _stable_file_metadata(os.fstat(descriptor))
        _match_file_ref(reference, metadata)
        digest = hashlib.sha256()
        total = 0
        while True:
            if authority is None:
                with reference._store._locked():
                    current_grant = reference._store._grant_for_file_ref(reference.grant_id)
                    if current_grant.root != reference._root:
                        raise ValidationError("local file reference grant root changed")
                    block = os.read(descriptor, chunk_size)
            else:
                with authority.chunk():
                    block = os.read(descriptor, chunk_size)
            if not block:
                break
            total += len(block)
            if total > reference.size:
                raise ValidationError("local file grew beyond its captured identity")
            digest.update(block)
            yield block
        if total != reference.size or digest.hexdigest() != reference.sha256:
            raise ValidationError("local file reference content changed while streamed")


def _is_cloud_placeholder_metadata(name: str, metadata: os.stat_result) -> bool:
    if name.endswith(".icloud"):
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return bool(attributes & (0x1000 | 0x400000))


def _read_granted_file(*, grant: LocalFileGrant, relative: Path) -> dict[str, Any]:
    root = Path(grant.root)
    relative_text = relative.as_posix()
    preview = assess_local_path(relative, selected_root=root, content_requested=True)
    if preview.relative_path is not None and preview.relative_path != relative_text:
        return _result(
            grant=grant,
            relative_path=relative_text,
            decision=AwarenessDecision.EXCLUDE,
            reason="symbolic-link-ancestry",
        )

    screened = read_screened_local_content(
        relative,
        selected_root=root,
        expected_root_identity=(grant.device, grant.inode),
    )
    result = _result(
        grant=grant,
        relative_path=relative_text,
        decision=screened.path.decision,
        reason=screened.path.reason,
    )
    if screened.screening is not None:
        result["screening"] = {
            "bytes_screened": screened.screening.bytes_screened,
            "decision": screened.screening.decision.value,
            "reasons": list(screened.screening.reasons),
        }
        if screened.screening.decision is AwarenessDecision.QUARANTINE:
            result["decision"] = AwarenessDecision.QUARANTINE.value
            result["reason"] = "content-screen"
    if screened.content is not None:
        try:
            result["content"] = screened.content.decode("utf-8")
        except UnicodeDecodeError:
            result["screening"] = {
                "bytes_screened": len(screened.content),
                "decision": AwarenessDecision.QUARANTINE.value,
                "reasons": ["non-utf8-content"],
            }
            result["decision"] = AwarenessDecision.QUARANTINE.value
            result["reason"] = "non-utf8-content"
    return result


def _grant_root(value: Path | str) -> tuple[Path, tuple[int, int]]:
    text = _bounded_text(str(value), "selected root")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("selected root must be an absolute directory path")
    try:
        listed = os.lstat(candidate)
        canonical = candidate.resolve(strict=True)
        resolved = os.stat(canonical, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError("selected root is unavailable") from exc
    if (
        _is_link_or_reparse(listed)
        or not stat.S_ISDIR(listed.st_mode)
        or not stat.S_ISDIR(resolved.st_mode)
        or _identity(listed) != _identity(resolved)
    ):
        raise ValidationError("selected root must be one stable real directory")
    assessment = assess_local_path(canonical, selected_root=canonical)
    if assessment.decision is AwarenessDecision.EXCLUDE:
        raise ValidationError(f"selected root is not grantable: {assessment.reason}")
    return canonical, _identity(resolved)


def _real_directory(value: Path, label: str) -> tuple[Path, tuple[int, int]]:
    try:
        listed = os.lstat(value)
        canonical = value.resolve(strict=True)
        resolved = os.stat(canonical, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError(f"local-file {label} is unavailable") from exc
    if (
        _is_link_or_reparse(listed)
        or not stat.S_ISDIR(listed.st_mode)
        or not stat.S_ISDIR(resolved.st_mode)
        or _identity(listed) != _identity(resolved)
    ):
        raise ValidationError(f"local-file {label} must be one stable real directory")
    return canonical, _identity(resolved)


def _directory_matches(value: str, *, expected: tuple[int, int]) -> bool:
    try:
        metadata = os.lstat(value)
    except OSError:
        return False
    return (
        not _is_link_or_reparse(metadata)
        and stat.S_ISDIR(metadata.st_mode)
        and _identity(metadata) == expected
    )


def _root_matches(grant: LocalFileGrant) -> bool:
    try:
        metadata = os.lstat(grant.root)
    except OSError:
        return False
    return (
        not _is_link_or_reparse(metadata)
        and stat.S_ISDIR(metadata.st_mode)
        and _identity(metadata) == (grant.device, grant.inode)
    )


def _prepare_private_storage(root: Path) -> None:
    if not os.path.lexists(root):
        root.mkdir(parents=True, mode=0o700)
        if os.name != "nt":
            root.chmod(0o700)
    try:
        metadata = os.lstat(root)
    except OSError as exc:
        raise ValidationError("Seld host-local storage is unavailable") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("Seld host-local storage must be one real directory")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValidationError("Seld host-local storage must be owner-only")


def _validate_private_file(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValidationError("Seld local-file grant store is unavailable") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("Seld local-file grant store must be one regular file")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValidationError("Seld local-file grant store must be owner-only")


def _grant_from_value(value: object) -> LocalFileGrant:
    if not isinstance(value, dict) or set(value) != _GRANT_KEYS:
        raise ValidationError("Seld local-file grant has an unsupported shape")
    device = value.get("device")
    inode = value.get("inode")
    vault_device = value.get("vault_device")
    vault_inode = value.get("vault_inode")
    created_at = value.get("created_at")
    if any(
        type(identity) is not int or identity < 0
        for identity in (device, inode, vault_device, vault_inode)
    ):
        raise ValidationError("Seld local-file grant has an invalid root identity")
    if not isinstance(created_at, str):
        raise ValidationError("Seld local-file grant has an invalid creation time")
    return LocalFileGrant(
        grant_id=_grant_id(value.get("grant_id")),
        vault_id=canonical_vault_id(value.get("vault_id")),
        vault_root=_stored_absolute_path(value.get("vault_root"), "vault root"),
        vault_device=cast(int, vault_device),
        vault_inode=cast(int, vault_inode),
        root=_stored_absolute_path(value.get("root"), "selected root"),
        device=cast(int, device),
        inode=cast(int, inode),
        created_at=stored_time(created_at, "local-file grant creation time"),
    )


def _validate_legacy_grant(value: object) -> None:
    if not isinstance(value, dict) or set(value) != _LEGACY_GRANT_KEYS:
        raise ValidationError("Seld local-file grant has an unsupported shape")
    device = value.get("device")
    inode = value.get("inode")
    created_at = value.get("created_at")
    if type(device) is not int or device < 0 or type(inode) is not int or inode < 0:
        raise ValidationError("Seld local-file grant has an invalid root identity")
    if not isinstance(created_at, str):
        raise ValidationError("Seld local-file grant has an invalid creation time")
    _grant_id(value.get("grant_id"))
    canonical_vault_id(value.get("vault_id"))
    _stored_absolute_path(value.get("vault_root"), "vault root")
    _stored_absolute_path(value.get("root"), "selected root")
    stored_time(created_at, "local-file grant creation time")


def _grant_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("local-file grant ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError("local-file grant ID is invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValidationError("local-file grant ID is invalid")
    return value


def _stored_absolute_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"Seld local-file grant has an invalid {label}")
    text = _bounded_text(value, label)
    if not Path(text).is_absolute():
        raise ValidationError(f"Seld local-file grant has an invalid {label}")
    return text


def _relative_path(value: str) -> Path:
    text = _bounded_text(value, "relative_path")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.anchor or not candidate.parts:
        raise ValidationError("relative_path must name one path beneath the granted root")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValidationError("relative_path cannot contain empty, dot, or parent components")
    return candidate


def _bounded_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} is not valid Unicode text") from exc
    if not encoded or len(encoded) > MAX_LOCAL_PATH_BYTES or b"\x00" in encoded:
        raise ValidationError(f"{label} is empty, too large, or contains a null byte")
    return value


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    file_attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_flag)


def _result(
    *,
    grant: LocalFileGrant,
    relative_path: str,
    decision: AwarenessDecision,
    reason: str,
) -> dict[str, Any]:
    return {
        "capability": LOCAL_FILE_READ_CAPABILITY,
        "decision": decision.value,
        "grant_id": grant.grant_id,
        "persisted": False,
        "reason": reason,
        "relative_path": relative_path,
        "selected_root": grant.root,
        "source": LOCAL_FILE_SOURCE_ID,
        "tool_binding": LOCAL_FILE_READER_TOOL,
        "transient": True,
    }
