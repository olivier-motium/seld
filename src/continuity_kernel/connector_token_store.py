"""CAS-protected token bundle publication backed by opaque secret custody."""

from __future__ import annotations

import hmac
import json
import math
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn, cast

from continuity_kernel.atomic import atomic_write, durable_unlink, exclusive_lock, read_regular_file
from continuity_kernel.connector_identifiers import (
    ConnectionId,
    SecretName,
    SecretReference,
    SecretStore,
    parse_connection_id,
    parse_secret_reference,
    resolve_secret_reference,
)
from continuity_kernel.connector_time import format_utc
from continuity_kernel.errors import (
    ConflictError,
    DegradedIntegrityError,
    MutationCommittedError,
    NotFoundError,
    ValidationError,
)

TOKEN_STATE_FORMAT_VERSION: Final = 1
MAX_TOKEN_STATE_BYTES: Final = 4 * 1024
MAX_TOKEN_BUNDLE_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class TokenState:
    """Non-secret pointer to one immutable token bundle in a SecretStore."""

    connection_id: ConnectionId
    version: int
    secret_reference: SecretReference
    updated_at: datetime
    format_version: int = TOKEN_STATE_FORMAT_VERSION

    def __post_init__(self) -> None:
        clean_id = parse_connection_id(self.connection_id)
        if self.secret_reference.connection_id != clean_id:
            raise ValidationError("token state secret belongs to another connection")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValidationError("token state version must be a positive integer")
        if self.format_version != TOKEN_STATE_FORMAT_VERSION:
            raise ValidationError("token state format version is unsupported")
        format_utc(self.updated_at)


@dataclass(frozen=True)
class ResolvedToken:
    """Transient token bytes paired with their non-secret persisted state."""

    state: TokenState
    value: bytes


class AtomicTokenStore:
    """Publish immutable secret versions behind one atomically replaced pointer."""

    def __init__(self, root: Path | str, secrets_store: SecretStore) -> None:
        expanded = Path(root).expanduser()
        if not expanded.is_absolute():
            raise ValidationError("connector token storage root must be absolute")
        self.root = Path(os.path.abspath(expanded))
        self.secrets = secrets_store

    @contextmanager
    def exclusive_lifecycle(
        self,
        connection_id: ConnectionId,
        *,
        lock_timeout_seconds: float = 10.0,
    ) -> Iterator[None]:
        """Serialize metadata-aware operations for one connection."""

        clean_id = parse_connection_id(connection_id)
        _validate_lock_timeout(lock_timeout_seconds)
        self._prepare()
        lifecycle_path = self.root / "locks" / f"{clean_id}.lifecycle.lock"
        with exclusive_lock(lifecycle_path, timeout=lock_timeout_seconds):
            yield

    def state(
        self,
        connection_id: ConnectionId,
        *,
        lock_timeout_seconds: float = 10.0,
    ) -> TokenState | None:
        clean_id = parse_connection_id(connection_id)
        _validate_lock_timeout(lock_timeout_seconds)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id), timeout=lock_timeout_seconds):
            return self._load_unlocked(clean_id)

    def read(self, connection_id: ConnectionId) -> ResolvedToken:
        clean_id = parse_connection_id(connection_id)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id)):
            state = self._load_unlocked(clean_id)
            if state is None:
                raise NotFoundError("connector token state was not found")
            return ResolvedToken(
                state=state,
                value=resolve_secret_reference(state.secret_reference, self.secrets),
            )

    def occupied(self, connection_id: ConnectionId) -> bool:
        """Report whether any pointer or bounded crash-recovery slot exists."""

        clean_id = parse_connection_id(connection_id)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id)):
            if self._load_unlocked(clean_id) is not None:
                return True
            return any(
                self.secrets.get_secret(clean_id, name) is not None
                for name in (SecretName("token-slot-a"), SecretName("token-slot-b"))
            )

    def validate_import(self, connection_id: ConnectionId, value: bytes) -> None:
        """Accept only empty custody or an exact resumable first import."""

        clean_id = parse_connection_id(connection_id)
        secret = _token_bytes(value)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id)):
            self._assert_import_compatible_unlocked(clean_id, secret)

    def ensure_imported(
        self,
        connection_id: ConnectionId,
        value: bytes,
        *,
        updated_at: datetime | None = None,
    ) -> TokenState:
        """Idempotently publish one exact version-one credential for archive restore."""

        clean_id = parse_connection_id(connection_id)
        secret = _token_bytes(value)
        observed_at = updated_at or datetime.now(UTC)
        format_utc(observed_at)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id)):
            self._assert_import_compatible_unlocked(clean_id, secret)
            current = self._load_unlocked(clean_id)
            if current is not None:
                try:
                    atomic_write(self._state_path(clean_id), _encode_state(current), mode=0o600)
                except Exception as exc:
                    raise DegradedIntegrityError(
                        "credential import durability is unconfirmed; rerun the same archive"
                    ) from exc
                return current

            reference = SecretReference(clean_id, SecretName("token-slot-a"))
            state = TokenState(
                connection_id=clean_id,
                version=1,
                secret_reference=reference,
                updated_at=observed_at,
            )
            try:
                if self.secrets.get_secret(clean_id, reference.name) is None:
                    self.secrets.set_secret(clean_id, reference.name, secret)
                atomic_write(self._state_path(clean_id), _encode_state(state), mode=0o600)
            except Exception as exc:
                raise DegradedIntegrityError(
                    "credential import may be partially committed; rerun the same archive"
                ) from exc
            return state

    def update(
        self,
        connection_id: ConnectionId,
        *,
        expected_version: int,
        value: bytes,
        updated_at: datetime | None = None,
    ) -> TokenState:
        """CAS-update one token; version zero means no persisted token yet."""

        clean_id = parse_connection_id(connection_id)
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValidationError("expected token version must be a non-negative integer")
        if not isinstance(value, bytes) or not value or len(value) > MAX_TOKEN_BUNDLE_BYTES:
            raise ValidationError("token bundle is empty or exceeds its size bound")
        observed_at = updated_at or datetime.now(UTC)
        format_utc(observed_at)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id)):
            current = self._load_unlocked(clean_id)
            current_version = current.version if current is not None else 0
            if current_version != expected_version:
                raise ConflictError("connector token changed; reload before updating")
            return self._publish_unlocked(clean_id, current, value, observed_at)

    def refresh_serialized(
        self,
        connection_id: ConnectionId,
        *,
        transform: Callable[[ResolvedToken], bytes | None],
        lock_timeout_seconds: float = 30.0,
        updated_at: datetime | None = None,
    ) -> ResolvedToken:
        """Resolve, transform, and publish one credential under the same process lock."""

        clean_id = parse_connection_id(connection_id)
        observed_at = updated_at or datetime.now(UTC)
        format_utc(observed_at)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id), timeout=lock_timeout_seconds):
            current = self._load_unlocked(clean_id)
            if current is None:
                raise NotFoundError("connector token state was not found")
            resolved = ResolvedToken(
                state=current,
                value=resolve_secret_reference(current.secret_reference, self.secrets),
            )
            replacement = transform(resolved)
            if replacement is None:
                return resolved
            next_state = self._publish_unlocked(clean_id, current, replacement, observed_at)
            return ResolvedToken(state=next_state, value=bytes(replacement))

    def delete(self, connection_id: ConnectionId) -> bool:
        """Revoke both bounded rotation slots before removing their host-local pointer."""

        clean_id = parse_connection_id(connection_id)
        self._prepare()
        with exclusive_lock(self._lock_path(clean_id)):
            current = self._load_unlocked(clean_id)
            removed = current is not None
            for name in (SecretName("token-slot-a"), SecretName("token-slot-b")):
                if self.secrets.get_secret(clean_id, name) is not None:
                    self.secrets.delete_secret(clean_id, name)
                    removed = True
            path = self._state_path(clean_id)
            if os.path.lexists(path):
                durable_unlink(path)
            return removed

    def _publish_unlocked(
        self,
        connection_id: ConnectionId,
        previous: TokenState | None,
        value: bytes,
        updated_at: datetime,
    ) -> TokenState:
        if not isinstance(value, bytes) or not value or len(value) > MAX_TOKEN_BUNDLE_BYTES:
            raise ValidationError("token bundle is empty or exceeds its size bound")
        next_version = (previous.version if previous is not None else 0) + 1
        next_reference = SecretReference(
            connection_id=connection_id,
            name=_slot_for_version(next_version),
        )
        next_state = TokenState(
            connection_id=connection_id,
            version=next_version,
            secret_reference=next_reference,
            updated_at=updated_at,
        )
        self.secrets.set_secret(connection_id, next_reference.name, bytes(value))
        try:
            atomic_write(self._state_path(connection_id), _encode_state(next_state), mode=0o600)
        except Exception as publish_error:
            self._handle_publish_failure(
                next_state=next_state,
                previous=previous,
                publish_error=publish_error,
            )
        self._delete_previous_after_commit(previous)
        return next_state

    def _assert_import_compatible_unlocked(
        self,
        connection_id: ConnectionId,
        value: bytes,
    ) -> None:
        slot_a = SecretName("token-slot-a")
        slot_b = SecretName("token-slot-b")
        current = self._load_unlocked(connection_id)
        first = self.secrets.get_secret(connection_id, slot_a)
        second = self.secrets.get_secret(connection_id, slot_b)
        if current is not None:
            if (
                current.version != 1
                or current.secret_reference.name != slot_a
                or first is None
                or second is not None
                or not hmac.compare_digest(first, value)
            ):
                raise ConflictError("this host already has different connector credential state")
            return
        if second is not None or (first is not None and not hmac.compare_digest(first, value)):
            raise ConflictError("this host already has different connector credential state")

    def _handle_publish_failure(
        self,
        *,
        next_state: TokenState,
        previous: TokenState | None,
        publish_error: Exception,
    ) -> NoReturn:
        """Retain a possibly committed secret; clean up only a proven-unpublished one."""

        try:
            visible = self._load_unlocked(next_state.connection_id)
        except Exception as readback_error:
            raise DegradedIntegrityError(
                "token publication outcome is unknown; staged secret was retained"
            ) from readback_error
        if visible == next_state:
            self._delete_previous_after_commit(previous)
            raise MutationCommittedError(
                "token update is visible, but durable publication reported a failure; reload"
            ) from publish_error
        if visible != previous:
            raise DegradedIntegrityError(
                "token publication outcome is unknown; staged secret was retained"
            ) from publish_error
        try:
            self.secrets.delete_secret(
                next_state.connection_id,
                next_state.secret_reference.name,
            )
        except Exception as cleanup_error:
            raise DegradedIntegrityError(
                "token publication failed and staged secret cleanup also failed"
            ) from cleanup_error
        raise publish_error

    def _delete_previous_after_commit(self, previous: TokenState | None) -> None:
        if previous is None:
            return
        try:
            self.secrets.delete_secret(
                previous.connection_id,
                previous.secret_reference.name,
            )
        except Exception as cleanup_error:
            raise MutationCommittedError(
                "token update committed, but the prior secret could not be removed; reload"
            ) from cleanup_error

    def _load_unlocked(self, connection_id: ConnectionId) -> TokenState | None:
        path = self._state_path(connection_id)
        if not os.path.lexists(path):
            return None
        encoded = read_regular_file(
            path,
            label="connector token state",
            max_bytes=MAX_TOKEN_STATE_BYTES,
        )
        return _decode_state(encoded, expected_connection_id=connection_id)

    def _prepare(self) -> None:
        _private_directory(self.root)
        _private_directory(self.root / "locks")
        _private_directory(self.root / "state")

    def _lock_path(self, connection_id: ConnectionId) -> Path:
        return self.root / "locks" / f"{connection_id}.lock"

    def _state_path(self, connection_id: ConnectionId) -> Path:
        return self.root / "state" / f"{connection_id}.json"


def _encode_state(state: TokenState) -> bytes:
    encoded = (
        json.dumps(
            {
                "connection_id": str(state.connection_id),
                "format_version": state.format_version,
                "secret_reference": str(state.secret_reference),
                "updated_at": format_utc(state.updated_at),
                "version": state.version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_TOKEN_STATE_BYTES:
        raise ValidationError("connector token state exceeds its size bound")
    return encoded


def _slot_for_version(version: int) -> SecretName:
    return SecretName("token-slot-a" if version % 2 else "token-slot-b")


def _token_bytes(value: object) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > MAX_TOKEN_BUNDLE_BYTES:
        raise ValidationError("token bundle is empty or exceeds its size bound")
    return bytes(value)


def _validate_lock_timeout(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValidationError("connector credential lock timeout must be non-negative and finite")


def _decode_state(encoded: bytes, *, expected_connection_id: ConnectionId) -> TokenState:
    try:
        value = cast(object, json.loads(encoded.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("stored connector token state is invalid") from exc
    if not isinstance(value, dict):
        raise ValidationError("stored connector token state has an unsupported shape")
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValidationError("stored connector token state has an unsupported shape")
    payload = {cast(str, key): item for key, item in raw.items()}
    if set(payload) != {
        "connection_id",
        "format_version",
        "secret_reference",
        "updated_at",
        "version",
    }:
        raise ValidationError("stored connector token state has an unsupported shape")
    clean_id = parse_connection_id(payload["connection_id"])
    if clean_id != expected_connection_id:
        raise ValidationError("stored token state belongs to another connection")
    version = payload["version"]
    format_version = payload["format_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("stored connector token version is invalid")
    if format_version != TOKEN_STATE_FORMAT_VERSION:
        raise ValidationError("stored connector token format version is unsupported")
    timestamp = payload["updated_at"]
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValidationError("stored connector token update time is invalid")
    try:
        parsed_time = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError("stored connector token update time is invalid") from exc
    return TokenState(
        connection_id=clean_id,
        version=version,
        secret_reference=parse_secret_reference(payload["secret_reference"]),
        updated_at=parsed_time,
        format_version=format_version,
    )


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValidationError(f"connector token storage is unavailable: {path}") from exc
    file_attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(file_attributes & reparse_flag)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValidationError("connector token storage must use real directories")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValidationError("connector token storage permissions are too broad")
