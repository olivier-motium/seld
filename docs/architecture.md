# Architecture

## Components

`continuity_kernel.records` defines bounded task, entity, and work-thread
records. `continuity_kernel.vault` owns validation, locking, atomic persistence,
context rendering, backup, restore, and health checks. The CLI and dependency-
free stdio MCP server are adapters over that same vault.

The Codex integration generates a per-Codex-home local marketplace containing
the Continuity plugin, MCP manifest, and skill. The manifest points directly to
the installed standalone executable for release installs, or to the active
console/module launcher for source installs.

## Write Flow

1. A caller reads an exact record and receives its SHA-256 revision.
2. The caller submits an update with that revision.
3. Continuity takes the appropriate cross-process lock and re-reads the record.
4. A mismatched revision is rejected as a stale write.
5. Valid content is written to a same-directory temporary file, flushed, and
   atomically replaced.
6. The containing directory is synchronized where the platform supports it.
7. A bounded audit event is appended under the journal lock.

Global operations such as backup hold the global lock before record locks so a
snapshot cannot mix record versions.

## Ownership

The vault is user data. Continuity never deletes it during Codex uninstall.
The generated marketplace, plugin registration, managed instruction block, and
installation receipt are integration state. The installer records what it
owns and removes only those components when their expected content is still
present.

Installation failure rolls back components added by that invocation. Existing
marketplaces, plugins, instructions, binaries, and unrelated concurrent edits
are preserved. A conflicting unowned Continuity marketplace is reported rather
than adopted.

## Portability

The runtime uses only the Python standard library before freezing. PyInstaller
produces one executable per OS and architecture. Vault records and backups are
platform-neutral UTF-8/ZIP data and never contain executable installation
state.
