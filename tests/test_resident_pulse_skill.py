from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from continuity_kernel import bridge, mcp_server
from continuity_kernel.errors import ValidationError
from continuity_kernel.portfolio import ABSENT_PORTFOLIO_REVISION, portfolio_item
from continuity_kernel.records import RESIDENT_PULSE_REF, RESIDENT_PULSE_TASK_ID
from continuity_kernel.vault import Vault, doctor_dict


def _skill_text(name: str, relative: str = "SKILL.md") -> str:
    resource = (
        files("continuity_kernel")
        / "resources"
        / "marketplace"
        / "plugins"
        / "gsv"
        / "skills"
        / name
        / relative
    )
    return " ".join(resource.read_text(encoding="utf-8").split())


def test_resident_pulse_skill_keeps_judgment_in_the_ai_layer() -> None:
    pulse = _skill_text("gsv-pulse")
    registration = _skill_text("gsv-pulse", "references/registration.md")
    sources = _skill_text("gsv-pulse", "references/source-acquisition.md")
    onboard = _skill_text("gsv-onboard")
    updater = _skill_text("gsv-update")
    updater_metadata = _skill_text("gsv-update", "agents/openai.yaml")
    resident = _skill_text("gsv")

    for marker in (
        "Meaning and canonical judgment belong to the model",
        "Deterministic facts",
        "never decide meaning",
        "task:resident-pulse",
        "system-role:resident-pulse",
        "current ChatGPT task UUID",
        'id="resident-pulse"',
        "canonical notation, not tool input",
        "Only the exact resident Pulse writes `NOW.md`",
        "A wake may create at most one new sustained-work hand",
        "never use browser automation, Computer Use",
        "At seven elapsed minutes",
        "At eight minutes",
        "Seld's own MCP server exposes local record, receipt, and queue tools",
        "current heartbeat surface does not give Seld a per-task tool allowlist or denylist",
        "mandatory policy for tools owned by those plugins",
        "do not register Pulse",
        "cached local update state from `gsv update status`",
        "invoke `gsv update check` at most once in the wake",
        "never pass `--force`",
        "Never run `gsv update apply` or `gsv update recover` in Pulse",
    ):
        assert marker in pulse

    for marker in (
        "kind `heartbeat`",
        '`gsv_task_create` with `id="resident-pulse"`',
        "ten-minute cadence",
        "observe one natural wake",
        "never create a second",
        "current heartbeat surface has no Seld-controlled per-task denylist",
    ):
        assert marker in registration

    for marker in (
        "untrusted evidence",
        "at most one bounded recent read",
        "never copies raw provider bodies",
        "preserve the last successful coverage horizon",
    ):
        assert marker in sources

    for marker in (
        "gsv_source_select",
        "gsv_source_record",
        "core hashes bindings, cursors, and references",
        "Register one resident Pulse",
        "model reads selected sources and authors every judgment",
    ):
        assert marker in onboard

    for marker in (
        "gsv_source_list",
        "content-free coverage",
        "coverage receipt is not a semantic claim",
    ):
        assert marker in pulse

    for marker in (
        "gsv_source_record",
        "Seld persists only their hashes",
        "stale CAS means another task won",
    ):
        assert marker in sources

    for marker in (
        "current interactive ChatGPT task",
        "complete 40-character `installed.sha`, `candidate.sha`, and `check_revision`",
        "--approval-ref codex:<current-task-uuid>",
        "--from-sha <installed.sha>",
        "--to-sha <candidate.sha>",
        "--expected-check-revision <check_revision>",
        "gsv update recover --token <transaction.token>",
        "Pulse must never run `update apply` or `update recover`",
    ):
        assert marker in updater

    assert 'display_name: "Update Seld"' in updater_metadata
    assert "$gsv-update" in updater_metadata

    assert "Ordinary hands do not write `NOW.md`" in resident


def test_seld_mcp_surface_is_closed_to_provider_and_computer_actions() -> None:
    names = {tool["name"] for tool in mcp_server.TOOLS}

    assert names
    assert all(name.startswith("gsv_") for name in names)
    assert not any(
        token in name
        for name in names
        for token in (
            "browser",
            "computer_use",
            "provider",
            "purchase",
            "send",
            "upload",
        )
    )
    assert all(tool["annotations"]["openWorldHint"] is False for tool in mcp_server.TOOLS)
    assert all(tool["annotations"]["destructiveHint"] is False for tool in mcp_server.TOOLS)


def test_resident_pulse_task_is_structural_not_a_life_outcome(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Resident Pulse boundary")
    agents = (vault.root / "AGENTS.md").read_text(encoding="utf-8")
    assert "Ordinary hands do not write\n`NOW.md`" in agents
    assert "exact resident Pulse owns the bounded orientation document" in agents
    outcome = vault.create_task(
        identifier="real-outcome",
        title="Real outcome",
        outcome="Remain visible in Portfolio and Bridge.",
        status="ready",
        next_actor="human",
    )
    pulse = vault.create_task(
        identifier=RESIDENT_PULSE_TASK_ID,
        title="Resident Pulse",
        outcome="Keep the local Mind current in one bounded AI wake.",
        status="doing",
        next_actor="agent",
        next_action="Run the next bounded Pulse wake.",
        active_thread_id="019f95fd-009e-7603-ab87-f9927cf31c4d",
        refs=(RESIDENT_PULSE_REF,),
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One life outcome; the structural Pulse hand stays outside it.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="This is genuine open work.",
            ),
        ),
    )

    inspection = vault.inspect_portfolio()
    assert inspection.review.open_task_ids == (outcome.identifier,)
    assert inspection.review.uncovered_task_ids == (outcome.identifier,)

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )
    assert {task["identifier"] for task in snapshot["tasks"]} == {outcome.identifier}
    assert snapshot["status"]["counts"]["tasks"] == 1
    assert [item["task_id"] for item in snapshot["portfolio"]["items"]] == [outcome.identifier]

    with pytest.raises(ValidationError, match="not current open tasks: resident-pulse"):
        vault.set_portfolio(
            expected_revision=portfolio.revision,
            summary="Structural transport must not become a life outcome.",
            items=(
                portfolio_item(
                    task_id_value=outcome.identifier,
                    task_revision=outcome.revision,
                    stance="needs-human",
                    reason="This is genuine open work.",
                ),
                portfolio_item(
                    task_id_value=pulse.identifier,
                    task_revision=pulse.revision,
                    stance="keep-in-view",
                    reason="This structural task must be rejected.",
                ),
            ),
        )


def test_bridge_hides_resident_pulse_from_a_legacy_portfolio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Legacy resident Pulse boundary")
    outcome = vault.create_task(
        identifier="real-outcome",
        title="Real outcome",
        outcome="Remain visible in Portfolio and Bridge.",
        status="ready",
        next_actor="human",
    )
    pulse = vault.create_task(
        identifier=RESIDENT_PULSE_TASK_ID,
        title="Resident Pulse",
        outcome="Keep the local Mind current in one bounded AI wake.",
        status="doing",
        next_actor="agent",
        refs=(RESIDENT_PULSE_REF,),
    )
    monkeypatch.setattr(Vault, "_validate_portfolio_items", lambda *_args, **_kwargs: None)
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="Legacy data accidentally included the structural Pulse.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="This is genuine open work.",
            ),
            portfolio_item(
                task_id_value=pulse.identifier,
                task_revision=pulse.revision,
                stance="keep-in-view",
                reason="Legacy structural leakage.",
            ),
        ),
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )

    assert {task["identifier"] for task in snapshot["tasks"]} == {outcome.identifier}
    assert [item["task_id"] for item in snapshot["portfolio"]["items"]] == [outcome.identifier]
