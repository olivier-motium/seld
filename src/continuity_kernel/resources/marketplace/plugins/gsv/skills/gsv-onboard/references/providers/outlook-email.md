# Outlook Mail

Use this note after the person selects Outlook Mail. A host-owned Outlook app
may satisfy the bounded Pulse recipe. Portable Seld custody uses a separate
logical mail connection and never reuses an AI-host or browser session.

## Connect

```text
gsv connectors connect outlook_mail --access read --browser firefox
gsv connectors connect outlook_mail --access full --browser firefox
```

Read grants folder, message, MIME, and attachment reads. Full adds folder and
draft CRUD, reply/reply-all/forward/send, message updates/copy/move,
trash/restore/purge, and attachment mutations. The command opens Microsoft OAuth
with PKCE and a loopback callback, verifies the Graph identity, shows the
mailbox/account, and asks `Use this account? [y/N]` before publishing. The
default is no. A Read connection remains ready during a same-account Full
upgrade.

The person owns account and tenant selection, consent, administrator approval,
conditional access, and second factors. If the installed build lacks a public
Microsoft client registration, sign-in stops and saves nothing.

## Verify the source

Use `gsv connectors status outlook_mail`, confirm the expected mailbox and
tenant in human terms, then call `gsv_connector_source_read` with the exact
connection ID and source `outlook_mail` for one bounded recent mail window. A
successful empty result counts. The bound proves Pulse coverage; it does not
limit the interactive connector.

The isolated `gsv_connectors` server exposes `gsv_outlook_mail_read` and
`gsv_outlook_mail_write`. Sends, replies, and forwards are outward; trash is
recoverable; purge is separately permanent. The runtime binds each required
confirmation to the exact account, operation, input, scopes, and credential
version. Large MIME or attachment content uses owner-only local artifacts and
provider upload sessions rather than expanding JSON or the vault.

Return only the identity basis and bounded-read facts to `$gsv-onboard`. Do not
persist mail bodies, raw mailbox or tenant identifiers, OAuth material, or
provider error text.
