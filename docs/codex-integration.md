# Codex integration evidence

## What GSV uses

The Bridge creates new-task links with this shape:

```text
codex://new?prompt=<encoded>&originUrl=<encoded>&path=<encoded>
```

Every link includes an encoded prompt, the GSV repository as `originUrl`, and
the resolved local vault as `path`. The Bridge distinguishes three actions:

- `new_mind_url` exists only when the Tasks section is completely inspected and
  contains zero records. Its prompt starts the first Mind-shaping hand.
- `new_hand_url` starts an established, generic GSV hand. Its prompt contains no
  task identifier, resume language, or first-run language.
- a task `codex_url` resumes one exact commitment and therefore exists only for
  nonterminal tasks. Done and dropped records never receive one.
- the guided Portfolio review start link opens a new Codex hand with the finite
  review contract. Once the agent authors that hand ID on the review-session
  Task, Bridge renders the exact hand link.

## Capability-gated same-hand review turns

Guided review answers use the documented non-interactive Codex command only
after the local capability check passes. Bridge never calls the experimental
app-server protocol. The transport is off by default. A bounded source-tree
canary enables it by setting `GSV_CODEX_TURN_TRANSPORT=1` on the Bridge process;
this is an evidence switch, not a consumer release claim.

1. `POST /api/v1/control` appends the exact bounded user answer through queue
   CAS, creates and schedules its durable turn receipt when capability checks
   pass, and returns the event receipt.
2. For a guided event only, the browser submits `{ "event_id": "<uuid>" }` to
   authenticated, same-Origin `POST /api/v1/review-turn` as an idempotent trigger
   and recovery path.
3. The coordinator validates that the event is the one pending receipt for the
   current review session and that its target revision still matches. For an
   existing hand it invokes `codex exec resume <exact-thread-id>`. START has no
   thread ID yet, so one initial `codex exec` establishes the real thread and a
   second bind-resume turn writes that exact ID to the review-session Task and
   dispositions the event. Both paths ignore user configuration, use a
   read-only shell sandbox with no approval prompts, and configure only the
   required vault-bound GSV MCP server for semantic writes. The foundation
   requests high reasoning effort but otherwise uses the user's Codex
   subscription defaults. A test operator may supply a validated model or
   service-tier override through `GSV_GUIDED_REVIEW_MODEL` and
   `GSV_GUIDED_REVIEW_SERVICE_TIER`; neither override is a consumer default or
   proof of provider support.
4. `GET /api/v1/review-turn?event_id=<uuid>` reports only that event. There is no
   transport-history list. The browser polls it until a terminal state, may show
   the bounded final answer and prepared decision sheet in memory, and then
   reloads canonical state.

For START, the JSONL reader publishes a validated `thread.started` UUID into the
content-free running receipt as soon as Codex emits it, before the first model
turn completes. Bridge may therefore expose the exact hand while the initial
review is still working. A conflicting or malformed emitted ID remains a
terminal no-replay failure; it never creates a replacement hand.

The prepared sheet is not canon. It is accepted only when it names exactly the
current subject set, uses current Task anchors, and is bound to the accepted
receipt's post-turn review-session revision. The browser may submit any
answered subset as one bounded intent. Codex applies each named row through its
own fresh Task or owning-WorkThread CAS and readback; there is no hidden batch
transaction, and unanswered rows mean nothing. A regenerated session revision
invalidates every pick even when the subject IDs are unchanged. Restart keeps
the canonical session, queue, and disposition but honestly loses the transient
sheet and its unsubmitted picks.

The Mind normally prepares 3-10 consequential interventions and never more than
25. A row appears only when a concrete decision with a supported durable Task,
WorkThread, or Portfolio effect is available now, at least two materially different durable choices exist, and a
changed fact, due point, contradiction, dependency, priority, bounded offer, or
grounded dissent makes attention useful. Bridge's Edit-batch request may name up
to 25 exact open Task IDs outside that threshold. The coordinator treats that
list only as navigation: it replaces the prepared subject set through session
CAS, writes no outcome semantics, and adds no coverage. Free-form guidance,
Pause, and End use the same exact authenticated receipt path.

If the opening complete audit finds no qualifying intervention, START does not
invent a row. Its binding turn may accept a terminal review-session result only
when the exact scoped Task is new or changed from the opening context, belongs
to `thread:life-portfolio-review`, that WorkThread has cleared focus, and the
Task retains no subject, pause, active hand, shadow ref, future-work field, or
new coverage. The final answer is then a compact by-reason account with no
`bridge-sheet`.

Triggering the same event is idempotent. A failure proven to occur before
delivery is `failed_safe` and may be retried deliberately. Once delivery may
have occurred, the receipt becomes `delivery_uncertain`; Bridge will not replay
the answer and directs the person to the exact hand to reconcile it. Recovery
re-binds the canonical record context and confirms that the exact event is
still pending before any spawn. Canonical drift or an existing disposition
fails the recovery closed; an unrelated later queue append does not strand an
otherwise unchanged pending event. A Bridge restart retains queue/disposition/canonical truth
but intentionally loses the transient final answer and prepared sheet.

If either START process has already emitted a hand but terminates before the
review-session Task or WorkThread is committed, the receipt retains that exact
hand, becomes `delivery_uncertain`, and offers same-hand reconciliation without
replaying or creating another hand. If no hand identifier can be recovered,
Bridge suppresses every new-hand fallback rather than risk a duplicate.

Automatic review continuation remains off unless every local capability is
proved. An unavailable Codex executable, missing auth, absent exact hand,
unsupported version/configuration, or inability to enforce the restricted MCP
profile leaves the event pending and keeps the exact-hand fallback visible.
Source inspection and a fake transport test do not prove a real Plus-account
or app-native provider path; those rows remain open in the release ledger.

The top bar, all-clear state, and terminal-task inspector use `new_hand_url`;
only the true first-run panel uses `new_mind_url`. The Bridge emits none of
these links until Codex discovery succeeds and both the managed instructions
and GSV plugin are installed. The README install link omits `path` because it
opens a new task before a checkout or vault path can be assumed.

GSV does not use a legacy launch-route fallback.

## Resident Pulse task

The resident Pulse uses the ordinary app-native Codex task and skill surfaces,
not the guided-review subprocess transport and not the unexposed deterministic
`PulseController` or scheduler planner. Registration creates exactly one
dedicated Codex task, binds its real UUID to structural
`task:resident-pulse`, and—after a manual proof and fresh approval—targets that
same task with one app `heartbeat` on the ten-minute target cadence.

Each wake loads `$gsv-pulse`. The model freezes a bounded context and selected
source window, makes semantic judgments, and writes through the existing GSV
MCP CAS/readback tools. Mechanical code can validate structural markers,
revisions, bounds, content-free receipts, and no-replay behavior. The skill
performs the current-task comparison; host-observed correlation and a
connector-write privacy screen remain unproven gates. Mechanical code cannot
decide what a message means, create work from age alone, rank the Portfolio, or author NOW.
The structural Pulse task is excluded from Bridge commitment counts and guided
review. A source-tree package test does not prove registration or a natural
provider-backed wake; those remain open in the release ledger.

## Version-bounded verification

Observed read-only on 2026-07-22:

- Installed app: `/Applications/ChatGPT.app`
- App version/build: `26.715.72359` / `5718`
- Bundle identifier: `com.openai.codex`
- Signing team: `2DC432GLL2` (OpenAI)
- Registered URL scheme: `codex`
- Extracted `app.asar` SHA-256:
  `6c6528eb1e8450cdc506a59586f8caffe87576e200977e2a11bdea0cecf1c718`

The parser in that exact installed bundle routes host `new` to its new-task
parser and reads `prompt`, `originUrl`, and `path`. It rejects the route only
when all three are absent. This validates GSV's link shape for that installed
version without launching or submitting a task.

This is not an evergreen Codex API guarantee. Recheck the installed bundle or
an official supported contract before claiming compatibility with a later app
version. The copyable installation instruction in the README remains usable
without the custom scheme.

`tests/test_bridge.py` verifies URL encoding, action qualification, and
round-tripping for each prompt, origin URL, and local path.
