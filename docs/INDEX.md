# Documentation

Seld turns Codex into a resident AI for the parts of life and work a person
chooses to connect. It keeps one local, inspectable model of people, projects,
commitments, decisions, source coverage, and next actions; Bridge presents that
model, Pulse keeps it current, and Codex remains the reasoning and execution
surface.

Seld is the public product name. The current command, package, plugin and skill
identifiers remain `gsv`, and `GSV_*` environment variables and existing data
paths remain unchanged for compatibility.

Start with the [Product contract](product-contract.md) for the product people
experience. The remaining documents explain how that promise is implemented,
bounded, tested, and promoted without turning mechanical code into the mind.

- [Product contract](product-contract.md): the outcome, product hierarchy,
  first useful journey, and consumer acceptance criteria.
- [Installation](installation.md): supported consumer and developer paths,
  upgrades, offline setup, and reversible removal.
- [Onboarding](onboarding.md): the context-first target contract, the current
  public-interface gaps, and the implemented Bridge intent loop.
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
- [Release gates](release-gates.md): exact-candidate platform, provider,
  signing, soak, and daily-use evidence required before promotion.

The CLI and MCP server expose the same continuity kernel. The bearer-gated
Bridge provides read views plus a narrow authenticated, compare-and-swap,
append-only control queue for setup choices, approvals, corrections, and undo
requests. CLI and MCP can durably accept or reject those receipts, but cannot
execute them, authorize external action, author semantic canon, maintain a
second database, or infer task meaning.
