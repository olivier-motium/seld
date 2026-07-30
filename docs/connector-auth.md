# Standalone Connector Authentication

Seld-managed connectors can authenticate independently of the account used to
run ChatGPT, Codex, OpenCode, Open Interpreter, or another AI host. Changing the
reasoning host does not change connector custody. Seld never discovers, copies,
or converts an existing AI-host or browser session.

This is the authentication boundary for connector implementations. It does not
turn metadata into a provider reader: each connector still supplies its own
read adapter and, for OAuth, a provider-issued public client registration.

## Verify the custody adapter

The standard Seld source install includes the open-source OS-keyring adapter so
the same package and self-update path own both `gsv` and `gsv-auth`:

```bash
gsv-auth status
```

Seld accepts the native macOS Keychain, Windows Credential Manager, Secret
Service, and KWallet backends exposed by `keyring`. An unavailable, disabled,
or unapproved backend fails closed when a secret operation is requested.

## Built-in profiles

List the finite provider profiles shipped by this Seld version:

```bash
gsv-auth profiles
```

The current standalone reader surface is:

| Profile | Logical sources | Credential |
| --- | --- | --- |
| `google` | Gmail, Google Calendar, Google Drive metadata | Public desktop OAuth with offline refresh |
| `microsoft` | Outlook mail and Outlook Calendar | Public desktop OAuth |
| `slack` | One exact Slack conversation per read | Slack user OAuth with PKCE |
| `discord` | The dedicated Discord companion | Bot bearer token |

Profile-owned providers, source IDs, endpoints, scopes, and credential kinds
cannot be overridden. Catalog entries without a profile still require their
host-owned app, custom MCP server, or a future audited Seld reader; portable
metadata alone does not implement one.

## Add a built-in connection

The person first registers the public client or bot in the provider and reviews
the requested permissions. The agent may explain the steps and later inspect
redacted status. It must not create or change the provider application, choose
an account, consent, enter or reuse credentials, handle a second factor, or
change account, access, or security settings.

For Google, create a **Desktop app** OAuth client, configure its consent screen,
and enable the Gmail, Calendar, and Drive APIs. Google desktop clients use a
loopback IP at the root; port `0` lets Seld bind a fresh local port. The profile
requests offline access and requires a refresh token:

```bash
gsv-auth add \
  --profile google \
  --client-id GOOGLE_DESKTOP_CLIENT_ID \
  --redirect-uri 'http://127.0.0.1:0' \
  --label 'Personal Google'

gsv-auth oauth con-REPLACE_WITH_RETURNED_IDENTIFIER
```

One Google connection grants the three fixed read-only scopes. Gmail, Calendar,
and Drive remain separate logical source reads and receive separate coverage
receipts.

For Microsoft, register a public **Mobile and desktop application** that accepts
personal and/or organizational accounts as intended. Register
`http://localhost/oauth/callback`; Microsoft ignores the ephemeral port when
matching a localhost redirect, so Seld can bind one at runtime:

```bash
gsv-auth add \
  --profile microsoft \
  --client-id MICROSOFT_APPLICATION_CLIENT_ID \
  --redirect-uri 'http://localhost:0/oauth/callback' \
  --label 'Personal Microsoft'

gsv-auth oauth con-REPLACE_WITH_RETURNED_IDENTIFIER
```

The profile requests `offline_access`, `User.Read`, `Mail.Read`, and
`Calendars.Read`. Outlook mail and calendar remain separate logical reads.

For Slack, create a Slack app. This Seld profile requires PKCE, and Slack
documents that enabling it is one-way without support intervention, so the
person—not an agent—must make that change. Add exactly the four user-token
history scopes shown by `gsv-auth profiles` and register one exact callback
using a fixed free port. Use that same URI in Seld:

```bash
gsv-auth add \
  --profile slack \
  --client-id SLACK_CLIENT_ID \
  --redirect-uri 'http://localhost:49152/oauth/callback' \
  --label 'Work Slack'

gsv-auth oauth con-REPLACE_WITH_RETURNED_IDENTIFIER
```

Launch the Seld MCP process with one exact `SLACK_CHANNEL_ID` (`C…`, `D…`, or
`G…`). The reader validates it before resolving the OAuth token, calls only
`auth.test` and one `conversations.history` request, returns at most 15 items,
and does not expand thread replies. Keep the channel ID in the private process
environment, not the vault, generated plugin, chat, or a credential archive.

For Discord, create the portable bot connection, then enter the bot token at
the hidden prompt and bind that connection ID through the dedicated Discord
setup flow:

```bash
gsv-auth add --profile discord --label 'Personal Discord bot'
gsv-auth credential con-REPLACE_WITH_RETURNED_IDENTIFIER
```

Normal-user Discord tokens are rejected. The Discord channel allowlist remains
host-local and is not part of portable authentication.

## Add a manual connection

The low-level form remains available for an audited connector implementation
that is not yet a built-in profile. Create portable metadata first, then enter a
non-OAuth credential through hidden input:

```bash
gsv-auth add \
  --provider provider-slug \
  --source source-recipe \
  --kind bearer \
  --label 'Named account'

gsv-auth credential con-REPLACE_WITH_RETURNED_IDENTIFIER
```

The credential is never accepted as a command-line argument. The shipped
manual form is non-OAuth only. OAuth connections must use one of the finite
built-in profiles so their endpoints and read-only scopes cannot be widened.

The OAuth command opens the provider's consent page, binds a one-shot loopback
listener, verifies exact state, exchanges the code with PKCE, and stores the
result in the OS keyring. Seld supports public-client authorization code plus
refresh tokens. Confidential-client secrets, OIDC identity, and device flow are
not implemented.

## Inspect and use it safely

```bash
gsv-auth status
```

Status combines portable metadata with one of `available`, `missing`,
`backend_unavailable`, or `invalid`. It never includes a secret, token, keyring
reference, OAuth endpoint, client identifier, scope, or account fingerprint.
The read-only `gsv_connection_list` MCP tool exposes the same redacted view and
has no mutation or resolution method.

Connector runtime code resolves credentials inside the local process through
`ConnectorAuthManager.resolve_credential` or
`ConnectorAuthManager.resolve_oauth_access_token`. OAuth refresh is serialized
per connection and publishes one compare-and-swap token version. Provider
`invalid_grant` or Slack `invalid_refresh_token` marks the connection
`reauthorization_required` without deleting the last credential.

The read-only `gsv_connector_source_read` MCP tool accepts only an explicit
connection ID, one of the finite implemented source IDs, and a bounded limit.
Google, Microsoft, and Slack calls use a fixed internal timeout and
fixed-destination HTTPS GETs;
redirects, write endpoints, arbitrary URLs, ambient provider tokens, and
provider response bodies in error receipts are rejected. The connection,
credential version, and source selection are rechecked after each provider
call. Returned items are transient model input; the durable `SOURCES.md`
receipt contains only hashes, coverage, completeness, and fixed error codes.

## Move to another host

Portability has two deliberate parts:

1. a normal Seld backup carries `CONNECTIONS.md`, which has metadata but no
   secrets; and
2. a separate age-encrypted export carries the credentials for that exact
   vault ID and exact connection revision.

Install `age` and create or select an age identity. Keep the private identity
outside the vault, backup, shell history, and repository. Pass only its public
recipient to export:

```bash
gsv backup create
gsv-auth export \
  --output /safe/path/seld-auth.age \
  --recipient age1REPLACE_WITH_PUBLIC_RECIPIENT
```

On the destination, restore the matching vault backup first. The destination
must not contain different credential slots for those connection IDs; an exact
derived unverified state whose already-published credentials match byte for byte
is resumed:

```bash
gsv backup restore /safe/path/gsv-backup.zip /path/to/restored-vault
gsv-auth --vault /path/to/restored-vault import \
  /safe/path/seld-auth.age \
  --identity /safe/private/path/seld-auth.agekey
gsv-auth --vault /path/to/restored-vault status
```

Export writes only ciphertext and refuses to replace an existing destination.
Import refuses a different vault, unrelated `CONNECTIONS.md` revision,
malformed archive, or different destination credential. It marks every
connection `unverified` before publishing credentials and can resume after an
ambiguous or interrupted publication. Confirm the provider identity and
complete one bounded read before marking the source ready.

Host-local source policy is configured again on the destination. In particular,
the encrypted auth archive does not carry Slack's `SLACK_CHANNEL_ID` or the
Discord companion's channel allowlist.

## Remove or reauthorize

Use the current status revision when removing a connection so a stale operator
cannot delete newer metadata:

```bash
gsv-auth remove con-REPLACE_WITH_RETURNED_IDENTIFIER \
  --expected-revision REPLACE_WITH_STATUS_REVISION
```

Removal first compare-and-swap marks the connection revoked, then deletes both
bounded keyring rotation slots, then removes portable metadata. If cleanup is
interrupted, reload status and rerun removal with the current revision. To
rotate a non-OAuth credential, run `gsv-auth credential
<connection-id> --replace`. To repair OAuth, rerun `gsv-auth oauth
<connection-id>` and then perform a bounded provider identity/read check.

## Boundaries

- `CONNECTIONS.md`, ordinary backups, status, Bridge, logs, and MCP never carry
  credential bytes.
- Host-local pointer files contain only a vault-bound opaque keyring reference,
  version, and timestamp.
- The local OS user is trusted. The keyring does not isolate credentials from
  hostile code already running as that same user.
- Transfer confidentiality depends on the age identity remaining private.
- Auth is not account creation, provider client registration, connector
  implementation, or authorization for external writes.
- The built-in providers still require the person to register the application,
  review scopes, complete consent, enter a bot credential, and satisfy any
  administrator policy. Blanket agent approval cannot perform those acts.
