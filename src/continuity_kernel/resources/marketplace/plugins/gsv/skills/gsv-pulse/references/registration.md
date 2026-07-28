# Register one resident Pulse

Registration is an interactive setup action, never wake behavior. Perform it
only when the person explicitly asks to enable or repair the resident Pulse.

1. Confirm the exact installed Seld plugin and MCP tools are available in a fresh
   ChatGPT task. Do not register against source code or a different vault.
2. Inspect existing ChatGPT heartbeat automations and recent tasks. Reuse or repair
   one exact matching Pulse; never create a second because an old task is quiet.
3. In one dedicated ChatGPT task, read the real task UUID. Create canonical Task
   `task:resident-pulse` if absent by calling `gsv_task_create` with
   `id="resident-pulse"` and:

   - title `Resident Pulse`;
   - a plain outcome describing one bounded AI check-in that keeps the local Mind
     current;
   - nonterminal status, `next_actor=agent`, and a concrete next wake action;
   - `active_thread_id` equal to this exact ChatGPT task UUID; and
   - the single structural ref `system-role:resident-pulse`.

   If the Task already exists, use fresh CAS and require explicit takeover before
   changing its bound hand. Never add it to Portfolio or a life WorkThread.
4. Run `$gsv-pulse` manually once. Prove the identity guard, bounded context read,
   one reversible local CAS/readback, NOW write, and unattended authority limits.
5. Before asking for approval, explain the tool boundary. Seld's MCP server has
   no provider-action or Computer Use method. The ordinary ChatGPT task may
   still receive tools from other installed plugins, and the current heartbeat
   surface has no Seld-controlled per-task denylist. Read-only connector use and
   the ban on provider writes and Computer Use are therefore wake policy for
   those separately owned tools. If the person requires host enforcement, leave
   Pulse unregistered until the task exposes only read-only source tools or the
   host supports a task-scoped profile.
6. Present the exact automation name, ten-minute cadence, target task, prompt,
   and this boundary, then obtain fresh approval before creating or activating
   it.
7. Use the app-native automation surface with kind `heartbeat`, target this exact
   ChatGPT task, and this bounded prompt:

   `Wake as Seld's resident Mind. Use $gsv-pulse in wake mode. Verify the exact
   resident-pulse binding, integrate one bounded frozen batch from current Seld
   canon and selected read-only connector evidence, write only justified native
   CAS changes and NOW, and finish. Never create another schedule, take external
   action, use browser or Computer Use, change accounts or permissions, or push.`

8. Read the automation back. Then observe one natural wake and verify the same
   Task UUID, the expected NOW/document revision, no duplicate automation, no
   unauthorized external mutation, and honest stale/unknown source coverage.

Start paused when the app surface permits it. Observe one natural installed
wake before presenting the resident loop as active.

Moving or restoring a vault never proves the machine's task, connector, or
automation binding. Re-register on the new host; preserve the Mind but do not
copy the old machine's automation ID or ChatGPT task UUID.
