"""Idempotent migration for the Culture-Grade onboarding/control foundation."""

from __future__ import annotations

import json
import os
import secrets
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from continuity_kernel.atomic import (
    PINNED_PATH_ROOT_SUPPORTED,
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    atomic_write,
    durable_publish_new,
    exclusive_lock,
    read_regular_file,
    sha256_bytes,
)
from continuity_kernel.atomic import (
    move_no_replace as _move_no_replace,
)
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    NotFoundError,
    ValidationError,
)
from continuity_kernel.records import format_time, stored_time
from continuity_kernel.vault import Vault

MIGRATION_ID: Final = "culture-grade-foundation-v1"
MIGRATION_FORMAT_VERSION: Final = 1
MAX_RECEIPT_BYTES: Final = 64 * 1024
MAX_REMOVAL_MARKER_BYTES: Final = 1024
FOUNDATION_DIRECTORIES: Final = (
    "onboarding",
    ".gsv/control",
    ".gsv/migrations",
)
MIGRATION_PHASES: Final = frozenset({"applied", "removing", "rolled_back"})
_PINNED_PARENT_SUPPORTED: Final = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.mkdir in os.supports_dir_fd
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
)
_POSIX_OS = cast(Any, os)


@dataclass(frozen=True)
class MigrationPlan:
    migration_id: str
    vault_id: str
    applicable: bool
    already_applied: bool
    create_directories: tuple[str, ...]
    existing_directories: tuple[str, ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DirectoryOwnership:
    relative_path: str
    device: int
    inode: int


@dataclass(frozen=True)
class MigrationReceipt:
    format_version: int
    migration_id: str
    vault_id: str
    backup_path: str
    backup_sha256: str
    created_directories: tuple[str, ...]
    directory_ownership: tuple[DirectoryOwnership, ...]
    phase: str
    remaining_directories: tuple[str, ...]
    removed_directories: tuple[str, ...]
    applied_at: str
    revision: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FoundationMigration:
    """Add portable onboarding and control directories without touching user records."""

    def __init__(self, vault: Vault):
        self.vault = vault
        # The receipt deliberately lives outside every directory this migration
        # owns. It remains durable while rollback removes the final directory.
        self.receipt_path = vault.root / f".gsv/migration-{MIGRATION_ID}.json"
        self.lock_path = vault.root / ".gsv/locks/migration.lock"

    def plan(self) -> MigrationPlan:
        identity = self.vault.identity()
        blockers: list[str] = []
        create: list[str] = []
        existing: list[str] = []
        for relative in FOUNDATION_DIRECTORIES:
            path = self.vault.root / relative
            try:
                metadata = os.lstat(path)
            except FileNotFoundError:
                create.append(relative)
                continue
            except OSError:
                blockers.append(f"{relative}:unavailable")
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                blockers.append(f"{relative}:not-real-directory")
            else:
                existing.append(relative)
        receipt_exists = os.path.lexists(self.receipt_path)
        already_applied = False
        if receipt_exists:
            try:
                receipt = self.load_receipt()
                if receipt.vault_id != identity["vault_id"]:
                    blockers.append("receipt:vault-identity-mismatch")
                elif receipt.phase == "applied":
                    if create:
                        blockers.append("receipt:applied-directories-missing")
                    else:
                        for ownership in receipt.directory_ownership:
                            if ownership.relative_path in create:
                                continue
                            path = self.vault.root / ownership.relative_path
                            try:
                                metadata = os.lstat(path)
                            except FileNotFoundError:
                                continue
                            except OSError:
                                blockers.append(
                                    f"receipt:owned-directory-unavailable:{ownership.relative_path}"
                                )
                                continue
                            if (int(metadata.st_dev), int(metadata.st_ino)) != (
                                ownership.device,
                                ownership.inode,
                            ):
                                blockers.append(
                                    f"receipt:owned-directory-changed:{ownership.relative_path}"
                                )
                        already_applied = not blockers
                elif receipt.phase == "removing":
                    blockers.append("receipt:rollback-in-progress")
            except (NotFoundError, ValidationError):
                blockers.append("receipt:invalid")
        return MigrationPlan(
            migration_id=MIGRATION_ID,
            vault_id=str(identity["vault_id"]),
            applicable=not blockers and not already_applied,
            already_applied=already_applied and not blockers,
            create_directories=tuple(create),
            existing_directories=tuple(existing),
            blockers=tuple(blockers),
        )

    def apply(self, *, observed_at: datetime | None = None) -> MigrationReceipt:
        with _bound_migration_lock(self.vault.root, self.lock_path):
            root_identity = _real_directory_identity(
                self.vault.root,
                label="migration vault root",
            )
            receipt_parent_identity = _real_directory_identity(
                self.receipt_path.parent,
                label="migration receipt parent",
            )
            current = self.plan()
            if current.blockers:
                raise ValidationError("migration plan is blocked: " + ", ".join(current.blockers))
            if current.already_applied:
                try:
                    _validate_apply_path_identities(
                        self.vault.root,
                        root_identity=root_identity,
                        receipt_parent=self.receipt_path.parent,
                        receipt_parent_identity=receipt_parent_identity,
                    )
                    receipt = self.load_receipt()
                    for owned in receipt.directory_ownership:
                        _verify_owned_directory_identity_for_receipt(self.vault.root, owned)
                    _validate_apply_path_identities(
                        self.vault.root,
                        root_identity=root_identity,
                        receipt_parent=self.receipt_path.parent,
                        receipt_parent_identity=receipt_parent_identity,
                    )
                except Exception as exc:
                    raise DegradedIntegrityError(
                        "applied migration receipt could not be bound to its canonical vault path"
                    ) from exc
                return receipt
            backup = self.vault.create_backup()
            backup_path = Path(str(backup["backup"]))
            try:
                relative_backup = backup_path.relative_to(self.vault.root).as_posix()
            except ValueError as exc:
                raise ValidationError(
                    "migration backup must remain inside the vault backups folder"
                ) from exc
            created: list[str] = []
            ownership: list[DirectoryOwnership] = []
            preserve_created = False
            try:
                for relative in FOUNDATION_DIRECTORIES:
                    path = self.vault.root / relative
                    try:
                        metadata = os.lstat(path)
                    except FileNotFoundError:
                        owned = _publish_owned_empty_directory(self.vault.root, relative)
                        created.append(relative)
                        ownership.append(owned)
                        continue
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                        raise ValidationError(
                            f"migration target is not a real directory: {relative}"
                        )
                for owned in ownership:
                    _verify_owned_directory_for_receipt(self.vault.root, owned)
                receipt = MigrationReceipt(
                    format_version=MIGRATION_FORMAT_VERSION,
                    migration_id=MIGRATION_ID,
                    vault_id=current.vault_id,
                    backup_path=relative_backup,
                    backup_sha256=str(backup["sha256"]),
                    created_directories=tuple(created),
                    directory_ownership=tuple(ownership),
                    phase="applied",
                    remaining_directories=tuple(reversed(created)),
                    removed_directories=(),
                    applied_at=format_time(observed_at or datetime.now(UTC)),
                    revision="",
                )
                encoded = _encode_receipt(receipt)
                _validate_apply_path_identities(
                    self.vault.root,
                    root_identity=root_identity,
                    receipt_parent=self.receipt_path.parent,
                    receipt_parent_identity=receipt_parent_identity,
                )
                try:
                    atomic_write(self.receipt_path, encoded)
                except Exception as exc:
                    try:
                        _validate_apply_path_identities(
                            self.vault.root,
                            root_identity=root_identity,
                            receipt_parent=self.receipt_path.parent,
                            receipt_parent_identity=receipt_parent_identity,
                        )
                    except (OSError, ValidationError, DegradedIntegrityError) as identity_exc:
                        preserve_created = True
                        raise DegradedIntegrityError(
                            "migration receipt publication crossed a changed canonical vault path; "
                            "all visible paths were preserved"
                        ) from identity_exc
                    if os.path.lexists(self.receipt_path):
                        try:
                            visible = read_regular_file(
                                self.receipt_path,
                                label="migration receipt",
                                max_bytes=MAX_RECEIPT_BYTES,
                            )
                        except Exception as read_exc:
                            preserve_created = True
                            raise DegradedIntegrityError(
                                "migration receipt publication has an unknown visible state"
                            ) from read_exc
                        preserve_created = True
                        if visible == encoded:
                            raise MutationCommittedError(
                                "migration was applied, but receipt durability was not confirmed"
                            ) from exc
                        raise DegradedIntegrityError(
                            "migration receipt publication has an unknown visible state"
                        ) from exc
                    raise
                try:
                    _validate_apply_path_identities(
                        self.vault.root,
                        root_identity=root_identity,
                        receipt_parent=self.receipt_path.parent,
                        receipt_parent_identity=receipt_parent_identity,
                    )
                    visible = read_regular_file(
                        self.receipt_path,
                        label="migration receipt",
                        max_bytes=MAX_RECEIPT_BYTES,
                    )
                    if visible != encoded:
                        raise DegradedIntegrityError(
                            "migration receipt changed during canonical-path verification"
                        )
                    for owned in ownership:
                        _verify_owned_directory_identity_for_receipt(self.vault.root, owned)
                    _validate_apply_path_identities(
                        self.vault.root,
                        root_identity=root_identity,
                        receipt_parent=self.receipt_path.parent,
                        receipt_parent_identity=receipt_parent_identity,
                    )
                except Exception as exc:
                    preserve_created = True
                    raise DegradedIntegrityError(
                        "migration receipt is visible, but its canonical vault path or ownership "
                        "could not be proven; all visible paths were preserved"
                    ) from exc
                return _parse_receipt(encoded)
            except Exception as exc:
                try:
                    _validate_apply_path_identities(
                        self.vault.root,
                        root_identity=root_identity,
                        receipt_parent=self.receipt_path.parent,
                        receipt_parent_identity=receipt_parent_identity,
                    )
                except (OSError, ValidationError, DegradedIntegrityError) as identity_exc:
                    preserve_created = True
                    if not isinstance(exc, DegradedIntegrityError):
                        raise DegradedIntegrityError(
                            "migration vault path changed during apply; all visible paths were "
                            "preserved"
                        ) from identity_exc
                if not preserve_created:
                    owned_by_path = {item.relative_path: item for item in ownership}
                    for relative in reversed(created):
                        with suppress(Exception):
                            _remove_owned_empty_directory(
                                self.vault.root,
                                relative=relative,
                                ownership=owned_by_path[relative],
                                backup_path=relative_backup,
                            )
                raise

    def rollback(self, *, expected_revision: str) -> dict[str, Any]:
        with _bound_migration_lock(self.vault.root, self.lock_path):
            receipt = self.load_receipt()
            if receipt.revision != expected_revision:
                raise ConflictError("migration receipt changed; reload before rollback")
            self._require_current_vault(receipt)
            if receipt.phase == "rolled_back":
                return _rollback_result(receipt, already_rolled_back=True)
            _validate_rollback_targets(self.vault.root, self.receipt_path, receipt)
            if receipt.phase == "applied":
                self._require_current_vault(receipt)
                receipt = self._store_receipt(replace(receipt, phase="removing", revision=""))

            while receipt.remaining_directories:
                self._require_current_vault(receipt)
                relative = receipt.remaining_directories[0]
                ownership = _ownership_for(receipt, relative)
                _remove_owned_empty_directory(
                    self.vault.root,
                    relative=relative,
                    ownership=ownership,
                    backup_path=receipt.backup_path,
                )
                self._require_current_vault(receipt)
                receipt = self._store_receipt(
                    replace(
                        receipt,
                        remaining_directories=receipt.remaining_directories[1:],
                        removed_directories=(*receipt.removed_directories, relative),
                        revision="",
                    )
                )

            self._require_current_vault(receipt)
            receipt = self._store_receipt(replace(receipt, phase="rolled_back", revision=""))
            return _rollback_result(receipt, already_rolled_back=False)

    def _require_current_vault(self, receipt: MigrationReceipt) -> None:
        try:
            current = str(self.vault.identity()["vault_id"])
        except (KeyError, OSError, TypeError, ValidationError) as exc:
            raise ValidationError(
                "migration rollback cannot verify the current vault identity"
            ) from exc
        if current != receipt.vault_id:
            raise ConflictError(
                "migration receipt belongs to a different logical vault; rollback was refused"
            )

    def load_receipt(self) -> MigrationReceipt:
        if not os.path.lexists(self.receipt_path):
            raise NotFoundError("foundation migration has not been applied")
        encoded = read_regular_file(
            self.receipt_path,
            label="migration receipt",
            max_bytes=MAX_RECEIPT_BYTES,
        )
        return _parse_receipt(encoded)

    def _store_receipt(self, receipt: MigrationReceipt) -> MigrationReceipt:
        encoded = _encode_receipt(receipt)
        atomic_write(self.receipt_path, encoded)
        return _parse_receipt(encoded)


@dataclass
class _MigrationRootGuard:
    """Keep one apply bound to the exact canonical vault root it entered."""

    root: Path
    root_parent_identity: tuple[int, int]
    root_identity: tuple[int, int]
    pinned_root: PinnedPathRoot | None

    @classmethod
    def capture(cls, root: Path) -> _MigrationRootGuard:
        parent_identity = _real_directory_identity(
            root.parent,
            label="migration vault root parent",
        )
        root_identity = _real_directory_identity(root, label="migration vault root")
        pinned_root = PinnedPathRoot(root) if PINNED_PATH_ROOT_SUPPORTED else None
        guard = cls(
            root=root,
            root_parent_identity=parent_identity,
            root_identity=root_identity,
            pinned_root=pinned_root,
        )
        try:
            guard.validate()
        except Exception:
            guard.close()
            raise
        return guard

    def validate(self) -> None:
        try:
            parent_identity = _real_directory_identity(
                self.root.parent,
                label="migration vault root parent",
            )
            root_identity = _real_directory_identity(self.root, label="migration vault root")
            if parent_identity != self.root_parent_identity or root_identity != self.root_identity:
                raise DegradedIntegrityError(
                    "migration vault root changed identity; all visible paths were preserved"
                )
            if self.pinned_root is not None and not self.pinned_root.directory_exists(".gsv"):
                raise DegradedIntegrityError(
                    "migration vault state directory disappeared; all visible paths were preserved"
                )
        except DegradedIntegrityError:
            raise
        except (OSError, ValidationError) as exc:
            raise DegradedIntegrityError(
                "migration vault root lost its canonical path; all visible paths were preserved"
            ) from exc

    def close(self) -> None:
        if self.pinned_root is not None:
            self.pinned_root.close()


@contextmanager
def _bound_migration_lock(root: Path, lock_path: Path) -> Iterator[None]:
    """Validate the root again after the ordinary named lock has fully exited.

    The named lock proves its own leaf and immediate parent. A vault-root swap
    can nevertheless transplant that parent and remain invisible to the lock's
    final check. Keeping a pinned root open across the lock, then validating it
    after lock release, closes that transaction-boundary gap.
    """

    guard = _MigrationRootGuard.capture(root)
    body_completed = False
    try:
        try:
            with exclusive_lock(lock_path):
                guard.validate()
                yield
                guard.validate()
                body_completed = True
        except Exception as exc:
            if not body_completed:
                raise
            try:
                guard.validate()
            except Exception as identity_exc:
                raise DegradedIntegrityError(
                    "migration completed, but the vault root changed during lock exit; "
                    "visible state is unknown and all paths were preserved"
                ) from identity_exc
            raise DegradedIntegrityError(
                "migration completed, but lock exit could not be proven; visible state is "
                "unknown and all paths were preserved"
            ) from exc
        try:
            guard.validate()
        except Exception as exc:
            raise DegradedIntegrityError(
                "migration completed, but the vault root changed during lock exit; visible "
                "state is unknown and all paths were preserved"
            ) from exc
    finally:
        guard.close()


def _publish_owned_empty_directory(root: Path, relative: str) -> DirectoryOwnership:
    """Publish one migration-owned directory without ever adopting a replacement inode."""

    path = root / relative
    parent_descriptor, parent_identity = _parent_pin(path.parent, relative=relative)
    stage_descriptor = -1
    try:
        for _attempt in range(8):
            stage_name = f".{path.name}.gsv-create-{secrets.token_hex(16)}.stage"
            try:
                if parent_descriptor >= 0:
                    os.mkdir(stage_name, 0o700, dir_fd=parent_descriptor)
                else:
                    os.mkdir(path.parent / stage_name, 0o700)
            except FileExistsError:
                continue
            break
        else:
            raise DegradedIntegrityError(
                f"migration could not reserve a private stage for {relative}; paths were preserved"
            )
        stage_path = path.parent / stage_name
        listed = _entry_metadata(path.parent, stage_name, parent_descriptor)
        if listed is None:
            raise DegradedIntegrityError(
                f"migration staged directory disappeared for {relative}; all paths were preserved"
            )
        if parent_descriptor >= 0:
            flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
            stage_descriptor = os.open(stage_name, flags, dir_fd=parent_descriptor)
            opened = os.fstat(stage_descriptor)
            _POSIX_OS.fchmod(stage_descriptor, 0o700)
            os.fsync(stage_descriptor)
            has_entries = bool(os.listdir(stage_descriptor))
        else:
            opened = listed
            try:
                with os.scandir(stage_path) as entries:
                    has_entries = next(entries, None) is not None
            except OSError as exc:
                raise DegradedIntegrityError(
                    f"migration could not inspect its staged directory for {relative}; the staged "
                    "path was preserved"
                ) from exc
        if (
            not stat.S_ISDIR(listed.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or _identity(listed) != _identity(opened)
        ):
            raise DegradedIntegrityError(
                f"migration staged directory changed identity for {relative}; all paths were "
                "preserved"
            )
        if has_entries:
            raise DegradedIntegrityError(
                f"migration staged directory was not empty for {relative}; all paths were preserved"
            )
        ownership = DirectoryOwnership(
            relative_path=relative,
            device=int(opened.st_dev),
            inode=int(opened.st_ino),
        )
        _validate_pinned_parent(
            path.parent,
            parent_descriptor,
            parent_identity,
            relative=relative,
        )
        try:
            _move_no_replace(stage_path, path)
        except OSError as exc:
            _validate_pinned_parent(
                path.parent,
                parent_descriptor,
                parent_identity,
                relative=relative,
            )
            stage = _entry_metadata(path.parent, stage_name, parent_descriptor)
            target = _entry_metadata(path.parent, path.name, parent_descriptor)
            stage_matches = stage is not None and _identity(stage) == _identity(opened)
            if isinstance(exc, FileExistsError) and stage_matches and target is not None:
                raise ConflictError(
                    f"migration target appeared during creation: {relative}; both paths were "
                    "preserved"
                ) from exc
            if stage_matches and target is None:
                raise ValidationError(
                    f"migration could not publish its staged directory for {relative}; the stage "
                    "was preserved"
                ) from exc
            raise DegradedIntegrityError(
                f"migration directory publication has an unknown outcome for {relative}; all paths "
                "were preserved"
            ) from exc
        _verify_owned_empty_directory(
            path,
            ownership=ownership,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
            stage_name=stage_name,
            stage_descriptor=stage_descriptor,
            context="during creation",
        )
        if parent_descriptor >= 0:
            os.fsync(parent_descriptor)
        else:
            _fsync_parent(path.parent)
        _verify_owned_empty_directory(
            path,
            ownership=ownership,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
            stage_name=stage_name,
            stage_descriptor=stage_descriptor,
            context="during creation",
        )
        return ownership
    finally:
        if stage_descriptor >= 0:
            os.close(stage_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _parent_pin(parent: Path, *, relative: str) -> tuple[int, tuple[int, int] | None]:
    if not _PINNED_PARENT_SUPPORTED:
        return -1, None
    flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(parent, flags)
        opened = os.fstat(descriptor)
        visible = os.lstat(parent)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ValidationError(
            f"migration could not pin the parent directory for {relative}"
        ) from exc
    identity = _identity(opened)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(visible.st_mode)
        or _identity(visible) != identity
    ):
        os.close(descriptor)
        raise ValidationError(f"migration parent directory changed identity for {relative}")
    return descriptor, identity


def _validate_pinned_parent(
    parent: Path,
    descriptor: int,
    identity: tuple[int, int] | None,
    *,
    relative: str,
) -> None:
    if descriptor < 0:
        return
    assert identity is not None
    try:
        opened = os.fstat(descriptor)
        visible = os.lstat(parent)
    except OSError as exc:
        raise DegradedIntegrityError(
            f"migration parent directory became unavailable for {relative}; all paths were "
            "preserved"
        ) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(visible.st_mode)
        or _identity(opened) != identity
        or _identity(visible) != identity
    ):
        raise DegradedIntegrityError(
            f"migration parent directory changed identity for {relative}; all paths were preserved"
        )


def _entry_metadata(
    parent: Path,
    name: str,
    parent_descriptor: int,
) -> os.stat_result | None:
    try:
        if parent_descriptor >= 0:
            return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        return os.lstat(parent / name)
    except FileNotFoundError:
        return None


def _real_directory_identity(path: Path, *, label: str) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValidationError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"{label} is not a real directory: {path}")
    return _identity(metadata)


def _validate_apply_path_identities(
    root: Path,
    *,
    root_identity: tuple[int, int],
    receipt_parent: Path,
    receipt_parent_identity: tuple[int, int],
) -> None:
    if _real_directory_identity(root, label="migration vault root") != root_identity:
        raise DegradedIntegrityError(
            "migration vault root changed identity; all visible paths were preserved"
        )
    if (
        _real_directory_identity(receipt_parent, label="migration receipt parent")
        != receipt_parent_identity
    ):
        raise DegradedIntegrityError(
            "migration receipt parent changed identity; all visible paths were preserved"
        )


def _verify_owned_directory_for_receipt(root: Path, ownership: DirectoryOwnership) -> None:
    path = root / ownership.relative_path
    parent_descriptor, parent_identity = _parent_pin(
        path.parent,
        relative=ownership.relative_path,
    )
    try:
        _verify_owned_empty_directory(
            path,
            ownership,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
            stage_name=None,
            stage_descriptor=-1,
            context="before its ownership receipt",
        )
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _verify_owned_directory_identity_for_receipt(
    root: Path,
    ownership: DirectoryOwnership,
) -> None:
    path = root / ownership.relative_path
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise DegradedIntegrityError(
            f"migration-created directory is unavailable after receipt publication: "
            f"{ownership.relative_path}"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or _identity(metadata) != (ownership.device, ownership.inode)
    ):
        raise DegradedIntegrityError(
            f"migration-created directory changed after receipt publication: "
            f"{ownership.relative_path}; all visible paths were preserved"
        )


def _verify_owned_empty_directory(
    path: Path,
    ownership: DirectoryOwnership,
    *,
    parent_descriptor: int,
    parent_identity: tuple[int, int] | None,
    stage_name: str | None,
    stage_descriptor: int,
    context: str,
) -> None:
    _validate_pinned_parent(
        path.parent,
        parent_descriptor,
        parent_identity,
        relative=ownership.relative_path,
    )
    expected = (ownership.device, ownership.inode)
    metadata = _entry_metadata(path.parent, path.name, parent_descriptor)
    if metadata is None:
        if stage_name is not None:
            raise DegradedIntegrityError(
                f"migration directory disappeared during publication: {ownership.relative_path}; "
                "all paths were preserved"
            )
        raise ConflictError(
            f"migration-created directory disappeared {context}: {ownership.relative_path}"
        )
    if stage_name is not None:
        stage = _entry_metadata(path.parent, stage_name, parent_descriptor)
        if stage is not None:
            raise DegradedIntegrityError(
                f"migration publication retained a staged entry for {ownership.relative_path}; "
                "all paths were preserved"
            )
        if stage_descriptor >= 0:
            opened_stage = os.fstat(stage_descriptor)
            if not stat.S_ISDIR(opened_stage.st_mode) or _identity(opened_stage) != expected:
                raise DegradedIntegrityError(
                    f"migration staged directory changed for {ownership.relative_path}; all paths "
                    "were preserved"
                )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ConflictError(
            f"migration-created directory changed type {context}: {ownership.relative_path}"
        )
    if _identity(metadata) != expected:
        raise ConflictError(
            f"migration-created directory changed identity {context}: "
            f"{ownership.relative_path}; the replacement was preserved"
        )
    descriptor = -1
    try:
        if parent_descriptor >= 0:
            flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            has_entries = bool(os.listdir(descriptor))
        else:
            opened = metadata
            with os.scandir(path) as entries:
                has_entries = next(entries, None) is not None
        if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != expected:
            raise ConflictError(
                f"migration-created directory changed while it was opened {context}: "
                f"{ownership.relative_path}; the replacement was preserved"
            )
        if has_entries:
            raise ConflictError(
                f"migration-created directory received data {context}: "
                f"{ownership.relative_path}; the data was preserved"
            )
        _validate_pinned_parent(
            path.parent,
            parent_descriptor,
            parent_identity,
            relative=ownership.relative_path,
        )
        visible = _entry_metadata(path.parent, path.name, parent_descriptor)
        if visible is None or _identity(visible) != expected:
            raise ConflictError(
                f"migration-created directory changed during ownership verification {context}: "
                f"{ownership.relative_path}; all paths were preserved"
            )
    except OSError as exc:
        raise DegradedIntegrityError(
            f"migration-created directory became unavailable {context}: "
            f"{ownership.relative_path}; all paths were preserved"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_rollback_targets(
    root: Path,
    receipt_path: Path,
    receipt: MigrationReceipt,
) -> None:
    del receipt_path
    for relative in receipt.remaining_directories:
        _validate_rollback_target(root, receipt, relative)


def _validate_rollback_target(root: Path, receipt: MigrationReceipt, relative: str) -> None:
    path = root / relative
    ownership = _ownership_for(receipt, relative)
    quarantine, marker = _removal_paths(path, relative, ownership)
    marker_exists = _validate_removal_marker(marker, relative, ownership)
    metadata = _lstat_optional(path)
    quarantine_metadata = _lstat_optional(quarantine)

    if quarantine_metadata is not None:
        _validate_owned_directory(
            quarantine,
            quarantine_metadata,
            relative=relative,
            ownership=ownership,
            backup_path=receipt.backup_path,
        )
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        if marker_exists or quarantine_metadata is not None:
            return
        raise ValidationError(f"migration rollback target changed type: {relative}")
    if _identity(metadata) != (ownership.device, ownership.inode):
        if marker_exists or quarantine_metadata is not None:
            return
        raise ConflictError(
            f"migration rollback target changed identity: {relative}; the replacement is not "
            "owned by this migration"
        )
    _validate_empty_directory(path, relative=relative, backup_path=receipt.backup_path)
    if quarantine_metadata is not None:
        raise ConflictError(
            f"migration rollback has two copies of an owned directory: {relative}; "
            "both paths were preserved"
        )


def _ownership_for(receipt: MigrationReceipt, relative: str) -> DirectoryOwnership:
    ownership = next(
        (item for item in receipt.directory_ownership if item.relative_path == relative),
        None,
    )
    if ownership is None:
        raise ValidationError(f"migration rollback target has no ownership proof: {relative}")
    return ownership


def _remove_owned_empty_directory(
    root: Path,
    *,
    relative: str,
    ownership: DirectoryOwnership,
    backup_path: str,
) -> None:
    """Remove only the inode recorded by the migration receipt.

    The owned directory is first moved without replacement to a private sibling.
    The empty quarantine is retained as a host-local tombstone. A durable marker
    makes that logical removal resumable before receipt progress is recorded.
    Retention is intentional: portable pathname APIs cannot remove a directory
    by inode, so deleting the quarantine would reopen the same identity-swap
    race at a different name.
    """

    path = root / relative
    quarantine, marker = _removal_paths(path, relative, ownership)
    expected_identity = (ownership.device, ownership.inode)

    for _attempt in range(3):
        marker_exists = _validate_removal_marker(marker, relative, ownership)
        metadata = _lstat_optional(path)
        quarantine_metadata = _lstat_optional(quarantine)

        if quarantine_metadata is not None and _identity(quarantine_metadata) != expected_identity:
            raise DegradedIntegrityError(
                f"migration removal quarantine changed identity for {relative}; all paths "
                "were preserved"
            )

        if quarantine_metadata is None:
            if metadata is None:
                # An older rollback may have removed the path before storing
                # progress. New rollbacks retain an owned quarantine tombstone.
                return
            if _identity(metadata) != expected_identity:
                if marker_exists:
                    # The owned inode was already isolated. A new path at the
                    # public name belongs to somebody else.
                    return
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise ValidationError(f"migration rollback target changed type: {relative}")
                raise ConflictError(
                    f"migration rollback target changed identity: {relative}; the replacement "
                    "is not owned by this migration"
                )
            _validate_owned_directory(
                path,
                metadata,
                relative=relative,
                ownership=ownership,
                backup_path=backup_path,
            )
            try:
                _move_no_replace(path, quarantine)
            except FileExistsError:
                continue
            except OSError as exc:
                moved = _lstat_optional(quarantine)
                source = _lstat_optional(path)
                if moved is not None and _identity(moved) == expected_identity and source is None:
                    quarantine_metadata = moved
                elif (
                    source is not None and _identity(source) == expected_identity and moved is None
                ):
                    raise ValidationError(
                        f"migration rollback could not isolate owned directory: {relative}"
                    ) from exc
                else:
                    raise DegradedIntegrityError(
                        f"migration rollback move has an unknown outcome for {relative}; "
                        "all visible paths were preserved"
                    ) from exc
            else:
                quarantine_metadata = _lstat_optional(quarantine)

        if quarantine_metadata is None:
            continue
        if _identity(quarantine_metadata) != expected_identity:
            _restore_quarantine_if_possible(quarantine, path)
            raise ConflictError(
                f"migration rollback target changed identity during removal: {relative}; "
                "the replacement was preserved"
            )
        _validate_owned_directory(
            quarantine,
            quarantine_metadata,
            relative=relative,
            ownership=ownership,
            backup_path=backup_path,
        )
        _publish_removal_marker(marker, relative, ownership)

        # Re-read after the marker write. Content arriving during the marker
        # fsync is preserved by restoring the owned directory when possible.
        quarantine_metadata = _lstat_optional(quarantine)
        if quarantine_metadata is None:
            raise DegradedIntegrityError(
                f"migration removal quarantine disappeared for {relative}; no deletion was "
                "attempted"
            )
        if _identity(quarantine_metadata) != expected_identity:
            raise DegradedIntegrityError(
                f"migration removal quarantine changed identity for {relative}; all paths "
                "were preserved"
            )
        try:
            _validate_empty_directory(
                quarantine,
                relative=relative,
                backup_path=backup_path,
            )
        except ConflictError:
            _restore_quarantine_if_possible(quarantine, path)
            raise

        visible = _lstat_optional(path)
        if visible is not None and _identity(visible) == expected_identity:
            raise DegradedIntegrityError(
                f"migration-owned directory is visible at both public and quarantine paths: "
                f"{relative}; both paths were preserved"
            )
        return

    raise DegradedIntegrityError(
        f"migration rollback could not establish a stable removal state for {relative}; "
        "all visible paths were preserved"
    )


def _validate_owned_directory(
    path: Path,
    metadata: os.stat_result,
    *,
    relative: str,
    ownership: DirectoryOwnership,
    backup_path: str,
) -> None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"migration rollback target changed type: {relative}")
    if _identity(metadata) != (ownership.device, ownership.inode):
        raise ConflictError(
            f"migration rollback target changed identity: {relative}; the replacement is not "
            "owned by this migration"
        )
    _validate_empty_directory(path, relative=relative, backup_path=backup_path)


def _validate_empty_directory(path: Path, *, relative: str, backup_path: str) -> None:
    try:
        entries = {entry.name for entry in os.scandir(path)}
    except OSError as exc:
        raise ValidationError(f"migration rollback target is unavailable: {relative}") from exc
    if entries:
        raise ConflictError(
            f"migration directory contains user or runtime data: {relative}; restore the "
            f"verified backup at {backup_path} into a new target instead"
        )


def _removal_paths(
    path: Path,
    relative: str,
    ownership: DirectoryOwnership,
) -> tuple[Path, Path]:
    token = sha256_bytes(
        f"{MIGRATION_ID}\0{relative}\0{ownership.device}\0{ownership.inode}".encode()
    )[:24]
    stem = f".{path.name}.gsv-remove-{token}"
    return path.parent / f"{stem}.quarantine", path.parent / f"{stem}.marker"


def _removal_marker_bytes(relative: str, ownership: DirectoryOwnership) -> bytes:
    return (
        json.dumps(
            {
                "device": ownership.device,
                "inode": ownership.inode,
                "migration_id": MIGRATION_ID,
                "relative_path": relative,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _validate_removal_marker(
    marker: Path,
    relative: str,
    ownership: DirectoryOwnership,
) -> bool:
    if not os.path.lexists(marker):
        return False
    try:
        visible = read_regular_file(
            marker,
            label="migration removal marker",
            max_bytes=MAX_REMOVAL_MARKER_BYTES,
        )
    except Exception as exc:
        raise DegradedIntegrityError(
            f"migration removal marker is unavailable for {relative}; all paths were preserved"
        ) from exc
    if visible != _removal_marker_bytes(relative, ownership):
        raise DegradedIntegrityError(
            f"migration removal marker changed for {relative}; all paths were preserved"
        )
    return True


def _publish_removal_marker(
    marker: Path,
    relative: str,
    ownership: DirectoryOwnership,
) -> None:
    if _validate_removal_marker(marker, relative, ownership):
        return
    content = _removal_marker_bytes(relative, ownership)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    stage: Path | None = None
    try:
        for _attempt in range(8):
            candidate = marker.with_name(f".{marker.name}.gsv-stage-{secrets.token_hex(16)}")
            try:
                descriptor = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            stage = candidate
            break
        if stage is None:
            raise DegradedIntegrityError(
                f"migration could not reserve a removal-marker stage for {relative}; "
                "all paths were preserved"
            )
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short migration removal marker write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            durable_publish_new(stage, marker)
        except FileExistsError as exc:
            if _validate_removal_marker(marker, relative, ownership):
                return
            raise DegradedIntegrityError(
                f"migration removal marker appeared during publication for {relative}; "
                f"the staged marker was preserved at {stage}"
            ) from exc
        except DurablePublishError as exc:
            marker_matches = _validate_removal_marker(marker, relative, ownership)
            if exc.outcome is PublishOutcome.COMMITTED and marker_matches:
                raise MutationCommittedError(
                    f"migration removal marker is visible for {relative}, but its durability "
                    "was not confirmed; retry from the current receipt"
                ) from exc
            if exc.outcome is PublishOutcome.UNPUBLISHED and not marker_matches:
                raise ValidationError(
                    f"migration could not publish its removal marker for {relative}; "
                    f"the complete stage was preserved at {stage}"
                ) from exc
            raise DegradedIntegrityError(
                f"migration removal-marker publication has an unknown outcome for {relative}; "
                "all visible paths were preserved"
            ) from exc
        if not _validate_removal_marker(marker, relative, ownership):
            raise DegradedIntegrityError(
                f"migration removal marker is missing after publication for {relative}; "
                "all visible paths were preserved"
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restore_quarantine_if_possible(quarantine: Path, path: Path) -> bool:
    if not os.path.lexists(quarantine) or os.path.lexists(path):
        return False
    try:
        _move_no_replace(quarantine, path)
    except OSError:
        return False
    return True


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encode_receipt(receipt: MigrationReceipt) -> bytes:
    payload = receipt.to_dict()
    payload.pop("revision")
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _parse_receipt(encoded: bytes) -> MigrationReceipt:
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("migration receipt is invalid JSON") from exc
    expected = {
        "applied_at",
        "backup_path",
        "backup_sha256",
        "created_directories",
        "directory_ownership",
        "format_version",
        "migration_id",
        "phase",
        "remaining_directories",
        "removed_directories",
        "vault_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValidationError("migration receipt has an unsupported shape")
    if (
        not isinstance(payload["format_version"], int)
        or isinstance(payload["format_version"], bool)
        or payload["format_version"] != MIGRATION_FORMAT_VERSION
        or payload["migration_id"] != MIGRATION_ID
    ):
        raise ValidationError("migration receipt has an unsupported version")
    if not isinstance(payload["vault_id"], str) or not isinstance(payload["backup_path"], str):
        raise ValidationError("migration receipt identity is invalid")
    try:
        canonical_vault_id = str(uuid.UUID(payload["vault_id"]))
    except ValueError as exc:
        raise ValidationError("migration receipt vault ID is invalid") from exc
    if canonical_vault_id != payload["vault_id"]:
        raise ValidationError("migration receipt vault ID is not canonical")
    backup_path = PurePosixPath(payload["backup_path"])
    if (
        backup_path.is_absolute()
        or ".." in backup_path.parts
        or len(backup_path.parts) < 2
        or backup_path.parts[0] != "backups"
    ):
        raise ValidationError("migration receipt backup path is invalid")
    if (
        not isinstance(payload["backup_sha256"], str)
        or len(payload["backup_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in payload["backup_sha256"])
    ):
        raise ValidationError("migration receipt backup digest is invalid")
    created = payload["created_directories"]
    remaining = payload["remaining_directories"]
    removed = payload["removed_directories"]
    if not isinstance(created, list) or any(
        not isinstance(item, str) or item not in FOUNDATION_DIRECTORIES for item in created
    ):
        raise ValidationError("migration receipt directory list is invalid")
    if len(created) != len(set(created)):
        raise ValidationError("migration receipt directory list contains duplicates")
    if (
        not isinstance(remaining, list)
        or not isinstance(removed, list)
        or any(not isinstance(item, str) for item in (*remaining, *removed))
        or any(item not in created for item in (*remaining, *removed))
        or len(remaining) != len(set(remaining))
        or len(removed) != len(set(removed))
        or set(remaining) & set(removed)
        or set(remaining) | set(removed) != set(created)
    ):
        raise ValidationError("migration receipt rollback progress is invalid")
    phase = payload["phase"]
    if not isinstance(phase, str) or phase not in MIGRATION_PHASES:
        raise ValidationError("migration receipt phase is invalid")
    if phase == "applied" and removed:
        raise ValidationError("applied migration receipt cannot record removed directories")
    if phase == "rolled_back" and remaining:
        raise ValidationError("rolled-back migration receipt cannot retain directories")
    raw_ownership = payload["directory_ownership"]
    if not isinstance(raw_ownership, list):
        raise ValidationError("migration receipt ownership proof is invalid")
    ownership: list[DirectoryOwnership] = []
    for item in raw_ownership:
        if (
            not isinstance(item, dict)
            or set(item) != {"device", "inode", "relative_path"}
            or not isinstance(item["relative_path"], str)
            or item["relative_path"] not in created
            or not isinstance(item["device"], int)
            or isinstance(item["device"], bool)
            or item["device"] < 0
            or not isinstance(item["inode"], int)
            or isinstance(item["inode"], bool)
            or item["inode"] < 0
        ):
            raise ValidationError("migration receipt ownership proof is invalid")
        ownership.append(
            DirectoryOwnership(
                relative_path=item["relative_path"],
                device=item["device"],
                inode=item["inode"],
            )
        )
    if {item.relative_path for item in ownership} != set(created) or len(ownership) != len(created):
        raise ValidationError("migration receipt ownership proof is incomplete")
    if not isinstance(payload["applied_at"], str):
        raise ValidationError("migration receipt timestamp is invalid")
    applied_at = stored_time(payload["applied_at"], "migration receipt timestamp")
    return MigrationReceipt(
        format_version=MIGRATION_FORMAT_VERSION,
        migration_id=MIGRATION_ID,
        vault_id=payload["vault_id"],
        backup_path=payload["backup_path"],
        backup_sha256=payload["backup_sha256"],
        created_directories=tuple(created),
        directory_ownership=tuple(ownership),
        phase=phase,
        remaining_directories=tuple(remaining),
        removed_directories=tuple(removed),
        applied_at=applied_at,
        revision=sha256_bytes(encoded),
    )


def _rollback_result(receipt: MigrationReceipt, *, already_rolled_back: bool) -> dict[str, Any]:
    return {
        "already_rolled_back": already_rolled_back,
        "backup_path": receipt.backup_path,
        "migration_id": MIGRATION_ID,
        "receipt_revision": receipt.revision,
        "removed_directories": list(receipt.removed_directories),
        "rolled_back": True,
    }
