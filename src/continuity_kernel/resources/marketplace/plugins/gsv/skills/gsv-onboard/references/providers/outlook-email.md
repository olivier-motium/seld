# Outlook Email connection check

Use this note after Outlook Email is selected and its app tools appear in a
fresh ChatGPT task. Microsoft owns sign-in, tenant selection, consent, and
conditional-access checks.

1. Ask for the exact expected mailbox and, where relevant, tenant. Use a
   read-only profile call when available. Verify the tenant only from an
   immutable tenant-bearing result, never from the email domain alone.
2. Read one bounded recent mail window. A successful empty result counts.
3. Do not draft, send, forward, move, flag, archive, delete, or change read
   state. Outlook Email, Calendar, Teams, and SharePoint remain separate grants.
4. Missing tools get one new-task retry after enablement. Auth, wrong-mailbox,
   tenant, admin, or conditional-access failures return to ChatGPT settings or
   the administrator before another probe.
5. Return the identity status and bounded-read facts to `$gsv-onboard` for the
   exact source-state read, CAS write, and readback.
