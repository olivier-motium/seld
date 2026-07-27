# GSV

## Turn Codex into the AI that manages your life.

GSV 1.0 is being built to bring your email, messages, calendars, files, GitHub
work, and Codex activity into one current picture of your life. It is meant to
keep track of people, projects, promises, decisions, waiting-fors, and what
needs attention next. Codex remains the reasoning engine. The local Bridge
shows durable GSV records and the exact work Codex can continue. This foundation
also contains a source-tested, capability-gated prepared Portfolio review driven
by your explicit answers; its automatic same-hand transport is not enabled for
consumer installs until the installed Codex gate passes.

Your canonical GSV records are readable files on your computer. There is no GSV
account, hosted database, telemetry service, or second chat app.

> **This repository contains the public continuity kernel, not the 1.0
> replacement product.** The current foundation branch implements one bounded
> Bridge-intent round trip and packages context-first onboarding plus one
> resident AI Pulse contract. Pulse is a dedicated Codex task awakened by an
> app-native heartbeat: the model reads bounded sources, judges what matters,
> and authors canonical changes. The implemented kernel validates the
> structural Pulse marker, CAS, bounded content-free receipts, and no-replay;
> exact app-task correlation and connector-write privacy screening remain
> unproven gates. The older onboarding,
> source-readiness, Pulse-admission, and scheduler-planning classes remain inert
> foundation code. Every Plus-account, connector, clean-machine, signing,
> platform, and soak gate remains open. [See the exact release
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

Bridge follows one simple authority contract: **the browser presents and
captures; the agent judges; native records remain truth**. Its authenticated
control route can queue an explicit guided-review answer, setup choice,
approval, correction, or undo request. A CLI or MCP agent reads that receipt,
authors any justified Task, WorkThread, and complete Portfolio changes through
fresh compare-and-swap writes, reads them back, and only then accepts or rejects
the receipt. Disposition itself does not execute the request, authorize an
external action, change semantic records, or mark work complete. This control
lane currently requires secure directory-pinned storage available on macOS and
Linux; Windows fails closed while canonical Bridge reads stay available.

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

- Durable local Tasks, Entities, WorkThreads, Mind, current orientation, and an
  authored complete Portfolio with exact Task and optional WorkThread anchors.
- Compare-and-swap writes, crash recovery, verified backup/restore, and
  reversible Codex integration.
- A private loopback Bridge with exact continuation links.

This foundation branch adds a guided all-open Portfolio review over that same
authenticated, compare-and-swap Bridge control queue. The AI normally prepares
3-10 consequential rows and never more than 25; each has an authored question,
recommendation, reasoning, and choices, while freeform answers remain available.
The agent—not browser code—applies each answered row independently through Task,
WorkThread, and Portfolio CAS commands. Checked means only reviewed in that
finite session, never completed. Durable
accept/reject dispositions remain readable from a fresh CLI or MCP process on
macOS and Linux. Windows has no secure pinned-store backend in this foundation
and fails the control lane closed. Automatic same-hand delivery is off by
default and requires the documented source-canary switch; without installed
provider proof, this is implemented foundation behavior rather than a current
consumer-install claim.

The packaged `$gsv-onboard` and `$gsv-pulse` skills now describe the same
AI-native operating loop used to shape the public product: accepted context is
written through document CAS, selected connectors are read by the model, and
one exact resident Pulse task owns each bounded wake. The structural Pulse task
is hidden from the life Portfolio and guided review. Source-tree tests prove
that contract and packaging; they do not prove that a clean installed Codex
account can register, wake, or use a live connector.

The branch also contains typed onboarding sessions, host receipts,
source-attestation validators, deterministic Pulse admission, and OS-scheduler
planners. They are deliberately not wired into the resident intelligence. They
remain inert foundation contracts until a narrow safety need and installed
evidence justify a public surface.

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
sync, or deterministic semantic worker. The target resident loop relies on a
Codex app heartbeat waking the AI task; it is not an embedded GSV daemon. These
limits describe GSV itself; Codex has separate processing and account
boundaries.

## Release status

A predecessor macOS Apple Silicon `0.2.0` continuity candidate passed local
installation and recovery checks. That evidence does not promote this branch
or the 1.0 product. The Culture-Grade foundation has not passed Gate 0, a clean
Plus-account install, live connector proof, scheduled autonomy, macOS/Windows
signing, the cross-platform matrix, the 72-hour resident-heartbeat soak, the 30-day
account soak, or the six-person beta. No public `0.2.0` build exists yet, and
the published `0.1.0` release does not include the dashboard.

## Install

Once a matching `0.2.0` build is published, give Codex this instruction:

> Open https://github.com/olivier-motium/gsv and read `AGENT_INSTALL.md`.
> Install the verified release for this computer without replacing existing
> GSV or Codex data. Open GSV, then help me restart Codex and begin
> `$gsv-onboard` in one fresh task. If no matching verified release exists,
> stop and tell me.

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
- [Resident AI Pulse contract](docs/pulse.md)
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
