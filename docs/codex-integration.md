# Codex integration evidence

## What GSV uses

The Bridge creates new-task links with this shape:

```text
codex://new?prompt=<encoded>&originUrl=<encoded>&path=<encoded>
```

Every link includes an encoded prompt, the GSV repository as `originUrl`, and
the resolved local vault as `path`. The Bridge distinguishes three actions:

- `new_mind_url` exists only when the Tasks section is completely inspected and
  contains zero records. Its prompt starts the first Mind-shaping hand.
- `new_hand_url` starts an established, generic GSV hand. Its prompt contains no
  task identifier, resume language, or first-run language.
- a task `codex_url` resumes one exact commitment and therefore exists only for
  nonterminal tasks. Done and dropped records never receive one.
- the guided Portfolio review start link opens a new Codex hand with the finite
  review contract. Once the agent authors that hand ID on the review-session
  Task, Bridge renders the exact hand link. This public integration does not
  claim a supported API for injecting later turns into that existing hand.

The top bar, all-clear state, and terminal-task inspector use `new_hand_url`;
only the true first-run panel uses `new_mind_url`. The Bridge emits none of
these links until Codex discovery succeeds and both the managed instructions
and GSV plugin are installed. The README install link omits `path` because it
opens a new task before a checkout or vault path can be assumed.

GSV does not use a legacy launch-route fallback.

## Version-bounded verification

Observed read-only on 2026-07-22:

- Installed app: `/Applications/ChatGPT.app`
- App version/build: `26.715.72359` / `5718`
- Bundle identifier: `com.openai.codex`
- Signing team: `2DC432GLL2` (OpenAI)
- Registered URL scheme: `codex`
- Extracted `app.asar` SHA-256:
  `6c6528eb1e8450cdc506a59586f8caffe87576e200977e2a11bdea0cecf1c718`

The parser in that exact installed bundle routes host `new` to its new-task
parser and reads `prompt`, `originUrl`, and `path`. It rejects the route only
when all three are absent. This validates GSV's link shape for that installed
version without launching or submitting a task.

This is not an evergreen Codex API guarantee. Recheck the installed bundle or
an official supported contract before claiming compatibility with a later app
version. The copyable installation instruction in the README remains usable
without the custom scheme.

`tests/test_bridge.py` verifies URL encoding, action qualification, and
round-tripping for each prompt, origin URL, and local path.
