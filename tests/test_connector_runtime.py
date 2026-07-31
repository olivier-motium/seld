from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from continuity_kernel.connector_adapter import (
    ConnectorAdapterRegistry,
    ConnectorAdapterResult,
    ConnectorRuntimeCredential,
)
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_contract import ConnectorEffect, OperationSpec
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_session import ConnectorSession
from continuity_kernel.connector_transport import ConnectorTransport
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.vault import Vault

CONNECTION_ID = parse_connection_id("con-" + "r" * 32)


class _Adapter:
    providers = frozenset({"gmail"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object | None, str | None]] = []
        self.effect: ConnectorEffect | None = None
        self.continuation: object | None = None
        self.after_execute: Callable[[], None] | None = None

    def classify_effect(self, operation: OperationSpec, input_value: object) -> ConnectorEffect:
        del input_value
        return self.effect or operation.effect

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
        del credential, transport
        self.calls.append((operation.name, input_value, continuation, write_idempotency_key))
        if self.after_execute is not None:
            self.after_execute()
        return ConnectorAdapterResult(
            {"accepted": True, "operation": operation.name},
            continuation=self.continuation,
        )


def _prepared(
    tmp_path: Path,
    *,
    access: str = "full",
) -> tuple[Vault, ConnectorAuthManager, _Adapter, ConnectorRuntime]:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Connector runtime")
    profile = get_profile("google")
    now = datetime.now(UTC)
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider="google",
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(label="Alice at Example"),
        scopes=profile.scopes_for(access),
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-google-client",
            redirect_uris=("http://127.0.0.1:0",),
            authorization_endpoint=profile.authorization_endpoint,
            token_endpoint=profile.token_endpoint,
        ),
        health=ConnectionHealth.READY,
        created_at=now,
        updated_at=now,
        version=1,
        last_verified_at=now,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    stored = vault.get_connection_snapshot().connection(CONNECTION_ID)
    assert stored is not None
    manager.store_oauth_credential(
        CONNECTION_ID,
        OAuthCredential(
            access_token="runtime-access-token",
            refresh_token="runtime-refresh-token",
            token_type=OAuthTokenType.BEARER,
            scopes=stored.scopes,
            issued_at=now,
            expires_at=None,
        ),
        expected_token_version=0,
    )
    adapter = _Adapter()
    runtime = ConnectorRuntime(
        vault,
        adapters=ConnectorAdapterRegistry((adapter,)),
        auth_manager=manager,
        session=ConnectorSession(secret=b"s" * 32),
    )
    return vault, manager, adapter, runtime


def test_read_uses_process_local_cursor_bound_to_exact_input_and_state(tmp_path: Path) -> None:
    _vault, _manager, adapter, runtime = _prepared(tmp_path)
    adapter.continuation = {"page_token": "provider-private-page-two"}
    first = runtime.call_tool(
        "gsv_gmail_read",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {"page_size": 2},
            "operation": "messages.list",
        },
    )
    assert first["status"] == "ok"
    assert first["result"] == {"accepted": True, "operation": "messages.list"}
    cursor = first["cursor"]
    assert isinstance(cursor, str)
    assert "provider-private-page-two" not in cursor

    second = runtime.call_tool(
        "gsv_gmail_read",
        {
            "connection_id": str(CONNECTION_ID),
            "cursor": cursor,
            "input": {"page_size": 2},
            "operation": "messages.list",
        },
    )
    assert second["status"] == "ok"
    assert adapter.calls[-1][2] == {"page_token": "provider-private-page-two"}

    with pytest.raises(ConflictError, match="binding"):
        runtime.call_tool(
            "gsv_gmail_read",
            {
                "connection_id": str(CONNECTION_ID),
                "cursor": cursor,
                "input": {"page_size": 3},
                "operation": "messages.list",
            },
        )


def test_read_only_tier_rejects_write_before_secret_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, manager, adapter, runtime = _prepared(tmp_path, access="read")

    def fail_resolution(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("secret resolution was reached")

    monkeypatch.setattr(manager, "resolve_oauth_access_token_state", fail_resolution)
    with pytest.raises(ValidationError, match="Read-only"):
        runtime.call_tool(
            "gsv_gmail_write",
            {
                "connection_id": str(CONNECTION_ID),
                "input": {},
                "operation": "drafts.create",
            },
        )
    assert adapter.calls == []


def test_safe_mutation_executes_once_but_outward_effect_requires_bound_confirmation(
    tmp_path: Path,
) -> None:
    _vault, _manager, adapter, runtime = _prepared(tmp_path)
    safe = runtime.call_tool(
        "gsv_gmail_write",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {"subject": "Draft only", "text_body": "Not sent"},
            "operation": "drafts.create",
        },
    )
    assert safe["status"] == "ok"
    assert [call[0] for call in adapter.calls] == ["drafts.create"]

    values = {
        "connection_id": str(CONNECTION_ID),
        "input": {"draft_id": "draft-one"},
        "operation": "drafts.send",
    }
    preview = runtime.call_tool("gsv_gmail_write", values)
    assert preview["status"] == "confirmation_required"
    assert preview["account"] == "Alice at Example"
    assert preview["preview"] == {"draft_id": "draft-one"}
    assert [call[0] for call in adapter.calls] == ["drafts.create"]

    confirmed = runtime.call_tool(
        "gsv_gmail_write",
        {**values, "confirmation_token": preview["confirmation_token"]},
    )
    assert confirmed["status"] == "ok"
    assert [call[0] for call in adapter.calls] == ["drafts.create", "drafts.send"]
    assert adapter.calls[-1][3] is not None
    assert len(adapter.calls[-1][3] or "") == 22
    with pytest.raises(ConflictError, match="already been consumed"):
        runtime.call_tool(
            "gsv_gmail_write",
            {**values, "confirmation_token": preview["confirmation_token"]},
        )
    assert [call[0] for call in adapter.calls] == ["drafts.create", "drafts.send"]


def test_confirmation_is_bound_to_exact_mutation_and_adapter_cannot_downgrade(
    tmp_path: Path,
) -> None:
    _vault, _manager, adapter, runtime = _prepared(tmp_path)
    original = {
        "connection_id": str(CONNECTION_ID),
        "input": {"draft_id": "draft-one"},
        "operation": "drafts.send",
    }
    preview = runtime.call_tool("gsv_gmail_write", original)
    with pytest.raises(ConflictError, match="binding"):
        runtime.call_tool(
            "gsv_gmail_write",
            {
                **original,
                "confirmation_token": preview["confirmation_token"],
                "input": {"draft_id": "draft-two"},
            },
        )
    adapter.effect = ConnectorEffect.SAFE_MUTATION
    with pytest.raises(ValidationError, match="cannot downgrade"):
        runtime.call_tool("gsv_gmail_write", original)


def test_successful_mutation_reports_connection_change_without_inviting_retry(
    tmp_path: Path,
) -> None:
    vault, _manager, adapter, runtime = _prepared(tmp_path)

    def change_connection() -> None:
        snapshot = vault.get_connection_snapshot()
        connection = snapshot.connection(CONNECTION_ID)
        assert connection is not None
        vault.mark_connection_health(
            expected_revision=snapshot.revision,
            connection_id=CONNECTION_ID,
            health=ConnectionHealth.DEGRADED,
            observed_at=connection.updated_at + timedelta(microseconds=1),
        )

    adapter.after_execute = change_connection
    result = runtime.call_tool(
        "gsv_gmail_write",
        {
            "connection_id": str(CONNECTION_ID),
            "input": {"subject": "Draft"},
            "operation": "drafts.create",
        },
    )
    assert result["status"] == "completed_state_changed"
    assert result["do_not_retry"] is True
    assert "Review provider state" in str(result["warning"])
    assert len(adapter.calls) == 1


def test_unverified_connection_and_wrong_source_binding_fail_closed(tmp_path: Path) -> None:
    vault, _manager, adapter, runtime = _prepared(tmp_path)
    snapshot = vault.get_connection_snapshot()
    connection = snapshot.connection(CONNECTION_ID)
    assert connection is not None
    vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=CONNECTION_ID,
        health=ConnectionHealth.UNVERIFIED,
        observed_at=connection.updated_at + timedelta(microseconds=1),
    )
    with pytest.raises(ValidationError, match="verified"):
        runtime.call_tool(
            "gsv_gmail_read",
            {
                "connection_id": str(CONNECTION_ID),
                "input": {},
                "operation": "messages.list",
            },
        )
    assert adapter.calls == []
