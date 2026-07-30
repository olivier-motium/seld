from __future__ import annotations

import os
from pathlib import Path

import pytest

import continuity_kernel.vault as vault_module
from continuity_kernel import atomic
from continuity_kernel.atomic import portable_relative
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    PersistenceError,
    ValidationError,
)
from continuity_kernel.resident_signals import ResidentSignalStore
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    not atomic.PINNED_PATH_ROOT_SUPPORTED,
    reason="directory-pinned mutation proof requires POSIX dir-fd support",
)


def test_root_binding_and_detached_rollback_never_touch_substituted_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "MIND.md").write_bytes(b"old\n")
    store = atomic.PinnedPathRoot(root)
    parked = tmp_path / "parked-vault"
    foreign = tmp_path / "foreign-vault"
    try:
        with (
            pytest.raises(ValidationError, match="canonical path"),
            store.bind_directory("."),
        ):
            store.replace_regular_file_if_exact(
                "MIND.md",
                expected=b"old\n",
                replacement=b"new\n",
                label="mind",
                max_bytes=1024,
            )
            root.rename(parked)
            foreign.mkdir()
            (foreign / "MIND.md").write_bytes(b"new\n")
            root.symlink_to(foreign, target_is_directory=True)
            store.rollback_regular_file_if_exact(
                "MIND.md",
                expected=b"new\n",
                replacement=b"old\n",
                label="mind",
                max_bytes=1024,
            )
    finally:
        store.close()

    assert (parked / "MIND.md").read_bytes() == b"old\n"
    assert (foreign / "MIND.md").read_bytes() == b"new\n"


def test_exact_replace_restores_old_bytes_when_parent_changes_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    records = root / "tasks"
    records.mkdir(parents=True)
    (records / "item.md").write_bytes(b"old\n")
    parked = tmp_path / "parked-tasks"
    foreign = tmp_path / "foreign-tasks"
    foreign.mkdir()
    (foreign / "item.md").write_bytes(b"foreign\n")
    actual_verify = atomic._verify_regular_file_at
    swapped = False

    def verify_then_swap(*args: object, **kwargs: object) -> None:
        nonlocal swapped
        actual_verify(*args, **kwargs)  # type: ignore[arg-type]
        if not swapped and args[1] == "item.md":
            swapped = True
            records.rename(parked)
            records.symlink_to(foreign, target_is_directory=True)

    monkeypatch.setattr(atomic, "_verify_regular_file_at", verify_then_swap)
    store = atomic.PinnedPathRoot(root)
    try:
        with (
            pytest.raises(atomic.DurablePublishError) as raised,
            store.bind_directory("tasks"),
        ):
            store.replace_regular_file_if_exact(
                "tasks/item.md",
                expected=b"old\n",
                replacement=b"new\n",
                label="item",
                max_bytes=1024,
            )
    finally:
        store.close()

    assert raised.value.outcome is atomic.PublishOutcome.UNPUBLISHED
    assert (parked / "item.md").read_bytes() == b"old\n"
    assert (foreign / "item.md").read_bytes() == b"foreign\n"


def test_exact_replace_keeps_rollback_bytes_until_published_identity_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned late retain")
    task = vault.create_task(
        identifier="late-retain",
        title="Late retain",
        outcome="The exact prior bytes remain rollbackable through final identity retention.",
    )
    tasks = vault.root / "tasks"
    canonical = tasks / "late-retain.md"
    original = canonical.read_bytes()
    parked = tmp_path / "parked-late-retain"
    actual_retain = atomic.PinnedPathRoot._retain_named_regular_file
    swapped = False

    def substitute_before_retain(
        store: atomic.PinnedPathRoot,
        parts: tuple[str, ...],
        parent: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        label: str,
    ) -> None:
        nonlocal swapped
        if not swapped and parts == ("tasks", "late-retain.md"):
            swapped = True
            tasks.rename(parked)
            tasks.mkdir()
            (tasks / name).write_bytes(b"foreign\n")
        actual_retain(
            store,
            parts,
            parent,
            name,
            expected_identity=expected_identity,
            label=label,
        )

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "_retain_named_regular_file",
        substitute_before_retain,
    )
    with pytest.raises(PersistenceError, match="was not committed"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            title="Rejected late retain",
        )

    assert swapped
    assert (parked / canonical.name).read_bytes() == original
    assert canonical.read_bytes() == b"foreign\n"


def test_exact_replace_restores_old_bytes_on_synchronous_post_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    records = root / "tasks"
    records.mkdir(parents=True)
    path = records / "item.md"
    path.write_bytes(b"old\n")
    actual_verify = atomic._verify_regular_file_at

    def fail_verification(*args: object, **kwargs: object) -> None:
        if kwargs.get("expected") == b"new\n":
            raise OSError("injected verification failure")
        actual_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(atomic, "_verify_regular_file_at", fail_verification)
    store = atomic.PinnedPathRoot(root)
    try:
        with (
            pytest.raises(atomic.DurablePublishError) as raised,
            store.bind_directory("tasks"),
        ):
            store.replace_regular_file_if_exact(
                "tasks/item.md",
                expected=b"old\n",
                replacement=b"new\n",
                label="item",
                max_bytes=1024,
            )
    finally:
        store.close()

    assert raised.value.outcome is atomic.PublishOutcome.UNPUBLISHED
    assert path.read_bytes() == b"old\n"


def test_pinned_append_rolls_back_original_when_journal_parent_is_substituted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    journal = root / "journal"
    journal.mkdir(parents=True)
    (journal / "events.jsonl").write_bytes(b"before\n")
    parked = tmp_path / "parked-journal"
    foreign = tmp_path / "foreign-journal"
    foreign.mkdir()
    (foreign / "events.jsonl").write_bytes(b"foreign\n")
    actual_write = atomic._write_all
    swapped = False

    def write_then_swap(descriptor: int, content: bytes) -> None:
        nonlocal swapped
        actual_write(descriptor, content)
        if not swapped:
            swapped = True
            journal.rename(parked)
            journal.symlink_to(foreign, target_is_directory=True)

    monkeypatch.setattr(atomic, "_write_all", write_then_swap)
    store = atomic.PinnedPathRoot(root)
    try:
        with pytest.raises(atomic.DurableAppendError) as raised:
            store.append_durable(
                "journal/events.jsonl",
                b"addition\n",
                label="journal",
            )
    finally:
        store.close()

    assert raised.value.outcome is atomic.AppendOutcome.RESTORED
    assert (parked / "events.jsonl").read_bytes() == b"before\n"
    assert (foreign / "events.jsonl").read_bytes() == b"foreign\n"


@pytest.mark.parametrize(
    "relative",
    (
        "tasks/pinned.md",
        "entities/pinned.md",
        "threads/pinned.md",
        "MIND.md",
        "DIRECTION.md",
        "PORTFOLIO.md",
        "SOURCES.md",
    ),
)
def test_each_canonical_file_category_rejects_leaf_or_ancestor_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned category matrix")
    path = vault.root / relative
    previous = path.read_bytes() if path.exists() else None
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_before: dict[str, bytes] = {}
    actual_replace = atomic.PinnedPathRoot.replace_regular_file_if_exact
    swapped = False

    def substitute_then_replace(
        store: atomic.PinnedPathRoot,
        target: Path | str,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        target_relative = Path(target)
        if not swapped and target_relative.as_posix() == relative:
            swapped = True
            if len(target_relative.parts) > 1:
                parent = path.parent
                parked = tmp_path / f"parked-{parent.name}"
                parent.rename(parked)
                foreign_parent = outside / parent.name
                foreign_parent.mkdir()
                parent.symlink_to(foreign_parent, target_is_directory=True)
            else:
                parked = tmp_path / f"parked-{path.name}"
                if path.exists():
                    path.rename(parked)
                foreign_leaf = outside / path.name
                foreign_bytes = previous if previous is not None else b"foreign\n"
                foreign_leaf.write_bytes(foreign_bytes)
                outside_before[path.name] = foreign_bytes
                path.symlink_to(foreign_leaf)
        actual_replace(store, target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "replace_regular_file_if_exact",
        substitute_then_replace,
    )
    with pytest.raises((ConflictError, PersistenceError, ValidationError)):
        vault._persist_with_event(
            path=path,
            content=b"replacement\n",
            previous=previous,
            operation="test.pinned-category",
            identifier=relative,
            before_revision=None,
            after_revision="a" * 64,
        )

    assert swapped
    if len(Path(relative).parts) > 1:
        assert not any((outside / path.parent.name).iterdir())
    else:
        assert (outside / path.name).read_bytes() == outside_before[path.name]


def test_absent_record_publication_rejects_real_parent_rename_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned real parent")
    tasks = vault.root / "tasks"
    parked = tmp_path / "parked-tasks"
    actual_replace = atomic.PinnedPathRoot.replace_regular_file_if_exact
    swapped = False

    def swap_then_replace(
        store: atomic.PinnedPathRoot,
        relative: Path | str,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if not swapped and Path(relative).as_posix() == "tasks/real-parent.md":
            swapped = True
            tasks.rename(parked)
            tasks.mkdir()
        actual_replace(store, relative, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "replace_regular_file_if_exact",
        swap_then_replace,
    )
    with pytest.raises(PersistenceError, match="was not committed"):
        vault.create_task(
            identifier="real-parent",
            title="Real parent",
            outcome="An absent planted parent cannot receive this record.",
        )

    assert swapped
    assert not (parked / "real-parent.md").exists()
    assert not any(tasks.iterdir())


def test_audit_failure_rolls_back_only_the_original_pinned_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned audit rollback")
    parked = tmp_path / "parked-tasks"
    foreign = tmp_path / "foreign-tasks"

    def substitute_then_fail(
        store: atomic.PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        label: str,
    ) -> None:
        tasks = vault.root / "tasks"
        tasks.rename(parked)
        foreign.mkdir()
        planted = foreign / "audit-rollback.md"
        planted.write_bytes((parked / "audit-rollback.md").read_bytes())
        tasks.symlink_to(foreign, target_is_directory=True)
        raise atomic.DurableAppendError(
            "injected restored audit failure",
            outcome=atomic.AppendOutcome.RESTORED,
        )

    monkeypatch.setattr(atomic.PinnedPathRoot, "append_durable", substitute_then_fail)
    with pytest.raises(DegradedIntegrityError, match="foreign tree was left untouched"):
        vault.create_task(
            identifier="audit-rollback",
            title="Audit rollback",
            outcome="Only the original pinned tree may be rolled back.",
        )

    assert not (parked / "audit-rollback.md").exists()
    assert (foreign / "audit-rollback.md").exists()


def test_post_audit_parent_substitution_reports_committed_and_preserves_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned post-audit outcome")
    tasks = vault.root / "tasks"
    parked = tmp_path / "parked-tasks"
    actual_append = atomic.PinnedPathRoot.append_durable

    def append_then_substitute(
        store: atomic.PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        label: str,
    ) -> None:
        actual_append(store, relative, content, label=label)
        tasks.rename(parked)
        tasks.mkdir()

    monkeypatch.setattr(atomic.PinnedPathRoot, "append_durable", append_then_substitute)
    with pytest.raises(MutationCommittedError, match="were committed"):
        vault.create_task(
            identifier="post-audit",
            title="Post-audit substitution",
            outcome="The outcome remains explicitly committed.",
        )

    assert (parked / "post-audit.md").exists()
    assert not any(tasks.iterdir())
    assert b'"identifier":"post-audit"' in (vault.root / "journal/events.jsonl").read_bytes()


def test_post_commit_lock_parent_substitution_preserves_committed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned committed lock teardown")
    task = vault.create_task(
        identifier="committed-lock-teardown",
        title="Before lock teardown",
        outcome="Committed task and journal bytes remain a committed public outcome.",
    )
    locks = vault.root / ".gsv/locks"
    parked = tmp_path / "parked-locks-after-commit"
    actual_persist = Vault._persist_with_event
    swapped = False

    def persist_then_substitute(self: Vault, **kwargs: object) -> None:
        nonlocal swapped
        actual_persist(self, **kwargs)  # type: ignore[arg-type]
        if not swapped:
            swapped = True
            locks.rename(parked)
            locks.mkdir()

    monkeypatch.setattr(Vault, "_persist_with_event", persist_then_substitute)
    with pytest.raises(MutationCommittedError, match="were committed"):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            title="After lock teardown",
        )

    assert swapped
    assert vault.get_task(task.identifier).title == "After lock teardown"
    journal = (vault.root / "journal/events.jsonl").read_bytes()
    assert b'"operation":"task.update"' in journal
    assert not any(locks.iterdir())


def test_symlinked_journal_leaf_gets_zero_bytes_and_canonical_create_rolls_back(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned journal leaf")
    journal = vault.root / "journal/events.jsonl"
    outside = tmp_path / "outside-events.jsonl"
    outside.write_bytes(b"outside\n")
    journal.unlink()
    journal.symlink_to(outside)

    with pytest.raises(PersistenceError, match="was not committed"):
        vault.create_task(
            identifier="journal-symlink",
            title="Journal symlink",
            outcome="The outside journal gets no bytes.",
        )

    assert outside.read_bytes() == b"outside\n"
    assert not (vault.root / "tasks/journal-symlink.md").exists()


@pytest.mark.parametrize("substitution", ("parent", "leaf"))
def test_journal_identity_pinned_before_real_rename_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned journal identity")
    journal = vault.root / "journal"
    events = journal / "events.jsonl"
    original = events.read_bytes()
    parked = tmp_path / "parked-journal"
    actual_append = atomic.PinnedPathRoot.append_durable
    foreign_events: Path

    def substitute_then_append(
        store: atomic.PinnedPathRoot,
        relative: Path | str,
        content: bytes,
        *,
        label: str,
    ) -> None:
        nonlocal foreign_events
        if substitution == "parent":
            journal.rename(parked)
            journal.mkdir()
            foreign_events = journal / "events.jsonl"
            foreign_events.write_bytes(original)
        else:
            parked.mkdir()
            events.rename(parked / events.name)
            foreign_events = events
            foreign_events.write_bytes(original)
        actual_append(store, relative, content, label=label)

    foreign_events = events
    monkeypatch.setattr(atomic.PinnedPathRoot, "append_durable", substitute_then_append)
    with pytest.raises(PersistenceError, match="prior pinned file state was restored"):
        vault.create_task(
            identifier=f"journal-real-{substitution}",
            title="Journal identity",
            outcome="A planted real file must receive no audit bytes.",
        )

    assert foreign_events.read_bytes() == original
    original_events = parked / "events.jsonl"
    assert original_events.read_bytes() == original
    assert not (vault.root / "tasks" / f"journal-real-{substitution}.md").exists()


@pytest.mark.parametrize("operation", ("status", "list", "get", "append", "ack", "compact"))
def test_signal_store_rejects_symlinked_signals_ancestor_without_outside_write(
    tmp_path: Path,
    operation: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned signals")
    store = ResidentSignalStore(vault.root)
    signal = store.append(
        kind="source-due",
        event_key="pinned-operation",
        envelope={"source": "gmail"},
    )
    revision = store.list().revision
    signals = vault.root / ".gsv/signals"
    before = {
        path.relative_to(signals): path.read_bytes()
        for path in signals.rglob("*")
        if path.is_file()
    }
    parked = tmp_path / "parked-signals"
    outside = tmp_path / "outside-signals"
    signals.rename(parked)
    outside.mkdir()
    signals.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError):
        if operation == "status":
            store.status()
        elif operation == "list":
            store.list()
        elif operation == "get":
            store.get(signal.input_id)
        elif operation == "append":
            store.append(kind="source-due", envelope={"source": "calendar"})
        elif operation == "ack":
            store.acknowledge(
                (signal.input_id,),
                expected_revision=revision,
                consumer="resident-mind",
            )
        else:
            store.compact(retain_recent=0)

    assert not any(outside.iterdir())
    assert {
        path.relative_to(parked): path.read_bytes() for path in parked.rglob("*") if path.is_file()
    } == before


def test_signal_append_rejects_signals_parent_swap_during_exact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned signal append")
    signals = vault.root / ".gsv/signals"
    parked = tmp_path / "parked-signals"
    outside = tmp_path / "outside-signals"
    actual_replace = atomic.PinnedPathRoot.replace_regular_file_if_exact
    swapped = False

    def swap_then_replace(
        store: atomic.PinnedPathRoot,
        relative: Path | str,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if not swapped and Path(relative).as_posix() == ".gsv/signals/inputs.jsonl":
            swapped = True
            signals.rename(parked)
            outside.mkdir()
            outside.rename(signals)
        actual_replace(store, relative, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "replace_regular_file_if_exact",
        swap_then_replace,
    )
    with pytest.raises(ValidationError):
        vault.append_resident_signal(kind="source-due", envelope={"source": "gmail"})

    assert swapped
    assert not any(signals.iterdir())
    assert not (parked / "inputs.jsonl").exists()


def test_signal_exchange_keeps_displaced_bytes_until_published_identity_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned signal late retain")
    signal_store = ResidentSignalStore(vault.root)
    signal_store.append(kind="source-due", envelope={"source": "one"})
    signals = vault.root / ".gsv/signals"
    inputs = signals / "inputs.jsonl"
    original = inputs.read_bytes()
    parked = tmp_path / "parked-signal-late-retain"
    actual_retain = atomic.PinnedPathRoot._retain_named_regular_file
    swapped = False

    def substitute_before_retain(
        store: atomic.PinnedPathRoot,
        parts: tuple[str, ...],
        parent: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        label: str,
    ) -> None:
        nonlocal swapped
        if not swapped and parts == (".gsv", "signals", "inputs.jsonl"):
            swapped = True
            signals.rename(parked)
            signals.mkdir()
            (signals / name).write_bytes(b"foreign\n")
        actual_retain(
            store,
            parts,
            parent,
            name,
            expected_identity=expected_identity,
            label=label,
        )

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "_retain_named_regular_file",
        substitute_before_retain,
    )
    with pytest.raises(PersistenceError, match="was not published"):
        signal_store.append(kind="source-due", envelope={"source": "two"})

    assert swapped
    assert (parked / inputs.name).read_bytes() == original
    assert inputs.read_bytes() == b"foreign\n"


def test_signal_compaction_parent_swap_preserves_recoverable_original_and_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_root = tmp_path / "vault"
    vault = Vault(vault_root)
    vault.initialize(name="Pinned signal compaction")
    signals_store = ResidentSignalStore(vault_root)
    first = signals_store.append(
        kind="source-due",
        event_key="source:one",
        envelope={"source": "one"},
    )
    signals_store.append(
        kind="source-due",
        event_key="source:two",
        envelope={"source": "two"},
    )
    signals_store.acknowledge(
        (first.input_id,),
        expected_revision=signals_store.list().revision,
        consumer="resident-mind",
    )
    signals = vault_root / ".gsv/signals"
    parked = tmp_path / "parked-signals"
    outside = tmp_path / "outside-signals"
    actual_replace = atomic.PinnedPathRoot.replace_regular_file_if_exact
    swapped = False

    def swap_before_archive(
        store: atomic.PinnedPathRoot,
        relative: Path | str,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        relative_text = Path(relative).as_posix()
        if (
            not swapped
            and relative_text.startswith(".gsv/signals/archive/inputs-")
            and (signals / "compaction.json").exists()
        ):
            swapped = True
            signals.rename(parked)
            outside.mkdir()
            signals.symlink_to(outside, target_is_directory=True)
        actual_replace(store, relative, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "replace_regular_file_if_exact",
        swap_before_archive,
    )
    with pytest.raises(ValidationError):
        signals_store.compact(retain_recent=1)

    assert swapped
    assert not any(outside.iterdir())
    assert (parked / "compaction.json").exists()

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "replace_regular_file_if_exact",
        actual_replace,
    )
    signals.unlink()
    parked.rename(signals)
    recovered = ResidentSignalStore(vault_root)
    assert recovered.status().inputs == 1
    assert not recovered.compaction_marker_path.exists()


def test_signal_store_rejects_root_symlink_substitution_without_outside_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    vault = Vault(root)
    vault.initialize(name="Pinned signal root")
    parked = tmp_path / "parked-vault"
    outside = tmp_path / "outside-vault"
    root.rename(parked)
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError):
        vault.append_resident_signal(kind="source-due", envelope={"source": "gmail"})

    assert not any(outside.iterdir())


def test_doctor_reports_but_never_deletes_quarantine_state(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Quarantine doctor")
    quarantine = vault.root / "tasks/.task.md.seld-quarantine-deadbeef"
    quarantine.write_bytes(b"only retained prior bytes\n")

    report = vault.doctor(repair=True)

    assert any(
        issue.code == "orphan-storage-transaction" and issue.path.endswith(quarantine.name)
        for issue in report.issues
    )
    assert quarantine.read_bytes() == b"only retained prior bytes\n"


def test_authoritative_leaf_identity_rejects_identical_regular_file_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned leaf identity")
    task = vault.create_task(
        identifier="leaf-identity",
        title="Leaf identity",
        outcome="An identical foreign inode is never adopted by the CAS.",
    )
    canonical = vault.root / "tasks/leaf-identity.md"
    parked = tmp_path / "parked-leaf-identity.md"
    original = canonical.read_bytes()
    actual_persist = Vault._persist_with_event
    substituted = False

    def substitute_before_persist(self: Vault, **kwargs: object) -> None:
        nonlocal substituted
        if not substituted and kwargs.get("path") == canonical:
            substituted = True
            canonical.rename(parked)
            canonical.write_bytes(original)
        actual_persist(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Vault, "_persist_with_event", substitute_before_persist)
    with pytest.raises((ConflictError, ValidationError)):
        vault.update_task(
            task.identifier,
            expected_revision=task.revision,
            title="Rejected replacement",
        )

    assert substituted
    assert parked.read_bytes() == original
    assert canonical.read_bytes() == original
    assert canonical.stat().st_ino != parked.stat().st_ino


def test_create_pins_absent_record_parent_before_persist_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned absent parent")
    tasks = vault.root / "tasks"
    parked = tmp_path / "parked-authoritative-tasks"
    actual_persist = Vault._persist_with_event
    substituted = False

    def substitute_before_persist(self: Vault, **kwargs: object) -> None:
        nonlocal substituted
        if not substituted:
            substituted = True
            tasks.rename(parked)
            tasks.mkdir()
        actual_persist(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Vault, "_persist_with_event", substitute_before_persist)
    with pytest.raises((PersistenceError, ValidationError)):
        vault.create_task(
            identifier="parent-before-persist",
            title="Parent before persist",
            outcome="The parent pinned at the absence check remains authoritative.",
        )

    assert substituted
    assert not any(parked.iterdir())
    assert not any(tasks.iterdir())


def test_exact_create_rolls_back_when_move_reports_post_rename_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    (root / "tasks").mkdir(parents=True)
    actual_move = atomic._move_no_replace_at
    injected = False

    def move_then_fail(*args: object, **kwargs: object) -> None:
        nonlocal injected
        actual_move(*args, **kwargs)  # type: ignore[arg-type]
        if not injected:
            injected = True
            raise OSError("injected directory fsync failure after rename")

    monkeypatch.setattr(atomic, "_move_no_replace_at", move_then_fail)
    store = atomic.PinnedPathRoot(root)
    try:
        with (
            store.bind_directory("tasks"),
            pytest.raises(atomic.DurablePublishError) as raised,
        ):
            store.replace_regular_file_if_exact(
                "tasks/post-rename.md",
                expected=None,
                replacement=b"new\n",
                label="post-rename",
                max_bytes=1024,
            )
    finally:
        store.close()

    assert injected
    assert raised.value.outcome is atomic.PublishOutcome.UNPUBLISHED
    assert not (root / "tasks/post-rename.md").exists()


@pytest.mark.parametrize("substitution", ("leaf", "parent"))
def test_doctor_repair_never_truncates_symlinked_journal(
    tmp_path: Path,
    substitution: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned doctor journal")
    journal = vault.root / "journal"
    events = journal / "events.jsonl"
    outside = tmp_path / "outside-journal"
    outside.mkdir()
    outside_events = outside / "events.jsonl"
    outside_events.write_bytes(b'{"foreign-torn":')

    if substitution == "leaf":
        events.unlink()
        events.symlink_to(outside_events)
    else:
        parked = tmp_path / "parked-journal"
        journal.rename(parked)
        journal.symlink_to(outside, target_is_directory=True)

    result = vault.doctor(repair=True)

    assert not result.healthy
    assert any(issue.code == "invalid-journal" for issue in result.issues)
    assert outside_events.read_bytes() == b'{"foreign-torn":'
    assert "journal/events.jsonl" not in result.repaired


def test_vault_lock_refuses_linked_gsv_before_writing_lock_bytes(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned vault locks")
    state = vault.root / ".gsv"
    parked = tmp_path / "parked-gsv"
    outside = tmp_path / "outside-gsv"
    (outside / "locks").mkdir(parents=True)
    state.rename(parked)
    state.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError):
        vault.create_task(
            identifier="linked-lock",
            title="Linked lock",
            outcome="No outside lock byte is created.",
        )

    assert not any((outside / "locks").iterdir())
    assert not (vault.root / "tasks/linked-lock.md").exists()


def test_signal_lock_refuses_linked_lock_directory_without_outside_write(
    tmp_path: Path,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned signal locks")
    locks = vault.root / ".gsv/locks"
    parked = tmp_path / "parked-locks"
    outside = tmp_path / "outside-locks"
    outside.mkdir()
    locks.rename(parked)
    locks.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError):
        ResidentSignalStore(vault.root).append(
            kind="source-due",
            envelope={"source": "gmail"},
        )

    assert not any(outside.iterdir())
    assert not (vault.root / ".gsv/signals/inputs.jsonl").exists()


def test_doctor_orphan_report_only_preserves_scan_time_parent_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned orphan cleanup")
    tasks = vault.root / "tasks"
    orphan = tasks / ".item.md.tmp-orphan"
    orphan.write_bytes(b"same orphan bytes")
    parked = tmp_path / "parked-orphan-tasks"
    outside = tmp_path / "outside-orphan-tasks"
    outside.mkdir()
    outside_orphan = outside / orphan.name
    outside_orphan.write_bytes(orphan.read_bytes())
    actual_portable_relative = portable_relative
    substituted = False

    def swap_after_scan(path: Path, root: Path) -> str:
        nonlocal substituted
        if not substituted and path == orphan:
            substituted = True
            tasks.rename(parked)
            outside.rename(tasks)
        return actual_portable_relative(path, root)

    monkeypatch.setattr(vault_module, "portable_relative", swap_after_scan)
    result = vault.doctor(repair=True)

    assert substituted
    assert not result.healthy
    issue = next(issue for issue in result.issues if issue.path.endswith(orphan.name))
    assert issue.code == "orphan-temp"
    assert not issue.repairable
    assert not result.repaired
    assert (tasks / orphan.name).read_bytes() == b"same orphan bytes"
    assert (parked / orphan.name).read_bytes() == b"same orphan bytes"


def test_pinned_append_never_recloses_descriptor_after_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    journal = root / "journal"
    journal.mkdir(parents=True)
    (journal / "events.jsonl").write_bytes(b"before\n")
    store = atomic.PinnedPathRoot(root)
    actual_close = os.close
    actual_write = atomic._write_all
    actual_validate = atomic.PinnedPathRoot._validate_directory_path
    failed_descriptor: int | None = None
    close_calls: list[int] = []
    armed = False
    appended = False

    def arm_after_append(descriptor: int, content: bytes) -> None:
        nonlocal appended
        actual_write(descriptor, content)
        if content == b"after\n":
            appended = True

    def arm_after_final_validation(
        pinned: atomic.PinnedPathRoot,
        parts: tuple[str, ...],
        descriptor: int,
    ) -> None:
        nonlocal armed
        actual_validate(pinned, parts, descriptor)
        if appended:
            armed = True

    def close_then_report_error(descriptor: int) -> None:
        nonlocal failed_descriptor
        close_calls.append(descriptor)
        if armed and failed_descriptor is None:
            failed_descriptor = descriptor
            close_calls.clear()
            close_calls.append(descriptor)
            actual_close(descriptor)
            raise OSError("injected close report after descriptor consumption")
        actual_close(descriptor)

    monkeypatch.setattr(atomic, "_write_all", arm_after_append)
    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "_validate_directory_path",
        arm_after_final_validation,
    )
    monkeypatch.setattr(os, "close", close_then_report_error)
    try:
        with pytest.raises(atomic.DurableAppendError) as raised:
            store.append_durable("journal/events.jsonl", b"after\n", label="journal")
    finally:
        monkeypatch.setattr(os, "close", actual_close)
        store.close()

    assert raised.value.outcome is atomic.AppendOutcome.COMMITTED
    assert failed_descriptor is not None
    assert close_calls.count(failed_descriptor) == 1
    assert (journal / "events.jsonl").read_bytes() == b"before\nafter\n"


def test_vault_create_parent_close_error_reports_unaudited_commit_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned create close outcome")
    actual_retain = atomic.PinnedPathRoot._retain_named_regular_file
    actual_close = os.close
    armed_parent: int | None = None
    failed_parent: int | None = None
    next_close_after_failure: int | None = None

    def retain_then_arm(
        store: atomic.PinnedPathRoot,
        parts: tuple[str, ...],
        parent: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        label: str,
    ) -> None:
        nonlocal armed_parent
        actual_retain(
            store,
            parts,
            parent,
            name,
            expected_identity=expected_identity,
            label=label,
        )
        if parts == ("tasks", "close-committed.md"):
            armed_parent = parent

    def consume_parent_then_fail(descriptor: int) -> None:
        nonlocal failed_parent, next_close_after_failure
        if failed_parent is not None and next_close_after_failure is None:
            next_close_after_failure = descriptor
        if failed_parent is None and descriptor == armed_parent:
            failed_parent = descriptor
            actual_close(descriptor)
            raise OSError("injected committed parent close failure")
        actual_close(descriptor)

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "_retain_named_regular_file",
        retain_then_arm,
    )
    monkeypatch.setattr(os, "close", consume_parent_then_fail)
    try:
        with pytest.raises(DegradedIntegrityError, match="unknown or unaudited"):
            vault.create_task(
                identifier="close-committed",
                title="Close committed",
                outcome="A visible but unaudited record is never reported unpublished.",
            )
    finally:
        monkeypatch.setattr(os, "close", actual_close)

    assert failed_parent is not None
    assert next_close_after_failure != failed_parent
    assert (vault.root / "tasks/close-committed.md").exists()
    assert (
        b'"identifier":"close-committed"' not in (vault.root / "journal/events.jsonl").read_bytes()
    )


def test_signal_exchange_parent_close_error_reports_committed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Pinned signal close outcome")
    signal_store = ResidentSignalStore(vault.root)
    signal_store.append(kind="source-due", envelope={"source": "one"})
    actual_retain = atomic.PinnedPathRoot._retain_named_regular_file
    actual_close = os.close
    armed_parent: int | None = None
    failed_parent: int | None = None
    next_close_after_failure: int | None = None

    def retain_then_arm(
        store: atomic.PinnedPathRoot,
        parts: tuple[str, ...],
        parent: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        label: str,
    ) -> None:
        nonlocal armed_parent
        actual_retain(
            store,
            parts,
            parent,
            name,
            expected_identity=expected_identity,
            label=label,
        )
        if parts == (".gsv", "signals", "inputs.jsonl"):
            armed_parent = parent

    def consume_parent_then_fail(descriptor: int) -> None:
        nonlocal failed_parent, next_close_after_failure
        if failed_parent is not None and next_close_after_failure is None:
            next_close_after_failure = descriptor
        if failed_parent is None and descriptor == armed_parent:
            failed_parent = descriptor
            actual_close(descriptor)
            raise OSError("injected signal parent close failure")
        actual_close(descriptor)

    monkeypatch.setattr(
        atomic.PinnedPathRoot,
        "_retain_named_regular_file",
        retain_then_arm,
    )
    monkeypatch.setattr(os, "close", consume_parent_then_fail)
    try:
        with pytest.raises(MutationCommittedError, match="committed"):
            signal_store.append(kind="source-due", envelope={"source": "two"})
    finally:
        monkeypatch.setattr(os, "close", actual_close)
        monkeypatch.setattr(
            atomic.PinnedPathRoot,
            "_retain_named_regular_file",
            actual_retain,
        )

    assert failed_parent is not None
    assert next_close_after_failure != failed_parent
    assert signal_store.status().inputs == 2


def test_exact_unlink_parent_close_error_reports_committed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    records = root / "records"
    records.mkdir(parents=True)
    target = records / "value"
    target.write_bytes(b"owned\n")
    store = atomic.PinnedPathRoot(root)
    actual_unlink = atomic._unlink_private_if_identity_at
    actual_close = os.close
    armed_parent: int | None = None
    failed_parent: int | None = None
    next_close_after_failure: int | None = None

    def unlink_then_arm(
        parent: int,
        name: str,
        identity: tuple[int, int],
        *,
        label: str,
        missing_ok: bool = False,
    ) -> None:
        nonlocal armed_parent
        actual_unlink(
            parent,
            name,
            identity,
            label=label,
            missing_ok=missing_ok,
        )
        if "seld-quarantine" in name:
            armed_parent = parent

    def consume_parent_then_fail(descriptor: int) -> None:
        nonlocal failed_parent, next_close_after_failure
        if failed_parent is not None and next_close_after_failure is None:
            next_close_after_failure = descriptor
        if failed_parent is None and descriptor == armed_parent:
            failed_parent = descriptor
            actual_close(descriptor)
            raise OSError("injected unlink parent close failure")
        actual_close(descriptor)

    monkeypatch.setattr(atomic, "_unlink_private_if_identity_at", unlink_then_arm)
    monkeypatch.setattr(os, "close", consume_parent_then_fail)
    try:
        with (
            pytest.raises(atomic.DurablePublishError) as raised,
            store.bind_directory("records"),
        ):
            store.unlink_regular_file_if_exact(
                "records/value",
                expected=b"owned\n",
                label="value",
                max_bytes=1024,
            )
    finally:
        monkeypatch.setattr(os, "close", actual_close)
        store.close()

    assert raised.value.outcome is atomic.PublishOutcome.COMMITTED
    assert failed_parent is not None
    assert next_close_after_failure != failed_parent
    assert not target.exists()


def test_private_exact_unlink_never_creates_a_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    stages = root / "stages"
    stages.mkdir(parents=True)
    target = stages / f".owned.seld-stage-{'a' * 32}"
    target.write_bytes(b"private stage\n")
    store = atomic.PinnedPathRoot(root)

    def reject_quarantine(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("private exact unlink attempted a quarantine rename")

    monkeypatch.setattr(atomic, "_move_no_replace_at", reject_quarantine)
    try:
        with store.bind_directory("stages"):
            store.unlink_private_regular_file_if_exact(
                target.relative_to(root),
                expected=b"private stage\n",
                label="private stage",
                max_bytes=1024,
            )
    finally:
        store.close()

    assert not target.exists()
    assert not tuple(stages.glob("*.seld-quarantine-*"))


def test_doctor_discards_unpublished_signal_operation(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Signal operation doctor")
    operation = vault.root / ".gsv/signals/operations" / ("a" * 32)
    operation.mkdir(parents=True)
    stage = operation / "live_inputs.jsonl"
    stage.write_bytes(b'{"retained":true}\n')

    report = vault.doctor(repair=True)

    assert report.healthy is True
    assert not operation.exists()
    assert not stage.exists()
