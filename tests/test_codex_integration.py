from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from continuity_kernel import (
    atomic as atomic_module,
)
from continuity_kernel import (
    codex_integration as integration,
)
from continuity_kernel import resident_context, whatsapp
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ConflictError, SetupError, ValidationError


@dataclass
class FakeCodex:
    marketplaces: dict[str, str] = field(default_factory=dict)
    plugins: set[str] = field(default_factory=set)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    required_manifest: Path | None = None

    def run(self, executable: str, arguments: list[str], home: Path) -> dict[str, Any]:
        del executable, home
        command = tuple(arguments)
        self.calls.append(command)
        checks_or_removes = (
            command
            in {
                ("plugin", "list", "--json"),
                ("plugin", "marketplace", "list", "--json"),
            }
            or command[:2] == ("plugin", "remove")
            or command[:3]
            == (
                "plugin",
                "marketplace",
                "remove",
            )
        )
        if (
            self.required_manifest is not None
            and checks_or_removes
            and (self.plugins or self.marketplaces)
            and not self.required_manifest.is_file()
        ):
            raise SetupError("provider refused operation because marketplace manifest is absent")
        if command == ("plugin", "marketplace", "list", "--json"):
            return {
                "marketplaces": [
                    {"name": name, "root": root} for name, root in self.marketplaces.items()
                ]
            }
        if command[:3] == ("plugin", "marketplace", "add"):
            self.marketplaces[integration.MARKETPLACE_NAME] = command[3]
            return {"ok": True}
        if command[:3] == ("plugin", "marketplace", "remove"):
            self.marketplaces.pop(command[3], None)
            return {"ok": True}
        if command == ("plugin", "list", "--json"):
            return {
                "installed": [
                    {"enabled": True, "pluginId": plugin} for plugin in sorted(self.plugins)
                ]
            }
        if command[:2] == ("plugin", "add"):
            self.plugins.add(command[2])
            return {"ok": True}
        if command[:2] == ("plugin", "remove"):
            self.plugins.discard(command[2])
            return {"ok": True}
        raise AssertionError(f"unexpected fake Codex command: {command}")


@pytest.fixture
def fake_codex(monkeypatch: pytest.MonkeyPatch) -> FakeCodex:
    fake = FakeCodex()
    monkeypatch.setattr(integration, "_codex_executable", lambda: "fake-codex")
    monkeypatch.setattr(integration, "_run_json", fake.run)
    return fake


def _marketplace_root(home: Path) -> Path:
    return integration._marketplace_root(home)


def _required_text(value: str | None) -> str:
    assert value is not None
    return value


def _generated_mcp_environment(vault: Path) -> dict[str, str]:
    contents, _ = integration._marketplace_contents(
        vault,
        runtime=("test-python", ["-m", "continuity_kernel"]),
    )
    encoded = contents["plugins/gsv/.mcp.json"]
    assert isinstance(encoded, bytes)
    payload = json.loads(encoded.decode("utf-8"))
    environment = payload["mcpServers"]["gsv"]["env"]
    assert isinstance(environment, dict)
    return environment


def _write_imported_skill(vault: Path, name: str = "resident-exact") -> Path:
    skill = vault / "context/resident/skills" / name
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_bytes(
        f"---\nname: {name}\ndescription: Exact resident skill.\n---\n\n# Resident exact\n".encode()
    )
    (skill / "references/context.md").write_bytes(b"# Context\n")
    (skill / "scripts/check.py").write_bytes(b"#!/usr/bin/env python3\nprint('resident')\n")
    if os.name != "nt":
        (skill / "SKILL.md").chmod(0o600)
        (skill / "references/context.md").chmod(0o600)
        (skill / "scripts/check.py").chmod(0o700)
    return skill


def test_generated_mcp_environment_omits_unsupplied_service_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.delenv(whatsapp.SERVICE_LABEL_ENV, raising=False)

    assert _generated_mcp_environment(vault) == {
        integration.GSV_DATA_DIR_ENV: str(data_dir()),
        "GSV_VAULT": str(vault.resolve()),
    }


def test_generated_mcp_environment_preserves_only_valid_explicit_service_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv(whatsapp.SERVICE_LABEL_ENV, "ai.example.wacli-sync")
    monkeypatch.setenv("UNRELATED_PROVIDER_SECRET", "must-not-copy")
    monkeypatch.setenv("DISCORD_USER_TOKEN", "must-remain-session-only")
    monkeypatch.setenv("DISCORD_CHANNEL_IDS", "111111111111111111")

    assert _generated_mcp_environment(vault) == {
        integration.GSV_DATA_DIR_ENV: str(data_dir()),
        "GSV_VAULT": str(vault.resolve()),
        whatsapp.SERVICE_LABEL_ENV: "ai.example.wacli-sync",
    }


def test_generated_mcp_environment_pins_default_or_isolated_host_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.delenv(integration.GSV_DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(whatsapp.SERVICE_LABEL_ENV, raising=False)
    default_data = str(data_dir())
    default_environment = _generated_mcp_environment(vault)

    isolated = tmp_path / "isolated-host-data"
    monkeypatch.setenv(integration.GSV_DATA_DIR_ENV, str(isolated))
    isolated_environment = _generated_mcp_environment(vault)

    assert default_environment[integration.GSV_DATA_DIR_ENV] == default_data
    assert isolated_environment == {
        integration.GSV_DATA_DIR_ENV: str(isolated.resolve()),
        "GSV_VAULT": str(vault.resolve()),
    }


def test_generated_mcp_environment_rejects_vault_data_authority_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv(integration.GSV_DATA_DIR_ENV, str(vault / ".host-data"))

    with pytest.raises(ValidationError, match="must be separate absolute paths"):
        _generated_mcp_environment(vault)


@pytest.mark.parametrize(
    "label",
    [
        "",
        "ai.example service",
        "ai/example/service",
        "ai.example." + "x" * 256,
        "ai." + "s" + "k-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456",
    ],
)
def test_generated_mcp_environment_rejects_malformed_service_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv(whatsapp.SERVICE_LABEL_ENV, label)

    with pytest.raises(ValidationError, match="bounded non-secret launchd service label"):
        _generated_mcp_environment(vault)


def test_install_bundles_imported_skill_in_the_receipt_bound_marketplace(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    skill = _write_imported_skill(vault)
    monkeypatch.delenv(whatsapp.SERVICE_LABEL_ENV, raising=False)

    installed = integration.install_codex(vault=vault, codex_home=home)
    generated = Path(installed.marketplace_root) / "plugins/gsv/skills/resident-exact"

    assert (generated / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()
    assert (generated / "references/context.md").read_bytes() == (
        skill / "references/context.md"
    ).read_bytes()
    assert (generated / "scripts/check.py").read_bytes() == (
        skill / "scripts/check.py"
    ).read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE((generated / "SKILL.md").stat().st_mode) == 0o600
        assert stat.S_IMODE((generated / "references/context.md").stat().st_mode) == 0o600
        assert stat.S_IMODE((generated / "scripts/check.py").stat().st_mode) == 0o700
        invoked = subprocess.run(
            [str(generated / "scripts/check.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        assert invoked.returncode == 0
        assert invoked.stdout == "resident\n"
        receipt_manifest = _receipt_payload(home)["marketplace_manifest"]
        assert receipt_manifest[
            "plugins/gsv/skills/resident-exact/references/context.md"
        ].startswith("file:")
        assert receipt_manifest["plugins/gsv/skills/resident-exact/scripts/check.py"].startswith(
            "file+x:"
        )
    mcp = json.loads(
        (Path(installed.marketplace_root) / "plugins/gsv/.mcp.json").read_text(encoding="utf-8")
    )
    assert mcp["mcpServers"]["gsv"]["env"][integration.GSV_DATA_DIR_ENV] == str(data_dir())
    assert integration.codex_status(codex_home=home)["manifest_verified"] is True
    assert fake_codex.plugins == {integration.PLUGIN_ID}

    reinstalled = integration.install_codex(vault=vault, codex_home=home)
    reinstalled_skill = Path(reinstalled.marketplace_root) / "plugins/gsv/skills/resident-exact"
    if os.name != "nt":
        assert stat.S_IMODE((reinstalled_skill / "references/context.md").stat().st_mode) == 0o600
        assert stat.S_IMODE((reinstalled_skill / "scripts/check.py").stat().st_mode) == 0o700
    assert integration.codex_status(codex_home=home)["manifest_verified"] is True
    if os.name != "nt":
        (reinstalled_skill / "scripts/check.py").chmod(0o600)
        assert integration.codex_status(codex_home=home)["manifest_verified"] is False


def test_install_receipt_carries_a_large_bounded_resident_skill_inventory(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    for index in range(61):
        _write_imported_skill(vault, f"resident-{index:03d}")
    monkeypatch.delenv(whatsapp.SERVICE_LABEL_ENV, raising=False)
    receipt_sizes: list[int] = []
    write_receipt = integration._write_receipt_state

    def record_receipt_size(
        target_home: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        context: str,
    ) -> None:
        receipt_sizes.append(len(replacement))
        write_receipt(
            target_home,
            expected=expected,
            replacement=replacement,
            context=context,
        )

    monkeypatch.setattr(integration, "_write_receipt_state", record_receipt_size)

    installed = integration.install_codex(vault=vault, codex_home=home)

    assert max(receipt_sizes) > 64 * 1024
    assert max(receipt_sizes) <= integration.RECEIPT_MAX_BYTES
    assert integration.codex_status(codex_home=home)["manifest_verified"] is True
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert Path(installed.marketplace_root).is_dir()


def test_install_uses_one_transactional_resident_skill_snapshot(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    skill = _write_imported_skill(vault)
    original = (skill / "SKILL.md").read_bytes()
    materialize = integration._materialize_marketplace
    mutated = False

    def mutate_source_after_preflight(
        target: Path,
        contents: dict[str, bytes | resident_context.ResidentSkillFile | None],
        manifest: dict[str, str],
    ) -> None:
        nonlocal mutated
        mutated = True
        (skill / "SKILL.md").write_text(
            "---\nname: resident-exact\ndescription: Changed after preflight.\n---\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            (skill / "scripts/check.py").chmod(0o600)
        materialize(target, contents, manifest)

    monkeypatch.setattr(integration, "_materialize_marketplace", mutate_source_after_preflight)

    installed = integration.install_codex(vault=vault, codex_home=home)
    generated = Path(installed.marketplace_root) / "plugins/gsv/skills/resident-exact/SKILL.md"

    assert mutated is True
    assert generated.read_bytes() == original
    assert generated.read_bytes() != (skill / "SKILL.md").read_bytes()
    if os.name != "nt":
        generated_script = generated.parent / "scripts/check.py"
        assert stat.S_IMODE(generated_script.stat().st_mode) == 0o700
        assert stat.S_IMODE((skill / "scripts/check.py").stat().st_mode) == 0o600
    assert integration.codex_status(codex_home=home)["manifest_verified"] is True
    assert fake_codex.plugins == {integration.PLUGIN_ID}


def test_generated_instructions_keep_legacy_import_mechanics_inert(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    resident = vault / "context/resident"
    resident.mkdir(parents=True)
    legacy_guidance = (
        "# Imported resident\n\n"
        "Use the private checkout and `.sbrain/PULSE`, then run `gsv pending-show`.\n"
    )
    (resident / "AGENTS.md").write_bytes(legacy_guidance.encode("utf-8"))
    control = resident / "control"
    control.mkdir()
    (control / "RESIDENT").write_text("legacy-host-task-binding\n", encoding="utf-8")

    contents, _ = integration._marketplace_contents(
        vault,
        runtime=("test-python", ["-m", "continuity_kernel"]),
    )
    generated = contents["plugins/gsv/skills/gsv/SKILL.md"]

    assert resident_context.read_resident_guidance(vault)["content"] == legacy_guidance
    assert isinstance(generated, bytes)
    assert b"installed Seld plugin and the active vault" in generated
    assert b"context/resident/control` is inert legacy data" in generated
    assert b"legacy `gsv pending-*` commands" in generated
    assert b"legacy-host-task-binding" not in generated
    assert (
        "installed Seld plugin and active vault remain authoritative" in integration.MANAGED_BLOCK
    )


def test_imported_builtin_skill_collision_fails_before_local_install_mutation(
    tmp_path: Path,
    fake_codex: FakeCodex,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    agents.write_text("# Existing\n", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_imported_skill(vault, "gsv")

    with pytest.raises(ConflictError, match="preserved the built-in skill"):
        integration.install_codex(vault=vault, codex_home=home)

    assert agents.read_text(encoding="utf-8") == "# Existing\n"
    assert not integration._marketplace_root(home).exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def _receipt_payload(home: Path) -> dict[str, Any]:
    payload = json.loads(integration._receipt_path(home).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _retained_paths(home: Path) -> list[Path]:
    root = _marketplace_root(home)
    return [
        root.parent / record["basename"] for record in _receipt_payload(home)["cleanup_pending"]
    ]


def _downgrade_receipt_to_v1(home: Path) -> None:
    current = _receipt_payload(home)
    legacy = {
        "codex_home": current["codex_home"],
        "format_version": 1,
        "marketplace_digest": current["marketplace_digest"],
        "marketplace_owned": True,
        "marketplace_root": current["marketplace_root"],
        "plugin_owned": True,
    }
    integration._receipt_path(home).write_text(
        json.dumps(legacy, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fill_cleanup_capacity(
    home: Path,
    *,
    count: int = integration.MAX_CLEANUP_RECORDS,
) -> list[Path]:
    root = _marketplace_root(home)
    records: list[dict[str, Any]] = []
    paths: list[Path] = []
    for index in range(count):
        token = f"{index + 1:032x}"
        path = root.parent / f".{root.name}.old-{token}"
        path.mkdir()
        (path / "retained.txt").write_text(f"retained {index}\n", encoding="utf-8")
        records.append(
            integration._retained_record(
                root,
                kind="old",
                manifest=integration._tree_manifest(path),
                operation_token=token,
            )
        )
        paths.append(path)
    payload = _receipt_payload(home)
    payload["cleanup_pending"] = records
    integration._validate_receipt_payload(
        payload,
        home=home.resolve(),
        path=integration._receipt_path(home),
    )
    integration._receipt_path(home).write_bytes(integration._encode_receipt(payload))
    return paths


def test_install_retry_and_uninstall_preserve_existing_instructions(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    agents.write_text("# Existing instructions\n\nKeep this.\n", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()

    first = integration.install_codex(vault=vault, codex_home=home)
    generated_marketplace = Path(first.marketplace_root)
    plugin_manifest = json.loads(
        (generated_marketplace / "plugins/gsv/.codex-plugin/plugin.json").read_text(
            encoding="utf-8"
        )
    )
    assert "set up seld" in plugin_manifest["interface"]["defaultPrompt"][0].casefold()
    assert "$gsv-onboard" in agents.read_text(encoding="utf-8")
    onboard = generated_marketplace / "plugins/gsv/skills/gsv-onboard"
    assert (onboard / "SKILL.md").is_file()
    assert (onboard / "agents/openai.yaml").is_file()
    assert {path.name for path in (onboard / "references").iterdir() if path.is_file()} == {
        "computer-use.md",
        "connector-readiness.md",
        "context-intake.md",
        "local-files.md",
        "recovery.md",
        "source-catalog.md",
    }
    provider_names = {
        "asana",
        "atlassian",
        "box",
        "discord",
        "figma",
        "github",
        "gmail",
        "google-calendar",
        "google-drive",
        "notion",
        "outlook-calendar",
        "outlook-email",
        "sharepoint",
        "slack",
        "teams",
    }
    providers = onboard / "references/providers"
    assert {path.stem for path in providers.glob("*.md")} == provider_names
    source_catalog = (onboard / "references/source-catalog.md").read_text(encoding="utf-8")
    recovery = (onboard / "references/recovery.md").read_text(encoding="utf-8")
    assert "local-source staged-status" in recovery
    assert "local-source adopt-staged" in recovery
    assert "local-source adopt-checkpoint" not in recovery
    for provider_name in provider_names:
        provider_text = " ".join(
            (providers / f"{provider_name}.md").read_text(encoding="utf-8").split()
        )
        assert f"providers/{provider_name}.md" in source_catalog
        assert "$gsv-onboard" in provider_text
        assert "fresh ChatGPT task" in provider_text
    pulse = generated_marketplace / "plugins/gsv/skills/gsv-pulse"
    assert (pulse / "SKILL.md").is_file()
    assert {path.name for path in (pulse / "references").iterdir() if path.is_file()} == {
        "registration.md",
        "source-acquisition.md",
    }
    update = generated_marketplace / "plugins/gsv/skills/gsv-update"
    assert (update / "SKILL.md").is_file()
    assert (update / "agents/openai.yaml").is_file()
    assert not (home / "AGENTS.md.gsv-backup").exists()
    second = integration.install_codex(vault=vault, codex_home=home)
    removed = integration.uninstall_codex(codex_home=home)

    assert first.plugin_installed and second.plugin_installed
    assert agents.read_text(encoding="utf-8") == "# Existing instructions\n\nKeep this.\n"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert removed["plugin_removed"] is True
    assert removed["marketplace_files_removed"] is False
    assert removed["marketplace_files_state"] == "retained"
    assert removed["integration_removed"] is True
    assert removed["cleanup_complete"] is False
    assert removed["recovery_retained"] is True
    assert removed["user_data_preserved"] is True
    assert vault.exists()
    assert not generated_marketplace.exists()
    retained = _retained_paths(home)
    assert retained == [Path(path) for path in removed["retained_cleanup_paths"]]
    assert len(retained) == 1
    assert all(path.is_dir() for path in retained)
    receipt = _receipt_payload(home)
    assert receipt["integration_active"] is False
    assert receipt["marketplace_owned"] is False
    assert receipt["plugin_owned"] is False
    content = agents.read_text(encoding="utf-8")
    assert integration.BLOCK_START not in content
    assert integration.BLOCK_END not in content


def test_codex_status_ready_requires_receipt_provider_root_and_exact_manifest(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    ready = integration.codex_status(codex_home=home)
    assert ready["ready"] is True
    assert ready["receipt_active"] is True
    assert ready["marketplace_root_verified"] is True
    assert ready["manifest_verified"] is True

    (Path(installed.marketplace_root) / "plugins/gsv/SKILL.md").write_text(
        "changed after installation\n", encoding="utf-8"
    )
    changed = integration.codex_status(codex_home=home)
    assert changed["plugin_installed"] is True
    assert changed["ready"] is False
    assert changed["manifest_verified"] is False


def test_identical_reinstall_is_a_true_noop_without_new_recovery_or_provider_mutation(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    first = integration.install_codex(vault=vault, codex_home=home)
    root = Path(first.marketplace_root)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    receipt_identity_before = (receipt.stat().st_ino, receipt.stat().st_mtime_ns)
    root_identity_before = (root.stat().st_ino, root.stat().st_mtime_ns)
    root_manifest_before = integration._tree_manifest(root)
    recovery_before = sorted(path.name for path in root.parent.iterdir())
    calls_before = len(fake_codex.calls)

    second = integration.install_codex(vault=vault, codex_home=home)

    assert second == first
    assert receipt.read_bytes() == receipt_before
    assert (receipt.stat().st_ino, receipt.stat().st_mtime_ns) == receipt_identity_before
    assert (root.stat().st_ino, root.stat().st_mtime_ns) == root_identity_before
    assert integration._tree_manifest(root) == root_manifest_before
    assert sorted(path.name for path in root.parent.iterdir()) == recovery_before
    assert not any("add" in call or "remove" in call for call in fake_codex.calls[calls_before:])


def test_capacity_bound_different_vault_reinstall_refuses_before_any_mutation(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    root = Path(installed.marketplace_root)
    retained = _fill_cleanup_capacity(home)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    agents_before = (home / "AGENTS.md").read_bytes()
    root_before = integration._tree_manifest(root)
    entries_before = sorted(path.name for path in root.parent.iterdir())
    retained_before = {path: integration._tree_manifest(path) for path in retained}
    calls_before = len(fake_codex.calls)

    with pytest.raises(
        (SetupError, ValidationError),
        match=r"capacity|too many retained|no room",
    ):
        integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)

    assert receipt.read_bytes() == receipt_before
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert integration._tree_manifest(root) == root_before
    assert sorted(path.name for path in root.parent.iterdir()) == entries_before
    assert {path: integration._tree_manifest(path) for path in retained} == retained_before
    assert not any("add" in call or "remove" in call for call in fake_codex.calls[calls_before:])
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(root.resolve())}


def test_capacity_bound_uninstall_refuses_before_provider_or_local_mutation(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    root = Path(installed.marketplace_root)
    retained = _fill_cleanup_capacity(home)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    agents_before = (home / "AGENTS.md").read_bytes()
    root_before = integration._tree_manifest(root)
    entries_before = sorted(path.name for path in root.parent.iterdir())
    retained_before = {path: integration._tree_manifest(path) for path in retained}
    calls_before = len(fake_codex.calls)

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["integration_removed"] is False
    assert removed["provider_cleanup_verified"] is False
    assert removed["provider_checkpointed"] is False
    assert removed["provider_cleanup_skipped"] is True
    capacity_error = " ".join(
        str(value)
        for value in (
            removed["local_cleanup_error"],
            removed["provider_cleanup_error"],
            removed["provider_checkpoint_error"],
        )
        if value is not None
    )
    assert any(phrase in capacity_error for phrase in ("capacity", "too many retained", "no room"))
    assert receipt.read_bytes() == receipt_before
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert integration._tree_manifest(root) == root_before
    assert sorted(path.name for path in root.parent.iterdir()) == entries_before
    assert {path: integration._tree_manifest(path) for path in retained} == retained_before
    assert not any("add" in call or "remove" in call for call in fake_codex.calls[calls_before:])
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(root.resolve())}


def test_uninstall_uses_exact_current_record_among_historical_removes(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    first_uninstall = integration.uninstall_codex(codex_home=home)
    historical_path = Path(first_uninstall["retained_cleanup_paths"][0])
    assert historical_path.is_dir()

    integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)
    captured: dict[str, Any] = {}
    real_remove = integration._remove_owned_marketplace

    def capture_checkpoint(
        receipt: dict[str, Any],
        *,
        expected_manifest: object,
    ) -> integration._MarketplaceCleanup:
        captured.update(json.loads(json.dumps(receipt)))
        return real_remove(receipt, expected_manifest=expected_manifest)

    monkeypatch.setattr(integration, "_remove_owned_marketplace", capture_checkpoint)
    second_uninstall = integration.uninstall_codex(codex_home=home)

    remove_records = [
        record for record in captured["cleanup_pending"] if record["kind"] == "remove"
    ]
    assert len(remove_records) == 2
    current = captured["uninstall_record"]
    historical = [record for record in remove_records if record != current]
    assert len(historical) == 1
    root = _marketplace_root(home)
    current_path = root.parent / current["basename"]
    assert root.parent / historical[0]["basename"] == historical_path
    assert historical_path.is_dir()
    assert current_path.is_dir()
    assert second_uninstall["cleanup_complete"] is False
    assert set(second_uninstall["retained_cleanup_paths"]) == {
        str(historical_path),
        str(current_path),
    }


def test_provider_checkpoint_resume_does_not_hide_historical_marketplace_bytes(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    first_uninstall = integration.uninstall_codex(codex_home=home)
    historical = Path(first_uninstall["retained_cleanup_paths"][0])
    assert historical.is_dir()

    integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)
    root = _marketplace_root(home)
    manifest = integration._tree_manifest(root)
    snapshot = integration._load_receipt_snapshot(home)
    integration._checkpoint_provider_verified(
        home,
        snapshot,
        marketplace_manifest=manifest,
    )
    shutil.rmtree(root)
    fake_codex.plugins.clear()
    fake_codex.marketplaces.clear()
    monkeypatch.setattr(integration, "_present_pending_paths", lambda *_args, **_kwargs: [])

    resumed = integration.uninstall_codex(codex_home=home)

    assert resumed["cleanup_complete"] is False
    assert resumed["integration_removed"] is True
    assert resumed["marketplace_files_state"] == "already_missing"
    assert resumed["recovery_retained"] is True
    assert resumed["retained_cleanup_paths"] == [str(historical)]
    assert resumed["marketplace_files_removed"] is False
    assert str(historical) in _required_text(resumed["next"])
    assert "re-run `gsv codex uninstall`" in _required_text(resumed["next"])
    assert historical.is_dir()


def test_uninstall_refuses_tampered_historical_recovery_before_provider_or_local_mutation(
    tmp_path: Path,
    fake_codex: FakeCodex,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    first_uninstall = integration.uninstall_codex(codex_home=home)
    historical = [Path(path) for path in first_uninstall["retained_cleanup_paths"]]
    assert historical
    installed = integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)
    root = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    root_before = integration._tree_manifest(root)
    agents_before = agents.read_bytes()
    receipt_before = receipt.read_bytes()
    calls_before = list(fake_codex.calls)
    tampered = historical[0] / "concurrent-user-file.txt"
    tampered.write_bytes(b"preserve tampered historical recovery\n")

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["integration_removed"] is False
    assert removed["provider_cleanup_skipped"] is True
    assert removed["provider_cleanup_verified"] is False
    assert removed["provider_checkpointed"] is False
    assert "retained recovery tree changed" in _required_text(removed["local_cleanup_error"])
    assert removed["marketplace_files_state"] == "verified_present"
    assert fake_codex.calls == calls_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(root.resolve())}
    assert integration._tree_manifest(root) == root_before
    assert agents.read_bytes() == agents_before
    assert receipt.read_bytes() == receipt_before
    assert tampered.read_bytes() == b"preserve tampered historical recovery\n"


def test_recovery_only_uninstall_is_idempotent_and_makes_no_provider_calls(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    removed = integration.uninstall_codex(codex_home=home)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    retained_before = {path: integration._tree_manifest(path) for path in _retained_paths(home)}
    calls_before = list(fake_codex.calls)
    monkeypatch.setattr(integration, "_present_pending_paths", lambda *_args, **_kwargs: [])

    first_recovery = integration.uninstall_codex(codex_home=home)
    second_recovery = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert first_recovery == second_recovery
    assert first_recovery["cleanup_complete"] is False
    assert first_recovery["local_cleanup_verified"] is True
    assert first_recovery["integration_removed"] is True
    assert first_recovery["recovery_retained"] is True
    assert set(first_recovery["retained_cleanup_paths"]) == {str(path) for path in retained_before}
    assert first_recovery["marketplace_files_state"] == "retained"
    assert first_recovery["marketplace_files_removed"] is False
    assert fake_codex.calls == calls_before
    assert receipt.read_bytes() == receipt_before
    assert {path: integration._tree_manifest(path) for path in retained_before} == retained_before


def test_recovery_only_uninstall_completes_after_exact_retained_paths_are_retired(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    first = integration.uninstall_codex(codex_home=home)
    calls_before = list(fake_codex.calls)

    assert first["cleanup_complete"] is False
    assert first["local_cleanup_verified"] is True
    assert first["integration_removed"] is True
    assert first["recovery_retained"] is True
    retired_paths = list(map(Path, first["retained_cleanup_paths"]))
    for retained in retired_paths:
        shutil.rmtree(retained)
    monkeypatch.setattr(
        integration,
        "_present_pending_paths",
        lambda *_args, **_kwargs: [str(path) for path in retired_paths],
    )

    completed = integration.uninstall_codex(codex_home=home)

    assert completed["cleanup_complete"] is True
    assert completed["local_cleanup_verified"] is True
    assert completed["integration_removed"] is True
    assert completed["marketplace_files_removed"] is True
    assert completed["recovery_retained"] is False
    assert completed["retained_cleanup_paths"] == []
    assert completed["receipt_missing"] is True
    assert fake_codex.calls == calls_before


def test_recovery_only_retry_reports_reappeared_public_root_without_changing_history(
    tmp_path: Path,
    fake_codex: FakeCodex,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    removed = integration.uninstall_codex(codex_home=home)
    root = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    retained_before = {path: integration._tree_manifest(path) for path in _retained_paths(home)}
    calls_before = list(fake_codex.calls)
    root.mkdir()
    sentinel = root / "reappeared-user-file.txt"
    sentinel.write_bytes(b"preserve reappeared public root\n")

    retried = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert retried["cleanup_complete"] is False
    assert retried["integration_removed"] is False
    assert retried["local_cleanup_verified"] is False
    assert retried["manual_review_required"] is True
    assert retried["local_cleanup_error"] == (
        f"the public marketplace path reappeared after uninstall: {root}"
    )
    assert retried["marketplace_files_path"] == str(root)
    assert retried["marketplace_files_state"] == "changed_or_unsafe"
    assert retried["marketplace_files_removed"] is False
    assert sentinel.read_bytes() == b"preserve reappeared public root\n"
    assert receipt.read_bytes() == receipt_before
    assert {path: integration._tree_manifest(path) for path in retained_before} == retained_before
    assert fake_codex.calls == calls_before


def test_recovery_only_retry_reports_reappeared_managed_block_without_changing_history(
    tmp_path: Path,
    fake_codex: FakeCodex,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    root = Path(installed.marketplace_root)
    managed_agents = (home / "AGENTS.md").read_bytes()
    removed = integration.uninstall_codex(codex_home=home)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    retained_before = {path: integration._tree_manifest(path) for path in _retained_paths(home)}
    calls_before = list(fake_codex.calls)
    agents = home / "AGENTS.md"
    agents.write_bytes(managed_agents)

    retried = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert retried["cleanup_complete"] is False
    assert retried["integration_removed"] is False
    assert retried["local_cleanup_verified"] is False
    assert retried["manual_review_required"] is True
    assert retried["local_cleanup_error"] == (
        f"the Seld managed instruction block reappeared after uninstall: {agents}"
    )
    assert retried["marketplace_files_path"] == str(next(iter(retained_before)))
    assert retried["marketplace_files_state"] == "retained"
    assert retried["marketplace_files_removed"] is False
    assert not root.exists()
    assert agents.read_bytes() == managed_agents
    assert receipt.read_bytes() == receipt_before
    assert {path: integration._tree_manifest(path) for path in retained_before} == retained_before
    assert fake_codex.calls == calls_before


def test_recovery_only_receipt_durability_failure_is_conservative_and_structured(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    root = Path(installed.marketplace_root)
    removed = integration.uninstall_codex(codex_home=home)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    retained_before = {path: integration._tree_manifest(path) for path in _retained_paths(home)}
    calls_before = list(fake_codex.calls)

    def fail_durability(path: Path, expected: bytes) -> None:
        assert path == receipt
        assert expected == receipt_before
        raise OSError("injected receipt durability confirmation failure")

    monkeypatch.setattr(integration, "_confirm_receipt_durable", fail_durability)
    retried = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert retried["cleanup_complete"] is False
    assert retried["integration_removed"] is False
    assert retried["local_cleanup_verified"] is False
    assert retried["manual_review_required"] is True
    assert retried["local_cleanup_error"] == ("injected receipt durability confirmation failure")
    assert retried["marketplace_files_path"] == str(root)
    assert retried["marketplace_files_state"] == "changed_or_unsafe"
    assert retried["marketplace_files_removed"] is False
    assert retried["receipt_state"] == "owned"
    assert retried["receipt_preserved_for_retry"] is True
    assert receipt.read_bytes() == receipt_before
    assert {path: integration._tree_manifest(path) for path in retained_before} == retained_before
    assert fake_codex.calls == calls_before


@pytest.mark.parametrize("mutation", ["added", "missing"])
def test_reinstall_refuses_changed_or_partial_receipt_owned_marketplace(
    tmp_path: Path, fake_codex: FakeCodex, mutation: str
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    sentinel = marketplace / "user-sentinel.txt"
    if mutation == "added":
        sentinel.write_bytes(b"preserve exact user bytes\n")
    else:
        (marketplace / "plugins/gsv/skills/gsv/SKILL.md").unlink()
    calls_before = list(fake_codex.calls)

    with pytest.raises(SetupError, match="missing, changed, or only partly"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert fake_codex.calls == calls_before
    if mutation == "added":
        assert sentinel.read_bytes() == b"preserve exact user bytes\n"
    else:
        assert not (marketplace / "plugins/gsv/skills/gsv/SKILL.md").exists()


def test_reinstall_rechecks_prior_marketplace_immediately_before_replacement(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    agents_before = agents.read_bytes()
    receipt_before = receipt.read_bytes()
    manifest_before = integration._tree_manifest(marketplace)
    calls_before = len(fake_codex.calls)
    sentinel = marketplace / "concurrent-user-file.txt"
    real_replace = integration._replace_marketplace

    def mutate_before_replace(
        vault: Path,
        *,
        runtime: tuple[str, list[str]] | None = None,
        target: Path,
        expected_prior_manifest: dict[str, str] | None = None,
        before_moves: Any,
        prepared: tuple[
            dict[str, bytes | resident_context.ResidentSkillFile | None], dict[str, str]
        ]
        | None = None,
    ) -> integration._MarketplaceChange:
        sentinel.write_bytes(b"preserve concurrent user bytes\n")
        return real_replace(
            vault,
            runtime=runtime,
            target=target,
            expected_prior_manifest=expected_prior_manifest,
            before_moves=before_moves,
            prepared=prepared,
        )

    monkeypatch.setattr(integration, "_replace_marketplace", mutate_before_replace)

    with pytest.raises(ConflictError, match="changed immediately before replacement"):
        integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)

    new_calls = fake_codex.calls[calls_before:]
    assert not any("add" in call or "remove" in call for call in new_calls)
    assert agents.read_bytes() == agents_before
    assert receipt.read_bytes() == receipt_before
    assert sentinel.read_bytes() == b"preserve concurrent user bytes\n"
    current = integration._tree_manifest(marketplace)
    assert {key: current[key] for key in manifest_before} == manifest_before
    assert not list(marketplace.parent.glob(f".{marketplace.name}.old-*"))


def test_reinstall_rechecks_prior_marketplace_after_atomic_isolation(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    calls_before = len(fake_codex.calls)
    real_publish = integration._publish_directory_new
    injected = False

    def mutate_after_isolation(source: Path, target: Path) -> None:
        nonlocal injected
        real_publish(source, target)
        if source == marketplace and target.name.startswith(f".{marketplace.name}.old-"):
            (target / "concurrent-user-file.txt").write_bytes(b"preserve move-boundary bytes\n")
            injected = True

    monkeypatch.setattr(integration, "_publish_directory_new", mutate_after_isolation)

    with pytest.raises(SetupError, match="changed at the isolation boundary"):
        integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)

    assert injected is True
    transition_receipt = _receipt_payload(home)
    transition = transition_receipt["install_transition"]
    retained_prior = marketplace.parent / transition["previous"]["basename"]
    partial_candidate = marketplace.parent / transition["candidate"]["basename"]
    assert (retained_prior / "concurrent-user-file.txt").read_bytes() == (
        b"preserve move-boundary bytes\n"
    )
    assert partial_candidate.is_dir()
    assert not marketplace.exists()
    assert receipt.read_bytes() != receipt_before
    assert not any("add" in call or "remove" in call for call in fake_codex.calls[calls_before:])


def test_reinstall_preserves_both_trees_when_target_reappears_after_isolation(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    calls_before = len(fake_codex.calls)
    real_publish = integration._publish_directory_new
    sentinel = marketplace / "race-created-user-file.txt"

    def recreate_target_after_isolation(source: Path, target: Path) -> None:
        real_publish(source, target)
        if source == marketplace and target.name.startswith(f".{marketplace.name}.old-"):
            marketplace.mkdir()
            sentinel.write_bytes(b"preserve replacement destination race\n")

    monkeypatch.setattr(
        integration,
        "_publish_directory_new",
        recreate_target_after_isolation,
    )

    with pytest.raises(SetupError, match="publish target already exists"):
        integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)

    transition_receipt = _receipt_payload(home)
    transition = transition_receipt["install_transition"]
    previous = marketplace.parent / transition["previous"]["basename"]
    candidate = marketplace.parent / transition["candidate"]["basename"]
    assert sentinel.read_bytes() == b"preserve replacement destination race\n"
    assert (previous / "plugins/gsv/skills/gsv/SKILL.md").is_file()
    assert (candidate / "plugins/gsv/skills/gsv/SKILL.md").is_file()
    assert receipt.read_bytes() != receipt_before
    assert not any("add" in call or "remove" in call for call in fake_codex.calls[calls_before:])


def test_process_death_after_prior_isolation_recovers_with_manifest_then_retries_offline(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    provider_manifest = marketplace / ".agents/plugins/marketplace.json"
    fake_codex.required_manifest = provider_manifest
    real_publish = integration._publish_directory_new

    def die_after_prior_isolation(source: Path, target: Path) -> None:
        real_publish(source, target)
        if source == marketplace and target.name.startswith(f".{marketplace.name}.old-"):
            raise SimulatedProcessDeath("simulated process death after prior isolation")

    monkeypatch.setattr(integration, "_publish_directory_new", die_after_prior_isolation)
    with pytest.raises(SimulatedProcessDeath, match="after prior isolation"):
        integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)

    transition_receipt = _receipt_payload(home)
    transition = transition_receipt["install_transition"]
    previous_record = transition["previous"]
    assert previous_record is not None
    previous_path = marketplace.parent / previous_record["basename"]
    candidate_path = marketplace.parent / transition["candidate"]["basename"]
    assert not marketplace.exists()
    assert integration._tree_manifest(previous_path) == previous_record["manifest"]
    assert integration._tree_manifest(candidate_path) == transition["candidate"]["manifest"]
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace.resolve())}

    monkeypatch.setattr(integration, "_publish_directory_new", real_publish)
    real_write_receipt = integration._write_receipt_state
    checkpoint_seen: dict[str, Any] = {}

    def observe_interrupted_checkpoint(
        target_home: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        context: str,
    ) -> None:
        if context == "interrupted-install provider checkpoint":
            checkpoint_seen.update(
                {
                    "manifest_present": provider_manifest.is_file(),
                    "marketplace_manifest": integration._tree_manifest(marketplace),
                    "marketplace_registered": bool(fake_codex.marketplaces),
                    "plugin_registered": bool(fake_codex.plugins),
                    "receipt": json.loads(replacement.decode("utf-8")),
                }
            )
        real_write_receipt(
            target_home,
            expected=expected,
            replacement=replacement,
            context=context,
        )

    monkeypatch.setattr(integration, "_write_receipt_state", observe_interrupted_checkpoint)
    removed = integration.uninstall_codex(codex_home=home)

    assert checkpoint_seen["manifest_present"] is True
    assert checkpoint_seen["marketplace_manifest"] == previous_record["manifest"]
    assert checkpoint_seen["marketplace_registered"] is False
    assert checkpoint_seen["plugin_registered"] is False
    assert checkpoint_seen["receipt"]["uninstall_phase"] == "provider_verified"
    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is True
    assert removed["marketplace_files_state"] == "retained"
    assert not marketplace.exists()
    recovery = _receipt_payload(home)
    retained_by_kind = {record["kind"]: record for record in recovery["cleanup_pending"]}
    assert set(retained_by_kind) == {"new", "remove"}
    for record in retained_by_kind.values():
        retained_path = marketplace.parent / record["basename"]
        assert integration._tree_manifest(retained_path) == record["manifest"]
    assert retained_by_kind["new"]["manifest"] == transition["candidate"]["manifest"]
    assert retained_by_kind["remove"]["manifest"] == previous_record["manifest"]

    calls_before_retry = list(fake_codex.calls)
    receipt_before_retry = integration._receipt_path(home).read_bytes()
    monkeypatch.setattr(
        integration,
        "_codex_executable",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not run on recovery retry")),
    )
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is False
    assert retried["provider_cleanup_verified"] is True
    assert retried["recovery_retained"] is True
    assert fake_codex.calls == calls_before_retry
    assert integration._receipt_path(home).read_bytes() == receipt_before_retry


def test_first_install_transition_does_not_infer_provider_absence_after_registrations_appear(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    home = tmp_path / "codex"
    home.mkdir()
    root = _marketplace_root(home)
    real_materialize = integration._materialize_marketplace

    def die_before_materialization(
        target: Path,
        contents: dict[str, bytes | None],
        expected_manifest: dict[str, str],
    ) -> None:
        del target, contents, expected_manifest
        raise SimulatedProcessDeath("simulated death after first-install transition receipt")

    monkeypatch.setattr(integration, "_materialize_marketplace", die_before_materialization)
    with pytest.raises(SimulatedProcessDeath, match="first-install transition receipt"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    transition_receipt = _receipt_payload(home)
    assert transition_receipt["install_transition"]["provider_before"] == {
        "marketplace": False,
        "plugin": False,
    }
    assert transition_receipt["install_transition"]["provider_attempts"] == {
        "marketplace": False,
        "plugin": False,
    }
    assert not root.exists()
    fake_codex.plugins.add(integration.PLUGIN_ID)
    fake_codex.marketplaces[integration.MARKETPLACE_NAME] = str(root.resolve())
    calls_before = len(fake_codex.calls)
    monkeypatch.setattr(integration, "_materialize_marketplace", real_materialize)

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["integration_removed"] is False
    assert removed["provider_cleanup_verified"] is False
    assert removed["provider_checkpointed"] is False
    assert removed["registration_cleanup_deferred"] is True
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(root.resolve())}
    assert not any("add" in call or "remove" in call for call in fake_codex.calls[calls_before:])
    assert "install_transition" in _receipt_payload(home)


def test_first_install_rolls_back_marketplace_add_that_commits_then_errors(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    real_run = fake_codex.run
    failed = False

    def commit_marketplace_then_error(
        executable: str,
        arguments: list[str],
        codex_home: Path,
    ) -> dict[str, Any]:
        nonlocal failed
        result = real_run(executable, arguments, codex_home)
        if arguments[:3] == ["plugin", "marketplace", "add"] and not failed:
            failed = True
            raise SetupError("injected ambiguous marketplace add outcome")
        return result

    monkeypatch.setattr(integration, "_run_json", commit_marketplace_then_error)

    with pytest.raises(SetupError, match="ambiguous marketplace add outcome"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    recovery = _receipt_payload(home)
    assert failed is True
    assert recovery["integration_active"] is False
    assert "install_transition" not in recovery
    assert fake_codex.marketplaces == {}
    assert fake_codex.plugins == set()
    assert not _marketplace_root(home).exists()
    assert all(path.is_dir() for path in _retained_paths(home))


def test_first_install_rolls_back_plugin_add_that_commits_then_errors(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    real_run = fake_codex.run
    failed = False

    def commit_plugin_then_error(
        executable: str,
        arguments: list[str],
        codex_home: Path,
    ) -> dict[str, Any]:
        nonlocal failed
        result = real_run(executable, arguments, codex_home)
        if arguments[:2] == ["plugin", "add"] and not failed:
            failed = True
            raise SetupError("injected ambiguous plugin add outcome")
        return result

    monkeypatch.setattr(integration, "_run_json", commit_plugin_then_error)

    with pytest.raises(SetupError, match="ambiguous plugin add outcome"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    recovery = _receipt_payload(home)
    assert failed is True
    assert recovery["integration_active"] is False
    assert "install_transition" not in recovery
    assert fake_codex.marketplaces == {}
    assert fake_codex.plugins == set()
    assert not _marketplace_root(home).exists()
    assert all(path.is_dir() for path in _retained_paths(home))


def test_process_death_after_provider_add_is_recovered_from_attempt_checkpoint(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    home = tmp_path / "codex"
    home.mkdir()
    marketplace = _marketplace_root(home)
    real_run = fake_codex.run

    def die_after_plugin_add(
        executable: str,
        arguments: list[str],
        codex_home: Path,
    ) -> dict[str, Any]:
        result = real_run(executable, arguments, codex_home)
        if arguments[:2] == ["plugin", "add"]:
            raise SimulatedProcessDeath("simulated death after provider add")
        return result

    monkeypatch.setattr(integration, "_run_json", die_after_plugin_add)
    with pytest.raises(SimulatedProcessDeath, match="after provider add"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    transition = _receipt_payload(home)["install_transition"]
    assert transition["provider_before"] == {"marketplace": False, "plugin": False}
    assert transition["provider_attempts"] == {"marketplace": True, "plugin": True}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace)}
    assert fake_codex.plugins == {integration.PLUGIN_ID}

    monkeypatch.setattr(integration, "_run_json", real_run)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["integration_removed"] is True
    assert fake_codex.marketplaces == {}
    assert fake_codex.plugins == set()
    assert not marketplace.exists()
    assert _receipt_payload(home)["integration_active"] is False


def test_transition_checkpoint_failure_separates_provider_verification_from_durability(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)

    def die_before_instructions(target_home: Path) -> integration._InstructionChange:
        del target_home
        raise SimulatedProcessDeath("simulated death before instruction install")

    monkeypatch.setattr(integration, "_install_instructions", die_before_instructions)
    with pytest.raises(SimulatedProcessDeath, match="before instruction install"):
        integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)

    root = _marketplace_root(home)
    receipt = integration._receipt_path(home)
    assert _receipt_payload(home)["install_transition"]["provider_before"] == {
        "marketplace": True,
        "plugin": True,
    }
    receipt_before = receipt.read_bytes()
    root_before = integration._tree_manifest(root)
    real_write_receipt = integration._write_receipt_state

    def fail_interrupted_checkpoint(
        target_home: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        context: str,
    ) -> None:
        if context == "interrupted-install provider checkpoint":
            raise OSError("injected interrupted checkpoint write failure")
        real_write_receipt(
            target_home,
            expected=expected,
            replacement=replacement,
            context=context,
        )

    monkeypatch.setattr(integration, "_write_receipt_state", fail_interrupted_checkpoint)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is False
    assert removed["provider_checkpoint_error"] is not None
    assert "injected interrupted checkpoint write failure" in removed["provider_checkpoint_error"]
    assert removed["provider_cleanup_error"] is None
    assert removed["integration_removed"] is False
    assert removed["uninstall_phase"] == "install_transition"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert receipt.read_bytes() == receipt_before
    assert integration._tree_manifest(root) == root_before


def test_preexisting_marketplace_and_plugin_are_rejected_without_mutation(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    vault = tmp_path / "vault"
    marketplace = _marketplace_root(home)
    marketplace.mkdir(parents=True)
    marker = marketplace / "user-owned.txt"
    marker.write_text("preserve", encoding="utf-8")
    fake_codex.marketplaces[integration.MARKETPLACE_NAME] = str(marketplace)
    fake_codex.plugins.add(integration.PLUGIN_ID)

    with pytest.raises(SetupError, match="not owned"):
        integration.install_codex(vault=vault, codex_home=home)
    removed = integration.uninstall_codex(codex_home=home)

    assert integration.PLUGIN_ID in fake_codex.plugins
    assert integration.MARKETPLACE_NAME in fake_codex.marketplaces
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert removed["cleanup_complete"] is False
    assert removed["manual_review_required"] is True
    assert removed["marketplace_files_state"] == "unowned_evidence"
    assert removed["preexisting_plugin_preserved"] is None
    assert removed["plugin_removed"] is None


def test_disabled_exact_plugin_install_preflight_fails_before_yield_or_mutation(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    root = _marketplace_root(home)
    yielded = False

    def disabled_plugin_provider(
        executable: str,
        arguments: list[str],
        codex_home: Path,
    ) -> dict[str, Any]:
        if arguments == ["plugin", "list", "--json"]:
            fake_codex.calls.append(tuple(arguments))
            return {
                "installed": [
                    {
                        "enabled": False,
                        "pluginId": integration.PLUGIN_ID,
                    }
                ]
            }
        return fake_codex.run(executable, arguments, codex_home)

    monkeypatch.setattr(integration, "_run_json", disabled_plugin_provider)
    with (
        pytest.raises(SetupError, match="plugin is disabled"),
        integration.install_codex_transaction(
            vault=tmp_path / "vault",
            codex_home=home,
        ) as result,
    ):
        yielded = True
        assert result.plugin_installed is not True

    assert yielded is False
    assert not root.exists()
    assert not (home / "AGENTS.md").exists()
    assert not integration._receipt_path(home).exists()
    assert not any("add" in call or "remove" in call for call in fake_codex.calls)
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_preexisting_plugin_without_marketplace_is_rejected_without_mutation(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    fake_codex.plugins.add(integration.PLUGIN_ID)

    with pytest.raises(SetupError, match="plugin is not owned"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {}
    assert not _marketplace_root(home).exists()
    assert not (home / "AGENTS.md").exists()


def test_local_marketplace_without_receipt_is_not_overwritten_or_adopted(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    marketplace = _marketplace_root(home)
    marketplace.mkdir(parents=True)
    marker = marketplace / "unowned.txt"
    marker.write_bytes(b"preserve exactly\n")

    with pytest.raises(SetupError, match="not owned by a valid receipt"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert marker.read_bytes() == b"preserve exactly\n"
    assert fake_codex.calls == []
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not integration._receipt_path(home).exists()


def test_incomplete_managed_markers_roll_back_only_new_components(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = f"# User file\n\n{integration.BLOCK_START}\nunfinished\n"
    agents.write_text(original, encoding="utf-8")

    with pytest.raises(ValidationError, match="incomplete"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert agents.read_text(encoding="utf-8") == original
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not integration._receipt_path(home).exists()


@pytest.mark.parametrize(
    "ambiguous",
    [
        f"{integration.MANAGED_BLOCK}\n\n{integration.MANAGED_BLOCK}\n",
        (
            f"{integration.BLOCK_START}\n"
            f"{integration.BLOCK_START}\nnested\n"
            f"{integration.BLOCK_END}\n"
            f"{integration.BLOCK_END}\n"
        ),
    ],
)
def test_uninstall_rejects_multiple_or_nested_managed_blocks(
    tmp_path: Path,
    fake_codex: FakeCodex,
    ambiguous: str,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    agents = home / "AGENTS.md"
    agents.write_text(ambiguous, encoding="utf-8")
    receipt = integration._receipt_path(home)
    calls_before = list(fake_codex.calls)

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["local_cleanup_verified"] is False
    assert removed["manual_review_required"] is True
    assert "multiple or nested" in _required_text(removed["local_cleanup_error"])
    assert removed["provider_cleanup_skipped"] is True
    assert receipt.exists()
    assert fake_codex.calls == calls_before
    assert agents.read_text(encoding="utf-8") == ambiguous


def test_instruction_preflight_failure_prevents_all_provider_calls(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    calls_before = list(fake_codex.calls)

    agents_before = (home / "AGENTS.md").read_bytes()

    def fail_verification(_: Path) -> integration._InstructionCleanupPlan:
        raise SetupError("injected instruction preflight failure")

    monkeypatch.setattr(integration, "_plan_instruction_removal", fail_verification)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["local_cleanup_verified"] is False
    assert removed["provider_cleanup_skipped"] is True
    assert removed["provider_cleanup_verified"] is False
    assert "preflight failure" in _required_text(removed["local_cleanup_error"])
    assert fake_codex.calls == calls_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert integration.MARKETPLACE_NAME in fake_codex.marketplaces
    assert Path(installed.marketplace_root).exists()
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert receipt.exists()


@pytest.mark.parametrize(
    "invalid_case",
    [
        "non_object",
        "missing_version",
        "boolean_version",
        "unsupported_version",
        "wrong_home",
        "non_boolean_ownership",
        "false_plugin_ownership",
        "false_marketplace_ownership",
        "false_both_ownership",
        "missing_owned_root",
        "invalid_owned_digest",
        "invalid_uninstall_phase",
        "phase_without_manifest",
        "manifest_without_phase",
        "invalid_manifest_path",
        "invalid_manifest_value",
    ],
)
def test_invalid_receipt_shape_or_schema_fails_before_any_uninstall_mutation(
    tmp_path: Path,
    fake_codex: FakeCodex,
    invalid_case: str,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    payload: object = json.loads(receipt.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    if invalid_case == "non_object":
        payload = []
    elif invalid_case == "missing_version":
        payload.pop("format_version")
    elif invalid_case == "boolean_version":
        payload["format_version"] = True
    elif invalid_case == "unsupported_version":
        payload["format_version"] = 99
    elif invalid_case == "wrong_home":
        payload["codex_home"] = str(tmp_path / "other-codex")
    elif invalid_case == "non_boolean_ownership":
        payload["plugin_owned"] = 1
    elif invalid_case == "false_plugin_ownership":
        payload["plugin_owned"] = False
    elif invalid_case == "false_marketplace_ownership":
        payload["marketplace_owned"] = False
    elif invalid_case == "false_both_ownership":
        payload["plugin_owned"] = False
        payload["marketplace_owned"] = False
    elif invalid_case == "missing_owned_root":
        payload.pop("marketplace_root")
    elif invalid_case == "invalid_owned_digest":
        payload["marketplace_digest"] = "not-a-sha256"
    elif invalid_case == "invalid_uninstall_phase":
        payload["uninstall_phase"] = "provider_done"
    elif invalid_case == "phase_without_manifest":
        payload["uninstall_phase"] = "provider_verified"
    elif invalid_case == "manifest_without_phase":
        payload["marketplace_manifest"] = {"owned.txt": "directory"}
    elif invalid_case == "invalid_manifest_path":
        payload["uninstall_phase"] = "provider_verified"
        payload["marketplace_manifest"] = {"../outside": "directory"}
    elif invalid_case == "invalid_manifest_value":
        payload["uninstall_phase"] = "provider_verified"
        payload["marketplace_manifest"] = {"owned.txt": "file:not-a-digest"}
    invalid = (json.dumps(payload, sort_keys=True) + "\n").encode()
    receipt.write_bytes(invalid)
    agents_before = (home / "AGENTS.md").read_bytes()
    marketplace_digest = integration._tree_digest(marketplace)
    provider_calls = list(fake_codex.calls)

    with pytest.raises(ValidationError, match="receipt"):
        integration.uninstall_codex(codex_home=home)

    assert receipt.read_bytes() == invalid
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert integration._tree_digest(marketplace) == marketplace_digest
    assert fake_codex.calls == provider_calls
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace.resolve())}


def test_v1_receipt_is_loaded_and_next_install_emits_strict_v2(
    tmp_path: Path,
    fake_codex: FakeCodex,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    _downgrade_receipt_to_v1(home)

    loaded = integration._load_receipt(home)

    assert loaded["format_version"] == 1
    assert loaded["integration_active"] is True
    assert loaded["cleanup_pending"] == []

    integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)
    emitted = _receipt_payload(home)

    assert set(emitted) == {
        "cleanup_pending",
        "codex_home",
        "format_version",
        "integration_active",
        "marketplace_digest",
        "marketplace_manifest",
        "marketplace_owned",
        "marketplace_root",
        "plugin_owned",
    }
    assert emitted["format_version"] == 2
    assert emitted["integration_active"] is True
    assert [record["kind"] for record in emitted["cleanup_pending"]] == ["old"]
    assert _retained_paths(home)[0].is_dir()


@pytest.mark.parametrize(
    "invalid_case",
    ["short_token", "kind_prefix_mismatch", "nested_basename"],
)
def test_generated_and_loaded_v2_receipts_reject_unsafe_retained_records(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    root = _marketplace_root(home)
    token = "a" * 32
    manifest = {"owned": "directory"}
    invalid_record: dict[str, Any]
    if invalid_case == "short_token":
        invalid_record = {
            "basename": f".{root.name}.remove-short",
            "kind": "remove",
            "manifest": manifest,
        }
    elif invalid_case == "kind_prefix_mismatch":
        invalid_record = {
            "basename": f".{root.name}.old-{token}",
            "kind": "remove",
            "manifest": manifest,
        }
    else:
        invalid_record = {
            "basename": f".{root.name}.remove-{token}/nested",
            "kind": "remove",
            "manifest": manifest,
        }

    with pytest.raises(ValidationError, match=r"receipt|retained"):
        integration._receipt_bytes(
            home=home,
            marketplace_root=root,
            active_manifest=None,
            cleanup_pending=[invalid_record],
        )

    valid_record = {
        "basename": f".{root.name}.remove-{token}",
        "kind": "remove",
        "manifest": manifest,
    }
    encoded = integration._receipt_bytes(
        home=home,
        marketplace_root=root,
        active_manifest=None,
        cleanup_pending=[valid_record],
    )
    loaded_payload = json.loads(encoded.decode("utf-8"))
    loaded_payload["cleanup_pending"] = [invalid_record]
    receipt = integration._receipt_path(home)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(loaded_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match=r"receipt|retained"):
        integration._load_receipt(home)


def test_failed_final_status_restores_existing_agents_and_removes_new_backup(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = b"# User instructions\n\nPreserve byte for byte.\n"
    agents.write_bytes(original)
    monkeypatch.setattr(
        integration,
        "codex_status",
        lambda **_: {"plugin_installed": False, "instructions_installed": True},
    )

    with pytest.raises(SetupError, match="did not report"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert agents.read_bytes() == original
    assert not (home / "AGENTS.md.gsv-backup").exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_caller_failure_inside_staged_install_rolls_back_integration(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = b"# Existing instructions\n"
    agents.write_bytes(original)

    with (
        pytest.raises(SetupError, match="Bridge failed"),
        integration.install_codex_transaction(vault=tmp_path / "vault", codex_home=home),
    ):
        raise SetupError("Bridge failed after Codex verification")

    assert agents.read_bytes() == original
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    recovery = _receipt_payload(home)
    assert recovery["integration_active"] is False
    assert [record["kind"] for record in recovery["cleanup_pending"]] == ["failed"]
    retained = _retained_paths(home)
    assert len(retained) == 1
    assert retained[0].is_dir()
    first = integration.uninstall_codex(codex_home=home)
    second = integration.uninstall_codex(codex_home=home)
    assert first == second
    assert first["cleanup_complete"] is False
    assert first["integration_removed"] is True
    assert first["recovery_retained"] is True


def test_process_death_after_instruction_write_never_orphans_a_backup_file(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = b"# Existing instructions\n"
    agents.write_bytes(original)
    real_status = integration.codex_status

    def die_during_status(**_kwargs: object) -> dict[str, Any]:
        raise SimulatedProcessDeath("simulated death after instruction write")

    monkeypatch.setattr(integration, "codex_status", die_during_status)
    with pytest.raises(SimulatedProcessDeath, match="after instruction write"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert integration.BLOCK_START.encode() in agents.read_bytes()
    assert not (home / "AGENTS.md.gsv-backup").exists()

    monkeypatch.setattr(integration, "codex_status", real_status)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["integration_removed"] is True
    assert removed["cleanup_complete"] is False
    assert agents.read_bytes() == original
    assert not (home / "AGENTS.md.gsv-backup").exists()


def test_failed_status_preserves_preexisting_components(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    marketplace = _marketplace_root(home)
    marketplace.mkdir(parents=True)
    marker = marketplace / "preexisting.txt"
    marker.write_text("preserve", encoding="utf-8")
    fake_codex.marketplaces[integration.MARKETPLACE_NAME] = str(marketplace)
    fake_codex.plugins.add(integration.PLUGIN_ID)
    monkeypatch.setattr(
        integration,
        "codex_status",
        lambda **_: {"plugin_installed": False, "instructions_installed": True},
    )

    with pytest.raises(SetupError):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert integration.MARKETPLACE_NAME in fake_codex.marketplaces
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not (home / "AGENTS.md").exists()


def test_failed_reinstall_restores_previous_marketplace_bytes(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    first_vault = tmp_path / "first-vault"
    second_vault = tmp_path / "second-vault"
    _write_imported_skill(first_vault)
    _write_imported_skill(second_vault)
    installed = integration.install_codex(vault=first_vault, codex_home=home)
    manifest = Path(installed.marketplace_root) / "plugins/gsv/.mcp.json"
    installed_skill = Path(installed.marketplace_root) / "plugins/gsv/skills/resident-exact"
    before = manifest.read_bytes()
    real_status = integration.codex_status
    monkeypatch.setattr(
        integration,
        "codex_status",
        lambda **_: {"plugin_installed": False, "instructions_installed": True},
    )

    with pytest.raises(SetupError):
        integration.install_codex(vault=second_vault, codex_home=home)

    assert manifest.read_bytes() == before
    if os.name != "nt":
        assert stat.S_IMODE((installed_skill / "references/context.md").stat().st_mode) == 0o600
        assert stat.S_IMODE((installed_skill / "scripts/check.py").stat().st_mode) == 0o700
    assert real_status(codex_home=home)["manifest_verified"] is True
    payload = json.loads(before)
    assert payload["mcpServers"]["gsv"]["env"]["GSV_VAULT"] == str(first_vault.resolve())
    assert payload["mcpServers"]["gsv"]["env"][integration.GSV_DATA_DIR_ENV] == str(data_dir())


def test_first_install_visible_transition_write_error_is_accepted_by_readback(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    vault = tmp_path / "vault"
    receipt = integration._receipt_path(home)
    marketplace = _marketplace_root(home)
    real_atomic_write = atomic_module.atomic_write
    failed = False

    def commit_receipt_then_fail(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal failed
        real_atomic_write(path, content, mode=mode)
        if path == receipt and not failed:
            failed = True
            raise OSError("injected first-install receipt durability failure")

    monkeypatch.setattr(integration, "atomic_write", commit_receipt_then_fail)
    installed = integration.install_codex(vault=vault, codex_home=home)
    payload = _receipt_payload(home)

    assert failed is True
    assert payload["format_version"] == 2
    assert payload["integration_active"] is True
    assert "install_transition" not in payload
    assert marketplace.is_dir()
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace)}

    monkeypatch.setattr(integration, "atomic_write", real_atomic_write)
    removed = integration.uninstall_codex(codex_home=home)

    assert installed.plugin_installed is True
    assert removed["cleanup_complete"] is False
    assert removed["recovery_retained"] is True
    assert receipt.exists()


def test_reinstall_visible_transition_write_error_is_accepted_by_readback(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    first_vault = tmp_path / "first-vault"
    second_vault = tmp_path / "second-vault"
    installed = integration.install_codex(vault=first_vault, codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / "plugins/gsv/.mcp.json"
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    manifest_before = manifest.read_bytes()
    agents_before = agents.read_bytes()
    digest_before = integration._tree_digest(marketplace)
    real_atomic_write = atomic_module.atomic_write
    failed = False

    def commit_receipt_then_fail(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal failed
        real_atomic_write(path, content, mode=mode)
        if path == receipt and content != receipt_before and not failed:
            failed = True
            raise OSError("injected update receipt durability failure")

    monkeypatch.setattr(integration, "atomic_write", commit_receipt_then_fail)
    reinstalled = integration.install_codex(vault=second_vault, codex_home=home)
    updated_manifest = json.loads(
        (Path(reinstalled.marketplace_root) / "plugins/gsv/.mcp.json").read_text(encoding="utf-8")
    )
    payload = _receipt_payload(home)
    assert failed is True
    assert receipt.read_bytes() != receipt_before
    assert manifest.read_bytes() != manifest_before
    assert agents.read_bytes() == agents_before
    assert integration._tree_digest(marketplace) != digest_before
    assert payload["integration_active"] is True
    assert "install_transition" not in payload
    assert len(payload["cleanup_pending"]) == 1
    assert _retained_paths(home)[0].is_dir()
    assert updated_manifest["mcpServers"]["gsv"]["env"]["GSV_VAULT"] == str(second_vault.resolve())
    assert updated_manifest["mcpServers"]["gsv"]["env"][integration.GSV_DATA_DIR_ENV] == str(
        data_dir()
    )
    monkeypatch.setattr(integration, "atomic_write", real_atomic_write)
    assert integration.uninstall_codex(codex_home=home)["cleanup_complete"] is False


def test_existing_managed_block_is_restored_exactly_on_failure(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = (
        "# Before\n\n"
        f"{integration.BLOCK_START}\nold managed content\n{integration.BLOCK_END}\n\n"
        "# After\n"
    ).encode()
    agents.write_bytes(original)
    monkeypatch.setattr(
        integration,
        "codex_status",
        lambda **_: {"plugin_installed": False, "instructions_installed": True},
    )

    with pytest.raises(SetupError):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert agents.read_bytes() == original


def test_conflicting_marketplace_root_fails_without_plugin_or_instruction_mutation(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    fake_codex.marketplaces[integration.MARKETPLACE_NAME] = str(tmp_path / "someone-else")

    with pytest.raises(SetupError, match="already points somewhere else"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert fake_codex.plugins == set()
    assert not (home / "AGENTS.md").exists()


def _launch_generated_manifest(marketplace: Path) -> dict[str, Any]:
    payload = json.loads((marketplace / "plugins/gsv/.mcp.json").read_text(encoding="utf-8"))
    server = payload["mcpServers"]["gsv"]
    environment = os.environ.copy()
    for name in (integration.GSV_DATA_DIR_ENV, "GSV_VAULT", whatsapp.SERVICE_LABEL_ENV):
        environment.pop(name, None)
    environment.update(server["env"])
    process = subprocess.Popen(
        [server["command"], *server["args"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    process.stdin.close()
    assert process.wait(timeout=10) == 0, process.stderr.read() if process.stderr else ""
    return {"response": json.loads(line), "server": server}


def _prepare_test_marketplace(
    root: Path,
    vault: Path,
    runtime: tuple[str, list[str]],
) -> Path:
    change = integration._replace_marketplace(
        vault,
        runtime=runtime,
        target=root / "marketplace",
        before_moves=lambda _: None,
    )
    return change.path


def test_exact_source_runtime_manifest_executes(tmp_path: Path) -> None:
    marketplace = _prepare_test_marketplace(
        tmp_path,
        tmp_path / "vault",
        (sys.executable, ["-m", "continuity_kernel"]),
    )

    result = _launch_generated_manifest(marketplace)

    assert result["server"]["command"] == sys.executable
    assert result["server"]["args"] == ["-m", "continuity_kernel", "mcp", "serve"]
    assert result["server"]["env"][integration.GSV_DATA_DIR_ENV] == str(data_dir())
    assert result["response"]["result"]["serverInfo"]["name"] == "gsv"


@pytest.mark.skipif(os.name == "nt", reason="executable shebang fixture is POSIX-specific")
def test_exact_frozen_runtime_manifest_executes_without_python_module_args(tmp_path: Path) -> None:
    launcher = tmp_path / "gsv-frozen"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from continuity_kernel.mcp_server import serve\n"
        "if sys.argv[1:] != ['mcp', 'serve']:\n"
        "    raise SystemExit(64)\n"
        "raise SystemExit(serve())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    marketplace = _prepare_test_marketplace(
        tmp_path,
        tmp_path / "vault",
        (str(launcher), []),
    )

    result = _launch_generated_manifest(marketplace)

    assert result["server"]["command"] == str(launcher)
    assert result["server"]["args"] == ["mcp", "serve"]
    assert result["response"]["result"]["serverInfo"]["name"] == "gsv"


def test_runtime_command_switches_for_frozen_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("continuity_kernel.codex_integration.sys.frozen", True, raising=False)

    command, arguments = integration._runtime_command()

    assert command == sys.executable
    assert arguments == []


def test_codex_timeout_is_reported_as_setup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*_: object, **__: object) -> object:
        raise subprocess.TimeoutExpired("codex", 60)

    monkeypatch.setattr("continuity_kernel.codex_integration.subprocess.run", timeout)

    with pytest.raises(SetupError, match="timed out"):
        integration._run_json("codex", ["plugin", "list", "--json"], tmp_path)


@pytest.mark.parametrize("failure_call", [1, 3, 6])
def test_provider_failure_at_any_stage_keeps_receipt_and_retries_cleanly(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    agents_before = (home / "AGENTS.md").read_bytes()
    calls = 0

    def fail_provider_stage(
        executable: str, arguments: list[str], codex_home: Path
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            if failure_call > 1:
                fake_codex.run(executable, arguments, codex_home)
            raise SetupError(f"injected provider failure at call {failure_call}")
        return fake_codex.run(executable, arguments, codex_home)

    monkeypatch.setattr(integration, "_run_json", fail_provider_stage)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["codex_available"] is True
    assert removed["provider_cleanup_verified"] is False
    assert removed["provider_cleanup_skipped"] is False
    assert removed["provider_cleanup_error"] == (
        f"injected provider failure at call {failure_call}"
    )
    assert removed["plugin_removed"] is None
    assert removed["marketplace_removed"] is None
    assert removed["marketplace_files_state"] == "verified_present"
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert "Re-run `gsv codex uninstall`" in _required_text(removed["next"])
    assert receipt.exists()
    assert Path(installed.marketplace_root).exists()
    assert (home / "AGENTS.md").read_bytes() == agents_before

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is False
    assert retried["marketplace_files_state"] == "retained"
    assert retried["integration_removed"] is True
    assert retried["recovery_retained"] is True
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert receipt.exists()
    assert all(path.is_dir() for path in _retained_paths(home))


def test_uninstall_without_codex_preserves_provider_files_and_retry_receipt(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = b"# Existing instructions\n\nKeep this exactly.\n"
    agents.write_bytes(original)
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    installed_agents = agents.read_bytes()

    def unavailable() -> str:
        raise SetupError("Codex executable is unavailable")

    monkeypatch.setattr(integration, "_codex_executable", unavailable)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["codex_available"] is False
    assert removed["codex_home"] == str(home.resolve())
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert removed["instructions_removed"] is False
    assert removed["local_cleanup_verified"] is False
    assert removed["marketplace_files_removed"] is False
    assert removed["marketplace_files_state"] == "verified_present"
    assert removed["marketplace_removed"] is None
    assert removed["plugin_removed"] is None
    assert "Re-run `gsv codex uninstall`" in _required_text(removed["next"])
    assert removed["provider_cleanup_error"] == "Codex executable is unavailable"
    assert removed["provider_cleanup_verified"] is False
    assert removed["receipt_preserved_for_retry"] is True
    assert removed["registration_cleanup_deferred"] is True
    assert removed["user_data_preserved"] is True
    assert agents.read_bytes() == installed_agents
    assert marketplace.exists()
    assert receipt.exists()
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace.resolve())}

    monkeypatch.setattr(integration, "_codex_executable", lambda: "fake-codex")
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["registration_cleanup_deferred"] is False
    assert retried["plugin_removed"] is True
    assert retried["marketplace_removed"] is True
    assert retried["marketplace_files_removed"] is False
    assert retried["marketplace_files_state"] == "retained"
    assert retried["integration_removed"] is True
    assert retried["recovery_retained"] is True
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert agents.read_bytes() == original
    assert receipt.exists()
    assert all(path.is_dir() for path in _retained_paths(home))


def test_uninstall_without_codex_or_receipt_is_unverified_and_non_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = b"# User-owned instructions\n"
    agents.write_bytes(original)

    def unavailable() -> str:
        raise SetupError("Codex executable is unavailable")

    monkeypatch.setattr(integration, "_codex_executable", unavailable)
    first = integration.uninstall_codex(codex_home=home)
    second = integration.uninstall_codex(codex_home=home)

    assert first == second
    assert first["cleanup_complete"] is False
    assert first["manual_review_required"] is True
    assert first["registration_cleanup_deferred"] is False
    assert first["deferred_registrations"] == []
    assert "Restore the matching ownership receipt" in _required_text(first["next"])
    assert first["instructions_removed"] is False
    assert agents.read_bytes() == original
    assert not integration._receipt_path(home).exists()


def test_missing_receipt_result_has_the_full_common_uninstall_shape(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    installed_home = tmp_path / "installed-codex"
    installed_home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=installed_home)
    complete = integration.uninstall_codex(codex_home=installed_home)

    missing_home = tmp_path / "missing-receipt-codex"
    missing_home.mkdir()
    (missing_home / "AGENTS.md").write_text(
        f"{integration.BLOCK_START}\nmanaged\n{integration.BLOCK_END}\n",
        encoding="utf-8",
    )
    missing = integration.uninstall_codex(codex_home=missing_home)

    assert set(missing) == set(complete)
    assert missing["integration_removed"] is False
    assert missing["recovery_retained"] is False
    assert missing["retained_cleanup_paths"] == []


def test_uninstall_retains_recovery_catalog_when_registrations_are_already_absent(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    fake_codex.plugins.clear()
    fake_codex.marketplaces.clear()

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["codex_available"] is True
    assert removed["cleanup_complete"] is False
    assert removed["registration_cleanup_deferred"] is False
    assert removed["plugin_removed"] is False
    assert removed["marketplace_removed"] is False
    assert removed["marketplace_files_removed"] is False
    assert removed["marketplace_files_state"] == "retained"
    assert removed["integration_removed"] is True
    assert removed["recovery_retained"] is True
    assert not Path(installed.marketplace_root).exists()
    assert not (home / "AGENTS.md").exists()
    assert integration._receipt_path(home).exists()
    assert all(path.is_dir() for path in _retained_paths(home))


def test_legacy_missing_local_state_is_repaired_for_manifest_dependent_provider(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    receipt = integration._receipt_path(home)
    _downgrade_receipt_to_v1(home)
    (home / "AGENTS.md").unlink()
    shutil.rmtree(marketplace)
    calls_before = len(fake_codex.calls)
    fake_codex.required_manifest = manifest

    removed = integration.uninstall_codex(codex_home=home)
    uninstall_calls = fake_codex.calls[calls_before:]

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["local_cleanup_verified"] is True
    assert removed["marketplace_files_state"] == "retained"
    assert removed["plugin_removed"] is True
    assert removed["marketplace_removed"] is True
    assert receipt.exists()
    assert _receipt_payload(home)["integration_active"] is False
    assert not marketplace.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not any("add" in command for command in uninstall_calls)


def test_legacy_repair_scaffold_survives_provider_failure_and_retries(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    receipt = integration._receipt_path(home)
    _downgrade_receipt_to_v1(home)
    (home / "AGENTS.md").unlink()
    shutil.rmtree(marketplace)
    fake_codex.required_manifest = manifest
    real_run = fake_codex.run
    failed = False

    def fail_first_list(executable: str, arguments: list[str], codex_home: Path) -> dict[str, Any]:
        nonlocal failed
        if not failed:
            failed = True
            assert manifest.is_file()
            raise SetupError("injected provider retry")
        return real_run(executable, arguments, codex_home)

    monkeypatch.setattr(integration, "_run_json", fail_first_list)
    first = integration.uninstall_codex(codex_home=home)

    assert first["cleanup_complete"] is False
    assert first["provider_checkpointed"] is False
    assert first["marketplace_files_state"] == "changed_or_unsafe"
    assert first["uninstall_phase"] == "install_transition"
    assert manifest.is_file()
    assert receipt.exists()

    monkeypatch.setattr(integration, "_run_json", real_run)
    second = integration.uninstall_codex(codex_home=home)

    assert second["cleanup_complete"] is False
    assert second["provider_checkpointed"] is True
    assert not marketplace.exists()
    assert receipt.exists()
    assert _receipt_payload(home)["integration_active"] is False


def test_legacy_partial_scaffold_after_process_death_is_rematerialized_for_provider(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    _downgrade_receipt_to_v1(home)
    (home / "AGENTS.md").unlink()
    shutil.rmtree(marketplace)
    real_write = integration._write_exclusive
    writes = 0

    def die_after_first_scaffold_file(path: Path, content: bytes) -> None:
        nonlocal writes
        real_write(path, content)
        writes += 1
        if writes == 1:
            raise SimulatedProcessDeath("simulated death during partial legacy scaffold")

    monkeypatch.setattr(integration, "_write_exclusive", die_after_first_scaffold_file)
    with pytest.raises(SimulatedProcessDeath, match="partial legacy scaffold"):
        integration.uninstall_codex(codex_home=home)

    recovery = _receipt_payload(home)
    transition = recovery["install_transition"]
    partial_record = transition["repair"]
    partial_path = marketplace.parent / partial_record["basename"]
    partial_manifest = integration._tree_manifest(partial_path)
    assert partial_manifest
    assert partial_manifest != partial_record["manifest"]
    assert integration._manifest_is_owned_subset(
        partial_manifest,
        partial_record["manifest"],
    )
    assert not marketplace.exists()

    monkeypatch.setattr(integration, "_write_exclusive", real_write)
    fake_codex.required_manifest = manifest
    calls_before = len(fake_codex.calls)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is True
    assert removed["integration_removed"] is True
    assert removed["marketplace_files_state"] == "retained"
    assert not marketplace.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert any("remove" in call for call in fake_codex.calls[calls_before:])
    final_receipt = _receipt_payload(home)
    assert final_receipt["integration_active"] is False
    final_paths = [Path(path) for path in removed["retained_cleanup_paths"]]
    assert final_paths
    assert any(
        all(manifest.get(relative) == digest for relative, digest in partial_manifest.items())
        for manifest in (integration._tree_manifest(path) for path in final_paths)
    )


def test_provider_repair_reserves_partial_scaffold_and_remove_records(
    tmp_path: Path,
    fake_codex: FakeCodex,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    historical = _fill_cleanup_capacity(
        home,
        count=integration.MAX_CLEANUP_RECORDS - 1,
    )
    receipt_before = integration._receipt_path(home).read_bytes()
    calls_before = list(fake_codex.calls)
    shutil.rmtree(marketplace)

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_skipped"] is True
    assert "needs two free records" in _required_text(removed["local_cleanup_error"])
    assert removed["uninstall_phase"] is None
    assert removed["marketplace_files_state"] == "already_missing"
    assert "Provider repair was not started" in _required_text(removed["next"])
    assert str(historical[0]) in _required_text(removed["next"])
    assert integration._receipt_path(home).read_bytes() == receipt_before
    assert fake_codex.calls == calls_before
    assert all(path.is_dir() for path in historical)
    assert not list(marketplace.parent.glob(f".{marketplace.name}.repair-*"))


def test_provider_repair_at_two_free_slots_survives_partial_scaffold_crash(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    provider_manifest = marketplace / ".agents/plugins/marketplace.json"
    historical = _fill_cleanup_capacity(
        home,
        count=integration.MAX_CLEANUP_RECORDS - 2,
    )
    shutil.rmtree(marketplace)
    real_write = integration._write_exclusive
    writes = 0

    def die_after_first_scaffold_file(path: Path, content: bytes) -> None:
        nonlocal writes
        real_write(path, content)
        writes += 1
        if writes == 1:
            raise SimulatedProcessDeath("simulated capacity-bound scaffold crash")

    monkeypatch.setattr(integration, "_write_exclusive", die_after_first_scaffold_file)
    with pytest.raises(SimulatedProcessDeath, match="capacity-bound scaffold crash"):
        integration.uninstall_codex(codex_home=home)

    transition_receipt = _receipt_payload(home)
    transition = transition_receipt["install_transition"]
    repair = marketplace.parent / transition["repair"]["basename"]
    assert len(transition_receipt["cleanup_pending"]) == integration.MAX_CLEANUP_RECORDS - 2
    assert repair.is_dir()
    assert all(path.is_dir() for path in historical)

    monkeypatch.setattr(integration, "_write_exclusive", real_write)
    # A separately recovered public provider tree can safely coexist with the
    # partial, receipt-tracked repair: it must match the exact packaged repair
    # manifest, while the partial tree remains immutable recovery evidence.
    marketplace.mkdir()
    for relative, content in integration._legacy_repair_contents().items():
        target = marketplace.joinpath(*PurePosixPath(relative).parts)
        if content is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    assert integration._tree_manifest(marketplace) == transition["repair"]["manifest"]
    fake_codex.required_manifest = provider_manifest
    resumed = integration.uninstall_codex(codex_home=home)

    assert resumed["cleanup_complete"] is False
    assert resumed["provider_cleanup_verified"] is True
    assert resumed["integration_removed"] is True
    recovered_records = _receipt_payload(home)["cleanup_pending"]
    assert len(recovered_records) == integration.MAX_CLEANUP_RECORDS
    assert sum(record["kind"] == "repair" for record in recovered_records) == 1
    assert sum(record["kind"] == "remove" for record in recovered_records) == 1
    assert repair.is_dir()
    assert not marketplace.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_failed_legacy_scaffold_stage_is_receipt_tracked_and_resumes_without_deletion(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    _downgrade_receipt_to_v1(home)
    (home / "AGENTS.md").unlink()
    shutil.rmtree(marketplace)
    calls_before = list(fake_codex.calls)
    real_write = integration._write_exclusive
    writes = 0

    def fail_second_write(path: Path, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected staged scaffold write failure")
        real_write(path, content)

    monkeypatch.setattr(integration, "_write_exclusive", fail_second_write)
    first = integration.uninstall_codex(codex_home=home)

    assert first["cleanup_complete"] is False
    assert first["provider_cleanup_skipped"] is True
    assert not marketplace.exists()
    assert receipt.exists()
    assert fake_codex.calls == calls_before
    recovery = _receipt_payload(home)
    assert recovery["install_transition"]["purpose"] == "provider_repair"
    repair_record = recovery["install_transition"]["repair"]
    repair = marketplace.parent / repair_record["basename"]
    partial_manifest = integration._tree_manifest(repair)
    assert partial_manifest
    assert partial_manifest != repair_record["manifest"]
    assert integration._manifest_is_owned_subset(
        partial_manifest,
        repair_record["manifest"],
    )
    assert first["recovery_retained"] is True
    assert str(repair) in first["retained_cleanup_paths"]

    monkeypatch.setattr(integration, "_write_exclusive", real_write)
    second = integration.uninstall_codex(codex_home=home)

    assert second["cleanup_complete"] is False
    assert second["recovery_retained"] is True
    assert not marketplace.exists()
    assert receipt.exists()
    final_receipt = _receipt_payload(home)
    assert final_receipt["integration_active"] is False
    final_paths = [Path(path) for path in second["retained_cleanup_paths"]]
    assert final_paths
    assert all(path.is_dir() for path in final_paths)
    assert any(
        all(manifest.get(relative) == digest for relative, digest in partial_manifest.items())
        for manifest in (integration._tree_manifest(path) for path in final_paths)
    )


def test_legacy_scaffold_directory_fsync_failure_prevents_provider_calls(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    _downgrade_receipt_to_v1(home)
    (home / "AGENTS.md").unlink()
    shutil.rmtree(marketplace)
    calls_before = list(fake_codex.calls)

    def fail_directory_fsync(root: Path, manifest: dict[str, str]) -> None:
        assert root.name.startswith(f".{marketplace.name}.repair-")
        assert manifest == integration._legacy_repair_manifest()
        raise OSError("injected staged directory fsync failure")

    monkeypatch.setattr(integration, "_fsync_staged_directories", fail_directory_fsync)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_skipped"] is True
    assert removed["provider_cleanup_verified"] is False
    assert "directory fsync failure" in _required_text(removed["local_cleanup_error"])
    assert not marketplace.exists()
    assert receipt.exists()
    assert fake_codex.calls == calls_before
    repairs = list(marketplace.parent.glob(f".{marketplace.name}.repair-*"))
    assert len(repairs) == 1
    assert (repairs[0] / integration.LEGACY_REPAIR_MARKER_NAME).is_file()


def test_legacy_scaffold_publish_preserves_race_created_final_root(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    _downgrade_receipt_to_v1(home)
    (home / "AGENTS.md").unlink()
    shutil.rmtree(marketplace)
    calls_before = list(fake_codex.calls)
    real_publish = integration._publish_directory_new
    sentinel = marketplace / "race-created-user-file.txt"

    def race_publish(source: Path, target: Path) -> None:
        target.mkdir()
        sentinel.write_bytes(b"preserve exact race bytes\n")
        real_publish(source, target)

    monkeypatch.setattr(integration, "_publish_directory_new", race_publish)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_skipped"] is True
    assert removed["manual_review_required"] is True
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert sentinel.read_bytes() == b"preserve exact race bytes\n"
    assert receipt.exists()
    assert fake_codex.calls == calls_before
    recovery = _receipt_payload(home)
    repair_record = recovery["install_transition"]["repair"]
    repair = marketplace.parent / repair_record["basename"]
    assert integration._tree_manifest(repair) == repair_record["manifest"]
    assert removed["recovery_retained"] is True
    assert str(repair) in removed["retained_cleanup_paths"]


def test_provider_cleanup_runs_before_verified_marketplace_files_are_quarantined(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    agents = home / "AGENTS.md"
    agents_before = agents.read_bytes()
    digest_before = integration._tree_digest(marketplace)
    fake_codex.required_manifest = manifest

    def require_manifest(executable: str, arguments: list[str], codex_home: Path) -> dict[str, Any]:
        assert manifest.is_file()
        assert agents.read_bytes() == agents_before
        assert integration._tree_digest(marketplace) == digest_before
        return fake_codex.run(executable, arguments, codex_home)

    monkeypatch.setattr(integration, "_run_json", require_manifest)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["marketplace_files_state"] == "retained"
    assert not marketplace.exists()
    assert len(_retained_paths(home)) == 1
    assert _retained_paths(home)[0].is_dir()
    assert not agents.exists()


def test_provider_reappearance_before_checkpoint_is_deferred_and_preserves_local_root(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    marketplace_before = integration._tree_manifest(marketplace)
    agents_before = (home / "AGENTS.md").read_bytes()
    provider_calls = 0

    def reappear_after_final_remove_list(
        executable: str,
        arguments: list[str],
        codex_home: Path,
    ) -> dict[str, Any]:
        nonlocal provider_calls
        result = fake_codex.run(executable, arguments, codex_home)
        provider_calls += 1
        if provider_calls == 6:
            assert arguments == ["plugin", "marketplace", "list", "--json"]
            assert fake_codex.plugins == set()
            assert fake_codex.marketplaces == {}
            fake_codex.plugins.add(integration.PLUGIN_ID)
            fake_codex.marketplaces[integration.MARKETPLACE_NAME] = str(marketplace.resolve())
        return result

    monkeypatch.setattr(integration, "_run_json", reappear_after_final_remove_list)
    removed = integration.uninstall_codex(codex_home=home)

    assert provider_calls == 8
    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is False
    assert removed["provider_checkpointed"] is False
    assert removed["registration_cleanup_deferred"] is True
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert "provider state changed before checkpoint" in _required_text(
        removed["provider_cleanup_error"]
    )
    assert "could not persist provider completion" in _required_text(
        removed["provider_checkpoint_error"]
    )
    assert removed["instructions_removed"] is False
    assert removed["marketplace_files_state"] == "verified_present"
    assert integration._tree_manifest(marketplace) == marketplace_before
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert receipt.read_bytes() == receipt_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace.resolve())}


def test_disabled_plugin_reappearance_before_checkpoint_preserves_local_state(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    receipt_before = receipt.read_bytes()
    marketplace_before = integration._tree_manifest(marketplace)
    agents_before = (home / "AGENTS.md").read_bytes()
    provider_calls = 0
    disabled_plugin_visible = False

    def disabled_plugin_appears_after_final_removal_lists(
        executable: str,
        arguments: list[str],
        codex_home: Path,
    ) -> dict[str, Any]:
        nonlocal provider_calls, disabled_plugin_visible
        result = fake_codex.run(executable, arguments, codex_home)
        provider_calls += 1
        if provider_calls == 6:
            assert arguments == ["plugin", "marketplace", "list", "--json"]
            assert fake_codex.plugins == set()
            assert fake_codex.marketplaces == {}
            disabled_plugin_visible = True
        if (
            disabled_plugin_visible
            and arguments == ["plugin", "list", "--json"]
            and provider_calls > 6
        ):
            return {
                "installed": [
                    {
                        "enabled": False,
                        "pluginId": integration.PLUGIN_ID,
                    }
                ]
            }
        return result

    monkeypatch.setattr(
        integration,
        "_run_json",
        disabled_plugin_appears_after_final_removal_lists,
    )
    removed = integration.uninstall_codex(codex_home=home)

    assert provider_calls == 8
    assert disabled_plugin_visible is True
    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is False
    assert removed["provider_checkpointed"] is False
    assert removed["registration_cleanup_deferred"] is True
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert "provider state changed before checkpoint" in _required_text(
        removed["provider_cleanup_error"]
    )
    assert "plugin registration changed" in _required_text(removed["provider_cleanup_error"])
    assert "could not persist provider completion" in _required_text(
        removed["provider_checkpoint_error"]
    )
    assert removed["instructions_removed"] is False
    assert removed["marketplace_files_state"] == "verified_present"
    assert integration._tree_manifest(marketplace) == marketplace_before
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert receipt.read_bytes() == receipt_before
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_provider_checkpoint_failure_leaves_local_bytes_for_provider_reverification(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    agents_before = agents.read_bytes()
    marketplace_before = integration._tree_digest(marketplace)
    receipt_before = receipt.read_bytes()
    real_atomic_write = atomic_module.atomic_write

    def fail_checkpoint(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        if path == receipt and b'"uninstall_phase": "provider_verified"' in content:
            raise OSError("injected checkpoint write failure")
        real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(integration, "atomic_write", fail_checkpoint)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is False
    assert "checkpoint write failed before commit" in _required_text(
        removed["provider_checkpoint_error"]
    )
    assert agents.read_bytes() == agents_before
    assert integration._tree_digest(marketplace) == marketplace_before
    assert receipt.read_bytes() == receipt_before
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}

    monkeypatch.setattr(integration, "atomic_write", real_atomic_write)
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is False
    assert retried["provider_checkpointed"] is True
    assert not agents.exists()
    assert not marketplace.exists()
    assert receipt.exists()
    assert _receipt_payload(home)["integration_active"] is False


def test_provider_checkpoint_postcommit_error_is_accepted_by_exact_readback(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    real_atomic_write = atomic_module.atomic_write
    raised = False

    def commit_then_raise(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal raised
        real_atomic_write(path, content, mode=mode)
        if not raised and path == receipt and b'"uninstall_phase": "provider_verified"' in content:
            raised = True
            raise OSError("injected post-commit checkpoint error")

    monkeypatch.setattr(integration, "atomic_write", commit_then_raise)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_checkpointed"] is True
    assert removed["provider_checkpoint_error"] is None
    assert not marketplace.exists()
    assert receipt.exists()
    assert _receipt_payload(home)["integration_active"] is False
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_visible_but_unconfirmed_provider_checkpoint_never_deletes_local_state(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    agents_before = agents.read_bytes()
    marketplace_before = integration._tree_manifest(marketplace)
    real_atomic_write = atomic_module.atomic_write

    def commit_checkpoint_then_fail(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        real_atomic_write(path, content, mode=mode)
        if path == receipt and b'"uninstall_phase": "provider_verified"' in content:
            raise OSError("injected parent fsync failure")

    def durability_unconfirmed(path: Path, expected: bytes) -> None:
        del path, expected
        raise OSError("injected durability reconfirmation failure")

    monkeypatch.setattr(integration, "atomic_write", commit_checkpoint_then_fail)
    monkeypatch.setattr(integration, "_confirm_receipt_durable", durability_unconfirmed)
    monkeypatch.setattr(
        integration,
        "_apply_instruction_removal",
        lambda _: (_ for _ in ()).throw(AssertionError("instructions must not be removed")),
    )
    monkeypatch.setattr(
        shutil,
        "rmtree",
        lambda _: (_ for _ in ()).throw(AssertionError("marketplace must not be removed")),
    )

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is False
    assert removed["provider_checkpoint_candidate_visible"] is True
    assert "visible but durability could not be confirmed" in _required_text(
        removed["provider_checkpoint_error"]
    )
    assert removed["uninstall_phase"] == "provider_verified"
    assert removed["receipt_state"] == "visible_unconfirmed"
    assert removed["receipt_preserved_for_retry"] is True
    assert removed["manual_review_required"] is False
    assert "confirm the visible provider checkpoint" in _required_text(removed["next"])
    assert agents.read_bytes() == agents_before
    assert integration._tree_manifest(marketplace) == marketplace_before
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_successful_checkpoint_write_with_failed_confirmation_is_retryable_candidate(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    agents_before = agents.read_bytes()
    marketplace_before = integration._tree_manifest(marketplace)

    def durability_unconfirmed(path: Path, expected: bytes) -> None:
        assert path == receipt
        assert b'"uninstall_phase": "provider_verified"' in expected
        raise OSError("injected successful-write confirmation failure")

    monkeypatch.setattr(integration, "_confirm_receipt_durable", durability_unconfirmed)
    monkeypatch.setattr(
        integration,
        "_apply_instruction_removal",
        lambda _: (_ for _ in ()).throw(AssertionError("instructions must not be removed")),
    )
    monkeypatch.setattr(
        shutil,
        "rmtree",
        lambda _: (_ for _ in ()).throw(AssertionError("marketplace must not be removed")),
    )

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is False
    assert removed["provider_checkpoint_candidate_visible"] is True
    assert removed["receipt_state"] == "visible_unconfirmed"
    assert removed["uninstall_phase"] == "provider_verified"
    assert removed["receipt_preserved_for_retry"] is True
    assert removed["manual_review_required"] is False
    assert "successful-write confirmation failure" in _required_text(
        removed["provider_checkpoint_error"]
    )
    assert agents.read_bytes() == agents_before
    assert integration._tree_manifest(marketplace) == marketplace_before
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


@pytest.mark.parametrize("receipt_mutation", ["missing", "replacement"])
def test_provider_checkpoint_unknown_receipt_state_is_reported_truthfully(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
    receipt_mutation: str,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    agents_before = agents.read_bytes()
    marketplace_before = integration._tree_manifest(marketplace)
    replacement = b"concurrent ownership receipt\n"
    real_atomic_write = atomic_module.atomic_write

    def mutate_checkpoint(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        if path == receipt and b'"uninstall_phase": "provider_verified"' in content:
            if receipt_mutation == "missing":
                path.unlink()
            else:
                path.write_bytes(replacement)
            raise OSError(f"injected receipt {receipt_mutation}")
        real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(integration, "atomic_write", mutate_checkpoint)
    removed = integration.uninstall_codex(codex_home=home)

    expected_state = "missing" if receipt_mutation == "missing" else "conflicted"
    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is False
    assert "unknown ownership receipt state" in _required_text(removed["provider_checkpoint_error"])
    assert removed["receipt_state"] == expected_state
    assert removed["receipt_missing"] is (True if receipt_mutation == "missing" else None)
    assert removed["receipt_preserved_for_retry"] is (
        False if receipt_mutation == "missing" else None
    )
    assert removed["manual_review_required"] is True
    assert agents.read_bytes() == agents_before
    assert integration._tree_manifest(marketplace) == marketplace_before
    if receipt_mutation == "replacement":
        assert receipt.read_bytes() == replacement
    else:
        assert not receipt.exists()


def test_local_removal_failure_retries_offline_from_provider_checkpoint(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    receipt = integration._receipt_path(home)
    fake_codex.required_manifest = manifest
    real_publish = integration._publish_directory_new
    failed = False

    def fail_once(source: Path, target: Path) -> None:
        nonlocal failed
        if (
            not failed
            and source == marketplace
            and target.name.startswith(f".{marketplace.name}.remove-")
        ):
            failed = True
            raise PermissionError("injected local quarantine failure")
        real_publish(source, target)

    monkeypatch.setattr(integration, "_publish_directory_new", fail_once)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["provider_checkpointed"] is True
    assert removed["uninstall_phase"] == "provider_verified"
    assert removed["local_cleanup_verified"] is False
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert removed["manual_review_required"] is True
    assert "quarantine failure" in _required_text(removed["local_cleanup_error"])
    assert receipt.exists()
    assert marketplace.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert (home / "AGENTS.md").exists()
    calls_before_retry = len(fake_codex.calls)

    monkeypatch.setattr(integration, "_publish_directory_new", real_publish)
    monkeypatch.setattr(
        integration,
        "_codex_executable",
        lambda: (_ for _ in ()).throw(SetupError("Codex must not run")),
    )
    retried = integration.uninstall_codex(codex_home=home)
    retry_calls = fake_codex.calls[calls_before_retry:]

    assert retried["cleanup_complete"] is False
    assert retried["provider_cleanup_verified"] is True
    assert retried["marketplace_files_state"] == "retained"
    assert retried["recovery_retained"] is True
    assert receipt.exists()
    assert not marketplace.exists()
    assert _retained_paths(home)[0].is_dir()
    assert retry_calls == []


def test_partial_install_candidate_is_cataloged_and_recovery_is_idempotent(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    marketplace = _marketplace_root(home)
    receipt = integration._receipt_path(home)
    real_write = integration._write_exclusive
    writes = 0

    def fail_after_partial_candidate(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise PermissionError("injected partial candidate failure")
        real_write(path, content, mode=mode)

    monkeypatch.setattr(integration, "_write_exclusive", fail_after_partial_candidate)
    with pytest.raises(SetupError, match="partial candidate failure"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    recovery = _receipt_payload(home)
    assert recovery["integration_active"] is False
    assert "install_transition" not in recovery
    assert [record["kind"] for record in recovery["cleanup_pending"]] == ["new"]
    retained = _retained_paths(home)
    assert len(retained) == 1
    observed = integration._tree_manifest(retained[0])
    expected = recovery["cleanup_pending"][0]["manifest"]
    assert observed == expected
    assert observed
    assert not marketplace.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert receipt.exists()

    monkeypatch.setattr(integration, "_write_exclusive", real_write)
    first = integration.uninstall_codex(codex_home=home)
    second = integration.uninstall_codex(codex_home=home)

    assert first == second
    assert first["cleanup_complete"] is False
    assert first["integration_removed"] is True
    assert first["marketplace_files_state"] == "retained"
    assert first["retained_cleanup_paths"] == [str(retained[0])]
    assert retained[0].is_dir()


def test_uninstall_isolates_and_rechecks_post_inspection_marketplace_race(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    sentinel = marketplace / "concurrent-user-file.txt"
    real_publish = integration._publish_directory_new
    injected = False

    def mutate_before_isolation(source: Path, target: Path) -> None:
        nonlocal injected
        if source == marketplace and target.name.startswith(f".{marketplace.name}.remove-"):
            sentinel.write_bytes(b"preserve post-inspection bytes\n")
            injected = True
        real_publish(source, target)

    monkeypatch.setattr(integration, "_publish_directory_new", mutate_before_isolation)
    removed = integration.uninstall_codex(codex_home=home)

    assert injected is True
    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert removed["manual_review_required"] is True
    assert removed["user_data_preserved"] is True
    quarantines = list(marketplace.parent.glob(f".{marketplace.name}.remove-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / sentinel.name).read_bytes() == b"preserve post-inspection bytes\n"
    assert not marketplace.exists()
    assert receipt.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_uninstall_never_recursively_deletes_quarantined_marketplace(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    real_rmtree = shutil.rmtree

    def forbid_quarantine_rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path).name.startswith(f".{marketplace.name}.remove-"):
            raise AssertionError("retained marketplace quarantine must not use rmtree")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", forbid_quarantine_rmtree)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["marketplace_files_state"] == "retained"
    assert removed["manual_review_required"] is False
    assert removed["user_data_preserved"] is True
    assert receipt.exists()
    retained = _retained_paths(home)
    assert len(retained) == 1
    assert (retained[0] / "plugins/gsv/skills/gsv/SKILL.md").is_file()


def test_uninstall_preserves_public_and_isolated_trees_on_destination_race(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    receipt = integration._receipt_path(home)
    real_publish = integration._publish_directory_new
    sentinel = marketplace / "race-created-user-file.txt"

    def recreate_after_isolation(source: Path, target: Path) -> None:
        real_publish(source, target)
        if source == marketplace and target.name.startswith(f".{marketplace.name}.remove-"):
            marketplace.mkdir()
            sentinel.write_bytes(b"preserve destination race bytes\n")

    monkeypatch.setattr(integration, "_publish_directory_new", recreate_after_isolation)
    removed = integration.uninstall_codex(codex_home=home)
    quarantines = list(marketplace.parent.glob(f".{marketplace.name}.remove-*"))

    assert removed["cleanup_complete"] is False
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert removed["manual_review_required"] is True
    assert removed["user_data_preserved"] is True
    assert sentinel.read_bytes() == b"preserve destination race bytes\n"
    assert len(quarantines) == 1
    assert (quarantines[0] / "plugins/gsv/skills/gsv/SKILL.md").is_file()
    assert receipt.exists()


def test_agents_change_after_provider_commit_is_not_overwritten_and_retry_is_idempotent(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    receipt = integration._receipt_path(home)
    agents = home / "AGENTS.md"
    fake_codex.required_manifest = manifest
    provider_calls = 0

    def change_after_final_list(
        executable: str, arguments: list[str], codex_home: Path
    ) -> dict[str, Any]:
        nonlocal provider_calls
        result = fake_codex.run(executable, arguments, codex_home)
        provider_calls += 1
        if provider_calls == 6:
            agents.write_bytes(agents.read_bytes() + b"\n# Concurrent user note\n")
        return result

    monkeypatch.setattr(integration, "_run_json", change_after_final_list)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["local_cleanup_verified"] is False
    assert "changed after uninstall preflight" in _required_text(removed["local_cleanup_error"])
    assert b"# Concurrent user note" in agents.read_bytes()
    assert not marketplace.exists()
    quarantines = list(marketplace.parent.glob(f".{marketplace.name}.remove-*"))
    assert len(quarantines) == 1
    assert receipt.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    calls_before_retry = len(fake_codex.calls)

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    retried = integration.uninstall_codex(codex_home=home)
    retry_calls = fake_codex.calls[calls_before_retry:]

    assert retried["cleanup_complete"] is False
    assert agents.read_text(encoding="utf-8") == "# Concurrent user note\n"
    assert not marketplace.exists()
    assert receipt.exists()
    assert _receipt_payload(home)["integration_active"] is False
    assert not any("add" in command for command in retry_calls)


def test_marketplace_change_after_provider_commit_is_preserved_until_safe_retry(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    receipt = integration._receipt_path(home)
    changed = marketplace / "concurrent-user-file.txt"
    fake_codex.required_manifest = manifest
    provider_calls = 0

    def change_after_final_list(
        executable: str, arguments: list[str], codex_home: Path
    ) -> dict[str, Any]:
        nonlocal provider_calls
        result = fake_codex.run(executable, arguments, codex_home)
        provider_calls += 1
        if provider_calls == 6:
            changed.write_text("preserve me\n", encoding="utf-8")
        return result

    monkeypatch.setattr(integration, "_run_json", change_after_final_list)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["local_cleanup_verified"] is False
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert removed["manual_review_required"] is True
    assert changed.read_text(encoding="utf-8") == "preserve me\n"
    assert receipt.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    calls_before_retry = len(fake_codex.calls)

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    still_partial = integration.uninstall_codex(codex_home=home)

    assert still_partial["cleanup_complete"] is False
    assert fake_codex.calls[calls_before_retry:] == []
    assert changed.exists()
    changed.unlink()
    completed = integration.uninstall_codex(codex_home=home)

    assert completed["cleanup_complete"] is False
    assert not marketplace.exists()
    assert receipt.exists()
    assert completed["marketplace_files_state"] == "retained"
    assert _receipt_payload(home)["integration_active"] is False


def test_explicit_invalid_codex_override_fails_closed_during_uninstall(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    agents_before = (home / "AGENTS.md").read_bytes()
    marketplace = Path(installed.marketplace_root)
    marketplace_digest = integration._tree_digest(marketplace)
    receipt = integration._receipt_path(home)
    monkeypatch.setenv("GSV_CODEX", str(tmp_path / "missing-codex"))

    def invalid_override() -> str:
        raise SetupError("GSV_CODEX does not point to a regular Codex executable")

    monkeypatch.setattr(integration, "_codex_executable", invalid_override)
    with pytest.raises(SetupError, match="GSV_CODEX"):
        integration.uninstall_codex(codex_home=home)

    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert integration._tree_digest(marketplace) == marketplace_digest
    assert receipt.exists()
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert integration.MARKETPLACE_NAME in fake_codex.marketplaces


def test_changed_owned_marketplace_is_preserved_and_provider_cleanup_is_skipped(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    changed = marketplace / "locally-edited.txt"
    changed.write_text("keep this local change\n", encoding="utf-8")
    receipt = integration._receipt_path(home)
    agents = home / "AGENTS.md"
    agents_before = agents.read_bytes()
    calls_before = list(fake_codex.calls)

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["local_cleanup_verified"] is False
    assert removed["manual_review_required"] is True
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert removed["marketplace_files_path"] == str(marketplace)
    assert removed["local_cleanup_error"] == "recorded marketplace files changed after installation"
    assert removed["provider_cleanup_skipped"] is True
    assert removed["provider_cleanup_error"] is None
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert "left untouched" in _required_text(removed["next"])
    assert changed.read_text(encoding="utf-8") == "keep this local change\n"
    assert receipt.exists()
    assert fake_codex.calls == calls_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert integration.MARKETPLACE_NAME in fake_codex.marketplaces
    assert agents.read_bytes() == agents_before


def test_redirected_marketplace_registration_is_not_removed(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    generated = Path(installed.marketplace_root)
    redirected = tmp_path / "user-marketplace"
    redirected.mkdir()
    marker = redirected / "keep.txt"
    marker.write_text("user-owned\n", encoding="utf-8")
    fake_codex.marketplaces[integration.MARKETPLACE_NAME] = str(redirected)
    receipt = integration._receipt_path(home)
    agents = home / "AGENTS.md"
    agents_before = agents.read_bytes()

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is False
    assert "points somewhere other" in _required_text(removed["provider_cleanup_error"])
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert receipt.exists()
    assert generated.exists()
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces[integration.MARKETPLACE_NAME] == str(redirected)
    assert marker.read_text(encoding="utf-8") == "user-owned\n"
    assert agents.read_bytes() == agents_before


def test_recovery_catalog_write_failure_is_partial_and_retriable(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    original_write = integration._write_receipt_state

    def fail_recovery_catalog(
        target_home: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        context: str,
    ) -> None:
        if context == "uninstall recovery catalog":
            raise PermissionError("injected recovery catalog permission failure")
        original_write(
            target_home,
            expected=expected,
            replacement=replacement,
            context=context,
        )

    monkeypatch.setattr(integration, "_write_receipt_state", fail_recovery_catalog)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["receipt_cleanup_error"] == (
        "could not persist the uninstall recovery catalog: "
        "injected recovery catalog permission failure"
    )
    assert removed["receipt_preserved_for_retry"] is True
    assert "finish removing the ownership receipt" in _required_text(removed["next"])
    assert receipt.exists()

    monkeypatch.setattr(integration, "_write_receipt_state", original_write)
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is False
    assert receipt.exists()
    assert _receipt_payload(home)["integration_active"] is False


def test_true_clean_no_receipt_state_is_an_idempotent_noop(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents = home / "AGENTS.md"
    original = b"# User-owned instructions\n"
    agents.write_bytes(original)

    first = integration.uninstall_codex(codex_home=home)
    second = integration.uninstall_codex(codex_home=home)

    assert first == second
    assert first["cleanup_complete"] is True
    assert first["receipt_missing"] is True
    assert first["manual_review_required"] is False
    assert first["next"] is None
    assert agents.read_bytes() == original
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}


def test_interrupted_install_without_receipt_is_partial_and_non_destructive(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration._replace_marketplace(
        tmp_path / "vault",
        target=_marketplace_root(home),
        before_moves=lambda _: None,
    )
    integration._install_instructions(home)
    fake_codex.marketplaces[integration.MARKETPLACE_NAME] = str(_marketplace_root(home))
    fake_codex.plugins.add(integration.PLUGIN_ID)
    agents_before = (home / "AGENTS.md").read_bytes()
    digest_before = integration._tree_digest(_marketplace_root(home))

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["receipt_missing"] is True
    assert removed["manual_review_required"] is True
    assert removed["marketplace_files_state"] == "unowned_evidence"
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert removed["preexisting_plugin_preserved"] is None
    assert "Restore the matching ownership receipt" in _required_text(removed["next"])
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert integration._tree_digest(_marketplace_root(home)) == digest_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(_marketplace_root(home))}
    assert fake_codex.calls == [
        ("plugin", "list", "--json"),
        ("plugin", "marketplace", "list", "--json"),
    ]


def test_orphan_inspection_first_query_failure_is_partial(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()

    def fail_first(*_: object) -> dict[str, Any]:
        raise SetupError("plugin inspection timed out")

    monkeypatch.setattr(integration, "_run_json", fail_first)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["manual_review_required"] is True
    assert removed["provider_cleanup_error"] == (
        "could not inspect Codex registrations: plugin inspection timed out"
    )
    assert removed["deferred_registrations"] == []


def test_orphan_inspection_preserves_plugin_evidence_when_second_query_fails(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    fake_codex.plugins.add(integration.PLUGIN_ID)

    def fail_marketplace(executable: str, arguments: list[str], codex_home: Path) -> dict[str, Any]:
        if arguments == ["plugin", "marketplace", "list", "--json"]:
            raise SetupError("marketplace inspection timed out")
        return fake_codex.run(executable, arguments, codex_home)

    monkeypatch.setattr(integration, "_run_json", fail_marketplace)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["manual_review_required"] is True
    assert removed["deferred_registrations"] == ["plugin"]
    assert removed["preexisting_plugin_preserved"] is None
    assert "marketplace inspection timed out" in _required_text(removed["provider_cleanup_error"])
    assert fake_codex.plugins == {integration.PLUGIN_ID}


@pytest.mark.parametrize(
    ("payload", "helper", "message"),
    [
        ({}, "plugin", "must be a list"),
        ({"installed": {}}, "plugin", "must be a list"),
        ({"installed": [None]}, "plugin", "must be an object"),
        ({"installed": [{}]}, "plugin", "pluginId"),
        ({"installed": [{"pluginId": 3}]}, "plugin", "pluginId"),
        ({}, "marketplace", "must be a list"),
        ({"marketplaces": {}}, "marketplace", "must be a list"),
        ({"marketplaces": [None]}, "marketplace", "must be an object"),
        ({"marketplaces": [{}]}, "marketplace", "name"),
        ({"marketplaces": [{"name": integration.MARKETPLACE_NAME}]}, "marketplace", "root"),
    ],
)
def test_provider_list_shape_and_identity_fields_are_required(
    payload: dict[str, Any], helper: str, message: str
) -> None:
    with pytest.raises(SetupError, match=message):
        if helper == "plugin":
            integration._plugin_items(payload, context="test provider list")
        else:
            integration._marketplace_items(payload, context="test provider list")


def test_codex_status_requires_boolean_enabled_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setattr(integration, "_codex_executable", lambda: "fake-codex")

    def provider_list(_executable: str, arguments: list[str], _home: Path) -> dict[str, Any]:
        if "marketplace" in arguments:
            return {"marketplaces": []}
        return {"installed": [{"pluginId": integration.PLUGIN_ID, "enabled": "yes"}]}

    monkeypatch.setattr(integration, "_run_json", provider_list)

    with pytest.raises(SetupError, match="enabled"):
        integration.codex_status(codex_home=home)


@pytest.mark.parametrize("duplicate_kind", ["plugin", "marketplace"])
def test_reinstall_rejects_duplicate_owned_provider_identities_before_mutation(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_kind: str,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "first-vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    agents_before = agents.read_bytes()
    receipt_before = receipt.read_bytes()
    marketplace_before = integration._tree_manifest(marketplace)
    calls_before = len(fake_codex.calls)

    def duplicate_provider(
        executable: str, arguments: list[str], codex_home: Path
    ) -> dict[str, Any]:
        payload = fake_codex.run(executable, arguments, codex_home)
        if duplicate_kind == "plugin" and arguments == ["plugin", "list", "--json"]:
            payload["installed"] = [
                {"enabled": True, "pluginId": integration.PLUGIN_ID},
                {"enabled": True, "pluginId": integration.PLUGIN_ID},
            ]
        if duplicate_kind == "marketplace" and arguments == [
            "plugin",
            "marketplace",
            "list",
            "--json",
        ]:
            payload["marketplaces"] = [
                {"name": integration.MARKETPLACE_NAME, "root": str(marketplace)},
                {
                    "name": integration.MARKETPLACE_NAME,
                    "root": str(tmp_path / "different-root"),
                },
            ]
        return payload

    monkeypatch.setattr(integration, "_run_json", duplicate_provider)
    with pytest.raises(SetupError, match="duplicate"):
        integration.install_codex(vault=tmp_path / "second-vault", codex_home=home)

    new_calls = fake_codex.calls[calls_before:]
    assert not any("add" in call or "remove" in call for call in new_calls)
    assert agents.read_bytes() == agents_before
    assert receipt.read_bytes() == receipt_before
    assert integration._tree_manifest(marketplace) == marketplace_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace.resolve())}


@pytest.mark.parametrize("duplicate_kind", ["plugin", "marketplace"])
def test_uninstall_rejects_duplicate_owned_provider_identities_before_mutation(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_kind: str,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    agents = home / "AGENTS.md"
    receipt = integration._receipt_path(home)
    agents_before = agents.read_bytes()
    receipt_before = receipt.read_bytes()
    marketplace_before = integration._tree_manifest(marketplace)
    calls_before = len(fake_codex.calls)

    def duplicate_provider(
        executable: str, arguments: list[str], codex_home: Path
    ) -> dict[str, Any]:
        payload = fake_codex.run(executable, arguments, codex_home)
        if duplicate_kind == "plugin" and arguments == ["plugin", "list", "--json"]:
            payload["installed"] = [
                {"enabled": True, "pluginId": integration.PLUGIN_ID},
                {"enabled": True, "pluginId": integration.PLUGIN_ID},
            ]
        if duplicate_kind == "marketplace" and arguments == [
            "plugin",
            "marketplace",
            "list",
            "--json",
        ]:
            payload["marketplaces"] = [
                {"name": integration.MARKETPLACE_NAME, "root": str(marketplace)},
                {
                    "name": integration.MARKETPLACE_NAME,
                    "root": str(tmp_path / "different-root"),
                },
            ]
        return payload

    monkeypatch.setattr(integration, "_run_json", duplicate_provider)
    removed = integration.uninstall_codex(codex_home=home)

    new_calls = fake_codex.calls[calls_before:]
    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is False
    assert removed["manual_review_required"] is True
    assert "duplicate" in _required_text(removed["provider_cleanup_error"])
    identity = integration.PLUGIN_ID if duplicate_kind == "plugin" else integration.MARKETPLACE_NAME
    context = (
        "plugin list before uninstall"
        if duplicate_kind == "plugin"
        else "marketplace list before uninstall"
    )
    assert removed["next"] == (
        "The Codex provider state is ambiguous: Codex returned duplicate "
        f"{identity!r} identities in {context}; provider state was left unchanged. "
        "Inspect and remove the duplicate Seld registration explicitly, then re-run "
        "`gsv codex uninstall`."
    )
    assert "points to" not in removed["next"]
    assert not any("add" in call or "remove" in call for call in new_calls)
    assert agents.read_bytes() == agents_before
    assert receipt.read_bytes() == receipt_before
    assert integration._tree_manifest(marketplace) == marketplace_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace.resolve())}


@pytest.mark.parametrize(
    ("malformed_call", "malformed"),
    [
        (1, {}),
        (1, {"installed": [{}]}),
        (2, {"marketplaces": [{}]}),
        (5, {"installed": [{}]}),
        (
            6,
            {"marketplaces": [{"name": integration.MARKETPLACE_NAME, "root": 7}]},
        ),
    ],
)
def test_malformed_provider_state_keeps_receipt_before_or_after_removal(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
    malformed_call: int,
    malformed: dict[str, Any],
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    calls = 0

    def malformed_provider(
        executable: str, arguments: list[str], codex_home: Path
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == malformed_call:
            return malformed
        return fake_codex.run(executable, arguments, codex_home)

    monkeypatch.setattr(integration, "_run_json", malformed_provider)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is False
    assert "malformed" in _required_text(removed["provider_cleanup_error"])
    assert receipt.exists()

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    retried = integration.uninstall_codex(codex_home=home)
    assert retried["cleanup_complete"] is False
    assert retried["marketplace_files_state"] == "retained"
    assert receipt.exists()
    assert _receipt_payload(home)["integration_active"] is False


def test_receipt_marketplace_root_is_bound_to_exact_codex_home(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home_a = tmp_path / "codex-a"
    home_b = tmp_path / "codex-b"
    home_a.mkdir()
    home_b.mkdir()
    installed_a = integration.install_codex(vault=tmp_path / "vault-a", codex_home=home_a)
    change_b = integration._replace_marketplace(
        tmp_path / "vault-b",
        target=_marketplace_root(home_b),
        before_moves=lambda _: None,
    )
    receipt_a = integration._receipt_path(home_a)
    payload = json.loads(receipt_a.read_text(encoding="utf-8"))
    payload["marketplace_root"] = str(change_b.path)
    payload["marketplace_digest"] = integration._tree_digest(change_b.path)
    tampered = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt_a.write_bytes(tampered)
    agents_before = (home_a / "AGENTS.md").read_bytes()
    digest_a = integration._tree_digest(Path(installed_a.marketplace_root))
    digest_b = integration._tree_digest(change_b.path)
    calls_before = list(fake_codex.calls)

    with pytest.raises(ValidationError, match="different marketplace"):
        integration.uninstall_codex(codex_home=home_a)

    assert receipt_a.read_bytes() == tampered
    assert (home_a / "AGENTS.md").read_bytes() == agents_before
    assert integration._tree_digest(Path(installed_a.marketplace_root)) == digest_a
    assert integration._tree_digest(change_b.path) == digest_b
    assert fake_codex.calls == calls_before


@pytest.mark.skipif(os.name == "nt", reason="symlink setup needs elevated privileges")
def test_dangling_receipt_symlink_is_rejected_before_mutation(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    receipt.unlink()
    receipt.symlink_to(tmp_path / "missing-receipt")
    agents_before = (home / "AGENTS.md").read_bytes()
    digest_before = integration._tree_digest(Path(installed.marketplace_root))
    calls_before = list(fake_codex.calls)

    with pytest.raises(ValidationError, match="symbolic link"):
        integration.uninstall_codex(codex_home=home)

    assert receipt.is_symlink()
    assert (home / "AGENTS.md").read_bytes() == agents_before
    assert integration._tree_digest(Path(installed.marketplace_root)) == digest_before
    assert fake_codex.calls == calls_before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_receipt_fifo_is_rejected_without_opening_or_blocking(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    if sys.platform == "win32":
        pytest.skip("FIFO creation is unavailable")
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    receipt.unlink()
    os.mkfifo(receipt)
    calls_before = list(fake_codex.calls)

    with pytest.raises(ValidationError, match="regular file"):
        integration.uninstall_codex(codex_home=home)

    assert receipt.exists()
    assert Path(installed.marketplace_root).exists()
    assert (home / "AGENTS.md").exists()
    assert fake_codex.calls == calls_before


def test_receipt_permission_failure_is_structured_and_non_destructive(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    real_open = os.open
    calls_before = list(fake_codex.calls)

    def deny_receipt(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(os.fsdecode(path)) == receipt:
            raise PermissionError("injected permission failure")
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", deny_receipt)
    with pytest.raises(ValidationError, match="could not open Codex integration receipt"):
        integration.uninstall_codex(codex_home=home)

    assert receipt.exists()
    assert Path(installed.marketplace_root).exists()
    assert (home / "AGENTS.md").exists()
    assert fake_codex.calls == calls_before


@pytest.mark.skipif(os.name == "nt", reason="symlink setup needs elevated privileges")
def test_symlinked_agents_is_rejected_before_install_provider_calls(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    outside = tmp_path / "outside-agents.md"
    outside.write_bytes(b"# External instructions\n")
    agents = home / "AGENTS.md"
    agents.symlink_to(outside)

    with pytest.raises(ValidationError, match="symbolic link"):
        integration.install_codex(vault=tmp_path / "vault", codex_home=home)

    assert agents.is_symlink()
    assert outside.read_bytes() == b"# External instructions\n"
    assert fake_codex.calls == []
    assert not _marketplace_root(home).exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink setup needs elevated privileges")
def test_symlinked_agents_is_rejected_before_uninstall_mutation_or_provider_calls(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    agents = home / "AGENTS.md"
    outside = tmp_path / "outside-agents.md"
    agents.replace(outside)
    agents.symlink_to(outside)
    outside_before = outside.read_bytes()
    marketplace = Path(installed.marketplace_root)
    digest_before = integration._tree_digest(marketplace)
    receipt_before = integration._receipt_path(home).read_bytes()
    calls_before = list(fake_codex.calls)

    with pytest.raises(ValidationError, match="symbolic link"):
        integration.uninstall_codex(codex_home=home)

    assert agents.is_symlink()
    assert outside.read_bytes() == outside_before
    assert integration._tree_digest(marketplace) == digest_before
    assert integration._receipt_path(home).read_bytes() == receipt_before
    assert fake_codex.calls == calls_before


def test_added_empty_marketplace_directory_is_preserved_as_changed(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    added = marketplace / "user-empty-directory"
    added.mkdir()
    calls_before = list(fake_codex.calls)

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert added.is_dir()
    assert fake_codex.calls == calls_before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_special_marketplace_file_is_preserved_and_fails_closed(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    if sys.platform == "win32":
        pytest.skip("FIFO creation is unavailable")
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    fifo = marketplace / "user.fifo"
    os.mkfifo(fifo)
    calls_before = list(fake_codex.calls)

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["marketplace_files_state"] == "changed_or_unsafe"
    assert "special file" in _required_text(removed["local_cleanup_error"])
    assert fifo.exists()
    assert fake_codex.calls == calls_before


def test_marketplace_manifest_propagates_nested_scandir_failure(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    unreadable = marketplace / "plugins/gsv"
    real_scandir = os.scandir

    def fail_nested(path: Any) -> Any:
        if Path(path) == unreadable:
            raise PermissionError("injected unreadable marketplace subtree")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_nested)

    with pytest.raises(ValidationError, match="could not read generated marketplace directory"):
        integration._tree_manifest(marketplace)


def test_windows_directory_durability_uses_typed_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeFunction:
        def __init__(self, result: object) -> None:
            self.result = result
            self.calls: list[tuple[object, ...]] = []
            self.argtypes: list[object] | None = None
            self.restype: object | None = None

        def __call__(self, *arguments: object) -> object:
            self.calls.append(arguments)
            return self.result

    class FakeKernel32:
        def __init__(self) -> None:
            self.CreateFileW = FakeFunction(0x123456789)
            self.FlushFileBuffers = FakeFunction(1)
            self.CloseHandle = FakeFunction(1)

    kernel32 = FakeKernel32()

    def fake_loader(name: str, *, use_last_error: bool) -> FakeKernel32:
        assert name == "kernel32"
        assert use_last_error is True
        return kernel32

    monkeypatch.setitem(ctypes.__dict__, "WinDLL", fake_loader)
    configured = integration._windows_kernel32()

    assert configured is kernel32
    assert kernel32.CreateFileW.argtypes == [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    assert kernel32.CreateFileW.restype is wintypes.HANDLE
    assert kernel32.FlushFileBuffers.argtypes == [wintypes.HANDLE]
    assert kernel32.FlushFileBuffers.restype is wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CloseHandle.restype is wintypes.BOOL
    directory = tmp_path / "stage"
    monkeypatch.setattr(integration, "_windows_kernel32", lambda: kernel32)
    integration._flush_windows_directory(directory)

    assert kernel32.CreateFileW.calls == [
        (
            str(directory),
            0x40000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
    ]
    assert kernel32.FlushFileBuffers.calls == [(0x123456789,)]
    assert kernel32.CloseHandle.calls == [(0x123456789,)]


def test_windows_no_replace_move_uses_typed_write_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeFunction:
        def __init__(self, result: object) -> None:
            self.result = result
            self.calls: list[tuple[object, ...]] = []
            self.argtypes: list[object] | None = None
            self.restype: object | None = None

        def __call__(self, *arguments: object) -> object:
            self.calls.append(arguments)
            return self.result

    class FakeKernel32:
        def __init__(self) -> None:
            self.MoveFileExW = FakeFunction(1)

    kernel32 = FakeKernel32()

    def fake_loader(name: str, *, use_last_error: bool) -> FakeKernel32:
        assert name == "kernel32"
        assert use_last_error is True
        return kernel32

    monkeypatch.setitem(ctypes.__dict__, "WinDLL", fake_loader)
    configured = atomic_module._windows_move_kernel32()

    assert configured is kernel32
    assert kernel32.MoveFileExW.argtypes == [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    assert kernel32.MoveFileExW.restype is wintypes.BOOL

    source = tmp_path / "source"
    target = tmp_path / "target"
    monkeypatch.setattr(atomic_module, "_windows_move_kernel32", lambda: kernel32)
    atomic_module._move_windows_path_new(source, target)

    assert kernel32.MoveFileExW.calls == [(str(source), str(target), 0x00000008)]


@pytest.mark.skipif(os.name != "nt", reason="requires the real Win32 directory APIs")
def test_windows_directory_durability_and_no_replace_runtime(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "owned.txt").write_bytes(b"owned\n")
    manifest = integration._tree_manifest(source)

    integration._fsync_staged_directories(source, manifest)
    target = tmp_path / "target"
    integration._publish_directory_new(source, target)

    assert not source.exists()
    assert (target / "nested/owned.txt").read_bytes() == b"owned\n"
    collision = tmp_path / "collision"
    collision.mkdir()
    (collision / "different.txt").write_bytes(b"different\n")
    with pytest.raises(FileExistsError):
        integration._publish_directory_new(collision, target)
    assert (target / "nested/owned.txt").read_bytes() == b"owned\n"
    assert (collision / "different.txt").read_bytes() == b"different\n"


def test_install_transaction_serializes_uninstall_until_receipt_commit(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    finished = threading.Event()
    removed: list[integration.UninstallResult] = []
    errors: list[BaseException] = []

    def uninstall() -> None:
        try:
            removed.append(integration.uninstall_codex(codex_home=home))
        except BaseException as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)
        finally:
            finished.set()

    with integration.install_codex_transaction(vault=tmp_path / "vault", codex_home=home):
        worker = threading.Thread(target=uninstall)
        worker.start()
        assert not finished.wait(0.2)

    assert finished.wait(5)
    worker.join(timeout=1)
    assert errors == []
    assert removed[0]["cleanup_complete"] is False
    assert removed[0]["marketplace_files_state"] == "retained"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert integration._receipt_path(home).exists()
    assert _receipt_payload(home)["integration_active"] is False


def test_recovery_catalog_compare_and_write_preserves_replacement(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    original_write = integration._write_receipt_state
    replacement = b"concurrent ownership receipt\n"

    def replace_before_catalog_write(
        target_home: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        context: str,
    ) -> None:
        if context == "uninstall recovery catalog":
            receipt.write_bytes(b"concurrent ownership receipt\n")
        original_write(
            target_home,
            expected=expected,
            replacement=replacement,
            context=context,
        )

    monkeypatch.setattr(integration, "_write_receipt_state", replace_before_catalog_write)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert "changed before uninstall recovery catalog" in _required_text(
        removed["receipt_cleanup_error"]
    )
    assert removed["receipt_state"] == "conflicted"
    assert removed["receipt_preserved_for_retry"] is None
    assert receipt.read_bytes() == replacement
