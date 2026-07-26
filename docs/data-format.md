# Data Format

## Vault Layout

```text
GSV/
  .gsv/
    manifest.json
    locks/
  DIRECTION.md
  MIND.md
  NOW.md
  PORTFOLIO.md
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

`PORTFOLIO.md` is a typed authored judgment over the complete nonterminal Task
set. Version 2 carries one exact Direction revision. Its ordered items carry an
exact Task revision, stance, reason, optional exact owning WorkThread revision,
and either exact Direction aim IDs or an explicit unaligned reason. The order
is authored priority; the kernel does not derive it from rank, age, status, due
dates, activity, or text. `portfolio set` requires the current Portfolio
revision (`absent` for the first write), complete open-set coverage, and fresh
Direction, Task, and WorkThread anchors. Version 1 remains readable for
migration but has no Direction alignment authority. Once Direction exists, the
next complete Portfolio write is the explicit version-1-to-version-2 migration:
it fails with a validation error until the current Direction revision and an
alignment or explicit unaligned reason for every item are supplied.

Tasks may carry an optional authored integer `rank` and one opaque
`active_thread_id`. Rank is task truth, independent of Portfolio order. The
active hand is execution continuity, not evidence of progress or completion;
terminal Tasks cannot retain one.

WorkThreads carry an optional `focus_task_id` that must name one of their exact
`task_ids`. A closed WorkThread cannot retain focus. The bounded review session
belongs to `thread:life-portfolio-review`, which focuses that session Task while
the review is active or paused.

## Guided review references

One nonterminal review-session Task carries deterministic navigation facts:

```text
review-scope:all-open
review-subject:task:<task-id>
review-state:paused
review-option:<intent>:task:<subject-task-id>:<canonical-percent-encoded-consequence>
review-covered:task:<task-id>@<task-sha256>
review-covered:task:<task-id>@<task-sha256>|thread:<thread-id>@<thread-sha256>
```

Canonical option encoding leaves only `A-Z`, `a-z`, `0-9`, `_`, `.`, `-`, and
`~` unescaped. Every other UTF-8 byte is percent-encoded with uppercase
hexadecimal digits.

Scope appears exactly once. Subject and paused state appear at most once.
Options name the exact current subject Task and use one of `keep`, `act-next`,
`defer`, `reprioritize`, `reshape`,
`drop-or-merge`, or `skip`, at most once each. A current subject may carry at
most five options. Their one-line consequence text is an agent-authored,
standalone answer of at most 200 characters for the exact subject. Invisible
format controls and duplicate case-insensitive answers are rejected. Bridge
decodes and queues that exact visible answer but never supplies meaning or a
hidden payload. Replacing a subject also replaces its option refs.
Coverage records the exact Task revision checked and, when the Portfolio item
has an owning WorkThread, that exact thread revision too. Legacy unanchored
`review-covered:task:<task-id>` refs from unreleased development builds remain
readable only for explicit migration; they are stale and never count as current
coverage. Duplicate, malformed, wrong-owner, or conflicting review refs fail
closed.

One review-session Task currently supports at most 512 open outcomes as a
product contract. The typed Portfolio can represent a larger open set, but a
single Task's 256 KiB record envelope cannot carry worst-case revision-aware
coverage for all 10,000 Portfolio items. Larger guided reviews remain a
promotion blocker until bounded checkpointing or compaction exists; they must
not be presented as complete.

Coverage is a checked-on-these-bytes navigation fact. A Task or owning
WorkThread revision change invalidates it; a newly open Task has no coverage and
joins the remaining set. Neither case changes Portfolio order or semantics
mechanically. A terminal review Task retains its historical scope and coverage
but must clear subject, paused state, and active hand; its owning WorkThread
must clear focus.

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

The full-vault logical digest covers both canonical Markdown and the private
`.gsv/control/` intent, disposition, archive, and transport-receipt lane. It is
a backup-fidelity signal, not a canon-only "did my semantic records change"
digest.
