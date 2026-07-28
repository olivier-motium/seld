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
Reflect a provisional picture immediately. Persist only the context the person
accepts, through `gsv_document_update` against the exact current `MIND.md`
revision, then read it back.

## Connect where life happens

Read [the source catalog](references/source-catalog.md), then recommend sources
from the accepted context rather than presenting a generic checklist. The
person chooses the sources and accounts.

When `local_files` is selected, read [local file access](references/local-files.md).
Logical source selection does not grant a directory; create an exact host-local
root grant only after showing the path and receiving fresh approval.

Use one setup wave:

1. Call `gsv_source_list`, then show one combined checklist for the selected
   ChatGPT apps, custom MCP apps, local read tools, and required macOS
   permissions.
2. Let the person complete OAuth, credentials, 2FA, legal terms, administrator
   approval, and OS privacy prompts personally.
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

Seld supports any user-enabled ChatGPT app or MCP tool that can satisfy the
same bounded read contract. The catalog supplies first-class recipes for
ChatGPT activity, Gmail, Google Calendar, Drive and Sheets, Outlook mail and
calendar, Slack, Teams, GitHub, Asana, Atlassian, Box, Figma, Notion,
SharePoint, local files, Apple Messages, WhatsApp, Shopify, Instagram, and
optional screen context.

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
