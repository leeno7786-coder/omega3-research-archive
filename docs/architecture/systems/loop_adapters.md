# System: Loop adapters

> **Agent anchor** — bridge between Orchestrator and metacognition/autonomous loops.

## What it is (today)

`core/loop_adapters.py` — `make_*_runner()` factories returning `runner(shared_state) -> dict`.

Each adapter:

1. Reads `_epoch` + upstream `LAST_*` keys.
2. Builds loop `Context`.
3. Runs `run_audited_loop(loop_id, run_*_loop, ctx)`.
4. Writes canonical envelope via `_safe_write_canonical` (logs on failure, does not abort loop work).

## What each adapter does (production)

| Factory | Writes | Notable reads |
|---------|--------|----------------|
| `make_auto_runner` | `LAST_AUTO_CYCLE` | (orders from files/queue inside loop) |
| `make_meta_runner` | `LAST_META_ORDERS`, `LAST_META_CYCLE` | `LAST_AUTO_CYCLE` at **start** (usually prior epoch / None) |
| `make_global_runner` | `LAST_GLOBAL_OBSERVATIONS`, `LAST_GLOBAL_GOALS` | auto, meta, **prior** `LAST_CURIOSITY_CYCLE`, trajectory |
| `make_curiosity_runner` | `LAST_CURIOSITY_CYCLE` | prior curiosity |
| `make_deepsleep_runner` | `LAST_DEEPSLEEP_CONSOLIDATION`; **clears** cascade keys | auto, meta, global, trajectory |
| `make_omega_runner` | `LAST_OMEGA_*`, identity lobe | `LAST_DEEPSLEEP_CONSOLIDATION` |

Also: `is_grounded_learning_goal`, `build_meta_cycle_payload`, `build_curiosity_cycle_payload`.

## Symphony target

Split into **movement runners** per `symphony_cue_sheet.yaml` (META_A, META_B, GLOBAL_A, GLOBAL_B, etc.). No full-loop runner on a timer.

## Do not break

- `is_grounded_learning_goal` LAW (FINAL VERDICT).
- Deepsleep clear list must persist meta/curiosity to journal **before** clear (engine snapshot).
- `_extract_dispatched_goal_id` / `rationale.source_goal_id` path (goal mill).

## Key file

- `core/loop_adapters.py`
