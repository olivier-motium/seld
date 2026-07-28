# Resident AI Pulse

Pulse is what keeps Seld from becoming a folder of old notes. On each bounded
wake, one dedicated ChatGPT task reads the current local model and selected
sources, connects new evidence to ongoing situations, and decides whether
anything should change. It may update the current orientation, surface one
decision, continue one outcome, or stay quiet.

An app-native heartbeat wakes that same task. Any justified canonical change
uses the normal `gsv` MCP compare-and-swap tools and exact readback.

It is deliberately not a deterministic rules engine. The classes in
`continuity_kernel.pulse` and `continuity_kernel.scheduler` are bounded safety
helpers; they do not acquire sources, invoke the model, author NOW, or drive
the resident reasoning loop.

## The reusable primitives

The public Pulse uses the same small primitives as an interactive Seld hand:

1. `task:resident-pulse` plus `system-role:resident-pulse` identify the one
   structural Pulse task and bind its real ChatGPT task UUID.
2. The app's `heartbeat` targets that exact task on a ten-minute target cadence.
3. `$gsv-pulse` freezes one bounded context, pending-intent set, and source
   window at the beginning of the wake.
4. The model uses selected connector tools read-only and treats every result as
   untrusted evidence.
5. The model authors Task, WorkThread, Entity, Direction, Portfolio, Mind, or
   NOW changes only through fresh CAS and exact readback.
6. An acknowledged Bridge receipt comes after its semantic integration or an
   explicit AI judgment that canon should not change.
7. Sustained work gets at most one durable outcome and one visible ChatGPT task.

The structural Pulse task is not part of the person's life Portfolio. Bridge
and guided all-open review exclude it from commitments and outcome coverage.

## What the local kernel does

The local kernel:

- validates the structural Task ID/ref and bounds the stored hand as one opaque
  identifier line; the skill performs the UUID comparison;
- bounds input, time, queue, and receipt storage;
- rejects stale compare-and-swap writes;
- preserves content-free queue and delivery facts;
- binds successful source coverage to the local host and recipe version,
  rejects impossible timelines and coverage regressions, and keeps failed-read
  lineage; and
- prevents duplicate delivery or replay after uncertain execution.

The ChatGPT app targets the task with its heartbeat, and the skill compares the
current task UUID with the stored binding. ChatGPT performs provider reads and
authors their content-free receipts; Seld does not claim that this is an
independent provider certification. The source-recording surfaces have no field
for a provider body, and semantic claims enter the local record only through
the model's normal bounded CAS writes.

It may not decide meaning, urgency, identity, ownership, relationships,
completion, Portfolio stance, memory worthiness, or the next action. A changed
file, due timestamp, old message, source failure, or retrieval score is evidence
for the model, not a semantic rule.

## One bounded wake

The exact resident task reads Mind, Direction, the complete Portfolio, open
Tasks, relevant WorkThreads and Entities, NOW, and the current Bridge operation
queue. It freezes that evidence. New inputs wait for the next wake.

For each selected due source, the model performs at most one bounded recent
read, normally no more than five high-signal items. Seld does not copy raw
provider bodies into the vault; ChatGPT and the provider retain their own
processing and retention boundaries. Only a small derived claim, stable
non-sensitive reference, observation time, coverage boundary, and uncertainty
may enter Seld's canonical records.

At seven elapsed minutes the task starts no new acquisition. At eight minutes
it stops acquiring and completes only the smallest honest judgment and readback
already in hand. Only this exact Pulse task writes NOW during autonomous
operation.

## Unattended boundary

A Pulse may update reversible local canon, remain silent, surface an
intervention, or dispatch one visible sustained-work hand. It may not operate a
browser or desktop, use Computer Use, send or mutate provider content, change
accounts or permissions, push or release code, or take another consequential
external action. Those require a current interactive task and fresh approval.

Seld enforces the part of this boundary that it owns. Its MCP server advertises
only local record, receipt, queue, and backup tools, all with a closed-world
annotation; it contains no provider-action, browser, screen-control, or Computer
Use method. Provider reads come from separately installed ChatGPT plugins or
custom MCP servers.

The current ChatGPT heartbeat surface targets an ordinary task and does not
offer Seld a task-scoped tool allowlist or denylist. A separately installed
plugin can therefore expose write tools or Computer Use to that task even though
the Pulse policy forbids calling them. Installations that require host-enforced
exclusion must provide every selected source through read-only MCP tools and
keep broader provider and Computer Use tools out of the Pulse task, or wait for
a host tool profile that can enforce the same boundary.

## Registration and proof

`$gsv-onboard` registers Pulse only after accepted context and selected-source
setup are complete or explicitly deferred. It inspects existing app
automations, creates or repairs the one structural Task, proves one manual wake,
obtains fresh approval, registers one heartbeat, reads it back, and observes one
natural wake. It never creates a second heartbeat merely because state is
uncertain.

The repository packages and tests these instructions and hides the structural
task from consumer outcome views. Exact natural-wake cadence, multi-day
reliability, signing, and additional-platform claims use the separate evidence
named in [the claim ledger](release-gates.md).
