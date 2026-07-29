# Changelog

The product was renamed from GSV to Seld in `0.2.0`.
The `gsv` command, package, plugin and skill identifiers, `GSV_*` environment
variables, existing data paths, and historical `0.1.0` entries retain their
original names for compatibility.

## 0.3.0 - 2026-07-28

Read the [0.3.0 release notes](docs/releases/0.3.0.md) for the consumer-facing
overview of the rebuilt Bridge, resident Pulse, source onboarding, and durable
decision loop.

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
- Add the event-bound, `gsv`-only MCP profile and same-hand Codex review-turn
  transport with fail-closed capability checks and durable no-replay receipts.
- Add context-first onboarding, a source capability catalog, bounded live-read
  guidance, local privacy screening, and an AI-authored Pulse skill.
- Remove unused deterministic onboarding, Pulse-controller,
  scheduler-planning, and migration modules. The shipped skills own semantic
  orchestration; legacy backup marker recognition remains for safe restore.
- Add host-local, per-vault directory grants and the bounded
  `gsv_local_file_read` surface. Selecting local files grants nothing by itself,
  and deselection revokes every root grant before publishing the new source
  selection.
- Add the canonical `SOURCES.md` coverage ledger with `gsv source` CLI and
  `gsv_source_*` MCP surfaces: explicit selection, content-free AI-attested
  read/failure receipts, stale-CAS protection, account continuity, and
  fresh-process Bridge visibility.
- Add the private Bridge with read-only record views plus bounded intent and
  review-turn routes, including authenticated loopback access and responsive
  states.
- Add approval-gated self-update for the official macOS `uv` source install.
  Pulse can notice a newer reviewed public-main revision, while Bridge and MCP
  remain cache-only. Exact-SHA installation preserves the prior environment
  until fresh-process vault, ChatGPT, and Bridge checks pass, with an
  independent recovery entrypoint across interrupted swaps.
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

## 0.2.0 - 2026-07-28

- Publish the Seld resident-mind core with canonical local records,
  authenticated Bridge reads, the bounded intent queue, guided Portfolio
  review, exact continuation, and reversible ChatGPT integration.

## 0.1.0

- Fresh-history local GSV kernel.
- Typed Markdown tasks, canonical entities, and work threads.
- Cross-platform locks, atomic writes, exact revisions, and audit events.
- Verified backup and restore.
- Codex marketplace plugin, MCP server, skill, and reversible setup.
- Synthetic GSV demo and clean-install validation.
