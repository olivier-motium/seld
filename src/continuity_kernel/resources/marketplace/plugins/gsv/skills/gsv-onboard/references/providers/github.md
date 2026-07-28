# GitHub connection check

Use this note after GitHub is selected. Choose the one route exposed in the
fresh ChatGPT task: the GitHub app for ChatGPT-login sessions, or the official
hosted GitHub MCP for an API-key session. Do not require both.

1. Ask for the exact GitHub login, organization context, and repository scope.
   Use the chosen route's read-only current-user or profile call. Do not treat a
   local `gh` login as proof of either app route.
2. Read bounded recent activity from one named repository or approved scope.
   Do not enumerate private repositories to establish access.
3. Do not create or alter issues, pull requests, reviews, branches, releases,
   repositories, or permissions during onboarding. Never request a PAT in chat;
   API-key users configure it outside the conversation.
4. Missing tools get one new-task retry after enablement. Wrong login, scope,
   SSO, app approval, or organization policy returns to the chosen provider
   flow before another probe.
5. Return the selected route, identity result, scope, and bounded-read facts to
   `$gsv-onboard` for a hashed, CAS-protected Seld receipt.
