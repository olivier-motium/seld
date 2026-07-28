# Seld

## Your chief of staff is already caught up.

**It proposes. You decide.**

While you are away, Seld checks the sources you choose to connect, holds fresh
changes against what it already knows about your life, and gets the few
decisions that actually need you ready. Most changes are noise. You do not hear
about those.

The sources you choose can include WhatsApp, Slack, Outlook, Gmail and Google
Calendar, Teams, GitHub, local files, and activity in the ChatGPT app. You pick
the places that matter to you. Seld does not need access to everything to start
being useful.

Seld is an open-source, ambient AI chief of staff that lives on your Mac. It
keeps one current record of your people, projects, commitments, decisions, and
next moves. Pulse works quietly in the background. When something changes, it
reasons over the new evidence and that durable context before deciding whether
anything deserves your attention.

Nothing is sent, booked, paid, archived, or deleted without you. Seld prepares
the move. You make it.

[See how it works](#while-you-were-away) · [Install Seld](#install) ·
[Read the technical status](#technical-status)

## While you were away

Imagine two hours away from your laptop. Three things change:

- A document lands in a work thread. It is the document an invoice has been
  waiting on, but it arrived somewhere other than the thread you were watching.
- A calendar event moves. The review now happens before the work it is meant to
  review.
- A reply still has not arrived, but nothing about that situation has changed
  enough to require another interruption.

Pulse wakes in the background. It reads the changes from the sources you
connected, compares them with the record on your Mac, and works out what each
change means for what you already have open. It updates the record. It sends
nothing.

When you return, Seld has three outcomes ready:

| Outcome | What Seld prepared |
| --- | --- |
| **Cancelled** | Do not chase the invoice. The missing documents arrived, so the next move is to reconcile them. |
| **Prepared** | The calendar conflict is real. A short reschedule is drafted and waiting for you to send, rewrite, or ignore. |
| **Quiet** | The unanswered message stays where it was. Nothing changed, so it does not get a line in your day. |

*Illustrative data. The workflow reflects how Seld is designed to connect
changes across sources without acting outward on its own.*

While you were away, Seld kept up.

## The Rundown

Seld brings decisions to you one at a time. Each item says where the situation
stands and the move it would make next. You can keep the recommendation, change
it, close the outcome, or act yourself. Then Seld moves to the next item.

The Rundown is deliberately small. No board to maintain. No growing pile of
notifications. No need to reconstruct why something matters before you can
decide. The work of catching up has already happened.

If new evidence contradicts the current plan, Seld tells you before you act. A
checked item remains open unless you actually close it. Your answer stays with
the outcome instead of disappearing into a conversation.

## How it works

1. **You connect the sources that matter.** Choose the mail, calendars, chat,
   source control, local files, optional screen context, and ChatGPT app
   activity that Seld may read. Where it has no coverage, it says so.
2. **One record lives on your Mac.** Seld keeps what is open, what you decided,
   whose move it is, and the next step in plain Markdown files on your disk.
3. **Pulse runs quietly in the background.** It reads bounded fresh changes and
   reasons over them together with the durable record.
4. **Only what matters surfaces.** A changed plan, a new risk, or a decision
   that now needs you becomes a Rundown item. Everything else stays quiet.
5. **You decide.** Keep, change, close, or act. Nothing leaves your machine
   unless you send it.

That is the loop: connected sources, durable context, ambient reasoning, a few
prepared decisions, and you in control.

## What stays current

Seld tracks situations, not just facts about you.

- **Direction:** what you are trying to change, what matters now, and the
  boundaries the system must respect.
- **Portfolio:** every open outcome, its priority, current state, next actor,
  and next action.
- **People and projects:** canonical records that connect conversations,
  documents, decisions, and ongoing work without copying whole accounts into a
  new service.
- **Living situations:** longer-running threads that preserve why something is
  happening and how the evidence changed.
- **Source awareness:** what was observed, which sources were covered, what is
  uncertain, and when the view needs refreshing.
- **Exact continuity:** the execution task that owns each real outcome and a
  safe continuation when that task finishes, crashes, or is replaced.

That lets Seld answer five practical questions at any time: What needs me? Why
now? What changed? Who acts next? Where does the work continue?

## A real cross-source result

The private resident system that informed Seld once showed an invoice follow-up
as overdue because documents discussed in WhatsApp were missing from the local
package. Teams showed that the documents had already been sent. The system
cancelled the duplicate chase, changed the next action, and carried that
correction into the exact task reconciling the files.

No individual source held the whole story. The value came from connecting them,
understanding what the combined evidence meant, and preserving the corrected
next move.

The same pattern applies to ordinary life:

| Situation | What Seld keeps current |
| --- | --- |
| A dentist appointment is discussed by email and appears on the calendar. | The commitment, date, preparation, and whether anyone still needs a reply. |
| A recruiter asks for a CV while another thread contains role constraints. | The person, role, approved document, constraints, and exact reply task. |
| A family commitment conflicts with a project deadline. | Both commitments, the decision, and who needs to act next. |
| A software release fails overnight. | The outcome, failure evidence, prior decision, and exact next check. |

This is evidence from the private resident workflow, not a claim that every
connector is proven in the current public build. The exact public evidence is
listed in [Technical status](#technical-status).

## Trust and control

The honest version of an AI that reads your life is a short list of exactly
what it touches.

- **A folder you can open.** Your record is plain Markdown on your own disk,
  with an audit log beside it. It remains readable if Seld is uninstalled.
- **Only sources you choose.** Connectors are read-only. Seld does not write,
  reply, archive, or delete in those sources.
- **No outward action on its own.** No message goes out, meeting gets booked,
  or payment gets made without you doing it.
- **No Seld account.** There is no hosted Seld database, subscription, or seat
  to lose.
- **No Seld telemetry.** The model still processes selected material under your
  ChatGPT account and OpenAI's terms, just like other material you use in that
  app.
- **Open source.** The code is Apache-2.0 and the runtime has no third-party
  Python dependencies.

What Seld knows about you stays inspectable. Open the record, correct it, or
remove something you do not want retained.

## Replacing OpenClaw or Hermes?

[OpenClaw](https://docs.openclaw.ai/) and
[Hermes Agent](https://hermes-agent.nousresearch.com/docs/) are capable local,
proactive agent systems. Seld is for people who want the AI they already use in
the ChatGPT desktop app to become a resident chief of staff for work and life.

Seld's distinction is its operating model. It keeps an explicit Direction,
complete Portfolio, commitments, canonical people and projects, longer-running
situations, source freshness, the next actor, and exact execution continuity.
Messages and files supply evidence for that model. They are not the product by
themselves.

Bridge is the current view. Pulse keeps that view current. The model does the
reasoning. Your records stay on your computer and remain inspectable without
Seld running.

## What it runs on

Seld runs through the ChatGPT desktop app on macOS, using the ChatGPT plan you
already have. Windows and Claude support are coming.

There is no Seld subscription or usage meter. Bridge is not a second chat app.
You read the current view there and continue the conversation and work in the
ChatGPT app.

## Install

The consumer release is installed by giving the ChatGPT desktop app this exact
instruction:

> Open https://github.com/olivier-motium/seld and read `AGENT_INSTALL.md`.
> Install the verified release for this Mac without replacing existing Seld or
> ChatGPT app data. Open Seld, restart the ChatGPT app when instructed, and help
> me begin `$gsv-onboard` in one fresh task. If no matching verified release
> exists, stop and tell me.

The installer verifies the release checksum, preserves existing records, opens
Bridge, and runs health and recovery checks. It stops safely if the matching
release is not available. A consumer install does not require Python, `uv`, or
`make`.

Version `0.2.0` is not published yet, so this instruction currently stops at
the release check rather than substituting an unverified source build. Building
from source remains a maintainer path until that release exists.

## Technical status

This repository currently contains the public continuity kernel for Seld, not
the finished consumer release. The confident product description above is the
launch target. Promotion requires installed evidence, not source code or a
passing unit suite.

The public foundation proves:

- durable local Tasks, Entities, WorkThreads, Mind, current orientation, and an
  authored complete Portfolio with exact Task and optional WorkThread anchors;
- compare-and-swap writes, crash recovery, verified backup and restore, and
  reversible ChatGPT app integration;
- a private loopback Bridge with authenticated reads and exact continuation;
- an append-only, compare-and-swap Bridge control queue whose intents can be
  read and accepted or rejected through CLI and MCP surfaces, with durable
  dispositions visible from a fresh process;
- a guided all-open Portfolio review over that same queue, with authored
  questions and options, freeform corrections, stale-state protection, and no
  browser-authored semantic decisions;
- context-first onboarding and resident Pulse skill contracts packaged for the
  AI layer.

The connector, onboarding, Pulse, scheduler, and source-attestation modules
include foundation contracts that are not all promoted to consumer surfaces.
Clean-account connector use, app-native Pulse wakes, signing, supported
installation, and daily-use soaks remain open. Windows has no secure
directory-pinned control-store backend in this foundation and the write lane
fails closed.

The current source recipe set covers Seld itself, ChatGPT app activity, Gmail,
Google Calendar, Outlook mail and calendar, Slack, Teams, GitHub, local files,
optional screen context, and experimental read-only WhatsApp. Apple Messages
and iMessage, Shopify, and Instagram are launch-scope connector gaps. They are
not silently treated as supported sources.

See the [full release-gate and evidence ledger](docs/release-gates.md). The
README does not replace it.

### Product name and compatibility identifiers

Seld is the product and repository name. The current foundation retains the
existing `gsv` executable, Python package, plugin and skill identifiers,
`GSV_*` environment variables, and `~/GSV` default records folder. Those are
compatibility interfaces, not the consumer brand.

## The intelligence stays in the AI

Seld does not turn an old email, changed file, or due date into a task through a
fixed semantic rule. The model reads bounded evidence in context and decides
what it means: whether two records describe the same person, whether a
commitment changed, whether something deserves attention, and what the next
action should be.

Deterministic code has a smaller job. It authenticates Bridge, protects
revisions, bounds inputs, records delivery facts, prevents replay, and supports
recovery. It cannot decide priority, completion, relationships, memory
worthiness, or the meaning of a message.

## Try the local recovery proof

Run Seld with no arguments to open Bridge:

```bash
gsv
```

Run the isolated proof without reading your real Seld files:

```bash
gsv demo
```

The demo proves that a fresh process recovers an exact saved update, an older
copy cannot overwrite newer work, an interrupted save can be repaired, and a
verified backup restores the same records. Its provider data is synthetic. It
does not claim that a live connector was tested.

## Everyday commands

```bash
gsv                  # Open Bridge
gsv doctor           # Check files, integration, and local health
gsv status           # Show a compact status summary
gsv bridge status    # Check the Bridge process
gsv backup create    # Create a verified backup without overwriting one
gsv bridge stop      # Stop Bridge
```

Backup creation refuses unsupported files, unsafe destinations, or a name that
already exists. Verification and restore still work if the current
configuration is unavailable:

```bash
gsv backup verify /path/to/gsv-backup.zip
gsv backup restore /path/to/gsv-backup.zip /path/to/restored-gsv
```

Restore verifies files before publishing them and never silently switches the
active records folder. See [Installation](docs/installation.md) for the full
backup and restore contract.

## Remove it

Remove only the ChatGPT app integration while keeping the executable:

```bash
gsv codex uninstall
```

Release uninstallers remove Seld-owned integration and executable files while
preserving records, configuration, backups, and unrelated app data:

```bash
sh scripts/uninstall.sh
```

If ownership or provider cleanup cannot be verified, removal stops and prints
the exact recovery command instead of guessing.

## Technical documentation

- [Product contract](docs/product-contract.md)
- [Installation and recovery](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Trust model](docs/trust-model.md)
- [ChatGPT app integration evidence](docs/codex-integration.md)
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
