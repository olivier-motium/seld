# Microsoft Teams connection check

Use this note after Teams is selected and its separately installed app tools
appear in a fresh ChatGPT task.

1. Ask for the exact Microsoft account and expected tenant. Use a read-only
   profile result for the user; verify the tenant only when the result contains
   an immutable tenant identifier that matches.
2. Read one small recent chat or channel window. Do not enumerate teams or
   conversations simply to establish access.
3. Do not send messages, react, create meetings, change teams or membership, or
   mutate Planner. Teams, Outlook, and SharePoint are separate grants.
4. Missing tools get one new-task retry after enablement. Wrong user, auth,
   tenant, conditional-access, or admin failures return to the Microsoft-owned
   connection flow before another probe.
5. Return the verified and unverified fields plus the bounded-read result to
   `$gsv-onboard` for source-state CAS and readback.
