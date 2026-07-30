# Slack connection check

Use this note after Slack is selected and either its host-owned app or a Seld-
managed `slack` connection is available. Never copy a ChatGPT/Slack cookie or
session, accept an ambient token, or ask for a credential, code, or second
factor in chat.

1. Ask for the expected workspace and member account. With a host-owned app,
   use a read-only identity result when available. With Seld-managed auth, the
   person confirms the workspace/member during Slack's consent flow;
   `auth.test` is required but Seld returns only a stable hashed account
   binding.
2. A Seld-managed connection uses the exact built-in `slack` profile. The
   person creates the Slack app, enables Slack's documented one-way PKCE
   setting, registers one fixed
   `http://localhost:<port>/oauth/callback`, reviews the four user history
   scopes, and runs `gsv-auth oauth`. The agent may explain and inspect redacted
   status but must not change the app, consent, select the account, or enter a
   credential.
3. Launch the Seld MCP process with one exact private `SLACK_CHANNEL_ID` and
   call `gsv_connector_source_read` with source `slack` plus the portable
   connection ID. The reader validates that policy before token resolution,
   requires a user OAuth identity, calls only `auth.test` and one
   `conversations.history` request, and returns at most 15 recent items.
4. This read covers only the chosen conversation. It does not expand thread
   replies and must not be described as thread coverage. Do not send, draft,
   react, edit, delete, invite, change membership, or alter channel state.
5. If a host-owned tool is absent, enable it and open one new task. For Seld-
   managed auth, inspect redacted status and let the person resolve OAuth,
   workspace, enterprise, or administrator policy. A missing host-private
   channel ID is a setup gap, not permission to fall back to another token or
   session.
6. Return the identity basis and bounded-read facts to `$gsv-onboard` for the
   CAS-protected, content-free Seld receipt. Never store the channel ID, token,
   raw cursor, workspace/member ID, or provider error body in the vault.
