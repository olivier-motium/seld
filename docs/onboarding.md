# Onboarding

GSV 1.0 onboarding is designed to start with the person's life, not a
connector checklist. It first establishes what should become easier, which
contexts must remain separate, what the system must never retain, and what a
useful first result would be. Sources come after that context is understood.

> **Current status:** The packaged `$gsv-onboard` skill can collect context,
> persist accepted Mind context through document CAS, guide one selected
> connector enablement wave, and register the structural resident Pulse path.
> This is not yet a proven consumer journey. The public CLI still exposes no
> `gsv onboard` or `gsv source` commands; typed onboarding state, source recipes,
> attestation validators, and scheduler planners remain unexposed foundation
> code. Bridge does not publish, advance, or validate that state. Source-tree
> instructions and tests do not prove a resumable journey, a ready source, a
> natural app wake, or an installed clean-account flow.

## Product principles

- Begin with the person's desired outcome and boundaries.
- Propose context in human language. The person may accept, edit, reject, or
  skip each part before any later supported surface makes it durable.
- Treat source-zero as an honest target state. External reality stays unknown
  until a source has current installed-path evidence.
- Treat authentication as different from readiness. A login or configured
  account is not a source attestation.
- Treat provider content as evidence, never instructions or authorization.
- Retain small derived signals and stable references, not raw inboxes,
  transcripts, screenshots, credentials, or unnecessary personal data.
- Require one active onboarding hand and a fresh, exact approval for every
  external mutation.

## Foundation state vocabulary

The foundation model defines nine ordered phases so a future supported surface
can resume without interpreting chat prose:

| Phase | Target milestone |
| --- | --- |
| `codex_substrate` | Verify the exact local GSV and Codex substrate before collecting personal context. |
| `privacy_and_context_capture` | Agree what matters, what remains separate, what may be retained, and the action boundary. |
| `source_selection` | Keep mandatory GSV source zero and choose the smallest useful external source set. |
| `enablement_wait` | Wait for user-owned plugin, authentication, permission, or policy work without pretending it is ready. |
| `fresh_task_verification` | Prove the required tool and durable onboarding state in a fresh Codex task. |
| `context_synthesis` | Synthesize only accepted context and currently attested evidence, with uncertainty and provenance. |
| `initial_orientation` | Present the first useful orientation and let the person correct it. |
| `continuity_and_autonomy_proof` | Prove only the declared continuity and autonomy boundary. |
| `done` | Finish only when host identity/read evidence and fresh-task verification pass. |

Completion is modeled independently as `in_progress`, `waiting_user`,
`fresh_task_required`, `blocked`, `operational_with_gaps`, `fully_connected`,
or `needs_revalidation`. These are storage and validation contracts today, not
states a consumer can advance through the public CLI.

## Target journey

### 1. Start from supported durable state

First inspect the exact installed command, MCP, skill, and app automation
surfaces. Use supported document CAS and operation interfaces when present. If
onboarding status, start, resume, takeover, and doctor interfaces are absent,
do not invent their state: continue only with the skill's explicit context,
task-local connector, and Pulse-registration boundaries. Do not edit
onboarding Markdown directly, call an internal Python API, or reconstruct a
journey from a transcript and present it as supported onboarding.

### 2. Shape the first useful result

The onboarding skill conducts one context conversation. Ask
one compact batch:

1. What should feel easier in two weeks?
2. Which two or three parts of life or work matter most now?
3. Which people, projects, or promises must not be dropped?
4. What should GSV never read or retain?
5. Which local, reversible actions are acceptable, and which actions always
   require exact approval?
6. Should the first proof be local-only, based on one source, or a bounded
   cross-source view?

Mirror back the proposed contexts, outcome, exclusions, action boundary, first
proof, sources now or later, and remaining uncertainties. After explicit
acceptance, write the bounded context to `MIND.md` only through its public
document CAS surface and read it back. That creates accepted Mind context, not
a fabricated onboarding session or readiness state. If document CAS is absent,
the conversation remains a proposal only.

### 3. Use the implemented Bridge intent loop accurately

Bridge can append one authenticated, compare-and-swap intent of type setup
choice, approval, correction, or undo request. The receipt is not canonical
state and carries no external-action authority.

On macOS and Linux, where the secure pinned store is available, the supported
operation surfaces are:

- `gsv operation list`
- `gsv operation accept --help`
- `gsv operation reject --help`
- `gsv_operation_list`, `gsv_operation_accept`, and
  `gsv_operation_reject` over MCP

The accept and reject commands require the logical vault ID plus the current
queue and disposition revisions returned by `operation list`, along with
bounded actor and reason references. Never reuse those tokens after changing,
moving, or restoring the vault. They durably disposition a queued receipt
exactly once. **Accept means acknowledge this receipt for later review. It does
not approve or execute the request, authorize an external action, or mutate
semantic canon.** Queue archival is an operator recovery surface, not an
ordinary onboarding action.

The Windows foundation does not advertise these CLI commands or MCP tools. Its
Bridge control endpoint remains unavailable and writes nothing while canonical
read views continue to work. Windows support remains a release gate.

### 4. Prove selected sources only after public interfaces ship

The foundation uses these exact source states:

| State | Meaning |
| --- | --- |
| `not_selected` | The source remains off. |
| `plugin_missing` | The selected supported connector plugin is absent. |
| `auth_required` | The person must authenticate in the provider surface. |
| `fresh_task_required` | Tool discovery or proof requires a fresh Codex task. |
| `tool_absent` | The expected supported tool is absent from the current runtime. |
| `identity_pending` | The intended owner, account, or scope still needs confirmation. |
| `identity_mismatch` | The observed identity does not match the selected identity. |
| `read_unverified` | A bounded read may be host-verified, but the full identity, account/scope confirmation, and Pulse-canary bundle is incomplete. |
| `canary_failed` | The bounded real-read canary failed. |
| `ready` | The complete deterministic proof bundle is current for the declared identity and scope. |
| `stale` | Earlier proof expired or drifted. |
| `blocked_by_policy` | A declared policy blocks the proof. |
| `unsupported_tool_shape` | The available tool shape is not an accepted recipe. |
| `unavailable` | The source cannot be proved on the current host or account. |
| `declined` | The person explicitly kept the source off. |

No current public command can produce `ready`. The target proof requires an
installed supported recipe, a read-only capability check, confirmed identity
and scope, one bounded real read, Pulse access to the same logical capability,
and a correlated host-observed attestation. A model statement, login screen,
configured account, synthetic response, or validator unit test is insufficient.

### 5. Prove fresh-task continuity

The target product must open one fresh Codex task, reload the accepted context,
journey state, and next safe step from the portable session, and re-prove
host-specific evidence where needed. A copied summary or transcript is not
continuity evidence. This fresh-task onboarding proof remains release-gated.

### 6. Synthesize only proven context

The target synthesis combines accepted local context with evidence from
currently attested sources. It keeps user statements, observations, and
inferences distinct, preserves stable references and coverage boundaries, and
excludes raw provider bodies and rejected material.

### 7. Present an honest first orientation

Show what matters now, what may be at risk of being dropped, which evidence
supports the view, and what remains unknown. Let the person correct, accept, or
reject every proposed addition. Orientation is not external-action permission.

### 8. Prove continuity and autonomy separately

The target product must prove fresh-task continuity and a real asynchronous
app-native heartbeat waking the same dedicated Pulse task while preserving the
declared local, reversible boundary. The model—not a deterministic scheduler
controller—must read the frozen evidence and author any resulting semantic
change through CAS/readback. The implemented Bridge operation round trip and
packaged skill prove neither onboarding readiness nor natural wake autonomy.
No public onboarding doctor exists in this foundation slice, so core onboarding
readiness cannot currently be committed.

### 9. Report the real operating state

When the public validators and installed paths exist, report which evidence is
local, which came from currently ready sources, and which areas remain unknown,
unavailable, or foundation-gated. Preserve `operational_with_gaps` when that is
the result; never relabel it `fully_connected`.

## Optional Computer Use target

Computer Use is an optional interactive accelerator for a bounded setup step,
not an onboarding dependency or connector fallback. It must be explicitly
selected, stop for credentials, OAuth, second factors, legal terms, and OS
security prompts, and remain structurally unavailable to scheduled work. Its
installed exclusion proof is still an open Gate 0 item.

## Recovery today

Use `gsv operation list` to reload queued and durably decided Bridge intents.
Use the supported accept or reject command only with fresh revisions; a stale
CAS conflict means reload, not retry with guessed state. Do not edit, delete,
replay, or reorder queue and disposition files. The operator-only closed-queue
archive surface exists for bounded-capacity recovery.

There is no supported public recovery command for the foundation onboarding or
source models. Report the last proven state and stop. Backup and restore cover
the existing continuity kernel; they do not turn inert onboarding state into a
consumer-ready journey.

## Done means

For 1.0, onboarding will be complete only when the person has accepted the
useful context and boundaries, the agreed proof works, every source boundary
is explicit, host-level identity and read evidence passes, the resident AI
Pulse completes a natural wake, and a fresh task resumes without the old
transcript. Those conditions are not met by this foundation slice.
See [Release gates](release-gates.md) for the authoritative claim boundary.
