from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuity_kernel import cli
from continuity_kernel.config import config_path, load_config, save_config
from continuity_kernel.errors import SetupError


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

    def fail_install(**_: object) -> object:
        raise SetupError("injected Codex failure")

    monkeypatch.setattr(cli, "install_codex", fail_install)
    result = cli.main(
        [
            "--vault",
            str(requested),
            "setup",
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

    def fail_install(**_: object) -> object:
        raise SetupError("injected Codex failure")

    monkeypatch.setattr(cli, "install_codex", fail_install)
    result = cli.main(
        [
            "--vault",
            str(requested),
            "setup",
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

    result = cli.main(["--json", "setup", "--no-codex"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert Path(output["result"]["vault"]["vault"]) == configured.resolve()


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
