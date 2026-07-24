from __future__ import annotations

import asyncio
import plistlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from continuity_kernel.errors import ValidationError
from continuity_kernel.scheduler import (
    AppliedSchedulerReceipt,
    AsyncSchedulerCanary,
    CanaryObservation,
    CanaryState,
    LaunchdScheduler,
    SchedulerKind,
    SchedulerPlan,
    SchedulerSpec,
    WindowsTaskScheduler,
    make_applied_receipt,
    make_canary_observation,
    read_canary_observation,
    write_canary_observation,
)

NOW = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)


def _spec() -> SchedulerSpec:
    return SchedulerSpec(
        identifier="com.gsv.pulse",
        executable="/opt/gsv/bin/gsv",
        arguments=("pulse", "--mechanical"),
        working_directory="/opt/gsv",
    )


def test_launchd_plan_is_user_scoped_single_instance_and_side_effect_free(tmp_path: Path) -> None:
    plan = LaunchdScheduler(user_id=501).plan(_spec(), definition_dir=tmp_path)
    payload = plistlib.loads(plan.definition_bytes)

    assert plan.backend is SchedulerKind.LAUNCHD
    assert payload["Label"] == "com.gsv.pulse"
    assert payload["StartInterval"] == 600
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"] == ["/opt/gsv/bin/gsv", "pulse", "--mechanical"]
    assert plan.install_command[:3] == ("/bin/launchctl", "bootstrap", "gui/501")
    assert plan.receipt.user_session_only is True
    assert plan.receipt.single_instance is True
    assert plan.receipt.coalesced_sleep_catch_up is True
    assert plan.receipt.elevated is False
    assert plan.receipt.mechanical_cpu_budget_seconds == 5
    assert plan.receipt.cognitive_wall_clock_budget_seconds == 8 * 60
    assert plan.receipt.active_source_max_staleness_seconds == 6 * 60 * 60
    assert plan.receipt.whole_mind_max_staleness_seconds == 24 * 60 * 60
    assert not plan.definition_path.exists()


def test_windows_plan_uses_interactive_least_privilege_and_ignore_new(tmp_path: Path) -> None:
    windows_spec = SchedulerSpec(
        identifier="gsv-pulse",
        executable=r"C:\Program Files\GSV\gsv.exe",
        arguments=("pulse", "--mechanical"),
        working_directory=r"C:\Program Files\GSV",
    )
    plan = WindowsTaskScheduler().plan(windows_spec, definition_dir=tmp_path)
    xml = plan.definition_bytes.decode("utf-8")

    assert plan.backend is SchedulerKind.WINDOWS_TASK_SCHEDULER
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<Interval>PT10M</Interval>" in xml
    assert "Password" not in xml
    assert plan.install_command[:4] == (
        "schtasks.exe",
        "/Create",
        "/TN",
        r"\GSV\gsv-pulse",
    )
    assert "/F" not in plan.install_command
    assert not plan.definition_path.exists()


def test_scheduler_rejects_path_lookup_for_a_background_executable() -> None:
    with pytest.raises(ValidationError, match="executable must be an absolute path"):
        SchedulerSpec(identifier="gsv-pulse", executable="gsv")


def test_scheduler_cognitive_budget_cannot_exceed_eight_minutes() -> None:
    with pytest.raises(ValidationError, match="eight minutes"):
        SchedulerSpec(
            identifier="gsv-pulse",
            executable="/opt/gsv/bin/gsv",
            cognitive_wall_clock_budget_seconds=9 * 60,
        )


class _SuccessfulOperator:
    def __init__(self) -> None:
        self.token: str | None = None
        self.uninstalled = False

    async def install(self, plan: SchedulerPlan) -> AppliedSchedulerReceipt:
        return make_applied_receipt(plan, installed_at=NOW)

    async def trigger(self, plan: SchedulerPlan, *, canary_token: str) -> None:
        self.token = canary_token

    async def observe(
        self,
        plan: SchedulerPlan,
        *,
        canary_token: str,
    ) -> CanaryObservation | None:
        assert self.token == canary_token
        return make_canary_observation(plan, token=canary_token, observed_at=NOW)

    async def uninstall(self, plan: SchedulerPlan) -> None:
        self.uninstalled = True


def test_async_canary_reaches_verified_only_after_exact_observation(tmp_path: Path) -> None:
    plan = LaunchdScheduler(user_id=501).plan(_spec(), definition_dir=tmp_path)
    operator = _SuccessfulOperator()
    canary = AsyncSchedulerCanary(now=lambda: NOW)

    receipt = asyncio.run(canary.run(plan, operator))

    assert receipt.success is True
    assert receipt.final_state is CanaryState.VERIFIED
    assert tuple(item.state for item in receipt.transitions) == (
        CanaryState.PLANNED,
        CanaryState.INSTALLING,
        CanaryState.INSTALLED,
        CanaryState.TRIGGERING,
        CanaryState.AWAITING_OBSERVATION,
        CanaryState.VERIFIED,
    )
    assert receipt.observation is not None
    assert operator.uninstalled is False


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


class _SilentOperator(_SuccessfulOperator):
    async def observe(
        self,
        plan: SchedulerPlan,
        *,
        canary_token: str,
    ) -> CanaryObservation | None:
        return None


class _FailedInstallOperator(_SuccessfulOperator):
    async def install(self, plan: SchedulerPlan) -> AppliedSchedulerReceipt:
        raise OSError("install outcome is uncertain")


class _HungInstallOperator(_SuccessfulOperator):
    async def install(self, plan: SchedulerPlan) -> AppliedSchedulerReceipt:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _HungTriggerOperator(_SuccessfulOperator):
    async def trigger(self, plan: SchedulerPlan, *, canary_token: str) -> None:
        await asyncio.Event().wait()


def test_async_canary_timeout_rolls_back_unverified_scheduler(tmp_path: Path) -> None:
    plan = WindowsTaskScheduler().plan(_spec(), definition_dir=tmp_path)
    operator = _SilentOperator()
    clock = _FakeClock()
    canary = AsyncSchedulerCanary(
        timeout_seconds=2,
        poll_interval_seconds=0.5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=lambda: NOW,
    )

    receipt = asyncio.run(canary.run(plan, operator))

    assert receipt.success is False
    assert receipt.final_state is CanaryState.ROLLED_BACK
    assert receipt.error is not None and receipt.error.startswith("TimeoutError:")
    assert operator.uninstalled is True
    assert receipt.transitions[-2].state is CanaryState.ROLLING_BACK
    assert receipt.transitions[-1].state is CanaryState.ROLLED_BACK


def test_async_canary_never_removes_a_job_after_an_uncertain_install(tmp_path: Path) -> None:
    plan = LaunchdScheduler(user_id=501).plan(_spec(), definition_dir=tmp_path)
    operator = _FailedInstallOperator()

    receipt = asyncio.run(AsyncSchedulerCanary(now=lambda: NOW).run(plan, operator))

    assert receipt.success is False
    assert receipt.final_state is CanaryState.FAILED
    assert operator.uninstalled is False


def test_async_canary_deadline_bounds_install_and_owned_trigger_cleanup(tmp_path: Path) -> None:
    plan = LaunchdScheduler(user_id=501).plan(_spec(), definition_dir=tmp_path)

    install_operator = _HungInstallOperator()
    install_receipt = asyncio.run(
        AsyncSchedulerCanary(
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            now=lambda: NOW,
        ).run(plan, install_operator)
    )
    assert install_receipt.final_state is CanaryState.FAILED
    assert install_operator.uninstalled is False

    trigger_operator = _HungTriggerOperator()
    trigger_receipt = asyncio.run(
        AsyncSchedulerCanary(
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            now=lambda: NOW,
        ).run(plan, trigger_operator)
    )
    assert trigger_receipt.final_state is CanaryState.ROLLED_BACK
    assert trigger_operator.uninstalled is True


class _CancelledAfterInstallOperator(_SuccessfulOperator):
    def __init__(self) -> None:
        super().__init__()
        self.trigger_started = asyncio.Event()

    async def trigger(self, plan: SchedulerPlan, *, canary_token: str) -> None:
        self.trigger_started.set()
        await asyncio.Event().wait()


def test_async_canary_cancellation_removes_only_its_owned_install(tmp_path: Path) -> None:
    async def exercise() -> _CancelledAfterInstallOperator:
        plan = LaunchdScheduler(user_id=501).plan(_spec(), definition_dir=tmp_path)
        operator = _CancelledAfterInstallOperator()
        task = asyncio.create_task(AsyncSchedulerCanary(now=lambda: NOW).run(plan, operator))
        await operator.trigger_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return operator

    operator = asyncio.run(exercise())

    assert operator.uninstalled is True


def test_canary_observation_round_trips_through_atomic_local_receipt(tmp_path: Path) -> None:
    plan = LaunchdScheduler(user_id=501).plan(_spec(), definition_dir=tmp_path)
    observation = make_canary_observation(plan, token="bounded-canary", observed_at=NOW)
    path = tmp_path / "canary.json"

    write_canary_observation(path, observation)

    assert read_canary_observation(path) == observation
