from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from continuity_kernel.connector_adapter import ConnectorAdapterRegistry
from continuity_kernel.connector_adapter_microsoft import MicrosoftConnectorAdapter
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_session import ConnectorSession
from continuity_kernel.connector_transfer import ArtifactStore
from continuity_kernel.connector_transport import (
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.vault import Vault

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="descriptor-pinned connector transfer is POSIX-only",
)

_CONNECTION_ID = parse_connection_id("con-" + "m" * 32)


class _RuntimeTransport(ConnectorTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.stream_requests: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.requests.append(kwargs)
        path = kwargs["path"]
        if path in {
            "/v1.0/me/messages/message-1/attachments",
            "/v1.0/me/calendar/events/event-1/attachments",
        }:
            return ConnectorResponse(
                kwargs["origin"],
                201,
                {},
                b'{"id":"runtime-uploaded"}',
            )
        if path == "/v1.0/me/calendar/events/event-1":
            return ConnectorResponse(
                kwargs["origin"],
                200,
                {},
                (
                    b'{"attendees":[],"body":{"content":"Existing",'
                    b'"contentType":"html"},"changeKey":"event-version-1",'
                    b'"id":"event-1","isOnlineMeeting":false,"isOrganizer":true}'
                ),
            )
        raise AssertionError(f"unexpected Microsoft provider route: {path}")

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        self.stream_requests.append(kwargs)
        raise AssertionError("runtime proof should use one direct upload")

    def download_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        del kwargs
        raise AssertionError("runtime confirmation proof should not download")


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Vault, ConnectorRuntime, _RuntimeTransport]:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Microsoft transfer runtime")
    profile = get_profile("microsoft")
    now = datetime.now(UTC)
    scopes = profile.scopes_for("full")
    connection = ConnectionMetadata(
        connection_id=_CONNECTION_ID,
        provider="microsoft",
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(
            fingerprint="sha256:" + "d" * 64,
            label="Configured Outlook Full",
        ),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="public-microsoft-client",
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
        connection=connection,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    manager.ensure_imported_credential(
        connection,
        OAuthCredential(
            access_token="runtime-outlook-token",
            refresh_token="runtime-refresh-token",
            token_type=OAuthTokenType.BEARER,
            scopes=scopes,
            issued_at=now,
            expires_at=None,
        ).to_bytes(),
    )
    transport = _RuntimeTransport()
    runtime = ConnectorRuntime(
        vault,
        adapters=ConnectorAdapterRegistry((MicrosoftConnectorAdapter(),)),
        auth_manager=manager,
        transport=transport,
        session=ConnectorSession(secret=b"m" * 32),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    return vault, runtime, transport


def _attachment_values(
    vault: Vault,
    root: Path,
    *,
    provider: str = "outlook_mail",
) -> dict[str, object]:
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    grant = vault.grant_local_file_root(root)["grant"]
    grant_id = grant["grant_id"]
    assert isinstance(grant_id, str)
    attachment = {
        "content_type": "text/plain",
        "local_file": {"grant_id": grant_id, "relative_path": "payload.txt"},
        "name": "payload.txt",
    }
    input_value: dict[str, object]
    if provider == "outlook_mail":
        input_value = {"attachment": attachment, "message_id": "message-1"}
    else:
        input_value = {
            "attachment": attachment,
            "calendar_id": "primary",
            "change_key": "event-version-1",
            "event_id": "event-1",
        }
    return {
        "connection_id": str(_CONNECTION_ID),
        "input": input_value,
        "operation": "attachments.add",
    }


def test_outlook_runtime_binds_confirmation_to_snapshot_and_replays_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    root = tmp_path / "selected"
    root.mkdir()
    source = root / "payload.txt"
    original = b"snapshot before confirmation"
    source.write_bytes(original)
    values = _attachment_values(vault, root)
    try:
        preview = runtime.call_tool("gsv_outlook_mail_write", values)
        token = preview["confirmation_token"]
        assert preview["status"] == "confirmation_required"
        assert isinstance(token, str)
        assert transport.requests == []

        input_value = cast(dict[str, object], values["input"])
        attachment = cast(dict[str, object], input_value["attachment"])
        tampered_input = {
            **input_value,
            "attachment": {
                **attachment,
                "local_file": {
                    **cast(dict[str, object], attachment["local_file"]),
                    "relative_path": "different.txt",
                },
            },
        }
        with pytest.raises(ConflictError, match="binding"):
            runtime.call_tool(
                "gsv_outlook_mail_write",
                {**values, "confirmation_token": token, "input": tampered_input},
            )
        assert transport.requests == []

        source.write_bytes(b"changed after preview")
        completed = runtime.call_tool(
            "gsv_outlook_mail_write",
            {**values, "confirmation_token": token},
        )
        assert completed["status"] == "ok"
        assert len(transport.requests) == 1
        body = cast(dict[str, object], transport.requests[0]["json_body"])
        assert base64.b64decode(cast(str, body["contentBytes"])) == original
        assert transport.requests[0]["credential"].secret == "runtime-outlook-token"
        assert transport.stream_requests == []

        with pytest.raises(ConflictError, match=r"unavailable|expired|replayed"):
            runtime.call_tool(
                "gsv_outlook_mail_write",
                {**values, "confirmation_token": token},
            )
        assert len(transport.requests) == 1
    finally:
        runtime.close()


def test_outlook_calendar_runtime_binds_preflight_and_snapshot_to_one_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime, transport = _runtime(tmp_path, monkeypatch)
    root = tmp_path / "calendar-selected"
    root.mkdir()
    source = root / "payload.txt"
    original = b"calendar snapshot before confirmation"
    source.write_bytes(original)
    values = _attachment_values(runtime.vault, root, provider="outlook_calendar")
    attachment_path = "/v1.0/me/calendar/events/event-1/attachments"
    try:
        preview = runtime.call_tool("gsv_outlook_calendar_write", values)
        token = preview["confirmation_token"]
        assert preview["status"] == "confirmation_required"
        assert isinstance(token, str)
        assert all(request["path"] != attachment_path for request in transport.requests)

        input_value = cast(dict[str, object], values["input"])
        attachment = cast(dict[str, object], input_value["attachment"])
        with pytest.raises(ConflictError, match="binding"):
            runtime.call_tool(
                "gsv_outlook_calendar_write",
                {
                    **values,
                    "confirmation_token": token,
                    "input": {
                        **input_value,
                        "attachment": {
                            **attachment,
                            "local_file": {
                                **cast(dict[str, object], attachment["local_file"]),
                                "relative_path": "different.txt",
                            },
                        },
                    },
                },
            )
        assert all(request["path"] != attachment_path for request in transport.requests)

        source.write_bytes(b"calendar file changed after preview")
        completed = runtime.call_tool(
            "gsv_outlook_calendar_write",
            {**values, "confirmation_token": token},
        )
        assert completed["status"] == "ok"
        writes = [request for request in transport.requests if request["path"] == attachment_path]
        assert len(writes) == 1
        body = cast(dict[str, object], writes[0]["json_body"])
        assert base64.b64decode(cast(str, body["contentBytes"])) == original
        assert writes[0]["headers"] == {
            "If-Match": "event-version-1",
            "Prefer": 'IdType="ImmutableId"',
        }

        with pytest.raises(ConflictError, match=r"unavailable|expired|replayed"):
            runtime.call_tool(
                "gsv_outlook_calendar_write",
                {**values, "confirmation_token": token},
            )
        assert (
            len([request for request in transport.requests if request["path"] == attachment_path])
            == 1
        )
    finally:
        runtime.close()


def test_outlook_oversized_local_snapshot_fails_before_confirmation_or_provider_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    root = tmp_path / "oversized"
    root.mkdir()
    with (root / "payload.txt").open("wb") as stream:
        stream.truncate(150 * 1024**2 + 1)
    values = _attachment_values(vault, root)
    try:
        with pytest.raises(ValidationError, match="provider operation's size limit"):
            runtime.call_tool("gsv_outlook_mail_write", values)
        assert transport.requests == []
        assert transport.stream_requests == []
    finally:
        runtime.close()
