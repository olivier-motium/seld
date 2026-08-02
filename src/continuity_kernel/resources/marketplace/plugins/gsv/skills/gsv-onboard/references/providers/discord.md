# Discord

Discord is bot-only. Never accept, copy, or use a normal-user token. The person
creates or selects the application, authorizes its bot for the intended server
and channels, and owns every provider permission and security decision.

## Prepare the bot in Discord

Use Discord's Developer Portal and official bot authorization flow. Keep the
grant least-authority:

1. Create or select one application and its Bot. Never automate a normal user
   account or use a self-bot token.
2. Enable the privileged **Message Content Intent** only when this bot must read
   non-bot message bodies, embeds, attachments, components, or polls. Leave
   other privileged intents off unless an independently reviewed feature needs
   them.
3. Install the bot into the exact intended server with the `bot` scope.
4. For reads, grant only **View Channels** and **Read Message History** in the
   intended channels.
5. For the matching interactive features, add only what is needed: **Send
   Messages**, **Send Messages in Threads**, **Embed Links**, **Attach Files**,
   **Add Reactions**, **Send Polls**, **Create Public Threads**, **Create Private
   Threads**, and **Manage Threads**. Add **Manage Channels** only when the
   person explicitly wants channel CRUD.
6. Treat **Use External Emojis** and **Mention Everyone** as optional explicit
   choices, not defaults. Never grant **Administrator**. The current catalog
   does not need **Manage Messages**, **Manage Roles**, or unrelated moderation
   permissions.
7. Restrict the bot role and channel overwrites to the exact channels where the
   person wants it to operate.

The agent may explain this checklist and wait for completion. It must not create
or change the application, enable an intent, install the bot, choose a server,
or alter roles, permissions, access, or security settings.

Official references:

- <https://docs.discord.com/developers/bots/overview>
- <https://docs.discord.com/developers/platform/oauth2-and-permissions>
- <https://docs.discord.com/developers/events/gateway>
- <https://docs.discord.com/developers/resources/message>

## Connect the bot

Run this in an interactive local terminal:

```bash
gsv connectors connect discord --access full --alias 'Household bot'
```

Seld reads the bot token only through a hidden prompt, calls Discord's fixed
`/users/@me` identity route with bot authorization, and refuses a non-bot
identity. It shows the verified bot in human terms and asks `Use this account?
[y/N]`; no is the default. Only yes publishes privacy-safe connection metadata
and places the token in the OS keyring.

Never put the token in chat, an argument, an environment variable, a file, a
log, or the vault. Confirm the displayed bot is the intended one, then inspect
`gsv connectors status discord` and the typed `identity.get` result before using
other operations. Use `gsv connectors alias <connection-id> --alias '<label>'`
to repair only the local label.

To forget local custody, use `gsv connectors disconnect <connection-id>` after
confirmation. That does not revoke Discord access. `gsv connectors
revocation-help <connection-id>` explains the separate provider-side step
without taking it.

## Keep Pulse and interactive actions separate

The standard `gsv_connectors` MCP server exposes one typed Discord read tool and
one typed Discord write tool. Its closed catalog covers bot identity,
guild/channel/thread/message/attachment/reaction reads and the corresponding
bot-authorized user-content mutations. It never exposes a caller URL, method,
header, token, or generic Discord proxy. Discord has no Read tier because the
same bot token is the authority boundary; Full still means only the cataloged
bot operations, not administrative APIs.

Outward operations such as messages, attachments, reactions, or direct messages
return an exact preview and short-lived confirmation token. Destructive and
permanent operations are separately classified. The runtime binds confirmation
to the bot, operation, exact input, grant, and credential version; Discord
message idempotency is derived from that confirmation rather than caller input.

Interactive CRUD does not make Discord a Pulse source. This build reports
`source_setup_required` after connection because it does not package or
recommend a Discord Pulse companion. Leave Discord unselected for Pulse unless
an exact separate companion runtime, bot binding, and host-private channel
allowlist have been independently audited, installed, and verified. If that
ever happens,
Pulse remains forward-only and read-only; its content-free receipt and
acknowledgement path never authorizes an interactive action.

Return only the verified bot identity basis and any independently proven
bounded-read facts to `$gsv-onboard`. Never persist a raw channel, guild, user,
or message identifier, token, provider error body, or permission payload in the
vault.
