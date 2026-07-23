"""Per-user configuration and platform paths."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from continuity_kernel.atomic import atomic_write
from continuity_kernel.errors import SetupError, ValidationError

CONFIG_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class Config:
    format_version: int
    vault: str

    @property
    def vault_path(self) -> Path:
        return _resolve_configured_vault(self.vault)


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


def load_config(*, required: bool = True) -> Config | None:
    path = config_path()
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise SetupError("GSV is not configured. Run `gsv setup` first.") from None
        return None
    except OSError as exc:
        raise ValidationError(f"invalid GSV configuration: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValidationError(f"configuration cannot be a symbolic link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"configuration must be a regular file: {path}")
    if metadata.st_size > CONFIG_MAX_BYTES:
        raise ValidationError(f"GSV configuration is too large: {path}")
    try:
        encoded = _read_config_bytes(path, metadata)
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid GSV configuration: {path}") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise ValidationError("unsupported GSV configuration version")
    vault = payload.get("vault")
    if not isinstance(vault, str) or not vault.strip():
        raise ValidationError("GSV configuration has no valid vault path")
    return Config(format_version=1, vault=str(_resolve_configured_vault(vault)))


def _read_config_bytes(path: Path, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            raise ValidationError(f"configuration changed while it was opened: {path}")
        chunks: list[bytes] = []
        remaining = CONFIG_MAX_BYTES + 1
        while remaining:
            block = os.read(descriptor, remaining)
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        encoded = b"".join(chunks)
        if len(encoded) > CONFIG_MAX_BYTES:
            raise ValidationError(f"GSV configuration is too large: {path}")
        finished = os.fstat(descriptor)
        if finished.st_size != opened.st_size or finished.st_mtime_ns != opened.st_mtime_ns:
            raise ValidationError(f"configuration changed while it was read: {path}")
        listed = os.lstat(path)
        if (listed.st_dev, listed.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError(f"configuration changed while it was read: {path}")
        return encoded
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _resolve_configured_vault(value: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValidationError("GSV configuration has an invalid vault path") from exc


def save_config(vault: Path) -> Config:
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        target.parent.chmod(0o700)
    config = Config(format_version=1, vault=str(vault.expanduser().resolve()))
    encoded = (json.dumps(asdict(config), indent=2, sort_keys=True) + "\n").encode()
    atomic_write(target, encoded)
    return config


def resolve_vault(explicit: str | Path | None = None, *, require_config: bool = True) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    override = os.environ.get("GSV_VAULT")
    if override:
        return Path(override).expanduser().resolve()
    config = load_config(required=require_config)
    return config.vault_path if config else default_vault()
