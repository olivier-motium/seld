from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import continuity_kernel.slack_tasks as slack_tasks
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.slack_tasks import SlackTaskReader
from continuity_kernel.vault import Vault

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
CONNECTION_ID = parse_connection_id("con-" + "s" * 32)
CHANNEL_ID = "C123456789"
USER_ID = "U123456789"
MESSAGE_TS = "1786356000.000001"


class _Runtime:
    def __init__(self) -> None:
        self.closed = False

    def call_tool(self, name: str, values: dict[str, object]) -> dict[str, object]:
        assert name == "gsv_slack_read"
        assert values["connection_id"] == str(CONNECTION_ID)
        operation = values["operation"]
        if operation == "identity.get":
            result: dict[str, object] = {
                "ok": True,
                "team": "Synthetic workspace",
                "team_id": "T123456789",
                "user": "synthetic-user",
                "user_id": USER_ID,
            }
        elif operation == "search.messages":
            result = {
                "ok": True,
                "messages": {
                    "matches": [
                        {
                            "channel": {"id": CHANNEL_ID, "name": "project"},
                            "text": "A bounded synthetic message",
                            "ts": MESSAGE_TS,
                            "user": USER_ID,
                            "username": "Teammate",
                        }
                    ],
                    "pagination": {
                        "page": 1,
                        "page_count": 1,
                        "total_count": 1,
                    },
                },
            }
        elif operation in {"messages.list", "threads.list"}:
            result = {
                "ok": True,
                "messages": [
                    {
                        "text": "Expanded synthetic context",
                        "ts": MESSAGE_TS,
                        "user": USER_ID,
                        "username": "Teammate",
                    }
                ],
            }
        else:
            raise AssertionError(f"unexpected operation: {operation}")
        return {
            "connection_id": str(CONNECTION_ID),
            "effect": "read",
            "operation": operation,
            "provider": "slack",
            "result": result,
            "status": "ok",
        }

    def close(self) -> None:
        self.closed = True


def _reader(tmp_path: Path) -> tuple[Vault, SlackTaskReader]:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Slack task reader")
    profile = get_profile("slack")
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider="slack",
        source_ids=("slack",),
        credential_kind=CredentialKind.OAUTH2,
        account=AccountMetadata(fingerprint="sha256:" + "a" * 64, label="Synthetic Slack"),
        scopes=profile.read_scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="synthetic-client",
            redirect_uris=("http://localhost:43127/oauth/callback",),
            authorization_endpoint=cast(str, profile.authorization_endpoint),
            token_endpoint=cast(str, profile.token_endpoint),
        ),
        health=ConnectionHealth.READY,
        created_at=NOW,
        updated_at=NOW,
        version=1,
        last_verified_at=NOW,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=NOW,
    )
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("slack",),
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "connector-state",
    )
    reader = SlackTaskReader(
        vault,
        auth_manager=manager,
        runtime_factory=lambda: cast(ConnectorRuntime, _Runtime()),
        now=lambda: NOW,
    )
    return vault, reader


def test_task_shaped_slack_reads_use_portable_connection_and_opaque_context(
    tmp_path: Path,
) -> None:
    _vault, reader = _reader(tmp_path)

    assert reader.status() == {
        "account_label": "Synthetic Slack",
        "access": "read",
        "available": True,
        "identity_verified": True,
        "mode": "read_only",
        "provider": "slack",
        "user_name": "synthetic-user",
        "workspace_name": "Synthetic workspace",
    }
    inbox = reader.inbox(since="2026-08-01", max_pages=1, max_results=10)
    groups = cast(list[dict[str, object]], inbox["groups"])
    message = cast(list[dict[str, object]], groups[0]["messages"])[0]
    reference = cast(str, message["ref"])
    context = reader.context(reference, before=1, after=1)

    assert reference.startswith("slack:v1:")
    assert context["focus_ref"] == reference
    assert len(cast(list[dict[str, object]], context["messages"])) == 1
    rendered = repr((inbox, context))
    assert CHANNEL_ID not in rendered
    assert USER_ID not in rendered


def test_slack_poll_records_the_canonical_source_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, reader = _reader(tmp_path)
    source_revision = vault.get_source_snapshot().revision

    monkeypatch.setattr(
        slack_tasks,
        "read_connector_source",
        lambda *_args, **_kwargs: {
            "items": [{"text": "bounded", "timestamp": "2026-08-10T10:00:00Z"}],
            "record": {
                "accountBinding": "synthetic-account",
                "completeness": "partial",
                "coveredThrough": "2026-08-10T10:00:00Z",
                "errorCode": None,
                "evidenceRefs": ["synthetic-evidence"],
                "result": "success",
                "source": "slack",
                "toolBinding": "synthetic-tool",
            },
            "result": "success",
            "sourceRevision": source_revision,
        },
    )

    result = reader.poll(limit=25)

    assert result["status"] == "success"
    slack = next(
        item
        for item in cast(list[dict[str, object]], vault.source_status()["sources"])
        if cast(dict[str, object], item["recipe"])["source"] == "slack"
    )
    assert slack["freshness"] == "current"
