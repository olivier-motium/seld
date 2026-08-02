from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

import continuity_kernel.connector_http as connector_http
import continuity_kernel.connector_sources as connector_sources
from continuity_kernel.connections import render_connection_snapshot
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_credentials import OAuthCredential
from continuity_kernel.connector_identifiers import ConnectionId, parse_connection_id
from continuity_kernel.connector_oauth import OAuthTokenEndpointError, OAuthTokenType
from continuity_kernel.connector_profiles import get_profile
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_sources import read_connector_source
from continuity_kernel.errors import ConflictError, SetupError, ValidationError
from continuity_kernel.vault import Vault

BASE_TIME = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
TOKEN = "synthetic-access-token"
PRIVATE_BODY = "private-provider-body"


def _prepared(
    tmp_path: Path,
    *,
    source_id: str,
    provider: str,
    marker: str,
    token_endpoint: str | None = None,
) -> tuple[Vault, ConnectorAuthManager, ConnectionId]:
    vault = Vault(tmp_path / f"vault-{marker}")
    vault.initialize(name="Synthetic connector test")
    connection_id = parse_connection_id("con-" + marker * 32)
    if provider == "google":
        authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        default_token_endpoint = "https://oauth2.googleapis.com/token"
    elif provider == "microsoft":
        authorization_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        default_token_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    else:
        assert provider == "slack"
        authorization_endpoint = "https://slack.com/oauth/v2_user/authorize"
        default_token_endpoint = "https://slack.com/api/oauth.v2.user.access"
    scopes = get_profile(provider).read_scopes
    metadata = ConnectionMetadata(
        connection_id=connection_id,
        provider=provider,
        source_ids=(source_id,),
        credential_kind=CredentialKind.OAUTH2,
        account=AccountMetadata(
            fingerprint="sha256:" + "a" * 64,
            label="Synthetic account",
        ),
        scopes=scopes,
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="synthetic-client",
            redirect_uris=(
                "http://localhost:49152/oauth/callback"
                if provider == "slack"
                else (
                    "http://127.0.0.1:49152"
                    if provider == "google"
                    else "http://127.0.0.1:49152/callback"
                ),
            ),
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint or default_token_endpoint,
        ),
        health=ConnectionHealth.READY,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
        last_verified_at=BASE_TIME,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=BASE_TIME,
    )
    vault.select_sources(
        expected_revision=vault.get_source_snapshot().revision, sources=(source_id,)
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / f"tokens-{marker}",
    )
    credential = OAuthCredential(
        access_token=TOKEN,
        refresh_token=None if provider == "slack" else f"{provider}-refresh-token",
        token_type=OAuthTokenType.BEARER,
        scopes=scopes,
        issued_at=BASE_TIME,
        expires_at=None,
    )
    manager.ensure_imported_credential(metadata, credential.to_bytes())
    return vault, manager, connection_id


def _install_reader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vault: Vault,
    manager: ConnectorAuthManager,
    get_json: Callable[[str, Mapping[str, str], float], object],
) -> None:
    def manager_for_vault(observed_vault: Vault) -> ConnectorAuthManager:
        assert observed_vault is vault
        return manager

    monkeypatch.setattr(connector_sources, "ConnectorAuthManager", manager_for_vault)
    monkeypatch.setattr(connector_sources, "http_get_json", get_json)


def _record_delivery(vault: Vault, delivery: dict[str, object]) -> None:
    record = delivery["record"]
    assert isinstance(record, dict)
    evidence_refs = record["evidenceRefs"]
    assert isinstance(evidence_refs, list)
    vault.record_source_observation(
        expected_revision=str(delivery["sourceRevision"]),
        source_id=str(record["source"]),
        actor_ref="synthetic-connector-test",
        result=str(record["result"]),
        covered_through=str(record["coveredThrough"]),
        completeness=str(record["completeness"]),
        account_binding=str(record["accountBinding"]),
        tool_binding=str(record["toolBinding"]),
        evidence_refs=tuple(str(item) for item in evidence_refs),
    )


def test_google_gmail_delivery_records_only_hashed_receipt_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="gmail",
        provider="google",
        marker="a",
    )
    calls: list[tuple[str, Mapping[str, str], float]] = []

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        calls.append((url, headers, timeout))
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        if url.endswith("/profile"):
            return {"emailAddress": "account@example.test"}
        if url.endswith("/messages?maxResults=2"):
            return {"messages": [{"id": "provider-message-1"}]}
        if "/messages/provider-message-1?" in url:
            return {
                "id": "provider-message-1",
                "snippet": "bounded provider snippet",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Synthetic subject"},
                        {"name": "From", "value": "sender@example.test"},
                        {"name": "Date", "value": "Thu, 30 Jul 2026 09:00:00 +0000"},
                    ]
                },
                "body": PRIVATE_BODY,
            }
        pytest.fail("unexpected fixed Gmail endpoint")

    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=get_json,
    )
    actual_state = manager.tokens.state
    state_lock_timeouts: list[float] = []

    def state_with_budget(
        connection: ConnectionId,
        *,
        lock_timeout_seconds: float = 10.0,
    ) -> object:
        state_lock_timeouts.append(lock_timeout_seconds)
        return actual_state(connection, lock_timeout_seconds=lock_timeout_seconds)

    monkeypatch.setattr(manager.tokens, "state", state_with_budget)
    delivery = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="gmail",
        limit=2,
        observed_at=BASE_TIME,
        timeout_seconds=3.0,
    )

    assert delivery["result"] == "success"
    items = delivery["items"]
    assert isinstance(items, list)
    assert items == [
        {
            "subject": "Synthetic subject",
            "from": "sender@example.test",
            "date": "Thu, 30 Jul 2026 09:00:00 +0000",
            "snippet": "bounded provider snippet",
        }
    ]
    _record_delivery(vault, delivery)
    stored = (vault.root / "SOURCES.md").read_text(encoding="utf-8")
    for private_value in (
        TOKEN,
        "account@example.test",
        "provider-message-1",
        "Synthetic subject",
        "sender@example.test",
        PRIVATE_BODY,
    ):
        assert private_value not in stored
    assert len(calls) == 3
    timeouts = [call[2] for call in calls]
    assert all(0 < timeout <= 3.0 for timeout in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)
    assert len(state_lock_timeouts) == 1
    assert 0 <= state_lock_timeouts[0] <= 3.0


def test_microsoft_mail_uses_fixed_bounds_and_hashes_partial_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="outlook_mail",
        provider="microsoft",
        marker="b",
    )
    calls: list[str] = []
    private_next_link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=private-page"

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del headers, timeout
        calls.append(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path == "/v1.0/me":
            assert query == {"$select": ["id,displayName,userPrincipalName,mail"]}
            return {"id": "microsoft-account-id"}
        if parsed.path == "/v1.0/me/messages":
            assert query["$top"] == ["2"]
            assert query["$select"] == ["id,subject,receivedDateTime,from,isRead"]
            assert query["$orderby"] == ["receivedDateTime desc"]
            return {
                "value": [
                    {
                        "id": "provider-mail-id",
                        "subject": "Synthetic mail",
                        "receivedDateTime": "2026-07-30T09:00:00Z",
                        "from": {
                            "emailAddress": {
                                "name": "Synthetic sender",
                                "address": "sender@example.test",
                            }
                        },
                        "isRead": False,
                    }
                ],
                "@odata.nextLink": private_next_link,
            }
        pytest.fail("unexpected fixed Microsoft endpoint")

    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=get_json,
    )
    delivery = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="outlook_mail",
        limit=2,
        observed_at=BASE_TIME,
    )

    assert delivery["result"] == "success"
    record = delivery["record"]
    assert isinstance(record, dict)
    assert record["completeness"] == "partial"
    assert "cursor" not in record
    assert private_next_link not in json.dumps(delivery, sort_keys=True)
    assert "provider-mail-id" not in json.dumps(delivery, sort_keys=True)
    assert len(calls) == 2


def test_connection_provider_is_rejected_before_secret_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _manager, connection_id = _prepared(
        tmp_path,
        source_id="gmail",
        provider="microsoft",
        marker="c",
    )

    def fail_manager(observed_vault: Vault) -> ConnectorAuthManager:
        del observed_vault
        pytest.fail("secret resolution was reached")

    def fail_get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del url, headers, timeout
        pytest.fail("network transport was reached")

    monkeypatch.setattr(connector_sources, "ConnectorAuthManager", fail_manager)
    monkeypatch.setattr(connector_sources, "http_get_json", fail_get_json)
    with pytest.raises(ValidationError, match="provider"):
        read_connector_source(
            vault,
            connection_id=str(connection_id),
            source_id="gmail",
        )


def test_untrusted_oauth_endpoint_is_rejected_before_secret_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _manager, connection_id = _prepared(
        tmp_path,
        source_id="gmail",
        provider="google",
        marker="g",
        token_endpoint="https://attacker.example/token",
    )

    def fail_manager(observed_vault: Vault) -> ConnectorAuthManager:
        del observed_vault
        pytest.fail("secret resolution was reached")

    monkeypatch.setattr(connector_sources, "ConnectorAuthManager", fail_manager)
    with pytest.raises(ValidationError, match="OAuth endpoints"):
        read_connector_source(
            vault,
            connection_id=str(connection_id),
            source_id="gmail",
        )


def test_write_scope_is_rejected_before_secret_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _manager, connection_id = _prepared(
        tmp_path,
        source_id="slack",
        provider="slack",
        marker="q",
    )
    snapshot = vault.get_connection_snapshot()
    connection = snapshot.connection(connection_id)
    assert connection is not None
    unsafe = replace(connection, scopes=(*connection.scopes, "chat:write"))
    (vault.root / "CONNECTIONS.md").write_text(
        render_connection_snapshot(replace(snapshot, connections=(unsafe,))),
        encoding="utf-8",
    )
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123456789")

    def fail_manager(observed_vault: Vault) -> ConnectorAuthManager:
        del observed_vault
        pytest.fail("secret resolution was reached")

    monkeypatch.setattr(connector_sources, "ConnectorAuthManager", fail_manager)
    with pytest.raises(ValidationError, match="built-in access tier"):
        read_connector_source(
            vault,
            connection_id=str(connection_id),
            source_id="slack",
        )


def test_keyring_failure_returns_only_fixed_auth_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="gmail",
        provider="google",
        marker="h",
    )

    def fail_resolve(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise SetupError("private keyring backend detail")

    def fail_transport(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del url, headers, timeout
        pytest.fail("provider transport was reached")

    monkeypatch.setattr(manager, "resolve_oauth_access_token_state", fail_resolve)
    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=fail_transport,
    )
    failure = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="gmail",
    )
    assert failure["result"] == "failure"
    assert failure["errorCode"] == "auth_required"
    assert "private keyring" not in json.dumps(failure, sort_keys=True)


def test_slack_invalid_refresh_token_returns_expired_auth_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="slack",
        provider="slack",
        marker="r",
    )
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123456789")

    def invalid_refresh(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OAuthTokenEndpointError(
            error="invalid_refresh_token",
            description=None,
            status_code=200,
        )

    monkeypatch.setattr(manager, "resolve_oauth_access_token_state", invalid_refresh)
    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=lambda *_args: pytest.fail("provider read was reached"),
    )

    failure = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="slack",
    )

    assert failure["result"] == "failure"
    assert failure["errorCode"] == "auth_expired"


def test_gmail_uses_one_deadline_across_followup_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="gmail",
        provider="google",
        marker="i",
    )
    calls: list[str] = []

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del headers, timeout
        calls.append(url)
        if url.endswith("/profile"):
            return {"emailAddress": "account@example.test"}
        pytest.fail("expired operation budget allowed a followup request")

    times = iter((100.0, 100.0, 101.0, 102.0, 116.0, 116.0))
    monkeypatch.setattr(connector_sources, "monotonic", lambda: next(times))
    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=get_json,
    )
    failure = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="gmail",
        timeout_seconds=15.0,
    )
    assert failure["result"] == "failure"
    assert failure["errorCode"] == "timeout"
    assert len(calls) == 1


def test_google_calendar_preserves_timezone_in_complete_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="google_calendar",
        provider="google",
        marker="j",
    )
    requested_paths: list[str] = []

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del headers, timeout
        parsed = urlsplit(url)
        requested_paths.append(parsed.path)
        if parsed.path.endswith("/users/me/calendarList/primary"):
            return {"id": "primary@example.test"}
        if parsed.path.endswith("/calendars/primary/events"):
            query = parse_qs(parsed.query)
            assert query["singleEvents"] == ["true"]
            assert query["orderBy"] == ["startTime"]
            return {
                "items": [
                    {
                        "id": "calendar-event-1",
                        "summary": "Synthetic event",
                        "start": {
                            "dateTime": "2026-07-31T10:00:00",
                            "timeZone": "Europe/Brussels",
                        },
                        "end": {
                            "dateTime": "2026-07-31T11:00:00",
                            "timeZone": "Europe/Brussels",
                        },
                        "updated": "2026-07-30T09:00:00Z",
                        "organizer": {"email": "organizer@example.test"},
                    }
                ]
            }
        pytest.fail("unexpected fixed Google Calendar endpoint")

    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=get_json,
    )
    delivery = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="google_calendar",
        observed_at=BASE_TIME,
    )
    assert delivery["result"] == "success"
    items = cast(list[dict[str, object]], delivery["items"])
    assert items[0]["start"] == {
        "dateTime": "2026-07-31T10:00:00",
        "timeZone": "Europe/Brussels",
    }
    record = cast(dict[str, object], delivery["record"])
    assert record["completeness"] == "complete"
    assert requested_paths == [
        "/calendar/v3/users/me/calendarList/primary",
        "/calendar/v3/calendars/primary/events",
    ]


def test_outlook_calendar_uses_calendar_view_and_preserves_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="outlook_calendar",
        provider="microsoft",
        marker="k",
    )

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del headers, timeout
        parsed = urlsplit(url)
        if parsed.path == "/v1.0/me":
            return {"id": "microsoft-account-id"}
        if parsed.path == "/v1.0/me/calendar/calendarView":
            query = parse_qs(parsed.query)
            assert query["$top"] == ["5"]
            assert set(query) == {"startDateTime", "endDateTime", "$top"}
            return {
                "value": [
                    {
                        "id": "outlook-event-1",
                        "subject": "Synthetic recurring occurrence",
                        "start": {
                            "dateTime": "2026-07-31T08:00:00.0000000",
                            "timeZone": "UTC",
                        },
                        "end": {
                            "dateTime": "2026-07-31T09:00:00.0000000",
                            "timeZone": "UTC",
                        },
                        "organizer": {
                            "emailAddress": {
                                "name": "Organizer",
                                "address": "organizer@example.test",
                            }
                        },
                        "lastModifiedDateTime": "2026-07-30T09:00:00Z",
                    }
                ]
            }
        pytest.fail("unexpected fixed Outlook Calendar endpoint")

    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=get_json,
    )
    delivery = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="outlook_calendar",
        observed_at=BASE_TIME,
    )
    assert delivery["result"] == "success"
    items = cast(list[dict[str, object]], delivery["items"])
    assert items[0]["start"] == {
        "dateTime": "2026-07-31T08:00:00.0000000",
        "timeZone": "UTC",
    }


def test_drive_incomplete_search_cannot_claim_complete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="google_drive",
        provider="google",
        marker="l",
    )

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del headers, timeout
        parsed = urlsplit(url)
        if parsed.path.endswith("/about"):
            return {"user": {"permissionId": "drive-account-id"}}
        if parsed.path.endswith("/files"):
            query = parse_qs(parsed.query)
            assert "incompleteSearch" in query["fields"][0]
            return {"files": [], "incompleteSearch": True}
        pytest.fail("unexpected fixed Drive endpoint")

    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=get_json,
    )
    delivery = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="google_drive",
        observed_at=BASE_TIME,
    )
    assert delivery["result"] == "explicit_empty"
    record = cast(dict[str, object], delivery["record"])
    assert record["completeness"] == "partial"
    assert "cursor" not in record


def test_private_provider_failures_reduce_to_fixed_error_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for marker, status, expected in (
        ("d", 401, "auth_expired"),
        ("e", 429, "rate_limited"),
    ):
        vault, manager, connection_id = _prepared(
            tmp_path,
            source_id="gmail",
            provider="google",
            marker=marker,
        )

        def http_failure(
            url: str,
            headers: Mapping[str, str],
            timeout: float,
            status_code: int = status,
        ) -> object:
            del headers, timeout
            raise HTTPError(
                url,
                status_code,
                PRIVATE_BODY,
                Message(),
                BytesIO(PRIVATE_BODY.encode("utf-8")),
            )

        _install_reader(
            monkeypatch,
            vault=vault,
            manager=manager,
            get_json=http_failure,
        )
        failure = read_connector_source(
            vault,
            connection_id=str(connection_id),
            source_id="gmail",
            observed_at=BASE_TIME,
        )
        assert failure["result"] == "failure"
        assert failure["errorCode"] == expected
        assert failure["items"] == []
        record = failure["record"]
        assert isinstance(record, dict)
        assert set(record) == {"source", "result", "errorCode", "toolBinding"}
        assert PRIVATE_BODY not in json.dumps(failure, sort_keys=True)

    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="gmail",
        provider="google",
        marker="f",
    )

    def malformed_success(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del url, headers, timeout
        return [PRIVATE_BODY]

    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=malformed_success,
    )
    malformed = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="gmail",
        observed_at=BASE_TIME,
    )
    assert malformed["result"] == "failure"
    assert malformed["errorCode"] == "read_failed"
    assert malformed["items"] == []
    assert PRIVATE_BODY not in json.dumps(malformed, sort_keys=True)


def test_unverified_connection_is_refused_before_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="gmail",
        provider="google",
        marker="u",
    )
    snapshot = vault.get_connection_snapshot()
    vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=connection_id,
        health=ConnectionHealth.UNVERIFIED,
        observed_at=BASE_TIME.replace(microsecond=1),
    )
    provider_calls = 0

    def provider_must_not_run(
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> object:
        nonlocal provider_calls
        del url, headers, timeout
        provider_calls += 1
        pytest.fail("provider access reached")

    _install_reader(
        monkeypatch,
        vault=vault,
        manager=manager,
        get_json=provider_must_not_run,
    )

    failure = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="gmail",
        observed_at=BASE_TIME,
    )

    assert failure["errorCode"] == "auth_required"
    assert provider_calls == 0


def test_provider_auth_rejection_requires_reauthorization_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123456789")
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="slack",
        provider="slack",
        marker="v",
    )
    provider_calls = 0

    def invalid_auth(
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> object:
        nonlocal provider_calls
        del headers, timeout
        assert url.endswith("/auth.test")
        provider_calls += 1
        return {"ok": False, "error": "invalid_auth"}

    _install_reader(monkeypatch, vault=vault, manager=manager, get_json=invalid_auth)
    first = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="slack",
        observed_at=BASE_TIME,
    )
    second = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="slack",
        observed_at=BASE_TIME,
    )

    connection = vault.get_connection_snapshot().connection(connection_id)
    assert first["errorCode"] == "auth_expired"
    assert second["errorCode"] == "auth_required"
    assert connection is not None
    assert connection.health is ConnectionHealth.REAUTHORIZATION_REQUIRED
    assert provider_calls == 1


def test_slack_reads_one_exact_channel_with_portable_user_oauth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="slack",
        provider="slack",
        marker="m",
    )
    channel_id = "C123456789"
    team_id = "T123456789"
    user_id = "U123456789"
    slack_timestamp = f"{int(BASE_TIME.timestamp())}.000001"
    calls: list[str] = []
    monkeypatch.setenv("SLACK_CHANNEL_ID", channel_id)
    monkeypatch.setenv("SLACK_TOKEN", "ambient-token-must-not-be-used")

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert 0 < timeout <= 15.0
        calls.append(url)
        parsed = urlsplit(url)
        if parsed.path == "/api/auth.test":
            return {"ok": True, "team_id": team_id, "user_id": user_id}
        if parsed.path == "/api/conversations.history":
            assert parse_qs(parsed.query) == {
                "channel": [channel_id],
                "limit": ["15"],
            }
            return {
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": user_id,
                        "text": "x" * 600,
                        "ts": slack_timestamp,
                    }
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "private-cursor"},
            }
        pytest.fail("unexpected fixed Slack endpoint")

    _install_reader(monkeypatch, vault=vault, manager=manager, get_json=get_json)
    delivery = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="slack",
        limit=25,
        observed_at=BASE_TIME,
    )

    assert delivery["result"] == "success"
    items = cast(list[dict[str, object]], delivery["items"])
    assert len(items) == 1
    assert items[0]["text"] == "x" * 500
    assert items[0]["timestamp"] == "2026-07-30T09:00:00.000001Z"
    assert str(items[0]["channelRef"]).startswith("sha256:")
    assert str(items[0]["authorRef"]).startswith("sha256:")
    record = cast(dict[str, object], delivery["record"])
    assert record["completeness"] == "partial"
    assert len(calls) == 2
    serialized_delivery = json.dumps(delivery, sort_keys=True)
    for private_value in (
        TOKEN,
        "ambient-token-must-not-be-used",
        channel_id,
        team_id,
        user_id,
        slack_timestamp,
        "private-cursor",
    ):
        assert private_value not in serialized_delivery
    _record_delivery(vault, delivery)
    stored = (vault.root / "SOURCES.md").read_text(encoding="utf-8")
    assert "x" * 100 not in stored


def test_slack_requires_ok_and_rejects_bot_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123456789")
    for marker, identity, expected_error in (
        ("n", {"ok": False, "error": "invalid_auth"}, "auth_expired"),
        (
            "o",
            {
                "ok": True,
                "team_id": "T123456789",
                "user_id": "U123456789",
                "bot_id": "B123456789",
            },
            "read_failed",
        ),
    ):
        vault, manager, connection_id = _prepared(
            tmp_path,
            source_id="slack",
            provider="slack",
            marker=marker,
        )

        def get_json(
            url: str,
            headers: Mapping[str, str],
            timeout: float,
            identity_payload: object = identity,
        ) -> object:
            del headers, timeout
            assert url.endswith("/auth.test")
            return identity_payload

        _install_reader(monkeypatch, vault=vault, manager=manager, get_json=get_json)
        failure = read_connector_source(
            vault,
            connection_id=str(connection_id),
            source_id="slack",
            observed_at=BASE_TIME,
        )
        assert failure["result"] == "failure"
        assert failure["errorCode"] == expected_error


def test_slack_history_error_and_credential_rotation_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123456789")
    vault, manager, connection_id = _prepared(
        tmp_path,
        source_id="slack",
        provider="slack",
        marker="p",
    )
    history_calls = 0

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        nonlocal history_calls
        del headers, timeout
        if url.endswith("/auth.test"):
            return {"ok": True, "team_id": "T123456789", "user_id": "U123456789"}
        history_calls += 1
        return {"ok": False, "error": "missing_scope"}

    _install_reader(monkeypatch, vault=vault, manager=manager, get_json=get_json)
    denied = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="slack",
        observed_at=BASE_TIME,
    )
    assert denied["errorCode"] == "permission_denied"
    assert history_calls == 1

    def rotating_get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del headers, timeout
        if url.endswith("/auth.test"):
            return {"ok": True, "team_id": "T123456789", "user_id": "U123456789"}
        current = manager.tokens.state(connection_id)
        assert current is not None
        replacement = OAuthCredential(
            access_token="rotated-access-token",
            refresh_token=None,
            token_type=OAuthTokenType.BEARER,
            scopes=get_profile("slack").read_scopes,
            issued_at=BASE_TIME,
            expires_at=None,
        )
        manager.tokens.update(
            connection_id,
            expected_version=current.version,
            value=replacement.to_bytes(),
            updated_at=replacement.issued_at,
        )
        return {"ok": False, "error": "missing_scope"}

    monkeypatch.setattr(connector_sources, "http_get_json", rotating_get_json)
    with pytest.raises(ConflictError, match="credential changed"):
        read_connector_source(
            vault,
            connection_id=str(connection_id),
            source_id="slack",
            observed_at=BASE_TIME,
        )


class _RedirectHandler(Protocol):
    def http_error_302(self, *args: object, **kwargs: object) -> object: ...


class _RedirectOpener:
    def __init__(self, handler: _RedirectHandler) -> None:
        self.handler = handler
        self.method: str | None = None
        self.timeout: float | None = None

    def open(self, request: object, *, timeout: float) -> object:
        assert isinstance(request, Request)
        self.method = request.get_method()
        self.timeout = timeout
        reject = cast(Callable[..., object], self.handler.http_error_302)
        return reject(request, object(), 302, "Found", {})


class _JSONResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _JSONResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def getcode(self) -> int:
        return 200

    def read(self, size: int) -> bytes:
        assert size == connector_http.MAX_RESPONSE_BYTES + 1
        return self.body


class _SuccessOpener:
    def __init__(self, response: _JSONResponse) -> None:
        self.response = response
        self.request: Request | None = None

    def open(self, request: object, *, timeout: float) -> _JSONResponse:
        assert isinstance(request, Request)
        assert timeout == 3.0
        self.request = request
        return self.response


def test_http_boundary_allows_the_exact_slack_history_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _SuccessOpener(_JSONResponse(b'{"ok":true,"messages":[]}'))
    monkeypatch.setattr(connector_http, "build_opener", lambda *handlers: opener)

    result = connector_http.get_json(
        "https://slack.com/api/conversations.history?channel=C123456789&limit=15",
        {"Accept": "application/json", "Authorization": "Bearer synthetic"},
        3.0,
    )

    assert result == {"ok": True, "messages": []}
    assert opener.request is not None
    assert opener.request.get_method() == "GET"


def test_http_boundary_allows_the_exact_google_calendar_primary_identity_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _SuccessOpener(_JSONResponse(b'{"id":"primary-calendar"}'))
    monkeypatch.setattr(connector_http, "build_opener", lambda *handlers: opener)

    result = connector_http.get_json(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList/primary",
        {"Accept": "application/json", "Authorization": "Bearer synthetic"},
        3.0,
    )

    assert result == {"id": "primary-calendar"}
    assert opener.request is not None
    assert opener.request.get_method() == "GET"


def test_http_boundary_rejects_redirects_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    openers: list[_RedirectOpener] = []

    def fake_build_opener(*handlers: object) -> _RedirectOpener:
        assert len(handlers) == 1
        opener = _RedirectOpener(cast(_RedirectHandler, handlers[0]))
        openers.append(opener)
        return opener

    monkeypatch.setattr(connector_http, "build_opener", fake_build_opener)
    with pytest.raises(connector_http.ConnectorRedirectError):
        connector_http.get_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            {"Accept": "application/json", "Authorization": "Bearer synthetic"},
            3.0,
        )
    assert openers[0].method == "GET"
    assert openers[0].timeout == 3.0

    for unsafe_url in (
        "https://slack.com/api/chat.postMessage",
        ("https://slack.com/api/conversations.history?channel=C123456789&limit=15&cursor=attacker"),
    ):
        with pytest.raises(ValidationError, match="not allowed"):
            connector_http.get_json(
                unsafe_url,
                {"Accept": "application/json", "Authorization": "Bearer synthetic"},
                3.0,
            )
