# System: Meta loop (governance + reflection)

> **Agent anchor** · Package: `metacognition/meta_loop/` · Full detail: `metacognition/meta_loop/README.md`

## What it is

**Governance layer** — chooses what autonomous does next. **Chooses; never does** (no inference, no tools, no memory writes).

## Phases (compatibility monolith)

| Phase | Role |
| ----- | ---- |
| 1 intake | Aggregate compatibility inputs; symphony `META_A` uses a caller-supplied episode/SharedState snapshot instead |
| 2 synthesis | Rule-table intent/domain/priority |
| 3 assembly | ≤1–2 bound tool calls |
| 4 dispatch | Seal order to drop/file **only if** goal + tool_calls |

## What it is expected to do

- Emit **orders** for autonomous when capability exists.
- Stay **silent** when idle / no tools (autonomous must not fabricate work).
- Publish disposition via adapter → `LAST_META_CYCLE` (dispatch truth today).

## Product truth after S17

- Boot no longer schedules full Meta as an epoch loop.
- Symphony product routing uses split `META_A` / `META_B` movement runners from `core.symphony_runner`.
- In symphony mode, Meta planning reads episode/SharedState snapshot data and does **not** read `goals.jsonl` or `autonomous_outcomes.jsonl` as planning intake.
- The monolithic adapter remains compatibility-only for direct tests and historical audits.

## Symphony target

Split:

- **META_A** — phases 2,3,4 (govern) → `AUTO_INITIATE`
- **META_B** — intake outcome + reflect → `META_REFLECTED`

Reads **episode** + **guidance.trajectory**, not raw jsonl for planning.

## Writes / reads

- Writes: `LAST_META_ORDERS`, `LAST_META_CYCLE`.
- Reads: episode snapshot, guidance trajectory, `episode.auto`, and compatibility `LAST_AUTO_CYCLE`.

## Do not break

- Dispatch gate: no order without goal + tool_calls.
- `package_builder` rationale.`source_goal_id` (goal mortality).
- `build_meta_cycle_payload` same-cycle invariant.
