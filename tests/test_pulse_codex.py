from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.pulse_codex import (
    COMPACT_TOKENS,
    EFFORT,
    MODEL,
    SERVICE_TIER,
    LunaSession,
    _isolated_configuration,
    _json_value,
    _usage_delta,
)


def test_session_refuses_a_missing_codex_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(ValidationError, match="Codex is unavailable"):
        LunaSession(instructions="Summarize", work_directory=tmp_path)


def test_session_refuses_a_compaction_threshold_below_one_thousand_tokens(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="at least 1000 tokens"):
        LunaSession(
            instructions="Summarize",
            work_directory=tmp_path,
            executable="/opt/codex",
            compact_tokens=999,
        )


def test_new_session_is_idle_until_started(tmp_path: Path) -> None:
    session = LunaSession(
        instructions="Summarize",
        work_directory=tmp_path,
        executable="/opt/codex",
    )

    assert session.executable == "/opt/codex"
    assert session.compact_tokens == COMPACT_TOKENS
    assert session.thread_id is None
    assert session.alive is False


def test_connection_loss_fails_every_waiting_request_and_turn(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        session = LunaSession(
            instructions="Summarize",
            work_directory=tmp_path,
            executable="/opt/codex",
        )
        loop = asyncio.get_running_loop()
        request = loop.create_future()
        finished = loop.create_future()
        finished.set_result({})
        session._pending = {"1": request, "2": finished}
        session._turn_done = loop.create_future()
        session._compaction_done = loop.create_future()

        session._fail_pending()

        messages = []
        for future in (request, session._turn_done, session._compaction_done):
            with pytest.raises(ValidationError) as caught:
                await future
            messages.append(str(caught.value))
        assert finished.result() == {}
        return messages

    assert asyncio.run(scenario()) == [
        "Luna native connection ended",
        "Luna native turn connection ended",
        "Luna compaction connection ended",
    ]


def test_isolated_configuration_disables_inherited_tools_and_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.seld]\ncommand = "gsv"\n\n[mcp_servers.other-tool_2]\ncommand = "x"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    values = _isolated_configuration(4_000)

    assert values["model"] == MODEL
    assert values["model_reasoning_effort"] == EFFORT
    assert values["service_tier"] == SERVICE_TIER
    assert values["model_auto_compact_token_limit"] == 4_000
    assert values["approval_policy"] == "never"
    assert values["web_search"] == "disabled"
    assert values["history.persistence"] == "none"
    assert values["analytics.enabled"] is False
    assert values["otel.log_user_prompt"] is False
    assert values["features.shell_tool"] is False
    assert values["features.computer_use"] is False
    assert values["mcp_servers.seld.enabled"] is False
    assert values["mcp_servers.other-tool_2.enabled"] is False


def test_isolated_configuration_without_a_codex_config_adds_no_mcp_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))

    values = _isolated_configuration(COMPACT_TOKENS)

    assert not [key for key in values if key.startswith("mcp_servers.")]
    assert values["features.apps"] is False


def test_isolated_configuration_refuses_an_mcp_name_it_cannot_disable_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text(
        '[mcp_servers."bad name"]\ncommand = "x"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    with pytest.raises(ValidationError, match="cannot be safely disabled"):
        _isolated_configuration(COMPACT_TOKENS)


def test_usage_delta_reports_only_monotonic_growth() -> None:
    assert _usage_delta(None, {"input": 1}) is None
    assert _usage_delta({"input": 10, "output": 4}, {"input": 3}) == {"input": 7, "output": 4}
    assert _usage_delta({"input": 2}, {"input": 3}) is None


def test_json_value_turns_nested_mappings_and_tuples_into_json_types() -> None:
    value = _json_value({"a": ({"b": (1, 2)}, "c"), "d": None})

    assert value == {"a": [{"b": [1, 2]}, "c"], "d": None}
    assert isinstance(value["a"], list)
    assert isinstance(value["a"][0]["b"], list)
