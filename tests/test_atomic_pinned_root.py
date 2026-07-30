from __future__ import annotations

import errno
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel import atomic as atomic_module
from continuity_kernel.control_queue import EMPTY_REVISION, ControlQueue, ControlStorageError
from continuity_kernel.errors import ValidationError
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned storage is POSIX-only foundation"
)
_POSIX_OS = cast(Any, os)


def test_pinned_directory_listing_is_sorted_filtered_and_bounded(tmp_path: Path) -> None:
    root = tmp_path / "pinned"
    directory = root / "history"
    directory.mkdir(parents=True)
    (directory / "b.jsonl").write_bytes(b"b")
    (directory / "a.jsonl").write_bytes(b"a")
    (directory / "note.txt").write_bytes(b"note")
    store = atomic_module.PinnedPathRoot(root)
    try:
        assert store.list_directory_entry_names("history", max_entries=3, suffix=".jsonl") == (
            "a.jsonl",
            "b.jsonl",
        )
        with pytest.raises(ValidationError, match="supported bound"):
            store.list_directory_entry_names("history", max_entries=2, suffix=".jsonl")
    finally:
        store.close()


@pytest.mark.parametrize("error_number", (errno.ELOOP, errno.ENOTDIR))
def test_pinned_read_maps_nofollow_path_faults_to_stable_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    root = tmp_path / "pinned"
    value = root / "control/value"
    value.parent.mkdir(parents=True)
    value.write_bytes(b"canonical")
    store = atomic_module.PinnedPathRoot(root)
    actual_open = os.open

    def fail_leaf_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "value" and kwargs.get("dir_fd") is not None:
            raise OSError(error_number, "injected no-follow path fault")
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_leaf_open)
    try:
        with pytest.raises(ValidationError, match="could not read value beneath the pinned root"):
            store.read_regular_file("control/value", label="value", max_bytes=64)
    finally:
        store.close()


def test_pinned_directory_count_uses_descriptor_and_honors_match_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "one.json").write_bytes(b"one")
    (receipts / "two.json").write_bytes(b"two")
    (receipts / "ignored.tmp").write_bytes(b"temp")
    actual_scandir = os.scandir
    observed: list[object] = []

    def tracked_scandir(path: Any) -> Any:
        observed.append(path)
        return actual_scandir(path)

    monkeypatch.setattr(os, "scandir", tracked_scandir)
    store = atomic_module.PinnedPathRoot(root)
    try:
        assert store.count_directory_entries("receipts", suffix=".json") == 2
        assert store.count_directory_entries("receipts", suffix=".json", stop_at=1) == 1
    finally:
        store.close()

    assert observed
    assert all(isinstance(value, int) for value in observed)


def test_pinned_write_reports_unknown_if_root_is_replaced_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    root.mkdir()
    (root / "control").mkdir()
    parked = tmp_path / "pinned-parked"
    actual_rename = os.rename
    swapped = False

    def publish_then_replace_root(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        actual_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if target == "value" and not swapped:
            swapped = True
            actual_rename(root, parked)
            root.mkdir()
            (root / "control").mkdir()

    monkeypatch.setattr(os, "rename", publish_then_replace_root)

    store = atomic_module.PinnedPathRoot(root)
    try:
        with pytest.raises(atomic_module.DurablePublishError) as raised:
            store.atomic_write("control/value", b"published-before-root-swap")
    finally:
        store.close()

    assert swapped is True
    assert raised.value.outcome is atomic_module.PublishOutcome.UNKNOWN
    assert not (root / "control/value").exists()
    assert (parked / "control/value").read_bytes() == b"published-before-root-swap"


def test_pinned_write_never_reports_committed_after_descendant_swap_and_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    control = root / "control"
    control.mkdir(parents=True)
    parked = root / "control-parked"
    actual_rename = os.rename
    actual_fsync = os.fsync
    swapped = False

    def publish_then_replace_parent(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        actual_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if target == "value" and not swapped:
            swapped = True
            actual_rename(control, parked)
            control.mkdir()

    def fail_parent_fsync_after_swap(descriptor: int) -> None:
        if swapped:
            raise OSError("injected parent fsync failure after descendant swap")
        actual_fsync(descriptor)

    monkeypatch.setattr(os, "rename", publish_then_replace_parent)
    monkeypatch.setattr(os, "fsync", fail_parent_fsync_after_swap)
    store = atomic_module.PinnedPathRoot(root)
    try:
        with pytest.raises(atomic_module.DurablePublishError) as raised:
            store.atomic_write("control/value", b"published outside canonical path")
    finally:
        store.close()

    assert swapped is True
    assert raised.value.outcome is atomic_module.PublishOutcome.UNKNOWN
    assert not (control / "value").exists()
    assert (parked / "value").read_bytes() == b"published outside canonical path"


def test_hard_link_publication_never_reports_committed_after_foreign_target_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "staged"
    target = tmp_path / "published"
    source.write_bytes(b"owned staged bytes")
    swapped = False

    def replace_target_then_fail(_directory: Path) -> None:
        nonlocal swapped
        target.unlink()
        target.write_bytes(b"foreign target bytes")
        swapped = True
        raise OSError("injected directory fsync failure after foreign replacement")

    monkeypatch.setattr(atomic_module, "_fsync_directory", replace_target_then_fail)

    with pytest.raises(atomic_module.DurablePublishError) as raised:
        atomic_module.durable_publish_new(source, target)

    assert swapped is True
    assert raised.value.outcome is atomic_module.PublishOutcome.UNKNOWN
    assert source.read_bytes() == b"owned staged bytes"
    assert target.read_bytes() == b"foreign target bytes"


def test_pinned_read_rejects_a_replaced_canonical_root(tmp_path: Path) -> None:
    root = tmp_path / "pinned"
    root.mkdir()
    (root / "control").mkdir()
    (root / "control/value").write_bytes(b"original")
    parked = tmp_path / "pinned-parked"
    store = atomic_module.PinnedPathRoot(root)
    root.rename(parked)
    root.mkdir()
    (root / "control").mkdir()
    (root / "control/value").write_bytes(b"replacement")

    try:
        with pytest.raises(ValidationError, match="canonical path"):
            store.read_regular_file("control/value", label="value", max_bytes=64)
    finally:
        store.close()


def test_pinned_read_rejects_a_replaced_descendant_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    control = root / "control"
    control.mkdir(parents=True)
    (control / "value").write_bytes(b"stale")
    parked = root / "control-parked"
    store = atomic_module.PinnedPathRoot(root)
    actual_open_parent = store._open_parent
    swapped = False

    def open_parent_then_replace_descendant(
        relative: Path | str | tuple[str, ...], *, create_parent: bool = False
    ) -> tuple[int, str]:
        nonlocal swapped
        parent, name = actual_open_parent(relative, create_parent=create_parent)
        if not swapped:
            control.rename(parked)
            control.mkdir()
            (control / "value").write_bytes(b"new canonical bytes")
            swapped = True
        return parent, name

    monkeypatch.setattr(store, "_open_parent", open_parent_then_replace_descendant)
    try:
        with pytest.raises(ValidationError, match="directory changed identity"):
            store.read_regular_file("control/value", label="value", max_bytes=64)
    finally:
        store.close()

    assert swapped is True
    assert (parked / "value").read_bytes() == b"stale"
    assert (control / "value").read_bytes() == b"new canonical bytes"


def test_descendant_swap_cannot_bypass_control_queue_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    Vault(root).initialize(name="Descendant CAS proof")
    queue = ControlQueue(root)
    first = queue.append(
        kind="correction",
        subject="mind:first",
        choice="first",
        expected_revision=EMPTY_REVISION,
        observed_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )
    stale_bytes = queue.path.read_bytes()
    newer = queue.append(
        kind="correction",
        subject="mind:second",
        choice="second",
        expected_revision=first.revision,
        observed_at=datetime(2026, 7, 24, 12, 1, tzinfo=UTC),
    )
    newer_bytes = queue.path.read_bytes()
    queue.path.write_bytes(stale_bytes)
    control = root / ".gsv/control"
    parked = root / ".gsv/control-parked"
    actual_open_parent = atomic_module.PinnedPathRoot._open_parent
    swapped = False

    def open_queue_then_install_newer_canonical_parent(
        store: atomic_module.PinnedPathRoot,
        relative: Path | str | tuple[str, ...],
        *,
        create_parent: bool = False,
    ) -> tuple[int, str]:
        nonlocal swapped
        parent, name = actual_open_parent(store, relative, create_parent=create_parent)
        if name == "queue.jsonl" and not swapped:
            control.rename(parked)
            control.mkdir()
            (control / "initialized").write_bytes(b'{"schema_version":1,"state":"initialized"}\n')
            (control / "queue.jsonl").write_bytes(newer_bytes)
            swapped = True
        return parent, name

    monkeypatch.setattr(
        atomic_module.PinnedPathRoot,
        "_open_parent",
        open_queue_then_install_newer_canonical_parent,
    )

    with pytest.raises(ControlStorageError, match="directory changed identity"):
        queue.append(
            kind="correction",
            subject="mind:third",
            choice="must not replace newer state",
            expected_revision=first.revision,
            observed_at=datetime(2026, 7, 24, 12, 2, tzinfo=UTC),
        )

    assert swapped is True
    assert queue.snapshot() == newer
    assert (parked / "queue.jsonl").read_bytes() == stale_bytes


def test_pinned_directory_result_rejects_a_replaced_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    control = root / "control"
    control.mkdir(parents=True)
    parked = root / "control-parked"
    store = atomic_module.PinnedPathRoot(root)
    actual_open_directory = store._open_directory
    swapped = False

    def open_directory_then_replace_descendant(
        parts: tuple[str, ...], *, create: bool = False, mode: int = 0o700
    ) -> int:
        nonlocal swapped
        descriptor = actual_open_directory(parts, create=create, mode=mode)
        if parts == ("control",) and not swapped:
            control.rename(parked)
            control.mkdir()
            swapped = True
        return descriptor

    monkeypatch.setattr(store, "_open_directory", open_directory_then_replace_descendant)
    try:
        with pytest.raises(ValidationError, match="directory changed identity"):
            store.directory_exists("control")
    finally:
        store.close()

    assert swapped is True


def test_missing_read_rechecks_root_before_reporting_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    root.mkdir()
    (root / "control").mkdir()
    parked = tmp_path / "pinned-parked"
    store = atomic_module.PinnedPathRoot(root)
    actual_open_parent = store._open_parent

    def open_parent_then_replace_root(
        relative: Path | str, *, create_parent: bool = False
    ) -> tuple[int, str]:
        parent, name = actual_open_parent(relative, create_parent=create_parent)
        root.rename(parked)
        root.mkdir()
        (root / "control").mkdir()
        return parent, name

    monkeypatch.setattr(store, "_open_parent", open_parent_then_replace_root)
    try:
        with pytest.raises(ValidationError, match="canonical path"):
            store.read_regular_file(
                "control/missing",
                label="missing value",
                max_bytes=64,
                missing_ok=True,
            )
    finally:
        store.close()


def test_pinned_lock_never_yields_after_canonical_root_replacement(tmp_path: Path) -> None:
    root = tmp_path / "pinned"
    root.mkdir()
    parked = tmp_path / "pinned-parked"
    old_store = atomic_module.PinnedPathRoot(root)
    root.rename(parked)
    root.mkdir()
    yielded = False

    try:
        with (
            pytest.raises(ValidationError, match="canonical path"),
            old_store.exclusive_root_lock(),
        ):
            yielded = True
    finally:
        old_store.close()

    assert yielded is False
    replacement_store = atomic_module.PinnedPathRoot(root)
    try:
        with replacement_store.exclusive_root_lock():
            pass
    finally:
        replacement_store.close()


def test_pinned_lock_detects_root_replacement_before_success(tmp_path: Path) -> None:
    root = tmp_path / "pinned"
    root.mkdir()
    parked = tmp_path / "pinned-parked"
    store = atomic_module.PinnedPathRoot(root)

    try:
        with (
            pytest.raises(ValidationError, match="canonical path"),
            store.exclusive_root_lock(),
        ):
            root.rename(parked)
            root.mkdir()
    finally:
        store.close()


def test_pinned_file_lock_rejects_leaf_replacement_immediately_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    lock_directory = root / ".gsv/locks"
    lock_directory.mkdir(parents=True)
    lock_path = lock_directory / "global.lock"
    lock_path.write_bytes(b"0")
    parked = lock_directory / "global.lock-parked"
    actual_open = os.open
    replaced = False

    def open_then_replace_lock(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        descriptor = actual_open(path, flags, mode, dir_fd=dir_fd)
        if path == "global.lock" and dir_fd is not None and not replaced:
            replaced = True
            os.rename("global.lock", "global.lock-parked", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            replacement = actual_open(
                "global.lock",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | cast(Any, os).O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                os.write(replacement, b"0")
            finally:
                os.close(replacement)
        return descriptor

    store = atomic_module.PinnedPathRoot(root)
    monkeypatch.setattr(os, "open", open_then_replace_lock)
    yielded = False
    try:
        with (
            pytest.raises(ValidationError, match="lock target changed identity"),
            store.exclusive_file_lock(".gsv/locks/global.lock"),
        ):
            yielded = True
    finally:
        store.close()

    assert replaced is True
    assert yielded is False
    assert parked.read_bytes() == b"0"
    assert lock_path.read_bytes() == b"0"


def test_pinned_file_lock_rejects_leaf_replacement_after_acquire_before_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    lock_directory = root / ".gsv/locks"
    lock_directory.mkdir(parents=True)
    lock_path = lock_directory / "global.lock"
    lock_path.write_bytes(b"0")
    parked = lock_directory / "global.lock-parked"
    actual_acquire = atomic_module._acquire
    replaced = False

    def acquire_then_replace_lock(handle: atomic_module._LockTarget, *, timeout: float) -> None:
        nonlocal replaced
        actual_acquire(handle, timeout=timeout)
        if not replaced:
            replaced = True
            lock_path.rename(parked)
            lock_path.write_bytes(b"0")

    store = atomic_module.PinnedPathRoot(root)
    monkeypatch.setattr(atomic_module, "_acquire", acquire_then_replace_lock)
    yielded = False
    try:
        with (
            pytest.raises(ValidationError, match="lock target changed identity"),
            store.exclusive_file_lock(".gsv/locks/global.lock"),
        ):
            yielded = True
    finally:
        store.close()

    assert replaced is True
    assert yielded is False
    assert parked.read_bytes() == b"0"
    assert lock_path.read_bytes() == b"0"


def test_pinned_file_lock_detects_leaf_replacement_before_normal_exit(tmp_path: Path) -> None:
    root = tmp_path / "pinned"
    lock_directory = root / ".gsv/locks"
    lock_directory.mkdir(parents=True)
    lock_path = lock_directory / "global.lock"
    parked = lock_directory / "global.lock-parked"
    store = atomic_module.PinnedPathRoot(root)
    replacement_store = atomic_module.PinnedPathRoot(root)
    replacement_acquired = False

    try:
        with (
            pytest.raises(ValidationError, match="lock target changed identity"),
            store.exclusive_file_lock(".gsv/locks/global.lock"),
        ):
            lock_path.rename(parked)
            lock_path.write_bytes(b"0")
            with replacement_store.exclusive_file_lock(".gsv/locks/global.lock"):
                replacement_acquired = True
    finally:
        replacement_store.close()
        store.close()

    assert replacement_acquired is True
    assert parked.read_bytes() == b"0"
    assert lock_path.read_bytes() == b"0"


def test_pinned_file_lock_never_yields_after_descendant_parent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    lock_directory = root / ".gsv/locks"
    lock_directory.mkdir(parents=True)
    lock_path = lock_directory / "global.lock"
    lock_path.write_bytes(b"0")
    parked = root / ".gsv/locks-parked"
    actual_acquire = atomic_module._acquire
    swapped = False

    def acquire_then_replace_parent(handle: atomic_module._LockTarget, *, timeout: float) -> None:
        nonlocal swapped
        actual_acquire(handle, timeout=timeout)
        if not swapped:
            lock_directory.rename(parked)
            lock_directory.mkdir()
            (lock_directory / "global.lock").write_bytes(b"0")
            swapped = True

    store = atomic_module.PinnedPathRoot(root)
    monkeypatch.setattr(atomic_module, "_acquire", acquire_then_replace_parent)
    yielded = False
    try:
        with (
            pytest.raises(ValidationError, match="directory changed identity"),
            store.exclusive_file_lock(".gsv/locks/global.lock"),
        ):
            yielded = True
    finally:
        store.close()

    assert swapped is True
    assert yielded is False
    assert (parked / "global.lock").read_bytes() == b"0"
    assert (lock_directory / "global.lock").read_bytes() == b"0"


def test_regular_file_watch_second_dup_failure_closes_first_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    records = root / "records"
    records.mkdir(parents=True)
    path = records / "value"
    path.write_bytes(b"value")
    parent = os.open(records, os.O_RDONLY | _POSIX_OS.O_DIRECTORY)
    descriptor = os.open(path, os.O_RDONLY)
    baseline = len(os.listdir("/dev/fd"))
    actual_dup = os.dup
    calls = 0

    def fail_second_duplicate(value: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second dup failure")
        return actual_dup(value)

    monkeypatch.setattr(os, "dup", fail_second_duplicate)
    try:
        with pytest.raises(OSError, match="second dup"):
            atomic_module.PinnedPathRoot._duplicate_regular_file_watch(
                ("records", "value"),
                parent,
                descriptor,
                identity=(path.stat().st_dev, path.stat().st_ino),
            )
        assert len(os.listdir("/dev/fd")) == baseline
    finally:
        monkeypatch.setattr(os, "dup", actual_dup)
        os.close(descriptor)
        os.close(parent)


def test_named_retain_dup_failure_preserves_previous_authoritative_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    records = root / "records"
    records.mkdir(parents=True)
    path = records / "value"
    path.write_bytes(b"value")
    store = atomic_module.PinnedPathRoot(root)
    store.read_regular_file(
        "records/value",
        label="value",
        max_bytes=1024,
        retain=True,
    )
    previous = store._retained_files[("records", "value")]
    baseline = len(os.listdir("/dev/fd"))

    def fail_watch_duplicate(*args: object, **kwargs: object) -> atomic_module._RegularFileWatch:
        raise OSError("injected watch duplication failure")

    monkeypatch.setattr(
        atomic_module.PinnedPathRoot,
        "_duplicate_regular_file_watch",
        staticmethod(fail_watch_duplicate),
    )
    parent = os.open(records, os.O_RDONLY | _POSIX_OS.O_DIRECTORY)
    try:
        with pytest.raises(OSError, match="watch duplication"):
            store._retain_named_regular_file(
                ("records", "value"),
                parent,
                "value",
                expected_identity=(path.stat().st_dev, path.stat().st_ino),
                label="value",
            )
        assert store._retained_files[("records", "value")] is previous
        assert len(os.listdir("/dev/fd")) == baseline + 1
    finally:
        os.close(parent)
        store.close()


def test_directory_traversal_close_error_never_retries_or_leaks_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    (root / "one/two").mkdir(parents=True)
    store = atomic_module.PinnedPathRoot(root)
    baseline = len(os.listdir("/dev/fd"))
    current = os.dup(store._root_descriptor)
    actual_close = os.close
    actual_open = os.open
    injected = False
    close_calls: dict[int, int] = {}
    opened_children: list[int] = []

    def tracked_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = actual_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        opened_children.append(descriptor)
        return descriptor

    def consume_then_fail(descriptor: int) -> None:
        nonlocal injected
        close_calls[descriptor] = close_calls.get(descriptor, 0) + 1
        if not injected and descriptor == current:
            injected = True
            actual_close(descriptor)
            raise OSError("injected consumed close failure")
        actual_close(descriptor)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", consume_then_fail)
    try:
        with pytest.raises(ValidationError, match="could not traverse"):
            store._open_directory_from_descriptor(
                current,
                ("one", "two"),
                create=False,
                mode=0o700,
            )
        assert injected
        assert close_calls[current] == 1
        assert opened_children
        assert close_calls[opened_children[0]] == 1
        assert len(os.listdir("/dev/fd")) == baseline
    finally:
        monkeypatch.setattr(os, "close", actual_close)
        monkeypatch.setattr(os, "open", actual_open)
        store.close()
