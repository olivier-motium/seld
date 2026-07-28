# Documentation

Seld turns the ChatGPT desktop app into a resident AI for the parts of life and work a person
chooses to connect. It keeps one local, inspectable model of people, projects,
commitments, decisions, source coverage, and next actions; Bridge presents that
model, Pulse keeps it current, and ChatGPT remains the reasoning and execution
surface.

Seld is the public product name. The current command, package, plugin and skill
identifiers remain `gsv`, and `GSV_*` environment variables and existing data
paths remain unchanged for compatibility.

Start with the [Product contract](product-contract.md) for the product people
experience. The remaining documents explain the implementation, its tests and
boundaries, and the separation between model judgment and deterministic safety
code.

- [Seld 0.3.0 release notes](releases/0.3.0.md): the rebuilt Bridge,
  context-first onboarding, resident Pulse, source coverage, and durable
  decision loop.
- [Product contract](product-contract.md): the outcome, product hierarchy,
  first useful journey, and consumer acceptance criteria.
- [Installation](installation.md): supported consumer and developer paths,
  upgrades, offline setup, and reversible removal.
- [Onboarding](onboarding.md): the shipped context-first journey, source
  verification, Pulse registration, and Bridge intent loop.
- [Architecture](architecture.md): components, ownership boundaries, and write
  flow.
- [ChatGPT integration evidence](codex-integration.md): version-bounded app
  discovery and `codex://new` link evidence.
- [Data format](data-format.md): authoritative Markdown records, revisions, and
  relationships.
- [Trust model](trust-model.md): threat assumptions, guarantees, and explicit
  non-goals.
- [State-of-the-art architecture assessment](state-of-the-art.md): the exact
  Seld candidate, category definition, comparison method, and current OpenClaw
  and Hermes Agent evidence behind the architectural claim.
- [Release process](release.md): reproducible release inputs, platform assets,
  checksums, provenance, and acceptance gates.
- [Evidence and 1.0 claim boundaries](release-gates.md): exact proof required
  for signed binaries, additional platforms, service levels, and comparative
  claims beyond the current public source distribution.

The CLI and MCP server expose the same local continuity primitives. The bearer-gated
Bridge provides read views plus a narrow authenticated, compare-and-swap,
append-only control queue for setup choices, approvals, corrections, and undo
requests. CLI and MCP can durably accept or reject those receipts, but cannot
execute them, authorize external action, author semantic canon, maintain a
second database, or infer task meaning.
