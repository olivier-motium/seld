from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import continuity_kernel.local_files as local_files_module
from continuity_kernel import bridge
from continuity_kernel.errors import NotFoundError, PersistenceError, ValidationError
from continuity_kernel.local_files import LOCAL_FILE_READER_TOOL, LocalFileGrantStore
from continuity_kernel.privacy import MAX_SCREEN_BYTES
from continuity_kernel.records import format_time
from continuity_kernel.source_state import ABSENT_SOURCE_REVISION
from continuity_kernel.vault import Vault, doctor_dict


def _grant_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "vault",
) -> tuple[Vault, LocalFileGrantStore]:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault = Vault(tmp_path / name)
    vault.initialize(name="Local file proof")
    return vault, LocalFileGrantStore(
        vault_root=vault.root,
        vault_id=vault.identity()["vault_id"],
    )


def _select_local_files(vault: Vault) -> dict[str, object]:
    return vault.select_sources(
        expected_revision=ABSENT_SOURCE_REVISION,
        sources=("local_files",),
    )


def _record_local_success(vault: Vault, *, expected_revision: str) -> dict[str, Any]:
    return vault.record_source_observation(
        expected_revision=expected_revision,
        source_id="local_files",
        actor_ref="codex:local-file-task",
        result="success",
        covered_through=format_time(datetime.now(UTC)),
        completeness="complete",
        tool_binding=LOCAL_FILE_READER_TOOL,
    )


@pytest.mark.skipif(os.name == "nt", reason="secure descriptor-pinned reads are POSIX-only")
def test_local_file_grant_returns_one_safe_file_transiently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    note = selected / "Notes" / "week.txt"
    note.parent.mkdir(parents=True)
    note.write_text("Monday: call Alex", encoding="utf-8")
    before = vault.logical_digest()

    granted = store.create(selected)["grant"]
    result = store.read(
        grant_id=granted["grant_id"],
        relative_path="Notes/week.txt",
    )

    assert result == {
        "capability": "local.files.bounded_read",
        "content": "Monday: call Alex",
        "decision": "content_allowed",
        "grant_id": granted["grant_id"],
        "persisted": False,
        "reason": "selected-regular-file",
        "relative_path": "Notes/week.txt",
        "screening": {
            "bytes_screened": 17,
            "decision": "content_allowed",
            "reasons": [],
        },
        "selected_root": str(selected.resolve()),
        "source": "local_files",
        "tool_binding": LOCAL_FILE_READER_TOOL,
        "transient": True,
    }
    assert vault.logical_digest() == before
    encoded = store.path.read_bytes()
    assert b"Monday: call Alex" not in encoded
    assert stat.S_IMODE(os.lstat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(store.storage_root).st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="secure descriptor-pinned reads are POSIX-only")
@pytest.mark.parametrize(
    ("relative_path", "content", "reason"),
    [
        ("notes.txt", b"password: this-is-a-real-password-value", "content-screen"),
        ("router.txt", b"router password=hunter2", "content-screen"),
        (
            "remote.txt",
            b"url = https://alice:" + b"short-pass@example.com/org/repo.git\n",
            "content-screen",
        ),
        ("large.txt", b"a" * (MAX_SCREEN_BYTES + 1), "content-screen"),
        ("binary.txt", b"\xff\xfeordinary", "non-utf8-content"),
    ],
    ids=("secret", "short-password", "credential-uri", "oversize", "non-utf8"),
)
def test_local_file_reader_withholds_quarantined_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    content: bytes,
    reason: str,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / relative_path).write_bytes(content)
    grant_id = store.create(selected)["grant"]["grant_id"]

    result = store.read(grant_id=grant_id, relative_path=relative_path)

    assert result["decision"] == "quarantine"
    assert result["reason"] == reason
    assert "content" not in result
    assert result["persisted"] is False


def test_local_file_reader_never_opens_cloud_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "remote.txt.icloud").write_bytes(b"")
    grant_id = store.create(selected)["grant"]["grant_id"]

    result = store.read(grant_id=grant_id, relative_path="remote.txt.icloud")

    assert result["decision"] == "placeholder"
    assert result["reason"] == "cloud-placeholder"
    assert "content" not in result


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_local_file_reader_rejects_symlink_ancestry_and_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    inside = selected / "inside"
    inside.mkdir(parents=True)
    (inside / "note.txt").write_text("inside", encoding="utf-8")
    (selected / "alias").symlink_to(inside, target_is_directory=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("outside", encoding="utf-8")
    (selected / "escape").symlink_to(outside, target_is_directory=True)
    grant_id = store.create(selected)["grant"]["grant_id"]

    aliased = store.read(grant_id=grant_id, relative_path="alias/note.txt")
    escaped = store.read(grant_id=grant_id, relative_path="escape/note.txt")

    assert aliased["decision"] == "exclude"
    assert aliased["reason"] == "symbolic-link-ancestry"
    assert escaped["decision"] == "exclude"
    assert escaped["reason"] == "outside-selected-root"
    assert "content" not in aliased
    assert "content" not in escaped


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory identity semantics")
def test_local_file_grant_rejects_root_replacement_even_between_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "note.txt").write_text("granted", encoding="utf-8")
    grant_id = store.create(selected)["grant"]["grant_id"]
    actual_root_matches = local_files_module._root_matches
    swapped = False

    def swap_after_check(grant: local_files_module.LocalFileGrant) -> bool:
        nonlocal swapped
        matched = actual_root_matches(grant)
        if matched and not swapped:
            swapped = True
            selected.rename(tmp_path / "former-selected")
            selected.mkdir()
            (selected / "note.txt").write_text("replacement", encoding="utf-8")
        return matched

    monkeypatch.setattr(local_files_module, "_root_matches", swap_after_check)

    with pytest.raises(ValidationError, match="no longer matches its grant"):
        store.read(grant_id=grant_id, relative_path="note.txt")


def test_local_file_grant_is_bound_to_exact_vault_identity_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_vault, first = _grant_store(tmp_path, monkeypatch, name="first-vault")
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "note.txt").write_text("granted", encoding="utf-8")
    grant_id = first.create(selected)["grant"]["grant_id"]

    second_vault = Vault(tmp_path / "second-vault")
    second_vault.initialize(name="Other vault")
    second = LocalFileGrantStore(
        vault_root=second_vault.root,
        vault_id=second_vault.identity()["vault_id"],
    )
    moved_root = tmp_path / "restored-elsewhere"
    moved_root.mkdir()
    moved_same_identity = LocalFileGrantStore(
        vault_root=moved_root,
        vault_id=first_vault.identity()["vault_id"],
    )

    with pytest.raises(NotFoundError, match="not found for this vault"):
        second.read(grant_id=grant_id, relative_path="note.txt")
    with pytest.raises(NotFoundError, match="not found for this vault"):
        moved_same_identity.read(grant_id=grant_id, relative_path="note.txt")


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory identity semantics")
def test_same_path_vault_restore_requires_a_new_local_file_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, _ = _grant_store(tmp_path, monkeypatch)
    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    (selected_root / "note.txt").write_text("private context", encoding="utf-8")
    selected = _select_local_files(vault)
    grant_id = vault.grant_local_file_root(selected_root)["grant"]["grant_id"]
    current = _record_local_success(vault, expected_revision=str(selected["revision"]))
    assert current["sources"][0]["freshness"] == "current"

    backup = Path(vault.create_backup(tmp_path / "same-path-restore.zip")["backup"])
    original_root = vault.root
    original_root.rename(tmp_path / "retired-vault")
    Vault.restore_backup(backup, original_root)

    restored = Vault(original_root)
    assert restored.identity()["vault_id"] == vault.identity()["vault_id"]
    assert restored.list_local_file_grants()["grants"] == []
    assert restored.source_status()["sources"][0]["freshness"] == "needs_revalidation"
    with pytest.raises(NotFoundError, match="not found for this vault"):
        restored.read_local_file(grant_id=grant_id, relative_path="note.txt")

    replacement_grant = restored.grant_local_file_root(selected_root)["grant"]["grant_id"]
    assert replacement_grant != grant_id
    assert (
        restored.read_local_file(
            grant_id=replacement_grant,
            relative_path="note.txt",
        )["content"]
        == "private context"
    )


def test_legacy_unbound_local_file_grants_are_invalidated_on_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    grant_id = store.create(selected_root)["grant"]["grant_id"]
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["format_version"] = 1
    for grant in payload["grants"]:
        grant.pop("vault_device")
        grant.pop("vault_inode")
    store.path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    store.path.chmod(0o600)

    restarted = LocalFileGrantStore(vault_root=store.vault_root, vault_id=store.vault_id)
    assert restarted.list()["grants"] == []
    with pytest.raises(NotFoundError, match="not found for this vault"):
        restarted.read(grant_id=grant_id, relative_path="note.txt")

    replacement = restarted.create(selected_root)
    assert replacement["created"] is True
    assert replacement["grant"]["grant_id"] != grant_id
    assert json.loads(store.path.read_text(encoding="utf-8"))["format_version"] == 2


def test_local_file_grant_revoke_is_durable_and_idempotent_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "note.txt").write_text("granted", encoding="utf-8")
    first = store.create(selected)
    repeated = store.create(selected)
    grant_id = first["grant"]["grant_id"]

    assert first["created"] is True
    assert repeated == {"created": False, "grant": first["grant"]}
    assert [item["grant_id"] for item in store.list()["grants"]] == [grant_id]
    assert store.revoke(grant_id)["revoked"] is True
    assert store.list()["grants"] == []
    with pytest.raises(NotFoundError, match="not found for this vault"):
        store.read(grant_id=grant_id, relative_path="note.txt")


@pytest.mark.skipif(os.name == "nt", reason="secure descriptor-pinned reads are POSIX-only")
def test_local_file_public_surface_requires_selection_and_revokes_on_membership_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, _ = _grant_store(tmp_path, monkeypatch)
    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    (selected_root / "note.txt").write_text("granted", encoding="utf-8")

    with pytest.raises(ValidationError, match="source is not selected"):
        vault.grant_local_file_root(selected_root)

    source_state = _select_local_files(vault)
    grant_id = vault.grant_local_file_root(selected_root)["grant"]["grant_id"]
    assert vault.read_local_file(grant_id=grant_id, relative_path="note.txt")["content"] == (
        "granted"
    )

    deselected = vault.select_sources(
        expected_revision=str(source_state["revision"]),
        sources=(),
    )
    assert vault.list_local_file_grants() == {
        "grants": [],
        "source_selected": False,
    }
    with pytest.raises(ValidationError, match="source is not selected"):
        vault.read_local_file(grant_id=grant_id, relative_path="note.txt")

    vault.select_sources(
        expected_revision=deselected["revision"],
        sources=("local_files",),
    )
    with pytest.raises(NotFoundError, match="not found for this vault"):
        vault.read_local_file(grant_id=grant_id, relative_path="note.txt")


def test_local_file_grant_store_revoke_all_is_scoped_to_exact_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_vault, first = _grant_store(tmp_path, monkeypatch, name="first")
    second_vault = Vault(tmp_path / "second")
    second_vault.initialize(name="Second")
    second = LocalFileGrantStore(
        vault_root=second_vault.root,
        vault_id=second_vault.identity()["vault_id"],
    )
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    first_root.mkdir()
    second_root.mkdir()
    first.create(first_root)
    second_id = second.create(second_root)["grant"]["grant_id"]

    assert first.revoke_all()["revoked"] == 1
    assert first.list()["grants"] == []
    assert [grant["grant_id"] for grant in second.list()["grants"]] == [second_id]
    assert first_vault.identity()["vault_id"] != second_vault.identity()["vault_id"]


@pytest.mark.skipif(os.name == "nt", reason="secure descriptor-pinned reads are POSIX-only")
def test_local_file_grants_fail_closed_when_source_selection_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, _ = _grant_store(tmp_path, monkeypatch)
    source_state = _select_local_files(vault)
    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    (selected_root / "note.txt").write_text("granted", encoding="utf-8")
    grant_id = vault.grant_local_file_root(selected_root)["grant"]["grant_id"]

    def fail_persistence(**_: object) -> None:
        raise PersistenceError("injected source persistence failure")

    monkeypatch.setattr(vault, "_persist_with_event", fail_persistence)
    with pytest.raises(PersistenceError, match="injected source persistence failure"):
        vault.select_sources(
            expected_revision=str(source_state["revision"]),
            sources=(),
        )

    assert vault.get_source_snapshot().selected_sources == ("local_files",)
    assert vault.list_local_file_grants()["grants"] == []
    with pytest.raises(NotFoundError, match="not found for this vault"):
        vault.read_local_file(grant_id=grant_id, relative_path="note.txt")


@pytest.mark.skipif(os.name == "nt", reason="secure descriptor-pinned reads are POSIX-only")
def test_local_file_grant_create_revoke_and_empty_set_require_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, _ = _grant_store(tmp_path, monkeypatch)
    selected = _select_local_files(vault)
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    first_root.mkdir()
    second_root.mkdir()
    first_id = vault.grant_local_file_root(first_root)["grant"]["grant_id"]

    current = _record_local_success(
        vault,
        expected_revision=str(selected["revision"]),
    )
    assert current["sources"][0]["freshness"] == "current"
    assert vault.source_status()["sources"][0]["freshness"] == "current"

    second_id = vault.grant_local_file_root(second_root)["grant"]["grant_id"]
    assert vault.source_status()["sources"][0]["freshness"] == "needs_revalidation"
    projected = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": False},
    )
    assert projected["sources"]["sources"][0]["freshness"] == "needs_revalidation"

    same_selection = vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    assert same_selection["sources"][0]["freshness"] == "needs_revalidation"
    assert same_selection["sources"] == vault.source_status()["sources"]

    failed = vault.record_source_observation(
        expected_revision=same_selection["revision"],
        source_id="local_files",
        actor_ref="codex:local-file-task",
        result="failure",
        error_code="local_path_rejected",
    )
    assert failed["sources"][0]["freshness"] == "needs_revalidation"
    assert failed["sources"] == vault.source_status()["sources"]

    refreshed = _record_local_success(
        vault,
        expected_revision=vault.get_source_snapshot().revision,
    )
    assert refreshed["sources"][0]["freshness"] == "current"
    vault.revoke_local_file_grant(second_id)
    assert vault.source_status()["sources"][0]["freshness"] == "needs_revalidation"

    _record_local_success(
        vault,
        expected_revision=vault.get_source_snapshot().revision,
    )
    vault.revoke_local_file_grant(first_id)
    status = vault.source_status()["sources"][0]
    assert status["freshness"] == "needs_revalidation"
    assert vault.list_local_file_grants()["grants"] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory identity semantics")
def test_local_file_root_replacement_requires_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, _ = _grant_store(tmp_path, monkeypatch)
    selected = _select_local_files(vault)
    root = tmp_path / "selected"
    root.mkdir()
    (root / "note.txt").write_text("original", encoding="utf-8")
    grant_id = vault.grant_local_file_root(root)["grant"]["grant_id"]
    _record_local_success(vault, expected_revision=str(selected["revision"]))
    assert vault.source_status()["sources"][0]["freshness"] == "current"

    root.rename(tmp_path / "former-selected")
    root.mkdir()
    (root / "note.txt").write_text("replacement", encoding="utf-8")

    assert vault.source_status()["sources"][0]["freshness"] == "needs_revalidation"
    assert vault.list_local_file_grants()["grants"][0]["current"] is False
    with pytest.raises(ValidationError, match="root changed"):
        vault.read_local_file(grant_id=grant_id, relative_path="note.txt")


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("/etc/passwd", "relative_path must name one path"),
        ("../etc/passwd", "relative_path cannot contain"),
    ],
)
def test_local_file_reader_requires_relative_path_beneath_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    message: str,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    selected = tmp_path / "selected"
    selected.mkdir()
    grant_id = store.create(selected)["grant"]["grant_id"]

    with pytest.raises(ValidationError, match=message):
        store.read(grant_id=grant_id, relative_path=relative_path)


def test_local_file_grant_rejects_unavailable_symlink_and_protected_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store = _grant_store(tmp_path, monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValidationError, match="stable real directory"):
        store.create(alias)
    with pytest.raises(ValidationError, match="unavailable"):
        store.create(tmp_path / "missing")
    if os.name != "nt":
        with pytest.raises(ValidationError, match="not grantable"):
            store.create(Path("/etc").resolve())


def test_local_files_source_receipt_rejects_ambient_tool_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Local file binding")
    selected = vault.select_sources(
        expected_revision=ABSENT_SOURCE_REVISION,
        sources=("local_files",),
    )
    granted_root = tmp_path / "granted"
    granted_root.mkdir()
    grant_id = vault.grant_local_file_root(granted_root)["grant"]["grant_id"]

    for invalid_binding in (None, "ambient.shell.cat", f" {LOCAL_FILE_READER_TOOL} "):
        with pytest.raises(ValidationError, match="gsv_local_file_read"):
            vault.record_source_observation(
                expected_revision=selected["revision"],
                source_id="local_files",
                actor_ref="codex:local-file-task",
                result="success",
                covered_through="2026-07-28T10:00:00Z",
                completeness="complete",
                tool_binding=invalid_binding,
            )

    recorded = _record_local_success(
        vault,
        expected_revision=str(selected["revision"]),
    )

    observation = recorded["sources"][0]["observation"]
    assert observation["result"] == "success"
    assert recorded["sources"][0]["freshness"] == "current"
    stored = (vault.root / "SOURCES.md").read_text(encoding="utf-8")
    assert LOCAL_FILE_READER_TOOL not in stored
    assert "ambient.shell.cat" not in stored
    assert str(granted_root) not in stored
    assert grant_id not in stored
