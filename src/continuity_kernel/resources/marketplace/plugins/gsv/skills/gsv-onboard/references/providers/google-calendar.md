# Google Calendar connection check

Use this note only after Google Calendar is selected and its separately
installed app is available in a fresh ChatGPT task. Google owns OAuth and
account selection; never request credentials, codes, or tokens.

1. Ask which Google account and calendar set the person expects. Use a
   read-only profile or calendar identity call when available and require an
   exact account match before calling it verified.
2. Read a bounded current calendar window, including a successful empty
   window. Record the time span separately from the time of the read because a
   future window does not stay fresh until its end.
3. Do not create, edit, move, RSVP to, or delete events during onboarding.
4. Missing tools require one new-task retry after the app is enabled. Auth,
   wrong-account, Workspace policy, or admin errors return to ChatGPT settings
   or the administrator rather than another connector route.
5. Hand the identity, result, tool shape, observation time, covered window, and
   stable references back to `$gsv-onboard` for the CAS-protected Seld receipt.
