# Canonical Seld / GSV terminology

This reference defines the authoritative system terminology for the Seld deployment, resident intelligence, standing and temporary agent roles, lifecycles, and fault states. Packaged and imported resident guidance across GSV, Mind, Operator, Pulse, and Shipyard defers to this vocabulary.

## Structural and resident architecture

- **Vessel**: The Seld deployment — the local runtime instance, vault, state store, and execution environment that contains and carries the resident Mind and GSV authority on a given host.
- **Mind**: The durable resident intelligence and authoritative point of view carried by the vessel. It owns judgment, meaning, Direction, and retained context.
- **Avatar**: The human-facing embodiment or conversational interface interacting directly with the user.
- **Crew**: Durable standing agents commissioned and retired to maintain persistent operational roles; command-deck seats like Pulse and Chief can be embodiments or shifts of the Mind, while crew refers to independently commissioned durable roles.

## Authority roles

- **Overseer**: A read-only boundary and risk review authority that inspects bounds, safety, integrity, and operational risk; an overseer never sets strategy or authorizes outcomes.
- **Orchestrator**: The coordination authority and sole adjudicator/integration owner for its arc, responsible for scheduling, dispatching work, evaluating dependencies, adjudicating outcomes, and integrating execution units.

## Operational execution units

- **Droid**: A general temporary bounded executor deployed to carry out discrete implementation, investigation, sustained work, or multi-step tasks.
- **Drone**: A narrow recurring service or sensor running on cadence (such as a periodic heartbeat wake or mechanical sense sweep).
- **Probe**: A disposable, read-only scout launched to inspect, check, or sample transient state without durable side effects.

## Unit lifecycle

Temporary execution units follow a clean mechanical lifecycle:
- **Launch**: Initializing and starting the execution unit process or task container.
- **Deploy**: Binding the unit to an explicit assignment, task, or workspace and initiating execution.
- **Recall**: Pausing, requesting status from, or reassigning a running unit.
- **Recycle**: Orderly cleanup, resource reclamation, workspace cleanup, and terminal tear-down of a completed or cancelled unit.

Do not use biological lifecycle terms (such as birth, born, stillborn, death, dead, killed) for agent or task lifecycle states. Conceptual gates use operational language (such as the **task-creation gate**).

## Unit fault states

When an execution unit fails, classify its state using exact mechanical conditions:
- **Launch failed or no first turn**: The process failed to start or did not produce an initial turn or handshake.
- **Stalled**: The unit is active and holds execution resources, but has ceased making observable progress, emitting keepalives, or advancing its workflow without entering a valid waiting or terminal state (elapsed time alone is not the verdict).
- **Crashed**: The process or turn exited abnormally with a nonzero error code or unhandled exception.
- **Unreachable**: Communication or transport to the unit failed while the process status is indeterminate.
- **Process absent**: The expected operating-system or runtime process identity is no longer present on the host.

## Legacy and compatibility boundaries

- Stored fields and APIs (e.g., `active_thread_id`, `gsv_execution_bindings` tool returning `active_hands`) remain stable for compatibility and transport.
- Conceptual guidance refers to active execution units or droids rather than legacy "hands" or "workers".
- Low-level operating system and Python technical constructs (e.g., OS worker processes, OS signals/process termination, HTTP health probes) retain standard technical language without conflicting with Seld agent role semantics.
