#!/usr/bin/env python3
"""Fail closed on likely secrets, private absolute paths, and configured private terms."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_REPOSITORY_FILE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
PYINSTALLER_COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
ALLOWED_REPOSITORY_BINARY_PATHS = frozenset(
    {
        "src/continuity_kernel/resources/bridge/fonts/nunito-sans-var.woff2",
        "src/continuity_kernel/resources/bridge/fonts/nunito-var.woff2",
    }
)
BINARY_MAGICS = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
    b"PK\x03\x04",
    b"\x1f\x8b",
    b"wOFF",
    b"wOF2",
    b"\x7fELF",
    b"MZ",
)
BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".avif",
        ".bin",
        ".bz2",
        ".dmg",
        ".exe",
        ".gif",
        ".gz",
        ".heic",
        ".icns",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp4",
        ".pdf",
        ".png",
        ".pyc",
        ".tar",
        ".tgz",
        ".webp",
        ".woff",
        ".woff2",
        ".xz",
        ".zip",
    }
)
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}
EXCLUDED_FILENAMES = {".coverage"}
PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    re.compile(rb"[A-Za-z]:\\Users\\[A-Za-z0-9._ -]+\\"),
    re.compile(rb"[A-Za-z]:\\\\Users\\\\[A-Za-z0-9._ -]+\\\\"),
)


@dataclass(frozen=True)
class Finding:
    path: str
    scope: str


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        print(json.dumps({"ok": True, "self_test": True}, sort_keys=True))
        return 0

    root = args.root.expanduser().resolve()
    patterns = (*PATTERNS, *_private_term_patterns())
    findings, scanned = scan_tree(root, patterns)
    findings.extend(scan_repository_artifact_policy(root))
    for artifact in args.artifact:
        target = artifact.expanduser().resolve()
        if target.is_dir():
            artifact_findings, artifact_scanned = scan_artifact_tree(target, patterns)
        else:
            artifact_findings, artifact_scanned = scan_artifact_path(
                target,
                patterns,
                root=target.parent,
            )
        findings.extend(artifact_findings)
        scanned += artifact_scanned
    if not args.no_history:
        findings.extend(scan_history(root, patterns))

    unique = sorted({(finding.scope, finding.path) for finding in findings})
    if unique:
        for scope, path in unique:
            print(f"privacy finding [{scope}]: {path}")
        print(
            json.dumps({"findings": len(unique), "ok": False, "scanned": scanned}, sort_keys=True)
        )
        return 1
    print(json.dumps({"findings": 0, "ok": True, "scanned": scanned}, sort_keys=True))
    return 0


def scan_tree(root: Path, patterns: tuple[re.Pattern[bytes], ...]) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if path.name in EXCLUDED_FILENAMES:
            continue
        if any(part in EXCLUDED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        path_findings, count = scan_path(path, patterns, root=root)
        findings.extend(path_findings)
        scanned += count
    return findings, scanned


def scan_path(
    path: Path, patterns: tuple[re.Pattern[bytes], ...], *, root: Path
) -> tuple[list[Finding], int]:
    if path.is_symlink():
        return [Finding(_relative(path, root), "symlink")], 0
    if not path.is_file():
        return [], 0
    size = path.stat().st_size
    if size > MAX_SCAN_BYTES:
        return [Finding(_relative(path, root), "oversized-unscanned")], 0
    content = path.read_bytes()
    findings = [
        Finding(_relative(path, root), "working-tree")
        for pattern in patterns
        if pattern.search(content)
    ]
    return findings, 1


def scan_repository_artifact_policy(root: Path) -> list[Finding]:
    """Reject repository noise while allowing only the two licensed Seld fonts."""

    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.name in EXCLUDED_FILENAMES:
            continue
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        display = relative.as_posix()
        size = path.stat().st_size
        if size > MAX_REPOSITORY_FILE_BYTES:
            findings.append(Finding(display, "repository-file-oversized"))
            continue
        content = path.read_bytes()
        if (
            path.suffix.lower() in BINARY_SUFFIXES or _looks_binary(content)
        ) and display not in ALLOWED_REPOSITORY_BINARY_PATHS:
            findings.append(Finding(display, "repository-binary-not-allowed"))
    return findings


def _looks_binary(content: bytes) -> bool:
    sample = content[:8_192]
    return b"\0" in sample or any(sample.startswith(magic) for magic in BINARY_MAGICS)


def scan_artifact_tree(
    root: Path,
    patterns: tuple[re.Pattern[bytes], ...],
) -> tuple[list[Finding], int]:
    """Scan every artifact byte stream, including supported compressed members."""

    findings: list[Finding] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        path_findings, count = scan_artifact_path(path, patterns, root=root)
        findings.extend(path_findings)
        scanned += count
    return findings, scanned


def scan_artifact_path(
    path: Path,
    patterns: tuple[re.Pattern[bytes], ...],
    *,
    root: Path,
) -> tuple[list[Finding], int]:
    """Scan an artifact and its bounded decompressed payload without extracting it."""

    findings, scanned = scan_path(path, patterns, root=root)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
        return findings, scanned
    display = _relative(path, root)
    try:
        if zipfile.is_zipfile(path):
            nested_findings, nested_scanned = _scan_zip_artifact(path, display, patterns)
        elif tarfile.is_tarfile(path):
            nested_findings, nested_scanned = _scan_tar_artifact(path, display, patterns)
        elif PYINSTALLER_COOKIE_MAGIC in path.read_bytes():
            nested_findings, nested_scanned = _scan_pyinstaller_artifact(
                path,
                display,
                patterns,
            )
        else:
            return findings, scanned
    except (
        OSError,
        EOFError,
        ImportError,
        KeyError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ):
        findings.append(Finding(display, "artifact-archive-unreadable"))
        return findings, scanned
    findings.extend(nested_findings)
    return findings, scanned + nested_scanned


def _scan_zip_artifact(
    path: Path,
    display: str,
    patterns: tuple[re.Pattern[bytes], ...],
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    scanned = 0
    total = 0
    with zipfile.ZipFile(path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            return [Finding(display, "artifact-too-many-members")], 0
        for member in members:
            if member.is_dir():
                continue
            member_display = _artifact_member(display, member.filename)
            if not _safe_artifact_member_name(member.filename):
                findings.append(Finding(member_display, "artifact-member-unsafe"))
                continue
            if member.flag_bits & 0x1:
                findings.append(Finding(member_display, "artifact-member-encrypted"))
                continue
            if member.file_size > MAX_SCAN_BYTES or total + member.file_size > MAX_SCAN_BYTES:
                findings.append(Finding(member_display, "artifact-member-oversized"))
                continue
            with archive.open(member, "r") as source:
                content = source.read(MAX_SCAN_BYTES + 1)
            if len(content) > MAX_SCAN_BYTES or total + len(content) > MAX_SCAN_BYTES:
                findings.append(Finding(member_display, "artifact-member-oversized"))
                continue
            total += len(content)
            scanned += 1
            findings.extend(_content_findings(content, member_display, "artifact-member", patterns))
    return findings, scanned


def _scan_tar_artifact(
    path: Path,
    display: str,
    patterns: tuple[re.Pattern[bytes], ...],
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    scanned = 0
    total = 0
    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            return [Finding(display, "artifact-too-many-members")], 0
        for member in members:
            if member.isdir():
                continue
            member_display = _artifact_member(display, member.name)
            if not _safe_artifact_member_name(member.name):
                findings.append(Finding(member_display, "artifact-member-unsafe"))
                continue
            if not member.isfile():
                findings.append(Finding(member_display, "artifact-member-unsupported"))
                continue
            if member.size > MAX_SCAN_BYTES or total + member.size > MAX_SCAN_BYTES:
                findings.append(Finding(member_display, "artifact-member-oversized"))
                continue
            source = archive.extractfile(member)
            if source is None:
                findings.append(Finding(member_display, "artifact-member-unreadable"))
                continue
            with source:
                content = source.read(MAX_SCAN_BYTES + 1)
            if len(content) > MAX_SCAN_BYTES or total + len(content) > MAX_SCAN_BYTES:
                findings.append(Finding(member_display, "artifact-member-oversized"))
                continue
            total += len(content)
            scanned += 1
            findings.extend(_content_findings(content, member_display, "artifact-member", patterns))
    return findings, scanned


def _scan_pyinstaller_artifact(
    path: Path,
    display: str,
    patterns: tuple[re.Pattern[bytes], ...],
) -> tuple[list[Finding], int]:
    """Inspect the CArchive and embedded PYZ when the release dependency is present."""

    try:
        readers = importlib.import_module("PyInstaller.archive.readers")
        reader = readers.CArchiveReader(str(path))
    except (ImportError, AttributeError):
        return [Finding(display, "pyinstaller-payload-unscanned")], 0

    findings: list[Finding] = []
    scanned = 0
    total = 0
    entries = 0
    for name, metadata in reader.toc.items():
        entries += 1
        member_display = _artifact_member(display, str(name))
        if entries > MAX_ARCHIVE_ENTRIES:
            findings.append(Finding(display, "artifact-too-many-members"))
            break
        if not _safe_artifact_member_name(str(name)):
            findings.append(Finding(member_display, "artifact-member-unsafe"))
            continue
        uncompressed_length = metadata[2]
        if (
            type(uncompressed_length) is not int
            or uncompressed_length < 0
            or uncompressed_length > MAX_SCAN_BYTES
            or total + uncompressed_length > MAX_SCAN_BYTES
        ):
            findings.append(Finding(member_display, "artifact-member-oversized"))
            continue
        content = reader.extract(name)
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_SCAN_BYTES
            or total + len(content) > MAX_SCAN_BYTES
        ):
            findings.append(Finding(member_display, "artifact-member-unreadable"))
            continue
        total += len(content)
        scanned += 1
        findings.extend(_content_findings(content, member_display, "artifact-member", patterns))
        if metadata[-1] == "z":
            pyz_findings, pyz_scanned, pyz_total, pyz_entries = _scan_pyz_artifact(
                reader.open_embedded_archive(name),
                member_display,
                patterns,
                remaining_bytes=MAX_SCAN_BYTES - total,
                remaining_entries=MAX_ARCHIVE_ENTRIES - entries,
            )
            findings.extend(pyz_findings)
            scanned += pyz_scanned
            total += pyz_total
            entries += pyz_entries
    return findings, scanned


def _scan_pyz_artifact(
    archive: Any,
    display: str,
    patterns: tuple[re.Pattern[bytes], ...],
    *,
    remaining_bytes: int,
    remaining_entries: int,
) -> tuple[list[Finding], int, int, int]:
    findings: list[Finding] = []
    scanned = 0
    total = 0
    entries = 0
    for name in archive.toc:
        entries += 1
        member_display = _artifact_member(display, str(name))
        if entries > remaining_entries:
            findings.append(Finding(display, "artifact-too-many-members"))
            break
        if not _safe_artifact_member_name(str(name).replace(".", "/")):
            findings.append(Finding(member_display, "artifact-member-unsafe"))
            continue
        content = archive.extract(name, raw=True)
        if content is None:
            continue
        if not isinstance(content, bytes) or len(content) > remaining_bytes - total:
            findings.append(Finding(member_display, "artifact-member-oversized"))
            continue
        total += len(content)
        scanned += 1
        findings.extend(_content_findings(content, member_display, "artifact-member", patterns))
    return findings, scanned, total, entries


def _content_findings(
    content: bytes,
    display: str,
    scope: str,
    patterns: tuple[re.Pattern[bytes], ...],
) -> list[Finding]:
    return [Finding(display, scope) for pattern in patterns if pattern.search(content)]


def _artifact_member(archive: str, member: str) -> str:
    return f"{archive}!{member}"


def _safe_artifact_member_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    parts = normalized.split("/")
    return (
        bool(name)
        and not normalized.startswith("/")
        and all(part not in {"", ".", ".."} for part in parts)
    )


def scan_history(root: Path, patterns: tuple[re.Pattern[bytes], ...]) -> list[Finding]:
    if not (root / ".git").exists():
        return []
    revisions = subprocess.run(
        ["git", "rev-list", "--all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    findings: list[Finding] = []
    scanned_blobs: set[str] = set()
    for revision in revisions:
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "-l", "--full-tree", revision],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        for line in listing.stdout.splitlines():
            metadata, path = line.split("\t", 1)
            _mode, kind, blob, raw_size = metadata.split()
            if kind != "blob":
                continue
            if raw_size == "-" or int(raw_size) > MAX_SCAN_BYTES:
                findings.append(Finding(path, "git-history-oversized"))
                continue
            if blob in scanned_blobs:
                continue
            scanned_blobs.add(blob)
            try:
                content = subprocess.run(
                    ["git", "cat-file", "blob", blob],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    timeout=30,
                ).stdout
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                findings.append(Finding(path, "git-history-unreadable"))
                continue
            if any(pattern.search(content) for pattern in patterns):
                findings.append(Finding(path, "git-history"))
    return findings


def _private_term_patterns() -> tuple[re.Pattern[bytes], ...]:
    raw = os.environ.get("GSV_PRIVATE_TERMS", "")
    terms = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(re.compile(re.escape(term.encode("utf-8")), re.IGNORECASE) for term in terms)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="gsv-privacy-self-test-") as raw:
        root = Path(raw)
        clean = root / "clean.txt"
        bad = root / "bad.txt"
        clean.write_text("synthetic clean fixture", encoding="utf-8")
        bad.write_text("-----BEGIN " + "PRIVATE KEY-----", encoding="utf-8")
        findings, scanned = scan_tree(root, PATTERNS)
        if scanned != 2 or [finding.path for finding in findings] != ["bad.txt"]:
            raise RuntimeError("privacy scanner self-test did not detect exactly the canary")


if __name__ == "__main__":
    raise SystemExit(main())
