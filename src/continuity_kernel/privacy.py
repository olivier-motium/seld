"""Deterministic local-awareness exclusions and pre-model content screening."""

from __future__ import annotations

import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Final

from continuity_kernel.errors import ValidationError

MAX_SCREEN_BYTES: Final = 256 * 1024
MAX_LOCAL_CONTENT_BYTES: Final = 1024 * 1024
_PINNED_LOCAL_READ_SUPPORTED: Final = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)

_EXCLUDED_COMPONENTS: Final = frozenset(
    {
        ".1password",
        ".aws",
        ".azure",
        ".docker",
        ".gnupg",
        ".kube",
        ".ssh",
        "1password",
        "auth.db",
        "bitwarden",
        "cookies",
        "keychains",
        "keyrings",
        "login data",
        "passwords",
        "wallets",
    }
)
_EXCLUDED_SUFFIXES: Final = (
    ".env",
    ".kdbx",
    ".key",
    ".p12",
    ".pfx",
    ".pem",
    ".wallet",
)
_EXCLUDED_NAMES: Final = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_SECRET_NAME: Final = re.compile(r"(?:^|\.)(?:credential|credentials|secret|secrets)(?:\.|$)")
_CODEX_CREDENTIAL_CONTAINERS: Final = frozenset({".codex", ".openai", "openai"})
_CODEX_CREDENTIAL_ARTIFACT: Final = re.compile(r"^(?:auth(?:\.jsonl?)?|.+[-_.]auth(?:\.jsonl?)?)$")
_POSIX_SYSTEM_ROOTS: Final = frozenset(
    {
        "applications",
        "bin",
        "dev",
        "etc",
        "library",
        "proc",
        "sbin",
        "sys",
        "system",
        "usr",
    }
)
_SECRET_PATTERNS: Final = (
    ("private-key", re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("github-token", re.compile(rb"\b(?:gh[oprsu]_[A-Za-z0-9_]{20,})\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "credential-assignment",
        re.compile(
            rb"(?i)(?<![A-Za-z0-9_-])(?:[\"']?)(?:api[_-]?key|client[_-]?secret|access[_-]?token|"
            rb"refresh[_-]?token|id[_-]?token|password|secret|token)(?:[\"']?)\s*[:=]\s*"
            rb"(?:[\"']?)[^\s\"',;}]{12,}(?:[\"']?)"
        ),
    ),
)
_HIGH_ENTROPY_TOKEN: Final = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9])")
_MACOS_FILE_PROVIDER_ROOTS: Final = (
    Path.home() / "Library/CloudStorage",
    Path.home() / "Library/Mobile Documents",
)


class AwarenessDecision(StrEnum):
    EXCLUDE = "exclude"
    METADATA_ONLY = "metadata_only"
    CONTENT_ALLOWED = "content_allowed"
    PLACEHOLDER = "placeholder"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class PathAssessment:
    decision: AwarenessDecision
    reason: str
    relative_path: str | None


@dataclass(frozen=True)
class ContentAssessment:
    decision: AwarenessDecision
    reasons: tuple[str, ...]
    bytes_screened: int


@dataclass(frozen=True)
class ScreenedLocalContent:
    """A bounded local read that never returns bytes rejected by the screen."""

    path: PathAssessment
    screening: ContentAssessment | None
    content: bytes | None


class _CloudPlaceholderDetected(Exception):
    """Internal signal used to discard bytes when offline state changes before reading."""


def assess_local_path(
    path: Path | str,
    *,
    selected_root: Path | str,
    content_requested: bool = False,
) -> PathAssessment:
    """Assess one already-selected path without opening its content."""

    root_input = Path(selected_root).expanduser()
    if not root_input.is_absolute():
        root_input = Path.cwd() / root_input
    if _is_protected_absolute_path(root_input):
        return PathAssessment(AwarenessDecision.EXCLUDE, "protected-selected-root", None)
    try:
        root_metadata = os.lstat(root_input)
    except OSError:
        return PathAssessment(AwarenessDecision.EXCLUDE, "unavailable-selected-root", None)
    if stat.S_ISLNK(root_metadata.st_mode):
        return PathAssessment(AwarenessDecision.EXCLUDE, "selected-root-symbolic-link", None)
    if not stat.S_ISDIR(root_metadata.st_mode):
        return PathAssessment(AwarenessDecision.EXCLUDE, "selected-root-not-directory", None)
    try:
        root = root_input.resolve(strict=True)
    except (OSError, RuntimeError):
        return PathAssessment(AwarenessDecision.EXCLUDE, "unavailable-selected-root", None)
    if _is_protected_absolute_path(root):
        return PathAssessment(AwarenessDecision.EXCLUDE, "protected-selected-root", None)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved_parent = candidate.parent.resolve()
        normalized = resolved_parent / candidate.name
        relative = normalized.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return PathAssessment(AwarenessDecision.EXCLUDE, "outside-selected-root", None)
    relative_text = relative.as_posix()
    if _is_protected_absolute_path(normalized):
        return PathAssessment(AwarenessDecision.EXCLUDE, "protected-path", relative_text)
    if relative_text in {"", "."}:
        return PathAssessment(AwarenessDecision.METADATA_ONLY, "selected-root", relative_text)
    parts = tuple(part.casefold() for part in relative.parts)
    joined = "/".join(parts)
    if _is_structurally_excluded(parts, joined):
        return PathAssessment(AwarenessDecision.EXCLUDE, "protected-path", relative_text)
    try:
        metadata = os.lstat(normalized)
    except OSError:
        return PathAssessment(AwarenessDecision.EXCLUDE, "unavailable-path", relative_text)
    if stat.S_ISLNK(metadata.st_mode):
        return PathAssessment(AwarenessDecision.EXCLUDE, "symbolic-link", relative_text)
    if _is_cloud_placeholder(normalized, metadata):
        return PathAssessment(AwarenessDecision.PLACEHOLDER, "cloud-placeholder", relative_text)
    if content_requested and _is_unproven_cloud_storage_path(normalized):
        return PathAssessment(
            AwarenessDecision.METADATA_ONLY,
            "cloud-residency-unverified",
            relative_text,
        )
    if stat.S_ISDIR(metadata.st_mode):
        return PathAssessment(AwarenessDecision.METADATA_ONLY, "directory", relative_text)
    if not stat.S_ISREG(metadata.st_mode):
        return PathAssessment(AwarenessDecision.EXCLUDE, "unsupported-file-type", relative_text)
    if metadata.st_size > MAX_LOCAL_CONTENT_BYTES:
        return PathAssessment(AwarenessDecision.METADATA_ONLY, "content-size-bound", relative_text)
    return PathAssessment(
        AwarenessDecision.CONTENT_ALLOWED if content_requested else AwarenessDecision.METADATA_ONLY,
        "selected-regular-file",
        relative_text,
    )


def read_screened_local_content(
    path: Path | str,
    *,
    selected_root: Path | str,
) -> ScreenedLocalContent:
    """Read and screen one approved file through the stable no-follow boundary.

    Offline placeholders are rejected before the stable open and checked again
    on the opened descriptor before any bytes are read. Quarantined bytes never
    appear in the returned value.
    """

    root_input = Path(selected_root).expanduser()
    if not root_input.is_absolute():
        root_input = Path.cwd() / root_input
    try:
        approved_root = os.lstat(root_input)
    except OSError:
        approved_root = None

    assessment = assess_local_path(path, selected_root=root_input, content_requested=True)
    if assessment.decision is not AwarenessDecision.CONTENT_ALLOWED:
        return ScreenedLocalContent(path=assessment, screening=None, content=None)
    if assessment.relative_path is None:
        raise ValidationError("selected local context has no relative path")

    if not _PINNED_LOCAL_READ_SUPPORTED:
        unavailable = PathAssessment(
            AwarenessDecision.EXCLUDE,
            "secure-pinned-read-unsupported",
            assessment.relative_path,
        )
        return ScreenedLocalContent(path=unavailable, screening=None, content=None)

    if approved_root is None or not stat.S_ISDIR(approved_root.st_mode):
        raise ValidationError(f"selected local context root changed before access: {root_input}")
    try:
        root = root_input.resolve(strict=True)
        resolved_root = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise ValidationError(
            f"could not inspect selected local context root: {root_input}: {exc}"
        ) from exc
    if not stat.S_ISDIR(resolved_root.st_mode) or not _same_posix_object(
        approved_root,
        resolved_root,
    ):
        raise ValidationError(f"selected local context root changed before access: {root_input}")
    relative = Path(assessment.relative_path)
    normalized = root.joinpath(*relative.parts)

    # Re-evaluate at the point of access. The stable opener below independently
    # rejects a leaf-link or metadata swap between this check and os.open.
    current = assess_local_path(normalized, selected_root=root, content_requested=True)
    if current.decision is not AwarenessDecision.CONTENT_ALLOWED:
        return ScreenedLocalContent(path=current, screening=None, content=None)
    try:
        content = _read_bounded_non_placeholder(
            root,
            relative,
            approved_root=approved_root,
        )
    except _CloudPlaceholderDetected:
        placeholder = PathAssessment(
            AwarenessDecision.PLACEHOLDER,
            "cloud-placeholder",
            current.relative_path,
        )
        return ScreenedLocalContent(path=placeholder, screening=None, content=None)
    screening = screen_local_content(content)
    return ScreenedLocalContent(
        path=current,
        screening=screening,
        content=content if screening.decision is AwarenessDecision.CONTENT_ALLOWED else None,
    )


def screen_local_content(content: bytes) -> ContentAssessment:
    """Quarantine suspicious bytes before they can enter a model context."""

    if len(content) > MAX_SCREEN_BYTES:
        return ContentAssessment(
            AwarenessDecision.QUARANTINE,
            ("screen-size-bound",),
            min(len(content), MAX_SCREEN_BYTES),
        )
    reasons = [name for name, pattern in _SECRET_PATTERNS if pattern.search(content)]
    if _contains_high_entropy_secret(content):
        reasons.append("high-entropy-token")
    if b"\x00" in content:
        reasons.append("binary-content")
    return ContentAssessment(
        AwarenessDecision.QUARANTINE if reasons else AwarenessDecision.CONTENT_ALLOWED,
        tuple(sorted(set(reasons))),
        len(content),
    )


def _is_structurally_excluded(parts: tuple[str, ...], joined: str) -> bool:
    name = parts[-1]
    if _is_secret_or_credential_name(name):
        return True
    if any(part in _CODEX_CREDENTIAL_CONTAINERS for part in parts[:-1]) and (
        _CODEX_CREDENTIAL_ARTIFACT.fullmatch(name) is not None
    ):
        return True
    if any(left == ".config" and right == "gcloud" for left, right in pairwise(parts)):
        return True
    if any(left == ".config" and right == "gh" for left, right in pairwise(parts)):
        return True
    if any(part in _EXCLUDED_COMPONENTS for part in parts):
        return True
    return any(component in joined for component in ("browser/", "chrome/", "firefox/")) and (
        "cookie" in name or "login" in name or "credential" in name
    )


def _is_secret_or_credential_name(name: str) -> bool:
    if name in _EXCLUDED_NAMES or name.endswith(_EXCLUDED_SUFFIXES):
        return True
    if name == ".envrc" or name.startswith(".env.") or name.startswith(".env-"):
        return True
    if any(name.startswith(f"{config}.") for config in (".netrc", ".npmrc", ".pypirc")):
        return True
    return _SECRET_NAME.search(name) is not None


def _is_protected_absolute_path(path: Path) -> bool:
    """Reject system trees, other users' homes, and protected components."""

    lexical = Path(os.path.abspath(path))
    parts = tuple(
        part.casefold()
        for part in lexical.parts
        if part not in {lexical.anchor, lexical.drive, os.sep}
    )
    if not parts:
        return True
    joined = "/".join(parts)
    if _is_structurally_excluded(parts, joined):
        return True

    if os.name == "nt":
        protected_roots = tuple(
            Path(value).expanduser()
            for variable in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
            if (value := os.environ.get(variable))
        )
        if any(_is_relative_to(lexical, root) for root in protected_roots):
            return True
    elif (
        parts[0] in _POSIX_SYSTEM_ROOTS
        or (parts[0] == "private" and len(parts) > 1 and parts[1] in {"etc", "root"})
        or (
            parts[0] == "private"
            and len(parts) > 2
            and parts[1:3] in {("var", "db"), ("var", "root"), ("var", "run")}
        )
        or (parts[0] == "var" and len(parts) > 1 and parts[1] in {"db", "root", "run"})
    ):
        return True

    home = Path.home().expanduser().resolve()
    home_parent_name = home.parent.name.casefold()
    if parts[0] in {"home", "users"} or (
        home_parent_name in {"home", "users"} and _is_relative_to(lexical, home.parent)
    ):
        return not _is_relative_to(lexical, home)
    if parts[0] == "root":
        return not _is_relative_to(lexical, home)
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
    except ValueError:
        return False
    return True


def _read_bounded_non_placeholder(
    root: Path,
    relative: Path,
    *,
    approved_root: os.stat_result,
) -> bytes:
    """Read beneath one approved root without resolving descendant pathnames."""

    parts = relative.parts
    if relative.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("selected local context must be a safe relative path")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_descriptor = -1
    directory_descriptors: list[int] = []
    directory_links: list[tuple[int, str, tuple[int, int]]] = []
    file_descriptor = -1
    try:
        try:
            root_descriptor = os.open(root, directory_flags)
        except OSError as exc:
            raise ValidationError(
                f"could not pin selected local context root: {root}: {exc}"
            ) from exc
        opened_root = os.fstat(root_descriptor)
        if not stat.S_ISDIR(opened_root.st_mode) or not _same_posix_object(
            approved_root,
            opened_root,
        ):
            raise ValidationError(
                f"selected local context root changed while it was opened: {root}"
            )

        parent = root_descriptor
        for component in parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=parent)
            except OSError as exc:
                raise ValidationError(
                    f"could not pin selected local context ancestry beneath {root}: {exc}"
                ) from exc
            child_metadata = os.fstat(child)
            if not stat.S_ISDIR(child_metadata.st_mode):
                os.close(child)
                raise ValidationError(
                    f"selected local context ancestry is not a directory beneath {root}"
                )
            directory_links.append((parent, component, _posix_identity(child_metadata)))
            directory_descriptors.append(child)
            parent = child

        name = parts[-1]
        display_path = root.joinpath(*parts)
        try:
            listed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise ValidationError(
                f"could not inspect selected local context beneath {root}: {exc}"
            ) from exc
        if _is_cloud_placeholder(display_path, listed):
            raise _CloudPlaceholderDetected
        if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
            raise ValidationError(
                f"selected local context must be a regular file, not a link: {display_path}"
            )
        if listed.st_size > MAX_LOCAL_CONTENT_BYTES:
            raise ValidationError(f"selected local context exceeds its size bound: {display_path}")

        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            file_descriptor = os.open(name, file_flags, dir_fd=parent)
        except OSError as exc:
            raise ValidationError(
                f"could not open selected local context beneath {root}: {exc}"
            ) from exc
        opened = os.fstat(file_descriptor)
        opened_snapshot = _stable_file_snapshot(opened)
        if not stat.S_ISREG(opened.st_mode) or not _same_posix_object(listed, opened):
            raise ValidationError(
                f"selected local context changed while it was opened: {display_path}"
            )
        if _is_cloud_placeholder(display_path, opened):
            raise _CloudPlaceholderDetected

        chunks: list[bytes] = []
        remaining = MAX_LOCAL_CONTENT_BYTES + 1
        while remaining:
            block = os.read(file_descriptor, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        content = b"".join(chunks)
        if len(content) > MAX_LOCAL_CONTENT_BYTES:
            raise ValidationError(f"selected local context exceeds its size bound: {display_path}")

        finished = os.fstat(file_descriptor)
        if _is_cloud_placeholder(display_path, finished):
            raise _CloudPlaceholderDetected
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            raise ValidationError(
                f"selected local context changed while it was read: {display_path}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or _stable_file_snapshot(finished) != opened_snapshot
            or not _same_posix_object(current, opened)
        ):
            raise ValidationError(
                f"selected local context changed while it was read: {display_path}"
            )

        for link_parent, component, identity in directory_links:
            try:
                linked = os.stat(component, dir_fd=link_parent, follow_symlinks=False)
            except OSError as exc:
                raise ValidationError(
                    "selected local context ancestry changed while it was read: "
                    f"{display_path}: {exc}"
                ) from exc
            if not stat.S_ISDIR(linked.st_mode) or _posix_identity(linked) != identity:
                raise ValidationError(
                    f"selected local context ancestry changed while it was read: {display_path}"
                )
        try:
            current_root = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise ValidationError(
                f"selected local context root changed while it was read: {root}: {exc}"
            ) from exc
        if not stat.S_ISDIR(current_root.st_mode) or not _same_posix_object(
            opened_root,
            current_root,
        ):
            raise ValidationError(f"selected local context root changed while it was read: {root}")
        return content
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _posix_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _same_posix_object(left: os.stat_result, right: os.stat_result) -> bool:
    return _posix_identity(left) == _posix_identity(right)


def _stable_file_snapshot(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _is_cloud_placeholder(path: Path, metadata: os.stat_result) -> bool:
    if path.name.endswith(".icloud"):
        return True
    file_attributes = int(getattr(metadata, "st_file_attributes", 0))
    file_attribute_offline = 0x1000
    file_attribute_recall_on_data_access = 0x400000
    return bool(file_attributes & (file_attribute_offline | file_attribute_recall_on_data_access))


def _is_unproven_cloud_storage_path(path: Path) -> bool:
    """Keep macOS File Provider content closed until residency is proven safely."""

    if sys.platform != "darwin":
        return False
    return any(_is_relative_to(path, root) for root in _MACOS_FILE_PROVIDER_ROOTS)


def _contains_high_entropy_secret(content: bytes) -> bool:
    for match in _HIGH_ENTROPY_TOKEN.finditer(content):
        token = match.group(0).rstrip(b"=")
        if len(token) < 32 or len(set(token)) < 12:
            continue
        counts = {value: token.count(value) for value in set(token)}
        entropy = -sum(
            (count / len(token)) * math.log2(count / len(token)) for count in counts.values()
        )
        if entropy >= 4.2:
            return True
    return False
