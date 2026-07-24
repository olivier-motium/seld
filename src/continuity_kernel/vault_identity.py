"""One strict parser for the canonical logical-vault identity record."""

from __future__ import annotations

import json
import re
from typing import Any, Final

from continuity_kernel.errors import ValidationError
from continuity_kernel.records import stored_time, title_text

VAULT_FORMAT_VERSION: Final = 1
VAULT_MANIFEST_KEYS: Final = frozenset({"created_at", "format_version", "name", "vault_id"})
REQUIRED_VAULT_DIRECTORIES: Final = (
    ".gsv",
    ".gsv/locks",
    "tasks",
    "entities",
    "threads",
    "journal",
    "backups",
)
VAULT_ID: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def canonical_vault_id(value: object) -> str:
    """Return one canonical UUID-shaped vault ID or fail closed."""

    if not isinstance(value, str) or VAULT_ID.fullmatch(value) is None:
        raise ValidationError("vault manifest has an invalid vault identity")
    return value


def parse_vault_manifest(encoded: bytes) -> dict[str, Any]:
    """Parse the exact canonical manifest schema shared by every vault surface."""

    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("vault manifest is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != VAULT_MANIFEST_KEYS:
        raise ValidationError("vault manifest has an unsupported shape")
    version = payload.get("format_version")
    if type(version) is not int or version != VAULT_FORMAT_VERSION:
        raise ValidationError("unsupported vault version")
    vault_id = canonical_vault_id(payload.get("vault_id"))
    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        raise ValidationError("vault manifest has an invalid creation time")
    try:
        created_at = stored_time(created_at, "vault creation time")
    except ValidationError as exc:
        raise ValidationError("vault manifest has an invalid creation time") from exc
    if created_at != payload["created_at"]:
        raise ValidationError("vault manifest creation time is not canonical")
    name = payload.get("name")
    if not isinstance(name, str):
        raise ValidationError("vault manifest has an invalid name")
    try:
        clean_name = title_text(name)
    except ValidationError as exc:
        raise ValidationError("vault manifest has an invalid name") from exc
    if clean_name != name:
        raise ValidationError("vault manifest name is not canonical")
    return {
        "created_at": created_at,
        "format_version": version,
        "name": clean_name,
        "vault_id": vault_id,
    }
