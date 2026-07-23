# Data Format

## Vault Layout

```text
GSV/
  .gsv/
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
<!-- gsv:{"id":"ship-atlas","kind":"task",...} -->
# Ship Atlas

## Outcome

Atlas is deployed with rollback evidence.
```

The `revision` returned by the CLI and MCP API is the SHA-256 digest of the
complete stored Markdown bytes, including their exact line endings. Content is
decoded and validated from that same bounded read. The revision is computed,
not stored in the record. Updates require that exact revision.

## Identity And Relations

Task IDs are lowercase slugs such as `ship-atlas`. Entity and work-thread IDs
are typed IDs such as `company:atlas` and `thread:atlas-release`. Filenames must
agree with record identity. Relationships are explicit ID lists; names, text
similarity, aliases, and timestamps never create edges automatically.

New task IDs also reject Windows-reserved basenames such as `con`, `nul`,
`com1`, and `lpt1`. A pre-0.2 vault that already contains one of those task IDs
remains readable on its source platform, but its backup is intentionally not
portable. Rename that task and update explicit references on the source
platform before creating a cross-platform backup; GSV never silently rewrites
an existing record identity.

## Bounds

Records, text fields, references, relationships, context output, archive
members, and total restored bytes are bounded. Symbolic links and malformed or
duplicate archive paths are rejected. Unknown format versions fail closed.

## Documents

`MIND.md` and `NOW.md` are bounded user-authored documents. Context rendering
quotes stored content and labels it as data so text in the vault is not treated
as higher-priority instructions.

## Context Pack

The context pack always preserves its fixed header plus Mind, Now, open-task,
and active-work-thread sections. Mind and Now become clearly marked prefix
excerpts when needed, with exact stored-character omission counts. Task and
work-thread records are included only as complete blocks. The pack considers
those records in canonical identifier order and admits a block only when it
fits; this mechanical capacity rule is not a priority or recency signal. Both
record sections report included, total, and omitted counts, including when no
record fits. The final rendered string is never repaired by raw slicing.

## Journal

`journal/events.jsonl` is a bounded, append-only audit aid for successful GSV
mutations. Markdown remains authoritative. A caught append failure has an
explicit restored, committed, or unknown outcome; GSV rolls canonical bytes
back only after the journal is known restored. The journal is not a multi-file
transaction log and can lag canonical Markdown if the process or operating
system dies in the narrow interval between the two durable writes. `gsv doctor
--repair` can remove only an invalid, non-terminated final fragment after every
complete preceding record validates; it synchronizes the truncation and never
synthesizes an event or changes canonical Markdown. Complete invalid records
and valid events missing only their final newline remain for manual review.

## Backups

A backup contains vault files plus a manifest of relative names and SHA-256
digests. Archive-entry and total expanded sizes are validated separately before
contents are read. Portable paths exclude platform aliases and control
characters. Verification checks the manifest before restore. Backup creation
publishes the complete same-directory stage with a hard link or native atomic
no-replace move, never a partial copy into the destination, and verifies the
staged inode and digest around archive validation. Restore writes to a staging directory and
publishes the completed vault only after every member passes validation.
Failed unpublished restore stages remain named recovery evidence; they are not
recursively removed by doctor repair.
Checksums detect accidental corruption; they are not cryptographic
authentication or encryption.
