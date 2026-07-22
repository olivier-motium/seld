# GSV

GSV gives Codex durable, inspectable memory across tasks and sessions. It keeps
tasks, entities, evidence, and work threads in local, versioned Markdown without
a hosted service, database server, embedding model, API key, or background
daemon.

The project is an early release candidate. Its format is intentionally small
and human-readable, but backward compatibility is not guaranteed before `1.0`.

## Install

Prerequisite: Codex Desktop or the Codex CLI must be installed and `codex` must
be available on `PATH`.

The currently validated consumer release candidate is macOS 15 on Apple
Silicon. The repository also defines macOS Intel, Linux x86_64/ARM64, and
Windows x86_64 assets, but those targets are not promoted as supported until
their exact hosted-runner builds complete the release workflow successfully.

After a release is published, macOS and Linux users can run:

```bash
curl --proto '=https' --tlsv1.2 -fsSLO \
  https://raw.githubusercontent.com/olivier-motium/gsv/main/scripts/install.sh
sh install.sh
```

Windows users can run this in PowerShell:

```powershell
Invoke-WebRequest `
  https://raw.githubusercontent.com/olivier-motium/gsv/main/scripts/install.ps1 `
  -OutFile install.ps1
.\install.ps1
```

The installer downloads the platform-specific standalone executable and its
SHA-256 checksum. It does not require Python, `uv`, or `make`. It stages the new
binary in the destination directory, replaces an existing binary atomically,
and restores the previous binary if setup fails.

Setup creates `~/GSV` by default, registers a local Codex marketplace
and plugin through the Codex CLI, and adds one managed block to Codex's
`AGENTS.md`. Existing instructions are preserved. Restart Codex after setup,
open a fresh task, and ask:

> What do you remember?

See [Installation](docs/installation.md) for exact paths, offline installation,
upgrades, and removal.

## Verify

```bash
gsv doctor
gsv status
gsv demo
gsv context
```

`gsv demo` uses and removes an isolated temporary vault containing synthetic data.
It demonstrates same-name entity disambiguation, fresh-process resumption,
stale-write rejection, crash recovery, backup verification, and logical restore
equivalence.

## Use

```bash
gsv task create --id ship-atlas --title "Ship Atlas" \
  --outcome "Atlas is deployed with rollback evidence" \
  --status doing --next-actor agent \
  --next-action "Run the failover test"

gsv task list
gsv context
gsv backup create
gsv doctor
```

Every update requires the exact `revision` returned by the preceding read:

```bash
gsv task update ship-atlas \
  --expected-revision '<sha256 revision>' \
  --next-action "Record the observed failover result"
```

A stale revision fails instead of overwriting a newer record.

## Uninstall

The integration-only command removes installer-owned Codex components and
leaves the executable installed:

```bash
gsv codex uninstall
```

The release uninstall scripts remove the executable and Codex integration:

```bash
sh scripts/uninstall.sh
```

```powershell
.\scripts\uninstall.ps1
```

Both paths preserve the vault, configuration, backups, unrelated Codex
settings, and components the installer does not own.

## Boundaries

GSV is not a chat application, autonomous scheduler, vector database,
connector marketplace, secret store, or cloud sync service. Markdown is
authoritative. The CLI and MCP server share the same typed kernel. Provider
integrations, remote execution, and background polling are outside the initial
trust boundary.

Read the [documentation index](docs/INDEX.md), [architecture](docs/architecture.md),
[trust model](docs/trust-model.md), and [data format](docs/data-format.md) before
extending the kernel.

## Development

Source development requires Python 3.11+ and `uv`:

```bash
uv sync --extra dev --extra release
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=continuity_kernel --cov-report=term-missing
uv run python scripts/privacy_check.py .
```

`make` is an optional developer convenience, never a consumer prerequisite.
The project is licensed under Apache-2.0. Contributions use the Developer
Certificate of Origin described in [CONTRIBUTING.md](CONTRIBUTING.md).
