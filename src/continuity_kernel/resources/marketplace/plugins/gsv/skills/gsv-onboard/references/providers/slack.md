# Slack

Use this note after the person selects Slack. A host-owned Slack app may satisfy
the bounded Pulse recipe. Portable Seld custody uses its own logical Slack
connection and never copies a ChatGPT cookie, browser session, or ambient token.

## Connect

```bash
gsv connectors connect slack --access read --alias 'Work Slack'
gsv connectors connect slack --access full --alias 'Work Slack'
```

Read grants the typed identity, user, conversation, message, thread, file, and
reaction read catalog. Full adds direct-message, channel, message/thread, file,
and reaction mutations. The command opens Slack's one-way PKCE OAuth flow on
the fixed loopback redirect, verifies both workspace and member with
`auth.test`, shows the result in human terms, and asks `Use this account? [y/N]`
before publishing. The default is no. A Read connection remains ready during a
same-account Full upgrade.

The person owns workspace selection, consent, administrator approval, and
second factors. The agent must not change the Slack application or workspace,
select the account, consent, or enter credentials. If the installed build lacks
a public Slack client registration, sign-in stops and saves nothing.

## Verify the source

Use `gsv connectors status slack`, confirm the expected workspace and member,
then use the narrow `gsv_connector_source_read` Pulse path for one bounded
recent conversation window under the exact host-private source policy. A
successful empty result counts. This bound proves coverage and does not limit
the separate interactive connector.

The isolated `gsv_connectors` server exposes `gsv_slack_read` and
`gsv_slack_write`. It translates only cataloged operations to fixed Slack API
methods, treats HTTP 200 with `ok:false` as provider failure, and keeps page
continuations sealed. Messages, replies, files, reactions, and membership-
affecting operations are outward and require a bound preview/confirmation when
classified that way. Archive/delete operations are destructive or permanent as
cataloged. Large files use Slack's response-derived external upload flow while
the provider URL remains process-local and pinned to Slack-owned hosts.

Return only the identity basis and bounded-read facts to `$gsv-onboard`. Never
store a channel ID, token, raw cursor, workspace/member ID, provider upload URL,
or provider error body in the vault.
