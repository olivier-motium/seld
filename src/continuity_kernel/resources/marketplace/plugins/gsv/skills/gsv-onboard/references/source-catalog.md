# Seld source catalog

The recipe names below are logical capabilities. Tool names vary by ChatGPT
app, custom MCP server, and version. Verify the actual task-local tool shape
instead of hard-coding a marketplace label.

For a listed ChatGPT app, open only that source's linked connection note after
the person selects it. The app is installed separately and owns OAuth; the
Seld note adds identity checks, a bounded read, recovery, and the handoff back
to `$gsv-onboard`.

| Source | Setup surface | Bounded verification |
| --- | --- | --- |
| Seld on this computer | Installed Seld MCP or exact-vault local CLI | Verify the vault identity and one bounded `gsv_context` read; this is source zero and remains available when a task-local plugin is absent. |
| ChatGPT activity | Installed Seld/ChatGPT task tools | Confirm the current task/account context and read recent relevant activity. |
| [Gmail](providers/gmail.md) | Gmail app in ChatGPT | Confirm the Google account; read a small recent mail window. |
| [Google Calendar](providers/google-calendar.md) | Google Calendar app | Confirm the calendar identity; read a bounded current window. |
| [Google Drive](providers/google-drive.md) | Google Drive app | Confirm the account; search or read a small recent set. |
| Google Sheets | Google Drive/Sheets app tools | Confirm the account; read only the named sheet/range needed. |
| [Outlook mail](providers/outlook-email.md) and [calendar](providers/outlook-calendar.md) | Outlook apps | Confirm the mailbox/calendar; read a bounded recent mail or calendar window. |
| [Slack](providers/slack.md) | Slack app | Confirm the workspace; read a small recent channel, thread, or DM window. |
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
| [Discord](providers/discord.md) | Seld's exact CLI-bound, GET-only `discord-mcp` companion; token and channel allowlist are inherited from the private session environment | Prefer bot mode. User-token mode requires a visible Discord self-bot warning and the person's explicit informed opt-in. Confirm the account and exact channel-set digest, create or replay one forward-only bounded delivery, persist its exact source receipt, then acknowledge. Never send, react, join, expose a token, or persist raw Discord identifiers in the vault. |
| Shopify | A Shopify app or custom read-only MCP app | Confirm the store; read a bounded recent order/customer window. |
| Instagram | A Meta/Instagram app or custom read-only MCP app | Confirm the account; read a bounded recent activity or message window within granted scopes. |
| Screen context | Optional local derived-context tool | Ask for a fresh per-flow approval; retain derived signals, never frames. |

If a listed first-party app is unavailable for the person's plan, region, or
workspace, a custom MCP app may satisfy the same recipe. Do not substitute
browser scraping, ask for credentials in chat, or weaken the read-only Pulse
boundary.
