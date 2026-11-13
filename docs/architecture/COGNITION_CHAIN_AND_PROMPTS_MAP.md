# COGNITION CHAIN AND PROMPTS MAP

> **Historical snapshot — superseded 2026-08-03.** Windowed Omega and
> window-level DeepSleep references below are retired. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

**Work Order:** `#4102a20e` from claude  
**Date:** 2026-06-08  
**Scope:** READ-ONLY map — full cognition chain end-to-end + every model prompt + flags  
**Branch:** `main`

**See Also:**
- [`README.md`](README.md) — Architecture directory index
- [`ACTIVE_FRONT_MAP.md`](ACTIVE_FRONT_MAP.md) — Current active front mapping
- [`LIVE_CODEBASE_MAP.md`](LIVE_CODEBASE_MAP.md) — Live codebase structure
- [`PRESSURE_LOOP_CLOSURE_MAP.md`](PRESSURE_LOOP_CLOSURE_MAP.md) — Pressure loop closure architecture
- [AGENTS.md](../../AGENTS.md) — governing cognition cycle

---

## TABLE OF CONTENTS

1. [PART 1 — THE FULL COGNITION CHAIN](#part-1--the-full-cognition-chain)
2. [PART 2 — EVERY MODEL PROMPT IN THE CHAIN](#part-2--every-model-prompt-in-the-chain)
3. [PART 3 — FLAGS](#part-3--flags)

---

## PART 1 — THE FULL COGNITION CHAIN

### Executive Summary

The Omega 3.0 cognition chain executes as a **single cascading ignition sequence** triggered by pressure, flowing through 7 layers (Global A → Meta A → AUTO → Meta B → Global B → Omega → DeepSleep), with a **10-episode windowed reflection** (Omega + DeepSleep run once per locked window, not per-episode).

- **Per-episode path (episodes 1-9):** Pressure → Global A → Meta A → AUTO → Meta B → Global B → episode close → per-episode DeepSleep (truncated)
- **Window-level path (every 10th episode):** At episode 10 close, window locks → Omega reflection → window-level DeepSleep → archive window → reseed staging
- **Ignition cascade:** Each layer ignites the next via return values (no central loop); failures propagate and halt the system (fail-hard cognition)
- **Data carrier:** The `CognitiveEpisode` object carries all state through the cascade; `EpisodeStore` owns window lifecycle

---

### Sequencer: cognition_chain.py + pressure_loop/loop.py

#### Phase: Pressure Tick Loop
- **Entry:** `cognition_chain.py:28-34` (`run_symphony_through_deepsleep` wrapper) → `pressure_loop/loop.py:81-127` (`run_pressure_tick_loop`)
- **What it does:** Infinite loop that ticks every ~120s (configurable). Each tick gathers contributors, evaluates pressure, and on ignition hands off to cognition chain.
- **Handoff to next:** Returns `None` (loop continues); on ignition calls `ignite_episode(store, ignition)` → `cognition_chain.py:36`
- **Data reads/writes:**
  - **Reads:** `pressure_loop_pause_state()` (dev pill gate), `pressure_tick_interval_seconds()` (policy)
  - **Writes:** `shared_state._pressure_tick`, `shared_state._epoch`, `pressure_loop_paused` (pause state)

#### Phase: Gather Contributors
- **Entry:** `pressure_loop/loop.py:39-44` → `pressure_loop/pressure.py:gather_contributors()`
- **What it does:** Collects active contributors from pressure field (curiosity_gap, operator_command, outstanding_question, learning_debt, epistemic_emptiness, idle_probe_aggregate, operator_query, operator_response). Applies neuromod modulation (dopamine vigor, serotonin patience damp).
- **Handoff to next:** Returns `List[Contributor]` → evaluate phase
- **Data reads/writes:**
  - **Reads:** `_PENDING_QUEUE` (operator input queue), `_RESEED_STAGING` (DeepSleep reseed), `_RESEED_SUPPRESS` (Omega pressure feedback), pressure_field readers
  - **Writes:** Clears `_PENDING_QUEUE` after drain

#### Phase: Evaluate Pressure
- **Entry:** `pressure_loop/loop.py:46-66` → `pressure_loop/pressure.py:evaluate()`
- **What it does:** Determines if dominant contributor exceeds open threshold with margin over runner-up. Returns `EpisodeIgnition` (episode_id, aggregate_pressure, opened_by, dominant_contributor, contributors, trajectory_snapshot) or `None` (no ignition).
- **Handoff to next:** Returns `Optional[EpisodeIgnition]` → ignite_episode (if not None) or decay_neuromod_tonic (if None)
- **Data reads/writes:**
  - **Reads:** `pressure_open_threshold()`, `pressure_winner_ceiling()`, `pressure_winner_margin()`, `LAST_OMEGA_TRAJECTORY` (shared_state)
  - **Writes:** None (read-only evaluation)

---

### Orchestrator: cognition_chain.py

#### Phase: Ignite Episode (Entry to Cascade)
- **Entry:** `cognition_chain.py:36-92` (`ignite_episode`)
- **What it does:**
  1. **Check for window reflection:** `_maybe_run_window_reflection(store)` (line 38) — if a locked 10-episode window exists, run Omega + DeepSleep BEFORE opening new episode
  2. **Open episode:** `store.open(opened_by=ignition.opened_by, pressure=pressure_snapshot)` (line 49) → creates `CognitiveEpisode`
  3. **Cascade cognition:** `_start_global_a(store, episode, ignition)` (line 67) — ignites Global A
  4. **Close episode:** `store.close(cascade_status=cascade_status)` (line 82) — marks episode complete, appends enriched body to window
  5. **Per-episode DeepSleep:** `_maybe_run_per_episode_deepsleep(store)` (line 83) — truncated consolidation for THIS episode
  6. **Check for window reflection again:** `_maybe_run_window_reflection(store)` (line 84) — if window now locked, run Omega + DeepSleep
- **Handoff to next:** Returns closed `CognitiveEpisode` → pressure loop resumes
- **Data reads/writes:**
  - **Reads:** `slice2_meta_a_front_enabled()`, `slice3_auto_front_enabled()`, `slice4_meta_b_global_b_front_enabled()`, `slice5_omega_front_enabled()`, `slice6_deepsleep_front_enabled()` (policy gates)
  - **Writes:** `episode.global_['action_goal']`, `episode.meta['execution_action_pack']`, `episode.auto`, `episode.meta['reflection']`, `episode.global_['outcome']`, reflection window state

#### Phase: Maybe Run Window Reflection (Omega + DeepSleep Window Path)
- **Entry:** `cognition_chain.py:139-177` (`_maybe_run_window_reflection`)
- **What it does:**
  1. **Consume locked window:** `store.consume_window_ready()` (line 146) — returns locked 10-episode window or None
  2. **Run Omega:** `run_omega_for_window(window)` (line 156) — reflects over 10 episodes
  3. **Run window-level DeepSleep:** `run_deepsleep_for_window(store, window, omega_outcome, skip_episode_level_phases=True)` (line 164) — consolidates window
  4. **Archive and reset:** `store.archive_and_reset_window(window, omega_outcome=outcome_dict)` (line 177) — archives window to disk, clears maintained window
- **Handoff to next:** Returns `None` (side-effect only); resumes pressure loop
- **Data reads/writes:**
  - **Reads:** `window.episode_bodies` (10 enriched episodes), `window.trajectory_snapshot`, Omega/DeepSleep stores
  - **Writes:** `memory/600_journal/reflection_windows/{window_id}.json` (archive), clears `guardian/state/reflection_window.json`

---

### Layer 1: Global A (IGNITION → ACTION GOAL)

#### Phase: Ignition (Passthrough Validation)
- **Entry:** `global_loop/loop.py:37-38` → `global_loop/ignition/run.py:8-16`
- **What it does:** Validates `EpisodeIgnition` shape (opened_by, dominant_contributor present). Never rebuilds; passes through canonical pressure signal.
- **Handoff to next:** Returns `EpisodeIgnition` → intent phase
- **Data reads/writes:** None (validation only)

#### Phase: Intent (Opened By → ALLOWED_INTENT)
- **Entry:** `global_loop/loop.py:39-41` → `global_loop/intent/run.py:52-66`
- **What it does:** Maps `ignition.opened_by` (e.g., "operator_query", "curiosity_gap", "learning_debt") + dominant message to one of 7 ALLOWED_INTENTS: RESPOND, PROACTIVE_RESPONSE, RESOLVE_QUERY, LEARN, USE_TOOL, IMAGE_CREATE, IDLE.
- **Handoff to next:** Returns `str` (intent) → extract phase
- **Data reads/writes:** None (deterministic mapping)

#### Phase: Extract (Intent + Message → Target)
- **Entry:** `global_loop/loop.py:42-43` → `global_loop/extract/run.py:48-50`
- **What it does:** Extracts topic/target from dominant message (e.g., "learn quantum mechanics" → "quantum mechanics"). Respects explicit domain metadata from contributor.
- **Handoff to next:** Returns `Optional[str]` (target) → goal phase
- **Data reads/writes:** None (deterministic extraction)

#### Phase: Goal (Ignition + Intent + Target → ActionGoal)
- **Entry:** `global_loop/loop.py:44-45` → `global_loop/goal/run.py:10-20`
- **What it does:** Assembles `ActionGoal(episode_id, intent, target, confidence, reason)`. Confidence = dominant_contributor.weight (clamped 0-1).
- **Handoff to next:** Returns `ActionGoal` → writes to `episode.global_['action_goal']` (line 48-54 in loop.py)
- **Data reads/writes:**
  - **Writes:** `episode.global_['action_goal']` = {episode_id, intent, target, confidence, reason}

#### Phase: Ignite Meta A
- **Entry:** `cognition_chain.py:196-207` (`_start_meta_a`)
- **What it does:** Calls `run_meta_a(episode)` → cascades to Meta A phases
- **Handoff to next:** Returns `Optional[Dict]` (ExecutionActionPack) or raises on failure
- **Data reads/writes:** Reads `episode.global_['action_goal']`, writes `episode.meta['execution_action_pack']`

---

### Layer 2: Meta A (ACTION GOAL → EXECUTION ACTION PACK)

#### Phase: Intake (Episode → MetaAInput)
- **Entry:** `meta_loop/loop.py:41-44` → `meta_loop/intake/run.py:43-72`
- **What it does:** Extracts `MetaAInput(episode_id, pressure, trajectory_snapshot, action_goal)` from episode. Defensive extraction; returns None if no action_goal.
- **Handoff to next:** Returns `Optional[MetaAInput]` → action phase
- **Data reads/writes:**
  - **Reads:** `episode.global_['action_goal']`, `episode.pressure`, `episode.meta`
  - **Writes:** None

#### Phase: Action (Intent → Meta Action + Base Execution Fields)
- **Entry:** `meta_loop/loop.py:45-47` → `meta_loop/action/run.py:60-96`
- **What it does:** Maps intent (RESPOND, LEARN, USE_TOOL, etc.) to meta action (RESPONSE, USE_TOOL, IMAGE_CREATION, etc.). Initializes `MetaAState` with execution_intent, execution_inputs, constraints.
- **Handoff to next:** Returns `MetaAState` → tool phase
- **Data reads/writes:**
  - **Reads:** `mi.action_goal.intent`, contributor metadata
  - **Writes:** `state.action`, `state.execution_intent`, `state.execution_inputs`, `state.constraints`

#### Phase: Tool (Intent → Capability Tool)
- **Entry:** `meta_loop/loop.py:48-57` → `meta_loop/tool/run.py:43-169`
- **What it does:** Selects capability tool per intent (web_fetch for LEARN, file_reader for file tasks, vision_reasoning_tool for image analysis, etc.). For LEARN, resolves topic via `resolve_learning_topic()`.
- **Handoff to next:** Returns `MetaAState` → memory phase
- **Data reads/writes:**
  - **Reads:** `state.intent`, `state.text`, `state.target_str`, topic resolution
  - **Writes:** `state.tool`, `state.tool_reason`, `state.execution_inputs`

#### Phase: Memory (Intent → Memory Required + Scope)
- **Entry:** `meta_loop/loop.py:58-59` → `meta_loop/memory/run.py:80-134`
- **What it does:** Determines if memory recall is needed (e.g., RESPOND with "remember" keyword → memory_required=True). Sets memory_scope (operator_history, conversation, trajectory, identity, knowledge). Routes to MRIL tool (remember_knowledge, remember_identity, etc.).
- **Handoff to next:** Returns `MetaAState` → pack phase
- **Data reads/writes:**
  - **Reads:** `state.intent`, `state.text`, regex patterns for memory hints
  - **Writes:** `state.memory_required`, `state.memory_reason`, `state.memory_scope`, `state.memory_query`, `state.memory_tools`, `state.memory_delivery_mode`

#### Phase: Pack (MetaAState → ExecutionActionPack)
- **Entry:** `meta_loop/loop.py:60-61` → `meta_loop/pack/run.py:97-133`
- **What it does:** Assembles `ExecutionActionPack(episode_id, action, tool, memory_required, memory_scope, memory_tools, execution_intent, execution_inputs, execution_constraints, source_action_goal)`. Validates hard-ban on model/inference keys (planning_only=True, no_inference_in_meta=True, no_execution_in_meta=True, no_model_fields=True).
- **Handoff to next:** Returns `ExecutionActionPack` → writes to `episode.meta['execution_action_pack']` (line 64 in loop.py)
- **Data reads/writes:**
  - **Writes:** `episode.meta['execution_action_pack']` = {action, tool, tool_reason, memory_required, memory_reason, memory_scope, memory_query, memory_tools, memory_delivery_mode, execution_intent, execution_inputs, execution_constraints, source_action_goal}

#### Phase: Ignite AUTO
- **Entry:** `cognition_chain.py:210-233` (`_start_auto`)
- **What it does:** Calls `run_auto_front(episode)` → cascades to AUTO phases. After AUTO returns, computes neuromod (dopamine/serotonin modulation for next tick).
- **Handoff to next:** Returns `Optional[AutoOutcomePack]` or raises on failure
- **Data reads/writes:** Reads `episode.meta['execution_action_pack']`, writes `episode.auto`, `shared_state` neuromod salience

---

### Layer 3: AUTO (EXECUTION ACTION PACK → AUTO OUTCOME PACK)

#### Phase: Intake (Episode → AutoInput)
- **Entry:** `auto_loop/loop.py:63-66` → `auto_loop/intake/run.py:10-67`
- **What it does:** Extracts `AutoInput(episode_id, pressure, source_action_goal, execution_pack, trajectory_snapshot)` from episode. Returns None if no execution_action_pack.
- **Handoff to next:** Returns `Optional[AutoInput]` → validate phase
- **Data reads/writes:**
  - **Reads:** `episode.meta['execution_action_pack']`, `episode.global_['action_goal']`, `episode.pressure`

#### Phase: Validate (ExecutionActionPack Hard-Ban)
- **Entry:** `auto_loop/loop.py:71-82` → `auto_loop/validate/run.py:10-57`
- **What it does:** Reuses Meta A hard-ban validation (no model/inference keys). Returns (ok, failure_kind, failure_reason). If invalid, builds failure outcome and returns early.
- **Handoff to next:** Returns `(bool, Optional[str], Optional[str])` → recall phase (if ok) or fold phase (if not ok)
- **Data reads/writes:**
  - **Reads:** `ai.execution_pack`
  - **Writes:** None (validation only)

#### Phase: Recall (ExecutionActionPack → MemoryEnvelope)
- **Entry:** `auto_loop/loop.py:84-87` → `auto_loop/recall/run.py:10-67`
- **What it does:** Builds pre-execute `MemoryEnvelope(memory_required, memory_status, memory_reason, memory_scope, items, error)`. If memory_required=True, status="pending" (actual lobe reads happen in execute via MRIL tools). If False, status="skipped".
- **Handoff to next:** Returns `MemoryEnvelope` → promptpack phase
- **Data reads/writes:**
  - **Reads:** `ai.execution_pack.memory_required`, `ai.execution_pack.memory_scope`
  - **Writes:** None (envelope only)

#### Phase: PromptPack (ExecutionActionPack + MemoryEnvelope → PromptPack)
- **Entry:** `auto_loop/loop.py:89-94` → `auto_loop/promptpack/run.py:25-177`
- **What it does:** Deterministically constructs `PromptPack(prompt_type, response_mode, system_prompt, user_prompt)` from execution_pack, memory_envelope, and pressure. Prompt type = action + memory_required (e.g., RESPONSE_REASONING, TOOL_CALL_SYSTEM, IMAGE_SYSTEM).
- **Handoff to next:** Returns `PromptPack` → route phase
- **Data reads/writes:**
  - **Reads:** `ai.execution_pack`, `memory_envelope`, `ai.pressure`, system prompt policy
  - **Writes:** None (deterministic construction)

#### Phase: Route (PromptPack → Model Profile)
- **Entry:** `auto_loop/loop.py:96-104` → `auto_loop/route/run.py:26-94`
- **What it does:** Selects model profile (TEXT_FAST, TEXT_REASONING, TOOL_EXECUTOR, IMAGE_GENERATION, DOCUMENT_GENERATION) based on prompt_type and memory_enriched flag.
- **Handoff to next:** Returns `Optional[str]` (model_profile) → execute phase
- **Data reads/writes:**
  - **Reads:** `prompt_pack.prompt_type`, `memory_enriched`, `auto_web_fetch_requires_query()` policy
  - **Writes:** None (deterministic routing)

#### Phase: Execute (PromptPack + Model Profile → Execution Result)
- **Entry:** `auto_loop/loop.py:106-121` → `auto_loop/execute/run.py:55-946`
- **What it does:**
  1. **Model inference:** Calls runtime client (omega-portable-lite at :11434) with system/user prompts → generates response
  2. **Tool invocation:** If action=USE_TOOL and tool specified, invokes tool (web_fetch, file_reader, vision_reasoning_tool, etc.)
  3. **MRIL memory recall:** If memory_required=True, executes remember_* tool (remember_knowledge, remember_identity, etc.) → populates memory_envelope.items
  4. **Image generation:** If action=IMAGE_CREATE, calls image generation model
  5. **Failure handling:** Catches model/tool failures → returns failure_kind (invalid_execution_pack, model_error, tool_error, etc.)
- **Handoff to next:** Returns `Dict[str, Any]` (exec_result with outputs, failure_kind, model_id, inference_latency_s, tokens, memory_envelope) → fold phase
- **Data reads/writes:**
  - **Reads:** `prompt_pack.system_prompt`, `prompt_pack.user_prompt`, `ai.execution_pack`, runtime client
  - **Writes:** `memory_envelope.items` (MRIL recall results), `memory_envelope.memory_status` (success/failed)

#### Phase: Fold (AutoInput + Execution Result → Episode.auto)
- **Entry:** `auto_loop/loop.py:139-150` → `auto_loop/fold/run.py:24-171`
- **What it does:** Folds execution result into `episode.auto` dict with telemetry (status, cycle_kind, failure_kind, model_id, runtime_ms, tokens, memory, prompt_pack hash, outputs). Determines cycle_kind (success, fail, skipped) from status.
- **Handoff to next:** Returns `AutoOutcomePack` → writes to `episode.auto` (line 151 in loop.py)
- **Data reads/writes:**
  - **Writes:** `episode.auto` = {episode_id, action, status, failure_kind, failure_reason, memory, model_profile, model_id, prompt_pack, outputs, telemetry}

#### Phase: Ignite Meta B
- **Entry:** `cognition_chain.py:236-242` (`_start_meta_b_global_b`)
- **What it does:** Calls `run_meta_b(episode)` → cascades to Meta B phases
- **Handoff to next:** Returns `Optional[GlobalOutcomePack]` or raises on failure
- **Data reads/writes:** Reads `episode.auto`, writes `episode.meta['reflection']`, `episode.global_['outcome']`

---

### Layer 4: Meta B (AUTO OUTCOME → META REFLECTION PACK)

#### Phase: Consistency (Episode → MetaBInput + Consistency Check)
- **Entry:** `meta_loop/loop.py:86-89` → `meta_loop/consistency/run.py:36-160`
- **What it does:** Extracts `MetaBInput(episode_id, pressure, source_action_goal, execution_pack, auto_outcome)` from episode. Validates packet consistency (action_goal fields stable, execution_pack fields stable, auto_outcome present).
- **Handoff to next:** Returns `(MetaBInput, dict)` (consistency check results) → disposition phase
- **Data reads/writes:**
  - **Reads:** `episode.global_['action_goal']`, `episode.meta['execution_action_pack']`, `episode.auto`

#### Phase: Disposition (Consistency + Auto Cycle Kind → Disposition + Status)
- **Entry:** `meta_loop/loop.py:91-93` → `meta_loop/disposition/run.py:40-58`
- **What it does:** Maps auto.cycle_kind (success, fail, skipped) → (disposition, meta_status). Disposition ∈ {completed, degraded, failed, skipped, invalid}. Meta status ∈ {success, partial, failed, skipped, invalid}.
- **Handoff to next:** Returns `(str, str)` (disposition, status) → reflect phase
- **Data reads/writes:**
  - **Reads:** `mbi.auto_outcome.cycle_kind`, `meta_b_cycle_kind_map()` policy

#### Phase: Reflect (MetaBInput + Consistency + Disposition → MetaReflectionPack)
- **Entry:** `meta_loop/loop.py:94-95` → `meta_loop/reflect/run.py:39-82`
- **What it does:** Assembles `MetaReflectionPack(episode_id, consistency, execution, trace_summary)` with execution block (disposition, status, memory_status, action, warnings).
- **Handoff to next:** Returns `MetaReflectionPack` → writes to `episode.meta['reflection']` (line 98-99 in loop.py)
- **Data reads/writes:**
  - **Writes:** `episode.meta['reflection']` = {consistency, execution: {disposition, status, memory_status, action, warnings}, trace_summary}

---

### Layer 5: Global B (META REFLECTION → GLOBAL OUTCOME PACK)

#### Phase: Assemble (Episode + MetaReflection → GlobalBInput)
- **Entry:** `global_loop/loop.py:81-84` → `global_loop/assemble/run.py:82-83`
- **What it does:** Extracts `GlobalBInput(episode_id, pressure, source_action_goal, execution_pack, auto_outcome, meta_reflection)` from episode + meta reflection. Returns None if no auto_outcome.
- **Handoff to next:** Returns `Optional[GlobalBInput]` → goal_state phase
- **Data reads/writes:**
  - **Reads:** `episode.global_['action_goal']`, `episode.meta['execution_action_pack']`, `episode.auto`, `episode.meta['reflection']`, `episode.pressure`

#### Phase: Goal State (Disposition → Goal State)
- **Entry:** `global_loop/loop.py:88-89` → `global_loop/goal_state/run.py:26-27`
- **What it does:** Maps disposition (completed, degraded, failed, skipped, invalid) → goal_state (1:1 mapping).
- **Handoff to next:** Returns `str` (goal_state) → ground phase
- **Data reads/writes:**
  - **Reads:** `disposition` from meta_reflection.execution

#### Phase: Ground (Auto Outcome + Meta Reflection → Grounded + Grounding Basis)
- **Entry:** `global_loop/loop.py:95-96` → `global_loop/ground/run.py:10-31`
- **What it does:** Checks existence of auto outputs (response_text, tool_result, image_artifact) and meta disposition. Grounded = bool(basis) AND disposition is not None. Grounding basis = list of paths where evidence exists.
- **Handoff to next:** Returns `(bool, List[str])` (grounded, grounding_basis) → summarize phase
- **Data reads/writes:**
  - **Reads:** `gbi.auto_outcome.auto.outputs`, `gbi.meta_reflection.execution.disposition`

#### Phase: Summarize (Goal State + Action + Failure Kind → Summary)
- **Entry:** `global_loop/loop.py:97-98` → `global_loop/summarize/run.py:8-34`
- **What it does:** Generates deterministic summary string based on goal_state and action (e.g., "Execution completed successfully." for completed, "Tool execution failed: {failure_kind}" for failed).
- **Handoff to next:** Returns `str` (summary) → followups phase
- **Data reads/writes:**
  - **Reads:** `goal_state`, `action`, `failure_kind`, summary templates from policy

#### Phase: Followups (Goal State + Failure Kind + Action → Followup List)
- **Entry:** `global_loop/loop.py:99-100` → `global_loop/followups/run.py:8-88`
- **What it does:** Generates non-executable followup suggestions (e.g., "needs_clarification" for failed clarification, "needs_retry" for tool failure, "none" for completed/skipped).
- **Handoff to next:** Returns `List[dict]` (followups) → package phase
- **Data reads/writes:**
  - **Reads:** `goal_state`, `failure_kind`, `action`, followup rules from policy

#### Phase: Package (GlobalBInput + Goal State + Grounding + Summary + Followups → GlobalOutcomePack)
- **Entry:** `global_loop/loop.py:102-111` → `global_loop/package/run.py:26-108`
- **What it does:** Assembles `GlobalOutcomePack(episode_id, goal, outcome, observations, artifacts, followups, consistency)` with outcome block (goal_state, grounded, grounding_basis, summary, failure_kind, failure_reason).
- **Handoff to next:** Returns `GlobalOutcomePack` → writes to `episode.global_['outcome']` (line 111 in loop.py)
- **Data reads/writes:**
  - **Writes:** `episode.global_['outcome']` = {goal_state, grounded, grounding_basis, summary, failure_kind, failure_reason, observations, artifacts, followups, consistency}

---

### Episode Close & Window Management

#### Phase: Episode Close (EpisodeStore.close)
- **Entry:** `cognition_chain.py:82` → `pressure_loop/episode.py:353-480` (`EpisodeStore.close`)
- **What it does:**
  1. **Mark complete:** Sets `episode.state = "complete"`, `episode.cascade_status = cascade_status`
  2. **Reset pressure:** Calls `reset_pressure_accumulator()` (pressure field drive reset)
  3. **Archive episode:** Calls `archive_episode(ep)` → appends PriorEpisode JSONL record to `memory/600_journal/episodes.jsonl`
  4. **Build enriched body:** Calls `enriched_episode_from_episode(ep)` → creates dict with intent, action, outcome, pressure, execution, brain_turns, topic_history, open_query_state, cascade_status, neuromod
  5. **Append to window:** Calls `_append_episode_to_reflection_window(episode_id, cascade_status, body)` → appends enriched body to maintained window; locks window at 10 episodes
- **Handoff to next:** Returns closed `CognitiveEpisode` (or None if no active)
- **Data reads/writes:**
  - **Reads:** `episode.global_['outcome']`, `episode.auto['neuromod']`, `episode.pressure`
  - **Writes:** `episode.state`, `episode.cascade_status`, `episode.closed_at`, `memory/600_journal/episodes.jsonl`, `guardian/state/reflection_window.json` (window state)

#### Phase: Append Episode to Reflection Window
- **Entry:** `pressure_loop/episode.py:657-689` (`_append_episode_to_reflection_window`)
- **What it does:**
  1. **Load or create window:** If no window exists, creates new `ReflectionWindow` via `open_reflection_window(episode_id, trajectory_snapshot=read_trajectory())`
  2. **Append episode:** Calls `append_episode_to_window(window, episode_id, cascade_status, body)` → appends episode_id + enriched body
  3. **Lock at 10:** When window.episode_count >= 10, sets window.status = STATUS_LOCKED
  4. **Save window:** Persists window to `guardian/state/reflection_window.json`
  5. **Set ready flag:** If window.is_locked() and window.episode_count >= 10, sets `self._window_ready = True`
- **Data reads/writes:**
  - **Reads:** `guardian/state/reflection_window.json` (load), `guardian/state/current_trajectory.json` (trajectory snapshot)
  - **Writes:** `guardian/state/reflection_window.json` (window state with episodes, episode_bodies, cascade_status_by_episode, episode_mastery_deltas)

---

### Layer 6: Omega (Window-Level Reflection, Every 10th Episode)

#### Trigger: Window Locked
- **Condition:** `EpisodeStore.window_ready == True` (set when 10th episode appended to window)
- **Entry:** `cognition_chain.py:139-177` (`_maybe_run_window_reflection`)

#### Phase: Omega Intake (Locked Window → OmegaInput)
- **Entry:** `omega_loop/loop.py:37-38` → `omega_loop/intake/run.py:43-67`
- **What it does:** Builds `OmegaInput(window_id, trajectory_snapshot, episode_window, trajectory_diff, current_mastery_state, open_questions_summary, mastery_changes)` from locked window. Computes trajectory_diff (what changed in the 10-episode window). Loads current mastery state + open questions summary.
- **Handoff to next:** Returns `OmegaInput` → reason phase
- **Data reads/writes:**
  - **Reads:** `window.episode_bodies` (10 enriched episodes), `window.trajectory_snapshot`, `load_mastery()`, `load_queries()`, `_load_all()` (open questions)

#### Phase: Omega Reason (OmegaInput → Raw Reasoning Text)
- **Entry:** `omega_loop/loop.py:39-44` → `omega_loop/reason/run.py:14-128`
- **What it does:** Calls model (METACOGNITIVE_REASONING profile) with system + user prompts built from OmegaInput. Prompts include: 10-episode summary, trajectory snapshot, mastery state, open questions. Returns raw model text (JSON-formatted reasoning).
- **Handoff to next:** Returns `Optional[str]` (raw model output) → reflect phase
- **Data reads/writes:**
  - **Reads:** `omega_input` (all fields), model runtime client
  - **Writes:** None (read-only inference)

#### Phase: Omega Reflect (Raw Reasoning → OmegaReflection + TrajectoryUpdate)
- **Entry:** `omega_loop/loop.py:45-46` → `omega_loop/reflect/run.py:reflect()`
- **What it does:** Parses model JSON output → `OmegaReflection(reasoning, emergent_observations, open_questions, pressure_feedback)` + `TrajectoryUpdate(target_domain, target_capability, rationale)`. Validates JSON structure; fail-hard on parse failure.
- **Handoff to next:** Returns `(OmegaReflection, TrajectoryUpdate)` → package phase
- **Data reads/writes:**
  - **Reads:** `raw` (model output), `omega_input` (for validation context)

#### Phase: Omega Package (OmegaReflection + TrajectoryUpdate → OmegaOutcomePack + Lobe 9 Write)
- **Entry:** `omega_loop/loop.py:47-54` → `omega_loop/package/run.py:10-55`
- **What it does:**
  1. **Build pack:** Creates `OmegaOutcomePack(window_id, reflection, trajectory_update, trajectory_diff)`
  2. **Write episodes to lobe 9:** Calls `write_episode_to_temporal(body)` for each episode in window → writes to `memory/lobe_9/episodes/{episode_id}.json`
  3. **Write trajectory to lobe 9:** Calls `write_trajectory(traj)` → writes to `memory/lobe_9/trajectory.json`
- **Handoff to next:** Returns `OmegaOutcomePack` → DeepSleep intake
- **Data reads/writes:**
  - **Reads:** `reflection`, `trajectory_update`, `episode_bodies`
  - **Writes:** `memory/lobe_9/episodes/{episode_id}.json` (10 episodes), `memory/lobe_9/trajectory.json` (new trajectory)

---

### Layer 7: DeepSleep (Per-Episode + Window-Level)

#### Per-Episode DeepSleep (Truncated Consolidation)
- **Entry:** `cognition_chain.py:95-136` (`_maybe_run_per_episode_deepsleep`)
- **What it does:** Runs truncated DeepSleep consolidation for a single closed episode (NOT the window). Phases: intake → mastery → failure → semantic → consolidate → consolidate_memory. Skips identity, trajectory, query_lifecycle, projection, reseed, closure (window-only phases).
- **Handoff to next:** Returns `Optional[DeepSleepOutcomePack]` or None
- **Data reads/writes:**
  - **Reads:** `store.last_closed_body()` (enriched episode body)
  - **Writes:** Mastery store, failure store, semantic store, memory lobes (via consolidate_to_memory)

#### Window-Level DeepSleep (Every 10th Episode)
- **Entry:** `cognition_chain.py:164` → `deepsleep_loop/loop.py:37-310` (`run_deepsleep_for_window`)
- **What it does:** Full DeepSleep consolidation over the 10-episode window. Phases: intake → mastery → identity → semantic → trajectory → failure → query_lifecycle → consolidate → projection → consolidate_memory → reseed → closure.
- **Handoff to next:** Returns `DeepSleepOutcomePack` → archive_and_reset_window
- **Data reads/writes:**
  - **Reads:** `window.episode_bodies`, `window.trajectory_snapshot`, `omega_outcome`
  - **Writes:** Mastery store, identity store, semantic store, trajectory store, failure store, query store, projection store, memory lobes, `_RESEED_STAGING`, `_RESEED_SUPPRESS`

---

### Data Flow Summary

#### Shared State (core/shared_state.py)
- **Reads:** `_pressure_tick`, `_epoch`, `pressure_loop_paused`, `LAST_OMEGA_TRAJECTORY`, `LAST_INGRESS_PRESSURE`
- **Writes:** `_pressure_tick`, `_epoch`, `pressure_loop_paused`, `_pressure_loop_pause`

#### Episode Store (pressure_loop/episode.py)
- **Maintains:** Active `CognitiveEpisode`, reflection window (10-episode maintained state), last_closed_body
- **Lifecycle:** open() → close() → archive_and_reset_window() (every 10 episodes)

#### Memory Lobes
- **Lobe 200 (Knowledge):** Semantic entries, knowledge artifacts
- **Lobe 400 (Mastery):** Domain mastery records (knowledge_mastery, application_mastery)
- **Lobe 600 (Journal):** Episode journal entries (intent, action, outcome, omega_reasoning)
- **Lobe 700 (Identity):** Identity traits (name, strength, evidence_count)
- **Lobe 9 (Temporal):** Omega-reflected episodes + trajectory (written by Omega.package)

#### Stores (deepsleep_loop/)
- **mastery_store.py:** `{domain_id: MasteryRecord}`
- **failure_store.py:** `{failure_key: FailurePattern}`
- **semantic_store.py:** `{domain: SemanticEntry}`
- **identity_store.py:** `{trait_key: Trait}`
- **trajectory_store.py:** `{trajectory_key: TrajectoryRecord}`
- **query_store.py:** `{query_key: OpenQuery}`

#### Reflection Window (pressure_loop/reflection_window.py)
- **Location:** `guardian/state/reflection_window.json` (in-memory, crash-persisted)
- **Lifecycle:** Created on first episode close, appends episodes 1-10, locks at 10, consumed by Omega, archived to `memory/600_journal/reflection_windows/{window_id}.json`
- **Contents:** `window_id`, `started_at_episode`, `trajectory_snapshot`, `episodes[]`, `episode_bodies[]` (enriched), `cascade_status_by_episode`, `episode_mastery_deltas`, `status` (COLLECTING/LOCKED)

#### Episode Journal (pressure_loop/episode_journal.py)
- **Location:** `memory/600_journal/episodes.jsonl` (append-only JSONL)
- **Per-episode entry:** `PriorEpisode(episode_id, ignition_reason, intent, action, outcome, trajectory_update, cascade_status)`
- **Enriched body:** `{episode_id, intent, action, outcome, pressure, execution, brain_turns, topic_history, open_query_state, cascade_status, neuromod}`

---

### Key File References

| Phase | File | Function | Line |
|-------|------|----------|------|
| Pressure Tick | `pressure_loop/loop.py` | `run_pressure_tick_loop` | 81 |
| Gather Contributors | `pressure_loop/pressure.py` | `gather_contributors` | ~120 |
| Evaluate Pressure | `pressure_loop/pressure.py` | `evaluate` | ~200 |
| Ignite Episode | `cognition_chain.py` | `ignite_episode` | 36 |
| Maybe Run Window Reflection | `cognition_chain.py` | `_maybe_run_window_reflection` | 139 |
| Global A Ignition | `global_loop/ignition/run.py` | `run` | 8 |
| Global A Intent | `global_loop/intent/run.py` | `run` | 52 |
| Global A Extract | `global_loop/extract/run.py` | `run` | 48 |
| Global A Goal | `global_loop/goal/run.py` | `run` | 10 |
| Meta A Intake | `meta_loop/intake/run.py` | `run` | 71 |
| Meta A Action | `meta_loop/action/run.py` | `run` | 60 |
| Meta A Tool | `meta_loop/tool/run.py` | `run` | 43 |
| Meta A Memory | `meta_loop/memory/run.py` | `run` | 80 |
| Meta A Pack | `meta_loop/pack/run.py` | `run` | 97 |
| AUTO Intake | `auto_loop/intake/run.py` | `run` | 66 |
| AUTO Validate | `auto_loop/validate/run.py` | `run` | 56 |
| AUTO Recall | `auto_loop/recall/run.py` | `memory_envelope_before_execute` | 10 |
| AUTO PromptPack | `auto_loop/promptpack/run.py` | `build_prompt_pack` | 25 |
| AUTO Route | `auto_loop/route/run.py` | `select_model_profile` | 26 |
| AUTO Execute | `auto_loop/execute/run.py` | `execute` | ~400 |
| AUTO Fold | `auto_loop/fold/run.py` | `_fold_episode_auto` | 24 |
| Meta B Consistency | `meta_loop/consistency/run.py` | `meta_b_input_from_episode` | 36 |
| Meta B Disposition | `meta_loop/disposition/run.py` | `derive_disposition` | 40 |
| Meta B Reflect | `meta_loop/reflect/run.py` | `run` | 39 |
| Global B Assemble | `global_loop/assemble/run.py` | `run` | 82 |
| Global B Goal State | `global_loop/goal_state/run.py` | `run` | 26 |
| Global B Ground | `global_loop/ground/run.py` | `run` | 10 |
| Global B Summarize | `global_loop/summarize/run.py` | `run` | 8 |
| Global B Followups | `global_loop/followups/run.py` | `run` | 8 |
| Global B Package | `global_loop/package/run.py` | `run` | 26 |
| Episode Close | `pressure_loop/episode.py` | `EpisodeStore.close` | 353 |
| Append to Window | `pressure_loop/episode.py` | `_append_episode_to_reflection_window` | 657 |
| Per-Episode DeepSleep | `deepsleep_loop/loop.py` | `run_deepsleep_for_episode` | 60 |
| Omega Intake | `omega_loop/intake/run.py` | `build_omega_input_for_window` | 43 |
| Omega Reason | `omega_loop/reason/run.py` | `reason` | 14 |
| Omega Reflect | `omega_loop/reflect/run.py` | `reflect` | ~60 |
| Omega Package | `omega_loop/package/run.py` | `package_window` | 10 |
| Window DeepSleep | `deepsleep_loop/loop.py` | `run_deepsleep_for_window` | 37 |
| DeepSleep Mastery | `deepsleep_loop/mastery/run.py` | `update_mastery` | ~50 |
| DeepSleep Identity | `deepsleep_loop/identity/run.py` | `update_identity` | ~50 |
| DeepSleep Semantic | `deepsleep_loop/semantic/run.py` | `promote_semantic` | 30 |
| DeepSleep Trajectory | `deepsleep_loop/trajectory/run.py` | `maintain_trajectory` | ~50 |
| DeepSleep Failure | `deepsleep_loop/failure/run.py` | `consolidate_failures` | ~100 |
| DeepSleep Query | `deepsleep_loop/query_lifecycle/run.py` | `run_query_lifecycle` | ~100 |
| DeepSleep Consolidate | `deepsleep_loop/consolidate/run.py` | `build_journal_entry` | 17 |
| DeepSleep Consolidate Memory | `deepsleep_loop/consolidate_memory/run.py` | `consolidate_to_memory` | ~100 |
| DeepSleep Reseed | `deepsleep_loop/reseed/run.py` | `generate_reseed` | ~150 |
| DeepSleep Closure | `deepsleep_loop/closure/run.py` | `finalize_window` | 11 |
| Archive Window | `pressure_loop/episode.py` | `archive_and_reset_window` | 628 |

---

### Failure Modes & Halt Conditions

**Fail-Hard Cognition (Operator Directive: "cognition crashes the system")**

1. Episode open fails → RuntimeError raised, cascade halts
2. Global A produces no ActionGoal → RuntimeError raised, cascade halts
3. Meta A produces no ExecutionActionPack → RuntimeError raised, cascade halts
4. AUTO produces no AutoOutcomePack → RuntimeError raised, cascade halts
5. Omega intake fails → RuntimeError raised, window reflection halts
6. Omega reason fails → RuntimeError raised, window reflection halts
7. Omega reflect fails → RuntimeError raised, window reflection halts
8. DeepSleep intake fails → RuntimeError raised, consolidation halts
9. DeepSleep mastery/identity/semantic/trajectory/failure/query phases fail → RuntimeError raised, consolidation halts

**Failure-Soft (Non-Fatal, Logged)**

1. Episode journal archive fails → logged, cascade continues
2. Reflection window append fails → logged, cascade continues
3. Pressure accumulator reset fails → logged, cascade continues
4. Neuromod tonic decay fails → logged, cascade continues
5. Per-episode DeepSleep fails → logged, cascade continues
6. Telemetry emit fails → logged, cascade continues

---

## PART 2 — EVERY MODEL PROMPT IN THE CHAIN

### Executive Summary

Only **two layers** in the cognition chain invoke models: **AUTO** (synthesis, tool dispatch, image generation) and **Omega** (windowed reflection). All other layers (Global A/B, Meta A/B, DeepSleep) are deterministic. Memory operations (classify, embed) and HUD services (image, face, STT) also invoke models but are outside the core cognition cascade.

The `RuntimeClient` class (singleton via `get_runtime_client()`) at `runtime/client.py` provides all model invocation methods. Base URL: `http://127.0.0.1:11434` (configurable via `OMEGA_RUNTIME_URL` env or `config/runtime.yaml`).

---

### Section 1: Runtime Client Methods (runtime/client.py)

#### 1.1 Text Generation

**Call Site:** `runtime/client.py:472-544`
- **Method:** `generate(prompt, system, max_tokens, temperature, top_p, top_k, stop, tools, chat_template_kwargs, enable_thinking, timeout)`
- **Capability:** `coding_model`
- **Runtime Slot:** `gemma_e4b` (per `config/runtime.yaml:65`)
- **Endpoint:** `/generate` (POST)
- **Default Params:**
  - `max_tokens`: None (runtime-owned)
  - `temperature`: None (runtime-owned)
  - `top_p`: None (runtime-owned)
  - `enable_thinking`: False (P3-S32: disabled by default to avoid reasoning timeouts)
  - `timeout`: 60s (TIMEOUTS["generate"])
- **Output:** `Optional[str]` (text only)
- **Telemetry:** `emit_inference("runtime", "generate", latency_ms, ok, status)`
- **Busy Tracking:** Wrapped in `_runtime_busy_scope(operation="generate")` (P3-S31)

**Call Site:** `runtime/client.py:546-608`
- **Method:** `generate_completion(prompt, system, max_tokens, temperature, top_p, top_k, stop, tools, chat_template_kwargs, enable_thinking, timeout)`
- **Capability:** `coding_model`
- **Runtime Slot:** `gemma_e4b`
- **Endpoint:** `/generate` (POST)
- **Default Params:** Same as `generate()` above
- **Output:** `Optional[dict]` (full response including `tool_calls`, `finish_reason`, etc.)
- **Telemetry:** `emit_inference("runtime", "generate_completion", latency_ms, ok, status, tool_calls)`

**Call Site:** `runtime/client.py:610-691`
- **Method:** `generate_stream(prompt, system, max_tokens, temperature, top_p, top_k, stop, chat_template_kwargs, enable_thinking, timeout)`
- **Capability:** `coding_model`
- **Runtime Slot:** `gemma_e4b`
- **Endpoint:** `/generate/stream` (POST with SSE)
- **Output:** `Iterator[str]` (token-by-token streaming)
- **Error Handling:** Raises `RuntimeStreamError` on server `event: error`

#### 1.2 Classification (Hierarchical)

**Call Site:** `runtime/client.py:309-353`
- **Method:** `classify_hierarchical(text, force_lobe=None)`
- **Capability:** `classify`
- **Runtime Slot:** `setfit_lobe` (primary); `setfit_knowledge`, `setfit_coding`, `setfit_identity`, `setfit_situational`, `setfit_workflows` (per-lobe specialists)
- **Endpoint:** `/classify` (POST)
- **Params:** `text`: query string; `force_lobe`: Optional[int] 0-11
- **Default Timeout:** 5s
- **Output:** `ClassifyResult(ok, coord, label, confidence, tokens, stages, latency_ms, reason)`
- **Fallback:** Always returns `coord` (defaults to `[5,0]` on failure)
- **Telemetry:** `emit_inference("memory", "classify_hierarchical", latency_ms, ok, tokens, status, error)`

#### 1.3 Embedding

**Call Site:** `runtime/client.py:377-398`
- **Method:** `embed(text)`
- **Capability:** `embedding`
- **Runtime Slot:** `embedding_gemma`
- **Endpoint:** `/embed` (POST)
- **Default Timeout:** 5s
- **Output:** `Optional[list[float]]` (embedding vector)
- **Telemetry:** `emit_inference("memory", "embed", latency_ms, ok, dim, error)`

**Call Site:** `runtime/client.py:423-448`
- **Method:** `embed_batch(texts)`
- **Capability:** `embedding`
- **Runtime Slot:** `embedding_gemma`
- **Endpoint:** `/embed` (POST)
- **Output:** `Optional[list[list[float]]]` (batch of vectors)
- **Telemetry:** `emit_inference("memory", "embed_batch", latency_ms, ok, batch_size)`

#### 1.4 Reranking (Retired)

**Call Site:** `runtime/client.py:450-470`
- **Method:** `rerank(query, documents, top_n)`
- **Status:** Retired (BGE removed)
- **Output:** Returns None (endpoint returns `ok=false`)
- **Telemetry:** `emit_inference("memory", "rerank", ok=False, reason="rerank_unavailable", doc_count)`

#### 1.5 Speech-to-Text (STT)

**Call Site:** `runtime/client.py:693-712`
- **Method:** `transcribe(audio_bytes, audio_format="wav", language="auto", vad_filter=True)`
- **Capability:** `stt`
- **Runtime Slot:** `whisper_small_en`
- **Endpoint:** `/transcribe` (POST)
- **Default Timeout:** 30s
- **Output:** `Optional[str]` (transcribed text)

#### 1.6 Voice Command (Audio-In, Action-Out)

**Call Site:** `runtime/client.py:714-752`
- **Method:** `voice_command(audio_bytes, audio_format="wav", tools=None, system=None, max_tokens=None, temperature=None)`
- **Capability:** `voice`
- **Runtime Slot:** `jackrong_qwen`
- **Endpoint:** `/voice/command` (POST)
- **Default Timeout:** 60s
- **Output:** `Optional[dict]` with keys: `{transcript, intent, tool_calls, text, language, latency_ms}`

#### 1.7 Text-to-Speech (TTS)

**Call Site:** `runtime/client.py:754-773`
- **Method:** `synthesise(text, voice="jarvis", length_scale=1.0)`
- **Capability:** `tts`
- **Runtime Slot:** `jarvis_piper`
- **Endpoint:** `/synthesise` (POST)
- **Default Timeout:** 10s
- **Output:** `Optional[bytes]` (audio bytes, base64-decoded)

#### 1.8 Face Recognition & Enrollment

**Call Site:** `runtime/client.py:775-791`
- **Method:** `face_identify(image_bytes, image_format="jpeg")`
- **Capability:** `face`
- **Runtime Slots:** `face_det_10g`, `face_w600k_r50`, `face_2d106det`, `face_1k3d68`, `face_genderage`
- **Endpoint:** `/face/identify` (POST)
- **Default Timeout:** 10s
- **Output:** `Optional[dict]` with keys: `{name, confidence, landmarks, bbox, embedding (512-dim float list)}`

**Call Site:** `runtime/client.py:793-807`
- **Method:** `face_enroll(image_bytes, name)`
- **Endpoint:** `/face/enroll` (POST)
- **Output:** `bool` (success/failure)

**Call Site:** `runtime/client.py:809-821`
- **Method:** `face_landmarks(image_bytes, kind="2d106")`
- **Endpoint:** `/face/landmarks` (POST)
- **Output:** `Optional[dict]` (landmark coordinates)

#### 1.9 Image Generation

**Call Site:** `runtime/client.py:823-853`
- **Method:** `image_generate(prompt, width=512, height=512, steps=4, seed=None)`
- **Capability:** `image_generation`
- **Runtime Slot:** `sd_turbo`
- **Endpoint:** `/image/generate` (POST)
- **Default Timeout:** 120s
- **Output:** `Optional[bytes]` (image bytes, base64-decoded)

#### 1.10 Vision Reasoning (Image Description)

**Call Site:** `runtime/client.py:855-883`
- **Method:** `vision_describe(image_bytes, prompt=None, image_format="jpeg", max_tokens=None, temperature=None)`
- **Capability:** `specialist_vision`
- **Runtime Slot:** `cosmos_reason2b`
- **Endpoint:** `/vision/describe` (POST)
- **Default Timeout:** 30s
- **Output:** `Optional[str]` (description text)

#### 1.11 Document Extraction

**Call Site:** `runtime/client.py:885-912`
- **Method:** `document_extract(file_bytes, filename="document", document_format="auto")`
- **Endpoint:** `/document/extract` (POST)
- **Default Timeout:** 60s
- **Output:** `Optional[dict]` with keys: `{text, format, page_count, metadata, latency_ms}`

---

### Section 2: Omega Loop Model Invocations

#### 2.1 Omega Reason Phase

**Call Site:** `omega_loop/reason/run.py:14-128`
- **Function:** `reason(omega_input, reason_fn=None, runtime_client=None)`
- **Operation:** `metacognitive_reasoning`
- **Capability:** `coding_model`
- **Runtime Slot:** `gemma_e4b`
- **Invocation Method:** `invoke_runtime_generate()` (via `runtime/socket.py:216`)
- **Prompt Builder:** `omega_loop/_prompt.py:179-189` → `build_omega_prompt(omega_input)`
- **System Prompt:** `omega_loop/_prompt.py:20-59` (`OMEGA_SYSTEM_PROMPT`)
  - Verbatim: "You are Omega. You are analyzing a completed 10-episode behavior window..."
  - Includes `<|think|>` reasoning block marker (see FLAG 1.1 in Part 3)
- **User Prompt:** `omega_loop/_prompt.py:62-125` (`OMEGA_REASONING_TASK`)
  - Placeholders: `{trajectory_snapshot}`, `{trajectory_diff}`, `{episode_window}`, `{current_mastery_state}`, `{open_questions_summary}`, `{mastery_changes}`
  - Appended with `OMEGA_OUTPUT_SCHEMA` (lines 132-153)
- **Output Schema:** JSON with keys:
  - `reasoning`: `{what, why, who, where, when, how, how_action}`
  - `episode_assessment`: `{outcome_alignment, trajectory_alignment, novelty_score, confidence}`
  - `pattern_analysis`: `{recurring_patterns, successful_patterns, failed_patterns, anomalies}`
  - `emergent_observations`: `{repeated_behaviors, stable_patterns, unresolved_patterns}`
  - `trajectory`: `{target_domain, target_capability, confidence, rationale, evidence, continuation_recommended, next_learning_pressure, next_inquiry_pressure}`
  - `open_question_detected`: boolean
  - `open_questions`: array of `{question, domain, confidence}`
  - `pressure_feedback`: `{contributors_to_raise, contributors_to_lower, contributors_to_create}`
- **Params:**
  - `max_tokens`: `omega_reason_max_tokens()` (policy-driven)
  - `temperature`: `omega_reason_temperature()` (policy-driven)
  - `top_p`: `omega_reason_top_p()` (policy-driven)
  - `enable_thinking`: True (P3-S32 override: reasoning enabled for Omega)
  - `timeout`: `omega_reason_timeout()` (policy-driven)
- **Output Parser:** `omega_loop/reflect/run.py:20-63` → `_extract_json_object(text)`
  - Extracts JSON from reasoning preamble + markdown fences
  - Handles unbalanced `</think>` tags
  - Greedy `{.*}` extraction from tail after last `</thinking>` close tag
- **Telemetry:**
  - `emit_data("omega", "prompt_composition", ...)` (lines 44-59)
  - `emit_data("omega", "model_output", ...)` (lines 110-125)
  - `emit_data("omega", "trajectory_update", ...)` (lines 98-105)

#### 2.2 Omega Reflect Phase

**Call Site:** `omega_loop/reflect/run.py:66-108`
- **Function:** `reflect(raw_text, omega_input)`
- **Operation:** Deterministic JSON parsing (no model call)
- **Input:** Raw text from `reason()` phase
- **Output:** `(OmegaReflection, TrajectoryUpdate)` tuple
- **Failure Mode:** Fail-hard cognition — raises RuntimeError if model produced no output or unparseable JSON

---

### Section 3: AUTO Loop Model Invocations

#### 3.1 AUTO Execute Phase (Primary Generation)

**Call Site:** `auto_loop/execute/run.py:256-304`
- **Function:** `_generate(operation, prompt, system, max_tokens, tools=None, temperature=None)` (nested in `run()`)
- **Invocation:** `invoke_runtime_generate(operation, episode_id, client, prompt, system, max_tokens, temperature, top_p, enable_thinking, tools, timeout)`
- **Capability:** `coding_model`
- **Runtime Slot:** `gemma_e4b`
- **Operations:**
  - `auto_synth`: AUTO synthesis (response/proactive/tool-arg generation)
  - `auto_mril_tool`: Memory tool hop-1 (tool call generation for remember_*)
  - `auto_tool_result_synth`: Tool result synthesis
  - `auto_image`: Image generation (via `invoke_runtime_tool("image_tool", ...)`)
- **Params:**
  - `max_tokens`: `auto_profile_max_tokens(profile)` (TEXT_REASONING=1024, TEXT_FAST=256, TOOL_EXECUTOR=512)
  - `temperature`: `auto_generate_temperature()` (policy-driven)
  - `top_p`: `auto_generate_top_p()` (policy-driven)
  - `enable_thinking`: False (P3-S32: disabled for structured output)
  - `timeout`: None (uses TIMEOUTS["generate"] = 60s)
- **Output:** `Optional[dict]` with keys: `{text, tool_calls, tokens_in, tokens_out, ...}`
- **Telemetry:** `emit_inference("auto", operation, episode_id, phase, latency_ms, ok)`

**Memory Tool Hop-1 (MRIL):**
- **Call Site:** `auto_loop/execute/run.py:387-397`
- **System Prompt:** `auto_memory_tool_hop1_law()` (policy-driven)
- **User Prompt:** `build_memory_tool_hop1_prompt(tool_spec)` (`auto_loop/mril_tools/format.py:96-122`)
- **Prompt Content:** "Recall activation — output ONE tool call block only. No prose. No answers."
- **Temperature:** `auto_mril_generate_temperature()` (policy-driven)
- **Max Tokens:** `min(256, max_tok)`
- **Tools:** `[remember_tool_openai_schema(tool_spec)]`

#### 3.2 AUTO Image Generation

**Call Site:** `auto_loop/execute/run.py:602-696`
- **Action:** `IMAGE_CREATION`
- **Tool:** `image_tool`
- **Invocation:** `invoke_runtime_tool("image_tool", operation="auto_image", episode_id, client, prompt, ...)`
- **Capability:** `image_generation`
- **Runtime Slot:** `sd_turbo`
- **Prompt:** `prompt_pack.image_spec.get("prompt")` or `prompt_pack.user_prompt`
- **Output:** Image bytes (persisted to `runtime/image_store.persist_generated_image()`)
- **Telemetry:** `emit_inference("auto", "auto_image", episode_id, phase, latency_ms, ok, tool="image_tool")`

#### 3.3 AUTO PromptPack Phase (Deterministic)

**Call Site:** `auto_loop/promptpack/run.py:25-177`
- **Function:** `build_prompt_pack(execution_pack, memory_envelope, pressure=None)`
- **Operation:** Deterministic (no model call)
- **System Prompt:** `auto_system_prompt(prompt_type, memory_enriched=bool(pack.get("memory_required")))`
  - Loaded from `pressure_loop/policy.py:852-878`
  - Base: "You are Omega AUTO executing a Meta plan."
  - Variants per `prompt_type`: RESPONSE_REASONING, RESPONSE_QUESTION, RESPONSE_ACK, PROACTIVE_REASONING, PROACTIVE_ACK, TOOL_CALL_SYSTEM, IMAGE_SYSTEM, DOCUMENT_SYSTEM, IDLE_SYSTEM
  - Memory enrichment: appends `memory_tool_law` if `memory_enriched=True`

#### 3.4 AUTO Route Phase (Deterministic)

**Call Site:** `auto_loop/route/run.py:26-94`
- **Function:** `select_model_profile(prompt_type, memory_enriched, tool_call_is_direct)`
- **Operation:** Deterministic (no model call)
- **Output:** Model profile string (TEXT_REASONING, TEXT_FAST, TOOL_EXECUTOR, IMAGE_GENERATION, None)

#### 3.5 AUTO Recall Phase (Deterministic)

**Call Site:** `auto_loop/recall/run.py:10-67`
- **Function:** `memory_envelope_before_execute(execution_pack)`
- **Operation:** Deterministic (no model call)
- **Output:** `MemoryEnvelope(memory_required, memory_status, memory_reason, memory_scope, items, error)`

#### 3.6 AUTO Fold Phase (Deterministic)

**Call Site:** `auto_loop/fold/run.py:24-171`
- **Function:** `_fold_episode_auto(ai, status, cycle_kind, memory_envelope, model_profile, model_id, prompt_pack, exec_result, runtime_ms)`
- **Operation:** Deterministic (no model call)
- **Output:** `AutoOutcomePack(episode_id, source_action_goal, execution_pack, auto={status, cycle_kind, memory_status, model_profile, outputs, failure_kind, failure_reason, telemetry})`

---

### Section 4: Memory Subsystem Model Invocations

#### 4.1 Hierarchical Classifier (Memory Routing)

**Call Site:** `memory/engine/hierarchical_classifier.py:147-199`
- **Class:** `HierarchicalClassifier`
- **Method:** `classify(query)` → `(label_id, coord, confidence, tokens)`
- **Invocation:** `self._client.classify_hierarchical(query, force_lobe=force_lobe)`
- **Client:** `get_runtime_client()` (`runtime/client.py:935-942`)
- **Capability:** `classify`
- **Runtime Slots:**
  - Primary: `setfit_lobe` (stage-1 lobe selection)
  - Specialists: `setfit_knowledge`, `setfit_coding`, `setfit_identity`, `setfit_situational`, `setfit_workflows` (stage-2 subdomain)
- **Endpoint:** `/classify` (POST)
- **Default Timeout:** 5s
- **Output:** `ClassifyResult(ok, coord, label, confidence, tokens, stages, latency_ms, reason)`
- **Fallback Coord:** `[5,0]` (skills/tools lobe) on failure
- **Used By:**
  - `memory/gates/base.py:121` (gate write classification)
  - `memory/engine/walker.py:706-712` (walker query classification)
- **Batch Method:** `classify_batch(queries)` (lines 279-287)

#### 4.2 Semantic State Engine (Embedding)

**Call Site:** `memory/semantic_state_engine.py:261-291`
- **Function:** `embed(text)`
- **Invocation:** `rt.embed(text)` where `rt = get_runtime_client()`
- **Capability:** `embedding`
- **Runtime Slot:** `embedding_gemma`
- **Endpoint:** `/embed` (POST)
- **Default Timeout:** 5s
- **Output:** `Optional[list[float]]` (embedding vector)
- **Failure Handling:** Returns None (graceful degrade)
- **Telemetry:** `emit_inference("memory", "embed", latency_ms, ok, dim, error)` (lines 393-420)

**Call Site:** `memory/semantic_state_engine.py:325-370`
- **Function:** `embed_batch(texts)`
- **Invocation:** `rt.embed_batch(texts)`
- **Output:** `Optional[list[list[float]]]`
- **Telemetry:** `emit_inference("memory", "embed_batch", latency_ms, ok, batch_size)`

**Call Site:** `memory/gates/base.py:223-265`
- **Method:** `_attach_embedding(mem, text)` (static)
- **Invocation:** `embed(text)` → `build_embeddings_block(vec)` → attach to `mem["embeddings"]`
- **Failure Mode:** Failure-soft (writes without vector if embed unavailable)

---

### Section 5: Runtime Socket Abstraction

#### 5.1 invoke_runtime_generate (Primary Cognition Socket)

**Call Site:** `runtime/socket.py:216-270`
- **Function:** `invoke_runtime_generate(operation, episode_id, client=None, **kwargs)`
- **Wrapper Around:** `RuntimeClient.generate_completion()` (via `_invoke_socket()`)
- **Preflight:** `preflight_runtime_operation(RuntimeCapability.CODING_MODEL, operation, cycle_id, client)`
- **Params Passed Through:** `prompt`, `system`, `max_tokens`, `temperature`, `top_p`, `enable_thinking`, `tools`, `timeout`
- **Output:** `Optional[dict]` (full response)
- **Token Extraction:** `result.get("tokens")` or `result.get("usage")` or `{prompt: tokens_in, completion: tokens_out}`
- **Model Extraction:** `result.get("model")` or `result.get("model_id")`
- **Telemetry:** `emit_inference("runtime", operation, episode_id, latency_ms, ok, tokens, model, slot, status, error)`

#### 5.2 invoke_runtime_tool (Tool Dispatch Socket)

**Call Site:** `runtime/socket.py:172-213`
- **Function:** `invoke_runtime_tool(tool, operation, episode_id, client=None, **kwargs)`
- **Wrapper Around:** Runtime tool methods (image_generate, vision_describe, face_identify, etc.)
- **Preflight:** `preflight_runtime_operation(capability, operation, cycle_id, client)`
- **Supported Tools:**
  - `image_tool` → `image_generate(prompt, width, height, steps, seed)`
  - `vision_reasoning_tool` → `vision_describe(image_bytes, prompt, image_format, max_tokens, temperature)`
  - `face_tool` → `face_identify(image_bytes, image_format)`

#### 5.3 invoke_runtime_service (Utility Services)

**Call Site:** `runtime/socket.py:107-148`
- **Function:** `invoke_runtime_service(service, operation, episode_id, client=None, **kwargs)`
- **Services:** embed, classify, stt, tts, voice, etc.
- **Preflight:** `preflight_runtime_operation(capability, operation, cycle_id, client)`

---

### Section 6: Runtime Capability Broker

**Call Site:** `runtime/runtime_capability_broker.py:417-477`
- **Function:** `preflight_runtime_operation(capability, operation, cycle_id, client, health_body)`
- **Capabilities Brokered:**
  - `VOICE` → `/voice/command` (voice_command)
  - `IMAGE` → `/image/generate` (image_generate)
  - `STT` → `/transcribe` (transcribe)
  - `TTS` → `/synthesise` (synthesise)
  - `FACE` → `/face/identify` (face_identify)
  - `DOCUMENT_PARSING` → `/document/extract` (document_extract)
  - `CODING_MODEL` → `/generate` (generate_completion)
  - `SPECIALIST_VISION` → `/vision/describe` (vision_describe)
  - `SPECIALIST_VIDEO` → `/video/describe` (video_describe, with fallback to SPECIALIST_VISION)
  - `EMBED` → `/embed` (embed)
  - `CLASSIFY` → `/classify` (classify_hierarchical)
- **Output:** `RuntimeOperationDecision(proceed, route, capability_status, receipt)`
- **Routes:** `PROCEED` → call the runtime; `RESPONSE_HOLD` → do NOT call; `ALTERNATE_CAPABILITY` → try alternate

---

### Section 7: Summary Table — All Model Invocations

| Layer | Operation | Call Site | Capability | Slot | Endpoint | Max Tokens | Temperature | Top P | Enable Thinking | Timeout | Output Type |
|-------|-----------|-----------|-----------|------|----------|-----------|-------------|-------|-----------------|---------|------------|
| **Omega** | metacognitive_reasoning | `omega_loop/reason/run.py:78` | coding_model | gemma_e4b | /generate | policy-driven | policy-driven | policy-driven | True | policy-driven | dict |
| **AUTO** | auto_synth | `auto_loop/execute/run.py:259` | coding_model | gemma_e4b | /generate | profile-driven | policy-driven | policy-driven | False | 60s | dict |
| **AUTO** | auto_mril_tool | `auto_loop/execute/run.py:390` | coding_model | gemma_e4b | /generate | 256 | policy-driven | policy-driven | False | 60s | dict |
| **AUTO** | auto_tool_result_synth | `auto_loop/execute/run.py:563` | coding_model | gemma_e4b | /generate | policy-driven | policy-driven | policy-driven | False | 60s | dict |
| **AUTO** | auto_image | `auto_loop/execute/run.py:612` | image_generation | sd_turbo | /image/generate | N/A | N/A | N/A | N/A | 120s | bytes |
| **Memory** | classify | `memory/engine/hierarchical_classifier.py:198` | classify | setfit_lobe + specialists | /classify | N/A | N/A | N/A | N/A | 5s | ClassifyResult |
| **Memory** | embed | `memory/semantic_state_engine.py:291` | embedding | embedding_gemma | /embed | N/A | N/A | N/A | N/A | 5s | list[float] |
| **Memory** | embed_batch | `memory/semantic_state_engine.py:327` | embedding | embedding_gemma | /embed | N/A | N/A | N/A | N/A | 5s | list[list[float]] |
| **HUD** | image_generate | `hud/server.py:2017` | image_generation | sd_turbo | /image/generate | N/A | N/A | N/A | N/A | 120s | bytes |
| **HUD** | face_identify | `hud/server.py:1222` | face | face_* | /face/identify | N/A | N/A | N/A | N/A | 10s | dict |
| **HUD** | transcribe | `hud/server.py:2938` | stt | whisper_small_en | /transcribe | N/A | N/A | N/A | N/A | 30s | str |
| **Runtime** | vision_describe | `runtime/client.py:877` | specialist_vision | cosmos_reason2b | /vision/describe | policy-driven | policy-driven | N/A | N/A | 30s | str |
| **Runtime** | transcribe | `runtime/client.py:697` | stt | whisper_small_en | /transcribe | N/A | N/A | N/A | N/A | 30s | str |
| **Runtime** | synthesise | `runtime/client.py:755` | tts | jarvis_piper | /synthesise | N/A | N/A | N/A | N/A | 10s | bytes |
| **Runtime** | voice_command | `runtime/client.py:742` | voice | jackrong_qwen | /voice/command | policy-driven | policy-driven | N/A | N/A | 60s | dict |
| **Runtime** | document_extract | `runtime/client.py:897` | document_parsing | N/A | /document/extract | N/A | N/A | N/A | N/A | 60s | dict |

---

## PART 3 — FLAGS

### Executive Summary

**1 CRITICAL mismatch** and **2 IMPORTANT verifications** identified. The codebase is functionally sound but has a prompt-format inconsistency that should be fixed.

---

### FLAG 1: Thinking Tag Mismatches

#### FLAG 1.1 — CRITICAL: Omega System Prompt Declares Qwen-Style `<|think|>` But Served by Gemma

**File:** `omega_loop/_prompt.py:56`

```python
OMEGA_SYSTEM_PROMPT = """You are Omega.
...
<|think|>

Reason step by step through your analysis, then return ONLY the structured
JSON object as your final answer."""
```

**Problem:**
- The system prompt explicitly includes `<|think|>` (Qwen 3.5 chat template syntax)
- Omega is served via `invoke_runtime_generate(operation="metacognitive_reasoning", enable_thinking=True)` at `omega_loop/reason/run.py:87`
- The runtime socket is configured to use **gemma_e4b** (`config/runtime.yaml:65`, slot: gemma_e4b)
- **Gemma has NO Qwen-style `</thinking>` tags** — it uses inline think tokens, not XML tags

**Impact:**
- The `<|think|>` tag in the prompt is **literal text** that Gemma will output, not a trigger for reasoning mode
- Omega's reasoning is controlled by `enable_thinking=True` in the runtime call, NOT the prompt tag
- The prompt is **misleading** — it suggests Qwen-style reasoning but the model doesn't understand that syntax

**Evidence:**
- `omega_loop/reason/run.py:87`: `enable_thinking=True` passed to `invoke_runtime_generate`
- `runtime/client.py:484-507`: `enable_thinking` parameter merged into `chat_template_kwargs`
- `runtime/client.py:493-502`: Comments explicitly state "Qwen 3 / 3.5 chat templates enable a `</thinking>...` reasoning block by default"
- `runtime/model_control.py:45`: `DEFAULT_COGNITION_MODEL = "gemma_e4b"` (NOT Qwen)
- `config/runtime.yaml:65`: `slot: gemma_e4b` for the `/generate` endpoint

**Recommendation:**
- **REMOVE** the `<|think|>` tag from `OMEGA_SYSTEM_PROMPT` (line 56)
- The inline think token is added by the runtime's chat template, not the prompt
- If reasoning is desired, rely on `enable_thinking=True` parameter (already set)

---

#### FLAG 1.2: AUTO Execute Explicitly Disables Thinking, Omega Enables It

**File:** `auto_loop/execute/run.py:272` vs `omega_loop/reason/run.py:87`

**AUTO (Autonomous Loop):**
```python
# auto_loop/execute/run.py:272
res = invoke_runtime_generate(
    ...
    enable_thinking=False,
    tools=tools,
)
```

**OMEGA (Metacognitive Reflection):**
```python
# omega_loop/reason/run.py:87
res = invoke_runtime_generate(
    operation="metacognitive_reasoning",
    ...
    enable_thinking=True,
    tools=None,
    ...
)
```

**Problem:**
- AUTO (tool dispatch, JSON synthesis) defaults `enable_thinking=False` — correct per operator directive (P3-S32)
- OMEGA (10-episode reflection) sets `enable_thinking=True` — intentional for reasoning
- Both use the SAME model (`gemma_e4b`) but with opposite thinking modes
- **Inconsistency:** AUTO's reasoning tasks (tool argument synthesis, response generation) disable thinking, while Omega's reflection enables it

**Evidence:**
- `runtime/client.py:493-502`: P3-S32 directive states "turn its reasoning off" for substrate paths; reasoning is opt-in
- `omega_loop/reason/run.py:99-127`: Telemetry explicitly tracks thinking vs JSON split, expecting thinking output
- `omega_loop/reflect/run.py:24-48`: JSON extraction logic tolerates `</thinking>` / `</thinking>` tags in the output

**Impact:**
- **Acceptable asymmetry** — AUTO doesn't need reasoning (deterministic tool dispatch), Omega does (10-episode synthesis)
- However, the prompt mismatch (FLAG 1.1) means Omega's `<|think|>` tag is **not functional** anyway

---

#### FLAG 1.3: Socket Slot Name Mismatch — Stale `jackrong_qwen` Reference

**File:** `runtime/socket.py:247` (comment)

```python
# Runtime reports the model it actually served under "model_id"
# (ops.resolve("generate")). Reading the wrong key here let a stale
# socket slot name ("jackrong_qwen") mislabel a gemma_e4b inference.
```

**Problem:**
- The comment references a **past bug** where socket slot name `jackrong_qwen` (voice/CLI model) was mistakenly used to label cognition inferences
- Current code correctly reads `result.get("model") or result.get("model_id")` from the runtime response
- However, the **voice socket still has `slot: jackrong_qwen`** (`config/runtime.yaml:54`)

**Evidence:**
- `config/runtime.yaml:54`: `voice` service has `slot: jackrong_qwen`
- `config/runtime.yaml:65`: `generate` service has `slot: gemma_e4b`
- `runtime/model_control.py:44`: `DEFAULT_BROWSER_MODEL = "jackrong_qwen"` (CLI/browser agent)
- `runtime/model_control.py:45`: `DEFAULT_COGNITION_MODEL = "gemma_e4b"` (cognition)

**Impact:**
- **LOW RISK** — the runtime now correctly reports the actual model ID in the response
- Socket slot names are metadata for audit/telemetry, not invocation targets
- The comment is a **historical note**, not an active bug

---

### FLAG 2: Oversized Prompts

#### FLAG 2.1: Omega Reflection Prompt Size

**File:** `omega_loop/_prompt.py:179-189`

The Omega reflection prompt is **VERY LARGE** and **PREFILL-DOMINATED**:

```python
def build_omega_prompt(omega_input: OmegaInput) -> Tuple[str, str]:
    """Fill prompt placeholders with JSON-serialized window input."""
    user = OMEGA_REASONING_TASK.format(
        trajectory_snapshot=_json_block(omega_input.trajectory_snapshot),
        trajectory_diff=omega_input.trajectory_diff,
        episode_window=_json_block(omega_input.episode_window),
        current_mastery_state=_json_block(omega_input.current_mastery_state),
        open_questions_summary=_json_block(omega_input.open_questions_summary),
        mastery_changes=_json_block(omega_input.mastery_changes),
    )
    return OMEGA_SYSTEM_PROMPT, user + "\n\n" + OMEGA_OUTPUT_SCHEMA
```

**Estimated Size:**
- **System prompt:** ~500 chars (lines 20-59)
- **Reasoning task template:** ~1200 chars (lines 62-125)
- **Output schema:** ~1500 chars (lines 132-153)
- **JSON-serialized input blocks:**
  - `trajectory_snapshot`: 500-2000 chars (dict with domain/capability/confidence/rationale)
  - `episode_window`: **10-50KB** (10 episodes x 1-5KB each with intent/action/outcome/mastery)
  - `current_mastery_state`: 1-5KB (mastery scores per domain)
  - `open_questions_summary`: 1-5KB (open questions list)
  - `mastery_changes`: 2-10KB (per-episode deltas)

**Total Estimated:** **~20-70KB** (5,000-17,500 tokens at 4 chars/token)

**Evidence:**
- `omega_loop/reason/run.py:43-59`: Telemetry emits prompt composition with character counts
- `omega_loop/reason/run.py:48`: `est_prompt_tokens=prompt_chars // 4` (4 chars per token estimate)
- The prompt is **PREFILL-DOMINATED** — most tokens are context, not generation

**Impact:**
- **ACCEPTABLE** — Omega runs once per 10-episode window, not per-episode
- Latency is acceptable for a ~120s pressure tick
- However, **no stop tokens are configured** — the model must generate until it naturally stops or hits `max_tokens`

---

#### FLAG 2.2: AUTO Execute Prompt Size

**File:** `auto_loop/promptpack/run.py:25-168`

AUTO prompts are **MUCH SMALLER** and **GENERATION-DOMINATED**:

- **System prompt:** ~100 chars (from `auto_system_prompt()`)
- **User prompt:** 100-1000 chars (dominant message + target)
- **Context blocks:** 0-5KB (tool block, constraints, memory results)
- **Total:** ~1-10KB (250-2500 tokens)

**Evidence:**
- `auto_loop/promptpack/run.py:56-127`: Prompt construction is deterministic and minimal
- `auto_loop/execute/run.py:249-275`: Model invocation with `max_tokens=auto_profile_max_tokens(profile)` (256-1024 tokens)

**Impact:**
- **OPTIMAL** — fast inference, minimal prefill cost

---

### FLAG 3: Format Inconsistencies

#### FLAG 3.1: Omega Uses JSON-Only Output, AUTO Uses Reasoning Mode

**File:** `omega_loop/_prompt.py:123-125` vs `auto_loop/promptpack/run.py:57-92`

**OMEGA:**
```python
Reason step by step through your analysis, then output ONLY a single JSON
object with EXACTLY the structure below as your final answer — no prose and
no other top-level keys."""
```
- **Output format:** JSON-only (no markdown fences, no prose)
- **Response mode:** "reasoning" (but prompt says JSON-only)
- **Validation:** `omega_loop/reflect/run.py:20-63` extracts JSON from reasoning preamble

**AUTO:**
```python
response_mode: Optional[str] = "reasoning"  # line 57
# ... various modes: "reasoning", "question", "ack", "summary", None
```
- **Output format:** Varies by action (RESPONSE, PROACTIVE_RESPONSE, USE_TOOL, IMAGE_CREATION, IDLE)
- **Response modes:** "reasoning", "question", "ack", "summary", None
- **No explicit JSON requirement** — responses are free-form text

**Problem:**
- **Inconsistent instruction formatting:**
  - Omega: "output ONLY a single JSON object" (strict)
  - AUTO: "reasoning" mode allows prose + optional JSON
- **No markdown fences in Omega output schema** — the JSON is bare, not wrapped in ` ```json `
- **AUTO responses are NOT JSON-validated** — they're free-form text

**Impact:**
- **ACCEPTABLE** — Omega and AUTO have different requirements (reflection vs. response)
- However, **Omega's prompt is misleading** — it says "JSON object only" but the system prompt includes `<|think|>` which will produce reasoning text first

---

#### FLAG 3.2: No Stop Tokens Configured for Omega

**File:** `omega_loop/reason/run.py:78-90`

```python
res = invoke_runtime_generate(
    operation="metacognitive_reasoning",
    episode_id=f"{omega_input.window_id}-omega",
    client=runtime_client,
    prompt=user,
    system=system,
    max_tokens=omega_reason_max_tokens(),
    temperature=omega_reason_temperature(),
    top_p=omega_reason_top_p(),
    enable_thinking=True,
    tools=None,
    timeout=omega_reason_timeout(),
)
```

**Problem:**
- **No `stop` parameter** is passed to `invoke_runtime_generate`
- The model will generate until `max_tokens` is reached or it naturally stops
- For JSON output, this is **risky** — the model might truncate the JSON mid-object

**Evidence:**
- `runtime/client.py:472-544`: `generate()` accepts `stop: Optional[list[str]]` but Omega doesn't use it
- `runtime/client.py:514`: `if stop is not None: payload["stop"] = stop`
- No stop tokens are configured in `config/runtime.yaml` for the generate socket

**Recommendation:**
- Consider adding `stop=["}"]` or similar to ensure JSON completion
- Or rely on `max_tokens` being large enough (check `omega_reason_max_tokens()` in policy)

---

### FLAG 4: Capability vs Runtime-Slot Mismatches

#### FLAG 4.1: Metacognitive_Reasoning Operation Uses Coding_Model Capability

**File:** `omega_loop/reason/run.py:79` + `runtime/runtime_capability_broker.py:121-125`

```python
# omega_loop/reason/run.py:79
res = invoke_runtime_generate(
    operation="metacognitive_reasoning",
    ...
)

# runtime/runtime_capability_broker.py:121-125
RuntimeCapability.CODING_MODEL: CapabilityDeclaration(
    RuntimeCapability.CODING_MODEL,
    "/generate",
    "generate_completion",
),
```

**Problem:**
- The operation is named `"metacognitive_reasoning"` (suggests reasoning capability)
- But the socket is `runtime_generate_socket()` which declares `capability: coding_model`
- **Mismatch:** The operation name suggests a distinct reasoning capability, but it's served by the coding model

**Evidence:**
- `config/runtime.yaml:61-65`: `generate` socket has `capability: coding_model`
- `runtime/runtime_capability_broker.py:24`: `CODING_MODEL = "coding_model"` (not a reasoning capability)
- `runtime/socket.py:216-236`: `invoke_runtime_generate` uses `resolve_runtime_generate()` which returns the coding_model socket

**Impact:**
- **ACCEPTABLE** — Omega reasoning is a cognitive task, not a separate capability
- The operation name is descriptive (metacognitive_reasoning), but the capability is correct (coding_model)
- No actual mismatch in routing — both AUTO and OMEGA use the same `/generate` endpoint

---

#### FLAG 4.2: Voice Socket Declares `jackrong_qwen`, Cognition Declares `gemma_e4b`

**File:** `config/runtime.yaml:50-65`

```yaml
voice:
  endpoint: /voice/command
  capability: voice
  method: voice_command
  slot: jackrong_qwen

generate:
  endpoint: /generate
  capability: coding_model
  method: generate_completion
  slot: gemma_e4b
```

**Problem:**
- **Two different models** for two different endpoints
- `jackrong_qwen` is the **CLI/browser-agent model** (operator-controlled, hotswap)
- `gemma_e4b` is the **cognition model** (lazy at boot; logical response resident once loaded)
- The voice socket is **NOT used by cognition** — it's for operator voice commands

**Evidence:**
- `runtime/model_control.py:44-45`: `DEFAULT_BROWSER_MODEL = "jackrong_qwen"` vs `DEFAULT_COGNITION_MODEL = "gemma_e4b"`
- `config/runtime.yaml:50-54`: Voice service uses `jackrong_qwen`
- `config/runtime.yaml:61-65`: Generate service uses `gemma_e4b`

**Impact:**
- **CORRECT DESIGN** — voice and cognition are separate concerns
- No mismatch — each socket is correctly bound to its model

---

### FLAG 5: Summary Table — Thinking Tag Usage by Layer

| Layer | Prompt File | `enable_thinking` | Model | Thinking Tag | Issue |
|-------|-------------|-------------------|-------|--------------|-------|
| **OMEGA** | `omega_loop/_prompt.py` | `True` | gemma_e4b | `<\|think\|>` (Qwen syntax) | **MISMATCH** — Qwen tag on Gemma model |
| **AUTO** | `auto_loop/promptpack/run.py` | `False` | gemma_e4b | None | Correct — no reasoning needed |
| **GLOBAL A** | `global_loop/intent/run.py` | N/A | N/A | N/A | Deterministic (no model) |
| **META A** | `meta_loop/tool/run.py` | N/A | N/A | N/A | Deterministic (no model) |
| **META B** | `meta_loop/reflect/run.py` | N/A | N/A | N/A | Deterministic (no model) |
| **GLOBAL B** | `global_loop/goal/run.py` | N/A | N/A | N/A | Deterministic (no model) |
| **DeepSleep** | `deepsleep_loop/consolidate/run.py` | N/A | N/A | N/A | Deterministic (no model) |

---

### Recommendations

#### CRITICAL (Fix Immediately)

1. **Remove `<|think|>` tag from Omega system prompt** (`omega_loop/_prompt.py:56`)
   - Gemma doesn't understand Qwen-style tags
   - Reasoning is controlled by `enable_thinking=True` parameter, not the prompt
   - **Action:** Delete line 56 or replace with a generic reasoning instruction

2. **Verify Omega JSON extraction handles Gemma's inline think tokens**
   - Gemma uses inline think tokens, not XML tags
   - `omega_loop/reflect/run.py:48` splits on `</thinking>` / `</thinking>` (Qwen syntax)
   - **Action:** Test with actual Gemma output to confirm JSON extraction works

#### IMPORTANT (Verify & Document)

3. **Document the thinking mode asymmetry**
   - AUTO disables thinking (P3-S32 directive)
   - OMEGA enables thinking (intentional for reflection)
   - **Action:** Add a comment in `omega_loop/reason/run.py:87` explaining why OMEGA enables thinking

4. **Add stop tokens to Omega generation**
   - Currently relies on `max_tokens` to prevent truncation
   - **Action:** Consider `stop=["}"]` to ensure JSON completion

5. **Verify `omega_reason_max_tokens()` is large enough**
   - Omega output includes reasoning + JSON
   - **Action:** Check `pressure_loop/policy.py` for the configured max_tokens value

#### NICE-TO-HAVE (Future Cleanup)

6. **Rename operation from `"metacognitive_reasoning"` to `"omega_reflection"`**
   - More descriptive of what's happening
   - Avoids confusion with "reasoning capability"

7. **Update socket slot comment** (`runtime/socket.py:247`)
   - The comment references a past bug; consider removing or updating

---

### Files Affected

#### Critical Changes Required
- `omega_loop/_prompt.py` — Remove/fix `<|think|>` tag (line 56)
- `omega_loop/reason/run.py` — Add documentation comment (line 87)

#### Files to Verify
- `omega_loop/reflect/run.py` — JSON extraction logic for Gemma output
- `pressure_loop/policy.py` — `omega_reason_max_tokens()` value
- `config/runtime.yaml` — Socket configuration

#### Files with Acceptable Asymmetry
- `auto_loop/execute/run.py` — `enable_thinking=False` (correct)
- `runtime/client.py` — P3-S32 directive (correct)
- `runtime/model_control.py` — Model separation (correct)

---

### Testing Recommendations

1. **Unit test:** Omega JSON extraction with Gemma's inline think tokens
2. **Integration test:** Full Omega reflection cycle with actual gemma_e4b model
3. **Regression test:** Verify AUTO still works with `enable_thinking=False`
4. **Telemetry audit:** Check `omega_loop/reason/run.py:99-127` thinking_chars calculation with Gemma output

---

**END OF MAP**
