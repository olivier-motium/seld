from __future__ import annotations

import json
import webbrowser
from argparse import Namespace
from pathlib import Path

import pytest

from continuity_kernel import cli
from continuity_kernel.connector_onboarding import ConnectorIdentityReview
from continuity_kernel.connector_profiles import ConnectorAccessTier
from continuity_kernel.vault import Vault


class _Onboarding:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list(self) -> dict[str, object]:
        return {"connections": [], "revision": "absent"}

    def status(self, target: str | None) -> dict[str, object]:
        return {"connections": [], "target": target}

    def connect_oauth(self, connector: str, **kwargs: object) -> dict[str, object]:
        self.calls.append((connector, kwargs))
        return {"connector": connector, "status": "connected"}

    def connect_discord(self, token: bytes, **kwargs: object) -> dict[str, object]:
        self.calls.append(("discord", {**kwargs, "token": token}))
        return {"connector": "discord", "status": "connected"}

    def disconnect(self, connection_id: str) -> dict[str, object]:
        self.calls.append(("disconnect", {"connection_id": connection_id}))
        return {"connection_id": connection_id, "status": "disconnected_locally"}


def _install_fake_onboarding(
    monkeypatch: pytest.MonkeyPatch,
    fake: _Onboarding,
) -> None:
    monkeypatch.setattr(cli, "ConnectorAuthManager", lambda vault: object())
    monkeypatch.setattr(cli, "ConnectorOnboarding", lambda manager: fake)


def test_connector_list_and_status_are_first_class_json_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)

    assert cli.main(["--json", "--vault", str(vault), "connectors", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["result"]["connections"] == []

    assert cli.main(["--json", "--vault", str(vault), "connectors", "status", "google_drive"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["result"]["target"] == "google_drive"


def test_oauth_connect_passes_firefox_and_manual_url_fallback_without_credentials_in_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)
    opened: list[str] = []

    class _Firefox:
        def open(self, url: str) -> bool:
            opened.append(url)
            return True

    monkeypatch.setattr(webbrowser, "get", lambda name: _Firefox())
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "connectors",
                "connect",
                "gmail",
                "--access",
                "full",
                "--browser",
                "firefox",
            ]
        )
        == 0
    )

    _output = capsys.readouterr()
    connector, kwargs = fake.calls[0]
    assert connector == "gmail"
    assert kwargs["access"] == "full"
    opener = kwargs["browser_opener"]
    assert callable(opener) and opener("https://accounts.example/authorize") is True
    assert opened == ["https://accounts.example/authorize"]
    assert "client_id" not in kwargs and "token" not in kwargs


def test_identity_confirmation_shows_exact_account_and_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = ConnectorIdentityReview(
        connector="outlook_mail",
        provider="microsoft",
        access=ConnectorAccessTier.FULL,
        display_label="Ada <ada@example.test>",
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert cli._confirm_connector_identity(review) is False
    stderr = capsys.readouterr().err
    assert "Ada <ada@example.test>" in stderr
    assert "Outlook Mail" in stderr
    assert "Full" in stderr


def test_disconnect_requires_confirmation_and_explains_local_only_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    args = Namespace(
        connectors_command="disconnect",
        connection_id="con-" + "a" * 32,
        yes=False,
    )

    result = cli._connectors(vault, args)
    assert result["status"] == "cancelled"
    assert fake.calls == []

    args.yes = True
    result = cli._connectors(vault, args)
    assert result["status"] == "disconnected_locally"
    assert fake.calls[0][0] == "disconnect"


def test_connector_interrupt_returns_130_with_safe_recovery_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()

    def interrupted(connector: str, **kwargs: object) -> dict[str, object]:
        del connector, kwargs
        raise KeyboardInterrupt

    fake.connect_oauth = interrupted  # type: ignore[method-assign]
    _install_fake_onboarding(monkeypatch, fake)
    result = cli.main(
        [
            "--vault",
            str(vault),
            "connectors",
            "connect",
            "slack",
            "--access",
            "read",
            "--no-browser",
        ]
    )

    assert result == 130
    assert "connectors list" in capsys.readouterr().err
