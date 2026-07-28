# Why Seld is state of the art

- Assessment: `resident-personal-operations/v1`
- Evaluated: 2026-07-28
- Seld: `0.3.0` at `e1269abedd8f48a05e1bb6df0adac6639c5d7f2d`
- OpenClaw source snapshot: [`da5820b39c09b857bb2411fda247ebea9456211c`](https://github.com/openclaw/openclaw/tree/da5820b39c09b857bb2411fda247ebea9456211c)
- Hermes Agent source snapshot: [`f228e145ba35cbbf785eded2021ae6682285b91b`](https://github.com/NousResearch/hermes-agent/tree/f228e145ba35cbbf785eded2021ae6682285b91b)

Seld sets the standard for resident personal operations: keeping a person's
work and life current across the sources they choose, understanding changes in
context, and preparing the few decisions that move things forward.

## The standard for the category

A state-of-the-art resident personal system needs six primary product
capabilities working as one operating loop:

1. a whole-person model of direction, outcomes, people, projects, decisions,
   and ongoing situations;
2. source-aware truth that distinguishes current, partial, stale, failed, and
   unread evidence;
3. selective interruption that turns a large stream of activity into a small
   set of useful decisions;
4. semantic continuity across conversations, processes, restarts, and failed
   execution hands;
5. approval and replay safety for consequential control intents; and
6. AI ownership of meaning, with deterministic code limited to integrity,
   privacy, bounds, and recovery.

A system reaches the state-of-the-art frontier when all six are built into one
product model and no compared system provides a more complete implementation of
that same loop. Seld 0.3.0 meets that standard.

## Current comparison

| Dimension | Seld | OpenClaw | Hermes Agent |
| --- | --- | --- | --- |
| Whole-person operating model | **Built in.** Direction, a complete Portfolio, outcomes, canonical people and projects, decisions, and longer-running situations form one resident model. | **Strong adjacent capability.** Goals, Workboard, user memory, and Memory Wiki cover durable objectives, entities, evidence, and operating work as general-agent components. | **Strong adjacent capability.** Persistent goals with completion contracts, Kanban, memory, context files, and session search cover durable objectives and context as general-agent components. |
| Source-aware truth | **Built in.** After a bounded read, Seld records hashed host and tool lineage, coverage, completeness, recipe version, and freshness. Identity-bearing sources also carry a hashed account binding; unread sources remain visibly unread. | **Strong adjacent capability.** Memory Wiki provides structured claims, evidence, provenance, contradiction reports, and freshness. The reviewed product model does not join per-source coverage to one complete personal Portfolio and decision loop. | **Strong adjacent capability.** Curated memory and external memory providers preserve context. The reviewed product model does not expose the same selected-source coverage contract inside one complete personal Portfolio and decision loop. |
| Decision compression | **Built in.** Pulse reasons over new evidence and durable context, while the Rundown presents only decisions that now benefit from the user's attention. | **Strong adjacent capability.** Heartbeats and standing orders support proactive work; the Control UI and Workboard expose operating state. | **Strong adjacent capability.** Cron, persistent goals, and Kanban support proactive and delegated work. |
| Semantic continuity | **Built in.** The same durable outcome, active hand, next actor, evidence, and user disposition survive task and process boundaries. | **Built in for agent work.** Goals survive restarts, Workboard links runs and sessions, and notification cursors are replay-safe. | **Built in for agent work.** Goals auto-continue against explicit completion contracts, and Kanban retains execution state across work sessions. |
| Approval and replay safety | **Built in.** Browser intent enters an authenticated compare-and-swap queue; CLI or MCP explicitly accepts or rejects it; the result remains visible after restart; consequential outward action still requires the user. | **Built in for execution.** Exec approvals and scoped runtime controls protect consequential commands and tool use. | **Built in for execution.** Command approvals, security policy, and configurable memory or skill write approval protect consequential changes. |
| AI owns meaning | **Built in.** The model decides identity, priority, task meaning, relationships, and what deserves attention. Deterministic code protects storage, identity, bounds, privacy, recovery, and replay. | **Different composition.** Agents reason over goals, memory, automation, and operating work, while the platform owns more lifecycle and orchestration semantics. | **Different composition.** Agents reason over goals, memory, skills, and delegated work, while goals, judges, Kanban, and automation share more lifecycle semantics. |
| Inspectable local state | **Built in.** Canonical Markdown remains readable and correctable without Seld, with compare-and-swap revisions, audit, backup, restore, and repair. | **Built in.** Local memory, QMD, Memory Wiki, gateway state, and plugin stores provide a strong inspectable local substrate. | **Built in.** Local memory, context files, session state, and Kanban provide a strong inspectable local substrate. |
| Ambient cognition | **Built in.** A bounded Pulse combines fresh source evidence with the complete resident model and records honest coverage gaps. | **Built in.** Heartbeats, scheduled tasks, background tasks, and standing orders provide broad ambient execution. | **Built in.** Cron, persistent goals, background review, and delegated work provide broad ambient execution. |
| Platform reach | **Focused.** Seld uses the ChatGPT desktop app on macOS as the conversation, reasoning, and execution surface. | **Platform strength.** OpenClaw provides broad channel, platform, plugin, and gateway coverage. | **Platform strength.** Hermes provides broad messaging, gateway, integration, and automation coverage. |

## Verdict

Seld is state of the art because it is the only system in this comparison that
makes the complete person-level loop the primary product: Direction, Portfolio,
source-aware truth, selective attention, semantic continuity, and approval-safe
handoff all operate on the same living model. OpenClaw and Hermes are broad
agent platforms, while Seld is the resident AI chief of staff that stays caught
up with your life and prepares what should happen next.

## Seld evidence

- [Product contract](product-contract.md)
- [Architecture and ownership boundaries](architecture.md)
- [Canonical data and source coverage](data-format.md)
- [Pulse contract](pulse.md)
- [Trust model](trust-model.md)
- [Installed and release evidence](release-gates.md)

## Comparison sources

The source snapshots above make the assessment reproducible. These official
product pages provide the readable capability descriptions:

OpenClaw: [Goal](https://docs.openclaw.ai/tools/goal),
[Workboard](https://docs.openclaw.ai/plugins/workboard),
[memory](https://docs.openclaw.ai/concepts/memory),
[QMD](https://docs.openclaw.ai/concepts/memory-qmd),
[Memory Wiki](https://docs.openclaw.ai/plugins/memory-wiki),
[heartbeat](https://docs.openclaw.ai/gateway/heartbeat),
[background tasks](https://docs.openclaw.ai/automation/tasks),
[standing orders](https://docs.openclaw.ai/automation/standing-orders),
[exec approvals](https://docs.openclaw.ai/tools/exec-approvals), and
[channels](https://docs.openclaw.ai/channels).

Hermes Agent: [persistent memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/),
[persistent goals](https://hermes-agent.nousresearch.com/docs/user-guide/features/goals),
[Kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban),
[cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron),
[delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation),
[messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), and
[security](https://hermes-agent.nousresearch.com/docs/user-guide/security/).
