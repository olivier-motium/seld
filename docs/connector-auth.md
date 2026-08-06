# Standalone Connector Authentication

Seld-managed connectors authenticate independently of ChatGPT, Codex,
OpenCode, Open Interpreter, a browser profile, or another AI host. Changing the
reasoning host does not change connector custody, and Seld never discovers,
copies, or converts an existing host session.

The ordinary user surface is `gsv connectors`. The lower-level `gsv-auth`
command exists for encrypted transfer and audited contributor compatibility;
it is not the normal onboarding path.

## Check this build first

Run the content-free readiness and account checks against the intended vault:

```bash
gsv connectors readiness
gsv connectors list
```

`readiness` reports whether the installed build contains valid public OAuth
registrations for Google, Microsoft, and Slack and whether any required
host-local client secret is configured. It never prints a client identifier or
secret. Missing, invalid, setup-required, and keyring-unavailable states are
distinct failures; each stops before OAuth and saves nothing. Provider
implementation alone is not sign-in readiness.

Google's desktop flow uses PKCE and the packaged client ID. Google's token
endpoint requires that client's matching secret for both code exchange and
refresh. A person with access to the owning Google Cloud project configures it
once for the active vault through hidden input:

```bash
gsv connectors client-secret set google
```

The value is stored only in the vault-scoped OS keyring and is shared by Gmail,
Google Calendar, and Google Drive in that vault. It is never accepted as a
command-line argument or written to the vault, backup, credential archive,
logs, receipts, or public registration. A restored vault or another vault or
host therefore requires the value again. Rotate it with `--replace`; remove it
with `gsv connectors client-secret clear google`. Rotating the packaged client
ID or deleting a vault can leave the old, unreachable keyring item behind
because the secret store deliberately cannot enumerate entries.

`list` shows the finite connector catalog and redacted local state. It never
returns a token, OAuth endpoint, raw account identifier, client identifier,
scope secret, keyring reference, or account fingerprint.

## Connect one logical source

Each source gets its own least-authority grant and a recognizable local label:

```bash
gsv connectors connect gmail --access read --alias 'Personal Gmail'
gsv connectors connect google_calendar --access full --alias 'Family Calendar'
gsv connectors connect google_drive --access full --alias 'Work Drive'
gsv connectors connect outlook_mail --access full --alias 'Work Mail'
gsv connectors connect outlook_calendar --access read --alias 'Work Calendar'
gsv connectors connect slack --access full --alias 'Motium Slack'
```

Connecting Gmail never silently grants Calendar or Drive. Outlook Mail never
silently grants Outlook Calendar. The supported logical names are `gmail`,
`google_calendar`, `google_drive`, `outlook_mail`, `outlook_calendar`, `slack`,
and bot-only `discord`.

The command opens the provider's consent page in the default browser, uses
authorization code with PKCE and a one-shot loopback callback, verifies the
returned provider identity, and shows that identity in human terms. It then
asks `Use this account? [y/N]`; no is the default. Only yes publishes
privacy-safe metadata and OS-keyring custody.

Use `--browser firefox` when Firefox is preferred. Use `--no-browser` to print
the exact URL, open it on the same computer, and keep the command running for
the callback. The person—not the agent—chooses the account, consents, completes
second factors, accepts legal terms, and obtains any administrator approval.

The local label is not provider identity. It is safe to change without OAuth:

```bash
gsv connectors alias con-REPLACE --alias 'Personal Gmail'
```

## Read and Full access

Read enables the connector's closed typed read catalog. Full adds the closed
typed create, update, send/share, recoverable-delete, and permanent-operation
catalog within the provider grant. The interactive `gsv_connectors` MCP server
exposes exactly one read and one write tool per logical source; it does not
expose a generic URL, method, header, token, or provider proxy.

Outward and destructive calls first return an exact preview and short-lived
confirmation token. Execution rebinds that token to the connection, operation,
input, scopes, and credential version. Recoverable delete and permanent purge
are separate operations. Gmail's broad irreversible-purge scope is omitted
from normal Full access and must be requested deliberately:

```bash
gsv connectors connect gmail --access full \
  --with-permanent-delete \
  --alias 'Personal Gmail'
```

A same-account Read connection stays ready until a Full upgrade is verified
and published. Pulse remains a separate bounded read-only lane; the small read
used to prove source coverage does not limit the interactive connector.

### Google Drive moves and deletion

Before `files.move`, `files.trash`, `files.restore`, or `files.purge`, the agent
calls `files.get` with `lifecycle_snapshot: true` and replays the returned
normalized snapshot as `expected_file`. A move also snapshots the destination
folder as `expected_destination`. The person sees names, parents, trash state,
version, and relevant capabilities in the confirmation preview; access-bearing
Drive resource keys stay bound to the confirmation but are displayed only as a
digest.

Seld reads the target and destination again when it creates the preview, when
the person confirms, and immediately before dispatch. Drift or a missing
provider capability stops the write and asks for a fresh preview. Shared-drive
routing is inferred from the snapshots. Move derives the one current parent to
remove and one reviewed destination to add; it does not ask the agent to invent
the parent transition. Link-shared target, current-parent, and destination keys
are sent only through Google's `X-Goog-Drive-Resource-Keys` header.

`files.trash` is the everyday delete path. `files.restore` works only for an
explicitly trashed item. `files.purge` works only for a currently trashed item
and has a separate permanent confirmation. Move and restore also require
confirmation because hierarchy changes can change inherited access and restore
can re-expose content. Successful move, trash, and restore calls validate the
provider's returned file ID, version, and resulting state; an unusable success
response is reported as outcome unknown and must not be retried automatically.

Drive v3 does not document a conditional file update/delete primitive for the
returned `version`. Seld therefore uses repeated application-side comparison,
not a claimed provider CAS guarantee; a small final read-to-write race remains.

## Discord bot onboarding

Discord is Full-only because the bot token is the authority boundary:

```bash
gsv connectors connect discord --access full --alias 'Household bot'
```

Seld accepts the bot token only through hidden terminal input, verifies it with
Discord's fixed bot identity route, rejects a normal-user token, and defaults
the final identity prompt to no. Never put the token in chat, an argument, an
environment variable, a file, a log, or the vault.

The typed interactive Discord read/write catalog is available after a verified
connection. The current build does not package or recommend a Discord Pulse
companion. Leave Discord unselected for Pulse unless an exact separate companion
runtime, bot binding, and host-private channel allowlist have been independently
audited, installed, and verified.

## Inspect, retry, and recover

Inspect one logical source or exact connection without exposing credentials:

```bash
gsv connectors status gmail
gsv connectors status con-REPLACE
```

If OAuth returns the wrong account, answer no. Nothing new is published. Retry
the intended account; use `--new-account` only when the person deliberately
wants to retain a different account too. Reauthorize an existing OAuth
connection without changing its portable identity:

```bash
gsv connectors reauthorize con-REPLACE
```

Reauthorization preserves the prior local alias unless the person supplies a
replacement. Pass `--browser firefox` or `--no-browser` again when wanted.
Provider `invalid_grant` or Slack
`invalid_refresh_token` marks the connection as requiring reauthorization
without deleting the last credential.

To forget local custody, confirm an exact disconnect:

```bash
gsv connectors disconnect con-REPLACE
gsv connectors revocation-help con-REPLACE
```

Disconnect does not claim provider-side revocation. `revocation-help` explains
the separate provider step without taking it.

## Move credentials to another host

Portability has two deliberate parts:

1. a normal Seld backup carries `CONNECTIONS.md`, which contains metadata but
   no secrets; and
2. a separate age-encrypted archive carries credentials for that exact vault
   ID and exact connection revision.

Install `age`, create or select an age identity, and keep its private file
outside the vault, backup, repository, and shell history. Export only to its
public recipient:

```bash
gsv backup create
gsv-auth export \
  --output /safe/path/seld-auth.age \
  --recipient age1REPLACE_WITH_PUBLIC_RECIPIENT
```

On the destination, restore the matching vault backup before importing the
credential archive:

```bash
gsv backup restore /safe/path/gsv-backup.zip /path/to/restored-vault
gsv-auth --vault /path/to/restored-vault import \
  /safe/path/seld-auth.age \
  --identity /safe/private/path/seld-auth.agekey
```

Import validates the archive and destination custody before mutating portable
state. It refuses a different vault, unrelated `CONNECTIONS.md` revision,
malformed archive, or conflicting destination credential. Its receipt contains
ordered connection IDs but no host path; the CLI prints vault-qualified next
commands. Follow each one, optionally restoring a privacy-safe alias:

```bash
gsv --vault /path/to/restored-vault connectors resume con-REPLACE \
  --alias 'Personal Gmail'
```

Resume verifies the exact provider identity before the imported connection
becomes `ready`. Historical source coverage remains `needs_revalidation` until
`$gsv-onboard` performs a successful bounded read on the destination. Host-local
source policy is configured again; an auth archive never carries a Discord
channel allowlist or another host-private source boundary.

## Low-level contributor compatibility

`gsv-auth profiles`, `add`, `oauth`, `credential`, `status`, and `remove`
remain available for an audited connector implementation or migration test.
They expose the legacy finite-profile boundary and must not be presented as the
consumer setup path. Manual credentials enter only through hidden input. The
manual form is non-OAuth; OAuth endpoints and scopes remain profile-owned and
cannot be widened from the CLI.

Runtime code resolves credentials inside the local process. OAuth refresh is
serialized per connection and publishes one compare-and-swap token version.
The narrow `gsv_connector_source_read` Pulse tool accepts an exact connection
ID and a bounded implemented source. The separate interactive connector server
uses fixed provider destinations, closed schemas, sealed continuations, and
the confirmation boundary described above.

## Boundaries

- `CONNECTIONS.md`, ordinary backups, status, Bridge, logs, and MCP never carry
  credential bytes.
- The native macOS Keychain, Windows Credential Manager, Secret Service, and
  KWallet backends exposed by `keyring` are accepted; unavailable or unapproved
  custody fails closed.
- Host-local pointer files contain only a vault-bound opaque keyring reference,
  version, and timestamp.
- The local OS user is trusted. The keyring does not isolate credentials from
  hostile code already running as that user.
- Transfer confidentiality depends on the age identity remaining private.
- Auth is not account creation, provider registration, administrator consent,
  or authorization for an external action.
- Account, permission, security, billing, and other administrative APIs remain
  outside both the Pulse and interactive connector lanes.
