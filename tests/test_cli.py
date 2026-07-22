from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from continuity_kernel import bridge as bridge_module
from continuity_kernel import cli
from continuity_kernel.codex_integration import CodexInstallResult
from continuity_kernel.config import config_path, load_config, save_config
from continuity_kernel.errors import SetupError
from continuity_kernel.vault import Vault


def test_cli_json_task_lifecycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    assert cli.main(["--json", "--vault", str(vault), "init", "--configure"]) == 0
    capsys.readouterr()

    assert (
        cli.main(
            [
                "--json",
                "task",
                "create",
                "--id",
                "cli-proof",
                "--title",
                "CLI proof",
                "--outcome",
                "Machine-readable output works.",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)

    assert created["ok"] is True
    assert created["result"]["identifier"] == "cli-proof"


def test_setup_codex_failure_does_not_replace_existing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "existing-vault"
    requested = tmp_path / "requested-vault"
    save_config(existing)

    @contextmanager
    def fail_install(**_: object) -> Iterator[CodexInstallResult]:
        raise SetupError("injected Codex failure")
        yield cast(CodexInstallResult, None)  # pragma: no cover

    monkeypatch.setattr(cli, "install_codex_transaction", fail_install)
    result = cli.main(
        [
            "--vault",
            str(requested),
            "setup",
            "--no-bridge",
            "--codex-home",
            str(tmp_path / "codex"),
        ]
    )

    assert result == 2
    assert "injected Codex failure" in capsys.readouterr().err
    config = load_config()
    assert config is not None
    assert config.vault_path == existing.resolve()
    assert requested.exists()


def test_setup_codex_failure_removes_new_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested = tmp_path / "requested-vault"

    @contextmanager
    def fail_install(**_: object) -> Iterator[CodexInstallResult]:
        raise SetupError("injected Codex failure")
        yield cast(CodexInstallResult, None)  # pragma: no cover

    monkeypatch.setattr(cli, "install_codex_transaction", fail_install)
    result = cli.main(
        [
            "--vault",
            str(requested),
            "setup",
            "--no-bridge",
            "--codex-home",
            str(tmp_path / "codex"),
        ]
    )

    assert result == 2
    assert "injected Codex failure" in capsys.readouterr().err
    assert not config_path().exists()
    assert requested.exists()


def test_setup_reuses_configured_vault_when_no_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configured = tmp_path / "custom-vault"
    save_config(configured)

    result = cli.main(["--json", "setup", "--no-codex", "--no-bridge"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert Path(output["result"]["vault"]["vault"]) == configured.resolve()


def test_failed_codex_install_never_starts_or_opens_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested = tmp_path / "requested-vault"
    opened: list[str] = []

    def unexpected_server(*_args: object, **_kwargs: object) -> dict[str, object]:
        opened.append("server")
        return {}

    def unexpected_browser(*_args: object, **_kwargs: object) -> bool:
        opened.append("browser")
        return True

    monkeypatch.setattr(cli, "open_bridge", unexpected_server)
    monkeypatch.setattr(cli, "open_bridge_in_browser", unexpected_browser)

    @contextmanager
    def fail_install(**_: object) -> Iterator[CodexInstallResult]:
        raise SetupError("injected Codex failure")
        yield cast(CodexInstallResult, None)  # pragma: no cover

    monkeypatch.setattr(
        cli,
        "install_codex_transaction",
        fail_install,
    )

    result = cli.main(["--vault", str(requested), "setup"])

    assert result == 2
    assert opened == []
    assert "injected Codex failure" in capsys.readouterr().err
    assert not config_path().exists()


def test_successful_setup_starts_bridge_after_codex_check_and_opens_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []

    @contextmanager
    def staged_install(**_: object) -> Iterator[CodexInstallResult]:
        events.append("codex-ready")
        yield CodexInstallResult(
            codex_home=str(tmp_path / "codex"),
            marketplace="gsv-local",
            marketplace_root=str(tmp_path / "marketplace"),
            plugin="gsv@gsv-local",
            plugin_installed=True,
            instructions_installed=True,
            backup=None,
        )
        events.append("codex-committed")

    monkeypatch.setattr(cli, "install_codex_transaction", staged_install)

    def start_bridge(*_args: object, **_kwargs: object) -> dict[str, object]:
        events.append("bridge-started")
        return {
            "browser_opened": False,
            "running": True,
            "started": True,
            "url": "http://127.0.0.1:1234/",
        }

    def open_browser(*_args: object, **_kwargs: object) -> bool:
        events.append("browser-opened")
        return True

    monkeypatch.setattr(cli, "open_bridge", start_bridge)
    monkeypatch.setattr(cli, "open_bridge_in_browser", open_browser)

    result = cli.main(["--json", "--vault", str(tmp_path / "vault"), "setup"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert events == ["codex-ready", "bridge-started", "codex-committed", "browser-opened"]
    assert output["result"]["bridge"]["browser_opened"] is True


def test_setup_keeps_committed_install_when_browser_open_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    current_state: dict[str, object] = {}

    @contextmanager
    def staged_install(**_: object) -> Iterator[CodexInstallResult]:
        events.append("codex-ready")
        yield CodexInstallResult(
            codex_home=str(tmp_path / "codex"),
            marketplace="gsv-local",
            marketplace_root=str(tmp_path / "marketplace"),
            plugin="gsv@gsv-local",
            plugin_installed=True,
            instructions_installed=True,
            backup=None,
        )
        events.append("codex-committed")

    def started(vault: Vault, **_: object) -> dict[str, object]:
        current_state.update(
            {
                "instance_id": "a" * 32,
                "pid": 4242,
                "port": 43117,
                "token": "b" * 48,
                "url": "http://127.0.0.1:43117/",
                "vault": str(vault.root),
                "vault_id": vault.status()["vault_id"],
            }
        )
        return {"running": True, "started": True, "url": current_state["url"]}

    monkeypatch.setattr(cli, "install_codex_transaction", staged_install)
    monkeypatch.setattr(cli, "open_bridge", started)
    monkeypatch.setattr(bridge_module, "_current_state", lambda: (current_state, True, False))

    def browser_failure(*_: object, **__: object) -> bool:
        raise OSError("injected browser failure")

    monkeypatch.setattr(bridge_module, "_launch_browser", browser_failure)

    result = cli.main(["--json", "--vault", str(tmp_path / "vault"), "setup"])
    output = json.loads(capsys.readouterr().out)["result"]

    assert result == 0
    assert events == ["codex-ready", "codex-committed"]
    assert output["bridge"]["browser_opened"] is False
    assert "run gsv" in output["next"]
    assert load_config() is not None


def test_direct_bridge_open_survives_browser_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Browser failure proof")
    save_config(vault.root)
    state = {
        "instance_id": "a" * 32,
        "pid": 4242,
        "port": 43117,
        "token": "b" * 48,
        "url": "http://127.0.0.1:43117/",
        "vault": str(vault.root),
        "vault_id": vault.status()["vault_id"],
    }
    monkeypatch.setattr(bridge_module, "_current_state", lambda: (state, True, False))

    def browser_failure(*_: object, **__: object) -> bool:
        raise OSError("injected browser failure")

    monkeypatch.setattr(bridge_module, "_launch_browser", browser_failure)

    assert cli.main(["--json", "bridge", "open"]) == 0
    output = json.loads(capsys.readouterr().out)["result"]
    assert output["running"] is True
    assert output["browser_opened"] is False
    assert "Run gsv again" in output["next"]


def test_bare_gsv_opens_the_bridge_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configured = tmp_path / "vault"
    configured.mkdir()
    save_config(configured)
    calls: list[tuple[Path, bool]] = []

    def open_configured(vault: Vault, *, open_browser: bool) -> dict[str, object]:
        calls.append((vault.root, open_browser))
        return {"running": True, "started": False, "url": "http://127.0.0.1:1234/"}

    monkeypatch.setattr(cli, "open_bridge", open_configured)

    assert cli.main(["--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [(configured.resolve(), True)]
    assert output["result"]["running"] is True


def test_vault_doctor_remains_usable_without_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault-only"
    assert cli.main(["--vault", str(vault), "init", "--configure"]) == 0
    capsys.readouterr()

    def unavailable(**_: object) -> object:
        raise SetupError("Codex is unavailable")

    monkeypatch.setattr(cli, "codex_status", unavailable)
    assert cli.main(["--json", "doctor"]) == 0
    result = json.loads(capsys.readouterr().out)["result"]

    assert result["healthy"] is True
    assert result["codex"]["available"] is False


def test_codex_status_and_uninstall_do_not_require_vault_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "codex"
    seen: list[tuple[str, Path]] = []

    def status(*, codex_home: Path) -> dict[str, bool]:
        seen.append(("status", codex_home))
        return {"installed": False}

    def uninstall(*, codex_home: Path) -> dict[str, bool]:
        seen.append(("uninstall", codex_home))
        return {"user_data_preserved": True}

    monkeypatch.setattr(cli, "codex_status", status)
    monkeypatch.setattr(cli, "uninstall_codex", uninstall)

    assert cli.main(["--json", "codex", "status", "--codex-home", str(home)]) == 0
    capsys.readouterr()
    assert cli.main(["--json", "codex", "uninstall", "--codex-home", str(home)]) == 0

    assert seen == [("status", home.resolve()), ("uninstall", home.resolve())]
