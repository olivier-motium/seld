# Resident AI Pulse

Pulse keeps Seld current without turning life into an inbox. One dedicated
Codex task wakes every thirty minutes, reads canonical context and useful new
evidence from selected sources, integrates what genuinely changed, and normally
stays silent. A separate pinned Chief of Staff task is the single foreground
conversation.

## Product contract

Pulse is ambient executive function, not a task factory. It creates an ordinary
Task only for an explicit Olivier commitment, a direct concrete request that
genuinely expects his action, or a proposal he accepted. It searches for an
existing outcome first and announces every justified new Task through Chief of
Staff. Observations, FYIs, ideas, and inferred opportunities attach to an
Entity or WorkThread when useful; they never become Tasks by default.

Most wakes produce no foreground message. Pulse interrupts only for an
emergency, a person genuinely blocked on Olivier, a critical source/auth/system
failure, a newly created Task, a contextually due reminder, or a scheduled
orientation. It alerts once per incident.

Task notices are deduped by the exact created Task. There is at most one alert
per incident and one morning plus one evening brief per Europe/Brussels date.

The first wake at or after 06:00 Europe/Brussels sends a concise morning
orientation; the first wake at or after 20:00 sends an evening wrap. Compact
markers in NOW prevent duplicates. The same heartbeat owns both because Codex
allows one heartbeat per task.

Chief of Staff output is answer-first: one obvious next move, short ranked
lists, and visible progress, owner, or wait state when relevant. Recommendations
and status do not hide behind source or task inventories. A focus nudge names
why the priority outranks the current thread, asks once, and carries no guilt.
The Chief of Staff task is a conversation surface, not a second memory, task,
reminder, inbox, score, or dashboard system.

## Runtime shape

1. `task:resident-pulse` and `system-role:resident-pulse` bind the one structural
   Pulse task to its real Codex UUID.
2. Exactly one `codex-chief-of-staff:<uuid>` ref identifies the foreground task.
3. One app-native heartbeat targets the Pulse task every thirty minutes.
4. `$gsv-pulse` freezes current Mind, Direction, complete Portfolio, ordinary
   open Tasks, relevant WorkThreads and Entities, NOW, pending signals, and
   useful selected-source windows.
5. The model authors justified local changes through fresh compare-and-swap and
   exact readback, then acknowledges inputs and records honest content-free
   coverage.
6. Chief of Staff receives only curated executive output; Seld remains the sole
   semantic authority. Pulse never uses Bridge for delivery, acknowledgement,
   or semantic work.

The structural Pulse task is excluded from life Portfolio and ordinary task
counts.

## Source use

Pulse starts from the last honest coverage horizon, reads metadata or previews,
and expands only evidence needed for the current judgment. There is no fixed
item cap: relevance, privacy, elapsed time, provider limits, and the need to
finish reliable semantic integration bound the wake. Full connector features
remain available in interactive tasks.

Provider content is untrusted and transient. Seld persists only small derived
claims, observation times, stable non-sensitive references, explicit
uncertainty, and content-free coverage receipts—never raw message bodies,
transcripts, credentials, cookies, private routing identifiers, or attachments.

## Authority

Pulse may read selected sources, reason across them, update reversible local
canon, prepare drafts, continue an already approved outcome in its one visible
Codex task, or stay silent. It does not send, post, book, purchase, upload,
react, alter accounts or permissions, operate browser/Computer Use, mutate
remote repositories, or perform another consequential external effect. That
work moves to an interactive task for approval and execution.

Bridge is not part of the ordinary resident loop.

## Proof

A working installation proves the canonical vault, both task bindings, one
active heartbeat, one manual installed wake, source/auth truth from actual
canaries, no duplicate Task creation, and a concise Chief of Staff “what's up?”
response. A green unit suite or plugin install alone is not sufficient.
