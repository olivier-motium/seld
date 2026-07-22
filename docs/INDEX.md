# Documentation

- [Product contract](product-contract.md): the outcome, product hierarchy,
  first useful journey, and consumer acceptance criteria.
- [Installation](installation.md): supported consumer and developer paths,
  upgrades, offline setup, and reversible removal.
- [Architecture](architecture.md): components, ownership boundaries, and write
  flow.
- [Codex integration evidence](codex-integration.md): version-bounded app
  discovery and `codex://new` link evidence.
- [Data format](data-format.md): authoritative Markdown records, revisions, and
  relationships.
- [Trust model](trust-model.md): threat assumptions, guarantees, and explicit
  non-goals.
- [Release process](release.md): reproducible release inputs, platform assets,
  checksums, provenance, and acceptance gates.

The CLI and MCP server expose the same kernel. The bearer-gated Bridge is a
read-only human surface over the same authored state; it does not maintain a
second database or infer task meaning.
