# Trust Model

## Protected Outcomes

- A stale writer cannot silently replace a newer canonical record.
- An interrupted write does not expose a partial canonical record.
- A malformed, oversized, encrypted, duplicate, or path-traversing backup does
  not escape the restore target.
- A failed install does not replace the last working executable or remove
  pre-existing Codex components.
- Uninstall removes only integration state recorded as installer-owned and
  preserves vault and configuration data.
- MCP requests are bounded, validated, and mapped to the same operations as the
  CLI.

## Assumptions

GSV trusts the local operating-system account, the installed Codex CLI,
and the executable the user chose to run. Other local processes running as the
same user can read or modify an unencrypted vault. File locks coordinate
cooperating GSV processes; they are not a defense against a hostile
same-user process.

Vault text, imported references, and provider content are untrusted data.
GSV does not grant them authority to run commands, mutate external
systems, or override Codex instructions.

## Non-Goals

- Vault encryption, remote synchronization, multi-user authorization, and
  tamper-proof audit logs.
- Secret storage or credential management.
- Automatic task prioritization, semantic identity merging, or autonomous
  background action.
- Protection after the host, user account, Codex installation, or release
  signing identity is compromised.

## Distribution

Release installers verify the binary against the published SHA-256 file. GitHub
release provenance can establish which workflow produced an artifact, but a
checksum fetched from the same compromised release is not an independent trust
root. Production releases should also use platform signing where available.

The privacy gate scans the working tree, release artifacts, and reachable Git
history for likely credentials, user-home paths, and configured private terms.
Oversized or unreadable inputs fail closed and are reported by filename only.
Git-history scanning covers reachable blob contents; commit messages and author
metadata are intentionally outside that scanner's scope. Generated E2E reports
are scanned only after generation, in the release workflow's final privacy pass.
