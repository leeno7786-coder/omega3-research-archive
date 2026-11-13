# Topic Resolution Engine — Phase 2 Map (Devin)

**From:** claude → **Devin** (read-only map)  
**Spec source:** `docs/architecture/# WORK ORDER.txt` — Autonomous Knowledge Target Selection & Mastery-Guided Learning  
**Date:** 2026-06-08  
**Scope:** Phase 2 only — (1) Mastery update-on-acquisition uses resolved domain, (2) Reflection history fields (`topic_history`, `mastery_changes`, `open_questions_closed`, `open_questions_created`). Defer Phase 3 (memory-tool resolved_topic integration + trajectory-preferential resolution).

---

## AREA 1 — MASTERY UPDATE-ON-ACQUISITION

### (a) What `_episode_target(entry)` reads today

```python
# deepsleep_loop/mastery/run.py:35-44
def _episode_target(entry: Any, *, fallback: str = "") -> str:
    payload = _entry_dict(entry)
    outcome = payload.get("outcome")
    if isinstance(outcome, dict):
        goal = outcome.get("goal")
        if isinstance(goal, dict):
            raw = goal.get("target")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return fallback.strip()
```

**Field path read:** `entry["outcome"]["goal"]["target"]` → falls back to `action_goal.target` from `DeepSleepInput.action_goal` (derived from Omega trajectory / window reflection) when `outcome.goal.target` is absent.

The `outcome` dict comes from the episode’s global outcome (`global_.outcome`), built by `prior_episode_from_episode` at `pressure_loop/episode_journal.py:99-101`.

### (b) Does the enriched episode body carry `execution_inputs["domain"]`?

**Yes.** `enriched_episode_from_episode` (`pressure_loop/episode_journal.py:144-178`) builds:

```python
# episode_journal.py:153-156
if isinstance(meta, dict):
    pack = meta.get("execution_action_pack")
    if isinstance(pack, dict):
        execution["execution_action_pack"] = dict(pack)
```

The `ExecutionActionPack` dataclass (`meta_loop/pack/run.py:23-38`) includes `execution_inputs: dict`. When the Topic Resolution Engine fires, `meta_loop/tool/run.py:68-71` sets:

```python
state.execution_inputs = {
    "query": resolved.topic,
    "domain": resolved.domain,          # ← resolved domain
    "topic_source": resolved.source,
}
```

So the enriched body contains:
`body["execution"]["execution_action_pack"]["execution_inputs"]["domain"]` when the engine resolved a topic.

### (c) Minimal change to make mastery use the resolved domain

**RECOMMENDATION:** Modify `_episode_target` to prefer `execution_inputs["domain"]` before falling back to `outcome.goal.target`.

```python
# Proposed minimal change in deepsleep_loop/mastery/run.py:35-44
def _episode_target(entry: Any, *, fallback: str = "") -> str:
    payload = _entry_dict(entry)
    # NEW: prefer resolved domain from Topic Resolution Engine
    execution = payload.get("execution")
    if isinstance(execution, dict):
        pack = execution.get("execution_action_pack")
        if isinstance(pack, dict):
            inputs = pack.get("execution_inputs")
            if isinstance(inputs, dict):
                domain = str(inputs.get("domain") or "").strip()
                if domain:
                    return domain
    # EXISTS: fallback to outcome.goal.target (Global A extraction)
    outcome = payload.get("outcome")
    ...
```

**Why this is cleaner than writing into `outcome`:**
- `outcome` is owned by the global loop (`global_loop/outcome/run.py`) and consumed by `prior_episode_from_episode`. Changing its schema would require modifying `global_loop`, `prior_episode_from_episode`, and any downstream consumers of `PriorEpisode`.
- `execution_inputs["domain"]` is already the canonical carrier of the resolved topic. Reading it keeps the change localized to the DeepSleep mastery phase only.
- The fallback preserves backward compatibility for non-LEARN intents and pre-Phase-1 episodes.

**Confirming the increment path is reached:**

1. `cognition_chain.py:111-119` — window reflection runs Omega, then DeepSleep.
2. `deepsleep_loop/loop.py:68-74` — `mastery_phase.update_mastery(ds_input)` is called with the 10-episode window.
3. `update_mastery` (`deepsleep_loop/mastery/run.py:164-212`) iterates `ds_input.episode_window`.
4. For each successful entry, `_episode_target(entry, fallback=...)` returns the domain.
5. `_apply_mastery_for_episode` (`deepsleep_loop/mastery/run.py:116-161`) receives `domain_name=target`.
6. At line 140-141: if `intent == "LEARN"`, `knowledge = min(1.0, knowledge + deepsleep_mastery_knowledge_increment())`.
7. The record is written via `upsert_mastery()` to `deepsleep_loop/state/mastery.json`.

**EXISTS vs NEW:**
| Site | Status | Lines |
|------|--------|-------|
| `_episode_target` body | **NEW** (add resolved-domain read) | `deepsleep_loop/mastery/run.py:35-44` |
| `_apply_mastery_for_episode` increment logic | **EXISTS** | `deepsleep_loop/mastery/run.py:140-141` |
| `update_mastery` window iteration | **EXISTS** | `deepsleep_loop/mastery/run.py:195-210` |
| `execution_inputs["domain"]` carrier | **EXISTS** (set by Phase 1) | `meta_loop/tool/run.py:68-71` |

---

## AREA 2 — REFLECTION HISTORY FIELDS

### Temporal constraint (architecturally critical)

The current windowed flow (`cognition_chain.py:94-127`) is:

```
Window locks (10 enriched bodies)
  → Omega reflection (reads episode_window)
  → DeepSleep consolidation (mastery/identity/semantic update)
  → archive_and_reset_window
```

DeepSleep runs **after** Omega for the same window. Therefore:
- `mastery_changes` (produced by DeepSleep) are **not available to Omega for the current window**.
- They CAN be attached to the archived window / temporal lobe so the **next** window sees them.
- `topic_history` and `open_questions_closed/created` are episode-level data available at episode close time, so they CAN be in the current window’s bodies.

The map below respects this ordering.

---

### (a) `topic_history` — per-episode {topic, domain, source}

**Where produced:**
`meta_loop/tool/run.py:68-71` sets `execution_inputs = {"query": resolved.topic, "domain": resolved.domain, "topic_source": resolved.source}`.

**How it reaches the enriched body:**
The `execution_action_pack` (containing `execution_inputs`) is stored in `ep.meta` by `meta_loop/pack/run.py:97-116`. At episode close, `enriched_episode_from_episode` reads `meta.get("execution_action_pack")` and places it in `body["execution"]["execution_action_pack"]` (`pressure_loop/episode_journal.py:153-156`).

**Injection point — MINIMAL change:**
Add a `topic_history` field directly to the enriched body dict in `enriched_episode_from_episode` (`pressure_loop/episode_journal.py:168-184`). Extract from `execution["execution_action_pack"]["execution_inputs"]` when present:

```python
# Proposed addition in episode_journal.py:168-184
inputs = (
    execution.get("execution_action_pack", {}).get("execution_inputs")
    if isinstance(execution, dict) else None
)
topic_history = None
if isinstance(inputs, dict) and inputs.get("domain"):
    topic_history = {
        "topic": inputs.get("query"),
        "domain": inputs.get("domain"),
        "source": inputs.get("topic_source"),
    }

body = {
    ...
    "topic_history": topic_history,
    ...
}
```

**Where it reaches Omega:**
`omega_loop/intake/run.py:20-23` reads `window.episode_bodies` verbatim into `OmegaInput.episode_window`. The new `topic_history` key will be present in each body dict automatically.

**EXISTS vs NEW:**
| Site | Status | Lines |
|------|--------|-------|
| `execution_inputs` production | **EXISTS** (Phase 1) | `meta_loop/tool/run.py:68-71` |
| `execution_action_pack` in body | **EXISTS** | `pressure_loop/episode_journal.py:153-156` |
| `topic_history` field on body | **NEW** | `pressure_loop/episode_journal.py:168-184` |
| `episode_window` propagation | **EXISTS** | `omega_loop/intake/run.py:20-23` |

---

### (b) `mastery_changes` — knowledge/application deltas per domain

**Where produced:**
`deepsleep_loop/mastery/run.py:89-113` — `mastery_effects_from_records(records, prior=prior_mastery)` returns:

```python
{
  "<domain_id>": {
    "domain_name": str,
    "knowledge_delta": float,
    "application_delta": float,
    "knowledge_mastery": float,
    "application_mastery": float,
    "status": str,
  }
}
```

**Called at:** `deepsleep_loop/loop.py:70-74`:
```python
mastery_records = mastery_phase.update_mastery(ds_input)
mastery_effects = mastery_effects_from_records(mastery_records, prior=prior_mastery)
```

**Temporal reality:** Because DeepSleep runs **after** Omega for the same window, the current window’s `mastery_effects` cannot be injected into `OmegaInput.episode_window` for that window. Two valid approaches:

**Approach A — Include current mastery *state* in Omega intake (recommended for P2):**
Read `load_mastery()` in `omega_loop/intake/run.py` and attach `current_mastery_state` to `OmegaInput` (or embed per-episode `knowledge_mastery` values into the prompt). This gives Omega the absolute mastery picture, not the delta.

**Approach B — Attach mastery_changes to archived window for next-window history (recommended for P2+):**
After DeepSleep, `archive_and_reset_window` in `pressure_loop/reflection_window.py` archives the window to `memory/600_journal/reflection_windows/`. If `mastery_effects` are attached to each archived body (or as a window-level annotation), the next Omega window can see prior deltas. This requires modifying `archive_and_reset_window` to accept the DeepSleep outcome pack.

**Injection point for Approach A (minimal):**
Modify `build_omega_input_for_window` (`omega_loop/intake/run.py:10-30`) to read `load_mastery()` and add a `current_mastery_state` field to `OmegaInput`:

```python
# omega_loop/intake/run.py:10-30
from deepsleep_loop.mastery_store import load_mastery

def build_omega_input_for_window(window: Any) -> OmegaInput:
    ...
    current_mastery = {
        domain_id: {
            "domain_name": record.domain_name,
            "knowledge_mastery": float(record.knowledge_mastery),
            "application_mastery": float(record.application_mastery),
            "status": record.status,
        }
        for domain_id, record in (load_mastery() or {}).items()
    }
    return OmegaInput(
        ...
        current_mastery_state=current_mastery,  # NEW field on OmegaInput
    )
```

This requires adding `current_mastery_state: dict = field(default_factory=dict)` to the `OmegaInput` dataclass (`omega_loop/_omega_state.py:30-38`).

**EXISTS vs NEW:**
| Site | Status | Lines |
|------|--------|-------|
| `mastery_effects_from_records` delta builder | **EXISTS** | `deepsleep_loop/mastery/run.py:89-113` |
| `load_mastery` reader | **EXISTS** | `deepsleep_loop/mastery_store.py:47-71` |
| `OmegaInput` schema | **NEW** (add `current_mastery_state`) | `omega_loop/_omega_state.py:30-38` |
| `build_omega_input_for_window` | **NEW** (add mastery read) | `omega_loop/intake/run.py:10-30` |

---

### (c) `open_questions_closed` / `open_questions_created` — counts per window

**Where produced:**
- **Created:** `record_open_question(...)` at `pressure_loop/open_questions.py:207-261` appends an `OPEN` record to `guardian/state/omega_open_questions.jsonl`.
- **Closed:** `mark_resolved(...)` at `pressure_loop/open_questions.py:372-417` appends a `RESOLVED` record.

**Per-episode/window collection:**
Neither function is currently called with a mandatory `episode_id`. The `OpenQuestion` dataclass (`pressure_loop/open_questions.py:86-121`) has an optional `episode_id` field (line 114), but callers do not populate it consistently today.

**MINIMAL collection approach:**
1. **At episode close time** (`pressure_loop/episode_journal.py:144-178`), read the open-questions store and count:
   - `open_questions_created` = count of questions whose `asked_at` falls within this episode’s lifespan (or whose `asked_cycle` matches the episode). This is fragile because `asked_cycle` is a brain cycle_id, not an episode_id.
   - A better seam: emit a counter at the call sites.

2. **Better seam:** Modify `record_open_question` callers to pass `episode_id=episode.episode_id`. Then, at episode close, scan `_load_all()` for questions with `episode_id == current_episode_id` and status `OPEN` → created count; status `RESOLVED` with `resolved_at` in episode window → closed count.

3. **Window-level aggregation (recommended):** Since DeepSleep runs after Omega, count created/resolved per window by scanning the open-questions JSONL for records whose `episode_id` matches any of the 10 episodes in the window. Attach this as a window-level annotation to the archived window, or include it in the next window’s OmegaInput.

**Injection point — per-episode body (simplest):**
Add `open_questions_created` and `open_questions_closed` to `enriched_episode_from_episode` by reading the open-questions store and matching on a timestamp or episode_id. If `episode_id` is threaded to `record_open_question` callers:

```python
# In episode_journal.py:168-184 (proposed)
episode_open_created = 0
episode_open_closed = 0
if hasattr(ep, "episode_id"):
    # requires callers to pass episode_id to record_open_question
    for q in _load_all():  # or cached read
        if q.episode_id == ep.episode_id:
            if q.status == STATUS_OPEN:
                episode_open_created += 1
            elif q.status == STATUS_RESOLVED:
                episode_open_closed += 1
```

**Window-level alternative (if episode_id threading is deferred):**
Add `open_questions_summary` to `OmegaInput` in `build_omega_input_for_window`:

```python
# omega_loop/intake/run.py:10-30
from pressure_loop.open_questions import _load_all, _latest_per_id, STATUS_OPEN, STATUS_RESOLVED

latest = _latest_per_id(_load_all())
open_questions_created = sum(1 for q in latest.values() if q.status == STATUS_OPEN)
open_questions_closed = sum(1 for q in latest.values() if q.status == STATUS_RESOLVED)
```

This gives Omega the **global** open/resolved counts (not per-window), which still lets Omega say "system has N open questions, M resolved."

**EXISTS vs NEW:**
| Site | Status | Lines |
|------|--------|-------|
| `record_open_question` / `mark_resolved` | **EXISTS** | `pressure_loop/open_questions.py:207-261`, `372-417` |
| `episode_id` on OpenQuestion | **EXISTS** (field exists, unpopulated) | `pressure_loop/open_questions.py:114` |
| Per-episode open/closed counter | **NEW** | `pressure_loop/episode_journal.py:168-184` or `omega_loop/intake/run.py:10-30` |
| Threading `episode_id` to callers | **NEW** | Call sites of `record_open_question` / `mark_resolved` |

---

## PROMPT INJECTION — `omega_loop/_prompt.py`

The Omega prompt (`omega_loop/_prompt.py:59-105`) currently lists 10 analysis tasks. To make Omega reason over the new fields, add explicit references in `OMEGA_REASONING_TASK`:

```text
Episode Window (10 completed episodes, chronological):
{episode_window}

Mastery State (current system knowledge):
{current_mastery_state}

Open Questions Summary:
{open_questions_summary}
```

And add sub-items under the existing analysis list:

```text
6. Mastery progression (use current_mastery_state and per-episode topic_history)
7. Identity pressure
8. Emerging patterns
9. Strategic effectiveness
10. Future trajectory recommendations
11. Topic resolution effectiveness (how often did LEARN resolve a concrete topic vs abort?)
12. Open question lifecycle (created vs resolved ratio, stale questions)
```

**MINIMAL prompt change:**
No dataclass schema change is needed for `OmegaReflection` (it remains model-output-shaped). Only the prompt text (`OMEGA_REASONING_TASK`) needs to tell the model about the new context fields.

**EXISTS vs NEW:**
| Site | Status | Lines |
|------|--------|-------|
| `OMEGA_REASONING_TASK` template | **NEW** (add mastery/open-question placeholders) | `omega_loop/_prompt.py:59-105` |
| `build_omega_prompt` formatter | **NEW** (pass new fields) | `omega_loop/_prompt.py:159-166` |
| `OmegaReflection` dataclass | **EXISTS** (no schema change needed) | `omega_loop/_omega_state.py:50-75` |

---

## SUMMARY TABLE — EXISTS vs NEW per touch-point

| # | Touch-point | Status | File:Line | Minimal Change |
|---|-------------|--------|-----------|----------------|
| 1 | `_episode_target` reads resolved domain | **NEW** | `deepsleep_loop/mastery/run.py:35-44` | Add `execution_inputs["domain"]` as preferred read |
| 2 | `_apply_mastery_for_episode` increment path | **EXISTS** | `deepsleep_loop/mastery/run.py:140-141` | None — reached automatically when (1) is present |
| 3 | `topic_history` on enriched body | **NEW** | `pressure_loop/episode_journal.py:168-184` | Extract from `execution_inputs` |
| 4 | `current_mastery_state` on `OmegaInput` | **NEW** | `omega_loop/_omega_state.py:30-38` | Add dataclass field |
| 5 | `current_mastery_state` read in intake | **NEW** | `omega_loop/intake/run.py:10-30` | Call `load_mastery()` |
| 6 | `open_questions_created/closed` per episode | **NEW** | `pressure_loop/episode_journal.py:168-184` | Count from open-questions store |
| 7 | Thread `episode_id` to `record_open_question` | **NEW** | Call sites | Pass `episode_id=...` |
| 8 | Prompt mentions new fields | **NEW** | `omega_loop/_prompt.py:59-105` | Add placeholders + reasoning tasks |
| 9 | `build_omega_prompt` passes new data | **NEW** | `omega_loop/_prompt.py:159-166` | Include mastery/open-question JSON |

---

## DEFERRED TO PHASE 3

- Memory-tool `resolved_topic` integration — **DONE** (3b, 2026-06-10). See `TOPIC_RESOLUTION_3B_MAP.md`.
- Trajectory-preferential resolution — **DONE** (3c, 2026-06-10). See `TOPIC_RESOLUTION_3C_FEASIBILITY_MAP.md` AS-BUILT summary.
- Window-level `mastery_changes` delta attachment to archived windows for cross-window history — **DONE** (2026-06-10): archives already store `episode_mastery_deltas`; `build_omega_input_for_window` reads prior window via `load_latest_archived_reflection_window()` → `OmegaInput.prior_window_mastery_changes`.

---

*Mapped by Devin. Ready for Cursor TDD implementation per slice discipline.*
