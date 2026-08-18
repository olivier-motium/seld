from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

import pytest

import continuity_kernel.atomic as atomic_module
import continuity_kernel.scheduler as scheduler_module
from continuity_kernel.atomic import atomic_write, durable_unlink
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    PersistenceError,
    SetupError,
    ValidationError,
)
from continuity_kernel.scheduler import (
    ABSENT_SCHEDULER_REVISION,
    GSV_DATA_DIR_ENV,
    SCHEDULER_FINGERPRINT_ENV,
    SCHEDULER_LABEL,
    MacOSScheduler,
    RunnerResult,
)
from continuity_kernel.sense_sweep import PULSE_CANARY_ENV, heartbeat_status
from continuity_kernel.vault import Vault

WINDOWS_REQUIRES_DARWIN_SCHEDULER = pytest.mark.skipif(
    os.name == "nt",
    reason="the macOS launchd scheduler and its pinned storage are unavailable on Windows",
)


class FakeLaunchd:
    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.jobs: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.children: list[subprocess.Popen[bytes]] = []
        self.canary: Literal["async", "main-only", "missing"] = "async"
        self.canary_bootout: Literal["remove", "fail", "retain"] = "remove"
        self.fail_canary_print = False
        self.fail_main_print = False
        self.main_bootout: Literal["remove", "fail", "retain"] = "remove"
        self.bootstrap_failures: list[bool] = []
        self.crash_after_action: Literal["bootout", "bootstrap"] | None = None
        self.replace_after_bootout: tuple[Path, bytes] | None = None

    def __call__(self, command: Sequence[str], *, timeout_seconds: float) -> RunnerResult:
        del timeout_seconds
        values = tuple(str(value) for value in command)
        self.calls.append(values)
        action = values[1]
        if action == "print":
            label = values[-1].rsplit("/", 1)[-1]
            if ".canary." in label and self.fail_canary_print:
                return RunnerResult(124)
            if label == SCHEDULER_LABEL and self.fail_main_print:
                return RunnerResult(124)
            if label not in self.loaded:
                return RunnerResult(113)
            payload = self.jobs.get(label)
            return RunnerResult(0, _launchctl_output(payload) if payload else "foreign job\n")
        if action == "bootout":
            label = values[-1].rsplit("/", 1)[-1]
            if ".canary." in label and self.canary_bootout != "remove":
                return RunnerResult(1 if self.canary_bootout == "fail" else 0)
            if label == SCHEDULER_LABEL and self.main_bootout != "remove":
                return RunnerResult(1 if self.main_bootout == "fail" else 0)
            self.loaded.discard(label)
            self.jobs.pop(label, None)
            if self.crash_after_action == "bootout" and label == SCHEDULER_LABEL:
                self.crash_after_action = None
                raise SystemExit("crash after bootout")
            if self.replace_after_bootout is not None and label == SCHEDULER_LABEL:
                path, content = self.replace_after_bootout
                path.write_bytes(content)
                self.replace_after_bootout = None
            return RunnerResult(0)
        if action != "bootstrap":
            return RunnerResult(1)
        if self.bootstrap_failures and self.bootstrap_failures.pop(0):
            return RunnerResult(1)
        plist_path = Path(values[-1])
        payload = plistlib.loads(plist_path.read_bytes())
        label = payload["Label"]
        self.loaded.add(label)
        self.jobs[label] = payload
        if self.crash_after_action == "bootstrap" and label == SCHEDULER_LABEL:
            self.crash_after_action = None
            raise SystemExit("crash after bootstrap")
        if ".canary." not in label:
            return RunnerResult(0)
        arguments = tuple(payload["ProgramArguments"])
        if self.canary in {"async", "main-only"}:
            environment = {
                "HOME": str(Path.home()),
                "PATH": "/usr/bin:/bin",
            }
            environment.update(payload.get("EnvironmentVariables", {}))
            if self.canary == "main-only":
                environment.pop(PULSE_CANARY_ENV, None)
            self.children.append(
                subprocess.Popen(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                )
            )
        return RunnerResult(0)


def _launchctl_output(payload: dict[str, object]) -> str:
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    assert isinstance(environment, dict) and isinstance(arguments, list)
    fingerprint = environment[SCHEDULER_FINGERPRINT_ENV]
    data_root = environment[GSV_DATA_DIR_ENV]
    interval = payload.get("StartInterval")
    run_at_load = payload.get("RunAtLoad")
    cadence = f"run interval = {interval} seconds\n" if isinstance(interval, int) else ""
    properties = "properties = runatload\n" if run_at_load is True else ""
    return (
        "environment = {\n"
        f"    {GSV_DATA_DIR_ENV} => {data_root}\n"
        f"    {SCHEDULER_FINGERPRINT_ENV} => {fingerprint}\n"
        "}\n"
        "arguments = {\n"
        + "".join(f"    {argument}\n" for argument in arguments)
        + "}\n"
        + cadence
        + properties
    )


def _scheduler(tmp_path: Path) -> MacOSScheduler:
    vault = Vault(tmp_path / "vault")
    if not vault.root.exists():
        vault.initialize(name="Scheduler test")
    return MacOSScheduler(
        (
            sys.executable,
            "-m",
            "continuity_kernel",
            "--vault",
            str(vault.root),
            "--json",
            "pulse",
            "sweep",
        ),
        vault_root=vault.root,
        interval_seconds=300,
        data_root=tmp_path / "data",
        launch_agents_dir=tmp_path / "LaunchAgents",
        runtime_root=tmp_path / "runtime",
        uid=501,
        platform="darwin",
    )


def _install(scheduler: MacOSScheduler, runner: FakeLaunchd) -> str:
    installed = scheduler.install(
        expected_revision=ABSENT_SCHEDULER_REVISION,
        runner=runner,
    )
    assert installed.changed and installed.loaded
    return installed.receipt_revision


def _with_interval(scheduler: MacOSScheduler, interval_seconds: int) -> MacOSScheduler:
    return MacOSScheduler(
        scheduler.command,
        vault_root=scheduler.vault_root,
        interval_seconds=interval_seconds,
        data_root=scheduler.data_root,
        launch_agents_dir=scheduler.launch_agents_dir,
        runtime_root=scheduler.runtime_root,
        uid=scheduler.uid,
        platform="darwin",
    )


def test_scheduler_plan_is_exact_and_side_effect_free(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    plan = scheduler.plan()

    assert plan.label == SCHEDULER_LABEL
    assert plan.receipt_revision == ABSENT_SCHEDULER_REVISION
    assert plan.command[-5:] == (
        "--vault",
        str(scheduler.vault_root),
        "--json",
        "pulse",
        "sweep",
    )
    assert not scheduler.plist_path.exists()
    assert not scheduler.runtime_root.exists()
    payload = plistlib.loads(scheduler._plist_bytes())
    assert payload["Label"] == SCHEDULER_LABEL
    assert payload["ProgramArguments"] == list(plan.command)
    assert payload["EnvironmentVariables"] == {
        GSV_DATA_DIR_ENV: str(scheduler.data_root),
        SCHEDULER_FINGERPRINT_ENV: scheduler._job_fingerprint(),
    }
    assert payload["StartInterval"] == 300
    assert payload["RunAtLoad"] is True
    assert payload["StandardErrorPath"] == "/dev/null"


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_install_is_cas_protected_and_idempotent_with_injected_launchd(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)

    status = scheduler.status(runner=runner)
    again = scheduler.install(expected_revision=revision, runner=runner)

    assert status.current and status.installed and status.loaded
    assert not status.foreign and not status.stale
    assert not again.changed
    assert again.receipt_revision == revision
    with pytest.raises(ConflictError, match="receipt changed"):
        scheduler.install(expected_revision=ABSENT_SCHEDULER_REVISION, runner=runner)
    assert not any(call[1] == "bootout" for call in runner.calls)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_same_label_foreign_command_is_not_mistaken_for_the_owned_job(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    owned = runner.jobs[SCHEDULER_LABEL]
    environment = owned["EnvironmentVariables"]
    assert isinstance(environment, dict)
    runner.jobs[SCHEDULER_LABEL] = {
        "EnvironmentVariables": dict(environment),
        "Label": SCHEDULER_LABEL,
        "ProgramArguments": ["/usr/bin/false"],
    }

    status = scheduler.status(runner=runner)

    assert status.loaded and status.installed
    assert not status.current
    assert status.foreign and status.stale
    with pytest.raises(ConflictError, match="provenance"):
        scheduler.install(expected_revision=revision, runner=runner)
    with pytest.raises(ConflictError, match="provenance"):
        scheduler.uninstall(expected_revision=revision, runner=runner)
    assert runner.jobs[SCHEDULER_LABEL]["ProgramArguments"] == ["/usr/bin/false"]


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize("cadence", [1, None])
def test_copied_fingerprint_and_command_with_wrong_or_missing_cadence_is_foreign(
    tmp_path: Path,
    cadence: int | None,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    foreign = dict(runner.jobs[SCHEDULER_LABEL])
    if cadence is None:
        foreign.pop("StartInterval")
    else:
        foreign["StartInterval"] = cadence
    runner.jobs[SCHEDULER_LABEL] = foreign

    status = scheduler.status(runner=runner)

    assert status.loaded and status.foreign and status.stale
    assert not status.current
    with pytest.raises(ConflictError, match="provenance"):
        scheduler.uninstall(expected_revision=revision, runner=runner)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_copied_fingerprint_command_and_cadence_without_run_at_load_is_foreign(
    tmp_path: Path,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    foreign = dict(runner.jobs[SCHEDULER_LABEL])
    foreign.pop("RunAtLoad")
    runner.jobs[SCHEDULER_LABEL] = foreign

    status = scheduler.status(runner=runner)

    assert status.loaded and status.foreign and status.stale
    assert not status.current
    with pytest.raises(ConflictError, match="provenance"):
        scheduler.install(expected_revision=revision, runner=runner)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_copied_fingerprint_with_another_runtime_data_root_is_foreign(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    foreign = dict(runner.jobs[SCHEDULER_LABEL])
    environment = dict(cast(dict[str, object], foreign["EnvironmentVariables"]))
    environment[GSV_DATA_DIR_ENV] = str(tmp_path / "split-runtime")
    foreign["EnvironmentVariables"] = environment
    runner.jobs[SCHEDULER_LABEL] = foreign

    status = scheduler.status(runner=runner)

    assert status.loaded and status.foreign and status.stale
    assert not status.current
    with pytest.raises(ConflictError, match="provenance"):
        scheduler.uninstall(expected_revision=revision, runner=runner)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_failed_upgrade_reports_when_previous_job_cannot_be_restored(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    previous_plist = scheduler.plist_path.read_bytes()
    previous_receipt = scheduler.receipt_path.read_bytes()
    upgraded = MacOSScheduler(
        scheduler.command,
        vault_root=scheduler.vault_root,
        interval_seconds=600,
        launch_agents_dir=scheduler.launch_agents_dir,
        runtime_root=scheduler.runtime_root,
        uid=scheduler.uid,
        platform="darwin",
    )
    runner.bootstrap_failures = [True, True]

    with pytest.raises(SetupError, match="rollback failed; repair is required"):
        upgraded.install(expected_revision=revision, runner=runner)

    assert scheduler.plist_path.read_bytes() == previous_plist
    assert scheduler.receipt_path.read_bytes() == previous_receipt
    assert SCHEDULER_LABEL not in runner.loaded


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize(
    "boundary",
    ["operation", "plist", "receipt", "bootout", "bootstrap"],
)
def test_interrupted_upgrade_converges_from_a_fresh_scheduler_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    prior_revision = _install(scheduler, runner)
    target = _with_interval(scheduler, 600)
    actual_cas_replace = scheduler_module._cas_replace_file
    crashed = False

    def crash_after_file(
        path: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        maximum: int,
        label: str,
        mode: int = 0o600,
    ) -> None:
        nonlocal crashed
        actual_cas_replace(
            path,
            expected=expected,
            replacement=replacement,
            maximum=maximum,
            label=label,
            mode=mode,
        )
        candidate = Path(path)
        crash_target = {
            "operation": target.operation_path,
            "plist": target.plist_path,
            "receipt": target.receipt_path,
        }.get(boundary)
        if (
            not crashed
            and boundary in {"operation", "plist", "receipt"}
            and candidate == crash_target
            and (boundary == "operation" or target.operation_path.exists())
        ):
            crashed = True
            raise SystemExit(f"crash after {boundary}")

    if boundary in {"operation", "plist", "receipt"}:
        monkeypatch.setattr(scheduler_module, "_cas_replace_file", crash_after_file)
    else:
        runner.crash_after_action = boundary  # type: ignore[assignment]

    with pytest.raises(SystemExit, match=boundary):
        target.install(expected_revision=prior_revision, runner=runner)
    assert target.operation_path.exists()

    monkeypatch.setattr(scheduler_module, "_cas_replace_file", actual_cas_replace)
    fresh = _with_interval(scheduler, 600)
    interrupted = fresh.status(runner=runner)
    installed = fresh.install(expected_revision=prior_revision, runner=runner)
    final = fresh.status(runner=runner)

    assert interrupted.stale and not interrupted.current and not interrupted.foreign
    assert installed.changed and installed.loaded
    assert final.current and final.installed and final.loaded
    assert not final.foreign and not final.stale
    assert runner.jobs[SCHEDULER_LABEL]["StartInterval"] == 600
    assert not fresh.operation_path.exists()


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_receipt_write_error_rolls_back_to_the_exact_previous_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    prior_revision = _install(scheduler, runner)
    target = _with_interval(scheduler, 600)
    actual_cas_replace = scheduler_module._cas_replace_file
    failed = False

    def fail_after_receipt(
        path: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        maximum: int,
        label: str,
        mode: int = 0o600,
    ) -> None:
        nonlocal failed
        actual_cas_replace(
            path,
            expected=expected,
            replacement=replacement,
            maximum=maximum,
            label=label,
            mode=mode,
        )
        if not failed and Path(path) == target.receipt_path and target.operation_path.exists():
            failed = True
            raise OSError("receipt fsync failed")

    monkeypatch.setattr(scheduler_module, "_cas_replace_file", fail_after_receipt)
    with pytest.raises(OSError, match="receipt fsync failed"):
        target.install(expected_revision=prior_revision, runner=runner)
    monkeypatch.setattr(scheduler_module, "_cas_replace_file", actual_cas_replace)

    status = scheduler.status(runner=runner)
    assert status.current and status.loaded and status.receipt_revision == prior_revision
    assert runner.jobs[SCHEDULER_LABEL]["StartInterval"] == 300
    assert not scheduler.operation_path.exists()


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize("occupied", ["plist", "receipt"])
def test_recovery_preserves_foreign_file_created_after_prepublication_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occupied: str,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    actual_cas_replace = scheduler_module._cas_replace_file
    crashed = False

    def crash_after_operation(
        path: Path,
        *,
        expected: bytes | None,
        replacement: bytes,
        maximum: int,
        label: str,
        mode: int = 0o600,
    ) -> None:
        nonlocal crashed
        actual_cas_replace(
            path,
            expected=expected,
            replacement=replacement,
            maximum=maximum,
            label=label,
            mode=mode,
        )
        if not crashed and Path(path) == scheduler.operation_path:
            crashed = True
            raise SystemExit("crash after operation")

    monkeypatch.setattr(scheduler_module, "_cas_replace_file", crash_after_operation)
    with pytest.raises(SystemExit, match="operation"):
        scheduler.install(expected_revision=ABSENT_SCHEDULER_REVISION, runner=runner)
    monkeypatch.setattr(scheduler_module, "_cas_replace_file", actual_cas_replace)
    foreign_path = scheduler.plist_path if occupied == "plist" else scheduler.receipt_path
    foreign_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_path.write_bytes(b"foreign scheduler authority")

    fresh = _scheduler(tmp_path)
    with pytest.raises(ConflictError, match=f"foreign scheduler {occupied}"):
        fresh.uninstall(expected_revision=ABSENT_SCHEDULER_REVISION, runner=runner)

    assert foreign_path.read_bytes() == b"foreign scheduler authority"
    assert fresh.operation_path.exists()
    assert SCHEDULER_LABEL not in runner.loaded
    assert not any(call[1] == "bootout" for call in runner.calls)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize("prior_owned", [False, True])
def test_install_cas_preserves_plist_replaced_after_operation_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_owned: bool,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    expected_revision = ABSENT_SCHEDULER_REVISION
    target = scheduler
    if prior_owned:
        expected_revision = _install(scheduler, runner)
        target = _with_interval(scheduler, 600)
    actual_move = atomic_module._move_no_replace_at
    foreign = b"foreign scheduler plist after operation publication"
    injected = False

    def replace_at_final_publication(
        source_parent: int,
        source_name: str,
        target_parent: int,
        target_name: str,
    ) -> None:
        nonlocal injected
        if target_name == target.plist_path.name and ".seld-stage-" in source_name and not injected:
            target.plist_path.write_bytes(foreign)
            injected = True
        actual_move(source_parent, source_name, target_parent, target_name)

    monkeypatch.setattr(atomic_module, "_move_no_replace_at", replace_at_final_publication)
    with pytest.raises(ConflictError):
        target.install(expected_revision=expected_revision, runner=runner)

    assert target.plist_path.read_bytes() == foreign
    assert target.operation_path.exists()
    quarantines = list(
        target.plist_path.parent.glob(f".{target.plist_path.name}.seld-quarantine-*")
    )
    assert bool(quarantines) is prior_owned
    if prior_owned:
        assert quarantines[0].read_bytes() != foreign


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_scheduler_dirfd_mutation_never_follows_replaced_launch_agents_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = ABSENT_SCHEDULER_REVISION
    if action == "uninstall":
        revision = _install(scheduler, runner)
    actual_move = atomic_module._move_no_replace_at
    detached = tmp_path / "detached-LaunchAgents"
    foreign = b"foreign plist in replacement LaunchAgents"
    swapped = False

    def swap_parent_at_leaf_mutation(
        source_parent: int,
        source_name: str,
        target_parent: int,
        target_name: str,
    ) -> None:
        nonlocal swapped
        is_install_publication = target_name == scheduler.plist_path.name and (
            ".seld-stage-" in source_name
        )
        is_uninstall_quarantine = source_name == scheduler.plist_path.name and (
            ".seld-quarantine-" in target_name
        )
        if not swapped and (is_install_publication or is_uninstall_quarantine):
            scheduler.launch_agents_dir.rename(detached)
            scheduler.launch_agents_dir.mkdir()
            scheduler.plist_path.write_bytes(foreign)
            swapped = True
        actual_move(source_parent, source_name, target_parent, target_name)

    monkeypatch.setattr(atomic_module, "_move_no_replace_at", swap_parent_at_leaf_mutation)
    if action == "install":
        with pytest.raises(SetupError, match="rollback failed; repair is required"):
            scheduler.install(expected_revision=revision, runner=runner)
    else:
        with pytest.raises(ValidationError, match="canonical path"):
            scheduler.uninstall(expected_revision=revision, runner=runner)

    assert scheduler.plist_path.read_bytes() == foreign
    assert swapped


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_uninstall_preserves_plist_replaced_after_owned_job_is_unloaded(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    foreign = b"foreign scheduler plist after bootout"
    runner.replace_after_bootout = (scheduler.plist_path, foreign)

    with pytest.raises(ConflictError, match="plist changed before removal"):
        scheduler.uninstall(expected_revision=revision, runner=runner)

    assert scheduler.plist_path.read_bytes() == foreign
    assert scheduler.receipt_path.exists()
    assert SCHEDULER_LABEL not in runner.loaded


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize("loaded", [False, True])
def test_install_preserves_foreign_plist_or_job(tmp_path: Path, loaded: bool) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    scheduler.plist_path.parent.mkdir(parents=True)
    foreign = b"foreign launch agent"
    if not loaded:
        scheduler.plist_path.write_bytes(foreign)
    else:
        runner.loaded.add(SCHEDULER_LABEL)

    with pytest.raises(ConflictError, match="not owned"):
        scheduler.install(
            expected_revision=ABSENT_SCHEDULER_REVISION,
            runner=runner,
        )

    if not loaded:
        assert scheduler.plist_path.read_bytes() == foreign
    assert not any(call[1] in {"bootstrap", "bootout"} for call in runner.calls)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_run_canary_proves_the_exact_vault_bound_sweep_through_launchd(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)

    canary = scheduler.run_canary(
        expected_revision=revision,
        runner=runner,
        timeout_seconds=10,
    )

    assert canary.proved
    assert canary.failure is None
    assert canary.command == scheduler.command
    assert canary.heartbeat_observed_at is not None
    assert canary.heartbeat_sequence == 1
    assert runner.children
    assert all(child.wait(timeout=5) == 0 for child in runner.children)
    assert list((scheduler.data_root / "sense-sweep").rglob("heartbeat.json"))
    assert not any((scheduler.runtime_root / "canary").iterdir())


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize("cleanup_outcome", ("fail", "retain"))
def test_canary_fails_closed_and_retains_recovery_plist_when_job_remains_loaded(
    tmp_path: Path,
    cleanup_outcome: Literal["fail", "retain"],
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    runner.canary_bootout = cleanup_outcome

    canary = scheduler.run_canary(
        expected_revision=revision,
        runner=runner,
        timeout_seconds=10,
    )

    assert not canary.proved
    assert canary.failure == "canary_cleanup_failed"
    canary_labels = {label for label in runner.loaded if ".canary." in label}
    assert len(canary_labels) == 1
    canary_plists = tuple((scheduler.runtime_root / "canary").glob("*.plist"))
    assert len(canary_plists) == 1
    payload = plistlib.loads(canary_plists[0].read_bytes())
    assert payload["Label"] in canary_labels
    assert all(child.wait(timeout=5) == 0 for child in runner.children)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_canary_fails_closed_when_cleanup_absence_probe_is_inconclusive(
    tmp_path: Path,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    runner.fail_canary_print = True

    canary = scheduler.run_canary(
        expected_revision=revision,
        runner=runner,
        timeout_seconds=10,
    )

    assert not canary.proved
    assert canary.failure == "canary_cleanup_failed"
    assert not any(".canary." in label for label in runner.loaded)
    canary_plists = tuple((scheduler.runtime_root / "canary").glob("*.plist"))
    assert len(canary_plists) == 1
    assert all(child.wait(timeout=5) == 0 for child in runner.children)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_canary_fails_closed_when_the_exact_sweep_does_not_heartbeat(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    runner.canary = "missing"

    canary = scheduler.run_canary(
        expected_revision=revision,
        runner=runner,
        timeout_seconds=0.1,
    )

    assert not canary.proved
    assert canary.failure == "canary_missing"
    assert canary.heartbeat_observed_at is None
    assert canary.heartbeat_sequence is None


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_canary_does_not_accept_an_uncorrelated_main_job_heartbeat(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    runner.canary = "main-only"

    canary = scheduler.run_canary(
        expected_revision=revision,
        runner=runner,
        timeout_seconds=1,
    )

    assert not canary.proved
    assert canary.failure == "canary_missing"
    # The main job records its heartbeat AFTER the recall refresh attempt,
    # which can outlive the canary timeout. Wait for the job before reading.
    assert all(child.wait(timeout=5) == 0 for child in runner.children)
    assert heartbeat_status(scheduler.vault_root) is not None


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_status_reports_missing_owned_plist_as_stale_and_foreign_when_loaded(
    tmp_path: Path,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    _install(scheduler, runner)
    durable_unlink(scheduler.plist_path)

    status = scheduler.status(runner=runner)

    assert not status.current
    assert not status.installed
    assert status.loaded
    assert status.foreign
    assert status.stale


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_uninstall_requires_exact_owned_plist_and_is_idempotent(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    owned = scheduler.plist_path.read_bytes()
    scheduler.plist_path.write_bytes(b"foreign replacement")

    with pytest.raises(ConflictError, match="changed outside Seld"):
        scheduler.uninstall(expected_revision=revision, runner=runner)
    assert scheduler.plist_path.read_bytes() == b"foreign replacement"
    assert SCHEDULER_LABEL in runner.loaded

    atomic_write(scheduler.plist_path, owned, mode=0o644)
    removed = scheduler.uninstall(expected_revision=revision, runner=runner)
    again = scheduler.uninstall(
        expected_revision=ABSENT_SCHEDULER_REVISION,
        runner=runner,
    )

    assert removed.changed and not removed.loaded
    assert not scheduler.plist_path.exists()
    assert not scheduler.receipt_path.exists()
    assert not again.changed


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_uninstall_preserves_owned_state_when_launchd_observation_is_inconclusive(
    tmp_path: Path,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    plist = scheduler.plist_path.read_bytes()
    receipt = scheduler.receipt_path.read_bytes()
    runner.fail_main_print = True

    with pytest.raises(SetupError, match="could not inspect"):
        scheduler.uninstall(expected_revision=revision, runner=runner)

    assert scheduler.plist_path.read_bytes() == plist
    assert scheduler.receipt_path.read_bytes() == receipt
    assert SCHEDULER_LABEL in runner.loaded
    assert not any(call[1] == "bootout" for call in runner.calls)


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_uninstall_preserves_owned_state_when_bootout_does_not_remove_job(
    tmp_path: Path,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    plist = scheduler.plist_path.read_bytes()
    receipt = scheduler.receipt_path.read_bytes()
    runner.main_bootout = "retain"

    with pytest.raises(SetupError, match="could not prove"):
        scheduler.uninstall(expected_revision=revision, runner=runner)

    assert scheduler.plist_path.read_bytes() == plist
    assert scheduler.receipt_path.read_bytes() == receipt
    assert SCHEDULER_LABEL in runner.loaded


def test_non_macos_scheduler_is_explicitly_unsupported(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="macOS only"):
        MacOSScheduler(
            (
                sys.executable,
                "--vault",
                str(tmp_path / "vault"),
                "--json",
                "pulse",
                "sweep",
            ),
            vault_root=tmp_path / "vault",
            launch_agents_dir=tmp_path / "agents",
            runtime_root=tmp_path / "runtime",
            platform="win32",
        )


def test_scheduler_requires_a_uid_when_the_host_has_no_posix_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "getuid", raising=False)

    with pytest.raises(SetupError, match="requires a macOS user ID"):
        MacOSScheduler(
            (
                sys.executable,
                "--vault",
                str(tmp_path / "vault"),
                "--json",
                "pulse",
                "sweep",
            ),
            vault_root=tmp_path / "vault",
            data_root=tmp_path / "data",
            launch_agents_dir=tmp_path / "agents",
            runtime_root=tmp_path / "runtime",
            platform="darwin",
        )


def test_scheduler_rejects_a_boolean_host_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: True, raising=False)

    with pytest.raises(ValidationError, match="scheduler user ID is invalid"):
        MacOSScheduler(
            (
                sys.executable,
                "--vault",
                str(tmp_path / "vault"),
                "--json",
                "pulse",
                "sweep",
            ),
            vault_root=tmp_path / "vault",
            data_root=tmp_path / "data",
            launch_agents_dir=tmp_path / "agents",
            runtime_root=tmp_path / "runtime",
            platform="darwin",
        )


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_canary_requires_the_exact_current_receipt_revision(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    _install(scheduler, runner)

    with pytest.raises(ConflictError, match="receipt changed"):
        scheduler.run_canary(
            expected_revision=ABSENT_SCHEDULER_REVISION,
            runner=runner,
        )

    assert not any(".canary." in part for call in runner.calls for part in call)


def test_launchctl_observation_capture_is_memory_bounded() -> None:
    result = scheduler_module._run(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"),
        timeout_seconds=2,
    )

    assert result.stdout == ""


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
@pytest.mark.parametrize(
    ("outcome", "error_type"),
    (
        (atomic_module.PublishOutcome.UNPUBLISHED, PersistenceError),
        (atomic_module.PublishOutcome.COMMITTED, MutationCommittedError),
        (atomic_module.PublishOutcome.UNKNOWN, DegradedIntegrityError),
    ),
)
def test_scheduler_exact_publish_outcomes_map_to_public_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: atomic_module.PublishOutcome,
    error_type: type[Exception],
) -> None:
    root = tmp_path / "scheduler-root"
    state = root / "state"
    state.mkdir(parents=True)
    target = state / "receipt.json"

    def fail_publish(*args: object, **kwargs: object) -> None:
        raise atomic_module.DurablePublishError("injected scheduler outcome", outcome=outcome)

    monkeypatch.setattr(
        atomic_module.PinnedPathRoot,
        "replace_regular_file_if_exact",
        fail_publish,
    )
    with pytest.raises(error_type):
        scheduler_module._cas_replace_file(
            target,
            expected=None,
            replacement=b"{}\n",
            maximum=1024,
            label="scheduler receipt",
        )


@WINDOWS_REQUIRES_DARWIN_SCHEDULER
def test_scheduler_uninstall_rejects_identical_inode_substitution_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler(tmp_path)
    runner = FakeLaunchd()
    revision = _install(scheduler, runner)
    original = scheduler.plist_path.read_bytes()
    original_identity = scheduler.plist_path.stat().st_ino
    parked = scheduler.plist_path.with_name("parked-owned.plist")
    actual_unlink = scheduler_module._unlink_if_exact
    swapped = False

    def substitute_then_unlink(
        path: Path,
        *,
        expected: bytes,
        maximum: int,
        label: str,
    ) -> None:
        nonlocal swapped
        if not swapped and Path(path) == scheduler.plist_path:
            swapped = True
            scheduler.plist_path.rename(parked)
            scheduler.plist_path.write_bytes(original)
        actual_unlink(
            path,
            expected=expected,
            maximum=maximum,
            label=label,
        )

    monkeypatch.setattr(scheduler_module, "_unlink_if_exact", substitute_then_unlink)
    with pytest.raises(ValidationError, match="identity"):
        scheduler.uninstall(expected_revision=revision, runner=runner)

    assert swapped
    assert parked.read_bytes() == original
    assert scheduler.plist_path.read_bytes() == original
    assert parked.stat().st_ino == original_identity
    assert scheduler.plist_path.stat().st_ino != original_identity
