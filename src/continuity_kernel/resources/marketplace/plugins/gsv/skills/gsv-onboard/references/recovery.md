# Recovery

Recover only from state exposed by a supported public surface. Never rebuild an
interrupted onboarding journey from a transcript or edit readiness, queue, or
disposition files by hand.

## Interrupted onboarding

Read accepted Mind context, the current Pulse binding, and the Bridge request
queue. Continue in the same onboarding task when its exact task UUID is known.
If ownership is ambiguous, ask before taking over and never create a duplicate
Pulse or silently replace another active hand.

## Stale or failed source

Preserve the last successful coverage boundary, open one fresh ChatGPT task,
and repeat the selected source's identity plus bounded-read check. Let the
person reauthenticate only in the provider-owned app when it requests it. A
failed recheck is current evidence of stale or unavailable coverage, not an
empty source.

For a same-host migration, read `local-source staged-status`, then use
`local-source adopt-staged` only with its exact migration and source revisions
and disposition `adopt_verified_prefix`. Seld keeps the aggregate cursor opaque
and accepts the staged checkpoint only when the live database has the same
schema and generation and the old aggregate prefix is still present exactly.
A mismatch leaves the new checkpoint absent; do not fall back to a forward
baseline because that could skip the migration gap.

If a supported local adapter reports that its database was deliberately
replaced, do not baseline automatically or edit its checkpoint. Read the exact
checkpoint digest and sequence from `local-source status`. Only after the user
chooses to discard the old store horizon may you call `local-source rebaseline`
with those exact values and disposition `forward_only_reset`. Seld archives the
old checkpoint and any pending delivery, then marks the source as needing a
fresh proof. A stale CAS conflict means reload; an unavailable or unchanged
store means stop.

## Bridge operation queue

Use `gsv operation list` or `gsv_operation_list` to reload the current queue,
pending intents, durable dispositions, logical vault ID, and both CAS revisions.
Use the supported accept or reject operation only against those three exact
binding tokens. A stale conflict means reload; do not guess, replay, reorder, or
manually edit state.

Accept and reject acknowledge the intent only. They do not execute it,
authorize an external action, or mutate semantic records. The
`operation archive-closed` CLI and `gsv_operation_archive_closed` MCP tool are
operator-only bounded-capacity recovery surfaces; do not present them as an
ordinary onboarding step.

For damaged existing continuity-kernel state, stop the Bridge before following
the repository's verified backup-and-restore procedure. Restore only a validated
backup. Reconnect task-local source tools and re-register the Pulse on a new
host; do not copy machine-specific task or automation identities.

Report the last successful coverage, exact failed check, safe state now in
effect, and next human decision.
