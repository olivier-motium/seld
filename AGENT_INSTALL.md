# Install Seld with an agent

## Outcome

Install the verified standalone Seld artifact for this machine, preserve all
existing Seld and Codex state, prove that durable work survives a fresh
process, and leave the private Bridge open so the user can begin context-first
onboarding. The shipped executable and command remain named `gsv`.

## Non-negotiable boundaries

- Never use `sudo` or request an account, credential, or security-setting
  change.
- Never delete or replace Seld user data or unrelated/unowned Codex state. The
  verified installer may transactionally upgrade `gsv`-owned executable and
  integration components only when it can preserve or roll them back.
- Never treat a source checkout, Python environment, or hand-built binary as a
  consumer release unless the user explicitly asks for maintainer validation.
- Never install an asset whose platform, version, or SHA-256 cannot be
  established.
- If repository access, the matching release, Codex, or a required checksum is
  unavailable, stop and report the exact gap. Do not improvise another install
  path.

## Consumer path

1. Open `https://github.com/olivier-motium/seld` and confirm this file belongs to
   that repository.
2. Read the candidate status in `README.md` and the current version in
   `pyproject.toml`. Continue only when that exact version has a published
   release with an asset and `.sha256` file matching this OS and architecture.
3. Confirm Codex is installed. Seld checks the `codex` command and, on validated
   macOS builds, the Codex Desktop app bundle. Do not modify `PATH` merely to
   make discovery pass.
4. From a temporary or existing checkout, run the repository installer:

   macOS or Linux:

   ```bash
   sh scripts/install.sh
   ```

   Windows PowerShell:

   ```powershell
   .\scripts\install.ps1
   ```

   The installer selects the platform asset, downloads its checksum, verifies
   SHA-256, and stages the candidate beside the target. On upgrade it stops
   only an identity-verified Bridge before replacement. If setup fails, it
   restores the previous executable only when that exact binary proves it can
   read the unchanged current vault and control ledger, then attempts to
   restart a previously live Bridge. If compatibility is unproven, keep the
   candidate installed and preserve the previous binary as named recovery
   evidence. A post-commit Bridge failure is an installed repair state: do not
   roll back or claim the Bridge is ready.
5. Use the installed absolute path if the install directory is not already on
   `PATH`:

   - macOS/Linux: `${HOME}/.local/bin/gsv`
   - Windows: `%LOCALAPPDATA%\GSV\bin\gsv.exe`
6. Run these checks without reading or exporting private vault content:

   ```bash
   gsv doctor
   gsv demo
   gsv bridge status
   ```

   `gsv demo` uses an isolated temporary synthetic vault. It must report a
   child-written revision recovered by a fresh process, stale-write rejection,
   crash-artifact recovery, and equivalent backup/restore digests.
7. Run `gsv` once if the Bridge did not open automatically. Browser launch is
   best-effort and is not allowed to roll back a verified setup.
8. Ask the user to restart Codex after first setup, open one fresh task, and run
   `$gsv-onboard`. Report the installed version, binary path, doctor result,
   demo result, Bridge state, and any unavailable or unvalidated platform
   claim. Onboarding keeps context gathering in the AI conversation; never
   invent a durable onboarding session or mark a connector ready without a
   supported verified surface.

## What setup may change

Setup may create a default `~/GSV` records folder when no existing Seld config
selects one. It may also create a local marketplace for Seld, install the
bundled `gsv` plugin, add one bounded managed block to Codex's `AGENTS.md`, and
write `gsv`-owned config and receipts. The transaction preserves pre-existing
content and rolls back only components added by that invocation.

The Bridge binds to loopback on an ephemeral port. Its private snapshot API
requires a per-launch bearer capability stored in an owner-only local state
file and delivered to the browser in the URL fragment.

## Unreleased maintainer validation

`0.2.0` is currently Unreleased. Building and installing it from source is a
maintainer path, not the consumer promise. When the user explicitly requests
that path, build the standalone artifact, record its SHA-256, and feed both to
the same installer via `GSV_BINARY` and `GSV_BINARY_SHA256`. Run the clean-home
E2E before making any support claim. Do not tag, publish, dispatch a release,
change visibility, or push without separate authorization.
