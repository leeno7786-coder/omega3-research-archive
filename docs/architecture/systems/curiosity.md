# System: Curiosity (pressure + gaps)

> **Agent anchor** · Modules under `core/curiosity_*`

## What it is

**Deterministic gap detection + persistence + routing** — not random exploration. Unresolved incompleteness creates pressure → action paths (`ask_user`, `tool_creation`, `defer`, etc.).

## Components

| Module | Role |
| -------- | ---- |
| `curiosity_engine.py` | Detect gaps, score, route, `should_pursue` |
| `curiosity_store.py` | Persist `CuriosityEngineEntry` under memory open_questions |
| `curiosity_producer.py` | Cycle-context producer helpers |
| `curiosity_feedback_consumer.py` | `consume_pending` → mark_resolved/escalated/failed |
| `pending_curiosity_queue.py` | SharedState queue shape for brain verify |
| `make_curiosity_runner` | Compatibility adapter for legacy `LAST_CURIOSITY_CYCLE` pressure snapshots |

## What it is expected to do

- Surface **highest-pressure** open gap to pressure/ignition.
- Persist entries when pursuing.
- Close loop: brain verify / episode archive → rearm or **consume_pending** compatibility drain → store updates.
- Provide **aggregate pressure** for global grounding (symphony: same episode).

## Production truth after S17

- CueDesk opens episodes from pressure; curiosity contributes through `pressure_field.read_curiosity_gap`, idle probes, and episode rearm.
- `autonomous_engine.sweep_and_initiate` is closure-only in symphony mode; it drains pending feedback but does not open new brain/meta/global work.
- `make_curiosity_runner` is not product boot authority. It remains compatibility/test surface only.

## Symphony target

**Pressure field prime mover** — opens `CognitiveEpisode`, not a scheduled loop. Pressure is captured in `episode.pressure` when measured.

## Do not break

- ResolutionPath enum contract (`docs/contracts/proactive_cognition.md`).
- Queue record shape (`curiosity_id`, `brain_cycle_id`, `status`).
- Outstanding question blocks duplicate ask_user sweeps.
