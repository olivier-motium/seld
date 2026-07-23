# Installation

## Current release status

`0.2.0` is Unreleased and the repository is private. No public `0.2.0` binary
currently exists, and the `0.1.0` release does not contain the Bridge. The
consumer commands below become live only after the exact `0.2.0` assets and
checksums are published.

The exact `0.2.0` candidate is currently validated locally on macOS Apple
Silicon. macOS Intel, Linux x86_64/ARM64, and Windows x86_64 remain unpromoted
until the exact candidate passes its own hosted build and clean-install run.

## Agent-led install

Give Codex or another coding agent the repository URL and the complete prompt
in the README. [`AGENT_INSTALL.md`](../AGENT_INSTALL.md) is the machine-readable
operator contract. It requires the agent to stop instead of substituting a
source install when repository access, a matching release asset, or its
checksum is unavailable.

## Consumer prerequisites

GSV needs an installed Codex command surface. On the validated macOS path, it
checks `GSV_CODEX`, `PATH`, and the installed Codex Desktop app bundle; Codex
does not need to be added to `PATH` when that bundle contains the command.
Discovery on other platforms is not a support claim until that exact artifact
passes its hosted acceptance run.

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
owner-only receipt before replacing the old binary. If setup fails, it stops
any candidate Bridge, restores the old binary, and best-effort restarts a
previously live Bridge. A failed restart is reported explicitly.

## What setup changes

- macOS/Linux executable: `${HOME}/.local/bin/gsv`
- Windows executable: `%LOCALAPPDATA%\GSV\bin\gsv.exe`
- Default vault, only when no configured vault exists: `${HOME}/GSV`
- Codex home: `${CODEX_HOME}` or `${HOME}/.codex`
- A generated local marketplace under GSV's application-data directory
- The GSV Codex plugin and one bounded managed block in Codex's `AGENTS.md`
- An ownership receipt and config file pointing to the vault
- A private Bridge state receipt in GSV's application-data directory
- A private Bridge log in that same directory

Set `GSV_BIN_DIR`, `GSV_VAULT`, `GSV_CONFIG_DIR`, `GSV_DATA_DIR`, or
`CODEX_HOME` before installation to override those locations.

Setup verifies and stages the Codex integration before starting the Bridge.
Only after the integration transaction commits does it ask the default browser
to open the private Bridge session. Browser launch is best-effort: an
`OSError` or browser error returns `browser_opened: false`, keeps setup
committed, and prints `run gsv` as the next step.

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

Setup is idempotent and integration rollback removes only components added by
the failed invocation. Binary rollback restores the old executable and attempts
to restore its prior Bridge lifecycle. If a new ownership receipt becomes
visible but its write reports failure, first install removes only those exact
new bytes; an upgrade restores and durability-checks the exact prior receipt.
A concurrent or unrelated user change is left untouched.

Back up the vault before any future release that announces a format migration:

```bash
gsv backup create
gsv backup verify /path/to/backup.zip
```

Default backup names include a random suffix and are published without
replacing an existing path. Explicit output paths must be absent, and paths
inside the vault are accepted only below its owned `backups/` directory. GSV
identifies the vault and owned backup directory by filesystem identity, so case
or Unicode aliases cannot bypass that policy. It excludes only exact
GSV-writer temporary names; legitimate files that merely contain `.tmp-` are
included. GSV fails closed when it cannot enumerate every source entry or
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
is visible but its directory sync fails, GSV returns a nonzero committed-state
error naming the published target and the exact doctor command; it does not
pretend the target stayed untouched or retry over it. A prior empty target that
cannot be removed after publication is preserved and reported as a cleanup
warning.

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
uv sync --extra dev --extra release --extra visual
uv run gsv setup
```

That path requires Python 3.11+ and `uv`. It is not evidence that the standalone
consumer artifact installs on a clean machine.

## Removal

`gsv codex uninstall` removes GSV-owned Codex integration while keeping the
executable. The release uninstall script first stops only a Bridge instance
whose live health identity matches its owner-only receipt, then removes the
executable and GSV-owned integration.

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
automatically: GSV restores a packaged, digest-checked removal scaffold at the
receipt-bound path, lets Codex remove and verify the live registrations, then
deletes the scaffold. Immediately after provider verification GSV checkpoints
that phase and an exact owned-file manifest in the receipt. If local cleanup
raises after removing only some entries, re-run `gsv codex uninstall`;
the retry skips Codex and removes only an unchanged subset of the recorded
files. Cleanup first moves the tree to an exclusive sibling path, rescans it,
then removes only exact manifest entries bottom-up. A newly appeared entry
prevents directory removal and is restored or preserved for manual review.

The recovery scaffold is built in an exclusive sibling stage, each file and
nested directory is synchronized, and the complete tree is published without
replacement before any provider call. Duplicate GSV plugin IDs or marketplace
names are provider ambiguity: GSV performs no add or remove action and asks for
explicit inspection.

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
