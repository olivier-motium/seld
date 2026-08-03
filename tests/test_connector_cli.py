from __future__ import annotations

import json
import subprocess
import sys
import webbrowser
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import continuity_kernel.cli as cli
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_onboarding import ConnectorIdentityReview, ConnectorOnboarding
from continuity_kernel.connector_profiles import ConnectorAccessTier
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.errors import SetupError, mark_provider_authorization_may_remain
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

    def alias(
        self,
        connection_id: str,
        alias: str,
        **kwargs: object,
    ) -> dict[str, object]:
        self.calls.append(("alias", {"connection_id": connection_id, "alias": alias, **kwargs}))
        return {"connection_id": connection_id, "status": "alias_updated"}


def _install_fake_onboarding(
    monkeypatch: pytest.MonkeyPatch,
    fake: _Onboarding,
) -> None:
    manager = type("_Manager", (), {"probe_credential_custody": lambda self: None})()
    monkeypatch.setattr(cli, "ConnectorAuthManager", lambda vault: manager)
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


@pytest.mark.parametrize(
    "status",
    (
        "account_selection_required",
        "broader_access_already_connected",
        "broader_access_reauthorization_required",
        "cancelled",
        "credential_invalid_reconnect_required",
        "credential_missing_reconnect_required",
        "credential_pointer_invalid_reconnect_required",
        "different_account",
        "disconnect_cancelled",
        "identity_binding_missing_reconnect_required",
        "oauth_permissions_missing",
        "oauth_permissions_outside_selected_tier",
        "oauth_scope_profile_unrecognized",
        "setup_incomplete",
    ),
)
def test_incomplete_connector_outcomes_are_cli_failures(status: str) -> None:
    failure = cli._result_failure(
        Namespace(command="connectors"),
        {"status": status},
    )

    assert failure is not None
    assert failure[0] == 3


def test_connector_failure_json_is_not_reported_as_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI failure")
    fake = _Onboarding()

    def different_account(connector: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "connector": connector,
            "next": "gsv connectors connect gmail --access read",
            "status": "different_account",
        }

    fake.connect_oauth = different_account  # type: ignore[method-assign]
    _install_fake_onboarding(monkeypatch, fake)

    exit_code = cli.main(
        [
            "--json",
            "--vault",
            str(vault),
            "connectors",
            "connect",
            "gmail",
            "--access",
            "read",
            "--no-browser",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert output["ok"] is False
    assert output["result"]["status"] == "different_account"


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
                "--timeout",
                "42.5",
            ]
        )
        == 0
    )

    _output = capsys.readouterr()
    connector, kwargs = fake.calls[0]
    assert connector == "gmail"
    assert kwargs["access"] == "full"
    assert kwargs["browser_mode"] == "firefox"
    assert kwargs["timeout_seconds"] == 42.5
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


def test_permission_update_is_presented_before_sign_in_without_raw_scopes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._present_permission_update(
        "The existing connection stays active until its replacement is ready."
    )

    stderr = capsys.readouterr().err
    assert "before sign-in" in stderr
    assert "existing connection stays active" in stderr
    assert "https://" not in stderr


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


@pytest.mark.parametrize(
    ("access", "permanent_delete", "gmail_settings_control", "expected_phrases"),
    [
        (
            ConnectorAccessTier.READ,
            False,
            False,
            ("current everyday settings", "Explicit Gmail purge", "not available with Read"),
        ),
        (
            ConnectorAccessTier.FULL,
            False,
            True,
            (
                "everyday settings",
                "administrator delegation is not requested",
                "Explicit Gmail purge: off",
                "recoverable Trash",
                "deleted=true",
                "permanently",
            ),
        ),
        (
            ConnectorAccessTier.FULL,
            True,
            True,
            (
                "everyday settings",
                "administrator delegation is not requested",
                "Explicit Gmail purge: ON",
                "skipping the Trash",
                "cannot be undone",
            ),
        ),
    ],
)
def test_gmail_confirmation_discloses_permission_update_and_delete_semantics(
    access: ConnectorAccessTier,
    permanent_delete: bool,
    gmail_settings_control: bool,
    expected_phrases: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = ConnectorIdentityReview(
        connector="gmail",
        provider="google",
        access=access,
        display_label="Ada <ada@example.test>",
        permanent_delete=permanent_delete,
        gmail_settings_control=gmail_settings_control,
        permission_update=(
            "Replaces the existing connection after the current permissions are ready."
        ),
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert cli._confirm_connector_identity(review) is False
    stderr = capsys.readouterr().err
    assert "Permission update:" in stderr
    assert "existing connection" in stderr
    assert "https://" not in stderr
    for phrase in expected_phrases:
        assert phrase in stderr


@pytest.mark.parametrize(
    ("access", "calendar_list_control", "expected_phrases"),
    [
        (
            ConnectorAccessTier.READ,
            False,
            ("Google Calendar Read", "calendar-list settings", "changes are off"),
        ),
        (
            ConnectorAccessTier.FULL,
            True,
            ("Google Calendar Full", "visibility", "reminder", "subscription"),
        ),
        (
            ConnectorAccessTier.FULL,
            False,
            (
                "Legacy Google Calendar Full",
                "visible but not changeable",
                "keeps the current grant",
                "gsv connectors status google_calendar",
            ),
        ),
    ],
)
def test_google_calendar_confirmation_explains_calendar_list_authority(
    access: ConnectorAccessTier,
    calendar_list_control: bool,
    expected_phrases: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = ConnectorIdentityReview(
        connector="google_calendar",
        provider="google",
        access=access,
        display_label="Ada <ada@example.test>",
        calendar_list_control=calendar_list_control,
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert cli._confirm_connector_identity(review) is False
    stderr = capsys.readouterr().err
    for phrase in expected_phrases:
        assert phrase in stderr


def test_legacy_google_confirmation_is_honest_about_settings_and_permanent_deletion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = ConnectorIdentityReview(
        connector="google",
        provider="google",
        access=ConnectorAccessTier.FULL,
        display_label="Ada <ada@example.test>",
        permanent_delete=True,
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert cli._confirm_connector_identity(review) is False
    stderr = capsys.readouterr().err
    assert "Legacy Gmail Full: mailbox changes only" in stderr
    assert "does not add everyday settings control" in stderr
    assert "gsv connectors status gmail" in stderr
    assert "Explicit Gmail purge: ON" in stderr
    assert "Gmail messages" in stderr
    assert "cannot be undone" in stderr


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
    assert result["status"] == "disconnect_cancelled"
    assert result["next"] == f"gsv connectors status {args.connection_id}"
    assert fake.calls == []

    args.yes = True
    result = cli._connectors(vault, args)
    assert result["status"] == "disconnected_locally"
    assert fake.calls[0][0] == "disconnect"


def test_preconsent_connector_interrupt_returns_130_without_provider_claim(
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
    assert "Cancelled." in stderr
    assert "provider access" not in stderr
    assert "installed apps" not in stderr


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


def test_alias_command_parser_and_route_carry_expected_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = cli._parser()
    connection_id = "con-" + "e" * 32
    parsed = parser.parse_args(
        [
            "connectors",
            "alias",
            connection_id,
            "--alias",
            "Work Mail",
            "--expected-revision",
            "revision-before",
        ]
    )
    assert parsed.connection_id == connection_id
    assert parsed.alias == "Work Mail"
    assert parsed.expected_revision == "revision-before"

    vault = tmp_path / "vault"
    Vault(vault).initialize(name="Connector CLI")
    fake = _Onboarding()
    _install_fake_onboarding(monkeypatch, fake)
    assert (
        cli.main(
            [
                "--vault",
                str(vault),
                "connectors",
                "alias",
                connection_id,
                "--alias",
                "Work Mail",
                "--expected-revision",
                "revision-before",
            ]
        )
        == 0
    )
    assert fake.calls[-1] == (
        "alias",
        {
            "connection_id": connection_id,
            "alias": "Work Mail",
            "expected_revision": "revision-before",
        },
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
    assert fake.calls[-1][1]["browser_mode"] == "manual"

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
    assert fake.calls[-1][1]["browser_mode"] == "manual"
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

    monkeypatch.setattr(webbrowser, "get", unavailable)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", "darwin")

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
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "check": False,
                "timeout": 5,
            },
        )
    ]


def test_firefox_opener_returns_manual_fallback_when_non_macos_browser_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = Namespace(browser="firefox", no_browser=False)
    monkeypatch.setattr(webbrowser, "get", lambda name: (_ for _ in ()).throw(webbrowser.Error()))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        subprocess,
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
        error = KeyboardInterrupt()
        mark_provider_authorization_may_remain(error)
        raise error

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
    assert "provider sign-in may have started" in stderr
    assert "connectors list" in stderr
    assert "reauthorizing" in stderr


@pytest.mark.parametrize("post_consent", [False, True])
def test_connector_oauth_failure_guidance_is_phase_aware(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    post_consent: bool,
) -> None:
    def fail(_args: Namespace) -> object:
        error = SetupError("synthetic connector failure")
        if post_consent:
            mark_provider_authorization_may_remain(error)
        raise error

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
    assert payload["error"] == "synthetic connector failure"
    if post_consent:
        assert payload["provider_access_may_remain"] is True
        assert payload["revocation_help"]
    else:
        assert "provider_access_may_remain" not in payload
        assert "revocation_help" not in payload


def test_preconsent_custody_failure_has_no_provider_cleanup_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="Pre-consent failure")
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    monkeypatch.setattr(
        manager,
        "probe_credential_custody",
        lambda: (_ for _ in ()).throw(SetupError("synthetic keyring failure")),
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail(f"registration reached for {provider}"),
    )
    monkeypatch.setattr(cli, "ConnectorAuthManager", lambda current_vault: manager)
    monkeypatch.setattr(cli, "ConnectorOnboarding", lambda current_manager: onboarding)

    result = cli.main(
        [
            "--json",
            "--vault",
            str(vault_path),
            "connectors",
            "connect",
            "gmail",
            "--access",
            "read",
        ]
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload == {"error": "synthetic keyring failure", "ok": False}


def test_preconsent_custody_interrupt_is_json_safe_and_has_no_cleanup_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "vault"
    vault = Vault(vault_path)
    vault.initialize(name="Pre-consent interruption")
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    monkeypatch.setattr(
        manager,
        "probe_credential_custody",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    onboarding = ConnectorOnboarding(
        manager,
        registration_loader=lambda provider: pytest.fail(f"registration reached for {provider}"),
    )
    monkeypatch.setattr(cli, "ConnectorAuthManager", lambda current_vault: manager)
    monkeypatch.setattr(cli, "ConnectorOnboarding", lambda current_manager: onboarding)

    result = cli.main(
        [
            "--json",
            "--vault",
            str(vault_path),
            "connectors",
            "connect",
            "gmail",
            "--access",
            "read",
        ]
    )

    assert result == 130
    payload = json.loads(capsys.readouterr().err)
    assert payload == {"error": "cancelled", "ok": False}
