# System: Brain loop (sensory / mouth)

> **Agent anchor** · Package: `brain/` · Full detail: `brain/README.md`

## What it is

**Outward cognition** — user/system/autonomous initiator → delivered response + memory staging. Pure sequencer via `ExecutionEngine` + `phase_registry` (unlike autonomous Surface-B).

## Phases

`retrieve → translate → respond → verify → store`

- User/system: verify skipped.
- Autonomous: verify runs (deterministic resolution_status).

## What it is expected to do

- Turn `BrainIntake` into surface text + `write_memory(propose=True)` path.
- Write **`LAST_BRAIN_RESPONSE`** (canonical) after cycle.
- On autonomous + curiosity: enqueue **`PENDING_CURIOSITY_RESOLUTIONS`**, may set **`OUTSTANDING_USER_QUESTION`**.
- Update **conversation_state** on user/system turns.
- CONV-COG: consume outstanding question on user reply (`_conv_cog.py`).

## What it must NOT do

- Replace meta/global/omega planning.
- Run as epoch timer loop (event-driven via receiver/cadence).

## Entry points

- `make_brain_runner()` → `Orchestrator.initiate_brain_loop`
- `core/brain_receiver.py` — `handle_user_input`, `handle_system_signal`, `handle_autonomous_initiation`
- `core/response_engine.py` — proactive dispatch
- `core/autonomous_engine.py` — `initiate_cycle_from_curiosity`

## Symphony role

**Movement `SENSORY_INJECT`** — append to `episode.sensory[]`; may chain to META_A or AUTO. Same episode as cognition, not parallel immortal bus.

## Do not break

- `write_canonical(LAST_BRAIN_RESPONSE)` shape.
- Verify → pending curiosity queue for autonomous provenance.
- Guardian classify gate in retrieve (REJECT short-circuit).
