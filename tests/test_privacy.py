from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

import continuity_kernel.privacy as privacy_module
from continuity_kernel.errors import ValidationError
from continuity_kernel.privacy import (
    AwarenessDecision,
    assess_local_path,
    read_screened_local_content,
    screen_local_content,
)


@pytest.mark.parametrize(
    "relative",
    [
        ".ssh/id_ed25519",
        ".aws/credentials",
        "finance/wallet.kdbx",
        "project/.env",
        "project/.env.local",
        "project/.env.production",
        "project/runtime.secrets.yaml",
        "browser/Cookies",
        "notes/secrets.json",
        "notes/passwords.txt",
        "Library/Application Support/1Password/account.json",
        "Library/Application Support/Bitwarden/data.json",
        ".git-credentials",
        ".docker/config.json",
        ".config/gh/hosts.yml",
        ".codex/auth",
        ".codex/auth.json",
        ".codex/openrouter-provider-auth",
        ".openai/auth.json",
        ".openai/provider.auth.json",
        ".config/openai/auth.json",
    ],
)
def test_protected_paths_are_excluded_before_content_access(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("must never be opened", encoding="utf-8")

    result = assess_local_path(path, selected_root=tmp_path, content_requested=True)

    assert result.decision is AwarenessDecision.EXCLUDE
    assert result.reason == "protected-path"


@pytest.mark.parametrize(
    "relative_root",
    [
        ".ssh",
        "Library/Keychains",
        ".config/gcloud",
    ],
)
def test_selected_root_itself_cannot_bypass_protected_path_rules(
    tmp_path: Path,
    relative_root: str,
) -> None:
    selected = tmp_path / relative_root
    selected.mkdir(parents=True)

    result = assess_local_path(selected, selected_root=selected, content_requested=True)

    assert result.decision is AwarenessDecision.EXCLUDE
    assert result.reason == "protected-selected-root"


@pytest.mark.skipif(os.name == "nt", reason="POSIX system-root layout")
@pytest.mark.parametrize(
    "selected",
    [Path("/System"), Path("/etc"), Path("/" + "Users/not-current")],
)
def test_system_and_other_user_roots_are_structurally_excluded(selected: Path) -> None:
    result = assess_local_path(selected, selected_root=selected, content_requested=True)

    assert result.decision is AwarenessDecision.EXCLUDE
    assert result.reason == "protected-selected-root"


def test_selected_file_is_metadata_only_until_content_is_explicitly_requested(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Notes" / "week.txt"
    path.parent.mkdir()
    path.write_text("Monday: call Alex", encoding="utf-8")

    metadata = assess_local_path(path, selected_root=tmp_path)
    content = assess_local_path(path, selected_root=tmp_path, content_requested=True)

    assert metadata.decision is AwarenessDecision.METADATA_ONLY
    assert content.decision is AwarenessDecision.CONTENT_ALLOWED
    assert content.relative_path == "Notes/week.txt"


def test_safe_codex_activity_is_not_blocked_with_credential_artifacts(tmp_path: Path) -> None:
    activity = tmp_path / ".codex" / "history.jsonl"
    activity.parent.mkdir()
    activity.write_text('{"task_id":"task-safe","status":"completed"}\n', encoding="utf-8")

    result = assess_local_path(activity, selected_root=tmp_path, content_requested=True)

    assert result.decision is AwarenessDecision.CONTENT_ALLOWED
    assert result.reason == "selected-regular-file"


def test_symlink_and_outside_root_are_excluded(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = selected / "link.txt"
    link.symlink_to(outside)

    assert assess_local_path(link, selected_root=selected).reason == "symbolic-link"
    assert assess_local_path(outside, selected_root=selected).reason == "outside-selected-root"


def test_icloud_placeholder_is_not_opened(tmp_path: Path) -> None:
    placeholder = tmp_path / "remote.txt.icloud"
    placeholder.write_bytes(b"")

    result = assess_local_path(
        placeholder,
        selected_root=tmp_path,
        content_requested=True,
    )

    assert result.decision is AwarenessDecision.PLACEHOLDER
    assert result.reason == "cloud-placeholder"


def test_macos_file_provider_content_stays_metadata_only_without_residency_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_root = tmp_path / "Library/CloudStorage/OneDrive"
    provider_root.mkdir(parents=True)
    ordinary_name = provider_root / "current-plan.md"
    ordinary_name.write_text("bytes that opening could hydrate", encoding="utf-8")
    monkeypatch.setattr("continuity_kernel.privacy.sys.platform", "darwin")
    monkeypatch.setattr(
        privacy_module,
        "_MACOS_FILE_PROVIDER_ROOTS",
        (tmp_path / "Library/CloudStorage",),
    )

    result = read_screened_local_content(ordinary_name, selected_root=provider_root)

    assert result.path.decision is AwarenessDecision.METADATA_ONLY
    assert result.path.reason == "cloud-residency-unverified"
    assert result.screening is None
    assert result.content is None


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"-----BEGIN " + b"PRIVATE KEY-----\nabc", "private-key"),
        (b"token = gh" + b"p_abcdefghijklmnopqrstuvwxyz123456", "github-token"),
        (b"password: this-is-a-real-password-value", "credential-assignment"),
        (b"router password=hunter2", "credential-assignment"),
        (
            b'{"access_token": "sk-' + b'proj-this-is-a-quoted-json-credential"}',
            "credential-assignment",
        ),
        (
            b'[remote "origin"]\n url = https://alice:' + b"short-pass@example.com/org/repo.git\n",
            "credential-uri",
        ),
        (
            b"database_url=postgres://user:" + b"short-pass@db.example/app\n",
            "credential-uri",
        ),
        (
            b"Cookie: sessionid=" + b"abc123def456ghi789\n",
            "credential-header",
        ),
        (bytes(range(32)), "binary-content"),
    ],
)
def test_secret_screen_quarantines_without_returning_content(content: bytes, reason: str) -> None:
    result = screen_local_content(content)

    assert result.decision is AwarenessDecision.QUARANTINE
    assert reason in result.reasons
    assert not hasattr(result, "content")


def test_ordinary_context_is_allowed() -> None:
    result = screen_local_content(
        b"This week I need to finish the invoice review and call my dentist."
    )

    assert result.decision is AwarenessDecision.CONTENT_ALLOWED
    assert result.reasons == ()


def test_safe_quoted_json_counter_is_not_a_credential_assignment() -> None:
    result = screen_local_content(
        b'{"token_count": 123456789012345, "not_token": "ordinary-long-value"}'
    )

    assert result.decision is AwarenessDecision.CONTENT_ALLOWED
    assert result.reasons == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-pinned reads")
def test_screened_read_uses_root_pinned_component_no_follow_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "Notes" / "note.txt"
    note.parent.mkdir()
    note.write_text("Monday: call Alex", encoding="utf-8")
    calls: list[tuple[object, int, int | None]] = []
    actual_os_open = os.open

    def observed_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        calls.append((path, flags, dir_fd))
        return actual_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("continuity_kernel.privacy.os.open", observed_open)

    result = read_screened_local_content(note, selected_root=tmp_path)

    assert result.path.decision is AwarenessDecision.CONTENT_ALLOWED
    assert result.screening is not None
    assert result.screening.decision is AwarenessDecision.CONTENT_ALLOWED
    assert result.content == b"Monday: call Alex"
    assert calls[0][0] == tmp_path
    assert calls[0][2] is None
    assert calls[1][0] == "Notes"
    assert calls[1][2] is not None
    assert calls[2][0] == "note.txt"
    assert calls[2][2] is not None
    assert all(flags & cast(Any, os).O_NOFOLLOW for _, flags, _ in calls)


def test_screened_read_never_opens_a_cloud_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = tmp_path / "remote.txt.icloud"
    placeholder.write_bytes(b"")

    def forbidden_open(*_: object, **__: object) -> bytes:
        raise AssertionError("a placeholder must not be opened")

    monkeypatch.setattr(privacy_module, "_read_bounded_non_placeholder", forbidden_open)

    result = read_screened_local_content(placeholder, selected_root=tmp_path)

    assert result.path.decision is AwarenessDecision.PLACEHOLDER
    assert result.screening is None
    assert result.content is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-pinned reads")
def test_placeholder_state_change_before_stable_open_still_prevents_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "remote.txt"
    candidate.write_bytes(b"not locally resident")
    checks = 0

    def changing_placeholder_state(path: Path, metadata: os.stat_result) -> bool:
        nonlocal checks
        del path, metadata
        checks += 1
        return checks >= 3

    actual_os_open = os.open
    leaf_opened = False

    def observed_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal leaf_opened
        if path == "remote.txt" and dir_fd is not None:
            leaf_opened = True
        return actual_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(privacy_module, "_is_cloud_placeholder", changing_placeholder_state)
    monkeypatch.setattr("continuity_kernel.privacy.os.open", observed_open)

    result = read_screened_local_content(candidate, selected_root=tmp_path)

    assert checks == 3
    assert not leaf_opened
    assert result.path.decision is AwarenessDecision.PLACEHOLDER
    assert result.content is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-pinned reads")
def test_screened_read_withholds_quarantined_bytes(tmp_path: Path) -> None:
    secret = tmp_path / "notes.txt"
    secret.write_bytes(b"password: this-is-a-real-password-value")

    result = read_screened_local_content(secret, selected_root=tmp_path)

    assert result.screening is not None
    assert result.screening.decision is AwarenessDecision.QUARANTINE
    assert "credential-assignment" in result.screening.reasons
    assert result.content is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_screened_read_fails_closed_if_file_becomes_a_symlink_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    target = selected / "note.txt"
    target.write_text("ordinary", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")

    actual_os_open = os.open
    raced = False

    def raced_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if path == "note.txt" and dir_fd is not None and not raced:
            raced = True
            target.unlink()
            target.symlink_to(outside)
        return actual_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("continuity_kernel.privacy.os.open", raced_open)

    with pytest.raises(ValidationError, match="could not open selected local context"):
        read_screened_local_content(target, selected_root=selected)

    assert raced


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-pinned reads")
def test_screened_read_fails_closed_if_ancestor_is_swapped_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    approved_parent = selected / "approved"
    approved_parent.mkdir()
    target = approved_parent / "note.txt"
    target.write_text("approved", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("private outside bytes", encoding="utf-8")

    actual_os_open = os.open
    raced = False

    def raced_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if path == "approved" and dir_fd is not None and not raced:
            raced = True
            approved_parent.rename(selected / "approved-before-race")
            approved_parent.symlink_to(outside, target_is_directory=True)
        return actual_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("continuity_kernel.privacy.os.open", raced_open)

    with pytest.raises(ValidationError, match="could not pin selected local context ancestry"):
        read_screened_local_content(target, selected_root=selected)

    assert raced


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-pinned reads")
def test_screened_read_fails_closed_if_selected_root_is_swapped_during_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    target = selected / "note.txt"
    target.write_text("approved", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "note.txt"
    outside_target.write_text("private outside bytes", encoding="utf-8")

    actual_lstat = os.lstat
    selected_root_inspections = 0
    raced = False

    def raced_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal raced, selected_root_inspections
        metadata = actual_lstat(path, *args, **kwargs)
        if Path(path) == selected:
            selected_root_inspections += 1
        if selected_root_inspections == 2 and not raced:
            raced = True
            selected.rename(tmp_path / "selected-before-race")
            selected.symlink_to(outside, target_is_directory=True)
        return metadata

    monkeypatch.setattr(os, "lstat", raced_lstat)

    with pytest.raises(ValidationError, match="selected local context root changed before access"):
        read_screened_local_content(target, selected_root=selected)

    assert raced


def test_screened_read_reports_secure_pinning_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("ordinary", encoding="utf-8")
    monkeypatch.setattr(privacy_module, "_PINNED_LOCAL_READ_SUPPORTED", False)

    result = read_screened_local_content(target, selected_root=tmp_path)

    assert result.path.decision is AwarenessDecision.EXCLUDE
    assert result.path.reason == "secure-pinned-read-unsupported"
    assert result.screening is None
    assert result.content is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_nested_symlink_parent_cannot_escape_selected_root(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("private", encoding="utf-8")
    (selected / "linked").symlink_to(outside, target_is_directory=True)

    result = assess_local_path(selected / "linked/note.txt", selected_root=selected)

    assert result.decision is AwarenessDecision.EXCLUDE
    assert result.reason == "outside-selected-root"
