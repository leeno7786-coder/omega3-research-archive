# PRESSURE_LOOP_CLOSURE_MAP.md
## Omega pressure_feedback → DeepSleep reseed → next-cycle contributor → intent → action seam map
**Devin read-only audit · Ref: pressure-loop-closure-map · 2026-06-07 (refreshed for remediation)**

**See Also:**
- [`README.md`](README.md) — Architecture directory index
- [`ACTIVE_FRONT_MAP.md`](ACTIVE_FRONT_MAP.md) — Current active front mapping
- [`COGNITION_CHAIN_AND_PROMPTS_MAP.md`](COGNITION_CHAIN_AND_PROMPTS_MAP.md) — Cognition chain structure
- [`LIVE_CODEBASE_MAP.md`](LIVE_CODEBASE_MAP.md) — Live codebase structure
- [`PRESSURE_LOOP_CLOSURE_PACKETS.md`](PRESSURE_LOOP_CLOSURE_PACKETS.md) — Closure packets specification
- [`PRESSURE_LOOP_CLOSURE_README.md`](PRESSURE_LOOP_CLOSURE_README.md) — Closure readme
- [AGENTS.md](../../AGENTS.md) — governing cognition cycle

---

## 1. PRESSURE CONTRIBUTOR SCORING + AGGREGATE + IGNITION

### 1.1 Gather — from pressure_field readers to canonical Contributors
`pressure_loop/pressure.py:327-409` — `gather_contributors(shared_state)`

- Builds the reader registry via `pressure_loop.pressure_field.build_registry()` (`pressure.py:334`).
- Iterates every registered field ID; skips `_REMOVED_FIELD_IDS` = `{"guardian", "tool_creation_need", "evidence_terminal"}` (`pressure.py:42-44`).
- Maps field ID → canonical contributor type via `_FIELD_ID_TO_TYPE` (`pressure.py:47-55`):
  ```
  "curiosity_gap"         → "curiosity_gap"
  "operator_intent"       → "operator_command"
  "outstanding_question"  → "outstanding_question"
  "operator_message"      → "operator_query"
  "epistemic_emptiness"   → "epistemic_emptiness"
  "idle_probe_aggregate"  → "idle_probe_aggregate"
  "learning_debt"         → "learning_debt"
  ```
- Special-case: `operator_message` routes through `from_operator_message()` (`pressure.py:359`), which infers `operator_query`, `operator_command`, or `operator_response` (`pressure_loop/contributors.py:92-106`).
- Source is set to `"operator"` if field ID starts with `"operator"`, else `"autonomous"` (`pressure.py:357`).
- **Neuromod spike dispatch:** `CONTRIBUTOR_NEUROMOD_SPIKE` is threaded `episode_store` (`pressure.py:344-345`) — mirrors `CONTRIBUTOR_LEARNING_DEBT` pattern.
- **Reseed staging merge:** after field readers, `out.extend(list(_RESEED_STAGING))` then clears it (`pressure.py:372-374`).
- **Pending queue merge:** after reseed, `out.extend(list(_PENDING_QUEUE))` then clears it (`pressure.py:375-377`). Queue is only for operator inputs that arrived during an active episode (`pressure.py:114-127`).
- **Reseed suppress overlay:** after staging+queue, `_RESEED_SUPPRESS` subtracts per-type weights (`weight = max(0.0, weight - suppress[type])`, clamped ≥ 0) and clears (`pressure.py:378-388`).

### 1.2 Evaluate — hybrid gate (ceiling + margin)
`pressure_loop/pressure.py:412-486` — `evaluate(contributors, active_episode, ...)`

- If `active_episode` is True, contributors are enqueued (`_enqueue`) and evaluate returns `None` (`pressure.py:424-425`).
- Cross-tick neuromod is read and normalized into `EpisodeIgnition.neuromod_state` without modifying pressure.
- The hybrid gate uses only raw contributor weights plus the configured ceiling and margin.
- Advisory chemistry flows downstream into Meta, motor planning, execution foldback, and Omega; it may bias assertiveness/vigor/caution but cannot change the goal, intent, evidence, winner, or ignition decision.
- Winner resolution: `_resolve_hybrid_contributor_winner()` (`pressure.py:274-290`):
  - Top weight must reach `ceiling` (`pressure.py:245`).
  - Top must beat second by `margin` (`pressure.py:249`).
  - Ties / no runner-up → `ambiguous_winner` suppress.
- On winner found, `EpisodeIgnition` is created (`pressure.py:450-457`):
  - `opened_by = dom.type` (the **canonical** contributor type string).
  - `aggregate_pressure = dom.weight` (single winner weight, NOT sum).
  - `trajectory_snapshot` loaded from arg or shared_state (see §5).

### 1.3 No divisive-normalization aggregator in the live path
`symphony_choice_aggregator` was **DELETED** per `SLICE_1_PRESSURE_GLOBAL_A_PACKETS.md:D1`. The old `score_candidates → argmax → action signal` path is gone. Intent is decided purely by `opened_by` string in `global_loop/intent/run.py`.

---

## 2. THE RESEED / pressure_seed PATH (THE MERGE SEAM)

### 2.1 Where pressure_seed is built
`deepsleep_loop/loop.py:158-163` and `:178-196`

- `reseed_phase.generate_reseed(ds_input, None, mastery_records=...)` produces `reseed_contributors: List[Contributor]` (`loop.py:159-163`).
- These are serialized into `pressure_seed` (plain dicts) for the `DeepSleepOutcomePack` (`loop.py:178-186`).
- `pressure_seed` fields: `source`, `type`, `weight`, `metadata`.

### 2.2 Where reseed is staged into pressure
`deepsleep_loop/closure/run.py:11-25` — `finalize_window(store, reseed_contributors)`

- Calls `stage_reseed(reseed_contributors)` (`closure/run.py:22`).
- `stage_reseed` lives in `pressure_loop/pressure.py:84-88`:
  ```python
  def stage_reseed(contributors: Sequence[Contributor]) -> None:
      for c in contributors:
          if c.weight > 0.001:
              _RESEED_STAGING.append(c)
  ```
- `_RESEED_STAGING` is a module-level `List[Contributor]` (`pressure.py:58`).

### 2.3 Where reseed is consumed into the next tick
`pressure_loop/pressure.py:372-374` inside `gather_contributors()`:

```python
if _RESEED_STAGING:
    out.extend(list(_RESEED_STAGING))
    _RESEED_STAGING.clear()
```

- Reseed contributors are **prepended before** the pending operator queue (`pressure.py:372` vs `375`).
- On the next `run_pressure_tick_once` / `run_pressure_tick_loop` tick, `gather_contributors` drains `_RESEED_STAGING` into the contributor list that `evaluate` gates on.

### 2.4 What generate_reseed currently produces
`deepsleep_loop/reseed/run.py:64-164`

- `_mastery_gap_contributors` (`run.py:12-34`): type `"mastery_gap"`, weight = target − knowledge.
- `_application_gap_contributors` (`run.py:37-61`): type `"application_gap"`, weight = knowledge − application (if knowledge ≥ 0.60 and application < knowledge − 0.20).
- `trajectory_gap` contributor (`run.py:81-109`): type `"trajectory_gap"`, triggered by `continuation_recommended` in Omega reflection trajectory or trajectory_update. Weight from `next_learning_pressure` (clamped 0–1, default 0.5).
- `outstanding_question` contributors (`run.py:112-125`): one per open query, type `"outstanding_question"`, weight = query.confidence.
- **Omega `pressure_feedback`** (`run.py:127-163`): reads `reflection.pressure_feedback` from Omega window reflection:
  - `contributors_to_raise` → additive `Contributor(source="omega", type, weight=clamp(0,1), metadata={"origin": "omega_pressure_feedback"})` merged into reseed list (`stage_reseed`).
  - `contributors_to_create` → same semantics as raise (novel types allowed).
  - `contributors_to_lower` → suppression overlay via `stage_reseed_suppress(type, weight)` in `pressure_loop/pressure.py` (`weight = max(0.0, weight - suppress[type])` on next gather).

---

## 3. OMEGA pressure_feedback FLOW — LIVE

### 3.1 Where pressure_feedback is born
`omega_loop/_prompt.py:120` — explicit JSON schema in the model prompt:

```json
"pressure_feedback": {"contributors_to_raise": [], "contributors_to_lower": [], "contributors_to_create": []}
```

Field rule (`_prompt.py:128`): lists hold `{"type": "<NAME>", "weight": 0.0-1.0}`.

### 3.2 Where pressure_feedback is parsed
`omega_loop/reflect/run.py:91` — `OmegaReflection.from_dict(payload)`

- `OmegaReflection` dataclass has a `pressure_feedback: dict` field (`omega_loop/_omega_state.py:59`).
- `from_dict` copies `dict(d.get("pressure_feedback") or {})` (`_omega_state.py:74`).

### 3.3 Where pressure_feedback is validated
`omega_loop/_omega_state.py:209-230` — `validate_omega_reflection()`

- Checks `pressure_feedback` is a dict if present (`_omega_state.py:223-225`).
- **No whitelist gate** — operator directive: "unknown types are carried through and applied by the pressure intake, not rejected here" (`_omega_state.py:226-228`).

### 3.4 Where pressure_feedback reaches DeepSleep reseed
`deepsleep_loop/reseed/run.py:127-163` — `generate_reseed()` reads `reflection.get("pressure_feedback")` directly:

- `contributors_to_raise` → `stage_reseed(Contributor(source="omega", type, weight, metadata={"origin": "omega_pressure_feedback"}))` (`reseed/run.py:128-148`).
- `contributors_to_create` → same semantics as raise (novel types allowed) (`reseed/run.py:128-148`).
- `contributors_to_lower` → `stage_reseed_suppress(type, weight)` (`reseed/run.py:149-163`).

The `omega_outcome` dict is available in `generate_reseed` via `ds_input.omega_outcome`; `reflection = dict(omega.get("reflection") or {})`; `feedback = dict(reflection.get("pressure_feedback") or {})`.

### 3.5 Where suppression is applied on the next tick
`pressure_loop/pressure.py:84-99` — `stage_reseed_suppress` accumulates `_RESEED_SUPPRESS`; `gather_contributors` applies it:

```python
if _RESEED_SUPPRESS:
    suppressed = []
    for c in out:
        delta = _RESEED_SUPPRESS.get(c.type, 0.0)
        w = max(0.0, c.weight - delta)
        if w > 0.001:
            suppressed.append(make_contributor(c.source, c.type, w, dict(c.metadata)))
    out = suppressed
    _RESEED_SUPPRESS.clear()
```

**Result:** Omega's `contributors_to_lower` reduce the matching contributor type's weight on the next gather (`weight = max(0.0, weight - suppress[type])`).

---

## 4. opened_by → INTENT MAP

### 4.1 The mapping function
`global_loop/intent/run.py:24-49` — `_map_opened_by_to_intent(opened_by, message)`

| opened_by value | Intent | Notes |
|-----------------|--------|-------|
| `operator_query` | `RESPOND` | (`run.py:26-27`) |
| `operator_response` | `RESOLVE_QUERY` | (`run.py:28-29`) |
| `outstanding_question` | `PROACTIVE_RESPONSE` | (`run.py:30-31`) |
| `learning_debt`, `curiosity_gap`, `epistemic_emptiness` | `LEARN` | (`run.py:32-33`) |
| `identity_gap`, `trajectory_gap` | `IDLE` | (`run.py:34-35`) |
| `operator_command` | `IMAGE_CREATE` if `_IMAGE_HINTS` match (`run.py:37-38`)<br>`USE_TOOL` if `_TASK_HINTS` match (`run.py:39-40`)<br>`LEARN` if message starts with learn/study/teach me/research/explore (`run.py:41-45`)<br>else `USE_TOOL` (`run.py:46`) | `_IMAGE_HINTS` = `/\b(draw|paint|generate\s+image|image\s+of|picture\s+of|render\s+)\b/i` (`run.py:9-12`)<br>`_TASK_HINTS` = `/\b(run|execute|fetch|build|deploy|fix|implement|write\s+code)\b/i` (`run.py:13-16`) |
| `idle_probe_aggregate` | `IDLE` | (`run.py:47-48`) |
| **anything else** (including novel types) | `IDLE` | (`run.py:49`) |

### 4.2 Intent validation
`global_loop/intent/run.py:52-57` — `run(ignition)`

- Calls `_map_opened_by_to_intent(ignition.opened_by, _dominant_message(ignition))`.
- If returned intent is not in `ALLOWED_INTENTS`, falls back to `"IDLE"` (`run.py:55-56`).
- `ALLOWED_INTENTS` = `("RESPOND", "PROACTIVE_RESPONSE", "RESOLVE_QUERY", "LEARN", "USE_TOOL", "IMAGE_CREATE", "IDLE")` (`pressure_loop/contributors.py:12-21`).

### 4.3 Novel-type handling — LIVE
`global_loop/intent/run.py:49` returns `"IDLE"` for any unrecognised `opened_by` (including types created by `contributors_to_create`). `SELF_INQUIRY` was removed (operator directive 2026-07-09) and collapsed to `IDLE` — the mind chose quiet, ignite nothing; the idle-wake awakening owns real reflection now.

---

## 5. TRAJECTORY → IGNITION

### 5.1 Trajectory snapshot source for EpisodeIgnition
`pressure_loop/pressure.py:443-448` inside `evaluate()`:

```python
traj = trajectory_snapshot
if traj is None and shared_state is not None:
    raw = shared_state.read(LAST_OMEGA_TRAJECTORY, None)
    traj = dict(raw) if isinstance(raw, dict) else None
```

- `LAST_OMEGA_TRAJECTORY` = `"last_omega_trajectory"` (`core/shared_state_keys.py:145`).
- Both live callers (`pressure_loop/loop.py:36-41` and `pressure_loop/operator_chat.py`) pass **no** `trajectory_snapshot` argument, so it defaults to `None`.

### 5.2 LAST_OMEGA_TRAJECTORY is dormant on the new path
`core/shared_state_keys.py:950-962` — key contract says writer="omega_loop", readers=["global_loop", "deepsleep_loop"].

- `omega_loop/README.md:80`: "Omega does NOT write `LAST_OMEGA_TRAJECTORY`."
- `SLICE_5_BUILT_MAP.md`: confirms "No `LAST_OMEGA_TRAJECTORY` write (legacy key remains dormant)."
- The only reachable writer today is legacy `core/operator_pressure.py:271-313` (`seed_trajectory_from_operator_directive`), which requires explicit operator trigger.
- **Result:** `raw` is consistently `None` on the rebuild path, so `EpisodeIgnition.trajectory_snapshot` is `None` for regular (non-window) episodes.

### 5.3 Trajectory for reflection windows
`pressure_loop/reflection_window.py:203-226` — `open_reflection_window()`:

```python
if trajectory_snapshot is not None:
    snapshot = dict(trajectory_snapshot)
else:
    try:
        from pressure_loop.temporal_store import read_trajectory
        snapshot = read_trajectory()
    except Exception:
        snapshot = {}
```

- `read_trajectory()` queries lobe 9 `(9,2)` for the latest trajectory record (`pressure_loop/temporal_store.py:129-163`).
- This is the **lobe-9 trajectory baseline** — the prior window's Omega package phase wrote it there.
- Therefore **window-level** Omega input gets the trajectory snapshot correctly (from lobe 9), but **per-episode ignition** does NOT (dormant shared-state key).

### 5.4 Trajectory write path
`omega_loop/package/run.py:25-40`:

- `package_window()` calls `write_trajectory()` with `target_domain`, `target_capability`, `confidence`, `rationale`, `source_window_id` (`omega_loop/package/run.py:33-41`).
- This writes to lobe 9 `(9,2)` via `temporal_store.write_trajectory()` (`pressure_loop/temporal_store.py:166-184`).
- It also calls `save_current_trajectory()` (legacy disk file) for backward-compat.
- This is the **only** trajectory persistence on the new path. It does NOT write to SharedState / `LAST_OMEGA_TRAJECTORY`.

---

## SUMMARY — EXACT SEAMS (AS-BUILT)

| # | Seam | State | File:Line |
|---|---|------|-----------|
| 1 | **Omega pressure_feedback → DeepSleep reseed** | **LIVE.** `generate_reseed` reads `reflection.pressure_feedback` directly from `ds_input.omega_outcome` (`reseed/run.py:127-163`). No `DeepSleepInput` field needed. | `deepsleep_loop/reseed/run.py:76-80` → `feedback = dict(reflection.get("pressure_feedback") or {})` |
| 2 | **pressure_feedback → reseed contributors** | **LIVE.** `contributors_to_raise/create` → `stage_reseed(Contributor(...))`; `contributors_to_lower` → `stage_reseed_suppress(type, weight)`. | `deepsleep_loop/reseed/run.py:128-163` |
| 3 | **Reseed → next tick pressure** | **LIVE.** `stage_reseed` → `_RESEED_STAGING` drained before operator queue in `gather_contributors`; `_RESEED_SUPPRESS` applied after staging+queue merge (`weight = max(0.0, weight - suppress[type])`). | `pressure_loop/pressure.py:84-99` (suppress), `:372-374` (staging drain), `:378-388` (suppress apply) |
| 4 | **Novel contributor type → intent** | **LIVE.** `_map_opened_by_to_intent` falls back to `"IDLE"` for any unrecognized `opened_by` (`SELF_INQUIRY` removed, collapsed to `IDLE`). | `global_loop/intent/run.py:49` |
| 5 | **Trajectory on every ignition** | `LAST_OMEGA_TRAJECTORY` remains dormant; regular episodes get `trajectory_snapshot=None`. Window-level reflection gets trajectory via `temporal_store.read_trajectory()` (lobe 9). | `pressure_loop/pressure.py:443-448` (dormant SS read), `pressure_loop/reflection_window.py:211-217` (lobe-9 baseline) |

---

*Read-only audit. No code modified.*
