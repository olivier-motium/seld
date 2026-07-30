# Trust model

## Protected outcomes

- A stale writer cannot silently replace a newer canonical record.
- An interrupted write does not expose a partial canonical record.
- A caught audit-append failure either restores exact prior bytes, reports that
  the mutation committed, or reports unknown integrity without guessing.
- A malformed, oversized, encrypted, duplicate, or path-traversing backup does
  not escape the restore target.
- Backup creation never replaces an existing destination and cannot report a
  concurrently swapped published archive as the staged backup. It never exposes
  an incomplete final-path archive while falling back from hard links.
- A failed install does not replace the last working executable or remove
  pre-existing Codex components.
- A stale or changed update check cannot authorize a different source revision,
  and a failed source update either verifies the restored runtime or retains an
  exact recovery transaction instead of guessing.
- Uninstall removes only expected `gsv`-owned integration and preserves the records folder,
  configuration, backups, and unrelated Codex state.
- Marketplace replacement and uninstall recheck an atomically isolated tree;
  lifecycle trees are retained under receipt-bound paths, never recursively or
  manifest-deleted, and destination races preserve both trees.
- MCP requests are bounded, validated, and mapped to the same kernel as the
  CLI.
- Connector credentials never enter `CONNECTIONS.md`, an ordinary vault
  backup, Bridge, MCP output, status output, or a command-line argument.
- Connector secret custody fails closed unless an approved operating-system
  keyring backend is available. Concurrent OAuth callers serialize refresh and
  publish one new token version under compare-and-swap.
- Credential export is explicit, age-encrypted before publication, bound to one
  exact vault ID and connection revision, and import refuses a different
  snapshot or different destination custody while allowing only an exact
  unverified state with matching already-published credentials to resume.
- Selecting the `local_files` source grants no path. Directory authority stays
  in an owner-only host record bound to one exact vault-directory identity and
  selected root; grant changes or same-path vault replacement make earlier
  source proof require revalidation, and MCP cannot create or revoke a grant.
- Selecting `discord` grants no provider authority. A CLI-only host receipt
  binds one exact bundled GET-only companion runtime and one explicit
  bot-only Seld connection. On POSIX the verified open artifact, rather than a
  later path lookup, is handed to its pinned interpreter. The bot token is
  resolved only inside the operation from OS-keyring custody; raw channel IDs
  remain confined to the host environment and owner-only companion checkpoint.
  Neither enters the portable vault or generated MCP manifest. Poll cannot
  advance its cursor until Seld fresh-reads a matching
  account/tool/cursor/coverage/delivery receipt, and a changed executable,
  connection, credential version, account, or channel set fails closed.
- A loopback caller without the current per-launch bearer cannot read the
  Bridge snapshot or health identity.
- A stale, forged, or PID-reused Bridge receipt cannot cause Seld to signal an
  unrelated process.
- A stale Bridge control writer cannot append against a newer queue revision,
  and a Bridge control event cannot directly author or replace semantic canon.
- A Bridge control event receives at most one durable accept/reject disposition;
  stale queue or disposition revisions are rejected, and a fresh process reads
  the same result.
- Bridge, CLI, and MCP queue/disposition reads leave those append-only logs
  byte-for-byte unchanged; their crash recovery belongs to an explicit
  mutation. Guided-review snapshot and transport-status reads use a separate
  content-free receipt lane: polling may durably classify an orphaned in-flight
  receipt as `delivery_uncertain`, which disables replay without changing
  semantic canon.

## Bridge boundary

Every Bridge launch creates a random bearer capability and instance identity.
The owner-only state receipt stores the token with mode `0600` where POSIX
permissions apply. Seld normalizes the Bridge application-data directory to
`0700` and its diagnostic log to `0600` on POSIX, and refuses a symbolic-link
or non-regular log path. The browser receives the token after `#`, moves it
into session storage, and strips it from the address before API requests.
Query strings, normal referrers, and static-asset requests do not carry it.

An authenticated health timeout while the receipt PID is still alive preserves
the receipt and blocks open, stop, upgrade, and uninstall from guessing. A
concrete connection refusal on the receipt's validated loopback port clears the
receipt without signalling its unverified PID, so a later open can recover. A
dead PID or a concrete authenticated identity mismatch also clears the stale
receipt; only a complete identity match permits signalling.

The server binds only to `127.0.0.1`, validates the exact Host and Origin for
its bound port, and serves only bounded regular files inside its packaged
static root. It exposes two authenticated write routes. `POST /api/v1/control`
is a size-bounded, compare-and-swap append to the local intent queue; it accepts
only setup choices, approvals, corrections, and undo requests. `POST
/api/v1/review-turn` advances only the content-free transport-receipt lane and
may launch the restricted same-hand Codex transport; it cannot author semantic
canon. The CLI and MCP operation surfaces may durably accept or reject an intent
receipt, but disposition does not execute it or grant external-action authority.
Each mutation requires the logical vault ID and both CAS revisions returned by
the preceding operation snapshot. A live MCP process additionally binds to the
physical vault root it opened, so copied tokens and same-path replacement
cannot cross a vault boundary.
The supported host lifecycle keeps one Bridge server active for a vault. If
independently constructed servers race on one transport receipt, receipt CAS
fails closed to an uncertain delivery state rather than replaying the turn.
Stopping or killing Bridge does not prove that an already detached ChatGPT task
stopped: the parent owns an eight-minute timeout for each Codex CLI invocation,
while the child runs in its own process session. A START uses two sequential
invocations and can therefore take about sixteen minutes plus bounded setup;
the browser may stop foreground polling earlier while durable reconciliation
continues. A later Bridge therefore marks the orphaned
receipt `delivery_uncertain`, preserves any exact task, and never replays it.
Automatic same-hand continuation remains unavailable for that receipt until
the user reconciles the exact task and queued intent; Bridge keeps the safe
recovery path visible instead of replaying it.
Neither the receipt nor its disposition can directly write Tasks, Entities,
WorkThreads, `MIND.md`, `NOW.md`, onboarding, source readiness, grants, or
action policy. Every HTTP write method and path other than those two exact POST
routes remains denied. The
browser renders authored content using `textContent`, not HTML interpolation.

The control queue and its CLI/MCP disposition surfaces use the secure
directory-pinned storage primitive available on the current macOS consumer
path. Windows support is coming; the repository's portability code is not a
Windows consumer-support claim.

The bearer restores the intended same-user local-file boundary against casual
loopback reads. It is not a security boundary against hostile code already
running as the same OS user, which can inspect the local vault or process-owned
state.

## Connector authentication boundary

Portable connection metadata and secret custody are separate. The vault keeps
only provider, source, account label or fingerprint, scopes, public-client
registration, health, timestamps, and revisions. The host-local pointer files
name opaque keyring slots and contain no credential bytes. Keyring service names
are scoped to the logical vault ID.

Only local connector-auth code, including an explicit encrypted transfer, may
resolve a credential. `gsv-auth status` and the read-only
`gsv_connection_list` MCP tool return redacted availability. There is no MCP
method to add, reveal, export, import, authorize, refresh, or remove a
credential. OAuth token requests refuse redirects, bound response sizes, and
use one exact loopback origin assembled by Seld rather than trusting the HTTP
Host header.

The encrypted transfer archive necessarily contains credentials after
decryption. Seld sends plaintext to `age` over a pipe and never publishes a
plaintext archive, but the user remains responsible for the age identity and
destination file. Import accepts only the exact restored portable snapshot or
its derived in-progress state, requires empty or byte-exact resumable slots,
and makes provider readiness unverified before secret publication and until a
bounded real read succeeds.

This boundary trusts the local operating-system user. A hostile process already
running as that user may invoke the keyring or inspect a connector process; the
keyring is custody and portability separation, not same-user process isolation.

## Source-update boundary

The source updater accepts only an official macOS `uv` tool environment whose
package provenance, commit SHA, launcher, and receipt agree. It does not support
prebuilt or frozen binaries, Windows, another repository, an editable checkout,
or an unrecognized environment.

`gsv update status` is offline. Only the explicit CLI `gsv update check` may use
the network, and normal checks are mechanically limited to one public request
cycle per six-hour host cache. The request target is fixed to the public Seld
repository on `api.github.com` over TLS; redirects, oversized responses, and
unexpected repository or commit shapes fail closed. Bridge and MCP can project
the cached receipt but have no network-check or apply method, and no Bridge
browser POST route can apply an update.

An installable candidate must be one exact 40-character SHA descended from the
installed SHA on public `main`. GitHub must mark that commit verified, and every
returned GitHub Actions check run must form the complete reported set and be
successful for the same exact head SHA. The installed updater also requires the
named Seld release suite, so a lone green replacement workflow is insufficient.
The updater then asks `uv` for that exact commit. This trusts GitHub's TLS,
verification, and check-run metadata; it is not a claim that the source is a
separately signed or notarized release artifact. The Seld source revision is
exact, but `uv` resolves its declared Python dependencies at install time; this
source channel does not claim a separately locked or signed dependency bundle.

Application is interactive. The packaged skill shows the exact from/to SHAs
and check-receipt revision, reads the current ChatGPT task UUID, obtains fresh
approval in that task, and records it as `codex:<uuid>`. The CLI rejects a
changed installed SHA, candidate, or check revision. Pulse may run the
non-forced check and decide whether to surface it, but it may never call apply
or recovery under the resident skill policy. The current ChatGPT host does not
provide an attested interactive-task capability to the local CLI, so the
`codex:<uuid>` reference is durable approval evidence and stale-state binding,
not a cryptographic proof of who invoked the command. The structural boundary
is narrower: Bridge, browser routes, and MCP expose no apply or recovery method.

Before any candidate code executes, Seld creates and verifies a vault backup.
It then preflights the candidate with isolated Seld, ChatGPT, config, data,
home, and temporary roots. It preserves the current environment, installs the
exact candidate, reruns setup, and verifies source provenance, vault identity
and a frozen activation digest, doctor, ChatGPT integration, and prior Bridge
lifecycle from a fresh process. Before the environment moves, an owner-only
`seld-recover` launcher is durably published outside uv's managed `gsv` path;
it selects only the transaction's preserved or active runtime, strips ambient
Python injection variables, and is removed after a resolved outcome. The vault
writer lock is held only across setup and those checks, not network access,
staging, or installation. Legitimate earlier writes become the protected
digest, and within-attempt drift fails closed. Writes after an already audited
terminal outcome become the next attempt's baseline. Drift after setup could
have started remains ambiguous until an interactive recovery binds the exact
current digest and a fresh ChatGPT task approval. Failure restores the previous
runtime only after its source SHA matches the transaction and that runtime proves it
can read the current vault and control ledger. An interrupted state is retained
under one token-bound transaction for explicit `gsv update recover`; recovery
never chooses a different target or rewrites vault data. A state that cannot be
proved safe becomes `repair_required` and retains its evidence for explicit
operator repair instead of retrying automatically. Once repaired, the same
token can re-prove and close that transaction. Only `installed` or `rolled_back`
with no remaining recovery command may be superseded by another update.

## Assumptions

Seld trusts the local operating-system account, the installed Codex command it
discovers, and the executable the user chose to run. File locks coordinate
cooperating Seld processes; they do not defend against a hostile same-user
process.

Durability and lock behavior assume a local filesystem with ordinary host OS
semantics. Cloud-synchronized and network filesystems are not a supported
durability boundary for the vault.

Vault text, imported references, provider content, and repository pages are
untrusted data. They do not gain authority to run commands, mutate external
systems, alter account settings, or override Codex instructions.

Browser opening is best-effort after durable setup commits. A browser failure
does not roll back Codex integration or configuration.

## Non-goals

- Vault encryption, remote synchronization, multi-user authorization, and
  tamper-proof audit logs.
- Multi-file atomicity or a complete audit journal across process or operating-
  system death between canonical replacement and event append.
- A hosted credential broker, automatic credential synchronization, provider
  client registration, confidential-client OAuth, OIDC identity, or device
  authorization flow.
- Mechanical task prioritization, semantic identity merging, or unattended
  consequential external action.
- An OS scheduler that decides meaning, priority, or work. Pulse is AI-authored;
  the local layer may wake, bound, persist, and audit it but cannot replace that
  judgment.
- Protection after the host, user account, Codex installation, or release
  signing identity is compromised.

## Distribution

The current public distribution installs source from the GitHub repository.
Its self-update path is limited to the official macOS `uv` source environment
described above. Prebuilt and Windows self-update remain unsupported.
Optional prebuilt installers verify the binary against a published SHA-256 file.
A checksum fetched from the same compromised release is not an independent
trust root, so a prebuilt artifact may be called signed or notarized only when
that exact platform verification exists.

The privacy gate scans the working tree, generated reports, release artifacts,
and reachable Git history for likely credentials, user-home paths, and private
terms. Oversized or unreadable inputs fail closed and are reported by filename
only. History scanning covers reachable blob contents; commit messages and
author metadata are outside that scanner's scope.
