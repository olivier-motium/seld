# Gmail

Use this note after the person selects Gmail. A host-owned Gmail app may satisfy
the bounded Pulse recipe. For portable Seld custody, use a separate logical
Gmail connection; it never imports an AI-host or browser session.

## Connect

```bash
gsv connectors connect gmail --access read --alias 'Personal Gmail'
gsv connectors connect gmail --access full --alias 'Personal Gmail'
```

Choose one command. Read grants the typed Gmail read catalog. Full adds drafts,
send, label changes, read-state changes, trash/restore, and delete operations.
Permanent purge uses Google's separate broad mail permission and remains a
separate confirmed operation. The command opens Google OAuth with PKCE and a
loopback callback, verifies the returned Google identity, shows the account,
and asks `Use this account? [y/N]` before anything is published. The default is
no. During a Read-to-Full upgrade, the old Read connection stays ready until
the same-account Full connection is verified and published.

Gmail Full excludes irreversible purge unless the person deliberately adds
`--with-permanent-delete`.

The person owns account selection, consent, Workspace policy, administrator
approval, and second factors. The agent must not enter or reuse credentials,
codes, tokens, or second factors. If the build has no public Google client
registration, sign-in stops and saves nothing.

## Verify the source

Use `gsv connectors status gmail`, confirm the expected mailbox in human terms,
then call `gsv_connector_source_read` with the exact connection ID and source
`gmail` for one small recent mail window. An explicitly empty result is valid.
This bounded read exists only to prove Pulse coverage; it does not limit the
interactive connector.

The separate `gsv_connectors` MCP server exposes `gsv_gmail_read` and
`gsv_gmail_write`. Their closed schemas cover message, thread, attachment,
draft, and label operations. Sends are outward and confirmed. Trash, restore,
and label moves are one-step recoverable operations, including batches of up
to 1,000 messages. `messages.purge`, `threads.purge`, and
`messages.batch_purge` are permanent and require a bound confirmation.

Return the verified identity basis, bounded-read result, tool shape, coverage
horizon, and stable references to `$gsv-onboard`. Record no raw mail bodies,
provider tokens, mailbox address, or provider error text in the vault.
