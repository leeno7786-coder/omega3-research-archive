# NEUROMOD_IGNITION_FIX_MAP.md

**Scope:** READ-ONLY blast-radius map for the operator-directed neuromod-ignition fix.  
**Date:** 2026-06-08  
**Fix summary:** neuromodulation is an INHIBITOR + a signal delegated through ACTIONS and OUTCOMES, NOT a first-class pressure contributor aggregated from curiosity. Three behaviors:
1. `neuromod_spike` STOPS competing as a pressure contributor.
2. The neuromod state used for ignition modulation DECAYS toward baseline on IDLE ticks.
3. It INFLATES / refreshes from the OUTCOME on episode close.

> **Superseded authority contract (2026-07-28):** this file records the earlier
> inhibitor design. The live contract now treats tonic chemistry as advisory
> learning/memory/mood/deliberation context. It does not modulate pressure
> weights, ceiling, margin, winner, or ignition. See `pressure_loop/README.md`.

---

## A) COMPUTE SOURCE — inflate-on-outcome source

Where dopamine / serotonin / stress for an episode are computed and written to `episode.auto['neuromod']` / `last_closed_body`.

| Step | File | Lines | What |
|------|------|-------|------|
| Producer | `cognition_chain.py` | 220–231 | `_start_auto` runs **after** `run_auto_front(episode)` returns. Computes `baseline = store.recent_success_rate()`, calls `compute_episode_neuromod(auto, baseline)` (`autonomic/symphony_neuromod.py:256-324`), writes `episode.auto["neuromod"] = nm`, then `set_current_memory_salience(nm)`. |
| Formula | `autonomic/symphony_neuromod.py` | 256–324 | Pure RPE: `dopamine = rpe`, `plasticity = abs(rpe)`, `stress = 0.6*max(0,-v) + 0.4*max(0,-rpe) + 0.3*overrun`, `serotonin = baseline - 0.5*stress`, `strength = abs(rpe)`. |
| Cross-tick carrier | `pressure_loop/episode.py` | 428–436 | `close()` calls `enriched_episode_from_episode(ep)` → writes `self._last_closed_body = body` (line 431). The body carries `auto.neuromod` because `_start_auto` wrote it there. |
| Carrier read | `pressure_loop/episode.py` | 503–507 | `last_closed_body()` returns `self._last_closed_body` (dict or None). |
| Spike scalar | `autonomic/symphony_neuromod.py` | 329–343 | `compute_closed_body_spike(neuromod)` → `clamp(0.6*stress + 0.5*plasticity)` in [0,1]. |

**Key citation:** `cognition_chain.py:228-230`
```python
nm = compute_episode_neuromod(auto if isinstance(auto, dict) else {}, baseline)
if isinstance(auto, dict):
    episode.auto["neuromod"] = nm
```

---

## B) SPIKE-AS-CONTRIBUTOR — removal surface

### B.1 Reader
`pressure_loop/pressure_field.py:575-622` — `read_neuromod_spike(shared_state, *, episode_store=None)`
- Precedence: active episode `episode.auto["neuromod"]` → `episode_store.last_closed_body()["neuromod"]`.
- Both paths score via `compute_closed_body_spike`.
- Returns scalar in [0,1]; never raises.

### B.2 Registry registration
`pressure_loop/pressure_field.py:1486`
```python
CONTRIBUTOR_NEUROMOD_SPIKE: read_neuromod_spike,
```
Inside `_DEFAULT_READERS` (`:1482-1491`), consumed by `build_registry()` (`:1494-1508`).

### B.3 Gather site
`pressure_loop/pressure.py:385-511` — `gather_contributors`
- Line 397-398: skips field IDs in `_REMOVED_FIELD_IDS`.
- **Current** `_REMOVED_FIELD_IDS` (`pressure.py:42-44`):
  ```python
  _REMOVED_FIELD_IDS = frozenset(
      {"guardian", "tool_creation_need", "evidence_terminal"}
  )
  ```
  **neuromod_spike is NOT present** — it is still actively gathered.
- Line 402-403: `read_neuromod_spike` is the **only** reader that receives `episode_store` as an argument:
  ```python
  if field_id == pf.CONTRIBUTOR_NEUROMOD_SPIKE:
      weight = reader(shared_state, episode_store=episode_store)
  ```
- Line 414: maps to canonical contributor type:
  ```python
  ctype = _FIELD_ID_TO_TYPE.get(field_id, field_id)
  ```
  There is **no explicit mapping** for `neuromod_spike` in `_FIELD_ID_TO_TYPE` (`pressure.py:47-55`), so it falls through to the raw field_id string `"neuromod_spike"`.

### B.4 Clean-removal options
| Option | Where | Effect |
|--------|-------|--------|
| **Add to `_REMOVED_FIELD_IDS`** | `pressure.py:42` | One-line deletion; `gather_contributors` skip loop drops it. `build_registry` still registers it, but gather never invokes it. |
| **Drop from `_DEFAULT_READERS`** | `pressure_field.py:1486` | Removes it from the registry entirely. Cleanest, but any test or legacy harness calling `build_registry` with an expectation of the key would break. |

**Recommended:** add `"neuromod_spike"` to `_REMOVED_FIELD_IDS` in `pressure.py` — consistent with how guardian/tool_creation_need/evidence_terminal were removed, and keeps the registry shape stable for tests that inspect `ALL_CONTRIBUTORS`.

### B.5 Tests that reference the contributor
| Test file | Lines | What |
|-----------|-------|------|
| `tests/core/test_pressure_neuromod_contributor.py` | 1,14,28,34,49,60,63,69,72 | Direct tests of `read_neuromod_spike` + `gather_contributors` surfacing `neuromod_spike`. These would need deletion or pivot to tonic-state tests. |
| `tests/core/test_neuromod_no_legacy_slot.py` | 16,20,31,37,40,46,53 | Negative-proof tests (no legacy SS keys, no legacy strings in source). Mostly unaffected except line 31 (`test_last_neuromod_spike_not_in_shared_state_keys`) which tests naming, not behavior. |

---

## C) BLAST RADIUS — ignition selector (`symphony_ignition.py`)

### C.1 Direct references
| File | Lines | What |
|------|-------|------|
| `symphony_ignition.py` | 53 | Precedence rule 5 comment: `"neuromod_spike OR curiosity_gap > 0 → GLOBAL_PRELUDE"` |
| `symphony_ignition.py` | 144 | `_CONTRIB_NEUROMOD_SPIKE = "neuromod_spike"` |
| `symphony_ignition.py` | 329-340 | **Live read:** `neuromod_w = contributors.get(_CONTRIB_NEUROMOD_SPIKE, 0.0)`; if `> 0` or `curiosity_w > 0` → `GLOBAL_PRELUDE`. If `curiosity_w >= neuromod_w`, dominant = `curiosity_gap`; else dominant = `neuromod_spike`, reason = `"neuromod_spike"`. |

### C.2 Impact of removing the contributor
- If `neuromod_spike` is removed from `gather_contributors`, the `contributors` dict passed to `decide_ignition` will **never contain** `_CONTRIB_NEUROMOD_SPIKE`.
- `neuromod_w` will always be `0.0` at `symphony_ignition.py:329`.
- Precedence rule 5 collapses to `curiosity_gap > 0` only.
- The `"neuromod_spike"` reason string and neuromod-dominant path at `:337-340` become **dead code** (never reached).

### C.3 Are there OTHER consumers of the spike weight?
- **Encoding / salience:** `set_current_memory_salience(nm)` (`cognition_chain.py:231`) and `current_memory_salience()` (`autonomic/symphony_neuromod.py:140`) are **independent** of the pressure contributor path. They use the raw `nm` dict, not the spike scalar. **Safe** — salience latch is untouched by removing the contributor.
- **Terminal emits:** `emit_neuromod_terminal` and `_emit_neuromod_compute` in `symphony_neuromod.py` are observability-only. **Safe**.
- **Legacy docs / schema:** `schema/pressure.schema.yaml`, `schema/pressure_field.schema.yaml`, `docs/architecture/*`, `config/pressure_contributors.yaml` reference the string but do not execute code. Must be cleaned in a doc pass, but do not affect runtime.
- **Extension code:** `.github/extensions/omega3-review-automation/extension.mjs` references the string in comments/docs. Non-runtime.

**FLAG:** The only runtime risk is the ignition selector losing the `neuromod_spike → GLOBAL_PRELUDE` route. Per the operator fix, this is **intentional** — neuromod should not drive routing, only modulate it. The `GLOBAL_PRELUDE` path remains reachable via `curiosity_gap` and fallback.

---

## D) MODULATION — make it DECAY (static → decaying tonic)

### D.1 Current static read
`pressure_loop/pressure.py:135-183` — `_neuromod_from_last_body(episode_store)`
- Reads `episode_store.last_closed_body()` → extracts `body["neuromod"]` → returns `{"dopamine", "serotonin", "stress", "spike"}`.
- Called from `evaluate()` at `pressure.py:532`:
  ```python
  nm = neuromod if neuromod is not None else _neuromod_from_last_body(episode_store)
  ```
- **Problem:** on every idle tick, this re-reads the **same** `last_closed_body` — the neuromod state never changes between episodes, so the modulation is static. If the last episode was high-stress, every subsequent idle tick sees high-stress inhibition until a new episode closes and replaces the body.

### D.2 Existing accumulator pattern to mirror
`pressure_loop/pressure_field.py:374-383` — `_PRESSURE_ACCUMULATOR` + `reset_pressure_accumulator()`
- Module-level dict: `{"accumulated": 0.0}`.
- `reset_pressure_accumulator()` zeros it; called from `EpisodeStore.close()` (`episode.py:386-387`).
- **No decay** — the operator doctrine 2026-05-30 explicitly rejected per-tick decay for pressure (the "pinned-at-0.15-never-ignites bug").

### D.3 Proposed tonic-neuromod accumulator (map only)
A decaying tonic state would live in `pressure_field.py`, mirrored after the pressure accumulator:

```text
_NEUROMOD_TONIC: Dict[str, float] = {"dopamine": 0.0, "serotonin": 0.0, "stress": 0.0}
_NEUROMOD_TONIC_LOCK = threading.Lock()
```

Functions needed (and where they would live):

| Function | Proposed home | Called by | Purpose |
|----------|---------------|-----------|---------|
| `read_neuromod_tonic()` | `pressure_field.py` | `pressure.evaluate()` (replacing `_neuromod_from_last_body`) | Return the current decayed tonic dict (dopamine/serotonin/stress). |
| `decay_neuromod_tonic()` | `pressure_field.py` | `loop.py` idle tick path | Apply `decay_rate` per-tick toward zero/baseline. |
| `refresh_neuromod_tonic(neuromod: dict)` | `pressure_field.py` | `episode.py` close() | Replace tonic with the newly-closed episode's `body["neuromod"]` (or blend). |
| `reset_neuromod_tonic()` | `pressure_field.py` | Test harnesses only | Zero the tonic state. |

**Citations for hook placement:**
- `pressure.py:532` — today: `nm = neuromod if neuromod is not None else _neuromod_from_last_body(episode_store)`
- `pressure.py:135` — `_neuromod_from_last_body` definition

---

## E) HOOKS — idle decay + close refresh sites

### E.1 IDLE-tick decay site
`pressure_loop/loop.py:81-111` — `run_pressure_tick_loop`
- Lines 103-111: the actual tick body:
  ```python
  tick += 1
  ss.write("_pressure_tick", tick)
  ss.write("_epoch", tick)
  with phase_span("pressure", "tick", ...):
      _run_pressure_tick_body(runtime)
  time.sleep(interval)
  ```
- **Hook:** after `evaluate()` returns `None` (no ignition), the tick is idle. A `decay_neuromod_tonic()` call would belong **inside** `_run_pressure_tick_body`, after the `evaluate` block, conditioned on `ignition is None`.
- Exact insertion point: `pressure_loop/loop.py:66-71` (after the `if ignition is not None:` block, before function exit).

### E.2 CLOSE refresh site
`pressure_loop/episode.py:353-474` — `EpisodeStore.close()`
- Lines 428-436: `enriched_episode_from_episode(ep)` builds the body; `self._last_closed_body = body`.
- Lines 385-387: `reset_pressure_accumulator()` is called here (the pattern to mirror).
- **Hook:** a `refresh_neuromod_tonic(body["neuromod"])` call would belong **immediately after** `self._last_closed_body = body` (line 431), inside the same `try/except` block or in a new one following it.

---

## F) CONFIG — where params live + where to add decay_rate

### F.1 Existing neuromod config surfaces
| Loader | File | Lines | YAML key | What |
|--------|------|-------|----------|------|
| `pressure_neuromod_weight_modulation()` | `pressure_loop/policy.py` | 430-448 | `pressure.neuromod.weight_modulation` | Dopamine gamma: enabled, dopamine_coeff, gamma_min, gamma_max |
| `pressure_neuromod_hybrid_patience()` | `pressure_loop/policy.py` | 400-427 | `pressure.neuromod.hybrid_patience` + `threshold_modulation` | Serotonin damp: enabled, serotonin_weight_damp, serotonin_margin_coeff, serotonin_ceiling_coeff |
| Raw YAML | `config/pressure.yaml` | 44-60 | `pressure.neuromod.*` | Canonical defaults for the above |

### F.2 Where to add `tonic_decay_rate`
A new `tonic_decay` block would live **sibling** to `weight_modulation` and `hybrid_patience` under `pressure.neuromod`:

```yaml
# config/pressure.yaml (proposed insertion)
pressure:
  neuromod:
    # ... existing weight_modulation + hybrid_patience ...
    tonic_decay:
      enabled: true
      decay_rate: 0.05        # per-tick fraction toward baseline
      baseline: 0.0           # decay target for dopamine/stress (serotonin may differ)
      refresh_blend: 1.0      # 1.0 = replace tonic outright on close; <1.0 = partial blend
```

Corresponding loader in `pressure_loop/policy.py` would be added around line 449 (after `pressure_neuromod_weight_modulation`), following the same `_pressure_section()` → `neuromod.get(...)` pattern.

---

## G) RISK FLAGS

| Risk | Detail | Mitigation |
|------|--------|------------|
| **Salience / encoding drift** | The neuromod weave drives memory salience (`set_current_memory_salience`). If tonic decay makes the ambient neuromod lower than the true last-episode spike, memory writes during idle ticks encode at lower scale. **Current interpretation:** chemistry is a fading salience/learning amplifier, not pressure authority. | Verify `current_memory_salience()` consumers (`memory/strength_index.py:157-187`) behave correctly with lower tonic values. |
| **Ignition starvation** | Removing `neuromod_spike` as a contributor means high-stress episodes no longer directly trigger a follow-up episode via pressure. Only `curiosity_gap`, `learning_debt`, operator input, etc. can open episodes. **This is intentional** per the operator fix — the spike was causing deadlock by always re-firing. | Monitor that curiosity_gap + learning_debt pressure is sufficient to keep the loop alive. |
| **Test breakage** | `tests/core/test_pressure_neuromod_contributor.py` is entirely about the spike-as-contributor path. It will need deletion or rewrite into tonic-state tests. | Delete or rewrite in the same slice that lands the fix. |
| **Schema / docs debt** | `schema/pressure.schema.yaml`, `schema/pressure_field.schema.yaml`, `config/pressure_contributors.yaml`, and many `.md` files name `neuromod_spike` as a contributor. | Doc pass tracked separately; no runtime impact. |

---

## H) SUMMARY — files to touch (when the fix is implemented)

| File | Action | Why |
|------|--------|-----|
| `pressure_loop/pressure.py` | Add `"neuromod_spike"` to `_REMOVED_FIELD_IDS`; swap `_neuromod_from_last_body` → `read_neuromod_tonic` in `evaluate()` | Stop spike competing; use decaying tonic |
| `pressure_loop/pressure_field.py` | Add `_NEUROMOD_TONIC`, `read_neuromod_tonic()`, `decay_neuromod_tonic()`, `refresh_neuromod_tonic()` | Decaying state machine |
| `pressure_loop/loop.py` | Call `decay_neuromod_tonic()` on idle ticks | Decay toward baseline when no episode opens |
| `pressure_loop/episode.py` | Call `refresh_neuromod_tonic(body["neuromod"])` in `close()` | Inflate/refresh from outcome |
| `pressure_loop/policy.py` | Add `pressure_neuromod_tonic_decay()` loader | Config surface |
| `config/pressure.yaml` | Add `pressure.neuromod.tonic_decay` block | Operator-tunable decay rate |
| `symphony_ignition.py` | Remove `_CONTRIB_NEUROMOD_SPIKE` references (lines 53 comment, 144 constant, 329-340 branch) | Dead code after contributor removal |
| `tests/core/test_pressure_neuromod_contributor.py` | Delete or rewrite for tonic-state behavior | Tests must match new behavior |

---

*End of map. No code modified.*
