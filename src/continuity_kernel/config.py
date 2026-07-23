"""Per-user configuration and platform paths."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from continuity_kernel.atomic import atomic_write
from continuity_kernel.errors import SetupError, ValidationError


@dataclass(frozen=True)
class Config:
    format_version: int
    vault: str

    @property
    def vault_path(self) -> Path:
        return Path(self.vault).expanduser().resolve()


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
    if not path.exists():
        if required:
            raise SetupError("GSV is not configured. Run `gsv setup` first.")
        return None
    if path.is_symlink():
        raise ValidationError(f"configuration cannot be a symbolic link: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid GSV configuration: {path}") from exc
    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        raise ValidationError("unsupported GSV configuration version")
    vault = payload.get("vault")
    if not isinstance(vault, str) or not vault.strip():
        raise ValidationError("GSV configuration has no valid vault path")
    return Config(format_version=1, vault=vault)


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
