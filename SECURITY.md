# Security policy

## Supported versions

Security fixes target current public `main` and the latest published prebuilt
release, when one exists.

## Reporting

Do not open a public issue for a vulnerability that could expose a user's local
vault, bypass compare-and-swap writes, escape archive paths, or execute an
untrusted command.

If GitHub shows **Report a vulnerability** on the repository's Security page,
use it. If that private form is unavailable, open a public issue titled
`Security contact request` with no vulnerability details and ask the repository
owner to establish a private channel. Do not include a real vault, exploit,
credentials, tokens, provider payloads, private paths, or identifying data in
that issue. A synthetic reproduction is enough once a private channel exists.

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
terms through `GSV_PRIVATE_TERMS`.

The history scan covers reachable Git blob contents. It intentionally does not
scan commit messages or author metadata: those are public release metadata,
not product payloads, and contributors must review them before publishing.
