# Documentation

- [Installation](installation.md): supported consumer and developer paths,
  upgrades, offline setup, and reversible removal.
- [Architecture](architecture.md): components, ownership boundaries, and write
  flow.
- [Data format](data-format.md): authoritative Markdown records, revisions, and
  relationships.
- [Trust model](trust-model.md): threat assumptions, guarantees, and explicit
  non-goals.
- [Release process](release.md): reproducible release inputs, platform assets,
  checksums, provenance, and acceptance gates.

The CLI is the human and automation interface. The MCP server exposes the same
kernel to Codex; it does not maintain separate state.
