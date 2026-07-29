"""Bounded, read-only activation of user-imported resident context."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol, cast

from continuity_kernel.atomic import PINNED_PATH_ROOT_SUPPORTED, read_regular_file
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.privacy import AwarenessDecision, screen_local_content
from continuity_kernel.records import (
    TERMINAL_TASK_STATUSES,
    TERMINAL_THREAD_STATUSES,
    Task,
    WorkThread,
)

RESIDENT_ROOT: Final = PurePosixPath("context/resident")
RESIDENT_GUIDANCE: Final = RESIDENT_ROOT / "AGENTS.md"
RESIDENT_SKILLS: Final = RESIDENT_ROOT / "skills"
LEGACY_RESIDENT_CONTROL: Final = RESIDENT_ROOT / "control"
MAX_GUIDANCE_BYTES: Final = 256 * 1024
MAX_SKILL_FILE_BYTES: Final = 256 * 1024
MAX_SKILL_TOTAL_BYTES: Final = 16 * 1024 * 1024
MAX_SKILLS: Final = 256
MAX_SKILL_FILES: Final = 4_096
MAX_SKILL_ENTRIES: Final = 8_192
MAX_SKILL_DEPTH: Final = 16
MAX_SKILL_PATH_CHARACTERS: Final = 512
MAX_EXECUTION_BINDINGS: Final = 4_096
_SKILL_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_FRONTMATTER_NAME: Final = re.compile(r"^name\s*:\s*(?P<value>.+?)\s*$")
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_PROTECTED_COMPONENTS: Final = frozenset(
    {".aws", ".azure", ".git", ".gnupg", ".kube", ".ssh", "keychains", "wallets"}
)
_PROTECTED_FILENAMES: Final = frozenset(
    {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "secrets.json"}
)
_PROTECTED_SUFFIXES: Final = (".kdbx", ".key", ".p12", ".pfx", ".pem", ".wallet")
_SECRET_PATH_NAME: Final = re.compile(
    r"(?:^|\.)(?:credential|credentials|password|passwords|secret|secrets)(?:\.|$)"
)
_HIGH_ENTROPY_SHAPE: Final = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9])")
_NUMERIC_CONFIG_SHAPE: Final = re.compile(rb"[A-Z][A-Z0-9_]{15,}=[0-9]{1,32}")
_DOMAIN_PATH_PREFIX: Final = re.compile(rb"(?:^|[\s(<`])(?:[A-Za-z0-9-]+\.){2,}$")
_POSIX_OS = cast(Any, os)


@dataclass(frozen=True)
class ResidentSkillFile:
    relative_path: str
    content: bytes
    executable: bool


@dataclass(frozen=True)
class ResidentSkill:
    directory: str
    name: str
    files: tuple[ResidentSkillFile, ...]
    directories: tuple[str, ...]
    sha256: str
    total_bytes: int


@dataclass
class _ScanBounds:
    entries: int = 0
    files: int = 0
    total_bytes: int = 0


class _ExecutionBindingSource(Protocol):
    def list_tasks(self) -> list[Task]: ...

    def list_threads(self) -> list[WorkThread]: ...


def execution_bindings(source: _ExecutionBindingSource) -> dict[str, Any]:
    """Project complete explicit runtime bindings without semantic selection."""

    active_hands = [
        {
            "active_thread_id": task.active_thread_id,
            "revision": task.revision,
            "status": task.status,
            "task_id": task.identifier,
        }
        for task in sorted(source.list_tasks(), key=lambda item: item.identifier)
        if task.status not in TERMINAL_TASK_STATUSES and task.active_thread_id is not None
    ]
    focused_threads = [
        {
            "focus_task_id": thread.focus_task_id,
            "revision": thread.revision,
            "status": thread.status,
            "thread_id": thread.identifier,
        }
        for thread in sorted(source.list_threads(), key=lambda item: item.identifier)
        if thread.status not in TERMINAL_THREAD_STATUSES and thread.focus_task_id is not None
    ]
    if len(active_hands) + len(focused_threads) > MAX_EXECUTION_BINDINGS:
        raise ValidationError("explicit execution bindings exceed the bounded read surface")
    return {
        "active_hand_count": len(active_hands),
        "active_hands": active_hands,
        "focused_thread_count": len(focused_threads),
        "focused_threads": focused_threads,
        "format_version": 1,
    }


def read_resident_guidance(vault_root: Path) -> dict[str, Any]:
    """Read exact imported AGENTS guidance through one bounded no-follow path."""

    content = _read_resident_file(
        vault_root,
        RESIDENT_GUIDANCE,
        label="imported resident AGENTS guidance",
        max_bytes=MAX_GUIDANCE_BYTES,
        missing_ok=False,
    )
    assert content is not None
    text = validate_imported_resident_content(
        content,
        label="imported resident AGENTS guidance",
    )
    return {
        "bytes": len(content),
        "content": text,
        "path": RESIDENT_GUIDANCE.as_posix(),
        "sha256": sha256(content).hexdigest(),
    }


def resident_context_status(vault_root: Path) -> dict[str, Any]:
    """Return content-free activation status for imported guidance and skills."""

    guidance = _read_resident_file(
        vault_root,
        RESIDENT_GUIDANCE,
        label="imported resident AGENTS guidance",
        max_bytes=MAX_GUIDANCE_BYTES,
        missing_ok=True,
    )
    guidance_status: dict[str, Any]
    if guidance is None:
        guidance_status = {"path": RESIDENT_GUIDANCE.as_posix(), "present": False}
    else:
        validate_imported_resident_content(
            guidance,
            label="imported resident AGENTS guidance",
        )
        guidance_status = {
            "bytes": len(guidance),
            "path": RESIDENT_GUIDANCE.as_posix(),
            "present": True,
            "sha256": sha256(guidance).hexdigest(),
        }
    skills, shared_files = _resident_skill_inventory(vault_root)
    return {
        "available": guidance is not None or bool(skills),
        # Legacy private-host control projections are deliberately inert. Host
        # task and hand identity must be established by installed onboarding,
        # never revived from imported vault context.
        "excluded_paths": [LEGACY_RESIDENT_CONTROL.as_posix()],
        "format_version": 1,
        "guidance": guidance_status,
        "skills": [
            {
                "files": len(skill.files),
                "directory": skill.directory,
                "name": skill.name,
                "sha256": skill.sha256,
                "total_bytes": skill.total_bytes,
            }
            for skill in skills
        ],
        "shared_files": [
            {
                "bytes": len(item.content),
                "path": path,
                "sha256": sha256(item.content).hexdigest(),
            }
            for path, item in sorted(shared_files.items())
        ],
        "skills_total": len(skills),
    }


def resident_skills(vault_root: Path) -> tuple[ResidentSkill, ...]:
    """Snapshot all imported skills without following links or guessing relevance."""

    return _resident_skill_inventory(vault_root)[0]


def _resident_skill_inventory(
    vault_root: Path,
) -> tuple[tuple[ResidentSkill, ...], dict[str, ResidentSkillFile]]:

    entries = _snapshot_skill_tree(vault_root)
    if entries is None:
        return (), {}
    files, directories = entries
    _reject_portable_collisions(
        [*files, *directories],
        label="imported resident skill path",
    )
    top_level: dict[str, list[ResidentSkillFile]] = {}
    skill_directories: dict[str, list[str]] = {}
    shared_files: dict[str, ResidentSkillFile] = {}
    for relative, (content, executable) in sorted(files.items()):
        path = PurePosixPath(relative)
        if len(path.parts) < 2:
            shared_files[path.as_posix()] = ResidentSkillFile(path.as_posix(), content, executable)
            continue
        top_level.setdefault(path.parts[0], []).append(
            ResidentSkillFile(
                PurePosixPath(*path.parts[1:]).as_posix(),
                content,
                executable,
            )
        )
    for relative in sorted(directories):
        path = PurePosixPath(relative)
        if len(path.parts) == 1:
            skill_directories.setdefault(path.parts[0], [])
        else:
            skill_directories.setdefault(path.parts[0], []).append(
                PurePosixPath(*path.parts[1:]).as_posix()
            )
    directories_with_skills = sorted(set(top_level) | set(skill_directories))
    if len(directories_with_skills) > MAX_SKILLS:
        raise ValidationError("imported resident skills exceed the skill-count bound")
    _reject_portable_collisions(
        directories_with_skills,
        label="imported resident skill directory",
    )

    result: list[ResidentSkill] = []
    declared_names: list[str] = []
    for directory in directories_with_skills:
        _validate_skill_name(directory)
        skill_files = tuple(top_level.get(directory, ()))
        if not skill_files:
            raise ValidationError(f"imported resident skill {directory!r} contains no files")
        by_path = {item.relative_path: item.content for item in skill_files}
        skill_markdown = by_path.get("SKILL.md")
        if skill_markdown is None:
            raise ValidationError(f"imported resident skill {directory!r} is missing SKILL.md")
        declared_name = _skill_frontmatter_name(
            skill_markdown.decode("utf-8"),
            label=f"imported resident skill {directory!r}",
        )
        declared_names.append(declared_name)
        skill_dirs = tuple(skill_directories.get(directory, ()))
        manifest = [*(f"directory\0{value}\n" for value in skill_dirs)]
        manifest.extend(
            f"file\0{item.relative_path}\0{int(item.executable)}\0"
            f"{sha256(item.content).hexdigest()}\n"
            for item in skill_files
        )
        result.append(
            ResidentSkill(
                directory=directory,
                name=declared_name,
                files=skill_files,
                directories=skill_dirs,
                sha256=sha256("".join(sorted(manifest)).encode("utf-8")).hexdigest(),
                total_bytes=sum(len(item.content) for item in skill_files),
            )
        )
    _reject_portable_collisions(declared_names, label="imported resident $skill name")
    return tuple(result), shared_files


def add_resident_skills_to_marketplace(
    vault_root: Path,
    packaged: Mapping[str, bytes | None],
) -> dict[str, bytes | ResidentSkillFile | None]:
    """Merge imported skills into a generated plugin without overwriting any path."""

    result: dict[str, bytes | ResidentSkillFile | None] = dict(packaged)
    portable_paths: dict[str, str] = {}
    for existing in result:
        portable_paths[_portable_collision_key(existing)] = existing
    skills, shared_files = _resident_skill_inventory(vault_root)
    packaged_skill_names = _packaged_skill_names(packaged)
    for skill in skills:
        if _portable_collision_key(skill.name) in packaged_skill_names:
            raise ConflictError(
                "imported resident $skill name collides with a built-in skill: "
                f"{skill.name!r}; preserved the built-in skill"
            )
    shared_root = PurePosixPath("plugins/gsv/skills")
    for relative, shared_file in sorted(shared_files.items()):
        target = (shared_root / relative).as_posix()
        key = _portable_collision_key(target)
        if prior := portable_paths.get(key):
            raise ConflictError(
                "imported resident skill support file collides with an existing generated "
                f"plugin path: {target!r} conflicts with {prior!r}; preserved the built-in path"
            )
        portable_paths[key] = target
        result[target] = shared_file
    for skill in skills:
        root = PurePosixPath("plugins/gsv/skills") / skill.directory
        additions: dict[str, ResidentSkillFile | None] = {root.as_posix(): None}
        additions.update({(root / value).as_posix(): None for value in skill.directories})
        additions.update({(root / item.relative_path).as_posix(): item for item in skill.files})
        for relative, addition_content in sorted(additions.items()):
            key = _portable_collision_key(relative)
            if prior := portable_paths.get(key):
                raise ConflictError(
                    "imported resident skill collides with an existing generated plugin path: "
                    f"{relative!r} conflicts with {prior!r}; preserved the built-in skill"
                )
            portable_paths[key] = relative
            result[relative] = addition_content
    return result


def _packaged_skill_names(packaged: Mapping[str, bytes | None]) -> set[str]:
    names: set[str] = set()
    for relative, content in packaged.items():
        path = PurePosixPath(relative)
        if (
            len(path.parts) == 5
            and path.parts[:3] == ("plugins", "gsv", "skills")
            and path.name == "SKILL.md"
            and isinstance(content, bytes)
        ):
            try:
                text = content.decode("utf-8")
            except UnicodeError as exc:
                raise ValidationError(f"packaged skill is not UTF-8 text: {relative}") from exc
            declared = _skill_frontmatter_name(text, label=f"packaged skill {relative!r}")
            names.add(_portable_collision_key(declared))
    return names


def _snapshot_skill_tree(
    vault_root: Path,
) -> tuple[dict[str, tuple[bytes, bool]], set[str]] | None:
    if PINNED_PATH_ROOT_SUPPORTED:
        with _open_pinned_directory(vault_root, RESIDENT_SKILLS.parts) as descriptor:
            if descriptor is None:
                return None
            bounds = _ScanBounds()
            files: dict[str, tuple[bytes, bool]] = {}
            directories: set[str] = set()
            _scan_pinned_directory(
                descriptor,
                relative=PurePosixPath(),
                files=files,
                directories=directories,
                bounds=bounds,
            )
            return files, directories
    return _snapshot_skill_tree_fallback(vault_root)


@contextmanager
def _open_pinned_directory(
    vault_root: Path,
    relative_parts: tuple[str, ...],
) -> Iterator[int | None]:
    expanded = vault_root.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    try:
        canonical_parent = expanded.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve imported resident vault parent: {exc}") from exc
    canonical_root = canonical_parent / expanded.name
    names = (expanded.name, *relative_parts)
    descriptors: list[int] = []
    links: list[tuple[int, str, tuple[int, int]]] = []
    missing: tuple[int, str] | None = None
    flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
    parent = -1
    canonical_parent_identity: tuple[int, int] | None = None
    try:
        parent = os.open(canonical_parent, flags)
        canonical_parent_metadata = os.fstat(parent)
        if not stat.S_ISDIR(canonical_parent_metadata.st_mode):
            raise ValidationError("imported resident vault parent must be a real directory")
        canonical_parent_identity = _identity(canonical_parent_metadata)
        descriptors.append(parent)
        for name in names:
            directory_before = _stable_snapshot(os.fstat(parent))
            try:
                listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                missing = (parent, name)
                yield None
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if _stable_snapshot(os.fstat(parent)) != directory_before:
                        raise ValidationError(
                            "imported resident context parent changed while absence was inspected"
                        ) from None
                    _validate_pinned_links(links)
                    canonical_parent_current = os.lstat(canonical_parent)
                    if (
                        not stat.S_ISDIR(canonical_parent_current.st_mode)
                        or _identity(canonical_parent_current) != canonical_parent_identity
                    ):
                        raise ValidationError(
                            "imported resident vault changed at its canonical parent path"
                        ) from None
                    return
                raise ValidationError(
                    "imported resident context appeared while it was inspected"
                ) from None
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISDIR(listed.st_mode):
                raise ValidationError(
                    "imported resident context ancestry must contain only real directories"
                )
            child = os.open(name, flags, dir_fd=parent)
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(listed):
                os.close(child)
                raise ValidationError(
                    "imported resident context ancestry changed while it was opened"
                )
            links.append((parent, name, _identity(opened)))
            descriptors.append(child)
            parent = child
        yield parent
        _validate_pinned_links(links)
        canonical = os.lstat(canonical_root)
        canonical_parent_current = os.lstat(canonical_parent)
        if (
            not stat.S_ISDIR(canonical_parent_current.st_mode)
            or _identity(canonical_parent_current) != canonical_parent_identity
            or not stat.S_ISDIR(canonical.st_mode)
            or _identity(canonical) != links[0][2]
        ):
            raise ValidationError("imported resident vault changed at its canonical path")
    except ValidationError:
        raise
    except OSError as exc:
        target = RESIDENT_SKILLS.as_posix() if missing is None else missing[1]
        raise ValidationError(f"could not pin imported resident context {target}: {exc}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_pinned_links(links: list[tuple[int, str, tuple[int, int]]]) -> None:
    for parent, name, identity in links:
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise ValidationError(
                "imported resident context ancestry changed while it was read"
            ) from exc
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != identity:
            raise ValidationError("imported resident context ancestry changed while it was read")


def _scan_pinned_directory(
    descriptor: int,
    *,
    relative: PurePosixPath,
    files: dict[str, tuple[bytes, bool]],
    directories: set[str],
    bounds: _ScanBounds,
) -> None:
    directory_before = _stable_snapshot(os.fstat(descriptor))
    entries_before = _directory_entries(descriptor)
    for name, metadata in entries_before.items():
        bounds.entries += 1
        if bounds.entries > MAX_SKILL_ENTRIES:
            raise ValidationError("imported resident skills exceed the path-count bound")
        _validate_portable_component(name)
        child_relative = relative / name
        relative_text = child_relative.as_posix()
        _validate_skill_relative_path(child_relative)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError(
                f"imported resident skill path cannot be a symbolic link: {relative_text}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(relative_text)
            flags = os.O_RDONLY | _POSIX_OS.O_DIRECTORY | _POSIX_OS.O_NOFOLLOW
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if _stable_snapshot(opened) != _stable_snapshot(metadata):
                    raise ValidationError(
                        f"imported resident skill directory changed while opened: {relative_text}"
                    )
                _scan_pinned_directory(
                    child,
                    relative=child_relative,
                    files=files,
                    directories=directories,
                    bounds=bounds,
                )
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(current) != _identity(opened) or not stat.S_ISDIR(current.st_mode):
                    raise ValidationError(
                        f"imported resident skill directory changed while read: {relative_text}"
                    )
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(
                f"imported resident skill path must be a regular file: {relative_text}"
            )
        content = _read_pinned_file(
            descriptor,
            name,
            metadata,
            label="imported resident skill file",
            max_bytes=MAX_SKILL_FILE_BYTES,
            relative=relative_text,
        )
        _validate_imported_skill_content(content, relative_text)
        bounds.files += 1
        bounds.total_bytes += len(content)
        if bounds.files > MAX_SKILL_FILES:
            raise ValidationError("imported resident skills exceed the file-count bound")
        if bounds.total_bytes > MAX_SKILL_TOTAL_BYTES:
            raise ValidationError("imported resident skills exceed the total-size bound")
        files[relative_text] = (
            content,
            os.name != "nt" and bool(metadata.st_mode & stat.S_IXUSR),
        )
    entries_after = _directory_entries(descriptor)
    if {name: _stable_snapshot(metadata) for name, metadata in entries_after.items()} != {
        name: _stable_snapshot(metadata) for name, metadata in entries_before.items()
    }:
        raise ValidationError("imported resident skill directory changed while it was read")
    if _stable_snapshot(os.fstat(descriptor)) != directory_before:
        raise ValidationError("imported resident skill directory changed while it was read")


def _directory_entries(descriptor: int) -> dict[str, os.stat_result]:
    try:
        entries = []
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) > MAX_SKILL_ENTRIES:
                    raise ValidationError(
                        "one imported resident skill directory exceeds the entry-count bound"
                    )
        entries.sort(key=lambda item: item.name)
        return {entry.name: entry.stat(follow_symlinks=False) for entry in entries}
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError(f"could not inspect imported resident skills: {exc}") from exc


def _read_pinned_file(
    parent: int,
    name: str,
    listed: os.stat_result,
    *,
    label: str,
    max_bytes: int,
    relative: str,
) -> bytes:
    if listed.st_size > max_bytes:
        raise ValidationError(f"{label} exceeds its size bound: {relative}")
    flags = os.O_RDONLY | _POSIX_OS.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        opened_snapshot = _stable_snapshot(opened)
        if not stat.S_ISREG(opened.st_mode) or opened_snapshot != _stable_snapshot(listed):
            raise ValidationError(
                f"imported resident skill file changed while it was opened: {relative}"
            )
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
            raise ValidationError(f"{label} exceeds its size bound: {relative}")
        finished = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            _stable_snapshot(finished) != opened_snapshot
            or _stable_snapshot(current) != opened_snapshot
        ):
            raise ValidationError(
                f"imported resident skill file changed while it was read: {relative}"
            )
        return content
    finally:
        os.close(descriptor)


def _snapshot_skill_tree_fallback(
    vault_root: Path,
) -> tuple[dict[str, tuple[bytes, bool]], set[str]] | None:
    root = vault_root.joinpath(*RESIDENT_SKILLS.parts)
    if not os.path.lexists(root):
        return None
    _validate_fallback_ancestry(vault_root, RESIDENT_SKILLS.parts)
    before = _fallback_manifest(root)
    files: dict[str, tuple[bytes, bool]] = {}
    directories = {relative for relative, value in before.items() if value[0] == "directory"}
    bounds = _ScanBounds()
    for relative, (kind, snapshot) in sorted(before.items()):
        if kind != "file":
            continue
        content = read_regular_file(
            root.joinpath(*PurePosixPath(relative).parts),
            label=f"imported resident skill file {relative}",
            max_bytes=MAX_SKILL_FILE_BYTES,
        )
        _validate_imported_skill_content(content, relative)
        bounds.files += 1
        bounds.total_bytes += len(content)
        if bounds.files > MAX_SKILL_FILES or bounds.total_bytes > MAX_SKILL_TOTAL_BYTES:
            raise ValidationError("imported resident skills exceed their bounded snapshot")
        files[relative] = (
            content,
            os.name != "nt" and bool(snapshot[-1] & stat.S_IXUSR),
        )
    _validate_fallback_ancestry(vault_root, RESIDENT_SKILLS.parts)
    if _fallback_manifest(root) != before:
        raise ValidationError("imported resident skills changed while they were read")
    return files, directories


def _fallback_manifest(root: Path) -> dict[str, tuple[str, tuple[int, int, int, int, int]]]:
    result: dict[str, tuple[str, tuple[int, int, int, int, int]]] = {}
    entries_seen = 0

    def visit(directory: Path) -> None:
        nonlocal entries_seen
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ValidationError(f"could not inspect imported resident skills: {exc}") from exc
        for entry in entries:
            entries_seen += 1
            if entries_seen > MAX_SKILL_ENTRIES:
                raise ValidationError("imported resident skills exceed the path-count bound")
            _validate_portable_component(entry.name)
            path = Path(entry.path)
            relative = path.relative_to(root)
            relative_posix = PurePosixPath(*relative.parts)
            _validate_skill_relative_path(relative_posix)
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValidationError(
                    f"imported resident skill path cannot be a symbolic link: {relative_posix}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                result[relative_posix.as_posix()] = ("directory", _stable_snapshot(metadata))
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                result[relative_posix.as_posix()] = ("file", _stable_snapshot(metadata))
            else:
                raise ValidationError(
                    f"imported resident skill path must be a regular file: {relative_posix}"
                )

    visit(root)
    return result


def _validate_fallback_ancestry(vault_root: Path, parts: tuple[str, ...]) -> None:
    current = vault_root
    for component in parts:
        current = current / component
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ValidationError(f"could not inspect imported resident context: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError(
                "imported resident context ancestry must contain only real directories"
            )


def _read_resident_file(
    vault_root: Path,
    relative: PurePosixPath,
    *,
    label: str,
    max_bytes: int,
    missing_ok: bool,
) -> bytes | None:
    if PINNED_PATH_ROOT_SUPPORTED:
        parent_parts = relative.parts[:-1]
        with _open_pinned_directory(vault_root, parent_parts) as parent:
            if parent is None:
                if missing_ok:
                    return None
                raise ValidationError(f"{label} is missing")
            name = relative.parts[-1]
            parent_before = _stable_snapshot(os.fstat(parent))
            try:
                listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    try:
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        if _stable_snapshot(os.fstat(parent)) != parent_before:
                            raise ValidationError(
                                f"{label} parent changed while absence was inspected"
                            ) from None
                        return None
                    raise ValidationError(f"{label} appeared while it was inspected") from None
                raise ValidationError(f"{label} is missing") from None
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
                raise ValidationError(f"{label} must be a regular file, not a link")
            if listed.st_size > max_bytes:
                raise ValidationError(f"{label} exceeds its size bound")
            return _read_pinned_file(
                parent,
                name,
                listed,
                label=label,
                max_bytes=max_bytes,
                relative=relative.as_posix(),
            )
    path = vault_root.joinpath(*relative.parts)
    if not os.path.lexists(path):
        if missing_ok:
            return None
        raise ValidationError(f"{label} is missing")
    _validate_fallback_ancestry(vault_root, relative.parts[:-1])
    return read_regular_file(path, label=label, max_bytes=max_bytes)


def _validate_imported_skill_content(content: bytes, relative: str) -> None:
    validate_imported_resident_content(
        content,
        label=f"imported resident skill file {relative}",
    )


def validate_imported_resident_content(content: bytes, *, label: str) -> str:
    """Apply the exact activation-time text and privacy policy to resident bytes."""

    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError(f"{label} must be UTF-8 text") from exc
    screening = screen_local_content(content)
    if screening.decision is not AwarenessDecision.CONTENT_ALLOWED:
        blocking_reasons = tuple(
            reason
            for reason in screening.reasons
            if reason != "high-entropy-token" or not _entropy_is_documentation_only(content)
        )
        if not blocking_reasons:
            return text
        reasons = ", ".join(blocking_reasons)
        raise ValidationError(f"{label} failed privacy screening: {reasons}")
    return text


def _entropy_is_documentation_only(content: bytes) -> bool:
    """Distinguish human paths/slugs/config examples from opaque secret shapes."""

    found = False
    for match in _HIGH_ENTROPY_SHAPE.finditer(content):
        token = match.group(0).rstrip(b"=")
        if len(token) < 32 or len(set(token)) < 12 or _entropy(token) < 4.2:
            continue
        found = True
        line_start = content.rfind(b"\n", 0, match.start()) + 1
        prefix = content[line_start : match.start()]
        if _NUMERIC_CONFIG_SHAPE.fullmatch(match.group(0)) is not None:
            continue
        if _looks_like_human_path(token):
            continue
        if _looks_like_url_path(prefix, token):
            continue
        if _looks_like_human_slug(token):
            continue
        return False
    return found


def _looks_like_human_path(token: bytes) -> bool:
    if (
        not token.startswith(b"/")
        or token.count(b"/") < 2
        or any(marker in token for marker in (b"?", b"&", b"="))
    ):
        return False
    components = [component for component in token.split(b"/") if component]
    return bool(components) and all(
        len(component) <= 64
        and re.fullmatch(rb"[A-Za-z0-9._ -]+", component) is not None
        and not _opaque_component(component)
        for component in components
    )


def _looks_like_url_path(prefix: bytes, token: bytes) -> bool:
    scheme = prefix.rfind(b"://")
    has_scheme = scheme >= 0 and not any(
        character in prefix[scheme + 3 :] for character in b" \t`<>()[]"
    )
    if not has_scheme and _DOMAIN_PATH_PREFIX.search(prefix) is None:
        return False
    if any(marker in token for marker in (b"?", b"&", b"=")):
        return False
    components = [component for component in token.split(b"/") if component]
    return bool(components) and all(
        len(component) <= 128
        and re.fullmatch(rb"[A-Za-z0-9_-]+", component) is not None
        and (
            not _opaque_component(component)
            or _looks_like_human_slug(component)
            or _looks_like_lowercase_url_slug(component)
        )
        for component in components
    )


def _looks_like_human_slug(token: bytes) -> bool:
    if b"/" in token or b"=" in token:
        return False
    components = [part for part in re.split(rb"[_-]+", token) if part]
    separators = token.count(b"_") + token.count(b"-")
    return (
        separators >= 3
        and len(components) >= 4
        and all(len(part) <= 24 and part.isalnum() for part in components)
        and sum(
            any(65 <= value <= 90 or 97 <= value <= 122 for value in part) for part in components
        )
        >= 3
    )


def _looks_like_lowercase_url_slug(component: bytes) -> bool:
    return (
        component.lower() == component
        and component.count(b"-") >= 1
        and any(value in b"aeiou" for value in component)
    )


def _opaque_component(component: bytes) -> bool:
    return len(component) >= 32 and len(set(component)) >= 12 and _entropy(component) >= 4.2


def _entropy(value: bytes) -> float:
    counts = {character: value.count(character) for character in set(value)}
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def _skill_frontmatter_name(content: str, *, label: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise ValidationError(f"{label} SKILL.md is missing YAML frontmatter")
    try:
        end = lines.index("---", 1, min(len(lines), 129))
    except ValueError as exc:
        raise ValidationError(f"{label} SKILL.md has unterminated YAML frontmatter") from exc
    names = [
        match.group("value") for line in lines[1:end] if (match := _FRONTMATTER_NAME.match(line))
    ]
    if len(names) != 1:
        raise ValidationError(f"{label} SKILL.md must declare exactly one frontmatter name")
    value = names[0]
    if value[:1] == value[-1:] and value.startswith(('"', "'")):
        if value.startswith('"'):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"{label} SKILL.md has an invalid quoted name") from exc
            if not isinstance(decoded, str):
                raise ValidationError(f"{label} SKILL.md name must be text")
            value = decoded
        else:
            value = value[1:-1].replace("''", "'")
    _validate_skill_name(value)
    return value


def _validate_skill_name(name: str) -> None:
    if _SKILL_NAME.fullmatch(name) is None:
        raise ValidationError(
            f"imported resident skill name is not a portable exact $skill name: {name!r}"
        )
    _validate_portable_component(name)


def _validate_skill_relative_path(path: PurePosixPath) -> None:
    if len(path.parts) > MAX_SKILL_DEPTH:
        raise ValidationError("imported resident skill path exceeds the depth bound")
    if len(path.as_posix()) > MAX_SKILL_PATH_CHARACTERS:
        raise ValidationError("imported resident skill path exceeds the character bound")


def _validate_portable_component(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or name[-1] in {" ", "."}
        or any(ord(character) < 32 for character in name)
        or any(character in '<>:"/\\|?*' for character in name)
    ):
        raise ValidationError(f"imported resident skill path is not portable: {name!r}")
    stem = name.split(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValidationError(f"imported resident skill path is reserved on Windows: {name!r}")
    lowered = name.casefold()
    if (
        lowered in _PROTECTED_COMPONENTS
        or lowered in _PROTECTED_FILENAMES
        or lowered.endswith(_PROTECTED_SUFFIXES)
        or _SECRET_PATH_NAME.search(lowered) is not None
    ):
        raise ValidationError(
            f"imported resident skill path is excluded by privacy policy: {name!r}"
        )


def _reject_portable_collisions(values: list[str], *, label: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        key = _portable_collision_key(value)
        if prior := seen.get(key):
            raise ConflictError(f"{label} collision: {value!r} conflicts with {prior!r}")
        seen[key] = value


def _portable_collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _stable_snapshot(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_mode),
    )
