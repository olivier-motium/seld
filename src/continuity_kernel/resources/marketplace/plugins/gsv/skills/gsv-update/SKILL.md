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

## Obtain exact approval

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

Ask for fresh approval of those exact values in this task. A standing approval,
another task, or approval given before the final status read does not count.
After approval, run exactly once:

```text
gsv update apply --from-sha <installed.sha> --to-sha <candidate.sha> --expected-check-revision <check_revision> --approval-ref codex:<current-task-uuid>
```

If the command reports a conflict, re-read status and ask again only if the
person still wants the now-current update. Never weaken or bypass the SHA,
revision, or task-reference binding.

## Verify or recover

After `installed`, use a fresh process to run `gsv update status`, `gsv doctor`,
and `gsv bridge status`. Report `installed_bridge_repair`, `rolled_back`, and
`repair_required` exactly; none is a successful healthy install.

For `interrupted`, read the transaction's exact token, from/to SHAs, phase, and
recovery command. If `gsv` is temporarily unavailable during the environment
swap, use `seld-recover --json update status`; that owner-only entrypoint is
published before the move and runs outside uv's managed `gsv` path. Explain what
the retained transaction shows, obtain fresh interactive approval, then run the
reported exact command at most once. In the ordinary case it is:

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
Only after the person approves those exact current bytes, replace
`codex:<current-task-uuid>` in the reported command with this task's real UUID
and run it once. Never invent the UUID, remove the expected digest, or treat the
approval as permission for a different update. After that approval or another
manual repair, the same transaction token re-proves the exact active environment
and either closes the transaction, restores the previous environment, or keeps
the repair evidence. A clean `installed` or `rolled_back` outcome is resolved
only when `recovery_command` is empty.
