#!/usr/bin/env python3
"""Regenerate the privacy-clean Bridge screenshot and handoff GIF."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import threading
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
        "mobile": str(output / "bridge-mobile.png"),
        "mobile_inspector": str(output / "bridge-mobile-inspector.png"),
        "screenshot": str(output / "bridge-overview.png"),
        "stale": str(output / "bridge-stale.png"),
        "synthetic": True,
        "unavailable": str(output / "bridge-unavailable.png"),
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
    return {"instructions_installed": True, "plugin_installed": True}


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
            _assert_visibility(page, "#open-codex", visible=True)
            _assert_visibility(page, "#rail-backdrop", visible=False)
            _assert_visibility(page, "#inspector-backdrop", visible=False)
            _assert_visibility(page, "#inspector-foot", visible=False)
            _assert_visibility(page, "#connection-orb", visible=False)
            _assert_visibility(page, "#local-status .status-dot", visible=True)
            _assert_orb_canvas(page, ".continuity-orb")
            _assert_reduced_orb_static(page, ".continuity-orb")
            _assert_no_browser_errors(console_errors, page_errors)
            _assert_viewport(page)

            frame_one = root / "01-now.png"
            page.screenshot(path=str(frame_one), animations="disabled")
            page.screenshot(path=str(output / "bridge-overview.png"), animations="disabled")

            page.locator("button[data-view='commitments']").click()
            page.locator(".commitment-grid").wait_for()
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
        output / "bridge-mobile.png",
        output / "bridge-mobile-inspector.png",
        output / "bridge-stale.png",
        output / "bridge-unavailable.png",
    ):
        _assert_nonblank(image)
    return [frame_one, frame_two, frame_three]


def _launch(browser_type: Any) -> Browser:
    try:
        return cast(Browser, browser_type.launch(channel="chrome", headless=True))
    except Error:
        return cast(Browser, browser_type.launch(headless=True))


def _assert_viewport(page: Any) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    if overflow:
        raise RuntimeError("Bridge content overflows the current viewport")


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


def _assert_visibility(page: Any, selector: str, *, visible: bool) -> None:
    actual = page.locator(selector).is_visible()
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
