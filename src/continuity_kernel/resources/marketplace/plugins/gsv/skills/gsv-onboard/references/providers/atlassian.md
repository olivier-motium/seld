# Atlassian connection check

Use this note after Atlassian is selected and the Atlassian Rovo app tools are
available in a fresh ChatGPT task.

1. Ask for the expected Atlassian account, site, and Jira or Confluence scope.
   Use read-only profile and site metadata when available; do not infer the site
   from issue or page content.
2. Read a bounded current Jira issue set or Confluence page set from the chosen
   site. Do not search every accessible site to prove access.
3. Do not create, edit, transition, comment on, move, or delete provider data
   during onboarding.
4. Missing tools get one new-task retry after enablement. Wrong site, auth,
   organization policy, or admin approval returns to the Atlassian-owned
   connection flow before another probe.
5. Hand the identity and bounded-read facts to `$gsv-onboard`, which performs
   the source-state CAS and stores no provider body.
