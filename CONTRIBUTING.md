# Contributing

Keep changes small, owner-neutral, and backed by synthetic tests. Never commit
real user state, private paths, credentials, provider exports, or copied
conversation history.

Run:

```bash
uv sync --extra dev
make check
make e2e
```

All contributions require a Developer Certificate of Origin sign-off:

```text
Signed-off-by: Your Name <you@example.com>
```

Use `git commit -s` to add it. By signing off, you certify that you have the
right to submit the contribution under this project's license.
