# Google Drive

Use this note after the person selects Google Drive. A host-owned Drive app may
satisfy the bounded Pulse recipe. Portable Seld custody uses its own logical
Drive connection and never copies an AI-host or browser session.

## Connect

```text
gsv connectors connect google_drive --access read --browser firefox
gsv connectors connect google_drive --access full --browser firefox
```

Read grants typed My Drive and shared-drive metadata, content, permission,
comment, reply, and revision reads within the provider grant. Full adds file
create/update/copy/move/trash/restore/purge plus permission, comment, reply, and
revision mutations. The command opens Google OAuth with PKCE and a loopback
callback, verifies the account, shows it, and asks `Use this account? [y/N]`
before publishing. The default is no. A Read connection remains ready during a
same-account Full upgrade.

The person owns Google account selection, consent, Workspace policy,
administrator approval, and second factors. If the installed build lacks a
public Google client registration, sign-in stops and saves nothing.

## Verify the source

Use `gsv connectors status google_drive`, confirm the expected account, then
call `gsv_connector_source_read` with that exact connection ID and source
`google_drive`. Read one small recent metadata set. A successful empty result
counts. This small read proves Pulse coverage; it is not an artificial cap on
interactive Drive access.

The isolated `gsv_connectors` server exposes `gsv_google_drive_read` and
`gsv_google_drive_write`. It uses fixed Google routes, sealed page cursors,
shared-drive flags, and range-aware content reads. User-content writes remain
closed and typed. Sharing is outward, trash is recoverable, and purge is
permanent; each receives the matching preview and confirmation policy. Large
binary content uses the connector's local-file/artifact transfer lane rather
than inflating JSON or placing bytes in the vault.

Return only the verified identity basis, bounded-read result, horizon, and
stable references to `$gsv-onboard`. Do not persist file bodies, provider
cursors, raw account IDs, OAuth material, or error text.
