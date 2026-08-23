from __future__ import annotations

import plistlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import continuity_kernel.apple_messages as apple_adapter
from continuity_kernel.errors import ConflictError, ContinuityError, ValidationError
from continuity_kernel.local_source_delivery import (
    DISCARD_STALE_DELIVERY,
    FORWARD_ONLY_RESET,
    LocalSourceDelivery,
)
from continuity_kernel.resident_signals import ResidentSignalStore
from continuity_kernel.sense_sweep import order_due_sources, sense_sweep
from continuity_kernel.source_state import (
    empty_source_snapshot,
    record_source_observation,
    select_sources,
)
from continuity_kernel.vault import Vault

ACCOUNT = "sha256:" + "a" * 64
TOOL = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64


def test_pulse_source_ordering_whatsapp_mandatory_first() -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    selected = select_sources(
        empty_source_snapshot(),
        ("gmail", "whatsapp", "slack"),
        observed_at=now - timedelta(hours=1),
    )
    # WhatsApp has a fresh successful read 10 minutes ago
    observed = record_source_observation(
        selected,
        source_id="whatsapp",
        actor_ref="task-wa",
        result="success",
        covered_through="2026-08-23T09:50:00Z",
        completeness="complete",
        account_fingerprint=ACCOUNT,
        host_fingerprint=HOST,
        tool_fingerprint=TOOL,
        observed_at=now - timedelta(minutes=10),
    )
    ordered = order_due_sources(observed, observed_at=now)
    # WhatsApp is mandatory on every wake and ordered first
    assert ordered[0] == "whatsapp"
    assert set(ordered) == {"whatsapp", "gmail", "slack"}


def test_pulse_source_ordering_credential_deadline_inside_two_wakes() -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    selected = select_sources(
        empty_source_snapshot(),
        ("box", "github", "google_calendar", "slack"),
        observed_at=now - timedelta(days=2),
    )
    # Box was read 2 days ago and is due by TTL (proof_ttl=1 day)
    box_observed = record_source_observation(
        selected,
        source_id="box",
        actor_ref="task-box",
        result="success",
        covered_through="2026-08-21T10:00:00Z",
        completeness="complete",
        account_fingerprint=ACCOUNT,
        host_fingerprint=HOST,
        tool_fingerprint=TOOL,
        observed_at=now - timedelta(days=2),
    )
    # google_calendar credential expires in 25 minutes (inside 2 wakes / 60m)
    # slack credential expires in 50 minutes (inside 2 wakes / 60m)
    deadlines = {
        "google_calendar": now + timedelta(minutes=25),
        "slack": now + timedelta(minutes=50),
    }
    ordered = order_due_sources(box_observed, observed_at=now, credential_deadlines=deadlines)

    # 1. Credential deadline inside 60 min (earliest first: google_calendar then slack)
    # 2. Never-read (github)
    # 3. Oldest due_at (box)
    assert ordered == ("google_calendar", "slack", "github", "box")


def test_pulse_source_ordering_newly_changed_incident_fingerprint() -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    selected = select_sources(
        empty_source_snapshot(),
        ("box", "github", "figma"),
        observed_at=now - timedelta(days=2),
    )
    # Box read 2 days ago (due by TTL)
    box_observed = record_source_observation(
        selected,
        source_id="box",
        actor_ref="task-box",
        result="success",
        covered_through="2026-08-21T10:00:00Z",
        completeness="complete",
        account_fingerprint=ACCOUNT,
        host_fingerprint=HOST,
        tool_fingerprint=TOOL,
        observed_at=now - timedelta(days=2),
    )
    # figma had an incident that newly changed
    ordered = order_due_sources(
        box_observed,
        observed_at=now,
        changed_incident_sources=frozenset({"figma"}),
    )

    # 1. Newly changed incident (figma)
    # 2. Never-read (github)
    # 3. Oldest due_at (box)
    assert ordered == ("figma", "github", "box")


def test_pulse_source_ordering_never_read_and_oldest_due_at_tie_breakers() -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    selected = select_sources(
        empty_source_snapshot(),
        ("asana", "box", "notion", "github"),
        observed_at=now - timedelta(days=5),
    )
    # box read 4 days ago (due 3 days ago -> older due_at than asana)
    obs1 = record_source_observation(
        selected,
        source_id="box",
        actor_ref="task-box",
        result="success",
        covered_through="2026-08-19T10:00:00Z",
        completeness="complete",
        account_fingerprint=ACCOUNT,
        host_fingerprint=HOST,
        tool_fingerprint=TOOL,
        observed_at=now - timedelta(days=4),
    )
    # asana read 3 days ago (due 2 days ago)
    obs2 = record_source_observation(
        obs1,
        source_id="asana",
        actor_ref="task-asana",
        result="success",
        covered_through="2026-08-20T10:00:00Z",
        completeness="complete",
        account_fingerprint=ACCOUNT,
        host_fingerprint=HOST,
        tool_fingerprint=TOOL,
        observed_at=now - timedelta(days=3),
    )
    # github and notion are never-read -> ordered before older due, tie broken by source_id
    ordered = order_due_sources(obs2, observed_at=now)

    # Never-read: github, notion (alphabetical)
    # Oldest due_at: box (due 3 days ago) before asana (due 2 days ago)
    assert ordered == ("github", "notion", "box", "asana")


def test_unchanged_auth_or_tool_absent_incidents_do_not_fast_retry() -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    selected = select_sources(
        empty_source_snapshot(),
        ("gmail", "slack"),
        observed_at=now - timedelta(hours=2),
    )
    # gmail failed with auth_required 15 minutes ago (proof TTL is 24h)
    failed = record_source_observation(
        selected,
        source_id="gmail",
        actor_ref="task-gmail",
        result="failure",
        account_fingerprint=ACCOUNT,
        host_fingerprint=HOST,
        tool_fingerprint=TOOL,
        error_code="auth_required",
        observed_at=now - timedelta(minutes=15),
    )

    # Unchanged incident: gmail does not fast-retry every wake
    ordered_unchanged = order_due_sources(failed, observed_at=now)
    assert "gmail" not in ordered_unchanged
    assert ordered_unchanged == ("slack",)

    # Newly changed incident: gmail is immediately due
    ordered_changed = order_due_sources(
        failed,
        observed_at=now,
        changed_incident_sources=frozenset({"gmail"}),
    )
    assert ordered_changed == ("gmail", "slack")

    # After full proof TTL (24h), gmail becomes due again
    ordered_after_ttl = order_due_sources(failed, observed_at=now + timedelta(hours=25))
    assert "gmail" in ordered_after_ttl


def test_integration_sense_sweep_orders_slack_deadline_and_dedupes_auth_incident(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Sweep integration test")
    vault.select_sources(
        expected_revision="absent",
        sources=("whatsapp", "slack", "box", "gmail"),
    )

    # 1. Box read 2 days ago (due by TTL 1 day ago)
    rev1 = cast(
        str,
        vault.record_source_observation(
            expected_revision=vault.get_source_snapshot().revision,
            source_id="box",
            actor_ref="task-box",
            result="success",
            covered_through="2026-08-21T10:00:00Z",
            completeness="complete",
            account_binding="box-account",
            tool_binding="box.messages.recent_read",
            observed_at=now - timedelta(days=2),
        )["revision"],
    )

    # 2. Slack read 5h 35m ago (proof_ttl=6h -> deadline in 25m, inside 2 wakes!)
    rev2 = cast(
        str,
        vault.record_source_observation(
            expected_revision=rev1,
            source_id="slack",
            actor_ref="task-slack",
            result="success",
            covered_through="2026-08-23T04:25:00Z",
            completeness="complete",
            account_binding="slack-account",
            tool_binding="slack.messages.recent_read",
            observed_at=now - timedelta(hours=5, minutes=35),
        )["revision"],
    )

    # 3. Gmail failed with auth_required 15 min ago (stable incident)
    rev3 = cast(
        str,
        vault.record_source_observation(
            expected_revision=rev2,
            source_id="gmail",
            actor_ref="task-gmail",
            result="failure",
            account_binding="gmail-account",
            tool_binding="gmail.messages.recent_read",
            error_code="auth_required",
            observed_at=now - timedelta(minutes=15),
        )["revision"],
    )

    # 4. WhatsApp read 10 min ago (mandatory first on every wake)
    vault.record_source_observation(
        expected_revision=rev3,
        source_id="whatsapp",
        actor_ref="task-wa",
        result="success",
        covered_through="2026-08-23T09:50:00Z",
        completeness="complete",
        account_binding="whatsapp-account",
        tool_binding="seld.local.whatsapp-wacli.read.v1",
        observed_at=now - timedelta(minutes=10),
    )

    # Execute first sweep
    result1 = sense_sweep(vault, observed_at=now)
    assert result1.status == "complete"

    signals1 = ResidentSignalStore(vault.root).list().signals
    source_signals1 = [s for s in signals1 if s.kind == "source-due"]
    source_refs1 = [s.ref for s in source_signals1]

    # Deterministic order: WhatsApp mandatory first, Slack credential deadline in 25m,
    # Gmail changed incident, then Box
    assert source_refs1 == [
        "source:whatsapp",
        "source:slack",
        "source:gmail",
        "source:box",
    ]

    # Gmail auth incident was emitted with stable fingerprint key
    gmail_signals1 = [s for s in source_signals1 if s.ref == "source:gmail"]
    assert len(gmail_signals1) == 1
    assert gmail_signals1[0].event_key is not None
    assert gmail_signals1[0].event_key.startswith("source-incident:gmail:")
    initial_count = len(signals1)

    # Second sweep 5 minutes later: unchanged auth incident does NOT duplicate or requeue
    result2 = sense_sweep(vault, observed_at=now + timedelta(minutes=5))
    assert result2.status == "complete"

    signals2 = ResidentSignalStore(vault.root).list().signals
    gmail_signals2 = [s for s in signals2 if s.ref == "source:gmail"]
    # Gmail signal deduped in resident signal store; total count is unchanged
    assert len(gmail_signals2) == 1
    assert len(signals2) == initial_count


def _apple_store(root: Path, *, old_body: str = "already covered") -> Path:
    root.mkdir(parents=True)
    database = root / "chat.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE message (date INTEGER, is_from_me INTEGER, text TEXT, "
            "attributedBody BLOB, item_type INTEGER DEFAULT 0, "
            "associated_message_type INTEGER DEFAULT 0, "
            "cache_has_attachments INTEGER DEFAULT 0)"
        )
        connection.execute("INSERT INTO message VALUES (0, 0, ?, NULL, 0, 0, 0)", (old_body,))
    return database


def _append_apple(
    database: Path,
    body: str,
    *,
    timestamp: int = 800_000_000,
    item_type: int = 0,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO message VALUES (?, 0, ?, NULL, ?, 0, 0)",
            (timestamp, body, item_type),
        )


def test_apple_messages_same_store_stale_delivery_discard_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preferences = tmp_path / "test-home/Library/Preferences/com.apple.imservice.ids.iMessage.plist"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_bytes(plistlib.dumps({"ActiveAccounts": ["test-active-account"]}))
    monkeypatch.setattr(apple_adapter, "default_account_preferences", lambda: preferences)

    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Apple recovery test")
    vault.select_sources(expected_revision="absent", sources=("apple_messages", "whatsapp"))

    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")

    _append_apple(database, "message pending sequence 8")
    delivery.poll("apple_messages")
    before = delivery.status("apple_messages")
    assert before["pending"] is True
    assert before["sequence"] == 0
    token_digest = cast(str, before["pending_token_digest"])

    # Guard: reject discard_stale_delivery for sources other than apple_messages (e.g. WhatsApp)
    with pytest.raises(ValidationError, match="only supported for apple_messages"):
        delivery.rebaseline(
            "whatsapp",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition=DISCARD_STALE_DELIVERY,
            expected_source_revision=vault.get_source_snapshot().revision,
        )

    # Guard: reject discard_stale_delivery when Apple pending batch is healthy
    healthy_gap_rev = cast(
        str,
        vault.record_source_observation(
            expected_revision=vault.get_source_snapshot().revision,
            source_id="apple_messages",
            actor_ref="task-apple-healthy",
            result="success",
            covered_through="2026-08-23T09:00:00Z",
            completeness="partial",
            evidence_refs=(f"seld-local-source-gap:{token_digest}",),
            account_binding="test-active-account",
            tool_binding="seld.local.apple-messages.read.v1",
        )["revision"],
    )
    with pytest.raises(ConflictError, match="pending delivery is healthy"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition=DISCARD_STALE_DELIVERY,
            expected_source_revision=healthy_gap_rev,
        )

    # Tamper with the database content so delivered content changed
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE message SET text = 'tampered' WHERE text = 'message pending sequence 8'"
        )

    # Replay/poll fails with delivered content changed
    with pytest.raises(ContinuityError, match="content changed"):
        delivery.poll("apple_messages")

    # Existing forward_only_reset rejects same-store identity
    with pytest.raises(ConflictError, match="store identity has not changed"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition=FORWARD_ONLY_RESET,
        )

    # CAS and explicit disposition validation
    with pytest.raises(ConflictError, match="checkpoint changed"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=99,
            disposition=DISCARD_STALE_DELIVERY,
            expected_source_revision=vault.get_source_snapshot().revision,
        )
    with pytest.raises(ValidationError, match="requires disposition"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition="invalid_disposition",
        )

    # Fail-closed guard: missing expected_source_revision
    with pytest.raises(ValidationError, match="requires a valid expected_source_revision"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition=DISCARD_STALE_DELIVERY,
        )

    # Fail-closed guard: stale expected_source_revision
    with pytest.raises(ConflictError, match="revision does not match"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition=DISCARD_STALE_DELIVERY,
            expected_source_revision="0" * 64,
        )

    # Fail-closed guard: observation evidence digest does not match stale delivery
    mismatched_obs_rev = cast(
        str,
        vault.record_source_observation(
            expected_revision=healthy_gap_rev,
            source_id="apple_messages",
            actor_ref="task-apple-mismatched",
            result="success",
            covered_through="2026-08-23T09:00:00Z",
            completeness="partial",
            evidence_refs=("seld-local-source-gap:sha256:" + "f" * 64,),
            account_binding="test-active-account",
            tool_binding="seld.local.apple-messages.read.v1",
        )["revision"],
    )
    with pytest.raises(ConflictError, match="does not verify the exact stale delivery"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition=DISCARD_STALE_DELIVERY,
            expected_source_revision=mismatched_obs_rev,
        )

    # Persist the required content-free partial gap observation for this exact delivery
    valid_gap_rev = cast(
        str,
        vault.record_source_observation(
            expected_revision=mismatched_obs_rev,
            source_id="apple_messages",
            actor_ref="task-apple-gap-verified",
            result="success",
            covered_through="2026-08-23T09:00:00Z",
            completeness="partial",
            evidence_refs=(f"seld-local-source-gap:{token_digest}",),
            account_binding="test-active-account",
            tool_binding="seld.local.apple-messages.read.v1",
        )["revision"],
    )

    # Guarded recovery via discard_stale_delivery succeeds
    repaired = delivery.rebaseline(
        "apple_messages",
        expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
        expected_sequence=cast(int, before["sequence"]),
        disposition=DISCARD_STALE_DELIVERY,
        expected_source_revision=valid_gap_rev,
    )
    assert repaired["already_rebaselined"] is False
    assert repaired["pending_delivery_discarded"] is True
    assert repaired["disposition"] == DISCARD_STALE_DELIVERY
    assert repaired["source_health"] == "needs_reproof"
    assert repaired["sequence"] == 1

    after = delivery.status("apple_messages")
    assert after["pending"] is False
    assert after["source_health"] == "needs_reproof"
    assert after["sequence"] == 1

    # Fresh forward poll succeeds without error
    _append_apple(database, "fresh forward message after recovery", timestamp=800_000_001)
    poll_result = delivery.poll("apple_messages")
    assert poll_result["delivery"]["ack_required"] is True
    assert any(
        m.get("body") == "fresh forward message after recovery" for m in poll_result["messages"]
    )


def test_apple_messages_discard_recovery_rejects_complete_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preferences = tmp_path / "test-home/Library/Preferences/com.apple.imservice.ids.iMessage.plist"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_bytes(plistlib.dumps({"ActiveAccounts": ["test-active-account"]}))
    monkeypatch.setattr(apple_adapter, "default_account_preferences", lambda: preferences)

    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Apple complete observation test")
    vault.select_sources(expected_revision="absent", sources=("apple_messages",))

    store = tmp_path / "Messages"
    database = _apple_store(store)
    delivery = LocalSourceDelivery(vault, store_root=store)
    delivery.baseline("apple_messages")

    _append_apple(database, "message to discard")
    delivery.poll("apple_messages")
    before = delivery.status("apple_messages")
    token_digest = cast(str, before["pending_token_digest"])

    complete_obs_rev = cast(
        str,
        vault.record_source_observation(
            expected_revision=vault.get_source_snapshot().revision,
            source_id="apple_messages",
            actor_ref="task-apple-complete",
            result="success",
            covered_through="2026-08-23T09:00:00Z",
            completeness="complete",
            evidence_refs=(f"seld-local-source-gap:{token_digest}",),
            account_binding="test-active-account",
            tool_binding="seld.local.apple-messages.read.v1",
        )["revision"],
    )

    # Tamper with the database content so delivered content changed
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE message SET text = 'tampered' WHERE text = 'message to discard'")

    with pytest.raises(ConflictError, match="must record partial completeness"):
        delivery.rebaseline(
            "apple_messages",
            expected_checkpoint_digest=cast(str, before["checkpoint_digest"]),
            expected_sequence=cast(int, before["sequence"]),
            disposition=DISCARD_STALE_DELIVERY,
            expected_source_revision=complete_obs_rev,
        )
