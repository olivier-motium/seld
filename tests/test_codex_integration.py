from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import codex_integration as integration
from continuity_kernel.errors import SetupError, ValidationError


@dataclass
class FakeCodex:
    marketplaces: dict[str, str] = field(default_factory=dict)
    plugins: set[str] = field(default_factory=set)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, executable: str, arguments: list[str], home: Path) -> dict[str, Any]:
        del executable, home
        command = tuple(arguments)
        self.calls.append(command)
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
    assert not (home / "AGENTS.md.gsv-backup").exists()
    second = integration.install_codex(vault=vault, codex_home=home)
    removed = integration.uninstall_codex(codex_home=home)

    assert first.plugin_installed and second.plugin_installed
    assert agents.read_text(encoding="utf-8") == "# Existing instructions\n\nKeep this.\n"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert removed["plugin_removed"] is True
    assert removed["marketplace_files_removed"] is True
    assert removed["user_data_preserved"] is True
    assert vault.exists()
    assert not generated_marketplace.exists()
    content = agents.read_text(encoding="utf-8")
    assert integration.BLOCK_START not in content
    assert integration.BLOCK_END not in content


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
    assert removed["cleanup_complete"] is True
    assert removed["preexisting_plugin_preserved"] is None
    assert removed["plugin_removed"] is None


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
    assert "multiple or nested" in removed["local_cleanup_error"]
    assert removed["provider_cleanup_skipped"] is True
    assert receipt.exists()
    assert fake_codex.calls == calls_before
    assert agents.read_text(encoding="utf-8") == ambiguous


def test_instruction_post_verification_failure_prevents_all_provider_calls(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    calls_before = list(fake_codex.calls)

    def fail_verification(_: Path) -> bool:
        raise SetupError("injected instruction post-verification failure")

    monkeypatch.setattr(integration, "_instructions_installed", fail_verification)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["local_cleanup_verified"] is False
    assert removed["provider_cleanup_skipped"] is True
    assert removed["provider_cleanup_verified"] is False
    assert "post-verification failure" in removed["local_cleanup_error"]
    assert fake_codex.calls == calls_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert integration.MARKETPLACE_NAME in fake_codex.marketplaces
    assert not Path(installed.marketplace_root).exists()
    assert receipt.exists()


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
    assert not integration._receipt_path(home).exists()


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
    installed = integration.install_codex(vault=first_vault, codex_home=home)
    manifest = Path(installed.marketplace_root) / "plugins/gsv/.mcp.json"
    before = manifest.read_bytes()
    monkeypatch.setattr(
        integration,
        "codex_status",
        lambda **_: {"plugin_installed": False, "instructions_installed": True},
    )

    with pytest.raises(SetupError):
        integration.install_codex(vault=second_vault, codex_home=home)

    assert manifest.read_bytes() == before
    payload = json.loads(before)
    assert payload["mcpServers"]["gsv"]["env"]["GSV_VAULT"] == str(first_vault.resolve())


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
    change = integration._replace_marketplace(vault, runtime=runtime, target=root / "marketplace")
    integration._commit_marketplace(change)
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
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert "Re-run `gsv codex uninstall`" in removed["next"]
    assert receipt.exists()
    assert not Path(installed.marketplace_root).exists()
    assert not (home / "AGENTS.md").exists()

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is True
    assert retried["marketplace_files_state"] == "already_missing"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not receipt.exists()


def test_uninstall_without_codex_removes_owned_files_and_preserves_retry_receipt(
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

    def unavailable() -> str:
        raise SetupError("Codex executable is unavailable")

    monkeypatch.setattr(integration, "_codex_executable", unavailable)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["codex_available"] is False
    assert removed["codex_home"] == str(home.resolve())
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert removed["instructions_removed"] is True
    assert removed["local_cleanup_verified"] is True
    assert removed["marketplace_files_removed"] is True
    assert removed["marketplace_files_state"] == "removed"
    assert removed["marketplace_removed"] is None
    assert removed["plugin_removed"] is None
    assert "Re-run `gsv codex uninstall`" in removed["next"]
    assert removed["provider_cleanup_error"] == "Codex executable is unavailable"
    assert removed["provider_cleanup_verified"] is False
    assert removed["receipt_preserved_for_retry"] is True
    assert removed["registration_cleanup_deferred"] is True
    assert removed["user_data_preserved"] is True
    assert agents.read_bytes() == original
    assert not marketplace.exists()
    assert receipt.exists()
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces == {integration.MARKETPLACE_NAME: str(marketplace.resolve())}

    monkeypatch.setattr(integration, "_codex_executable", lambda: "fake-codex")
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["registration_cleanup_deferred"] is False
    assert retried["plugin_removed"] is True
    assert retried["marketplace_removed"] is True
    assert retried["marketplace_files_removed"] is False
    assert retried["marketplace_files_state"] == "already_missing"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not receipt.exists()


def test_uninstall_without_codex_or_receipt_is_an_idempotent_noop(
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
    assert first["cleanup_complete"] is True
    assert first["registration_cleanup_deferred"] is False
    assert first["deferred_registrations"] == []
    assert first["next"] is None
    assert first["instructions_removed"] is False
    assert agents.read_bytes() == original
    assert not integration._receipt_path(home).exists()


def test_uninstall_clears_receipt_when_owned_registrations_are_already_absent(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    fake_codex.plugins.clear()
    fake_codex.marketplaces.clear()

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["codex_available"] is True
    assert removed["cleanup_complete"] is True
    assert removed["registration_cleanup_deferred"] is False
    assert removed["plugin_removed"] is False
    assert removed["marketplace_removed"] is False
    assert removed["marketplace_files_removed"] is True
    assert not Path(installed.marketplace_root).exists()
    assert not (home / "AGENTS.md").exists()
    assert not integration._receipt_path(home).exists()


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
    assert "left untouched" in removed["next"]
    assert changed.read_text(encoding="utf-8") == "keep this local change\n"
    assert receipt.exists()
    assert fake_codex.calls == calls_before
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert integration.MARKETPLACE_NAME in fake_codex.marketplaces
    assert not (home / "AGENTS.md").exists()


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

    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is False
    assert "points somewhere other" in removed["provider_cleanup_error"]
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert receipt.exists()
    assert not generated.exists()
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces[integration.MARKETPLACE_NAME] == str(redirected)
    assert marker.read_text(encoding="utf-8") == "user-owned\n"


def test_receipt_removal_failure_is_partial_and_retriable(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    original_remove = integration._remove_receipt

    def fail_remove(_: Path) -> None:
        raise PermissionError("injected receipt permission failure")

    monkeypatch.setattr(integration, "_remove_receipt", fail_remove)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["receipt_cleanup_error"] == (
        "could not remove the ownership receipt: injected receipt permission failure"
    )
    assert removed["receipt_preserved_for_retry"] is True
    assert "finish removing the ownership receipt" in removed["next"]
    assert receipt.exists()

    monkeypatch.setattr(integration, "_remove_receipt", original_remove)
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is True
    assert not receipt.exists()
