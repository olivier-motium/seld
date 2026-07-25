from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import atomic as atomic_module
from continuity_kernel import control_queue as control_queue_module
from continuity_kernel.control_queue import (
    EMPTY_REVISION,
    ControlQueue,
    ControlStorageError,
    locked_control_store,
)
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    ValidationError,
)
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)


def _queue(tmp_path: Path) -> ControlQueue:
    root = tmp_path / "vault"
    Vault(root).initialize(name="Control queue")
    return ControlQueue(root)


def test_append_is_cas_protected_and_preserves_existing_events(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    first = queue.append(
        kind="setup_choice",
        subject="source:gmail",
        choice="selected",
        expected_revision=EMPTY_REVISION,
        observed_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )
    second = queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="Family time is protected on Friday evening.",
        expected_revision=first.revision,
        target_revision="a" * 64,
        observed_at=datetime(2026, 7, 24, 12, 1, tzinfo=UTC),
    )

    assert [event.kind.value for event in second.events] == ["setup_choice", "correction"]
    assert second.events[1].target_revision == "a" * 64
    assert queue.snapshot() == second
    with pytest.raises(ConflictError, match="reload"):
        queue.append(
            kind="approval",
            subject="operation:send-reply",
            choice="approve",
            expected_revision=EMPTY_REVISION,
        )


def test_locked_control_store_recovers_a_missing_internal_lock_directory(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    locks = queue.vault_root / ".gsv/locks"
    for child in locks.iterdir():
        child.unlink()
    locks.rmdir()

    written = queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="Keep Friday evening protected.",
        expected_revision=EMPTY_REVISION,
    )

    assert len(written.events) == 1
    assert locks.is_dir()


def test_control_snapshot_does_not_fsync_an_existing_lock_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _queue(tmp_path)
    queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Keep Friday evening protected.",
        expected_revision=EMPTY_REVISION,
    )
    synchronized: list[int] = []
    actual_fsync = os.fsync

    def observed_fsync(descriptor: int) -> None:
        synchronized.append(descriptor)
        actual_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observed_fsync)

    queue.snapshot()
    queue.snapshot()

    assert synchronized == []


@pytest.mark.parametrize("kind", ["approval", "undo_request"])
def test_only_narrow_control_shapes_are_persisted(tmp_path: Path, kind: str) -> None:
    queue = _queue(tmp_path)
    snapshot = queue.append(
        kind=kind,
        subject="operation:example",
        choice="approved" if kind == "approval" else "undo",
        expected_revision=EMPTY_REVISION,
    )
    stored = json.loads(queue.path.read_text(encoding="utf-8").splitlines()[1])

    assert set(stored) == {
        "choice",
        "created_at",
        "event_id",
        "kind",
        "schema_version",
        "source",
        "subject",
        "target_revision",
    }
    assert stored["kind"] == kind
    assert stored["source"] == "bridge"
    assert snapshot.events[0].event_id == stored["event_id"]


def test_unknown_kind_and_unbounded_content_are_rejected(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(ValidationError, match="allowed"):
        queue.append(
            kind="semantic_write",
            subject="mind:user-correction",
            choice="replace it",
            expected_revision=EMPTY_REVISION,
        )
    with pytest.raises(ValidationError, match="size bound"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="x" * 5000,
            expected_revision=EMPTY_REVISION,
        )


@pytest.mark.parametrize(
    "revision",
    (
        "0x" + "a" * 62,
        "+" + "a" * 63,
        "-" + "a" * 63,
        "a_" + "a" * 62,
        " " + "a" * 63,
    ),
)
def test_revision_fields_reject_noncanonical_hex_spellings(tmp_path: Path, revision: str) -> None:
    queue = _queue(tmp_path)

    with pytest.raises(ValidationError, match="SHA-256 revision"):
        queue.append(
            kind="correction",
            subject="record:direction.current",
            choice="Keep Friday evening protected.",
            expected_revision=EMPTY_REVISION,
            target_revision=revision,
        )


@pytest.mark.parametrize(
    ("kind", "subject", "message"),
    [
        ("setup_choice", "mind:gmail", "source:"),
        ("approval", "source:gmail", "operation:"),
        ("undo_request", "mind:last-operation", "operation:"),
        ("correction", "source:gmail", "mind: or record:"),
        ("correction", "not namespaced", "namespaced reference"),
    ],
)
def test_subject_is_a_kind_specific_reference_not_free_text(
    tmp_path: Path, kind: str, subject: str, message: str
) -> None:
    queue = _queue(tmp_path)

    with pytest.raises(ValidationError, match=message):
        queue.append(
            kind=kind,
            subject=subject,
            choice="The choice is the only free-text field.",
            expected_revision=EMPTY_REVISION,
        )


def test_stored_subject_is_revalidated_against_its_kind(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Keep Friday evening free.",
        expected_revision=EMPTY_REVISION,
    )
    records = [json.loads(line) for line in queue.path.read_text(encoding="utf-8").splitlines()]
    records[1]["subject"] = "source:gmail"
    event_line = json.dumps(records[1], separators=(",", ":"), sort_keys=True) + "\n"
    records[0]["events_digest"] = sha256(event_line.encode()).hexdigest()
    queue.path.write_text(
        json.dumps(records[0], separators=(",", ":"), sort_keys=True) + "\n" + event_line,
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="mind: or record:"):
        queue.snapshot()


def test_partial_or_forged_queue_fails_closed(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.path.parent.mkdir(parents=True, exist_ok=True)
    queue.path.write_text('{"kind":"approval"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="partial"):
        queue.snapshot()

    event = {
        "choice": "approve",
        "created_at": "2026-07-24T12:00:00Z",
        "event_id": "00000000-0000-0000-0000-000000000000",
        "kind": "approval",
        "schema_version": 1,
        "source": "provider",
        "subject": "operation:test",
        "target_revision": None,
    }
    event_line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
    header = {
        "disposition_head_revision": None,
        "event_count": 1,
        "events_digest": sha256(event_line.encode()).hexdigest(),
        "generation": 0,
        "opened_at": "2026-07-24T12:00:00Z",
        "previous_revision": None,
        "record_type": "generation",
        "schema_version": 1,
    }
    queue.path.write_text(
        json.dumps(header, separators=(",", ":"), sort_keys=True) + "\n" + event_line,
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="unsupported source"):
        queue.snapshot()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("opened_at", "2026-07-24T12:00:00"),
        ("created_at", "2026-07-24T14:00:00+02:00"),
    ],
)
def test_stored_queue_requires_canonical_utc_timestamps(
    tmp_path: Path, field: str, value: str
) -> None:
    queue = _queue(tmp_path)
    queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Keep Friday evening free.",
        expected_revision=EMPTY_REVISION,
    )
    records = [json.loads(line) for line in queue.path.read_text(encoding="utf-8").splitlines()]
    target = records[0] if field == "opened_at" else records[1]
    target[field] = value
    event_line = json.dumps(records[1], separators=(",", ":"), sort_keys=True) + "\n"
    records[0]["events_digest"] = sha256(event_line.encode()).hexdigest()
    queue.path.write_text(
        json.dumps(records[0], separators=(",", ":"), sort_keys=True) + "\n" + event_line,
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="timestamp"):
        queue.snapshot()


def test_missing_or_forged_generation_state_fails_closed(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Do not lose this.",
        expected_revision=EMPTY_REVISION,
    )
    queue.path.unlink()

    with pytest.raises(ValidationError, match="disappeared after initialization"):
        queue.snapshot()
    with pytest.raises(ValidationError, match="disappeared after initialization"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="Do not replace the missing queue.",
            expected_revision=EMPTY_REVISION,
        )

    forged = {
        "disposition_head_revision": None,
        "event_count": 0,
        "events_digest": sha256(b"").hexdigest(),
        "generation": 1,
        "opened_at": "2026-07-24T12:00:00Z",
        "previous_revision": "a" * 64,
        "record_type": "generation",
        "schema_version": 1,
    }
    queue.path.write_text(
        json.dumps(forged, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="no valid archived predecessor"):
        queue.snapshot()


def test_snapshot_bounds_lineage_io_but_mutation_verifies_the_full_chain(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    first = queue.append(
        kind="correction",
        subject="record:first",
        choice="first generation",
        expected_revision=EMPTY_REVISION,
    )
    with locked_control_store(queue.vault_root) as store:
        first_rotation = queue._rotate_closed_with_store(
            store,
            expected_revision=first.revision,
            closed_event_ids=frozenset({first.events[0].event_id}),
        )
    second = queue.append(
        kind="correction",
        subject="record:second",
        choice="second generation",
        expected_revision=first_rotation["revision"],
    )
    with locked_control_store(queue.vault_root) as store:
        second_rotation = queue._rotate_closed_with_store(
            store,
            expected_revision=second.revision,
            closed_event_ids=frozenset({second.events[0].event_id}),
        )
    oldest = queue.vault_root / first_rotation["archive"]
    oldest.write_bytes(b"corrupt old history\n")

    snapshot = queue.snapshot()
    assert snapshot.generation == 2
    assert snapshot.revision == second_rotation["revision"]

    with pytest.raises(ControlStorageError, match="predecessor archive"):
        queue.append(
            kind="correction",
            subject="record:third",
            choice="mutation must audit full lineage",
            expected_revision=snapshot.revision,
        )


def test_empty_preinitialization_directory_is_recoverable(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.path.parent.mkdir(parents=True)

    assert queue.snapshot().revision == EMPTY_REVISION

    appended = queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="This survives first initialization.",
        expected_revision=EMPTY_REVISION,
    )

    assert queue.snapshot() == appended
    assert (queue.path.parent / "initialized").read_bytes() == (
        control_queue_module._MARKER_INITIALIZED
    )


def test_failed_first_queue_publication_leaves_retryable_preparing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    actual_write = atomic_module.PinnedPathRoot.atomic_write
    failed = False

    def fail_first_queue_write(
        store: atomic_module.PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        nonlocal failed
        if str(relative) == ".gsv/control/queue.jsonl" and not failed:
            failed = True
            raise OSError("injected pre-publication failure")
        actual_write(store, relative, content, mode=mode)

    monkeypatch.setattr(atomic_module.PinnedPathRoot, "atomic_write", fail_first_queue_write)

    with pytest.raises(ControlStorageError, match="was not published"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="Retry me safely.",
            expected_revision=EMPTY_REVISION,
        )

    assert not queue.path.exists()
    assert (queue.path.parent / "initialized").read_bytes() == (
        control_queue_module._MARKER_PREPARING
    )
    assert ControlQueue(queue.vault_root).snapshot().revision == EMPTY_REVISION

    retried = ControlQueue(queue.vault_root).append(
        kind="correction",
        subject="mind:user-correction",
        choice="Retry me safely.",
        expected_revision=EMPTY_REVISION,
    )

    assert [event.choice for event in retried.events] == ["Retry me safely."]
    assert (queue.path.parent / "initialized").read_bytes() == (
        control_queue_module._MARKER_INITIALIZED
    )


def test_committed_queue_publication_failure_is_reported_and_visible_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    actual_write = atomic_module.PinnedPathRoot.atomic_write
    failed = False

    def commit_queue_then_fail(
        store: atomic_module.PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        nonlocal failed
        actual_write(store, relative, content, mode=mode)
        if str(relative) == ".gsv/control/queue.jsonl" and not failed:
            failed = True
            raise atomic_module.DurablePublishError(
                "injected post-rename directory fsync failure",
                outcome=atomic_module.PublishOutcome.COMMITTED,
            )

    monkeypatch.setattr(atomic_module.PinnedPathRoot, "atomic_write", commit_queue_then_fail)

    with pytest.raises(MutationCommittedError, match="visible"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="Already committed.",
            expected_revision=EMPTY_REVISION,
        )

    fresh = ControlQueue(queue.vault_root).snapshot()
    assert [event.choice for event in fresh.events] == ["Already committed."]
    with pytest.raises(ConflictError, match="reload"):
        ControlQueue(queue.vault_root).append(
            kind="correction",
            subject="mind:user-correction",
            choice="Do not duplicate me.",
            expected_revision=EMPTY_REVISION,
        )


def test_pinned_atomic_write_classifies_post_rename_fsync_failure_as_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    root.mkdir()
    (root / "control").mkdir()
    actual_rename = os.rename
    actual_fsync = os.fsync
    renamed = False
    failed = False

    def track_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal renamed
        actual_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if target == "value":
            renamed = True

    def fail_parent_fsync(descriptor: int) -> None:
        nonlocal failed
        if (
            renamed
            and not failed
            and os.path.samestat(os.fstat(descriptor), (root / "control").stat())
        ):
            failed = True
            raise OSError("injected directory fsync failure")
        actual_fsync(descriptor)

    monkeypatch.setattr(os, "rename", track_rename)
    monkeypatch.setattr(os, "fsync", fail_parent_fsync)

    store = atomic_module.PinnedPathRoot(root)
    try:
        with pytest.raises(atomic_module.DurablePublishError) as raised:
            store.atomic_write("control/value", b"committed")
    finally:
        store.close()

    assert raised.value.outcome is atomic_module.PublishOutcome.COMMITTED
    assert (root / "control/value").read_bytes() == b"committed"


def test_new_pinned_directory_fsyncs_its_parent_before_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pinned"
    root.mkdir()
    (root / ".gsv").mkdir()
    actual_fsync = os.fsync
    synchronized: list[tuple[int, int]] = []

    def observe_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        synchronized.append((int(metadata.st_dev), int(metadata.st_ino)))
        actual_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    store = atomic_module.PinnedPathRoot(root)
    try:
        store.ensure_directory(".gsv/control")
    finally:
        store.close()

    parent = (root / ".gsv").stat()
    created = (root / ".gsv/control").stat()
    assert (int(parent.st_dev), int(parent.st_ino)) in synchronized
    assert (int(created.st_dev), int(created.st_ino)) in synchronized


def test_directory_parent_fsync_failure_leaves_recoverable_preinitialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    parent = (queue.vault_root / ".gsv").stat()
    parent_identity = (parent.st_dev, parent.st_ino)
    actual_fsync = os.fsync
    failed = False

    def fail_new_control_parent_fsync(descriptor: int) -> None:
        nonlocal failed
        metadata = os.fstat(descriptor)
        if not failed and (metadata.st_dev, metadata.st_ino) == parent_identity:
            failed = True
            raise OSError("injected new-directory parent fsync failure")
        actual_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_new_control_parent_fsync)

    with pytest.raises(ValidationError, match="traverse pinned local storage"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="Retry after directory durability failure.",
            expected_revision=EMPTY_REVISION,
        )

    assert queue.path.parent.is_dir()
    assert not queue.path.exists()
    assert ControlQueue(queue.vault_root).snapshot().revision == EMPTY_REVISION

    appended = ControlQueue(queue.vault_root).append(
        kind="correction",
        subject="mind:user-correction",
        choice="Retry after directory durability failure.",
        expected_revision=EMPTY_REVISION,
    )
    assert [event.choice for event in appended.events] == [
        "Retry after directory durability failure."
    ]


def test_parser_rejects_a_stored_queue_beyond_the_event_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="First.",
        expected_revision=EMPTY_REVISION,
    )
    lines = queue.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    second = json.loads(lines[1])
    second["event_id"] = "00000000-0000-4000-8000-000000000002"
    event_lines = "".join(
        json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
        for item in (json.loads(lines[1]), second)
    )
    header["event_count"] = 2
    header["events_digest"] = sha256(event_lines.encode()).hexdigest()
    queue.path.write_text(
        json.dumps(header, separators=(",", ":"), sort_keys=True) + "\n" + event_lines,
        encoding="utf-8",
    )
    monkeypatch.setattr(control_queue_module, "MAX_EVENTS", 1)

    with pytest.raises(ValidationError, match="event count exceeds"):
        queue.snapshot()


def test_symlink_queue_is_never_followed(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    queue.path.parent.mkdir(parents=True, exist_ok=True)
    queue.path.symlink_to(outside)

    with pytest.raises(ValidationError, match="regular file, not a link"):
        queue.snapshot()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_symlinked_control_parent_cannot_escape_vault(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    Vault(root).initialize(name="Control parent")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_mode = outside.stat().st_mode & 0o777
    control = root / ".gsv/control"
    control.symlink_to(outside, target_is_directory=True)
    queue = ControlQueue(root)

    with pytest.raises(ValidationError, match="pinned local storage"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="Keep Friday evening free.",
            expected_revision=EMPTY_REVISION,
        )

    assert list(outside.iterdir()) == []
    assert outside.stat().st_mode & 0o777 == outside_mode


def test_replaceable_lock_namespace_cannot_split_the_pinned_vault_lock(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    locks = queue.vault_root / ".gsv/locks"
    parked = queue.vault_root / ".gsv/locks-parked"
    outside = tmp_path / "outside-locks"
    outside.mkdir()
    locks.rename(parked)
    locks.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValidationError, match="pinned local storage"):
            queue.append(
                kind="correction",
                subject="mind:user-correction",
                choice="Pinned lock stays with this vault.",
                expected_revision=EMPTY_REVISION,
            )
    finally:
        locks.unlink()
        parked.rename(locks)

    assert list(outside.iterdir()) == []
    assert queue.snapshot().revision == EMPTY_REVISION


def test_queue_read_detects_leaf_swap_after_stable_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Keep Friday evening free.",
        expected_revision=EMPTY_REVISION,
    )
    displaced = queue.path.with_name("queue.displaced")
    queue_identity = (queue.path.stat().st_dev, queue.path.stat().st_ino)
    actual_read = os.read
    raced = False

    def raced_read(descriptor: int, size: int) -> bytes:
        nonlocal raced
        block = actual_read(descriptor, size)
        opened = os.fstat(descriptor)
        if block and not raced and (opened.st_dev, opened.st_ino) == queue_identity:
            raced = True
            queue.path.replace(displaced)
            queue.path.write_bytes(block)
        return block

    monkeypatch.setattr(os, "read", raced_read)

    with pytest.raises(ValidationError, match="changed while it was read"):
        queue.snapshot()


def test_parent_swap_during_publication_stays_on_the_pinned_vault_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    control = queue.path.parent
    parked = control.with_name("control-parked")
    outside = tmp_path / "outside"
    outside.mkdir()
    actual_rename = os.rename
    raced = False

    def raced_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal raced
        if target == "queue.jsonl" and src_dir_fd is not None and not raced:
            raced = True
            actual_rename(control, parked)
            control.symlink_to(outside, target_is_directory=True)
            try:
                actual_rename(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
            finally:
                control.unlink()
                actual_rename(parked, control)
            return
        actual_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", raced_rename)

    appended = queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Keep Friday evening free.",
        expected_revision=EMPTY_REVISION,
    )

    assert raced is True
    assert len(appended.events) == 1
    assert [event.choice for event in ControlQueue(queue.vault_root).snapshot().events] == [
        "Keep Friday evening free."
    ]
    assert list(outside.iterdir()) == []


def test_permanent_parent_swap_during_publication_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    control = queue.path.parent
    parked = control.with_name("control-parked")
    actual_rename = os.rename
    raced = False

    def raced_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal raced
        if target == "queue.jsonl" and src_dir_fd is not None and not raced:
            raced = True
            actual_rename(control, parked)
            control.mkdir(mode=0o700)
            (control / "initialized").write_bytes(b'{"schema_version":1,"state":"initialized"}\n')
        actual_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", raced_rename)

    with pytest.raises(DegradedIntegrityError, match="unknown visible state"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="Never report a displaced write as successful.",
            expected_revision=EMPTY_REVISION,
        )

    assert raced is True
    assert not queue.path.exists()
    assert (parked / "queue.jsonl").exists()
    with pytest.raises(ValidationError, match="disappeared after initialization"):
        ControlQueue(queue.vault_root).snapshot()


def test_in_place_publication_corruption_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    actual_rename = os.rename
    corrupted = False

    def corrupt_after_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal corrupted
        actual_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if target == "queue.jsonl" and not corrupted:
            corrupted = True
            descriptor = os.open(queue.path, os.O_WRONLY)
            try:
                os.write(descriptor, b"[")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    monkeypatch.setattr(os, "rename", corrupt_after_rename)

    with pytest.raises(DegradedIntegrityError, match="unknown visible state"):
        queue.append(
            kind="correction",
            subject="mind:user-correction",
            choice="Never report corrupted bytes as successful.",
            expected_revision=EMPTY_REVISION,
        )

    assert corrupted is True
    with pytest.raises(ValidationError, match="invalid JSON"):
        ControlQueue(queue.vault_root).snapshot()


def test_parent_swap_between_queue_check_and_cas_preserves_newer_canonical_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    first = queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="First state.",
        expected_revision=EMPTY_REVISION,
    )
    stale_bytes = queue.path.read_bytes()
    newer = queue.append(
        kind="correction",
        subject="record:direction.current",
        choice="Newer canonical state.",
        expected_revision=first.revision,
    )
    newer_bytes = queue.path.read_bytes()
    queue.path.write_bytes(stale_bytes)
    control = queue.path.parent
    parked = control.with_name("control-parked-after-cas-check")
    actual_cas = atomic_module.PinnedPathRoot.compare_and_swap_regular_file
    swapped = False

    def swap_before_cas_write(
        store: atomic_module.PinnedPathRoot,
        relative: Path | str,
        *,
        expected: bytes | None,
        replacement: bytes,
        label: str,
        max_bytes: int,
        mode: int = 0o600,
    ) -> None:
        nonlocal swapped
        if str(relative) == ".gsv/control/queue.jsonl" and not swapped:
            control.rename(parked)
            control.mkdir()
            (control / "initialized").write_bytes(control_queue_module._MARKER_INITIALIZED)
            (control / "queue.jsonl").write_bytes(newer_bytes)
            swapped = True
        actual_cas(
            store,
            relative,
            expected=expected,
            replacement=replacement,
            label=label,
            max_bytes=max_bytes,
            mode=mode,
        )

    monkeypatch.setattr(
        atomic_module.PinnedPathRoot,
        "compare_and_swap_regular_file",
        swap_before_cas_write,
    )

    with pytest.raises(DegradedIntegrityError, match="unknown visible state"):
        queue.append(
            kind="correction",
            subject="record:direction.current",
            choice="Must not replace the newer queue.",
            expected_revision=first.revision,
        )

    monkeypatch.undo()
    assert swapped is True
    assert queue.path.read_bytes() == newer_bytes
    assert ControlQueue(queue.vault_root).snapshot() == newer
    assert (parked / "queue.jsonl").read_bytes() == stale_bytes


def test_transient_parent_swap_during_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    queue.append(
        kind="correction",
        subject="mind:user-correction",
        choice="Inside the vault.",
        expected_revision=EMPTY_REVISION,
    )
    control = queue.path.parent
    parked = control.with_name("control-parked")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "queue.jsonl").write_bytes(queue.path.read_bytes())
    actual_read = atomic_module.PinnedPathRoot.read_regular_file

    def raced_read(store: Any, relative: Any, **kwargs: Any) -> bytes | None:
        control.rename(parked)
        control.symlink_to(outside, target_is_directory=True)
        try:
            return actual_read(store, relative, **kwargs)
        finally:
            control.unlink()
            parked.rename(control)

    monkeypatch.setattr(atomic_module.PinnedPathRoot, "read_regular_file", raced_read)

    with pytest.raises(ValidationError, match="pinned local storage"):
        queue.snapshot()

    monkeypatch.undo()
    assert [event.choice for event in queue.snapshot().events] == ["Inside the vault."]
