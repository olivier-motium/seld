from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import pytest

import continuity_kernel.bridge_launcher as launcher
from continuity_kernel import cli
from continuity_kernel.bridge_launcher import (
    APP_BUNDLE_NAME,
    BUNDLE_IDENTIFIER,
    NATIVE_EXECUTABLE,
    RECEIPT_NAME,
    SERVICE_EXECUTABLE,
    install_native_bridge,
    native_bridge_status,
    open_native_bridge,
    uninstall_native_bridge,
)
from continuity_kernel.errors import ConflictError, SetupError
from continuity_kernel.vault import Vault


@dataclass
class Completed:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class HardStop(BaseException):
    pass


@dataclass
class SyntheticMacRunner:
    calls: list[tuple[str, ...]] = field(default_factory=list)
    fail_architecture: str | None = None

    def __call__(self, command: Any, **kwargs: object) -> Completed:
        call = tuple(str(item) for item in command)
        self.calls.append(call)
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        if call[:2] == ("/usr/bin/xcrun", "swiftc"):
            architecture = call[call.index("-target") + 1].split("-", 1)[0]
            if architecture == self.fail_architecture:
                return Completed(returncode=1, stderr=f"{architecture} compile failed")
            output = Path(call[call.index("-o") + 1])
            output.write_bytes(f"synthetic-{architecture}".encode())
        elif call[0] == "/usr/bin/lipo":
            output = Path(call[call.index("-output") + 1])
            inputs = [Path(value) for value in call[2 : call.index("-output")]]
            output.write_bytes(b"\n".join(path.read_bytes() for path in inputs))
        return Completed()


def _vault(tmp_path: Path, name: str = "records") -> Vault:
    vault = Vault(tmp_path / name)
    vault.initialize(name="Synthetic owner")
    return vault


def _executable(tmp_path: Path, body: str = "exit 0") -> Path:
    executable = tmp_path / "runtime with spaces/bin/gsv"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _install(
    tmp_path: Path,
    *,
    vault: Vault | None = None,
    executable: Path | None = None,
    runner: SyntheticMacRunner | None = None,
) -> tuple[dict[str, Any], Vault, Path, SyntheticMacRunner]:
    selected_vault = vault or _vault(tmp_path)
    selected_executable = executable or _executable(tmp_path)
    selected_runner = runner or SyntheticMacRunner()
    result = install_native_bridge(
        selected_vault,
        executable=selected_executable,
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
        runner=selected_runner,
    )
    return result, selected_vault, selected_executable, selected_runner


def _receipt(application: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((application / "Contents/Resources" / RECEIPT_NAME).read_text(encoding="utf-8")),
    )


def test_install_builds_owned_universal_app_with_exact_runtime_and_assets(
    tmp_path: Path,
) -> None:
    result, vault, executable, runner = _install(tmp_path)
    application = Path(result["application"])

    assert application == tmp_path / "Applications" / APP_BUNDLE_NAME
    assert result == {
        "application": str(application),
        "architectures": ["arm64", "x86_64"],
        "changed": True,
        "distribution_ready": False,
        "ownership_revision": result["ownership_revision"],
        "receipt_revision": result["receipt_revision"],
        "signing": "unsigned_local",
        "vault": str(vault.root),
        "vault_id": str(vault.identity()["vault_id"]),
    }
    swift_commands = [call for call in runner.calls if call[:2] == ("/usr/bin/xcrun", "swiftc")]
    assert [call[call.index("-target") + 1] for call in swift_commands] == [
        "arm64-apple-macos13.0",
        "x86_64-apple-macos13.0",
    ]
    assert [call[0] for call in runner.calls] == [
        "/usr/bin/xcrun",
        "/usr/bin/xcrun",
        "/usr/bin/lipo",
    ]
    assert (application / "Contents/MacOS" / NATIVE_EXECUTABLE).read_bytes() == (
        b"synthetic-arm64\nsynthetic-x86_64"
    )

    receipt = _receipt(application)
    assert receipt["bundle_identifier"] == BUNDLE_IDENTIFIER
    assert receipt["executable"] == str(executable.resolve())
    assert receipt["executable_sha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert receipt["vault"] == str(vault.root)
    assert receipt["vault_id"] == str(vault.identity()["vault_id"])
    assert receipt["bridge_state"] == str(tmp_path / "runtime-state/bridge-state.json")
    assert len(receipt["install_id"]) == 64
    assert not any("second-brain" in json.dumps(entry) for entry in receipt["bundle_manifest"])
    authority_path = tmp_path / "runtime-state/native-bridge/state/ownership.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    assert authority["install_id"] == receipt["install_id"]
    assert authority["bundle_receipt_revision"] == result["receipt_revision"]
    assert authority["state"] == "installed"
    assert authority_path.stat().st_mode & 0o077 == 0

    info = plistlib.loads((application / "Contents/Info.plist").read_bytes())
    assert info["CFBundleDisplayName"] == "Seld"
    assert info["CFBundleIdentifier"] == BUNDLE_IDENTIFIER
    assert info["SeldVaultRoot"] == str(vault.root)
    assert info["SeldVaultID"] == str(vault.identity()["vault_id"])
    assert info["SeldBridgeStatePath"] == str(tmp_path / "runtime-state/bridge-state.json")

    bundled_bridge = (
        application / "Contents/Resources/python/continuity_kernel/resources/bridge/index.html"
    )
    source_bridge = Path(str(files("continuity_kernel") / "resources/bridge/index.html"))
    assert bundled_bridge.read_bytes() == source_bridge.read_bytes()
    bundled_runtime = application / "Contents/Resources/python/continuity_kernel/bridge_launcher.py"
    assert (
        bundled_runtime.read_bytes()
        == Path(__file__)
        .parents[1]
        .joinpath("src/continuity_kernel/bridge_launcher.py")
        .read_bytes()
    )
    service = application / "Contents/MacOS" / SERVICE_EXECUTABLE
    service_text = service.read_text(encoding="utf-8")
    assert str(executable.resolve()) in service_text
    assert "PYTHONNOUSERSITE=1" in service_text
    assert "PYTHONDONTWRITEBYTECODE=1" in service_text
    assert 'PYTHONPATH="$service_directory/../Resources/python"' in service_text
    assert "/usr/bin/codesign" not in "\n".join(" ".join(call) for call in runner.calls)


def test_install_is_idempotent_without_recompiling(tmp_path: Path) -> None:
    first, vault, executable, runner = _install(tmp_path)
    runner.calls.clear()

    second = install_native_bridge(
        vault,
        executable=executable,
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
        runner=runner,
    )

    assert second == {**first, "changed": False}
    assert runner.calls == []
    assert (
        native_bridge_status(
            applications_dir=tmp_path / "Applications",
            executable=executable,
            runtime_data_dir=tmp_path / "runtime-state",
        )["current"]
        is True
    )


def test_service_and_swift_pin_the_exact_configured_vault(tmp_path: Path) -> None:
    capture = tmp_path / "captured-arguments"
    captured_data_dir = tmp_path / "captured-data-dir"
    executable = _executable(
        tmp_path,
        body=(
            'printf "%s\\n" "$@" > "$SELD_CAPTURE"\n'
            'printf "%s" "$GSV_DATA_DIR" > "$SELD_DATA_CAPTURE"'
        ),
    )
    result, vault, _, _ = _install(tmp_path, executable=executable)
    application = Path(result["application"])
    service = application / "Contents/MacOS" / SERVICE_EXECUTABLE
    environment = {
        **os.environ,
        "SELD_CAPTURE": str(capture),
        "SELD_DATA_CAPTURE": str(captured_data_dir),
    }

    subprocess.run(
        (
            str(service),
            "--json",
            "--vault",
            str(vault.root),
            "bridge",
            "open",
            "--no-browser",
        ),
        env=environment,
        check=True,
    )

    assert capture.read_text(encoding="utf-8").splitlines() == [
        "--json",
        "--vault",
        str(vault.root),
        "bridge",
        "open",
        "--no-browser",
    ]
    assert captured_data_dir.read_text(encoding="utf-8") == str(tmp_path / "runtime-state")
    swift = Path(str(files("continuity_kernel") / "resources/native/SeldBridge.swift")).read_text(
        encoding="utf-8"
    )
    assert '"--vault",\n            configuration.vaultRoot' in swift
    assert '"bridge",\n            "open",\n            "--no-browser"' in swift
    assert 'request.setValue("Bearer \\(state.token)"' in swift
    assert "health.vaultRootDevice == configuration.vaultRootDevice" in swift
    assert "health.vaultRootInode == configuration.vaultRootInode" in swift
    assert 'components.host == "127.0.0.1"' in swift


def test_status_and_mutations_refuse_foreign_or_tampered_apps(tmp_path: Path) -> None:
    applications = tmp_path / "Applications"
    foreign = applications / APP_BUNDLE_NAME
    foreign.mkdir(parents=True)
    (foreign / "owner-data").write_text("leave me alone", encoding="utf-8")
    executable = _executable(tmp_path)
    vault = _vault(tmp_path)

    status = native_bridge_status(
        applications_dir=applications,
        executable=executable,
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert status["error_code"] == "foreign_install"
    assert status["owned"] is False
    with pytest.raises(SetupError, match="foreign app"):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=applications,
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
        )
    with pytest.raises(SetupError, match="foreign app"):
        uninstall_native_bridge(
            applications_dir=applications,
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision="absent",
        )
    assert (foreign / "owner-data").read_text(encoding="utf-8") == "leave me alone"

    for child in tuple(foreign.iterdir()):
        child.unlink()
    foreign.rmdir()
    result, _, _, _ = _install(tmp_path, vault=vault, executable=executable)
    application = Path(result["application"])
    asset = application / "Contents/Resources/python/continuity_kernel/resources/bridge/index.html"
    asset.write_text("tampered", encoding="utf-8")

    status = native_bridge_status(
        applications_dir=applications,
        executable=executable,
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert status["error_code"] == "tampered_install"
    assert status["owned"] is True
    with pytest.raises(SetupError, match="integrity checks"):
        open_native_bridge(
            applications_dir=applications,
            executable=executable,
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
        )
    with pytest.raises(SetupError, match="integrity checks"):
        uninstall_native_bridge(
            applications_dir=applications,
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision=result["ownership_revision"],
        )
    assert asset.read_text(encoding="utf-8") == "tampered"


def test_self_consistent_bundle_without_host_anchor_is_foreign(tmp_path: Path) -> None:
    result, vault, executable, _runner = _install(tmp_path)
    application = Path(result["application"])
    authority = tmp_path / "runtime-state/native-bridge/state/ownership.json"
    authority.unlink()

    status = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        executable=executable,
        runtime_data_dir=tmp_path / "runtime-state",
    )

    assert status["error_code"] == "foreign_install"
    assert status["owned"] is False
    with pytest.raises(SetupError, match="foreign app"):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
            expected_revision=result["ownership_revision"],
        )
    with pytest.raises(SetupError, match="foreign app"):
        uninstall_native_bridge(
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision=result["ownership_revision"],
        )
    assert application.is_dir()


def test_update_and_uninstall_require_exact_host_revision(tmp_path: Path) -> None:
    first, vault, executable, _runner = _install(tmp_path)
    executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    executable.chmod(0o755)

    for stale in (None, "0" * 64):
        with pytest.raises(ConflictError, match="ownership changed"):
            install_native_bridge(
                vault,
                executable=executable,
                applications_dir=tmp_path / "Applications",
                runtime_data_dir=tmp_path / "runtime-state",
                runner=SyntheticMacRunner(),
                expected_revision=stale,
            )
    updated = install_native_bridge(
        vault,
        executable=executable,
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
        runner=SyntheticMacRunner(),
        expected_revision=first["ownership_revision"],
    )
    assert updated["changed"] is True
    assert updated["ownership_revision"] != first["ownership_revision"]

    with pytest.raises(ConflictError, match="ownership changed"):
        uninstall_native_bridge(
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision=first["ownership_revision"],
        )
    assert Path(updated["application"]).is_dir()


def test_concurrent_different_updates_allow_only_one_exact_revision(tmp_path: Path) -> None:
    first, vault, _executable_path, _runner = _install(tmp_path)
    executable_one = _executable(tmp_path / "one", body="exit 1")
    executable_two = _executable(tmp_path / "two", body="exit 2")

    def update(executable: Path) -> tuple[str, str]:
        try:
            result = install_native_bridge(
                vault,
                executable=executable,
                applications_dir=tmp_path / "Applications",
                runtime_data_dir=tmp_path / "runtime-state",
                runner=SyntheticMacRunner(),
                expected_revision=first["ownership_revision"],
            )
            return "installed", str(result["ownership_revision"])
        except ConflictError as exc:
            return "conflict", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, (executable_one, executable_two)))

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "installed"]
    status = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert status["healthy"] is True
    assert status["ownership_revision"] != first["ownership_revision"]


def test_destination_swap_after_validation_is_not_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, vault, executable, _runner = _install(tmp_path)
    executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    executable.chmod(0o755)
    application = Path(first["application"])
    preserved = tmp_path / "preserved-owned-app"
    original_swap = launcher._swap_application

    def attacked_swap(*args: Any, **kwargs: Any) -> Any:
        application.rename(preserved)
        application.mkdir()
        (application / "foreign-marker").write_text("do not replace", encoding="utf-8")
        return original_swap(*args, **kwargs)

    monkeypatch.setattr(launcher, "_swap_application", attacked_swap)
    with pytest.raises(ConflictError, match="changed before replacement"):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
            expected_revision=first["ownership_revision"],
        )

    assert (application / "foreign-marker").read_text(encoding="utf-8") == "do not replace"
    assert preserved.is_dir()


def test_manifest_and_copy_reject_symlink_swap_and_bounded_tree_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    safe = source / "safe.py"
    safe.write_text("safe", encoding="utf-8")
    manifest = launcher._descriptor_tree_manifest(
        source,
        label="test runtime",
        max_total_bytes=1024,
        exclude_runtime_noise=False,
    )
    outside = tmp_path / "outside.py"
    outside.write_text("foreign", encoding="utf-8")
    safe.unlink()
    safe.symlink_to(outside)
    with pytest.raises(SetupError, match="changed while it was bundled"):
        launcher._copy_runtime(source, destination, manifest)

    wide = tmp_path / "wide"
    wide.mkdir()
    for index in range(4):
        (wide / f"{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(launcher, "MAX_MANIFEST_ENTRIES", 3)
    with pytest.raises(SetupError, match="too many entries"):
        launcher._descriptor_tree_manifest(
            wide,
            label="wide app",
            max_total_bytes=1024,
            exclude_runtime_noise=False,
        )

    monkeypatch.setattr(launcher, "MAX_MANIFEST_ENTRIES", 100)
    monkeypatch.setattr(launcher, "MAX_MANIFEST_DEPTH", 2)
    deep = tmp_path / "deep"
    (deep / "one/two/three").mkdir(parents=True)
    with pytest.raises(SetupError, match="directory depth"):
        launcher._descriptor_tree_manifest(
            deep,
            label="deep app",
            max_total_bytes=1024,
            exclude_runtime_noise=False,
        )


def test_descriptor_cleanup_never_deletes_a_swapped_foreign_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "owned-cleanup"
    target.mkdir()
    (target / "owned.txt").write_text("owned", encoding="utf-8")
    metadata = os.lstat(target)
    expected_identity = (int(metadata.st_dev), int(metadata.st_ino))
    preserved = tmp_path / "preserved-owned"
    original_remove = launcher._remove_directory_contents_fd
    swapped = False

    def swap_then_remove(*args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            target.rename(preserved)
            target.mkdir()
            (target / "foreign.txt").write_text("foreign", encoding="utf-8")
        original_remove(*args, **kwargs)

    monkeypatch.setattr(launcher, "_remove_directory_contents_fd", swap_then_remove)
    with pytest.raises(SetupError, match="moved before final removal"):
        launcher._rmtree_exact(target, expected_identity=expected_identity)

    assert (target / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    assert preserved.is_dir()


def test_native_command_timeout_kills_its_descendant_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    monkeypatch.setattr(launcher, "SUBPROCESS_TIMEOUT_SECONDS", 0.2)

    with pytest.raises(SetupError, match="timed out"):
        launcher._run(
            (
                "/bin/sh",
                "-c",
                'sleep 60 & child=$!; echo "$child" > "$1"; wait "$child"',
                "seld-timeout",
                str(child_pid_path),
            ),
            runner=None,
        )

    child_pid = int(child_pid_path.read_text(encoding="ascii").strip())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = subprocess.run(
            ("/bin/ps", "-p", str(child_pid), "-o", "stat="),
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            status.returncode != 0
            or not status.stdout.strip()
            or status.stdout.lstrip().startswith("Z")
        ):
            break
        time.sleep(0.02)
    else:
        pytest.fail("native command descendant survived the process-group timeout")


def test_native_command_output_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "MAX_SUBPROCESS_OUTPUT_BYTES", 32)

    with pytest.raises(SetupError, match="too much output"):
        launcher._run(
            (sys.executable, "-c", "print('x' * 1024)"),
            runner=None,
        )


class _PermissionRaceProcess:
    pid = 45_678

    def __init__(self, *, exits: bool) -> None:
        self.exits = exits
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if not self.exits:
            raise subprocess.TimeoutExpired("synthetic native command", timeout or 0.0)
        return 0


def test_process_group_permission_race_accepts_only_an_observably_reaped_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_group_signal(_pid: int, _signal: int) -> None:
        raise PermissionError("process group exited during bounded-output handling")

    monkeypatch.setattr(os, "killpg", deny_group_signal)
    exited = _PermissionRaceProcess(exits=True)

    launcher._terminate_process_group(cast(subprocess.Popen[bytes], exited))

    assert exited.wait_timeouts == [0.1]


def test_process_group_permission_error_remains_fatal_for_a_running_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_group_signal(_pid: int, _signal: int) -> None:
        raise PermissionError("group signal denied")

    monkeypatch.setattr(os, "killpg", deny_group_signal)
    running = _PermissionRaceProcess(exits=False)

    with pytest.raises(PermissionError, match="group signal denied"):
        launcher._terminate_process_group(cast(subprocess.Popen[bytes], running))

    assert running.wait_timeouts == [0.1]


def test_compile_failure_preserves_the_previous_owned_app(tmp_path: Path) -> None:
    result, vault, executable, _ = _install(tmp_path)
    application = Path(result["application"])
    previous_receipt = (application / "Contents/Resources" / RECEIPT_NAME).read_bytes()
    executable.write_text("#!/bin/sh\nexit 4\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(SetupError, match="x86_64 compile failed"):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(fail_architecture="x86_64"),
            expected_revision=result["ownership_revision"],
        )

    assert (application / "Contents/Resources" / RECEIPT_NAME).read_bytes() == previous_receipt
    status = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert status["healthy"] is True
    assert status["current"] is False


def test_host_receipt_publication_failure_rolls_back_the_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, vault, executable, _runner = _install(tmp_path)
    application = Path(first["application"])
    prior_bundle = (application / "Contents/Resources" / RECEIPT_NAME).read_bytes()
    prior_authority = (tmp_path / "runtime-state/native-bridge/state/ownership.json").read_bytes()
    executable.write_text("#!/bin/sh\nexit 6\n", encoding="utf-8")
    executable.chmod(0o755)

    def fail_publication(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected authority publication failure")

    monkeypatch.setattr(launcher, "_write_authority", fail_publication)
    with pytest.raises(OSError, match="injected authority"):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
            expected_revision=first["ownership_revision"],
        )

    assert (application / "Contents/Resources" / RECEIPT_NAME).read_bytes() == prior_bundle
    assert (
        tmp_path / "runtime-state/native-bridge/state/ownership.json"
    ).read_bytes() == prior_authority


def test_fresh_status_recovers_hard_stop_after_prior_app_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, vault, executable, _runner = _install(tmp_path)
    executable.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    executable.chmod(0o755)
    original_rename = os.rename
    stopped = False

    def stop_after_preserve(source: Any, target: Any, **kwargs: Any) -> None:
        nonlocal stopped
        original_rename(source, target, **kwargs)
        if source == APP_BUNDLE_NAME and str(target).startswith(f".{APP_BUNDLE_NAME}.previous-"):
            stopped = True
            raise HardStop

    monkeypatch.setattr(os, "rename", stop_after_preserve)
    with pytest.raises(HardStop):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
            expected_revision=first["ownership_revision"],
        )
    assert stopped
    monkeypatch.setattr(os, "rename", original_rename)

    recovered = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert recovered["healthy"] is True
    assert recovered["ownership_revision"] == first["ownership_revision"]
    assert not list((tmp_path / "Applications").glob(".Seld.app.previous-*"))


def test_forged_lifecycle_receipt_never_deletes_a_foreign_app(tmp_path: Path) -> None:
    application = tmp_path / "Applications" / APP_BUNDLE_NAME
    application.mkdir(parents=True)
    marker = application / "foreign-marker"
    marker.write_bytes(b"preserve-exact-foreign-bytes")
    identity = application.stat()
    runtime_state = tmp_path / "runtime-state"
    state = runtime_state / "native-bridge/state"
    state.mkdir(parents=True)
    replacement = {
        "application": str(application),
        "application_device": identity.st_dev,
        "application_inode": identity.st_ino,
        "bundle_receipt_revision": "0" * 64,
        "format_version": 1,
        "install_id": "1" * 64,
        "ownership": "seld.native-bridge",
        "runtime_data_dir": str(runtime_state),
        "state": "installed",
        "vault": str(tmp_path / "foreign-vault"),
        "vault_id": "foreign-vault-id",
    }
    lifecycle = {
        "application": str(application),
        "backup_name": None,
        "candidate_identity": [identity.st_dev, identity.st_ino],
        "format_version": 1,
        "kind": "install",
        "prior_authority": None,
        "prior_identity": None,
        "replacement_authority": replacement,
        "runtime_data_dir": str(runtime_state),
        "state": "prepared",
    }
    lifecycle_path = state / "lifecycle.json"
    lifecycle_path.write_text(json.dumps(lifecycle, sort_keys=True) + "\n", encoding="utf-8")
    lifecycle_path.chmod(0o600)

    with pytest.raises(SetupError, match=r"foreign app|ownership receipt"):
        native_bridge_status(
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=runtime_state,
        )

    assert application.is_dir()
    assert marker.read_bytes() == b"preserve-exact-foreign-bytes"


def test_fresh_status_commits_hard_stop_after_install_authority_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, vault, executable, _runner = _install(tmp_path)
    executable.write_text("#!/bin/sh\nexit 8\n", encoding="utf-8")
    executable.chmod(0o755)
    original_write = launcher._write_authority

    def publish_then_stop(*args: Any, **kwargs: Any) -> None:
        original_write(*args, **kwargs)
        raise HardStop

    monkeypatch.setattr(launcher, "_write_authority", publish_then_stop)
    with pytest.raises(HardStop):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
            expected_revision=first["ownership_revision"],
        )
    monkeypatch.setattr(launcher, "_write_authority", original_write)

    recovered = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        executable=executable,
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert recovered["healthy"] is True
    assert recovered["current"] is True
    assert recovered["ownership_revision"] != first["ownership_revision"]
    assert not list((tmp_path / "Applications").glob(".Seld.app.previous-*"))


def test_fresh_status_finishes_install_after_backup_cleanup_hard_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, vault, executable, _runner = _install(tmp_path)
    executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    executable.chmod(0o755)
    original_remove = launcher._rmtree_exact
    stopped = False

    def remove_then_stop(path: Path, *, expected_identity: tuple[int, int]) -> None:
        nonlocal stopped
        original_remove(path, expected_identity=expected_identity)
        if path.name.startswith(f".{APP_BUNDLE_NAME}.previous-"):
            stopped = True
            raise HardStop

    monkeypatch.setattr(launcher, "_rmtree_exact", remove_then_stop)
    with pytest.raises(HardStop):
        install_native_bridge(
            vault,
            executable=executable,
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            runner=SyntheticMacRunner(),
            expected_revision=first["ownership_revision"],
        )
    assert stopped
    monkeypatch.setattr(launcher, "_rmtree_exact", original_remove)

    recovered = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        executable=executable,
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert recovered["healthy"] is True
    assert recovered["current"] is True


def test_fresh_status_rolls_back_hard_stop_before_uninstall_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _vault_record, _executable_path, _runner = _install(tmp_path)
    original_write = launcher._write_authority

    def stop_before_publication(*args: Any, **kwargs: Any) -> None:
        raise HardStop

    monkeypatch.setattr(launcher, "_write_authority", stop_before_publication)
    with pytest.raises(HardStop):
        uninstall_native_bridge(
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision=result["ownership_revision"],
        )
    monkeypatch.setattr(launcher, "_write_authority", original_write)

    recovered = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert recovered["healthy"] is True
    assert recovered["ownership_revision"] == result["ownership_revision"]
    assert not list((tmp_path / "Applications").glob(".Seld.app.uninstall-*"))


def test_fresh_uninstall_resumes_hard_stop_after_removing_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _vault_record, _executable_path, _runner = _install(tmp_path)
    original_write = launcher._write_authority

    def publish_then_stop(*args: Any, **kwargs: Any) -> None:
        original_write(*args, **kwargs)
        raise HardStop

    monkeypatch.setattr(launcher, "_write_authority", publish_then_stop)
    with pytest.raises(HardStop):
        uninstall_native_bridge(
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision=result["ownership_revision"],
        )
    monkeypatch.setattr(launcher, "_write_authority", original_write)

    pending = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert pending["error_code"] == "cleanup_pending"
    removed = uninstall_native_bridge(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
        expected_revision=pending["ownership_revision"],
    )
    assert removed["removed"] is True


def test_open_and_uninstall_require_owned_current_app_and_preserve_user_state(
    tmp_path: Path,
) -> None:
    result, vault, executable, _ = _install(tmp_path)
    runner = SyntheticMacRunner()

    opened = open_native_bridge(
        applications_dir=tmp_path / "Applications",
        executable=executable,
        runtime_data_dir=tmp_path / "runtime-state",
        runner=runner,
    )
    assert opened == {
        "application": result["application"],
        "opened": True,
        "vault": str(vault.root),
        "vault_id": str(vault.identity()["vault_id"]),
    }
    assert runner.calls == [("/usr/bin/open", result["application"])]

    removed = uninstall_native_bridge(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
        expected_revision=result["ownership_revision"],
    )
    assert removed == {
        "application": result["application"],
        "removed": True,
        "ownership_revision": removed["ownership_revision"],
        "vault_preserved": str(vault.root),
    }
    assert not Path(result["application"]).exists()
    assert vault.root.is_dir()
    assert executable.is_file()
    assert (
        uninstall_native_bridge(
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
        )["removed"]
        is False
    )


def test_partial_uninstall_cleanup_stays_tombstoned_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _vault_record, _executable_path, _runner = _install(tmp_path)
    original_remove = launcher._remove_directory_contents_fd
    injected = False

    def partial_then_fail(*args: Any, **kwargs: Any) -> None:
        nonlocal injected
        original_remove(*args, **kwargs)
        if not injected:
            injected = True
            raise SetupError("injected partial cleanup failure")

    monkeypatch.setattr(launcher, "_remove_directory_contents_fd", partial_then_fail)
    with pytest.raises(SetupError, match="cleanup remains pending"):
        uninstall_native_bridge(
            applications_dir=tmp_path / "Applications",
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision=result["ownership_revision"],
        )

    application = Path(result["application"])
    assert not application.exists()
    pending = native_bridge_status(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
    )
    assert pending["error_code"] == "cleanup_pending"
    assert pending["owned"] is True

    monkeypatch.setattr(launcher, "_remove_directory_contents_fd", original_remove)
    recovered = uninstall_native_bridge(
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
        expected_revision=pending["ownership_revision"],
    )
    assert recovered["removed"] is True
    assert not list((tmp_path / "Applications").glob(".Seld.app.uninstall-*"))


def test_authority_rollback_refuses_a_swapped_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _vault_record, _executable_path, _runner = _install(tmp_path)
    applications = tmp_path / "Applications"
    preserved = tmp_path / "preserved-quarantine"

    def swap_and_fail(*args: Any, **kwargs: Any) -> None:
        quarantine = next(applications.glob(".Seld.app.uninstall-*"))
        quarantine.rename(preserved)
        quarantine.mkdir()
        (quarantine / "foreign.txt").write_text("foreign", encoding="utf-8")
        raise OSError("injected ownership write failure")

    monkeypatch.setattr(launcher, "_write_authority", swap_and_fail)
    with pytest.raises(SetupError, match="quarantined Seld app changed"):
        uninstall_native_bridge(
            applications_dir=applications,
            runtime_data_dir=tmp_path / "runtime-state",
            expected_revision=result["ownership_revision"],
        )

    assert not Path(result["application"]).exists()
    assert preserved.is_dir()
    foreign = next(applications.glob(".Seld.app.uninstall-*"))
    assert (foreign / "foreign.txt").read_text(encoding="utf-8") == "foreign"


def test_native_sources_are_package_resources() -> None:
    package = files("continuity_kernel")
    swift = package / "resources/native/SeldBridge.swift"
    bridge = package / "resources/bridge/index.html"

    assert swift.is_file()
    assert bridge.is_file()
    assert "SeldVaultRoot" in swift.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "darwin", reason="native Seld app builds only on macOS")
def test_real_swift_build_produces_a_universal_temp_app(tmp_path: Path) -> None:
    executable = Path(sys.executable).with_name("gsv")
    if not executable.is_file():
        pytest.skip("the test environment has no installed gsv entrypoint")
    vault = _vault(tmp_path)

    result = install_native_bridge(
        vault,
        executable=executable,
        applications_dir=tmp_path / "Applications",
        runtime_data_dir=tmp_path / "runtime-state",
    )

    native = Path(result["application"]) / "Contents/MacOS" / NATIVE_EXECUTABLE
    inspected = subprocess.run(
        ("/usr/bin/lipo", "-archs", str(native)),
        capture_output=True,
        text=True,
        check=True,
    )
    assert set(inspected.stdout.split()) == {"arm64", "x86_64"}
    assert (
        native_bridge_status(
            applications_dir=tmp_path / "Applications",
            executable=executable,
            runtime_data_dir=tmp_path / "runtime-state",
        )["current"]
        is True
    )


def test_cli_routes_the_four_native_lifecycle_commands_without_browser_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = _vault(tmp_path)
    executable = _executable(tmp_path)
    calls: list[tuple[str, str | None]] = []

    def fake_install(
        selected: Vault,
        *,
        executable: Path,
        expected_revision: str | None,
    ) -> dict[str, object]:
        del selected, executable
        calls.append(("install", expected_revision))
        return {"application": "/synthetic/Seld.app", "changed": True}

    def fake_status() -> dict[str, object]:
        calls.append(("status", None))
        return {"installed": True}

    def fake_open() -> dict[str, object]:
        calls.append(("open", None))
        return {"opened": True}

    def fake_uninstall(*, expected_revision: str | None) -> dict[str, object]:
        calls.append(("uninstall", expected_revision))
        return {"removed": True}

    monkeypatch.setattr(cli, "current_gsv_executable", lambda: executable)
    monkeypatch.setattr(cli, "install_native_bridge", fake_install)
    monkeypatch.setattr(cli, "native_bridge_status", fake_status)
    monkeypatch.setattr(cli, "open_native_bridge", fake_open)
    monkeypatch.setattr(cli, "uninstall_native_bridge", fake_uninstall)

    assert (
        cli.main(
            [
                "--json",
                "--vault",
                str(vault.root),
                "bridge",
                "native-install",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["changed"] is True
    for command, result_field in (
        ("native-status", "installed"),
        ("native-open", "opened"),
    ):
        assert cli.main(["--json", "bridge", command]) == 0
        assert json.loads(capsys.readouterr().out)["result"][result_field] is True
    assert (
        cli.main(
            [
                "--json",
                "bridge",
                "native-uninstall",
                "--expected-revision",
                "ownership-revision",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["removed"] is True
    assert calls == [
        ("install", None),
        ("status", None),
        ("open", None),
        ("uninstall", "ownership-revision"),
    ]
