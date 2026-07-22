# Product visuals

The screenshots and `gsv-handoff.gif` contain only the synthetic Atlas demo
produced by `gsv demo`:

- `bridge-overview.png`: healthy Now view and static README fallback
- `bridge-inspector.png`: desktop commitment inspector
- `bridge-mobile.png`: healthy mobile Now view after real menu navigation
- `bridge-mobile-inspector.png`: full-width mobile inspector
- `bridge-stale.png`: last successful snapshot after an induced refresh failure
- `bridge-unavailable.png`: first load with an induced unavailable session
- `gsv-handoff.gif`: Now to Commitments to inspector

Regenerate them with:

```bash
uv sync --extra dev --extra visual
uv run python scripts/render_bridge_demo.py
```

The script drives the real bearer-gated loopback Bridge in a headless browser.
It verifies the synthetic task by role and exact rendered title, exercises the
mobile rail and inspector, checks hidden-state controls, forces stale and
unavailable transport states, rejects uncaught JavaScript or unexpected console
errors, checks sticky-header geometry and viewport overflow, rejects blank
captures, and never reads the configured user vault.
