# Model C Windowed Reflection — As-Built Map

> **Historical snapshot — superseded 2026-08-03.** Windowed reflection is not
> an active cognition or memory path. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

> **Status:** LANDED + live-proven (commits `1771a39`..`85abfb1` on `main`).  
> **Authority:** `docs/_archive/architecture/SLICE_4_MODEL_C_WINDOWED_REFLECTION.md` (build spec).  
> **Supersedes:** Slice-4c lobe-9-query model (`SLICE_4C_PACKET.md`).  
> **Mapper:** Devin · **Date:** 2026-06-07

---

## 0. Two-tier model (the core re-wire)

**Tier 1 — MAINTAINED WINDOW (in-flight):** a `ReflectionWindow` owned exclusively by `EpisodeStore`, holding the current ≤10 **enriched episode BODIES** (not just ids). In-memory + crash-persist `guardian/state/reflection_window.json`. Every `close()` appends the full enriched body to this window. At `WINDOW_SIZE = 10` the window locks.

**Tier 2 — TEMPORAL LOBE (lobe 9) = Omega's reflected read/write space:** reflected history. Episodes reach lobe 9 **ONLY** via Omega's package phase after the 10-episode reflection. Omega reads prior trajectory from lobe 9 at window-open time; writes the 10 reflected bodies + new trajectory back to lobe 9 after reasoning.

**Why two tiers:** the maintained window gives Omega/DeepSleep instant access to rich episode state (no lobe-9 query latency mid-cascade), while lobe 9 becomes the authoritative *reflected* archive — Omega owns what gets persisted, when, and in what form.

---

## 1. Lifecycle diagram

```text
Episode 1 closes
    |
    v
EpisodeStore._append_episode_to_reflection_window()
  -> no active window -> open_reflection_window()
     -> read_trajectory() from lobe 9 (temporal_store:132-217)
     -> snapshot immutable baseline
  -> append_episode_to_window() with enriched body
  -> save_reflection_window() to guardian/state/reflection_window.json
    |
    v
Episodes 2-9 close (same append path; window stays collecting)
    |
    v
Episode 10 — after Global B (inline, same tick)
    |
    v
_run_tenth_episode_window_pipeline()
  -> per-episode journal (DeepSleep mastery for ep 10)
  -> stage_episode_for_window_reflection() -> len == 10 -> LOCKED
  -> append_episode_mastery_delta() onto window
  -> _run_omega_window_reflection()
  -> store.get_locked_window() returns maintained window
    |
    +---> Omega intake (omega_loop/intake/run.py:10-30)
    |       reads window.episode_bodies (NOT lobe 9)
    |     Omega reason -> reflect -> package
    |     package writes 10 bodies + trajectory to lobe 9
    |       (omega_loop/package/run.py:25-40)
    |     -> write_episode_to_temporal(body) for each body
    |     -> write_trajectory(new_trajectory)
    |
    +---> DeepSleep intake (deepsleep_loop/intake/run.py:10-41)
    |       reads window.episode_bodies (same maintained window)
    |     mastery / identity / semantic / trajectory / failure /
    |     query_lifecycle / consolidate / projection / reseed / closure
    |
    v
EpisodeStore.archive_and_reset_window()
  -> archive_reflection_window() -> memory/600_journal/reflection_windows/{id}.json
  -> clear_reflection_window() -> unlink guardian/state/reflection_window.json
  -> in-memory window nulled
    |
    v
Episode 11 closes -> open next window (inherits lobe-9 trajectory baseline)
```

---

## 2. SEAMS table (as-built)

| Seam | Read or Write | Producer | Consumer | File:Line |
|---|---|---|---|---|
| **Active episode** | Write | `EpisodeStore.open()` | Global A, Meta A, AUTO, Meta B, Global B (slot mutations) | `pressure_loop/episode.py:261-327` |
| **Episode close** | Write | `EpisodeStore.close()` | — | `pressure_loop/episode.py:329-392` |
| **Enriched body** | Write | `enriched_episode_from_episode()` | `_append_episode_to_reflection_window` | `pressure_loop/episode_journal.py:144-178` → `episode.py:377-383` |
| **Reflection window JSON** | Write | `_append_episode_to_reflection_window()` | `load_reflection_window()` (boot resume) | `episode.py:537-569` → `reflection_window.py:132-149` |
| **Locked window consumption** | Read | `EpisodeStore.consume_window_ready()` | Omega + DeepSleep | `episode.py:501-504` → `cognition_chain.py:88` |
| **Window archive** | Write | `EpisodeStore.archive_and_reset_window()` | Audit / future recall | `episode.py:505-525` → `reflection_window.py:178-198` |
| **Omega intake** | Read | `window.episode_bodies` | `build_omega_input_for_window()` | `omega_loop/intake/run.py:20-23` |
| **Omega lobe-9 write** | Write | `package_window()` | lobe 9 `(9,1)` + `(9,2)` | `omega_loop/package/run.py:25-40` |
| **DeepSleep intake** | Read | `window.episode_bodies` | `build_deepsleep_input_for_window()` | `deepsleep_loop/intake/run.py:22-25` |
| **Trajectory baseline** | Read | `temporal_store.read_trajectory()` | `open_reflection_window()` | `pressure_loop/temporal_store.py:118-150` → `reflection_window.py:211-217` |
| **Trajectory update** | Write | `write_trajectory()` | next window's baseline | `pressure_loop/temporal_store.py:153-171` |

---

## 3. The 10 invariants + enforcement site

| # | Invariant | Enforced at | File:Line |
|---|---|---|---|
| 1 | **Omega never on a single episode.** | `run_omega_for_window` only accepts a locked window; no per-episode Omega path exists. | `omega_loop/loop.py:23-53` |
| 2 | **Omega only on locked 10-windows.** | `is_ready_for_reflection()` requires `is_locked()` + `episode_count >= WINDOW_SIZE`. | `pressure_loop/reflection_window.py:99-100` |
| 3 | **Every episode appended to the window at close.** | `EpisodeStore.close()` always calls `_append_episode_to_reflection_window` with the enriched body. | `pressure_loop/episode.py:376-385` |
| 4 | **DeepSleep only after Omega.** | `_maybe_run_window_reflection` runs Omega first, then DeepSleep, then archive. | `cognition_chain.py:95-114` |
| 5 | **Trajectory snapshot at window OPEN, immutable.** | `open_reflection_window()` captures `read_trajectory()` once; the snapshot field is never mutated after creation. | `pressure_loop/reflection_window.py:201-224` |
| 6 | **Exactly 10 episodes per window.** | `append_episode_to_window` locks at `WINDOW_SIZE = 10` and refuses an 11th append. | `pressure_loop/reflection_window.py:14`, `:234-248` |
| 7 | **New trajectory only after reflection.** | `write_trajectory()` is called only in `package_window` post-Omega-reasoning. | `omega_loop/package/run.py:31-40` |
| 8 | **Behavior-based reflection.** | Omega prompt analyzes patterns across the 10-episode window (`_prompt.py`). `trajectory_diff` is computed from the full episode_window bodies. | `omega_loop/_prompt.py`, `_omega_state.py:132-194` |
| 9 | **REFLECTION WINDOW LAW: only EpisodeStore mutates window state.** | Omega/DeepSleep receive the window as read-only; no method outside `EpisodeStore` calls `save_reflection_window`, `append_episode_to_window`, or `archive_and_reset_window`. | `pressure_loop/episode.py` (sole writer) |
| 10 | **Episodes reach lobe 9 ONLY via Omega post-reflection.** | `EpisodeStore.close()` does NOT call `write_episode_to_temporal` (removed in MC-1). The only lobe-9 episode writes are in `package_window`. | `omega_loop/package/run.py:27-29` |

---

## 4. Enriched episode body (what the window carries)

`enriched_episode_from_episode(ep)` (`pressure_loop/episode_journal.py:144-178`) builds each body from the closed episode:

```python
{
    "episode_id": str,
    "intent": str | None,           # from global_.action_goal.intent
    "action": str | None,           # from meta.execution_action_pack.action
    "outcome": dict | None,         # from global_.outcome
    "pressure": dict,               # full ep.pressure snapshot
    "execution": dict,              # execution_action_pack + state
    "brain_turns": list[dict],      # in-cognition brain receipts
    "open_query_state": list[dict], # active STATUS_OPEN questions
    "cascade_status": str,           # "completed" | "failed"
}
```

- `open_query_state` = latest-per-id OPEN questions from `core/open_questions.py` (`_latest_per_id(_load_all())`, filter `STATUS_OPEN`) — lazy import, failure-soft (empty list if unavailable).
- The **PriorEpisode** distillation (episode_id, ignition_reason, intent, action, outcome, trajectory_update, cascade_status) is still archived to the JSONL journal by `archive_episode()` (`episode_journal.py:186-203`), but Omega/DeepSleep no longer read from there — they use the maintained window's `episode_bodies` directly.

---

## 5. Before → after (what the Slice-4c query model was, and what Model C superseded)

### Slice-4c query model (superseded)

In the 4c design (`SLICE_4C_PACKET.md`):
- `EpisodeStore.close()` wrote each closed episode to lobe 9 immediately via `write_episode_to_temporal(ep)` (4a additive).
- The "window" was a **query** against lobe 9: `unreflected_episode_ids()` scanned lobe 9 for episodes after the last `reflection_marker`, returning up to 10 episode IDs.
- `consume_window_ready()` would query lobe 9, build a transient `ReflectionWindow` from the query results, and return it.
- `archive_and_reset_window()` would write a `reflection_marker` record to lobe 9 to advance the "reflected up to here" boundary.
- Omega/DeepSleep intake called `temporal_episodes_by_ids()` or `journal_episodes_by_ids()` to hydrate episode data.

### Why it was superseded

The 4c query model had two hardened BLOCKERS:
1. **BLOCKER-1 (enrichment):** Querying lobe 9 for episodes only returned the `PriorEpisode` distillation (lossy: no pressure, execution, brain_turns, open_query_state). Omega needed the **full enriched body** to reason behaviorally.
2. **BLOCKER-2 (read latency + fragility):** Making Omega/DeepSleep intake depend on a lobe-9 query meant every reflection tick incurred a memory read that could fail or return stale data. The maintained window keeps the rich bodies in-memory + crash-persisted JSON, eliminating the query path from the hot reflection cascade.

### Model C changes (what replaced 4c)

| Aspect | 4c query model | Model C (as-built) |
|---|---|---|
| **Window storage** | Lobe 9 is authoritative; window is a query | Maintained `ReflectionWindow` object (EpisodeStore-owned) |
| **Episode bodies at close** | Written to lobe 9 immediately (4a) | Appended to maintained window only; lobe 9 untouched |
| **What Omega reads** | `temporal_episodes_by_ids()` from lobe 9 | `window.episode_bodies` (maintained) |
| **What DeepSleep reads** | Same lobe-9 query or journal | `window.episode_bodies` (maintained) |
| **Lobe-9 writes** | At `close()` + at archive (marker) | **Only** in Omega package post-reflection |
| **Trajectory baseline** | `current_trajectory.json` | `temporal_store.read_trajectory()` from lobe 9 `(9,2)` |
| **Reflection marker** | `write_reflection_marker()` to lobe 9 | **Deleted** — not needed; window is maintained |
| **Dead code deleted** | — | `build_reflection_window_from_query`, `unreflected_episode_ids`, `temporal_window_ready`, `read/write_reflection_marker`, `temporal_episodes_by_ids`, `mark_window_reflection_pending` (MC-4) |

### What stayed from 4a

- `pressure_loop/temporal_store.py` — kept `write_episode_to_temporal()`, `read_trajectory()`, `write_trajectory()`.
- `temporal_store.write_episode_to_temporal()` — now called **only** from `omega_loop/package/run.py` with the enriched body dict (not the full CognitiveEpisode object).
- `skip_embed=True` on all temporal writes — lobe 9 records are not embedded, keeping the :11434 runtime unloaded for temporal persistence.

---

## 6. TrajectoryDiff fix (MC-0)

Prior to Model C, `open_reflection_window()` read from `load_current_trajectory()` → `guardian/state/current_trajectory.json`. This file was written by `omega_loop/package/run.py` calling `save_current_trajectory()`. On a fresh system or after any crash where the file was missing/empty, the trajectory snapshot was `{}`, causing `compute_trajectory_diff` to always return `ABANDONED` (ratio 0 / empty baseline).

**Fix:** `open_reflection_window()` now reads the trajectory baseline from `temporal_store.read_trajectory()` (`pressure_loop/temporal_store.py:118-150`), which queries lobe 9 `(9,2)` for the latest trajectory record. Even on a fresh system this returns `{}` (same behavior), but on a running system it correctly inherits the trajectory written by the **previous window's Omega package phase** to lobe 9 — so `TrajectoryDiff` is now meaningful across windows.

```python
# reflection_window.py:211-217
from pressure_loop.temporal_store import read_trajectory
snapshot = read_trajectory()   # lobe 9, not current_trajectory.json
```

---

## 7. Incoming (MC-6)

- **MC-6:** `write_episode_to_temporal()` will carry the **full enriched body** (intent/action/outcome/pressure/execution/brain_turns/open_query_state/cascade_status) instead of the current `PriorEpisode` distillation. The current temporal write stores only `episode_id`, `ignition_reason`, `intent`, `action`, `outcome`, `trajectory_update`, `cascade_status` — the enriched fields are dropped. This is tracked for a follow-up sub-slice.

---

## 8. Related files

| File | Role |
|------|------|
| `pressure_loop/reflection_window.py` | `ReflectionWindow` dataclass, load/save/archive/clear, `open_reflection_window`, `append_episode_to_window` |
| `pressure_loop/episode.py` | `EpisodeStore` — window owner; `close()` appends enriched body |
| `pressure_loop/episode_journal.py` | `enriched_episode_from_episode`, `PriorEpisode`, `archive_episode` |
| `pressure_loop/temporal_store.py` | Lobe-9 read/write: `write_episode_to_temporal`, `read_trajectory`, `write_trajectory` |
| `cognition_chain.py` | `_maybe_run_window_reflection`, `ignite_episode` |
| `omega_loop/loop.py` | `run_omega_for_window` |
| `omega_loop/intake/run.py` | `build_omega_input_for_window` (reads `window.episode_bodies`) |
| `omega_loop/package/run.py` | `package_window` (writes 10 bodies + trajectory to lobe 9) |
| `omega_loop/_omega_state.py` | `OmegaInput`, `TrajectoryDiff`, `OmegaOutcomePack` |
| `omega_loop/_prompt.py` | Window-level Omega prompt |
| `deepsleep_loop/loop.py` | `run_deepsleep_for_window` |
| `deepsleep_loop/intake/run.py` | `build_deepsleep_input_for_window` (reads `window.episode_bodies`) |
| `deepsleep_loop/closure/run.py` | `finalize_window` (reseed only) |
| `core/open_questions.py` | Source of `open_query_state` enrichment |
