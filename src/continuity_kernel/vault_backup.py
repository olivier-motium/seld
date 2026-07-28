"""Crash-safe, cross-platform backup and restore primitives for one vault."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import sys
import unicodedata
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp as _tempfile_mkdtemp
from tempfile import mkstemp as _tempfile_mkstemp
from typing import IO, Any, Final

from continuity_kernel.atomic import (
    PINNED_PATH_ROOT_SUPPORTED,
    DurablePublishError,
    PinnedPathRoot,
    PublishOutcome,
    atomic_write,
    durable_publish_new,
    durable_replace,
    durable_unlink,
    exclusive_lock,
    open_regular_file,
    read_regular_file,
    sha256_bytes,
    sha256_regular_file,
)
from continuity_kernel.errors import (
    ConflictError,
    ContinuityError,
    DegradedIntegrityError,
    MutationCommittedError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.records import WINDOWS_RESERVED_NAMES, format_time, stored_time
from continuity_kernel.vault_identity import (
    REQUIRED_VAULT_DIRECTORIES,
    canonical_vault_id,
    parse_vault_manifest,
)

MAX_BACKUP_ENTRIES: Final = 10_000
MAX_BACKUP_ENTRY_BYTES: Final = 16 * 1024 * 1024
MAX_BACKUP_TOTAL_BYTES: Final = 512 * 1024 * 1024
BACKUP_MANIFEST: Final = "GSV_BACKUP.json"
_MIGRATION_TOMBSTONE_MARKER: Final = re.compile(
    r"^\.(?P<name>onboarding|control|migrations)\.gsv-remove-"
    r"(?P<token>[0-9a-f]{24})\.marker$"
)
_MIGRATION_TOMBSTONE_RELATIVE: Final = {
    (".", "onboarding"): "onboarding",
    (".gsv", "control"): ".gsv/control",
    (".gsv", "migrations"): ".gsv/migrations",
}
_MIGRATION_TOMBSTONE_ID: Final = "culture-grade-foundation-v1"
_MIGRATION_TOMBSTONE_MARKER_MAX_BYTES: Final = 1024


def _mkstemp(*, prefix: str, suffix: str, dir: Path) -> tuple[int, str]:
    return _tempfile_mkstemp(prefix=prefix, suffix=suffix, dir=dir)


def _mkdtemp(*, prefix: str, dir: Path) -> str:
    return _tempfile_mkdtemp(prefix=prefix, dir=dir)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


@dataclass(frozen=True)
class _BackupInspection:
    infos: tuple[zipfile.ZipInfo, ...]
    manifest: dict[str, Any]
    actual: dict[str, str]

    @property
    def valid(self) -> bool:
        expected = self.manifest["files"]
        if not isinstance(expected, dict):
            return False
        return expected == self.actual


def _leaf_path(path: Path, *, label: str) -> Path:
    try:
        expanded = path.expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        return expanded.parent.resolve() / expanded.name
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError(f"invalid {label}: {path}: {exc}") from exc


def _generated_backup_destination(root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root / "backups" / f"gsv-{stamp}-{uuid.uuid4().hex}.zip"


def _validate_backup_destination_policy(root: Path, destination: Path) -> None:
    relative = _relative_to_directory_identity(root, destination)
    if relative is None:
        return
    if len(relative) < 2 or not _owned_backups_component(root, relative[0]):
        raise ValidationError(
            "a backup stored inside the vault must be within its owned backups/ directory"
        )


def _validate_backup_destination(path: Path) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValidationError(f"could not inspect backup destination: {path}: {exc}") from exc
    raise ConflictError(f"backup destination already exists and will not be replaced: {path}")


def _raise_backup_staging_io_error(
    *,
    temp: Path,
    destination: Path,
    primary: OSError,
) -> None:
    prefix = f"could not create a staged backup beside {destination}: {primary}"
    try:
        durable_unlink(temp)
    except OSError as cleanup_error:
        cleanup_state = _backup_staging_cleanup_state(temp, include_path=True)
        raise DegradedIntegrityError(
            f"{prefix}; no backup was published; {cleanup_state}; durable cleanup failed: "
            f"{cleanup_error}"
        ) from primary
    raise PersistenceError(
        f"{prefix}; no backup was published and the staged archive was durably discarded"
    ) from primary


def _backup_staging_cleanup_state(path: Path, *, include_path: bool = False) -> str:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "the staged archive is no longer visible, but deletion durability is unconfirmed"
    except OSError:
        return "the staged archive path state is unknown"
    location = f" at {path}" if include_path else ""
    return f"the staged archive remains{location}"


def _scan_backup_directory(
    root: Path,
    directory: Path,
    files: list[tuple[str, Path]],
) -> None:
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise ValidationError(f"could not read vault backup directory: {directory}: {exc}") from exc
    for entry in entries:
        path = Path(entry.path)
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValidationError(f"could not inspect vault backup path: {path}: {exc}") from exc
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError(f"vault backup refuses symbolic link: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            if relative == ".gsv/locks" or (
                directory == root and _owned_backups_component(root, entry.name)
            ):
                continue
            _scan_backup_directory(root, path, files)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(f"vault backup refuses unsupported file type: {relative}")
        # Rollback ownership is inode-bound to this host. A restored vault must
        # plan and establish fresh local ownership instead of inheriting a
        # receipt that could authorize deletion of replacement directories.
        if relative == ".gsv/migration-culture-grade-foundation-v1.json":
            continue
        if _is_owned_migration_tombstone_marker(path, relative):
            continue
        if _is_owned_vault_temp(relative):
            continue
        files.append((relative, path))


def _is_owned_migration_tombstone_marker(path: Path, relative: str) -> bool:
    """Recognize only the exact host-local marker emitted by foundation rollback."""

    portable = PurePosixPath(relative)
    matched = _MIGRATION_TOMBSTONE_MARKER.fullmatch(portable.name)
    if matched is None:
        return False
    parent = portable.parent.as_posix()
    expected_relative = _MIGRATION_TOMBSTONE_RELATIVE.get((parent, matched.group("name")))
    if expected_relative is None:
        return False
    try:
        encoded = read_regular_file(
            path,
            label="migration removal marker",
            max_bytes=_MIGRATION_TOMBSTONE_MARKER_MAX_BYTES,
        )
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        return False
    if not isinstance(payload, dict) or set(payload) != {
        "device",
        "inode",
        "migration_id",
        "relative_path",
    }:
        return False
    device = payload.get("device")
    inode = payload.get("inode")
    if (
        payload.get("migration_id") != _MIGRATION_TOMBSTONE_ID
        or payload.get("relative_path") != expected_relative
        or not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode < 0
    ):
        return False
    token = sha256_bytes(
        f"{_MIGRATION_TOMBSTONE_ID}\0{expected_relative}\0{device}\0{inode}".encode()
    )[:24]
    expected = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if matched.group("token") != token or encoded != expected:
        return False

    quarantine = path.with_suffix(".quarantine")
    try:
        before = os.lstat(quarantine)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or (int(before.st_dev), int(before.st_ino)) != (device, inode)
        ):
            return False
        with os.scandir(quarantine) as entries:
            if next(entries, None) is not None:
                return False
        after = os.lstat(quarantine)
    except OSError:
        return False
    return (
        stat.S_ISDIR(after.st_mode)
        and (int(after.st_dev), int(after.st_ino)) == (device, inode)
        and (int(after.st_dev), int(after.st_ino)) == (int(before.st_dev), int(before.st_ino))
    )


def _relative_to_directory_identity(root: Path, path: Path) -> tuple[str, ...] | None:
    try:
        root_metadata = os.lstat(root)
    except OSError as exc:  # pragma: no cover - manifest validation owns this boundary
        raise ValidationError(f"could not inspect vault root: {root}: {exc}") from exc
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    current = path
    parts: list[str] = []
    while True:
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            metadata = None
        except OSError as exc:
            raise ValidationError(
                f"could not inspect backup destination ancestor: {current}"
            ) from exc
        if metadata is not None and (metadata.st_dev, metadata.st_ino) == root_identity:
            return tuple(parts)
        parent = current.parent
        if parent == current:
            return None
        parts.insert(0, current.name)
        current = parent


def _owned_backups_component(root: Path, component: str) -> bool:
    owned = root / "backups"
    candidate = root / component
    try:
        owned_metadata = os.lstat(owned)
    except FileNotFoundError:
        return component == "backups"
    except OSError as exc:
        raise ValidationError(f"could not inspect owned backup directory: {owned}") from exc
    try:
        candidate_metadata = os.lstat(candidate)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationError(
            f"could not inspect backup destination directory: {candidate}"
        ) from exc
    return (
        stat.S_ISDIR(owned_metadata.st_mode)
        and stat.S_ISDIR(candidate_metadata.st_mode)
        and (owned_metadata.st_dev, owned_metadata.st_ino)
        == (candidate_metadata.st_dev, candidate_metadata.st_ino)
    )


def _is_owned_vault_temp(relative: str) -> bool:
    path = PurePosixPath(relative)
    name = path.name
    if not name.startswith(".") or ".tmp-" not in name:
        return False
    target_name, token = name[1:].rsplit(".tmp-", 1)
    if not target_name or not token or "." in token:
        return False
    parent = path.parent.as_posix()
    if parent == ".":
        return target_name in {
            "AGENTS.md",
            "DIRECTION.md",
            "MIND.md",
            "NOW.md",
            "PORTFOLIO.md",
            "README.md",
            "SOURCES.md",
        }
    if parent in {"tasks", "entities", "threads"}:
        return target_name.endswith(".md")
    if parent == "onboarding":
        return target_name == "session.md"
    if parent == ".gsv/control":
        return target_name in {"initialized", "queue.jsonl"} or (
            target_name.startswith("dispositions-") and target_name.endswith(".jsonl")
        )
    if parent == ".gsv/control/archive":
        return target_name.startswith("queue-") and target_name.endswith(".jsonl")
    if parent == ".gsv/control/runtime/turns":
        return target_name.endswith(".json")
    return (
        parent == ".gsv"
        and target_name in {"manifest.json", "migration-culture-grade-foundation-v1.json"}
    ) or (parent == "journal" and target_name == "events.jsonl")


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    return metadata.st_dev, metadata.st_ino


def _regular_file_matches(path: Path, identity: tuple[int, int], digest: str) -> bool:
    try:
        listed = os.lstat(path)
        if not stat.S_ISREG(listed.st_mode):
            return False
        if (listed.st_dev, listed.st_ino) != identity:
            return False
        captured = hashlib.sha256()
        with open_regular_file(path, label="published backup") as (descriptor, _):
            while block := os.read(descriptor, 1024 * 1024):
                captured.update(block)
        final = os.lstat(path)
        return (
            stat.S_ISREG(final.st_mode)
            and (final.st_dev, final.st_ino) == identity
            and captured.hexdigest() == digest
        )
    except (OSError, ValidationError):
        return False


def _directory_matches(path: Path, identity: tuple[int, int] | None) -> bool:
    if identity is None:
        return False
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity


def _restored_vault_matches(
    path: Path, *, identity: tuple[int, int], vault_id: str, digest: str
) -> bool:
    from continuity_kernel.vault import Vault

    if not _directory_matches(path, identity):
        return False
    try:
        vault = Vault(path)
        matches = vault.identity()["vault_id"] == vault_id and vault.logical_digest() == digest
    except (OSError, ContinuityError):
        return False
    return matches and _directory_matches(path, identity)


def _restore_prior_target(
    prior: Path,
    target: Path,
    *,
    identity: tuple[int, int] | None,
    cause: Exception,
) -> None:
    if not _directory_matches(prior, identity) or target.exists():
        raise DegradedIntegrityError(
            f"restore was not published, but the prior target could not be safely restored; "
            f"inspect {target} and {prior} before retrying"
        ) from cause
    try:
        durable_replace(prior, target)
    except Exception as rollback_error:
        if _directory_matches(target, identity) and not prior.exists():
            raise DegradedIntegrityError(
                f"restore was not published; the prior target is visible again at {target}, but "
                "directory durability could not be confirmed. Inspect it before retrying"
            ) from rollback_error
        raise DegradedIntegrityError(
            f"restore was not published and the prior target could not be restored; inspect "
            f"{target} and {prior} before retrying"
        ) from rollback_error
    if not _directory_matches(target, identity) or prior.exists():
        raise DegradedIntegrityError(
            f"restore was not published, but prior-target recovery could not be verified; inspect "
            f"{target} and {prior} before retrying"
        ) from cause


@dataclass(frozen=True)
class _BackupAncestry:
    directories: tuple[tuple[Path, tuple[int, int]], ...]


def _capture_backup_ancestry(root: Path, path: Path) -> _BackupAncestry:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValidationError("vault backup source escaped its canonical root") from exc
    directories: list[tuple[Path, tuple[int, int]]] = []
    current = root
    for component in (None, *relative.parts[:-1]):
        if component is not None:
            current /= component
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ValidationError(
                f"vault backup source ancestry is unavailable: {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError(
                f"vault backup source ancestry is not a real directory: {current}"
            )
        directories.append((current, (int(metadata.st_dev), int(metadata.st_ino))))
    return _BackupAncestry(tuple(directories))


def _validate_backup_ancestry(ancestry: _BackupAncestry) -> None:
    for path, identity in ancestry.directories:
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise ValidationError(
                f"vault backup source ancestry changed while it was read: {path}: {exc}"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or (int(metadata.st_dev), int(metadata.st_ino)) != identity
        ):
            raise ValidationError(f"vault backup source ancestry changed while it was read: {path}")


_BackupSourceStore = PinnedPathRoot | _BackupAncestry


@contextmanager
def _backup_source_store(root: Path) -> Iterator[_BackupSourceStore]:
    if not PINNED_PATH_ROOT_SUPPORTED:
        ancestry = _capture_backup_ancestry(root, root / ".gsv/manifest.json")
        try:
            yield ancestry
        finally:
            _validate_backup_ancestry(ancestry)
        return
    store = PinnedPathRoot(root)
    try:
        yield store
    finally:
        store.close()


def _read_backup_source(
    path: Path,
    *,
    root: Path,
    store: _BackupSourceStore,
) -> bytes:
    if isinstance(store, PinnedPathRoot):
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValidationError("vault backup source escaped its pinned root") from exc
        content = store.read_regular_file(
            relative,
            label="vault backup file",
            max_bytes=MAX_BACKUP_ENTRY_BYTES,
        )
        assert content is not None
        return content
    _validate_backup_ancestry(store)
    ancestry = _capture_backup_ancestry(root, path)
    content = read_regular_file(
        path,
        label="vault backup file",
        max_bytes=MAX_BACKUP_ENTRY_BYTES,
    )
    _validate_backup_ancestry(ancestry)
    _validate_backup_ancestry(store)
    return content


def _hash_backup_files(
    files: list[tuple[str, Path]],
    *,
    root: Path,
    store: _BackupSourceStore,
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    total = 0
    for relative, path in files:
        content = _read_backup_source(path, root=root, store=store)
        total += len(content)
        if total > MAX_BACKUP_TOTAL_BYTES:
            raise ValidationError("vault backup exceeds its total size bound")
        hashes[relative] = sha256_bytes(content)
    return hashes


@contextmanager
def _open_backup(path: Path) -> Iterator[tuple[Path, IO[bytes]]]:
    opened_path = _leaf_path(path, label="backup path")
    try:
        before = os.lstat(opened_path)
    except OSError as exc:
        raise ValidationError(f"invalid backup: {opened_path}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ValidationError(f"backup cannot be a symbolic link: {opened_path}")
    if not stat.S_ISREG(before.st_mode):
        raise ValidationError(f"backup must be a regular file: {opened_path}")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(opened_path, flags)
    except OSError as exc:
        raise ValidationError(f"invalid backup: {opened_path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValidationError(f"backup must remain a regular file: {opened_path}")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError(f"backup changed while it was being opened: {opened_path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            yield opened_path, handle
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError(f"invalid backup: {opened_path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _inspect_backup(handle: IO[bytes], path: Path) -> _BackupInspection:
    try:
        handle.seek(0)
        with zipfile.ZipFile(handle, "r") as archive:
            infos, manifest = _backup_metadata(archive)
            actual: dict[str, str] = {}
            total = 0
            for info in infos:
                if info.filename == BACKUP_MANIFEST or info.is_dir():
                    continue
                content = _read_archive_entry(archive, info)
                total += len(content)
                if total > MAX_BACKUP_TOTAL_BYTES:
                    raise ValidationError("backup exceeds its actual total size bound")
                actual[info.filename] = sha256_bytes(content)
        return _BackupInspection(infos=infos, manifest=manifest, actual=actual)
    except ValidationError:
        raise
    except (
        OSError,
        EOFError,
        KeyError,
        RuntimeError,
        NotImplementedError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise ValidationError(f"invalid backup: {path}") from exc


def _extract_backup(handle: IO[bytes], path: Path, stage: Path) -> _BackupInspection:
    """Extract and hash each member from one read of one pinned archive descriptor."""

    try:
        handle.seek(0)
        with zipfile.ZipFile(handle, "r") as archive:
            infos, manifest = _backup_metadata(archive)
            actual: dict[str, str] = {}
            total = 0
            for info in infos:
                name = info.filename
                parts, is_directory, _ = _portable_archive_path(name)
                if name == BACKUP_MANIFEST or is_directory:
                    continue
                content = _read_archive_entry(archive, info)
                total += len(content)
                if total > MAX_BACKUP_TOTAL_BYTES:
                    raise ValidationError("backup exceeds its actual total size bound")
                actual[name] = sha256_bytes(content)
                destination = stage.joinpath(*parts)
                try:
                    destination.relative_to(stage)
                except ValueError as exc:
                    raise ValidationError(f"unsafe backup entry: {name}") from exc
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(destination, content)
        return _BackupInspection(infos=infos, manifest=manifest, actual=actual)
    except ValidationError:
        raise
    except (
        OSError,
        EOFError,
        KeyError,
        RuntimeError,
        NotImplementedError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise ValidationError(f"invalid backup: {path}") from exc


def _backup_metadata(
    archive: zipfile.ZipFile,
) -> tuple[tuple[zipfile.ZipInfo, ...], dict[str, Any]]:
    infos = tuple(archive.infolist())
    _validate_archive_infos(list(infos))
    manifest = json.loads(_read_archive_entry(archive, archive.getinfo(BACKUP_MANIFEST)))
    _validate_backup_manifest(manifest)
    return infos, manifest


def _read_archive_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with archive.open(info, "r") as member:
        content = member.read(MAX_BACKUP_ENTRY_BYTES + 1)
    if len(content) > MAX_BACKUP_ENTRY_BYTES:
        raise ValidationError(f"backup entry exceeds its actual size bound: {info.filename}")
    return content


def _validate_backup_manifest(manifest: object) -> None:
    if not isinstance(manifest, dict) or set(manifest) != {
        "created_at",
        "files",
        "format_version",
        "vault_id",
    }:
        raise ValidationError("backup manifest has an unsupported shape")
    if type(manifest.get("format_version")) is not int:
        raise ValidationError("unsupported backup manifest version")
    if manifest["format_version"] != 1:
        raise ValidationError("unsupported backup manifest version")
    canonical_vault_id(manifest.get("vault_id"))
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise ValidationError("backup manifest has an invalid creation time")
    try:
        clean_created_at = stored_time(created_at, "backup creation time")
    except ValidationError as exc:
        raise ValidationError("backup manifest has an invalid creation time") from exc
    if clean_created_at != created_at:
        raise ValidationError("backup manifest creation time is not canonical")
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise ValidationError("backup manifest has no file map")
    for name, digest in expected.items():
        if not isinstance(name, str) or not name:
            raise ValidationError("backup manifest contains an invalid file name")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError(f"backup manifest has an invalid SHA-256 digest: {name}")


def _restore_target(path: Path) -> Path:
    return _leaf_path(path, label="restore target")


def _restore_stage_state(path: Path, identity: tuple[int, int]) -> str:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    if stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity:
        return "retained"
    return "changed"


def _raise_retained_restore_stage(
    *,
    stage: Path,
    state: str,
    primary: BaseException | None,
) -> None:
    if state == "retained":
        detail = (
            f"the unpublished restore stage is retained at {stage}; inspect it before removing "
            "that exact directory"
        )
        if isinstance(primary, ValidationError):
            raise ValidationError(f"{primary}; {detail}") from primary
        if isinstance(primary, ConflictError):
            raise ConflictError(f"{primary}; {detail}") from primary
        if primary is None:
            raise DegradedIntegrityError(f"restore did not consume its stage; {detail}")
        raise DegradedIntegrityError(f"{primary}; {detail}") from primary

    detail = (
        f"restore stage identity is {state} at {stage}; the path may contain replacement data and "
        "the original stage location is unknown. No recovery path was deleted"
    )
    if primary is None:
        raise DegradedIntegrityError(detail)
    raise DegradedIntegrityError(f"{primary}; {detail}") from primary


def _validate_restore_target(target: Path) -> bool:
    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationError(f"could not inspect restore target: {target}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValidationError(f"restore target cannot be a symbolic link: {target}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ConflictError(f"restore target must be an absent or empty directory: {target}")
    try:
        next(target.iterdir())
    except StopIteration:
        return True
    except OSError as exc:
        raise ValidationError(f"could not inspect restore target: {target}: {exc}") from exc
    raise ConflictError(f"restore target is not empty: {target}")


def _restore_required_directories(stage: Path) -> None:
    for relative in REQUIRED_VAULT_DIRECTORIES:
        path = stage / relative
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            try:
                path.mkdir(parents=True)
            except OSError as exc:
                raise ValidationError(
                    f"could not restore required vault directory: {relative}: {exc}"
                ) from exc
            continue
        except OSError as exc:
            raise ValidationError(
                f"could not inspect restored vault directory: {relative}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError(f"restored required vault path is not a directory: {relative}")


def _remove_prior_empty(path: Path) -> str | None:
    try:
        path.rmdir()
    except OSError as exc:
        return f"restored vault published, but the prior empty target remains at {path}: {exc}"
    return None


def _validate_archive_infos(infos: list[zipfile.ZipInfo]) -> None:
    names = [item.filename for item in infos]
    if BACKUP_MANIFEST not in names:
        raise ValidationError("backup manifest is missing")
    if len(infos) > MAX_BACKUP_ENTRIES:
        raise ValidationError("backup contains too many entries")
    seen: set[tuple[str, ...]] = set()
    spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    path_kinds: dict[tuple[str, ...], str] = {}
    total = 0
    for info in infos:
        name = info.filename
        parts, is_directory, normalized = _portable_archive_path(name)
        if normalized in seen:
            raise ValidationError(f"duplicate backup entry: {name}")
        seen.add(normalized)
        for index in range(1, len(parts) + 1):
            key = normalized[:index]
            spelling = parts[:index]
            previous_spelling = spellings.setdefault(key, spelling)
            if previous_spelling != spelling:
                raise ValidationError(f"duplicate backup entry alias: {name}")
            kind = "directory" if index < len(parts) or is_directory else "file"
            previous_kind = path_kinds.setdefault(key, kind)
            if previous_kind != kind:
                raise ValidationError(f"conflicting backup entry path: {name}")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        allowed_type = stat.S_IFDIR if is_directory else stat.S_IFREG
        if file_type not in {0, allowed_type}:
            raise ValidationError(f"unsupported backup entry type: {name}")
        if info.flag_bits & 0x1:
            raise ValidationError(f"encrypted backup entry is unsupported: {name}")
        if info.file_size > MAX_BACKUP_ENTRY_BYTES:
            raise ValidationError(f"backup entry exceeds its size bound: {name}")
        total += info.file_size
        if total > MAX_BACKUP_TOTAL_BYTES:
            raise ValidationError("backup exceeds its total size bound")


def _portable_archive_path(name: str) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        raise ValidationError(f"unsafe backup entry: {name}")
    is_directory = name.endswith("/")
    raw = name[:-1] if is_directory else name
    parts = tuple(raw.split("/"))
    if not raw or any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"unsafe backup entry: {name}")

    normalized: list[str] = []
    for part in parts:
        if (
            ":" in part
            or any(character in '<>"|?*' for character in part)
            or any(ord(character) < 32 for character in part)
        ):
            raise ValidationError(f"non-portable backup entry: {name}")
        if part.endswith((" ", ".")):
            raise ValidationError(f"non-portable backup entry: {name}")
        canonical = unicodedata.normalize("NFC", part)
        device_stem = canonical.split(".", 1)[0].casefold()
        if device_stem in WINDOWS_RESERVED_NAMES:
            raise ValidationError(f"non-portable backup entry: {name}")
        normalized.append(canonical.casefold())
    return parts, is_directory, tuple(normalized)


def create_backup(vault: Any, destination: Path | None = None) -> dict[str, Any]:
    vault._manifest()
    generated_destination = destination is None
    if generated_destination:
        destination = _generated_backup_destination(vault.root)
    assert destination is not None
    destination = _leaf_path(destination, label="backup output path")
    _validate_backup_destination_policy(vault.root, destination)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PersistenceError(
            f"could not prepare backup destination directory for {destination}: {exc}"
        ) from exc
    if not generated_destination:
        _validate_backup_destination(destination)
    with exclusive_lock(vault.state / "locks/global.lock"):
        try:
            descriptor, temp_name = _mkstemp(
                prefix=".gsv-backup.tmp-", suffix=".zip", dir=destination.parent
            )
        except OSError as exc:
            raise PersistenceError(
                f"could not allocate a staged backup beside {destination}: {exc}"
            ) from exc
        temp = Path(temp_name)
        preserve_staged = False
        try:
            try:
                try:
                    os.close(descriptor)
                finally:
                    # A failed close has an ambiguous descriptor state. Never retry it and risk
                    # closing a descriptor that the process has already reused.
                    descriptor = -1
                with _backup_source_store(vault.root) as source_store:
                    manifest_path = vault.root / ".gsv/manifest.json"
                    manifest_before = _read_backup_source(
                        manifest_path,
                        root=vault.root,
                        store=source_store,
                    )
                    vault_manifest = parse_vault_manifest(manifest_before)
                    files = vault._backup_files()
                    hashes: dict[str, str] = {}
                    total = 0
                    with zipfile.ZipFile(
                        temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
                    ) as archive:
                        for relative, path in files:
                            content = _read_backup_source(
                                path,
                                root=vault.root,
                                store=source_store,
                            )
                            total += len(content)
                            if total > MAX_BACKUP_TOTAL_BYTES:
                                raise ValidationError("vault backup exceeds its total size bound")
                            hashes[relative] = sha256_bytes(content)
                            archive.writestr(relative, content)
                        manifest_after = _read_backup_source(
                            manifest_path,
                            root=vault.root,
                            store=source_store,
                        )
                        if manifest_after != manifest_before:
                            raise ValidationError(
                                "vault identity changed while its backup was created"
                            )
                        manifest = {
                            "created_at": format_time(datetime.now(UTC)),
                            "files": hashes,
                            "format_version": 1,
                            "vault_id": vault_manifest["vault_id"],
                        }
                        archive.writestr(BACKUP_MANIFEST, _json_bytes(manifest))
                with temp.open("r+b") as handle:
                    os.fsync(handle.fileno())
                staged_verification = vault.verify_backup(temp)
                if not staged_verification["valid"]:
                    raise PersistenceError(
                        "backup staging verification failed; no backup was published"
                    )
                staged_identity = _path_identity(temp)
                staged_hash = sha256_regular_file(temp, label="staged backup")
            except OSError as exc:
                preserve_staged = True
                _raise_backup_staging_io_error(
                    temp=temp,
                    destination=destination,
                    primary=exc,
                )
            published_identity = staged_identity
            collisions = 0
            while True:
                try:
                    published_identity = durable_publish_new(temp, destination)
                except FileExistsError as exc:
                    if not generated_destination:
                        raise ConflictError(
                            f"backup destination already exists and was not replaced: {destination}"
                        ) from exc
                    collisions += 1
                    if collisions >= 16:
                        raise ConflictError(
                            "could not allocate a new default backup name after 16 collisions"
                        ) from exc
                    destination = _generated_backup_destination(vault.root)
                    continue
                except DurablePublishError as exc:
                    if exc.outcome is PublishOutcome.COMMITTED:
                        preserve_staged = True
                        raise MutationCommittedError(str(exc)) from exc
                    if exc.outcome is PublishOutcome.UNPUBLISHED:
                        try:
                            durable_unlink(temp)
                        except OSError as cleanup_error:
                            preserve_staged = True
                            cleanup_state = _backup_staging_cleanup_state(temp)
                            raise DegradedIntegrityError(
                                f"{exc}; backup staging cleanup failed at {temp}; "
                                f"{cleanup_state}: {cleanup_error}"
                            ) from exc
                        raise PersistenceError(str(exc)) from exc
                    preserve_staged = True
                    raise DegradedIntegrityError(str(exc)) from exc
                except Exception as exc:
                    if _regular_file_matches(destination, staged_identity, staged_hash):
                        preserve_staged = temp.exists()
                        staged_note = (
                            f"; the staged archive also remains at {temp}"
                            if preserve_staged
                            else ""
                        )
                        raise MutationCommittedError(
                            f"backup was published at {destination}, but directory durability "
                            "could not be confirmed; run "
                            "`gsv backup verify "
                            f"{shlex.quote(str(destination))}` before using it{staged_note}"
                        ) from exc
                    if temp.exists():
                        try:
                            durable_unlink(temp)
                        except OSError as cleanup_error:
                            preserve_staged = True
                            cleanup_state = _backup_staging_cleanup_state(temp)
                            raise DegradedIntegrityError(
                                f"backup was not published at {destination}, but staged "
                                f"archive cleanup failed at {temp}; {cleanup_state}: "
                                f"{cleanup_error}"
                            ) from exc
                        raise PersistenceError(
                            f"backup was not published at {destination}; the staged archive "
                            "was durably discarded"
                        ) from exc
                    preserve_staged = True
                    raise DegradedIntegrityError(
                        "could not determine whether backup publication changed "
                        f"{destination}; "
                        "inspect that path before retrying"
                    ) from exc
                break
        finally:
            if temp.exists() and not preserve_staged:
                primary = sys.exc_info()[1]
                try:
                    durable_unlink(temp)
                except OSError as cleanup_error:
                    cleanup_state = _backup_staging_cleanup_state(temp)
                    if primary is None:
                        raise DegradedIntegrityError(
                            f"backup staging cleanup failed at {temp}; {cleanup_state}: "
                            f"{cleanup_error}"
                        ) from cleanup_error
                    raise DegradedIntegrityError(
                        f"{primary}; backup staging cleanup failed at {temp}; "
                        f"{cleanup_state}: {cleanup_error}"
                    ) from primary
    try:
        if not _regular_file_matches(destination, published_identity, staged_hash):
            raise DegradedIntegrityError(
                f"backup destination changed before verification: {destination}"
            )
        verification = vault.verify_backup(destination)
    except ValidationError as exc:
        raise DegradedIntegrityError(
            f"backup was published at {destination}, but post-publication verification failed; "
            "do not use it until it passes `gsv backup verify`"
        ) from exc
    if not _regular_file_matches(destination, published_identity, staged_hash):
        raise DegradedIntegrityError(
            f"backup destination changed during verification: {destination}; inspect it before "
            "retrying"
        )
    if not verification["valid"]:
        raise DegradedIntegrityError(
            f"backup was published at {destination}, but its hashes no longer verify; "
            "do not use it until it passes `gsv backup verify`"
        )
    return {
        "backup": str(destination),
        "files": len(files),
        "sha256": staged_hash,
        "verified": True,
        "vault_id": manifest["vault_id"],
    }


def verify_backup(path: Path) -> dict[str, Any]:
    with _open_backup(path) as (opened_path, handle):
        inspection = _inspect_backup(handle, opened_path)
    return {
        "backup": str(opened_path),
        "files": len(inspection.actual),
        "valid": inspection.valid,
        "vault_id": inspection.manifest["vault_id"],
    }


def restore_backup(path: Path, target: Path) -> dict[str, Any]:
    # Local import avoids a module cycle while keeping backup mechanics separate
    # from the authoritative Vault mutation surface.
    from continuity_kernel.vault import Vault

    target = _restore_target(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PersistenceError(
            f"could not prepare the restore target parent for {target}: {exc}"
        ) from exc
    _validate_restore_target(target)
    try:
        stage = Path(_mkdtemp(prefix=f".{target.name}.tmp-restore-", dir=target.parent))
    except OSError as exc:
        raise PersistenceError(
            f"could not allocate a restore stage beside {target}: {exc}"
        ) from exc
    try:
        stage_identity = _path_identity(stage)
    except OSError as exc:
        raise DegradedIntegrityError(
            f"a restore stage was allocated at {stage}, but its identity could not be pinned; "
            "inspect that path before retrying"
        ) from exc
    prior_empty: Path | None = None
    cleanup_warning: str | None = None
    try:
        with _open_backup(path) as (opened_path, handle):
            inspection = _extract_backup(handle, opened_path, stage)
            if not inspection.valid:
                raise ValidationError("backup file hashes do not match its manifest")
        _restore_required_directories(stage)
        restored_stage = Vault(stage)
        doctor = restored_stage.doctor()
        if not doctor.healthy:
            issue_summary = "; ".join(
                f"{issue.path}: {issue.message}" for issue in doctor.issues[:3]
            )
            raise ValidationError(
                f"restored vault did not pass validation before publication: {issue_summary}"
            )
        if restored_stage.identity()["vault_id"] != inspection.manifest["vault_id"]:
            raise ValidationError("restored vault identity does not match its backup manifest")
        digest = restored_stage.logical_digest()
        with _backup_source_store(stage) as source_store:
            staged_hashes = _hash_backup_files(
                restored_stage._backup_files(),
                root=stage,
                store=source_store,
            )
        if staged_hashes != inspection.manifest["files"]:
            raise ValidationError(
                "staged vault files do not match the backup manifest before publication"
            )
        target_existed = _validate_restore_target(target)
        prior_identity: tuple[int, int] | None = None
        if target_existed:
            prior_identity = _path_identity(target)
            try:
                prior_empty = Path(
                    _mkdtemp(prefix=f".{target.name}.tmp-restore-prior-", dir=target.parent)
                )
            except OSError as exc:
                raise PersistenceError(
                    f"could not allocate prior-target recovery beside {target}: {exc}"
                ) from exc
            prior_empty.rmdir()
            try:
                durable_replace(target, prior_empty)
            except Exception as exc:
                if not target.exists() and _directory_matches(prior_empty, prior_identity):
                    # A successful stage publication fsyncs this same parent directory.
                    pass
                elif _directory_matches(target, prior_identity) and not prior_empty.exists():
                    raise PersistenceError(
                        f"restore was not published; the existing empty target at {target} "
                        "remains in place"
                    ) from exc
                else:
                    raise DegradedIntegrityError(
                        "could not determine whether the existing empty restore target moved; "
                        f"inspect {target} and {prior_empty} before retrying"
                    ) from exc
        try:
            if prior_empty is not None:
                _validate_restore_target(prior_empty)
            durable_replace(stage, target)
        except Exception as exc:
            if not stage.exists() and _restored_vault_matches(
                target,
                identity=stage_identity,
                vault_id=str(inspection.manifest["vault_id"]),
                digest=digest,
            ):
                prior_note = (
                    f" The prior empty target is preserved at {prior_empty}."
                    if prior_empty is not None and prior_empty.exists()
                    else ""
                )
                raise MutationCommittedError(
                    f"restore was published at {target}, but directory durability could not be "
                    "confirmed. Do not restore over this non-empty target; run "
                    f"`gsv --vault {shlex.quote(str(target))} doctor` and inspect it."
                    f"{prior_note}"
                ) from exc
            if _directory_matches(stage, stage_identity):
                if prior_empty is not None:
                    _restore_prior_target(
                        prior_empty,
                        target,
                        identity=prior_identity,
                        cause=exc,
                    )
                raise PersistenceError(f"restore was not published at {target}") from exc
            raise DegradedIntegrityError(
                f"could not determine whether the restore was published at {target}; run "
                f"`gsv --vault {shlex.quote(str(target))} doctor` if the target exists, and "
                "inspect restore temporary paths before retrying"
            ) from exc
        if not _restored_vault_matches(
            target,
            identity=stage_identity,
            vault_id=str(inspection.manifest["vault_id"]),
            digest=digest,
        ):
            raise DegradedIntegrityError(
                f"restore target changed during post-publication verification: {target}; "
                "the reported target was not accepted as the restored vault. Inspect that "
                "path and any displaced sibling before retrying"
            )
        if prior_empty is not None and prior_empty.exists():
            cleanup_warning = _remove_prior_empty(prior_empty)
            if cleanup_warning is None:
                prior_empty = None
    finally:
        stage_state = _restore_stage_state(stage, stage_identity)
        if stage_state != "absent":
            primary = sys.exc_info()[1]
            _raise_retained_restore_stage(
                stage=stage,
                state=stage_state,
                primary=primary,
            )
    return {
        "backup": str(opened_path),
        "durability_confirmed": True,
        "restored": str(target),
        "published": True,
        "vault_id": inspection.manifest["vault_id"],
        "digest": digest,
        "cleanup_warning": cleanup_warning,
        "preserved_prior_target": str(prior_empty) if cleanup_warning else None,
    }
