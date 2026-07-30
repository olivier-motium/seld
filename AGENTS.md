# Repository instructions

This repository contains the public, owner-neutral Seld kernel. Keep the
runtime local-first, dependency-light, and safe for nontechnical Codex users.

Host and user instructions remain authoritative. This file only adds
Seld-specific constraints; it does not redefine the requested outcome, invent
acceptance criteria, or turn checks into the objective.

- Never add real personal data, credentials, provider payloads, private paths,
  or fixtures copied from a user vault.
- Keep Markdown files authoritative. Derived indexes may be deleted and rebuilt.
- Preserve compare-and-swap writes, atomic replacement, bounded inputs, and
  cross-platform file locking.
- Codex integration must use supported plugin and MCP surfaces. Installation
  must preserve existing user configuration and be fully reversible.
- Test observable behavior at the lowest useful boundary. Avoid prose or
  source-shape locks, duplicate proof across layers, and exhaustive matrices
  without a named failure mode.
- Add focused proof for changed behavior. Exercise the real user path when a
  change materially affects setup, storage, backup, restore, or Codex
  integration.
- Use focused checks for bounded changes. Run `make check` for release or
  publication candidates, cross-cutting changes, or when the consequences make
  the full gate proportionate.
