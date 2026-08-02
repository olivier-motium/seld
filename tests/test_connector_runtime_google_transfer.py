from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from continuity_kernel import connector_gmail_transfer as gmail_transfer
from continuity_kernel.connector_adapter import ConnectorAdapterRegistry
from continuity_kernel.connector_adapter_google import GoogleConnectorAdapter
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_contract import ConnectorEffect
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_runtime import ConnectorRuntime
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_session import ConnectorSession
from continuity_kernel.connector_transfer import ArtifactStore, PreparedUpload
from continuity_kernel.connector_transport import (
    ConnectorMethod,
    ConnectorOutcomeUnknown,
    ConnectorResponse,
    ConnectorStreamResponse,
    ConnectorTransport,
)
from continuity_kernel.errors import ConflictError, ValidationError
from continuity_kernel.vault import Vault

_CONNECTION_ID = parse_connection_id("con-" + "g" * 32)


class _NoProviderHttp(ConnectorTransport):
    def __init__(self) -> None:
        self.call_count = 0

    def _fail(self) -> None:
        self.call_count += 1
        raise AssertionError("provider HTTP must not run during transfer preview")

    def request(self, **kwargs: Any) -> ConnectorResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")

    def request_provider_location(self, **kwargs: Any) -> ConnectorResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")

    def download_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        del kwargs
        self._fail()
        raise AssertionError("unreachable")


class _DriveLroTransport(ConnectorTransport):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = [
            {
                "name": "operations/download-1",
                "metadata": {"resourceKey": "provider-key"},
            },
            {
                "done": True,
                "name": "operations/download-1",
                "metadata": {"resourceKey": "provider-key"},
                "response": {
                    "@type": "type.googleapis.com/google.apps.drive.v3.DownloadFileResponse",
                    "downloadUri": ("https://drive.usercontent.google.com/download?ticket=runtime"),
                    "partialDownloadAllowed": False,
                },
            },
        ]

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append({"kind": "request", **kwargs})
        body = json.dumps(self._responses.pop(0)).encode()
        return ConnectorResponse(kwargs["origin"], 200, {}, body)

    def download_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        self.calls.append({"kind": "download_stream", **kwargs})
        body = b"runtime-video"
        sink = kwargs["sink"]
        sink.write(body)
        artifact = sink.finish()
        return ConnectorStreamResponse(
            kwargs["origin"],
            200,
            {},
            len(body),
            hashlib.sha256(body).hexdigest(),
            artifact=artifact,
        )


class _GmailModifyTransport(ConnectorTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.requests.append(kwargs)
        return ConnectorResponse(
            kwargs["origin"],
            200,
            {},
            b'{"id":"message-1","labelIds":["TRASH"]}',
        )


class _GmailSettingsTransport(ConnectorTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.requests.append(kwargs)
        return ConnectorResponse(kwargs["origin"], 200, {}, b'{"displayLanguage":"fr"}')


class _GmailFilterDeleteTransport(ConnectorTransport):
    def __init__(self, filter_resource: dict[str, object]) -> None:
        self.filter_resource = filter_resource
        self.requests: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.requests.append(kwargs)
        body = b"{}"
        if kwargs["method"] is ConnectorMethod.GET:
            body = json.dumps(self.filter_resource).encode()
        return ConnectorResponse(kwargs["origin"], 200, {}, body)


class _AmbiguousGmailSendTransport(_GmailModifyTransport):
    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.requests.append(kwargs)
        raise ConnectorOutcomeUnknown("Gmail send outcome is unknown")


class _GmailRawUploadTransport(ConnectorTransport):
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.ambiguous = ambiguous
        self.calls: list[dict[str, Any]] = []
        self.sent_bodies: list[bytes] = []

    def request(self, **kwargs: Any) -> ConnectorResponse:
        self.calls.append({"kind": "request", **kwargs})
        return ConnectorResponse(
            kwargs["origin"],
            200,
            {"location": "https://gmail.googleapis.com/upload/session/runtime-one"},
            b"{}",
        )

    def request_stream(self, **kwargs: Any) -> ConnectorStreamResponse:
        self.calls.append({"kind": "stream", **kwargs})
        source = kwargs["source"]
        assert isinstance(source, PreparedUpload)
        body = b"".join(source.iter_chunks())
        self.sent_bodies.append(body)
        if self.ambiguous:
            raise ConnectorOutcomeUnknown("ambiguous raw Gmail dispatch")
        return ConnectorStreamResponse(
            kwargs["origin"],
            200,
            {},
            len(body),
            hashlib.sha256(body).hexdigest(),
            control_body=b'{"id":"message-raw","threadId":"thread-raw","snippet":"private"}',
        )


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_permanent_delete: bool = False,
    legacy_full: bool = False,
) -> tuple[Vault, ConnectorRuntime, _NoProviderHttp]:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host-data"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Google transfer runtime")
    profile = get_profile("google")
    now = datetime.now(UTC)
    full_scopes = (
        profile.legacy_full_scopes[0]
        if legacy_full
        else profile.scopes_for(
            "full",
            include_supplemental=include_permanent_delete,
        )
    )
    connection = ConnectionMetadata(
        connection_id=_CONNECTION_ID,
        provider="google",
        source_ids=profile.source_ids,
        credential_kind=profile.credential_kind,
        account=AccountMetadata(
            fingerprint="sha256:" + "c" * 64,
            label="Configured Google Full",
        ),
        scopes=full_scopes,
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
        connection=connection,
        observed_at=now,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / "host-state",
    )
    credential = OAuthCredential(
        access_token="runtime-access-token",
        refresh_token="runtime-refresh-token",
        token_type=OAuthTokenType.BEARER,
        scopes=full_scopes,
        issued_at=now,
        expires_at=None,
    )
    manager.ensure_imported_credential(connection, credential.to_bytes())
    transport = _NoProviderHttp()
    runtime = ConnectorRuntime(
        vault,
        adapters=ConnectorAdapterRegistry((GoogleConnectorAdapter(),)),
        auth_manager=manager,
        transport=transport,
        session=ConnectorSession(secret=b"g" * 32),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )
    return vault, runtime, transport


def _write_sized_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)


def _fail_confirmation_issue(*args: object, **kwargs: object) -> str:
    del args, kwargs
    raise AssertionError("confirmation must not be issued for an oversized Gmail MIME upload")


def test_gmail_send_near_limit_attachment_fails_before_confirmation_or_provider_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "near-limit-root"
        _write_sized_file(selected / "near-limit.bin", 26_818_981)
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        monkeypatch.setattr(runtime.session, "issue_confirmation", _fail_confirmation_issue)

        with pytest.raises(ValidationError, match="documented provider upload limit"):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {
                        "attachments": [
                            {
                                "filename": "near-limit.bin",
                                "local_file": {
                                    "grant_id": grant_id,
                                    "relative_path": "near-limit.bin",
                                },
                                "mime_type": "application/octet-stream",
                            }
                        ],
                        "text_body": "Draft body",
                        "to": ["recipient@example.test"],
                    },
                    "operation": "messages.send",
                },
            )
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_aggregate_local_attachments_fail_before_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "aggregate-root"
        _write_sized_file(selected / "first.bin", 13_409_491)
        _write_sized_file(selected / "second.bin", 13_409_490)
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        monkeypatch.setattr(runtime.session, "issue_confirmation", _fail_confirmation_issue)

        with pytest.raises(ValidationError, match="documented provider upload limit"):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {
                        "attachments": [
                            {
                                "filename": "first.bin",
                                "local_file": {
                                    "grant_id": grant_id,
                                    "relative_path": "first.bin",
                                },
                                "mime_type": "application/octet-stream",
                            },
                            {
                                "filename": "second.bin",
                                "local_file": {
                                    "grant_id": grant_id,
                                    "relative_path": "second.bin",
                                },
                                "mime_type": "application/octet-stream",
                            },
                        ],
                        "text_body": "Draft body",
                    },
                    "operation": "drafts.create",
                },
            )
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_confirmed_replay_rechecks_mime_limit_before_consuming_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "replay-root"
        _write_sized_file(selected / "small.bin", 64)
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        values = {
            "connection_id": str(_CONNECTION_ID),
            "input": {
                "attachments": [
                    {
                        "filename": "small.bin",
                        "local_file": {
                            "grant_id": grant_id,
                            "relative_path": "small.bin",
                        },
                        "mime_type": "application/octet-stream",
                    }
                ],
                "text_body": "Draft body",
            },
            "operation": "drafts.create",
        }
        preview = runtime.call_tool("gsv_gmail_write", values)
        token = preview["confirmation_token"]
        assert isinstance(token, str)
        assert transport.call_count == 0

        consumed = False

        def record_consume(*args: object, **kwargs: object) -> str:
            nonlocal consumed
            consumed = True
            del args, kwargs
            raise AssertionError("confirmation must not be consumed after MIME validation fails")

        monkeypatch.setattr(runtime.session, "consume_confirmation", record_consume)
        monkeypatch.setattr(gmail_transfer, "GMAIL_UPLOAD_MAX_BYTES", 1)

        with pytest.raises(ValidationError, match="documented provider upload limit"):
            runtime.call_tool("gsv_gmail_write", {**values, "confirmation_token": token})
        assert consumed is False
        assert runtime.prepared_uploads.peek(token) is not None
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_google_transfer_previews_reach_confirmation_without_provider_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        selected = tmp_path / "granted-root"
        selected.mkdir()
        (selected / "gmail.txt").write_bytes(b"gmail attachment")
        (selected / "drive.bin").write_bytes(b"drive upload")
        vault.select_sources(
            expected_revision=vault.get_source_snapshot().revision,
            sources=("local_files",),
        )
        grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
        assert isinstance(grant_id, str)
        gmail = runtime.call_tool(
            "gsv_gmail_write",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": {
                    "attachments": [
                        {
                            "filename": "gmail.txt",
                            "local_file": {
                                "grant_id": grant_id,
                                "relative_path": "gmail.txt",
                            },
                            "mime_type": "text/plain",
                        }
                    ],
                    "text_body": "Draft body",
                },
                "operation": "drafts.create",
            },
        )
        drive = runtime.call_tool(
            "gsv_google_drive_write",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": {
                    "local_file": {
                        "grant_id": grant_id,
                        "relative_path": "drive.bin",
                    },
                    "mime_type": "application/octet-stream",
                    "name": "drive.bin",
                },
                "operation": "files.create",
            },
        )
        assert gmail["status"] == "confirmation_required"
        assert gmail["effect"] == ConnectorEffect.OUTWARD.value
        assert gmail["provider"] == "gmail"
        assert drive["status"] == "confirmation_required"
        assert drive["effect"] == ConnectorEffect.OUTWARD.value
        assert drive["provider"] == "google_drive"
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_raw_send_preview_binds_every_recipient_and_the_immutable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, preview_transport = _runtime(tmp_path, monkeypatch)
    selected = tmp_path / "raw-send-root"
    source = selected / "message.eml"
    source.parent.mkdir()
    reviewed = (
        b"From: Sender <sender@example.test>\r\n"
        b"Sender: delegate@example.test\r\n"
        b"Reply-To: replies@example.test\r\n"
        b"To: Alice <alice@example.test>\r\n"
        b"To: bob@example.test\r\n"
        b"Cc: carol@example.test\r\n"
        b"Bcc: hidden-one@example.test\r\n"
        b"Bcc: Hidden Two <hidden-two@example.test>\r\n"
        b"Subject: Reviewed raw send\r\n\r\nPrivate reviewed body"
    )
    source.write_bytes(reviewed)
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    assert isinstance(grant_id, str)
    values = {
        "connection_id": str(_CONNECTION_ID),
        "input": {
            "local_file": {
                "grant_id": grant_id,
                "relative_path": "message.eml",
            }
        },
        "operation": "messages.send",
    }
    try:
        preview = runtime.call_tool("gsv_gmail_write", values)
        assert preview["status"] == "confirmation_required"
        assert preview["effect"] == ConnectorEffect.OUTWARD.value
        assert preview_transport.call_count == 0
        public_preview = preview["preview"]
        assert isinstance(public_preview, dict)
        prepared = public_preview["prepared_content"]
        assert isinstance(prepared, dict)
        raw = prepared["gmail_raw_message"]
        assert isinstance(raw, dict)
        assert raw["headers_parsed"] is True
        assert raw["from"] == "Sender <sender@example.test>"
        assert raw["sender"] == "delegate@example.test"
        assert raw["reply_to"] == "replies@example.test"
        assert raw["subject"] == "Reviewed raw send"
        assert raw["to"] == ["Alice <alice@example.test>", "bob@example.test"]
        assert raw["cc"] == ["carol@example.test"]
        assert raw["bcc"] == [
            "hidden-one@example.test",
            "Hidden Two <hidden-two@example.test>",
        ]
        assert "Private reviewed body" not in json.dumps(preview)

        source.write_bytes(
            b"To: attacker@example.test\r\nSubject: Changed\r\n\r\nChanged after approval"
        )
        upload_transport = _GmailRawUploadTransport()
        runtime.transport = upload_transport
        completed = runtime.call_tool(
            "gsv_gmail_write",
            {**values, "confirmation_token": preview["confirmation_token"]},
        )
        assert completed["status"] == "ok"
        assert completed["result"] == {
            "id": "message-raw",
            "threadId": "thread-raw",
        }
        assert upload_transport.sent_bodies == [reviewed]
        assert [call["kind"] for call in upload_transport.calls] == ["request", "stream"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("operation", "content", "input_extra", "error"),
    (
        (
            "messages.send",
            b"Subject: No recipient\r\n\r\nBody",
            {},
            "at least one concrete",
        ),
        (
            "messages.send",
            b"To: recipient@example.test\nSubject: Bare LF\n\nBody",
            {},
            "headers are invalid",
        ),
        (
            "messages.import",
            b"From: sender@example.test\r\nTo: owner@example.test\r\n\r\nBody",
            {},
            "use internal_date_source=receivedTime",
        ),
    ),
)
def test_gmail_invalid_raw_message_fails_before_confirmation_or_provider_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    content: bytes,
    input_extra: dict[str, object],
    error: str,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    selected = tmp_path / "invalid-raw-root"
    source = selected / "message.eml"
    source.parent.mkdir()
    source.write_bytes(content)
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    issued: list[object] = []

    def record_confirmation(**kwargs: object) -> str:
        issued.append(kwargs)
        return "unexpected-token"

    monkeypatch.setattr(runtime.session, "issue_confirmation", record_confirmation)
    try:
        with pytest.raises(ValidationError, match=error):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {
                        **input_extra,
                        "local_file": {
                            "grant_id": grant_id,
                            "relative_path": "message.eml",
                        },
                    },
                    "operation": operation,
                },
            )
        assert issued == []
        assert transport.call_count == 0
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("operation", "input_extra", "content", "effect", "consequence"),
    (
        (
            "messages.insert",
            {"internal_date_source": "receivedTime"},
            b"Legacy header without a colon\r\n\r\nLegacy body",
            ConnectorEffect.OUTWARD,
            "without sending",
        ),
        (
            "messages.import",
            {"never_mark_spam": True, "process_for_calendar": True},
            (b"Date: Sat, 1 Aug 2026 10:00:00 +0200\r\nFrom: sender@example.test\r\n\r\nBody"),
            ConnectorEffect.OUTWARD,
            "Google Calendar",
        ),
        (
            "messages.insert",
            {"deleted": True, "internal_date_source": "receivedTime"},
            b"Legacy header without a colon\r\n\r\nLegacy body",
            ConnectorEffect.PERMANENT,
            "permanently deleted",
        ),
    ),
)
def test_gmail_migration_preview_explains_dynamic_effects_and_legacy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    input_extra: dict[str, object],
    content: bytes,
    effect: ConnectorEffect,
    consequence: str,
) -> None:
    vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    selected = tmp_path / f"{operation}-root"
    source = selected / "message.eml"
    source.parent.mkdir()
    source.write_bytes(content)
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    try:
        preview = runtime.call_tool(
            "gsv_gmail_write",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": {
                    **input_extra,
                    "local_file": {
                        "grant_id": grant_id,
                        "relative_path": "message.eml",
                    },
                },
                "operation": operation,
            },
        )
        assert preview["status"] == "confirmation_required"
        assert preview["effect"] == effect.value
        public_preview = preview["preview"]
        assert isinstance(public_preview, dict)
        prepared_content = public_preview["prepared_content"]
        assert isinstance(prepared_content, dict)
        raw = prepared_content["gmail_raw_message"]
        assert isinstance(raw, dict)
        assert raw["effective_internal_date_source"] == input_extra.get(
            "internal_date_source",
            "dateHeader" if operation == "messages.import" else "receivedTime",
        )
        assert any(consequence in item for item in raw["consequences"])
        if content.startswith(b"Legacy"):
            assert raw["headers_parsed"] is False
            assert raw["warnings"]
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_ambiguous_raw_send_spends_confirmation_and_never_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, runtime, _preview_transport = _runtime(tmp_path, monkeypatch)
    selected = tmp_path / "ambiguous-raw-root"
    source = selected / "message.eml"
    source.parent.mkdir()
    source.write_bytes(b"To: recipient@example.test\r\nSubject: One shot\r\n\r\nBody")
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision,
        sources=("local_files",),
    )
    grant_id = vault.grant_local_file_root(selected)["grant"]["grant_id"]
    values = {
        "connection_id": str(_CONNECTION_ID),
        "input": {
            "local_file": {
                "grant_id": grant_id,
                "relative_path": "message.eml",
            }
        },
        "operation": "messages.send",
    }
    try:
        preview = runtime.call_tool("gsv_gmail_write", values)
        token = preview["confirmation_token"]
        transport = _GmailRawUploadTransport(ambiguous=True)
        runtime.transport = transport
        with pytest.raises(ConnectorOutcomeUnknown, match="do not retry automatically"):
            runtime.call_tool(
                "gsv_gmail_write",
                {**values, "confirmation_token": token},
            )
        assert [call["kind"] for call in transport.calls] == ["request", "stream"]
        assert len(transport.sent_bodies) == 1

        with pytest.raises(ConflictError, match=r"consumed|unavailable|expired|replayed"):
            runtime.call_tool(
                "gsv_gmail_write",
                {**values, "confirmation_token": token},
            )
        assert [call["kind"] for call in transport.calls] == ["request", "stream"]
    finally:
        runtime.close()


def test_gmail_settings_confirmation_effects_are_bound_without_provider_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    cases = (
        (
            "settings.filters.create",
            {
                "action": {"forward": "archive@example.test"},
                "criteria": {"from": "sender@example.test"},
            },
            ConnectorEffect.OUTWARD,
            "future matching messages",
        ),
        (
            "settings.filters.create",
            {
                "action": {"add_label_ids": ["TRASH"]},
                "criteria": {"from": "sender@example.test"},
            },
            ConnectorEffect.DESTRUCTIVE,
            "future matching messages",
        ),
        (
            "settings.imap.update",
            {
                "auto_expunge": True,
                "enabled": True,
                "expunge_behavior": "deleteForever",
                "max_folder_size": 0,
            },
            ConnectorEffect.PERMANENT,
            "unrecoverable",
        ),
        (
            "settings.pop.update",
            {"access_window": "allMail", "disposition": "trash"},
            ConnectorEffect.DESTRUCTIVE,
            "future POP retrieval",
        ),
        (
            "settings.vacation.update",
            {
                "enable_auto_reply": True,
                "end_time": None,
                "response_body_html": "",
                "response_body_plain_text": "Back Monday",
                "response_subject": "Away",
                "restrict_to_contacts": False,
                "restrict_to_domain": False,
                "start_time": None,
            },
            ConnectorEffect.OUTWARD,
            "future qualifying messages",
        ),
    )
    try:
        for operation, input_value, expected_effect, warning in cases:
            preview = runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": input_value,
                    "operation": operation,
                },
            )
            assert preview["status"] == "confirmation_required"
            assert preview["effect"] == expected_effect.value
            assert preview["preview"] == input_value
            returned_warning = preview["warning"]
            assert isinstance(returned_warning, str)
            assert warning in returned_warning
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_filter_delete_preview_shows_and_rechecks_the_exact_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, _preview_transport = _runtime(tmp_path, monkeypatch)
    expected_filter = {
        "action": {"addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]},
        "criteria": {"from": "sender@example.test", "subject": "Daily status"},
        "id": "filter-1",
    }
    transport = _GmailFilterDeleteTransport(expected_filter)
    runtime.transport = transport
    input_value = {"expected_filter": expected_filter, "filter_id": "filter-1"}
    try:
        preview = runtime.call_tool(
            "gsv_gmail_write",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": input_value,
                "operation": "settings.filters.delete",
            },
        )

        assert preview["status"] == "confirmation_required"
        assert preview["effect"] == ConnectorEffect.PERMANENT.value
        assert preview["preview"] == input_value
        assert [request["method"] for request in transport.requests] == [ConnectorMethod.GET]

        completed = runtime.call_tool(
            "gsv_gmail_write",
            {
                "confirmation_token": preview["confirmation_token"],
                "connection_id": str(_CONNECTION_ID),
                "input": input_value,
                "operation": "settings.filters.delete",
            },
        )

        assert completed["status"] == "ok"
        assert [request["method"] for request in transport.requests] == [
            ConnectorMethod.GET,
            ConnectorMethod.GET,
            ConnectorMethod.GET,
            ConnectorMethod.DELETE,
        ]
        assert {request["path"] for request in transport.requests} == {
            "/gmail/v1/users/me/settings/filters/filter-1"
        }
    finally:
        runtime.close()


def test_gmail_safe_setting_update_is_one_step_and_uses_settings_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, _preview_transport = _runtime(tmp_path, monkeypatch)
    transport = _GmailSettingsTransport()
    runtime.transport = transport
    try:
        result = runtime.call_tool(
            "gsv_gmail_write",
            {
                "connection_id": str(_CONNECTION_ID),
                "input": {"display_language": "fr"},
                "operation": "settings.language.update",
            },
        )
        assert result["status"] == "ok"
        assert result["effect"] == ConnectorEffect.SAFE_MUTATION.value
        assert result["result"] == {"displayLanguage": "fr"}
        assert len(transport.requests) == 1
        assert transport.requests[0]["method"] is ConnectorMethod.PUT
        assert transport.requests[0]["path"] == "/gmail/v1/users/me/settings/language"
    finally:
        runtime.close()


def test_legacy_gmail_full_keeps_mail_authority_but_settings_upgrade_fails_actionably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, transport = _runtime(tmp_path, monkeypatch, legacy_full=True)
    try:
        with pytest.raises(
            ValidationError,
            match=r"reconnect Gmail Full access.*existing connection keeps working",
        ):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {"display_language": "fr"},
                    "operation": "settings.language.update",
                },
            )
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_recoverable_modify_executes_without_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, _unused = _runtime(tmp_path, monkeypatch)
    transport = _GmailModifyTransport()
    runtime.transport = transport
    values = {
        "connection_id": str(_CONNECTION_ID),
        "input": {"add_label_ids": ["TRASH"], "message_id": "message-1"},
        "operation": "messages.modify",
    }
    try:
        completed = runtime.call_tool(
            "gsv_gmail_write",
            values,
        )
        assert completed["status"] == "ok"
        assert completed["effect"] == ConnectorEffect.SAFE_MUTATION.value
        assert len(transport.requests) == 1
        assert transport.requests[0]["method"] is ConnectorMethod.POST
        assert transport.requests[0]["path"] == ("/gmail/v1/users/me/messages/message-1/modify")
        assert transport.requests[0]["json_body"] == {"addLabelIds": ["TRASH"]}
    finally:
        runtime.close()


def test_gmail_send_confirmation_binds_exact_message_and_is_single_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, _unused = _runtime(tmp_path, monkeypatch)
    transport = _GmailModifyTransport()
    runtime.transport = transport
    values = {
        "connection_id": str(_CONNECTION_ID),
        "input": {
            "subject": "Bound send",
            "text_body": "Hello",
            "to": ["recipient@example.test"],
        },
        "operation": "messages.send",
    }
    try:
        preview = runtime.call_tool("gsv_gmail_write", values)
        token = preview["confirmation_token"]
        assert preview["status"] == "confirmation_required"
        assert preview["effect"] == ConnectorEffect.OUTWARD.value
        assert isinstance(token, str)
        assert transport.requests == []

        with pytest.raises(ConflictError, match="confirmation binding does not match"):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    **values,
                    "confirmation_token": token,
                    "input": {
                        "subject": "Bound send",
                        "text_body": "Hello",
                        "to": ["recipient@example.test", "other@example.test"],
                    },
                },
            )
        assert transport.requests == []

        completed = runtime.call_tool(
            "gsv_gmail_write",
            {**values, "confirmation_token": token},
        )
        assert completed["status"] == "ok"
        assert completed["effect"] == ConnectorEffect.OUTWARD.value
        assert len(transport.requests) == 1
        assert transport.requests[0]["path"] == "/gmail/v1/users/me/messages/send"

        with pytest.raises(ConflictError, match=r"consumed|unavailable|expired|replayed"):
            runtime.call_tool(
                "gsv_gmail_write",
                {**values, "confirmation_token": token},
            )
        assert len(transport.requests) == 1
    finally:
        runtime.close()


def test_gmail_invalid_send_fails_before_confirmation_is_issued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    confirmation_issued = False

    def issue_confirmation(*args: object, **kwargs: object) -> str:
        nonlocal confirmation_issued
        confirmation_issued = True
        del args, kwargs
        raise AssertionError("invalid send must not issue confirmation")

    monkeypatch.setattr(runtime.session, "issue_confirmation", issue_confirmation)
    try:
        for input_value in (
            {"text_body": "No recipient"},
            {
                "text_body": "Invalid thread",
                "thread_id": "caller-supplied-thread",
                "to": ["recipient@example.test"],
            },
        ):
            with pytest.raises(ValidationError):
                runtime.call_tool(
                    "gsv_gmail_write",
                    {
                        "connection_id": str(_CONNECTION_ID),
                        "input": input_value,
                        "operation": "messages.send",
                    },
                )
        assert confirmation_issued is False
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_ambiguous_send_is_not_retried_and_spends_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, _unused = _runtime(tmp_path, monkeypatch)
    transport = _AmbiguousGmailSendTransport()
    runtime.transport = transport
    values = {
        "connection_id": str(_CONNECTION_ID),
        "input": {"text_body": "Hello", "to": ["recipient@example.test"]},
        "operation": "messages.send",
    }
    try:
        preview = runtime.call_tool("gsv_gmail_write", values)
        token = preview["confirmation_token"]
        assert isinstance(token, str)
        with pytest.raises(ConnectorOutcomeUnknown):
            runtime.call_tool(
                "gsv_gmail_write",
                {**values, "confirmation_token": token},
            )
        assert len(transport.requests) == 1

        with pytest.raises(ConflictError, match=r"consumed|unavailable|expired|replayed"):
            runtime.call_tool(
                "gsv_gmail_write",
                {**values, "confirmation_token": token},
            )
        assert len(transport.requests) == 1
    finally:
        runtime.close()


def test_gmail_batch_purge_requires_separate_permanent_delete_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, transport = _runtime(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValidationError, match="permanent delete is not enabled"):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    "connection_id": str(_CONNECTION_ID),
                    "input": {"message_ids": ["message-1"]},
                    "operation": "messages.batch_purge",
                },
            )
        assert transport.call_count == 0
    finally:
        runtime.close()


def test_gmail_batch_purge_confirmation_binds_exact_message_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, _unused = _runtime(
        tmp_path,
        monkeypatch,
        include_permanent_delete=True,
    )
    transport = _GmailModifyTransport()
    runtime.transport = transport
    values = {
        "connection_id": str(_CONNECTION_ID),
        "input": {"message_ids": ["message-1", "message-2"]},
        "operation": "messages.batch_purge",
    }
    try:
        preview = runtime.call_tool("gsv_gmail_write", values)
        token = preview["confirmation_token"]
        assert preview["effect"] == ConnectorEffect.PERMANENT.value
        assert isinstance(token, str)

        with pytest.raises(ConflictError, match="confirmation binding does not match"):
            runtime.call_tool(
                "gsv_gmail_write",
                {
                    **values,
                    "confirmation_token": token,
                    "input": {"message_ids": ["message-1"]},
                },
            )
        assert transport.requests == []

        completed = runtime.call_tool(
            "gsv_gmail_write",
            {**values, "confirmation_token": token},
        )
        assert completed["effect"] == ConnectorEffect.PERMANENT.value
        assert len(transport.requests) == 1
        assert transport.requests[0]["path"] == "/gmail/v1/users/me/messages/batchDelete"
        assert transport.requests[0]["json_body"] == {"ids": ["message-1", "message-2"]}
    finally:
        runtime.close()


def test_drive_lro_runtime_hides_provider_state_and_resumes_into_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, runtime, _unused = _runtime(tmp_path, monkeypatch)
    transport = _DriveLroTransport()
    runtime.transport = transport
    envelope = {
        "connection_id": str(_CONNECTION_ID),
        "input": {
            "delivery": "artifact",
            "file_id": "file_1",
            "filename": "clip.mp4",
            "mime_type": "video/mp4",
        },
        "operation": "files.download",
    }
    try:
        pending = runtime.call_tool("gsv_google_drive_read", envelope)
        assert pending["result"] == {"retry_after_seconds": 10, "status": "pending"}
        cursor = pending["cursor"]
        assert isinstance(cursor, str)
        assert "download-1" not in cursor
        assert "provider-key" not in cursor
        assert "artifact" not in pending
        assert [call["kind"] for call in transport.calls] == ["request"]

        completed = runtime.call_tool(
            "gsv_google_drive_read",
            {**envelope, "cursor": cursor},
        )
        assert completed["result"] == {
            "bytes": len(b"runtime-video"),
            "delivery": "artifact",
        }
        assert "cursor" not in completed
        artifact = completed["artifact"]
        assert isinstance(artifact, dict)
        assert Path(str(artifact["path"])).read_bytes() == b"runtime-video"
        assert [call["kind"] for call in transport.calls] == [
            "request",
            "request",
            "download_stream",
        ]
        assert transport.calls[1]["headers"] == {
            "X-Goog-Drive-Resource-Keys": "file_1/provider-key"
        }
        assert transport.calls[2]["credential"] is None
        assert transport.calls[2]["google_drive_resource_key"] == (
            "file_1",
            "provider-key",
        )
    finally:
        runtime.close()
