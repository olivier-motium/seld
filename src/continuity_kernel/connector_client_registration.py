"""Strict, non-secret public OAuth client registrations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlsplit

from continuity_kernel.errors import SetupError, ValidationError

CLIENT_REGISTRATION_SCHEMA_VERSION: Final = 1
MAX_CLIENT_REGISTRATION_BYTES: Final = 64 * 1024

_RESOURCE_NAME: Final = "resources/connector_clients.json"
_PROVIDERS: Final = frozenset({"google", "microsoft", "slack"})
_LOOPBACK_IPS: Final = frozenset({"127.0.0.1", "::1"})
_MAX_CLIENT_ID_BYTES: Final = 2 * 1024
_MAX_REDIRECT_BYTES: Final = 2 * 1024


@dataclass(frozen=True)
class PublicClientRegistration:
    """One public client ID and its provider-approved redirect template."""

    provider: str
    client_id: str
    redirect_template: str
    client_secret_required: bool = False

    def __post_init__(self) -> None:
        provider = _provider(self.provider)
        _client_id(self.client_id)
        _redirect_template(provider, self.redirect_template)
        object.__setattr__(self, "provider", provider)


def load_public_client_registrations(
    *,
    source_path: str | Path | None = None,
    source_bytes: bytes | None = None,
) -> Mapping[str, PublicClientRegistration]:
    """Load the optional packaged catalog or an explicit test source."""

    encoded = _read_source(source_path=source_path, source_bytes=source_bytes)
    payload = _parse_json(encoded)
    providers = _mapping(payload.get("providers"), "connector client providers")
    if len(providers) > len(_PROVIDERS):
        raise ValidationError("connector client provider catalog exceeds its bound")
    result: dict[str, PublicClientRegistration] = {}
    for provider, value in providers.items():
        if provider not in _PROVIDERS:
            raise ValidationError("connector client provider is unsupported")
        entry = _mapping(value, "connector client registration")
        _registration_keys(entry)
        secret_required = entry.get("client_secret_required", False)
        if type(secret_required) is not bool:
            raise ValidationError("client secret requirement must be true or false")
        result[provider] = PublicClientRegistration(
            provider=provider,
            client_id=_client_id(entry["client_id"]),
            redirect_template=_redirect_template(
                provider,
                _text(entry["redirect_template"], "redirect template", _MAX_REDIRECT_BYTES),
            ),
            client_secret_required=secret_required,
        )
    return result


def load_public_client_registration(
    provider: str,
    *,
    source_path: str | Path | None = None,
    source_bytes: bytes | None = None,
) -> PublicClientRegistration:
    """Load one built-in public registration without accepting a client ID."""

    clean_provider = _provider(provider)
    registrations = load_public_client_registrations(
        source_path=source_path,
        source_bytes=source_bytes,
    )
    registration = registrations.get(clean_provider)
    if registration is None:
        raise SetupError(
            f"{clean_provider} sign-in is unavailable in this build; "
            "no public client registration is configured and nothing was saved"
        )
    return registration


def require_public_client_registrations(
    *,
    source_path: str | Path | None = None,
    source_bytes: bytes | None = None,
) -> tuple[str, ...]:
    """Fail a release unless every supported OAuth provider is registered.

    The return value deliberately contains provider names only. Client IDs are
    public, but release logs and readiness output do not need to expose them.
    """

    registrations = load_public_client_registrations(
        source_path=source_path,
        source_bytes=source_bytes,
    )
    missing = sorted(_PROVIDERS.difference(registrations))
    if missing:
        raise SetupError(
            "release is blocked because public OAuth client registrations are missing for: "
            + ", ".join(missing)
        )
    return tuple(sorted(registrations))


def _read_source(
    *,
    source_path: str | Path | None,
    source_bytes: bytes | None,
) -> bytes:
    if source_path is not None and source_bytes is not None:
        raise ValidationError("connector client registration has conflicting sources")
    if source_bytes is not None:
        if type(source_bytes) is not bytes:
            raise ValidationError("connector client registration source is invalid")
        return source_bytes
    if source_path is not None:
        try:
            with Path(source_path).open("rb") as handle:
                return handle.read(MAX_CLIENT_REGISTRATION_BYTES + 1)
        except (OSError, TypeError) as exc:
            raise _unavailable_error() from exc
    try:
        with files("continuity_kernel").joinpath(_RESOURCE_NAME).open("rb") as handle:
            return handle.read(MAX_CLIENT_REGISTRATION_BYTES + 1)
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise _unavailable_error() from exc


def _parse_json(encoded: bytes) -> dict[str, object]:
    if not encoded or len(encoded) > MAX_CLIENT_REGISTRATION_BYTES:
        raise ValidationError("connector client registration is empty or exceeds its size bound")
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("connector client registration is not valid JSON") from exc
    payload = _mapping(value, "connector client registration")
    _exact_keys(payload, {"schema_version", "providers"}, "connector client registration")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != CLIENT_REGISTRATION_SCHEMA_VERSION
    ):
        raise ValidationError("connector client registration schema version is unsupported")
    return payload


def _provider(value: object) -> str:
    if not isinstance(value, str) or value.casefold() not in _PROVIDERS:
        raise ValidationError("connector client provider is unsupported")
    return value.casefold()


def _client_id(value: object) -> str:
    return _text(value, "public client ID", _MAX_CLIENT_ID_BYTES, reject_whitespace=True)


def _redirect_template(provider: str, value: object) -> str:
    redirect = _text(value, "redirect template", _MAX_REDIRECT_BYTES)
    try:
        parsed = urlsplit(redirect)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"{provider} redirect template is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(f"{provider} redirect template is invalid")
    host = parsed.hostname.casefold()
    if provider == "google":
        valid = (
            parsed.scheme == "http" and host in _LOOPBACK_IPS and port == 0 and parsed.path == ""
        )
    elif provider == "microsoft":
        # Microsoft public desktop clients ignore the ephemeral port only for
        # localhost redirect URIs. Numeric loopback hosts require an exact
        # registered port and therefore cannot support Seld's dynamic listener.
        valid = (
            parsed.scheme == "http"
            and host == "localhost"
            and port == 0
            and parsed.path == "/oauth/callback"
        )
    else:
        valid = (
            parsed.scheme in {"http", "https"}
            and host == "localhost"
            and port not in {None, 0}
            and parsed.path == "/oauth/callback"
        )
    if not valid:
        raise ValidationError(f"{provider} redirect template is invalid")
    return redirect


def _text(
    value: object,
    label: str,
    max_bytes: int,
    *,
    reject_whitespace: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValidationError(f"{label} is invalid")
    if reject_whitespace and any(character.isspace() for character in value):
        raise ValidationError(f"{label} is invalid")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValidationError(f"{label} is too large")
    return value


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValidationError(f"{label} is invalid")
    return cast(dict[str, object], value)


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        if any(key.casefold() in {"client_secret", "secret"} for key in value):
            raise ValidationError("client secrets are not accepted")
        raise ValidationError(f"{label} has an unsupported shape")


def _registration_keys(value: Mapping[str, object]) -> None:
    required = {"client_id", "redirect_template"}
    allowed = required | {"client_secret_required"}
    keys = set(value)
    if any(key.casefold() in {"client_secret", "secret"} for key in keys):
        raise ValidationError("client secrets are not accepted")
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise ValidationError("connector client registration has an unsupported shape")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _unavailable_error() -> SetupError:
    return SetupError(
        "sign-in is unavailable in this build; no public client registration is configured "
        "and nothing was saved"
    )
