# Gmail connection check

Use this note after the person selects Gmail and its app tools appear in a fresh
ChatGPT task. The Gmail app owns sign-in, consent, account selection, and
revocation; never ask for or handle those credentials in chat.

1. Ask for the exact expected mailbox address. Call the app's read-only profile
   tool when available and compare the returned address exactly. Without a
   profile result, label identity observed rather than verified.
2. Read one small recent mail window through a read-only tool. An explicitly
   empty result is valid. Do not draft, send, label, archive, delete, forward,
   or alter read state during onboarding.
3. Treat Gmail, Calendar, and Drive as separate grants even when the account is
   the same. Do not infer one connection from another.
4. If tools are missing, confirm the app is enabled and start one new task. For
   an auth or wrong-account result, stop and let the person reconnect it in
   ChatGPT settings before one changed retry.
5. Return the verified or observed identity, bounded-read result, tool shape,
   coverage horizon, and stable references to `$gsv-onboard`. That skill alone
   records the result through fresh source-state CAS.
