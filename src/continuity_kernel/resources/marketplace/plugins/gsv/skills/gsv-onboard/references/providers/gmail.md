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
delete operations, and ordinary Gmail settings control for filters, the primary
signature, language, POP/IMAP, and vacation replies. Explicit message, thread,
or batch purge uses Google's separate broad mail permission and remains a
separately confirmed operation. Full requests Google's settings-basic permission;
it never requests Workspace administrator delegation.

Google classifies settings-basic as a restricted Gmail permission, so the public
OAuth app must be verified for it and Workspace policy may still deny it. If an
upgrade is denied or incomplete, Seld leaves the existing Full connection and
its current mailbox capabilities unchanged; a first-time connection saves
nothing and returns a copyable retry command. When an older Full connection
already has permanent-delete authority, the settings upgrade retains that
separately chosen authority and shows it again before replacing the old grant.
`gsv connectors status gmail` reports `settings_control=upgrade_required` for
an older Full connection and returns the exact reconnect command; the mailbox
connection remains usable while the person decides whether to upgrade.
If status identifies a `legacy_provider_bundle`, leave that bundle intact and
connect a separate logical Gmail Full account; the runtime's settings error
directs the person to reconnect logical Gmail Full access.
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
draft, label, filter, forwarding-visibility, primary send-as, language, POP/IMAP,
and vacation operations. Sends and automatic future replies or forwarding are
outward and confirmed. Future auto-archive, Spam-routing, or Trash-routing rules
are destructive and confirmed. IMAP `deleteForever` and filter deletion are
permanent and confirmed.
Trash, restore, and ordinary label moves remain one-step recoverable operations,
including batches of up to 1,000 messages. `messages.purge`, `threads.purge`,
and `messages.batch_purge` are permanent and require a bound confirmation.
User-label creation and updates include the full label visibility states and a
provider-validated text/background color pair. To remove a label from ordinary
views without destroying it, use `labels.update` with
`label_list_visibility=labelHide` and `message_list_visibility=hide`.
`labels.purge` is different: it immediately
deletes a user label and removes that label from every message and thread, with
no provider undo. Read the label first and pass its exact `id`, `name`, and
`type` as `expected_label`; Seld reads it again and refuses a changed or system
label before DELETE. Gmail labels have no ETag, so a change after Seld's final
read cannot be detected. Label purge uses ordinary Gmail Full access and does
not require `--with-permanent-delete`.
Before deleting a filter, read it with `settings.filters.get`, then pass the
returned `id`, `criteria`, and `action` as `expected_filter`. The confirmation
shows that complete reviewed rule, and Seld reads it again before deletion so a
rule changed in another client is never deleted under a stale confirmation.
`settings.vacation.update` is an explicit whole-state replacement: read the
current vacation settings first, then provide every subject/body/restriction
field and both schedule boundaries. Use `null` for a missing start or end
boundary; Seld omits that optional provider field while binding the explicit
no-boundary choice in the confirmation.

The consumer OAuth connector deliberately does not expose Workspace administrator
operations that Google restricts to a service account with domain-wide delegation:
delegate management, forwarding-address creation/deletion, automatic-forwarding
updates, and custom send-as lifecycle. It also excludes SMTP passwords, hosted
S/MIME private-key material, and client-side-encryption key management. Those need
a separate administrator/security credential model; reconnecting ordinary Gmail
Full must never request `gmail.settings.sharing`.

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
