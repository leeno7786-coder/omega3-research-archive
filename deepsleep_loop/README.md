# DeepSleep

DeepSleep is the single terminus of every cognition episode. It owns the
active episode's final transaction:

```
SRAM intake → consolidation → canonical filesystem writes
            → graph rebuild from filesystem → maintenance → close
```

The public entry point is
`run_deepsleep_for_episode(store, *, cascade_status="completed")`.
It requires an active `EpisodeStore` carrier. The store prepares the closed
episode body, then calls DeepSleep through its `before_finalize` transaction
seam. The carrier is archived and cleared only after the complete DeepSleep
pass succeeds.

## Episode sequence

`deepsleep_loop.loop._run_deepsleep_episode_core` executes one required pass:

1. build episode intake
2. update mastery, failure, semantic, identity, trajectory, and query state
3. consolidate the episode journal and memory envelopes
4. build the projection state
5. capture the canonical episode
6. ground any accepted practice report held in episode SRAM
7. drain episode SRAM through canonical ingestion
8. reconcile contradictions
9. rebuild the graph and DME projection from filesystem truth
10. rebuild pathway projections and run memory-integrity maintenance
11. return the outcome pack and allow `EpisodeStore` to finalize the close

Required phase failures propagate. They do not produce a degraded-success
pack, and the episode carrier remains available for a retry.

## Ownership boundaries

- Pressure is the only episode trigger.
- Omega runs before DeepSleep on every episode and chooses successor pressure.
- DeepSleep does not ignite cognition or invent successor pressure.
- Incoming memory is held by `memory.episode_sram`; only its DeepSleep drain
  may call the canonical `write_memory` path.
- The filesystem is truth. Recall projections are rebuilt from it after all
  episode writes and contradiction maintenance complete.
- Telemetry is observational and may not change the transaction outcome.

**Historical/retired note:** Earlier window-level closure and delayed-reindex
designs are not the authority for the current terminus. Use the live code and
[AGENTS.md](../AGENTS.md) for the governing contract.

## Verification

```powershell
.\.venv-brain\Scripts\python.exe -m pytest tests\core\test_deepsleep_per_episode_split.py tests\core\test_deepsleep_acceptance.py -q
.\.venv-brain\Scripts\python.exe -m ruff check deepsleep_loop
```
