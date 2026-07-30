"""Explicit age-encrypted connector credential export and import."""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from continuity_kernel.atomic import (
    atomic_write,
    durable_publish_new,
    durable_unlink,
    read_regular_file,
)
from continuity_kernel.connections import ConnectionSnapshot
from continuity_kernel.connector_auth import ConnectionHealth, ConnectionMetadata
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_time import format_utc
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    SetupError,
    ValidationError,
)

AUTH_ARCHIVE_FORMAT_VERSION: Final = 1
MAX_AUTH_ARCHIVE_BYTES: Final = 4 * 1024 * 1024
MAX_ENCRYPTED_ARCHIVE_BYTES: Final = 8 * 1024 * 1024
_AGE_CANDIDATES: Final = (
    Path("/opt/homebrew/bin/age"),
    Path("/usr/local/bin/age"),
    Path("/usr/bin/age"),
)


@dataclass(frozen=True)
class DecodedAuthArchive:
    vault_id: str
    connection_revision: str
    connections: tuple[tuple[ConnectionMetadata, bytes], ...]


def export_auth_archive(
    manager: ConnectorAuthManager,
    destination: Path,
    *,
    recipient: str,
    age_executable: Path | None = None,
) -> dict[str, object]:
    """Encrypt every configured credential without writing plaintext to disk."""

    snapshot = manager.vault.get_connection_snapshot()
    if not snapshot.connections:
        raise ValidationError("there are no connector credentials to export")
    records: list[dict[str, object]] = []
    for metadata in snapshot.connections:
        credential = manager.tokens.read(metadata.connection_id).value
        manager.validate_import_credential(metadata, credential)
        records.append(
            {
                "credential": base64.b64encode(credential).decode("ascii"),
                "metadata": metadata.to_dict(),
            }
        )
    plaintext = _encode_archive(
        {
            "connection_revision": snapshot.revision,
            "connections": records,
            "format_version": AUTH_ARCHIVE_FORMAT_VERSION,
            "vault_id": manager.vault_id,
        }
    )
    ciphertext = _run_age(
        ("--encrypt", "--recipient", _recipient(recipient)),
        plaintext,
        executable=age_executable,
        output_bound=MAX_ENCRYPTED_ARCHIVE_BYTES,
    )
    target = destination.expanduser().resolve()
    _publish_new_private(target, ciphertext)
    return {
        "connection_count": len(records),
        "encrypted": True,
        "export": str(target),
        "vault_id": manager.vault_id,
    }


def import_auth_archive(
    manager: ConnectorAuthManager,
    source: Path,
    *,
    identity: Path,
    age_executable: Path | None = None,
) -> dict[str, object]:
    """Restore onto the matching snapshot or its exact resumable import state."""

    ciphertext = read_regular_file(
        source.expanduser().resolve(),
        label="encrypted connector auth archive",
        max_bytes=MAX_ENCRYPTED_ARCHIVE_BYTES,
    )
    identity_path = _regular_path(identity, "age identity")
    plaintext = _run_age(
        ("--decrypt", "--identity", str(identity_path)),
        ciphertext,
        executable=age_executable,
        output_bound=MAX_AUTH_ARCHIVE_BYTES,
    )
    archive = _decode_archive(plaintext)
    if archive.vault_id != manager.vault_id:
        raise ConflictError("connector auth archive belongs to another vault")
    current = manager.vault.get_connection_snapshot()
    records = archive.connections
    metadata = tuple(item[0] for item in records)
    initial_snapshot = (
        archive.connection_revision == current.revision and metadata == current.connections
    )
    resumed_snapshot = _is_unverified_import_snapshot(current, metadata)
    if not initial_snapshot and not resumed_snapshot:
        raise ConflictError("portable connection records differ from the auth archive")

    for item, credential in records:
        manager.validate_import_credential(item, credential)
        manager.tokens.validate_import(item.connection_id, credential)

    if initial_snapshot:
        replacement = _unverified_snapshot(current)
        try:
            manager.vault.replace_connections(
                expected_revision=current.revision,
                snapshot=replacement,
            )
        except Exception as import_error:
            try:
                visible = manager.vault.get_connection_snapshot()
            except Exception as readback_error:
                raise DegradedIntegrityError(
                    "connector auth import metadata could not be reconciled; rerun the archive"
                ) from readback_error
            if _is_unverified_import_snapshot(visible, metadata):
                raise MutationCommittedError(
                    "connector auth import metadata is visible; rerun the archive to finish"
                ) from import_error
            if visible != current:
                raise DegradedIntegrityError(
                    "connector auth import metadata outcome is unknown; inspect before retrying"
                ) from import_error
            raise

    imported: list[ConnectionMetadata] = []
    for item, credential in records:
        try:
            manager.ensure_imported_credential(item, credential)
        except Exception as import_error:
            raise DegradedIntegrityError(
                "connector auth import is incomplete; rerun the same archive"
            ) from import_error
        imported.append(item)
    return {
        "connection_count": len(imported),
        "imported": True,
        "requires_verification": True,
        "vault_id": manager.vault_id,
    }


def _unverified_snapshot(current: ConnectionSnapshot) -> ConnectionSnapshot:
    now = datetime.now(UTC)
    floor = max((item.updated_at for item in current.connections), default=now)
    if now <= floor:
        now = floor + timedelta(microseconds=1)
    connections = tuple(
        replace(
            item,
            health=ConnectionHealth.UNVERIFIED,
            last_verified_at=None,
            updated_at=now,
            version=item.version + 1,
        )
        for item in current.connections
    )
    return ConnectionSnapshot(
        format_version=current.format_version,
        connections=connections,
        updated_at=format_utc(now),
        revision="",
    )


def _is_unverified_import_snapshot(
    current: ConnectionSnapshot,
    archived: tuple[ConnectionMetadata, ...],
) -> bool:
    if len(current.connections) != len(archived) or not current.connections:
        return False
    imported_at = current.connections[0].updated_at
    if current.updated_at != format_utc(imported_at):
        return False
    for visible, original in zip(current.connections, archived, strict=True):
        if visible != replace(
            original,
            health=ConnectionHealth.UNVERIFIED,
            last_verified_at=None,
            updated_at=imported_at,
            version=original.version + 1,
        ):
            return False
    return True


def _encode_archive(value: dict[str, object]) -> bytes:
    encoded = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_AUTH_ARCHIVE_BYTES:
        raise ValidationError("connector auth archive exceeds its size bound")
    return encoded


def _decode_archive(encoded: bytes) -> DecodedAuthArchive:
    if not encoded or len(encoded) > MAX_AUTH_ARCHIVE_BYTES:
        raise ValidationError("connector auth archive is empty or too large")
    try:
        value = cast(object, json.loads(encoded.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("connector auth archive is invalid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError("connector auth archive has an unsupported shape")
    payload = cast(dict[str, object], value)
    if set(payload) != {"connection_revision", "connections", "format_version", "vault_id"}:
        raise ValidationError("connector auth archive has an unsupported shape")
    if payload["format_version"] != AUTH_ARCHIVE_FORMAT_VERSION:
        raise ValidationError("connector auth archive version is unsupported")
    if not isinstance(payload["vault_id"], str) or not isinstance(
        payload["connection_revision"], str
    ):
        raise ValidationError("connector auth archive identity is invalid")
    raw_records = payload["connections"]
    if not isinstance(raw_records, list) or not raw_records:
        raise ValidationError("connector auth archive has no connection records")
    records: list[tuple[ConnectionMetadata, bytes]] = []
    for raw in raw_records:
        if not isinstance(raw, dict) or set(raw) != {"credential", "metadata"}:
            raise ValidationError("connector auth archive record is invalid")
        credential = raw["credential"]
        if not isinstance(credential, str):
            raise ValidationError("connector auth archive credential is invalid")
        try:
            secret = base64.b64decode(credential.encode("ascii"), validate=True)
        except (UnicodeError, ValueError) as exc:
            raise ValidationError("connector auth archive credential is invalid") from exc
        if not secret:
            raise ValidationError("connector auth archive credential is empty")
        records.append((ConnectionMetadata.from_dict(raw["metadata"]), secret))
    identifiers = [str(item.connection_id) for item, _secret in records]
    if identifiers != sorted(identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValidationError("connector auth archive connections are not canonical")
    return DecodedAuthArchive(
        connection_revision=payload["connection_revision"],
        connections=tuple(records),
        vault_id=payload["vault_id"],
    )


def _run_age(
    arguments: tuple[str, ...],
    content: bytes,
    *,
    executable: Path | None,
    output_bound: int,
) -> bytes:
    age = _age_executable(executable)
    try:
        result = subprocess.run(
            [str(age), *arguments],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError("age could not process the connector auth archive") from exc
    if result.returncode != 0:
        raise ValidationError("age rejected the connector auth archive or key")
    if not result.stdout or len(result.stdout) > output_bound:
        raise ValidationError("age output is empty or exceeds its size bound")
    return bytes(result.stdout)


def _age_executable(explicit: Path | None) -> Path:
    candidates = (explicit,) if explicit is not None else _AGE_CANDIDATES
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            resolved = _regular_path(candidate, "age executable")
        except (OSError, ValidationError):
            continue
        if os.access(resolved, os.X_OK):
            return resolved
    raise SetupError("age is unavailable; install age or pass its absolute executable path")


def _regular_path(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValidationError(f"{label} path must be absolute")
    resolved = expanded.resolve(strict=True)
    metadata = os.stat(resolved, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"{label} must be a regular file")
    return resolved


def _recipient(value: str) -> str:
    if not value.strip() or len(value.encode("utf-8")) > 4_096 or "\x00" in value:
        raise ValidationError("age recipient is invalid")
    return value


def _publish_new_private(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.seld-auth-stage-{secrets.token_hex(16)}"
    atomic_write(stage, content, mode=0o600)
    cleanup_stage = False
    try:
        durable_publish_new(stage, target)
        cleanup_stage = True
    except FileExistsError as exc:
        cleanup_stage = True
        raise ConflictError("connector auth export destination already exists") from exc
    finally:
        if cleanup_stage and os.path.lexists(stage):
            durable_unlink(stage)
