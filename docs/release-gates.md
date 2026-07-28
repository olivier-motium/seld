# Release gates

Release status is an evidence ledger, not a promise. Every release gate below
is currently **open** for the exact candidate on this branch. Local code,
passing unit tests, or a predecessor release cannot close an installed-path
gate.

Evidence belongs to exact bytes. Results from a predecessor commit, a local
development tree, or a rebuilt binary do not transfer to a changed candidate.

## Current gate ledger

| Gate | Evidence required for promotion | Current status |
| --- | --- | --- |
| Bounded foundation round trip | An authenticated Bridge intent survives the queue CAS boundary, is read and accepted or rejected through both supported CLI and MCP surfaces, and its disposition remains visible from a fresh process. Rejected Origin/bearer, stale CAS, bounded-queue recovery, and restart persistence are exercised. | **Open — POSIX-only foundation.** Secure directory-pinned storage is available on macOS/Linux; the Windows operation commands and tools are not advertised, Bridge writes fail closed, and canonical reads remain available. |
| Guided review same-hand transport | On the exact installed candidate and a clean Plus-class account, a Bridge answer uses the user's supported Codex subscription defaults, resumes the exact authored Codex hand through the restricted `gsv`-only MCP profile, applies each answered prepared row with independent CAS/readback, dispositions the receipt, returns a bounded receipt-bound sheet to Bridge, and preserves canonical progress after a fresh Bridge process without pretending the transient sheet survived. Safe failure, concurrent submit, stale target or row, killed turn, restart recovery, same-subject regeneration, and no-replay `delivery_uncertain` paths pass without provider, Computer Use, or external-action tools. | **Open — source-tree tests only.** Installed-candidate, clean-Plus-account, provider entitlement, and platform proof are absent. |
| Gate 0: product premise | A clean ChatGPT Plus-class account proves one app-native heartbeat waking the exact dedicated resident Pulse task, AI execution with selected plugin tools, host-observed structured tool results, structural Computer Use exclusion, and canonical CAS/readback without a duplicate hand. | **Open — unproven.** The packaged skill, model narration, mocks, an interactive task, and deterministic scheduler tests are not substitutes for a natural installed wake. |
| Clean-machine installation | The exact signed candidate installs transactionally on a clean host, preserves existing Codex and Seld data, passes health checks, and rolls back on failure. | **Open — unproven.** No clean-machine evidence exists for this candidate. |
| Upgrade and executable compatibility | Upgrade from the exact prior release proves Codex integration commits before Bridge starts, an honest post-commit Bridge-repair result that keeps the candidate, safe rollback when current bytes remain readable, and fail-closed retention of the candidate plus recovery binary after either a version-2 Task or control event makes the prior reader incompatible. No vault snapshot, truncation, or automatic down-migration is used. | **Open — source tests only.** The versioned readers and installer probes have local coverage; exact prior-release and installed-platform evidence are absent. |
| Provider-backed continuity | A user-authenticated, isolated Codex home produces an actual model-backed response in a fresh Codex task and continues from durable Seld state without copied credentials or transcript state. | **Open — no qualifying evidence.** A real existing-account source-tree canary ran, but it did not use an isolated Codex home or installed candidate. |
| Source connectors | Each promoted connector proves the exact identity and scope, one bounded real read in a fresh task, the resident AI Pulse reading the same logical capability, and a current host-observed receipt. | **Open — unproven.** Recipes, validators, mocks, configuration, authentication screens, and model narration are insufficient. The host receipt proves delivery facts, not semantic meaning. |
| Cross-platform installed path | The same exact candidate passes install, upgrade, backup, restore, uninstall, Bridge, resident heartbeat, and fresh-Codex-task continuity checks on macOS arm64, macOS x86_64, and Windows x86_64. | **Open — unproven.** Predecessor local macOS Apple Silicon evidence does not promote this candidate or another target. |
| Platform signing | macOS artifacts pass Developer ID signing and Apple notarization; Windows artifacts pass Authenticode signing. | **Open — absent.** GitHub provenance and SHA-256 checksums are useful but are not platform signing. |
| 72-hour resident-heartbeat soak | On each stable platform/architecture, the exact candidate completes a 72-hour awake-time soak and records at least 95% of expected app-native Pulse wakes, with sleep, failures, throttling, and recoveries accounted for. | **Open — unproven.** |
| 30-day account soak | The exact candidate completes a 30-day Plus-account economics and interference soak, supporting at least 24 full cognitive passes on a busy day without disabling or materially blocking normal interactive Codex use. | **Open — unproven.** |
| 14-day daily-use beta | Six non-author users run the exact candidate daily for 14 days: two users on each stable platform/architecture target. | **Open — unproven.** |

## Partial source-tree canary

On 2026-07-25, an authenticated real Codex CLI canary loaded source from base
commit `bdac8f704a14b9f7f68ab35d4765d3120d868104` with pre-documentation Git
index tree `7bf719b657e1baaa7d3163ee31af7959f76a514a`. HEAD alone is not the
executed candidate identity. The provider phase ran from 12:57:35 to 13:02:00
CEST on macOS 26.2 arm64 with Seld 0.2.0 and Codex CLI 0.145.0. Against a
synthetic vault, the canary rejected wrong bearer and Origin, rejected stale CAS
without changing the queue, completed START's two Codex invocations, completed
one same-hand answer with semantic readback and disposition, and proved restart
recovery, durable receipts, fresh-process CLI/MCP visibility, and event-scoped
MCP isolation.

The canary did not use a signed or installed candidate, a clean isolated
Plus-class home, an app-native task path, a connector or scheduler path, or
another platform. Later source and documentation changes produce a different Git tree, so
this is historical partial evidence, not exact-final-candidate promotion. Every
gate in the ledger remains open.

The stable 1.0 target matrix is macOS arm64, macOS x86_64, and Windows x86_64.
Windows arm64 remains experimental. An asset name or successful cross-compile
does not make a target stable; promotion requires hosted exact-target evidence.
Linux build support in the continuity kernel is not part of the 1.0 replacement
claim.

## Foundation promotion blockers

Passing model and unit tests do not promote the currently unexposed foundation
modules or prove the packaged AI Pulse on an installed account. Before wiring
more deterministic foundation code into a consumer surface, the exact candidate
must close a concrete safety need and the applicable design gap:

- source readiness needs one deterministic doctor that assembles and verifies
  identity/scope, bounded-read, and Pulse-canary evidence without a model
  committing `ready`;
- machine-local host-readiness persistence still uses cooperative path-based
  locking and replacement. It must gain descriptor-pinned, exact-byte CAS (or
  an equally strong Windows-capable primitive) before it becomes authority for
  permissions, connector state, or autonomy;
- the product path uses one app-native heartbeat, not the unexposed OS-scheduler
  planner. Registration still needs owned-task readback, duplicate prevention,
  and natural-wake evidence. If an OS scheduler is ever promoted separately,
  installation first needs ownership-aware reconciliation for commit-then-
  timeout, cancellation, and missing-receipt ambiguity; and
- control-queue recovery needs a bounded authenticated checkpoint or compaction
  design before consumer promotion. The foundation deliberately stops after
  1,024 archived generations rather than deleting tamper evidence or silently
  growing forever; and
- guided-review transport needs an authenticated receipt archival or compaction
  path before consumer promotion. The content-free receipt lane deliberately
  stops after 2,000 lifetime receipts; the intent remains queued with an exact
  `transport_receipt_store_full` reason and manual operation resolution, plus
  an exact-hand fallback when an authored hand exists, but automatic
  continuation cannot recover capacity yet;
- Bridge lifecycle needs an owned-child reconciliation contract before consumer
  promotion. Stopping Bridge does not prove that an already detached Codex turn
  stopped, so restart must retain the exact hand, mark delivery uncertain, and
  never replay it; and
- prepared-review timing needs installed/provider soak evidence before consumer
  promotion. The source contract permits 25 independently applied rows but the
  current turn ceiling is eight minutes; a timeout remains a terminal
  no-replay `delivery_uncertain` result, not proof that a large batch is usable;
  and
- guided all-open review needs bounded coverage checkpointing or an enforced
  product limit before consumer promotion. The current single-session contract
  is 512 open outcomes and 512 retained exact coverage anchors; the broader
  Portfolio format permits 10,000. Crossing either review bound fails closed
  until the session is checkpointed or older coverage refs are pruned through
  fresh Task CAS.

The `gsv-onboard` and `gsv-pulse` skills are packaged, but their installed
consumer flow remains gate-open. Typed onboarding/source state, deterministic
Pulse admission, OS-scheduler planning, privacy orchestration, and migration
modules remain unexposed while these and the installed release gates are open.

## Zero-incident criteria for the beta

The 14-day beta must complete with zero:

- corruption;
- data loss;
- unauthorized mutation;
- hidden staleness; and
- unrecoverable onboarding journeys.

A detected incident is not converted into success by recovery or by excluding
the affected user. Resolve the cause, produce a new exact candidate, and rerun
the applicable gate.

## Evidence packet

Keep the packet privacy-safe and independently checkable. Record:

- exact commit, tag, asset name, and binary SHA-256;
- operating system, architecture, Seld version, and Codex version;
- start and end timestamps;
- expected and observed resident heartbeat wakes when applicable;
- pass, failure, interruption, and recovery outcomes; and
- the validator or provider receipt needed to reproduce the conclusion.

Do not include credentials, tokens, raw provider bodies, full transcripts,
private screenshots, or unnecessary personal identifiers. A summary without
the exact artifact identity is not release evidence.

## Promotion and claim boundary

All required gates must pass on the same exact candidate. A change to release
bytes resets the evidence affected by that change. Elapsed calendar time,
source inspection, a successful demo, or evidence from another platform cannot
substitute for an unfinished gate.

Until every required row passes, describe the 1.0 product as **Unreleased** and
the relevant capability as **foundation-gated**. The bounded local round trip
may be reported with its exact evidence, but it does not imply consumer-grade
onboarding, live sources, Pulse autonomy, scheduled operation, or clean-machine
support. Do not claim that Seld 1.0 is always-on, autonomous, available
everywhere, a proven Jarvis-like system, or a proven replacement for OpenClaw,
Hermes Agent, or another product.
