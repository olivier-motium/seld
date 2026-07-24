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
| Gate 0: product premise | A clean ChatGPT Plus-class account proves unattended app-native Codex execution with selected plugin tools, host-observed structured tool results, structural Computer Use exclusion, and a real scheduled wake with the shell closed. | **Open — unproven.** Model narration, mocks, and an interactive task are not substitutes. |
| Clean-machine installation | The exact signed candidate installs transactionally on a clean host, preserves existing Codex and GSV data, passes health checks, and rolls back on failure. | **Open — unproven.** No clean-machine evidence exists for this candidate. |
| Provider-backed continuity | A user-authenticated, isolated Codex home produces an actual model-backed response in a fresh Codex task and continues from durable GSV state without copied credentials or transcript state. | **Open — unproven.** Synthetic tests, source inspection, and UI discovery are insufficient. |
| Source connectors | Each promoted connector proves the exact identity and scope, one bounded real read, Pulse access to the same logical capability, and a current host-observed deterministic attestation. | **Open — unproven.** Recipes, validators, mocks, configuration, and authentication screens are insufficient. |
| Cross-platform installed path | The same exact candidate passes install, upgrade, backup, restore, uninstall, Bridge, scheduler, and fresh-Codex-task continuity checks on macOS arm64, macOS x86_64, and Windows x86_64. | **Open — unproven.** Predecessor local macOS Apple Silicon evidence does not promote this candidate or another target. |
| Platform signing | macOS artifacts pass Developer ID signing and Apple notarization; Windows artifacts pass Authenticode signing. | **Open — absent.** GitHub provenance and SHA-256 checksums are useful but are not platform signing. |
| 72-hour scheduler soak | On each stable platform/architecture, the exact candidate completes a 72-hour awake-time soak and records at least 95% of expected wakes, with failures and recoveries accounted for. | **Open — unproven.** |
| 30-day account soak | The exact candidate completes a 30-day Plus-account economics and interference soak, supporting at least 24 full cognitive passes on a busy day without disabling or materially blocking normal interactive Codex use. | **Open — unproven.** |
| 14-day daily-use beta | Six non-author users run the exact candidate daily for 14 days: two users on each stable platform/architecture target. | **Open — unproven.** |

The stable 1.0 target matrix is macOS arm64, macOS x86_64, and Windows x86_64.
Windows arm64 remains experimental. An asset name or successful cross-compile
does not make a target stable; promotion requires hosted exact-target evidence.
Linux build support in the continuity kernel is not part of the 1.0 replacement
claim.

## Foundation promotion blockers

Passing model and unit tests do not promote the currently unexposed foundation
modules. Before wiring them into a consumer surface, the exact candidate must
also close these known design gaps:

- source readiness needs one deterministic doctor that assembles and verifies
  identity/scope, bounded-read, and Pulse-canary evidence without a model
  committing `ready`;
- machine-local host-readiness persistence still uses cooperative path-based
  locking and replacement. It must gain descriptor-pinned, exact-byte CAS (or
  an equally strong Windows-capable primitive) before it becomes authority for
  permissions, connector state, or autonomy;
- scheduler installation needs an ownership-aware reconciliation path for an
  OS install that commits but times out, is cancelled, or fails before its
  receipt returns; the current injected operator seam must not be connected to
  a host scheduler until that ambiguity is resolved; and
- control-queue recovery needs a bounded authenticated checkpoint or compaction
  design before consumer promotion. The foundation deliberately stops after
  1,024 archived generations rather than deleting tamper evidence or silently
  growing forever.

Onboarding, source, Pulse, scheduler, privacy, and migration modules remain
unexposed while these and the installed release gates are open.

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
- operating system, architecture, GSV version, and Codex version;
- start and end timestamps;
- expected and observed scheduler wakes when applicable;
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
support. Do not claim that GSV 1.0 is always-on, autonomous, available
everywhere, a proven Jarvis-like system, or a proven replacement for OpenClaw,
Hermes Agent, or another product.
