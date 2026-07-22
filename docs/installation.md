# Installation

## Consumer Prerequisite

Install Codex Desktop or the Codex CLI and ensure `codex` is available on
`PATH`. A release install does not require Python, `uv`, or `make`.

The POSIX installer uses HTTPS plus `sha256sum` or `shasum`; the Windows
installer uses PowerShell's `Invoke-WebRequest` and `Get-FileHash`.

## What Setup Changes

- macOS/Linux executable: `${HOME}/.local/bin/continuity`
- Windows executable: `%LOCALAPPDATA%\Continuity\bin\continuity.exe`
- Default vault: `${HOME}/Continuity`
- Codex home: `${CODEX_HOME}` or `${HOME}/.codex`
- A generated local marketplace under Continuity's application-data directory
- One bounded managed block in Codex's `AGENTS.md`
- A small ownership receipt and a config file pointing to the vault

Set `CONTINUITY_BIN_DIR`, `CONTINUITY_VAULT`, `CONTINUITY_CONFIG_DIR`,
`CONTINUITY_DATA_DIR`, or `CODEX_HOME` before installation to override those
locations.

## Offline Or Controlled Install

Download a release asset and checksum through your approved channel, verify the
checksum independently, then set:

```bash
CONTINUITY_BINARY=/path/to/continuity \
CONTINUITY_BINARY_SHA256='<expected sha256>' \
sh scripts/install.sh
```

The PowerShell installer accepts the same two environment variables.

Release-candidate macOS binaries are not yet Developer ID signed or notarized.
Safari or another browser may therefore quarantine a manually downloaded copy.
After independently verifying the checksum and provenance, use macOS System
Settings > Privacy & Security > Open Anyway if Gatekeeper blocks it. Do not
disable Gatekeeper globally.

## Upgrades

Run the installer again. It stages and verifies the candidate before moving the
current binary aside. Setup is idempotent. If setup fails, the candidate is
removed and the prior executable is restored. Integration rollback removes
only components added by that failed invocation.

Back up the vault before a future release that announces a format migration:

```bash
continuity backup create
continuity backup verify /path/to/backup.zip
```

## Source Development

The checkout path is deliberately separate from the consumer path:

```bash
uv sync --extra dev --extra release
uv run continuity setup
```

That path requires Python 3.11+ and `uv`; the release installer does not.

## Removal

`continuity codex uninstall` removes the plugin registration, owned marketplace,
generated marketplace files, managed instructions, and receipt. The release
uninstall script also removes the executable. Both preserve the vault and
configuration so reinstalling resumes the same state.

Neither Codex status nor Codex uninstall requires a readable Continuity vault
configuration. This keeps recovery possible when only the integration needs to
be inspected or removed.

Delete the vault or config only as a separate, deliberate user-data operation.
