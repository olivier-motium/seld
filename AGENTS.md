# Repository instructions

This repository contains the public, owner-neutral Continuity kernel. Keep the
runtime local-first, dependency-light, and safe for nontechnical Codex users.

- Never add real personal data, credentials, provider payloads, private paths,
  or fixtures copied from a user vault.
- Keep Markdown files authoritative. Derived indexes may be deleted and rebuilt.
- Preserve compare-and-swap writes, atomic replacement, bounded inputs, and
  cross-platform file locking.
- Codex integration must use supported plugin and MCP surfaces. Installation
  must preserve existing user configuration and be fully reversible.
- New behavior needs focused unit tests and an end-to-end user-path test when it
  affects setup, storage, backup, restore, or Codex integration.
- Run `make check` before treating a change as ready.
