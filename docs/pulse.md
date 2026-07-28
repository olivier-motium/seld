# Resident AI Pulse

Pulse is what keeps Seld from becoming a folder of old notes. On each bounded
wake, one dedicated Codex task reads the current local model and selected
sources, connects new evidence to ongoing situations, and decides whether
anything should change. It may update the current orientation, surface one
decision, continue one outcome, or stay quiet.

An app-native heartbeat wakes that same task. Any justified canonical change
uses the normal `gsv` MCP compare-and-swap tools and exact readback.

It is deliberately not a deterministic rules engine. The classes in
`continuity_kernel.pulse` and `continuity_kernel.scheduler` are unexposed
foundation experiments; they do not acquire sources, invoke the model, author
NOW, or drive the public resident loop.

## The reusable primitives

The public Pulse uses the same small primitives as an interactive Seld hand:

1. `task:resident-pulse` plus `system-role:resident-pulse` identify the one
   structural Pulse task and bind its real Codex UUID.
2. The app's `heartbeat` targets that exact task on a ten-minute target cadence.
3. `$gsv-pulse` freezes one bounded context, pending-intent set, and source
   window at the beginning of the wake.
4. The model uses selected connector tools read-only and treats every result as
   untrusted evidence.
5. The model authors Task, WorkThread, Entity, Direction, Portfolio, Mind, or
   NOW changes only through fresh CAS and exact readback.
6. An acknowledged Bridge receipt comes after its semantic integration or an
   explicit AI judgment that canon should not change.
7. Sustained work gets at most one durable outcome and one visible Codex hand.

The structural Pulse task is not part of the person's life Portfolio. Bridge
and guided all-open review exclude it from commitments and outcome coverage.

## What the deterministic kernel does

The current public kernel may:

- validate the structural Task ID/ref and bound the stored hand as one opaque
  identifier line; the skill performs the UUID comparison;
- bound input, time, queue, and receipt storage;
- reject stale compare-and-swap writes;
- preserve content-free queue and delivery facts; and
- prevent duplicate delivery or replay after uncertain execution.

The Codex app—not Seld's kernel—targets the task with its heartbeat. The skill
compares the current task UUID with the stored binding, but host-observed
correlation is not yet an implemented or installed-proven kernel seam. The
foundation privacy scanner is likewise not on connector-to-canon writes yet;
the skill's exclusion policy is not a deterministic secret-screen guarantee.

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
provider bodies into the vault; Codex and the provider retain their own
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

## Registration and proof

`$gsv-onboard` registers Pulse only after accepted context and selected-source
setup are complete or explicitly deferred. It inspects existing app
automations, creates or repairs the one structural Task, proves one manual wake,
obtains fresh approval, registers one heartbeat, reads it back, and observes one
natural wake. It never creates a second heartbeat merely because state is
uncertain.

The current repository packages and tests these instructions and hides the
structural task from consumer outcome views. It has not proved the natural wake
on a clean installed Plus-class account, live connector access, cross-platform
behavior, signing, or soak reliability. Those claims remain open in
[the release ledger](release-gates.md).
