# Changelog

## Unreleased

- Added standalone connector authentication with portable non-secret metadata,
  OS-keyring secret custody, native public-client OAuth with PKCE, serialized
  refresh, redacted MCP status, and resumable age-encrypted credential transfer.
- Migrated the Discord source bridge to an explicit bot-only portable
  connection, rejecting ambient and normal-user tokens and failing closed on
  connection or credential rotation during poll and acknowledgement.
- Added finite portable profiles for Google, Microsoft, Slack, and Discord,
  including provider-specific native OAuth rules and fixed read-only adapters
  for Gmail, Google Calendar, Google Drive metadata, Outlook mail, Outlook
  Calendar, and one exact Slack conversation. Provider-reported grants are
  constrained to those profiles, and connector removal uses a terminal,
  retryable revocation checkpoint across concurrent credential operations.
