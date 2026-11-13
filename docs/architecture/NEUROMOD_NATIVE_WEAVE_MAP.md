# Native Neuromodulation Weave — As-Built Map

> **Status:** LANDED + live-proven (commits `bf5d6ed0`..`b8e25c3b` on `main`).
> **Authority:** `docs/_archive/architecture/SLICE_NEUROMOD_NATIVE_WEAVE.md` (build spec).
> **Supersedes:** the `persist_neuromod_spike_projection` / `LAST_NEUROMOD_SPIKE` bolt-on path.
> **Mapper:** Devin · **Date:** 2026-06-07

---

## 0. The biology (decides the formula)

| Finding | Source | Consequence for the weave |
|---|---|---|
| **Dopamine encodes reward PREDICTION ERROR, not raw reward.** Positive RPE strengthens episodic encoding; DA enhances hippocampal LTP / replay / consolidation. | Schultz; Nature Hum. Behav. 2019; PNAS 2011 | `dopamine = signed RPE` vs a learned baseline. Routine success ≈ 0; surprising success = big burst. DA drives encode-**scale**. |
| **PE MAGNITUDE drives plasticity regardless of valence** (locus coeruleus broadcasts *unsigned* PE). | LC/PE 2023 | `plasticity = \|RPE\|`. Big wins AND big failures encode strongly (you learn from surprises of either sign). |
| **Acute stress (NE+cortisol) enhances consolidation of salient events + drives arousal/re-engagement; inverted-U.** | CSH Learn&Mem 2019; Yerkes-Dodson | `stress` rises on failure/disappointment/overrun → (a) shortens ambient decay-tau, (b) drives **next-cycle pressure** (arousal → re-engage). Clamp [0,1] = the saturating high end of the inverted-U. |
| **Serotonin → patience/persistence + slower forgetting.** | Nature Comms 2018 | `serotonin` from calm/sustained-success → longer decay-tau (durable memory in a stable rewarding regime). |

**Net behavior:** the mind over-encodes *surprises* (good or bad), treats the *routine* as mundane (baseline encoding), **habituates** to chronic failure (RPE shrinks as the baseline drops) while chronic **stress still drives pressure** to re-engage. That is the adaptive, biological response — not a flat success→good/fail→bad stub.

---

## 1. Architecture — ONE compute site (cleaner than "born in the fold")

RPE needs history (the baseline). The single place that has BOTH the fresh outcome (`episode.auto`) AND the episode store (for the baseline) AND owns the salience latch is **`cognition_chain._start_auto(store, episode)`**.
So neuromod is computed there, written onto the carrier (`episode.auto["neuromod"]`), and latched — one site, every cycle, all paths. **The fold is NOT modified** (it already emits `cycle_kind`/`status`/`runtime_ms`).

```text
pressure tick → ignite_episode → … → _start_auto(store, episode):
    run_auto_front(episode)                          # AUTO produces the outcome on episode.auto (unchanged)
    baseline = store.recent_success_rate(n=10)       # RPE expectation (single source of truth)
    nm = compute_episode_neuromod(episode.auto, baseline)   # signed RPE / |RPE| / stress / serotonin
    episode.auto["neuromod"] = nm                    # write to the CARRIER
    set_current_memory_salience(nm)                  # latch EVERY cycle (nm may be neutral → baseline)
    → _start_meta_b_global_b(...)
…
close(ep) → self._last_closed_body = enriched_episode_from_episode(ep)   # carries neuromod, window-independent
```

`episode` is the carrier; no central shared-state bus; `cognition_chain`'s protected `consume_window_ready`/`archive_and_reset_window` interface is untouched.

---

## 2. The formula (`autonomic/symphony_neuromod.py`, pure never-raise fns)

### `compute_episode_neuromod(auto: dict, baseline_success_rate: float) -> dict`
`autonomic/symphony_neuromod.py:216-277`

```python
v        = +1.0 if cycle_kind=="success" else -1.0 if cycle_kind=="fail" else 0.0   # skipped/absent → 0
expected = 2*clamp01(baseline_success_rate) - 1                 # rate 0→-1, .5→0, 1→+1
rpe      = clamp(v - expected, -2, 2) / 2.0                   # signed, ∈ [-1, 1]
overrun  = clamp01((runtime_ms - EXPECTED_MS)/EXPECTED_MS) if runtime_ms else 0.0

dopamine   = rpe                                               # signed RPE  (encode-scale)
plasticity = abs(rpe)                                          # |RPE| unsigned surprise (encode-scale)
stress     = clamp01(0.6*max(0,-v) + 0.4*max(0,-rpe) + 0.3*overrun)   # failure+disappointment+load
serotonin  = clamp01(baseline_clamped - 0.5*stress)            # calm/sustained success → patience
strength   = abs(rpe)                                          # audit-only (emit_neuromod_terminal)
return {dopamine, plasticity, stress, serotonin, strength, cycle_kind}
```

Never raises; value-level garbage (None/NaN) → zeros. It consumes **already-validated** outcome fields (downstream of the episode-validity gate at `auto_loop/loop.py:128-137`) → it never re-validates / never crashes (fail-fast: Reality, record+continue).

Worked examples (pin in N1):
- always-fail agent (baseline 0) succeeds → expected −1, v +1 → rpe +1 → DA +1, plast 1 (huge encode).
- routine-success agent (baseline 1) succeeds → expected +1, v +1 → rpe 0 → DA 0, plast 0 (mundane).
- routine-success agent fails → rpe −1 → DA −1, plast 1, stress 0.6 (vivid failure + drive).

### `compute_closed_body_spike(neuromod: dict) -> float`
`autonomic/symphony_neuromod.py:280-289`

```python
spike = clamp01(0.6*stress + 0.5*plasticity)     # arousal + surprise → re-engage; calm-routine → ~0
```

A failure (high stress + high plasticity) → high spike; a surprising win → moderate spike; a calm routine success → ~0. Used for BOTH the active-episode and cross-tick body-state reads (one scalar, no success-gate anywhere). `compute_neuromod_spike_scalar` (the old success-gated one) is **deleted**.

### `compute_memory_salience(neuromod)` — unchanged (keep, pure, live)
`autonomic/symphony_neuromod.py:76-88`

`scale = 1 + ENCODE_DOPAMINE_GAIN*max(0,dopamine) + ENCODE_PLASTICITY_GAIN*plasticity` (negative DA carried by |RPE| plasticity → failures still encode); `tau_factor` shortened by stress, lengthened by serotonin. We feed it correct values; we do not rewrite it.

Constants live in `autonomic/symphony_neuromod.py:42-47`:
- `ENCODE_DOPAMINE_GAIN = 0.6`
- `ENCODE_PLASTICITY_GAIN = 0.4`
- `STRESS_TAU_PULL = 0.5`
- `SEROTONIN_TAU_PUSH = 0.5`
- `TAU_FACTOR_MIN = 0.5`
- `TAU_FACTOR_MAX = 1.5`

---

## 3. The weave — producer → latch → consumer

### A. SALIENCE (memory encoding modulation)

| Step | File | Lines | What |
|---|---|---|---|
| **Producer** | `cognition_chain.py` | 170-181 | After `run_auto_front(episode)`, computes `baseline = store.recent_success_rate()`, calls `compute_episode_neuromod(auto, baseline)`, writes `episode.auto["neuromod"] = nm`, then `set_current_memory_salience(nm)`. Runs **unconditionally every cycle**. |
| **Reflection reset** | `cognition_chain.py` | 95-98 | `_maybe_run_window_reflection` entry calls `set_current_memory_salience(None)` so Omega/DeepSleep reflective writes encode at baseline, not under a stale reactive spike. |
| **Latch** | `autonomic/symphony_neuromod.py` | 91-102 | `set_current_memory_salience` → `_CURRENT_MEMORY_SALIENCE = compute_memory_salience(neuromod)`. `current_memory_salience()` returns the latched dict or None before first latch. |
| **Consumer** | `memory/strength_index.py` | 157-187 | `StrengthIndex.register(mid, write_strength)` samples `current_memory_salience()` if no explicit salience passed. `scale` multiplies `write_strength`; `tau_factor` scales `DECAY_TAU_SECONDS` into per-memory `encode_tau`. |

**Lagged model:** mid-AUTO recall-gate writes in cycle N encode under cycle N-1's latch (one-cycle lag — the dominant live `register()` volume). State the lagged model; do not over-claim same-cycle encoding.

### B. PRESSURE (cross-tick body-state contributor)

> **Current contract (corrected 2026-07-28):** the cross-tick tonic is advisory
> body state, not a contributor and not gate modulation. It is captured on
> `EpisodeIgnition.neuromod_state` and carried into downstream deliberation.

| Step | File | Lines | What |
|---|---|---|---|
| **Body capture** | `pressure_loop/episode.py` | 388-392 | `EpisodeStore.close()` builds `body = enriched_episode_from_episode(ep)` and stores `self._last_closed_body = body`. Window-independent → survives `archive_and_reset_window`. |
| **Body accessor** | `pressure_loop/episode.py` | 435-439 | `last_closed_body()` → `self._last_closed_body` (None at boot). Read-only; never raises. |
| **Enrichment** | `pressure_loop/episode_journal.py` | 176-183 | `enriched_episode_from_episode` carries `"neuromod": ep.auto["neuromod"] if dict+dict else {}`. |
| **Advisory tonic read** | `pressure_loop/pressure.py` | current | `evaluate()` reads the cross-tick tonic without changing pressure and stores normalized dopamine/serotonin/stress/spike in the ignition record. |
| **Decision propagation** | cognition cascade | current | Chemistry flows through Meta, motor planning, execution foldback, and Omega prompts as bounded bias over assertiveness, vigor, caution, learning, memory salience, and persistence. |
| **Legacy adapters** | `pressure_loop/pressure.py` | current | `_apply_neuromod_to_weights` and `modulate_contributor_weight_map` preserve raw weights as compatibility no-ops. |

**Authority boundary:** neuromod has no `Contributor` identity. It cannot self-ignite, suppress eligible pressure, change the pressure winner, or override operator/goal/evidence contracts.

---

## 4. Deletions (no bolt-on remnant)

| Symbol | Location | Why dead |
|---|---|---|
| `persist_neuromod_spike_projection` + `_build_spike_record` | `autonomic/symphony_neuromod.py` | latch now in `_start_auto`; SS slot retired |
| `normalize_episode_auto_neuromod` | `autonomic/symphony_neuromod.py` | neuromod written natively in `_start_auto`; already dead |
| `extract_neuromod_from_auto_result` | `autonomic/symphony_neuromod.py` | only caller was precedence #3 (deleted); we write the flat dict directly |
| `compute_neuromod_spike_scalar` | `autonomic/symphony_neuromod.py` | success-gate footgun; unified on `compute_closed_body_spike` |
| `read_neuromod_spike` precedence #2 (SS) + #3 (`LAST_AUTO_CYCLE`) | `pressure_loop/pressure_field.py` | replaced by episode-store read |
| SS read in `_neuromod_from_shared_state` | `pressure_loop/pressure.py` | repointed to `episode_store.last_closed_body` |
| `LAST_NEUROMOD_SPIKE` + `_validate_neuromod_spike` + KeyContract | `core/shared_state_keys.py` | retired slot |
| `config/pressure_contributors.yaml` neuromod_spike `shared_state_keys:[last_neuromod_spike]` | `config/pressure_contributors.yaml` | repointed note to "episode_store last-closed body" or dropped the line |

**KEEP:** `compute_memory_salience`, `compute_closed_body_spike`, `compute_episode_neuromod`, `set/current_memory_salience`, `emit_neuromod_terminal`.

---

## 5. Fail-fast classification (all new seams = Reality / record+continue / NEVER-RAISE)

`compute_episode_neuromod`, `compute_closed_body_spike`, `recent_success_rate`, `last_closed_body`, `read_neuromod_spike` (already), repointed `_neuromod_from_last_body` — body-state is **modulation**, absence = legitimate baseline. None of them is a continuity structure; none may raise into `gather_contributors`/`evaluate` (whose own contract is fail-hard on a raising reader). The producer consumes already-validated outcome fields → it legitimately never crashes (outcome-validity is upstream).

---

## 6. Cascade-invariant safety

No new ignition path (producer + latch run inside an already-open episode's cascade, exactly once via `_start_auto`). Pressure contributor changes a registered reader's data+value, not its presence; hybrid gate unchanged; `evaluate` refuses to open with an active episode. `last_closed_body()`/`_last_closed_body` are read-only / a single dict copy at close — no `open`/`close`/`consume`/`archive` contact; window lifecycle byte-for-byte unchanged. Single-threaded sequential cascade → no latch race; latch refreshed every cycle → bounded one-cycle lag.

---

## 7. Related files

| File | Role |
|------|------|
| `autonomic/symphony_neuromod.py` | Pure compute: `compute_episode_neuromod`, `compute_closed_body_spike`, `compute_memory_salience`, `set/current_memory_salience`, `emit_neuromod_terminal` |
| `cognition_chain.py` | ONE compute site: `_start_auto` computes + latches; `_maybe_run_window_reflection` resets latch |
| `memory/strength_index.py` | Consumer: `register()` samples ambient salience for `write_strength * scale` and `encode_tau` |
| `pressure_loop/episode.py` | `last_closed_body()` accessor; `close()` captures enriched body with neuromod |
| `pressure_loop/episode_journal.py` | `enriched_episode_from_episode` carries neuromod in the body |
| `pressure_loop/pressure_field.py` | Advisory tonic storage/read/decay; `neuromod_spike` remains excluded from contributors |
| `pressure_loop/pressure.py` | Pressure-only ignition plus advisory `neuromod_state` snapshot |
