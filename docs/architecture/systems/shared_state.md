# System: SharedState (inter-loop wire)

> **Agent anchor** — read before adding keys or cross-loop reads.

## What it is (today)

`core/shared_state.py` — thread-safe in-memory `dict` of keys → values.

- **`write(key, value)`** — permissive (any key).
- **`write_canonical(key, value)`** — must be in `KEY_REGISTRY` (`core/shared_state_keys.py`); shape validated.

## What it is expected to do

- Be the **only** sanctioned cross-loop channel (doctrine).
- Carry **canonical `LAST_*` slots** defined in `shared_state_keys.py` (15 keys).
- Carry scratch keys: `_epoch`, `orchestrator.autonomy_pause`.

## What it must NOT do

- Replace episode truth with ad-hoc keys for **planning** (symphony).
- Be treated as durable truth without `guardian/state_persistence.save_now` (production boot path often **does not save** after loops).

## Production truth (honest)

| Key lane | Cleared by deepsleep adapter? | Meta reads at epoch start? |
|----------|------------------------------|---------------------------|
| Cascade `LAST_*` (auto, meta, global, curiosity) | Yes (most) | Often **empty** |
| `last_omega_trajectory` | No | Global reads |
| Brain / conv keys | No | Parallel timeline |

## Symphony target

Shrink to:

- `Guidance` (trajectory + policy refs)
- `CognitiveEpisode` (current)
- Gates: pause, presence

`LAST_*` become **projections** or retire for planning.

## Key files

- `core/shared_state.py`
- `core/shared_state_keys.py`
- `policy/shared_state.policy.yaml` (persist policy)
- `guardian/state_persistence.py`

## Do not break

- `write_canonical` validation for canonical keys.
- Trajectory **not** cleared on deepsleep clear list (`loop_adapters.py`).
