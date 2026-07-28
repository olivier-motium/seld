# Seld

## Seld, your AI chief of staff.

Your life is split across email, WhatsApp, Slack, Teams, calendars, files,
GitHub, and a growing list of Codex tasks. Each tool sees one slice. Seld gives
Codex a durable, local model of the whole situation: the people who matter,
projects in motion, promises you made, decisions already taken, and what needs
you next.

Ask for the **Rundown** and Seld walks through what is still open, one item at a
time, with the next move already written. You can act, change the plan, defer
it, close it, or tell Seld its recommendation is wrong. The decision stays with
the outcome instead of disappearing into the conversation.

Bridge is the home screen. Pulse gives the system recurring awareness. Codex
does the reasoning and the work. Seld keeps the memory and operating state in
readable files on your computer, so a task can end without taking its context
with it. The 1.0 release target runs on a ChatGPT Plus-class plan without a
separate API key. There is no Seld account, hosted database, telemetry service,
or second chat app.

## How Seld manages a life

The 1.0 product is designed around one repeating loop:

1. **Understand your world.** Onboarding starts with your goals, obligations,
   relationships, boundaries, and preferred way of working.
2. **Read the places where life happens.** The AI uses the email, messaging,
   calendar, file, GitHub, and screen sources you explicitly choose.
3. **Keep one current model.** Seld maintains people, projects, commitments,
   decisions, unresolved situations, source coverage, and the next actor.
4. **Notice what changed.** One resident Pulse task revisits bounded evidence,
   connects it to the existing model, and decides whether anything deserves
   attention.
5. **Bring you the decision.** Bridge shows the small set of things that need
   you. Your answer continues in the exact Codex task that owns the outcome.

This is useful long after a message has been read. Seld remembers why a task
exists, which evidence changed it, what has already been decided, and where the
work should resume. Bridge should be able to answer five practical questions at
any time: What needs me? Why now? What changed? Who acts next? Where does the
work continue?

## One real cross-source result

In the private resident system that informs Seld, an invoice follow-up appeared
overdue because documents discussed in WhatsApp were missing from the local
package. Teams showed that the documents had already been sent. Seld cancelled
the duplicate chase, changed the next action, and carried the correction into
the exact Codex task reconciling the files.

The value came from connecting the sources and preserving the decision. The
mailbox alone had an incomplete story, and a chat summary would not have become
durable operating state. This is evidence from the private resident workflow,
not proof that the current public build connects WhatsApp or Teams.

The same model applies to ordinary work:

| Situation | What stays current |
| --- | --- |
| A dentist appointment is discussed by email and appears on the calendar. | The commitment, date, preparation, and whether anyone still needs a reply. |
| A recruiter asks for a CV while another thread contains role constraints. | The person, role, approved document, constraints, and exact reply task. |
| A family commitment conflicts with a project deadline. | Both commitments, the decision, and who needs to act next. |
| A release fails in Codex and continues in a fresh task. | The outcome, failure evidence, prior decision, and exact next check. |

## What Seld keeps between Codex tasks

- **Direction:** what you are trying to change, what matters now, and the
  boundaries the system must respect.
- **A complete Portfolio:** every open outcome, its current state, priority,
  next actor, and next action.
- **People and projects:** canonical records that connect conversations,
  documents, decisions, and ongoing work without copying whole inboxes.
- **Living situations:** longer-running threads that preserve why something is
  happening and how the evidence has changed.
- **Current awareness:** what was observed, which sources were covered, what is
  uncertain, and when the view needs refreshing.
- **Exact continuity:** one active Codex hand for each real outcome, with a
  safe continuation when that hand finishes, crashes, or is replaced.

## Replacing OpenClaw or Hermes?

[OpenClaw](https://docs.openclaw.ai/) and
[Hermes Agent](https://hermes-agent.nousresearch.com/docs/) are capable local,
proactive agent systems. They support memory, schedules, messaging, tools, and
Codex execution. Seld is for people who want Codex itself to become the resident
AI they use to run work and life.

Seld's distinction is its operating model. It keeps an explicit Direction,
complete Portfolio, commitments, canonical people and projects, longer-running
situations, source freshness, the next actor, and exact Codex-task continuity.
Messages and files supply bounded evidence for that model.

You keep talking and working in Codex. Bridge gives you the current view of
your life, and Pulse keeps that view from going stale. The records stay on your
computer and remain inspectable without Seld running.

## The intelligence stays in the AI

Seld does not turn an old email, changed file, or due date into a task through a
fixed rule. Codex reads the bounded evidence in context and decides what it
means: whether two records describe the same person, whether a commitment has
changed, whether something deserves attention, and what the next action should
be.

Deterministic code has a smaller job. It authenticates the local Bridge,
protects revisions, bounds inputs, records delivery facts, prevents replay, and
supports recovery. It cannot decide priority, completion, relationships,
memory worthiness, or the meaning of a message.

## Current public status

> **This repository contains the public continuity kernel, not the finished
> 1.0 replacement product.** It implements durable local records, the Bridge
> intent and review loop, context-first onboarding instructions, and the
> resident AI Pulse contract. Clean-account connector use, natural app-native
> Pulse wakes, signing, supported-platform installation, and daily-use soaks
> remain open. [See the exact release gates](docs/release-gates.md).

The replacement claim ships only when the installed product proves those
paths. Until then, the public build is foundation code for the product
described above.

**Name and compatibility:** Seld is the product name. This foundation keeps the
existing `gsv` command, package, plugin and skill identifiers, `GSV_*`
environment variables, and `~/GSV` default records folder. The repository now
lives at `olivier-motium/seld`; the rename does not change the existing
technical interfaces.

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

Seld stores its records in `~/GSV` by default. They are normal Markdown files,
so Git, `grep`, backup tools, and optional local search tools can inspect them.
The dashboard listens only on your computer. Its read views cannot change
semantic records. The one control endpoint can append a CAS-protected setup
choice, approval, correction, or undo request for later disposition. Accepting
or rejecting that receipt acknowledges the intent; it does not apply it.
Browser activity never decides what a task means or marks work complete.

Seld has no hosted service, database server, embedding model, API key, cloud
sync, or deterministic semantic worker. The target resident loop relies on a
Codex app heartbeat waking the AI task; it is not an embedded Seld daemon. These
limits describe Seld itself; Codex has separate processing and account
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

> Open https://github.com/olivier-motium/seld and read `AGENT_INSTALL.md`.
> Install the verified release for this computer without replacing existing
> Seld or Codex data. Open Seld, then help me restart Codex and begin
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

Run Seld with no arguments to open the dashboard:

```bash
gsv
```

Run the isolated proof without reading your real Seld files:

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
active Seld records folder. See [Installation](docs/installation.md) for the complete
backup and restore contract.

## Remove it

Remove only the Codex integration while keeping the executable:

```bash
gsv codex uninstall
```

Release uninstallers remove `gsv`-owned integration and executable files while
preserving your Seld records folder, configuration, backups, and unrelated Codex data:

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

`make` is an optional developer convenience. Seld is licensed under Apache-2.0.
Contributions use the Developer Certificate of Origin described in
[CONTRIBUTING.md](CONTRIBUTING.md).
