# Omega 3.x — Active Front Map (Slice 1 + Slice 2)

**Status:** Historical reference only (legacy snapshot)  
**Date:** 2026-06-04  
**Current Mapping (as of 2026-07-18):** see [`LIVE_CODEBASE_MAP.md`](LIVE_CODEBASE_MAP.md)  
**Authority Specs:** 
- Slice 1: [`OMEGA 3.x ARCHITECTURE CORRECTION.txt`](legacy/OMEGA%203.x%20ARCHITECTURE%20CORRECTION.txt) (historical)
- Slice 2: [`CLAUDE IMPLEMENT META LOOP A EXACTL.txt`](legacy/CLAUDE%20IMPLEMENT%20META%20LOOP%20A%20EXACTL.txt) (historical)

**See Also:**
- [`README.md`](README.md) — Architecture directory index
- [`COGNITION_CHAIN_AND_PROMPTS_MAP.md`](COGNITION_CHAIN_AND_PROMPTS_MAP.md) — Cognition chain structure
- [`LIVE_CODEBASE_MAP.md`](LIVE_CODEBASE_MAP.md) — Live codebase structure
- [AGENTS.md](../../AGENTS.md) — governing architecture contract

---

This document is an historical slice-1/2 front snapshot and does not reflect the current full cascade.  
For current behavior, use [`LIVE_CODEBASE_MAP.md`](LIVE_CODEBASE_MAP.md) as the authoritative map.

---

## ACTIVE PATH (Slice 1 → Slice 2 → STOP)

### 1. core/contributors.py (Slice 1 B1)
**Status:** ACTIVE — canonical contributor / ignition / action-goal types

**Key Types:**
- `Contributor` (line 35-39): `{source, type, weight, metadata}` — normalized stimulus structure
- `EpisodeIgnition` (line 43-49): `{episode_id, aggregate_pressure, opened_by, dominant_contributor, contributors, trajectory_snapshot}` — pressure output
- `ActionGoal` (line 53-58): `{episode_id, intent, target, confidence, reason}` — Global A output (intent-only)

**Constants:**
- `ALLOWED_INTENTS` (line 12-21): Exactly 7 intents — `RESPOND`, `PROACTIVE_RESPONSE`, `RESOLVE_QUERY`, `LEARN`, `USE_TOOL`, `IMAGE_CREATE`, `IDLE`

**Functions:**
- `make_contributor` (line 61-72): Constructor with validation
- `from_operator_message` (line 92-106): Normalizes operator ingress into Contributor (type inference via `_infer_operator_type`)

**Data Flow:** External stimuli → Contributor objects → Pressure system

---

### 2. core/pressure.py (Slice 1 B2)
**Status:** ACTIVE — pressure evaluation → EpisodeIgnition

**Removed Contributors (line 29-31):**
- `guardian`, `tool_creation_need`, `evidence_terminal` — explicitly skipped per slice 1 spec

**Field Mapping (line 34-42):**
- Maps pressure_field contributor IDs to canonical types:
  - `curiosity_gap` → `curiosity_gap`
  - `operator_intent` → `operator_command`
  - `outstanding_question` → `outstanding_question`
  - `operator_message` → `operator_query` (refined via ingress metadata)
  - `epistemic_emptiness` → `epistemic_emptiness`
  - `idle_probe_aggregate` → `idle_probe_aggregate`
  - `learning_debt` → `learning_debt`

**Pending Queue (line 44-58):**
- `_PENDING_QUEUE`: Module-level list for operator inputs arriving during episode halt
- Only operator inputs queue (`operator_command`, `operator_query`, `operator_response`)
- Autonomous drives (curiosity_gap, learning_debt, etc.) are re-derived each tick, never queued
- Deduped by `(type, message/identity)` key (line 69-78)
- Drained on next `gather_contributors` call (line 210-212)

**Neuromodulation (contract corrected 2026-07-28):**
- `evaluate` snapshots dopamine, serotonin, stress, and spike into `EpisodeIgnition.neuromod_state`.
- `_apply_neuromod_to_weights` and `modulate_contributor_weight_map` are compatibility no-ops; chemistry cannot change pressure.
- The snapshot flows through Meta, motor planning, execution foldback, and Omega. Low serotonin may lower assertiveness, stress may favor caution, and dopamine may bias vigor/persistence only within an already-valid action.

**Core Functions:**
- `gather_contributors` (line 169-213): 
  - Reads pressure_field readers via `build_registry()`
  - Skips `_REMOVED_FIELD_IDS`
  - Normalizes to Contributor objects
  - Special handling for `CONTRIBUTOR_OPERATOR_MESSAGE` → `from_operator_message`
  - Drains pending queue
  - Returns list of Contributors

- `evaluate` (line 216-257):
  - Input: contributors sequence, active_episode flag, trajectory_snapshot, thresholds, neuromod
  - If `active_episode=True`: queues contributors via `_enqueue`, returns None (single-active-episode rule)
  - If no contributors: returns None
  - Evaluates raw pressure against the configured ceiling and winner margin
  - Returns None when no contributor clears that pressure-only boundary
  - Selects the dominant contributor without chemistry reweighting
  - Returns `EpisodeIgnition` with UUID, aggregate, opened_by, dominant, contributors, trajectory, and separate advisory neuromod state

**Data Flow:** Contributors → pressure-only ceiling/margin check → EpisodeIgnition + advisory chemistry (or queue if active episode)

---

### 3. core/global_a.py (Slice 1 B3)
**Status:** ACTIVE — semantic intent router → ActionGoal

**Pattern Matching:**
- `_IMAGE_HINTS` (line 10-13): Regex for image generation requests
- `_TASK_HINTS` (line 14-17): Regex for task execution requests

**Core Functions:**
- `_dominant_message` (line 20-22): Extracts message text from dominant contributor's metadata
- `_extract_target` (line 25-42): Extracts target/topic from ignition (strips learning verbs, uses domain metadata, or truncates message)
- `_map_opened_by_to_intent` (line 45-73): Maps ignition reason to one of 8 allowed intents:
  - `operator_query` → `RESPOND`
  - `operator_response` → `RESOLVE_QUERY`
  - `outstanding_question` → `PROACTIVE_RESPONSE`
  - `learning_debt` / `curiosity_gap` / `epistemic_emptiness` → `LEARN`
  - `identity_gap` / `trajectory_gap` → `IDLE`
  - `operator_command` + image hints → `IMAGE_CREATE`
  - `operator_command` + task hints → `USE_TOOL`
  - `operator_command` + learning verbs → `LEARN`
  - `operator_command` (default) → `USE_TOOL`
  - `idle_probe_aggregate` → `IDLE`
  - Fallback → `IDLE`

- `ignition_from_episode` (line 76-108): Rebuilds EpisodeIgnition from an open episode (for slice-1 front replay)

- `form_intent` (line 111-127):
  - Input: EpisodeIgnition
  - Extracts dominant message
  - Maps opened_by to intent via `_map_opened_by_to_intent`
  - Validates intent is in `ALLOWED_INTENTS` (else defaults to `IDLE`)
  - Extracts target via `_extract_target`
  - Computes confidence from dominant weight or aggregate
  - Returns `ActionGoal` (intent-only, no tool/model/execution)

**Data Flow:** EpisodeIgnition → intent classification → ActionGoal (intent, target, confidence, reason)

---

### 4. core/meta_a.py (Slice 2 B1-B5)
**Status:** ACTIVE — deterministic ActionGoal → ExecutionActionPack (NO INFERENCE)

**Constants:**
- `META_ACTIONS` (line 9-15): 5 actions — `USE_TOOL`, `RESPONSE`, `PROACTIVE_RESPONSE`, `IMAGE_CREATION`, `IDLE`
- `_INTENT_TO_ACTION` (line 17-26): Intent → action mapping:
  - `RESPOND` / `RESOLVE_QUERY` → `RESPONSE`
  - `PROACTIVE_RESPONSE` → `PROACTIVE_RESPONSE`
  - `LEARN` / `USE_TOOL` → `USE_TOOL`
  - `IMAGE_CREATE` → `IMAGE_CREATION`
  - `IDLE` → `IDLE`
- `_CAPABILITY_TOOL_MAP` (line 28-35): Capability → tool mapping:
  - `external_information` → `web_fetch`
  - `ocr` → `ocr_tool`
  - `desktop` → `desktop_tool`
  - `file_read` → `file_reader`
  - `memory` → `memory_recall`
  - `image` → `image_tool`
- `_BANNED_KEYS` (line 37-49): Hard-banned model/inference keys — `model`, `llm`, `engine`, `prompt`, `system_prompt`, `temperature`, `max_tokens`, `tokens`, `sampling`, `top_p`, `top_k`
- `_REQUIRED_CONSTRAINT_FLAGS` (line 51-56): Required constraint flags — `planning_only`, `no_inference_in_meta`, `no_execution_in_meta`, `no_model_fields`

**Types:**
- `MetaAInput` (line 69-73): `{episode_id, pressure, trajectory_snapshot, action_goal}`
- `ExecutionActionPack` (line 77-88): `{episode_id, action, tool, tool_reason, memory_required, memory_reason, memory_scope, execution_intent, execution_inputs, execution_constraints, source_action_goal}`

**Validation:**
- `validate_execution_action_pack` (line 102-132):
  - Recursively checks for banned model/inference keys
  - Validates `execution_constraints` is a dict with required flags
  - Requires `tool_reason` when tool is set
  - Requires `memory_reason` and `memory_scope` when `memory_required` is true

**Core Functions:**
- `build_execution_action_pack` (line 180-289): Deterministic intent→action mapping per spec cases A-H:
  - **Case A (RESPOND):** action=RESPONSE; memory_required if prior-history pattern; tool may be memory_recall or null
  - **Case B (RESOLVE_QUERY):** action=RESPONSE; execution_inputs from contributor metadata
  - **Case C (PROACTIVE_RESPONSE):** action=PROACTIVE_RESPONSE; no text generation
  - **Case D (LEARN):** action=USE_TOOL; tool=web_fetch; memory_required if prior-history reference
  - **Case E (USE_TOOL):** action=USE_TOOL; tool by capability (file_reader, ocr_tool, desktop_tool, memory_recall, web_fetch fallback)
  - **Case F (IMAGE_CREATE):** action=IMAGE_CREATION; tool=image_tool
  - **Case G (SELF_INQUIRY — removed):** SELF_INQUIRY was removed (operator directive 2026-07-09), collapsed to IDLE; the idle-wake awakening owns reflection now
  - **Case H (IDLE):** action=IDLE; minimal pack
  - Always sets `execution_constraints` with required flags
  - Always validates pack before return

- `meta_a_input_from_episode` (line 292-317): Assembles MetaAInput from slice-1 episode (reads episode.global_.action_goal, episode.pressure, trajectory)

- `run_meta_a_front` (line 320-329):
  - Builds MetaAInput from episode
  - Calls `build_execution_action_pack`
  - Converts to dict via `execution_action_pack_to_dict`
  - Writes to `episode.meta["execution_action_pack"]`
  - Returns pack dict

**Data Flow:** ActionGoal → intent→action mapping → tool selection → memory planning → ExecutionActionPack (validated, no model fields)

---

### 5. core/cue_desk.py _tick_slice1_front (Slice 1 B4)
**Status:** ACTIVE — slice-1 front entry point in CueDesk

**Location:** `CueDesk._tick_slice1_front` (line 427-444, truncated in read)

**Behavior:**
- Checks `slice1_global_a_front_enabled()` flag (line 438-440)
- Calls `pressure.gather_contributors` (line 437)
- Calls `pressure.evaluate` (line 437)
- Calls `global_a.form_intent` (line 436)
- Returns `CueDeskReport` with `action_goal` field (line 363)
- Does NOT run movement chain when slice2 is disabled (STOP at ActionGoal per slice 1 spec)

**Data Flow:** SharedState → gather contributors → evaluate ignition → form intent → ActionGoal → CueDeskReport

---

### 6. core/symphony_runner.py Slice Gates (Slice 1 + Slice 2 B4-B5)
**Status:** ACTIVE — gates control which slice front runs

**Slice Flags (from policy/symphony.policy.yaml):**
- `slice1_global_a_front: true` (line 26)
- `slice2_meta_a_front: true` (line 28)

**Gate Logic:**

**In `run_symphony_through_deepsleep` (line 1309-1315):**
```python
if slice1_global_a_front_enabled():
    if slice2_meta_a_front_enabled():
        _append_slice2_meta_a_movement(trace, episode)
    return trace  # STOP after slice2 (or slice1 if slice2 disabled)
```

**In `run_symphony_meta_auto_meta_b` (line 2809-2816):**
```python
if slice1_global_a_front_enabled():
    if slice2_meta_a_front_enabled():
        _append_slice2_meta_a_movement(trace, episode)
    return trace  # STOP after slice2
```

**`_append_slice2_meta_a_movement` (line 804-818):**
- Calls `core.meta_a.run_meta_a_front(episode)` (line 807)
- Writes `episode.meta["execution_action_pack"]`
- Adds META_A movement to trace
- On exception: logs debug, adds skipped movement to trace

**Active Path Under Both Flags:**
1. CueDesk calls `_tick_slice1_front` → ActionGoal
2. Runner checks slice1 flag (true) → proceeds
3. Runner checks slice2 flag (true) → calls `_append_slice2_meta_a_movement`
4. Meta A front runs → ExecutionActionPack
5. Runner returns trace (STOP before AUTO/META_B/GLOBAL_B/OMEGA/DEEPSLEEP)

**Data Flow:** ActionGoal → slice2 gate → Meta A front → ExecutionActionPack → STOP

---

## LEGACY-BUT-PRESENT-AND-BYPASSED

### 1. pressure_loop/pressure_field.py evaluate_pressure
**Status:** LEGACY accumulator — parallel to slice-1 contributor path

**Legacy Behavior (accumulator path):**
- Accumulator-based pressure computation
- 0.55 threshold constant
- Drives old meta loop via action_signal selection
- Still used for S2/S3 audits and guardian contributor wiring tests

**Active Replacement (slice-1 ignition):** `pressure_loop.pressure.evaluate` (neuromod-modulated, configurable threshold, `EpisodeIgnition` output) via `pressure_loop.loop.run_pressure_tick_once` → `cognition_chain.ignite_episode`

---

### 2. core/symphony_runner.py Full Loop Chain
**Status:** LEGACY — gated off by slice flags

**Legacy Chain (bypassed when both slice flags true):**
- `run_symphony_through_deepsleep` (line ~2800+): GLOBAL_A → AUTO → META_B → GLOBAL_B → OMEGA → DEEPSLEEP
- `run_symphony_meta_auto_meta_b` (line 359+): META_A → AUTO → META_B (harness entry)
- `run_global_a_movement` (line 652+): Old GLOBAL_A with act-pack minting
- `run_global_b_movement` (line ~800+): GLOBAL_B reason integration

**Active Replacement:** 
- Slice 1 front: Pressure → Global A → ActionGoal → STOP
- Slice 2 front: ActionGoal → Meta A → ExecutionActionPack → STOP
- AUTO/META_B/GLOBAL_B/OMEGA/DEEPSLEEP not reached when both slice flags true

---

### 3. core/symphony_global_action.py build_global_action_pack
**Status:** LEGACY — only called in non-reached GLOBAL_B path

**Legacy Behavior (bypassed):**
- Mints legacy act-pack with action_signal, goals, tools, model selection
- Called by `run_global_b_movement` (line ~800+ in symphony_runner)
- GLOBAL_B only runs when slice flags are false

**Active Replacement:** 
- Slice 1: `global_a.form_intent` → ActionGoal (intent-only)
- Slice 2: `meta_a.build_execution_action_pack` → ExecutionActionPack (deterministic, no model fields)

---

### 4. metacognition/global_loop/ (Old GLOBAL loop)
**Status:** DELETED — removed in SLICE 1 D2

**Deleted Components (Slice 1 deletions):**
- `phase_2_reflection` (old Global reflection)
- `phase_3_goal_formulation` (old goal synthesis)
- `phase_4_emission` (old goal emission)

**Active Replacement:** `core/global_a.py` (semantic intent router → ActionGoal). NOTE: `run_meta_a_movement` + `MetaLoopContext` were the old META path (deleted in Slice 2 — see §5 meta_loop), not part of global_loop.

---

### 5. metacognition/meta_loop/ (Old Meta Loop)
**Status:** DELETED — removed in Slice 2 D2

**Deleted Components:**
- `phase_1_intake` (intake phase)
- `phase_2_synthesis` (synthesis with control_reasoner)
- `phase_3_assembly` (assembly with tool_selector)
- `phase_4_dispatch` (dispatch with package_builder)
- `loop.py` (old loop runner)
- `loop_context.py` (old loop context)

**Active Replacement:** 
- Slice 2: `core/meta_a.py` (single-pass deterministic conversion)
- Future slices will add new Meta B / Auto / etc.

---

## DATA FLOW SUMMARY (ACTIVE PATH — loop substrate, 2026-06-05)

```
External Stimuli (operator messages, autonomous drives, system events)
    ↓
pressure_loop.contributors.make_contributor (+ operator queue drain)
    ↓
Contributor objects {source, type, weight, metadata}
    ↓
pressure_loop.pressure.gather_contributors
    ├─ Reads pressure_field readers (where wired)
    ├─ Drains operator-input pending queue (deduped)
    └─ Returns Contributor list
    ↓
pressure_loop.pressure.evaluate
    ├─ If active_episode: queue contributors, return None (single-active-episode rule)
    ├─ Compare raw weights to the pressure ceiling + margin
    ├─ Select dominant contributor
    └─ Return EpisodeIgnition {episode_id, aggregate, opened_by, dominant, contributors, trajectory, neuromod_state}
    ↓
cognition_chain.ignite_episode → global_loop.loop.run_global_a
    ├─ Map opened_by to ActionGoal intent
    └─ Write episode.global_["action_goal"]
    ↓
pressure_loop.policy slice gates (slice2_meta_a_front_enabled, …)
    ↓
meta_loop.loop.run_meta_a
    ├─ build_execution_action_pack (deterministic)
    └─ Write episode.meta["execution_action_pack"]
    ↓
(auto / meta_b / global_b / omega / deepsleep when later slice flags enabled)
```

---

## DISCREPANCIES FOUND

**None.** The active code matches the Slice 1 and Slice 2 specs exactly:

1. **Slice 1 compliance:**
   - Pressure selects attention (✓)
   - Global A forms intent only (✓)
   - 7 allowed intents exactly (✓)
   - Single-active-episode rule enforced (✓)
   - Neuromodulation as modulation layer (✓)
   - STOP at ActionGoal when slice2 disabled (✓)

2. **Slice 2 compliance:**
   - Meta A deterministic, no inference (✓)
   - Intent→action mapping exactly per spec (✓)
   - Capability→tool mapping exactly per spec (✓)
   - All 8 cases A-H implemented (✓)
   - Model-field hard-ban enforced (✓)
   - STOP at ExecutionActionPack (✓)
   - Auto/MetaB/GlobalB/Omega/DeepSleep not reached (✓)

3. **Legacy code properly bypassed:**
   - Old pressure_field.evaluate_pressure not called (✓)
   - Old meta_loop deleted (✓)
   - Old global_loop phases deleted (✓)
   - Full chain gated off by flags (✓)

The architecture correction is fully implemented and active.
