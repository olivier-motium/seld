# Google Calendar connection check

Use this note after Google Calendar is selected and either its host-owned app or
a Seld-managed `google` connection is available. Never request credentials,
authorization codes, tokens, or second factors.

1. Ask which Google account and calendar set the person expects. With a host-
   owned app, use a read-only identity call when available. With Seld-managed
   auth, the person confirms the account during Google's consent flow and Seld
   returns only a stable hashed calendar/account binding.
2. For Seld-managed auth, call `gsv_connector_source_read` with the exact
   `google` connection ID and source `google_calendar`. Read a bounded current
   window, including a successful empty window. Keep the covered time span
   separate from the observation time.
3. Do not create, edit, move, RSVP to, or delete events. One Google profile also
   grants Gmail and Drive's fixed read-only scopes, but those sources still need
   separate reads and receipts.
4. Missing host tools require one new-task retry after enablement. For Seld-
   managed auth, inspect redacted status and let the person handle OAuth,
   account selection, Workspace policy, or administrator approval. Do not use
   another host session as a credential source.
5. Hand the identity basis, result, tool shape, observation time, covered
   window, and stable references to `$gsv-onboard` for the CAS-protected Seld
   receipt.
