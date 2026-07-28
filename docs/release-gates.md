# Evidence and 1.0 claim boundaries

Seld is a live public source distribution. This ledger does not decide whether
the product exists; it records the extra proof required for narrower promises
about prebuilt distribution, platform coverage, and reliability.

Evidence belongs to exact bytes. A source test can prove a source contract; it
cannot prove a signed installer, a provider account, or a multi-day operating
result. Each claim below therefore names the evidence appropriate to that
claim.

## What ships today

- A public macOS source install through
  `uv tool install 'git+https://github.com/olivier-motium/seld.git'`.
- Local Markdown records for Direction, Portfolio, Tasks, entities, work
  threads, decisions, and current orientation, with compare-and-swap writes,
  provenance, backup, restore, and repair.
- The authenticated local Bridge and its bounded append-only intent queue, with
  durable accept or reject disposition over CLI and MCP and no implied external
  action.
- Context-first AI onboarding, a source capability catalog, bounded live-read
  verification in the current ChatGPT task, and source-specific coverage
  reporting.
- The AI-authored Pulse skill, which reasons over current changes and durable
  context while the deterministic layer remains limited to integrity, privacy,
  bounds, and no-replay safety.

The consumer surface is the ChatGPT desktop app on macOS. Windows and Claude
support are coming.

## State-of-the-art position

Seld's current state-of-the-art claim is specific and inspectable: its system
design treats resident personal operations as one complete operating loop with
an explicit Direction and Portfolio, canonical entities and situations,
source-aware AI reasoning, selective interruption, exact task continuity, and
no-replay approval receipts. The versioned
[architecture assessment](state-of-the-art.md) records the decision rule,
current comparison, and exact candidate behind that position.

## Claims that require separate evidence

| Claim | Evidence required before making it | Current publication boundary |
| --- | --- | --- |
| Signed prebuilt macOS distribution | Exact GitHub release asset, SHA-256, workflow provenance, Developer ID signature, notarization, and a clean-host install/upgrade/uninstall run of those bytes. | The public source install is current. Do not describe a prebuilt asset as signed or available until it is published. |
| Windows consumer support | Exact Windows x64 asset, Authenticode, and installed-path proof for setup, Bridge, backup/restore, update, and uninstall. | Windows support is coming. Portability code and a cross-compile are not the support claim. |
| Claude consumer support | A packaged Claude integration and fresh-account installed-path proof using the same local records and approval boundary. | Claude support is coming. |
| Guaranteed unattended Pulse cadence or uptime | A natural app wake on the exact installed version plus a published 72-hour awake-time study whose expected wakes, sleep, throttling, and recovery are accounted for. | Describe Pulse behavior, not an uptime SLA or guaranteed ten-minute cadence. |
| Host-enforced Pulse tool isolation | A task-scoped host tool profile, or a fresh Pulse task exposing only Seld and read-only source tools, plus a natural-wake tool-inventory proof. | Seld's MCP boundary is structurally local and closed-world. Read-only use of tools from separately installed plugins remains Pulse policy because the current heartbeat surface has no Seld-controlled per-task denylist. |
| Plus-plan capacity or interference rate | A published 30-day study on the named account class, including cognitive passes, throttling, and effect on ordinary interactive use. | Do not publish a capacity number without the study. |
| Universal connector certification | Per-source identity/scope confirmation, bounded real read, current-task/Pulse access, and a privacy-safe receipt for the named provider surface. | Seld supports the sources the user enables through ChatGPT apps, custom MCP, and local read tools; onboarding verifies actual availability rather than claiming every provider account in advance. |
| Multi-user reliability rate | A versioned beta protocol, exact builds, participant/platform counts, incident definitions, and published results. | Do not invent adoption, incident, or retention numbers. |

## Exact source evidence

On 2026-07-28, the 0.3.0 implementation candidate at commit
`d0a17d871c45eb216f63a5f8f6041ef6e1b34ef3` was installed from its frozen
source tree into fresh isolated `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` locations.
The installed executable reported `0.3.0`, exposed the CLI, completed `gsv
demo`, and returned a healthy `gsv doctor` result with:

- a child-written revision recovered from a fresh process;
- stale-write rejection;
- interrupted-write recovery;
- backup verification; and
- logical restore equivalence.

The same frozen commit passed `make check` with 892 tests passed and 4 skipped,
strict mypy, Ruff, 82% branch coverage, and a zero-finding privacy scan. Its
browser matrix passed 22 Chromium checks across desktop, mobile, high contrast,
forced colors, reduced motion, same-origin enforcement, stale-CAS recovery, and
unavailable-state recovery. The package build, standalone build, and installed
end-to-end suite also passed directly from that commit.

This proves the 0.3.0 source-install, browser, package, privacy, and synthetic
continuity paths on macOS for those exact implementation bytes. The rows above
name the separate evidence needed for signed binaries, another platform, or a
published reliability rate.

## Provider-backed continuity evidence

On 2026-07-25, an authenticated Codex CLI canary loaded source from base commit
`bdac8f704a14b9f7f68ab35d4765d3120d868104` with pre-documentation Git
index tree `7bf719b657e1baaa7d3163ee31af7959f76a514a`. The provider phase ran
from 12:57:35 to 13:02:00 CEST on macOS 26.2 arm64 with Seld 0.2.0 and Codex
CLI 0.145.0. Against a synthetic vault it rejected wrong bearer and Origin,
rejected stale CAS without changing the queue, completed START's two Codex
invocations, completed one same-hand answer with semantic readback and
disposition, and proved restart recovery, durable receipts, fresh-process
CLI/MCP visibility, and event-scoped MCP isolation.

That canary is evidence for the named source/provider path on those exact
inputs. It is not reused as evidence for signed distribution, another platform,
or a later binary.

## Evidence packet format

Keep every packet privacy-safe and independently checkable. Record:

- exact commit, tag or source revision, asset name when applicable, and binary
  SHA-256 when applicable;
- operating system, architecture, Seld version, ChatGPT/Codex version, and
  source/provider versions relevant to the claim;
- start and end timestamps;
- expected and observed events for a timed study;
- pass, failure, interruption, and recovery outcomes; and
- the validator or privacy-safe receipt needed to reproduce the conclusion.

Do not include credentials, tokens, raw provider bodies, full transcripts,
private screenshots, or unnecessary personal identifiers. A summary without
the exact evaluated identity is not comparative or release evidence.

## Claim discipline

Use the smallest accurate statement:

- Seld itself is public, current, and source-installable.
- A prebuilt binary is available only when the matching GitHub release asset
  exists.
- A signed or notarized build is such only when the exact artifact verifies.
- Windows and Claude support are coming.
- A published reliability number belongs to its exact protocol and version.

Missing evidence for an expansion claim does not downgrade the current product.
A local test, source recipe, or historical canary likewise cannot establish a
platform, provider, signing, soak, or adoption claim.
