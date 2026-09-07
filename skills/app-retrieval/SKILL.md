---
name: app-retrieval
description: Find and read selected mail, calendars, documents, Slack, Notion, and WhatsApp content through Seld's local app corpus. Use for questions grounded in connected app content, including across coding workspaces.
---

# App retrieval

Use the installed `gsv apps` commands from any working directory. The active
Seld vault selects the corpus; the current repository does not. Pass the global
`--vault PATH` option when an explicit vault is required. Do not substitute the
default QMD index or assume that Seld's authored records contain app bodies.

Start with `gsv apps capabilities` to identify the exact account and supported
live operations. `gsv apps status` reports selected sources, coverage, and index
readiness. A listed account or a successful authorization is not proof that its
content was indexed.

```bash
gsv apps search "question or exact phrase" --connection-id CONNECTION --limit 8
gsv apps read --connection-id CONNECTION --object-id OBJECT
```

Select the account or workspace before searching when the question identifies
one. Use the returned object ID for exact readback. Read relevant sources before
making a substantive claim; a ranked snippet alone can omit a correction or
qualification. For a cross-source question, search the relevant connections and
compare the dated evidence.

For Slack, read the parent message and relevant replies in the same workspace
and channel or DM before resolving a discussion. For Notion, follow relevant
linked pages, database rows, and newer project versions. A successful demo or
an older blueprint does not close a later unresolved acceptance issue.

Distinguish source dates from fetch dates. Prefer a later explicit correction
over the earlier statement it corrects. State material scope, coverage, freshness,
or extraction gaps. Incomplete indexing, inaccessible media, text-only fallback,
and no matches do not establish that an event or message does not exist. For a
current-state question, use an available live read operation when the stored
source is stale or incomplete.

The corpus supports retrieval. For provider actions, use only operations listed
by `gsv apps capabilities` and inspect `gsv apps call --help`. Existing approval
requirements apply to writes. Retrieval does not authorize sending, editing,
deleting, or changing account access.

`gsv apps sync-all` advances configured sources and updates local indexes. Avoid
starting a second full refresh while the configured background job is running.
Do not alter selected accounts merely to answer a question.
