# Discord

Discord is bot-only. Discord forbids automating normal user accounts, so Seld
never accepts, copies, or uses a user token. The person creates or selects a
Discord application, authorizes its bot for the intended servers and channels,
and owns every provider permission or security decision.

## Connect the bot

Run this in an interactive local terminal:

```text
gsv connectors connect discord --access full
```

Seld reads the bot token only through a hidden prompt, calls Discord's fixed
`/users/@me` identity route with bot authorization, and refuses a non-bot
identity. It then shows the verified bot in human terms and asks `Use this
account? [y/N]`. The default is no. Only after yes does Seld publish privacy-safe
connection metadata and place the token in the OS keyring. Never put the token
in chat, an argument, an environment variable, a file, a log, or the vault.

Use `gsv connectors status discord` to inspect redacted state. To forget local
custody, use `gsv connectors disconnect <connection-id>` after confirmation.
That does not revoke Discord access; `gsv connectors revocation-help
<connection-id>` explains the separate provider-side step without taking it.

## Keep Pulse and interactive actions separate

The narrow Pulse source remains forward-only and read-only. Its channel
allowlist stays host-private, and delivery still follows status, poll,
content-free source receipt, then acknowledge. Do not put raw channel IDs in
the vault or treat a poll as permission to act.

The separately installed `gsv_connectors` MCP server exposes one typed Discord
read tool and one typed Discord write tool. Its closed catalog covers bot
identity, guild/channel/thread/message/attachment/reaction reads and the
corresponding bot-authorized user-content mutations. It never exposes a caller
URL, method, header, token, or generic Discord proxy. Discord does not grant a
Read tier because the same bot token is the authority boundary; Full still
means only the cataloged bot operations, not administrative APIs.

Outward operations such as messages, attachments, reactions, or direct messages
return an exact preview and short-lived confirmation token. Destructive and
permanent operations are separately classified. The runtime binds confirmation
to the bot, operation, exact input, grant, and credential version; Discord
message idempotency is derived from that confirmation rather than caller input.

## Verify onboarding

Confirm the bot identity with the person, then perform one bounded read in the
chosen channel set. A successful empty result counts. Wrong identity, missing
channel access, changed bot token, or a provider error stops the source. Record
only content-free coverage through `$gsv-onboard`; interactive CRUD never
widens Pulse coverage or creates provider authority.
