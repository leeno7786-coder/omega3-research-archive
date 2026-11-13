# DeepSleep Per-Episode Split — Architecture Map

> **Historical design map — superseded 2026-08-03.** DeepSleep is now one
> complete per-episode terminus and owns final closure; the split/window path
> described below is retired. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

**Scope:** MAP ONLY (no code changes). Documents the minimal split to fire DeepSleep **consolidation (mastery update + knowledge intake) per episode** while keeping Omega (inference) + window-level finalization at the 10-episode window boundary.

**Root cause addressed:** Omega runs inference on a 10-episode window, but DeepSleep currently runs **after** Omega at window-close. Therefore Omega reads mastery / trajectory / query state that is **stale** (pre-window). By moving deterministic, non-inference DeepSleep phases to per-episode, Omega on episode 10 reads state that already reflects episodes 1–10.

---

## 1. Current window-reflection coupling (verified by live boot)

### 1.1 Cascade and close
`cognition_chain.py:36–91`

```
ignite_episode(store, ignition)
  ├─ _maybe_run_window_reflection(store)          # pre-open: drains prior window
  ├─ store.open(...)
  ├─ _start_global_a → Meta A → AUTO → Meta B → Global B
  └─ finally:
       store.close(cascade_status)
       _maybe_run_window_reflection(store)         # post-close: window append + maybe reflect
```

`EpisodeStore.close()` (`pressure_loop/episode.py:353–469`):
1. Sets `cascade_status`, `state="complete"`, `closed_at`
2. `archive_episode(ep)` — writes `PriorEpisode` JSONL to episode journal (failure-soft)
3. `enriched_episode_from_episode(ep)` → `body` — builds the maintained-window body (`intent/action/outcome/pressure/execution/brain_turns/topic_history/open_query_state/cascade_status/neuromod`) (`pressure_loop/episode_journal.py:144–213`)
4. `_append_episode_to_reflection_window(episode_id, cascade_status, body)` (`pressure_loop/episode.py:651–683`)
   - Opens new `ReflectionWindow` if none active (`open_reflection_window` reads lobe-9 trajectory baseline)
   - `append_episode_to_window` — appends body; locks at `WINDOW_SIZE = 10` (`pressure_loop/reflection_window.py:445–481`)
   - Saves `guardian/state/reflection_window.json`
   - Sets `_window_ready = True` when locked

### 1.2 Window reflection handoff
`_maybe_run_window_reflection(store)` (`cognition_chain.py:94–127`)

```
window = store.consume_window_ready()       # returns locked window or None
if window:
  omega_outcome = run_omega_for_window(window)          # omega_loop/loop.py
  run_deepsleep_for_window(store, window, omega_outcome) # deepsleep_loop/loop.py:37–46
  store.archive_and_reset_window(window, omega_outcome) # archive + clear
```

### 1.3 DeepSleep phase chain (all window-level today)
`deepsleep_loop/loop.py:49–250` — `_run_deepsleep_core` with intake builder `build_deepsleep_input_for_window(window, omega_outcome)`

| Phase | File | What it does |
|-------|------|--------------|
| intake | `deepsleep_loop/intake/run.py:10–41` | Builds `DeepSleepInput` from `window.episode_bodies[]` + `omega_outcome` + `window.trajectory_snapshot` |
| mastery | `deepsleep_loop/mastery/run.py:173–221` | `update_mastery`: for each success entry in `episode_window`, if `LEARN`/`USE_TOOL`, increment knowledge/application mastery in `load_mastery()` store |
| identity | `deepsleep_loop/identity/run.py:63–102` | `update_identity`: strengthens traits from `omega_outcome.reflection.emergent_observations` |
| semantic | `deepsleep_loop/semantic/run.py:30–76` | `promote_semantic`: when mastery record meets evidence + knowledge threshold, promote to semantic memory |
| trajectory | `deepsleep_loop/trajectory/run.py:103–167` | `maintain_trajectory`: applies **decay** to all active records, then upserts from `omega_outcome.trajectory_update` |
| failure | `deepsleep_loop/failure/run.py:139–195` | `consolidate_failures`: for each failed entry in `episode_window`, increment `FailurePattern` frequency in store |
| query_lifecycle | `deepsleep_loop/query_lifecycle/run.py:163–192` | `run_query_lifecycle`: create from `omega_outcome.reflection.open_questions`, resolve from `omega_outcome.query_resolutions`, archive resolved |
| consolidate | `deepsleep_loop/consolidate/run.py:91–115` | `consolidate`: builds `JournalEntry` from all effects + `knowledge_artifact_for_episode(auto_outcome)`, appends to journal JSONL |
| consolidate_memory | `deepsleep_loop/consolidate_memory/run.py:252–316` | `consolidate_to_memory`: writes envelopes (journal, identity, mastery, semantic, resolved queries) to `memory.api.write_memory` |
| reseed | `deepsleep_loop/reseed/run.py:85–217` | `generate_reseed`: mastery/application gap contributors + trajectory continuation + open queries + **Omega `pressure_feedback`** (raise/lower/create) → `stage_reseed` / `stage_reseed_suppress` |
| closure | `deepsleep_loop/closure/run.py:11–40` | `finalize_window`: `stage_reseed(reseed_contributors)` — pressure contributors staged for next tick |
| projection | `deepsleep_loop/projection/run.py:67–83` | `build_projection`: read-only snapshot of identity/mastery/trajectory stores |

---

## 2. Phase classification: EPISODE-LEVEL vs WINDOW-LEVEL

**Criterion:**
- **EPISODE-LEVEL** = deterministic, no model inference, needs only the **just-closed episode's body**, safe to run once per episode, does not double-count or corrupt state if run 10× per window.
- **WINDOW-LEVEL** = depends on **Omega inference output** (`omega_outcome`), or aggregates over the **entire 10-episode window**, or would corrupt state if run 10× (e.g. decay applied 10×).

### 2.1 EPISODE-LEVEL — safe to run per-episode

| Phase | Why episode-level |
|-------|-------------------|
| **mastery** (`mastery/run.py:173–221`) | Reads `ds_input.episode_window` (list of episode bodies), finds successful entries, increments knowledge/application per domain. Works correctly on a 1-element window. Store is cumulative (`load_mastery` → `upsert_mastery`); each episode updates its own domains. No Omega dependency. |
| **failure** (`failure/run.py:139–195`) | Same pattern: scans `episode_window` for failed entries, increments `FailurePattern` frequency. Works on 1-element window. Store is cumulative. No Omega dependency. |
| **semantic** (`semantic/run.py:30–76`) | Depends only on `mastery_records` produced by mastery phase. If mastery runs per-episode, semantic can check thresholds and promote. `existing` guard in `load_semantic` prevents double-promotion. No Omega dependency. |
| **consolidate** (`consolidate/run.py:91–115`) | Builds `JournalEntry` from effects + `auto_outcome` (computed from episode_window) + optional `knowledge_artifact_for_episode`. `omega_reasoning` and `trajectory_snapshot` would be empty when run without Omega — acceptable for a per-episode journal. No Omega dependency for the core entry. |
| **consolidate_memory** (`consolidate_memory/run.py:252–316`) | Promotes envelopes from the journal/effects to `memory.api`. Works with whatever effects are available from per-episode phases. Memory writes are idempotent per `episode_id` + envelope type. |

### 2.2 WINDOW-LEVEL — must stay at window-close

| Phase | Why window-level |
|-------|------------------|
| **identity** (`identity/run.py:63–102`) | Requires `omega_outcome.reflection.emergent_observations` (`repeated_behaviors`, `stable_patterns`) and `reasoning.who`. Without Omega this phase is a no-op. |
| **trajectory** (`trajectory/run.py:103–167`) | Requires `omega_outcome.trajectory_update` (target_domain, target_capability, confidence). **Critical:** `_apply_decay` runs unconditionally on every call. If run per-episode, decay would fire 10× per window — catastrophic confidence degradation. |
| **query_lifecycle** (`query_lifecycle/run.py:163–192`) | Requires `omega_outcome.reflection.open_questions` and `omega_outcome.query_resolutions`. Without Omega, no queries are created or resolved. `_archive_resolved` would run 10× (idempotent but wasteful). |
| **reseed** (`reseed/run.py:85–217`) | Requires Omega `pressure_feedback` (raise/lower/create), `trajectory_update` continuation flag, and consumes `open_queries()` state that is only meaningful after window-level `query_lifecycle`. Also calls `stage_reseed` / `stage_reseed_suppress` which mutate `_RESEED_STAGING` / `_RESEED_SUPPRESS` (`pressure_loop/pressure.py:57–99`). |
| **closure** (`closure/run.py:11–40`) | Calls `finalize_window` → `stage_reseed`. By design the final window boundary action. |
| **projection** (`projection/run.py:67–83`) | Read-only snapshot. Not harmful per-episode, but produces a degraded snapshot pre-Omega. Better to keep as the **final** post-window state. |
| **intake** (`intake/run.py:10–41`) | The current builder is `build_deepsleep_input_for_window(window, omega_outcome)`. The per-episode path needs a **new** builder (see §3). |

---

## 3. Minimal split proposal

### 3.1 Hook point
In `cognition_chain.py:80–90` (the `finally` block of `ignite_episode`):

```python
        try:
            if store.current() is not None:
                store.close(cascade_status=cascade_status)
            _maybe_run_per_episode_deepsleep(store)   # NEW
            _maybe_run_window_reflection(store)
```

`_maybe_run_per_episode_deepsleep` fires **only when a single episode just closed** and the window is **not yet locked** (or more simply: fires unconditionally after every `close()`, using the `last_closed_body` already held by `EpisodeStore`).

### 3.2 Per-episode DeepSleepInput
New builder (conceptual, in `deepsleep_loop/intake/run.py` or adjacent):

```python
def build_deepsleep_input_for_episode(episode_body: dict, episode_id: str) -> DeepSleepInput:
    return DeepSleepInput(
        window_id=episode_id,               # _run_deepsleep_core uses this as context_id
        window_reflection={},               # no Omega outcome yet
        episode_window=[dict(episode_body)],
        trajectory_snapshot={},             # empty; trajectory stays window-level
        omega_reasoning={},                 # empty; Omega hasn't run
    )
```

All EPISODE-LEVEL phases operate correctly on this input:
- `auto_outcome` property computes from the 1-element `episode_window`
- `global_outcome` property computes from the 1-element `episode_window`
- `action_goal` extracts intent/target from the single body
- `execution_pack` extracts action from the single body

### 3.3 Per-episode runner
New entry point in `deepsleep_loop/loop.py` (adjacent to `run_deepsleep_for_window`):

```python
def run_deepsleep_for_episode(store, episode_id: str, episode_body: dict):
    return _run_deepsleep_episode_core(
        store,
        episode_id,
        lambda: build_deepsleep_input_for_episode(episode_body, episode_id),
    )
```

`_run_deepsleep_episode_core` is a **truncated** version of `_run_deepsleep_core` that runs **only** the EPISODE-LEVEL phases:

```
intake → mastery → failure → semantic → consolidate → consolidate_memory
```

It **skips** identity, trajectory, query_lifecycle, reseed, closure, projection.

### 3.4 Window-level runner (unchanged structure, narrower input expectation)
`run_deepsleep_for_window(store, window, omega_outcome)` continues to run the **full** `_run_deepsleep_core` with `build_deepsleep_input_for_window`. By the time it runs:
- `mastery` store already reflects episodes 1–10
- `failure` store already reflects episodes 1–10
- `semantic` store already reflects any promotions from episodes 1–10

The window-level phases (identity, trajectory, query_lifecycle, reseed, closure) then add the Omega-coupled layer on top of this already-accurate substrate.

### 3.5 Visual split

```
Per episode (after EpisodeStore.close):
  ┌─ intake (1-episode body)
  ├─ mastery update
  ├─ failure consolidation
  ├─ semantic promotion (from mastery)
  ├─ consolidate (journal entry)
  └─ consolidate_memory (memory.api writes)

Per window (at 10th episode, after Omega):
  ┌─ intake (10-episode window + omega_outcome)
  ├─ identity (omega emergent observations)
  ├─ trajectory (omega trajectory_update + decay)
  ├─ query_lifecycle (omega open_questions / resolutions)
  ├─ consolidate (window-level journal entry WITH omega reasoning)
  ├─ consolidate_memory (window-level envelopes)
  ├─ reseed (omega pressure_feedback + trajectory + gaps)
  ├─ closure / finalize_window (stage_reseed)
  └─ projection (final snapshot)
```

---

## 4. Flagged behavior changes

### 4.1 Knowledge recallable within the same window
Per-episode `consolidate_memory` writes knowledge artifacts to `memory.api` immediately after episode close. Later episodes in the **same window** (e.g. episode 7 after episode 3's consolidation) could recall that knowledge via AUTO's `remember_*` tools. **Assessment: SAFE and desirable** — earlier availability is a feature, not a bug. The memory substrate is designed for live reads.

### 4.2 Mastery current per-episode
After episode 3 closes, `load_mastery()` returns state reflecting episodes 1–3. Any reader (e.g. Omega on episode 10, or HUD telemetry) sees progressively more accurate state. This is the **primary benefit** of the split.

### 4.3 Journal semantics change
Today: `consolidate` appends **1 JournalEntry per 10-episode window`.
After split: `consolidate` appends **1 JournalEntry per episode` (from per-episode path) + **1 JournalEntry per window` (from window-level path).

- `read_journal(n=50)` would return ~50 episodes instead of ~50 windows.
- Any consumer interpreting journal lines as "windows" must be updated.
- **Mitigation:** the per-episode journal entry can be tagged (`"scope": "episode"`) and the window-level entry tagged (`"scope": "window"`) so consumers can filter.

### 4.4 Memory write granularity
Per-episode `consolidate_memory` writes:
- 1 journal envelope per episode
- 1 mastery envelope per updated domain per episode
- 1 failure envelope per failure pattern per episode (if any)
- Semantic envelopes only when promotion threshold is first crossed

This is **more granular** than today but not corrupt. The `cycle_id` in each envelope is the `episode_id`, so duplicates are distinguishable.

### 4.5 Pressure reseed timing stays at window boundary
Per-episode DeepSleep does **not** stage reseed contributors. Reseed (mastery gaps, application gaps, trajectory continuation, open questions, Omega pressure feedback) is still staged only at window-close via `finalize_window`. Therefore:
- Episodes 1–9 do **not** see reseeded pressure from their own mastery updates.
- Episode 11 (first episode of next window) sees the accumulated reseed from the full 10-episode window.
- **Assessment: acceptable for minimal split.** Immediate per-episode reseed feedback would require moving `reseed` to per-episode, but `reseed` depends on Omega `pressure_feedback` and `open_queries` state, so it cannot be split without deeper changes.

### 4.6 Trajectory decay timing
Trajectory decay (`_apply_decay`) remains at window-level. If it ran per-episode, every episode would increment `passes_since_reinforcement` and potentially apply confidence decay. Keeping it window-level preserves the current decay cadence.

### 4.7 Query lifecycle timing
Open queries are created from Omega reflection and resolved from Omega `query_resolutions`. They remain window-level. The `open_queries()` call in `reseed` therefore returns the same state for the entire window. No per-episode open-query churn.

---

## 5. Composition with in-flight Phase 2

Phase 2 work order (`TOPIC_RESOLUTION_P2_WORKORDER.md`) adds:

1. **`_episode_target` prefers resolved `execution_inputs.domain`** (`deepsleep_loop/mastery/run.py:35–53`)
   - **Composes perfectly.** `_episode_target` reads `execution.execution_action_pack.execution_inputs.domain` from the enriched body. Per-episode mastery runs on the same enriched body, so domain resolution works identically.

2. **`topic_history` on the enriched body** (`pressure_loop/episode_journal.py:173–179`)
   - **Composes perfectly.** The enriched body carries `topic_history` into the reflection window. Per-episode DeepSleep sees it in the 1-element `episode_window`.

3. **`current_mastery_state` snapshot on `OmegaInput`** (`omega_loop/intake` — failure-soft `load_mastery`)
   - **This is the precise motivation for the split.** Today Omega reads `load_mastery()` before any window-level DeepSleep has updated it, so it sees stale pre-window state. After the split, Omega reads `load_mastery()` **after** 10 per-episode mastery updates, so it sees the true current state. The temporal constraint is resolved.

4. **`global open_questions_summary` on `OmegaInput`** (no episode_id threading for P2)
   - **Composes perfectly.** This is a window-level Omega input. It stays window-level. Per-episode DeepSleep does not touch queries.

5. **Prompt fields** (Omega prompt additions)
   - **Composes perfectly.** The prompt reads `current_mastery_state` which is now accurate.

**Deferred Phase 3 items** (mastery-delta cross-window history, per-episode open-question counting, memory-tool, trajectory-preferential) are unaffected by this split and remain deferred.

---

## 6. Files touched in a future build (NOT changed now)

| File | Change |
|------|--------|
| `cognition_chain.py:80–90` | Insert `_maybe_run_per_episode_deepsleep(store)` after `store.close()` |
| `deepsleep_loop/loop.py` | Add `run_deepsleep_for_episode` + `_run_deepsleep_episode_core` (truncated phase chain) |
| `deepsleep_loop/intake/run.py` | Add `build_deepsleep_input_for_episode` |
| `deepsleep_loop/consolidate/run.py` | Optionally tag journal entries with `"scope": "episode"` / `"scope": "window"` |

---

*Map compiled by Devin. Ready for operator/Claude review before slicing the build.*
