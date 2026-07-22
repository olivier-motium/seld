# Security policy

## Supported versions

Security fixes currently target the latest tagged release only.

## Reporting

Do not open a public issue for a vulnerability that could expose a user's local
vault, bypass compare-and-swap writes, escape archive paths, or execute an
untrusted command. Use GitHub private vulnerability reporting once the public
repository is available.

Until then, report privately to the repository owner. Do not include a real
vault, credentials, tokens, or provider payloads. A synthetic reproduction is
enough.

## Scope

The strongest guarantees are local: bounded parsing, explicit identity,
cross-platform advisory locks, atomic replacement, exact revisions, verified
backups, and reversible Codex installation. The vault is not encrypted by this
project; users should rely on full-disk encryption and operating-system access
controls.

Backup SHA-256 manifests detect accidental corruption; they do not authenticate
the backup author and do not encrypt its contents. Restore rejects encrypted,
oversized, duplicate, symbolic-link, malformed, and path-traversing archive
members before publishing a vault.

Release checksums fetched from the same release host are not an independent
trust root. Verify GitHub artifact provenance and platform signatures when they
are available. Apple notarization and Windows Authenticode are not present until
the release notes explicitly say otherwise.

The privacy scanner fails closed on unreadable or oversized working-tree and
history objects, reports filenames only, and accepts project-specific private
terms through `CONTINUITY_PRIVATE_TERMS`.

The history scan covers reachable Git blob contents. It intentionally does not
scan commit messages or author metadata: those are public release metadata,
not product payloads, and contributors must review them before publishing.
