from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import e2e_self_update as self_update


def _native_paths(root: Path) -> dict[str, Path]:
    paths = {
        name: root / name
        for name in ("bin", "codex", "config", "data", "home", "tmp", "tools", "uv-cache")
    }
    for path in paths.values():
        path.mkdir(parents=True)
    return paths


def test_native_lane_is_darwin_only_before_tool_or_source_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda _name: pytest.fail("Darwin rejection must precede tool discovery"),
    )

    with pytest.raises(RuntimeError, match="only on macOS"):
        self_update.run_e2e(tmp_path / "proof", tmp_path / "source", native=True)


def test_run_e2e_forwards_optional_native_lane_to_each_isolated_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\n", encoding="utf-8")
    (source / ".git").mkdir()
    root = tmp_path / "proof"
    root.mkdir()
    revisions = iter(("a" * 40, "b" * 40))
    observed: list[tuple[str, bool]] = []

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def copy_fixture(_source: Path, target: Path) -> None:
        (target / "src/continuity_kernel").mkdir(parents=True)

    def git(_root: Path, *arguments: str) -> str:
        return next(revisions) if arguments == ("rev-parse", "HEAD") else ""

    def scenario(root: Path, **arguments: Any) -> dict[str, Any]:
        observed.append((root.name, bool(arguments["native"])))
        return {"scenario": root.name}

    def crash(root: Path, **arguments: Any) -> dict[str, Any]:
        observed.append((root.name, bool(arguments["native"])))
        return {"scenario": root.name}

    monkeypatch.setattr(self_update, "_copy_candidate_tree", copy_fixture)
    monkeypatch.setattr(self_update, "_git", git)
    monkeypatch.setattr(self_update, "_scenario", scenario)
    monkeypatch.setattr(self_update, "_crash_recovery_scenario", crash)

    report = self_update.run_e2e(root, source, native=True)

    assert report["native_lifecycle"] is True
    assert observed == [("success", True), ("rollback", True), ("crash-recovery", True)]


def test_fresh_native_status_requires_the_isolated_home_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _native_paths(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    application = paths["home"] / "Applications/Seld.app"
    application.mkdir(parents=True)
    status = {
        "application": str(application),
        "current": True,
        "healthy": True,
        "installed": True,
        "owned": True,
        "ownership_revision": "a" * 64,
        "vault": str(vault),
    }
    monkeypatch.setattr(self_update, "_json_cli", lambda *_args, **_kwargs: dict(status))

    assert self_update._fresh_native_status(paths["bin"] / "gsv", {}, vault, paths) == status

    status["application"] = str(tmp_path / "Applications/Seld.app")
    with pytest.raises(RuntimeError, match="isolated native"):
        self_update._fresh_native_status(paths["bin"] / "gsv", {}, vault, paths)


def test_native_uninstall_uses_exact_revision_and_leaves_only_absent_cas_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _native_paths(tmp_path)
    application = paths["home"] / "Applications/Seld.app"
    application.mkdir(parents=True)
    authority = paths["data"] / "native-bridge/state/ownership.json"
    lifecycle = paths["data"] / "native-bridge/state/lifecycle.json"
    authority.parent.mkdir(parents=True)
    authority.write_text(json.dumps({"state": "installed"}), encoding="utf-8")
    lifecycle.write_text(json.dumps({"format_version": 1, "state": "idle"}), encoding="utf-8")
    current_revision = "a" * 64
    absent_revision = "b" * 64
    calls: list[list[str]] = []

    def cli(_gsv: Path, _environment: dict[str, str], arguments: list[str]) -> dict[str, Any]:
        calls.append(arguments)
        if arguments[:2] == ["bridge", "native-uninstall"]:
            assert arguments == [
                "bridge",
                "native-uninstall",
                "--expected-revision",
                current_revision,
            ]
            application.rmdir()
            authority.write_text(json.dumps({"state": "absent"}), encoding="utf-8")
            return {"removed": True, "ownership_revision": absent_revision}
        return {
            "application": str(application),
            "installed": False,
            "owned": False,
            "ownership_revision": absent_revision,
        }

    monkeypatch.setattr(self_update, "_json_cli", cli)

    proof = self_update._prove_native_uninstall(
        paths["bin"] / "gsv",
        {},
        paths,
        {"ownership_revision": current_revision},
    )

    assert proof == {
        "absent_state_cas_retained": True,
        "application_removed": True,
        "expected_revision": current_revision,
        "lifecycle_operation_residue": False,
        "ownership_revision": absent_revision,
        "temporary_residue": [],
    }
    assert calls == [
        ["bridge", "native-uninstall", "--expected-revision", current_revision],
        ["bridge", "native-status"],
    ]
    assert not os.path.lexists(application)
