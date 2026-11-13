# Pressure Loop Closure — As-Built Documentation

> **Historical snapshot — superseded 2026-08-03.** Its window-level Omega,
> reseed, and closure descriptions are retired. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

**Status:** BUILT + verified (475/0 core tests, including `test_omega_pressure_feedback_alters_next_tick_intent`).  
**Authority:** `docs/_archive/architecture/PRESSURE_LOOP_CLOSURE_PACKETS.md` + `docs/architecture/PRESSURE_LOOP_CLOSURE_MAP.md`  
**Branch:** `main`  
**Built by:** Cursor (TDD from packets) · Verified by Claude · Mapped by Devin

---

## Core Change (one sentence)

Omega's window reflection now emits `pressure_feedback` (`contributors_to_raise` / `lower` / `create`) that flows through DeepSleep reseed into the next pressure tick, altering scores and therefore the gated intent — the mind alters its own course.

---

## End-to-End Path: How Omega Steers the Next Cycle

```text
Omega window reflection (10-episode window)
    ↓
pressure_feedback JSON block in OmegaReasoning
    ↓
OmegaOutcomePack.reflection.pressure_feedback
    ↓
DeepSleep intake reads omega_outcome → reflection (deepsleep_loop/intake/run.py:32)
    ↓
generate_reseed parses raise / lower / create (deepsleep_loop/reseed/run.py:127-163)
    ↓
raise/create → Contributor(source="omega", type, weight) → stage_reseed (additive)
lower → stage_reseed_suppress(type, weight) → _RESEED_SUPPRESS overlay
    ↓
finalize_window → stage_reseed(reseed_contributors) (deepsleep_loop/closure/run.py:22)
    ↓
Next pressure tick → gather_contributors (pressure_loop/pressure.py:362-378)
    ↓
_RESEED_STAGING merged → _RESEED_SUPPRESS applied (weight = max(0.0, weight - delta))
    ↓
evaluate → hybrid gate (ceiling + margin) → dominant contributor → opened_by
    ↓
global_loop/intent/run.py:24-49 → opened_by → ALLOWED_INTENT
    ↓
ActionGoal → cognition cascade
```

**Key invariant:** The steering is **pressure-mediated**, not trajectory-direct. The dominant contributor that wins the hybrid gate decides the intent; Omega only changes which contributor wins by raising, lowering, or creating types.

---

## As-Built Code Map (file:line citations)

### 1. Omega pressure_feedback birth

- **`omega_loop/_prompt.py:120`** — JSON schema in the model prompt explicitly requires `"pressure_feedback": {"contributors_to_raise": [], "contributors_to_lower": [], "contributors_to_create": []}`.
- **`omega_loop/_omega_state.py:59`** — `OmegaReflection` dataclass carries `pressure_feedback: dict`.
- **`omega_loop/_omega_state.py:74`** — `from_dict` copies `dict(d.get("pressure_feedback") or {})`.
- **`omega_loop/_omega_state.py:209-230`** — `validate_omega_reflection()` checks `pressure_feedback` is a dict if present; **no whitelist gate** — unknown types are carried through.

**Matches MAP §3.1-3.3 exactly.**

### 2. DeepSleep intake (the handoff)

- **`deepsleep_loop/intake/run.py:32`** — `reflection = dict(outcome_dict.get("reflection") or {})`.
- **No explicit `pressure_feedback` extraction** — it remains inside `reflection` and is read directly by `generate_reseed` from `omega_outcome` → `reflection` (`deepsleep_loop/reseed/run.py:77`).

**Matches MAP §3.4** — `DeepSleepInput` still has no dedicated `pressure_feedback` field; the reseed phase reads from the nested dict directly.

### 3. generate_reseed — parsing pressure_feedback

**File:** `deepsleep_loop/reseed/run.py:127-163`

```python
feedback = dict(reflection.get("pressure_feedback") or {})
for key in ("contributors_to_raise", "contributors_to_create"):
    for entry in feedback.get(key) or []:
        ...
        contributors.append(
            make_contributor(
                "omega", ctype, weight,
                {"origin": "omega_pressure_feedback"},
            )
        )
for entry in feedback.get("contributors_to_lower") or []:
    ...
    from pressure_loop.pressure import stage_reseed_suppress
    stage_reseed_suppress(ctype, weight)
```

**Semantics:**
- `raise` / `create` → additive reseed contributors, source `"omega"`, weight clamped `[0,1]`, metadata `{"origin": "omega_pressure_feedback"}`.
- `lower` → calls `stage_reseed_suppress(type, weight)` (max-per-type accumulation, clamped `[0,1]`).
- Both skip entries with `weight <= 0.001`.

**Matches PACKET 1 + PACKET 2 exactly.** Divergence from MAP: none. The `contributors_to_create` branch shares the same code path as `contributors_to_raise`, so novel types are treated identically to existing types (no special handling needed beyond the intent fallback).

### 4. Suppression overlay in pressure.py

**File:** `pressure_loop/pressure.py:59,91-99,368-378`

```python
_RESEED_SUPPRESS: Dict[str, float] = {}

def stage_reseed_suppress(contributor_type: str, weight: float) -> None:
    ...
    _RESEED_SUPPRESS[ctype] = max(_RESEED_SUPPRESS.get(ctype, 0.0), w)
```

Applied in `gather_contributors` (`pressure.py:368-378`):

```python
if _RESEED_SUPPRESS:
    suppressed: List[Contributor] = []
    for c in out:
        delta = _RESEED_SUPPRESS.get(c.type, 0.0)
        w = max(0.0, c.weight - delta)
        if w > 0.001:
            suppressed.append(make_contributor(c.source, c.type, w, dict(c.metadata)))
    out = suppressed
    _RESEED_SUPPRESS.clear()
```

**Matches PACKET 2 exactly:** suppression is applied **after** `_RESEED_STAGING` and `_PENDING_QUEUE` merge, then cleared symmetrically with `_RESEED_STAGING`.

### 5. Intent fallback — novel type → IDLE

**File:** `global_loop/intent/run.py:24-49`

```python
def _map_opened_by_to_intent(opened_by: str, message: str) -> str:
    ...
    if ob in ("idle_probe_aggregate",):
        return "IDLE"
    return "IDLE"
```

**SELF_INQUIRY was removed** (operator directive 2026-07-09) and collapsed to `IDLE` — the idle-wake awakening owns real reflection now. What PACKET 3 once routed to `SELF_INQUIRY` (the novel/catch-all fallback) now returns `IDLE`. The explicit `idle_probe_aggregate → IDLE` branch is preserved, and `SELF_INQUIRY` is gone from `ALLOWED_INTENTS` (`pressure_loop/contributors.py:12-21`), which now ends at `IDLE`.

**Full opened_by → intent map (as-built):**

| opened_by | Intent | File:Line |
|-----------|--------|-----------|
| `operator_query` | `RESPOND` | `run.py:26-27` |
| `operator_response` | `RESOLVE_QUERY` | `run.py:28-29` |
| `outstanding_question` | `PROACTIVE_RESPONSE` | `run.py:30-31` |
| `learning_debt`, `curiosity_gap`, `epistemic_emptiness` | `LEARN` | `run.py:32-33` |
| `identity_gap`, `trajectory_gap` | `IDLE` | `run.py:34-35` |
| `operator_command` | `IMAGE_CREATE` / `USE_TOOL` / `LEARN` (heuristic) | `run.py:36-46` |
| `idle_probe_aggregate` | `IDLE` | `run.py:47-48` |
| **anything else** (novel types) | `IDLE` | `run.py:49` |

### 6. The cascade handoff

**File:** `cognition_chain.py:76-114`

```python
def _maybe_run_window_reflection(store) -> None:
    ...
    omega_outcome = run_omega_for_window(window)
    ...
    run_deepsleep_for_window(store, window, omega_outcome)
    ...
    store.archive_and_reset_window(window, omega_outcome=outcome_dict)
```

`run_deepsleep_for_window` calls the reseed phase, which calls `finalize_window`, which calls `stage_reseed`. The next pressure tick drains `_RESEED_STAGING` and `_RESEED_SUPPRESS` in `gather_contributors`.

**Matches PACKET 4 integration path exactly.**

---

## Match / Divergence from Pre-Build Map

| Seam | MAP Prediction | As-Built Reality | Divergence |
|------|---------------|------------------|------------|
| **1** Omega pressure_feedback → DeepSleep | Extend `DeepSleepInput` or read directly from `omega_outcome` | `generate_reseed` reads directly from `ds_input.omega_outcome` → `reflection` → `pressure_feedback` (`run.py:77,127`); no new `DeepSleepInput` field added | **None** — direct read worked, no struct change needed |
| **2** pressure_feedback → reseed contributors | Add parsing of raise/lower/create into Contributors | Implemented exactly as predicted: raise/create additive, lower via suppression overlay | **None** |
| **3** Reseed → next tick pressure | No structural change needed; merge into same reseed list | `_RESEED_STAGING` + `_RESEED_SUPPRESS` work as predicted; `finalize_window` unchanged | **None** |
| **4** Novel contributor type → intent | Change fallback `IDLE` → `SELF_INQUIRY` | `SELF_INQUIRY` since removed (operator directive 2026-07-09); the fallback is back to `IDLE` (`global_loop/intent/run.py:49`) | **Reverted** |
| **5** Trajectory on every ignition | `LAST_OMEGA_TRAJECTORY` dormant; optional wire of `current_trajectory.json` into `evaluate()` | **HELD IN RESERVE** — `evaluate()` still reads `LAST_OMEGA_TRAJECTORY` (`pressure.py:432-437`) which is `None` on the rebuild path; per-operator directive, pressure-mediated steering is tested first before adding the direct-factor lever | **Held** — no divergence, intentionally deferred |

---

## Test Proof

**File:** `tests/core/test_pressure_loop_closure.py` (6 tests)

| Test | What it proves | Lines |
|------|---------------|-------|
| `test_generate_reseed_includes_pressure_feedback_raise_and_create` | `raise` + `create` emit `source="omega"` contributors with correct weight | `55-69` |
| `test_stage_reseed_suppress_reduces_gathered_weight` | `_RESEED_SUPPRESS` overlay reduces a gathered contributor's weight | `75-89` |
| `test_generate_reseed_lowers_via_suppression_overlay` | `lower` in pressure_feedback → suppression → reduced weight on next gather | `92-110` |
| `test_novel_opened_by_maps_to_idle` | Novel `opened_by` (e.g. `system_diagnostic`) returns `IDLE` | `117-118` |
| `test_idle_probe_aggregate_stays_idle` | Explicit `idle_probe_aggregate` still returns `IDLE` | `120-121` |
| `test_omega_pressure_feedback_alters_next_tick_intent` | **End-to-end course alteration:** raise `learning_debt@1.0` + lower `idle_probe_aggregate@0.5` → next tick `opened_by="learning_debt"` → intent `LEARN` (not `IDLE`) | `127-158` |

**Full core suite at build time:** 475 passed / 0 failed.

---

## Steering Semantics

### Raise
Increases the weight of an existing contributor type. If that type already has a field reader (e.g. `learning_debt`, `curiosity_gap`), the additive reseed stacks on top. If the type is field-only, the reseed may be the sole source.

### Lower
Reduces the weight of a contributor type via suppression overlay. The result is `max(0.0, weight - suppress)`. This is the **only** way the pressure system can subtract weight — additive reseed (`stage_reseed`) filters out weights ≤ 0.001 and cannot represent negative deltas.

### Create
Introduces a **novel** contributor type. Because there is no field reader for an unknown type, the reseed contributor is the sole source. When it wins the hybrid gate, `opened_by` is the novel type string, which falls through to `IDLE` — SELF_INQUIRY (the former catch-all reflect intent) was removed 2026-07-09 and collapsed to `IDLE`; the idle-wake awakening owns reflection now.

### Clamp discipline
All weights are clamped to `[0,1]`:
- `max(0.0, min(1.0, weight))` in `generate_reseed` (`run.py:136,156`).
- `_clamp_unit` in `pressure.py:131-132` for `stage_reseed_suppress`.
- `max(0.0, weight - delta)` in suppression apply (`pressure.py:372`).

This keeps the hybrid gate (`ceiling` + `margin`) stable.

---

## Held in Reserve — Seam 5 (Trajectory Direct-Factor)

**Status:** NOT BUILT.

The pre-build map identified that `LAST_OMEGA_TRAJECTORY` is dormant (`pressure.py:432-437` reads `None` on the rebuild path). A potential future lever is to replace that dormant SharedState read with a direct read of `guardian/state/current_trajectory.json` (the same path `reflection_window.py` uses for window snapshots), so the standing trajectory rides every episode ignition as a direct factor.

**Operator directive:** Pressure-mediated steering (this build) is tested first. If it proves too blunt, Seam 5 is the next lever.

---

## Related Files

| File | Role |
|------|------|
| `deepsleep_loop/reseed/run.py` | `generate_reseed` — parses Omega `pressure_feedback` |
| `pressure_loop/pressure.py` | `_RESEED_SUPPRESS`, `stage_reseed_suppress`, `gather_contributors` suppression apply |
| `global_loop/intent/run.py` | `_map_opened_by_to_intent` — novel-type fallback `IDLE` |
| `cognition_chain.py` | `_maybe_run_window_reflection` — Omega → DeepSleep synchronous handoff |
| `omega_loop/_prompt.py` | Prompt schema requiring `pressure_feedback` |
| `omega_loop/_omega_state.py` | `OmegaReflection.pressure_feedback` field + validation |
| `tests/core/test_pressure_loop_closure.py` | 6-test suite proving raise, lower, create, suppression, novel intent, end-to-end course alteration |

---

*As-built map. No code modified.*
