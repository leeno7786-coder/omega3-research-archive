# Windowed Omega Reflection — As-Built Documentation

> **Historical snapshot — superseded 2026-08-03.** The windowed Omega and
> window-level DeepSleep paths described here are retired. This file is not
> current architecture authority; see `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

**Status:** BUILT + live-verified (480/0 core tests + 10-tick live boot: ticks 1-9 journal-only, tick 10 fired Gemma reflection over a 10-episode window → deepsleep window consolidation → reflection_window reset → current_trajectory updated).  
**Authority:** `docs/_archive/architecture/WINDOWED_OMEGA_REFLECTION_PACKETS.md` + `docs/architecture/The 10-episode window is the cleane.txt`  
**Map ref:** `EPISODE_LIFECYCLE_MAP.md`  
**Branch:** `main`

---

## Core Change (one sentence)

Omega moved from **per-episode** (ran inline in the cascade every cycle) to **per-window** (runs once per 10 completed episodes, triggered by `EpisodeStore`). Omega became a *behavior* analyzer, not an *event* analyzer.

---

## 10-Episode Window Lifecycle

```text
Episode 1 closes
    ↓
EpisodeStore: no active window → OPEN new window
    ↓
Snapshot current_trajectory.json → immutable baseline
    ↓
Collect episodes 2-9 (journal only)
    ↓
Episode 10 closes
    ↓
EpisodeStore: count == 10 → LOCK window, set window_ready
    ↓
Pressure tick (next ignition) → _maybe_run_window_reflection()
    ↓
Omega (run_omega_for_window) → reason over 10 episodes → new trajectory
    ↓
DeepSleep (run_deepsleep_for_window) → consolidate window → reseed
    ↓
EpisodeStore.archive_and_reset_window() → archive to memory/600_journal/reflection_windows
    ↓
Clear reflection_window.json
    ↓
Episode 11 closes → OPEN next window (inherits updated trajectory)
```

**Per-tick behavior:** 9 out of 10 episodes end at `EpisodeStore.close()` (journal + window append only). The 10th episode triggers the full window reflection chain synchronously before the next tick begins.

---

## Ownership Boundaries

> **REFLECTION WINDOW LAW:** The reflection window is owned exclusively by `EpisodeStore`. Omega may read windows. DeepSleep may consume Omega outputs. Neither Omega nor DeepSleep may create, append, modify, close, or archive windows. Only `EpisodeStore` may mutate reflection window state.

| Operation | Owner | File:Line | Notes |
|-----------|-------|-----------|-------|
| Window open | `EpisodeStore` | `pressure_loop/episode.py:537-540` | On first `close()` with no active window |
| Window append | `EpisodeStore` | `pressure_loop/episode.py:542` | `_append_episode_to_reflection_window` |
| Window lock | `EpisodeStore` | `pressure_loop/reflection_window.py:213-214` | `append_episode_to_window` at `WINDOW_SIZE=10` |
| Window persist | `EpisodeStore` | `pressure_loop/episode.py:544` | `save_reflection_window` |
| Window ready signal | `EpisodeStore` | `pressure_loop/episode.py:545-546` | `_window_ready = True` |
| Window consume | `EpisodeStore` | `pressure_loop/episode.py:481-487` | `consume_window_ready()` (idempotent) |
| Window archive/reset | `EpisodeStore` | `pressure_loop/episode.py:494-509` | `archive_and_reset_window()` |
| Trajectory snapshot | `EpisodeStore` | `pressure_loop/reflection_window.py:188` | `open_reflection_window` reads `current_trajectory.json` |
| Trajectory write | `Omega package` | `omega_loop/package/run.py:25-36` | `save_current_trajectory` |
| Omega read | `Omega` | `omega_loop/intake/run.py:11-30` | Reads window, builds `OmegaInput` |
| DeepSleep consume | `DeepSleep` | `deepsleep_loop/intake/run.py:11-41` | Reads `WindowReflection` + window |

---

## State Files

| File | Path | Shape | Writer | Reader |
|------|------|-------|--------|--------|
| Active reflection window | `guardian/state/reflection_window.json` | `ReflectionWindow` dict | `EpisodeStore.close()` | `EpisodeStore.__init__` (resume), Omega intake, DeepSleep intake |
| Current trajectory | `guardian/state/current_trajectory.json` | `{target_domain, target_capability, confidence, rationale, source_window_id, trajectory_diff}` | `omega_loop/package/run.py` | `open_reflection_window` (snapshot at window-open) |
| Window archive | `memory/600_journal/reflection_windows/{window_id}.json` | `{window, omega_outcome}` | `EpisodeStore.archive_and_reset_window()` | Audit / future recall |
| Episode journal | `memory/600_journal/episodes.jsonl` (default) | `PriorEpisode` JSONL lines | `EpisodeStore.close()` → `archive_episode` | `journal_episodes_by_ids` (Omega/DeepSleep intake) |

### `ReflectionWindow` shape (`pressure_loop/reflection_window.py:43`)
```python
{
    "window_id": str,
    "started_at_episode": str,
    "episode_count": int,
    "trajectory_snapshot": dict,  # immutable baseline
    "episodes": [episode_id, ...],  # exactly 10 when locked
    "status": "collecting" | "locked"
}
```

---

## The 8 Invariants

1. **Omega never runs on a single episode.** Omega only consumes locked `ReflectionWindow` objects with 10 episodes.
2. **Omega only runs on completed windows.** The window must be `locked` (`status == "locked"` and `episode_count >= 10`).
3. **Every episode still journals.** `EpisodeStore.close()` always calls `archive_episode()` (`pressure_loop/episode.py:355-360`), regardless of window state.
4. **DeepSleep only executes after Omega.** `cognition_chain._maybe_run_window_reflection` runs Omega first, then DeepSleep, then archive (`cognition_chain.py:76-114`).
5. **Window trajectory snapshot is immutable.** Once `open_reflection_window` copies `current_trajectory.json` into the window, that baseline never changes for the life of the window (`tests/core/test_reflection_window.py:140-169`).
6. **Window = exactly 10 episodes.** `WINDOW_SIZE = 10` (`pressure_loop/reflection_window.py:14`). `append_episode_to_window` locks at 10 and refuses an 11th (`pressure_loop/reflection_window.py:206-214`; `tests/core/test_reflection_window.py:110-137`).
7. **New trajectory only after reflection.** `current_trajectory.json` is written by `omega_loop/package/run.py:25-36` during package phase. The next window's snapshot inherits this value.
8. **Reflection is behavior-based, not event-based.** The Omega prompt explicitly analyzes patterns across the 10-episode window (`omega_loop/_prompt.py:19-56`).

---

## Synchronous Execution Model

**NO event bus.** The spec's `WindowReadyEvent → Omega consumes` is **NOT** an event bus. It is:
- `close()` sets a `_window_ready` flag (`pressure_loop/episode.py:545-546`)
- The **pressure tick** runs Omega + DeepSleep synchronously after the 10th close, then resets the window
- Anyone who builds an event bus / async consumer / second state machine has hit the exact trap the spec warns about

The flow is strictly sequential within one tick:

```text
ignite_episode(store, ignition)
    _maybe_run_window_reflection(store)   # if window_ready from PREVIOUS episode
    store.open(...)
    _start_global_a(...)
    _start_meta_a(...)
    _start_auto(...)
    _start_meta_b_global_b(...)
    finally:
        store.close()                       # journals + appends to window
        _maybe_run_window_reflection(store) # if window is NOW ready (10th episode)
```

`cognition_chain.py:34-73` shows the exact synchronous handoff. The pressure loop thread is blocked until `ignite_episode` returns.

---

## Cascade Decoupling

Omega and DeepSleep are **removed** from the inline per-episode cascade. The cascade now ends at `Meta B + Global B` (`cognition_chain.py:151-157`).

Old cascade (pre-windowed):
```text
Global A → Meta A → AUTO → Meta B → Global B → Omega → DeepSleep → close
```

New cascade (windowed):
```text
Global A → Meta A → AUTO → Meta B → Global B → close → [window reflection if ready]
```

`_start_omega` and `_start_deepsleep` no longer exist in `cognition_chain.py`. The close call relocated from DeepSleep closure to the `finally` block of `ignite_episode` (`cognition_chain.py:70-71`), so every episode closes uniformly.

---

## Omega — Windowed Behavior Analyzer

### Entry point
`omega_loop/loop.py:23` `run_omega_for_window(window, ...)`

### Intake (`omega_loop/intake/run.py:11`)
Builds `OmegaInput` from the locked window:
- `window_id` — from `ReflectionWindow.window_id`
- `trajectory_snapshot` — the immutable baseline
- `episode_window` — 10 `PriorEpisode` dicts loaded via `journal_episodes_by_ids`
- `trajectory_diff` — precomputed `FOLLOWING|PARTIAL_FOLLOWING|DRIFTING|ABANDONED`

### TrajectoryDiff (`omega_loop/_omega_state.py:132`)
Deterministic ratio of window episodes that successfully align with the snapshot's `target_domain`/`target_capability`:
- `FOLLOWING` — ≥75% aligned + success
- `PARTIAL_FOLLOWING` — ≥40%
- `DRIFTING` — ≥15%
- `ABANDONED` — <15% or empty window

### Reason (`omega_loop/reason/run.py:14`)
- Prompt is window-level: "Analyze this 10-episode behavior window" (`omega_loop/_prompt.py:59`)
- `enable_thinking=True` — metacognitive reasoning uses the model's thinking block
- Calls `invoke_runtime_generate` with capability `coding_model` (`config/runtime.yaml:57-61`)
- Live boot used gemma_e4b (runtime registry on-demand load)

### Package (`omega_loop/package/run.py:10`)
Persists the new trajectory to `guardian/state/current_trajectory.json` so the next window inherits it.

---

## DeepSleep — Window Consolidation

### Entry point
`deepsleep_loop/loop.py:37` `run_deepsleep_for_window(store, window, omega_outcome)`

### Intake (`deepsleep_loop/intake/run.py:11`)
Builds `DeepSleepInput` from window + `OmegaOutcomePack`:
- `window_id` / `window_reflection` / `episode_window` / `trajectory_snapshot` / `omega_reasoning`
- Compatibility properties alias to per-episode field names so existing phase code requires minimal change

### Closure shift
`deepsleep_loop/closure/run.py:11` `finalize_window(store, reseed_contributors)` **only stages reseed**. It does NOT call `store.close()` and does NOT reset pressure. Episode closure moved to `cognition_chain.py:70-71`.

### Outcome pack
`DeepSleepOutcomePack` now carries `episode_closed=False` and `pressure_reset=False` because `EpisodeStore` already handled both at cascade end.

---

## Trajectory Continuity

```text
Window N opens
    ↓
Snapshot current_trajectory.json (from Window N-1's Omega)
    ↓
10 episodes collect
    ↓
Omega reasons → writes NEW trajectory to current_trajectory.json
    ↓
Window N+1 opens → snapshots the NEW trajectory
```

This guarantees every window is evaluated against the trajectory produced by the *previous* window's reflection, creating a stable chain of behavioral baselines.

---

## Crash Recovery

1. `EpisodeStore.__init__` loads `reflection_window.json` via `_load_reflection_window_state()` (`pressure_loop/episode.py:254`).
2. If a window was `collecting` with N episodes, collection resumes on the next `close()`.
3. If a window was `locked` and `window_ready` was True, `_maybe_run_window_reflection` retries on the next tick.
4. No reconstruction is needed — the journal JSONL is the source of truth; the window only stores episode IDs.
5. If Omega or DeepSleep fails mid-window, `mark_window_reflection_pending()` re-arms `window_ready` so the next tick retries (`cognition_chain.py:92-93`, `102-103`).

---

## Model Residency

- **Generate socket:** `config/runtime.yaml:57-61` — capability `coding_model`, method `generate_completion`
- **Live model:** gemma_e4b served by omega-portable-lite at `http://127.0.0.1:11434`
- **Load model:** on-demand (lazy) in the runtime registry; loads when the 10th-tick Omega triggers the first generate call
- **Eviction:** runtime registry evicts when idle (portable-lite owns model lifecycle)
- **Thinking:** `enable_thinking=True` for Omega reason (metacognitive reflection benefits from chain-of-thought)

---

## Test Coverage

| Test | File | Coverage |
|------|------|----------|
| Window open/lock/append | `tests/core/test_reflection_window.py` | Opens on first close, collects to 10, locks at 10, no 11th append, snapshot immutable, persists+resumes |
| Window Omega trajectory | `tests/core/test_windowed_omega_trajectory.py` | `TrajectoryDiff` computation, trajectory persistence |
| Window DeepSleep consolidation | `tests/core/test_deepsleep_window_consolidation.py` | Full window chain: intake → mastery → identity → semantic → trajectory → failure → query → consolidate → memory |
| DeepSleep full acceptance | `tests/core/test_deepsleep_full_acceptance.py` | End-to-end window-level DeepSleep with stores |

---

## Related Files

| File | Role |
|------|------|
| `pressure_loop/episode.py` | `EpisodeStore` — window owner |
| `pressure_loop/reflection_window.py` | `ReflectionWindow` dataclass, load/save/archive/clear, `current_trajectory.json` helpers |
| `pressure_loop/episode_journal.py` | `PriorEpisode`, `archive_episode`, `journal_episodes_by_ids` |
| `cognition_chain.py` | `_maybe_run_window_reflection`, `ignite_episode` (close in finally) |
| `omega_loop/loop.py` | `run_omega_for_window` |
| `omega_loop/intake/run.py` | `build_omega_input_for_window` |
| `omega_loop/_omega_state.py` | `OmegaInput`, `TrajectoryDiff`, `OmegaOutcomePack` |
| `omega_loop/_prompt.py` | Window-level prompt |
| `omega_loop/package/run.py` | `package_window` + trajectory persistence |
| `deepsleep_loop/loop.py` | `run_deepsleep_for_window` |
| `deepsleep_loop/intake/run.py` | `build_deepsleep_input_for_window` |
| `deepsleep_loop/_deepsleep_state.py` | `DeepSleepInput` (windowed shape) |
| `deepsleep_loop/closure/run.py` | `finalize_window` (reseed only) |
| `boot.py` | `run_continuous` → pressure tick → cognition chain |
