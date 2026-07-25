# Changelog

## 0.2.0 - Unreleased

- Add authored whole-life Direction and a complete ordered Portfolio whose
  judgments are bound to exact Direction, Task, and WorkThread revisions.
- Add a finite guided review of every open outcome, carried by one ordinary
  review-session Task and the canonical life-portfolio-review WorkThread, with
  revision-anchored checked coverage, pause/resume, and explicit terminal
  semantics.
- Add the authenticated, append-only Bridge control queue with vault-bound CAS,
  bounded generations, durable CLI/MCP accept or reject dispositions, and
  archival only after every live intent is decided. Disposition acknowledges an
  intent; it never approves or executes an external action.
- Add the event-bound, GSV-only MCP profile and same-hand Codex review-turn
  transport. The transport is off by default and remains foundation-gated while
  installed-candidate, clean-account, platform, signing, and soak gates are
  open.
- Add tested foundation contracts for context-first onboarding, deterministic
  source attestations, local privacy screening, mechanical and cognitive Pulse
  admission, scheduler canaries, and reversible migration. They remain
  unexposed until their named release gates close.
- Add the private, read-only Bridge for current orientation, commitments, and
  storylines, including authenticated loopback access and responsive states.
- Make live-Bridge upgrades quiesce before executable replacement and restore
  the previous Bridge best-effort on rollback.
- Make bare `gsv` open the configured Bridge and make setup install Codex
  before starting or opening the local surface.
- Add the killed-hand synthetic continuity proof and a packaged-Bridge browser
  gate that persists no generated screenshots or GIFs.
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
