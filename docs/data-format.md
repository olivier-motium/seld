# Data Format

## Vault Layout

```text
Continuity/
  .continuity/
    manifest.json
    locks/
  MIND.md
  NOW.md
  AGENTS.md
  README.md
  tasks/*.md
  entities/*.md
  threads/*.md
  journal/events.jsonl
  backups/*.zip
```

Markdown is authoritative. Each typed record starts with one machine-readable
HTML comment and remains understandable in a text editor:

```markdown
<!-- continuity:{"id":"ship-atlas","kind":"task",...} -->
# Ship Atlas

## Outcome

Atlas is deployed with rollback evidence.
```

The `revision` returned by the CLI and MCP API is the SHA-256 digest of the
complete stored Markdown bytes. It is computed, not stored in the record.
Updates require that exact revision.

## Identity And Relations

Task IDs are lowercase slugs such as `ship-atlas`. Entity and work-thread IDs
are typed IDs such as `company:atlas` and `thread:atlas-release`. Filenames must
agree with record identity. Relationships are explicit ID lists; names, text
similarity, aliases, and timestamps never create edges automatically.

## Bounds

Records, text fields, references, relationships, context output, archive
members, and total restored bytes are bounded. Symbolic links and malformed or
duplicate archive paths are rejected. Unknown format versions fail closed.

## Documents

`MIND.md` and `NOW.md` are bounded user-authored documents. Context rendering
quotes stored content and labels it as data so text in the vault is not treated
as higher-priority instructions.

## Backups

A backup contains vault files plus a manifest of relative names and SHA-256
digests. Archive-entry and total expanded sizes are validated separately before
contents are read. Verification checks the manifest before restore. Restore writes
to a staging directory and publishes the completed vault only after every
member passes validation. Checksums detect accidental corruption; they are not
cryptographic authentication or encryption.
