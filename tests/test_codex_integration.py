from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
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
    assert removed["cleanup_complete"] is False
    assert removed["manual_review_required"] is True
    assert removed["marketplace_files_state"] == "unowned_evidence"
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
    assert "multiple or nested" in removed["local_cleanup_error"]
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
    assert "preflight failure" in removed["local_cleanup_error"]
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
    assert "Re-run `gsv codex uninstall`" in removed["next"]
    assert receipt.exists()
    assert Path(installed.marketplace_root).exists()
    assert (home / "AGENTS.md").read_bytes() == agents_before

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is True
    assert retried["marketplace_files_state"] == "removed"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not receipt.exists()


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
    assert "Re-run `gsv codex uninstall`" in removed["next"]
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
    assert retried["marketplace_files_removed"] is True
    assert retried["marketplace_files_state"] == "removed"
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert agents.read_bytes() == original
    assert not receipt.exists()


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
    assert "Restore the matching ownership receipt" in first["next"]
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


def test_legacy_missing_local_state_is_repaired_for_manifest_dependent_provider(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    receipt = integration._receipt_path(home)
    (home / "AGENTS.md").unlink()
    shutil.rmtree(marketplace)
    calls_before = len(fake_codex.calls)
    fake_codex.required_manifest = manifest

    removed = integration.uninstall_codex(codex_home=home)
    uninstall_calls = fake_codex.calls[calls_before:]

    assert removed["cleanup_complete"] is True
    assert removed["provider_cleanup_verified"] is True
    assert removed["local_cleanup_verified"] is True
    assert removed["marketplace_files_state"] == "removed"
    assert removed["plugin_removed"] is True
    assert removed["marketplace_removed"] is True
    assert not receipt.exists()
    assert not marketplace.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not any("add" in command for command in uninstall_calls)


def test_provider_cleanup_runs_before_verified_marketplace_files_are_removed(
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

    assert removed["cleanup_complete"] is True
    assert removed["provider_cleanup_verified"] is True
    assert removed["marketplace_files_state"] == "removed"
    assert not marketplace.exists()
    assert not agents.exists()


def test_local_removal_failure_is_manual_but_can_retry_when_bytes_are_unchanged(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    receipt = integration._receipt_path(home)
    fake_codex.required_manifest = manifest
    real_rmtree = shutil.rmtree
    failed = False

    def fail_once(path: Path) -> None:
        nonlocal failed
        if Path(path) == marketplace and not failed:
            failed = True
            raise PermissionError("injected local deletion failure")
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", fail_once)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["local_cleanup_verified"] is False
    assert removed["marketplace_files_state"] == "removal_failed"
    assert removed["manual_review_required"] is True
    assert "may have removed only part" in removed["next"]
    assert "Re-run `gsv codex uninstall` to retry" not in removed["next"]
    assert receipt.exists()
    assert marketplace.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not (home / "AGENTS.md").exists()
    calls_before_retry = len(fake_codex.calls)

    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    retried = integration.uninstall_codex(codex_home=home)
    retry_calls = fake_codex.calls[calls_before_retry:]

    assert retried["cleanup_complete"] is True
    assert retried["provider_cleanup_verified"] is True
    assert retried["marketplace_files_state"] == "removed"
    assert not receipt.exists()
    assert not marketplace.exists()
    assert not any("add" in command for command in retry_calls)


def test_partial_marketplace_deletion_is_not_promised_as_automatically_retriable(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    installed = integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    marketplace = Path(installed.marketplace_root)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    owned_skill = marketplace / "plugins/gsv/skills/gsv/SKILL.md"
    receipt = integration._receipt_path(home)
    fake_codex.required_manifest = manifest
    real_rmtree = shutil.rmtree
    failed = False

    def delete_then_fail(path: Path) -> None:
        nonlocal failed
        if Path(path) == marketplace and not failed:
            failed = True
            owned_skill.unlink()
            raise PermissionError("injected failure after partial deletion")
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", delete_then_fail)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert removed["provider_cleanup_verified"] is True
    assert removed["local_cleanup_verified"] is False
    assert removed["marketplace_files_state"] == "removal_failed"
    assert removed["manual_review_required"] is True
    assert "may have removed only part" in removed["next"]
    assert receipt.exists()
    assert marketplace.exists()
    assert not owned_skill.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    calls_before_retry = len(fake_codex.calls)

    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    retried = integration.uninstall_codex(codex_home=home)

    assert retried["cleanup_complete"] is False
    assert retried["provider_cleanup_skipped"] is True
    assert retried["marketplace_files_state"] == "changed_or_unsafe"
    assert retried["manual_review_required"] is True
    assert fake_codex.calls[calls_before_retry:] == []
    assert receipt.exists()
    assert marketplace.exists()


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
    assert "changed after uninstall preflight" in removed["local_cleanup_error"]
    assert b"# Concurrent user note" in agents.read_bytes()
    assert marketplace.exists()
    assert receipt.exists()
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    calls_before_retry = len(fake_codex.calls)

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    retried = integration.uninstall_codex(codex_home=home)
    retry_calls = fake_codex.calls[calls_before_retry:]

    assert retried["cleanup_complete"] is True
    assert agents.read_text(encoding="utf-8") == "# Concurrent user note\n"
    assert not marketplace.exists()
    assert not receipt.exists()
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

    assert completed["cleanup_complete"] is True
    assert not marketplace.exists()
    assert not receipt.exists()


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
    assert "left untouched" in removed["next"]
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
    assert "points somewhere other" in removed["provider_cleanup_error"]
    assert removed["deferred_registrations"] == ["plugin", "marketplace"]
    assert receipt.exists()
    assert generated.exists()
    assert fake_codex.plugins == {integration.PLUGIN_ID}
    assert fake_codex.marketplaces[integration.MARKETPLACE_NAME] == str(redirected)
    assert marker.read_text(encoding="utf-8") == "user-owned\n"
    assert agents.read_bytes() == agents_before


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

    def fail_remove(_: Path, *, expected: bytes) -> None:
        del expected
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
    marketplace_change = integration._replace_marketplace(
        tmp_path / "vault", target=_marketplace_root(home)
    )
    integration._commit_marketplace(marketplace_change)
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
    assert "Restore the matching ownership receipt" in removed["next"]
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
    assert "marketplace inspection timed out" in removed["provider_cleanup_error"]
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
    monkeypatch.setattr(
        integration,
        "_run_json",
        lambda *_: {"installed": [{"pluginId": integration.PLUGIN_ID, "enabled": "yes"}]},
    )

    with pytest.raises(SetupError, match="enabled"):
        integration.codex_status(codex_home=home)


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
    assert "malformed" in removed["provider_cleanup_error"]
    assert receipt.exists()

    monkeypatch.setattr(integration, "_run_json", fake_codex.run)
    retried = integration.uninstall_codex(codex_home=home)
    assert retried["cleanup_complete"] is True
    assert not receipt.exists()


def test_receipt_marketplace_root_is_bound_to_exact_codex_home(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home_a = tmp_path / "codex-a"
    home_b = tmp_path / "codex-b"
    home_a.mkdir()
    home_b.mkdir()
    installed_a = integration.install_codex(vault=tmp_path / "vault-a", codex_home=home_a)
    change_b = integration._replace_marketplace(
        tmp_path / "vault-b", target=_marketplace_root(home_b)
    )
    integration._commit_marketplace(change_b)
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
    assert "special file" in removed["local_cleanup_error"]
    assert fifo.exists()
    assert fake_codex.calls == calls_before


def test_install_transaction_serializes_uninstall_until_receipt_commit(
    tmp_path: Path, fake_codex: FakeCodex
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    finished = threading.Event()
    removed: list[dict[str, Any]] = []
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
    assert removed[0]["cleanup_complete"] is True
    assert fake_codex.plugins == set()
    assert fake_codex.marketplaces == {}
    assert not integration._receipt_path(home).exists()


def test_receipt_compare_and_delete_preserves_replacement(
    tmp_path: Path,
    fake_codex: FakeCodex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    receipt = integration._receipt_path(home)
    original_remove = integration._remove_receipt
    replacement = (
        json.dumps(
            {
                "codex_home": str(home.resolve()),
                "format_version": integration.RECEIPT_FORMAT_VERSION,
                "marketplace_owned": False,
                "plugin_owned": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()

    def replace_before_unlink(target_home: Path, *, expected: bytes) -> None:
        receipt.write_bytes(replacement)
        original_remove(target_home, expected=expected)

    monkeypatch.setattr(integration, "_remove_receipt", replace_before_unlink)
    removed = integration.uninstall_codex(codex_home=home)

    assert removed["cleanup_complete"] is False
    assert "changed before cleanup completed" in removed["receipt_cleanup_error"]
    assert removed["receipt_preserved_for_retry"] is True
    assert receipt.read_bytes() == replacement
