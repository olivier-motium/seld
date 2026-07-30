# Changelog

## Unreleased

- Added standalone connector authentication with portable non-secret metadata,
  OS-keyring secret custody, native public-client OAuth with PKCE, serialized
  refresh, redacted MCP status, and resumable age-encrypted credential transfer.
- Migrated the Discord source bridge to an explicit bot-only portable
  connection, rejecting ambient and normal-user tokens and failing closed on
  connection or credential rotation during poll and acknowledgement.
