# Seld

## Your AI chief of staff is already caught up.

Seld checks the sources you connect and compares new activity with a local
record of your people, projects, commitments, and decisions. It prepares the
few decisions that need your attention and leaves the rest in the record.

The sources you choose can include WhatsApp, Slack, Outlook, Gmail and Google
Calendar, Teams, GitHub, local files, and activity in the ChatGPT app. You pick
the places that matter to you, and Seld can start being useful without access
to everything.

**It proposes. You decide.**

Seld is an open-source, ambient AI chief of staff for the ChatGPT desktop app on
your Mac. Once you enable Pulse and verify its first wake, a dedicated ChatGPT
task reviews new evidence in the background and updates the same inspectable
record. Bridge shows the resulting brief, open work, saved context, and source
coverage.

Seld waits for your approval before a message is sent, an appointment is
booked, a payment is made, or anything else is changed outside its local
record.

[See how it works](#while-you-were-away) · [Install Seld](#install) ·
[Why Seld is state of the art](#state-of-the-art-design-for-resident-personal-operations)

## State-of-the-art design for resident personal operations

Yes, Seld is state of the art for resident personal operations because it
handles the whole job as one coherent system.

That job is keeping a person's real work and
life current across many sources, understanding what changed in context, and
turning that change into a manageable set of decisions. Seld handles this as one
operating loop. Direction and Portfolio establish what matters; one local
record connects people, projects, commitments, decisions, and ongoing
situations; Pulse interprets fresh source activity in that context; and the
Rundown presents the resulting decisions. Seld carries each outcome and saved
answer safely across ChatGPT tasks and process restarts without duplicating a
decision or action.

The model decides what evidence means. A smaller mechanical layer protects
identity, revisions, privacy, bounded source coverage, recovery, and replay
prevention. The [architecture assessment](docs/state-of-the-art.md) shows why
this integrated whole-life operating model sets the standard for the category.

## While you were away

During two hours away from your laptop, a missing invoice document lands in an
unexpected work thread, a calendar move puts a review before the work it is
meant to review, and an unanswered message remains exactly as it was.

On its next verified wake, Pulse reads those changes from the sources you
connected, compares them with the record on your Mac, and works out what they
mean for the work already in progress. It updates the local record without
sending anything.

The resulting brief contains:

| Outcome | What Seld prepared |
| --- | --- |
| **Cancelled** | Do not chase the invoice. The missing documents arrived, so the next move is to reconcile them. |
| **Prepared** | The calendar conflict is real. A short reschedule is drafted and waiting for you to send, rewrite, or ignore. |
| **Quiet** | The unanswered message stays where it was. Nothing changed, so it does not get a line in your day. |

## What caught up looks like

- An email says an order still has not shipped. Seld links it to the matching
  Shopify order, prepares the reply, and lays out the cancellation, reorder,
  and refund paths. Nothing is changed or sent until you approve the move.
- A large order exceeds one site's capacity. Staffing and a second site make it
  feasible, so Seld proposes an allocation. When a later WhatsApp message
  changes who is available, the proposed allocation changes before you commit.
- Delivery notes generated from Google Sheets keep improving because Seld
  remembers the human corrections. The next batch starts from what people
  actually fixed, not the same generic template.

In each case, Seld uses the relevant records to prepare a consequential action
for approval.

## The Rundown

The Rundown presents one decision at a time, including the current situation
and the proposed next move. You can accept the recommendation, change it, close
the outcome, or handle it yourself without maintaining another board or
notification queue.

If new evidence contradicts the current plan, Seld tells you before you act. A
checked item remains open unless you actually close it. Your answer stays with
the outcome instead of disappearing into a conversation.

## How it works

1. **You connect the sources that matter.** Choose the mail, calendars, chat,
   source control, local files, optional screen context, and ChatGPT app
   activity that Seld may read. Where it has no coverage, it says so.
2. **One record lives on your Mac.** Seld keeps what is open, what you decided,
   whose move it is, and the next step in plain Markdown files on your disk.
3. **Pulse reviews changes in the background.** Once enabled and verified, it
   reads bounded fresh changes and reasons over them with the durable record.
4. **Only what matters surfaces.** A changed plan, a new risk, or a decision
   that now needs you becomes a Rundown item. Everything else stays quiet.
5. **Seld waits for approval.** Pulse can process selected material in ChatGPT,
   while messages, bookings, payments, and provider changes remain under your
   control.

## What stays current

Seld keeps the context around each fact so it can follow an ongoing situation.

- **Direction:** what you are trying to change, what matters now, and the
  boundaries the system must respect.
- **Portfolio:** every open outcome, its priority, current state, next actor,
  and next action.
- **People and projects:** stable records that connect conversations,
  documents, decisions, and ongoing work without copying whole accounts into a
  new service.
- **Living situations:** longer-running threads that preserve why something is
  happening and how the evidence changed.
- **Source awareness:** what was observed, which sources were covered, what is
  uncertain, and when the view needs refreshing.
- **Exact continuity:** the execution task that owns each real outcome and a
  safe continuation when that task finishes, crashes, or is replaced.

Together, these records preserve the current situation, its owner, and the
exact place where the work continues.

## A real cross-source result

The private resident system that informed Seld once showed an invoice follow-up
as overdue because documents discussed in WhatsApp were missing from the local
package. Teams showed that the documents had already been sent. The system
cancelled the duplicate chase, changed the next action, and carried that
correction into the exact task reconciling the files.

Because no individual source held the whole story, Seld connected the evidence,
interpreted it in context, and preserved the corrected next move.

The same pattern applies to ordinary life:

| Situation | What Seld keeps current |
| --- | --- |
| A dentist appointment is discussed by email and appears on the calendar. | The commitment, date, preparation, and whether anyone still needs a reply. |
| A recruiter asks for a CV while another thread contains role constraints. | The person, role, approved document, constraints, and exact reply task. |
| A family commitment conflicts with a project deadline. | Both commitments, the decision, and who needs to act next. |
| A software release fails overnight. | The outcome, failure evidence, prior decision, and exact next check. |

The public Seld project is built from that resident operating model.

## Trust and control

Seld makes its storage, source access, and action boundaries inspectable.

- **Local record.** Your record is plain Markdown on your own disk,
  with an audit log beside it. It remains readable if Seld is uninstalled.
- **Chosen sources.** For source ingestion, Seld calls only read
  operations from the tools you enable. It does not reply, archive, or delete
  in those sources.
- **Local-file access.** Each directory requires a separate host-local grant;
  selecting local files alone grants no filesystem access.
- **Approval required.** No message goes out, meeting gets booked,
  or payment gets made without you doing it.
- **Account model.** There is no hosted Seld database, subscription, or seat
  to lose.
- **Telemetry.** Seld does not collect telemetry. The model processes selected
  material under your ChatGPT account and OpenAI's terms, just like other
  material you use in that app.
- **Open source.** The code is Apache-2.0 and the runtime has no third-party
  Python dependencies.

What Seld knows about you stays inspectable. Open the record, correct it, or
remove something you do not want retained.

## Replacing OpenClaw or Hermes?

[OpenClaw](https://docs.openclaw.ai/) and
[Hermes Agent](https://hermes-agent.nousresearch.com/docs/) are broad agent
platforms built to run tools, schedules, and conversations across many
channels. Seld replaces them for people who want the AI in the ChatGPT desktop
app to manage the moving parts of their work and life.

Seld keeps one current operating model with your Direction, complete Portfolio,
commitments, people and project records, longer-running situations, source
freshness, next actor, and exact execution continuity. Your messages, mail,
calendar, files, and active ChatGPT work can update that model on each verified
Pulse wake, so the system can prepare the right decision without making you
reconstruct the story.

Bridge presents the latest verified view. Pulse refreshes it and records any
coverage gap, while the model reasons over records that remain inspectable on
your computer without Seld running.

## What it runs on

Seld runs through the ChatGPT desktop app on macOS, using your existing ChatGPT
account. Windows and Claude support are coming.

Seld has no subscription or usage meter, and Bridge serves as its reading and
control surface. Conversation and execution remain in the ChatGPT app.

## Install

[Install with ChatGPT](https://seld.ai/#install)

On that page, click **Install with ChatGPT** to open the desktop app with a
ready-to-review prompt. It does not send it. To start manually, open the public
repository and give ChatGPT this instruction:

> Open https://github.com/olivier-motium/seld and read `AGENT_INSTALL.md`.
> Install the current public source distribution on this Mac without replacing
> existing Seld or ChatGPT app data. Explain each permission before requesting
> it, open Seld, restart the ChatGPT app when instructed, and help me begin
> `$gsv-onboard` in one fresh task so I can connect the sources I choose.

The current macOS install uses Python 3.11+ and
[`uv`](https://docs.astral.sh/uv/). The exact command is:

```bash
uv tool install 'git+https://github.com/olivier-motium/seld.git'
gsv setup
```

Setup preserves existing records and unrelated ChatGPT configuration, installs
Seld's local plugin and skills, and opens Bridge. The install agent then runs
the health and recovery checks in [`AGENT_INSTALL.md`](AGENT_INSTALL.md).

## Included in the source distribution

The macOS source distribution includes:

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
- context-first onboarding, source selection, AI-performed bounded reads, and
  a content-free coverage ledger visible in Bridge, CLI, and MCP from a fresh
  process; and
- the resident Pulse skill, which lets the AI read selected sources, reason
  over fresh changes and durable context, and author the current Mind through
  exact compare-and-swap writes.

The source catalog includes ChatGPT activity, Gmail, Google Calendar, Google
Drive and Sheets, Outlook, Slack, Teams, GitHub, Asana, Atlassian, Box, Figma,
Notion, SharePoint, local files, Apple Messages, WhatsApp, Shopify, Instagram,
and optional screen context. The user enables the relevant ChatGPT app, custom
MCP server, or local read tool. Onboarding confirms the intended account and
performs a bounded read before recording the AI-authored coverage horizon. Seld
marks a source current only while the successful read remains within its
recipe's freshness window on the computer and recipe version that produced it.

The supported consumer surface is macOS. Windows and Claude support are coming.
Evidence for source installation, packaging, and reliability studies is
recorded in the [public evidence ledger](docs/release-gates.md).

### Product name and compatibility identifiers

Seld is the product and repository name. The current implementation retains the
existing `gsv` executable, Python package, plugin and skill identifiers,
`GSV_*` environment variables, and `~/GSV` default records folder. Those are
compatibility interfaces, not the consumer brand.

## How intelligence and safety divide the work

Seld does not turn an old email, changed file, or due date into a task through a
fixed semantic rule. The model reads bounded evidence in context and decides
what it means: whether two records describe the same person, whether a
commitment changed, whether something deserves attention, and what the next
action should be.

The deterministic code authenticates Bridge, protects revisions, bounds inputs,
records delivery facts, prevents replay, and supports recovery. Priority,
completion, relationships, memory worthiness, and the meaning of a message
remain model judgments.

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

- [Seld 0.3.0 release notes](docs/releases/0.3.0.md)
- [Product contract](docs/product-contract.md)
- [Installation and recovery](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Trust model](docs/trust-model.md)
- [State-of-the-art architecture assessment](docs/state-of-the-art.md)
- [ChatGPT app integration evidence](docs/codex-integration.md)
- [Onboarding and source ecosystem](docs/onboarding.md)
- [Resident AI Pulse contract](docs/pulse.md)
- [Evidence and expansion claims](docs/release-gates.md)

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
