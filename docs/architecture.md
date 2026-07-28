# Architecture

Seld has one product loop. The AI reads bounded context from the sources a person
selected, relates it to durable local records, authors any justified change
through compare-and-swap writes, and presents the current result in Bridge.
When work is needed, one exact ChatGPT task carries the outcome. Pulse repeats the
same reasoning loop on a bounded heartbeat; it does not replace the model with
rules.

The architecture exists to keep that intelligence useful and safe. Mechanical
code owns identity, storage, bounds, receipts, privacy, and recovery. The AI
owns meaning, priority, relationships, memory judgment, and what happens next.

## Product hierarchy

- **Seld** is the local continuity layer: the records, persistence protocol,
  recovery, and adapters.
- **Mind** is the authored point of view in durable documents and records.
- **ChatGPT tasks** are replaceable execution hands using the same kernel.
- **Bridge** is the human home screen plus a narrow append-only queue for
  explicit setup choices, approvals, corrections, and undo requests. A queued
  intent and its accept/reject disposition cannot write semantic records or
  authorize an external action.
- **Pulse** is one dedicated ChatGPT task awakened by an app-native heartbeat.
  The model reorients, reads bounded selected sources, and authors judgment.
  It is not a rules engine.

## Kernel

`continuity_kernel.records` defines bounded task, entity, and work-thread
records. `continuity_kernel.portfolio` defines one typed complete authored
Portfolio. `continuity_kernel.vault` owns validation, locking, atomic
persistence, context rendering, backup, restore, and health checks. The CLI and
dependency-free stdio MCP server are adapters over that same vault.

`continuity_kernel.source_state` adds the portable `SOURCES.md` ledger over
that same vault. `gsv source` and `gsv_source_*` expose explicit selection plus
content-free AI-attested read/failure receipts; Bridge shows current, partial,
unread, stale, failed, and host/recipe-revalidation states. Successful receipts
are bound to the local host and recipe version, and failed attempts retain the
lineage of any prior successful coverage. Provider reads stay in the
user-enabled ChatGPT app, custom MCP server, or audited local reader. The core
never parses provider content or decides what it means.

Local-file access uses a separate host-local authority store. Selecting the
logical source grants no directory access. Grant and revoke are explicit CLI
operations bound to one exact vault and root; MCP can list current grants and
read one named relative path but cannot create authority. The reader pins the
root identity, refuses links and path escape, avoids cloud placeholders, and
passes content through the privacy screen. The portable source ledger retains
only a fingerprint of the current grant set, so any grant change makes earlier
coverage require a new bounded read.

The packaged `gsv-onboard` and `gsv-pulse` skills compose document, Task,
WorkThread, Portfolio, source, operation, and Bridge surfaces. Deterministic
code on those paths protects identity, privacy, bounds, compare-and-swap writes,
recovery, and replay safety. Seld ships no second rules-based onboarding,
Pulse, scheduler, or migration control plane.

Markdown in the vault is authoritative. Deterministic code may persist,
validate, traverse, and render authored facts. It may not infer task meaning,
priority, identity, ownership, thread membership, or completion from prose or
activity.

## Resident AI Pulse

The public Pulse composition deliberately reuses ordinary Seld primitives:

1. one structural `task:resident-pulse` is bound to one real ChatGPT task UUID by
   `system-role:resident-pulse`;
2. one app-native `heartbeat` wakes that same ChatGPT task on a ten-minute target
   cadence;
3. the `$gsv-pulse` skill freezes a bounded context and source window;
4. the model reads selected sources directly, treats their contents as
   untrusted evidence, and makes the semantic judgment;
5. justified Task, WorkThread, Entity, Direction, Portfolio, Mind, and NOW
   changes use the existing native CAS surfaces and exact readback; and
6. one wake may create at most one visible sustained-work ChatGPT hand.

The implemented deterministic substrate has only mechanical authority: validate
the structural Pulse Task marker, bound its stored hand as one opaque identifier
line, reject stale CAS, bound input, preserve content-free operation receipts,
and prevent duplicate delivery. The skill performs the UUID comparison. The
content-free source ledger records AI-attested coverage rather than claiming
independent provider certification. It does not decide what matters, translate
a source item into a task, rank work, curate memory, or write an orientation.
The exact resident Pulse is the sole autonomous NOW writer.

The structural Pulse task is transport identity, not a life outcome. Bridge
and guided Portfolio review exclude it from commitment counts and open-outcome
selection. Seld makes no guaranteed wake-rate or uptime claim; those narrower
measurements require the exact installed evidence named in the claim ledger.
See [Resident AI Pulse](pulse.md).

## ChatGPT integration

Setup generates a per-Codex-home local marketplace containing the bundled
`gsv` plugin,
MCP manifest, and skill. The manifest points directly to the standalone
executable for release installs, or to the active module launcher for source
installs.

The MCP adapter resolves one vault for its process lifetime. An explicit global
`--vault` argument takes precedence over `GSV_VAULT` and user configuration, so
later requests cannot silently drift to a different vault.

Installation is a staged transaction. Marketplace registration, plugin
installation, and the managed `AGENTS.md` block are verified before setup
commits their stable ownership receipt and only then starts the Bridge. A
pre-commit setup failure rolls back only components introduced by that
invocation. A post-commit Bridge failure is an explicit installed repair state,
not a trigger for substituting an older executable. Existing or concurrently
changed user components are left untouched.

Expanded persisted mechanics are versioned without encoding meaning: Task
version 2 carries only a 2–25-subject review set, and control event/generation
version 2 carries only an exact review correction above the version-1 4 KiB
bound. Readers keep the version-1 grammar intact. An installer may restore a
previous executable only after read-only status, doctor, and supported ledger
probes prove stable identity and digest; there is no automatic semantic
down-conversion.

The macOS discovery implementation checks an explicit `GSV_CODEX`, `PATH`, and
known Codex Desktop app bundles. Codex does not have to be on `PATH` when the
installed bundle exposes its command. This code path is not current-candidate
installed-platform evidence.

## Bridge lifecycle

The Bridge is a dependency-free loopback HTTP server over one exact vault. A
child binds port `0`, lets the OS select an available port, creates a random
instance identity and bearer capability, and atomically reports its bound port
in an owner-only state receipt.

The private snapshot and health endpoints require the per-launch bearer. The
browser receives it in the URL fragment, moves it into `sessionStorage`, and
removes it from the visible URL before requesting private data. Static assets
do not require vault access. The Bridge rejects cross-origin requests, every
write outside its single bounded control endpoint, path traversal, symlinked
assets, and oversized assets.

Stop is fail-closed. Seld signals a receipt PID only when the live authenticated
health response matches the same instance, PID, port, and vault. A stale,
forged, or PID-reused receipt with a concrete mismatch is removed without
signaling the process. A concrete refusal on the validated loopback port also
removes only the stale receipt and permits a fresh Bridge; it never signals the
unverified PID. Timeouts, resets, malformed responses, and other unavailable
health outcomes preserve the receipt, start no replacement, and ask the
operator to retry. That keeps the only bearer and identity evidence available
for a later safe stop.

The Bridge renders authored documents and records live. Doctor results are
cached briefly and refreshed outside the metadata lock. Codex discovery runs as
one background refresh, so an initial snapshot returns an explicit `checking`
state instead of waiting on a subprocess; concurrent polls cannot start more
checks. The backend derives one `codex.ready` value from successful discovery,
the managed instruction block, and the installed plugin. It emits deep links
only when that value is true: a Mind-shaping link only for a proven zero-task
vault, a generic new-hand link for established use, and task-specific resume
links only for nonterminal commitments.

Record projection is deliberately more available than the strict Vault, CLI,
context pack, and MCP surfaces. The Bridge inspects Tasks, Entities, and Threads
independently and reports each section as `complete`, `partial`, or
`unavailable`, with exact readable records and bounded issue paths. One malformed,
oversized, invalid-UTF-8, symlinked, or nonregular record cannot hide valid
records in another section. A missing, linked, non-directory, or untrustworthy
record directory is unavailable, never an empty ledger. Fresh projection issues
are merged into the cached doctor view immediately, so a partial snapshot cannot
inherit a briefly stale healthy label. The authoritative Vault read APIs remain
strict and fail closed on the same invalid state.

Health returns the manifest-only vault identity cached when the server binds;
it never lists records, runs doctor, or hashes vault files. Each snapshot derives
its counts from the exact readable projection and exposes section completeness
separately. Its `status` block intentionally contains identity and counts, not
the explicit CLI status command's full-vault logical digest.

## Write flow

1. A caller reads an exact record and receives its SHA-256 revision.
2. The caller submits an update with that revision.
3. Seld takes the appropriate cross-process lock and re-reads the record.
4. A mismatched revision is rejected as a stale write.
5. Valid content is written to a same-directory temporary file, flushed, and
   atomically replaced.
6. The containing directory is synchronized where the platform supports it.
7. A bounded audit event is appended under the journal lock.

This is deliberately not described as a multi-file transaction. For ordinary
reported append failures, the append primitive first restores the journal to
its exact previous length. Seld then restores the exact previous canonical bytes
or removes an invocation-owned create, but only while the current bytes still
match that invocation. A post-fsync cleanup failure is reported as committed;
unknown journal state or failed canonical recovery is reported as degraded and
left in place for inspection.

A process or operating-system death between canonical replacement and journal
append can still leave authoritative Markdown without its audit event. `0.3.0`
does not ship a pending-mutation protocol and the journal is not authoritative.
Callers must reload after any committed or degraded persistence error rather
than retrying with a stale revision.

Global operations such as backup hold the global lock before record locks so a
snapshot cannot mix record versions.

Bridge control events follow a deliberately smaller write flow. The browser
reads the current queue revision from the authenticated snapshot, submits one
of four fixed intent shapes, and the core appends it only when that revision is
still current. A separate CLI or MCP process reads the logical vault ID plus
queue and disposition revisions and may durably accept or reject one event
exactly once against all three values. A long-lived MCP process also binds
operations to the physical vault root it opened. That disposition survives
restart, but it only acknowledges the intent. It does not execute it, authorize
an external action, or mutate semantic canon. Provider text cannot add fields
or turn an event or disposition into authority. Closed-queue archival is an
operator capacity-recovery seam, not an ordinary Bridge or onboarding action.
Snapshot, Bridge GET, CLI list, and MCP list are observational for queue and
disposition state: they never anchor or repair those append-only logs. The
snapshot and review-transport GET paths may durably classify a content-free
in-flight turn receipt as `delivery_uncertain` after a crash; that monotone
reconciliation disables replay and does not mutate semantic canon. An explicit
accept, reject, or archive mutation validates the vault binding and revisions
from the queue/disposition observation before performing any deterministic
recovery it needs. Running two Bridge processes against one vault is unsupported;
their instance leases can conservatively classify each other's in-flight review
turns as `delivery_uncertain` rather than risk replay.

### Guided Portfolio review

The guided review is a thin composition of existing primitives. It does not
introduce a transcript database or a second task system:

1. The agent authors the complete `PORTFOLIO.md` through `portfolio set`, with
   exact Direction, Task, and optional owning-WorkThread anchors.
2. One ordinary nonterminal review-session Task belongs to exactly
   `thread:life-portfolio-review`, and that WorkThread focuses the session Task.
   The Task carries one all-open scope, at most 25 exact current subjects, and
   one exact ChatGPT task. `review-state:paused` pauses that same session without
   clearing its subjects or hand. A legacy single-subject session may also
   carry authored quick-option references; a prepared multi-subject session
   keeps its questions and recommendations out of canon.
3. Bridge projects those authored declarations. It never chooses the next Task,
   interprets an answer, derives rank from card position, or changes canon.
   A single-subject card may include authored Task/WorkThread state, exact
   evidence refs, authored revision staleness, recommendation, choice
   consequences, and one question. A prepared board comes only from a bounded
   transient `bridge-sheet` envelope and is rendered only after deterministic
   validation binds its exact subject set and Task anchors to the accepted
   receipt's post-turn session revision. The agent normally surfaces 3-10 rows,
   never more than 25, and only when each row has a concrete decision or bounded
   Mind action now, at least two materially different durable choices, and a
   changed fact, due point, contradiction, dependency, priority, bounded offer,
   or grounded dissent that makes attention useful.
4. A button or freeform answer appends one authenticated correction intent
   bound to the current review-session revision. A stale queue CAS preserves
   the typed answer and requires a deliberate retry against the refreshed
   board. A prepared board may submit only the answered subset in one bounded
   intent; unanswered subjects mean nothing. **Edit batch** can explicitly pull
   up to 25 exact open Task IDs into the next prepared set, but that selection is
   navigation only: it changes no outcome or Portfolio judgment and adds no
   coverage. Free-form guidance, Pause, and End remain available while a sheet
   is prepared. Choices are keyed to the exact
   receipt and session revision and are cleared when either changes. An
   authored legacy quick choice queues the exact standalone sentence shown on
   its button; there is no hidden choice ID, payload, or different instruction.
5. When the local Codex capability check passes, the authenticated append
   creates and schedules one durable turn receipt. The browser's bounded
   review-turn endpoint is an idempotent trigger and recovery path for that
   exact event ID. START first creates a ChatGPT task, then immediately resumes it
   to bind the emitted UUID; later answers resume the authored hand once. Every
   invocation uses the required vault-bound `gsv` MCP server and exposes no
   provider, Computer Use, shell-write, or external-action tools. The browser
   polls only that event ID. Its wait card distinguishes locally saved,
   delivery confirmation, and running states, advances elapsed time locally,
   and pulses only after an actual receipt read. It says when the exact hand is
   known and makes clear that the user may leave without losing the durable
   request; it never displays a fabricated percentage or model-progress claim.
6. The review agent reloads the operation, exact session, every answered
   subject, owning WorkThreads, decisive evidence, and one Portfolio
   inspection; interprets each answer; applies only that row's explicit
   semantic decision through a fresh native CAS; and reads it back before
   moving to the next row. There is no batch transaction: a stale row fails
   independently and must be re-prepared against current truth. The agent then
   accepts or rejects the one receipt. Acceptance is acknowledgement, never the
   semantic write itself.
7. Advancing replaces the prepared subject set and adds revision-aware coverage
   only for outcomes actually checked on current truth. Audited-but-hidden and
   unanswered outcomes remain uncovered. A Task or owning WorkThread change
   makes its row or coverage stale and returns the outcome to the remaining
   scope. A newly open outcome joins the scope. Review progress does not resolve
   the outcome or change Portfolio order mechanically.
8. A successful turn may return one bounded final answer plus a bounded
   prepared sheet for transient Bridge display before the browser reloads
   canon. Neither is persisted. A Bridge restart preserves canonical progress
   but reports that the prepared questions must be regenerated.
   Safe pre-delivery failure may be retried. `delivery_uncertain` is terminal,
   disables replay, and offers the exact-hand link for reconciliation. When a
   terminal transport receipt still has a pending intent, Bridge also exposes
   the existing operation-review link and copyable prompt so the user can
   explicitly acknowledge or reject that receipt without replaying it.
9. Ending a review clears the review WorkThread focus first, then terminalizes
   the session Task through fresh CAS as the final semantic step. The terminal
   Task retains only bounded scope and anchored coverage evidence; subject,
   pause, hand, shadow, and future-work fields are cleared. Besides an explicit
   End or complete anchored coverage, a fresh complete audit may close when no
   open outcome passes all three intervention tests. That path adds no coverage
   and returns only a compact by-reason account of the silent set.
   The same rule applies at opening: START may return a terminal scoped session
   without manufacturing a subject, but the deterministic verifier requires a
   new or changed review Task owned by the review WorkThread with focus cleared
   and no active navigation, hand, future-work fields, or added coverage.

There is no transcript store, semantic queue executor, review database, or
browser-side task policy. The same-hand transport is capability-gated: when the
required executable, auth, or restricted MCP configuration cannot be proved,
Bridge leaves the receipt pending. A missing exact hand also blocks later
answers; START instead creates and binds the one new hand described above.
Bridge uses the exact-hand fallback whenever one is available. This source path
and synthetic tests are not Gate 0 or provider-backed release evidence. The
local capability probe is cached for only a bounded interval, so repaired Codex
authentication can become visible without restarting Bridge.

This control write flow currently depends on POSIX directory descriptors,
`O_NOFOLLOW`, and directory-root locking. It is enabled on macOS and Linux.
Windows support is coming; Seld does not advertise the operation CLI or MCP
surface there until an equivalent secure pinned-store backend exists. Bridge reports
the control lane unavailable, does not fall back to a path-based writer, and
keeps canonical read projections usable.

Backup creation reads each included regular file once, hashes those captured
bytes, and writes the same bytes into a staged ZIP. Symlinks and other special
files and traversal errors fail closed. Publication uses a same-directory
no-replace hard link or a native atomic no-replace move of the complete stage;
it never falls back to replacement or a partial final-path copy. If neither
primitive is available, the archive remains unpublished. Default names include
a random suffix. The destination must match the staged inode and SHA-256 before
and after archive verification, and the returned digest is the captured staged
digest. Verification rejects unsafe or non-portable member names, non-regular
Unix entry types, control characters, case or Unicode aliases, and size-bound
failures.

Restore keeps one archive descriptor open and reads each data member once into
a sibling stage while computing the compared hash map. It then enumerates the
staged regular files, checks vault identity, and runs doctor before an atomic
publication. An existing target is accepted only when it is an empty real
directory and is restored if publication did not occur. Rename failures are
classified by source and target identity plus logical digest: unpublished work
is rolled back, while a visible committed restore with unconfirmed directory
durability is reported as degraded and left intact for explicit doctor review.
After a successful move, restore rechecks the public target's pinned directory
identity, vault identity, and logical digest around the full read-back; a target
swap is never reported as a successful restore.
An unpublished failed stage is also retained and named instead of recursively
deleted by a later pathname lookup. A changed stage pathname is an unknown
recovery state: neither the replacement path nor a displaced original is
removed. Doctor reports retained directories but never marks recursive deletion
as repairable.
Configuration and live Codex or Bridge state are outside this restore
transaction and require the documented stop-plus-setup activation step.

## Ownership and recovery

The records folder and backups are user data. Seld never deletes them during Codex
uninstall. The generated marketplace, plugin registration, managed instruction
block, Bridge receipt, and installation receipt are integration state. Removal
touches only expected content recorded as `gsv`-owned. Uninstall snapshots the
exact managed instruction bytes and verifies the marketplace digest before
provider mutation, keeps the marketplace manifest present while Codex removes
and verifies its registrations, then rechecks and isolates the local state. Any
incomplete stage retains the ownership receipt and executable for the exact
recovery path. A receipt-bound legacy state whose generated marketplace is
already absent gets a deterministic packaged removal scaffold so a
manifest-dependent Codex provider can finish cleanup. Reinstall and uninstall
atomically isolate a receipt-owned marketplace under a unique sibling path and
rescan it before replacement or quarantine. Lifecycle trees are never
recursively or manifest-deleted: inactive recovery catalogs retain their exact
paths and hashes while active integration removal completes independently. Any
added or changed entry remains a manual-review boundary. Legacy scaffold paths
and manifests are durable before materialization, then file- and
directory-synchronized before no-replace publication and provider access.
Duplicate target plugin or marketplace identities stop provider mutation as
ambiguous
state.

## Portability

The runtime uses the Python standard library before freezing, plus bundled
static Bridge assets. The UI bundles the open-source Nunito and Nunito Sans
variable fonts with their OFL 1.1 license; the Bridge serves them from the same
loopback origin with deterministic `font/woff2` metadata and retains system
font fallbacks. PyInstaller produces one executable per OS and architecture.
Vault records and backups are platform-neutral UTF-8/ZIP data and never
contain executable installation state.
