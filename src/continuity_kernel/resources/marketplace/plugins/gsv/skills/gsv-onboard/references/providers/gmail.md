# Gmail connection check

Use this note after the person selects Gmail and either the host-owned Gmail app
or a Seld-managed `google` connection is available. Never ask for or handle a
Google credential, authorization code, token, or second factor in chat.

1. Ask for the expected mailbox. With the host-owned app, use its read-only
   profile call when available. With Seld-managed auth, the person confirms the
   account shown by Google's consent page; `gsv_connector_source_read` returns a
   stable hashed account binding, not the mailbox address, so describe that
   identity as user-confirmed rather than independently revealed by Seld.
2. For Seld-managed auth, use the exact `google` connection ID and source
   `gmail`. Read one small recent mail window. An explicitly empty result is
   valid. Do not draft, send, label, archive, delete, forward, or alter read
   state during onboarding.
3. One Seld-managed Google grant carries Gmail, Calendar, and Drive's fixed
   read-only scopes. They remain separate logical reads and coverage receipts;
   success here does not prove Calendar or Drive coverage.
4. If a host-owned tool is missing, confirm that app and open one new task. For
   Seld-managed auth, inspect redacted `gsv-auth status`; the person alone may
   rerun `gsv-auth oauth`, select the account, consent, or resolve Workspace
   policy. Never fall back to an AI-host or browser session.
5. Return the identity basis, bounded-read result, tool shape, coverage horizon,
   and stable references to `$gsv-onboard`. That skill alone records the result
   through fresh source-state CAS.
