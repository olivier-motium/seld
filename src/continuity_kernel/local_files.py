"""Host-local grants and bounded, privacy-screened file reads.

Grant roots live outside the portable vault in an owner-only host record. File
content is returned only to the current CLI or MCP caller and is never written
to the Seld vault or the grant record.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from continuity_kernel.atomic import atomic_write, exclusive_lock, read_regular_file, sha256_bytes
from continuity_kernel.config import data_dir
from continuity_kernel.errors import NotFoundError, ValidationError
from continuity_kernel.privacy import (
    AwarenessDecision,
    assess_local_path,
    read_screened_local_content,
)
from continuity_kernel.records import format_time, stored_time
from continuity_kernel.source_recipes import get_recipe
from continuity_kernel.vault_identity import canonical_vault_id

LOCAL_FILE_SOURCE_ID: Final = "local_files"
LOCAL_FILE_READER_TOOL: Final = "gsv_local_file_read"
LOCAL_FILE_READ_CAPABILITY: Final = get_recipe(LOCAL_FILE_SOURCE_ID).read_capability
LOCAL_FILE_GRANT_FORMAT_VERSION: Final = 2
_LEGACY_LOCAL_FILE_GRANT_FORMAT_VERSION: Final = 1
MAX_LOCAL_PATH_BYTES: Final = 16 * 1024
MAX_LOCAL_GRANTS: Final = 128
MAX_LOCAL_GRANT_STORE_BYTES: Final = 512 * 1024
_GRANT_KEYS: Final = frozenset(
    {
        "created_at",
        "device",
        "grant_id",
        "inode",
        "root",
        "vault_device",
        "vault_id",
        "vault_inode",
        "vault_root",
    }
)
_LEGACY_GRANT_KEYS: Final = _GRANT_KEYS - {"vault_device", "vault_inode"}


@dataclass(frozen=True)
class LocalFileGrant:
    grant_id: str
    vault_id: str
    vault_root: str
    vault_device: int
    vault_inode: int
    root: str
    device: int
    inode: int
    created_at: str

    def to_dict(self, *, current: bool | None = None) -> dict[str, Any]:
        """Return the minimal user-facing grant shape, never storage identity fields."""

        value: dict[str, Any] = {
            "created_at": self.created_at,
            "grant_id": self.grant_id,
            "selected_root": self.root,
        }
        if current is not None:
            value["current"] = current
        return value


class LocalFileGrantStore:
    """Owner-only host grants bound to one exact logical vault and path."""

    def __init__(self, *, vault_root: Path | str, vault_id: str):
        root = Path(vault_root).expanduser()
        if not root.is_absolute():
            raise ValidationError("local-file grants require an absolute vault root")
        canonical_root, vault_identity = _real_directory(root, "vault root")
        self.vault_root = str(canonical_root)
        self.vault_device, self.vault_inode = vault_identity
        self.vault_id = canonical_vault_id(vault_id)
        self.storage_root = data_dir() / "local-file-authority"
        self.path = self.storage_root / "local-file-grants.json"
        self.lock_path = self.storage_root / "locks/local-file-grants.lock"

    def create(self, selected_root: Path | str) -> dict[str, Any]:
        root, identity = _grant_root(selected_root)
        with self._locked():
            grants = list(self._load())
            for grant in grants:
                if (
                    self._belongs(grant)
                    and grant.root == str(root)
                    and (grant.device, grant.inode) == identity
                ):
                    return {"created": False, "grant": grant.to_dict(current=True)}
            grants = [
                grant for grant in grants if not (self._belongs(grant) and grant.root == str(root))
            ]
            if len(grants) >= MAX_LOCAL_GRANTS:
                raise ValidationError("local-file grant limit reached; revoke an unused root")
            grant = LocalFileGrant(
                grant_id=str(uuid.uuid4()),
                vault_id=self.vault_id,
                vault_root=self.vault_root,
                vault_device=self.vault_device,
                vault_inode=self.vault_inode,
                root=str(root),
                device=identity[0],
                inode=identity[1],
                created_at=format_time(datetime.now(UTC)),
            )
            grants.append(grant)
            self._save(tuple(grants))
            return {"created": True, "grant": grant.to_dict(current=True)}

    def list(self) -> dict[str, Any]:
        with self._locked():
            grants = tuple(grant for grant in self._load() if self._belongs(grant))
            return {
                "grants": [
                    grant.to_dict(current=_root_matches(grant))
                    for grant in sorted(grants, key=lambda item: (item.root, item.grant_id))
                ],
            }

    def source_binding(self, *, require_current_grant: bool = False) -> str:
        """Return a privacy-safe binding to this vault's exact current grant set."""

        with self._locked():
            grants = tuple(
                sorted(
                    (grant for grant in self._load() if self._belongs(grant)),
                    key=lambda item: item.grant_id,
                )
            )
            entries = [
                {
                    "current": _root_matches(grant),
                    "device": grant.device,
                    "grant_id": grant.grant_id,
                    "inode": grant.inode,
                    "root": grant.root,
                }
                for grant in grants
            ]
            if require_current_grant and not any(entry["current"] for entry in entries):
                raise ValidationError(
                    "successful local_files coverage requires one current host-local grant"
                )
            encoded = json.dumps(
                {
                    "grants": entries,
                    "vault_id": self.vault_id,
                    "vault_device": self.vault_device,
                    "vault_inode": self.vault_inode,
                    "vault_root": self.vault_root,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return f"{LOCAL_FILE_READER_TOOL}:grant-set:{sha256_bytes(encoded)}"

    def revoke(self, grant_id: str) -> dict[str, Any]:
        clean_id = _grant_id(grant_id)
        with self._locked():
            grants = list(self._load())
            matched = next(
                (grant for grant in grants if grant.grant_id == clean_id and self._belongs(grant)),
                None,
            )
            if matched is None:
                raise NotFoundError("local-file grant not found for this vault")
            self._save(tuple(grant for grant in grants if grant.grant_id != clean_id))
            return {"grant": matched.to_dict(current=_root_matches(matched)), "revoked": True}

    def revoke_all(self) -> dict[str, Any]:
        """Remove every host grant for this exact vault identity and path."""

        with self._locked():
            grants = list(self._load())
            retained = tuple(grant for grant in grants if not self._belongs(grant))
            revoked = len(grants) - len(retained)
            if revoked:
                self._save(retained)
            return {"revoked": revoked}

    def read(self, *, grant_id: str, relative_path: str) -> dict[str, Any]:
        """Read while holding the grant lock so completed revocation is final."""

        clean_id = _grant_id(grant_id)
        relative = _relative_path(relative_path)
        with self._locked():
            grant = next(
                (
                    item
                    for item in self._load()
                    if item.grant_id == clean_id and self._belongs(item)
                ),
                None,
            )
            if grant is None:
                raise NotFoundError("local-file grant not found for this vault")
            if not _root_matches(grant):
                raise ValidationError(
                    "local-file grant root changed; revoke it and grant the root again"
                )
            return _read_granted_file(grant=grant, relative=relative)

    def _belongs(self, grant: LocalFileGrant) -> bool:
        return (
            grant.vault_id == self.vault_id
            and grant.vault_root == self.vault_root
            and (grant.vault_device, grant.vault_inode) == (self.vault_device, self.vault_inode)
            and _directory_matches(
                self.vault_root,
                expected=(self.vault_device, self.vault_inode),
            )
        )

    def _locked(self) -> Any:
        _prepare_private_storage(self.storage_root)
        return exclusive_lock(self.lock_path)

    def _load(self) -> tuple[LocalFileGrant, ...]:
        if not os.path.lexists(self.path):
            return ()
        _validate_private_file(self.path)
        encoded = read_regular_file(
            self.path,
            label="Seld local-file grants",
            max_bytes=MAX_LOCAL_GRANT_STORE_BYTES,
        )
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Seld local-file grant store is invalid") from exc
        format_version = payload.get("format_version") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"format_version", "grants"}
            or format_version
            not in {
                _LEGACY_LOCAL_FILE_GRANT_FORMAT_VERSION,
                LOCAL_FILE_GRANT_FORMAT_VERSION,
            }
            or not isinstance(payload.get("grants"), list)
            or len(payload["grants"]) > MAX_LOCAL_GRANTS
        ):
            raise ValidationError("Seld local-file grant store has an unsupported shape")
        if format_version == _LEGACY_LOCAL_FILE_GRANT_FORMAT_VERSION:
            # Version 1 did not bind authority to the vault directory object. Validate
            # its shape, then expose no authority; the next explicit grant rewrites
            # the host-local store in version 2.
            for value in payload["grants"]:
                _validate_legacy_grant(value)
            return ()
        grants = tuple(_grant_from_value(value) for value in payload["grants"])
        if len({grant.grant_id for grant in grants}) != len(grants):
            raise ValidationError("Seld local-file grant store contains duplicate IDs")
        return grants

    def _save(self, grants: tuple[LocalFileGrant, ...]) -> None:
        payload = {
            "format_version": LOCAL_FILE_GRANT_FORMAT_VERSION,
            "grants": [asdict(grant) for grant in sorted(grants, key=lambda item: item.grant_id)],
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_LOCAL_GRANT_STORE_BYTES:
            raise ValidationError("Seld local-file grant store exceeds its size bound")
        atomic_write(self.path, encoded, mode=0o600)
        _validate_private_file(self.path)


def validate_local_file_tool_binding(
    *,
    source_id: str,
    result: str,
    tool_binding: str | None,
) -> None:
    """Prevent local-file coverage from claiming an ambient filesystem tool."""

    if source_id != LOCAL_FILE_SOURCE_ID:
        return
    binding_required = result in {"success", "explicit_empty"}
    if (binding_required and tool_binding != LOCAL_FILE_READER_TOOL) or (
        tool_binding is not None and tool_binding != LOCAL_FILE_READER_TOOL
    ):
        raise ValidationError(
            f"{LOCAL_FILE_SOURCE_ID} observations require tool_binding {LOCAL_FILE_READER_TOOL!r}"
        )


def _read_granted_file(*, grant: LocalFileGrant, relative: Path) -> dict[str, Any]:
    root = Path(grant.root)
    relative_text = relative.as_posix()
    preview = assess_local_path(relative, selected_root=root, content_requested=True)
    if preview.relative_path is not None and preview.relative_path != relative_text:
        return _result(
            grant=grant,
            relative_path=relative_text,
            decision=AwarenessDecision.EXCLUDE,
            reason="symbolic-link-ancestry",
        )

    screened = read_screened_local_content(
        relative,
        selected_root=root,
        expected_root_identity=(grant.device, grant.inode),
    )
    result = _result(
        grant=grant,
        relative_path=relative_text,
        decision=screened.path.decision,
        reason=screened.path.reason,
    )
    if screened.screening is not None:
        result["screening"] = {
            "bytes_screened": screened.screening.bytes_screened,
            "decision": screened.screening.decision.value,
            "reasons": list(screened.screening.reasons),
        }
        if screened.screening.decision is AwarenessDecision.QUARANTINE:
            result["decision"] = AwarenessDecision.QUARANTINE.value
            result["reason"] = "content-screen"
    if screened.content is not None:
        try:
            result["content"] = screened.content.decode("utf-8")
        except UnicodeDecodeError:
            result["screening"] = {
                "bytes_screened": len(screened.content),
                "decision": AwarenessDecision.QUARANTINE.value,
                "reasons": ["non-utf8-content"],
            }
            result["decision"] = AwarenessDecision.QUARANTINE.value
            result["reason"] = "non-utf8-content"
    return result


def _grant_root(value: Path | str) -> tuple[Path, tuple[int, int]]:
    text = _bounded_text(str(value), "selected root")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValidationError("selected root must be an absolute directory path")
    try:
        listed = os.lstat(candidate)
        canonical = candidate.resolve(strict=True)
        resolved = os.stat(canonical, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError("selected root is unavailable") from exc
    if (
        _is_link_or_reparse(listed)
        or not stat.S_ISDIR(listed.st_mode)
        or not stat.S_ISDIR(resolved.st_mode)
        or _identity(listed) != _identity(resolved)
    ):
        raise ValidationError("selected root must be one stable real directory")
    assessment = assess_local_path(canonical, selected_root=canonical)
    if assessment.decision is AwarenessDecision.EXCLUDE:
        raise ValidationError(f"selected root is not grantable: {assessment.reason}")
    return canonical, _identity(resolved)


def _real_directory(value: Path, label: str) -> tuple[Path, tuple[int, int]]:
    try:
        listed = os.lstat(value)
        canonical = value.resolve(strict=True)
        resolved = os.stat(canonical, follow_symlinks=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValidationError(f"local-file {label} is unavailable") from exc
    if (
        _is_link_or_reparse(listed)
        or not stat.S_ISDIR(listed.st_mode)
        or not stat.S_ISDIR(resolved.st_mode)
        or _identity(listed) != _identity(resolved)
    ):
        raise ValidationError(f"local-file {label} must be one stable real directory")
    return canonical, _identity(resolved)


def _directory_matches(value: str, *, expected: tuple[int, int]) -> bool:
    try:
        metadata = os.lstat(value)
    except OSError:
        return False
    return (
        not _is_link_or_reparse(metadata)
        and stat.S_ISDIR(metadata.st_mode)
        and _identity(metadata) == expected
    )


def _root_matches(grant: LocalFileGrant) -> bool:
    try:
        metadata = os.lstat(grant.root)
    except OSError:
        return False
    return (
        not _is_link_or_reparse(metadata)
        and stat.S_ISDIR(metadata.st_mode)
        and _identity(metadata) == (grant.device, grant.inode)
    )


def _prepare_private_storage(root: Path) -> None:
    if not os.path.lexists(root):
        root.mkdir(parents=True, mode=0o700)
        if os.name != "nt":
            root.chmod(0o700)
    try:
        metadata = os.lstat(root)
    except OSError as exc:
        raise ValidationError("Seld host-local storage is unavailable") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("Seld host-local storage must be one real directory")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValidationError("Seld host-local storage must be owner-only")


def _validate_private_file(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValidationError("Seld local-file grant store is unavailable") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("Seld local-file grant store must be one regular file")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValidationError("Seld local-file grant store must be owner-only")


def _grant_from_value(value: object) -> LocalFileGrant:
    if not isinstance(value, dict) or set(value) != _GRANT_KEYS:
        raise ValidationError("Seld local-file grant has an unsupported shape")
    device = value.get("device")
    inode = value.get("inode")
    vault_device = value.get("vault_device")
    vault_inode = value.get("vault_inode")
    created_at = value.get("created_at")
    if any(
        type(identity) is not int or identity < 0
        for identity in (device, inode, vault_device, vault_inode)
    ):
        raise ValidationError("Seld local-file grant has an invalid root identity")
    if not isinstance(created_at, str):
        raise ValidationError("Seld local-file grant has an invalid creation time")
    return LocalFileGrant(
        grant_id=_grant_id(value.get("grant_id")),
        vault_id=canonical_vault_id(value.get("vault_id")),
        vault_root=_stored_absolute_path(value.get("vault_root"), "vault root"),
        vault_device=cast(int, vault_device),
        vault_inode=cast(int, vault_inode),
        root=_stored_absolute_path(value.get("root"), "selected root"),
        device=cast(int, device),
        inode=cast(int, inode),
        created_at=stored_time(created_at, "local-file grant creation time"),
    )


def _validate_legacy_grant(value: object) -> None:
    if not isinstance(value, dict) or set(value) != _LEGACY_GRANT_KEYS:
        raise ValidationError("Seld local-file grant has an unsupported shape")
    device = value.get("device")
    inode = value.get("inode")
    created_at = value.get("created_at")
    if type(device) is not int or device < 0 or type(inode) is not int or inode < 0:
        raise ValidationError("Seld local-file grant has an invalid root identity")
    if not isinstance(created_at, str):
        raise ValidationError("Seld local-file grant has an invalid creation time")
    _grant_id(value.get("grant_id"))
    canonical_vault_id(value.get("vault_id"))
    _stored_absolute_path(value.get("vault_root"), "vault root")
    _stored_absolute_path(value.get("root"), "selected root")
    stored_time(created_at, "local-file grant creation time")


def _grant_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("local-file grant ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError("local-file grant ID is invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValidationError("local-file grant ID is invalid")
    return value


def _stored_absolute_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"Seld local-file grant has an invalid {label}")
    text = _bounded_text(value, label)
    if not Path(text).is_absolute():
        raise ValidationError(f"Seld local-file grant has an invalid {label}")
    return text


def _relative_path(value: str) -> Path:
    text = _bounded_text(value, "relative_path")
    candidate = Path(text)
    if candidate.is_absolute() or not candidate.parts:
        raise ValidationError("relative_path must name one path beneath the granted root")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValidationError("relative_path cannot contain empty, dot, or parent components")
    return candidate


def _bounded_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{label} is not valid Unicode text") from exc
    if not encoded or len(encoded) > MAX_LOCAL_PATH_BYTES or b"\x00" in encoded:
        raise ValidationError(f"{label} is empty, too large, or contains a null byte")
    return value


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    file_attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_flag)


def _result(
    *,
    grant: LocalFileGrant,
    relative_path: str,
    decision: AwarenessDecision,
    reason: str,
) -> dict[str, Any]:
    return {
        "capability": LOCAL_FILE_READ_CAPABILITY,
        "decision": decision.value,
        "grant_id": grant.grant_id,
        "persisted": False,
        "reason": reason,
        "relative_path": relative_path,
        "selected_root": grant.root,
        "source": LOCAL_FILE_SOURCE_ID,
        "tool_binding": LOCAL_FILE_READER_TOOL,
        "transient": True,
    }
