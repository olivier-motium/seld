# Changelog

## 0.2.0 - Unreleased

- Add the private, read-only Bridge for current orientation, commitments, and
  storylines, including authenticated loopback access and responsive states.
- Make live-Bridge upgrades quiesce before executable replacement and restore
  the previous Bridge best-effort on rollback.
- Make bare `gsv` open the configured Bridge and make setup install Codex
  before starting or opening the local surface.
- Add the killed-hand synthetic continuity proof and regenerable README
  visuals.
- Make backup creation no-clobber, collision-resistant, traversal-complete,
  alias-safe, and identity-checked across post-publication verification. Use
  native atomic no-replace moves when hard links are unavailable, without ever
  exposing a partial final-path archive.
- Keep backup verification and restore usable with missing or invalid UTF-8
  configuration, including symlink and special-file configuration paths, with
  structured failures for ordinary configured commands.
- Keep failed unpublished restore stages as named recovery evidence, refuse
  recursive doctor cleanup, and structure staging/path I/O failures without a
  traceback.
- Verify the published restore target against the staged directory identity,
  vault identity, and logical digest before reporting success.
- Recheck receipt-owned marketplace bytes after atomic isolation during
  reinstall and uninstall, preserve destination races, and delete only exact
  manifest entries rather than recursively removing a mutable public path.

## 0.1.0

- Fresh-history local GSV kernel.
- Typed Markdown tasks, canonical entities, and work threads.
- Cross-platform locks, atomic writes, exact revisions, and audit events.
- Verified backup and restore.
- Codex marketplace plugin, MCP server, skill, and reversible setup.
- Synthetic GSV demo and clean-install validation.
