# System: Deep sleep (consolidation / archive)

> **Agent anchor** · Package: `metacognition/deep_sleep/` · Full detail: `metacognition/deep_sleep/README.md`

## What it is

**Offline consolidation** — replay, contradiction handling, forgetting candidates, journal persistence, trait/policy proposals. **Not** tool creation (Omega-only).

## Phases

1. intake  
2. reactivation (replay, schema — read-mostly)  
3. reconciliation (contradictions, forgetting — guardian-gated writes)  
4. persistence (consolidation, prune, journal)

Also: `core/deepsleep_engine.py` — `run_consolidation_pass`, `_snapshot_upstream_telemetry`, queue drains.

## What it is expected to do

- Archive upstream cascade into **journal** (`deepsleep_store`).
- Drain reflection / deepsleep candidate queues at pass start.
- Run **auto_drain** for trusted internal guardian queue (adapter telemetry).
- **Persist** meta_cycle + curiosity_cycle before any bus clear (FINAL VERDICT law).

## Production truth (miswire)

- Runs **mid-epoch** (after global + curiosity, **before omega** in boot).
- Adapter **clears** `LAST_AUTO_CYCLE`, meta, global, curiosity slots after journal snapshot.
- Omega then reads consolidation envelope, not live global meta of same pass.

## Symphony target

**Movement `DEEPSLEEP`**: trigger **`EPISODE_COMPLETE` only** (after omega). Archive `CognitiveEpisode`; **never clear trajectory**.

## Writes / reads

| Writes | Reads |
|--------|-------|
| `LAST_DEEPSLEEP_CONSOLIDATION`, journal files | All `LAST_*` via `_UPSTREAM_SLOTS` |

## Do not break

- `_UPSTREAM_SLOTS` includes `meta_cycle`, `curiosity`, `omega_trajectory`.
- Persist-before-clear ordering (tests in `test_goal_mortality`, journal cascade tests).
