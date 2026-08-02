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
send, raw-message transfer, label changes, read-state changes, trash/restore,
and delete operations. Explicit message, thread, or batch purge uses Google's
separate broad mail permission and remains a separately confirmed operation.
The command opens Google OAuth with PKCE and a loopback callback, verifies the
returned Google identity, shows the account, and asks `Use this account? [y/N]`
before anything is published. The default is no. During a Read-to-Full upgrade,
the old Read connection stays ready until the same-account Full connection is
verified and published.

Gmail Full excludes the explicit message, thread, and batch purge operations
unless the person deliberately adds `--with-permanent-delete`.

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

For an existing `.eml` file, use the typed `local_file` selector rather than
copying raw bytes into the conversation. `messages.send` sends that exact
RFC822 snapshot. Its confirmation shows every decoded To, Cc, and Bcc mailbox,
with Bcc separate, plus From, Sender, Reply-To, and Subject; it never previews
the body. `messages.insert` and `messages.import` store the exact snapshot
without sending its listed recipients. Insert defaults to provider receipt
time. Import defaults to the message's Date header and fails before confirmation
if the headers cannot be safely validated for that mode; choose
`internal_date_source=receivedTime` for legacy mail that should be stored
opaquely.

Strict raw send accepts valid UTF-8 in unstructured headers such as Subject.
Encode non-ASCII address display names with RFC 2047; unencoded UTF-8 address
tokens, invalid byte sequences, and control bytes fail closed. Insert/import
with `internal_date_source=receivedTime` preserve those headers opaquely instead.
Strict raw send also rejects `Resent-To`, `Resent-Cc`, and `Resent-Bcc` so the
confirmation never relies on ambiguous routing semantics.

Migration flags remain explicit in the same confirmation. `deleted=true` is a
permanent, Google Workspace-only state rather than Trash, so it receives the
permanent warning even though Google authorizes it through ordinary Gmail Full
access. `process_for_calendar=true` warns that Gmail may add extracted meetings
to Google Calendar. `never_mark_spam=true` bypasses spam classification while
Gmail's import path performs no SPF checks. The confirmation binds the immutable
local snapshot, its digest, its flags, and the parsed preview. If Gmail's final
upload outcome is unknown, Seld does not replay it; reconcile Gmail before any
manual retry.

Return the verified identity basis, bounded-read result, tool shape, coverage
horizon, and stable references to `$gsv-onboard`. Record no raw mail bodies,
provider tokens, mailbox address, or provider error text in the vault.
