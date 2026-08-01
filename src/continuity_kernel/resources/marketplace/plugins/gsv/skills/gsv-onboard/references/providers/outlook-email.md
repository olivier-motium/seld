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
version. A local-file `attachments.add` is snapshot-bound and promoted to an
outward confirmation: preview reads the file into an immutable local snapshot
and does not write to Outlook; a matching confirmation replay dispatches the
upload once. Inline `content_base64` keeps the existing small compatibility
path.

Attachment uploads follow Microsoft's transfer contract. A file smaller than
3 MB (3,000,000 bytes) uses one direct attachment POST, including zero-byte
files. Files from 3 MB through 150 MiB use `createUploadSession` and sequential
3 MiB chunks to the exact preauthenticated `https://outlook.office.com` URL,
without an Authorization header on those PUTs. 150 MiB is this connector's
hard provider ceiling; tenant, mailbox, policy, and service limits can still
reject a smaller upload. Shared or delegated large attachment sessions may
also hit Microsoft's documented known issue; this connector does not hide or
retry that failure.

MIME reads remain inline by default for compatibility. Request
`delivery: "artifact"` for `messages.mime`, or for an attachment read, to
stream `/$value` into the owner-only transient artifact store. Attachment
metadata reads select concrete metadata fields and never deliver
`contentBytes`; artifact results include a bounded receipt and local path.
Outlook MIME and attachment artifact downloads stop at the connector's 150 MiB
Outlook ceiling before they can consume unbounded local disk.

Return only the identity basis and bounded-read facts to `$gsv-onboard`. Do not
persist mail bodies, raw mailbox or tenant identifiers, OAuth material, or
provider error text.
