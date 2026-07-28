# Installation

Installing Seld adds the local Bridge, bundled `gsv` plugin, skills, and durable
records to Codex. It preserves existing Codex configuration and Seld data,
creates no Seld cloud account, and can be removed without deleting the
person's records. The executable and command remain named `gsv`.

## Current release status

`0.2.0` is Unreleased and the repository is private. No public `0.2.0` binary
currently exists, and the `0.1.0` release does not contain the Bridge. The
consumer commands below become live only after the exact `0.2.0` assets and
checksums are published.

A predecessor `0.2.0` development snapshot passed local macOS Apple Silicon
checks. That evidence does not transfer to this candidate. Every target,
including macOS Apple Silicon, remains unpromoted until the exact release bytes
pass their own hosted build and clean-install run.

## Agent-led install

Give Codex or another coding agent the repository URL and the complete prompt
in the README. [`AGENT_INSTALL.md`](../AGENT_INSTALL.md) is the machine-readable
operator contract. It requires the agent to stop instead of substituting a
source install when repository access, a matching release asset, or its
checksum is unavailable.

## Consumer prerequisites

Seld needs an installed Codex command surface. The macOS discovery code checks
`GSV_CODEX`, `PATH`, and the installed Codex Desktop app bundle; Codex
does not need to be added to `PATH` when that bundle contains the command.
Source-level discovery behavior is not a support claim until the exact artifact
passes its hosted acceptance run on that target.

A release install does not require Python, `uv`, or `make`. The POSIX installer
uses HTTPS plus `sha256sum` or `shasum`; the Windows installer uses PowerShell's
`Invoke-WebRequest` and `Get-FileHash`.

## Consumer commands

After `0.2.0` is published, macOS and Linux:

```bash
curl --proto '=https' --tlsv1.2 -fsSLO \
  https://raw.githubusercontent.com/olivier-motium/gsv/main/scripts/install.sh
sh install.sh
```

Windows PowerShell:

```powershell
Invoke-WebRequest `
  https://raw.githubusercontent.com/olivier-motium/gsv/main/scripts/install.ps1 `
  -OutFile install.ps1
.\install.ps1
```

The installer downloads the pinned platform artifact and `.sha256`, verifies
the binary, and stages it in the target directory. On upgrade, the staged
candidate stops only a Bridge whose authenticated live identity matches its
owner-only receipt before replacing the old binary. Setup commits the verified
Codex integration receipt before starting Bridge. A later Bridge-start failure
returns the distinct installed-repair exit status `4`, keeps the candidate and
committed integration, preserves any previous executable as recovery evidence,
and never prints that Bridge is ready. It does not trigger executable rollback.
If setup fails before that commit, the installer stops any candidate
Bridge and restores the old executable only after that exact executable proves,
through read-only status, doctor, and supported control-ledger reads, that it
can read the current state without drift. Otherwise the candidate remains in
place and the previous executable is preserved as named recovery evidence.

## What setup changes

- macOS/Linux executable: `${HOME}/.local/bin/gsv`
- Windows executable: `%LOCALAPPDATA%\GSV\bin\gsv.exe`
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

After a healthy first setup, restart Codex, open one fresh task, and run
`$gsv-onboard`. The skill gathers context in conversation and persists only
accepted Mind material through document CAS; this foundation does not expose a
durable OnboardingSession or connector-readiness command.

## Verify first run

```bash
gsv doctor
gsv demo
gsv bridge status
```

`gsv demo` creates and removes an isolated temporary synthetic vault. It does
not read the configured vault. `gsv` with no arguments opens the Bridge for the
configured vault.

Restart Codex after first setup so the new marketplace, plugin, and managed
instructions are loaded.

## Offline or controlled install

Obtain a candidate binary and its expected checksum through the approved
channel, verify its provenance independently, then run:

```bash
GSV_BINARY=/path/to/gsv \
GSV_BINARY_SHA256='<expected sha256>' \
sh scripts/install.sh
```

PowerShell accepts the same two environment variables.

The macOS candidate is not yet Developer ID signed or notarized. A browser may
quarantine a downloaded copy. After independently verifying its checksum and
provenance, use macOS System Settings > Privacy & Security > Open Anyway if
Gatekeeper blocks it. Do not disable Gatekeeper globally.

## Upgrades

Run the installer again. The verified staged candidate quiesces a live Bridge
before executable replacement, then setup starts the candidate Bridge. If the
PID is alive but authenticated health is temporarily unavailable, upgrade
aborts without replacing the executable or deleting the receipt; retry after
checking `gsv bridge status` or `gsv bridge stop`.

Setup is idempotent and pre-commit integration rollback removes only components
added by the failed invocation. A compatible binary rollback restores the old
executable and attempts to restore its prior Bridge lifecycle. If a new
ownership receipt becomes visible but its write reports failure, first install
removes only those exact new bytes; an upgrade restores and durability-checks
the exact prior receipt. A concurrent or unrelated user change is left
untouched.

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

## Source development

Source development is deliberately separate from the consumer promise:

```bash
uv sync --extra dev --extra release --extra browser-test
uv run playwright install chromium
uv run python scripts/verify_bridge_browser.py
uv run gsv setup
```

That path requires Python 3.11+ and `uv`. It is not evidence that the standalone
consumer artifact installs on a clean machine.

## Removal

`gsv codex uninstall` removes `gsv`-owned Codex integration while keeping the
executable. The release uninstall script first stops only a Bridge instance
whose live health identity matches its owner-only receipt. It removes the
executable only after both the active integration and receipt-bound recovery
catalog are retired.

Both paths preserve the vault, configuration, backups, unrelated Codex
instructions, marketplaces, and plugins. Neither Codex status nor uninstall
requires a readable vault configuration. Delete the vault or config only as a
separate, deliberate user-data operation.

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
