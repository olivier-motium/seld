from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel import bridge as bridge_module
from continuity_kernel import cli, resident_import
from continuity_kernel import vault_backup as vault_backup_module
from continuity_kernel.atomic import durable_replace as actual_durable_replace
from continuity_kernel.codex_integration import CodexInstallResult
from continuity_kernel.config import config_path, load_config, save_config
from continuity_kernel.errors import SetupError, ValidationError
from continuity_kernel.sense_sweep import SweepRecallStatus
from continuity_kernel.source_state import ABSENT_SOURCE_REVISION
from continuity_kernel.vault import Vault


def test_scheduled_recall_refresh_defers_without_load_average(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "recall-refresh-vault")
    vault.initialize(name="Recall refresh without load average")
    monkeypatch.delattr(os, "getloadavg", raising=False)

    assert cli._scheduled_recall_refresh(vault) == SweepRecallStatus(
        False,
        None,
        False,
        "deferred_budget",
    )


def test_cli_resident_activation_survives_a_fresh_process(tmp_path: Path) -> None:
    vault_path = tmp_path / "resident-activation-vault"
    vault = Vault(vault_path)
    vault.initialize(name="Resident activation")
    resident = vault_path / "context/resident"
    skill = resident / "skills/exact-native/references"
    skill.mkdir(parents=True)
    guidance = "# Resident guidance\n\nRead this exact imported file.\n"
    (resident / "AGENTS.md").write_bytes(guidance.encode("utf-8"))
    (skill.parent / "SKILL.md").write_bytes(
        b"---\nname: exact-native\ndescription: Exact native skill.\n---\n\n# Exact\n"
    )
    (skill / "proof.md").write_bytes(b"# Proof\n\nNative reference.\n")
    control = resident / "control"
    control.mkdir()
    (control / "PULSE").write_text("legacy-private-task\n", encoding="utf-8")
    task = vault.create_task(
        identifier="fresh-process-task",
        title="Fresh process task",
        outcome="Remain structurally discoverable.",
        status="doing",
        next_actor="agent",
        next_action="Read through a new process.",
    )
    task = vault._update_task_dispatch(
        task.identifier,
        status="doing",
        active_thread_id="fresh-process-hand",
        expected_revision=task.revision,
    )
    vault.create_thread(
        identifier="thread:fresh-process",
        title="Fresh process thread",
        purpose="Carry the exact activation proof.",
        summary="The proof is active.",
        focus_task_id=task.identifier,
        task_ids=(task.identifier,),
    )

    def run(*arguments: str) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuity_kernel",
                "--json",
                "--vault",
                str(vault_path),
                *arguments,
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=os.environ.copy(),
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        return cast(dict[str, Any], json.loads(completed.stdout)["result"])

    status = run("resident-context", "status")
    shown = run("resident-context", "show", "--format", "json")
    bindings = run("execution-bindings")

    assert shown["content"] == guidance
    assert status["skills_total"] == 1
    assert status["skills"][0]["name"] == "exact-native"
    assert status["excluded_paths"] == ["context/resident/control"]
    assert "legacy-private-task" not in repr(status)
    assert bindings["active_hands"] == [
        {
            "active_thread_id": "fresh-process-hand",
            "revision": task.revision,
            "status": "doing",
            "task_id": task.identifier,
        }
    ]
    assert bindings["focused_threads"][0]["focus_task_id"] == task.identifier


def test_cli_pulse_sweep_is_visible_from_a_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "pulse-vault"
    host_data = tmp_path / "host-data"
    Vault(vault_path).initialize(name="Fresh process Pulse")
    monkeypatch.setenv("GSV_DATA_DIR", str(host_data))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        cli,
        "_scheduled_recall_refresh",
        lambda _vault: SweepRecallStatus(True, True, True, None),
    )

    assert cli.main(["--json", "--vault", str(vault_path), "pulse", "sweep"]) == 0
    swept = json.loads(capsys.readouterr().out)["result"]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault_path),
            "pulse",
            "status",
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=os.environ.copy(),
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    restarted = json.loads(completed.stdout)["result"]
    assert swept["status"] == "complete"
    assert swept["recall"] == {
        "attempted": True,
        "changed": True,
        "failure": None,
        "updated": True,
    }
    assert restarted["heartbeat"]["observed_at"] == swept["observed_at"]
    assert restarted["heartbeat"]["sequence"] == 1
    assert restarted["signals"]["pending"] == 0


def test_cli_scheduler_plan_pins_executable_vault_and_mechanical_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "scheduler-vault"
    executable = tmp_path / "installed-gsv"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    Vault(vault_path).initialize(name="Scheduler provenance")
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    monkeypatch.setattr(cli, "current_gsv_executable", lambda: executable)
    scheduler_type = cast(Any, cli).MacOSScheduler

    def macos_scheduler(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(uid=501, platform="darwin")
        return scheduler_type(*args, **kwargs)

    monkeypatch.setattr(cli, "MacOSScheduler", macos_scheduler)

    assert cli.main(["--json", "--vault", str(vault_path), "scheduler", "plan"]) == 0
    plan = json.loads(capsys.readouterr().out)["result"]

    assert plan["command"] == [
        str(executable),
        "--vault",
        str(vault_path.resolve()),
        "--json",
        "pulse",
        "sweep",
    ]
    assert plan["interval_seconds"] == 600
    assert not (tmp_path / "host-data").exists()


@pytest.mark.parametrize("command", ["install", "run-canary", "uninstall"])
def test_cli_scheduler_mutations_require_exact_receipt_cas(command: str) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["scheduler", command])


def test_cli_local_source_migration_uses_only_vault_staged_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "staged-local-source"
    Vault(vault_path).initialize(name="Staged local source")
    calls: list[tuple[str, str | None]] = []

    def staged_status(vault: Vault, *, delivery: object) -> dict[str, object]:
        del delivery
        calls.append(("status", str(vault.root)))
        return {"checkpoints": [], "migration_revision": "a" * 64}

    def adopt_staged(
        vault: Vault,
        *,
        source: str,
        expected_migration_revision: str,
        expected_source_revision: str,
        disposition: str,
        delivery: object,
    ) -> dict[str, object]:
        del delivery
        assert expected_migration_revision == "a" * 64
        assert expected_source_revision == "b" * 64
        assert disposition == "adopt_verified_prefix"
        calls.append(("adopt", source))
        return {"adopted": True, "source": source}

    monkeypatch.setattr(resident_import, "staged_local_source_checkpoint_status", staged_status)
    monkeypatch.setattr(resident_import, "adopt_staged_local_source_checkpoint", adopt_staged)

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "local-source",
                "staged-status",
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)["result"]
    assert status["migration_revision"] == "a" * 64
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "local-source",
                "adopt-staged",
                "--source",
                "apple_messages",
                "--expected-migration-revision",
                "a" * 64,
                "--expected-source-revision",
                "b" * 64,
                "--disposition",
                "adopt_verified_prefix",
            ]
        )
        == 0
    )
    adopted = json.loads(capsys.readouterr().out)["result"]

    assert adopted == {"adopted": True, "source": "apple_messages"}
    assert calls == [("status", str(vault_path.resolve())), ("adopt", "apple_messages")]
    default_args = cli._parser().parse_args(["local-source", "staged-status"])
    explicit_args = cli._parser().parse_args(
        [
            "local-source",
            "staged-status",
            "--service-label",
            "ai.example.explicit-sync",
        ]
    )
    assert default_args.service_label is None
    assert explicit_args.service_label == "ai.example.explicit-sync"
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "local-source",
                "adopt-staged",
                "--source",
                "apple_messages",
                "--expected-migration-revision",
                "a" * 64,
                "--expected-source-revision",
                "b" * 64,
                "--disposition",
                "adopt_verified_prefix",
                "--prior-cursor",
                "raw-provider-cursor",
            ]
        )


def test_fresh_cli_reports_an_absent_staged_migration_without_blocking_baseline(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "fresh-local-source"
    vault = Vault(vault_path)
    vault.initialize(name="Fresh local source")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "continuity_kernel",
            "--json",
            "--vault",
            str(vault_path),
            "local-source",
            "staged-status",
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=os.environ.copy(),
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    status = json.loads(completed.stdout)["result"]
    assert status == {
        "checkpoints": [],
        "migration_revision": resident_import.ABSENT_LOCAL_SOURCE_MIGRATION_REVISION,
        "source_revision": vault.get_source_snapshot().revision,
        "vault_id": vault.identity()["vault_id"],
    }
    assert not (vault_path / ".gsv/migrations/local-source-checkpoints.json").exists()


def test_cli_signal_append_rejects_provider_text_and_accepts_only_record_pointer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "signal-vault"
    Vault(vault_path).initialize(name="Signal privacy")

    exit_code = cli.main(
        [
            "--json",
            "--vault",
            str(vault_path),
            "signal",
            "append",
            "--record-ref",
            "Email body with sk-live-secret",
            "--change-type",
            "observation",
        ]
    )

    assert exit_code == 2
    failure = json.loads(capsys.readouterr().err)
    assert "canonical result reference must name one typed record revision" in failure["error"]
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "signal",
                "append",
                "--kind",
                "provider-message",
                "--envelope-json",
                '{"body":"raw provider body"}',
            ]
        )


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


def test_cli_source_selection_read_and_fresh_process_visibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "source-vault"
    assert cli.main(["--json", "--vault", str(vault), "init"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "source",
                "select",
                "--expected-revision",
                ABSENT_SOURCE_REVISION,
                "--source",
                "gmail",
            ]
        )
        == 0
    )
    selected = json.loads(capsys.readouterr().out)["result"]
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "source",
                "record",
                "--expected-revision",
                selected["revision"],
                "--source",
                "gmail",
                "--actor-ref",
                "fresh-chatgpt-task",
                "--result",
                "explicit_empty",
                "--covered-through",
                "2026-07-28T10:00:00Z",
                "--completeness",
                "complete",
                "--account-binding",
                "workspace:test-account",
                "--tool-binding",
                "gmail.search.v1",
            ]
        )
        == 0
    )
    recorded = json.loads(capsys.readouterr().out)["result"]

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "source",
                "record",
                "--expected-revision",
                selected["revision"],
                "--source",
                "gmail",
                "--actor-ref",
                "stale-chatgpt-task",
                "--result",
                "failure",
                "--error-code",
                "timeout",
            ]
        )
        == 2
    )
    stale_error = json.loads(capsys.readouterr().err)
    assert "record changed" in stale_error["error"]

    assert cli.main(["--json", "--vault", str(vault), "source", "list"]) == 0
    restarted = json.loads(capsys.readouterr().out)["result"]
    assert restarted["state"]["revision"] == recorded["revision"]
    assert restarted["state"]["sources"][0]["observation"]["result"] == "explicit_empty"
    assert any(item["source"] == "gmail" for item in restarted["catalog"])


@pytest.mark.skipif(os.name == "nt", reason="secure descriptor-pinned reads are POSIX-only")
def test_cli_local_file_read_is_bounded_and_does_not_mutate_vault(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault = tmp_path / "vault"
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "note.txt").write_text("A bounded local note.", encoding="utf-8")
    assert cli.main(["--json", "--vault", str(vault), "init"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "source",
                "select",
                "--expected-revision",
                ABSENT_SOURCE_REVISION,
                "--source",
                "local_files",
            ]
        )
        == 0
    )
    capsys.readouterr()
    before = Vault(vault).logical_digest()

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "local-file",
                "grant",
                "--root",
                str(selected),
            ]
        )
        == 0
    )
    granted = json.loads(capsys.readouterr().out)["result"]["grant"]

    assert cli.main(["--json", "--vault", str(vault), "local-file", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)["result"]
    assert listed["source_selected"] is True
    assert [item["grant_id"] for item in listed["grants"]] == [granted["grant_id"]]

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "local-file",
                "read",
                "--grant-id",
                granted["grant_id"],
                "--relative-path",
                "note.txt",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)["result"]

    assert result["content"] == "A bounded local note."
    assert result["tool_binding"] == "gsv_local_file_read"
    assert result["persisted"] is False
    assert Vault(vault).logical_digest() == before

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "local-file",
                "revoke",
                "--grant-id",
                granted["grant_id"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["revoked"] is True
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault),
                "local-file",
                "read",
                "--grant-id",
                granted["grant_id"],
                "--relative-path",
                "note.txt",
            ]
        )
        == 2
    )
    assert "not found for this vault" in json.loads(capsys.readouterr().err)["error"]


def test_cli_authors_rank_hand_and_complete_portfolio(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault_path = tmp_path / "portfolio-vault"
    assert cli.main(["--json", "--vault", str(vault_path), "init"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "task",
                "create",
                "--id",
                "ranked-outcome",
                "--title",
                "Ranked outcome",
                "--outcome",
                "Keep exact authored order.",
                "--rank",
                "3",
                "--active-thread-id",
                "exact-hand",
            ]
        )
        == 0
    )
    task = json.loads(capsys.readouterr().out)["result"]

    item = json.dumps(
        {
            "reason": "The user should decide deliberately.",
            "stance": "needs-human",
            "task_id": task["identifier"],
            "task_revision": task["revision"],
        }
    )
    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault_path),
                "portfolio",
                "set",
                "--expected-revision",
                "absent",
                "--summary",
                "One complete open outcome.",
                "--item-json",
                item,
            ]
        )
        == 0
    )
    portfolio = json.loads(capsys.readouterr().out)["result"]
    assert portfolio["items"][0]["task_id"] == "ranked-outcome"

    assert cli.main(["--json", "--vault", str(vault_path), "portfolio", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)["result"]
    assert shown == portfolio
    assert task["rank"] == 3
    assert task["active_thread_id"] == "exact-hand"


def test_cli_does_not_advertise_unsupported_control_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "CONTROL_STORE_SUPPORTED", False)

    parser = cli._parser()
    with pytest.raises(SystemExit) as stopped:
        parser.parse_args(["operation", "list"])

    assert stopped.value.code == 2


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


def test_setup_failure_preserves_a_concurrent_configuration_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested = tmp_path / "requested-vault"
    concurrent = tmp_path / "concurrent-vault"

    @contextmanager
    def fail_after_concurrent_change(**_: object) -> Iterator[CodexInstallResult]:
        save_config(concurrent)
        raise SetupError("injected Codex failure")
        yield cast(CodexInstallResult, None)  # pragma: no cover

    monkeypatch.setattr(cli, "install_codex_transaction", fail_after_concurrent_change)

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
    assert "changed concurrently and was left untouched" in capsys.readouterr().err
    configured = load_config()
    assert configured is not None
    assert configured.vault_path == concurrent.resolve()


def test_setup_keeps_committed_configuration_when_browser_launch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "existing-vault"
    requested = tmp_path / "requested-vault"
    Vault(existing).initialize(name="Existing")
    save_config(existing)

    monkeypatch.setattr(
        cli,
        "open_bridge",
        lambda *_args, **_kwargs: {"running": True, "started": True},
    )
    monkeypatch.setattr(
        cli,
        "open_bridge_in_browser",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SetupError("browser stage failed")),
    )
    stopped: list[bool] = []
    monkeypatch.setattr(cli, "stop_bridge", lambda: stopped.append(True))

    result = cli.main(["--json", "--vault", str(requested), "setup", "--no-codex"])
    output = json.loads(capsys.readouterr().out)["result"]

    assert result == 0
    assert output["setup_complete"] is True
    assert output["bridge"]["running"] is True
    assert output["bridge"]["browser_opened"] is False
    assert output["bridge"]["browser_error"] == "browser stage failed"
    assert "run `gsv` to open it" in output["next"]
    assert stopped == []
    committed = load_config()
    assert committed is not None
    assert committed.vault_path == requested.resolve()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_setup_refuses_fifo_configuration_without_blocking(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("FIFO creation is unavailable")
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    os.mkfifo(configuration)

    assert cli.main(["--vault", str(tmp_path / "vault"), "setup", "--no-codex"]) == 2
    assert stat.S_ISFIFO(os.lstat(configuration).st_mode)


def test_setup_reuses_configured_vault_when_no_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configured = tmp_path / "custom-vault"
    save_config(configured)

    result = cli.main(["--json", "setup", "--no-codex", "--no-bridge"])
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert Path(output["result"]["vault"]["vault"]) == configured.resolve()
    assert "ChatGPT integration was skipped" in output["result"]["next"]
    assert "Restart the ChatGPT desktop app" not in output["result"]["next"]


def test_unhealthy_setup_preserves_config_and_never_calls_provider_or_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "existing-vault"
    corrupt = tmp_path / "corrupt-vault"
    Vault(existing).initialize(name="Existing")
    Vault(corrupt).initialize(name="Corrupt")
    (corrupt / "tasks/broken.md").write_text("not a GSV task\n", encoding="utf-8")
    save_config(existing)
    events: list[str] = []

    @contextmanager
    def unexpected_install(**_: object) -> Iterator[CodexInstallResult]:
        events.append("codex")
        yield cast(CodexInstallResult, None)

    def unexpected_bridge(*_: object, **__: object) -> dict[str, object]:
        events.append("bridge")
        return {}

    def unexpected_browser(*_: object, **__: object) -> bool:
        events.append("browser")
        return True

    monkeypatch.setattr(cli, "install_codex_transaction", unexpected_install)
    monkeypatch.setattr(cli, "open_bridge", unexpected_bridge)
    monkeypatch.setattr(cli, "open_bridge_in_browser", unexpected_browser)

    exit_code = cli.main(["--json", "--vault", str(corrupt), "setup"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["ok"] is False
    assert payload["error"] == (
        "Seld setup stopped because the local record is unhealthy; follow result.next."
    )
    assert payload["result"]["setup_complete"] is False
    assert payload["result"]["doctor"]["healthy"] is False
    assert payload["result"]["doctor"]["issues"][0]["path"] == "tasks/broken.md"
    assert payload["result"]["codex"] is None
    assert payload["result"]["bridge"] is None
    assert f"gsv --vault {shlex.quote(str(corrupt.resolve()))} doctor" in payload["result"]["next"]
    assert events == []
    config = load_config()
    assert config is not None
    assert config.vault_path == existing.resolve()


def test_setup_next_text_matches_every_feature_flag_combination() -> None:
    for no_codex in (False, True):
        for no_bridge in (False, True):
            for no_browser in (False, True):
                bridge = None if no_bridge else {"browser_opened": not no_browser}
                message = cli._setup_next(
                    no_codex=no_codex,
                    no_bridge=no_bridge,
                    no_browser=no_browser,
                    bridge=bridge,
                )
                if no_codex:
                    assert "ChatGPT integration was skipped" in message
                    assert "Restart the ChatGPT desktop app" not in message
                    assert "$gsv-onboard" not in message
                else:
                    assert "Restart the ChatGPT desktop app" in message
                    assert "$gsv-onboard" in message
                    assert "ChatGPT integration was skipped" not in message
                if no_bridge:
                    assert "The Bridge was not started" in message
                    assert "The Bridge is running" not in message
                    assert "The Bridge is open" not in message
                elif no_browser:
                    assert "The Bridge is running locally" in message
                    assert "The Bridge is open" not in message
                else:
                    assert "The Bridge is open" in message


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
    assert events == ["codex-ready", "codex-committed", "bridge-started", "browser-opened"]
    assert output["result"]["bridge"]["browser_opened"] is True


def test_setup_keeps_committed_integration_and_reports_bridge_start_failure(
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

    def fail_bridge(*_args: object, **_kwargs: object) -> dict[str, object]:
        events.append("bridge-failed")
        raise OSError("injected Bridge start failure")

    monkeypatch.setattr(cli, "install_codex_transaction", staged_install)
    monkeypatch.setattr(cli, "open_bridge", fail_bridge)
    stopped: list[bool] = []
    monkeypatch.setattr(cli, "stop_bridge", lambda: stopped.append(True))

    vault_path = tmp_path / "vault"
    result = cli.main(["--json", "--vault", str(vault_path), "setup"])
    output = json.loads(capsys.readouterr().out)["result"]

    assert result == 4
    assert events == ["codex-ready", "codex-committed", "bridge-failed"]
    assert stopped == []
    assert output["setup_complete"] is True
    assert output["bridge"]["running"] is False
    assert output["bridge"]["error"] == "injected Bridge start failure"
    assert "did not start" in output["next"]
    assert load_config() is not None


@pytest.mark.skipif(os.name == "nt", reason="synthetic prior executable is a POSIX script")
def test_rollback_check_proves_stable_exact_vault_and_control_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Rollback compatibility")
    vault_id = str(vault.identity()["vault_id"])
    digest = vault.logical_digest()
    previous = tmp_path / "previous-gsv"
    previous.write_text(
        f"""#!/bin/sh
case "${{4:-}}" in
  status) printf '%s\\n' '{{"ok":true,"result":{{"vault_id":"{vault_id}","digest":"{digest}"}}}}' ;;
  doctor) printf '%s\\n' '{{"ok":true,"result":{{"healthy":true,"vault_id":"{vault_id}"}}}}' ;;
  operation) printf '%s\\n' '{{"ok":true,"result":{{"pending":[],"decided":[]}}}}' ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    previous.chmod(0o755)

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault.root),
                "rollback-check",
                str(previous),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)["result"]
    assert result == {
        "compatible": True,
        "digest": digest,
        "reason_code": None,
        "vault_id": vault_id,
    }


@pytest.mark.skipif(os.name == "nt", reason="synthetic prior executable is a POSIX script")
@pytest.mark.parametrize("failure", ("incompatible", "malformed", "drift", "timeout"))
def test_rollback_check_fails_closed_on_old_reader_or_probe_ambiguity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name=f"Rollback {failure}")
    vault_id = str(vault.identity()["vault_id"])
    digest = vault.logical_digest()
    counter = tmp_path / "status-count"
    previous = tmp_path / "previous-gsv"
    if failure == "timeout":
        body = "sleep 1\n"
        monkeypatch.setattr(cli, "ROLLBACK_PROBE_TIMEOUT_SECONDS", 0.05)
    elif failure == "malformed":
        body = "printf 'not-json\\n'\n"
    elif failure == "incompatible":
        body = """case "${4:-}" in
  status) printf '{"ok":true,"result":{"vault_id":"wrong","digest":"wrong"}}\\n' ;;
  doctor) printf '{"ok":false,"error":"unsupported task record version 2"}\\n'; exit 3 ;;
  operation) printf '{"ok":true,"result":{"pending":[]}}\\n' ;;
esac
"""
    else:
        monkeypatch.setenv("GSV_TEST_STATUS_COUNT", str(counter))
        body = f"""case "${{4:-}}" in
  status)
    count=0
    [ ! -f "$GSV_TEST_STATUS_COUNT" ] || count="$(cat "$GSV_TEST_STATUS_COUNT")"
    count=$((count + 1))
    printf '%s\\n' "$count" > "$GSV_TEST_STATUS_COUNT"
    observed='{digest}'
    [ "$count" -eq 1 ] || observed='{"f" * 64}'
    printf '{{"ok":true,"result":{{"vault_id":"{vault_id}","digest":"%s"}}}}\\n' "$observed"
    ;;
  doctor) printf '{{"ok":true,"result":{{"healthy":true}}}}\\n' ;;
  operation) printf '{{"ok":true,"result":{{"pending":[]}}}}\\n' ;;
esac
"""
    previous.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    previous.chmod(0o755)

    result = cli._rollback_compatibility(vault, previous)

    assert result["compatible"] is False
    assert result["reason_code"] in {
        "previous_reader_probe_failed",
        "previous_reader_state_mismatch",
    }


def test_setup_keeps_committed_install_when_browser_open_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    current_state: bridge_module.BridgeState | None = None

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
        nonlocal current_state
        current_state = bridge_module.BridgeState(
            format_version=bridge_module.STATE_VERSION,
            instance_id="a" * 32,
            pid=4242,
            port=43117,
            token="b" * 48,
            url="http://127.0.0.1:43117/",
            vault=str(vault.root),
            vault_id=str(vault.status()["vault_id"]),
        )
        return {"running": True, "started": True, "url": current_state.url}

    monkeypatch.setattr(cli, "install_codex_transaction", staged_install)
    monkeypatch.setattr(cli, "open_bridge", started)

    def current() -> tuple[
        bridge_module.BridgeState | None,
        bool,
        bool,
        dict[str, object],
    ]:
        assert current_state is not None
        metadata = os.lstat(current_state.vault)
        return (
            current_state,
            True,
            False,
            {
                "vault_id": current_state.vault_id,
                "vault_root_device": int(metadata.st_dev),
                "vault_root_inode": int(metadata.st_ino),
            },
        )

    monkeypatch.setattr(bridge_module, "_current_state", current)

    def browser_failure(*_: object, **__: object) -> bool:
        raise OSError("injected browser failure")

    monkeypatch.setattr(bridge_module, "_launch_browser", browser_failure)

    result = cli.main(["--json", "--vault", str(tmp_path / "vault"), "setup"])
    output = json.loads(capsys.readouterr().out)["result"]

    assert result == 0
    assert events == ["codex-ready", "codex-committed"]
    assert output["bridge"]["browser_opened"] is False
    assert "run `gsv`" in output["next"]
    assert load_config() is not None


def test_direct_bridge_open_survives_browser_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Browser failure proof")
    save_config(vault.root)
    state = bridge_module.BridgeState(
        format_version=bridge_module.STATE_VERSION,
        instance_id="a" * 32,
        pid=4242,
        port=43117,
        token="b" * 48,
        url="http://127.0.0.1:43117/",
        vault=str(vault.root),
        vault_id=str(vault.status()["vault_id"]),
    )
    metadata = os.lstat(vault.root)
    health = {
        "vault_id": state.vault_id,
        "vault_root_device": int(metadata.st_dev),
        "vault_root_inode": int(metadata.st_ino),
    }
    monkeypatch.setattr(
        bridge_module,
        "_current_state",
        lambda: (state, True, False, health),
    )

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


def test_unhealthy_doctor_returns_nonzero_and_json_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unhealthy")
    (vault.root / "tasks/broken.md").write_text("not a GSV task\n", encoding="utf-8")
    save_config(vault.root)
    monkeypatch.setattr(cli, "codex_status", lambda **_: {"plugin_installed": False})

    exit_code = cli.main(["--json", "doctor"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["ok"] is False
    assert payload["error"] == (
        "Seld doctor found unresolved integrity issues; follow result.issues."
    )
    assert payload["result"]["healthy"] is False
    assert payload["result"]["issues"][0]["path"] == "tasks/broken.md"


def test_doctor_never_recursively_repairs_retained_restore_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Repairable")
    orphan = vault.root.parent / f".{vault.root.name}.tmp-restore-crash"
    orphan.mkdir()
    (orphan / "partial").write_text("partial", encoding="utf-8")
    save_config(vault.root)
    monkeypatch.setattr(cli, "codex_status", lambda **_: {"plugin_installed": False})

    exit_code = cli.main(["--json", "doctor", "--repair"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["ok"] is False
    assert payload["result"]["healthy"] is False
    assert payload["result"]["repaired"] == []
    assert payload["result"]["issues"][0]["path"] == f"../{orphan.name}"
    assert payload["result"]["issues"][0]["repairable"] is False
    assert (orphan / "partial").read_text(encoding="utf-8") == "partial"


def test_backup_verify_and_restore_do_not_require_readable_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Portable")
    source.create_task(
        identifier="portable-task",
        title="Portable task",
        outcome="Restore without prior configuration.",
    )
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    configuration.write_bytes(b"{broken configuration")
    before = configuration.read_bytes()

    assert cli.main(["--json", "backup", "verify", str(backup)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert verified["result"]["valid"] is True

    target = tmp_path / "restored"
    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)

    assert restored["ok"] is True
    assert restored["result"]["configuration_changed"] is False
    assert restored["result"]["configuration_matches_target"] == "unknown"
    assert restored["result"]["activation_required"] is True
    assert restored["result"]["activation_commands"] == [
        "gsv bridge stop",
        f"gsv --vault {shlex.quote(str(target.resolve()))} setup",
    ]
    assert "Codex plus The Bridge" in restored["result"]["next"]
    assert configuration.read_bytes() == before

    assert cli.main(["--json", "--vault", str(target), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["result"]["vault_id"] == source.identity()["vault_id"]


def test_restore_keeps_published_result_when_configuration_is_invalid_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Portable")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    before = b"\xff\xfeinvalid utf-8 configuration"
    configuration.write_bytes(before)
    target = tmp_path / "restored"

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)

    assert restored["ok"] is True
    assert restored["result"]["published"] is True
    assert restored["result"]["configuration_matches_target"] == "unknown"
    assert configuration.read_bytes() == before
    assert Vault(target).doctor().healthy is True


def test_configuration_match_probe_treats_filesystem_failure_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable_configuration(*, required: bool = False) -> None:
        del required
        raise PermissionError("injected unreadable configuration")

    monkeypatch.setattr(cli, "load_config", unreadable_configuration)

    assert cli._configuration_matches_target(tmp_path / "restored") == "unknown"


def test_invalid_utf8_configuration_is_structured_for_loader_and_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    configuration.write_bytes(b"\xff\xfeinvalid utf-8 configuration")

    with pytest.raises(ValidationError, match="invalid Seld configuration"):
        load_config()

    assert cli.main(["--json", "status"]) == 2
    output = capsys.readouterr()
    payload = json.loads(output.err)
    assert payload == {
        "error": f"invalid Seld configuration: {configuration}",
        "ok": False,
    }


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_restore_publishes_without_opening_fifo_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if sys.platform == "win32":
        pytest.skip("FIFO creation is unavailable")
    source = Vault(tmp_path / "source")
    source.initialize(name="FIFO recovery")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    os.mkfifo(configuration)
    target = tmp_path / "restored"

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)

    assert restored["result"]["published"] is True
    assert restored["result"]["configuration_matches_target"] == "unknown"
    assert stat.S_ISFIFO(os.lstat(configuration).st_mode)
    assert Vault(target).doctor().healthy is True


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require elevated privileges")
def test_restore_publishes_with_dangling_configuration_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Symlink recovery")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    missing = configuration.parent / "missing-config.json"
    configuration.symlink_to(missing)
    target = tmp_path / "restored"

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)

    assert restored["result"]["published"] is True
    assert restored["result"]["configuration_matches_target"] == "unknown"
    assert configuration.is_symlink()
    assert os.readlink(configuration) == str(missing)
    assert Vault(target).doctor().healthy is True


def test_restore_publishes_when_configured_vault_contains_nul(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Invalid path recovery")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    before = (json.dumps({"format_version": 1, "vault": "bad\x00path"}) + "\n").encode()
    configuration.write_bytes(before)
    target = tmp_path / "restored"

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)

    assert restored["result"]["published"] is True
    assert restored["result"]["configuration_matches_target"] == "unknown"
    assert configuration.read_bytes() == before
    assert Vault(target).doctor().healthy is True

    assert cli.main(["--json", "status"]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["ok"] is False
    assert failure["error"] == "Seld configuration has an invalid records path"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require elevated privileges")
def test_restore_publishes_when_configured_vault_is_a_symlink_loop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Loop recovery")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    loop = tmp_path / "loop"
    loop.symlink_to(loop.name)
    configuration = config_path()
    configuration.parent.mkdir(parents=True)
    before = (json.dumps({"format_version": 1, "vault": str(loop)}) + "\n").encode()
    configuration.write_bytes(before)
    target = tmp_path / "restored"

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)

    assert restored["result"]["published"] is True
    assert restored["result"]["configuration_matches_target"] == "unknown"
    assert configuration.read_bytes() == before
    assert loop.is_symlink()
    assert Vault(target).doctor().healthy is True


def test_restore_keeps_existing_configured_vault_until_explicit_setup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configured = Vault(tmp_path / "configured")
    configured.initialize(name="Configured")
    source = Vault(tmp_path / "source")
    source.initialize(name="Restored")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    save_config(configured.root)
    config_before = config_path().read_bytes()
    target = tmp_path / "restored"

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    capsys.readouterr()
    assert config_path().read_bytes() == config_before

    assert cli.main(["--json", "status"]) == 0
    bare_status = json.loads(capsys.readouterr().out)
    assert bare_status["result"]["vault_id"] == configured.identity()["vault_id"]

    assert cli.main(["--json", "--vault", str(target), "status"]) == 0
    explicit_status = json.loads(capsys.readouterr().out)
    assert explicit_status["result"]["vault_id"] == source.identity()["vault_id"]


def test_restore_reports_when_existing_configuration_already_matches_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Recovered")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    target = tmp_path / "restored"
    save_config(target)
    before = config_path().read_bytes()

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)

    assert restored["result"]["configuration_changed"] is False
    assert restored["result"]["configuration_matches_target"] is True
    assert config_path().read_bytes() == before
    assert cli.main(["--json", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["result"]["vault_id"] == source.identity()["vault_id"]


def test_restore_matches_configured_target_through_filesystem_case_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Case identity recovery")
    backup = Path(source.create_backup(tmp_path / "portable.zip")["backup"])
    configured_alias = tmp_path / "restored"
    target = tmp_path / "Restored"
    save_config(configured_alias)

    assert cli.main(["--json", "backup", "restore", str(backup), str(target)]) == 0
    restored = json.loads(capsys.readouterr().out)
    if not configured_alias.exists() or not os.path.samefile(configured_alias, target):
        pytest.skip("filesystem is case-sensitive")

    assert restored["result"]["configuration_matches_target"] is True
    assert cli._configuration_matches_target(target) is True


def test_invalid_backup_verification_returns_nonzero_json_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = Vault(tmp_path / "source")
    source.initialize(name="Tamper proof")
    original = Path(source.create_backup(tmp_path / "original.zip")["backup"])
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(original, "r") as archive:
        entries = {item.filename: archive.read(item.filename) for item in archive.infolist()}
    entries["MIND.md"] += b"tampered"
    with zipfile.ZipFile(tampered, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    exit_code = cli.main(["--json", "backup", "verify", str(tampered)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["ok"] is False
    assert payload["error"] == "Seld backup verification failed; do not restore this archive."
    assert payload["result"]["valid"] is False


def test_unverified_backup_creation_returns_nonzero_json_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Configured")
    save_config(vault.root)
    monkeypatch.setattr(
        Vault,
        "create_backup",
        lambda _self, _destination=None: {
            "backup": str(tmp_path / "invalid.zip"),
            "verified": False,
        },
    )

    exit_code = cli.main(["--json", "backup", "create"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["ok"] is False
    assert payload["error"] == (
        "Seld backup creation did not verify; do not use the reported archive."
    )


@pytest.mark.parametrize(
    ("unlink_first", "expected_state"),
    [
        (False, "the staged archive remains"),
        (True, "the staged archive is no longer visible, but deletion durability is unconfirmed"),
    ],
)
def test_backup_cleanup_failure_is_visible_in_cli_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unlink_first: bool,
    expected_state: str,
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Cleanup truth")
    destination = tmp_path / "backup.zip"

    def fail_source_capture(_: Path, **_kwargs: Any) -> bytes:
        raise ValidationError("injected source capture failure")

    def fail_cleanup(path: Path) -> None:
        if unlink_first:
            path.unlink()
        raise OSError("injected cleanup durability failure")

    monkeypatch.setattr(vault_backup_module, "_read_backup_source", fail_source_capture)
    monkeypatch.setattr(vault_backup_module, "durable_unlink", fail_cleanup)

    exit_code = cli.main(
        [
            "--json",
            "--vault",
            str(vault.root),
            "backup",
            "create",
            "--output",
            str(destination),
        ]
    )
    payload = json.loads(capsys.readouterr().err)

    assert exit_code == 2
    assert payload["ok"] is False
    assert "injected source capture failure" in payload["error"]
    assert "injected cleanup durability failure" in payload["error"]
    assert expected_state in payload["error"]
    assert ".gsv-backup.tmp-" in payload["error"]
    assert not destination.exists()
    assert bool(list(tmp_path.glob(".gsv-backup.tmp-*.zip"))) is (not unlink_first)


def test_backup_staging_allocation_failure_is_structured_cli_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unwritable output")
    destination = tmp_path / "unwritable" / "backup.zip"
    destination.parent.mkdir()

    def fail_staging(*_: object, **__: object) -> tuple[int, str]:
        raise PermissionError("injected unwritable output directory")

    monkeypatch.setattr(vault_backup_module, "_mkstemp", fail_staging)

    exit_code = cli.main(
        [
            "--json",
            "--vault",
            str(vault.root),
            "backup",
            "create",
            "--output",
            str(destination),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error"] == (
        f"could not allocate a staged backup beside {destination}: "
        "injected unwritable output directory"
    )
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert not destination.exists()


@pytest.mark.parametrize("failure_point", ["descriptor-close", "zip-open", "temp-open", "fsync"])
def test_backup_staging_io_failures_are_structured_and_cleaned(
    failure_point: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Staging I/O")
    destination = tmp_path / "backup.zip"
    injected = f"injected {failure_point} failure"

    if failure_point == "descriptor-close":
        real_close = os.close
        real_mkstemp = vault_backup_module._mkstemp
        staged_descriptor: int | None = None
        failed = False

        def capture_staged_descriptor(*args: Any, **kwargs: Any) -> tuple[int, str]:
            nonlocal staged_descriptor
            descriptor, path = real_mkstemp(*args, **kwargs)
            staged_descriptor = descriptor
            return descriptor, path

        def fail_close(descriptor: int) -> None:
            nonlocal failed
            real_close(descriptor)
            if descriptor == staged_descriptor and not failed:
                failed = True
                raise PermissionError(injected)

        monkeypatch.setattr(vault_backup_module, "_mkstemp", capture_staged_descriptor)
        monkeypatch.setattr(os, "close", fail_close)
    elif failure_point == "zip-open":

        def fail_zip(*_: object, **__: object) -> zipfile.ZipFile:
            raise PermissionError(injected)

        monkeypatch.setattr(zipfile, "ZipFile", fail_zip)
    elif failure_point == "temp-open":
        real_open = Path.open

        def fail_temp_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> Any:
            if path.name.startswith(".gsv-backup.tmp-") and mode == "r+b":
                raise PermissionError(injected)
            return cast(Any, real_open)(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_temp_open)
    else:
        real_fsync = os.fsync
        real_mkstemp = vault_backup_module._mkstemp
        staged_identity: tuple[int, int] | None = None
        failed = False

        def capture_staged_identity(*args: Any, **kwargs: Any) -> tuple[int, str]:
            nonlocal staged_identity
            descriptor, path = real_mkstemp(*args, **kwargs)
            metadata = os.fstat(descriptor)
            staged_identity = (int(metadata.st_dev), int(metadata.st_ino))
            return descriptor, path

        def fail_staged_fsync(descriptor: int) -> None:
            nonlocal failed
            metadata = os.fstat(descriptor)
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            if not failed and identity == staged_identity:
                failed = True
                raise PermissionError(injected)
            real_fsync(descriptor)

        monkeypatch.setattr(vault_backup_module, "_mkstemp", capture_staged_identity)
        monkeypatch.setattr(os, "fsync", fail_staged_fsync)

    exit_code = cli.main(
        [
            "--json",
            "--vault",
            str(vault.root),
            "backup",
            "create",
            "--output",
            str(destination),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["ok"] is False
    assert injected in payload["error"]
    assert "no backup was published" in payload["error"]
    assert "durably discarded" in payload["error"]
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert not destination.exists()
    assert not list(tmp_path.glob(".gsv-backup.tmp-*.zip"))


def test_backup_staging_io_and_cleanup_failures_are_both_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Staging cleanup")
    destination = tmp_path / "backup.zip"

    def fail_zip(*_: object, **__: object) -> zipfile.ZipFile:
        raise PermissionError("injected disk write failure")

    def fail_cleanup(_: Path) -> None:
        raise PermissionError("injected staged cleanup failure")

    monkeypatch.setattr(zipfile, "ZipFile", fail_zip)
    monkeypatch.setattr(vault_backup_module, "durable_unlink", fail_cleanup)

    exit_code = cli.main(
        [
            "--json",
            "--vault",
            str(vault.root),
            "backup",
            "create",
            "--output",
            str(destination),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["ok"] is False
    assert "injected disk write failure" in payload["error"]
    assert "injected staged cleanup failure" in payload["error"]
    assert "the staged archive remains" in payload["error"]
    assert "Traceback" not in captured.err
    assert not destination.exists()
    assert len(list(tmp_path.glob(".gsv-backup.tmp-*.zip"))) == 1


def test_backup_commands_report_symlink_loop_paths_as_structured_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Path validation")
    valid_backup = Path(vault.create_backup(tmp_path / "valid.zip")["backup"])
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - restricted Windows environments
        pytest.skip(f"symbolic links unavailable: {exc}")

    cases = (
        (
            [
                "--json",
                "--vault",
                str(vault.root),
                "backup",
                "create",
                "--output",
                str(loop / "created.zip"),
            ],
            "invalid backup output path",
        ),
        (["--json", "backup", "verify", str(loop / "missing.zip")], "invalid backup path"),
        (
            [
                "--json",
                "backup",
                "restore",
                str(valid_backup),
                str(loop / "restored"),
            ],
            "invalid restore target",
        ),
    )

    for arguments, expected in cases:
        exit_code = cli.main(arguments)
        captured = capsys.readouterr()
        payload = json.loads(captured.err)

        assert exit_code == 2
        assert payload["ok"] is False
        assert expected in payload["error"]
        assert str(loop) in payload["error"]
        assert "Traceback" not in captured.err
        assert captured.out == ""


def test_restore_target_parent_failure_is_structured_cli_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Restore parent")
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_bytes(b"preserve\n")
    target = blocked_parent / "restored"

    exit_code = cli.main(["--json", "backup", "restore", str(backup), str(target)])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error"].startswith(f"could not prepare the restore target parent for {target}:")
    assert "Traceback" not in captured.err
    assert blocked_parent.read_bytes() == b"preserve\n"


def test_restore_stage_allocation_failure_is_structured_cli_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Restore allocation")
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored"

    def fail_stage(*_: object, **__: object) -> str:
        raise PermissionError("injected restore stage allocation failure")

    monkeypatch.setattr(vault_backup_module, "_mkdtemp", fail_stage)

    exit_code = cli.main(["--json", "backup", "restore", str(backup), str(target)])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error"] == (
        f"could not allocate a restore stage beside {target}: "
        "injected restore stage allocation failure"
    )
    assert "Traceback" not in captured.err
    assert not target.exists()


def test_restore_target_swap_is_a_structured_degraded_cli_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Restore target swap")
    backup = Path(vault.create_backup(tmp_path / "backup.zip")["backup"])
    target = tmp_path / "restored"
    displaced = tmp_path / ".restored.published-displaced"

    def publish_then_swap(source: Path, destination: Path) -> None:
        actual_durable_replace(source, destination)
        if destination == target and source.name.startswith(".restored.tmp-restore-"):
            destination.replace(displaced)
            destination.mkdir()
            (destination / "replacement.txt").write_bytes(b"preserve\n")

    monkeypatch.setattr(vault_backup_module, "durable_replace", publish_then_swap)

    exit_code = cli.main(["--json", "backup", "restore", str(backup), str(target)])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 2
    assert payload["ok"] is False
    assert "post-publication verification" in payload["error"]
    assert str(target) in payload["error"]
    assert "Traceback" not in captured.err
    assert (target / "replacement.txt").read_bytes() == b"preserve\n"
    assert Vault(displaced).logical_digest() == vault.logical_digest()


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
        return {"cleanup_complete": True, "user_data_preserved": True}

    monkeypatch.setattr(cli, "codex_status", status)
    monkeypatch.setattr(cli, "uninstall_codex", uninstall)

    assert cli.main(["--json", "codex", "status", "--codex-home", str(home)]) == 0
    capsys.readouterr()
    assert cli.main(["--json", "codex", "uninstall", "--codex-home", str(home)]) == 0

    assert seen == [("status", home.resolve()), ("uninstall", home.resolve())]


def test_partial_codex_uninstall_prints_result_and_returns_retry_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "codex"
    monkeypatch.setattr(
        cli,
        "uninstall_codex",
        lambda **_: {
            "cleanup_complete": False,
            "next": "Re-run `gsv codex uninstall`.",
            "provider_cleanup_error": "Codex timed out",
        },
    )

    assert cli.main(["--json", "codex", "uninstall", "--codex-home", str(home)]) == 3
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is False
    assert payload["error"] == "Seld cleanup is incomplete; follow result.next and retry."
    assert payload["result"]["cleanup_complete"] is False
    assert payload["result"]["provider_cleanup_error"] == "Codex timed out"


def test_partial_codex_uninstall_keeps_recovery_details_in_human_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "codex"
    monkeypatch.setattr(
        cli,
        "uninstall_codex",
        lambda **_: {
            "cleanup_complete": False,
            "manual_review_required": True,
            "next": "Inspect the changed files, then re-run `gsv codex uninstall`.",
            "provider_cleanup_error": "Codex timed out",
        },
    )

    assert cli.main(["codex", "uninstall", "--codex-home", str(home)]) == 3
    captured = capsys.readouterr()

    assert '"manual_review_required": true' in captured.out
    assert "Inspect the changed files" in captured.out
    assert '"provider_cleanup_error": "Codex timed out"' in captured.out
    assert "cleanup is incomplete" in captured.err
