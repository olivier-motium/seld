from __future__ import annotations

import json
import webbrowser
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from continuity_kernel import cli
from continuity_kernel.connector_onboarding import ConnectorIdentityReview
from continuity_kernel.connector_profiles import ConnectorAccessTier
from continuity_kernel.errors import SetupError
from continuity_kernel.vault import Vault


class _Onboarding:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list(self) -> dict[str, object]:
        return {"connections": [], "revision": "absent"}

    def registration_readiness(self) -> dict[str, dict[str, str]]:
        return {
            "google": {"sign_in": "available", "status": "ready"},
            "microsoft": {"sign_in": "available", "status": "ready"},
            "slack": {"sign_in": "available", "status": "ready"},
        }

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

    def resume(self, connection_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("resume", {"connection_id": connection_id, **kwargs}))
        return {"connection_id": connection_id, "status": "connected"}

    def reauthorize_oauth(self, connection_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("reauthorize", {"connection_id": connection_id, **kwargs}))
        return {"connection_id": connection_id, "status": "connected"}


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

    assert cli.main(["--json", "--vault", str(vault), "connectors", "readiness"]) == 0
    readiness = json.loads(capsys.readouterr().out)["result"]
    assert readiness == {
        "oauth_registration_ready": True,
        "registration_readiness": fake.registration_readiness(),
        "vault_healthy_independent": True,
    }


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


def test_authorization_url_is_always_printed_even_when_browser_opened(
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "https://accounts.example/authorize?state=opaque"
    cli._present_authorization_url(url, True)
    stderr = capsys.readouterr().err
    assert "browser is open" in stderr
    assert "safe to copy" in stderr
    assert url in stderr


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
    stderr = capsys.readouterr().err
    assert "provider access may remain" in stderr
    assert "installed apps" in stderr
    assert "connectors list" in stderr


def test_connector_resume_and_gmail_purge_step_up_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)
    connection_id = "con-" + "a" * 32

    assert (
        cli.main(
            [
                "--vault",
                str(vault),
                "connectors",
                "resume",
                connection_id,
                "--alias",
                "Work Mail",
            ]
        )
        == 0
    )
    assert fake.calls[-1][0] == "resume"
    assert fake.calls[-1][1]["alias"] == "Work Mail"

    assert (
        cli.main(
            [
                "--vault",
                str(vault),
                "connectors",
                "connect",
                "gmail",
                "--access",
                "full",
                "--with-permanent-delete",
                "--no-browser",
            ]
        )
        == 0
    )
    assert fake.calls[-1][1]["include_permanent_delete"] is True


def test_connect_parser_routes_connection_selector_and_rejects_conflicting_flags() -> None:
    parser = cli._parser()
    connection_id = "con-" + "a" * 32
    parsed = parser.parse_args(
        [
            "connectors",
            "connect",
            "gmail",
            "--access",
            "read",
            "--connection-id",
            connection_id,
        ]
    )
    assert parsed.connection_id == connection_id
    assert parsed.new_account is False
    assert parsed.browser is None

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "connectors",
                "connect",
                "gmail",
                "--access",
                "read",
                "--connection-id",
                connection_id,
                "--new-account",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "connectors",
                "connect",
                "gmail",
                "--access",
                "read",
                "--browser",
                "firefox",
                "--no-browser",
            ]
        )


def test_connect_selector_and_reauthorize_route_through_same_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)
    connection_id = "con-" + "b" * 32

    assert (
        cli.main(
            [
                "--vault",
                str(vault),
                "connectors",
                "connect",
                "gmail",
                "--access",
                "read",
                "--connection-id",
                connection_id,
                "--no-browser",
            ]
        )
        == 0
    )
    assert fake.calls[-1][1]["connection_id"] == connection_id

    assert (
        cli.main(
            [
                "--vault",
                str(vault),
                "connectors",
                "reauthorize",
                connection_id,
                "--alias",
                "Work Mail",
                "--no-browser",
            ]
        )
        == 0
    )
    assert fake.calls[-1][0] == "reauthorize"
    assert fake.calls[-1][1]["connection_id"] == connection_id
    assert fake.calls[-1][1]["alias"] == "Work Mail"
    assert callable(fake.calls[-1][1]["confirm_identity"])


def test_discord_rejects_browser_flags_instead_of_ignoring_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)

    result = cli.main(
        [
            "--vault",
            str(vault),
            "connectors",
            "connect",
            "discord",
            "--access",
            "full",
            "--no-browser",
        ]
    )

    assert result == 2
    assert fake.calls == []
    assert "browser options" in capsys.readouterr().err

    result = cli.main(
        [
            "--vault",
            str(vault),
            "connectors",
            "connect",
            "discord",
            "--access",
            "full",
            "--connection-id",
            "con-" + "d" * 32,
        ]
    )

    assert result == 2
    assert fake.calls == []
    assert "--connection-id" in capsys.readouterr().err


def test_discord_rejects_oauth_timeout_instead_of_ignoring_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)

    result = cli.main(
        [
            "--vault",
            str(vault),
            "connectors",
            "connect",
            "discord",
            "--access",
            "full",
            "--timeout",
            "30",
        ]
    )

    assert result == 2
    assert fake.calls == []
    assert "--timeout is unavailable" in capsys.readouterr().err


def test_firefox_opener_is_lazy_and_uses_bounded_macos_bundle_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = Namespace(browser="firefox", no_browser=False)
    looked_up: list[str] = []
    subprocess_calls: list[tuple[list[str], dict[str, object]]] = []

    def unavailable(name: str) -> object:
        looked_up.append(name)
        raise webbrowser.Error("Firefox is unavailable")

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        subprocess_calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.webbrowser, "get", unavailable)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    opener = cli._connector_browser_opener(args)
    assert looked_up == []
    assert callable(opener)
    assert opener("https://accounts.example/authorize") is True
    assert looked_up == ["firefox"]
    assert subprocess_calls == [
        (
            [
                "open",
                "-b",
                "org.mozilla.firefox",
                "-u",
                "https://accounts.example/authorize",
            ],
            {
                "stdin": cli.subprocess.DEVNULL,
                "stdout": cli.subprocess.DEVNULL,
                "stderr": cli.subprocess.DEVNULL,
                "check": False,
                "timeout": 5,
            },
        )
    ]


def test_firefox_opener_returns_manual_fallback_when_non_macos_browser_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = Namespace(browser="firefox", no_browser=False)
    monkeypatch.setattr(
        cli.webbrowser, "get", lambda name: (_ for _ in ()).throw(webbrowser.Error())
    )
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("macOS fallback reached"),
    )

    opener = cli._connector_browser_opener(args)
    assert callable(opener)
    assert opener("https://accounts.example/authorize") is False


def test_manual_authorization_message_explains_same_computer_wait(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._present_authorization_url("https://accounts.example/authorize", False)
    stderr = capsys.readouterr().err
    assert "on this computer" in stderr
    assert "command keeps running" in stderr


def test_connector_reauthorize_interrupt_has_recovery_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()

    def interrupted(connection_id: str, **kwargs: object) -> dict[str, object]:
        del connection_id, kwargs
        raise KeyboardInterrupt

    fake.reauthorize_oauth = interrupted  # type: ignore[method-assign]
    _install_fake_onboarding(monkeypatch, fake)
    connection_id = "con-" + "c" * 32

    result = cli.main(
        [
            "--vault",
            str(vault),
            "connectors",
            "reauthorize",
            connection_id,
            "--no-browser",
        ]
    )

    assert result == 130
    stderr = capsys.readouterr().err
    assert "provider access may remain" in stderr
    assert "connectors list" in stderr
    assert "reauthorizing" in stderr


def test_connector_oauth_failure_has_provider_cleanup_guidance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_args: Namespace) -> object:
        raise SetupError("synthetic post-consent failure")

    monkeypatch.setattr(cli, "_dispatch", fail)

    result = cli.main(
        [
            "--json",
            "connectors",
            "connect",
            "gmail",
            "--access",
            "read",
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "synthetic post-consent failure"
    assert payload["provider_access_may_remain"] is True
    assert payload["revocation_help"]
