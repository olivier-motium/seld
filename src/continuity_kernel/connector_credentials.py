"""Secret-only credential codecs used inside connector runtimes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from continuity_kernel.connector_oauth import OAuthTokenSet, OAuthTokenType
from continuity_kernel.connector_time import format_utc, parse_utc, validate_utc
from continuity_kernel.errors import ValidationError

OAUTH_CREDENTIAL_FORMAT_VERSION: Final = 1
MAX_OAUTH_CREDENTIAL_BYTES: Final = 1024 * 1024
MAX_TOKEN_TEXT_BYTES: Final = 256 * 1024


@dataclass(frozen=True, repr=False)
class OAuthCredential:
    """A persisted OAuth token set whose repr never reveals token material."""

    access_token: str
    refresh_token: str | None
    token_type: OAuthTokenType
    scopes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime | None
    format_version: int = OAUTH_CREDENTIAL_FORMAT_VERSION

    def __post_init__(self) -> None:
        _token_text(self.access_token, "access token")
        if self.refresh_token is not None:
            _token_text(self.refresh_token, "refresh token")
        if self.token_type is not OAuthTokenType.BEARER:
            raise ValidationError("OAuth credential token type is unsupported")
        if any(
            not scope or any(character.isspace() for character in scope) for scope in self.scopes
        ):
            raise ValidationError("OAuth credential scopes are invalid")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValidationError("OAuth credential scopes contain duplicates")
        validate_utc(self.issued_at, "OAuth credential issue time")
        if self.expires_at is not None:
            validate_utc(self.expires_at, "OAuth credential expiry time")
            if self.expires_at < self.issued_at:
                raise ValidationError("OAuth credential expires before it was issued")
        if self.format_version != OAUTH_CREDENTIAL_FORMAT_VERSION:
            raise ValidationError("OAuth credential format version is unsupported")

    def __repr__(self) -> str:
        return (
            "OAuthCredential(access_token=<redacted>, refresh_token="
            f"{'<present>' if self.refresh_token is not None else '<absent>'}, "
            f"expires_at={self.expires_at!r})"
        )

    def usable_at(self, observed_at: datetime, *, minimum_validity_seconds: int = 60) -> bool:
        validate_utc(observed_at, "OAuth credential observation time")
        if minimum_validity_seconds < 0:
            raise ValidationError("minimum OAuth validity must be non-negative")
        return self.expires_at is None or self.expires_at > observed_at + timedelta(
            seconds=minimum_validity_seconds
        )

    def to_bytes(self) -> bytes:
        encoded = (
            json.dumps(
                {
                    "access_token": self.access_token,
                    "expires_at": format_utc(self.expires_at) if self.expires_at else None,
                    "format_version": self.format_version,
                    "issued_at": format_utc(self.issued_at),
                    "refresh_token": self.refresh_token,
                    "scopes": list(self.scopes),
                    "token_type": self.token_type.value,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_OAUTH_CREDENTIAL_BYTES:
            raise ValidationError("OAuth credential exceeds its size bound")
        return encoded

    @classmethod
    def from_bytes(cls, encoded: bytes) -> OAuthCredential:
        if not encoded or len(encoded) > MAX_OAUTH_CREDENTIAL_BYTES:
            raise ValidationError("OAuth credential is empty or exceeds its size bound")
        try:
            value = cast(object, json.loads(encoded.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("OAuth credential is invalid JSON") from exc
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ValidationError("OAuth credential has an unsupported shape")
        payload = cast(dict[str, object], value)
        if set(payload) != {
            "access_token",
            "expires_at",
            "format_version",
            "issued_at",
            "refresh_token",
            "scopes",
            "token_type",
        }:
            raise ValidationError("OAuth credential has an unsupported shape")
        scopes = payload["scopes"]
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            raise ValidationError("OAuth credential scopes are invalid")
        token_type = payload["token_type"]
        if token_type != OAuthTokenType.BEARER.value:
            raise ValidationError("OAuth credential token type is unsupported")
        refresh_token = payload["refresh_token"]
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise ValidationError("OAuth credential refresh token is invalid")
        expires_at = payload["expires_at"]
        return cls(
            access_token=_token_text(payload["access_token"], "access token"),
            refresh_token=(
                None if refresh_token is None else _token_text(refresh_token, "refresh token")
            ),
            token_type=OAuthTokenType.BEARER,
            scopes=tuple(cast(list[str], scopes)),
            issued_at=parse_utc(payload["issued_at"], "OAuth credential issue time"),
            expires_at=(
                None
                if expires_at is None
                else parse_utc(expires_at, "OAuth credential expiry time")
            ),
            format_version=_format_version(payload["format_version"]),
        )


def credential_from_token_set(
    token_set: OAuthTokenSet,
    *,
    issued_at: datetime | None = None,
    previous: OAuthCredential | None = None,
) -> OAuthCredential:
    observed_at = (issued_at or datetime.now(UTC)).astimezone(UTC)
    scopes = (
        token_set.scopes if token_set.scopes is not None else (previous.scopes if previous else ())
    )
    refresh_token = token_set.refresh_token
    if refresh_token is None and previous is not None:
        refresh_token = previous.refresh_token
    expires_at = (
        observed_at + timedelta(seconds=token_set.expires_in_seconds)
        if token_set.expires_in_seconds is not None
        else None
    )
    return OAuthCredential(
        access_token=token_set.access_token,
        refresh_token=refresh_token,
        token_type=token_set.token_type,
        scopes=scopes,
        issued_at=observed_at,
        expires_at=expires_at,
    )


def _token_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_TOKEN_TEXT_BYTES:
        raise ValidationError(f"OAuth credential {label} is empty or too large")
    if "\x00" in value:
        raise ValidationError(f"OAuth credential {label} contains a null byte")
    return value


def _format_version(value: object) -> int:
    if value != OAUTH_CREDENTIAL_FORMAT_VERSION:
        raise ValidationError("OAuth credential format version is unsupported")
    return OAUTH_CREDENTIAL_FORMAT_VERSION
