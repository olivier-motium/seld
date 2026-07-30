# Outlook Email connection check

Use this note after Outlook Email is selected and either its host-owned app or a
Seld-managed `microsoft` connection is available. The person owns sign-in,
tenant/account selection, consent, conditional access, and second factors.

1. Ask for the expected mailbox and, where relevant, tenant. With a host-owned
   app, use a read-only profile call when available. With Seld-managed auth, the
   person confirms the account shown by Microsoft; the reader returns a stable
   hashed Graph account binding, not a raw mailbox or tenant identifier.
2. For Seld-managed auth, call `gsv_connector_source_read` with the exact
   `microsoft` connection ID and source `outlook_mail`. Read one bounded recent
   mail window. A successful empty result counts.
3. Do not draft, send, forward, move, flag, archive, delete, or change read
   state. One Microsoft profile grants the fixed Outlook mail and calendar
   scopes, but each logical source needs its own read and receipt. Teams and
   SharePoint are not implemented by this profile.
4. Missing host tools get one new-task retry after enablement. For Seld-managed
   auth, inspect redacted status and let the person handle OAuth, wrong-mailbox,
   tenant, administrator, or conditional-access failures. Never reuse another
   AI host or browser session.
5. Return the identity basis and bounded-read facts to `$gsv-onboard` for the
   exact source-state read, CAS write, and readback.
