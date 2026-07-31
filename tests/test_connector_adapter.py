from __future__ import annotations

import pytest

from continuity_kernel.connector_adapter import (
    ConnectorAdapterRegistry,
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
)
from continuity_kernel.connector_contract import ConnectorEffect, OperationSpec
from continuity_kernel.connector_transport import (
    AuthorizationScheme,
    ConnectorCredential,
    ConnectorTransport,
)
from continuity_kernel.errors import ValidationError


class _Adapter:
    def __init__(self, *providers: str) -> None:
        self._providers = frozenset(providers)

    @property
    def providers(self) -> frozenset[str]:
        return self._providers

    def classify_effect(self, operation: OperationSpec, input_value: object) -> ConnectorEffect:
        del input_value
        return operation.effect

    def execute(
        self,
        operation: OperationSpec,
        input_value: object,
        *,
        continuation: object | None,
        credential: ConnectorRuntimeCredential,
        transport: ConnectorTransport,
        write_idempotency_key: str | None = None,
    ) -> ConnectorAdapterResult:
        del operation, input_value, continuation, credential, transport, write_idempotency_key
        return ConnectorAdapterResult({"ok": True})


def test_adapter_registry_is_exact_and_rejects_duplicate_providers() -> None:
    google = _Adapter("gmail", "google_calendar", "google_drive")
    slack = _Adapter("slack")
    registry = ConnectorAdapterRegistry((google, slack))
    assert registry.providers == ("gmail", "google_calendar", "google_drive", "slack")
    assert registry.get("gmail") is google
    assert registry.get("slack") is slack
    with pytest.raises(ValidationError, match="duplicated"):
        ConnectorAdapterRegistry((google, _Adapter("gmail")))
    with pytest.raises(ValidationError, match="no runtime adapter"):
        registry.get("discord")


def test_runtime_credential_and_adapter_result_are_bounded_and_redacted() -> None:
    credential = ConnectorRuntimeCredential(
        credential=ConnectorCredential(
            scheme=AuthorizationScheme.BEARER,
            secret="provider-secret",
        ),
        granted_scopes=("Mail.Read",),
        version=3,
    )
    assert "provider-secret" not in repr(credential)
    assert credential.version == 3

    original = {"items": [{"id": "one"}]}
    result = ConnectorAdapterResult(original, continuation={"offset": 1})
    original["items"][0]["id"] = "changed"
    assert result.payload == {"items": [{"id": "one"}]}
    assert result.continuation == {"offset": 1}

    with pytest.raises(ValidationError, match="runtime scopes"):
        ConnectorRuntimeCredential(
            credential=credential.credential,
            granted_scopes=("scope with whitespace",),
            version=1,
        )
