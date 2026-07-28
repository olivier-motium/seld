# Figma connection check

Use this note after Figma is selected and its separately installed app tools
appear in a fresh ChatGPT task.

1. Ask for the expected Figma account and team or organization. Use a read-only
   profile or account result when available; file authorship does not verify the
   connected user.
2. Read bounded metadata or content from one recent file or project in the
   approved scope. Do not enumerate every team or project.
3. Do not edit files, create comments, change sharing, export in bulk, or alter
   team membership during onboarding.
4. Missing tools get one new-task retry after enablement. Wrong account, team,
   auth, organization policy, or admin errors return to the Figma-owned
   connection flow before another probe.
5. Return the identity status and bounded-read facts to `$gsv-onboard`, which
   writes the source receipt through fresh CAS.
