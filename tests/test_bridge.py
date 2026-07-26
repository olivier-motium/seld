from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from collections.abc import Iterator
from http import HTTPStatus
from http.client import BadStatusLine, HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, urlopen

import pytest

from continuity_kernel import __version__, bridge, bridge_projection, mcp_server
from continuity_kernel import control_queue as control_queue_module
from continuity_kernel.config import data_dir
from continuity_kernel.control_queue import (
    CONTROL_STORE_SUPPORTED,
    EMPTY_REVISION,
    ControlQueue,
    ControlStorageError,
)
from continuity_kernel.errors import MutationCommittedError, SetupError, ValidationError
from continuity_kernel.operations import OperationLedger
from continuity_kernel.portfolio import ABSENT_PORTFOLIO_REVISION, portfolio_item
from continuity_kernel.records import review_coverage_ref, review_option_ref
from continuity_kernel.vault import Vault, doctor_dict

INSTANCE_ID = "a" * 32
ACCESS_TOKEN = "b" * 48
REVIEW_HAND_ID = "019f95fd-009e-7603-ab87-f9927cf31c4d"


def test_codex_metadata_degrades_when_provider_process_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_status() -> dict[str, Any]:
        raise FileNotFoundError("missing Codex executable")

    monkeypatch.setattr(bridge, "codex_status", fail_status)

    result = bridge._codex_metadata()

    assert result == {"available": False, "error": "missing Codex executable"}


def test_guided_review_deep_link_fallback_matches_installed_skill_contract() -> None:
    prompt = " ".join(bridge_projection._GUIDED_REVIEW_PROMPT.split()).casefold()
    skill_resource = (
        files("continuity_kernel")
        / "resources"
        / "marketplace"
        / "plugins"
        / "gsv"
        / "skills"
        / "gsv"
        / "SKILL.md"
    )
    skill = " ".join(skill_resource.read_text(encoding="utf-8").split()).casefold()
    shared_contract_markers = (
        "thread:life-portfolio-review",
        "workthreads and entities",
        "review-state:paused",
        "review-covered:task:<id>@<task-revision>",
        "|thread:<thread-id>@<thread-revision>",
        "direction cas when relevant",
        "complete portfolio cas when affected",
        "new open outcomes",
        "active hand",
        "workthread focus",
        "raw codex thread uuid",
        "active_thread_id",
        "gsv workthread id",
        "codex-thread:*",
        "status=waiting",
        "next_actor=human",
        "next_action",
        "waiting_on",
        "terminalize the session",
    )
    for marker in shared_contract_markers:
        assert marker in prompt
        assert marker in skill
    for semantic_word in ("checked", "never", "resolved", "clear"):
        assert semantic_word in prompt
        assert semantic_word in skill

    bridge_javascript = (
        files("continuity_kernel") / "resources" / "bridge" / "bridge.js"
    ).read_text(encoding="utf-8")
    assert "subject.contradictions" not in bridge_javascript
    assert "exactHandFallback(review.hand_url)" not in bridge_javascript


@pytest.fixture
def running_bridge(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[bridge.BridgeHTTPServer, str]]:
    monkeypatch.setattr(
        bridge,
        "codex_status",
        lambda: {
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
            "ready": True,
        },
    )
    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as static_root:
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, 0),
            vault,
            Path(static_root),
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://{bridge.LOOPBACK_HOST}:{server.server_address[1]}"
        try:
            yield server, base
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _request(url: str, *, token: str | None = None, origin: str | None = None) -> Request:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return Request(url, headers=headers)


def _state(vault: Vault, *, port: int = 43117, pid: int = 4242) -> dict[str, object]:
    return {
        "format_version": bridge.STATE_VERSION,
        "instance_id": INSTANCE_ID,
        "pid": pid,
        "port": port,
        "token": ACCESS_TOKEN,
        "url": f"http://{bridge.LOOPBACK_HOST}:{port}/",
        "vault": str(vault.root),
        "vault_id": vault.identity()["vault_id"],
    }


def _health_response(payload: dict[str, object]) -> bridge._HealthProbe:
    return bridge._HealthProbe(bridge._HealthOutcome.RESPONSE, payload)


def _health_unavailable() -> bridge._HealthProbe:
    return bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_snapshot_projects_authored_records_and_codex_links(vault: Vault) -> None:
    task = vault.create_task(
        identifier="ship-atlas",
        title="Ship Atlas",
        outcome="The migration is complete.",
        status="doing",
        next_actor="agent",
        next_action="Run the acceptance suite.",
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
            "ready": True,
        },
    )

    projected = next(item for item in snapshot["tasks"] if item["identifier"] == task.identifier)
    link = urlsplit(projected["codex_url"])
    query = parse_qs(link.query)
    assert link.scheme == "codex"
    assert link.netloc == "new"
    assert query["originUrl"] == [bridge.REPOSITORY_URL]
    assert query["path"] == [str(vault.root)]
    assert "ship-atlas" in query["prompt"][0]
    assert snapshot["codex"]["ready"] is True
    assert "new_mind_url" not in snapshot["codex"]
    new_hand = parse_qs(urlsplit(snapshot["codex"]["new_hand_url"]).query)
    assert new_hand == {
        "originUrl": [bridge.REPOSITORY_URL],
        "path": [str(vault.root)],
        "prompt": [
            "Start a new GSV hand. Read the installed GSV context and exact current records "
            "before deciding what deserves attention."
        ],
    }
    assert all(
        forbidden not in new_hand["prompt"][0].casefold()
        for forbidden in ("ship-atlas", "resume", "continue", "first run")
    )
    assert snapshot["projection"]["sections"]["tasks"] == {
        "issues": [],
        "readable": 1,
        "state": "complete",
        "unreadable": 0,
    }
    assert snapshot["bridge"] == {
        "control_queue": CONTROL_STORE_SUPPORTED,
        "local": True,
        "semantic_write": False,
        "version": __version__,
    }
    if os.name == "nt":
        assert snapshot["controls"] == {
            "archived_decided": None,
            "available": False,
            "decided": None,
            "disposition_revision": None,
            "generation": None,
            "history": [],
            "items": [],
            "pending": None,
            "queue_revision": None,
            "review_prompt": None,
            "review_url": None,
            "state": "unavailable",
        }
    else:
        review_prompt = snapshot["controls"]["review_prompt"]
        review_url = snapshot["controls"]["review_url"]
        assert "This is review only" in review_prompt
        assert "do not apply the requested change" in review_prompt
        assert parse_qs(urlsplit(review_url).query)["prompt"] == [review_prompt]
        assert snapshot["controls"] == {
            "archived_decided": 0,
            "available": True,
            "decided": 0,
            "disposition_revision": EMPTY_REVISION,
            "generation": 0,
            "history": [],
            "items": [],
            "pending": 0,
            "queue_revision": EMPTY_REVISION,
            "review_prompt": review_prompt,
            "review_url": review_url,
            "state": "ready",
        }


def test_snapshot_projects_one_exact_guided_review_subject_without_inference(
    vault: Vault,
) -> None:
    first = vault.create_task(
        identifier="first-outcome",
        title="First exact outcome",
        outcome="Decide whether this still earns its place.",
        status="ready",
        next_actor="human",
        rank=10,
    )
    second = vault.create_task(
        identifier="second-outcome",
        title="Second exact outcome",
        outcome="Choose its real horizon.",
        status="ready",
        next_actor="human",
        rank=20,
    )
    keep_option = review_option_ref(
        intent="keep",
        subject_task_id="first-outcome",
        consequence="Leave the outcome unchanged and check only this review step.",
    )
    session = vault.create_task(
        identifier="review-session",
        title="Review every open outcome",
        outcome="Check each outcome without equating checked with resolved.",
        status="waiting",
        next_actor="human",
        next_action="Keep this current and advance.",
        waiting_on="Should I keep it unchanged?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            "review-subject:task:first-outcome",
            keep_option,
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One review session is active.",
        status="active",
        next_move="Continue the exact focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    portfolio = vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="This is the complete authored outcome set.",
        items=(
            portfolio_item(
                task_id_value=first.identifier,
                task_revision=first.revision,
                stance="needs-human",
                reason="Check whether it is current.",
            ),
            portfolio_item(
                task_id_value=second.identifier,
                task_revision=second.revision,
                stance="needs-human",
                reason="Check its horizon after the first outcome.",
            ),
        ),
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )
    review = snapshot["portfolio"]["review"]
    assert snapshot["portfolio"]["revision"] == portfolio.revision
    assert review["state"] == "active"
    assert review["actionable"] is True
    assert review["subject_task_id"] == first.identifier
    assert review["subject"]["position"] == 1
    assert review["recommendation"] == "Keep this current and advance."
    assert review["question"] == "Should I keep it unchanged?"
    assert review["checked_count"] == 0
    assert review["checked_current_count"] == 0
    assert review["uncovered_count"] == 2
    assert review["options"] == [
        {
            "consequence": "Leave the outcome unchanged and check only this review step.",
            "intent": "keep",
        }
    ]
    assert "contradictions" not in review["subject"]

    changed_second = vault.update_task(
        second.identifier,
        expected_revision=second.revision,
        next_action="An unrelated newer action.",
    )
    assert changed_second.revision != second.revision
    drifted = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )
    assert drifted["portfolio"]["state"] == "stale"
    assert drifted["portfolio"]["stale_count"] == 1
    assert drifted["portfolio"]["review"]["actionable"] is True

    advanced = vault.update_task(
        session.identifier,
        expected_revision=session.revision,
        remove_refs=("review-subject:task:first-outcome", keep_option),
        add_refs=(
            review_coverage_ref(
                task_id_value=first.identifier,
                task_revision=first.revision,
            ),
            "review-subject:task:second-outcome",
        ),
        next_action="Reauthor the stale anchor before asking about the second outcome.",
        waiting_on="Should its horizon change?",
    )
    assert advanced.revision != session.revision
    checked = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )["portfolio"]["review"]
    assert checked["checked_count"] == 1
    assert checked["checked_current_count"] == 1
    assert vault.get_task(first.identifier).status == "ready"
    assert checked["state"] == "conflict"
    assert checked["actionable"] is False
    assert "stale" in checked["issue"].casefold()


def test_snapshot_reenters_changed_coverage_and_includes_new_open_outcomes(
    vault: Vault,
) -> None:
    outcome = vault.create_task(
        identifier="anchored-outcome",
        title="Anchored outcome",
        outcome="Remain checked only while its exact context remains current.",
        status="ready",
        next_actor="human",
    )
    owner = vault.create_thread(
        identifier="thread:anchored-work",
        title="Anchored work",
        purpose="Own the exact outcome context.",
        summary="The original context is current.",
        status="active",
        next_move="Keep the context explicit.",
        task_ids=(outcome.identifier,),
    )
    coverage = review_coverage_ref(
        task_id_value=outcome.identifier,
        task_revision=outcome.revision,
        work_thread_id=owner.identifier,
        work_thread_revision=owner.revision,
    )
    session = vault.create_task(
        identifier="anchored-review",
        title="Review every open outcome",
        outcome="Check exact current outcomes without inventing completion.",
        status="waiting",
        next_actor="human",
        next_action="Choose the next exact subject deliberately.",
        waiting_on="Which current outcome should come next?",
        active_thread_id=REVIEW_HAND_ID,
        refs=("review-scope:all-open", coverage),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One review session is active.",
        status="active",
        next_move="Continue the exact focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="The complete current open set.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="Keep this visible while its owning context is current.",
                work_thread_id=owner.identifier,
                work_thread_revision=owner.revision,
            ),
        ),
    )

    current = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )["portfolio"]["review"]
    assert current["checked_current_count"] == 1
    assert current["uncovered_count"] == 0

    changed_owner = vault.update_thread(
        owner.identifier,
        expected_revision=owner.revision,
        summary="The owning context materially changed after review.",
    )
    assert changed_owner.revision != owner.revision
    later = vault.create_task(
        identifier="later-open-outcome",
        title="Later open outcome",
        outcome="Join the exact all-open scope after this session began.",
        status="ready",
        next_actor="human",
    )

    changed = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )["portfolio"]["review"]
    assert changed["checked_count"] == 1
    assert changed["checked_current_count"] == 0
    assert changed["revisit_task_ids"] == [outcome.identifier]
    assert changed["new_open_task_ids"] == [later.identifier]
    assert changed["uncovered_task_ids"] == [outcome.identifier, later.identifier]
    assert changed["uncovered_count"] == 2


def test_snapshot_marks_a_new_workthread_owner_as_stale_and_nonactionable(
    vault: Vault,
) -> None:
    outcome = vault.create_task(
        identifier="unthreaded-subject",
        title="Unthreaded subject",
        outcome="Remain current only while the exact unthreaded anchor holds.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="ownership-review",
        title="Review every open outcome",
        outcome="Check exact current outcomes without inventing ownership.",
        status="waiting",
        next_actor="human",
        next_action="Keep the exact owner relation visible.",
        waiting_on="Does this still belong outside a storyline?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            f"review-subject:task:{outcome.identifier}",
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One review session is active.",
        status="active",
        next_move="Continue the exact focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="The subject is deliberately unthreaded.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="Keep the exact owner relation explicit.",
            ),
        ),
    )
    current = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )["portfolio"]
    assert current["state"] == "current"
    assert current["review"]["actionable"] is True

    gained_owner = vault.create_thread(
        identifier="thread:new-owner",
        title="New exact owner",
        purpose="Own the subject after Portfolio authorship.",
        summary="This owner relation is newer than the Portfolio anchor.",
        status="active",
        next_move="Reauthor the Portfolio before continuing the review.",
        task_ids=(outcome.identifier,),
    )

    drifted = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )["portfolio"]
    assert drifted["state"] == "stale"
    assert drifted["stale_count"] == 1
    assert drifted["items"][0]["thread_stale"] is True
    assert drifted["items"][0]["work_thread"]["identifier"] == gained_owner.identifier
    assert drifted["review"]["state"] == "conflict"
    assert drifted["review"]["actionable"] is False


def test_snapshot_never_exposes_review_controls_across_inspection_race(
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = vault.create_task(
        identifier="racing-subject",
        title="Racing subject",
        outcome="Never expose a control against mixed canonical revisions.",
        status="ready",
        next_actor="human",
    )
    session = vault.create_task(
        identifier="racing-review",
        title="Review every open outcome",
        outcome="Check exact current outcomes only.",
        status="waiting",
        next_actor="human",
        next_action="Keep the exact evidence boundary.",
        waiting_on="Is this exact revision still current?",
        active_thread_id=REVIEW_HAND_ID,
        refs=(
            "review-scope:all-open",
            f"review-subject:task:{outcome.identifier}",
        ),
    )
    vault.create_thread(
        identifier="thread:life-portfolio-review",
        title="Finite Portfolio reviews",
        purpose="Own only bounded all-open review sessions.",
        summary="One review session is active.",
        status="active",
        next_move="Continue the exact focused review session.",
        focus_task_id=session.identifier,
        task_ids=(session.identifier,),
    )
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="One exact anchored subject.",
        items=(
            portfolio_item(
                task_id_value=outcome.identifier,
                task_revision=outcome.revision,
                stance="needs-human",
                reason="The control is safe only on this exact revision.",
            ),
        ),
    )
    earlier_inspection = vault.inspect_portfolio()
    changed = vault.update_task(
        outcome.identifier,
        expected_revision=outcome.revision,
        next_action="This revision landed after the locked inspection.",
    )
    assert changed.revision != outcome.revision
    monkeypatch.setattr(vault, "inspect_portfolio", lambda: earlier_inspection)

    mixed = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )["portfolio"]
    assert mixed["review"]["state"] == "active"
    assert mixed["review"]["subject"]["stale"] is True
    assert mixed["review"]["subject"]["staleness"]
    assert mixed["review"]["actionable"] is False


def test_snapshot_projects_an_empty_open_scope_as_finished(vault: Vault) -> None:
    vault.set_portfolio(
        expected_revision=ABSENT_PORTFOLIO_REVISION,
        summary="There are no current open outcomes.",
        items=(),
    )

    review = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "ready": True},
    )["portfolio"]["review"]

    assert review["state"] == "finished"
    assert review["open_count"] == 0
    assert review["uncovered_count"] == 0


def test_snapshot_exposes_mind_shaping_only_for_a_proven_empty_ledger(vault: Vault) -> None:
    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
            "ready": True,
        },
    )

    mind_link = urlsplit(snapshot["codex"]["new_mind_url"])
    hand_link = urlsplit(snapshot["codex"]["new_hand_url"])
    assert (mind_link.scheme, mind_link.netloc) == ("codex", "new")
    assert (hand_link.scheme, hand_link.netloc) == ("codex", "new")
    assert parse_qs(mind_link.query)["path"] == [str(vault.root)]
    assert "$gsv-onboard" in parse_qs(mind_link.query)["prompt"][0]
    assert parse_qs(hand_link.query)["originUrl"] == [bridge.REPOSITORY_URL]


def test_missing_secure_pinned_storage_degrades_only_the_control_lane(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, base = running_bridge
    task = server.vault.create_task(
        identifier="canonical-survives-control-platform-gap",
        title="Keep canonical records readable",
        outcome="A missing secure control-store backend cannot hide canonical work.",
    )

    def unavailable_store(_root: Path) -> None:
        raise ValidationError("secure directory-pinned storage is unavailable on this platform")

    monkeypatch.setattr(control_queue_module, "PinnedPathRoot", unavailable_store)

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        snapshot = json.loads(response.read())

    assert any(item["identifier"] == task.identifier for item in snapshot["tasks"])
    assert snapshot["controls"]["state"] == "unavailable"
    assert not (server.vault.root / ".gsv/control").exists()

    request = Request(
        f"{base}/api/v1/control",
        data=json.dumps(
            {
                "choice": "Do not create an insecure fallback.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": "mind:user-correction",
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as rejected:
        urlopen(request, timeout=2)

    assert rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    assert not (server.vault.root / ".gsv/control").exists()


@pytest.mark.parametrize("status", ["done", "dropped"])
def test_terminal_history_gets_new_hand_but_never_resume_or_first_run(
    vault: Vault, status: str
) -> None:
    terminal = vault.create_task(
        identifier=f"terminal-{status}",
        title=f"Terminal {status}",
        outcome="The exact outcome remains in the closed record.",
        status=status,
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
            "ready": True,
        },
    )

    projected = next(
        item for item in snapshot["tasks"] if item["identifier"] == terminal.identifier
    )
    assert "codex_url" not in projected
    assert "new_mind_url" not in snapshot["codex"]
    assert snapshot["codex"]["new_hand_url"].startswith("codex://new?")


def test_only_nonterminal_tasks_receive_resume_links(vault: Vault) -> None:
    vault.create_task(
        identifier="closed",
        title="Closed",
        outcome="Closed deliberately.",
        status="done",
    )
    vault.create_task(
        identifier="open",
        title="Open",
        outcome="Still open.",
        status="ready",
        next_actor="agent",
        next_action="Continue from exact current truth.",
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
            "ready": True,
        },
    )
    by_id = {task["identifier"]: task for task in snapshot["tasks"]}

    assert "codex_url" not in by_id["closed"]
    assert (
        "Resume the GSV commitment `open`"
        in parse_qs(urlsplit(by_id["open"]["codex_url"]).query)["prompt"][0]
    )
    assert "new_mind_url" not in snapshot["codex"]


@pytest.mark.parametrize(
    "integration",
    [
        {"available": False, "instructions_installed": False, "plugin_installed": False},
        {"available": True, "instructions_installed": False, "plugin_installed": True},
        {"available": True, "instructions_installed": True, "plugin_installed": False},
    ],
)
def test_snapshot_withholds_every_codex_url_until_integration_is_ready(
    vault: Vault, integration: dict[str, bool]
) -> None:
    vault.create_task(
        identifier="not-actionable",
        title="Not actionable",
        outcome="No deep link is exposed before setup is complete.",
    )

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration=integration,
    )

    assert snapshot["codex"]["ready"] is False
    assert "new_mind_url" not in snapshot["codex"]
    assert all("codex_url" not in task for task in snapshot["tasks"])


def test_snapshot_never_calls_full_status_or_logical_digest(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Bridge snapshots must not hash the full vault")

    monkeypatch.setattr(vault, "status", forbidden)
    monkeypatch.setattr(vault, "logical_digest", forbidden)

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": True, "plugin_installed": True},
    )

    assert snapshot["status"]["vault_id"] == vault.identity()["vault_id"]
    assert snapshot["status"]["counts"] == {"tasks": 0, "entities": 0, "threads": 0}
    assert "digest" not in snapshot["status"]


def test_authenticated_snapshot_keeps_valid_tasks_when_one_record_is_malformed(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    valid = server.vault.create_task(
        identifier="still-readable",
        title="Still readable",
        outcome="This exact valid record remains visible.",
        status="ready",
        next_actor="agent",
        next_action="Keep the valid record visible.",
    )
    invalid = server.vault.root / "tasks/broken.md"
    invalid.write_text("# Missing typed metadata\n", encoding="utf-8")
    nonregular = server.vault.root / "tasks/not-a-file.md"
    nonregular.mkdir()

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        assert response.status == HTTPStatus.OK
        snapshot = json.loads(response.read())

    assert [task["identifier"] for task in snapshot["tasks"]] == [valid.identifier]
    section = snapshot["projection"]["sections"]["tasks"]
    assert section["state"] == "partial"
    assert section["readable"] == 1
    assert section["unreadable"] == 2
    assert {issue["path"] for issue in section["issues"]} == {
        "tasks/broken.md",
        "tasks/not-a-file.md",
    }
    assert snapshot["doctor"]["healthy"] is False
    assert {issue["path"] for issue in snapshot["doctor"]["issues"]} >= {
        "tasks/broken.md",
        "tasks/not-a-file.md",
    }

    with pytest.raises(ValidationError):
        server.vault.list_tasks()
    with pytest.raises(ValidationError):
        server.vault.status()
    with pytest.raises(ValidationError):
        server.vault.context_pack()
    with pytest.raises(ValidationError):
        mcp_server._call("gsv_context", {"max_characters": 4_000}, vault=server.vault)
    doctor = doctor_dict(server.vault.doctor())
    assert {issue["path"] for issue in doctor["issues"]} >= {
        "tasks/broken.md",
        "tasks/not-a-file.md",
    }


def test_health_probe_turns_malformed_http_response_into_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge,
        "_open_loopback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BadStatusLine("broken")),
    )

    probe = bridge._probe_health("http://127.0.0.1:43117/", token=ACCESS_TOKEN, timeout=0)

    assert probe.outcome is bridge._HealthOutcome.UNAVAILABLE


def test_loopback_health_requests_disable_ambient_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = object()
    observed: list[object] = []

    class ProxyFreeOpener:
        def open(self, request: Request, *, timeout: float) -> object:
            observed.extend((request, timeout))
            return response

    def build_proxy_free_opener(*handlers: object) -> ProxyFreeOpener:
        observed.extend(handlers)
        return ProxyFreeOpener()

    monkeypatch.setattr(bridge, "build_opener", build_proxy_free_opener)
    request = Request("http://127.0.0.1:43117/api/v1/health")

    result = bridge._open_loopback(request, timeout=0.5)

    assert result is response
    assert isinstance(observed[0], ProxyHandler)
    assert cast(Any, observed[0]).proxies == {}
    assert isinstance(observed[1], bridge._RejectRedirects)
    assert observed[2:] == [request, 0.5]


@pytest.mark.parametrize(
    "url",
    [
        "http://attacker.example:43117/api/v1/health",
        "http://localhost:43117/api/v1/health",
        "https://127.0.0.1:43117/api/v1/health",
        "http://127.0.0.1/api/v1/health",
        "http://127.0.0.1:0/api/v1/health",
    ],
)
def test_loopback_health_requests_reject_non_exact_loopback_before_opening(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_build_opener(*_handlers: object) -> object:
        raise AssertionError("an invalid health URL must not reach the network")

    monkeypatch.setattr(bridge, "build_opener", forbidden_build_opener)

    with pytest.raises(ValueError, match=r"only http://127\.0\.0\.1:<port>"):
        bridge._open_loopback(Request(url), timeout=0.5)


def test_loopback_health_request_refuses_redirect_before_sink_request() -> None:
    sink_requests: list[str | None] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            sink_requests.append(self.headers.get("Authorization"))
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    sink = ThreadingHTTPServer((bridge.LOOPBACK_HOST, 0), SinkHandler)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header(
                "Location", f"http://{bridge.LOOPBACK_HOST}:{sink.server_address[1]}/sink"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    redirect = ThreadingHTTPServer((bridge.LOOPBACK_HOST, 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    original = f"http://{bridge.LOOPBACK_HOST}:{redirect.server_address[1]}/health"
    try:
        request = Request(original, headers={"Authorization": "Bearer synthetic-token"})
        with pytest.raises(HTTPError) as rejected:
            bridge._open_loopback(request, timeout=2)

        assert rejected.value.code == HTTPStatus.FOUND
        assert rejected.value.url == original
        assert sink_requests == []
    finally:
        redirect.shutdown()
        redirect_thread.join(timeout=3)
        redirect.server_close()
        sink.shutdown()
        sink_thread.join(timeout=3)
        sink.server_close()


def test_only_bad_tasks_return_http_200_but_never_become_a_first_run(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    invalid = server.vault.root / "tasks/only-bad.md"
    invalid.write_bytes(b"\xff\xfe\x00")

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        assert response.status == HTTPStatus.OK
        snapshot = json.loads(response.read())
    ready_snapshot = bridge.bridge_snapshot(
        server.vault,
        doctor=doctor_dict(server.vault.doctor()),
        integration={
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
            "ready": True,
        },
    )

    assert snapshot["tasks"] == []
    assert snapshot["projection"]["sections"]["tasks"]["state"] == "partial"
    assert snapshot["projection"]["sections"]["tasks"]["readable"] == 0
    assert "new_mind_url" not in ready_snapshot["codex"]
    assert ready_snapshot["codex"]["new_hand_url"].startswith("codex://new?")


def test_malformed_entities_and_threads_degrade_only_their_sections(vault: Vault) -> None:
    task = vault.create_task(
        identifier="healthy-task",
        title="Healthy task",
        outcome="The task section remains complete.",
    )
    (vault.root / "entities/broken.md").write_text("not an entity\n", encoding="utf-8")
    (vault.root / "threads/broken.md").write_bytes(b"\xff\xfe")

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=doctor_dict(vault.doctor()),
        integration={"available": False},
    )
    sections = snapshot["projection"]["sections"]

    assert [item["identifier"] for item in snapshot["tasks"]] == [task.identifier]
    assert sections["tasks"]["state"] == "complete"
    assert sections["entities"]["state"] == "partial"
    assert sections["threads"]["state"] == "partial"
    assert sections["entities"]["issues"][0]["path"] == "entities/broken.md"
    assert sections["threads"]["issues"][0]["path"] == "threads/broken.md"


def test_missing_or_linked_record_directory_is_unavailable_not_empty(
    running_bridge: tuple[bridge.BridgeHTTPServer, str], tmp_path: Path
) -> None:
    server, base = running_bridge
    tasks = server.vault.root / "tasks"
    tasks.rmdir()

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        assert response.status == HTTPStatus.OK
        missing = json.loads(response.read())
    assert missing["projection"]["sections"]["tasks"]["state"] == "unavailable"
    assert missing["projection"]["sections"]["tasks"]["issues"][0]["path"] == "tasks"
    assert "new_mind_url" not in missing["codex"]

    outside = tmp_path / "outside-tasks"
    outside.mkdir()
    try:
        tasks.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")
    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        assert response.status == HTTPStatus.OK
        linked = json.loads(response.read())
    assert linked["projection"]["sections"]["tasks"]["state"] == "unavailable"
    assert linked["tasks"] == []
    assert linked["doctor"]["healthy"] is False


def test_failed_section_enumeration_is_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached_doctor = doctor_dict(vault.doctor())
    original_scandir = os.scandir

    def fail_tasks(path: str | os.PathLike[str]) -> Any:
        if Path(path) == vault.root / "tasks":
            raise OSError("injected enumeration failure")
        return original_scandir(path)

    monkeypatch.setattr("continuity_kernel.bridge.os.scandir", fail_tasks)

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=cached_doctor,
        integration={"available": False},
    )

    section = snapshot["projection"]["sections"]["tasks"]
    assert section["state"] == "unavailable"
    assert section["readable"] == 0
    assert section["issues"][0]["path"] == "tasks"


def test_record_created_between_projection_scans_makes_section_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached_doctor = doctor_dict(vault.doctor())
    tasks = vault.root / "tasks"
    original_scandir = os.scandir
    task_scans = 0

    def add_record_before_confirmation(path: str | os.PathLike[str]) -> Any:
        nonlocal task_scans
        if Path(path) == tasks:
            task_scans += 1
            if task_scans == 2:
                (tasks / "appeared-late.md").write_text("not a valid task\n", encoding="utf-8")
        return original_scandir(path)

    monkeypatch.setattr("continuity_kernel.bridge.os.scandir", add_record_before_confirmation)

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=cached_doctor,
        integration={"available": False},
    )

    section = snapshot["projection"]["sections"]["tasks"]
    assert task_scans == 2
    assert section["state"] == "unavailable"
    assert section["readable"] == 0
    assert section["issues"][0]["path"] == "tasks"
    assert "changed during inspection" in section["issues"][0]["message"]


def test_same_named_record_inode_swap_between_projection_scans_is_detected(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = vault.create_task(
        identifier="stable-name",
        title="Stable name",
        outcome="Detect replacement even when its content and name are unchanged.",
    )
    cached_doctor = doctor_dict(vault.doctor())
    task_path = vault.root / "tasks" / f"{task.identifier}.md"
    original_text = task_path.read_text(encoding="utf-8")
    original_inode = os.lstat(task_path).st_ino
    original_scandir = os.scandir
    task_scans = 0

    def replace_record_before_confirmation(path: str | os.PathLike[str]) -> Any:
        nonlocal task_scans
        if Path(path) == vault.root / "tasks":
            task_scans += 1
            if task_scans == 2:
                replacement = vault.root / "tasks/replacement.tmp"
                replacement.write_text(original_text, encoding="utf-8")
                os.replace(replacement, task_path)
        return original_scandir(path)

    monkeypatch.setattr("continuity_kernel.bridge.os.scandir", replace_record_before_confirmation)

    snapshot = bridge.bridge_snapshot(
        vault,
        doctor=cached_doctor,
        integration={"available": False},
    )

    section = snapshot["projection"]["sections"]["tasks"]
    assert task_scans == 2
    assert os.lstat(task_path).st_ino != original_inode
    assert section["state"] == "unavailable"
    assert section["readable"] == 0
    assert section["issues"][0]["path"] == "tasks"


def test_codex_deep_link_round_trips_encoded_prompt_path_and_origin(tmp_path: Path) -> None:
    vault = tmp_path / "GSV vault & proof"
    prompt = "Resume Atlas A&B, then check path / evidence."

    link = urlsplit(bridge.codex_deep_link(vault, prompt))
    query = parse_qs(link.query)

    assert link.scheme == "codex"
    assert link.netloc == "new"
    assert query == {
        "originUrl": [bridge.REPOSITORY_URL],
        "path": [str(vault.resolve())],
        "prompt": [prompt],
    }


def test_packaged_bridge_bundle_contains_ui_assets_and_licenses() -> None:
    resource = files("continuity_kernel") / "resources/bridge"
    expected = {
        "bridge.css",
        "bridge.js",
        "gsv-mark.svg",
        "index.html",
        "licenses/Lucide-ISC.txt",
        "licenses/Thinking-Orbs-MIT.txt",
    }

    with as_file(resource) as root:
        present = {
            path.relative_to(root).as_posix() for path in Path(root).rglob("*") if path.is_file()
        }

    assert expected <= present


def test_bridge_ui_tokens_and_dependency_free_orb_contract() -> None:
    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as root:
        css = "\n".join(
            (Path(root) / name).read_text(encoding="utf-8")
            for name in ("bridge.css", "bridge-components.css", "bridge-responsive.css")
        )
        html = (Path(root) / "index.html").read_text(encoding="utf-8")
        javascript = "\n".join(
            (Path(root) / name).read_text(encoding="utf-8")
            for name in ("bridge.js", "thinking-orbs.js")
        )

    for token in (
        "--text-xs: 12px",
        "--text-sm: 13px",
        "--text-md: 14px",
        "--text-title: 24px",
        "--ink: #292929",
        "--muted: #5d5d5d",
        "--quiet: #9e9e9e",
        "--radius-nav: 8px",
        "--radius-card: 16px",
        "--radius-pill: 999px",
    ):
        assert token in css
    assert '"SF Pro Text"' in css
    assert '"SF Pro Display"' in css
    assert "@font-face" not in css
    assert "letter-spacing: -0.15px" in css
    assert "letter-spacing: 0" not in css
    assert {match.group(1) for match in re.finditer(r"font-weight:\s*(\d+)", css)} == {
        "400",
        "500",
    }
    assert re.search(r"\.nav-icon,[^{]+\{[^}]*width:\s*14px", css, re.DOTALL)
    assert re.search(r"\.thinking-orb\s*\{[^}]*width:\s*20px[^}]*height:\s*20px", css, re.DOTALL)
    assert "thinking-orbs 0.1.1" in javascript
    assert "382be79c472cd600277f01e14f98f8c0ee18dcb0" in javascript
    assert "prefers-reduced-motion: reduce" in javascript
    assert "IntersectionObserver" in javascript
    assert "Math.min(2, window.devicePixelRatio || 1)" in javascript
    assert "snapshotSignature" in javascript
    assert "snapshot.codex.new_hand_url" in javascript
    assert '"new_mind_url"' in javascript
    assert "Nothing is open right now." in javascript
    assert "more · View all work" in javascript
    assert "more not shown." in javascript
    assert 'taskProjection.state !== "complete"' in javascript
    assert 'readable: fallback, state: "unavailable", unreadable: 0' in javascript
    assert 'from "react"' not in javascript
    assert 'aria-live="polite"' not in html
    assert ".woff2" not in bridge._MIME_TYPES


def test_http_surface_requires_per_launch_bearer_for_private_data(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    with urlopen(f"{base}/", timeout=2) as response:
        assert response.status == HTTPStatus.OK
        assert b"Your work in Codex, in one place." in response.read()

    with pytest.raises(HTTPError) as missing:
        urlopen(_request(f"{base}/api/v1/snapshot"), timeout=2)
    assert missing.value.code == HTTPStatus.FORBIDDEN

    with pytest.raises(HTTPError) as wrong:
        urlopen(_request(f"{base}/api/v1/snapshot", token="c" * 48), timeout=2)
    assert wrong.value.code == HTTPStatus.FORBIDDEN

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        payload = json.loads(response.read())
    assert payload["status"]["vault_id"] == server.vault.identity()["vault_id"]

    with urlopen(_request(f"{base}/api/v1/health", token=ACCESS_TOKEN), timeout=2) as response:
        health = json.loads(response.read())
    assert health["instance_id"] == INSTANCE_ID
    assert health["port"] == server.server_address[1]


def test_health_endpoint_uses_only_cached_manifest_identity(
    running_bridge: tuple[bridge.BridgeHTTPServer, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, base = running_bridge

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("health must not scan or validate vault content")

    for name in (
        "identity",
        "status",
        "list_tasks",
        "list_entities",
        "list_threads",
        "doctor",
        "logical_digest",
    ):
        monkeypatch.setattr(server.vault, name, forbidden)

    with urlopen(_request(f"{base}/api/v1/health", token=ACCESS_TOKEN), timeout=2) as response:
        health = json.loads(response.read())

    assert health["vault_id"] == server.vault_id
    assert health["instance_id"] == INSTANCE_ID
    assert health["vault_root_device"] == server.vault_root_identity[0]
    assert health["vault_root_inode"] == server.vault_root_identity[1]


def test_erroneous_head_response_has_no_body_and_keeps_connection_usable(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    _, base = running_bridge
    target = urlsplit(base)
    assert target.port is not None
    connection = HTTPConnection(bridge.LOOPBACK_HOST, target.port, timeout=2)
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        connection.request("HEAD", "/missing", headers=headers)
        missing = connection.getresponse()
        assert missing.status == HTTPStatus.NOT_FOUND
        assert missing.read() == b""

        connection.request("GET", "/api/v1/health", headers=headers)
        healthy = connection.getresponse()
        assert healthy.status == HTTPStatus.OK
        assert json.loads(healthy.read())["service"] == "gsv-bridge"
    finally:
        connection.close()


def test_static_route_rejects_an_in_tree_symlink(vault: Vault, tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "real.js").write_text("console.log('real');\n", encoding="utf-8")
    try:
        (static / "alias.js").symlink_to(static / "real.js")
    except OSError:
        pytest.skip("file symlinks are unavailable on this platform")
    server = bridge.BridgeHTTPServer(
        (bridge.LOOPBACK_HOST, 0),
        vault,
        static,
        access_token=ACCESS_TOKEN,
        instance_id=INSTANCE_ID,
        integration_provider=lambda: {"available": False, "ready": False},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{bridge.LOOPBACK_HOST}:{server.server_address[1]}"
    try:
        with pytest.raises(HTTPError) as rejected:
            urlopen(_request(f"{base}/static/alias.js"), timeout=2)
        assert rejected.value.code == HTTPStatus.NOT_FOUND
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_snapshot_endpoint_never_calls_full_status_or_logical_digest(
    running_bridge: tuple[bridge.BridgeHTTPServer, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, base = running_bridge

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("snapshot endpoint must not hash the full vault")

    monkeypatch.setattr(server.vault, "status", forbidden)
    monkeypatch.setattr(server.vault, "logical_digest", forbidden)

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        snapshot = json.loads(response.read())

    assert snapshot["status"]["vault_id"] == server.vault_id
    assert "digest" not in snapshot["status"]


def test_snapshot_does_not_wait_for_slow_codex_status_and_refreshes_once(
    vault: Vault,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_integration() -> dict[str, object]:
        calls.append("started")
        started.set()
        assert release.wait(timeout=3)
        return {
            "available": True,
            "instructions_installed": True,
            "plugin_installed": True,
            "ready": True,
        }

    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as static_root:
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, 0),
            vault,
            Path(static_root),
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            integration_provider=slow_integration,
        )
        try:
            before = time.monotonic()
            first = server.snapshot()
            elapsed = time.monotonic() - before
            second = server.snapshot()

            assert elapsed < 0.5
            assert started.wait(timeout=1)
            assert first["codex"]["checking"] is True
            assert second["codex"]["checking"] is True
            assert calls == ["started"]

            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                refreshed = server.snapshot()
                if refreshed["codex"].get("available") is True:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("Codex metadata did not finish its background refresh")

            assert refreshed["codex"]["checking"] is False
            assert refreshed["codex"]["plugin_installed"] is True
            assert calls == ["started"]
        finally:
            release.set()
            server.server_close()


def test_http_surface_rejects_cross_origin_and_writes(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    _, base = running_bridge
    with pytest.raises(HTTPError) as cross_origin:
        urlopen(
            _request(
                f"{base}/api/v1/snapshot",
                token=ACCESS_TOKEN,
                origin="http://127.0.0.1:9",
            ),
            timeout=2,
        )
    assert cross_origin.value.code == HTTPStatus.FORBIDDEN

    request = Request(
        f"{base}/api/v1/snapshot",
        data=b"{}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Origin": base},
        method="POST",
    )
    with pytest.raises(HTTPError) as write:
        urlopen(request, timeout=2)
    assert write.value.code == HTTPStatus.METHOD_NOT_ALLOWED


@pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)
def test_bridge_accepts_only_authenticated_cas_control_events(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    body = json.dumps(
        {
            "choice": "selected",
            "expected_revision": EMPTY_REVISION,
            "kind": "setup_choice",
            "subject": "source:gmail",
        }
    ).encode("utf-8")
    request = Request(
        f"{base}/api/v1/control",
        data=body,
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        assert response.status == HTTPStatus.CREATED
        appended = json.loads(response.read())

    stored = ControlQueue(server.vault.root).snapshot()
    assert appended["event"]["kind"] == "setup_choice"
    assert appended["revision"] == stored.revision
    assert stored.events[0].subject == "source:gmail"
    assert server.vault.list_tasks() == []

    with pytest.raises(HTTPError) as stale:
        urlopen(request, timeout=2)
    assert stale.value.code == HTTPStatus.CONFLICT

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        snapshot = json.loads(response.read())
    assert snapshot["controls"] == {
        "archived_decided": 0,
        "available": True,
        "decided": 0,
        "disposition_revision": EMPTY_REVISION,
        "generation": 0,
        "history": [],
        "items": [
            {
                "event": {
                    "choice": "selected",
                    "created_at": stored.events[0].created_at,
                    "event_id": stored.events[0].event_id,
                    "kind": "setup_choice",
                    "schema_version": 1,
                    "source": "bridge",
                    "subject": "source:gmail",
                    "target_revision": None,
                },
                "status": "pending",
            }
        ],
        "pending": 1,
        "queue_revision": stored.revision,
        "review_prompt": snapshot["controls"]["review_prompt"],
        "review_url": snapshot["controls"]["review_url"],
        "state": "ready",
    }
    assert "do not apply the requested change" in snapshot["controls"]["review_prompt"]
    if snapshot["controls"]["review_url"] is not None:
        assert snapshot["controls"]["review_url"].startswith("codex://new?")
    assert snapshot["bridge"]["semantic_write"] is False


@pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)
def test_live_bridge_refuses_reads_and_writes_after_vault_path_replacement(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
    tmp_path: Path,
) -> None:
    server, base = running_bridge
    original_vault_id = server.vault_id
    parked = tmp_path / "original-vault"
    server.vault.root.rename(parked)
    replacement = Vault(server.vault.root)
    replacement.initialize(name="Replacement vault")
    assert replacement.identity()["vault_id"] != original_vault_id

    with pytest.raises(HTTPError) as stale_snapshot:
        urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2)
    assert stale_snapshot.value.code == HTTPStatus.SERVICE_UNAVAILABLE

    body = json.dumps(
        {
            "choice": "This bearer must not cross the vault boundary.",
            "expected_revision": EMPTY_REVISION,
            "kind": "correction",
            "subject": "mind:user-correction",
        }
    ).encode("utf-8")
    request = Request(
        f"{base}/api/v1/control",
        data=body,
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as stale_control:
        urlopen(request, timeout=2)
    assert stale_control.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    assert not (replacement.root / ".gsv/control").exists()

    # Health continues to identify the authenticated process receipt; it is
    # deliberately not a claim that the vault pathname is still writable.
    with urlopen(_request(f"{base}/api/v1/health", token=ACCESS_TOKEN), timeout=2) as response:
        health = json.loads(response.read())
    assert health["vault_id"] == original_vault_id


@pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)
def test_bridge_post_never_repairs_a_replacement_vault_after_mid_request_swap(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, base = running_bridge
    replacement_root = tmp_path / "prepared-replacement"
    replacement = Vault(replacement_root)
    replacement.initialize(name="Prepared replacement")
    replacement_queue = ControlQueue(replacement.root).append(
        kind="correction",
        subject="mind:user-correction",
        choice="Keep the replacement queue byte-for-byte unchanged.",
        expected_revision=EMPTY_REVISION,
    )
    replacement_queue_path = replacement.root / ".gsv/control/queue.jsonl"
    replacement_before = replacement_queue_path.read_bytes()
    assert replacement_queue.revision != EMPTY_REVISION
    assert not tuple((replacement.root / ".gsv/control").glob("dispositions-*.head.jsonl"))

    parked = tmp_path / "post-swap-original"
    actual_append = ControlQueue.append
    swapped = False

    def append_then_swap(queue: ControlQueue, **kwargs: Any) -> Any:
        nonlocal swapped
        result = actual_append(queue, **kwargs)
        if queue.vault_root == server.vault.root and not swapped:
            swapped = True
            server.vault.root.rename(parked)
            replacement_root.rename(server.vault.root)
        return result

    monkeypatch.setattr(ControlQueue, "append", append_then_swap)
    request = Request(
        f"{base}/api/v1/control",
        data=json.dumps(
            {
                "choice": "Append only to the startup vault.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": "mind:user-correction",
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )

    with pytest.raises(HTTPError) as rejected:
        urlopen(request, timeout=2)

    assert swapped is True
    assert rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    current_control = server.vault.root / ".gsv/control"
    assert (current_control / "queue.jsonl").read_bytes() == replacement_before
    assert not tuple(current_control.glob("dispositions-*.head.jsonl"))
    assert (parked / ".gsv/control/queue.jsonl").is_file()


@pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)
def test_bridge_get_never_repairs_same_id_replacement_after_precheck_swap(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, base = running_bridge
    replacement_root = tmp_path / "same-id-replacement"
    replacement = Vault(replacement_root)
    replacement.initialize(name="Same logical ID replacement")
    manifest_path = replacement.root / ".gsv/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["vault_id"] = server.vault_id
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ControlQueue(replacement.root).append(
        kind="correction",
        subject="mind:user-correction",
        choice="Do not initialize a head in this same-ID replacement.",
        expected_revision=EMPTY_REVISION,
    )
    replacement_queue_path = replacement.root / ".gsv/control/queue.jsonl"
    replacement_before = replacement_queue_path.read_bytes()
    parked = tmp_path / "get-swap-original"
    actual_require = server._require_current_vault_identity
    swapped = False

    def verify_then_swap() -> None:
        nonlocal swapped
        actual_require()
        if not swapped:
            swapped = True
            server.vault.root.rename(parked)
            replacement_root.rename(server.vault.root)

    monkeypatch.setattr(server, "_require_current_vault_identity", verify_then_swap)

    with pytest.raises(HTTPError) as rejected:
        urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2)

    assert swapped is True
    assert rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    current_control = server.vault.root / ".gsv/control"
    assert (current_control / "queue.jsonl").read_bytes() == replacement_before
    assert not tuple(current_control.glob("dispositions-*.head.jsonl"))


def test_bridge_control_endpoint_rejects_missing_auth_cross_origin_and_unknown_shape(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    _, base = running_bridge
    body = json.dumps(
        {
            "choice": "approve",
            "expected_revision": EMPTY_REVISION,
            "kind": "approval",
            "subject": "operation:test",
        }
    ).encode("utf-8")

    for headers, expected in (
        ({"Content-Type": "application/json"}, HTTPStatus.FORBIDDEN),
        (
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            HTTPStatus.FORBIDDEN,
        ),
        (
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:9",
            },
            HTTPStatus.FORBIDDEN,
        ),
        (
            {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
                "Origin": f"{base}/not-an-origin",
            },
            HTTPStatus.FORBIDDEN,
        ),
        (
            {
                "Authorization": f"Bearer {'c' * 48}",
                "Content-Type": "application/json",
                "Origin": base,
            },
            HTTPStatus.FORBIDDEN,
        ),
    ):
        request = Request(f"{base}/api/v1/control", data=body, headers=headers, method="POST")
        with pytest.raises(HTTPError) as rejected:
            urlopen(request, timeout=2)
        assert rejected.value.code == expected

    unsupported = json.dumps(
        {
            "choice": "approve",
            "expected_revision": EMPTY_REVISION,
            "kind": "approval",
            "provider_body": "untrusted content",
            "subject": "operation:test",
        }
    ).encode("utf-8")
    request = Request(
        f"{base}/api/v1/control",
        data=unsupported,
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as rejected:
        urlopen(request, timeout=2)
    assert rejected.value.code == HTTPStatus.BAD_REQUEST


def test_rejected_post_closes_before_unread_body_can_be_parsed_as_another_request(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    target = urlsplit(base)
    assert target.port is not None
    smuggled = (
        "GET /api/v1/snapshot HTTP/1.1\r\n"
        f"Host: {bridge.LOOPBACK_HOST}:{target.port}\r\n"
        f"Origin: {base}\r\n"
        f"Authorization: Bearer {ACCESS_TOKEN}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    first = (
        "POST /api/v1/control HTTP/1.1\r\n"
        f"Host: {bridge.LOOPBACK_HOST}:{target.port}\r\n"
        "Origin: http://127.0.0.1:9\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(smuggled)}\r\n\r\n"
    ).encode("ascii")
    received = bytearray()
    with socket.create_connection((bridge.LOOPBACK_HOST, target.port), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall(first + smuggled)
        while True:
            chunk = connection.recv(16 * 1024)
            if not chunk:
                break
            received.extend(chunk)

    assert received.count(b"HTTP/1.1 ") == 1
    assert b"HTTP/1.1 403" in received
    assert not (server.vault.root / ".gsv/control").exists()


def test_review_turn_storage_failure_is_service_unavailable_not_not_found(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, base = running_bridge
    event_id = "11111111-1111-4111-8111-111111111111"

    def unavailable(_event_id: object) -> None:
        raise ControlStorageError("injected unavailable receipt store")

    monkeypatch.setattr(server.turn_transport, "receipt", unavailable)
    with pytest.raises(HTTPError) as get_rejected:
        urlopen(
            _request(
                f"{base}/api/v1/review-turn?event_id={event_id}",
                token=ACCESS_TOKEN,
                origin=base,
            ),
            timeout=2,
        )
    assert get_rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE

    post = Request(
        f"{base}/api/v1/review-turn",
        data=json.dumps({"event_id": event_id}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as post_rejected:
        urlopen(post, timeout=2)
    assert post_rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE


def test_bridge_control_endpoint_rejects_forged_host_without_creating_control_store(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    target = urlsplit(base)
    assert target.port is not None
    body = json.dumps(
        {
            "choice": "Do not trust a forged Host header.",
            "expected_revision": EMPTY_REVISION,
            "kind": "correction",
            "subject": "mind:user-correction",
        }
    ).encode("utf-8")
    connection = HTTPConnection(bridge.LOOPBACK_HOST, target.port, timeout=2)
    try:
        connection.putrequest("POST", "/api/v1/control", skip_host=True)
        connection.putheader("Host", f"attacker.example:{target.port}")
        connection.putheader("Authorization", f"Bearer {ACCESS_TOKEN}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Origin", base)
        connection.endheaders(body)
        response = connection.getresponse()
        response.read()
    finally:
        connection.close()

    assert response.status == HTTPStatus.FORBIDDEN
    assert not (server.vault.root / ".gsv/control").exists()


def test_bridge_reports_an_unconfirmed_control_commit_without_claiming_no_change(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, base = running_bridge

    def unconfirmed(*_args: object, **_kwargs: object) -> None:
        raise MutationCommittedError("injected post-publication durability failure")

    monkeypatch.setattr(ControlQueue, "append", unconfirmed)
    request = Request(
        f"{base}/api/v1/control",
        data=json.dumps(
            {
                "choice": "Keep this correction visible until its outcome is known.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": "mind:user-correction",
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )

    with pytest.raises(HTTPError) as rejected:
        urlopen(request, timeout=2)

    assert rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    payload = json.loads(rejected.value.read())
    assert "could not confirm its durable result" in payload["error"]
    assert "Reload the queue before retrying" in payload["error"]
    assert "not changed" not in payload["error"]


def test_bridge_preflights_dispositions_before_committing_a_new_intent(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    first_body = json.dumps(
        {
            "choice": "First correction.",
            "expected_revision": EMPTY_REVISION,
            "kind": "correction",
            "subject": "mind:user-correction",
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Origin": base,
    }
    with urlopen(
        Request(f"{base}/api/v1/control", data=first_body, headers=headers, method="POST"),
        timeout=2,
    ) as response:
        first = json.loads(response.read())

    queue_path = server.vault.root / ".gsv/control/queue.jsonl"
    queue_before = queue_path.read_bytes()
    disposition_path = server.vault.root / ".gsv/control/dispositions-0000000000000000.jsonl"
    disposition_path.write_bytes(b"{}\n")
    second_body = json.dumps(
        {
            "choice": "This must not append after failed disposition preflight.",
            "expected_revision": first["revision"],
            "kind": "correction",
            "subject": "mind:user-correction",
        }
    ).encode("utf-8")

    with pytest.raises(HTTPError) as rejected:
        urlopen(
            Request(f"{base}/api/v1/control", data=second_body, headers=headers, method="POST"),
            timeout=2,
        )

    assert rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    assert queue_path.read_bytes() == queue_before
    assert len(ControlQueue(server.vault.root).snapshot().events) == 1


def test_bridge_returns_the_committed_queue_revision_without_a_post_commit_ledger_read(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, base = running_bridge
    actual_snapshot = OperationLedger.snapshot
    calls = 0

    def preflight_once(ledger: OperationLedger, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ControlStorageError("injected post-commit ledger failure")
        return actual_snapshot(ledger, **kwargs)

    monkeypatch.setattr(OperationLedger, "snapshot", preflight_once)
    body = json.dumps(
        {
            "choice": "Return the committed queue revision.",
            "expected_revision": EMPTY_REVISION,
            "kind": "correction",
            "subject": "mind:user-correction",
        }
    ).encode("utf-8")
    request = Request(
        f"{base}/api/v1/control",
        data=body,
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )

    with urlopen(request, timeout=2) as response:
        result = json.loads(response.read())

    assert response.status == HTTPStatus.CREATED
    assert calls == 1
    assert result["revision"] == ControlQueue(server.vault.root).snapshot().revision


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_bridge_non_post_methods_never_append_control_events(
    running_bridge: tuple[bridge.BridgeHTTPServer, str], method: str
) -> None:
    server, base = running_bridge
    request = Request(
        f"{base}/api/v1/control",
        data=json.dumps(
            {
                "choice": "approve",
                "expected_revision": EMPTY_REVISION,
                "kind": "approval",
                "subject": "operation:test",
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method=method,
    )

    with pytest.raises(HTTPError) as rejected:
        urlopen(request, timeout=2)

    assert rejected.value.code == HTTPStatus.METHOD_NOT_ALLOWED
    assert not (server.vault.root / ".gsv/control").exists()


def test_corrupt_control_queue_does_not_hide_canonical_bridge_reads(
    running_bridge: tuple[bridge.BridgeHTTPServer, str],
) -> None:
    server, base = running_bridge
    task = server.vault.create_task(
        identifier="canonical-stays-visible",
        title="Canonical stays visible",
        outcome="The Bridge degrades only its noncanonical control lane.",
    )
    queue = ControlQueue(server.vault.root)
    queue.path.parent.mkdir(parents=True, exist_ok=True)
    queue.path.write_bytes(b"{}")

    with urlopen(_request(f"{base}/api/v1/snapshot", token=ACCESS_TOKEN), timeout=2) as response:
        snapshot = json.loads(response.read())

    assert response.status == HTTPStatus.OK
    assert any(item["identifier"] == task.identifier for item in snapshot["tasks"])
    assert snapshot["controls"] == {
        "archived_decided": None,
        "available": False,
        "decided": None,
        "disposition_revision": None,
        "generation": None,
        "history": [],
        "items": [],
        "pending": None,
        "queue_revision": None,
        "review_prompt": None,
        "review_url": None,
        "state": "unavailable",
    }

    request = Request(
        f"{base}/api/v1/control",
        data=json.dumps(
            {
                "choice": "Keep the stored corruption untouched.",
                "expected_revision": EMPTY_REVISION,
                "kind": "correction",
                "subject": "mind:user-correction",
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Origin": base,
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as rejected:
        urlopen(request, timeout=2)

    assert rejected.value.code == HTTPStatus.SERVICE_UNAVAILABLE
    assert queue.path.read_bytes() == b"{}"


def test_stop_never_signals_reused_pid_when_health_identity_mismatches(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        bridge,
        "_probe_health",
        lambda *_args, **_kwargs: _health_response(
            {
                "service": "gsv-bridge",
                "instance_id": "c" * 32,
                "pid": 4242,
                "port": 43117,
                "vault_id": vault.identity()["vault_id"],
            }
        ),
    )
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    result = bridge.stop_bridge()

    assert signaled == []
    assert result["stopped"] is False
    assert result["stale_reason"] == "identity_mismatch"
    assert result["stale_receipt_removed"] is True
    assert not bridge._state_path().exists()


def test_stop_signals_only_a_fully_verified_bridge(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(vault)
    bridge._write_state(state)
    alive = {4242: True}
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(
        bridge,
        "_probe_health",
        lambda *_args, **_kwargs: _health_response(
            {
                "service": "gsv-bridge",
                "instance_id": INSTANCE_ID,
                "pid": 4242,
                "port": 43117,
                "vault_id": vault.identity()["vault_id"],
            }
        ),
    )

    def terminate(pid: int) -> None:
        signaled.append(pid)
        alive[pid] = False

    monkeypatch.setattr(bridge, "_terminate_pid", terminate)

    result = bridge.stop_bridge()

    assert signaled == [4242]
    assert result["stopped"] is True
    assert not bridge._state_path().exists()


def test_stop_failure_preserves_verified_receipt(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    signaled: list[int] = []
    clock = iter((0.0, 0.0, 4.0))
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr("continuity_kernel.bridge.time.monotonic", lambda: next(clock))
    monkeypatch.setattr(
        bridge,
        "_probe_health",
        lambda *_args, **_kwargs: _health_response(
            {
                "service": "gsv-bridge",
                "instance_id": INSTANCE_ID,
                "pid": 4242,
                "port": 43117,
                "vault_id": vault.identity()["vault_id"],
            }
        ),
    )
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    with pytest.raises(SetupError, match="did not stop"):
        bridge.stop_bridge()

    assert signaled == [4242]
    assert bridge._state_path().is_file()


def test_forged_or_invalid_receipt_is_removed_without_signaling(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(vault)
    state["url"] = "http://127.0.0.1:9/"
    bridge._state_path().parent.mkdir(parents=True, exist_ok=True)
    bridge._state_path().write_text(json.dumps(state), encoding="utf-8")
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    result = bridge.stop_bridge()

    assert signaled == []
    assert result["stale_receipt_removed"] is True
    assert not bridge._state_path().exists()


def test_default_ephemeral_port_avoids_an_occupied_port(vault: Vault) -> None:
    resource = files("continuity_kernel") / "resources/bridge"
    with socket.socket() as occupied, as_file(resource) as static_root:
        occupied.bind((bridge.LOOPBACK_HOST, 0))
        occupied_port = occupied.getsockname()[1]
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, bridge.DEFAULT_PORT),
            vault,
            Path(static_root),
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
        )
        try:
            assert server.server_address[1] != occupied_port
            assert server.server_address[1] > 0
        finally:
            server.server_close()


def test_bridge_bind_never_depends_on_reverse_dns(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_lookup(_host: str) -> str:
        raise AssertionError("a loopback-only server must not perform reverse DNS")

    monkeypatch.setattr("continuity_kernel.bridge.socket.getfqdn", forbidden_lookup)
    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as static_root:
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, bridge.DEFAULT_PORT),
            vault,
            Path(static_root),
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
        )
        try:
            assert server.server_name == bridge.LOOPBACK_HOST
            assert server.server_port == server.server_address[1]
        finally:
            server.server_close()


def test_state_receipt_is_private_and_open_token_stays_in_fragment(vault: Vault) -> None:
    state = _state(vault)
    bridge._write_state(state)

    if os.name != "nt":
        assert bridge._state_path().stat().st_mode & 0o777 == 0o600
    open_url = bridge._open_url(bridge.BridgeState.from_payload(state))
    before_fragment, fragment = open_url.split("#", 1)
    assert ACCESS_TOKEN not in before_fragment
    assert fragment == f"token={ACCESS_TOKEN}"
    assert "token" not in bridge.bridge_status()


def test_serve_bridge_holds_one_runtime_owner_for_its_full_lifetime(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    constructed: list[str] = []
    failures: list[BaseException] = []

    class BlockingServer:
        def __init__(
            self,
            _address: tuple[str, int],
            target_vault: Vault,
            _static_root: Path,
            *,
            access_token: str,
            instance_id: str,
        ) -> None:
            assert access_token
            constructed.append(instance_id)
            self.instance_id = instance_id
            self.server_address = (bridge.LOOPBACK_HOST, 43117)
            self.vault_id = str(target_vault.identity()["vault_id"])

        def serve_forever(self, *, poll_interval: float) -> None:
            assert poll_interval == 0.25
            if self.instance_id != INSTANCE_ID:
                if release.is_set():
                    return
                raise AssertionError("a second server was constructed while the owner was live")
            entered.set()
            assert release.wait(timeout=5)

        def server_close(self) -> None:
            return None

    monkeypatch.setattr(bridge, "BridgeHTTPServer", BlockingServer)

    def run_owner() -> None:
        try:
            bridge.serve_bridge(
                vault,
                write_state=False,
                access_token=ACCESS_TOKEN,
                instance_id=INSTANCE_ID,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    owner = threading.Thread(target=run_owner)
    owner.start()
    assert entered.wait(timeout=5)

    with pytest.raises(SetupError, match="already owns this local runtime"):
        bridge.serve_bridge(
            vault,
            write_state=False,
            access_token="d" * 48,
            instance_id="c" * 32,
        )

    assert constructed == [INSTANCE_ID]
    release.set()
    owner.join(timeout=5)
    assert not owner.is_alive()
    assert failures == []

    bridge.serve_bridge(
        vault,
        write_state=False,
        access_token="d" * 48,
        instance_id="c" * 32,
    )
    assert constructed == [INSTANCE_ID, "c" * 32]


def test_frozen_bridge_child_requests_an_independent_pyinstaller_runtime(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("continuity_kernel.bridge.sys.frozen", True, raising=False)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/synthetic/parent-extraction")

    environment = bridge._bridge_child_environment(vault)

    assert environment["GSV_VAULT"] == str(vault.root)
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_frozen_start_accepts_worker_pid_after_exact_launch_identity(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LiveLauncher:
        def poll(self) -> None:
            return None

    state = _state(vault, pid=5252)
    monkeypatch.setattr(bridge, "_load_state", lambda: bridge.BridgeState.from_payload(state))

    observed = bridge._wait_for_state(
        INSTANCE_ID,
        cast(Any, LiveLauncher()),
        expected_pid=None,
        vault_id=str(vault.identity()["vault_id"]),
        timeout=0.1,
    )

    assert observed is not None
    assert observed.payload() == state


def test_source_start_requires_receipt_to_match_launcher_pid(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExitedLauncher:
        def poll(self) -> int:
            return 1

    state = _state(vault, pid=5252)
    monkeypatch.setattr(bridge, "_load_state", lambda: bridge.BridgeState.from_payload(state))

    observed = bridge._wait_for_state(
        INSTANCE_ID,
        cast(Any, ExitedLauncher()),
        expected_pid=4242,
        vault_id=str(vault.identity()["vault_id"]),
        timeout=0.1,
    )

    assert observed is None


def test_windows_liveness_branch_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[int] = []
    monkeypatch.setattr(bridge, "_IS_WINDOWS", True)

    def windows_probe(pid: int) -> bool:
        observed.append(pid)
        return True

    monkeypatch.setattr(bridge, "_windows_pid_alive", windows_probe)

    def forbidden_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("the Windows liveness branch must not call os.kill")

    monkeypatch.setattr("continuity_kernel.bridge.os.kill", forbidden_kill)

    assert bridge._pid_alive(4242) is True
    assert observed == [4242]


def test_status_preserves_receipt_when_live_health_is_temporarily_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge, "_probe_health", lambda *_args, **_kwargs: _health_unavailable())

    result = bridge.bridge_status()

    assert result["running"] is True
    assert result["identity_verified"] is False
    assert result["health_unavailable"] is True
    assert bridge._state_path().is_file()


def test_open_preserves_receipt_and_starts_nothing_when_live_health_is_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge, "_probe_health", lambda *_args, **_kwargs: _health_unavailable())

    def forbidden_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a replacement Bridge must not be started")

    monkeypatch.setattr("continuity_kernel.bridge.subprocess.Popen", forbidden_popen)

    with pytest.raises(SetupError, match="receipt was preserved"):
        bridge.open_bridge(vault, open_browser=False)

    assert bridge._state_path().is_file()


def test_stop_preserves_receipt_and_signals_nothing_when_live_health_is_unavailable(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge._write_state(_state(vault))
    signaled: list[int] = []
    monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(bridge, "_probe_health", lambda *_args, **_kwargs: _health_unavailable())
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    with pytest.raises(SetupError, match="No process was signalled"):
        bridge.stop_bridge()

    assert signaled == []
    assert bridge._state_path().is_file()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("injected timeout"),
        ConnectionResetError(errno.ECONNRESET, "injected reset"),
        OSError(errno.EHOSTUNREACH, "injected unreachable host"),
    ],
)
def test_health_probe_treats_non_refusal_network_errors_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(bridge, "_open_loopback", fail_request)
    monkeypatch.setattr(bridge, "_loopback_connection_refused", lambda *_args, **_kwargs: False)

    probe = bridge._probe_health("http://127.0.0.1:43117/", token=ACCESS_TOKEN, timeout=0)

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_health_probe_treats_malformed_response_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedResponse:
        status = HTTPStatus.OK

        def __enter__(self) -> MalformedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not-json"

    monkeypatch.setattr(bridge, "_open_loopback", lambda *_args, **_kwargs: MalformedResponse())

    probe = bridge._probe_health("http://127.0.0.1:43117/", token=ACCESS_TOKEN, timeout=0)

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_health_probe_never_classifies_remote_refusal_as_local_stale_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = URLError(ConnectionRefusedError(errno.ECONNREFUSED, "injected refusal"))

    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise refused

    monkeypatch.setattr(bridge, "_open_loopback", fail_request)

    probe = bridge._probe_health("http://example.invalid:9/", token=ACCESS_TOKEN, timeout=0)

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.UNAVAILABLE)


def test_health_probe_recognizes_windows_loopback_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError(errno.EINVAL, "injected Windows refusal")
    cast(Any, error).winerror = 10061

    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise URLError(error)

    monkeypatch.setattr(bridge, "_open_loopback", fail_request)

    probe = bridge._probe_health(f"http://{bridge.LOOPBACK_HOST}:9/", token=ACCESS_TOKEN, timeout=0)

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.REFUSED)


def test_health_probe_confirms_opaque_loopback_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PendingSocket:
        def __enter__(self) -> PendingSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def setblocking(self, _blocking: bool) -> None:
            return None

        def connect_ex(self, _address: tuple[str, int]) -> int:
            return 10035

        def getsockopt(self, _level: int, _option: int) -> int:
            return 0

        def setsockopt(self, _level: int, _option: int, _value: int) -> None:
            return None

        def bind(self, _address: tuple[str, int]) -> None:
            return None

    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise URLError(OSError(errno.EINVAL, "opaque transport failure"))

    connection = PendingSocket()
    monkeypatch.setattr(bridge, "_open_loopback", fail_request)
    monkeypatch.setattr(bridge, "_IS_WINDOWS", True)
    monkeypatch.setattr("continuity_kernel.bridge.socket.socket", lambda *_args: connection)
    monkeypatch.setattr(
        "continuity_kernel.bridge.select.select",
        lambda *_args: ([], [], []),
    )

    probe = bridge._probe_health(
        f"http://{bridge.LOOPBACK_HOST}:43117/", token=ACCESS_TOKEN, timeout=0
    )

    assert probe == bridge._HealthProbe(bridge._HealthOutcome.REFUSED)


def test_windows_refusal_fallback_preserves_a_bound_loopback_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as listener:
        listener.bind((bridge.LOOPBACK_HOST, 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        monkeypatch.setattr(bridge, "_IS_WINDOWS", True)
        monkeypatch.setattr(
            "continuity_kernel.bridge.select.select",
            lambda *_args: ([], [], []),
        )

        refused = bridge._loopback_connection_refused(
            f"http://{bridge.LOOPBACK_HOST}:{port}/", timeout=0
        )

    assert refused is False


def test_source_bridge_command_uses_the_minimal_worker(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", "/synthetic/python")

    assert bridge._bridge_command(vault, instance_id=INSTANCE_ID) == [
        "/synthetic/python",
        "-m",
        "continuity_kernel.bridge_worker",
        "--vault",
        str(vault.root),
        "--port",
        "0",
        "--instance-id",
        INSTANCE_ID,
    ]


def test_stop_cleans_refused_receipt_without_signalling_unverified_pid(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    signaled: list[int] = []
    with socket.socket() as released:
        released.bind((bridge.LOOPBACK_HOST, 0))
        port = int(released.getsockname()[1])
    bridge._write_state(_state(vault, port=port, pid=os.getpid()))
    monkeypatch.setattr(bridge, "_terminate_pid", signaled.append)

    result = bridge.stop_bridge()

    assert signaled == []
    assert result == {
        "running": False,
        "stale_reason": "connection_refused",
        "stale_receipt_removed": True,
        "stopped": False,
    }
    assert not bridge._state_path().exists()


def test_open_replaces_refused_receipt_without_signalling_unverified_pid(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    signaled: list[int] = []
    with socket.socket() as released:
        released.bind((bridge.LOOPBACK_HOST, 0))
        port = int(released.getsockname()[1])
    bridge._write_state(_state(vault, port=port, pid=os.getpid()))
    with monkeypatch.context() as guarded:
        guarded.setattr(bridge, "_terminate_pid", signaled.append)
        opened = bridge.open_bridge(vault, open_browser=False)

    try:
        replacement = bridge._load_state()
        assert opened["started"] is True
        assert signaled == []
        assert replacement is not None
        assert replacement.pid != os.getpid()
        assert replacement.instance_id != INSTANCE_ID
    finally:
        stopped = bridge.stop_bridge()
    assert stopped["stopped"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_bridge_runtime_directory_and_log_are_private_and_reject_symlinks(
    tmp_path: Path,
) -> None:
    root = data_dir()
    root.mkdir(parents=True)
    root.chmod(0o755)

    secured = bridge._private_runtime_dir()
    log_path = secured / "bridge.log"
    with bridge._open_private_log(log_path) as log:
        log.write(b"synthetic diagnostic\n")

    assert secured.stat().st_mode & 0o777 == 0o700
    assert log_path.stat().st_mode & 0o777 == 0o600

    log_path.unlink()
    outside = tmp_path / "outside.log"
    outside.write_text("must remain untouched", encoding="utf-8")
    log_path.symlink_to(outside)
    with pytest.raises(SetupError, match="unsafe Bridge log"):
        bridge._open_private_log(log_path)
    assert outside.read_text(encoding="utf-8") == "must remain untouched"


def test_real_detached_bridge_child_binds_reports_and_stops(vault: Vault) -> None:
    opened = bridge.open_bridge(vault, open_browser=False)
    try:
        status = bridge.bridge_status()
        state = bridge._load_state()
        assert opened["started"] is True
        assert opened["browser_opened"] is False
        assert status["running"] is True
        assert status["identity_verified"] is True
        assert status["port"] > 0
        assert state is not None
        health = bridge._health_payload(state.url, token=state.token, timeout=2)
        assert bridge._state_matches_health(state, health)
    finally:
        stopped = bridge.stop_bridge()
    assert stopped["stopped"] is True
    assert not bridge._state_path().exists()


@pytest.mark.skipif(os.name == "nt", reason="same-path inode replacement proof is POSIX-only")
def test_open_refuses_to_reuse_a_bridge_after_same_id_vault_replacement(vault: Vault) -> None:
    original_vault_id = vault.identity()["vault_id"]
    opened = bridge.open_bridge(vault, open_browser=False)
    parked = vault.root.with_name(f"{vault.root.name}-parked")
    try:
        vault.root.rename(parked)
        shutil.copytree(parked, vault.root)
        replacement = Vault(vault.root)
        assert replacement.identity()["vault_id"] == original_vault_id

        with pytest.raises(SetupError, match="earlier vault directory"):
            bridge.open_bridge(replacement, open_browser=False)
        assert bridge.open_bridge_in_browser(replacement) is False
        assert opened["started"] is True
    finally:
        stopped = bridge.stop_bridge()
    assert stopped["stopped"] is True
