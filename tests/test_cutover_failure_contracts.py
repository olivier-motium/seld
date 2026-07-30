from __future__ import annotations

import ctypes
import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel import bridge, bridge_worker, resident_context, sqlite_snapshot
from continuity_kernel.errors import ValidationError

_POSIX_OS = cast(Any, os)
_RESOURCE = cast(Any, importlib.import_module("resource")) if os.name != "nt" else None
_RLIM_INFINITY = -1 if _RESOURCE is None else int(_RESOURCE.RLIM_INFINITY)


def _write_skill(vault: Path, *, name: str = "exact-skill", frontmatter: str | None = None) -> Path:
    skill = vault / "context/resident/skills" / name
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(
        frontmatter or f"---\nname: {name}\ndescription: Exact imported skill.\n---\n\n# Exact\n",
        encoding="utf-8",
    )
    (skill / "references/evidence.md").write_text("# Evidence\n\nRead exactly.\n", encoding="utf-8")
    (skill / "scripts/check.py").write_text("print('exact')\n", encoding="utf-8")
    return skill


def _use_resident_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resident_context, "PINNED_PATH_ROOT_SUPPORTED", False)


def test_resident_fallback_reads_one_exact_tree_and_preserves_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_resident_fallback(monkeypatch)
    absent = tmp_path / "absent"

    status = resident_context.resident_context_status(absent)
    assert status["available"] is False
    assert status["guidance"] == {
        "path": "context/resident/AGENTS.md",
        "present": False,
    }
    with pytest.raises(ValidationError, match="guidance is missing"):
        resident_context.read_resident_guidance(absent)

    vault = tmp_path / "vault"
    resident = vault / "context/resident"
    resident.mkdir(parents=True)
    guidance = "# Resident guidance\n\nKeep this exact.\n"
    (resident / "AGENTS.md").write_text(guidance, encoding="utf-8")
    skill = _write_skill(vault)
    (skill / "references/nested").mkdir()
    (skill / "references/nested/detail.md").write_text("# Detail\n", encoding="utf-8")

    shown = resident_context.read_resident_guidance(vault)
    skills = resident_context.resident_skills(vault)

    assert shown["content"] == guidance
    assert [item.name for item in skills] == ["exact-skill"]
    assert skills[0].directories == ("references", "references/nested", "scripts")
    assert [item.relative_path for item in skills[0].files] == [
        "SKILL.md",
        "references/evidence.md",
        "references/nested/detail.md",
        "scripts/check.py",
    ]


def test_resident_fallback_rejects_links_special_files_bounds_and_tree_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_resident_fallback(monkeypatch)

    if os.name != "nt":
        linked_vault = tmp_path / "linked"
        linked_skill = _write_skill(linked_vault)
        target = linked_skill / "references/target.md"
        target.write_text("ordinary\n", encoding="utf-8")
        (linked_skill / "references/link.md").symlink_to(target)
        with pytest.raises(ValidationError, match="symbolic link"):
            resident_context.resident_skills(linked_vault)

        linked_ancestor_vault = tmp_path / "linked-ancestor"
        real_skills = linked_ancestor_vault / "real-skills"
        real_skills.mkdir(parents=True)
        resident = linked_ancestor_vault / "context/resident"
        resident.mkdir(parents=True)
        (resident / "skills").symlink_to(real_skills, target_is_directory=True)
        with pytest.raises(ValidationError, match="ancestry must contain only real directories"):
            resident_context.resident_skills(linked_ancestor_vault)

        special_vault = tmp_path / "special"
        special_skill = _write_skill(special_vault)
        _POSIX_OS.mkfifo(special_skill / "references/pipe")
        with pytest.raises(ValidationError, match="must be a regular file"):
            resident_context.resident_skills(special_vault)

    count_vault = tmp_path / "count"
    _write_skill(count_vault)
    monkeypatch.setattr(resident_context, "MAX_SKILL_ENTRIES", 0)
    with pytest.raises(ValidationError, match="path-count bound"):
        resident_context.resident_skills(count_vault)
    monkeypatch.setattr(resident_context, "MAX_SKILL_ENTRIES", 8_192)

    size_vault = tmp_path / "size"
    _write_skill(size_vault)
    monkeypatch.setattr(resident_context, "MAX_SKILL_TOTAL_BYTES", 1)
    with pytest.raises(ValidationError, match="bounded snapshot"):
        resident_context.resident_skills(size_vault)
    monkeypatch.setattr(resident_context, "MAX_SKILL_TOTAL_BYTES", 16 * 1024 * 1024)

    raced_vault = tmp_path / "raced"
    raced_skill = _write_skill(raced_vault)
    original_validate = resident_context._validate_imported_skill_content
    mutated = False

    def mutate_after_read(content: bytes, relative: str) -> None:
        nonlocal mutated
        original_validate(content, relative)
        if not mutated:
            mutated = True
            (raced_skill / "appeared.md").write_text("# Appeared\n", encoding="utf-8")

    monkeypatch.setattr(resident_context, "_validate_imported_skill_content", mutate_after_read)
    with pytest.raises(ValidationError, match="changed while they were read"):
        resident_context.resident_skills(raced_vault)


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("# Missing\n", "missing YAML frontmatter"),
        ("---\nname: exact-skill\n", "unterminated YAML frontmatter"),
        (
            "---\nname: exact-skill\nname: duplicate\n---\n",
            "declare exactly one frontmatter name",
        ),
        ('---\nname: "unterminated\\"\n---\n', "invalid quoted name"),
    ],
)
def test_resident_fallback_rejects_ambiguous_skill_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frontmatter: str,
    message: str,
) -> None:
    _use_resident_fallback(monkeypatch)
    vault = tmp_path / "vault"
    _write_skill(vault, frontmatter=frontmatter)

    with pytest.raises(ValidationError, match=message):
        resident_context.resident_skills(vault)


def test_resident_fallback_rejects_nonportable_depth_and_scan_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_resident_fallback(monkeypatch)

    reserved_vault = tmp_path / "reserved"
    _write_skill(reserved_vault, name="CON")
    with pytest.raises(ValidationError, match="reserved on Windows"):
        resident_context.resident_skills(reserved_vault)

    deep_vault = tmp_path / "deep"
    deep_skill = _write_skill(deep_vault)
    deep = deep_skill
    for index in range(resident_context.MAX_SKILL_DEPTH + 1):
        deep /= f"level-{index}"
    deep.mkdir(parents=True)
    with pytest.raises(ValidationError, match="depth bound"):
        resident_context.resident_skills(deep_vault)

    scan_vault = tmp_path / "scan"
    _write_skill(scan_vault)

    def refuse_scan(path: object) -> Any:
        del path
        raise OSError("denied")

    monkeypatch.setattr(os, "scandir", refuse_scan)
    with pytest.raises(ValidationError, match="could not inspect imported resident skills"):
        resident_context.resident_skills(scan_vault)


def test_resident_privacy_allows_human_documentation_shapes_but_not_opaque_material() -> None:
    documentation = (
        b"LONG_NUMERIC_CONFIGURATION=1234567890123456\n"
        b"/srv/example/Projects/this-is-a-very-long-project-folder/notes\n"
        b"https://example.com/alpha9-bravo8-charlie7-delta6-echo5-foxtrot4\n"
        b"Alpha9-Bravo8-Charlie7-Delta6-Echo5-Foxtrot4\n"
    )
    assert resident_context.validate_imported_resident_content(
        documentation, label="documentation"
    ) == documentation.decode("utf-8")
    with pytest.raises(ValidationError, match="high-entropy-token"):
        resident_context.validate_imported_resident_content(
            b"6QJv8rE2mK9zWp4Lc7Hd1Tx5Bn3Fg0YsUaIeOoPvNqR\n",
            label="opaque material",
        )


def test_sqlite_snapshot_copy_fallback_preserves_exact_main_and_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "provider.sqlite"
    database.write_bytes(b"exact-main")
    wal = tmp_path / "provider.sqlite-wal"
    wal.write_bytes(b"exact-wal")
    monkeypatch.setattr(sys, "platform", "linux")

    with sqlite_snapshot.pinned_sqlite_snapshot(database, label="provider database") as (
        snapshot,
        identity,
        wal_absent,
    ):
        assert snapshot.read_bytes() == b"exact-main"
        assert snapshot.with_name(snapshot.name + "-wal").read_bytes() == b"exact-wal"
        assert snapshot.stat().st_mode & 0o777 == 0o600
        assert identity[:2] == (database.stat().st_dev, database.stat().st_ino)
        assert wal_absent is False


def test_sqlite_snapshot_rejects_path_size_journal_and_identity_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.sqlite"
    with (
        pytest.raises(ValidationError, match="is missing"),
        sqlite_snapshot.pinned_sqlite_snapshot(missing, label="missing database"),
    ):
        pass

    if os.name != "nt":
        target = tmp_path / "target.sqlite"
        target.write_bytes(b"target")
        linked = tmp_path / "linked.sqlite"
        linked.symlink_to(target)
        with (
            pytest.raises(ValidationError, match="regular file, not a link"),
            sqlite_snapshot.pinned_sqlite_snapshot(linked, label="linked database"),
        ):
            pass

    oversized = tmp_path / "oversized.sqlite"
    oversized.write_bytes(b"x")
    monkeypatch.setattr(sqlite_snapshot, "MAX_DATABASE_BYTES", 0)
    with (
        pytest.raises(ValidationError, match="size bound"),
        sqlite_snapshot.pinned_sqlite_snapshot(oversized, label="oversized database"),
    ):
        pass
    monkeypatch.setattr(sqlite_snapshot, "MAX_DATABASE_BYTES", 32 * 1024 * 1024 * 1024)

    journaled = tmp_path / "journaled.sqlite"
    journaled.write_bytes(b"database")
    (tmp_path / "journaled.sqlite-journal").write_bytes(b"active")
    with (
        pytest.raises(ValidationError, match="active rollback journal"),
        sqlite_snapshot.pinned_sqlite_snapshot(journaled, label="journaled database"),
    ):
        pass

    disappeared = tmp_path / "disappeared.sqlite"
    disappeared.write_bytes(b"database")
    with (
        pytest.raises(ValidationError, match="disappeared during the snapshot"),
        sqlite_snapshot.pinned_sqlite_snapshot(disappeared, label="disappeared database"),
    ):
        disappeared.unlink()

    wal_appeared = tmp_path / "wal-appeared.sqlite"
    wal_appeared.write_bytes(b"database")
    with (
        pytest.raises(ValidationError, match="WAL appeared during the snapshot"),
        sqlite_snapshot.pinned_sqlite_snapshot(wal_appeared, label="changing database"),
    ):
        (tmp_path / "wal-appeared.sqlite-wal").write_bytes(b"late")


def test_sqlite_snapshot_open_race_and_copy_mutation_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "raced.sqlite"
    database.write_bytes(b"original")
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(b"replacement")
    original_open = cast(Callable[..., int], os.open)
    swapped = False

    def replace_before_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if Path(path) == database and not swapped:
            swapped = True
            os.replace(replacement, database)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with (
        pytest.raises(ValidationError, match="changed while it was opened"),
        sqlite_snapshot.pinned_sqlite_snapshot(database, label="raced database"),
    ):
        pass

    monkeypatch.setattr(os, "open", original_open)
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"declared")
    descriptor = os.open(source, os.O_RDONLY)
    original_read = os.read
    monkeypatch.setattr(sys, "platform", "linux")
    try:
        monkeypatch.setattr(
            os,
            "read",
            lambda fd, size: b"" if fd == descriptor else original_read(fd, size),
        )
        with pytest.raises(ValidationError, match="ended before its declared size"):
            sqlite_snapshot._copy_or_clone(descriptor, tmp_path / "short.sqlite", max_bytes=64)
    finally:
        os.close(descriptor)

    growing = tmp_path / "growing.sqlite"
    growing.write_bytes(b"declared")
    descriptor = os.open(growing, os.O_RDONLY)
    reads = 0

    def grow_after_copy(fd: int, size: int) -> bytes:
        nonlocal reads
        if fd != descriptor:
            return original_read(fd, size)
        reads += 1
        if reads == 1:
            return original_read(fd, size)
        return b"x"

    monkeypatch.setattr(os, "read", grow_after_copy)
    try:
        with pytest.raises(ValidationError, match="grew past its declared size"):
            sqlite_snapshot._copy_or_clone(descriptor, tmp_path / "grew.sqlite", max_bytes=64)
    finally:
        os.close(descriptor)


def test_sqlite_clone_failure_removes_partial_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"source")
    destination = tmp_path / "partial.sqlite"
    destination.write_bytes(b"partial")
    descriptor = os.open(source, os.O_RDONLY)

    def unavailable_libc(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise OSError("clone unavailable")

    monkeypatch.setattr(ctypes, "CDLL", unavailable_libc)
    try:
        assert sqlite_snapshot._clone_descriptor(descriptor, destination) is False
    finally:
        os.close(descriptor)
    assert not destination.exists()


def test_detached_bridge_worker_dispatches_exact_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stages: list[str] = []
    served: dict[str, Any] = {}

    def serve(vault: Any, *, port: int, instance_id: str) -> None:
        served.update(root=vault.root, port=port, instance_id=instance_id)

    monkeypatch.delenv("GSV_BRIDGE_DETACH_IN_CHILD", raising=False)
    monkeypatch.setattr(bridge_worker, "_startup_marker", stages.append)
    monkeypatch.setattr(bridge, "serve_bridge", serve)

    result = bridge_worker.main(
        ["--vault", str(tmp_path / "vault"), "--port", "43210", "--instance-id", "exact"]
    )

    assert result == 0
    assert served == {"root": tmp_path / "vault", "port": 43210, "instance_id": "exact"}
    assert stages == ["entered", "imports ready", "serving"]


@pytest.mark.parametrize(
    ("soft_limit", "expected_end"),
    [(128, 128), (_RLIM_INFINITY, 1_048_576)],
)
@pytest.mark.skipif(os.name == "nt", reason="detached process groups are POSIX-only")
def test_detached_bridge_worker_closes_descriptors_before_new_session(
    monkeypatch: pytest.MonkeyPatch,
    soft_limit: int,
    expected_end: int,
) -> None:
    assert _RESOURCE is not None
    closed: list[tuple[int, int]] = []
    detached: list[bool] = []
    stages: list[str] = []
    monkeypatch.setenv("GSV_BRIDGE_DETACH_IN_CHILD", "1")
    monkeypatch.setattr(_RESOURCE, "getrlimit", lambda _: (soft_limit, soft_limit))
    monkeypatch.setattr(os, "closerange", lambda start, end: closed.append((start, end)))
    monkeypatch.setattr(os, "setsid", lambda: detached.append(True))
    monkeypatch.setattr(bridge_worker, "_startup_marker", stages.append)

    bridge_worker._detach_if_requested()

    assert closed == [(3, expected_end)]
    assert detached == [True]
    assert stages == ["detached"]
    assert "GSV_BRIDGE_DETACH_IN_CHILD" not in os.environ


def test_bridge_worker_marker_failure_never_prevents_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(descriptor: int, content: bytes) -> int:
        del descriptor, content
        raise OSError("stderr unavailable")

    monkeypatch.setattr(os, "write", fail_write)
    bridge_worker._startup_marker("entered")
