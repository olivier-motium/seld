# Outlook Calendar connection check

Use this note after Outlook Calendar is selected and either its host-owned app
or a Seld-managed `microsoft` connection is available. The person owns sign-in,
tenant/account selection, consent, conditional access, and second factors.

1. Ask for the expected Microsoft account, tenant when relevant, and calendar
   set. With a host-owned app, use a read-only identity result. With Seld-
   managed auth, the person confirms the account shown by Microsoft and the
   reader returns only a stable hashed Graph account binding.
2. For Seld-managed auth, call `gsv_connector_source_read` with the exact
   `microsoft` connection ID and source `outlook_calendar`. Read a bounded
   current event window and keep its covered interval separate from the
   observation time.
3. Do not create, edit, move, RSVP to, or delete events. One Microsoft profile
   also grants Outlook mail's fixed scope, but mail requires its own read and
   receipt. Teams and SharePoint are not implemented by this profile.
4. Missing host tools get one new-task retry after enablement. For Seld-managed
   auth, inspect redacted status and let the person resolve OAuth, wrong-account,
   tenant, administrator, or conditional-access errors. Never use another host
   session as a credential source.
5. Return the identity basis and result to `$gsv-onboard`, which records the
   content-free coverage receipt against the exact current source-state
   revision.
