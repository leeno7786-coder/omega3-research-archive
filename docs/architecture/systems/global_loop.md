# System: Global loop (reason + goals)

> **Agent anchor** · Package: `metacognition/global_loop/` · Full detail: `metacognition/global_loop/README.md`

## What it is

**Reflection + goal formation** — turns telemetry and upstream truth into **grounded goals** and observations. Does not execute tools.

## Phases (compatibility monolith)

| Phase | Role |
| ----- | ---- |
| 1 intake | Aggregate compatibility trends, WAL goals, and upstream context |
| 2 reflection | Observations from autonomic trends + strength thresholds |
| 3 goal_formulation | Synthesize candidates; trajectory alignment; **LAW grounding** from meta failures |
| 4 emission | Append to `goals.jsonl` only outside symphony inline mode; Symphony uses runner-mediated WAL projection after `GLOBAL_B` |

## What it is expected to do

- Emit **at most one primary goal per cycle** (`select_primary_goal`).
- Mint **LAW-traced** learning goals from `failed_to_initiate` meta records + curiosity pressure evidence.
- Filter ungrounded goals (`is_grounded_learning_goal`) — log rejects, do not silent-drop.
- Honor active **OmegaTrajectory** (boost aligned candidates).

## Product truth after S17

- Boot no longer schedules the monolithic global loop as an epoch step.
- Symphony product routing uses `GLOBAL_A` and `GLOBAL_B` movement runners.
- `GLOBAL_PRELUDE` can run before Meta for raw curiosity; `META_EXECUTIVE` still runs Global after Meta/Auto.
- `goals.jsonl` is WAL/projection only under Symphony, never planning intake.

## Symphony target

- **GLOBAL_A** — orient: world model + strength frame + trajectory.
- **GLOBAL_B** — reflect, formulate, emit using **this episode** pressure.

Downward: enriches meta grounding next META_A.

## Writes / reads

- Writes: `LAST_GLOBAL_OBSERVATIONS`, `LAST_GLOBAL_GOALS`, and WAL `goals.jsonl` projection.
- Reads: episode pressure, episode meta/auto, trajectory, and strength frame.

## Do not break

- `goal_synthesizer._ground_goals_from_upstream`
- `phase_4_emission` grounded gate
- One-goal-per-cycle cap
