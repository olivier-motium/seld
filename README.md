# GSV

## Keep your work with Codex in one place.

GSV keeps a local record of what you are trying to get done, what already
happened, and what should happen next. You keep using Codex Desktop. Open the
private GSV dashboard to see your work and create a Codex task for any
unfinished item.

When a Codex task ends, fails, or gets replaced, the work record stays. GSV
stores those records as readable files on your computer. There is no GSV
account, hosted database, or cloud service.

> **`0.2.0` is private and unreleased.** The public install path is not live
> yet. [See what remains to validate](#release-status).

## What this looks like

| You ask Codex | The saved GSV record keeps |
| --- | --- |
| “Continue the failed release.” | Why CI failed, the decision already made, and the next check. |
| “Pick up the recruiter reply.” | Who it concerns, the current status, and the next action. |
| “Finish the invoice review.” | Which items are verified or missing, decisions already made, and the next check. |

The work does not have to be software. GSV records an outcome, status, related
people or projects, supporting references, and the next action you chose to
save. It does not guess priorities from chat history or activity.

## What you can do

- **See what is open.** The local dashboard shows work that is ready, in
  progress, waiting, saved for later, or closed, including who acts next when
  that has been recorded.
- **Open an unfinished item in Codex.** GSV creates a new-task link with a
  prompt that names the work item and points Codex to your local GSV folder.
- **Keep decisions from being overwritten.** If an older process tries to save
  over newer work, GSV rejects the update.
- **Inspect and back up your GSV records.** Tasks, context, people, projects,
  and longer-running work are plain Markdown files.

## A normal GSV flow

```text
Ask Codex to do something
        ↓
Codex reads or updates the relevant local record
        ↓
GSV shows what happened and what comes next
        ↓
Open that item in Codex when you are ready to continue
```

The current candidate proves the file and process recovery path with synthetic
data. A real continuation into a second Codex task is still being validated
and is not claimed as complete.

## How it fits with other tools

| Product | Its job | Where GSV fits |
| --- | --- | --- |
| **Codex** | Lets you ask AI to write code, review changes, and carry out work. | GSV keeps the saved work those Codex tasks can read and update on your computer. |
| **OpenClaw** | Lets you reach and run agents through messaging apps. | GSV keeps you in Codex instead of adding another chat surface. |
| **Hermes Agent** | Runs an agent with its own tools, memory, schedules, and messaging. | GSV keeps Codex as your agent app and organizes its work locally. |

## On your computer

GSV stores its records in `~/GSV` by default. They are normal Markdown files,
so Git, `grep`, backup tools, and optional local search tools can inspect them.
The dashboard listens only on your computer and is read-only. It never decides
what a task means or marks work complete from browser activity.

GSV has no hosted service, database server, embedding model, API key, cloud
sync, or autonomous background worker. These limits describe GSV itself;
Codex has separate processing and account boundaries.

## Release status

The macOS Apple Silicon `0.2.0` candidate has passed local installation and
recovery checks. Other platforms and a real continuation into a second Codex
task remain release gates. No public `0.2.0` build exists yet,
and the published `0.1.0` release does not include the dashboard.

## Install

Once a matching `0.2.0` build is published, give Codex this instruction:

> Open https://github.com/olivier-motium/gsv and read `AGENT_INSTALL.md`.
> Install the verified release for this computer without replacing existing
> GSV or Codex data. Open GSV and show me what is open, what is waiting, and
> what is assigned to me. If no matching verified release exists, stop and
> tell me.

The agent-led installer selects the platform build, verifies its SHA-256,
preserves unrelated Codex configuration, opens the local dashboard, and runs
health and recovery checks. A consumer install does not need Python, `uv`, or
`make`.

The shell and PowerShell installers are documented in
[Installation](docs/installation.md). Building from source is a maintainer
path while `0.2.0` remains unreleased.

## See the recovery proof

Run GSV with no arguments to open the dashboard:

```bash
gsv
```

Run the isolated proof without reading your real GSV files:

```bash
gsv demo
```

The demo verifies that:

1. one process saves a new task update and then stops;
2. a fresh process reads the exact saved update;
3. an older copy cannot overwrite newer work;
4. an interrupted save can be repaired;
5. a verified backup restores the same files and saved records.

Unavailable email coverage is labeled synthetic. The demo does not claim that
a live connector was disabled or tested.

## Everyday commands

```bash
gsv                  # Open the local dashboard
gsv doctor           # Check files, Codex integration, and local health
gsv status           # Show a compact status summary
gsv bridge status    # Check the dashboard process
gsv backup create    # Create a verified backup without overwriting one
gsv bridge stop      # Stop the local dashboard
```

Backup creation refuses unsupported files, unsafe destinations, or a name that
already exists. Verification and restore still work if the current
configuration is unavailable:

```bash
gsv backup verify /path/to/gsv-backup.zip
gsv backup restore /path/to/gsv-backup.zip /path/to/restored-gsv
```

Restore verifies files before publishing them and never silently switches your
active GSV folder. See [Installation](docs/installation.md) for the complete
backup and restore contract.

## Remove it

Remove only the Codex integration while keeping the executable:

```bash
gsv codex uninstall
```

Release uninstallers remove GSV-owned integration and executable files while
preserving your GSV folder, configuration, backups, and unrelated Codex data:

```bash
sh scripts/uninstall.sh
```

```powershell
.\scripts\uninstall.ps1
```

If ownership or provider cleanup cannot be verified, removal stops and prints
the exact recovery command instead of guessing.

## Technical documentation

- [Product contract](docs/product-contract.md)
- [Installation and recovery](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Trust model](docs/trust-model.md)
- [Codex integration evidence](docs/codex-integration.md)

## Develop

Source development requires Python 3.11+ and `uv`:

```bash
uv sync --extra dev --extra release --extra browser-test
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=continuity_kernel --cov-report=term-missing
uv run playwright install chromium
uv run python scripts/verify_bridge_browser.py
uv run python scripts/privacy_check.py .
```

`make` is an optional developer convenience. GSV is licensed under Apache-2.0.
Contributions use the Developer Certificate of Origin described in
[CONTRIBUTING.md](CONTRIBUTING.md).
