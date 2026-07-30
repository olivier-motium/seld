# Seld source catalog

The recipe names below are logical capabilities. Tool names vary by ChatGPT
app, custom MCP server, and version. Verify the actual task-local tool shape
instead of hard-coding a marketplace label.

For a listed source, open only its linked connection note after the person
selects it. A host-owned ChatGPT app is installed separately and owns its
non-portable OAuth. A connector implementation using Seld-managed auth instead
uses `gsv-auth` and the OS keyring; it never imports the host app's session.
The Seld note adds identity checks, a bounded read, recovery, and the handoff
back to `$gsv-onboard` in either case.

| Source | Setup surface | Bounded verification |
| --- | --- | --- |
| Seld on this computer | Installed Seld MCP or exact-vault local CLI | Verify the vault identity and one bounded `gsv_context` read; this is source zero and remains available when a task-local plugin is absent. |
| ChatGPT activity | Installed Seld/ChatGPT task tools | Confirm the current task/account context and read recent relevant activity. |
| [Gmail](providers/gmail.md) | Gmail app or Seld-managed `google` profile | Confirm the Google account; read a small recent mail window. |
| [Google Calendar](providers/google-calendar.md) | Google Calendar app or Seld-managed `google` profile | Confirm the calendar identity; read a bounded current window. |
| [Google Drive](providers/google-drive.md) | Google Drive app or Seld-managed `google` profile | Confirm the account; search or read a small recent metadata set. |
| Google Sheets | Google Drive/Sheets app tools | Confirm the account; read only the named sheet/range needed. |
| [Outlook mail](providers/outlook-email.md) and [calendar](providers/outlook-calendar.md) | Outlook apps or Seld-managed `microsoft` profile | Confirm the mailbox/calendar; read a bounded recent mail or calendar window. |
| [Slack](providers/slack.md) | Slack app, or Seld-managed `slack` profile with one exact host-private channel ID | Confirm the workspace/member; read at most 15 recent items from that one conversation. The Seld-managed reader does not expand thread replies. |
| [Microsoft Teams](providers/teams.md) | Teams app | Confirm the tenant/account; read a small recent chat window. |
| [GitHub](providers/github.md) | GitHub app or official hosted MCP | Confirm the account/organization; read bounded recent repository activity. |
| [Asana](providers/asana.md) | Asana app | Confirm the workspace/account; read a bounded current task or project window. |
| [Atlassian](providers/atlassian.md) | Atlassian Rovo app | Confirm the site/account; read a bounded current Jira or Confluence window. |
| [Box](providers/box.md) | Box app | Confirm the account; search or read a small recent file set. |
| [Figma](providers/figma.md) | Figma app | Confirm the account/team; read a bounded recent file or project window. |
| [Notion](providers/notion.md) | Notion app | Confirm the workspace; search or read a bounded recent page set. |
| [SharePoint](providers/sharepoint.md) | SharePoint app | Confirm the tenant/site; search or read a bounded recent file or site window. |
| [Local files](local-files.md) | Seld's host-local grant and `gsv_local_file_read` | Approve one exact root; read only named regular files without hydrating cloud placeholders. |
| Apple Messages | Seld's read-only local adapter | Confirm the local profile and macOS permission. Check staged status first and adopt a verified imported prefix when present; create a forward-only baseline only when there is no staged checkpoint or prior receipt, then verify one bounded replay-safe delta. |
| WhatsApp | Seld's read-only `wacli` companion | Confirm the account and companion health. Check staged status first and adopt a verified imported prefix when present; create a forward-only baseline only when there is no staged checkpoint or prior receipt, then verify one bounded replay-safe delta without sending or reacting. |
| [Discord](providers/discord.md) | Seld's exact CLI-bound, GET-only `discord-mcp` companion; one explicit bot-only Seld connection supplies the keyring-held token, while the channel allowlist remains host-local | The person creates and authorizes the bot, enters its token through hidden `gsv-auth` input, and chooses the allowlist. Bind the returned connection ID, confirm the account and channel-set digest, create or replay one forward-only bounded delivery, persist its exact source receipt, then acknowledge. Never accept a user token, send, react, join, expose a token, or persist raw Discord identifiers in the vault. |
| Shopify | A Shopify app or custom read-only MCP app | Confirm the store; read a bounded recent order/customer window. |
| Instagram | A Meta/Instagram app or custom read-only MCP app | Confirm the account; read a bounded recent activity or message window within granted scopes. |
| Screen context | Optional local derived-context tool | Ask for a fresh per-flow approval; retain derived signals, never frames. |

If a listed first-party app is unavailable for the person's plan, region, or
workspace, a custom MCP app or implemented Seld-managed connector may satisfy
the same recipe. Do not claim that auth metadata alone implements a provider
reader. Do not substitute browser scraping, ask for credentials in chat, reuse
an AI-host login, or weaken the read-only Pulse boundary.
