# Outlook Calendar connection check

Use this note after Outlook Calendar is selected and its separately installed
app is exposed in a fresh ChatGPT task.

1. Ask for the expected Microsoft account, tenant when relevant, and calendar
   set. Use a read-only profile or calendar identity result for exact matching;
   an email domain does not verify a tenant.
2. Read a bounded current event window and keep its covered interval separate
   from the observation time. A future interval does not extend freshness.
3. Do not create, edit, move, RSVP to, or delete events during onboarding, and
   do not treat Outlook Email or another Microsoft app as proof of this grant.
4. Missing tools get one new-task retry after enablement. Wrong-account, auth,
   tenant, admin, or conditional-access errors return to the provider-owned
   connection flow.
5. Return the result to `$gsv-onboard`, which records the content-free coverage
   receipt against the exact current source-state revision.
