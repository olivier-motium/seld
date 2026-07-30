from __future__ import annotations

import json
from collections.abc import Callable, Mapping
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
from continuity_kernel.connector_oauth import OAuthTokenType
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_sources import read_connector_source
from continuity_kernel.errors import SetupError, ValidationError
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
    else:
        authorization_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        default_token_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    metadata = ConnectionMetadata(
        connection_id=connection_id,
        provider=provider,
        source_ids=(source_id,),
        credential_kind=CredentialKind.OAUTH2,
        account=AccountMetadata(label="Synthetic account"),
        scopes=("connector.read",),
        client=ClientMetadata(
            kind=ClientKind.PUBLIC,
            identifier="synthetic-client",
            redirect_uris=("http://127.0.0.1:49152/callback",),
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint or default_token_endpoint,
        ),
        health=ConnectionHealth.UNKNOWN,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
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
    manager.store_oauth_credential(
        connection_id,
        OAuthCredential(
            access_token=TOKEN,
            refresh_token=None,
            token_type=OAuthTokenType.BEARER,
            scopes=("connector.read",),
            issued_at=BASE_TIME,
            expires_at=None,
        ),
        expected_token_version=0,
    )
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
    delivery = read_connector_source(
        vault,
        connection_id=str(connection_id),
        source_id="gmail",
        limit=2,
        observed_at=BASE_TIME,
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
    assert all(0 < timeout <= 15.0 for timeout in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)


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

    monkeypatch.setattr(manager, "resolve_oauth_access_token", fail_resolve)
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

    times = iter((100.0, 100.0, 101.0, 102.0, 116.0))
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

    def get_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
        del headers, timeout
        parsed = urlsplit(url)
        if parsed.path.endswith("/calendars/primary"):
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
