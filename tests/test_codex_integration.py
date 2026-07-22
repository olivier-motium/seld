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
    assert removed["preexisting_plugin_preserved"] is True
    assert removed["plugin_removed"] is False


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


def test_failed_uninstall_verification_keeps_ownership_receipt_for_retry(
    tmp_path: Path, fake_codex: FakeCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    integration.install_codex(vault=tmp_path / "vault", codex_home=home)
    original_status = integration.codex_status
    calls = 0

    def fail_second_status(*, codex_home: Path | None = None) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SetupError("injected final verification failure")
        return original_status(codex_home=codex_home)

    monkeypatch.setattr(integration, "codex_status", fail_second_status)
    with pytest.raises(SetupError, match="injected"):
        integration.uninstall_codex(codex_home=home)

    assert integration._receipt_path(home).exists()
