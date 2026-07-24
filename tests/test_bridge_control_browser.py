from __future__ import annotations

import os
import threading
from importlib.resources import as_file, files
from pathlib import Path

import pytest

from continuity_kernel.bridge import BridgeHTTPServer
from continuity_kernel.operations import OperationLedger
from continuity_kernel.vault import Vault

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="secure directory-pinned control storage is POSIX-only foundation"
)

ACCESS_TOKEN = "e" * 48
INSTANCE_ID = "f" * 32


def test_browser_queues_correction_and_renders_durable_disposition(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault")
    vault.initialize(name="Browser control proof")
    static_resource = files("continuity_kernel") / "resources/bridge"
    console_errors: list[str] = []
    http_errors: list[str] = []

    with as_file(static_resource) as static_root:
        server = BridgeHTTPServer(
            ("127.0.0.1", 0),
            vault,
            static_root,
            access_token=ACCESS_TOKEN,
            instance_id=INSTANCE_ID,
            integration_provider=lambda: {
                "available": True,
                "instructions_installed": True,
                "plugin_installed": True,
                "ready": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(
                    color_scheme="dark",
                    reduced_motion="reduce",
                    viewport={"height": 720, "width": 960},
                )
                page.on(
                    "console",
                    lambda message: (
                        console_errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on(
                    "response",
                    lambda response: (
                        http_errors.append(f"{response.status} {response.url}")
                        if response.status >= 400
                        else None
                    ),
                )
                page.goto(f"{base}/#token={ACCESS_TOKEN}", wait_until="networkidle")
                page.locator('[data-view="system"]').click()
                review_link = page.get_by_role("link", name="Review in Codex")
                review_link.wait_for(timeout=5_000)
                assert review_link.get_attribute("href").startswith("codex://new?")
                page.get_by_text("Copy prompt instead", exact=True).click()
                assert page.get_by_role("button", name="Copy prompt").is_visible()
                assert (
                    page.locator(".control-prompt-fallback code")
                    .filter(has_text="This is review only")
                    .is_visible()
                )
                page.get_by_label("What should GSV correct?").fill("Friday evening is protected.")

                ledger = OperationLedger(vault.root)
                before_refresh = ledger.snapshot()
                ledger.queue.append(
                    kind="correction",
                    subject="mind:user-correction",
                    choice="Background correction arrived first.",
                    expected_revision=before_refresh.queue_revision,
                )

                # The production ten-second poll rebuilds the System view when the queue changes.
                page.get_by_text("Background correction arrived first.").wait_for(timeout=12_000)
                assert (
                    page.get_by_label("What should GSV correct?").input_value()
                    == "Friday evening is protected."
                )

                before_race = ledger.snapshot()
                ledger.queue.append(
                    kind="correction",
                    subject="mind:user-correction",
                    choice="A second correction won the CAS race.",
                    expected_revision=before_race.queue_revision,
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text("The queue changed while you were editing.", exact=False).wait_for(
                    timeout=5_000
                )
                assert (
                    page.get_by_label("What should GSV correct?").input_value()
                    == "Friday evening is protected."
                )
                assert page.get_by_text("Background correction arrived first.").is_visible()
                assert page.get_by_text("A second correction won the CAS race.").is_visible()

                page.evaluate(
                    """
                    window.__gsvOriginalFetch = window.fetch;
                    window.fetch = async (...args) => {
                      const request = String(args[0]);
                      if (request.endsWith('/api/v1/control')) {
                        await new Promise((resolve) => window.setTimeout(resolve, 700));
                        return new Response(
                          JSON.stringify({error: 'control queue changed; reload'}),
                          {status: 409, headers: {'Content-Type': 'application/json'}},
                        );
                      }
                      if (request.endsWith('/api/v1/snapshot')) {
                        return new Response(
                          JSON.stringify({error: 'snapshot temporarily unavailable'}),
                          {status: 503, headers: {'Content-Type': 'application/json'}},
                        );
                      }
                      return window.__gsvOriginalFetch(...args);
                    };
                    void 0;
                    """
                )
                page.get_by_label("What should GSV correct?").fill("Original submitted draft.")
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_label("What should GSV correct?").fill(
                    "Newer text typed while save was pending."
                )
                page.get_by_text("The queue changed while you were editing.", exact=False).wait_for(
                    timeout=5_000
                )
                assert (
                    page.get_by_label("What should GSV correct?").input_value()
                    == "Newer text typed while save was pending."
                )
                assert page.get_by_text(
                    "The queue changed while you were editing. Your current draft is still "
                    "here; refresh the queue, then retry.",
                    exact=True,
                ).is_visible()
                assert not page.get_by_text("review the refreshed queue", exact=False).is_visible()
                page.evaluate("window.fetch = window.__gsvOriginalFetch; void 0;")

                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text("Newer text typed while save was pending.").wait_for(timeout=5_000)
                page.get_by_text("Waiting", exact=True).first.wait_for(timeout=5_000)

                pending = ledger.snapshot()
                assert len(pending.pending) == 3
                user_event = next(
                    event
                    for event in pending.pending
                    if event.choice == "Newer text typed while save was pending."
                )
                ledger.decide(
                    event_id=user_event.event_id,
                    decision="accepted",
                    actor_ref="core:doctor",
                    reason_code="supported-correction",
                    expected_queue_revision=pending.queue_revision,
                    expected_disposition_revision=pending.disposition_revision,
                    result_ref="control:acknowledged",
                )

                page.reload(wait_until="networkidle")
                page.locator('[data-view="system"]').click()
                page.get_by_text("Acknowledged, not applied", exact=True).wait_for(timeout=5_000)
                assert page.get_by_text("supported-correction", exact=True).is_visible()
                assert page.get_by_text("Newer text typed while save was pending.").is_visible()

                page.evaluate(
                    """
                    window.__gsvOriginalFetch = window.fetch;
                    window.fetch = async (...args) => {
                      const request = String(args[0]);
                      if (request.endsWith('/api/v1/snapshot')) {
                        return new Response(
                          JSON.stringify({error: 'snapshot temporarily unavailable'}),
                          {status: 503, headers: {'Content-Type': 'application/json'}},
                        );
                      }
                      return window.__gsvOriginalFetch(...args);
                    };
                    void 0;
                    """
                )
                page.get_by_label("What should GSV correct?").fill(
                    "This write survives a failed view refresh."
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "Queued the correction, but the view could not refresh. Refresh before "
                    "entering another correction.",
                    exact=True,
                ).wait_for(timeout=5_000)
                assert page.get_by_label("What should GSV correct?").input_value() == ""
                assert any(
                    event.choice == "This write survives a failed view refresh."
                    for event in ledger.snapshot().pending
                )
                assert not page.get_by_text(
                    "could not confirm whether the correction was saved", exact=False
                ).is_visible()
                page.evaluate("window.fetch = window.__gsvOriginalFetch; void 0;")
                page.reload(wait_until="networkidle")
                page.locator('[data-view="system"]').click()

                page.route(
                    "**/api/v1/control",
                    lambda route: route.fulfill(
                        body='{"error":"durability could not be confirmed"}',
                        content_type="application/json",
                        status=503,
                    ),
                    times=1,
                )
                page.get_by_label("What should GSV correct?").fill(
                    "Keep this text until storage is confirmed."
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "GSV could not confirm whether the correction was saved.", exact=False
                ).wait_for(timeout=5_000)
                assert (
                    page.get_by_label("What should GSV correct?").input_value()
                    == "Keep this text until storage is confirmed."
                )
                assert (
                    not page.locator(".control-form")
                    .get_by_text("Your local files were not changed.", exact=False)
                    .is_visible()
                )

                page.route(
                    "**/api/v1/control",
                    lambda route: route.fulfill(
                        body='{"error":"control queue reached its bounded event limit"}',
                        content_type="application/json",
                        status=400,
                    ),
                    times=1,
                )
                page.get_by_role("button", name="Queue correction").click()
                page.get_by_text(
                    "The correction was not queued: control queue reached its bounded event limit",
                    exact=True,
                ).wait_for(timeout=5_000)
                assert (
                    page.get_by_label("What should GSV correct?").input_value()
                    == "Keep this text until storage is confirmed."
                )

                closed = ledger.snapshot()
                for pending_event in closed.pending:
                    closed = ledger.decide(
                        event_id=pending_event.event_id,
                        decision="rejected",
                        actor_ref="core:doctor",
                        reason_code="superseded-test-race",
                        expected_queue_revision=closed.queue_revision,
                        expected_disposition_revision=closed.disposition_revision,
                    )
                ledger.archive_closed(
                    expected_queue_revision=closed.queue_revision,
                    expected_disposition_revision=closed.disposition_revision,
                )
                page.reload(wait_until="networkidle")
                page.locator('[data-view="system"]').click()
                page.get_by_text("Previous queue · history only", exact=True).wait_for(
                    timeout=5_000
                )
                assert page.get_by_text("Acknowledged, not applied", exact=True).is_visible()
                assert page.get_by_text("Newer text typed while save was pending.").is_visible()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    assert any("409" in message for message in console_errors)
    unexpected = [
        message
        for message in console_errors
        if "409" not in message and "503" not in message and "400" not in message
    ]
    assert unexpected == [], http_errors
