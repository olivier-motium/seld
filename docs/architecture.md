# Architecture

## Product hierarchy

- **GSV** is the local vehicle: the vault, persistence protocol, recovery, and
  adapters.
- **Mind** is the authored point of view in durable documents and records.
- **Hands** are replaceable MCP or Codex execution episodes using the same
  kernel.
- **Bridge** is the read-only human surface over current authored state.
- **Pulse** and **Shipyard** are operating roles with durable review boundaries
  in `0.2.0`, not autonomous scheduler or self-modification services.

## Kernel

`continuity_kernel.records` defines bounded task, entity, and work-thread
records. `continuity_kernel.vault` owns validation, locking, atomic persistence,
context rendering, backup, restore, and health checks. The CLI and
dependency-free stdio MCP server are adapters over that same vault.

Markdown in the vault is authoritative. Deterministic code may persist,
validate, traverse, and render authored facts. It may not infer task meaning,
priority, identity, ownership, thread membership, or completion from prose or
activity.

## Codex integration

Setup generates a per-Codex-home local marketplace containing the GSV plugin,
MCP manifest, and skill. The manifest points directly to the standalone
executable for release installs, or to the active module launcher for source
installs.

The MCP adapter resolves one vault for its process lifetime. An explicit global
`--vault` argument takes precedence over `GSV_VAULT` and user configuration, so
later requests cannot silently drift to a different vault.

Installation is a staged transaction. Marketplace registration, plugin
installation, and the managed `AGENTS.md` block are verified before setup
starts the Bridge. A later setup failure rolls back only components introduced
by that invocation. Existing or concurrently changed user components are left
untouched.

On the validated macOS path, Codex discovery checks an explicit `GSV_CODEX`,
`PATH`, and known Codex Desktop app bundles. Codex does not have to be on
`PATH` when the installed bundle exposes its command.

## Bridge lifecycle

The Bridge is a dependency-free loopback HTTP server over one exact vault. A
child binds port `0`, lets the OS select an available port, creates a random
instance identity and bearer capability, and atomically reports its bound port
in an owner-only state receipt.

The private snapshot and health endpoints require the per-launch bearer. The
browser receives it in the URL fragment, moves it into `sessionStorage`, and
removes it from the visible URL before requesting private data. Static assets
do not require vault access. The Bridge rejects cross-origin requests, writes,
path traversal, symlinked assets, and oversized assets.

Stop is fail-closed. GSV signals a receipt PID only when the live authenticated
health response matches the same instance, PID, port, and vault. A stale,
forged, or PID-reused receipt with a concrete mismatch is removed without
signaling the process. A concrete refusal on the validated loopback port also
removes only the stale receipt and permits a fresh Bridge; it never signals the
unverified PID. Timeouts, resets, malformed responses, and other unavailable
health outcomes preserve the receipt, start no replacement, and ask the
operator to retry. That keeps the only bearer and identity evidence available
for a later safe stop.

The Bridge renders authored documents and records live. Doctor results are
cached briefly and refreshed outside the metadata lock. Codex discovery runs as
one background refresh, so an initial snapshot returns an explicit `checking`
state instead of waiting on a subprocess; concurrent polls cannot start more
checks. The backend derives one `codex.ready` value from successful discovery,
the managed instruction block, and the installed plugin. It emits task and
new-Mind deep links only when that value is true.

Health returns the manifest-only vault identity cached when the server binds;
it never lists records, runs doctor, or hashes vault files. Each snapshot loads
each record collection once and derives its counts from those records. Its
`status` block intentionally contains identity and counts, not the explicit CLI
status command's full-vault logical digest.

## Write flow

1. A caller reads an exact record and receives its SHA-256 revision.
2. The caller submits an update with that revision.
3. GSV takes the appropriate cross-process lock and re-reads the record.
4. A mismatched revision is rejected as a stale write.
5. Valid content is written to a same-directory temporary file, flushed, and
   atomically replaced.
6. The containing directory is synchronized where the platform supports it.
7. A bounded audit event is appended under the journal lock.

This is deliberately not described as a multi-file transaction. For ordinary
reported append failures, the append primitive first restores the journal to
its exact previous length. GSV then restores the exact previous canonical bytes
or removes an invocation-owned create, but only while the current bytes still
match that invocation. A post-fsync cleanup failure is reported as committed;
unknown journal state or failed canonical recovery is reported as degraded and
left in place for inspection.

A process or operating-system death between canonical replacement and journal
append can still leave authoritative Markdown without its audit event. `0.2.0`
does not ship a pending-mutation protocol and the journal is not authoritative.
Callers must reload after any committed or degraded persistence error rather
than retrying with a stale revision.

Global operations such as backup hold the global lock before record locks so a
snapshot cannot mix record versions.

Backup creation reads each included regular file once, hashes those captured
bytes, and writes the same bytes into a staged ZIP. Symlinks and other special
files and traversal errors fail closed. Publication uses a same-directory
no-replace hard link or a native atomic no-replace move of the complete stage;
it never falls back to replacement or a partial final-path copy. If neither
primitive is available, the archive remains unpublished. Default names include
a random suffix. The destination must match the staged inode and SHA-256 before
and after archive verification, and the returned digest is the captured staged
digest. Verification rejects unsafe or non-portable member names, non-regular
Unix entry types, control characters, case or Unicode aliases, and size-bound
failures.

Restore keeps one archive descriptor open and reads each data member once into
a sibling stage while computing the compared hash map. It then enumerates the
staged regular files, checks vault identity, and runs doctor before an atomic
publication. An existing target is accepted only when it is an empty real
directory and is restored if publication did not occur. Rename failures are
classified by source and target identity plus logical digest: unpublished work
is rolled back, while a visible committed restore with unconfirmed directory
durability is reported as degraded and left intact for explicit doctor review.
Configuration and live Codex or Bridge state are outside this restore
transaction and require the documented stop-plus-setup activation step.

## Ownership and recovery

The vault and backups are user data. GSV never deletes them during Codex
uninstall. The generated marketplace, plugin registration, managed instruction
block, Bridge receipt, and installation receipt are integration state. Removal
touches only expected content recorded as GSV-owned. Uninstall snapshots the
exact managed instruction bytes and verifies the marketplace digest before
provider mutation, keeps the marketplace manifest present while Codex removes
and verifies its registrations, then rechecks and deletes the local state. Any
incomplete stage retains the ownership receipt and executable for the exact
recovery path. A receipt-bound legacy state whose generated marketplace is
already absent gets a deterministic packaged removal scaffold so a
manifest-dependent Codex provider can finish cleanup. Reinstall and uninstall
atomically isolate a receipt-owned marketplace under a unique sibling path and
rescan it before replacement or deletion. Uninstall deletes exact verified
manifest entries bottom-up and uses directory removal to detect any newly
appeared entry; destination races preserve both trees. A local cleanup error
after provider verification retains a durable provider-complete phase and the
exact owned entry hashes. A later offline retry may delete only an unchanged
subset of that manifest; any added or changed entry remains a manual-review
boundary. Legacy scaffolds are file- and directory-synchronized in an
exclusive stage before no-replace publication and provider access. Duplicate
target plugin or marketplace identities stop provider mutation as ambiguous
state.

## Portability

The runtime uses the Python standard library before freezing, plus bundled
static Bridge assets. The UI uses the host system font stack and ships no font
runtime. PyInstaller produces one executable per OS and architecture. Vault
records and backups are platform-neutral UTF-8/ZIP data and never contain
executable installation state.
