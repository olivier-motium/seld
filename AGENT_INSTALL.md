# Install Seld with an agent

## Outcome

Install Seld from its public source distribution on macOS, preserve all
existing Seld and ChatGPT state, prove that durable work survives a fresh
process, and leave the local Bridge open so the user can begin context-first
onboarding. The executable and command remain named `gsv` for compatibility.

## Non-negotiable boundaries

- Never use `sudo` or request an account, credential, or security-setting
  change.
- Never delete or replace Seld user data or unrelated/unowned Codex state. The
  verified installer may transactionally upgrade `gsv`-owned executable and
  integration components only when it can preserve or roll them back.
- Never treat a hand-built binary or editable checkout as equivalent to the
  public source distribution.
- Never install an asset whose platform, version, or SHA-256 cannot be
  established.
- If repository access, `uv`, the ChatGPT desktop app, or the required local
  command surface is unavailable, stop and report the exact gap.

## Consumer path

1. Open `https://github.com/olivier-motium/seld` and confirm this file belongs to
   that repository.
2. Confirm the host is macOS and the ChatGPT desktop app is installed. Windows
   and Claude support are coming. Do not modify `PATH` merely to make discovery
   pass.
3. Confirm Python 3.11+ and `uv` are available. Inspect `command -v gsv`; never
   force-replace an executable that does not belong to Seld.
4. Install the current public source distribution:

   ```bash
   uv tool install 'git+https://github.com/olivier-motium/seld.git'
   ```

   If the Seld-managed tool already exists and the user asked to update it,
   use `uv tool install --force` with the same URL. Do not clone a private
   checkout, copy credentials, or build an ad hoc binary.
5. Use the installed absolute path if the install directory is not already on
   `PATH`:

   - macOS: `${HOME}/.local/bin/gsv`
6. Run setup, then the checks below without reading or exporting private vault
   content:

   ```bash
   gsv setup
   gsv doctor
   gsv demo
   gsv bridge status
   ```

   `gsv demo` uses an isolated temporary synthetic vault. It must report a
   child-written revision recovered by a fresh process, stale-write rejection,
   crash-artifact recovery, and equivalent backup/restore digests.
7. Run `gsv` once if the Bridge did not open automatically. Browser launch is
   best-effort and is not allowed to roll back a verified setup.
8. Ask the user to restart the ChatGPT desktop app after first setup, open one
   fresh task, and run
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

## Optional prebuilt validation

A prebuilt install is valid only when an exact platform artifact and its
`.sha256` file exist in the same GitHub release. The release installer verifies
that checksum before replacement. For local release validation, build the
standalone artifact, record its SHA-256, and feed both to the installer via
`GSV_BINARY` and `GSV_BINARY_SHA256`; do not present that local build as the
public distribution. Do not tag, publish, dispatch a release, change visibility,
or push without separate authorization.
