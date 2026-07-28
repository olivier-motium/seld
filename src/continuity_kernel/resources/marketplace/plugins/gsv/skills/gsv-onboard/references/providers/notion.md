# Notion connection check

Use this note after Notion is selected and its separately installed app tools
are exposed in a fresh ChatGPT task.

1. Ask for the expected account and workspace. Use a read-only profile or
   workspace identity call when available. A page owner or title does not prove
   the connected workspace.
2. Search or read a bounded recent page set in the approved scope. Keep the
   search narrow enough that onboarding does not become workspace ingestion.
3. Do not create, edit, move, comment on, share, archive, or delete pages or
   databases during onboarding.
4. Missing tools get one new-task retry after enablement. Wrong workspace,
   auth, integration scope, or admin errors return to the Notion-owned
   connection flow before another probe.
5. Return the identity and bounded-read facts to `$gsv-onboard` for the
   content-free, CAS-protected source receipt.
