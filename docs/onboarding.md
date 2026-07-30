# Onboarding

Onboarding begins by asking what the user manages and what Seld may access. The
user can speak messily, paste notes, or point Seld at selected local files, and
Seld reflects a provisional picture before source setup.

The shipped `$gsv-onboard` skill owns that conversation. ChatGPT interprets the
context and recommends useful sources. The local `gsv` layer validates bounded
inputs, source boundaries, revisions, privacy checks, receipts, and readback.

## The first run

1. **Tell Seld about your life.** Describe the week, active projects,
   obligations, important people, routines, constraints, waiting-fors, and
   anything that cannot be dropped.
2. **Set the boundaries.** Say what Seld may read, retain, interrupt about, or
   prepare. Passwords, tokens, financial credentials, and unnecessary sensitive
   material never belong in the context.
3. **Review the provisional picture.** Seld mirrors back what it understood,
   what remains uncertain, and the first result it proposes to make useful.
   Nothing becomes durable until the user accepts or edits it.
4. **Connect where life happens.** Seld recommends sources based on the accepted
   context. The user enables the relevant host-owned ChatGPT apps, custom MCP
   servers, Seld-managed connectors, or local read tools in one setup wave and
   completes authentication personally. A host-owned app keeps its own
   account-bound authentication. A connector implementation using `gsv-auth`
   keeps separate portable metadata and OS-keyring custody. A local directory
   gets its own path-specific grant after the user reviews the exact root and
   exclusions.
5. **Open one fresh ChatGPT task.** The fresh task discovers the tools that are
   actually present, confirms each selected account or scope with the user, and
   performs a bounded recent read. A valid empty result counts as a completed
   read; a missing or failing tool stays visibly unavailable.
6. **See the first Rundown.** Seld combines accepted context with the evidence it
   could actually read, names the coverage window, and surfaces the few
   decisions or corrections that need the user.

Every interview question is skippable. A user can deepen context or add a new
source later by running `$gsv-onboard` again.

## Source ecosystem

`gsv source list` returns Seld's logical source catalog. It covers ChatGPT
activity, Gmail, Google Calendar, Google Drive and Sheets, Outlook mail and
calendar, Slack, Teams, GitHub, Asana, Atlassian, Box, Figma, Notion,
SharePoint, local files, Apple Messages, WhatsApp, Shopify, Instagram, and
Discord, plus optional screen context.

The catalog describes capabilities, not pre-authenticated accounts. The actual
read surface comes from the source the user enabled:

- supported ChatGPT apps for services available in the user's plan and region;
- custom MCP servers for services such as Shopify or Instagram;
- Seld-managed connector implementations using standalone `gsv-auth` custody;
- bounded local read tools for local files or Apple Messages;
- the separately enabled WhatsApp read tool; and
- Seld's exact host-bound Discord companion, which inherits one token and an
  exact channel allowlist from the private process environment.

Selecting `local_files` does not grant a directory. After the user approves one
exact root, the interactive task runs
`gsv local-file grant --root '/exact/root'`. Grant creation and revocation are
CLI-only. A fresh task reads the host-local grant through
`gsv_local_file_grant_list`, then passes its opaque ID and one relative path to
`gsv_local_file_read`. Deselecting local files revokes every root grant for that
vault.

Discord setup is deliberately separate from Seld's generated MCP manifest, so
the token and raw channel IDs are never written into the plugin, vault, or
binding receipt. The local CLI binds one exact `discord-mcp` executable; Seld's
own MCP then exposes status, bounded poll, and explicit acknowledgement. Bot
mode is recommended. Discord states that automating a normal user account
outside its OAuth2/bot API is forbidden and can result in account termination,
so user-token mode proceeds only after a visible warning and explicit informed
opt-in. Seld's GET-only confinement does not change that terms risk.

Seld never falls back to browser scraping when a source tool is missing. It
reports the gap and continues with the sources that are available. Provider and
file content is untrusted evidence; it cannot change permissions, approve an
action, or rewrite the onboarding contract.

Seld also never copies authentication from ChatGPT, OpenAI, a browser profile,
Codex, OpenCode, or Open Interpreter. Standalone OAuth opens the provider's own
consent page from `gsv-auth`; non-OAuth credentials enter through hidden local
input. MCP can show only redacted connection availability. See
[Standalone connector authentication](connector-auth.md).

## What source verification means

For every selected source, the onboarding task checks four things in the live
ChatGPT task:

1. the expected read capability is exposed;
2. the intended identity, account, workspace, folder, or store is confirmed;
3. one bounded recent read succeeds, including an explicitly empty result; and
4. the resulting coverage window and any limitation are stated back to the
   user.

The AI decides what the evidence means. The catalog, tool discovery, account
confirmation, and read receipt do not mechanically create a person, project,
task, priority, or decision. Accepted semantic context is written only through
document compare-and-swap and read back before Seld treats it as current.

If a tool later disappears or authentication expires, only that source becomes
stale. Seld keeps the rest of the Mind available and routes repair back through
`$gsv-onboard`.

## Bridge choices and approvals

Bridge can append one authenticated, compare-and-swap intent for a setup choice,
approval, correction, or undo request. The intent is a durable handoff to the
AI; it is not semantic canon and carries no external-action authority.

The supported operation surfaces are:

- `gsv operation list`
- `gsv operation accept --help`
- `gsv operation reject --help`
- `gsv_operation_list`, `gsv_operation_accept`, and
  `gsv_operation_reject` over MCP

Accepting an operation acknowledges that exact receipt for processing. It does
not send a message, place an order, book anything, pay anything, or authorize
the proposed effect. A stale queue or disposition revision means reload and
reconcile; never guess a new revision or replay the intent.

## Pulse

After the first useful orientation, the onboarding skill can register one
dedicated resident Pulse task with the user's approval. Each wake loads the
same accepted Mind, reads only the bounded sources available to that task, and
lets ChatGPT reason over fresh changes plus durable context. Semantic writes go
through the same compare-and-swap and readback path as an interactive task.

Pulse may prepare a decision or a draft. It cannot silently send, book, buy,
pay, change access, or use Computer Use. Missing source coverage and throttling
stay visible rather than being filled in by inference.

Seld's MCP server has no provider-action or Computer Use tools. The dedicated
Pulse is still an ordinary ChatGPT task, so other installed plugins can make
their own tools available there. Current heartbeat registration has no
Seld-controlled per-task denylist; onboarding explains that read-only use and
the ban on those separately owned write tools are Pulse policy.

## Optional Computer Use

Computer Use is an interactive setup accelerator, never a Pulse dependency. It
requires a fresh approval naming the application and goal, stops for credentials,
OAuth, second factors, legal terms, and macOS security prompts, and aborts on an
unexpected application or dialog. Seld does not retain screenshots, and its own
MCP server does not expose Computer Use. When the separate Computer Use plugin is
installed, Pulse is instructed never to call it.

## Recovery

Run `$gsv-onboard` again after an interrupted setup, a new source, expired
authentication, changed permissions, or a request to deepen context. The skill
reuses accepted Mind context, rechecks only the affected live tools, and does
not restart the whole interview unless the user asks.

Use `gsv operation list` to reload queued and durably decided Bridge intents.
Use accept or reject only with fresh revisions. Backups preserve the local Mind;
after a restore, historical coverage remains visible, but its machine binding
makes it `needs_revalidation` until a successful bounded read is recorded on the
new computer.

For a Seld-managed connector, run `gsv-auth status` first. Reauthorize only the
affected connection, or restore an exact matching vault backup and then import
its separate age-encrypted credential archive. Imported connections remain
unverified until the onboarding task confirms identity and completes a bounded
provider read.

## A useful first run is complete when

- the user has accepted enough context for Seld to understand the intended
  outcome and boundaries;
- every selected source was either read in a bounded live call or named as an
  exact gap;
- the first orientation distinguishes user statements, observations,
  inferences, and unknowns; and
- a fresh ChatGPT task can reload the accepted Mind without copying the old
  transcript.

A completed first run leaves accepted context, explicit source coverage, and a
Mind that reloads in a fresh task.
