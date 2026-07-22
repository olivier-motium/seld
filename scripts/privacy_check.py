#!/usr/bin/env python3
"""Fail closed on likely secrets, private absolute paths, and configured private terms."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_SCAN_BYTES = 64 * 1024 * 1024
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
    for artifact in args.artifact:
        target = artifact.expanduser().resolve()
        if target.is_dir():
            artifact_findings, artifact_scanned = scan_tree(target, patterns)
        else:
            artifact_findings, artifact_scanned = scan_path(target, patterns, root=target.parent)
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
    raw = os.environ.get("CONTINUITY_PRIVATE_TERMS", "")
    terms = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(re.compile(re.escape(term.encode("utf-8")), re.IGNORECASE) for term in terms)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="continuity-privacy-self-test-") as raw:
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
