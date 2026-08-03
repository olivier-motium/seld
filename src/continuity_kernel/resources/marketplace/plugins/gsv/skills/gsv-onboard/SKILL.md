---
name: gsv-onboard
description: Guide first setup and source repair for Seld by gathering accepted context, verifying selected reads, preparing the initial Mind, and registering Pulse when requested.
---

# Seld onboarding

Learn the person's current situation, connect the relevant sources, prove one
bounded read from each selected source in the current ChatGPT task, and prepare
the first useful orientation.

The AI owns interviewing, source selection, synthesis, and judgment. The local
`gsv` surfaces own only durable records, compare-and-swap writes, bounded
receipts, recovery, and no-replay delivery.

## Begin with the person

Before synthesizing context or changing `MIND.md`, call `gsv_document_show` for
the current Mind and `gsv_resident_context_status`. When imported resident
guidance is present, read it with `gsv_resident_guidance_show`. Treat the
existing Mind and the user-context, preferences, provenance, corrections, and
uncertainty in that guidance as retained context. The installed Seld tools
remain authoritative for mechanics, but onboarding must augment this retained
context rather than replace it with a cleaner new summary.

Read [context intake](references/context-intake.md). Ask one compact, skippable
batch:

- What would make Seld worth having this month?
- What does this week actually look like?
- What are you trying to change?
- Who or what can you not afford to drop?
- Which projects, obligations, routines, waiting-fors, and constraints matter?
- What may Seld read, retain, interrupt about, or prepare for approval?

Accept pasted notes and user-selected local files. Never ask for passwords,
tokens, financial credentials, second factors, or unnecessary raw transcripts.
Reflect a provisional picture immediately. Show what would be added, corrected,
or left unchanged. Persist only the context the person accepts, through
`gsv_document_update` against the exact current `MIND.md` revision, then read it
back. Because that tool replaces the complete document, the proposed content
must preserve every retained statement that the person did not explicitly
correct or remove, including its provenance and uncertainty. If a lossless
merge is ambiguous or would exceed the supported document boundary, stop
before the write and ask one focused question; never trade existing context for
a successful onboarding step.

## Connect where life happens

Read [the source catalog](references/source-catalog.md), then recommend sources
from the accepted context rather than presenting a generic checklist. The
person chooses the sources and accounts.

When `local_files` is selected, read [local file access](references/local-files.md).
Logical source selection does not itself grant a directory. Use the current
outcome-scoped approval for every necessary host-local root grant inside the
selected `local_files` scope; do not ask again per root unless the selected
source, path consequence, or outcome changes.

Use one setup wave:

1. Call `gsv_source_list`. Treat `gsv` — Seld on this computer — as source
   zero and keep it selected whenever the person is onboarding this vault. If
   the Seld MCP tools are missing in the current task, use the installed local
   `gsv --vault <exact-root>` CLI against the same vault; absence from one task
   is not evidence that the Mind was uninstalled or lost. Then show one
   combined checklist for the selected ChatGPT apps, custom MCP apps, local
   read tools, and required host-OS permissions.
2. Let the person complete OAuth, credentials, 2FA, legal terms, administrator
   approval, and OS privacy prompts personally. A host-owned app keeps its own
   non-portable authentication. For Gmail, Google Calendar, Google Drive,
   Outlook mail, Outlook Calendar, or Slack, first run `gsv connectors
   readiness` and `gsv connectors list`. If the exact build reports a missing
   or invalid public registration, sign-in is unavailable and nothing is saved;
   do not send an ordinary user to a provider developer console. Otherwise use
   `gsv connectors connect <logical-source> --access read|full --alias
   '<recognizable label>'`. The default browser opens the provider page; add
   `--browser firefox` only when requested. With `--no-browser`, the person must
   open the printed URL on the same computer while the command remains running.
   Seld waits on a loopback callback, verifies the provider identity, shows the
   exact account in human terms, and asks `Use this account? [y/N]` before
   publishing. The default is no. A Read connection remains ready until a Full
   upgrade for the same account is verified and published. Discord uses `gsv
   connectors connect discord --access full --alias '<recognizable label>'`;
   its bot token is accepted only through hidden interactive input and is never
   a command argument. The agent may explain and wait for redacted status, but
   it must not consent, enter or reuse credentials or second factors, or change
   an account, application, access, permission, or security setting. Never copy
   or reuse a ChatGPT, OpenAI, browser, Codex, OpenCode, or Open Interpreter
   session.
3. After the person confirms the set, call `gsv_source_select` against the exact
   returned source-state revision. This stores only the selection and purges
   coverage for anything deselected; it does not claim a provider is live.
4. Open one fresh ChatGPT task after the apps and plugins are enabled.
5. Discover the actual read-only tools exposed in that task. Map them to the
   logical recipe capabilities returned by `gsv_source_list`; never depend on
   a display name or a repository file that is unavailable in the installed
   task.
6. For each selected source, confirm the account or workspace in human terms,
   then perform one bounded recent read. An explicitly empty successful read is
   valid. A wrong identity, missing tool, auth failure, or policy block is not.
7. Fresh-read `gsv_source_list`, then call `gsv_source_record` against that exact
   source-state revision. Pass the transient account and tool bindings, result,
   coverage horizon, completeness, and only stable references needed for
   correlation. The core hashes bindings, cursors, and references before they
   enter the vault. For a failure, record no claimed coverage and use one of
   the error classifications advertised by `gsv_source_record`. Never put
   provider text or an identifier in that field. Read the updated source state
   back.
8. Record retention, interruption, and action boundaries in accepted Mind
   context. Do not copy raw provider bodies.

For selected Apple Messages or WhatsApp, use Seld's host-local delivery
surface. Before any baseline, call `gsv_local_source_staged_status`. If this
vault contains a `pending` or `completion_pending` checkpoint for the selected
source, call `gsv_local_source_adopt_staged` with the returned exact migration
and source revisions and disposition `adopt_verified_prefix`. Re-read until it
reports `adopted`, then poll and process the during-cutover delta. A mismatch,
`host_conflict`, `needs_reproof`, or unavailable store stops that source; never
fall back to a forward baseline because doing so could skip the migration gap.

Only when staged status proves there is no checkpoint for that source and the
host has no earlier Seld delivery receipt, confirm the account and permission
boundary and call `gsv_local_source_baseline` once to begin forward-only. Then
use `gsv_local_source_poll` for the canary. The poll does not advance its
checkpoint. After the model has written and read back its explicit semantic
disposition, call `gsv_local_source_acknowledge` with the exact token,
source-state revision, result references, actor, and confirmed account binding.
Never import existing message history merely to establish a baseline.

For selected Discord, read [Discord setup](references/providers/discord.md)
before asking the person to enable anything. Discord is bot-only: never accept,
copy, or use a normal-user token. The person creates and authorizes the bot,
then runs `gsv connectors connect discord --access full --alias '<label>'` and
enters the token through hidden local input. The onboarding agent
may explain those steps and read redacted status, but it must not consent, enter
or reuse credentials, change account or application settings, or alter
permissions. The standard `gsv_connectors` MCP server exposes Discord's typed
bot read/write operations; it never accepts a user token. This build does not
package or recommend a Discord Pulse companion. Leave Discord unselected for
Pulse unless an exact separate companion runtime, bot binding, and host-private
channel allowlist have been independently audited, installed, and verified.

For a selected Seld-managed Google, Microsoft, or Slack source, read its
provider note and inspect `gsv connectors status` or `gsv_connection_list`,
which expose only redacted portable state. Each logical source has its own
least-authority Read and Full grants; connecting Gmail does not silently grant
Calendar or Drive, and connecting Outlook mail does not silently grant
Calendar. Use `gsv_connector_source_read` only for the narrow, bounded Pulse
verification described here. Standard setup registers the isolated
`gsv_connectors` MCP server, which exposes exactly one read and one write tool
per logical connector. Read access permits the typed read catalog. Full access
adds user-content create, update, send/share, recoverable delete, and separately
classified permanent delete operations. Calls still fail closed against the
exact connection, granted scopes, provider routes, and closed input schema. Outward,
destructive, and permanent operations return a bound preview and short-lived
confirmation token before execution; permanent purge is a distinct operation
and permission. A wrong identity is a setup gap, never permission to fall back
to an ambient token or host-owned session.

Seld supports any user-enabled ChatGPT app or MCP tool that can satisfy the
same bounded read contract. The catalog supplies first-class recipes for
ChatGPT activity, Gmail, Google Calendar, Drive and Sheets, Outlook mail and
calendar, Slack, Teams, GitHub, Asana, Atlassian, Box, Figma, Notion,
SharePoint, local files, Apple Messages, WhatsApp, Shopify, Instagram, and
optional screen context. The read-only Pulse lane and the interactive CRUD lane
are intentionally separate: broader connector capabilities never widen what
Pulse may read or authorize.

A source may inform the current synthesis after a successful read and fresh,
content-free coverage receipt. Pulse rechecks its availability and freshness
when relevant. A missing tool remains an explicit coverage gap, while semantic
interpretation stays with the model.

## Use the Bridge request loop precisely

Bridge setup choices, approvals, corrections, and undo requests are append-only
receipts, not semantic truth or action authority.

- Read them through `gsv operation list` or `gsv_operation_list`.
- Accept or reject only the exact event against the returned queue,
  disposition, and vault revisions.
- Integrate any justified meaning through native record CAS and readback before
  acknowledging the receipt.
- Reload on stale CAS. Never edit, reorder, replay, or delete queue files.

Acceptance acknowledges delivery. It does not send a message, change a
provider, or approve an external action.

## Register one resident Pulse

After the first context and source wave, follow the Pulse
[registration contract](../gsv-pulse/references/registration.md). Bind
`task:resident-pulse` to one real ChatGPT task, prove one manual bounded wake,
inspect existing automations, and create at most one app-native heartbeat for
that exact task after fresh user approval.

Observe one natural wake. The heartbeat only wakes the AI skill; the model
reads selected sources and authors every judgment. The Pulse policy forbids
Computer Use. Seld's MCP server does not expose it, although a separately
installed Computer Use plugin may still be visible in the ordinary ChatGPT
task; explain that host boundary before registration.

On macOS, install and verify the separate mechanical sense sweep before
presenting autonomy as ready. It may wake on a fixed cadence and append
content-free source-due or WorkThread-recheck evidence. It never runs semantic
recall, reads provider bodies, or decides meaning. Optional QMD setup and
refresh remain explicit recall operations outside this five-second mechanical
path. Use the supported scheduler plan, install, status, and asynchronous canary
surfaces; require the exact owned receipt revision for mutation and refuse a
foreign job or plist.

On Windows, Codex Automations can own the intelligent Pulse. Public Seld does
not yet ship the Windows prebuilt, native Bridge, or OS mechanical scheduler,
so report that mechanical coverage gap instead of pretending the macOS sweep is
installed. On every host, the AI Pulse remains the only layer that interprets
delivered facts.

## Finish with a useful first view

Run one bounded manual Pulse or equivalent onboarding synthesis. Read accepted
Mind context and the successfully verified source windows, then produce:

- a provisional Direction;
- the few current outcomes and waiting-fors that matter;
- named people, projects, and situations only where evidence supports them;
- honest source coverage and unknowns; and
- the first small Rundown, if a real decision needs the person.

Every proposed outcome or claim links back to a source label, observation time,
and stable non-sensitive reference when available. If coverage is thin, show a
useful orientation without inventing certainty.

Read [Computer Use](references/computer-use.md) only when the person asks for
interactive setup help, and [recovery](references/recovery.md) when setup was
interrupted or a source changed identity.
