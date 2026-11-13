# Topic Resolution Phase 3a — Mastery-DELTA Narrative Map

> **Historical work-order snapshot.** It is retained for provenance, not as
> current architecture authority. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

**Work order:** `#64a3d081` (claude → devin) · **Date:** 2026-06-08  
**AS-BUILT:** 2026-06-10 — **DONE** (Option 2 window annotation). Tests: `tests/cognition/test_topic_resolution_3a.py`.  
**Goal:** Omega reflection narrates real growth using per-episode mastery deltas that now exist in-window, complementing P2's absolute `current_mastery_state`.

---

## (a) Where the per-episode mastery delta is available

### Production site
`deepsleep_loop/loop.py:85-92` inside `_run_deepsleep_episode_core`:

```python
mastery_effects: dict = {}
with phase_span("deepsleep", "mastery", episode_id=context_id):
    prior_mastery = load_mastery()
    mastery_records = mastery_phase.update_mastery(ds_input)
    mastery_effects = mastery_effects_from_records(
        mastery_records,
        prior=prior_mastery,
    )
```

### Delta shape
`deepsleep_loop/mastery/run.py:98-122` — `mastery_effects_from_records` returns:

```python
{
    "<domain_id>": {
        "domain_name": str,
        "knowledge_delta": float,      # Δ from prior episode
        "application_delta": float,    # Δ from prior episode
        "knowledge_mastery": float,    # absolute after update
        "application_mastery": float,  # absolute after update
        "status": str,                 # "learning" | "operational" | "mastered"
    }
}
```

### Carrier into the pack
`deepsleep_loop/consolidate/run.py:83` — `build_journal_entry` embeds it:

```python
mastery_effects=dict(mastery_effects or {}),
```

`deepsleep_loop/loop.py:145-157` — the returned `DeepSleepOutcomePack` carries it inside `journal_entry`:

```python
pack = DeepSleepOutcomePack(
    episode_id=context_id,
    journal_entry=journal_entry,        # ← dict containing "mastery_effects"
    ...
)
```

### Current disposal site (the discard seam)
`cognition_chain.py:95-125` — `_maybe_run_per_episode_deepsleep` calls `run_deepsleep_for_episode` but **discards the return value**:

```python
with phase_span("pressure", "deepsleep_episode_handoff", episode_id=episode_id):
    run_deepsleep_for_episode(store, episode_id, body)   # ← return value lost
```

**Conclusion:** the per-episode delta is alive inside `pack.journal_entry["mastery_effects"]` for roughly 50 ms before it is garbage-collected. The fix is to capture the pack.

---

## (b) The CLEANEST seam to get the delta INTO Omega's window input

### Timing constraint (architecturally critical)

```
store.close()
  ├─ enriched_episode_from_episode(ep)        → body built
  ├─ _append_episode_to_reflection_window()  → body appended + window SAVED
  └─ ...
_maybe_run_per_episode_deepsleep(store)
  └─ run_deepsleep_for_episode(...)
       └─ _run_deepsleep_episode_core
            └─ mastery_effects produced        → delta computed AFTER body in window
_maybe_run_window_reflection(store)
  └─ Omega runs, reads window
```

The delta is produced **after** the body is already in the window. Two options:

### Option 1 — retroactive body mutation
After per-episode DeepSleep returns, reach into the just-appended window body and inject `mastery_delta`.

- **Seam:** `EpisodeStore._reflection_window.episode_bodies[-1]` (in-memory list) after `store.close()`.
- **Problem:** the window was already persisted to disk in `_append_episode_to_reflection_window` (`pressure_loop/episode.py:681`). Mutating the in-memory body later requires a **second** `save_reflection_window` call. Also, the caller (`_maybe_run_per_episode_deepsleep`) would need intimate access to the store's internal window state (tight coupling).
- **Verdict:** works, but messy — double-save + coupling violation.

### Option 2 — window-level annotation (RECOMMENDED)
Add a new `episode_mastery_deltas` list to `ReflectionWindow`. Each per-episode DeepSleep appends its delta as a stand-alone annotation. Omega intake reads the list and includes it in `OmegaInput`.

- **Seam 1 — dataclass:** `ReflectionWindow` (`pressure_loop/reflection_window.py:44-52`) gains:
  ```python
  episode_mastery_deltas: list[dict[str, Any]] = field(default_factory=list)
  ```
  `to_dict`/`from_dict` updated to round-trip it.

- **Seam 2 — store API:** `EpisodeStore` gains:
  ```python
  def append_episode_mastery_delta(
      self, episode_id: str, delta: dict[str, Any]
  ) -> None:
      ...
  ```
  This appends `{"episode_id": episode_id, "mastery_effects": delta}` to `self._reflection_window.episode_mastery_deltas`, then calls `save_reflection_window`.

- **Seam 3 — caller capture:** `cognition_chain.py` `_maybe_run_per_episode_deepsleep` captures the pack:
  ```python
  pack = run_deepsleep_for_episode(store, episode_id, body)
  if pack and isinstance(pack, dict) or hasattr(pack, "journal_entry"):
      delta = pack.journal_entry.get("mastery_effects")
      if delta:
          store.append_episode_mastery_delta(episode_id, delta)
  ```

- **Why this is cleaner:**
  1. No retroactive mutation of episode bodies (the body is immutable episode truth).
  2. One additional save per episode — same cost as Option 1, but the save is explicit and owned by the store.
  3. `OmegaInput` can read the annotation list directly from the window object without store coupling.
  4. Episodes 1-9 annotate in their own ticks; episode 10 annotates in tick 10 **before** `_maybe_run_window_reflection` runs, so all 10 deltas are present when Omega consumes the window.

---

## (c) OmegaInput field + omega_loop/intake change

### EXISTS — P2 already wired
`omega_loop/_omega_state.py:30-38` (`OmegaInput` dataclass) already carries:

```python
@dataclass(frozen=True)
class OmegaInput:
    window_id: str
    trajectory_snapshot: dict
    episode_window: list
    trajectory_diff: str = "PARTIAL_FOLLOWING"
    current_mastery_state: dict = field(default_factory=dict)   # ← P2 absolute snapshot
    open_questions_summary: dict = field(default_factory=dict)   # ← P2
```

### NEW — add `mastery_changes`
Add a `mastery_changes` field to `OmegaInput`:

```python
# omega_loop/_omega_state.py:30-38
mastery_changes: list = field(default_factory=list)   # ← NEW for 3a
```

### NEW — intake reads window annotation
`omega_loop/intake/run.py:43-65` (`build_omega_input_for_window`):

```python
def build_omega_input_for_window(window: Any) -> OmegaInput:
    ...
    episode_window = [dict(b) for b in ...]
    mastery_changes = list(getattr(window, "episode_mastery_deltas", []) or [])
    return OmegaInput(
        ...
        current_mastery_state=_current_mastery_state(),
        open_questions_summary=_open_questions_summary(),
        mastery_changes=mastery_changes,          # ← NEW for 3a
    )
```

**Shape expected in `mastery_changes`:**
```python
[
    {
        "episode_id": "ep_...",
        "mastery_effects": {
            "<domain_id>": {
                "domain_name": "...",
                "knowledge_delta": 0.05,
                "application_delta": 0.0,
                ...
            }
        }
    },
    ...
]
```

---

## (d) omega_loop/_prompt.py addition

### EXISTS — P2 prompt text
`omega_loop/_prompt.py:59-113` (`OMEGA_REASONING_TASK`) already lists:

```
6. Mastery progression (use current_mastery_state and per-episode topic_history)
```

### NEW — explicit delta instruction
Replace task 6 with a sharper instruction that references the new `mastery_changes` field:

```text
6. Mastery progression:
   - Use current_mastery_state for the absolute snapshot.
   - Use per-episode topic_history to see what was pursued.
   - Use mastery_changes (per-episode deltas) to narrate GROWTH:
     e.g. "pursued attention 4× across the window; knowledge_mastery rose 0.21 → 0.43;
           application_mastery remained flat at 0.10; domain moved from learning to operational."
   - Call out stagnation as well as growth.
```

### NEW — formatter inclusion
`omega_loop/_prompt.py:167-175` (`build_omega_prompt`) must pass the new field:

```python
user = OMEGA_REASONING_TASK.format(
    trajectory_snapshot=_json_block(omega_input.trajectory_snapshot),
    trajectory_diff=omega_input.trajectory_diff,
    episode_window=_json_block(omega_input.episode_window),
    current_mastery_state=_json_block(omega_input.current_mastery_state),
    open_questions_summary=_json_block(omega_input.open_questions_summary),
    mastery_changes=_json_block(omega_input.mastery_changes),   # ← NEW for 3a
)
```

The `OMEGA_OUTPUT_SCHEMA` (`omega_loop/_prompt.py:120-141`) does **not** need to change — the model still emits the same `OmegaReflection` shape; the prompt simply gives it richer context to reason over.

---

## (e) Composition with P2 (current_mastery_state + topic_history)

| P2 field | What it gives Omega | 3a addition | How they compose |
|----------|---------------------|-------------|------------------|
| `current_mastery_state` | Absolute snapshot of every domain's `knowledge_mastery` / `application_mastery` / `status` at window reflection time. | None — stays as-is. | Omega sees "where the system stands now." |
| `topic_history` (per-episode body key) | What topic/domain/source each episode pursued. | None — stays as-is. | Omega sees "what was attempted." |
| `mastery_changes` (NEW) | Per-episode delta per domain (knowledge_delta, application_delta). | The new field. | Omega sees "how much each attempt actually moved the needle." |

**No collision:** `current_mastery_state` is the **absolute** end-state; `mastery_changes` is the **relative** delta series. The prompt tells the model to use both. `topic_history` tells it *which* domains were touched; `mastery_changes` tells it *by how much*.

---

## (f) Defer 3b + 3c

- **3b — Memory-tool `resolved_topic` integration:** **DONE** (2026-06-10). See `TOPIC_RESOLUTION_3B_MAP.md` AS-BUILT note; tests: `test_topic_resolution_3b.py`.
- **3c — Trajectory-preferential cascade ordering:** **DONE** (2026-06-10). Shipped as 3c.1 + 3c.2. See `TOPIC_RESOLUTION_3C_FEASIBILITY_MAP.md` AS-BUILT summary.

---

## EXISTS vs NEW summary table

| # | Touch-point | Status | File:Line | Change |
|---|-------------|--------|-----------|--------|
| 1 | `mastery_effects_from_records` delta builder | **EXISTS** | `deepsleep_loop/mastery/run.py:98-122` | None — delta already computed |
| 2 | `DeepSleepOutcomePack.journal_entry` carrier | **EXISTS** | `deepsleep_loop/loop.py:145-157` | None — delta already in pack |
| 3 | Capture pack in per-episode hook | **NEW** | `cognition_chain.py:107-109` | Assign `pack = run_deepsleep_for_episode(...)` |
| 4 | `ReflectionWindow.episode_mastery_deltas` | **NEW** | `pressure_loop/reflection_window.py:44-52` | Add dataclass field + round-trip in to_dict/from_dict |
| 5 | `EpisodeStore.append_episode_mastery_delta` | **NEW** | `pressure_loop/episode.py` | Append to window + save |
| 6 | `OmegaInput.mastery_changes` | **NEW** | `omega_loop/_omega_state.py:30-38` | Add dataclass field |
| 7 | `build_omega_input_for_window` reads deltas | **NEW** | `omega_loop/intake/run.py:43-65` | Pass `getattr(window, "episode_mastery_deltas", [])` |
| 8 | `OMEGA_REASONING_TASK` mentions deltas | **NEW** | `omega_loop/_prompt.py:59-113` | Sharpen task 6 to reference `mastery_changes` |
| 9 | `build_omega_prompt` passes deltas | **NEW** | `omega_loop/_prompt.py:167-175` | Add `mastery_changes=_json_block(...)` to `.format()` |

---

## Key timing nuance — resolved

The delta is produced **after** the body is appended to the window. The recommended Option 2 solves this by treating the delta as a **window-level annotation** rather than a body field. Episode 10's delta is appended to the window in the **same tick**, during `_maybe_run_per_episode_deepsleep`, which runs **before** `_maybe_run_window_reflection`. Therefore all 10 deltas are present when Omega consumes the locked window.

## AS-BUILT note (2026-06-10)

All nine touch-points in the EXISTS vs NEW table are integrated. Production wiring uses `_run_per_episode_terminus` + `_apply_per_episode_mastery_delta` in `cognition_chain.py` (the map's `_maybe_run_per_episode_deepsleep` name was refactored). Episode 10 uses `_run_tenth_episode_window_pipeline` (journal → stage → delta → Omega).

*Mapped by Devin. Built and verified.*
