"""Shared runtime boundary implemented by finite provider adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from continuity_kernel.connector_contract import (
    ConnectorEffect,
    OperationSpec,
    canonicalize_json,
)
from continuity_kernel.connector_transport import ConnectorCredential, ConnectorTransport
from continuity_kernel.errors import ValidationError


@dataclass(frozen=True, repr=False)
class ConnectorRuntimeCredential:
    """One transient provider credential plus its exact current grant facts."""

    credential: ConnectorCredential
    granted_scopes: tuple[str, ...]
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.credential, ConnectorCredential):
            raise ValidationError("connector runtime credential is invalid")
        if (
            not isinstance(self.granted_scopes, tuple)
            or len(self.granted_scopes) > 256
            or any(
                not isinstance(scope, str)
                or not scope
                or len(scope) > 2_048
                or any(character.isspace() for character in scope)
                for scope in self.granted_scopes
            )
            or len(set(self.granted_scopes)) != len(self.granted_scopes)
        ):
            raise ValidationError("connector runtime scopes are invalid")
        if type(self.version) is not int or self.version < 1:
            raise ValidationError("connector runtime credential version is invalid")

    def __repr__(self) -> str:
        return (
            "ConnectorRuntimeCredential(credential=<redacted>, "
            f"granted_scopes={self.granted_scopes!r}, version={self.version!r})"
        )


@dataclass(frozen=True)
class ConnectorAdapterResult:
    """Bounded JSON output and an optional provider-owned continuation."""

    payload: object
    continuation: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", canonicalize_json(self.payload))
        if self.continuation is not None:
            object.__setattr__(
                self,
                "continuation",
                canonicalize_json(self.continuation),
            )


class ConnectorAdapter(Protocol):
    """Closed provider implementation. It never receives raw caller transport."""

    @property
    def providers(self) -> frozenset[str]: ...

    def classify_effect(self, operation: OperationSpec, input_value: object) -> ConnectorEffect: ...

    def execute(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
    ) -> ConnectorAdapterResult: ...


class ConnectorAdapterRegistry:
    """Immutable exact provider-to-adapter lookup."""

    def __init__(self, adapters: Sequence[ConnectorAdapter]) -> None:
        if not isinstance(adapters, Sequence) or not adapters:
            raise ValidationError("connector adapter registry cannot be empty")
        by_provider: dict[str, ConnectorAdapter] = {}
        for adapter in adapters:
            providers = adapter.providers
            if not isinstance(providers, frozenset) or not providers:
                raise ValidationError("connector adapter has no providers")
            for provider in providers:
                if not isinstance(provider, str) or not provider:
                    raise ValidationError("connector adapter provider is invalid")
                if provider in by_provider:
                    raise ValidationError("connector adapter provider is duplicated")
                by_provider[provider] = adapter
        self._by_provider: Mapping[str, ConnectorAdapter] = MappingProxyType(by_provider)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_provider))

    def get(self, provider: str) -> ConnectorAdapter:
        try:
            return self._by_provider[provider]
        except KeyError as exc:
            raise ValidationError("connector provider has no runtime adapter") from exc
