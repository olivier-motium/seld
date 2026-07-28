# SharePoint connection check

Use this note after SharePoint is selected and its separately installed app
tools appear in a fresh ChatGPT task.

1. Ask for the expected Microsoft account, tenant, and site. Use read-only
   profile and site metadata when available; verify a tenant only from an
   immutable tenant-bearing result.
2. Search or read a bounded recent file or site window in the approved scope.
   Do not enumerate the tenant merely to establish access.
3. Do not create, edit, move, share, delete, or change permissions. SharePoint,
   Outlook, and Teams remain separate grants.
4. Missing tools get one new-task retry after enablement. Wrong tenant, site,
   account, auth, conditional-access, or admin errors return to the
   Microsoft-owned connection flow before another probe.
5. Return the identity and bounded-read result to `$gsv-onboard` for source-state
   CAS, content-free storage, and readback.
