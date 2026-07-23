# Trust model

## Protected outcomes

- A stale writer cannot silently replace a newer canonical record.
- An interrupted write does not expose a partial canonical record.
- A caught audit-append failure either restores exact prior bytes, reports that
  the mutation committed, or reports unknown integrity without guessing.
- A malformed, oversized, encrypted, duplicate, or path-traversing backup does
  not escape the restore target.
- Backup creation never replaces an existing destination and cannot report a
  concurrently swapped published archive as the staged backup. It never exposes
  an incomplete final-path archive while falling back from hard links.
- A failed install does not replace the last working executable or remove
  pre-existing Codex components.
- Uninstall removes only expected GSV-owned integration and preserves vault,
  configuration, backups, and unrelated Codex state.
- Marketplace replacement and deletion recheck an atomically isolated tree;
  destination races and newly appeared entries are preserved, not recursively
  removed.
- MCP requests are bounded, validated, and mapped to the same kernel as the
  CLI.
- A loopback caller without the current per-launch bearer cannot read the
  Bridge snapshot or health identity.
- A stale, forged, or PID-reused Bridge receipt cannot cause GSV to signal an
  unrelated process.

## Bridge boundary

Every Bridge launch creates a random bearer capability and instance identity.
The owner-only state receipt stores the token with mode `0600` where POSIX
permissions apply. GSV normalizes the Bridge application-data directory to
`0700` and its diagnostic log to `0600` on POSIX, and refuses a symbolic-link
or non-regular log path. The browser receives the token after `#`, moves it
into session storage, and strips it from the address before API requests.
Query strings, normal referrers, and static-asset requests do not carry it.

An authenticated health timeout while the receipt PID is still alive preserves
the receipt and blocks open, stop, upgrade, and uninstall from guessing. A
concrete connection refusal on the receipt's validated loopback port clears the
receipt without signalling its unverified PID, so a later open can recover. A
dead PID or a concrete authenticated identity mismatch also clears the stale
receipt; only a complete identity match permits signalling.

The server binds only to `127.0.0.1`, validates the exact Host and Origin for
its bound port, exposes no write API, and serves only bounded regular files
inside its packaged static root. The browser writes authored content using
`textContent`, not HTML interpolation.

The bearer restores the intended same-user local-file boundary against casual
loopback reads. It is not a security boundary against hostile code already
running as the same OS user, which can inspect the local vault or process-owned
state.

## Assumptions

GSV trusts the local operating-system account, the installed Codex command it
discovers, and the executable the user chose to run. File locks coordinate
cooperating GSV processes; they do not defend against a hostile same-user
process.

Durability and lock behavior assume a local filesystem with ordinary host OS
semantics. Cloud-synchronized and network filesystems are not a supported
durability boundary for the vault.

Vault text, imported references, provider content, and repository pages are
untrusted data. They do not gain authority to run commands, mutate external
systems, alter account settings, or override Codex instructions.

Browser opening is best-effort after durable setup commits. A browser failure
does not roll back Codex integration or configuration.

## Non-goals

- Vault encryption, remote synchronization, multi-user authorization, and
  tamper-proof audit logs.
- Multi-file atomicity or a complete audit journal across process or operating-
  system death between canonical replacement and event append.
- Secret storage or credential management.
- Automatic task prioritization, semantic identity merging, or autonomous
  background action.
- An autonomous Pulse scheduler or self-modifying Shipyard daemon.
- Protection after the host, user account, Codex installation, or release
  signing identity is compromised.

## Distribution

Release installers verify the binary against the published SHA-256 file. A
checksum fetched from the same compromised release is not an independent trust
root. Production releases should also use platform signing where available.
The `0.2.0` macOS candidate is not yet Developer ID signed or notarized.

The privacy gate scans the working tree, generated reports, release artifacts,
and reachable Git history for likely credentials, user-home paths, and private
terms. Oversized or unreadable inputs fail closed and are reported by filename
only. History scanning covers reachable blob contents; commit messages and
author metadata are outside that scanner's scope.
