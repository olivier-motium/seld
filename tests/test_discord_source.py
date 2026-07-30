from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import continuity_kernel.discord_source as discord_source_module
from continuity_kernel import mcp_server
from continuity_kernel.connector_auth import (
    AccountMetadata,
    ClientKind,
    ClientMetadata,
    ConnectionHealth,
    ConnectionMetadata,
    CredentialKind,
)
from continuity_kernel.connector_auth_manager import ConnectorAuthManager
from continuity_kernel.connector_identifiers import ConnectionId, parse_connection_id
from continuity_kernel.connector_secrets import InMemorySecretStore
from continuity_kernel.connector_token_store import ResolvedToken, TokenState
from continuity_kernel.discord_source import (
    ABSENT_BINDING_REVISION,
    DiscordSourceBridge,
)
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError
from continuity_kernel.vault import Vault

CHANNEL = "111111111111111111"
TOKEN = "unit-test-secret-that-must-not-persist"
ACCOUNT = f"discord-bot-v1:sha256:{'a' * 64}"
USER_ACCOUNT = f"discord-user-v1:sha256:{'b' * 64}"
USER_WARNING = (
    "Discord forbids normal-user self-bot automation and may terminate the account; "
    "GET-only access does not remove that risk."
)
TOOL = "seld-discord-source:readonly:v2"
BASE_TIME = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
CONNECTION_ID = parse_connection_id("con-" + "d" * 32)


def _auth_manager(
    vault: Vault,
    tmp_path: Path,
    *,
    provider: str = "discord",
    source_ids: tuple[str, ...] = ("discord",),
    credential_kind: CredentialKind = CredentialKind.BEARER,
    health: ConnectionHealth = ConnectionHealth.UNKNOWN,
    store_credential: bool = True,
) -> ConnectorAuthManager:
    metadata = ConnectionMetadata(
        connection_id=CONNECTION_ID,
        provider=provider,
        source_ids=source_ids,
        credential_kind=credential_kind,
        account=AccountMetadata(label="Synthetic Discord bot"),
        scopes=(),
        client=ClientMetadata(kind=ClientKind.EXTERNAL),
        health=health,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
        version=1,
    )
    vault.put_connection(
        expected_revision=vault.get_connection_snapshot().revision,
        connection=metadata,
        observed_at=BASE_TIME,
    )
    manager = ConnectorAuthManager(
        vault,
        secret_store=InMemorySecretStore(),
        state_root=tmp_path / f"connector-auth-{vault.root.name}",
    )
    if store_credential:
        manager.store_credential(CONNECTION_ID, TOKEN.encode(), expected_token_version=0)
    return manager


def _bridge(
    vault: Vault,
    tmp_path: Path,
    *,
    provider: str = "discord",
    source_ids: tuple[str, ...] = ("discord",),
    credential_kind: CredentialKind = CredentialKind.BEARER,
    health: ConnectionHealth = ConnectionHealth.UNKNOWN,
    store_credential: bool = True,
) -> DiscordSourceBridge:
    manager = _auth_manager(
        vault,
        tmp_path,
        provider=provider,
        source_ids=source_ids,
        credential_kind=credential_kind,
        health=health,
        store_credential=store_credential,
    )
    return DiscordSourceBridge(vault, auth_manager=manager)


def _runtime(
    path: Path,
    *,
    extra_tool: bool = False,
    invalid_tool_name: bool = False,
    malformed: bool = False,
    stall: bool = False,
    overflow: bool = False,
    corrupt_delivery: bool = False,
    corrupt_ack: bool = False,
    extra_items: bool = False,
    user_mode: bool = False,
    protocol_version: str = "2025-03-26",
) -> Path:
    inventory_extra = (
        ",{'name':'discord_write_messages','inputSchema':{'type':'object','properties':{}}}"
        if extra_tool
        else ""
    )
    prefix = "this is not json\n" if malformed else ""
    status_tool_name = "None" if invalid_tool_name else "'discord_source_status'"
    source = f'''#!{sys.executable}
import datetime
import hashlib
import json
import os
import sys
import time
from pathlib import Path

STATE = Path(os.environ["DISCORD_STATE_FILE"])
ACCOUNT = {USER_ACCOUNT if user_mode else ACCOUNT!r}
AUTH_TYPE = {"user" if user_mode else "bot"!r}
TOOL = "{TOOL}"
WARNING = {USER_WARNING if user_mode else None!r}
CHANNEL_DIGEST = "sha256:" + "1" * 64
STALL = {stall!r}
OVERFLOW = {overflow!r}
CORRUPT_DELIVERY = {corrupt_delivery!r}
CORRUPT_ACK = {corrupt_ack!r}
EXTRA_ITEMS = {extra_items!r}
PROTOCOL_VERSION = {protocol_version!r}

def now():
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

def load():
    if not STATE.exists():
        return {{"sequence": 0, "pending": None, "committed": None, "last_ack": None}}
    return json.loads(STATE.read_text())

def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state["private_channels"] = os.environ["DISCORD_CHANNEL_IDS"].split(",")
    state["bot_token_digest"] = hashlib.sha256(
        os.environ["DISCORD_BOT_TOKEN"].encode()
    ).hexdigest()
    STATE.write_text(json.dumps(state))
    STATE.with_suffix(".envkeys").write_text(json.dumps(sorted(os.environ)))

def cursor(letter):
    return "discord-cursor-v1:sha256:" + letter * 64

def projected_items(pending):
    if pending["result"] == "explicit_empty":
        return []
    item = {{
        "observedAt":pending["covered"],"channelRef":"sha256:"+"2"*64,
        "messageRef":"sha256:"+"3"*64,"authorRef":"sha256:"+"4"*64,
        "preview":"bounded synthetic message","edited":False,
        "attachmentCount":0,"embedCount":0
    }}
    return [item, item] if EXTRA_ITEMS else [item]

def delivery_binding(*, cursor_value, result, completeness, covered, items):
    def fingerprint(value):
        return "sha256:" + hashlib.sha256(value.strip().encode()).hexdigest()
    canonical = json.dumps({{
        "accountFingerprint": fingerprint(ACCOUNT),
        "channelSetDigest": CHANNEL_DIGEST,
        "toolFingerprint": fingerprint(TOOL),
        "cursorDigest": fingerprint(cursor_value),
        "result": result,
        "completeness": completeness,
        "coveredThrough": covered,
        "items": items,
    }}, ensure_ascii=False, separators=(",", ":"))
    encoded = f"discord-delivery-v1\\0{{canonical}}".encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

def checkpoint(state):
    pending = state["pending"]
    committed = state["committed"]
    latest = pending or committed
    return {{
        "accountBinding": ACCOUNT if pending or committed else None,
        "channelSetDigest": CHANNEL_DIGEST if pending or committed else None,
        "cursorBinding": committed["cursor"] if committed else None,
        "lastAttempt": ({{
            "at": latest["attempted"],
            "result": latest["result"],
            "errorCode": None,
        }} if latest else None),
        "lastSuccessAt": committed["attempted"] if committed else None,
        "coveredThrough": committed["covered"] if committed else None,
        "pending": pending is not None,
        "pendingCursorBinding": pending["cursor"] if pending else None,
        "pendingDeliveryBinding": pending["delivery"] if pending else None,
        "pendingCoveredThrough": pending["covered"] if pending else None,
        "pendingResult": pending["result"] if pending else None,
        "pendingCompleteness": pending["completeness"] if pending else None,
    }}

TOOLS = [
    {{"name":{status_tool_name},"inputSchema":{{"type":"object","properties":{{}}}}}},
    {{"name":"discord_poll_messages","inputSchema":{{"type":"object","properties":{{
        "channelIds":{{"type":"array","items":{{"type":"string"}},"minItems":1,"maxItems":5}},
        "limit":{{"type":"integer","minimum":1,"maximum":25,"default":5}},
        "maxContentChars":{{"type":"integer","minimum":0,"maximum":500,"default":280}}
    }},"additionalProperties":False}}}},
    {{"name":"discord_acknowledge_messages","inputSchema":{{"type":"object","properties":{{
        "ackToken":{{"type":"string"}}
    }},"required":["ackToken"],"additionalProperties":False}}}}
    {inventory_extra}
]

def tool_result(name, arguments):
    if name == "discord_poll_messages" and STALL:
        time.sleep(1)
    if name == "discord_poll_messages" and OVERFLOW:
        print("x" * 2048, flush=True)
    state = load()
    attempted = now()
    if name == "discord_source_status":
        return {{
            "source":"discord","recipeVersion":"2","available":True,
            "attemptedAt":attempted,"authType":AUTH_TYPE,"accountLabel":"Synthetic account",
            "termsWarning":WARNING,
            "accountBinding":ACCOUNT,"configuredChannelSetDigest":CHANNEL_DIGEST,
            "configuredChannelCount":1,"toolBinding":TOOL,"checkpoint":checkpoint(state)
        }}
    if name == "discord_poll_messages":
        if state["pending"] is None:
            sequence = state["sequence"]
            is_baseline = sequence == 0
            pending = {{
                "attempted": attempted,
                "covered": attempted,
                "cursor": cursor("a" if is_baseline else "b"),
                "token": ("f" if is_baseline else "9") * 64,
                "result": "explicit_empty" if is_baseline else "success",
                "completeness": "complete" if is_baseline else "partial",
            }}
            pending["delivery"] = delivery_binding(
                cursor_value=pending["cursor"],
                result=pending["result"],
                completeness=pending["completeness"],
                covered=pending["covered"],
                items=projected_items(pending),
            )
            if CORRUPT_DELIVERY:
                pending["delivery"] = "sha256:" + "0" * 64
            state["pending"] = pending
            save(state)
        pending = state["pending"]
        items = projected_items(pending)
        return {{
            "source":"discord","recipeVersion":"2","attemptedAt":pending["attempted"],
            "lastSuccessAt":pending["attempted"],"result":pending["result"],
            "completeness":pending["completeness"],"coveredThrough":pending["covered"],
            "authType":AUTH_TYPE,"accountBinding":ACCOUNT,"toolBinding":TOOL,
            "channelSetDigest":CHANNEL_DIGEST,
            "cursorBinding":pending["cursor"],"evidenceRefs":[pending["delivery"]],
            "deliveryBinding":pending["delivery"],"baseline":state["sequence"] == 0,
            "replayedPending":False,"items":items,"acknowledgementRequired":True,
            "ackToken":pending["token"]
        }}
    if name == "discord_acknowledge_messages":
        token = arguments.get("ackToken")
        if state["pending"] and token == state["pending"]["token"]:
            state["committed"] = state["pending"]
            state["last_ack"] = token
            state["pending"] = None
            state["sequence"] += 1
            save(state)
            already = False
        elif token == state["last_ack"]:
            already = True
        else:
            raise RuntimeError("stale acknowledgement")
        committed = checkpoint(state)
        if CORRUPT_ACK:
            committed["cursorBinding"] = cursor("c")
        return {{"source":"discord","recipeVersion":"2",
                "acknowledged":True,"alreadyAcknowledged":already,
                "checkpoint":committed}}
    raise RuntimeError("unsupported tool")

print({prefix!r}, end="")
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {{"protocolVersion":PROTOCOL_VERSION,"capabilities":{{"tools":{{}}}},
                  "serverInfo":{{"name":"discord-mcp","version":"synthetic"}}}}
    elif request["method"] == "tools/list":
        result = {{"tools":TOOLS}}
    else:
        params = request["params"]
        payload = tool_result(params["name"], params["arguments"])
        result = {{"content":[{{"type":"text","text":json.dumps(payload)}}]}}
    print(json.dumps({{"jsonrpc":"2.0","id":request["id"],"result":result}}), flush=True)
'''
    path.write_text(source)
    path.chmod(0o700)
    return path


@pytest.fixture
def prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Vault, DiscordSourceBridge, Path]:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Discord source test")
    bridge = _bridge(vault, tmp_path)
    runtime = _runtime(tmp_path / "synthetic-discord")
    bridge.bind(
        runtime,
        connection_id=CONNECTION_ID,
        expected_revision=ABSENT_BINDING_REVISION,
    )
    selected = vault.select_sources(expected_revision="absent", sources=("discord",))
    assert selected["revision"] != "absent"
    return vault, bridge, runtime


def _provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_USER_TOKEN", "ambient-user-token-must-be-ignored")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "ambient-bot-token-must-be-ignored")
    monkeypatch.setenv("DISCORD_CHANNEL_IDS", CHANNEL)


def _record(vault: Vault, delivery: dict[str, object]) -> str:
    record = delivery["record"]
    assert isinstance(record, dict)
    status = vault.record_source_observation(
        expected_revision=str(delivery["sourceRevision"]),
        source_id="discord",
        actor_ref="codex:test",
        result=str(record["result"]),
        covered_through=str(record["coveredThrough"]),
        completeness=str(record["completeness"]),
        account_binding=str(record["accountBinding"]),
        tool_binding=str(record["toolBinding"]),
        cursor=str(record["cursor"]),
        evidence_refs=tuple(str(item) for item in record["evidenceRefs"]),
    )
    return str(status["revision"])


def test_binding_is_cas_bound_owner_only_and_contains_no_provider_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Binding test")
    bridge = _bridge(vault, tmp_path)
    runtime = _runtime(tmp_path / "synthetic-discord")
    assert bridge.binding_status() == {
        "bound": False,
        "bindingRevision": "absent",
        "runtimeCurrent": False,
    }
    bound = bridge.bind(
        runtime,
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    assert bound["bound"] is True
    assert bound["connectionId"] == CONNECTION_ID
    assert bound["migrationRequired"] is False
    assert bound["runtimeCurrent"] is True
    encoded = bridge.binding_path.read_text()
    assert TOKEN not in encoded
    assert CHANNEL not in encoded
    if os.name != "nt":
        assert bridge.binding_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ConflictError):
        bridge.bind(
            runtime,
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )

    link = tmp_path / "runtime-link"
    link.symlink_to(runtime)
    other_vault = Vault(tmp_path / "other-vault")
    other_vault.initialize(name="Other")
    with pytest.raises(ValidationError, match="non-symlinked"):
        _bridge(other_vault, tmp_path).bind(
            link,
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )

    native = tmp_path / "native-runtime"
    native.write_text("not a supported companion runtime\n")
    native.chmod(0o700)
    native_vault = Vault(tmp_path / "native-vault")
    native_vault.initialize(name="Native")
    with pytest.raises(ValidationError, match="pinned interpreter"):
        _bridge(native_vault, tmp_path).bind(
            native,
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )

    runtime.write_text(runtime.read_text() + "\n# drift\n")
    assert bridge.binding_status()["runtimeCurrent"] is False
    unbound = bridge.unbind(expected_revision=str(bound["bindingRevision"]))
    assert unbound == {"bound": False, "bindingRevision": "absent"}


def test_legacy_binding_requires_explicit_connection_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Legacy Discord binding")
    bridge = _bridge(vault, tmp_path)
    runtime = _runtime(tmp_path / "runtime")
    bridge.bind(
        runtime,
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    vault.select_sources(expected_revision="absent", sources=("discord",))
    _provider(monkeypatch)
    delivery = bridge.poll()
    receipt_revision = _record(vault, delivery)
    state_before = bridge.state_path.read_bytes()
    payload = json.loads(bridge.binding_path.read_text(encoding="ascii"))
    payload.pop("connection_id")
    payload["format_version"] = 1
    bridge.binding_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )

    legacy = bridge.binding_status()
    assert legacy["bound"] is True
    assert legacy["connectionId"] is None
    assert legacy["migrationRequired"] is True
    status = bridge.status()
    assert status["available"] is False
    assert status["errorCode"] == "auth_required"
    with pytest.raises(ContinuityError, match="legacy Discord binding"):
        bridge.poll()
    with pytest.raises(ContinuityError, match="legacy Discord binding"):
        bridge.acknowledge(
            ack_token=str(delivery["ackToken"]),
            expected_source_revision=receipt_revision,
        )
    assert bridge.state_path.read_bytes() == state_before

    unbound = bridge.unbind(expected_revision=str(legacy["bindingRevision"]))
    assert unbound == {"bound": False, "bindingRevision": "absent"}

    rebound = bridge.bind(
        runtime,
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    assert rebound["connectionId"] == CONNECTION_ID
    assert rebound["migrationRequired"] is False


@pytest.mark.parametrize(
    ("provider", "source_ids", "credential_kind", "health", "message"),
    [
        (
            "google",
            ("discord",),
            CredentialKind.BEARER,
            ConnectionHealth.UNKNOWN,
            "requires a Discord connection",
        ),
        (
            "discord",
            ("gmail",),
            CredentialKind.BEARER,
            ConnectionHealth.UNKNOWN,
            "does not authorize",
        ),
        (
            "discord",
            ("discord",),
            CredentialKind.API_KEY,
            ConnectionHealth.UNKNOWN,
            "bearer bot token",
        ),
        (
            "discord",
            ("discord",),
            CredentialKind.BEARER,
            ConnectionHealth.REVOKED,
            "requires reauthorization",
        ),
    ],
    ids=("wrong-provider", "wrong-source", "wrong-kind", "revoked"),
)
def test_binding_preflights_exact_portable_discord_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    source_ids: tuple[str, ...],
    credential_kind: CredentialKind,
    health: ConnectionHealth,
    message: str,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Discord connection preflight")
    bridge = _bridge(
        vault,
        tmp_path,
        provider=provider,
        source_ids=source_ids,
        credential_kind=credential_kind,
        health=health,
    )
    with pytest.raises(ValidationError, match=message):
        bridge.bind(
            _runtime(tmp_path / "runtime"),
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )


def test_status_and_poll_recheck_connection_health_and_token_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Discord operation preflight")
    bridge = _bridge(vault, tmp_path, store_credential=False)
    bridge.auth_manager.store_credential(
        CONNECTION_ID,
        b"credential with whitespace",
        expected_token_version=0,
    )
    bridge.bind(
        _runtime(tmp_path / "runtime"),
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    vault.select_sources(expected_revision="absent", sources=("discord",))
    _provider(monkeypatch)
    invalid_token = bridge.status()
    assert invalid_token["available"] is False
    assert invalid_token["errorCode"] == "auth_required"
    with pytest.raises(ContinuityError, match="bot credential is unavailable"):
        bridge.poll()
    assert not bridge.state_path.exists()

    bridge.auth_manager.tokens.delete(CONNECTION_ID)
    bridge.auth_manager.store_credential(
        CONNECTION_ID,
        TOKEN.encode(),
        expected_token_version=0,
    )
    snapshot = vault.get_connection_snapshot()
    vault.mark_connection_health(
        expected_revision=snapshot.revision,
        connection_id=CONNECTION_ID,
        health=ConnectionHealth.REVOKED,
    )
    revoked = bridge.status()
    assert revoked["available"] is False
    assert revoked["errorCode"] == "auth_required"
    with pytest.raises(ContinuityError, match="portable connection is unavailable"):
        bridge.poll()
    assert not bridge.state_path.exists()


def test_status_reports_setup_failures_without_exposing_paths_or_ids(
    prepared: tuple[Vault, DiscordSourceBridge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, bridge, _runtime_path = prepared
    bridge.auth_manager.tokens.delete(CONNECTION_ID)
    _provider(monkeypatch)
    status = bridge.status()
    assert status["available"] is False
    assert status["errorCode"] == "auth_required"
    assert str(_runtime_path) not in json.dumps(status)
    assert CHANNEL not in json.dumps(status)


def test_status_never_touches_discord_until_the_source_is_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Unselected Discord")
    bridge = _bridge(vault, tmp_path)
    bridge.bind(
        _runtime(tmp_path / "runtime"),
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    _provider(monkeypatch)
    status = bridge.status()
    assert status["selected"] is False
    assert status["available"] is False
    assert status["errorCode"] == "policy_blocked"
    assert not bridge.state_path.exists()


def test_baseline_record_ack_and_fresh_process_idempotence(
    prepared: tuple[Vault, DiscordSourceBridge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, bridge, runtime_path = prepared
    _provider(monkeypatch)
    status = bridge.status()
    assert status["available"] is True
    assert status["accountLabel"] == "Synthetic account"
    assert status["authType"] == "bot"
    assert status["termsWarning"] is None
    assert status["configuredChannelCount"] == 1
    delivery = bridge.poll(limit=5, max_content_chars=100)
    assert delivery["result"] == "explicit_empty"
    assert delivery["items"] == []
    assert delivery["record"]["evidenceRefs"] == [delivery["deliveryBinding"]]
    with pytest.raises(ConflictError, match="record the matching"):
        bridge.acknowledge(
            ack_token=str(delivery["ackToken"]),
            expected_source_revision=str(delivery["sourceRevision"]),
        )

    receipt_revision = _record(vault, delivery)
    acknowledged = bridge.acknowledge(
        ack_token=str(delivery["ackToken"]),
        expected_source_revision=receipt_revision,
    )
    assert acknowledged["alreadyAcknowledged"] is False
    restarted = DiscordSourceBridge(vault, auth_manager=bridge.auth_manager).acknowledge(
        ack_token=str(delivery["ackToken"]),
        expected_source_revision=receipt_revision,
    )
    assert restarted["alreadyAcknowledged"] is True

    host_bytes = b"".join(path.read_bytes() for path in (bridge.root).rglob("*") if path.is_file())
    vault_bytes = b"".join(path.read_bytes() for path in vault.root.rglob("*") if path.is_file())
    for encoded in (host_bytes, vault_bytes):
        assert TOKEN.encode() not in encoded
    assert CHANNEL.encode() in bridge.state_path.read_bytes()
    assert CHANNEL.encode() not in bridge.binding_path.read_bytes()
    assert CHANNEL.encode() not in vault_bytes

    current = vault.source_status()
    assert current["sources"][0]["freshness"] == "current"
    runtime_path.write_text(runtime_path.read_text() + "\n# reviewed runtime drift\n")
    drifted = vault.source_status()
    assert drifted["sources"][0]["freshness"] == "needs_revalidation"


def test_partial_delivery_progress_requires_the_exact_receipt(
    prepared: tuple[Vault, DiscordSourceBridge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, bridge, _runtime_path = prepared
    _provider(monkeypatch)
    baseline = bridge.poll()
    first_revision = _record(vault, baseline)
    bridge.acknowledge(
        ack_token=str(baseline["ackToken"]),
        expected_source_revision=first_revision,
    )

    delivery = bridge.poll(limit=1, max_content_chars=30)
    assert delivery["result"] == "success"
    assert delivery["completeness"] == "partial"
    assert delivery["items"][0]["preview"] == "bounded synthetic message"
    record = delivery["record"]
    assert isinstance(record, dict)
    wrong = vault.record_source_observation(
        expected_revision=str(delivery["sourceRevision"]),
        source_id="discord",
        actor_ref="codex:test",
        result=str(record["result"]),
        covered_through=str(record["coveredThrough"]),
        completeness=str(record["completeness"]),
        account_binding=str(record["accountBinding"]),
        tool_binding=str(record["toolBinding"]),
        cursor="discord-cursor-v1:sha256:" + "0" * 64,
        evidence_refs=tuple(str(item) for item in record["evidenceRefs"]),
    )
    with pytest.raises(ConflictError, match="does not match"):
        bridge.acknowledge(
            ack_token=str(delivery["ackToken"]),
            expected_source_revision=str(wrong["revision"]),
        )
    assert bridge.status()["checkpoint"]["pending"] is True


def test_child_environment_is_minimized(
    prepared: tuple[Vault, DiscordSourceBridge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, bridge, _runtime_path = prepared
    _provider(monkeypatch)
    monkeypatch.setenv("NODE_OPTIONS", "--require=unexpected")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross")
    bridge.poll()
    keys = json.loads(bridge.state_path.with_suffix(".envkeys").read_text())
    state = json.loads(bridge.state_path.read_text())
    assert "NODE_OPTIONS" not in keys
    assert "AWS_SECRET_ACCESS_KEY" not in keys
    assert "DISCORD_USER_TOKEN" not in keys
    assert "DISCORD_BOT_TOKEN" in keys
    assert state["bot_token_digest"] == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert set(keys) <= {
        "DISCORD_CHANNEL_IDS",
        "DISCORD_STATE_FILE",
        "DISCORD_BOT_TOKEN",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOG_LEVEL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "__CF_USER_TEXT_ENCODING",
    }


def test_user_token_companion_payload_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Discord user-token rejection")
    bridge = _bridge(vault, tmp_path)
    bridge.bind(
        _runtime(tmp_path / "user-runtime", user_mode=True),
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    vault.select_sources(expected_revision="absent", sources=("discord",))
    _provider(monkeypatch)

    with pytest.raises(ValidationError, match="account binding is invalid"):
        bridge.poll()


def test_poll_discards_provider_result_if_credential_rotates_during_read(
    prepared: tuple[Vault, DiscordSourceBridge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault, bridge, _runtime_path = prepared
    _provider(monkeypatch)
    original_state = bridge.auth_manager.tokens.state
    current = original_state(CONNECTION_ID)
    assert current is not None
    rotated = False

    def state_with_rotation(connection_id: ConnectionId) -> TokenState | None:
        nonlocal rotated
        if not rotated:
            rotated = True
            bridge.auth_manager.store_credential(
                CONNECTION_ID,
                b"rotated-discord-bot-token",
                expected_token_version=current.version,
            )
        return original_state(connection_id)

    monkeypatch.setattr(bridge.auth_manager.tokens, "state", state_with_rotation)
    with pytest.raises(ConflictError, match="credential changed during"):
        bridge.poll()


def test_acknowledgement_cannot_cross_credential_rotation(
    prepared: tuple[Vault, DiscordSourceBridge, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, bridge, _runtime_path = prepared
    _provider(monkeypatch)
    delivery = bridge.poll()
    receipt_revision = _record(vault, delivery)
    original_resolve = bridge.auth_manager.resolve_credential_state
    calls = 0

    def resolve_with_rotation(connection_id: ConnectionId | str) -> ResolvedToken:
        nonlocal calls
        calls += 1
        if calls == 2:
            current = bridge.auth_manager.tokens.state(CONNECTION_ID)
            assert current is not None
            bridge.auth_manager.store_credential(
                CONNECTION_ID,
                b"rotated-before-acknowledgement",
                expected_token_version=current.version,
            )
        return original_resolve(connection_id)

    monkeypatch.setattr(
        bridge.auth_manager,
        "resolve_credential_state",
        resolve_with_rotation,
    )
    with pytest.raises(ConflictError, match="credential changed before acknowledgement"):
        bridge.acknowledge(
            ack_token=str(delivery["ackToken"]),
            expected_source_revision=receipt_revision,
        )
    assert bridge.status()["checkpoint"]["pending"] is True


def test_binding_rejects_extra_tools_and_malformed_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    first = Vault(tmp_path / "first")
    first.initialize(name="First")
    with pytest.raises(ValidationError, match="exactly the three"):
        _bridge(first, tmp_path).bind(
            _runtime(tmp_path / "extra", extra_tool=True),
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )
    second = Vault(tmp_path / "second")
    second.initialize(name="Second")
    with pytest.raises(ValidationError, match="malformed MCP"):
        _bridge(second, tmp_path).bind(
            _runtime(tmp_path / "malformed", malformed=True),
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )
    third = Vault(tmp_path / "third")
    third.initialize(name="Third")
    with pytest.raises(ValidationError, match="invalid names"):
        _bridge(third, tmp_path).bind(
            _runtime(tmp_path / "invalid-name", invalid_tool_name=True),
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )
    fourth = Vault(tmp_path / "fourth")
    fourth.initialize(name="Fourth")
    with pytest.raises(ValidationError, match="identity is invalid"):
        _bridge(fourth, tmp_path).bind(
            _runtime(tmp_path / "wrong-protocol", protocol_version="2099-01-01"),
            connection_id=CONNECTION_ID,
            expected_revision="absent",
        )


@pytest.mark.parametrize(
    ("mode", "error_type", "message"),
    [
        ("stall", ContinuityError, "bounded local call"),
        ("overflow", ValidationError, "output exceeded"),
    ],
)
def test_companion_call_has_hard_time_and_output_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    error_type: type[Exception],
    message: str,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Bounded companion")
    bridge = _bridge(vault, tmp_path)
    runtime = _runtime(
        tmp_path / "bounded-runtime",
        stall=mode == "stall",
        overflow=mode == "overflow",
    )
    bridge.bind(
        runtime,
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    monkeypatch.setattr(discord_source_module, "MCP_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(discord_source_module, "MAX_MCP_OUTPUT_BYTES", 1024)
    vault.select_sources(expected_revision="absent", sources=("discord",))
    _provider(monkeypatch)
    with pytest.raises(error_type, match=message):
        bridge.poll()


def test_poll_rejects_a_delivery_binding_that_does_not_attest_its_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Delivery binding")
    bridge = _bridge(vault, tmp_path)
    bridge.bind(
        _runtime(tmp_path / "corrupt-runtime", corrupt_delivery=True),
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    vault.select_sources(expected_revision="absent", sources=("discord",))
    _provider(monkeypatch)
    with pytest.raises(ValidationError, match="does not attest"):
        bridge.poll()


@pytest.mark.parametrize(
    ("extra_items", "max_content_chars", "message"),
    [
        (True, 500, "items exceed"),
        (False, 0, "message preview"),
    ],
)
def test_poll_rechecks_the_exact_requested_output_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_items: bool,
    max_content_chars: int,
    message: str,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Action bound")
    bridge = _bridge(vault, tmp_path)
    bridge.bind(
        _runtime(tmp_path / "ignores-bound", extra_items=extra_items),
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    vault.select_sources(expected_revision="absent", sources=("discord",))
    _provider(monkeypatch)
    baseline = bridge.poll()
    revision = _record(vault, baseline)
    bridge.acknowledge(
        ack_token=str(baseline["ackToken"]),
        expected_source_revision=revision,
    )
    with pytest.raises(ValidationError, match=message):
        bridge.poll(limit=1, max_content_chars=max_content_chars)


def test_acknowledgement_rechecks_the_committed_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GSV_DATA_DIR", str(tmp_path / "host"))
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Corrupt acknowledgement")
    bridge = _bridge(vault, tmp_path)
    bridge.bind(
        _runtime(tmp_path / "corrupt-ack", corrupt_ack=True),
        connection_id=CONNECTION_ID,
        expected_revision="absent",
    )
    vault.select_sources(expected_revision="absent", sources=("discord",))
    _provider(monkeypatch)
    delivery = bridge.poll()
    revision = _record(vault, delivery)
    with pytest.raises(ConflictError, match="does not match"):
        bridge.acknowledge(
            ack_token=str(delivery["ackToken"]),
            expected_source_revision=revision,
        )


def test_seld_mcp_exposes_only_the_three_operational_discord_surfaces() -> None:
    tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
    discord_names = {name for name in tools if name.startswith("gsv_discord_source_")}
    assert discord_names == {
        "gsv_discord_source_acknowledge",
        "gsv_discord_source_poll",
        "gsv_discord_source_status",
    }
    assert tools["gsv_discord_source_status"]["annotations"]["readOnlyHint"] is True
    assert tools["gsv_discord_source_poll"]["annotations"]["readOnlyHint"] is False
    assert tools["gsv_discord_source_acknowledge"]["annotations"]["readOnlyHint"] is False
    assert "gsv_discord_source_bind" not in tools
    assert "gsv_discord_source_reset" not in tools


def test_fresh_process_cli_binds_connection_and_rejects_ambient_tokens(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    host_path = tmp_path / "host"
    vault = Vault(vault_path)
    vault.initialize(name="Fresh Discord CLI")
    _auth_manager(vault, tmp_path, store_credential=False)
    runtime = _runtime(tmp_path / "synthetic-discord")
    environment = os.environ.copy()
    environment.update(
        {
            "DISCORD_CHANNEL_IDS": CHANNEL,
            "DISCORD_USER_TOKEN": "ambient-user-token-must-be-ignored",
            "DISCORD_BOT_TOKEN": "ambient-bot-token-must-be-ignored",
            "GSV_DATA_DIR": str(host_path),
        }
    )

    def run(*arguments: str) -> dict[str, object]:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuity_kernel",
                "--json",
                "--vault",
                str(vault_path),
                *arguments,
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)["result"]
        assert isinstance(result, dict)
        return result

    binding = run("discord-source", "binding-status")
    assert binding["bindingRevision"] == "absent"
    run(
        "discord-source",
        "bind",
        "--runtime",
        str(runtime),
        "--connection-id",
        str(CONNECTION_ID),
        "--expected-revision",
        "absent",
    )
    bound = run("discord-source", "binding-status")
    assert bound["connectionId"] == CONNECTION_ID
    assert bound["migrationRequired"] is False
    run("source", "select", "--expected-revision", "absent", "--source", "discord")
    status = run("discord-source", "status")
    assert status["available"] is False
    assert status["errorCode"] == "auth_required"
    assert not (host_path / "discord-source" / "state").exists()

    durable = b"".join(
        path.read_bytes()
        for root in (vault_path, host_path)
        for path in root.rglob("*")
        if path.is_file()
    )
    assert b"ambient-user-token-must-be-ignored" not in durable
    assert b"ambient-bot-token-must-be-ignored" not in durable
    assert CHANNEL.encode() not in (vault_path / "SOURCES.md").read_bytes()
