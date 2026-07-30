"""Strict UTC timestamps shared by portable connector records."""

from __future__ import annotations

from datetime import UTC, datetime

from continuity_kernel.errors import ValidationError


def format_utc(value: datetime) -> str:
    validate_utc(value, "timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{label} must be a UTC ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValidationError(f"{label} must be a UTC ISO-8601 timestamp") from exc
    validate_utc(parsed, label)
    return parsed


def validate_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{label} must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValidationError(f"{label} must be timezone-aware UTC")
    return value
