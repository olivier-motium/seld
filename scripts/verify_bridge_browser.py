#!/usr/bin/env python3
"""Exercise the packaged Bridge in Chromium without persisting visual artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Error, Page, sync_playwright

from continuity_kernel import bridge
from continuity_kernel.demo import run_demo
from continuity_kernel.vault import Vault

TOKEN = "d" * 48
INSTANCE = "e" * 32
ARTIFACT_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".mov", ".mp4", ".png", ".webp"})
IGNORED_ARTIFACT_ROOTS = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist"}
)


def _artifact_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for directory, names, filenames in os.walk(root):
        names[:] = [name for name in names if name not in IGNORED_ARTIFACT_ROOTS]
        base = Path(directory)
        for name in filenames:
            path = base / name
            if path.suffix.lower() in ARTIFACT_SUFFIXES:
                snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return snapshot


def _synthetic_codex_status() -> dict[str, Any]:
    return {
        "available": True,
        "instructions_installed": True,
        "manifest_verified": True,
        "marketplace_registered": True,
        "marketplace_root_verified": True,
        "plugin_installed": True,
        "ready": True,
        "receipt_active": True,
    }


def _assert_no_overflow(page: Page) -> None:
    metrics = page.evaluate(
        """() => ({
          body: document.body.scrollWidth,
          document: document.documentElement.scrollWidth,
          viewport: window.innerWidth,
        })"""
    )
    if metrics["body"] > metrics["viewport"] or metrics["document"] > metrics["viewport"]:
        raise RuntimeError(f"Bridge overflows its viewport: {metrics}")


def _assert_healthy(page: Page, console_errors: list[str], page_errors: list[str]) -> None:
    try:
        page.locator("#local-status.is-healthy").wait_for(timeout=10_000)
    except Error as exc:
        status = page.locator("#local-status").inner_text()
        notice = page.locator("#connection-copy").inner_text()
        raise RuntimeError(
            f"Bridge did not become healthy: status={status!r}; notice={notice!r}; "
            f"console={console_errors!r}; page={page_errors!r}"
        ) from exc
    if page.locator("#connection-notice").is_visible():
        raise RuntimeError("a healthy Bridge retained its connection warning")
    page.locator("#open-codex").wait_for(state="visible", timeout=5_000)
    if page.locator('a[href^="codex://new?"]').count() < 1:
        raise RuntimeError("a ready Codex integration exposed no new-hand action")
    spacing = page.locator(".eyebrow").first.evaluate(
        "element => getComputedStyle(element).letterSpacing"
    )
    if spacing != "-0.15px":
        raise RuntimeError(f"the preserved Bridge letter spacing changed: {spacing!r}")
    if console_errors or page_errors:
        raise RuntimeError(
            f"Bridge emitted browser errors: console={console_errors!r}; page={page_errors!r}"
        )
    _assert_no_overflow(page)


def _verify_browser(browser: Browser, url: str) -> dict[str, bool]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page = browser.new_page(
        viewport={"width": 1440, "height": 920},
        device_scale_factor=1,
        reduced_motion="reduce",
    )
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    _assert_healthy(page, console_errors, page_errors)

    page.locator("button[data-view='commitments']").click()
    page.locator(".commitment-grid").wait_for()
    lanes = page.locator(".lane-head h3").all_inner_texts()
    if lanes != ["Ready", "Doing", "Waiting"]:
        raise RuntimeError(f"task lanes changed authored order: {lanes}")
    atlas = page.get_by_role("button", name=re.compile(r"Ship the Atlas migration"))
    if atlas.count() != 1:
        raise RuntimeError("the Atlas commitment is not uniquely addressable")
    atlas.click()
    page.locator("#inspector.is-open").wait_for()
    if not page.locator("#inspector-foot").is_visible():
        raise RuntimeError("the commitment inspector lost its continuation action")
    page.keyboard.press("Escape")
    page.locator("#inspector:not(.is-open)").wait_for()

    page.route("**/api/v1/snapshot", lambda route: route.fulfill(status=503, body="unavailable"))
    page.locator("button[data-view='now']").click()
    page.locator(".connection-notice.is-stale").wait_for(timeout=15_000)
    if not page.locator("#connection-orb").is_visible():
        raise RuntimeError("the stale state lost its continuity indicator")
    page.close()

    unavailable = browser.new_page(viewport={"width": 1024, "height": 760})
    unavailable.route(
        "**/api/v1/snapshot", lambda route: route.fulfill(status=503, body="unavailable")
    )
    unavailable.goto(url, wait_until="domcontentloaded")
    unavailable.locator(".unavailable-state").wait_for(timeout=5_000)
    _assert_no_overflow(unavailable)
    unavailable.close()

    mobile = browser.new_page(
        viewport={"width": 390, "height": 844},
        device_scale_factor=1,
        reduced_motion="reduce",
    )
    mobile.goto(url, wait_until="networkidle")
    mobile.locator("#local-status.is-healthy").wait_for(timeout=10_000)
    mobile.locator("#menu-button").click()
    mobile.locator("#rail.is-open").wait_for()
    if not mobile.locator("#rail-backdrop").is_visible():
        raise RuntimeError("the mobile navigation lost its backdrop")
    mobile.locator("button[data-view='commitments']").click()
    mobile.locator("#rail:not(.is-open)").wait_for()
    mobile.get_by_role("button", name=re.compile(r"Ship the Atlas migration")).click()
    mobile.locator("#inspector.is-open").wait_for()
    _assert_no_overflow(mobile)
    mobile.close()
    return {"desktop": True, "inspector": True, "mobile": True, "recovery": True}


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    artifacts_before = _artifact_snapshot(project_root)
    with tempfile.TemporaryDirectory(prefix="gsv-browser-proof-") as raw:
        vault_root = Path(raw) / "synthetic-vault"
        proof = run_demo(vault_root)
        if not proof["fresh_process_resumed"] or not proof["hand_process_killed"]:
            raise RuntimeError("the synthetic handoff proof failed before browser verification")
        resource = files("continuity_kernel") / "resources/bridge"
        with as_file(resource) as static_root:
            server = bridge.BridgeHTTPServer(
                (bridge.LOOPBACK_HOST, 0),
                Vault(vault_root),
                Path(static_root),
                access_token=TOKEN,
                instance_id=INSTANCE,
                integration_provider=_synthetic_codex_status,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://{bridge.LOOPBACK_HOST}:{server.server_address[1]}/#token={TOKEN}"
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        states = _verify_browser(browser, url)
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()
    artifacts_after = _artifact_snapshot(project_root)
    changed_artifacts = {
        path
        for path in artifacts_before.keys() | artifacts_after.keys()
        if artifacts_before.get(path) != artifacts_after.get(path)
    }
    if changed_artifacts:
        raise RuntimeError(
            "browser verification changed generated media: " + ", ".join(sorted(changed_artifacts))
        )
    print(
        json.dumps(
            {"artifact_files": len(changed_artifacts), "browser": "chromium", **states},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
