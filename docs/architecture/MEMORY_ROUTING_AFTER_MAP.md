# Memory Routing — AFTER Map (Slices 1–3 As-Built)

> **Scope:** As-built documentation of the deterministic two-lane memory routing system
> (recall + ingest + salience) landed in Slices 1–3. Read-only on code; verify every claim
> against file:line before trusting.
> **Date:** 2026-06-07
> **Agent:** Devin
> **Work order:** `#a6614813` (claude → devin)
> **Pairs with:** [`MEMORY_SUBSYSTEM_BEFORE_MAP.md`](MEMORY_SUBSYSTEM_BEFORE_MAP.md)

---

## 1. What changed (before → after summary)

| Before (pre-Slice 1–3) | After (as-built) |
|---|---|
| Recall: SetFit classifier seeds walker (X, Y) — no caller-enforced subdomain | Recall: deterministic `subdomain_for` helper routes MRIL recall to exact Y; walker skips SetFit when `allowed_subdomains` present |
| Ingest: SetFit predicts Y at gate; reads may miss writes due to classifier variance | Ingest: `apply_subdomain_override` forces the same deterministic Y that recall uses → **write-Y == read-Y** invariant |
| StrengthIndex: static `DECAY_TAU_SECONDS` for all memories | StrengthIndex: per-memory `encode_tau` scaled by ambient neuromod salience (stress shortens, serotonin lengthens) |
| Neuromod → memory: unwired seam | Neuromod → memory: latched salience sampled at `register()` time; `compute_memory_salience` is the single source of truth |

---

## 2. SEAMS table

### 2.1 Recall lane (Slice 1)

| Step | File:line | Contract |
|------|-----------|----------|
| Deterministic helper | `auto_loop/mril_tools/subdomain_helpers.py:72` | `subdomain_for(text, lobe) -> Optional[int]` — pronoun-based for identity (0) / persona (1); word-boundary term-match for knowledge (2), coding (8), workflows (10); None widens |
| Router attaches Y | `auto_loop/mril_tools/router.py:22` | `_tool_entry` calls `subdomain_for(query, lobe)` and injects `"subdomain"` into the tool spec |
| Tool passes constraint | `auto_loop/mril_tools/remember.py:40-42` | `allowed_subdomains = [int(tool_spec["subdomain"])]` when present |
| QuerySpec field | `memory/api.py:95` | `allowed_subdomains: Optional[List[int]]` — lane-discipline filter added to dataclass |
| API filters by Y | `memory/api.py:340-345` | `allowed_subdomains` intersects candidate `coord[1]` after lobe filter, before normalization |
| Walker deterministic seed | `memory/engine/walker.py:679-689` | `deterministic_subdomain = True` when `allowed_subdomains` + `force_lobe` both set → X/Y/tokens extracted directly; **SetFit `classify()` is skipped** |
| Walker candidate selection | `memory/engine/walker.py:722-727` | `list_ids_in_subdomain(X, y)` for each allowed Y → ordered dedup candidate list |
| Walker query adapter | `memory/engine/walker.py:881-896` | `walker_query` sets `force_lobe` from `allowed_lobes[0]` when `allowed_subdomains` present; re-scores with deterministic seed |

**Key invariant:** When MRIL routes a remember tool with a subdomain, the walker never calls the classifier. The candidate set is the exact subdomain(s) the helper dictated. This is the **failsafe zero-shot** seam — no model in the read path.

### 2.2 Ingest lane (Slice 2)

| Step | File:line | Contract |
|------|-----------|----------|
| Override function | `memory/gates/subdomain_override.py:7` | `apply_subdomain_override(text, coord, label_id) -> (coord, label_id)` — pure, lazy-imports helper |
| Single-lobe gate convergence | `memory/gates/base.py:364-404` | `classify_within_lobe` produces (label_id, coord, …); gate enforces X contract |
| Multi-lobe gate convergence | `memory/gates/base.py:406` | `_classify(text)` produces (label_id, coord, …) |
| Override at convergence | `memory/gates/base.py:409-414` | `apply_subdomain_override` runs at the single/multi-lobe convergence point; if helper Y differs from SetFit Y, coord/label_id are rewritten; telemetry emits `ingest_subdomain_override` |
| Result | — | Ingest lands at the same (X, Y) that recall will search, regardless of SetFit variance |

**Fallback behavior:** For lobes without a helper (3, 5, 6, 7, 9, 11) or texts the helper returns `None` for, `apply_subdomain_override` is a no-op — SetFit Y stands. This means SetFit is now **ingest-fallback-only** for non-helper cases.

### 2.3 Salience lane (Slice 3)

| Step | File:line | Contract |
|------|-----------|----------|
| Salience compute | `core/symphony_neuromod.py:147-163` | `compute_memory_salience(neuromod) -> {"scale": float, "tau_factor": float}` — reward/plasticity boost scale; stress shortens tau, serotonin lengthens it |
| Ambient latch set | `core/symphony_neuromod.py:166-171` | `set_current_memory_salience(neuromod)` latches once per cycle in `persist_neuromod_spike_projection` |
| Ambient latch read | `core/symphony_neuromod.py:174-177` | `current_memory_salience()` returns latched dict or None |
| Register samples latch | `memory/strength_index.py:157-186` | `register(mid, write_strength, salience=None)` — if None, samples `current_memory_salience()` at call time; writes `write_strength * scale` and `encode_tau = DECAY_TAU_SECONDS * tau_factor` |
| Decay read | `memory/strength_index.py:315-347` | `get_strength()` uses `rec.get("encode_tau", DECAY_TAU_SECONDS)` instead of hard-coded global |

**Constant source of truth:** `ENCODE_DOPAMINE_GAIN = 0.6`, `ENCODE_PLASTICITY_GAIN = 0.4`, `STRESS_TAU_PULL = 0.5`, `SEROTONIN_TAU_PUSH = 0.5`, `TAU_FACTOR_MIN = 0.5`, `TAU_FACTOR_MAX = 1.5` — all in `core/symphony_neuromod.py:107-112`.

---

## 3. Data-flow diagrams

### 3.1 Recall lane (deterministic MRIL)

```
operator_query
    |
    v
auto_loop/mril_tools/router.py:route_memory_tool(query)
    |
    +---> subdomain_for(query, lobe)  (pronoun / term-match / None)
    |         |
    |         +---> Y | None
    |
    +---> _tool_entry(tool, query)  injects "subdomain": Y into spec
    |
    v
auto_loop/mril_tools/remember.py:invoke_remember_tool(tool_spec, episode_id)
    |
    +---> QuerySpec(
    |       allowed_lobes=[lobe],
    |       allowed_subdomains=[Y],   # when Y present
    |       denied_lobes=[6, 11],
    |       ...)
    |
    v
memory/api.py:read_memory(query)
    |
    +---> walker_query(query)
    |       |
    |       +---> walker(..., force_lobe=lobe, allowed_subdomains=[Y])
    |             |
    |             +---> deterministic_subdomain = True
    |             |     (skips SetFit classify)
    |             |
    |             +---> X = force_lobe; Y = allowed_subdomains[0]
    |             |     tokens = clf.tokens(query)
    |             |     conf = 1.0
    |             |
    |             +---> candidate_ids = list_ids_in_subdomain(X, Y)
    |             |     (exact subdomain seed)
    |             |
    |             +---> composite_score(..., seed_xy=(X,Y), tokens)
    |             |     → rank → expand_links → assemble_context
    |             |
    |             +---> contexts
    |
    +---> API lobe filter + subdomain filter (read-side enforcement)
    |
    +---> RetrievalCandidate list
    |
    +---> si.touch(c.id) for every hit  (reconsolidation)
    |
    v
RetrievalResult(candidates, meta)
```

### 3.2 Ingest lane (write-Y == read-Y)

```
text + metadata
    |
    v
MemoryGate.ingest(text, metadata)
    |
    +---> _ingest_impl(text, metadata, id_out)
    |       |
    |       +---> classify_within_lobe(text, X)  [single-lobe]
    |       |     or _classify(text)            [multi-lobe]
    |       |     → (label_id, coord, confidence, tokens)
    |       |
    |       +---> apply_subdomain_override(text, coord, label_id)
    |       |     → (coord', label_id')
    |       |     If helper_y != setfit_y: coord' = [X, helper_y, Z]
    |       |     If helper_y is None or equal: no-op
    |       |
    |       +---> _write(text, coord', enrichment, metadata)
    |             |
    |             +---> build_memory_dict → atomic_write → path
    |             |
    |             +---> si.register(mem["id"], write_strength)
    |                   (samples current_memory_salience → scale + tau_factor)
    |
    v
path (or None on rejection)
```

### 3.3 Salience lifecycle

```
AUTO loop completes
    |
    v
core/symphony_neuromod.py:normalize_episode_auto_neuromod(episode, auto_result)
    |
    +---> extract_neuromod_from_auto_result(auto_result)
    |     → episode.auto["neuromod"] = {dopamine, plasticity, strength, stress, cycle_kind}
    |
    v
core/symphony_neuromod.py:persist_neuromod_spike_projection(shared_state, episode)
    |
    +---> compute_memory_salience(neuromod)
    |     → {"scale": 1.0 + 0.6*reward + 0.4*plasticity,
    |        "tau_factor": 1.0 - 0.5*stress + 0.5*serotonin}
    |
    +---> set_current_memory_salience(neuromod)  # latch
    |
    +---> shared_state.write_canonical(LAST_NEUROMOD_SPIKE, record)
    |
    v
[Next gate ingest or DeepSleep consolidation write]
    |
    v
memory/strength_index.py:register(mid, write_strength)
    |
    +---> salience = current_memory_salience()  # latched ambient
    |     or None → fallback scale=1.0, tau_factor=1.0
    |
    +---> stored write_strength = write_strength * scale
    |     stored encode_tau    = DECAY_TAU_SECONDS * tau_factor
    |
    v
memory/strength_index.py:get_strength(mid)
    |
    +---> age decay uses encode_tau (not global DECAY_TAU_SECONDS)
    |     → stronger reward memories encoded harder;
    |     → stressed memories fade faster;
    |     → serotonergic memories retain longer.
```

---

## 4. Before → after diff (per seam)

### 4.1 Recall lane diff

**Before:** `remember.py` set `allowed_lobes=[lobe]` but never passed a subdomain constraint. The walker called `classify(query)` to get (X, Y), then selected candidates by confidence tier (`≥0.85` → exact subdomain, `≥0.5` → neighborhood, `<0.5` → cross-lobe). MRIL recall was probabilistic and could mis-route across subdomains within the same lobe.

**After:** When a tool spec carries `"subdomain"`, `remember.py` passes `allowed_subdomains=[Y]` to `QuerySpec`. `walker_query` translates this into `force_lobe=lobe` + `allowed_subdomains=[Y]`. Inside `walker()`, `deterministic_subdomain` becomes `True`, the classifier is **skipped entirely**, and candidates are seeded directly from `list_ids_in_subdomain(X, Y)`. The only ML remaining in the recall path is the tokenization used for `composite_score` ranking — no SetFit inference.

**Regression guard:** Code lobes (8, 10) are **exempt** from exact-subdomain scoping even when `deterministic_subdomain` is true. The walker checks `deterministic_subdomain` first (line 722), then the code-lobe broadening (line 728) only fires when deterministic is false. This means deterministic tools targeting code/workflows still get exact subdomain scoping — verified by the branch ordering.

### 4.2 Ingest lane diff

**Before:** Gate `_ingest_impl` called `classify_within_lobe` or `_classify`, wrote to the predicted (X, Y), and the Y SetFit produced was the only Y on disk. If SetFit write-Y diverged from read-Y (common for sparse subdomain classifiers), the record became unfindable at exact-subdomain recall.

**After:** At the convergence point after classification and before permission check, `apply_subdomain_override(text, coord, label_id)` runs. If the deterministic helper returns a Y for this lobe, it **overrides** the SetFit Y. The gate telemetry (`ingest_subdomain_override`) emits when an override actually changes Y so drift is observable. The helper uses the **same** `subdomain_for` function that recall uses, so write-Y and read-Y are the same by construction.

**SetFit status:** Not deleted. Still used for:
- Lobes without helpers (3 reasoning, 5 tools, 6 journal, 7 vision, 9 temporal, 11 dev)
- Texts where the helper returns `None` (no pronoun match in identity/persona; no keyword match in knowledge/coding/workflows)
- Stage-1 lobe routing in multi-lobe gates
- Walker seeding for non-MRIL recall (e.g. `brain.retrieve`, gate `.recall()`, posture-aware compiler)

### 4.3 Salience diff

**Before:** `StrengthIndex.register()` stored `write_strength` verbatim and `get_strength()` used the global `DECAY_TAU_SECONDS` constant. Neuromod signals were computed but never reached memory encoding.

**After:** `register()` samples the ambient salience latch at call time. `write_strength` is multiplied by `scale` (≥1.0) and `encode_tau` is set to `DECAY_TAU_SECONDS * tau_factor`. `get_strength()` reads `encode_tau` from the per-memory record, falling back to the global constant for pre-Slice-3 memories. This means:
- **Reward cycles** (positive dopamine + plasticity) → harder initial encoding (higher `write_strength`)
- **Stressful cycles** → shorter retention (`tau_factor < 1`)
- **Aversive/serotonergic cycles** → longer retention (`tau_factor > 1`)

**Ambient latch semantics:** `set_current_memory_salience()` is called once per cycle from `persist_neuromod_spike_projection` (line 535). All memory writes within that cycle sample the **same** salience snapshot — deterministic, no race. If no spike has fired yet (boot, test paths), `current_memory_salience()` returns `None` → `scale=1.0`, `tau_factor=1.0` (neutral baseline).

---

## 5. Files touched / retained / superseded

| File | Status | Notes |
|------|--------|-------|
| `auto_loop/mril_tools/subdomain_helpers.py` | **NEW** Slice 1 | Deterministic helper library |
| `auto_loop/mril_tools/router.py` | **MODIFIED** Slice 1 | Injects subdomain into tool spec |
| `auto_loop/mril_tools/remember.py` | **MODIFIED** Slice 1 | Passes `allowed_subdomains` |
| `memory/api.py` | **MODIFIED** Slice 1 | `QuerySpec.allowed_subdomains` + filter |
| `memory/engine/walker.py` | **MODIFIED** Slice 1 | `deterministic_subdomain` branch + `walker_query` adapter |
| `memory/gates/subdomain_override.py` | **NEW** Slice 2 | Pure override function |
| `memory/gates/base.py` | **MODIFIED** Slice 2 | Wired at `_ingest_impl` convergence point |
| `core/symphony_neuromod.py` | **MODIFIED** Slice 3 | `compute_memory_salience` + latch + constants |
| `memory/strength_index.py` | **MODIFIED** Slice 3 | `register` samples salience; `get_strength` reads `encode_tau` |
| `memory/engine/hierarchical_classifier.py` | **RETAINED** | HTTP client to runtime; still loads SetFit. Used for non-deterministic recall + ingest fallback |
| `memory/gates/lobe_gates.py` | **RETAINED** | All 12 gates unchanged in contract; ingest now gets override |
| `memory/recall_lanes.py` | **RETAINED** | Lane runners unchanged; deterministic recall bypasses them |
| `memory/recall_compiler.py` | **RETAINED** | Posture-aware query prior unchanged |
| `memory/recall_arbitrator.py` | **RETAINED** | Arbitration unchanged |

---

## 6. Extending the system (adding a subdomain)

To add a new deterministic subdomain to a helper-routed lobe:

1. **Register in `dewey_map.SUBDOMAIN_MAP`** — add the `(X, Y) → dirname` entry.
2. **Add helper rule in `subdomain_helpers.py`**:
   - Identity (0) / Persona (1): add a pronoun or regex rule to `_identity_subdomain` / `_persona_subdomain`
   - Knowledge (2) / Coding (8) / Workflows (10): add the canonical dirname to `SUBDOMAIN_MAP`; `_term_match_subdomain` will automatically index it (no code change needed unless you want a custom matcher)
3. **Ingest automatically mirrors** via `apply_subdomain_override` — no additional wiring.
4. **Add a MRIL tool** (if this subdomain deserves its own remember path): register in `auto_loop/mril_tools/registry.py`, route in `router.py`, or rely on the existing lobe-wide tool + helper routing.

---

## 7. Known risks / seams (as-built, not deferred)

1. **Classifier skip scope:** Only MRIL recall with `allowed_subdomains` present skips the classifier. All other recall (`brain.retrieve`, gate `.recall()`, posture-aware compiler) still uses SetFit. If the runtime classifier is down, these paths degrade to lobe-2 fallback — not a crash, but a quality regression.
2. **Code/workflow broadening:** Even deterministic subdomain calls for lobes 8/10 do **not** get exact-subdomain scoping because the `deterministic_subdomain` branch (line 722) executes before the `X in (8, 10)` branch (line 728). Wait — actually, the `deterministic_subdomain` branch returns `candidate_ids` directly and the code-lobe branch is in `elif`, so deterministic code/workflow tools DO get exact subdomain scoping. The before-map warned about 8<->10 disambiguation; this is now bypassed for deterministic calls. **Risk:** a deterministic coding tool scoped to `Y=3` will miss records the classifier placed in `Y=4` — but that is the **intended** deterministic-home guarantee.
3. **Salience sampling timing:** The latch is set once per cycle in `persist_neuromod_spike_projection`. If a gate write happens BEFORE the spike is projected (e.g. boot-time seed, mid-cycle tool registration), `current_memory_salience()` returns `None` → neutral encoding. This is correct — no cycle has completed, so no body-state exists to bias encoding.
4. **SetFit fallback drift:** For non-helper lobes (3, 5, 6, 7, 9, 11) and helper-None texts, SetFit Y is still the authority. If SetFit write-Y drifts from read-Y (the original bug), those records remain at risk. The long-term fix is to add helpers for every lobe that needs deterministic routing.

---

*End of AFTER map. This is a read-only document — no code was modified.*
