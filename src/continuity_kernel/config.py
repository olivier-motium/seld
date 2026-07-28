"""Per-user configuration and platform paths."""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from continuity_kernel.atomic import atomic_write, durable_unlink, exclusive_lock, read_regular_file
from continuity_kernel.errors import SetupError, ValidationError

CONFIG_MAX_BYTES = 64 * 1024
HOST_ID_MAX_BYTES = 128
_HOST_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Config:
    format_version: int
    vault: str

    @property
    def vault_path(self) -> Path:
        return _resolve_configured_vault(self.vault)


@dataclass(frozen=True)
class ConfigSnapshot:
    exists: bool
    encoded: bytes


def config_dir() -> Path:
    override = os.environ.get("GSV_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/GSV"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "GSV"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gsv"


def data_dir() -> Path:
    override = os.environ.get("GSV_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/GSV"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "GSV"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "gsv"


def default_vault() -> Path:
    return (Path.home() / "GSV").resolve()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def config_path() -> Path:
    return config_dir() / "config.json"


def host_id_path() -> Path:
    """Return the machine-local identity path used to bind live source reads."""

    return data_dir() / "host-id"


def local_host_id(*, create: bool = False) -> str | None:
    """Load, or explicitly create, one privacy-safe machine-local UUID.

    Reading source status never creates state. A successful live source read
    creates the identifier when needed, allowing a restored portable vault to
    retain historical coverage without presenting it as proven on another host.
    """

    path = host_id_path()
    with exclusive_lock(data_dir() / "locks/host-id.lock"):
        if not os.path.lexists(path):
            if not create:
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                path.parent.chmod(0o700)
            value = str(uuid.uuid4())
            atomic_write(path, f"{value}\n".encode("ascii"))
            return value
        try:
            encoded = read_regular_file(
                path,
                label="Seld host identity",
                max_bytes=HOST_ID_MAX_BYTES,
            )
            value = encoded.decode("ascii").strip()
        except (UnicodeError, ValidationError) as exc:
            raise ValidationError("invalid Seld host identity") from exc
        if _HOST_ID.fullmatch(value) is None or encoded != f"{value}\n".encode("ascii"):
            raise ValidationError("invalid Seld host identity")
        return value


def _config_lock_path() -> Path:
    return config_dir() / "locks/config.lock"


def load_config(*, required: bool = True) -> Config | None:
    path = config_path()
    try:
        encoded = read_regular_file(
            path,
            label="Seld configuration",
            max_bytes=CONFIG_MAX_BYTES,
        )
    except ValidationError as exc:
        if not os.path.lexists(path):
            if required:
                raise SetupError("Seld is not configured. Run `gsv setup` first.") from None
            return None
        raise ValidationError(f"invalid Seld configuration: {path}") from exc
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid Seld configuration: {path}") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise ValidationError("unsupported Seld configuration version")
    vault = payload.get("vault")
    if not isinstance(vault, str) or not vault.strip():
        raise ValidationError("Seld configuration has no valid records path")
    return Config(format_version=1, vault=str(_resolve_configured_vault(vault)))


def _resolve_configured_vault(value: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValidationError("Seld configuration has an invalid records path") from exc


def save_config(vault: Path) -> Config:
    with exclusive_lock(_config_lock_path()):
        config, _ = _save_config_unlocked(vault)
    return config


def _save_config_unlocked(vault: Path) -> tuple[Config, bytes]:
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        target.parent.chmod(0o700)
    config = Config(format_version=1, vault=str(vault.expanduser().resolve()))
    encoded = (json.dumps(asdict(config), indent=2, sort_keys=True) + "\n").encode()
    atomic_write(target, encoded)
    return config, encoded


def _capture_config_unlocked() -> ConfigSnapshot:
    path = config_path()
    if not os.path.lexists(path):
        return ConfigSnapshot(False, b"")
    return ConfigSnapshot(
        True,
        read_regular_file(path, label="Seld configuration", max_bytes=CONFIG_MAX_BYTES),
    )


def activate_config(vault: Path) -> tuple[Config, ConfigSnapshot, bytes]:
    """Install setup configuration and return one exact rollback checkpoint."""

    with exclusive_lock(_config_lock_path()):
        previous = _capture_config_unlocked()
        config, installed = _save_config_unlocked(vault)
    return config, previous, installed


def restore_config(snapshot: ConfigSnapshot, *, expected: bytes) -> str | None:
    """Restore a snapshot only when the setup-owned bytes are still present."""

    path = config_path()
    try:
        with exclusive_lock(_config_lock_path()):
            if not os.path.lexists(path):
                current = b""
            else:
                current = read_regular_file(
                    path,
                    label="Seld configuration",
                    max_bytes=CONFIG_MAX_BYTES,
                )
            if current != expected:
                return "Seld configuration changed concurrently and was left untouched"
            if snapshot.exists:
                atomic_write(path, snapshot.encoded)
            elif os.path.lexists(path):
                durable_unlink(path)
        return None
    except (OSError, ValidationError) as exc:
        return f"could not restore the previous Seld configuration: {exc}"


def resolve_vault(explicit: str | Path | None = None, *, require_config: bool = True) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    override = os.environ.get("GSV_VAULT")
    if override:
        return Path(override).expanduser().resolve()
    config = load_config(required=require_config)
    return config.vault_path if config else default_vault()
