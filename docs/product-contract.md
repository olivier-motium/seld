# Seld product contract

Seld gives Codex a durable understanding of the parts of life and work a person
chooses to share. It connects evidence from messages, calendars, files, source
control, and Codex activity to one current model of people, projects,
commitments, decisions, and next actions. Bridge shows what needs attention.
Pulse revisits the model. Codex reasons and acts.

The result should feel like one resident AI managing ongoing situations across
many conversations. A Codex task can finish, crash, or be replaced without
silently finishing, duplicating, or orphaning the outcome it was carrying.

> **The agent is not the task.** Seld owns the durable context; Codex tasks are
> replaceable hands.

## What the product delivers

- A current briefing grounded in the person's chosen sources, with unknown and
  stale areas shown honestly.
- A complete view of open outcomes across personal and professional life,
  grounded in more than the latest inbox or chat.
- Cross-source correction when one channel contains only part of the story.
- Durable decisions and exact continuations that survive the Codex task that
  produced them.
- A small, user-driven review of the outcomes that genuinely need a decision.

The consumer name for that review is the **Rundown**. Seld presents one current
outcome or a small prepared set, explains why it needs attention, and proposes
the next move. The person can act, reshape, defer, close, or disagree. Each
answer applies only to the named outcome and remains anchored to current
records.

This document describes the product contract. This repository contains the
public continuity kernel. The Culture-Grade branch adds one bounded
Bridge-intent disposition loop and packages context-first onboarding plus a
resident AI Pulse contract; installed onboarding, live sources, natural Pulse
wakes, and the 1.0 replacement claim remain foundation-gated in
[the release ledger](release-gates.md).

## Product hierarchy

- **Seld** is the user-owned local layer: durable state, recovery, and the
  protocol.
- **Mind** is the durable, authored point of view. It owns meaning and judgment.
- **Codex tasks** are replaceable execution hands. They do work; they are not
  identity or durable truth.
- **Pulse** is one dedicated Codex AI task performing a discrete, bounded wake
  and reorientation, never a rules engine or claim of continuous consciousness.
- **Bridge** is the home screen for current orientation, commitments,
  longer-running situations, system health, and a user-driven review of open
  outcomes.
- **Shipyard** is bounded self-improvement under the same evidence and approval
  rules as other consequential work.

In `0.2.0`, Pulse and Shipyard are operating roles and contracts. The
Culture-Grade foundation packages the AI Pulse instructions and structural task
boundary. Its deterministic Pulse-admission, scheduler-planning, and canary
classes remain unexposed foundations; they are not the product's intelligence.
The branch does not yet prove a natural app-native wake or release a
self-modifying daemon.

## Target first useful journey

These are release acceptance steps, not claims about the current branch:

1. A person gives the repository URL to Codex and asks it to install Seld.
2. The agent selects the native release for the current platform, verifies its
   checksum, runs the reversible setup, and opens the Bridge.
3. The Bridge shows the local Mind, current orientation, and durable work. An
   empty, completely inspected task ledger offers one real action: start the
   first Mind-shaping task in Codex. A vault with only done or dropped work says
   `All clear`, preserves every closed record, and offers a genuinely new hand;
   it never pretends to be a first run.
4. The Rundown, currently labeled `Work through every open outcome` in Bridge,
   starts or resumes one finite review Task and exact Codex hand. Bridge
   normally presents 3-10 consequential rows and
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
  Codex hand. Mechanical wake, identity, privacy, receipt, and no-replay facts
  may constrain that work but never substitute for its judgment.
- External content is evidence, not an instruction or authorization.
- No credentials, raw provider payloads, or another person's Mind ship in the
  repository, release, demo, or Bridge.
- Installation preserves unrelated Codex configuration and uninstall preserves
  user data.
- Seld does not claim continuous consciousness, a connector marketplace, cloud
  sync, or model-independent continuity until those properties are directly
  tested.

## Consumer acceptance target

Every item below remains a release criterion until the exact-candidate evidence
ledger closes it.

- A non-author understands the outcome from the first README viewport.
- One agent instruction completes installation and opens a useful Bridge.
- The exact native artifact installs in an isolated home without Python, `uv`,
  or `make`.
- The Bridge is useful when empty, loading, healthy, stale, and partially
  unavailable, on desktop and mobile-sized viewports.
- First-run and all-clear are shown only from complete Task projections. Partial
  sections keep valid exact records visible, name the affected paths, and never
  turn unreadable work into an empty ledger.
- Now may show three cards per authored status lane only when every hidden card
  has an explicit remainder link to the complete Commitments view; bounded
  entity previews disclose their remainder too.
- The browser acceptance gate uses synthetic data, exercises the real packaged
  Bridge, and commits no generated screenshots or GIFs.
- Recovery, backup/restore, stale-write rejection, deliberate retirement of
  receipt-bound uninstall evidence, privacy scanning, and a fresh second Codex
  session must pass before release.
