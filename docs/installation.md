# Installation

Installing Seld adds the local Bridge, bundled `gsv` plugin, skills, and durable
records to the ChatGPT desktop app. It preserves existing ChatGPT configuration and Seld data,
creates no Seld cloud account, and can be removed without deleting the
person's records. The executable and command remain named `gsv`.

## Supported path

Seld is public and installable from its source distribution on macOS. The
current path uses `uv`, installs the `gsv` command directly from the public
repository, and then lets Seld configure its bundled plugin, skills, and local
Bridge:

```bash
uv tool install 'git+https://github.com/olivier-motium/seld.git'
gsv setup
```

If `uv` reports that the Seld-managed `gsv` tool is already installed, update
it deliberately and run setup again:

```bash
uv tool install --force 'git+https://github.com/olivier-motium/seld.git'
gsv setup
```

Do not force replacement when an unrelated program owns the existing `gsv`
command. Check `command -v gsv` first and resolve that collision explicitly.

The source-install command was exercised against the frozen 0.3.0 implementation
candidate at commit `fcffb499a9759b8e3790cde9a25b82cd30f467c6` on 2026-07-28. It
built and installed `gsv==0.3.0`, exposed the CLI from a fresh isolated tool
directory, returned a healthy `gsv doctor` result, and completed `gsv demo` with
fresh-process resume, stale-write rejection, interrupted-write recovery, and
backup/restore equivalence.

The current consumer surface is the ChatGPT desktop app on macOS. Windows and
Claude support are coming.

## Agent-led install

Give ChatGPT or another coding agent the repository URL and the complete prompt
in the README. [`AGENT_INSTALL.md`](../AGENT_INSTALL.md) is the machine-readable
operator contract. It uses the same public source distribution and preserves
existing state.

## Consumer prerequisites

Seld needs Python 3.11 or newer, `uv`, and the installed ChatGPT desktop app on
macOS. The discovery code checks `GSV_CODEX`, `PATH`, and the installed app
bundle; the bundled command
does not need to be added to `PATH` when that bundle contains the command.

Prebuilt installers are a separate distribution channel. Use one only when its
GitHub release contains the matching platform artifact and `.sha256` file. The
installer verifies that checksum before replacement and preserves the vault;
the supported public source path above does not depend on a prebuilt asset.

## Standalone connector authentication

The standard source install includes the Python `keyring` adapter so connector
custody survives the same exact-SHA install and self-update path as `gsv`.
Verify the entrypoint and the selected operating-system backend before adding a
credential:

```bash
gsv-auth status
```

The adapter is loaded only when connector custody is inspected or changed. An
unavailable, disabled, or unapproved keyring backend fails closed without
affecting the rest of the local record.

The `gsv-auth` flow is independent of the ChatGPT, OpenAI, Codex, OpenCode, or
Open Interpreter login. It never imports an existing browser or AI-host
session. A provider-specific connector still needs its own public OAuth client
registration or credential, granted by the user through that provider's normal
consent flow.

Encrypted credential transfer additionally requires the open-source `age`
executable. On macOS it can be installed separately with `brew install age`.
Keep the age identity outside the vault and its backups. See
[Standalone connector authentication](connector-auth.md) for setup, transfer,
and recovery commands.

## What setup changes

- `uv` tool executable: normally `${HOME}/.local/bin/gsv`
- Default vault, only when no configured vault exists: `${HOME}/GSV`
- Codex home: `${CODEX_HOME}` or `${HOME}/.codex`
- A generated local marketplace under Seld's application-data directory
- The bundled `gsv` Codex plugin and one bounded managed block in Codex's `AGENTS.md`
- An ownership receipt and config file pointing to the vault
- A private Bridge state receipt in Seld's application-data directory
- A private Bridge log in that same directory

Set `GSV_BIN_DIR`, `GSV_VAULT`, `GSV_CONFIG_DIR`, `GSV_DATA_DIR`, or
`CODEX_HOME` before installation to override those locations.

Setup verifies and stages the Codex integration before starting the Bridge.
Only after the integration transaction commits does it ask the default browser
to open the private Bridge session. Browser launch is best-effort: an
`OSError` or browser error returns `browser_opened: false`, keeps setup
committed, and prints `run gsv` as the next step.

After a healthy first setup, restart the ChatGPT desktop app, open one fresh
task, and run `$gsv-onboard`. The skill gathers context in conversation,
discovers the source tools available in that task, verifies selected sources
with bounded live reads, and persists only accepted Mind material through
document CAS.

## Verify first run

```bash
gsv doctor
gsv demo
gsv bridge status
```

`gsv demo` creates and removes an isolated temporary synthetic vault. It does
not read the configured vault. `gsv` with no arguments opens the Bridge for the
configured vault.

Restart the ChatGPT desktop app after first setup so the new marketplace, plugin, and managed
instructions are loaded.

## Optional QMD acceleration

Seld's bounded Markdown recall works without QMD. For semantic acceleration, install a clean
QMD 2.5.3 or newer runtime under Node.js 22 or newer using QMD's supported global install path:

```bash
npm install -g '@tobilu/qmd@^2.5.3'
gsv recall status
gsv recall rebuild
```

Seld invokes QMD only from `/opt/homebrew/bin/qmd`, `/usr/local/bin/qmd`, or an explicit absolute
`--executable` path; it does not trust a PATH-only executable. Each vault gets its own disposable,
privacy-screened snapshot and collection binding. A first rebuild may download local models and use
substantial disk and compute. If QMD is absent, broken, stale, or points at another collection,
`gsv recall search` stays on bounded Markdown recall and reports that fallback honestly. QMD never
becomes canonical state.

## Controlled prebuilt install

Obtain a candidate binary and its expected checksum through the approved
channel, verify its provenance independently, then run:

```bash
GSV_BINARY=/path/to/gsv \
GSV_BINARY_SHA256='<expected sha256>' \
sh scripts/install.sh
```

A locally built macOS binary may be unsigned and unnotarized. Gatekeeper may
quarantine it. After independently verifying its checksum and provenance, use
macOS System Settings > Privacy & Security > Open Anyway if Gatekeeper blocks
it. Do not disable Gatekeeper globally.

## Upgrades

Seld's resident updater supports the official `uv` source install on the current
macOS consumer path. It refuses frozen or prebuilt binaries, Windows installs,
unrecognized tool environments, and source installs that do not prove they
came from the official public repository.

Read local update state without network access, then request a bounded check:

```bash
gsv update status
gsv update check
```

`status` reads only owner-local receipts. `check` contacts the fixed public
GitHub API only when the local six-hour cache is due; `--force` is reserved for
an explicit interactive recheck. The candidate is one exact 40-character SHA
descended from the installed revision on public `main`. GitHub must report it as
verified, the complete exact-head GitHub Actions check set as successful, and
every named Seld release job as present. Seld then installs that exact SHA
rather than a moving branch name.

Pulse may call the non-forced check once per wake and let the AI decide whether
to mention the result. Its installed skill forbids apply and recovery. Bridge,
browser POST routes, and MCP structurally read cached update state only and
contain no update network or installation method.

Use `$gsv-update` in a current interactive ChatGPT task to review the exact
`installed.sha`, `candidate.sha`, and `check_revision`, then approve this exact
command:

```bash
gsv update apply \
  --from-sha '<installed.sha>' \
  --to-sha '<candidate.sha>' \
  --expected-check-revision '<check_revision>' \
  --approval-ref 'codex:<current-task-uuid>'
```

The updater validates that the installed revision and cached check still match
the approval, creates and verifies a vault backup, then stages the exact
candidate with isolated Seld, ChatGPT, config, data, home, and temporary roots
for its version and synthetic continuity proof. It stops only an authenticated
live Bridge and preserves the current tool environment before installing the
candidate. A fresh candidate process
must rerun setup, report the expected source SHA, prove the same vault identity,
pass doctor with ChatGPT integration ready, and restore the prior Bridge
lifecycle. Seld freezes the current vault digest only for setup and the
fresh-process checks: legitimate writes completed earlier remain intact, while
any canonical change during activation is detected and fails the transaction
instead of being silently accepted.

If candidate verification fails, Seld first proves the preserved runtime can
still read the current vault and control ledger, then restores that environment
and verifies it. Before moving the active environment, Seld also publishes one
owner-only `seld-recover` entrypoint outside uv's managed `gsv` path. If a
process or the machine stops while `gsv` is being replaced, a fresh process can
still inspect the exact transaction:

```bash
seld-recover --json update status
```

The normal status surface, or this recovery-only entrypoint during the swap,
exposes the exact token, phase, and recovery command. After fresh interactive
approval, resume only that transaction:

```bash
gsv update recover --token '<transaction.token>'
```

Recovery either finishes the checked candidate or restores the preserved
environment; it never selects a new revision. If neither environment can be
proved safe, Seld retains both and returns `repair_required` instead of deleting
evidence or guessing. If the vault changed after candidate or restored setup
could have run, Seld does not decide whether those bytes are legitimate. The
reported recovery command binds the current vault digest and requires fresh
approval from the current ChatGPT task before re-anchoring that one attempt.
After the reported repair or approval, the same token-bound recover command
re-verifies and closes the transaction. A transaction is clean
only when it reports `installed` or `rolled_back` with no recovery command; a
new update cannot overwrite retained repair or cleanup lineage. A prebuilt
installer, Windows install, or manual tool replacement does not use this
self-update transaction.

Executable rollback is not a data down-migration. Multi-subject review Tasks
and review answers above 4 KiB use explicit version-2 stored shapes; an older
reader must reject those shapes rather than reinterpret or truncate them. Seld
never restores a vault snapshot merely to make an old executable start. Manual
downgrades are unsupported unless that exact older executable first proves it
can read the current vault and control ledger.

Back up the vault before any future release that announces a format migration:

```bash
gsv backup create
gsv backup verify /path/to/backup.zip
```

Default backup names include a random suffix and are published without
replacing an existing path. Explicit output paths must be absent, and paths
inside the records folder are accepted only below its owned `backups/` directory. Seld
identifies the vault and owned backup directory by filesystem identity, so case
or Unicode aliases cannot bypass that policy. It excludes only exact
`gsv`-writer temporary names; legitimate files that merely contain `.tmp-` are
included. Seld fails closed when it cannot enumerate every source entry or
encounters a symlink or other special file. Publication uses a hard link when
available and otherwise a platform atomic no-replace move of the complete
same-directory stage. It never streams partial bytes into the final path and
reports an unsupported filesystem when neither primitive exists. It checks the
published path against the staged inode and SHA-256 immediately before and
after archive verification; a target swap is a degraded nonzero result rather
than a successful backup.

Verification and disaster-recovery restore are config-independent. This is
intentional: both commands remain usable when `config.json` is absent or
unreadable, including invalid UTF-8, symlinks, FIFOs, and invalid configured
paths. Configuration reads are bounded, no-follow, and regular-file-only.
Other commands report invalid configuration as a structured validation error.
A hash mismatch is reported
as `valid: false`, `ok: false`, and a nonzero exit; restore never publishes
that archive.

```bash
gsv backup restore /path/to/backup.zip /path/to/restored-vault
gsv --vault /path/to/restored-vault status
```

Restore reads one pinned archive, writes into a private sibling stage, compares
the staged regular files with the manifest, and runs doctor before publication.
The target must be absent or an empty real directory. On success the result
reports `published: true` and `durability_confirmed: true`. If the final rename
is visible but its directory sync fails, Seld returns a nonzero committed-state
error naming the published target and the exact doctor command; it does not
pretend the target stayed untouched or retry over it. A prior empty target that
cannot be removed after publication is preserved and reported as a cleanup
warning. An unpublished failed restore retains its exact private stage and names
that path in the nonzero result. Doctor reports retained stage directories for
manual inspection; `doctor --repair` does not recursively delete them.

Restore never rewrites configuration. It reports whether existing configuration
already matches the target when that can be determined safely. Activation is a
deliberate second step:

```bash
gsv bridge stop
gsv --vault /path/to/restored-vault setup
```

The first command must verify and stop the currently owned Bridge. The second
configures the restored vault and rebinds the Codex integration and Bridge.

## Contributor setup

To work on Seld itself:

```bash
uv sync --extra dev --extra release --extra browser-test
uv run playwright install chromium
uv run python scripts/verify_bridge_browser.py
uv run gsv setup
```

That path requires Python 3.11+ and `uv`; it installs development, release, and
browser-test dependencies rather than the minimal public tool.

## Removal

For the supported `uv` source install, remove the running surface, owned
ChatGPT app integration, and managed tool environment in that order:

```bash
gsv bridge stop
gsv codex uninstall
uv tool uninstall gsv
```

Do not run the final command until `gsv codex uninstall` reports complete
cleanup. If it retains recovery evidence, follow its exact retry instruction
first so the executable remains available for verified cleanup.

The release uninstall script is only for a checksummed prebuilt binary. It
first stops a Bridge instance whose live health identity matches its owner-only
receipt, then removes the executable after the active integration and
receipt-bound recovery catalog are retired:

```bash
sh scripts/uninstall.sh
```

Both removal paths preserve the vault, configuration, backups, unrelated
Codex instructions, marketplaces, and plugins. Neither Codex status nor
uninstall requires a readable vault configuration. Delete the vault or config
only as a separate, deliberate user-data operation.

If the Codex executable is unavailable, a command times out, or final provider
verification fails, cleanup is explicitly partial. The uninstaller may remove
the managed instruction block, but it keeps the digest-matched generated
marketplace available until Codex has removed and verified the registrations.
That preserves the provider manifest needed by a retry. It also keeps the
executable and owner-only receipt for the printed retry command, and returns a
nonzero retry exit without requiring `jq` or another JSON parser.

A valid older receipt whose generated marketplace is already missing is handled
automatically: Seld first records a unique repair transition, builds a packaged,
digest-checked removal scaffold at that receipt-bound path, and lets Codex
remove and verify the live registrations. Immediately after provider
verification Seld checkpoints that phase and one exact no-replace quarantine
path. Cleanup moves the public tree there and retains it as immutable recovery
evidence; it never recursively or manifest-deletes lifecycle trees. The active
integration can therefore be completely removed while the result separately
reports `recovery_retained` and every `retained_cleanup_paths` entry. A changed
or unexpected entry blocks verification and is preserved for manual review.

Uninstall result objects use `result_format_version: 1`. The fields describe
separate facts: `cleanup_complete` is true only when active integration and
receipt-bound recovery bytes are both gone; `integration_removed` means the public
marketplace and managed instruction block are absent;
`marketplace_files_removed` is true only when no marketplace bytes remain;
`recovery_retained` means immutable receipt-bound evidence still exists; and
`receipt_state` reports whether the exact catalog receipt is still owned. A
successful retained quarantine therefore reports `cleanup_complete: false`,
`integration_removed: true`, `marketplace_files_removed: false`, and
`recovery_retained: true` without conflating those outcomes.

When recovery evidence remains, the release uninstaller prints its exact paths,
keeps the executable, and exits with retry status `3`. This is an intentionally
incomplete physical cleanup even though `integration_removed` is true. Inspect and delete only
those paths if you choose to retire the evidence, then re-run the uninstaller;
the recovery-only pass compacts the catalog and removes the executable once no
retained bytes remain.

The release scripts also fail closed on malformed or unexpected cleanup output.
They remove the executable only when the cleanup output is verified to report
`ok: true` and `result.cleanup_complete: true`; an exit code alone is never
treated as proof of complete cleanup.

The recovery scaffold's exact path and manifest are durable before its first
directory appears. Each file and nested directory is synchronized, and the
complete tree is published without replacement before any provider call.
Duplicate `gsv` plugin IDs or marketplace names are provider ambiguity: Seld
performs no add or remove action and asks for explicit inspection.

A changed, unreadable, symlinked, or out-of-bound generated marketplace is left
untouched for manual review. Provider registration cleanup is skipped until
that local state is resolved, so a modified tree is not orphaned by removing
its ownership evidence. An explicit but invalid `GSV_CODEX` override fails
closed before changing integration state.

The ownership receipt is also fail-closed: it must be a versioned JSON object
bound to the exact Codex home, with typed ownership flags and a valid path and
SHA-256 for owned marketplace files. A malformed or mismatched receipt is left
byte-for-byte unchanged, no integration state is touched, and the release
uninstaller keeps the executable.
