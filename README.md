# GSV

## Turn Codex into the AI that manages your life.

GSV 1.0 is being built to bring your email, messages, calendars, files, GitHub
work, and Codex activity into one current picture of your life. It is meant to
keep track of people, projects, promises, decisions, waiting-fors, and what
needs attention next. You keep talking and working in Codex. The local Bridge
shows durable GSV records and the exact work Codex can continue.

Your canonical GSV records are readable files on your computer. There is no GSV
account, hosted database, telemetry service, or second chat app.

> **This repository contains the public continuity kernel, not the 1.0
> replacement product.** The current foundation branch implements one bounded
> Bridge-intent round trip for local verification. Its onboarding and source
> models, Pulse admission, and scheduler planners are foundation code, not
> installed consumer capabilities. Every Plus-account, connector, clean-machine,
> signing, platform, and soak gate remains open. [See the exact release
> gates](docs/release-gates.md).

## What GSV 1.0 is for

The 1.0 product is intended to give Codex a durable model of the parts of your
life you choose to share:

- **What you care about:** current direction, priorities, constraints, and
  routines.
- **What is happening:** active work, personal obligations, decisions, people,
  and longer-running situations.
- **What changed:** bounded evidence from connected sources, including honest
  gaps when a source is stale or unavailable.
- **What happens next:** the next actor, exact continuation, approval boundary,
  and one active Codex hand for each real outcome.

Bridge follows one simple contract: **read here, talk in Codex**. In this
foundation slice, its authenticated control route can queue an explicit setup
choice, approval, correction, or undo request. A separate CLI or MCP process
can accept or reject that queued intent durably. Disposition does not execute
the request, authorize an external action, change semantic records, or mark
work complete. This control lane currently requires secure directory-pinned
storage available on macOS and Linux; Windows fails closed and remains an open
cross-platform gate while canonical Bridge reads stay available.

## Why cross-source context matters

In the private resident workflow that informs this product, an invoice
follow-up looked overdue because documents mentioned in WhatsApp were missing
from the local package. Teams showed that those documents had already been
sent. The resident GSV cancelled the duplicate chase and changed the next
action to reconcile the received files. This is product evidence from the
private workflow, not proof that the public build currently connects WhatsApp
or Teams.

A mailbox-only assistant would have kept chasing. A chat summary would have
lost the correction later. GSV carried the decision, evidence references, and
new next action into the exact Codex task doing the work.

The same model applies to ordinary work:

| Situation | What GSV keeps current |
| --- | --- |
| A dentist appointment is discussed by email and appears on the calendar. | The commitment, date, open preparation, and whether anything still needs a reply. |
| A recruiter asks for a CV while another thread contains role constraints. | The person, role, latest approved document, constraints, and exact reply task. |
| A family commitment conflicts with a project deadline. | Both commitments, the constraint, the decision, and who needs to act next. |
| A release fails in Codex and continues in a fresh task. | The real outcome, failure evidence, decision already made, and exact next check. |

## Why GSV 1.0 aims to replace OpenClaw or Hermes

[OpenClaw](https://docs.openclaw.ai/) and
[Hermes Agent](https://hermes-agent.nousresearch.com/docs/) are capable local,
proactive agent systems. They support memory, schedules, messaging, tools, and
Codex execution. GSV 1.0 is designed for a different daily setup: native Codex
stays the conversation and execution surface, while GSV maintains an explicit
whole-life operating model around it.

That model includes Direction, Portfolio, commitments, canonical people and
projects, ongoing WorkThreads, source freshness, the next actor, and exact
Codex-task continuity. Messages and files become bounded evidence for authored
claims and references that a later session can inspect directly.

The replacement case is for people who want Codex to be the app they live in
and one local Mind to manage work and life across sources. Until the release
ledger passes, the public build remains a continuity kernel rather than a
proven OpenClaw or Hermes replacement.

## What is proven, and what is only foundation code

- Durable local Tasks, Entities, WorkThreads, Mind, and current orientation.
- Compare-and-swap writes, crash recovery, verified backup/restore, and
  reversible Codex integration.
- A private loopback Bridge with exact continuation links.

This foundation branch adds an authenticated, compare-and-swap Bridge control
queue plus durable accept/reject dispositions readable from a fresh CLI or MCP
process on macOS and Linux. Windows has no secure pinned-store backend in this
foundation and fails the control lane closed. That bounded loop is not a
semantic mutation or action executor.

The branch also contains context-first onboarding state, host readiness
receipts, source-attestation validators, Pulse admission, and scheduler-canary
planners. Those modules are inert foundation contracts until they are wired to
supported public interfaces and pass the installed-path gates.

The code does not claim consumer onboarding, live connectors, unattended
Plus-account operation, signed native installs, or cross-platform daily-use
proof until those paths have passed their release gates.

## On your computer

GSV stores its records in `~/GSV` by default. They are normal Markdown files,
so Git, `grep`, backup tools, and optional local search tools can inspect them.
The dashboard listens only on your computer. Its read views cannot change
semantic records. The one control endpoint can append a CAS-protected setup
choice, approval, correction, or undo request for later disposition. Accepting
or rejecting that receipt acknowledges the intent; it does not apply it.
Browser activity never decides what a task means or marks work complete.

GSV has no hosted service, database server, embedding model, API key, cloud
sync, or autonomous background worker. These limits describe GSV itself;
Codex has separate processing and account boundaries.

## Release status

A predecessor macOS Apple Silicon `0.2.0` continuity candidate passed local
installation and recovery checks. That evidence does not promote this branch
or the 1.0 product. The Culture-Grade foundation has not passed Gate 0, a clean
Plus-account install, live connector proof, scheduled autonomy, macOS/Windows
signing, the cross-platform matrix, the 72-hour scheduler soak, the 30-day
account soak, or the six-person beta. No public `0.2.0` build exists yet, and
the published `0.1.0` release does not include the dashboard.

## Install

Once a matching `0.2.0` build is published, give Codex this instruction:

> Open https://github.com/olivier-motium/gsv and read `AGENT_INSTALL.md`.
> Install the verified release for this computer without replacing existing
> GSV or Codex data. Open GSV and show me what is open, what is waiting, and
> what is assigned to me. If no matching verified release exists, stop and
> tell me.

The agent-led installer selects the platform build, verifies its SHA-256,
preserves unrelated Codex configuration, opens the local dashboard, and runs
health and recovery checks. A consumer install does not need Python, `uv`, or
`make`.

The shell and PowerShell installers are documented in
[Installation](docs/installation.md). Building from source is a maintainer
path while `0.2.0` remains unreleased.

## See the recovery proof

Run GSV with no arguments to open the dashboard:

```bash
gsv
```

Run the isolated proof without reading your real GSV files:

```bash
gsv demo
```

The demo verifies that:

1. one process saves a new task update and then stops;
2. a fresh process reads the exact saved update;
3. an older copy cannot overwrite newer work;
4. an interrupted save can be repaired;
5. a verified backup restores the same files and saved records.

Unavailable email coverage is labeled synthetic. The demo does not claim that
a live connector was disabled or tested.

## Everyday commands

```bash
gsv                  # Open the local dashboard
gsv doctor           # Check files, Codex integration, and local health
gsv status           # Show a compact status summary
gsv bridge status    # Check the dashboard process
gsv backup create    # Create a verified backup without overwriting one
gsv bridge stop      # Stop the local dashboard
```

Backup creation refuses unsupported files, unsafe destinations, or a name that
already exists. Verification and restore still work if the current
configuration is unavailable:

```bash
gsv backup verify /path/to/gsv-backup.zip
gsv backup restore /path/to/gsv-backup.zip /path/to/restored-gsv
```

Restore verifies files before publishing them and never silently switches your
active GSV folder. See [Installation](docs/installation.md) for the complete
backup and restore contract.

## Remove it

Remove only the Codex integration while keeping the executable:

```bash
gsv codex uninstall
```

Release uninstallers remove GSV-owned integration and executable files while
preserving your GSV folder, configuration, backups, and unrelated Codex data:

```bash
sh scripts/uninstall.sh
```

```powershell
.\scripts\uninstall.ps1
```

If ownership or provider cleanup cannot be verified, removal stops and prints
the exact recovery command instead of guessing.

## Technical documentation

- [Product contract](docs/product-contract.md)
- [Installation and recovery](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Trust model](docs/trust-model.md)
- [Codex integration evidence](docs/codex-integration.md)
- [Onboarding target and current gaps](docs/onboarding.md)
- [1.0 replacement release gates](docs/release-gates.md)

## Develop

Source development requires Python 3.11+ and `uv`:

```bash
uv sync --extra dev --extra release --extra browser-test
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=continuity_kernel --cov-report=term-missing
uv run playwright install chromium
uv run python scripts/verify_bridge_browser.py
uv run python scripts/privacy_check.py .
```

`make` is an optional developer convenience. GSV is licensed under Apache-2.0.
Contributions use the Developer Certificate of Origin described in
[CONTRIBUTING.md](CONTRIBUTING.md).
