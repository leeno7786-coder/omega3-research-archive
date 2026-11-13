# System: Orchestrator

> **Agent anchor** — read before changing `core/orchestrator.py` or boot wiring.

## What it is (today)

`core/orchestrator.py` holds **one `SharedState` per process** and calls registered runners when an explicit caller asks. It is a cycle controller, **not** a phase controller, and it does not decide Symphony product timing.

## What it is expected to do

- Register runners. In S17 product boot this is BrainLoop only; legacy loop runners remain compatibility/direct-API surfaces.
- **`initiate_brain_loop(intake)`** — run brain runner; **errors propagate** (user path).
- **`run_autonomy_cycle(sequence=...)`** — run named loops in order; **per-loop errors captured**, cycle continues (Failure Flow Law).
- Publish process default via `set_default_orchestrator()` (boot step 11).
- **Autonomy pause** — agent engagement pauses background loops only; brain still runs.

## What it must NOT do (today or symphony)

- Control phases inside a loop.
- Enforce canonical key contracts (that is `SharedState.write_canonical`).
- Persist state to disk (`save_now` is **not** called from `run_autonomy_cycle` — only `_invoke_loop` tests).

## Symphony product relationship

CueDesk is the product conductor. Orchestrator remains the shared-state and runner substrate that CueDesk/BrainLoop can use. It does not become the signal dispatcher itself.

## Production wiring

- `boot.py` registers BrainLoop and publishes the default orchestrator.
- `run_continuous` calls `CueDesk.tick(shared_state)` each product tick.
- `run_autonomy_cycle(sequence=...)` and `run_autonomy_forever()` remain direct compatibility APIs, not product boot scheduling.

## Key files

- `core/orchestrator.py`
- `boot.py` (registration + epoch)

## Do not break

- `get_default_orchestrator()` contract used across codebase.
- Brain error propagation vs autonomy error capture distinction.
