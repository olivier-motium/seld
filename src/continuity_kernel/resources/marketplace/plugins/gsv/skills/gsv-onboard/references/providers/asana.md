# Asana connection check

Use this note after Asana is selected and its separately installed app tools
are exposed in a fresh ChatGPT task.

1. Ask for the expected account and workspace. Use a read-only profile or
   workspace identity call when available, and require an exact match before
   marking either verified.
2. Read a bounded current task or project window chosen with the person. An
   explicitly empty successful result is valid.
3. Do not create, assign, comment on, move, complete, or delete work during
   onboarding.
4. Missing tools get one new-task retry after enablement. Wrong account,
   workspace, auth, guest-access, or admin errors return to the provider-owned
   connection flow before one changed retry.
5. Return the identity, tool shape, read result, horizon, and stable references
   to `$gsv-onboard` for the exact source-state CAS and readback.
