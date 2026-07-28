"""Cross-platform scheduler definitions and an asynchronous install canary.

Platform planners are side-effect free: they render exact launchd or Windows
Task Scheduler definitions and the commands an owning host may execute.  The
async canary operates only through an injected operator, so tests and callers
can prove the lifecycle without accidentally mutating the host scheduler.
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import secrets
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, cast
from xml.etree import ElementTree

from continuity_kernel.atomic import atomic_write, exclusive_lock, read_regular_file, sha256_bytes
from continuity_kernel.errors import ValidationError
from continuity_kernel.pulse import (
    ACTIVE_SOURCE_MAX_STALENESS_SECONDS,
    COGNITIVE_WALL_CLOCK_BUDGET_SECONDS,
    MECHANICAL_CPU_BUDGET_SECONDS,
    WHOLE_MIND_MAX_STALENESS_SECONDS,
)

SCHEDULER_FORMAT_VERSION: Final = 1
CANARY_FORMAT_VERSION: Final = 1
CANARY_MAX_BYTES: Final = 16 * 1024
SAFE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SchedulerKind(StrEnum):
    LAUNCHD = "launchd"
    WINDOWS_TASK_SCHEDULER = "windows_task_scheduler"


class CanaryState(StrEnum):
    PLANNED = "planned"
    INSTALLING = "installing"
    INSTALLED = "installed"
    TRIGGERING = "triggering"
    AWAITING_OBSERVATION = "awaiting_observation"
    VERIFIED = "verified"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass(frozen=True)
class SchedulerSpec:
    identifier: str
    executable: str
    arguments: tuple[str, ...] = ()
    interval_seconds: int = 10 * 60
    mechanical_cpu_budget_seconds: int = MECHANICAL_CPU_BUDGET_SECONDS
    cognitive_wall_clock_budget_seconds: int = COGNITIVE_WALL_CLOCK_BUDGET_SECONDS
    active_source_max_staleness_seconds: int = ACTIVE_SOURCE_MAX_STALENESS_SECONDS
    whole_mind_max_staleness_seconds: int = WHOLE_MIND_MAX_STALENESS_SECONDS
    working_directory: str | None = None

    def __post_init__(self) -> None:
        if not SAFE_IDENTIFIER.fullmatch(self.identifier):
            raise ValidationError("scheduler identifier is invalid")
        _command_text(self.executable, "scheduler executable")
        if not _absolute_path(self.executable):
            raise ValidationError("scheduler executable must be an absolute path")
        for argument in self.arguments:
            _command_text(argument, "scheduler argument", allow_empty=True)
        if self.working_directory is not None:
            _command_text(self.working_directory, "scheduler working directory")
            if not _absolute_path(self.working_directory):
                raise ValidationError("scheduler working directory must be an absolute path")
        if self.interval_seconds < 60:
            raise ValidationError("scheduler interval must be at least 60 seconds")
        if self.mechanical_cpu_budget_seconds != MECHANICAL_CPU_BUDGET_SECONDS:
            raise ValidationError("scheduler mechanical CPU budget is fixed at 5 seconds")
        if (
            not 1
            <= self.cognitive_wall_clock_budget_seconds
            <= min(
                self.interval_seconds,
                COGNITIVE_WALL_CLOCK_BUDGET_SECONDS,
            )
        ):
            raise ValidationError(
                "scheduler cognitive wall-clock budget cannot exceed eight minutes"
            )
        if self.active_source_max_staleness_seconds != ACTIVE_SOURCE_MAX_STALENESS_SECONDS:
            raise ValidationError("scheduler active-source maximum staleness is fixed at 6 hours")
        if self.whole_mind_max_staleness_seconds != WHOLE_MIND_MAX_STALENESS_SECONDS:
            raise ValidationError("scheduler whole-Mind maximum staleness is fixed at 24 hours")


@dataclass(frozen=True)
class SchedulerDefinitionReceipt:
    format_version: int
    backend: SchedulerKind
    identifier: str
    definition_path: str
    definition_sha256: str
    interval_seconds: int
    mechanical_cpu_budget_seconds: int
    cognitive_wall_clock_budget_seconds: int
    active_source_max_staleness_seconds: int
    whole_mind_max_staleness_seconds: int
    user_session_only: bool
    single_instance: bool
    coalesced_sleep_catch_up: bool
    elevated: bool


@dataclass(frozen=True)
class SchedulerPlan:
    backend: SchedulerKind
    spec: SchedulerSpec
    definition_path: Path
    definition_bytes: bytes
    install_command: tuple[str, ...]
    trigger_command: tuple[str, ...]
    status_command: tuple[str, ...]
    uninstall_command: tuple[str, ...]
    receipt: SchedulerDefinitionReceipt


@dataclass(frozen=True)
class AppliedSchedulerReceipt:
    format_version: int
    backend: SchedulerKind
    identifier: str
    definition_sha256: str
    installed_at: str


@dataclass(frozen=True)
class CanaryObservation:
    format_version: int
    token: str
    scheduler_id: str
    definition_sha256: str
    observed_at: str


@dataclass(frozen=True)
class CanaryTransition:
    state: CanaryState
    observed_at: str
    detail: str


@dataclass(frozen=True)
class CanaryRunReceipt:
    format_version: int
    backend: SchedulerKind
    scheduler_id: str
    definition_sha256: str
    success: bool
    final_state: CanaryState
    transitions: tuple[CanaryTransition, ...]
    observation: CanaryObservation | None
    error: str | None
    rollback_error: str | None


class AsyncSchedulerOperator(Protocol):
    """The only boundary allowed to mutate or query an operating-system scheduler."""

    async def install(self, plan: SchedulerPlan) -> AppliedSchedulerReceipt:
        """Return only while the exact identifier and definition digest are still installed."""

        ...

    async def trigger(self, plan: SchedulerPlan, *, canary_token: str) -> None: ...

    async def observe(
        self,
        plan: SchedulerPlan,
        *,
        canary_token: str,
    ) -> CanaryObservation | None: ...

    async def uninstall(self, plan: SchedulerPlan) -> None: ...


class LaunchdScheduler:
    """Render a least-privilege per-user launchd definition without installing it."""

    def __init__(self, *, user_id: int | None = None):
        if user_id is None:
            getuid = getattr(os, "getuid", None)
            if getuid is None:
                raise ValidationError("launchd planning requires an explicit user ID")
            user_id = int(getuid())
        if user_id < 0:
            raise ValidationError("launchd user ID is invalid")
        self.user_id = user_id

    def plan(self, spec: SchedulerSpec, *, definition_dir: Path) -> SchedulerPlan:
        definition_path = definition_dir.expanduser().resolve() / f"{spec.identifier}.plist"
        payload: dict[str, object] = {
            "Label": spec.identifier,
            "ProgramArguments": [spec.executable, *spec.arguments],
            "RunAtLoad": True,
            "StartInterval": spec.interval_seconds,
            "ProcessType": "Background",
            "KeepAlive": False,
            "ThrottleInterval": 60,
        }
        if spec.working_directory is not None:
            payload["WorkingDirectory"] = spec.working_directory
        definition = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
        domain = f"gui/{self.user_id}"
        target = f"{domain}/{spec.identifier}"
        return _plan(
            backend=SchedulerKind.LAUNCHD,
            spec=spec,
            definition_path=definition_path,
            definition=definition,
            install=("/bin/launchctl", "bootstrap", domain, str(definition_path)),
            trigger=("/bin/launchctl", "kickstart", "-k", target),
            status=("/bin/launchctl", "print", target),
            uninstall=("/bin/launchctl", "bootout", target),
        )


class WindowsTaskScheduler:
    """Render a current-user Task Scheduler definition without installing it."""

    def plan(self, spec: SchedulerSpec, *, definition_dir: Path) -> SchedulerPlan:
        definition_path = definition_dir.expanduser().resolve() / f"{spec.identifier}.xml"
        definition = _windows_task_xml(spec)
        task_name = f"\\GSV\\{spec.identifier}"
        executable = "schtasks.exe"
        return _plan(
            backend=SchedulerKind.WINDOWS_TASK_SCHEDULER,
            spec=spec,
            definition_path=definition_path,
            definition=definition,
            # Never replace a task we cannot prove this exact canary owns.
            install=(executable, "/Create", "/TN", task_name, "/XML", str(definition_path)),
            trigger=(executable, "/Run", "/TN", task_name),
            status=(executable, "/Query", "/TN", task_name, "/FO", "LIST", "/V"),
            uninstall=(executable, "/Delete", "/TN", task_name, "/F"),
        )


class AsyncSchedulerCanary:
    """Install, trigger, observe, and verify one scheduler through an async operator."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if not 0 < timeout_seconds <= 300:
            raise ValidationError("scheduler canary timeout is invalid")
        if not 0 < poll_interval_seconds <= timeout_seconds:
            raise ValidationError("scheduler canary poll interval is invalid")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.monotonic = monotonic
        self.sleep = sleep
        self.now = now

    async def run(
        self,
        plan: SchedulerPlan,
        operator: AsyncSchedulerOperator,
    ) -> CanaryRunReceipt:
        """Run one exact canary and roll back any installed but unverified job."""

        transitions: list[CanaryTransition] = []
        observation: CanaryObservation | None = None
        installation_owned = False
        rollback_error: str | None = None
        token = secrets.token_urlsafe(24)
        deadline = self.monotonic() + self.timeout_seconds

        def transition(state: CanaryState, detail: str) -> None:
            transitions.append(
                CanaryTransition(
                    state=state,
                    observed_at=_format_time(_aware_utc(self.now())),
                    detail=detail,
                )
            )

        transition(CanaryState.PLANNED, "exact scheduler definition frozen")

        async def within_deadline(awaitable: Awaitable[object]) -> object:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                close = getattr(awaitable, "close", None)
                if close is not None:
                    close()
                raise TimeoutError("scheduler canary exceeded its wall-clock deadline")
            try:
                return await asyncio.wait_for(awaitable, timeout=remaining)
            except TimeoutError as exc:
                raise TimeoutError("scheduler canary exceeded its wall-clock deadline") from exc

        async def rollback_owned_install() -> str | None:
            if not installation_owned:
                return None
            transition(CanaryState.ROLLING_BACK, "owned unverified scheduler removal requested")
            try:
                await asyncio.shield(
                    asyncio.wait_for(
                        operator.uninstall(plan),
                        timeout=min(5.0, self.timeout_seconds),
                    )
                )
            except BaseException as exc:
                transition(CanaryState.FAILED, "scheduler rollback failed")
                return f"{type(exc).__name__}: {exc}"
            transition(CanaryState.ROLLED_BACK, "owned unverified scheduler removed")
            return None

        try:
            transition(CanaryState.INSTALLING, "install requested")
            applied = cast(AppliedSchedulerReceipt, await within_deadline(operator.install(plan)))
            # Both supported definitions refuse an existing identifier. A
            # successful operator return therefore proves this run owns the job.
            installation_owned = True
            _validate_applied_receipt(plan, applied)
            transition(CanaryState.INSTALLED, "definition receipt matched")
            transition(CanaryState.TRIGGERING, "one canary trigger requested")
            await within_deadline(operator.trigger(plan, canary_token=token))
            transition(CanaryState.AWAITING_OBSERVATION, "waiting for exact canary receipt")
            while True:
                observation = cast(
                    CanaryObservation | None,
                    await within_deadline(operator.observe(plan, canary_token=token)),
                )
                if observation is not None:
                    _validate_observation(plan, token, observation)
                    transition(CanaryState.VERIFIED, "exact canary receipt observed")
                    return _canary_receipt(
                        plan=plan,
                        success=True,
                        state=CanaryState.VERIFIED,
                        transitions=transitions,
                        observation=observation,
                        error=None,
                        rollback_error=None,
                    )
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise TimeoutError("scheduler canary did not produce an exact receipt")
                await within_deadline(self.sleep(min(self.poll_interval_seconds, remaining)))
        except asyncio.CancelledError:
            await rollback_owned_install()
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            final_state = CanaryState.FAILED
            if installation_owned:
                rollback_error = await rollback_owned_install()
                if rollback_error is None:
                    final_state = CanaryState.ROLLED_BACK
            else:
                transition(CanaryState.FAILED, "scheduler was not installed")
            return _canary_receipt(
                plan=plan,
                success=False,
                state=final_state,
                transitions=transitions,
                observation=observation,
                error=error,
                rollback_error=rollback_error,
            )


def make_applied_receipt(
    plan: SchedulerPlan,
    *,
    installed_at: datetime | None = None,
) -> AppliedSchedulerReceipt:
    """Create the exact receipt an operator returns after a verified install command."""

    return AppliedSchedulerReceipt(
        format_version=SCHEDULER_FORMAT_VERSION,
        backend=plan.backend,
        identifier=plan.spec.identifier,
        definition_sha256=plan.receipt.definition_sha256,
        installed_at=_format_time(_aware_utc(installed_at or datetime.now(UTC))),
    )


def make_canary_observation(
    plan: SchedulerPlan,
    *,
    token: str,
    observed_at: datetime | None = None,
) -> CanaryObservation:
    """Create a content-free receipt from the process actually started by the scheduler."""

    return CanaryObservation(
        format_version=CANARY_FORMAT_VERSION,
        token=_bounded_text(token, "canary token", 256),
        scheduler_id=plan.spec.identifier,
        definition_sha256=plan.receipt.definition_sha256,
        observed_at=_format_time(_aware_utc(observed_at or datetime.now(UTC))),
    )


def write_canary_observation(path: Path, observation: CanaryObservation) -> None:
    """Atomically publish one local canary receipt for an asynchronous observer."""

    if observation.format_version != CANARY_FORMAT_VERSION:
        raise ValidationError("unsupported scheduler canary receipt version")
    payload = {
        "definition_sha256": _digest(observation.definition_sha256, "definition digest"),
        "format_version": CANARY_FORMAT_VERSION,
        "observed_at": _timestamp(observation.observed_at, "canary observation"),
        "scheduler_id": _bounded_text(observation.scheduler_id, "scheduler ID", 128),
        "token": _bounded_text(observation.token, "canary token", 256),
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > CANARY_MAX_BYTES:  # pragma: no cover - bounded fields make this defensive
        raise ValidationError("scheduler canary receipt exceeds its size bound")
    with exclusive_lock(path.with_suffix(f"{path.suffix}.lock")):
        atomic_write(path, encoded)


def read_canary_observation(path: Path) -> CanaryObservation | None:
    """Read one bounded canary receipt without following unsafe file types."""

    with exclusive_lock(path.with_suffix(f"{path.suffix}.lock")):
        if not os.path.lexists(path):
            return None
        encoded = read_regular_file(
            path,
            label="scheduler canary receipt",
            max_bytes=CANARY_MAX_BYTES,
        )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid scheduler canary receipt") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != CANARY_FORMAT_VERSION:
        raise ValidationError("unsupported scheduler canary receipt version")
    return CanaryObservation(
        format_version=CANARY_FORMAT_VERSION,
        token=_object_text(payload.get("token"), "canary token", 256),
        scheduler_id=_object_text(payload.get("scheduler_id"), "scheduler ID", 128),
        definition_sha256=_digest(payload.get("definition_sha256"), "definition digest"),
        observed_at=_timestamp(payload.get("observed_at"), "canary observation"),
    )


def _plan(
    *,
    backend: SchedulerKind,
    spec: SchedulerSpec,
    definition_path: Path,
    definition: bytes,
    install: tuple[str, ...],
    trigger: tuple[str, ...],
    status: tuple[str, ...],
    uninstall: tuple[str, ...],
) -> SchedulerPlan:
    digest = sha256_bytes(definition)
    receipt = SchedulerDefinitionReceipt(
        format_version=SCHEDULER_FORMAT_VERSION,
        backend=backend,
        identifier=spec.identifier,
        definition_path=str(definition_path),
        definition_sha256=digest,
        interval_seconds=spec.interval_seconds,
        mechanical_cpu_budget_seconds=spec.mechanical_cpu_budget_seconds,
        cognitive_wall_clock_budget_seconds=spec.cognitive_wall_clock_budget_seconds,
        active_source_max_staleness_seconds=spec.active_source_max_staleness_seconds,
        whole_mind_max_staleness_seconds=spec.whole_mind_max_staleness_seconds,
        user_session_only=True,
        single_instance=True,
        coalesced_sleep_catch_up=True,
        elevated=False,
    )
    return SchedulerPlan(
        backend=backend,
        spec=spec,
        definition_path=definition_path,
        definition_bytes=definition,
        install_command=install,
        trigger_command=trigger,
        status_command=status,
        uninstall_command=uninstall,
        receipt=receipt,
    )


def _windows_task_xml(spec: SchedulerSpec) -> bytes:
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ElementTree.register_namespace("", namespace)

    def child(
        parent: ElementTree.Element,
        name: str,
        text: str | None = None,
    ) -> ElementTree.Element:
        element = ElementTree.SubElement(parent, f"{{{namespace}}}{name}")
        element.text = text
        return element

    task = ElementTree.Element(f"{{{namespace}}}Task", {"version": "1.4"})
    registration = child(task, "RegistrationInfo")
    child(registration, "Description", "GSV bounded local Pulse scheduler")
    triggers = child(task, "Triggers")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true")
    repetition = child(logon, "Repetition")
    child(repetition, "Interval", _iso_duration(spec.interval_seconds))
    child(repetition, "StopAtDurationEnd", "false")
    principals = child(task, "Principals")
    principal = child(principals, "Principal")
    principal.set("id", "CurrentUser")
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    child(settings, "DisallowStartIfOnBatteries", "false")
    child(settings, "StopIfGoingOnBatteries", "false")
    child(settings, "StartWhenAvailable", "true")
    child(settings, "RunOnlyIfNetworkAvailable", "false")
    child(settings, "Enabled", "true")
    child(settings, "Hidden", "false")
    child(settings, "ExecutionTimeLimit", _iso_duration(spec.interval_seconds))
    actions = child(task, "Actions")
    actions.set("Context", "CurrentUser")
    execute = child(actions, "Exec")
    child(execute, "Command", spec.executable)
    if spec.arguments:
        child(execute, "Arguments", subprocess.list2cmdline(list(spec.arguments)))
    if spec.working_directory is not None:
        child(execute, "WorkingDirectory", spec.working_directory)
    return cast(bytes, ElementTree.tostring(task, encoding="utf-8", xml_declaration=True))


def _iso_duration(seconds: int) -> str:
    minutes, remaining = divmod(seconds, 60)
    if remaining:
        return f"PT{minutes}M{remaining}S"
    return f"PT{minutes}M"


def _validate_applied_receipt(plan: SchedulerPlan, receipt: AppliedSchedulerReceipt) -> None:
    if (
        receipt.format_version != SCHEDULER_FORMAT_VERSION
        or receipt.backend != plan.backend
        or receipt.identifier != plan.spec.identifier
        or receipt.definition_sha256 != plan.receipt.definition_sha256
    ):
        raise ValidationError("installed scheduler receipt does not match the frozen definition")
    _timestamp(receipt.installed_at, "scheduler install time")


def _validate_observation(
    plan: SchedulerPlan,
    token: str,
    observation: CanaryObservation,
) -> None:
    if (
        observation.format_version != CANARY_FORMAT_VERSION
        or observation.token != token
        or observation.scheduler_id != plan.spec.identifier
        or observation.definition_sha256 != plan.receipt.definition_sha256
    ):
        raise ValidationError("scheduler canary observation does not match the exact request")
    _timestamp(observation.observed_at, "canary observation")


def _canary_receipt(
    *,
    plan: SchedulerPlan,
    success: bool,
    state: CanaryState,
    transitions: list[CanaryTransition],
    observation: CanaryObservation | None,
    error: str | None,
    rollback_error: str | None,
) -> CanaryRunReceipt:
    return CanaryRunReceipt(
        format_version=CANARY_FORMAT_VERSION,
        backend=plan.backend,
        scheduler_id=plan.spec.identifier,
        definition_sha256=plan.receipt.definition_sha256,
        success=success,
        final_state=state,
        transitions=tuple(transitions),
        observation=observation,
        error=error,
        rollback_error=rollback_error,
    )


def _command_text(value: str, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    if "\x00" in value or "\n" in value or "\r" in value or (not value and not allow_empty):
        raise ValidationError(f"{label} is invalid")
    if len(value.encode("utf-8")) > 4096:
        raise ValidationError(f"{label} exceeds its size bound")
    return value


def _absolute_path(value: str) -> bool:
    return (
        value.startswith("/")
        or value.startswith("\\\\")
        or re.match(r"^[A-Za-z]:[\\/]", value) is not None
    )


def _bounded_text(value: str, label: str, maximum: int) -> str:
    normalized = _command_text(value, label).strip()
    if len(normalized.encode("utf-8")) > maximum:
        raise ValidationError(f"{label} exceeds its size bound")
    return normalized


def _object_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    return _bounded_text(value, label, maximum)


def _digest(value: object, label: str) -> str:
    result = _object_text(value, label, 128)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValidationError(f"{label} is invalid")
    return result


def _timestamp(value: object, label: str) -> str:
    result = _object_text(value, label, 64)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} must include a timezone")
    return _format_time(parsed.astimezone(UTC))


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValidationError("scheduler time must include a timezone")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
