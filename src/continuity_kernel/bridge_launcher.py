"""Owned, self-contained macOS launcher for the authenticated Seld Bridge."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

from continuity_kernel import __version__
from continuity_kernel.atomic import PinnedPathRoot, open_regular_file
from continuity_kernel.config import data_dir
from continuity_kernel.errors import ConflictError, SetupError, ValidationError
from continuity_kernel.vault import Vault

APP_NAME: Final = "Seld"
APP_BUNDLE_NAME: Final = f"{APP_NAME}.app"
BUNDLE_IDENTIFIER: Final = "ai.seld.bridge"
NATIVE_EXECUTABLE: Final = "SeldBridge"
SERVICE_EXECUTABLE: Final = "SeldBridgeService"
RECEIPT_NAME: Final = "seld-native-bridge.json"
RECEIPT_FORMAT_VERSION: Final = 1
AUTHORITY_FORMAT_VERSION: Final = 1
OWNERSHIP_MARKER: Final = "seld.native-bridge"
ABSENT_NATIVE_BRIDGE_REVISION: Final = "absent"
SUPPORTED_ARCHITECTURES: Final = ("arm64", "x86_64")
MINIMUM_MACOS_VERSION: Final = "13.0"
MAX_RECEIPT_BYTES: Final = 2 * 1024 * 1024
MAX_BUNDLE_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_RUNTIME_BYTES: Final = 128 * 1024 * 1024
MAX_AUTHORITY_BYTES: Final = 16 * 1024
MAX_LIFECYCLE_BYTES: Final = 64 * 1024
MAX_MANIFEST_FILES: Final = 20_000
MAX_MANIFEST_ENTRIES: Final = 24_000
MAX_MANIFEST_DEPTH: Final = 64
MAX_SUBPROCESS_OUTPUT_BYTES: Final = 1024 * 1024
SUBPROCESS_TIMEOUT_SECONDS: Final = 180.0
_SHA256_LENGTH: Final = 64
_INSTALL_ID_LENGTH: Final = 64
_LIFECYCLE_FORMAT_VERSION: Final = 1
_LIFECYCLE_PATH: Final = "state/lifecycle.json"


class Completed(Protocol):
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> Completed: ...


@dataclass(frozen=True)
class _Swap:
    installed_identity: tuple[int, int]
    backup_path: Path | None
    backup_identity: tuple[int, int] | None


@dataclass(frozen=True)
class _LifecycleState:
    payload: dict[str, Any] | None
    encoded: bytes | None


def native_bridge_paths(*, applications_dir: Path | None = None) -> dict[str, Path]:
    """Return the exact user-local paths owned by the native launcher."""

    applications = (applications_dir or Path.home() / "Applications").expanduser().resolve()
    application = applications / APP_BUNDLE_NAME
    return {
        "application": application,
        "receipt": application / "Contents/Resources" / RECEIPT_NAME,
    }


def current_gsv_executable() -> Path:
    """Resolve the installed public entrypoint used to create a self-contained launcher."""

    candidate = shutil.which("gsv")
    if candidate is None:
        raise SetupError(
            "Seld could not find the installed gsv command. Run native installation through "
            "the supported gsv entrypoint."
        )
    return _regular_executable(Path(candidate))


def install_native_bridge(
    vault: Vault,
    *,
    executable: Path,
    applications_dir: Path | None = None,
    runtime_data_dir: Path | None = None,
    runner: Runner | None = None,
    architectures: Sequence[str] = SUPPORTED_ARCHITECTURES,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Install one unsigned local app pinned to this runtime and exact vault identity."""

    target = native_bridge_paths(applications_dir=applications_dir)
    application = target["application"]
    applications = application.parent
    runtime_state = (runtime_data_dir or data_dir()).expanduser().resolve()
    _ensure_regular_directory(applications)
    authority_store = _authority_store(runtime_state, create=True)
    assert authority_store is not None
    try:
        authority_store.ensure_directory("state", mode=0o700)
        with authority_store.exclusive_root_lock():
            _recover_native_lifecycle(
                authority_store,
                application=application,
                runtime_state=runtime_state,
            )
            authority, authority_bytes = _read_authority(authority_store)
            existing: dict[str, Any] | None = None
            existing_identity: tuple[int, int] | None = None
            if os.path.lexists(application):
                existing = _owned_receipt(application)
                if authority is None or not _authority_matches(
                    authority,
                    application=application,
                    receipt=existing,
                    runtime_state=runtime_state,
                ):
                    raise SetupError(f"Refusing a foreign app at the Seld app path: {application}")
                _validate_bundle(application, existing)
                existing_identity = _directory_identity(application, label="installed Seld app")
                install_id = str(authority["install_id"])
            else:
                if authority is not None and authority["state"] == "installed":
                    raise SetupError(
                        "Seld's host ownership receipt exists but its app is missing; repair or "
                        "remove the stale receipt before installing."
                    )
                if authority is not None and authority["state"] == "removing":
                    raise SetupError(
                        "Seld native cleanup is pending; finish native-uninstall before installing"
                    )
                if authority is None:
                    if expected_revision not in {None, ABSENT_NATIVE_BRIDGE_REVISION}:
                        raise ConflictError("Seld native ownership changed before installation")
                else:
                    _require_expected_authority(authority, expected_revision)
                install_id = secrets.token_hex(32)

            resolved_executable = _regular_executable(executable)
            selected_architectures = _architectures(architectures)
            runtime_root = Path(__file__).resolve().parent
            source_runtime = _source_runtime_manifest(runtime_root)
            source_runtime_digest = _tree_digest(source_runtime)
            executable_digest = _sha256_file(resolved_executable)
            root_metadata = os.lstat(vault.root)
            if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
                raise SetupError(f"Seld vault is not a regular directory: {vault.root}")
            vault_id = str(vault.identity()["vault_id"])
            desired = {
                "application": str(application),
                "architectures": list(selected_architectures),
                "bridge_state": str(runtime_state / "bridge-state.json"),
                "bundle_identifier": BUNDLE_IDENTIFIER,
                "executable": str(resolved_executable),
                "executable_sha256": executable_digest,
                "format_version": RECEIPT_FORMAT_VERSION,
                "install_id": install_id,
                "ownership": OWNERSHIP_MARKER,
                "runtime_data_dir": str(runtime_state),
                "source_runtime_digest": source_runtime_digest,
                "vault": str(vault.root),
                "vault_id": vault_id,
                "vault_root_device": int(root_metadata.st_dev),
                "vault_root_inode": int(root_metadata.st_ino),
                "version": __version__,
            }
            if existing is not None and _receipt_matches(existing, desired):
                return _install_result(
                    application,
                    existing,
                    authority=authority,
                    changed=False,
                )
            if authority is not None and authority["state"] == "installed":
                _require_expected_authority(authority, expected_revision)

            native_source = runtime_root / "resources/native/SeldBridge.swift"
            _regular_file(
                native_source,
                label="Seld native source",
                max_bytes=MAX_BUNDLE_FILE_BYTES,
            )
            with tempfile.TemporaryDirectory(prefix=".seld-native-", dir=applications) as temporary:
                native_build_source = Path(temporary) / "SeldBridge.swift"
                native_source_bytes = _read_bundle_file(
                    runtime_root,
                    Path("resources/native/SeldBridge.swift"),
                    label="Seld native source",
                    max_bytes=MAX_BUNDLE_FILE_BYTES,
                )
                source_entry = next(
                    entry
                    for entry in source_runtime
                    if entry["path"] == "resources/native/SeldBridge.swift"
                )
                if hashlib.sha256(native_source_bytes).hexdigest() != source_entry["sha256"]:
                    raise SetupError("Seld native source changed before compilation")
                native_build_source.write_bytes(native_source_bytes)
                staged = Path(temporary) / APP_BUNDLE_NAME
                contents = staged / "Contents"
                macos = contents / "MacOS"
                resources = contents / "Resources"
                bundled_runtime = resources / "python/continuity_kernel"
                macos.mkdir(parents=True)
                bundled_runtime.mkdir(parents=True)

                native_binary = macos / NATIVE_EXECUTABLE
                _build_universal_binary(
                    native_build_source,
                    native_binary,
                    runner=runner,
                    architectures=selected_architectures,
                )
                native_binary.chmod(0o755)
                _copy_runtime(runtime_root, bundled_runtime, source_runtime)
                service = macos / SERVICE_EXECUTABLE
                _write_service_launcher(
                    service,
                    executable=resolved_executable,
                    executable_digest=executable_digest,
                    runtime_data_dir=runtime_state,
                )
                info = _info_plist(desired)
                info_path = contents / "Info.plist"
                with info_path.open("wb") as handle:
                    plistlib.dump(info, handle, fmt=plistlib.FMT_XML, sort_keys=True)
                info_path.chmod(0o644)

                bundle_manifest = _bundle_manifest(staged)
                receipt = {**desired, "bundle_manifest": bundle_manifest}
                receipt_path = resources / RECEIPT_NAME
                receipt_path.write_bytes(_encode_receipt(receipt))
                receipt_path.chmod(0o600)
                _validate_bundle(staged, receipt)
                staged_identity = _directory_identity(staged, label="staged Seld app")
                next_authority = _authority_payload(
                    application=application,
                    application_identity=staged_identity,
                    receipt=receipt,
                    runtime_state=runtime_state,
                )
                backup_name = (
                    f".{APP_BUNDLE_NAME}.previous-{secrets.token_hex(16)}"
                    if existing_identity is not None
                    else None
                )
                lifecycle = _install_lifecycle_payload(
                    application=application,
                    runtime_state=runtime_state,
                    prior_authority=authority,
                    replacement_authority=next_authority,
                    prior_identity=existing_identity,
                    candidate_identity=staged_identity,
                    backup_name=backup_name,
                )
                _begin_native_lifecycle(authority_store, lifecycle)
                try:
                    swap = _swap_application(
                        staged,
                        application,
                        expected_destination_identity=existing_identity,
                        expected_source_identity=staged_identity,
                        backup_name=backup_name,
                    )
                    installed_identity = _directory_identity(
                        application, label="installed Seld app"
                    )
                    if installed_identity != swap.installed_identity:
                        raise SetupError("Seld app changed immediately after installation")
                    _write_authority(
                        authority_store,
                        expected=authority_bytes,
                        replacement=next_authority,
                    )
                except Exception as exc:
                    try:
                        outcome = _recover_native_lifecycle(
                            authority_store,
                            application=application,
                            runtime_state=runtime_state,
                        )
                    except SetupError:
                        if isinstance(exc, ConflictError):
                            raise exc from None
                        raise
                    if outcome != "committed":
                        raise
                else:
                    outcome = _recover_native_lifecycle(
                        authority_store,
                        application=application,
                        runtime_state=runtime_state,
                    )
                    if outcome != "committed":
                        raise SetupError("Seld app installation did not commit")
                _require_idle_lifecycle(authority_store)

            installed = _owned_receipt(application)
            _validate_bundle(application, installed)
            published_authority, _published_bytes = _read_authority(authority_store)
            if published_authority is None or not _authority_matches(
                published_authority,
                application=application,
                receipt=installed,
                runtime_state=runtime_state,
            ):
                raise SetupError("Seld could not verify its host ownership receipt after install")
            return _install_result(
                application,
                installed,
                authority=published_authority,
                changed=True,
            )
    finally:
        authority_store.close()


def native_bridge_status(
    *,
    applications_dir: Path | None = None,
    executable: Path | None = None,
    runtime_data_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect launcher ownership and provenance without starting or changing anything."""

    application = native_bridge_paths(applications_dir=applications_dir)["application"]
    runtime_state = (runtime_data_dir or data_dir()).expanduser().resolve()
    authority_store = _authority_store(runtime_state, create=False)
    if authority_store is None:
        return _status_without_authority(application)
    try:
        with authority_store.exclusive_root_lock():
            _recover_native_lifecycle(
                authority_store,
                application=application,
                runtime_state=runtime_state,
            )
            return _native_bridge_status_locked(
                application,
                authority_store=authority_store,
                runtime_state=runtime_state,
                executable=executable,
            )
    finally:
        authority_store.close()


def open_native_bridge(
    *,
    applications_dir: Path | None = None,
    executable: Path | None = None,
    runtime_data_dir: Path | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Open only a healthy, current, Seld-owned native launcher."""

    application = native_bridge_paths(applications_dir=applications_dir)["application"]
    runtime_state = (runtime_data_dir or data_dir()).expanduser().resolve()
    authority_store = _authority_store(runtime_state, create=False)
    if authority_store is None:
        raise SetupError("The Seld app is not installed. Run `gsv bridge native-install` first.")
    try:
        with authority_store.exclusive_root_lock():
            _recover_native_lifecycle(
                authority_store,
                application=application,
                runtime_state=runtime_state,
            )
            status_payload = _native_bridge_status_locked(
                application,
                authority_store=authority_store,
                runtime_state=runtime_state,
                executable=executable,
            )
            if not status_payload.get("installed"):
                raise SetupError(
                    "The Seld app is not installed. Run `gsv bridge native-install` first."
                )
            if not status_payload.get("healthy"):
                raise SetupError(
                    str(status_payload.get("error", "The Seld app failed integrity checks"))
                )
            if status_payload.get("current") is not True:
                raise SetupError(
                    "The Seld app is from another runtime revision. Reinstall it before opening."
                )
            command = ("/usr/bin/open", str(application))
            completed = _run(command, runner=runner)
            if completed.returncode != 0:
                detail = _bounded_detail(completed.stderr)
                suffix = f": {detail}" if detail else ""
                raise SetupError(f"macOS could not open the Seld app{suffix}")
            return {
                "application": str(application),
                "opened": True,
                "vault": status_payload["vault"],
                "vault_id": status_payload["vault_id"],
            }
    finally:
        authority_store.close()


def uninstall_native_bridge(
    *,
    applications_dir: Path | None = None,
    runtime_data_dir: Path | None = None,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    """Remove only the exact healthy bundle proven by its Seld ownership receipt."""

    application = native_bridge_paths(applications_dir=applications_dir)["application"]
    runtime_state = (runtime_data_dir or data_dir()).expanduser().resolve()
    authority_store = _authority_store(runtime_state, create=False)
    if authority_store is None:
        if os.path.lexists(application):
            raise SetupError(f"Refusing a foreign app at the Seld app path: {application}")
        return {"application": str(application), "removed": False}
    try:
        with authority_store.exclusive_root_lock():
            _recover_native_lifecycle(
                authority_store,
                application=application,
                runtime_state=runtime_state,
            )
            authority, authority_bytes = _read_authority(authority_store)
            if not os.path.lexists(application):
                if authority is not None and authority["state"] == "installed":
                    raise SetupError("Seld's host ownership receipt exists but its app is missing")
                if authority is not None and authority["state"] == "removing":
                    _require_expected_authority(authority, expected_revision)
                    assert authority_bytes is not None
                    return _complete_native_removal(
                        authority_store,
                        authority=authority,
                        authority_bytes=authority_bytes,
                        application=application,
                    )
                return {"application": str(application), "removed": False}
            receipt = _owned_receipt(application)
            if authority is None or not _authority_matches(
                authority,
                application=application,
                receipt=receipt,
                runtime_state=runtime_state,
            ):
                raise SetupError(f"Refusing a foreign app at the Seld app path: {application}")
            _require_expected_authority(authority, expected_revision)
            _validate_bundle(application, receipt)
            installed_identity = _directory_identity(application, label="installed Seld app")
            quarantine_name = f".{APP_BUNDLE_NAME}.uninstall-{secrets.token_hex(16)}"
            quarantine = application.parent / quarantine_name
            removing = _authority_removing(
                authority,
                quarantine=quarantine,
                quarantine_identity=installed_identity,
            )
            removing_bytes = _encode_authority(removing)
            lifecycle = _uninstall_lifecycle_payload(
                application=application,
                runtime_state=runtime_state,
                prior_authority=authority,
                replacement_authority=removing,
                prior_identity=installed_identity,
                quarantine_name=quarantine_name,
            )
            _begin_native_lifecycle(authority_store, lifecycle)
            try:
                _quarantine_application(
                    application,
                    expected_identity=installed_identity,
                    quarantine_name=quarantine_name,
                )
                _write_authority(
                    authority_store,
                    expected=authority_bytes,
                    replacement=removing,
                )
            except Exception:
                outcome = _recover_native_lifecycle(
                    authority_store,
                    application=application,
                    runtime_state=runtime_state,
                )
                if outcome != "committed":
                    raise
            else:
                outcome = _recover_native_lifecycle(
                    authority_store,
                    application=application,
                    runtime_state=runtime_state,
                )
                if outcome != "committed":
                    raise SetupError("Seld native uninstall did not enter cleanup state")
            _require_idle_lifecycle(authority_store)
            return _complete_native_removal(
                authority_store,
                authority=removing,
                authority_bytes=removing_bytes,
                application=application,
            )
    finally:
        authority_store.close()


def _complete_native_removal(
    authority_store: PinnedPathRoot,
    *,
    authority: dict[str, Any],
    authority_bytes: bytes,
    application: Path,
) -> dict[str, Any]:
    if authority.get("state") != "removing":
        raise SetupError("Seld native cleanup state is invalid")
    quarantine = application.parent / str(authority["trash_name"])
    trash_identity = (int(authority["trash_device"]), int(authority["trash_inode"]))
    if os.path.lexists(quarantine):
        try:
            _rmtree_exact(quarantine, expected_identity=trash_identity)
        except Exception as exc:
            raise SetupError(
                "The Seld app is uninstalled, but owned cleanup remains pending; rerun "
                "native-uninstall with the current ownership revision"
            ) from exc
    tombstone = _authority_tombstone(authority)
    tombstone_bytes = _encode_authority(tombstone)
    try:
        _write_authority(
            authority_store,
            expected=authority_bytes,
            replacement=tombstone,
        )
    except Exception as exc:
        _current_authority, current_bytes = _read_authority(authority_store)
        if current_bytes != tombstone_bytes:
            raise SetupError(
                "The Seld app is removed, but its host cleanup receipt still needs repair"
            ) from exc
    return {
        "application": str(application),
        "removed": True,
        "ownership_revision": _authority_revision(tombstone),
        "vault_preserved": authority["previous_vault"],
    }


def _install_result(
    application: Path,
    receipt: dict[str, Any],
    *,
    authority: dict[str, Any] | None,
    changed: bool,
) -> dict[str, Any]:
    if authority is None:
        raise SetupError("Seld host ownership receipt is unavailable")
    return {
        "application": str(application),
        "architectures": list(receipt["architectures"]),
        "changed": changed,
        "distribution_ready": False,
        "receipt_revision": _receipt_revision(receipt),
        "ownership_revision": _authority_revision(authority),
        "signing": "unsigned_local",
        "vault": receipt["vault"],
        "vault_id": receipt["vault_id"],
    }


def _authority_store(runtime_state: Path, *, create: bool) -> PinnedPathRoot | None:
    root = runtime_state / "native-bridge"
    if create:
        _ensure_regular_directory(runtime_state, mode=0o700)
        _ensure_regular_directory(root, mode=0o700)
    elif not os.path.lexists(root):
        return None
    else:
        _ensure_regular_directory(root, mode=0o700)
    try:
        return PinnedPathRoot(root)
    except ValidationError as exc:
        raise SetupError("Seld native ownership storage is unavailable or unsafe") from exc


def _ensure_regular_directory(path: Path, *, mode: int | None = None) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        metadata = os.lstat(path)
    except OSError as exc:
        raise SetupError(f"Seld could not prepare a local directory: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SetupError(f"Seld local directory is not a regular directory: {path}")
    if mode is not None and os.name != "nt":
        path.chmod(mode)


def _read_authority(store: PinnedPathRoot) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        encoded = store.read_regular_file(
            "state/ownership.json",
            label="Seld native host ownership receipt",
            max_bytes=MAX_AUTHORITY_BYTES,
            missing_ok=True,
        )
    except ValidationError as exc:
        raise SetupError("Seld native host ownership receipt is invalid") from exc
    if encoded is None:
        return None, None
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError("Seld native host ownership receipt is invalid") from exc
    if not isinstance(payload, dict) or not _valid_authority(payload):
        raise SetupError("Seld native host ownership receipt is invalid")
    return payload, encoded


def _valid_authority(payload: dict[str, Any]) -> bool:
    common = {
        "application",
        "format_version",
        "install_id",
        "ownership",
        "runtime_data_dir",
        "state",
    }
    if payload.get("state") == "installed":
        required = common | {
            "application_device",
            "application_inode",
            "bundle_receipt_revision",
            "vault",
            "vault_id",
        }
        return bool(
            set(payload) == required
            and payload.get("format_version") == AUTHORITY_FORMAT_VERSION
            and payload.get("ownership") == OWNERSHIP_MARKER
            and all(
                isinstance(payload.get(key), str)
                for key in (
                    "application",
                    "bundle_receipt_revision",
                    "install_id",
                    "runtime_data_dir",
                    "vault",
                    "vault_id",
                )
            )
            and type(payload.get("application_device")) is int
            and type(payload.get("application_inode")) is int
            and _valid_install_id(payload.get("install_id"))
            and _valid_sha256(payload.get("bundle_receipt_revision"))
        )
    if payload.get("state") in {"absent", "removing"}:
        required = common | {
            "previous_bundle_receipt_revision",
            "previous_vault",
            "previous_vault_id",
        }
        if payload.get("state") == "removing":
            required |= {"trash_device", "trash_inode", "trash_name"}
        return bool(
            set(payload) == required
            and payload.get("format_version") == AUTHORITY_FORMAT_VERSION
            and payload.get("ownership") == OWNERSHIP_MARKER
            and all(
                isinstance(payload.get(key), str)
                for key in (
                    "application",
                    "install_id",
                    "previous_bundle_receipt_revision",
                    "previous_vault",
                    "previous_vault_id",
                    "runtime_data_dir",
                )
            )
            and (
                payload.get("state") != "removing"
                or (
                    type(payload.get("trash_device")) is int
                    and type(payload.get("trash_inode")) is int
                    and isinstance(payload.get("trash_name"), str)
                    and str(payload["trash_name"]).startswith(f".{APP_BUNDLE_NAME}.uninstall-")
                    and "/" not in str(payload["trash_name"])
                    and "\\" not in str(payload["trash_name"])
                )
            )
            and _valid_install_id(payload.get("install_id"))
            and _valid_sha256(payload.get("previous_bundle_receipt_revision"))
        )
    return False


def _authority_payload(
    *,
    application: Path,
    application_identity: tuple[int, int],
    receipt: dict[str, Any],
    runtime_state: Path,
) -> dict[str, Any]:
    return {
        "application": str(application),
        "application_device": application_identity[0],
        "application_inode": application_identity[1],
        "bundle_receipt_revision": _receipt_revision(receipt),
        "format_version": AUTHORITY_FORMAT_VERSION,
        "install_id": receipt["install_id"],
        "ownership": OWNERSHIP_MARKER,
        "runtime_data_dir": str(runtime_state),
        "state": "installed",
        "vault": receipt["vault"],
        "vault_id": receipt["vault_id"],
    }


def _authority_tombstone(authority: dict[str, Any]) -> dict[str, Any]:
    if authority.get("state") == "installed":
        bundle_revision = authority["bundle_receipt_revision"]
        vault = authority["vault"]
        vault_id = authority["vault_id"]
    elif authority.get("state") == "removing":
        bundle_revision = authority["previous_bundle_receipt_revision"]
        vault = authority["previous_vault"]
        vault_id = authority["previous_vault_id"]
    else:
        raise SetupError("Seld native host ownership receipt is not removable")
    return {
        "application": authority["application"],
        "format_version": AUTHORITY_FORMAT_VERSION,
        "install_id": authority["install_id"],
        "ownership": OWNERSHIP_MARKER,
        "previous_bundle_receipt_revision": bundle_revision,
        "previous_vault": vault,
        "previous_vault_id": vault_id,
        "runtime_data_dir": authority["runtime_data_dir"],
        "state": "absent",
    }


def _authority_removing(
    authority: dict[str, Any],
    *,
    quarantine: Path,
    quarantine_identity: tuple[int, int],
) -> dict[str, Any]:
    if authority.get("state") != "installed":
        raise SetupError("Seld native host ownership receipt is not installed")
    return {
        "application": authority["application"],
        "format_version": AUTHORITY_FORMAT_VERSION,
        "install_id": authority["install_id"],
        "ownership": OWNERSHIP_MARKER,
        "previous_bundle_receipt_revision": authority["bundle_receipt_revision"],
        "previous_vault": authority["vault"],
        "previous_vault_id": authority["vault_id"],
        "runtime_data_dir": authority["runtime_data_dir"],
        "state": "removing",
        "trash_device": quarantine_identity[0],
        "trash_inode": quarantine_identity[1],
        "trash_name": quarantine.name,
    }


def _encode_authority(authority: dict[str, Any]) -> bytes:
    return (json.dumps(authority, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _authority_revision(authority: dict[str, Any]) -> str:
    return hashlib.sha256(_encode_authority(authority)).hexdigest()


def _write_authority(
    store: PinnedPathRoot,
    *,
    expected: bytes | None,
    replacement: dict[str, Any],
) -> None:
    encoded = _encode_authority(replacement)
    with store.bind_directory("state"):
        store.compare_and_swap_regular_file(
            "state/ownership.json",
            expected=expected,
            replacement=encoded,
            label="Seld native host ownership receipt",
            max_bytes=MAX_AUTHORITY_BYTES,
            mode=0o600,
        )


def _install_lifecycle_payload(
    *,
    application: Path,
    runtime_state: Path,
    prior_authority: dict[str, Any] | None,
    replacement_authority: dict[str, Any],
    prior_identity: tuple[int, int] | None,
    candidate_identity: tuple[int, int],
    backup_name: str | None,
) -> dict[str, Any]:
    return {
        "application": str(application),
        "backup_name": backup_name,
        "candidate_identity": list(candidate_identity),
        "format_version": _LIFECYCLE_FORMAT_VERSION,
        "kind": "install",
        "prior_authority": prior_authority,
        "prior_identity": list(prior_identity) if prior_identity is not None else None,
        "replacement_authority": replacement_authority,
        "runtime_data_dir": str(runtime_state),
        "state": "prepared",
    }


def _uninstall_lifecycle_payload(
    *,
    application: Path,
    runtime_state: Path,
    prior_authority: dict[str, Any],
    replacement_authority: dict[str, Any],
    prior_identity: tuple[int, int],
    quarantine_name: str,
) -> dict[str, Any]:
    return {
        "application": str(application),
        "format_version": _LIFECYCLE_FORMAT_VERSION,
        "kind": "uninstall",
        "prior_authority": prior_authority,
        "prior_identity": list(prior_identity),
        "quarantine_name": quarantine_name,
        "replacement_authority": replacement_authority,
        "runtime_data_dir": str(runtime_state),
        "state": "prepared",
    }


def _idle_lifecycle_payload() -> dict[str, Any]:
    return {"format_version": _LIFECYCLE_FORMAT_VERSION, "state": "idle"}


def _encode_lifecycle(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_native_lifecycle(store: PinnedPathRoot) -> _LifecycleState:
    try:
        encoded = store.read_regular_file(
            _LIFECYCLE_PATH,
            label="Seld native lifecycle receipt",
            max_bytes=MAX_LIFECYCLE_BYTES,
            missing_ok=True,
        )
    except ValidationError as exc:
        raise SetupError("Seld native lifecycle receipt is invalid") from exc
    if encoded is None:
        return _LifecycleState(None, None)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError("Seld native lifecycle receipt is invalid") from exc
    if not isinstance(payload, dict) or not _valid_lifecycle(payload):
        raise SetupError("Seld native lifecycle receipt is invalid")
    return _LifecycleState(payload, encoded)


def _valid_lifecycle(payload: dict[str, Any]) -> bool:
    if payload.get("state") == "idle":
        return payload == _idle_lifecycle_payload()
    common = {
        "application",
        "format_version",
        "kind",
        "prior_authority",
        "prior_identity",
        "replacement_authority",
        "runtime_data_dir",
        "state",
    }
    if (
        payload.get("state") != "prepared"
        or payload.get("format_version") != _LIFECYCLE_FORMAT_VERSION
        or not isinstance(payload.get("application"), str)
        or not isinstance(payload.get("runtime_data_dir"), str)
        or not _valid_authority_or_absent(payload.get("prior_authority"))
        or not isinstance(payload.get("replacement_authority"), dict)
        or not _valid_authority(payload["replacement_authority"])
    ):
        return False
    kind = payload.get("kind")
    prior_identity = _identity_from_json(payload.get("prior_identity"))
    if payload.get("prior_identity") is not None and prior_identity is None:
        return False
    application = payload["application"]
    runtime_data_dir = payload["runtime_data_dir"]
    prior_authority = payload.get("prior_authority")
    replacement = payload["replacement_authority"]
    if (
        replacement.get("application") != application
        or replacement.get("runtime_data_dir") != runtime_data_dir
        or (
            isinstance(prior_authority, dict)
            and (
                prior_authority.get("application") != application
                or prior_authority.get("runtime_data_dir") != runtime_data_dir
            )
        )
    ):
        return False
    if kind == "install":
        if set(payload) != common | {"backup_name", "candidate_identity"}:
            return False
        candidate_identity = _identity_from_json(payload.get("candidate_identity"))
        backup_name = payload.get("backup_name")
        return bool(
            candidate_identity is not None
            and replacement.get("state") == "installed"
            and (
                (
                    prior_identity is None
                    and backup_name is None
                    and (
                        payload.get("prior_authority") is None
                        or payload["prior_authority"].get("state") == "absent"
                    )
                )
                or (
                    prior_identity is not None
                    and isinstance(backup_name, str)
                    and _valid_backup_name(backup_name)
                    and isinstance(payload.get("prior_authority"), dict)
                    and payload["prior_authority"].get("state") == "installed"
                    and payload["prior_authority"].get("application_device") == prior_identity[0]
                    and payload["prior_authority"].get("application_inode") == prior_identity[1]
                    and payload["prior_authority"].get("install_id")
                    == replacement.get("install_id")
                )
            )
            and replacement.get("application_device") == candidate_identity[0]
            and replacement.get("application_inode") == candidate_identity[1]
        )
    if kind == "uninstall":
        if set(payload) != common | {"quarantine_name"}:
            return False
        prior = prior_authority
        quarantine_name = payload.get("quarantine_name")
        return bool(
            prior_identity is not None
            and isinstance(prior, dict)
            and prior.get("state") == "installed"
            and replacement.get("state") == "removing"
            and isinstance(quarantine_name, str)
            and _valid_quarantine_name(quarantine_name)
            and replacement.get("trash_device") == prior_identity[0]
            and replacement.get("trash_inode") == prior_identity[1]
            and replacement.get("trash_name") == quarantine_name
            and prior.get("application_device") == prior_identity[0]
            and prior.get("application_inode") == prior_identity[1]
            and prior.get("install_id") == replacement.get("install_id")
            and prior.get("bundle_receipt_revision")
            == replacement.get("previous_bundle_receipt_revision")
            and prior.get("vault") == replacement.get("previous_vault")
            and prior.get("vault_id") == replacement.get("previous_vault_id")
        )
    return False


def _valid_authority_or_absent(value: object) -> bool:
    return value is None or (isinstance(value, dict) and _valid_authority(value))


def _identity_from_json(value: object) -> tuple[int, int] | None:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)
    ):
        return None
    return int(value[0]), int(value[1])


def _valid_backup_name(value: str) -> bool:
    return bool(
        value.startswith(f".{APP_BUNDLE_NAME}.previous-")
        and len(value) == len(f".{APP_BUNDLE_NAME}.previous-") + 32
        and all(character in "0123456789abcdef" for character in value.rsplit("-", 1)[1])
        and "/" not in value
        and "\\" not in value
    )


def _valid_quarantine_name(value: str) -> bool:
    return bool(
        value.startswith(f".{APP_BUNDLE_NAME}.uninstall-")
        and len(value) == len(f".{APP_BUNDLE_NAME}.uninstall-") + 32
        and all(character in "0123456789abcdef" for character in value.rsplit("-", 1)[1])
        and "/" not in value
        and "\\" not in value
    )


def _write_native_lifecycle(
    store: PinnedPathRoot,
    *,
    expected: bytes | None,
    replacement: dict[str, Any],
) -> bytes:
    encoded = _encode_lifecycle(replacement)
    with store.bind_directory("state"):
        store.compare_and_swap_regular_file(
            _LIFECYCLE_PATH,
            expected=expected,
            replacement=encoded,
            label="Seld native lifecycle receipt",
            max_bytes=MAX_LIFECYCLE_BYTES,
            mode=0o600,
        )
    return encoded


def _begin_native_lifecycle(store: PinnedPathRoot, payload: dict[str, Any]) -> bytes:
    if not _valid_lifecycle(payload) or payload.get("state") != "prepared":
        raise SetupError("Seld native lifecycle operation is invalid")
    current = _read_native_lifecycle(store)
    if current.payload is not None and current.payload.get("state") != "idle":
        raise SetupError("Seld native lifecycle recovery is still pending")
    return _write_native_lifecycle(
        store,
        expected=current.encoded,
        replacement=payload,
    )


def _finish_native_lifecycle(store: PinnedPathRoot, *, expected: bytes) -> None:
    _write_native_lifecycle(
        store,
        expected=expected,
        replacement=_idle_lifecycle_payload(),
    )


def _require_idle_lifecycle(store: PinnedPathRoot) -> None:
    current = _read_native_lifecycle(store)
    if current.payload is None or current.payload.get("state") != "idle":
        raise SetupError("Seld native lifecycle recovery did not finish")


def _recover_native_lifecycle(
    store: PinnedPathRoot,
    *,
    application: Path,
    runtime_state: Path,
) -> str | None:
    lifecycle = _read_native_lifecycle(store)
    operation = lifecycle.payload
    if operation is None or operation.get("state") == "idle":
        return None
    assert lifecycle.encoded is not None
    if operation.get("application") != str(application) or operation.get("runtime_data_dir") != str(
        runtime_state
    ):
        raise SetupError("Seld native lifecycle receipt belongs to another installation")
    if operation["kind"] == "install":
        outcome = _recover_native_install(
            store,
            operation=operation,
            application=application,
            runtime_state=runtime_state,
        )
    else:
        outcome = _recover_native_uninstall(
            store,
            operation=operation,
            application=application,
            runtime_state=runtime_state,
        )
    _finish_native_lifecycle(store, expected=lifecycle.encoded)
    return outcome


def _recover_native_install(
    store: PinnedPathRoot,
    *,
    operation: dict[str, Any],
    application: Path,
    runtime_state: Path,
) -> str:
    prior = operation["prior_authority"]
    replacement = operation["replacement_authority"]
    _authority, authority_bytes = _read_authority(store)
    prior_bytes = _encode_authority(prior) if prior is not None else None
    replacement_bytes = _encode_authority(replacement)
    candidate_identity = _required_json_identity(operation["candidate_identity"])
    prior_identity = _identity_from_json(operation["prior_identity"])
    backup_name = operation["backup_name"]
    backup = application.parent / backup_name if backup_name is not None else None
    application_identity = _optional_directory_identity(application)
    backup_identity = _optional_directory_identity(backup) if backup is not None else None

    if authority_bytes == replacement_bytes:
        if application_identity != candidate_identity:
            raise SetupError("Seld native install recovery found a changed published app")
        receipt = _owned_receipt(application)
        _validate_bundle(application, receipt)
        if not _authority_matches(
            replacement,
            application=application,
            receipt=receipt,
            runtime_state=runtime_state,
        ):
            raise SetupError("Seld native install recovery found mismatched ownership")
        if backup is not None and backup_identity is not None:
            if backup_identity != prior_identity:
                raise SetupError("Seld native install backup changed before cleanup")
            _rmtree_exact(backup, expected_identity=backup_identity)
        return "committed"

    if authority_bytes != prior_bytes:
        raise SetupError("Seld native install authority changed during recovery")
    if prior_identity is None:
        if backup_identity is not None:
            raise SetupError("Seld native first-install recovery found an unexpected backup")
        if application_identity == candidate_identity:
            _validate_lifecycle_candidate(
                application,
                replacement=replacement,
                runtime_state=runtime_state,
            )
            _rollback_application(
                application,
                _Swap(candidate_identity, None, None),
            )
        elif application_identity is not None:
            raise SetupError("Seld native first-install recovery found a foreign app")
    else:
        assert backup is not None
        if application_identity == prior_identity and backup_identity is None:
            pass
        elif application_identity == candidate_identity and backup_identity == prior_identity:
            _validate_lifecycle_candidate(
                application,
                replacement=replacement,
                runtime_state=runtime_state,
            )
            _rollback_application(
                application,
                _Swap(candidate_identity, backup, prior_identity),
            )
        elif application_identity is None and backup_identity == prior_identity:
            _restore_quarantine(application, backup, expected_identity=prior_identity)
        else:
            raise SetupError("Seld native install recovery found an ambiguous app swap")
        assert isinstance(prior, dict)
        restored_receipt = _owned_receipt(application)
        _validate_bundle(application, restored_receipt)
        if not _authority_matches(
            prior,
            application=application,
            receipt=restored_receipt,
            runtime_state=runtime_state,
        ):
            raise SetupError("Seld native install rollback no longer matches its authority")
    return "rolled_back"


def _validate_lifecycle_candidate(
    application: Path,
    *,
    replacement: dict[str, Any],
    runtime_state: Path,
) -> None:
    receipt = _owned_receipt(application)
    _validate_bundle(application, receipt)
    if not _authority_matches(
        replacement,
        application=application,
        receipt=receipt,
        runtime_state=runtime_state,
    ):
        raise SetupError("Seld native lifecycle candidate does not match its ownership receipt")


def _recover_native_uninstall(
    store: PinnedPathRoot,
    *,
    operation: dict[str, Any],
    application: Path,
    runtime_state: Path,
) -> str:
    prior = operation["prior_authority"]
    replacement = operation["replacement_authority"]
    _authority, authority_bytes = _read_authority(store)
    prior_bytes = _encode_authority(prior)
    replacement_bytes = _encode_authority(replacement)
    prior_identity = _required_json_identity(operation["prior_identity"])
    quarantine = application.parent / str(operation["quarantine_name"])
    application_identity = _optional_directory_identity(application)
    quarantine_identity = _optional_directory_identity(quarantine)

    if authority_bytes == replacement_bytes:
        if application_identity is not None:
            raise SetupError("Seld native uninstall recovery found an app after quarantine")
        if quarantine_identity not in {None, prior_identity}:
            raise SetupError("Seld native uninstall quarantine changed during recovery")
        return "committed"
    if authority_bytes != prior_bytes:
        raise SetupError("Seld native uninstall authority changed during recovery")
    if application_identity == prior_identity and quarantine_identity is None:
        pass
    elif application_identity is None and quarantine_identity == prior_identity:
        _restore_quarantine(application, quarantine, expected_identity=prior_identity)
    else:
        raise SetupError("The quarantined Seld app changed before rollback")
    restored_receipt = _owned_receipt(application)
    _validate_bundle(application, restored_receipt)
    if not _authority_matches(
        prior,
        application=application,
        receipt=restored_receipt,
        runtime_state=runtime_state,
    ):
        raise SetupError("Seld native uninstall rollback no longer matches its authority")
    return "rolled_back"


def _required_json_identity(value: object) -> tuple[int, int]:
    identity = _identity_from_json(value)
    if identity is None:
        raise SetupError("Seld native lifecycle identity is invalid")
    return identity


def _optional_directory_identity(path: Path | None) -> tuple[int, int] | None:
    if path is None or not os.path.lexists(path):
        return None
    return _directory_identity(path, label="Seld native lifecycle path")


def _require_expected_authority(authority: dict[str, Any], expected_revision: str | None) -> None:
    actual = _authority_revision(authority)
    if expected_revision != actual:
        raise ConflictError(
            "Seld native ownership changed; read native-status and retry with its exact "
            "ownership_revision"
        )


def _authority_matches(
    authority: dict[str, Any],
    *,
    application: Path,
    receipt: dict[str, Any],
    runtime_state: Path,
) -> bool:
    try:
        identity = _directory_identity(application, label="installed Seld app")
    except SetupError:
        return False
    return bool(
        authority.get("state") == "installed"
        and authority.get("application") == str(application)
        and authority.get("application_device") == identity[0]
        and authority.get("application_inode") == identity[1]
        and authority.get("bundle_receipt_revision") == _receipt_revision(receipt)
        and authority.get("install_id") == receipt.get("install_id")
        and authority.get("runtime_data_dir") == str(runtime_state)
        and authority.get("vault") == receipt.get("vault")
        and authority.get("vault_id") == receipt.get("vault_id")
    )


def _status_without_authority(application: Path) -> dict[str, Any]:
    if not os.path.lexists(application):
        return {
            "application": str(application),
            "healthy": False,
            "installed": False,
            "owned": False,
            "ownership_revision": ABSENT_NATIVE_BRIDGE_REVISION,
        }
    return {
        "application": str(application),
        "error": f"Refusing a foreign app at the Seld app path: {application}",
        "error_code": "foreign_install",
        "healthy": False,
        "installed": True,
        "owned": False,
        "ownership_revision": ABSENT_NATIVE_BRIDGE_REVISION,
    }


def _native_bridge_status_locked(
    application: Path,
    *,
    authority_store: PinnedPathRoot,
    runtime_state: Path,
    executable: Path | None,
) -> dict[str, Any]:
    authority, _authority_bytes = _read_authority(authority_store)
    if not os.path.lexists(application):
        if authority is not None and authority["state"] == "installed":
            return {
                "application": str(application),
                "error": "Seld's host ownership receipt exists but its app is missing",
                "error_code": "owned_install_missing",
                "healthy": False,
                "installed": False,
                "owned": True,
                "ownership_revision": _authority_revision(authority),
            }
        if authority is not None and authority["state"] == "removing":
            return {
                "application": str(application),
                "error": "Seld native cleanup is pending",
                "error_code": "cleanup_pending",
                "healthy": False,
                "installed": False,
                "owned": True,
                "ownership_revision": _authority_revision(authority),
                "vault": authority["previous_vault"],
                "vault_id": authority["previous_vault_id"],
            }
        payload = _status_without_authority(application)
        if authority is not None:
            payload["ownership_revision"] = _authority_revision(authority)
        return payload
    try:
        receipt = _owned_receipt(application)
    except SetupError as exc:
        return {
            "application": str(application),
            "error": str(exc),
            "error_code": "foreign_install",
            "healthy": False,
            "installed": True,
            "owned": False,
            "ownership_revision": (
                _authority_revision(authority)
                if authority is not None
                else ABSENT_NATIVE_BRIDGE_REVISION
            ),
        }
    if authority is None or not _authority_matches(
        authority,
        application=application,
        receipt=receipt,
        runtime_state=runtime_state,
    ):
        return {
            "application": str(application),
            "error": f"Refusing a foreign app at the Seld app path: {application}",
            "error_code": "foreign_install",
            "healthy": False,
            "installed": True,
            "owned": False,
            "ownership_revision": (
                _authority_revision(authority)
                if authority is not None
                else ABSENT_NATIVE_BRIDGE_REVISION
            ),
        }
    try:
        _validate_bundle(application, receipt)
    except SetupError as exc:
        return {
            "application": str(application),
            "error": str(exc),
            "error_code": "tampered_install",
            "healthy": False,
            "installed": True,
            "owned": True,
            "ownership_revision": _authority_revision(authority),
            "receipt_revision": _receipt_revision(receipt),
        }
    current = _current_provenance(receipt, executable=executable)
    return {
        **_install_result(
            application,
            receipt,
            authority=authority,
            changed=False,
        ),
        "current": current,
        "healthy": True,
        "installed": True,
        "owned": True,
    }


def _valid_install_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == _INSTALL_ID_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _directory_identity(path: Path, *, label: str) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SetupError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SetupError(f"{label} is not a regular directory: {path}")
    return int(metadata.st_dev), int(metadata.st_ino)


def _architectures(values: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(values)
    if not selected or len(selected) != len(set(selected)):
        raise ValidationError("Seld native architectures must be unique and non-empty")
    unsupported = [value for value in selected if value not in SUPPORTED_ARCHITECTURES]
    if unsupported:
        raise ValidationError(f"unsupported Seld native architecture: {unsupported[0]}")
    return selected


def _build_universal_binary(
    source: Path,
    destination: Path,
    *,
    runner: Runner | None,
    architectures: Sequence[str],
) -> None:
    compiled: list[Path] = []
    for architecture in architectures:
        output = destination.with_name(f"{destination.name}-{architecture}")
        compile_command: Sequence[str] = (
            "/usr/bin/xcrun",
            "swiftc",
            str(source),
            "-o",
            str(output),
            "-O",
            "-target",
            f"{architecture}-apple-macos{MINIMUM_MACOS_VERSION}",
            "-framework",
            "AppKit",
            "-framework",
            "WebKit",
        )
        completed = _run(compile_command, runner=runner)
        if completed.returncode != 0 or not output.is_file():
            detail = _bounded_detail(completed.stderr)
            suffix = f": {detail}" if detail else ""
            raise SetupError(f"macOS could not compile Seld for {architecture}{suffix}")
        compiled.append(output)
    if len(compiled) == 1:
        compiled[0].replace(destination)
        return
    lipo_command: Sequence[str] = (
        "/usr/bin/lipo",
        "-create",
        *(str(path) for path in compiled),
        "-output",
        str(destination),
    )
    completed = _run(lipo_command, runner=runner)
    if completed.returncode != 0 or not destination.is_file():
        detail = _bounded_detail(completed.stderr)
        suffix = f": {detail}" if detail else ""
        raise SetupError(f"macOS could not assemble the universal Seld app{suffix}")
    for path in compiled:
        path.unlink(missing_ok=True)


def _run(command: Sequence[str], *, runner: Runner | None) -> Completed:
    if runner is not None:
        return runner(command, capture_output=True, text=True, check=False)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        deadline = time.monotonic() + SUBPROCESS_TIMEOUT_SECONDS
        while process.poll() is None:
            if time.monotonic() >= deadline:
                _terminate_process_group(process)
                raise SetupError("Seld native command timed out")
            if (
                os.fstat(stdout_file.fileno()).st_size > MAX_SUBPROCESS_OUTPUT_BYTES
                or os.fstat(stderr_file.fileno()).st_size > MAX_SUBPROCESS_OUTPUT_BYTES
            ):
                _terminate_process_group(process)
                raise SetupError("Seld native command produced too much output")
            time.sleep(0.02)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(MAX_SUBPROCESS_OUTPUT_BYTES + 1)
        stderr = stderr_file.read(MAX_SUBPROCESS_OUTPUT_BYTES + 1)
        if len(stdout) > MAX_SUBPROCESS_OUTPUT_BYTES or len(stderr) > MAX_SUBPROCESS_OUTPUT_BYTES:
            raise SetupError("Seld native command produced too much output")
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    except PermissionError as exc:
        # A short-lived command can exit between poll() and killpg(); macOS may then report
        # EPERM for the vanished/reused process-group identifier. Only accept that race after
        # the owned child is observably reaped. A still-running group remains a hard failure.
        try:
            process.wait(timeout=0.1)
            return
        except subprocess.TimeoutExpired:
            raise exc from None
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _write_service_launcher(
    destination: Path,
    *,
    executable: Path,
    executable_digest: str,
    runtime_data_dir: Path,
) -> None:
    quoted_executable = shlex.quote(str(executable))
    destination.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"expected={shlex.quote(executable_digest)}\n"
        f"actual=$(/usr/bin/shasum -a 256 -- {quoted_executable} | /usr/bin/awk '{{print $1}}')\n"
        'if [ "$actual" != "$expected" ]; then\n'
        '  echo "Seld changed since this app was installed. '
        'Reinstall the app before opening." >&2\n'
        "  exit 78\n"
        "fi\n"
        "service_directory=${0%/*}\n"
        'export PYTHONPATH="$service_directory/../Resources/python"\n'
        "export PYTHONNOUSERSITE=1\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        "export PYTHONSAFEPATH=1\n"
        f"export GSV_DATA_DIR={shlex.quote(str(runtime_data_dir))}\n"
        f'exec {quoted_executable} "$@"\n',
        encoding="utf-8",
    )
    destination.chmod(0o755)


def _info_plist(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": NATIVE_EXECUTABLE,
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": "1",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "LSMinimumSystemVersion": MINIMUM_MACOS_VERSION,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSSupportsAutomaticGraphicsSwitching": True,
        "SeldBridgeStatePath": receipt["bridge_state"],
        "SeldRuntimeDigest": receipt["source_runtime_digest"],
        "SeldServiceExecutable": SERVICE_EXECUTABLE,
        "SeldVaultID": receipt["vault_id"],
        "SeldVaultRoot": receipt["vault"],
        "SeldVaultRootDevice": receipt["vault_root_device"],
        "SeldVaultRootInode": receipt["vault_root_inode"],
    }


def _source_runtime_manifest(root: Path) -> list[dict[str, Any]]:
    entries = _descriptor_tree_manifest(
        root,
        label="Seld runtime",
        max_total_bytes=MAX_RUNTIME_BYTES,
        exclude_runtime_noise=True,
    )
    required = {
        "__init__.py",
        "bridge.py",
        "cli.py",
        "resources/bridge/index.html",
        "resources/native/SeldBridge.swift",
    }
    present = {str(entry["path"]) for entry in entries}
    if missing := sorted(required - present):
        raise SetupError(f"Seld runtime is missing required file: {missing[0]}")
    return entries


def _copy_runtime(root: Path, destination: Path, entries: list[dict[str, Any]]) -> None:
    source_store = PinnedPathRoot(root)
    destination_store = PinnedPathRoot(destination)
    try:
        for entry in entries:
            relative = PurePosixPath(str(entry["path"]))
            parent = Path(*relative.parts).parent
            if parent.parts:
                destination_store.ensure_directory(parent, mode=0o700)
            encoded = source_store.read_regular_file(
                Path(*relative.parts),
                label=f"Seld runtime file {relative}",
                max_bytes=MAX_BUNDLE_FILE_BYTES,
            )
            assert encoded is not None
            if (
                len(encoded) != entry["size"]
                or hashlib.sha256(encoded).hexdigest() != entry["sha256"]
            ):
                raise SetupError(f"Seld runtime changed while it was bundled: {relative}")
            destination_store.atomic_write(
                Path(*relative.parts),
                encoded,
                mode=int(entry["mode"]),
            )
            copied = destination_store.read_regular_file(
                Path(*relative.parts),
                label=f"bundled Seld runtime file {relative}",
                max_bytes=MAX_BUNDLE_FILE_BYTES,
            )
            if copied != encoded:
                raise SetupError(f"Seld runtime copy changed unexpectedly: {relative}")
    except ValidationError as exc:
        raise SetupError("Seld runtime changed while it was bundled") from exc
    finally:
        destination_store.close()
        source_store.close()


def _bundle_manifest(application: Path) -> list[dict[str, Any]]:
    return _descriptor_tree_manifest(
        application,
        label="Seld app",
        max_total_bytes=MAX_RUNTIME_BYTES + MAX_BUNDLE_FILE_BYTES,
        exclude_runtime_noise=False,
    )


def _descriptor_tree_manifest(
    root: Path,
    *,
    label: str,
    max_total_bytes: int,
    exclude_runtime_noise: bool,
) -> list[dict[str, Any]]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        listed_root = os.lstat(root)
        root_descriptor = os.open(root, flags)
    except OSError as exc:
        raise SetupError(f"{label} is unavailable: {root}") from exc
    entries: list[dict[str, Any]] = []
    total = 0
    traversed = 0

    def walk(directory: int, prefix: tuple[str, ...]) -> None:
        nonlocal total, traversed
        if len(prefix) > MAX_MANIFEST_DEPTH:
            raise SetupError(f"{label} exceeds the safe directory depth")
        try:
            names: list[str] = []
            with os.scandir(directory) as children:
                for child in children:
                    traversed += 1
                    if traversed > MAX_MANIFEST_ENTRIES:
                        raise SetupError(f"{label} contains too many entries to inspect safely")
                    names.append(child.name)
        except OSError as exc:
            raise SetupError(f"{label} could not be enumerated safely") from exc
        for name in sorted(names):
            relative_parts = (*prefix, name)
            relative = PurePosixPath(*relative_parts)
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except OSError as exc:
                raise SetupError(f"{label} changed while it was enumerated: {relative}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise SetupError(f"{label} contains an unsupported symlink: {relative}")
            excluded = exclude_runtime_noise and (
                "__pycache__" in relative_parts or name == ".DS_Store" or name.endswith(".pyc")
            )
            if stat.S_ISDIR(metadata.st_mode):
                if excluded:
                    continue
                try:
                    descriptor = os.open(name, flags, dir_fd=directory)
                    opened = os.fstat(descriptor)
                except OSError as exc:
                    raise SetupError(
                        f"{label} changed while a directory was opened: {relative}"
                    ) from exc
                if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    os.close(descriptor)
                    raise SetupError(f"{label} directory changed while opened: {relative}")
                try:
                    walk(descriptor, relative_parts)
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                        raise SetupError(f"{label} directory changed while read: {relative}")
                finally:
                    os.close(descriptor)
                continue
            if excluded:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SetupError(f"{label} contains an unsupported file: {relative}")
            if len(entries) >= MAX_MANIFEST_FILES:
                raise SetupError(f"{label} contains too many files to bundle safely")
            encoded, opened = _read_regular_at(
                directory,
                name,
                expected=metadata,
                label=f"{label} file {relative}",
                max_bytes=MAX_BUNDLE_FILE_BYTES,
            )
            total += len(encoded)
            if total > max_total_bytes:
                raise SetupError(f"{label} is too large to bundle safely")
            entries.append(
                {
                    "mode": stat.S_IMODE(opened.st_mode),
                    "path": relative.as_posix(),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "size": len(encoded),
                }
            )

    try:
        opened_root = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(listed_root.st_mode)
            or stat.S_ISLNK(listed_root.st_mode)
            or not stat.S_ISDIR(opened_root.st_mode)
            or (listed_root.st_dev, listed_root.st_ino) != (opened_root.st_dev, opened_root.st_ino)
        ):
            raise SetupError(f"{label} is not a stable directory: {root}")
        walk(root_descriptor, ())
        current_root = os.lstat(root)
        if (current_root.st_dev, current_root.st_ino) != (
            opened_root.st_dev,
            opened_root.st_ino,
        ):
            raise SetupError(f"{label} changed identity while it was read")
        return entries
    finally:
        os.close(root_descriptor)


def _read_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: os.stat_result,
    label: str,
    max_bytes: int,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            or opened.st_size > max_bytes
        ):
            raise SetupError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        finished = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            len(encoded) > max_bytes
            or (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise SetupError(f"{label} changed while it was read")
        return encoded, opened
    except OSError as exc:
        raise SetupError(f"{label} could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _tree_digest(entries: list[dict[str, Any]]) -> str:
    encoded = json.dumps(entries, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _encode_receipt(receipt: dict[str, Any]) -> bytes:
    return (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _receipt_revision(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(_encode_receipt(receipt)).hexdigest()


def _owned_receipt(application: Path) -> dict[str, Any]:
    try:
        metadata = os.lstat(application)
    except FileNotFoundError as exc:
        raise SetupError("The Seld app is not installed") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SetupError(f"Refusing a foreign item at the Seld app path: {application}")
    receipt_path = application / "Contents/Resources" / RECEIPT_NAME
    try:
        encoded = _read_bundle_file(
            application,
            receipt_path.relative_to(application),
            label="Seld native ownership receipt",
            max_bytes=MAX_RECEIPT_BYTES,
        )
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, OSError, SetupError, ValidationError) as exc:
        raise SetupError(f"Refusing a foreign app at the Seld app path: {application}") from exc
    if not isinstance(payload, dict) or not _valid_receipt_identity(payload, application):
        raise SetupError(f"Refusing a foreign app at the Seld app path: {application}")
    return payload


def _valid_receipt_identity(payload: dict[str, Any], application: Path) -> bool:
    strings = {
        "application",
        "bridge_state",
        "bundle_identifier",
        "executable",
        "executable_sha256",
        "install_id",
        "ownership",
        "runtime_data_dir",
        "source_runtime_digest",
        "vault",
        "vault_id",
        "version",
    }
    if any(not isinstance(payload.get(key), str) for key in strings):
        return False
    integers = {"vault_root_device", "vault_root_inode"}
    if any(type(payload.get(key)) is not int for key in integers):
        return False
    architectures = payload.get("architectures")
    manifest = payload.get("bundle_manifest")
    return bool(
        payload.get("format_version") == RECEIPT_FORMAT_VERSION
        and payload.get("ownership") == OWNERSHIP_MARKER
        and payload.get("bundle_identifier") == BUNDLE_IDENTIFIER
        and payload.get("application") == str(application)
        and _valid_install_id(payload.get("install_id"))
        and _valid_sha256(payload.get("executable_sha256"))
        and _valid_sha256(payload.get("source_runtime_digest"))
        and isinstance(architectures, list)
        and bool(architectures)
        and all(item in SUPPORTED_ARCHITECTURES for item in architectures)
        and len(architectures) == len(set(architectures))
        and isinstance(manifest, list)
        and bool(manifest)
    )


def _validate_bundle(application: Path, receipt: dict[str, Any]) -> None:
    manifest = receipt.get("bundle_manifest")
    if not isinstance(manifest, list):
        raise SetupError("The Seld app ownership receipt is incomplete")
    expected: dict[str, dict[str, Any]] = {}
    for raw_entry in manifest:
        if not isinstance(raw_entry, dict) or not _valid_manifest_entry(raw_entry):
            raise SetupError("The Seld app ownership receipt has an invalid file manifest")
        relative = str(raw_entry["path"])
        if relative in expected:
            raise SetupError("The Seld app ownership receipt repeats a file")
        expected[relative] = raw_entry
    actual_entries = _bundle_manifest(application)
    actual = {str(entry["path"]): entry for entry in actual_entries}
    receipt_relative = f"Contents/Resources/{RECEIPT_NAME}"
    if set(actual) != set(expected) | {receipt_relative}:
        raise SetupError("The Seld app contains files outside its ownership receipt")
    for relative, expected_entry in expected.items():
        if actual.get(relative) != expected_entry:
            raise SetupError(f"The Seld app failed integrity checks: {relative}")
    info_path = application / "Contents/Info.plist"
    try:
        info = plistlib.loads(
            _read_bundle_file(
                application,
                info_path.relative_to(application),
                label="Seld app metadata",
                max_bytes=MAX_RECEIPT_BYTES,
            )
        )
    except (OSError, plistlib.InvalidFileException, ValidationError) as exc:
        raise SetupError("The Seld app metadata is invalid") from exc
    if info != _info_plist(receipt):
        raise SetupError("The Seld app metadata does not match its ownership receipt")


def _valid_manifest_entry(entry: dict[str, Any]) -> bool:
    if set(entry) != {"mode", "path", "sha256", "size"}:
        return False
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return False
    relative = PurePosixPath(path)
    return bool(
        not relative.is_absolute()
        and ".." not in relative.parts
        and "\\" not in path
        and type(entry.get("mode")) is int
        and 0 <= int(entry["mode"]) <= 0o7777
        and type(entry.get("size")) is int
        and 0 <= int(entry["size"]) <= MAX_BUNDLE_FILE_BYTES
        and _valid_sha256(entry.get("sha256"))
    )


def _receipt_matches(receipt: dict[str, Any], desired: dict[str, Any]) -> bool:
    return all(receipt.get(key) == value for key, value in desired.items())


def _current_provenance(receipt: dict[str, Any], *, executable: Path | None) -> bool:
    candidate = Path(str(receipt["executable"])) if executable is None else executable
    try:
        resolved = _regular_executable(candidate)
        runtime_manifest = _source_runtime_manifest(Path(__file__).resolve().parent)
        return bool(
            str(resolved) == receipt["executable"]
            and _sha256_file(resolved) == receipt["executable_sha256"]
            and _tree_digest(runtime_manifest) == receipt["source_runtime_digest"]
        )
    except (OSError, SetupError):
        return False


def _swap_application(
    source: Path,
    destination: Path,
    *,
    expected_destination_identity: tuple[int, int] | None,
    expected_source_identity: tuple[int, int],
    backup_name: str | None,
) -> _Swap:
    source_parent, source_parent_identity = _open_pinned_directory(source.parent)
    destination_parent, destination_parent_identity = _open_pinned_directory(destination.parent)
    backup_identity: tuple[int, int] | None = None
    try:
        source_identity = _named_directory_identity(source_parent, source.name)
        if source_identity != expected_source_identity:
            raise SetupError("The staged Seld app changed before installation")
        current_destination = _optional_named_directory_identity(
            destination_parent,
            destination.name,
        )
        if current_destination != expected_destination_identity:
            raise ConflictError("The installed Seld app changed before replacement")
        _validate_pinned_directory(
            source.parent,
            source_parent,
            source_parent_identity,
        )
        _validate_pinned_directory(
            destination.parent,
            destination_parent,
            destination_parent_identity,
        )
        if current_destination is not None:
            if backup_name is None or not _valid_backup_name(backup_name):
                raise SetupError("Seld install backup name is invalid")
            os.rename(
                destination.name,
                backup_name,
                src_dir_fd=destination_parent,
                dst_dir_fd=destination_parent,
            )
            backup_identity = _named_directory_identity(destination_parent, backup_name)
            if backup_identity != current_destination:
                raise SetupError("Seld could not preserve the prior app before replacement")
        elif backup_name is not None:
            raise SetupError("Seld install backup was requested without a prior app")
        try:
            if _named_directory_identity(source_parent, source.name) != expected_source_identity:
                raise SetupError("The staged Seld app changed before publication")
            if _optional_named_directory_identity(destination_parent, destination.name) is not None:
                raise ConflictError("The installed Seld app path changed before publication")
            _validate_pinned_directory(
                source.parent,
                source_parent,
                source_parent_identity,
            )
            _validate_pinned_directory(
                destination.parent,
                destination_parent,
                destination_parent_identity,
            )
            os.rename(
                source.name,
                destination.name,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
        except Exception:
            if backup_name is not None and (
                _optional_named_directory_identity(destination_parent, destination.name) is None
            ):
                os.rename(
                    backup_name,
                    destination.name,
                    src_dir_fd=destination_parent,
                    dst_dir_fd=destination_parent,
                )
            raise
        installed_identity = _named_directory_identity(destination_parent, destination.name)
        if installed_identity != expected_source_identity:
            raise SetupError("The installed Seld app changed during publication")
        _validate_pinned_directory(
            destination.parent,
            destination_parent,
            destination_parent_identity,
        )
        return _Swap(
            installed_identity=installed_identity,
            backup_path=(destination.parent / backup_name if backup_name is not None else None),
            backup_identity=backup_identity,
        )
    finally:
        os.close(destination_parent)
        os.close(source_parent)


def _rollback_application(application: Path, swap: _Swap) -> None:
    parent, parent_identity = _open_pinned_directory(application.parent)
    failed_name = f".{APP_BUNDLE_NAME}.rollback-{secrets.token_hex(16)}"
    try:
        if _named_directory_identity(parent, application.name) != swap.installed_identity:
            raise SetupError("Seld cannot roll back an app that changed after publication")
        os.rename(
            application.name,
            failed_name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        if swap.backup_path is not None:
            if _named_directory_identity(parent, swap.backup_path.name) != swap.backup_identity:
                raise SetupError("Seld's preserved app changed before rollback")
            os.rename(
                swap.backup_path.name,
                application.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
        _validate_pinned_directory(application.parent, parent, parent_identity)
    finally:
        os.close(parent)
    failed = application.parent / failed_name
    if os.path.lexists(failed):
        _rmtree_exact(failed, expected_identity=swap.installed_identity)


def _discard_backup(swap: _Swap) -> None:
    if swap.backup_path is None:
        return
    assert swap.backup_identity is not None
    _rmtree_exact(swap.backup_path, expected_identity=swap.backup_identity)


def _quarantine_application(
    application: Path,
    *,
    expected_identity: tuple[int, int],
    quarantine_name: str,
) -> Path:
    parent, parent_identity = _open_pinned_directory(application.parent)
    if not _valid_quarantine_name(quarantine_name):
        raise SetupError("Seld uninstall quarantine name is invalid")
    try:
        if _named_directory_identity(parent, application.name) != expected_identity:
            raise ConflictError("The installed Seld app changed before uninstall")
        _validate_pinned_directory(application.parent, parent, parent_identity)
        os.rename(
            application.name,
            quarantine_name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        if _named_directory_identity(parent, quarantine_name) != expected_identity:
            raise SetupError("The Seld app changed while it was quarantined")
        _validate_pinned_directory(application.parent, parent, parent_identity)
    finally:
        os.close(parent)
    return application.parent / quarantine_name


def _restore_quarantine(
    application: Path,
    quarantine: Path,
    *,
    expected_identity: tuple[int, int],
) -> None:
    parent, parent_identity = _open_pinned_directory(application.parent)
    try:
        if _optional_named_directory_identity(parent, application.name) is not None:
            raise SetupError("Seld cannot restore over another app")
        if _named_directory_identity(parent, quarantine.name) != expected_identity:
            raise SetupError("The quarantined Seld app changed before rollback")
        os.rename(
            quarantine.name,
            application.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        _validate_pinned_directory(application.parent, parent, parent_identity)
    finally:
        os.close(parent)


def _rmtree_exact(path: Path, *, expected_identity: tuple[int, int]) -> None:
    parent, parent_identity = _open_pinned_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        if _named_directory_identity(parent, path.name) != expected_identity:
            raise SetupError("Seld app cleanup target changed identity")
        descriptor = os.open(path.name, flags, dir_fd=parent)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (int(opened.st_dev), int(opened.st_ino)) != expected_identity
        ):
            raise SetupError("Seld app cleanup target changed while it was opened")
        _remove_directory_contents_fd(descriptor, depth=0, counter=[0])
        if _named_directory_identity(parent, path.name) != expected_identity:
            raise SetupError("Seld app cleanup target moved before final removal")
        _validate_pinned_directory(path.parent, parent, parent_identity)
        os.rmdir(path.name, dir_fd=parent)
        _validate_pinned_directory(path.parent, parent, parent_identity)
    except OSError as exc:
        raise SetupError("Seld could not remove its exact app cleanup target") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _remove_directory_contents_fd(
    directory: int,
    *,
    depth: int,
    counter: list[int],
) -> None:
    if depth > MAX_MANIFEST_DEPTH:
        raise SetupError("Seld app cleanup tree exceeds the safe directory depth")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        names: list[str] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                counter[0] += 1
                if counter[0] > MAX_MANIFEST_ENTRIES:
                    raise SetupError("Seld app cleanup tree contains too many entries")
                names.append(entry.name)
    except OSError as exc:
        raise SetupError("Seld app cleanup tree could not be enumerated safely") from exc
    for name in sorted(names):
        try:
            listed = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError as exc:
            raise SetupError("Seld app cleanup entry changed before removal") from exc
        if stat.S_ISLNK(listed.st_mode):
            raise SetupError("Seld app cleanup tree contains an unexpected symlink")
        if stat.S_ISDIR(listed.st_mode):
            child = -1
            try:
                child = os.open(name, flags, dir_fd=directory)
                opened = os.fstat(child)
                identity = (int(opened.st_dev), int(opened.st_ino))
                if not stat.S_ISDIR(opened.st_mode) or identity != (
                    int(listed.st_dev),
                    int(listed.st_ino),
                ):
                    raise SetupError("Seld app cleanup directory changed while opened")
                _remove_directory_contents_fd(
                    child,
                    depth=depth + 1,
                    counter=counter,
                )
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (int(current.st_dev), int(current.st_ino)) != identity:
                    raise SetupError("Seld app cleanup directory changed before removal")
                os.rmdir(name, dir_fd=directory)
            except OSError as exc:
                raise SetupError("Seld app cleanup directory could not be removed safely") from exc
            finally:
                if child >= 0:
                    os.close(child)
            continue
        if not stat.S_ISREG(listed.st_mode):
            raise SetupError("Seld app cleanup tree contains an unsupported file")
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory,
            )
            opened = os.fstat(descriptor)
            identity = (int(opened.st_dev), int(opened.st_ino))
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or identity != (int(listed.st_dev), int(listed.st_ino))
                or identity != (int(current.st_dev), int(current.st_ino))
            ):
                raise SetupError("Seld app cleanup file changed before removal")
            os.unlink(name, dir_fd=directory)
        except OSError as exc:
            raise SetupError("Seld app cleanup file could not be removed safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _open_pinned_directory(path: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        listed = os.lstat(path)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise SetupError(f"Seld could not pin a local directory: {path}") from exc
    identity = (int(opened.st_dev), int(opened.st_ino))
    if (
        not stat.S_ISDIR(listed.st_mode)
        or stat.S_ISLNK(listed.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (int(listed.st_dev), int(listed.st_ino)) != identity
    ):
        os.close(descriptor)
        raise SetupError(f"Seld local directory changed while it was opened: {path}")
    return descriptor, identity


def _validate_pinned_directory(
    path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
    except OSError as exc:
        raise SetupError(f"Seld local directory changed during publication: {path}") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or (int(opened.st_dev), int(opened.st_ino)) != expected_identity
        or (int(current.st_dev), int(current.st_ino)) != expected_identity
    ):
        raise SetupError(f"Seld local directory changed during publication: {path}")


def _named_directory_identity(parent: int, name: str) -> tuple[int, int]:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise SetupError(f"Seld app entry is unavailable: {name}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SetupError(f"Seld app entry is not a regular directory: {name}")
    return int(metadata.st_dev), int(metadata.st_ino)


def _optional_named_directory_identity(parent: int, name: str) -> tuple[int, int] | None:
    try:
        return _named_directory_identity(parent, name)
    except SetupError as exc:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        raise exc


def _regular_executable(value: Path) -> Path:
    path = value.expanduser().resolve()
    _regular_file(path, label="Seld executable", max_bytes=MAX_BUNDLE_FILE_BYTES)
    if not os.access(path, os.X_OK):
        raise SetupError(f"Seld executable is not executable: {path}")
    return path


def _regular_file(path: Path, *, label: str, max_bytes: int) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SetupError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SetupError(f"{label} is not a regular file: {path}")
    if metadata.st_size > max_bytes:
        raise SetupError(f"{label} is too large: {path}")
    return metadata


def _read_regular_bytes(path: Path, *, label: str, max_bytes: int) -> bytes:
    try:
        with open_regular_file(path, label=label, max_bytes=max_bytes) as (descriptor, _snapshot):
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
    except ValidationError:
        raise
    payload = b"".join(chunks)
    if len(payload) > max_bytes:
        raise ValidationError(f"{label} exceeds its size bound")
    return payload


def _read_bundle_file(root: Path, relative: Path, *, label: str, max_bytes: int) -> bytes:
    store = PinnedPathRoot(root)
    try:
        encoded = store.read_regular_file(relative, label=label, max_bytes=max_bytes)
        assert encoded is not None
        return encoded
    except ValidationError as exc:
        raise SetupError(f"{label} could not be read safely") from exc
    finally:
        store.close()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(
        _read_regular_bytes(
            path,
            label="Seld executable or runtime file",
            max_bytes=MAX_BUNDLE_FILE_BYTES,
        )
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _bounded_detail(value: str) -> str:
    return " ".join(value.split())[:500]
