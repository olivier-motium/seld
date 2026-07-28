# Prebuilt release process

Seld's current public distribution installs from source. This document covers
the additional process for publishing checksummed native executables; it does
not gate the source distribution.

## Inputs

The current workflow is a manual, non-publishing macOS candidate gate. It
installs the locked development and
release environments, runs lint, formatting, strict typing, tests, coverage,
the privacy gate, and package builds before producing native executables.
The quality job also installs an isolated Chromium runtime and drives the real
bearer-gated Bridge through healthy, inspector, mobile, stale, and unavailable
states. The gate persists no screenshots, GIFs, or other generated media.

## Assets

The candidate matrix produces:

- `gsv-macos-arm64`
- `gsv-macos-x86_64`

Each executable has a sibling `.sha256` file. GitHub artifact attestations bind
candidate executables and checksums to the workflow run. Each E2E report records the
exact binary SHA-256, product version, and observation time, and is privacy
scanned after it is generated.

## Acceptance Gates

An asset is not release-ready until its exact bytes pass:

1. Standalone version and MCP handshake smoke tests.
2. Privacy scanning, including configured private terms.
3. Installation into isolated config, data, home, Codex, temp, and vault roots.
4. Actual Codex marketplace and plugin registration.
5. Fresh-process create/read, stale-write rejection, interrupted-write repair,
   backup verification, logical restore equivalence, and full uninstall while
   preserving user data.
6. Native two-session Codex resume on at least the promoted desktop platform.
7. Authenticated Bridge lifecycle, browser-error resilience, packaged static
   resources, and real desktop/mobile/stale/unavailable rendering.
8. Reinstall over a verified live Bridge, with old-process exit, a fresh
   candidate instance, and unchanged vault digest.
9. Independent final review with every finding explicitly dispositioned.

CI can exercise the local Codex CLI contract with synthetic roots. A native
model-backed Codex session depends on runner authentication and is therefore a
release-promotion proof, not a unit-test substitute. Authenticate the isolated
Codex home directly for that proof; the harness has no credential-copy path.
Never copy or use live Codex authentication in shared CI.

## Signing

GitHub provenance and SHA-256 files are implemented. Apple notarization and
Developer ID signing require publisher-owned signing credentials and must be
added before publishing the candidate as a prebuilt consumer release.
The build must never persist signing credentials in the repository or artifact.

The workflow intentionally cannot publish a tag or GitHub release. Publishing
remains a separate external action after signing/notarization and exact-byte
installed-path evidence are complete.

The public source install remains available independently of this artifact
pipeline. Promote a prebuilt platform only after the exact asset completes the
matrix above; do not reuse a source-tree or predecessor-binary result as that
asset's signing or installed-path evidence.
