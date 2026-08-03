---
name: gsv-update
description: Review, approve, apply, or recover Seld's exact-revision source update. Use in an interactive ChatGPT task when Seld reports an available or interrupted update, or when the person asks to check or update Seld.
---

# Update Seld

Use this skill only in a current interactive ChatGPT task. Pulse may check for
an update and decide whether to surface it, but Pulse must never run `update
apply` or `update recover`.

## Read the local state

1. Run `gsv update status`. This reads installed provenance and cached update
   state without using the network.
2. If a fresh check is useful, run `gsv update check` once. Let its six-hour
   host cache decide whether network access is due. Use `--force` only when the
   person explicitly asks for an immediate interactive recheck; never use it
   from Pulse.
3. Treat `current`, `available`, `not_ready`, `unavailable`, `unsupported`, and
   `interrupted` as distinct states. Do not turn missing or failed evidence into
   an available update.

## Use the outcome approval

For `available`, read the current ChatGPT task UUID from the supported app/task
context. Stop if it is unavailable; never invent or reuse a task ID.

Show the person the complete 40-character `installed.sha`, `candidate.sha`, and
`check_revision`, plus the candidate's GitHub commit verification and complete
exact-head GitHub Actions check names and count. The required Seld release jobs
must all be present; an arbitrary green workflow is not enough. Explain that
Seld will create and verify a vault backup before staging or executing the exact
candidate with isolated host-state directories, stop only a verified live
Bridge, replace the managed `uv` tool environment, rerun setup and health
checks, and restore the previous environment if verification fails.

The person's request to update Seld or plain-language approval of this update
outcome covers the candidate identified by current status and every necessary
backup, apply, restart, verification, rollback, recovery, and repair action in
scope. SHA, revision, and task-reference values constrain execution; they are
not separate approval surfaces. Do not ask again because a later status read
changed an implementation value inside the same current update. Ask once only
if the target or consequence leaves the approved update outcome. Then run:

```text
gsv update apply --from-sha <installed.sha> --to-sha <candidate.sha> --expected-check-revision <check_revision> --approval-ref codex:<current-task-uuid>
```

If the command reports a conflict, re-read status and continue under the same
approval when it remains the same update outcome. Ask again only if the target
or consequence leaves that outcome. Never weaken or bypass the SHA, revision,
or task-reference binding.

## Verify or recover

After `installed`, use a fresh process to run `gsv update status`, `gsv doctor`,
and `gsv bridge status`. Report `installed_bridge_repair`, `rolled_back`, and
`repair_required` exactly; none is a successful healthy install.

For `interrupted`, read the transaction's exact token, from/to SHAs, phase, and
recovery command. If `gsv` is temporarily unavailable during the environment
swap, use `seld-recover --json update status`; that owner-only entrypoint is
published before the move and runs outside uv's managed `gsv` path. Explain what
the retained transaction shows, then run the reported command under the current
update-outcome approval. In the ordinary case it is:

```text
gsv update recover --token <transaction.token>
```

Recovery may finish the checked candidate or restore the preserved previous
environment. It never chooses a new target. Re-read status and health after it;
do not delete retained environments or retry over an unresolved transaction.

`repair_required` means automatic recovery has stopped. Preserve the retained
environments and follow the exact reported repair command in a current
interactive task. For `vault_changed_during_activation`, compare the current
local changes with the verified pre-update backup and explain the ambiguity.
When repair remains inside the approved update outcome, replace
`codex:<current-task-uuid>` in the reported command with this task's real UUID
and run it without another prompt. Ask again only if the target or consequence
leaves that outcome. Never invent the UUID or remove the expected digest. After
repair, the same transaction token re-proves the exact active environment
and either closes the transaction, restores the previous environment, or keeps
the repair evidence. A clean `installed` or `rolled_back` outcome is resolved
only when `recovery_command` is empty.
