from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path

import pytest

from continuity_kernel import recall as recall_module
from continuity_kernel.app_corpus_qmd import (
    QMDScopedRecord,
    QMDScopedSnapshot,
    ScopedQMDIndexManager,
    ScopedQMDRefreshError,
)
from continuity_kernel.atomic import PinnedPathRoot


def _record(
    connection_id: str, text: str, *, seed: str, legacy: bool = False
) -> tuple[QMDScopedRecord, bytes]:
    content = text.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return (
        QMDScopedRecord(
            key=key,
            connection_id=connection_id,
            document_path=(
                f"documents/{key}.md" if legacy else f"documents/{key}-{digest[:16]}.md"
            ),
            digest=digest,
        ),
        content,
    )


def _snapshot(fingerprint: str, *records: QMDScopedRecord) -> QMDScopedSnapshot:
    return QMDScopedSnapshot(fingerprint=fingerprint, records=tuple(records))


def _write_source(store: PinnedPathRoot, record: QMDScopedRecord, content: bytes) -> None:
    store.ensure_directory("documents")
    store.atomic_write(record.document_path, content)


def _runner(store: PinnedPathRoot, calls: list[tuple[str, ...]]):
    def run(command, **_kwargs):
        calls.append(command)
        if command[3:5] == ("collection", "add"):
            index = command[2]
            documents_root = command[5]
            collection = command[command.index("--name") + 1]
            store.ensure_directory("config")
            store.atomic_write(
                f"config/{index}.yml",
                (
                    "collections:\n"
                    f"  {collection}:\n"
                    f"    path: {documents_root}\n"
                    "    pattern: **/*.md\n"
                ).encode(),
            )
        return recall_module._CommandResult(0, b"", b"")

    return run


def test_scope_view_contains_only_authorized_connection_and_maps_only_manifest_hits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir(mode=0o700)
    store = PinnedPathRoot(root)
    try:
        permitted, permitted_bytes = _record(
            "con_allowed", "approved decision", seed="a", legacy=True
        )
        denied, denied_bytes = _record("con_denied", "many distractors", seed="b")
        _write_source(store, permitted, permitted_bytes)
        _write_source(store, denied, denied_bytes)
        for number in range(80):
            distractor, content = _record(
                "con_denied", f"distractor {number}", seed=f"distractor-{number}"
            )
            _write_source(store, distractor, content)

        calls: list[tuple[str, ...]] = []
        manager = ScopedQMDIndexManager(
            store,
            root,
            executable="qmd",
            environment={"PATH": "/usr/bin:/bin"},
            index="seld-apps",
            run_command=_runner(store, calls),
        )
        bindings = manager.refresh(
            _snapshot("a" * 64, permitted, denied), deadline=time.monotonic() + 10
        )
        binding = next(item for item in bindings if item.connection_id == "con_allowed")
        allowed_name = Path(permitted.document_path).name
        denied_name = Path(denied.document_path).name

        assert tuple(path.name for path in binding.documents_root.glob("*.md")) == (allowed_name,)
        metadata = os.lstat(binding.documents_root / allowed_name)
        assert stat.S_ISREG(metadata.st_mode)
        assert not stat.S_ISLNK(metadata.st_mode)
        assert (
            binding.record_key_for_reference(f"qmd://{binding.collection}/{allowed_name}")
            == permitted.key
        )
        assert (
            binding.record_key_for_reference(f"qmd://{binding.collection}/{denied_name}") is None
        )
        assert (
            binding.record_key_for_reference(str(binding.documents_root / allowed_name))
            == permitted.key
        )
        assert binding.record_key_for_reference(f"qmd://other/{allowed_name}") is None
        assert all(command[:3] == ("qmd", "--index", command[2]) for command in calls)
        collection_roots = [command[5] for command in calls if "collection" in command]
        assert str(binding.documents_root) in collection_roots
        assert all(root_value != str(root / "documents") for root_value in collection_roots)
    finally:
        store.close()


def test_unchanged_scope_reuses_view_but_changed_scope_updates_only_that_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir(mode=0o700)
    store = PinnedPathRoot(root)
    try:
        first_a, first_a_bytes = _record("con_a", "first A", seed="a")
        record_b, record_b_bytes = _record("con_b", "first B", seed="b")
        _write_source(store, first_a, first_a_bytes)
        _write_source(store, record_b, record_b_bytes)
        calls: list[tuple[str, ...]] = []
        manager = ScopedQMDIndexManager(
            store,
            root,
            executable="qmd",
            environment={},
            index="seld-apps",
            run_command=_runner(store, calls),
        )

        initial = manager.refresh(
            _snapshot("a" * 64, first_a, record_b), deadline=time.monotonic() + 10
        )
        calls.clear()
        reused = manager.refresh(
            _snapshot("b" * 64, first_a, record_b), deadline=time.monotonic() + 10
        )

        assert calls == []
        assert {item.snapshot_fingerprint for item in reused} == {"b" * 64}

        changed_a, changed_a_bytes = _record("con_a", "changed A", seed="a")
        _write_source(store, changed_a, changed_a_bytes)
        changed = manager.refresh(
            _snapshot("c" * 64, changed_a, record_b), deadline=time.monotonic() + 10
        )
        a_binding = next(item for item in changed if item.connection_id == "con_a")
        b_binding = next(item for item in changed if item.connection_id == "con_b")

        assert [command[3] for command in calls] == ["update", "embed"]
        assert all(command[2] == a_binding.index for command in calls)
        assert tuple(path.name for path in a_binding.documents_root.glob("*.md")) == (
            Path(changed_a.document_path).name,
        )
        assert not (a_binding.documents_root / Path(first_a.document_path).name).exists()
        assert tuple(path.name for path in b_binding.documents_root.glob("*.md")) == (
            Path(record_b.document_path).name,
        )
        assert initial[0].scope_fingerprint != a_binding.scope_fingerprint
    finally:
        store.close()


def test_qmd_failure_never_returns_a_scope_binding(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir(mode=0o700)
    store = PinnedPathRoot(root)
    try:
        record, content = _record("con_a", "content", seed="a")
        _write_source(store, record, content)
        calls: list[tuple[str, ...]] = []

        def fail(command, **_kwargs):
            calls.append(command)
            return recall_module._CommandResult(1, b"", b"failure")

        manager = ScopedQMDIndexManager(
            store,
            root,
            executable="qmd",
            environment={},
            index="seld-apps",
            run_command=fail,
        )

        with pytest.raises(ScopedQMDRefreshError, match="scoped app QMD refresh failed"):
            manager.refresh(_snapshot("a" * 64, record), deadline=time.monotonic() + 10)

        assert calls
        assert not next((root / "qmd-scopes").rglob("manifest.json"), None)
    finally:
        store.close()


def test_scope_binding_check_requires_the_exact_qmd_collection_target(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir(mode=0o700)
    store = PinnedPathRoot(root)
    try:
        record, content = _record("con_a", "trusted content", seed="a")
        _write_source(store, record, content)
        calls: list[tuple[str, ...]] = []
        manager = ScopedQMDIndexManager(
            store,
            root,
            executable="qmd",
            environment={},
            index="seld-apps",
            run_command=_runner(store, calls),
        )
        binding = manager.refresh(_snapshot("a" * 64, record), deadline=time.monotonic() + 10)[0]

        def bound(command, **_kwargs):
            if command[3:5] == ("collection", "show"):
                return recall_module._CommandResult(
                    0,
                    (
                        f"Collection: {binding.collection}\n"
                        f"  Path:     {binding.documents_root}\n"
                        "  Pattern:  **/*.md\n"
                    ).encode(),
                    b"",
                )
            return recall_module._CommandResult(0, b"", b"")

        assert (
            binding.collection_binding_problem(
                "qmd",
                cwd=root,
                environment={},
                deadline=time.monotonic() + 10,
                run_command=bound,
            )
            is None
        )

        def rebound(command, **_kwargs):
            if command[3:5] == ("collection", "show"):
                return recall_module._CommandResult(
                    0,
                    (
                        f"Collection: {binding.collection}\n"
                        f"  Path:     {root / 'foreign'}\n"
                        "  Pattern:  **/*.md\n"
                    ).encode(),
                    b"",
                )
            return recall_module._CommandResult(0, b"", b"")

        assert (
            binding.collection_binding_problem(
                "qmd",
                cwd=root,
                environment={},
                deadline=time.monotonic() + 10,
                run_command=rebound,
            )
            == "QMD scoped app corpus collection binding is unverified"
        )
    finally:
        store.close()
