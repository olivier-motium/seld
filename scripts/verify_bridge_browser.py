#!/usr/bin/env python3
"""Exercise the packaged Bridge in Chromium without persisting visual artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Error, Page, sync_playwright

from continuity_kernel import bridge
from continuity_kernel.demo import run_demo
from continuity_kernel.local_files import LOCAL_FILE_READER_TOOL
from continuity_kernel.source_state import ABSENT_SOURCE_REVISION
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


def _assert_no_unproven_caught_up(page: Page) -> None:
    copy = page.locator("body").inner_text().casefold()
    if "already caught up" in copy or "you're caught up" in copy:
        raise RuntimeError("Bridge claimed caught-up state without source/Pulse freshness evidence")


def _assert_no_false_ready(page: Page) -> None:
    title = page.locator("#page-title").inner_text().casefold()
    first_run = page.get_by_role("heading", name="Tell Seld what matters in your life.").count()
    qualified = page.locator(
        ".connection-notice.is-partial, .connection-notice.is-stale, "
        ".connection-notice.is-unavailable, .unavailable-state"
    ).count()
    if (first_run or qualified) and "ready" in title:
        raise RuntimeError(f"Bridge claimed a ready brief in a qualified state: {title!r}")


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
    typography = page.evaluate(
        """() => ({
          body: getComputedStyle(document.body).fontFamily,
          heading: getComputedStyle(document.querySelector('#page-title')).fontFamily,
          headingSize: parseFloat(getComputedStyle(document.querySelector('#page-title')).fontSize),
        })"""
    )
    if "Nunito Sans" not in typography["body"] or "Nunito" not in typography["heading"]:
        raise RuntimeError(f"the Seld Bridge fonts were not applied: {typography!r}")
    if typography["headingSize"] < 36:
        raise RuntimeError(f"the Seld display hierarchy collapsed: {typography!r}")
    _assert_no_unproven_caught_up(page)
    if console_errors or page_errors:
        raise RuntimeError(
            f"Bridge emitted browser errors: console={console_errors!r}; page={page_errors!r}"
        )
    _assert_no_overflow(page)


def _assert_contrast(page: Page, state_name: str) -> None:
    failures = page.evaluate(
        r"""() => {
          const parse = (value) => {
            const match = value.match(/[\d.]+/g);
            return match ? match.slice(0, 3).map(Number) : null;
          };
          const luminance = (rgb) => {
            const channels = rgb.map((value) => {
              const channel = value / 255;
              return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
          };
          const ratio = (foreground, background) => {
            const a = luminance(parse(foreground));
            const b = luminance(parse(background));
            return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
          };
          const visible = (node) => {
            const style = getComputedStyle(node);
            return style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity) > 0 && node.getClientRects().length > 0;
          };
          const directText = (node) => [...node.childNodes]
            .some((child) => child.nodeType === Node.TEXT_NODE && child.textContent.trim());
          const textNodes = [...document.querySelectorAll('body *')].filter((node) =>
            visible(node)
            && !node.closest(':disabled, [aria-disabled="true"]')
            && directText(node)
          );
          return textNodes.flatMap((node) => {
            const style = getComputedStyle(node);
            let parent = node;
            let background = style.backgroundColor;
            while (parent && (background === 'rgba(0, 0, 0, 0)' || background === 'transparent')) {
              parent = parent.parentElement;
              background = parent ? getComputedStyle(parent).backgroundColor : 'rgb(255, 255, 255)';
            }
            const actual = ratio(style.color, background);
            const size = parseFloat(style.fontSize);
            const weight = Number(style.fontWeight) || 400;
            const required = size >= 24 || (size >= 18.66 && weight >= 700) ? 3 : 4.5;
            if (actual + 0.005 >= required) return [];
            return [{
              background,
              color: style.color,
              ratio: actual,
              required,
              selector: node.id ? `#${node.id}` : `${node.tagName.toLowerCase()}.${node.className}`,
              text: node.textContent.trim().slice(0, 80),
            }];
          });
        }"""
    )
    if failures:
        raise RuntimeError(
            f"Seld Bridge text contrast fell below WCAG AA in {state_name}: {failures!r}"
        )


def _track_same_origin(page: Page, url: str, foreign_requests: list[str]) -> None:
    bridge_origin = urlsplit(url)
    page.on(
        "request",
        lambda request: (
            foreign_requests.append(request.url)
            if (
                urlsplit(request.url).scheme in {"http", "https"}
                and (
                    urlsplit(request.url).scheme != bridge_origin.scheme
                    or urlsplit(request.url).netloc != bridge_origin.netloc
                )
            )
            else None
        ),
    )


def _assert_same_origin_resources(page: Page, url: str) -> None:
    bridge_origin = urlsplit(url)
    foreign_resources = page.evaluate(
        """(origin) => performance.getEntriesByType('resource')
          .map((entry) => entry.name)
          .filter((name) => ['http:', 'https:'].includes(new URL(name).protocol))
          .filter((name) => new URL(name).origin !== origin)""",
        f"{bridge_origin.scheme}://{bridge_origin.netloc}",
    )
    if foreign_resources:
        raise RuntimeError(f"Bridge loaded foreign runtime resources: {foreign_resources!r}")


def _verify_browser(browser: Browser, url: str) -> dict[str, bool]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page = browser.new_page(
        viewport={"width": 1440, "height": 920},
        device_scale_factor=1,
        reduced_motion="reduce",
    )
    foreign_requests: list[str] = []
    _track_same_origin(page, url, foreign_requests)
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    _assert_healthy(page, console_errors, page_errors)
    _assert_contrast(page, "home")
    _assert_no_false_ready(page)
    _assert_same_origin_resources(page, url)

    page.locator("button[data-view='commitments']").click()
    page.get_by_role("heading", name="Review the decisions that need you").wait_for()
    _assert_contrast(page, "rundown")
    page.locator("button[data-view='storylines']").click()
    page.locator(".commitment-grid").wait_for()
    lanes = page.locator(".lane-head h3").all_inner_texts()
    if lanes != ["Ready", "Doing", "Waiting"]:
        raise RuntimeError(f"task lanes changed authored order: {lanes}")
    _assert_contrast(page, "everything")
    atlas = page.get_by_role("button", name=re.compile(r"Ship the Atlas migration"))
    if atlas.count() != 1:
        raise RuntimeError("the Atlas commitment is not uniquely addressable")
    atlas.click()
    page.locator("#inspector.is-open").wait_for()
    if not page.locator("#inspector-foot").is_visible():
        raise RuntimeError("the commitment inspector lost its continuation action")
    try:
        page.locator('#inspector-foot a[href^="codex://new?"]').wait_for(
            state="visible", timeout=5_000
        )
    except Error as exc:
        raise RuntimeError("the exact commitment inspector lost its ChatGPT continuation") from exc
    page.keyboard.press("Escape")
    page.locator("#inspector:not(.is-open)").wait_for()

    page.locator("button[data-view='mind']").click()
    _assert_contrast(page, "knowledge")
    page.locator("button[data-view='system']").click()
    for label, status in (
        ("Gmail", "Current"),
        ("Slack", "Current"),
        ("Figma", "Partial"),
        ("Files you choose", "Recheck"),
        ("Shopify", "Read failed"),
    ):
        row = page.locator(".system-row", has=page.get_by_role("heading", name=label, exact=True))
        if row.count() != 1 or row.locator(".system-state").inner_text() != status:
            raise RuntimeError(f"source health row was not rendered honestly: {label} -> {status}")
    local_files_row = page.locator(
        ".system-row",
        has=page.get_by_role("heading", name="Files you choose", exact=True),
    )
    local_files_copy = local_files_row.inner_text().casefold()
    if "current local access" not in local_files_copy or "new bounded read" not in local_files_copy:
        raise RuntimeError("the local-files revalidation row lost its authority-change explanation")
    _assert_contrast(page, "system")

    page.route("**/api/v1/snapshot", lambda route: route.fulfill(status=503, body="unavailable"))
    page.locator("button[data-view='now']").click()
    page.locator(".connection-notice.is-stale").wait_for(timeout=15_000)
    if not page.locator("#connection-orb").is_visible():
        raise RuntimeError("the stale state lost its continuity indicator")
    _assert_no_unproven_caught_up(page)
    _assert_no_false_ready(page)
    _assert_contrast(page, "stale")
    _assert_same_origin_resources(page, url)
    page.close()

    unavailable = browser.new_page(viewport={"width": 1024, "height": 760})
    _track_same_origin(unavailable, url, foreign_requests)
    unavailable.route(
        "**/api/v1/snapshot", lambda route: route.fulfill(status=503, body="unavailable")
    )
    unavailable.goto(url, wait_until="domcontentloaded")
    unavailable.locator(".unavailable-state").wait_for(timeout=5_000)
    _assert_no_unproven_caught_up(unavailable)
    _assert_no_false_ready(unavailable)
    _assert_contrast(unavailable, "unavailable")
    _assert_no_overflow(unavailable)
    _assert_same_origin_resources(unavailable, url)
    unavailable.close()

    mobile = browser.new_page(
        viewport={"width": 390, "height": 844},
        device_scale_factor=1,
        reduced_motion="reduce",
    )
    _track_same_origin(mobile, url, foreign_requests)
    mobile.goto(url, wait_until="networkidle")
    mobile.locator("#local-status.is-healthy").wait_for(timeout=10_000)
    mobile.locator("#menu-button").click()
    mobile.locator("#rail.is-open").wait_for()
    if not mobile.locator("#rail-backdrop").is_visible():
        raise RuntimeError("the mobile navigation lost its backdrop")
    mobile.locator("button[data-view='storylines']").click()
    mobile.locator("#rail:not(.is-open)").wait_for()
    mobile.get_by_role("button", name=re.compile(r"Ship the Atlas migration")).click()
    mobile.locator("#inspector.is-open").wait_for()
    _assert_no_overflow(mobile)
    _assert_contrast(mobile, "mobile")
    _assert_same_origin_resources(mobile, url)
    mobile.close()

    forced = browser.new_page(
        viewport={"width": 1024, "height": 760},
        reduced_motion="reduce",
        forced_colors="active",
    )
    _track_same_origin(forced, url, foreign_requests)
    forced.goto(url, wait_until="networkidle")
    forced.locator("#local-status.is-healthy").wait_for(timeout=10_000)
    forced.locator("button[data-view='commitments']").focus()
    forced_focus = forced.locator("button[data-view='commitments']").evaluate(
        "element => ({ outline: getComputedStyle(element).outlineStyle, text: element.innerText })"
    )
    if forced_focus["outline"] == "none" or "Rundown" not in forced_focus["text"]:
        raise RuntimeError(f"forced-colors focus/status treatment failed: {forced_focus!r}")
    _assert_no_overflow(forced)
    _assert_same_origin_resources(forced, url)
    forced.close()
    if foreign_requests:
        raise RuntimeError(f"Bridge requested foreign runtime resources: {foreign_requests!r}")
    return {
        "contrast": True,
        "desktop": True,
        "forced_colors": True,
        "inspector": True,
        "mobile": True,
        "same_origin": True,
        "recovery": True,
    }


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    artifacts_before = _artifact_snapshot(project_root)
    with tempfile.TemporaryDirectory(prefix="gsv-browser-proof-") as raw:
        os.environ["GSV_DATA_DIR"] = str(Path(raw) / "app-data")
        vault_root = Path(raw) / "synthetic-vault"
        proof = run_demo(vault_root)
        if not proof["fresh_process_resumed"] or not proof["hand_process_killed"]:
            raise RuntimeError("the synthetic handoff proof failed before browser verification")
        now = datetime.now(UTC)
        vault = Vault(vault_root)
        selected = vault.select_sources(
            expected_revision=ABSENT_SOURCE_REVISION,
            sources=("figma", "gmail", "local_files", "shopify", "slack"),
        )
        local_root = Path(raw) / "selected-local-files"
        local_root.mkdir()
        (local_root / "proof.txt").write_text("Synthetic bounded file proof.\n", encoding="utf-8")
        local_grant = vault.grant_local_file_root(local_root)["grant"]
        local_read = vault.read_local_file(
            grant_id=local_grant["grant_id"],
            relative_path="proof.txt",
        )
        if local_read.get("content") != "Synthetic bounded file proof.\n":
            raise RuntimeError("the synthetic local-file read failed before browser verification")
        revision = selected["revision"]
        for source_id, result, horizon, completeness, account, error in (
            ("gmail", "success", now, "complete", "synthetic-google", None),
            ("slack", "explicit_empty", now, "complete", "synthetic-slack", None),
            ("figma", "success", now, "partial", "synthetic-figma", None),
            (
                "local_files",
                "success",
                now - timedelta(days=8),
                "partial",
                None,
                None,
            ),
            ("shopify", "success", now, "complete", "synthetic-shopify", None),
            ("shopify", "failure", None, None, "synthetic-shopify", "auth_expired"),
        ):
            observed = vault.record_source_observation(
                expected_revision=revision,
                source_id=source_id,
                actor_ref="synthetic-browser-proof",
                result=result,
                covered_through=horizon.isoformat().replace("+00:00", "Z") if horizon else None,
                completeness=completeness,
                account_binding=account,
                tool_binding=(
                    LOCAL_FILE_READER_TOOL if source_id == "local_files" else "synthetic-read-tool"
                ),
                error_code=error,
            )
            revision = observed["revision"]
        expanded_local_root = Path(raw) / "second-selected-local-files"
        expanded_local_root.mkdir()
        vault.grant_local_file_root(expanded_local_root)
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
