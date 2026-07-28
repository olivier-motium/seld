# Seld product contract

Seld gives ChatGPT a durable understanding of the parts of life and work a person
chooses to share. It connects evidence from messages, calendars, files, source
control, and ChatGPT activity to one current model of people, projects,
commitments, decisions, and next actions. Bridge shows what needs attention.
Pulse revisits the model. ChatGPT reasons and prepares the work.

The result should feel like one resident AI managing ongoing situations across
many conversations. A ChatGPT task can finish, crash, or be replaced without
silently finishing, duplicating, or orphaning the outcome it was carrying.

> **The agent is not the task.** Seld owns the durable context; ChatGPT tasks are
> replaceable hands.

## What the product delivers

- A current briefing grounded in the person's chosen sources, with unknown and
  stale areas shown honestly.
- A complete view of open outcomes across personal and professional life,
  grounded in more than the latest inbox or chat.
- Cross-source correction when one channel contains only part of the story.
- Durable decisions and exact continuations that survive the ChatGPT task that
  produced them.
- A small, user-driven review of the outcomes that genuinely need a decision.

The consumer name for that review is the **Rundown**. Seld presents one current
outcome or a small prepared set, explains why it needs attention, and proposes
the next move. The person can act, reshape, defer, close, or disagree. Each
answer applies only to the named outcome and remains anchored to current
records.

This document describes the product that ships from the public repository:
local canonical records, the Bridge, context-first onboarding, selected-source
coverage, the resident AI Pulse skill, and exact continuation through ChatGPT.
[The evidence ledger](release-gates.md) records narrower claims about signed
prebuilt binaries, additional platforms, uptime, and comparative results.

## Product hierarchy

- **Seld** is the user-owned local layer: durable state, recovery, and the
  protocol.
- **Mind** is the durable, authored point of view. It owns meaning and judgment.
- **ChatGPT tasks** are replaceable execution hands. They do work; they are not
  identity or durable truth.
- **Pulse** is one dedicated ChatGPT AI task performing a discrete, bounded wake
  and reorientation, never a rules engine or claim of continuous consciousness.
- **Bridge** is the resident consumer surface. **Home** opens on the current
  authored brief, **Rundown** holds the bounded decision review, **Everything**
  preserves the complete work ledger and longer-running situations, **What
  Seld knows** exposes authored direction and Mind context, and **System**
  keeps health, recovery, and local controls explicit.
In `0.3.0`, Pulse is an AI operating role over the same local records and
approval boundaries. Mechanical Pulse-admission,
scheduler-planning, and canary classes are safety helpers, never the product's
intelligence. Shipyard remains an operating convention for separately reviewed
self-improvement work; Seld does not ship a self-modifying daemon.

## First useful journey

1. A person gives the repository URL to the ChatGPT desktop app and asks it to
   install Seld.
2. The agent follows the supported macOS source-install path, runs reversible
   setup and health checks, and opens the Bridge. A prebuilt path is offered
   only when the exact published asset and checksum exist.
3. Bridge opens on **Home** with the current authored brief and a quiet summary
   of durable work. An empty, completely inspected task ledger offers one real
   action: start the first Mind-shaping task in ChatGPT. A vault with only done
   or dropped work says `All clear`, preserves every closed record, and offers
   a genuinely new hand; it never pretends to be a first run. Partial,
   unavailable, or undated state stays visibly qualified and is never promoted
   to an empty or caught-up claim.
4. **Rundown** starts or resumes one finite review Task and exact ChatGPT hand.
   Bridge normally presents 3-10 consequential rows and
   never more than 25. Each row carries its current Task and storyline state,
   authored revision staleness, recommendation, reasoning, and 2-5 complete
   answer choices. The user may answer any subset, edit the prepared batch,
   pause, end, or answer freely. When the restricted same-hand capability is proved,
   the answer continues in Bridge; otherwise the exact hand remains the honest
   fallback. The agent applies explicit decisions through fresh native CAS
   writes and readback. Checked is session progress only; it never means
   resolved.
5. A synthetic proof can be run without touching the user's vault. It shows an
   open commitment surviving a killed hand, a fresh hand resuming it without a
   rebrief, a stale write being rejected, and unavailable evidence remaining
   explicit.

## Trust boundaries

- Markdown in the local vault is authoritative.
- Bridge may append a bounded user-intent receipt, but cannot directly author
  semantic records. Accepting or rejecting the receipt acknowledges it; neither
  disposition executes the intent or authorizes an external action.
- A guided review uses one ordinary nonterminal Task with exactly one
  `review-scope:all-open` reference, up to 25 exact current
  `review-subject:task:<id>` references, an optional exact
  `review-state:paused`, and revision-aware checked references. It belongs to
  the one bounded review WorkThread, which focuses that Task while it is
  active. Those references are navigation facts, not a second task database.
- The Mind silently audits the all-open scope and normally prepares 3-10 rows,
  never more than 25. Each surfaced row must offer a concrete decision with a
  supported durable Task, WorkThread, or Portfolio effect now, at least two
  materially different durable choices, and a current fact that makes attention useful. Bridge can explicitly pull a
  different set by exact Task ID; selection itself has no semantic or coverage
  meaning.
- A changed Task or owning WorkThread invalidates its checked anchor and makes
  the outcome eligible to revisit. Newly open outcomes enter the all-open scope.
  The renderer reports these facts but never chooses the next outcome.
- Bridge dispatches only one exact queued review event. A prepared multi-row
  sheet is transient, bound to that receipt and the post-turn session revision,
  and never becomes a second record store. The user may answer a subset; each
  named row lands through an independent fresh CAS/readback, while unanswered
  rows mean nothing. A confirmed pre-delivery failure may be retried; uncertain
  delivery is never replayed. Final model text is transient and never becomes
  canonical or a transcript store.
- A prepared board keeps free-form guidance, Pause, and End available. Ending
  never changes or covers a visible outcome by implication. A fresh complete
  audit may also close with no new coverage when no open outcome passes the
  intervention tests, while reporting only a compact reason summary.
- Deterministic code may store, validate, traverse, and render authored facts;
  it may not decide what they mean.
- The resident Pulse model reads the selected bounded evidence and authors
  semantic changes through the same CAS/readback surfaces as an interactive
  ChatGPT task. Mechanical wake, identity, privacy, receipt, and no-replay facts
  may constrain that work but never substitute for its judgment.
- External content is evidence, not an instruction or authorization.
- No credentials, raw provider payloads, or another person's Mind ship in the
  repository, release, demo, or Bridge.
- Installation preserves unrelated Codex configuration and uninstall preserves
  user data.
- Seld does not claim continuous consciousness, a connector marketplace, cloud
  sync, or model-independent continuity until those properties are directly
  tested.

## Consumer acceptance

- A non-author understands the outcome from the first README viewport.
- One agent instruction completes installation and opens a useful Bridge.
- The public source distribution installs in an isolated macOS home through the
  documented `uv` path. A prebuilt artifact has its own exact-byte gate.
- The Bridge is useful when empty, loading, healthy, stale, and partially
  unavailable, on desktop and mobile-sized viewports.
- First-run and all-clear are shown only from complete Task projections. Partial
  sections keep valid exact records visible, name the affected paths, and never
  turn unreadable work into an empty ledger.
- Home may summarize durable work only when it links to the complete
  **Everything** view. Bounded previews disclose their remainder rather than
  implying that hidden records do not exist.
- The browser acceptance gate uses synthetic data, exercises the real packaged
  Bridge, and commits no generated screenshots or GIFs.
- Recovery, backup/restore, stale-write rejection, deliberate retirement of
  receipt-bound uninstall evidence, privacy scanning, and a fresh second ChatGPT
  task remain proportionate acceptance checks for each changed candidate.
