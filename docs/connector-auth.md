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

## Add a connection

Create portable non-secret metadata first. `gsv-auth` returns a generated
connection ID. The `--source` value must name a source recipe known to this Seld
version.

```bash
gsv-auth add \
  --provider github \
  --source github \
  --kind bearer \
  --label 'Work GitHub'
```

Then run the credential command in an interactive terminal and paste the value
into its hidden prompt:

```bash
gsv-auth credential con-REPLACE_WITH_RETURNED_IDENTIFIER
```

The credential is never accepted as a command-line argument. A connector owns
the opaque credential format for `api_key`, `basic`, `bearer`, and
`service_account` records.

For OAuth, register a public native client with the provider and use one exact
loopback redirect template. Port `0` lets Seld bind an available local port.
The command below is a shape example: replace the provider, source recipe,
client ID, endpoints, and scopes with the connector's registered values.

```bash
gsv-auth add \
  --provider provider-slug \
  --source source-recipe \
  --kind oauth2 \
  --client-id PUBLIC_CLIENT_ID \
  --authorization-endpoint 'https://provider.example/authorize' \
  --token-endpoint 'https://provider.example/token' \
  --redirect-uri 'http://127.0.0.1:0/callback' \
  --scope read

gsv-auth oauth con-REPLACE_WITH_RETURNED_IDENTIFIER
```

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
`invalid_grant` marks the connection `reauthorization_required` without
deleting the last credential.

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

## Remove or reauthorize

Use the current status revision when removing a connection so a stale operator
cannot delete newer metadata:

```bash
gsv-auth remove con-REPLACE_WITH_RETURNED_IDENTIFIER \
  --expected-revision REPLACE_WITH_STATUS_REVISION
```

Removal deletes both bounded keyring rotation slots before deleting portable
metadata. To rotate a non-OAuth credential, run `gsv-auth credential
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
