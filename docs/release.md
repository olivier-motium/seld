# Release Process

## Inputs

Releases are built from an immutable Git tag. A manual `workflow_dispatch` runs
the same build and acceptance matrix without publishing, so every target can be
proved before the first tag. The workflow installs the locked development and
release environments, runs lint, formatting, strict typing, tests, coverage,
the privacy gate, and package builds before producing native executables.
The quality job also installs an isolated Chromium runtime and drives the real
bearer-gated Bridge through healthy, inspector, mobile, stale, and unavailable
states. The gate persists no screenshots, GIFs, or other generated media.

## Assets

The release matrix produces:

- `gsv-macos-arm64`
- `gsv-macos-x86_64`
- `gsv-linux-x86_64`
- `gsv-linux-arm64`
- `gsv-windows-x86_64.exe`

Windows ARM64 uses the x64 asset through Windows' built-in emulation until a
native ARM64 artifact has passed the same release gates. That emulated path is
not validated until its own hosted E2E evidence exists.

The Linux x86_64 binary is built on Ubuntu 22.04 and targets glibc 2.35 or
newer. The Linux ARM64 binary is currently built on Ubuntu 24.04 ARM and targets
glibc 2.39 or newer. These are compatibility floors, not generic claims that
every Linux distribution is supported.

Each executable has a sibling `.sha256` file. The release also carries the
install and uninstall scripts. GitHub artifact attestations bind published
executables and checksums to the workflow run. Each E2E report records the
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

GitHub provenance and SHA-256 files are implemented. Apple notarization,
Developer ID signing, and Windows Authenticode require publisher-owned signing
credentials and must be added before claiming those platform trust indicators.
The build must never persist signing credentials in the repository or artifact.

Publishing a repository, tag, release, or package is an external action and is
separate from local release validation.

A predecessor macOS Apple Silicon development asset passed two independent
MCP-process sessions and installed Codex integration/manifest checks. That
evidence is non-transferable and does not validate this candidate. A
model-backed native second Codex task and a green manual hosted-runner matrix
remain release-promotion gates; public platform support remains unclaimed until
the exact release bytes produce the corresponding evidence.
