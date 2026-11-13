# Episode Lifecycle Map — Code-Grounded (v2-integration branch)

> **Historical.** This map predates the 2026-09-15 cascade realignment and the
> 2026-09-25 four-lane routing design. It is not authority. Lane ownership is
> defined in `AGENTS.md`; the cascade overview is
> [`COGNITIVE_CASCADE_CURRENT.md`](COGNITIVE_CASCADE_CURRENT.md)
> and `cognition_chain.py`. Claims below that contradict those sources are stale.
>
> Live entry points (as of 2026-09-19): `ignite_episode` at `cognition_chain.py:1025`;
> `_start_global_a` `:2840`; `_start_meta_a` `:2853`; `_start_auto` `:3101`;
> `_start_meta_b_global_b` `:3597`. Omega and DeepSleep are invoked via
> `_run_omega_episode_cognition` and `deepsleep_loop.loop.run_deepsleep_for_episode`
> / `run_deepsleep_for_window` (`:602` / `:703`). There is no `_start_omega` or
> `_start_deepsleep`.

**Purpose:** Close the ownership-boundary gap for the Windowed Omega Reflection build (`WINDOWED_OMEGA_REFLECTION_PACKETS.md`). Every claim below is anchored to a real file:line in the current rebuild path. No invented components.

**Scope:** Live rebuild path only (Slice 1–6 front). Legacy/deleted modules are noted as "dormant" where they explain a read-fallback.

---

## 1. CASCADE ORDER + ENTRY

### Tick entry point

`boot.py:614` `run_continuous()` calls `run_pressure_tick_loop(runtime)` at `boot.py:940`.

`run_pressure_tick_loop` (`pressure_loop/loop.py:356`) is the only forever loop. Each pass runs `_run_pressure_tick_body(runtime)` (`pressure_loop/loop.py:192`): gather contributors, evaluate, admit through the one durable FIFO, dispatch onto a free physical pipeline.

`ignite_episode` lives in `cognition_chain.py:1025` (`_ignite_episode_cascade` at `:1187`) and is **synchronous, blocking** on that pipeline — other free pipelines may still admit later FIFO heads.

### Cascade sequence (`cognition_chain.py`)

`ignite_episode(store, ignition)` (`cognition_chain.py:1025`) opens the episode, then runs the cascade in a `try` block, then the `finally` at `:1353` stamps COMMITTING and runs the DeepSleep terminus.

```
ignite_episode                                            cognition_chain.py:1025
  └─ _ignite_episode_cascade                              :1187
       └─ _start_global_a                                 :2840
            └─ _start_meta_a                              :2853
                 └─ _start_auto                           :3101
                      └─ run_auto / _run_auto             auto_loop/loop.py:173 / :195
                      └─ _start_meta_b_global_b           :3597
                           └─ _run_omega_episode_cognition :1536
                           └─ run_deepsleep_for_episode   deepsleep_loop/loop.py:602
                           └─ run_deepsleep_for_window    deepsleep_loop/loop.py:703
```

There is no `_start_omega` or `_start_deepsleep`. Windowed reflection has landed; DeepSleep for a window is `run_deepsleep_for_window`.

**Synchronicity:** Each `_start_*` function calls the next layer directly on that pipeline. There is no event bus and no inter-tick resumption of a half-finished episode. Four interchangeable pipelines may run four episodes concurrently; admission order is still the FIFO.

---

## 2. EPISODE OPEN / CLOSE CALLERS

### `EpisodeStore.open(...)`

**Single caller on the live path:**

- `cognition_chain.py:1229` — inside `_ignite_episode_cascade`, immediately after the pressure snapshot is built.
  ```python
  episode = store.open(
      opened_by=ignition.opened_by,
      pipeline_id=_pipeline_id,
      work_kind=ignition.work_kind,
      pressure=pressure_snapshot,
      temporal=temporal_context,
      learning_state=learning_state_context,
  )
  ```

Definition: `pressure_loop/episode.py:304`. No other live code calls `EpisodeStore.open`. (`core.episode_wal` uses `restore()`, not `open()`.)

### `EpisodeStore.close()`

Definition: `pressure_loop/episode.py:367`. DeepSleep owns close via `store.close(before_finalize=..., after_archive=...)`.

| Caller | File:Line | Context |
|--------|-----------|---------|
| **Primary (episode)** | `deepsleep_loop/loop.py:657` | `run_deepsleep_for_episode` — SRAM drain + commit inside `before_finalize`. |
| **Primary (window)** | `deepsleep_loop/loop.py:768` | `run_deepsleep_for_window` — same seam, window intake. |
| **Terminus gate** | `cognition_chain.py:1353` | `finally` in `_ignite_episode_cascade`: if `store.current()` is set, hand off to `_run_per_episode_terminus` (`cognition_chain.py:1461` → `run_deepsleep_for_episode`) or `_run_window_terminus` (`cognition_chain.py:1521` → `run_deepsleep_for_window`). |

`deepsleep_loop/closure/run.py:11` `finalize_window` stages reseed after a window fold. It does **not** call `store.close()` — DeepSleep no longer owns close from that helper.

**What `close()` itself does** (`pressure_loop/episode.py:367`):

1. Sets `ep.state = "closing"` and `ep.closed_at = _now_iso()`.
2. Builds the enriched body, then runs `before_finalize` (DeepSleep commit) while the carrier is still active.
3. **Resets pressure accumulator** via `pressure_loop.pressure_field.reset_pressure_accumulator()`.
4. **Archives episode journal** via `pressure_loop.episode_journal.archive_episode(ep)`.
5. Runs `after_archive` (mail acknowledgement / transaction settle).

> **Critical:** `close()` does NOT clear `LAST_OMEGA_TRAJECTORY`. The cue-sheet contract carves trajectory out as multi-epoch guidance.

---

## 3. DEEPSLEEP CLOSURE / RESET — EXACT TOUCHES

### Full DeepSleep chain (`deepsleep_loop/loop.py:885`)

`_run_deepsleep_episode_core` is the one terminus. `run_deepsleep_for_episode` (`:602`) and `run_deepsleep_for_window` (`:703`) both call `store.close` then that core. Required substrate failures raise; they do not fail-soft into an empty success.

Phases inside `_run_deepsleep_episode_core` (window scope skips mastery / failure / semantic / identity / query_lifecycle via `WINDOW_SKIPPED_PHASES`):

1. **intake**
2. **mastery** (episode scope)
3. **failure** (episode scope)
4. **semantic** (episode scope)
5. **identity** (episode scope)
6. **trajectory** (both scopes)
7. **query_lifecycle** (episode scope)
8. **consolidate**
9. then the serialized commit region: SRAM drain, canonical writes, reindex, window append, maintenance

### Episode-close vs window-consolidation boundary

**Windowed reflection has landed.** `deepsleep_loop/loop.py` defines both
`run_deepsleep_for_episode` and `run_deepsleep_for_window`; `cognition_chain.py`
calls them on separate paths (`:1461` / `:1521`). The window (ten-episode reflection) is a first-class
lane on the same cascade — see [`COGNITIVE_CASCADE_CURRENT.md`](COGNITIVE_CASCADE_CURRENT.md).

- **Episode terminus:** SRAM drain, filesystem writes, graph reindex, maintenance, `store.close`.
- **Window terminus:** same commit region; skips the five per-episode re-derivation phases; consolidates trajectory + window synthesis.

---

## 4. TRAJECTORY FLOW

### Ignition snapshot

1. **Creation in pressure evaluation** (`pressure_loop/pressure.py:1880` `evaluate`):
   - Optional `trajectory_snapshot` argument.
   - When that argument is `None` and shared state is present, `evaluate` reads `shared_state.read(LAST_OMEGA_TRAJECTORY, None)` (`pressure_loop/pressure.py:1990`).
   - A missing slot is legitimately empty; a corrupt present value must not silently become `None`.
   - Result is stored on `EpisodeIgnition.trajectory_snapshot`.

2. **Pass-through to episode** (`cognition_chain.py:1203`):
   ```python
   "trajectory_snapshot": dict(ignition.trajectory_snapshot or {}),
   ```
   This stores the snapshot (or `{}`) into `episode.pressure["trajectory_snapshot"]`.

3. **Omega** no longer has `omega_loop/intake/run.py`. Per-episode Omega is `omega_loop/episode/`. Window trajectory updates live in `omega_loop/window/run.py`.

### `LAST_OMEGA_TRAJECTORY` / cue-sheet contract

- Defined in `core/shared_state_keys.py:145`.
- Key contract (`core/shared_state_keys.py:762`): writer="omega_loop", readers=["global_loop", "deepsleep_loop"]. Description says it is "multi-epoch directional pressure from omega" and is "NOT cleared by deepsleep's cascade reset."
- `hud/telemetry.py:2110` reads it for the Trajectory Star Map panel.
- Onboarding can publish into the slot (`hud/operator_onboarding.py`).

---

## 5. PER-EPISODE OMEGA / DEEPSLEEP CONSUMERS

Omega runs **every episode**. Windowed reflection is an additional Omega lane, not a substitute for the per-episode fold.

DeepSleep consumers are the phases inside `_run_deepsleep_episode_core` (`deepsleep_loop/loop.py:885`): mastery, identity, semantic, trajectory, failure, query lifecycle, projection/commit, SRAM drain through `memory.api`, reindex, maintenance, then `store.close`. Reseed after a window uses `deepsleep_loop/closure/run.py:11` `finalize_window` → `stage_reseed`.

---

## 6. SUMMARY TABLE: WHO OWNS WHAT

| Lifecycle Event | Owner | File:Line | Notes |
|-----------------|-------|-----------|-------|
| Tick loop entry | `pressure_loop.loop` | `boot.py:614` → `boot.py:940` → `pressure_loop/loop.py:356` | One loop; four pipelines. |
| Tick body | `pressure_loop.loop` | `pressure_loop/loop.py:192` | Gather, evaluate, FIFO admit, dispatch. |
| Cascade ignition | `cognition_chain` | `cognition_chain.py:1025` | Opens episode, runs cascade, terminus in `finally` `:1353`. |
| Global A | `global_loop.loop` | `global_loop/loop.py` (called from `_start_global_a` `:2840`) | Intent-only output (`ActionGoal`). |
| Meta A | `meta_loop.loop` | `meta_loop/loop.py` (called from `_start_meta_a` `:2853`) | Execution action pack. |
| AUTO | `auto_loop.loop` | `auto_loop/loop.py:173` (called from `_start_auto` `:3101`) | Model-running layer. |
| Meta B + Global B | `meta_loop.loop` | `meta_loop/loop.py` (called from `_start_meta_b_global_b` `:3597`) | Deterministic; produces `GlobalOutcomePack`. |
| Omega | `omega_loop.episode` | `_run_omega_episode_cognition` `:1536` | Model-running layer; every episode. |
| DeepSleep | `deepsleep_loop.loop` | `run_deepsleep_for_episode` `:602` / `run_deepsleep_for_window` `:703` | Core `:885`; close at `:657` / `:768`. |
| DeepSleep handoff | `cognition_chain` | `cognition_chain.py:1461` episode / `cognition_chain.py:1521` window | From the terminus `finally`. |
| Episode open | `EpisodeStore` | `pressure_loop/episode.py:304` | Called by `ignite_episode`. |
| Episode close | `EpisodeStore` | `pressure_loop/episode.py:367` | Called by DeepSleep `store.close`. |
| Window reseed | `deepsleep_loop.closure` | `deepsleep_loop/closure/run.py:11` | Stages omega reseed; does not close. |
| Pressure reset | `pressure_loop.pressure_field` | inside `EpisodeStore.close()` | After required close projections commit. |
| Episode journal archive | `pressure_loop.episode_journal` | inside `EpisodeStore.close()` | After retryable close work succeeds. |
| Trajectory read (ignition) | `pressure_loop.pressure` | `pressure_loop/pressure.py:1990` | `LAST_OMEGA_TRAJECTORY` when no explicit snapshot. |
| Trajectory write (omega) | `omega_loop.window` | `omega_loop/window/run.py` | Window `trajectory_update`; not a SharedState write from per-episode Omega. |

---

*Map generated by Devin (read-only audit lane). Ref: windowed-omega-reflection-map. Live citations refreshed 2026-09-19.*
