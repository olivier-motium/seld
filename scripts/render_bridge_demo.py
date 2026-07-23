#!/usr/bin/env python3
"""Regenerate the privacy-clean Bridge screenshot and handoff GIF."""

from __future__ import annotations

import argparse
import copy
import json
import re
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, cast

from PIL import Image
from playwright.sync_api import Browser, Error, sync_playwright

from continuity_kernel import bridge
from continuity_kernel.demo import run_demo
from continuity_kernel.vault import Vault

TOKEN = "d" * 48
INSTANCE = "e" * 32


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/assets"))
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gsv-visual-proof-") as raw:
        root = Path(raw)
        vault_root = root / "synthetic-vault"
        proof = run_demo(vault_root)
        if not proof["fresh_process_resumed"] or not proof["hand_process_killed"]:
            raise RuntimeError("the synthetic handoff proof failed before capture")
        frames = _capture(root, Vault(vault_root), output)
        _write_gif(frames, output / "gsv-handoff.gif")

    result = {
        "gif": str(output / "gsv-handoff.gif"),
        "inspector": str(output / "bridge-inspector.png"),
        "integrity_warning": str(output / "bridge-integrity-warning.png"),
        "mobile": str(output / "bridge-mobile.png"),
        "mobile_inspector": str(output / "bridge-mobile-inspector.png"),
        "mobile_recovery": str(output / "bridge-mobile-recovery.png"),
        "screenshot": str(output / "bridge-overview.png"),
        "stale": str(output / "bridge-stale.png"),
        "synthetic": True,
        "unknown_status": str(output / "bridge-unknown-status.png"),
        "unavailable": str(output / "bridge-unavailable.png"),
        "unavailable_codex": str(output / "bridge-codex-unavailable.png"),
    }
    print(json.dumps(result, sort_keys=True))
    return 0


def _capture(root: Path, vault: Vault, output: Path) -> list[Path]:
    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as static_root:
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, 0),
            vault,
            Path(static_root),
            access_token=TOKEN,
            instance_id=INSTANCE,
            integration_provider=_synthetic_codex_status,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://{bridge.LOOPBACK_HOST}:{server.server_address[1]}/#token={TOKEN}"
        try:
            return _capture_browser(root, output, url)
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _synthetic_codex_status() -> dict[str, Any]:
    return {"available": True, "instructions_installed": True, "plugin_installed": True}


def _capture_browser(root: Path, output: Path, url: str) -> list[Path]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = _launch(playwright.chromium)
        try:
            page = browser.new_page(
                viewport={"width": 1440, "height": 920},
                device_scale_factor=1,
                reduced_motion="reduce",
            )
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text) if message.type == "error" else None
                ),
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(url, wait_until="networkidle")
            try:
                page.locator("#local-status.is-healthy").wait_for(timeout=10_000)
            except Error as exc:
                status = page.locator("#local-status").inner_text()
                notice = page.locator("#connection-copy").inner_text()
                raise RuntimeError(
                    f"Bridge did not become healthy: status={status!r}; "
                    f"notice={notice!r}; console={console_errors!r}"
                ) from exc
            _assert_visibility(page, "#connection-notice", visible=False)
            _assert_visibility(page, "#task-count", visible=True)
            _assert_visibility(page, "#rail-backdrop", visible=False)
            _assert_visibility(page, "#inspector-backdrop", visible=False)
            _assert_visibility(page, "#inspector-foot", visible=False)
            _assert_visibility(page, "#connection-orb", visible=False)
            _assert_visibility(page, "#local-status .status-dot", visible=True)
            page.locator("#open-codex").wait_for(state="visible", timeout=5_000)
            _assert_codex_links(page, expected=True)
            _assert_orb_canvas(page, ".continuity-orb")
            _assert_reduced_orb_static(page, ".continuity-orb")
            _assert_no_browser_errors(console_errors, page_errors)
            _assert_viewport(page)

            frame_one = root / "01-now.png"
            page.screenshot(path=str(frame_one), animations="disabled")
            page.screenshot(path=str(output / "bridge-overview.png"), animations="disabled")

            page.locator("button[data-view='commitments']").click()
            page.locator(".commitment-grid").wait_for()
            _assert_exact_authored_task_state(page)
            frame_two = root / "02-commitments.png"
            page.screenshot(path=str(frame_two), animations="disabled")

            atlas = page.get_by_role("button", name=re.compile(r"Ship the Atlas migration"))
            if atlas.count() != 1:
                raise RuntimeError("the synthetic Atlas commitment was not uniquely addressable")
            if atlas.locator("h4").inner_text() != "Ship the Atlas migration":
                raise RuntimeError("the role-name locator no longer matches the synthetic title")
            atlas.click()
            page.locator("#inspector.is-open").wait_for()
            _assert_visibility(page, "#inspector-backdrop", visible=True)
            _assert_visibility(page, "#inspector-foot", visible=True)
            frame_three = root / "03-inspector.png"
            page.screenshot(path=str(frame_three), animations="disabled")
            page.screenshot(path=str(output / "bridge-inspector.png"), animations="disabled")

            page.keyboard.press("Escape")
            _assert_visibility(page, "#inspector-backdrop", visible=False)
            _assert_visibility(page, "#inspector-foot", visible=False)
            page.locator("button[data-view='now']").click()
            expected_error_index = len(console_errors)
            page.route("**/api/v1/snapshot", lambda route: route.abort())
            page.locator(".connection-notice.is-stale").wait_for(timeout=15_000)
            _assert_visibility(page, "#connection-orb", visible=True)
            _assert_visibility(page, "#local-status .status-dot", visible=False)
            _assert_orb_canvas(page, "#connection-orb")
            _assert_reduced_orb_static(page, "#connection-orb")
            page.screenshot(path=str(output / "bridge-stale.png"), animations="disabled")
            _consume_expected_network_errors(console_errors, expected_error_index)
            _assert_no_browser_errors(console_errors, page_errors)
            page.unroute("**/api/v1/snapshot")

            ready_snapshot = _read_snapshot(page)
            _verify_consumer_states(page, ready_snapshot)
            _verify_live_vault_states(browser, root)
            _capture_codex_recovery_states(page, ready_snapshot, output)
            _capture_unknown_status(page, ready_snapshot, output)
            _capture_integrity_warning(page, ready_snapshot, output)

            unavailable = browser.new_page(
                viewport={"width": 1024, "height": 760},
                device_scale_factor=1,
                reduced_motion="reduce",
            )
            unavailable_console_errors: list[str] = []
            unavailable_page_errors: list[str] = []
            unavailable.on(
                "console",
                lambda message: (
                    unavailable_console_errors.append(message.text)
                    if message.type == "error"
                    else None
                ),
            )
            unavailable.on("pageerror", lambda error: unavailable_page_errors.append(str(error)))
            unavailable.route("**/api/v1/snapshot", lambda route: route.abort())
            unavailable.goto(url, wait_until="domcontentloaded")
            unavailable.locator(".unavailable-state").wait_for(timeout=5_000)
            _assert_visibility(unavailable, "#connection-orb", visible=False)
            _assert_visibility(unavailable, "#local-status .status-dot", visible=True)
            unavailable.screenshot(
                path=str(output / "bridge-unavailable.png"), animations="disabled"
            )
            _consume_expected_network_errors(unavailable_console_errors, 0)
            _assert_no_browser_errors(unavailable_console_errors, unavailable_page_errors)
            unavailable.close()

            page.reload(wait_until="networkidle")
            page.locator("#local-status.is-healthy").wait_for(timeout=10_000)
            page.locator("#open-codex").wait_for(state="visible", timeout=5_000)
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#menu-button").click()
            page.locator("#rail.is-open").wait_for()
            _assert_visibility(page, "#rail-backdrop", visible=True)
            page.locator("button[data-view='now']").click()
            page.locator("#rail:not(.is-open)").wait_for()
            _assert_visibility(page, "#rail-backdrop", visible=False)
            _assert_mobile_heading(page)
            _assert_viewport(page)
            page.screenshot(path=str(output / "bridge-mobile.png"), animations="disabled")

            page.locator("#menu-button").click()
            page.locator("button[data-view='commitments']").click()
            mobile_atlas = page.get_by_role("button", name=re.compile(r"Ship the Atlas migration"))
            mobile_atlas.click()
            page.locator("#inspector.is-open").wait_for()
            _assert_viewport(page)
            page.screenshot(path=str(output / "bridge-mobile-inspector.png"), animations="disabled")
        finally:
            browser.close()
    _assert_no_browser_errors(console_errors, page_errors)
    for image in (
        frame_one,
        frame_two,
        frame_three,
        output / "bridge-overview.png",
        output / "bridge-inspector.png",
        output / "bridge-integrity-warning.png",
        output / "bridge-mobile.png",
        output / "bridge-mobile-inspector.png",
        output / "bridge-mobile-recovery.png",
        output / "bridge-stale.png",
        output / "bridge-unknown-status.png",
        output / "bridge-unavailable.png",
        output / "bridge-codex-unavailable.png",
    ):
        _assert_nonblank(image)
    return [frame_one, frame_two, frame_three]


def _read_snapshot(page: Any) -> dict[str, Any]:
    payload = page.evaluate(
        """async () => {
          const token = window.sessionStorage.getItem('gsv_bridge_token');
          const response = await fetch('/api/v1/snapshot', {
            cache: 'no-store',
            headers: {Accept: 'application/json', Authorization: `Bearer ${token}`},
          });
          if (!response.ok) throw new Error(`snapshot ${response.status}`);
          return response.json();
        }"""
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Bridge browser returned a non-object snapshot")
    return cast(dict[str, Any], payload)


def _route_snapshot(page: Any, payload: dict[str, Any]) -> Any:
    def handler(route: Any) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    page.route("**/api/v1/snapshot", handler)
    return handler


def _verify_consumer_states(page: Any, snapshot: dict[str, Any]) -> None:
    terminal = copy.deepcopy(snapshot)
    for index, task in enumerate(terminal["tasks"]):
        task["status"] = "done" if index % 2 == 0 else "dropped"
        task["next_actor"] = None
        task["next_action"] = None
        task["waiting_on"] = None
        task.pop("codex_url", None)
    terminal["projection"]["sections"]["tasks"] = {
        "issues": [],
        "readable": len(terminal["tasks"]),
        "state": "complete",
        "unreadable": 0,
    }
    handler = _route_snapshot(page, terminal)
    try:
        page.set_viewport_size({"width": 1440, "height": 920})
        page.evaluate("window.location.hash = '#now'")
        page.reload(wait_until="networkidle")
        page.get_by_text("All clear", exact=True).wait_for()
        page.get_by_text("No commitments are open.", exact=True).wait_for()
        if page.get_by_text("Your first hand", exact=True).count() != 0:
            raise RuntimeError("terminal history was rendered as a first run")
        if page.locator(".task-row").count() != len(terminal["tasks"]):
            raise RuntimeError("Now did not preserve every closed task record")
        page.locator(".task-row").first.click()
        page.locator("#inspector.is-open").wait_for()
        action = page.locator("#inspector-foot a.primary-action")
        if action.inner_text() != "Start a new hand":
            raise RuntimeError("a terminal inspector offered resume copy")
        if action.get_attribute("href") != terminal["codex"]["new_hand_url"]:
            raise RuntimeError("a terminal inspector did not use the generic new-hand link")
        page.keyboard.press("Escape")
        page.locator("button[data-view='commitments']").click()
        page.get_by_text("All clear", exact=True).wait_for()
        if page.locator(".task-row").count() != len(terminal["tasks"]):
            raise RuntimeError("Commitments did not preserve every closed task record")
        _assert_viewport(page)
        page.set_viewport_size({"width": 390, "height": 844})
        _assert_viewport(page)
        page.locator("#menu-button").click()
        page.locator("button[data-view='now']").click()
        page.get_by_text("All clear", exact=True).wait_for()
        if page.locator(".task-row").count() != len(terminal["tasks"]):
            raise RuntimeError("mobile Now hid closed task history")
        _assert_viewport(page)
    finally:
        page.unroute("**/api/v1/snapshot", handler)

    partial = copy.deepcopy(snapshot)
    partial_issue = {
        "code": "invalid-record",
        "message": "record metadata is invalid",
        "path": "tasks/unreadable.md",
        "repairable": False,
    }
    partial["projection"]["sections"]["tasks"] = {
        "issues": [partial_issue],
        "readable": len(partial["tasks"]),
        "state": "partial",
        "unreadable": 1,
    }
    partial["doctor"] = {
        **partial["doctor"],
        "healthy": False,
        "issues": [*partial["doctor"].get("issues", []), partial_issue],
    }
    handler = _route_snapshot(page, partial)
    try:
        page.set_viewport_size({"width": 1440, "height": 920})
        page.evaluate("window.location.hash = '#now'")
        page.reload(wait_until="networkidle")
        page.locator("#local-status.is-partial").wait_for()
        page.get_by_text("Some commitments could not be read", exact=True).wait_for()
        page.get_by_text(re.compile(r"tasks/unreadable\.md"), exact=False).wait_for()
        if page.get_by_text("All clear", exact=True).count() != 0:
            raise RuntimeError("a partial task projection claimed all-clear")
        if page.get_by_text("Your first hand", exact=True).count() != 0:
            raise RuntimeError("a partial task projection claimed first-run")
        expected_badge = f"{len(partial['tasks'])}+"
        if page.locator("#task-count").inner_text() != expected_badge:
            raise RuntimeError("the partial task badge hid unreadable records")
        _assert_viewport(page)
        page.set_viewport_size({"width": 390, "height": 844})
        _assert_viewport(page)
    finally:
        page.unroute("**/api/v1/snapshot", handler)

    only_bad = copy.deepcopy(partial)
    only_bad["tasks"] = []
    only_bad["projection"]["sections"]["tasks"]["readable"] = 0
    handler = _route_snapshot(page, only_bad)
    try:
        page.set_viewport_size({"width": 1440, "height": 920})
        page.reload(wait_until="networkidle")
        page.get_by_text("Some commitments could not be read", exact=True).wait_for()
        if page.get_by_text("All clear", exact=True).count() != 0:
            raise RuntimeError("an only-bad task section claimed all-clear")
        if page.get_by_text("Your first hand", exact=True).count() != 0:
            raise RuntimeError("an only-bad task section claimed first-run")
    finally:
        page.unroute("**/api/v1/snapshot", handler)

    missing_projection = copy.deepcopy(snapshot)
    missing_projection["tasks"] = []
    missing_projection.pop("projection", None)
    missing_projection["codex"]["new_mind_url"] = (
        "codex://new?prompt=Legacy%20Mind%20action&originUrl=gsv%3A%2F%2Fbridge"
    )
    handler = _route_snapshot(page, missing_projection)
    try:
        page.set_viewport_size({"width": 1440, "height": 920})
        page.evaluate("window.location.hash = '#now'")
        page.reload(wait_until="networkidle")
        page.get_by_text("Commitments unavailable", exact=True).wait_for()
        for forbidden in (
            "All clear",
            "Your first hand",
            "Shape the Mind that will meet you here.",
        ):
            if page.get_by_text(forbidden, exact=True).count() != 0:
                raise RuntimeError(
                    f"a snapshot without projection metadata exposed unsafe copy: {forbidden}"
                )
        if page.locator(f'a[href="{missing_projection["codex"]["new_mind_url"]}"]').count() != 0:
            raise RuntimeError("a snapshot without projection metadata exposed a Mind action")
        _assert_viewport(page)
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_text("Commitments unavailable", exact=True).wait_for()
        _assert_viewport(page)
    finally:
        page.unroute("**/api/v1/snapshot", handler)

    overflow = copy.deepcopy(snapshot)
    template = copy.deepcopy(snapshot["tasks"][0])
    overflow_tasks = []
    for status, count in (("ready", 4), ("waiting", 5)):
        for index in range(count):
            task = copy.deepcopy(template)
            task["identifier"] = f"{status}-{index + 1}"
            task["title"] = f"{status.title()} commitment {index + 1}"
            task["status"] = status
            task["next_actor"] = "agent" if status == "ready" else "external"
            task["next_action"] = f"Inspect {status} commitment {index + 1}."
            task["waiting_on"] = "A bounded external event." if status == "waiting" else None
            overflow_tasks.append(task)
    overflow["tasks"] = overflow_tasks
    overflow["projection"]["sections"]["tasks"] = {
        "issues": [],
        "readable": len(overflow_tasks),
        "state": "complete",
        "unreadable": 0,
    }
    overflow["entities"] = [
        {
            "entity_type": "topic",
            "identifier": f"topic:entity-{index + 1}",
            "title": f"Entity {index + 1}",
        }
        for index in range(13)
    ]
    overflow["projection"]["sections"]["entities"] = {
        "issues": [],
        "readable": 13,
        "state": "complete",
        "unreadable": 0,
    }
    handler = _route_snapshot(page, overflow)
    try:
        page.set_viewport_size({"width": 1440, "height": 920})
        page.evaluate("window.location.hash = '#now'")
        page.reload(wait_until="networkidle")
        page.get_by_role("button", name="1 more · View Commitments", exact=True).wait_for()
        page.get_by_role("button", name="2 more · View Commitments", exact=True).wait_for()
        page.get_by_role("button", name="1 more · View Commitments", exact=True).click()
        page.locator("button[data-view='commitments'].is-active").wait_for()
        if page.locator(".task-card").count() != 9:
            raise RuntimeError("the Now disclosure did not navigate to every commitment")
        page.locator("button[data-view='mind']").click()
        page.get_by_text("1 more entities not shown.", exact=True).wait_for()
        _assert_viewport(page)
        page.set_viewport_size({"width": 390, "height": 844})
        _assert_viewport(page)
        page.locator("#menu-button").click()
        page.locator("button[data-view='now']").click()
        page.get_by_role("button", name="1 more · View Commitments", exact=True).wait_for()
        page.get_by_role("button", name="2 more · View Commitments", exact=True).wait_for()
        _assert_viewport(page)
    finally:
        page.unroute("**/api/v1/snapshot", handler)
        page.set_viewport_size({"width": 1440, "height": 920})


def _verify_live_vault_states(browser: Browser, root: Path) -> None:
    terminal = Vault(root / "terminal-vault")
    terminal.initialize(name="Terminal state proof")
    for status in ("done", "dropped"):
        terminal.create_task(
            identifier=f"closed-{status}",
            title=f"Closed {status}",
            outcome=f"The {status} record remains visible.",
            status=status,
        )
    with _state_page(browser, terminal) as page:
        page.get_by_text("All clear", exact=True).wait_for(timeout=10_000)
        page.get_by_text("No commitments are open.", exact=True).wait_for()
        if page.locator(".task-row").count() != 2:
            raise RuntimeError("the real terminal vault hid closed history in Now")
        page.locator(".task-row").first.click()
        page.locator("#inspector-foot a", has_text="Start a new hand").wait_for(timeout=10_000)
        page.keyboard.press("Escape")
        page.locator("button[data-view='commitments']").click()
        if page.locator(".task-row").count() != 2:
            raise RuntimeError("the real terminal vault hid closed history in Commitments")
        _assert_viewport(page)
        page.set_viewport_size({"width": 390, "height": 844})
        _assert_viewport(page)

    partial = Vault(root / "partial-vault")
    partial.initialize(name="Partial state proof")
    partial.create_task(
        identifier="readable-task",
        title="Readable task",
        outcome="The valid task remains exact.",
        status="ready",
        next_actor="agent",
        next_action="Keep this readable while reporting the damaged neighbor.",
    )
    (partial.root / "tasks/unreadable.md").write_bytes(b"\xff\xfe")
    with _state_page(browser, partial) as page:
        page.locator("#local-status.is-partial").wait_for(timeout=10_000)
        page.get_by_text("Some commitments could not be read", exact=True).wait_for()
        page.get_by_text(re.compile(r"tasks/unreadable\.md"), exact=False).wait_for()
        if page.locator("#task-count").inner_text() != "1+":
            raise RuntimeError("the real partial vault hid its unreadable task count")
        if page.get_by_text("All clear", exact=True).count() != 0:
            raise RuntimeError("the real partial vault claimed all-clear")
        if page.get_by_text("Your first hand", exact=True).count() != 0:
            raise RuntimeError("the real partial vault claimed first-run")
        _assert_viewport(page)
        page.set_viewport_size({"width": 390, "height": 844})
        _assert_viewport(page)

    overflow = Vault(root / "overflow-vault")
    overflow.initialize(name="Overflow state proof")
    for status, count in (("ready", 4), ("waiting", 5)):
        for index in range(count):
            overflow.create_task(
                identifier=f"{status}-{index + 1}",
                title=f"{status.title()} commitment {index + 1}",
                outcome="Every commitment remains available in the complete view.",
                status=status,
                next_actor="agent" if status == "ready" else "external",
                next_action=f"Inspect {status} commitment {index + 1}.",
                waiting_on="A bounded external event." if status == "waiting" else None,
            )
    for index in range(13):
        overflow.create_entity(
            identifier=f"topic:entity-{index + 1}",
            title=f"Entity {index + 1}",
            entity_type="topic",
            summary="Synthetic entity used only for bounded disclosure proof.",
        )
    with _state_page(browser, overflow) as page:
        page.get_by_role("button", name="1 more · View Commitments", exact=True).wait_for()
        page.get_by_role("button", name="2 more · View Commitments", exact=True).wait_for()
        page.get_by_role("button", name="1 more · View Commitments", exact=True).click()
        if page.locator(".task-card").count() != 9:
            raise RuntimeError("the real overflow vault did not disclose all commitments")
        page.locator("button[data-view='mind']").click()
        page.get_by_text("1 more entities not shown.", exact=True).wait_for()
        _assert_viewport(page)
        page.set_viewport_size({"width": 390, "height": 844})
        _assert_viewport(page)
        page.locator("#menu-button").click()
        page.locator("button[data-view='now']").click()
        page.get_by_role("button", name="2 more · View Commitments", exact=True).wait_for()
        _assert_viewport(page)


@contextmanager
def _state_page(browser: Browser, vault: Vault) -> Iterator[Any]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    resource = files("continuity_kernel") / "resources/bridge"
    with as_file(resource) as static_root:
        server = bridge.BridgeHTTPServer(
            (bridge.LOOPBACK_HOST, 0),
            vault,
            Path(static_root),
            access_token=TOKEN,
            instance_id=INSTANCE,
            integration_provider=_synthetic_codex_status,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        page = browser.new_page(
            viewport={"width": 1440, "height": 920},
            device_scale_factor=1,
            reduced_motion="reduce",
        )
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        url = f"http://{bridge.LOOPBACK_HOST}:{server.server_address[1]}/#token={TOKEN}"
        try:
            page.goto(url, wait_until="networkidle")
            yield page
            _assert_no_browser_errors(console_errors, page_errors)
        finally:
            page.close()
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def _without_codex_links(snapshot: dict[str, Any], *, available: bool) -> dict[str, Any]:
    payload = copy.deepcopy(snapshot)
    payload["codex"] = {
        "available": available,
        "checking": False,
        **(
            {"error": "Codex executable was not found in the verified local paths."}
            if not available
            else {}
        ),
        "instructions_installed": False,
        "plugin_installed": False,
        "ready": False,
    }
    for task in payload["tasks"]:
        task.pop("codex_url", None)
    return payload


def _capture_codex_recovery_states(page: Any, snapshot: dict[str, Any], output: Path) -> None:
    uninstalled = _without_codex_links(snapshot, available=True)
    handler = _route_snapshot(page, uninstalled)
    try:
        page.reload(wait_until="networkidle")
        page.locator("#local-status.is-healthy").wait_for(timeout=5_000)
        _assert_codex_links(page, expected=False)
        page.locator("button[data-view='system']").click()
        page.get_by_text("Run setup", exact=True).wait_for()
        page.locator(".command-action code", has_text="gsv codex install").wait_for()
        page.locator("button[data-view='commitments']").click()
        page.locator(".task-card").first.click()
        page.locator("#inspector.is-open").wait_for()
        _assert_visibility(page, "#inspector-foot", visible=True)
        if page.locator("#continue-in-codex").count() != 0:
            raise RuntimeError("an unready inspector retained a hidden Codex anchor")
        page.locator("#inspector-foot code", has_text="gsv codex install").wait_for()
        page.keyboard.press("Escape")
    finally:
        page.unroute("**/api/v1/snapshot", handler)

    unavailable = _without_codex_links(snapshot, available=False)
    handler = _route_snapshot(page, unavailable)
    try:
        page.reload(wait_until="networkidle")
        page.locator("#local-status.is-healthy").wait_for(timeout=5_000)
        page.locator("button[data-view='system']").click()
        page.get_by_text("Unavailable", exact=True).wait_for()
        _assert_codex_links(page, expected=False)
        page.get_by_text(
            "Codex executable was not found in the verified local paths.", exact=True
        ).wait_for()
        command = page.locator(".command-action", has_text="gsv codex install")
        button = command.locator("button")
        button.click()
        page.wait_for_function(
            "button => ['Copied', 'Select command'].includes(button.textContent)",
            arg=button.element_handle(),
            timeout=2_000,
        )
        if button.inner_text() not in {"Copied", "Select command"}:
            raise RuntimeError("the Codex recovery command did not acknowledge its copy action")
        _reset_scroll(page)
        page.screenshot(path=str(output / "bridge-codex-unavailable.png"), animations="disabled")
    finally:
        page.unroute("**/api/v1/snapshot", handler)


def _capture_unknown_status(page: Any, snapshot: dict[str, Any], output: Path) -> None:
    payload = copy.deepcopy(snapshot)
    task = payload["tasks"][0]
    task["status"] = "reviewing"
    task["updated_at"] = (
        (datetime.now(UTC) - timedelta(seconds=59.2)).isoformat().replace("+00:00", "Z")
    )
    handler = _route_snapshot(page, payload)
    try:
        page.reload(wait_until="networkidle")
        page.locator("#local-status.is-healthy").wait_for(timeout=5_000)
        page.locator("button[data-view='commitments']").click()
        page.locator(".lane-head h3", has_text="Reviewing").wait_for()
        lane_names = page.locator(".lane-head h3").all_inner_texts()
        schema_order = ["Captured", "Ready", "Doing", "Waiting", "Someday"]
        known = [name for name in lane_names if name in schema_order]
        if known != sorted(known, key=schema_order.index) or lane_names[-1] != "Reviewing":
            raise RuntimeError(f"task lanes are not in deterministic schema order: {lane_names}")
        card = page.get_by_role("button", name=re.compile(re.escape(task["title"])))
        if card.locator(".status-pill").inner_text() != "Reviewing":
            raise RuntimeError("an unknown authored status was renamed or hidden")
        timestamp = card.locator("[data-relative-time]")
        if timestamp.inner_text() != "Just now":
            raise RuntimeError("the relative-time fixture did not begin at Just now")
        page.wait_for_timeout(2_000)
        if timestamp.inner_text() != "1m ago":
            raise RuntimeError("relative task time froze when the snapshot stayed unchanged")
        _reset_scroll(page)
        page.screenshot(path=str(output / "bridge-unknown-status.png"), animations="disabled")
    finally:
        page.unroute("**/api/v1/snapshot", handler)


def _capture_integrity_warning(page: Any, snapshot: dict[str, Any], output: Path) -> None:
    payload = copy.deepcopy(snapshot)
    payload["doctor"] = {
        **payload["doctor"],
        "healthy": False,
        "issues": [
            {
                "code": "invalid-journal",
                "message": "invalid final journal fragment",
                "path": ".gsv/events.jsonl",
                "repairable": True,
            },
            {
                "code": "invalid-record",
                "message": "record metadata is invalid",
                "path": "tasks/review-atlas.md",
                "repairable": False,
            },
        ],
    }
    handler = _route_snapshot(page, payload)
    try:
        page.reload(wait_until="networkidle")
        page.locator("#local-status.is-partial").wait_for(timeout=5_000)
        page.locator("button[data-view='system']").click()
        page.get_by_text(".gsv/events.jsonl", exact=True).wait_for()
        page.get_by_text("invalid final journal fragment", exact=True).wait_for()
        page.get_by_text("tasks/review-atlas.md", exact=True).wait_for()
        page.get_by_text("record metadata is invalid", exact=True).wait_for()
        commands = page.locator(".recovery-panel code").all_inner_texts()
        if commands != ["gsv doctor --repair", "gsv doctor"]:
            raise RuntimeError(
                f"doctor recovery commands do not distinguish issue types: {commands}"
            )
        _reset_scroll(page)
        page.screenshot(path=str(output / "bridge-integrity-warning.png"), animations="disabled")
        page.set_viewport_size({"width": 390, "height": 844})
        _reset_scroll(page)
        page.locator(".recovery-panel").scroll_into_view_if_needed()
        _assert_visibility(page, ".recovery-panel code", visible=True, first=True)
        if page.locator(".recovery-panel code").count() != 2:
            raise RuntimeError("mobile recovery did not retain both doctor commands")
        _assert_viewport(page)
        page.screenshot(path=str(output / "bridge-mobile-recovery.png"), animations="disabled")
        page.set_viewport_size({"width": 1440, "height": 920})
    finally:
        page.unroute("**/api/v1/snapshot", handler)


def _assert_exact_authored_task_state(page: Any) -> None:
    lanes = page.locator(".lane-head h3").all_inner_texts()
    if lanes != ["Ready", "Doing", "Waiting"]:
        raise RuntimeError(f"task lanes do not preserve authored schema order: {lanes}")
    pills = set(page.locator(".task-card .status-pill").all_inner_texts())
    if pills != {"Ready", "Doing", "Waiting"}:
        raise RuntimeError(f"task status labels were renamed: {sorted(pills)}")


def _assert_codex_links(page: Any, *, expected: bool) -> None:
    links = page.locator('a[href^="codex://new?"]')
    if expected:
        if links.count() < 1:
            raise RuntimeError("a ready Codex integration exposed no new-hand action")
        return
    if links.count() != 0:
        raise RuntimeError("an unready Codex integration exposed an actionable deep link")
    if page.locator("#open-codex").get_attribute("href") is not None:
        raise RuntimeError("the hidden top-bar Codex action retained an href")


def _launch(browser_type: Any) -> Browser:
    try:
        return cast(Browser, browser_type.launch(channel="chrome", headless=True))
    except Error:
        return cast(Browser, browser_type.launch(headless=True))


def _assert_viewport(page: Any) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    if overflow:
        raise RuntimeError("Bridge content overflows the current viewport")


def _reset_scroll(page: Any) -> None:
    page.evaluate(
        """() => {
          window.scrollTo({top: 0, left: 0, behavior: 'auto'});
          const rail = document.querySelector('.rail');
          if (rail) rail.scrollTop = 0;
        }"""
    )
    page.wait_for_timeout(50)
    if page.evaluate("window.scrollY") > 1:
        raise RuntimeError("Bridge state capture retained an unexpected vertical scroll offset")
    viewport = page.viewport_size or {}
    if viewport.get("width", 0) > 760:
        _assert_visibility(page, ".brand", visible=True)
        brand_top = page.locator(".brand").bounding_box()
        if brand_top is None or not 0 <= brand_top["y"] <= 60:
            raise RuntimeError(f"Bridge brand escaped the desktop viewport: {brand_top}")


def _assert_mobile_heading(page: Any) -> None:
    geometry = page.evaluate(
        """() => {
          const topbar = document.querySelector('.topbar').getBoundingClientRect();
          const eyebrowNode = document.querySelector('.page-heading > .eyebrow');
          const eyebrow = eyebrowNode.getBoundingClientRect();
          return {scrollY: window.scrollY, topbarBottom: topbar.bottom, eyebrowTop: eyebrow.top};
        }"""
    )
    if geometry["scrollY"] > 1 or geometry["eyebrowTop"] < geometry["topbarBottom"]:
        raise RuntimeError(f"mobile heading is obscured by the sticky topbar: {geometry}")
    _assert_visibility(page, ".page-heading > .eyebrow", visible=True)


def _assert_visibility(page: Any, selector: str, *, visible: bool, first: bool = False) -> None:
    locator = page.locator(selector)
    actual = (locator.first if first else locator).is_visible()
    if actual is not visible:
        expectation = "visible" if visible else "hidden"
        raise RuntimeError(f"expected {selector} to be {expectation}")


def _assert_orb_canvas(page: Any, selector: str) -> None:
    inked = page.locator(selector).first.evaluate(
        """canvas => {
          const context = canvas.getContext('2d');
          const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
          let count = 0;
          for (let index = 3; index < pixels.length; index += 4) {
            if (pixels[index] > 0) count += 1;
          }
          return count;
        }"""
    )
    if inked < 6:
        raise RuntimeError(f"thinking orb did not render enough canvas pixels: {inked}")


def _assert_reduced_orb_static(page: Any, selector: str) -> None:
    locator = page.locator(selector).first
    before = locator.evaluate("canvas => canvas.toDataURL()")
    page.wait_for_timeout(160)
    after = locator.evaluate("canvas => canvas.toDataURL()")
    if before != after:
        raise RuntimeError("reduced-motion thinking orb continued animating")


def _consume_expected_network_errors(errors: list[str], start: int) -> None:
    induced = errors[start:]
    unexpected = [error for error in induced if "net::ERR_FAILED" not in error]
    if unexpected:
        raise RuntimeError(f"offline-state capture emitted unexpected console errors: {unexpected}")
    del errors[start:]


def _assert_no_browser_errors(console_errors: list[str], page_errors: list[str]) -> None:
    if console_errors:
        raise RuntimeError(f"Bridge emitted browser console errors: {console_errors}")
    if page_errors:
        raise RuntimeError(f"Bridge emitted uncaught browser errors: {page_errors}")


def _assert_nonblank(path: Path) -> None:
    with Image.open(path) as image:
        colors = image.convert("RGB").resize((160, 100)).getcolors(maxcolors=16_001)
    if colors is None or len(colors) < 20:
        raise RuntimeError(f"visual proof appears blank or incomplete: {path}")


def _write_gif(frames: list[Path], target: Path) -> None:
    prepared: list[Image.Image] = []
    for path in frames:
        with Image.open(path) as image:
            width = 1120
            height = round(image.height * width / image.width)
            prepared.append(image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS))
    prepared[0].save(
        target,
        append_images=prepared[1:],
        disposal=2,
        duration=[1800, 1500, 2400],
        loop=0,
        optimize=True,
        save_all=True,
    )
    for prepared_image in prepared:
        prepared_image.close()


if __name__ == "__main__":
    raise SystemExit(main())
