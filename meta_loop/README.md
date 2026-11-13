# Meta Loop

Pure sequencer for **Meta A** (IntentDecision → ExecutionActionPack) and **Meta B** (AUTO outcome → MetaReflectionPack → Global B). No model calls in Meta phases.

> **v2 model.** Global A emits an `IntentDecision` (no goal, no target). Meta A plans execution from it. Meta B reflects the autonomous execution **only if it actually executed** — it never fabricates a failure for a non-execution (IDLE/skipped → `executed=False`, `result="none"`). There is **no goal / goal_state / goal_matches / goal reflection** in this loop. The name `action_goal` / `source_action_goal` survives **only** as a compat dual-read alias (`intent_decision or action_goal`) so old fixtures and locked windows still parse; the live field is `intent_decision`.

## Config, policy, and schema

| Artifact | Path |
| -------- | ---- |
| Defaults | `config/meta.yaml` (Meta A), `config/meta_b.yaml` (Meta B) |
| Types | `schema/meta.schema.yaml`, `schema/meta_b.schema.yaml` |
| Merged policy | `policy/symphony.policy.yaml` → `meta:` |
| Slice gates | `pressure_loop.policy` (`slice2_meta_a_front`, `slice3_auto_front`, `slice4_meta_b_global_b_front`) |

## Architecture

### `loop.py` — Pure Sequencer
The main sequencer with two entry points (meta_loop/loop.py).

**Key functions:**
- `run_meta_a(episode)` — intake → action → tool → memory → pack [→ skill selection if USE_SKILL]. Writes `episode.meta['execution_action_pack']` and returns the pack as a dict. For `USE_SKILL` actions also calls `attach_skill_execution_pack` (flag-gated on `OMEGA_USE_SKILL_ROUTING`). Returns `None` when intake finds no intent decision.
- `run_meta_b(episode)` — consistency → disposition → reflect. Writes `episode.meta['reflection']` and hands the reflection pack UP to `global_loop.loop.run_global_b`. Returns `None` when `episode.auto` is missing.

### Phase Sub-Package Pattern
Each phase is a separate sub-package under `meta_loop/` with the standard pattern:
- `<phase>/run.py` — Contains the `run()` function for that phase
- `<phase>/__init__.py` — Re-exports the main exports (from .run import ...)

The loop imports each phase package and calls its `run()` function. This modular design allows phases to be developed and tested independently.

### Shared State Carrier
- `_meta_a_state.py` — `MetaAState` dataclass, the shared carrier passed through Meta A phases (intake → action → tool → memory → pack).

## Meta A Phase Chain

### 1. `intake/` — Episode → MetaAInput
Assembles `MetaAInput` from episode (meta_loop/intake/run.py).

**Input:** `episode`
**Output:** `MetaAInput` (dataclass with: episode_id, pressure, trajectory_snapshot, action_goal)
**Behavior:** Reads `episode.global_['intent_decision']` (the live Global A `IntentDecision` dict), falling back to `episode.global_['action_goal']` as a compat alias. Folds pressure contributors and trajectory snapshot. Returns `None` if neither exists (no intent decision). The dataclass field is still named `action_goal` — it carries the `IntentDecision` dict (`episode_id, intent, confidence, reason`), not a goal/target.

### 2. `action/` — Intent → Meta Action
Maps global intent to meta action and initializes execution fields (meta_loop/action/run.py).

**Input:** `MetaAInput`
**Output:** `MetaAState` (updated with action, text, target_str, meta, execution_intent, execution_inputs). `target_str` comes from the dominant contributor message (`_dominant_text`) — Global A no longer carries a target.
**Mapping rules (intent → action) (`_INTENT_TO_ACTION_DEFAULT`):**
- `RESPOND` → `RESPONSE`
- `RESOLVE_QUERY` → `RESPONSE`
- `PROACTIVE_RESPONSE` → `PROACTIVE_RESPONSE`
- `ASK_OPERATOR` → `ASK_OPERATOR`
- `LEARN` → `USE_TOOL`
- `USE_TOOL` → `USE_TOOL`
- `IMAGE_CREATE` → `IMAGE_CREATION`
- `DOCUMENT_CREATE` → `DOCUMENT_CREATION`
- `IDLE` → `IDLE`
- `USE_SKILL` → `USE_SKILL`
- `PURSUE_OUTCOME` → `EXECUTE_MOTOR_PROGRAM`
- `PLUGIN_OPERATION` → `PLUGIN_OPERATION`
- `APPLY` → `USE_SKILL` (legacy compat shim — Global A canonicalizes `APPLY`→`USE_SKILL` when routing is ON)
- (unmapped intent) → `IDLE`

**META_ACTIONS** (10): `USE_TOOL`, `RESPONSE`, `PROACTIVE_RESPONSE`,
`ASK_OPERATOR`, `IMAGE_CREATION`, `DOCUMENT_CREATION`, `IDLE`, `USE_SKILL`,
`EXECUTE_MOTOR_PROGRAM`, and `PLUGIN_OPERATION`. `USE_SKILL` is the bounded
agentic-execution action; `EXECUTE_MOTOR_PROGRAM` is the motor-program
transport action; `PLUGIN_OPERATION` is the signed external-app route.

**Policy integration:** Reads `pressure_loop.policy.meta_intent_to_action()` for runtime overrides.

### 3. `tool/` — Capability Tool Selection
Selects capability tool per intent (meta_loop/tool/run.py).

**Input:** `MetaAState`
**Output:** `MetaAState` (updated with tool, tool_reason, execution_inputs)
**Rules (`meta_loop/tool/run.py:60-209`):**
- `RESPOND` → no tool; sets `execution_intent` only (response text is generated downstream in AUTO, never here)
- `LEARN` → runs the Topic Resolution Engine first (see below); if resolved, default tool via `pressure_loop.policy.meta_learn_default_tool()` (default `web_fetch`); if `NO_TOPIC_AVAILABLE`, `tool=None` + `execution_intent="SKIP_NO_TOPIC"`
- `USE_TOOL` + file hints (`.txt`/`.pdf`/`.csv`/`file`) → `file_reader`
- `USE_TOOL` + vision hints (understand image / describe scene / locate object / video) → `vision_reasoning_tool`
- `USE_TOOL` + face hints (recognize / age-gender / head pose / landmarks) → `face_tool`
- `USE_TOOL` + OCR hints (ocr / image-to-text / scan text) → `ocr_tool`
- `USE_TOOL` + desktop hints (desktop / UI automation / click / window) → `desktop_tool`
- `USE_TOOL` (fallback) → default tool
- `USE_SKILL` → `tool=None`; sets `tool_reason` to skill-mediated dispatch note and `execution_intent` to skill selection plan. Raw tools are internal to the ExecutionSkill — Meta A never selects them directly. The `skill/` phase (below) then selects the actual ExecutionSkill and builds the `SkillExecutionPack`.
- `IMAGE_CREATE` → `tool_for_directive_intent` or `image_tool`
- `DOCUMENT_CREATE` → `tool_for_directive_intent` or `document_tool` (folds `format`/`template`/`upload_ref` from contributor metadata)

There is no RESPOND-memory branch in the live tool phase. Memory recall for
response and learning routes is decided in the **memory** phase, not here.

**Policy integration:** Reads `pressure_loop.policy.meta_capability_tool_map()` for capability overrides and `meta_learn_default_tool()` for LEARN default.

### 3a. `skill/` — ExecutionSkill Selection (USE_SKILL branch only)
Phase 6O wiring — runs **only** when `pack_dict['action'] == 'USE_SKILL'` (`meta_loop/loop.py:68-72`). Inert while `OMEGA_USE_SKILL_ROUTING` is OFF.

**Files:** `meta_loop/skill/run.py`, `meta_loop/skill/foldback.py`

**`select_skill_for_intent(...)` (`skill/run.py`):** Deterministic scoring over the `ExecutionSkill` catalog. Derives the `needed_kind` (WRITE, INSPECT, TEST, etc.) from the objective text via `_KIND_MARKERS`, then scores every selectable skill on six weighted dimensions: intent_match (0.30), execution_kind_match (0.20), target_domain_match (0.15), tool_requirement_match (0.15), safety_compatibility (0.10), capability_strength (0.10). Tie-breaks by objective relevance → generality → strength → name. Returns a `SelectedSkillDecision`; on no safe match returns `selected=False` with a `failure_reason` (no silent drop). `read_only` skills are excluded for write-kind tasks; `self_modification` skills require explicit authorization.

**`attach_skill_execution_pack(episode, pack_dict)` (`skill/run.py`):** Phase 6O orchestrator. Extracts the objective from `execution_inputs`, calls `select_skill_for_intent`, builds a `SkillExecutionPack` (via `schema.skill_execution_pack`), embeds it in `pack_dict['execution_inputs']['skill_execution_pack']`, and also writes it to `episode.meta['skill_execution_pack']` for foldback. Taxonomy recall context is live and applied as an additive score nudge (`skill_boosts`/`skill_penalties`) without changing the deterministic base score. A failed selection records `episode.meta['skill_selection_failure']` and returns `None`; AUTO then surfaces a `missing_skill_pack` outcome.

**`foldback.py`:** Phase 6K — folds the bounded skill-execution result through
Meta B (`classify_meta_b_skill_outcome`) and Global B
(`integrate_global_b_skill_outcome`). Writes `episode.meta['skill_reflection']`
and `episode.global_['skill_outcome']` alongside the live reflection/outcome. A
`MetaBSkillLedger` enforces exactly one Meta B outcome per dispatch — a duplicate
or missing outcome is a hard error. `fold_skill_result(...)` is the orchestrating
entry point; `attach_skill_foldback(episode, folded)` writes the results onto the
episode.

### 4. `memory/` — Memory Required / Scope
Determines memory_required, scope, and reason per intent (meta_loop/memory/run.py).

**Input:** `MetaAState`
**Output:** `MetaAState` (updated with memory_required, memory_reason, memory_scope, memory_query, memory_delivery_mode, memory_tools)
**Rules (`meta_loop/memory/run.py:115-184`):**
- `RESPOND` + prior pattern → memory_required=True, scope=(operator_history, conversation), MRIL fields applied
- `RESOLVE_QUERY` → memory_required=False
- `LEARN` resolved topic → memory_required=True, forced domain-lobe recall (`remember_code` for coding, else `remember_knowledge`) on the concrete topic (memory-first LEARN, see §"Memory-First LEARN (3b)")
- `LEARN` + prior pattern (no resolved topic) → memory_required=True, scope=(operator_history,), MRIL fields applied
- `USE_SKILL` + non-empty query → memory_required=True, forced domain-lobe recall (`remember_code` for coding, else `remember_knowledge`) on the execution target (recall-before-apply for skill-mediated tasks)
- `USE_TOOL` + prior pattern → promoted to an MRIL `RESPONSE` (action=RESPONSE, tool cleared), scope=(operator_history, conversation) — there is **no** `memory_recall` tool; recall runs via MRIL in AUTO
- `SELF_INQUIRY` → treated as `USE_TOOL` by the action phase; the memory phase has **no dedicated `SELF_INQUIRY` branch**, so it does not promote to an MRIL `RESPONSE`.

Memory delivery is always via MRIL in AUTO (`memory_delivery_mode="inference_tool"`, `memory_tools` set by `auto_loop/mril_tools/router`); the legacy `memory_recall` tool name is retired.

**Policy integration:** Reads `pressure_loop.policy.meta_memory_pattern()` for regex patterns (respond_prior, learn_prior).

### 5. `pack/` — ExecutionActionPack Assembly
Assembles `ExecutionActionPack` and validates banned keys (meta_loop/pack/run.py).

**Input:** `MetaAState`
**Output:** `ExecutionActionPack` (dataclass)
**Fields:**
- `episode_id` — from state
- `action` — meta action from action phase (overridden to `IDLE` when tool is None and `execution_intent == "SKIP_NO_TOPIC"`)
- `tool` — selected tool (optional)
- `tool_reason` — why tool was selected
- `memory_required` — whether memory is needed
- `memory_reason` — why memory is needed
- `memory_scope` — tuple of memory scope keys
- `memory_query` — the recall query (required when memory_required)
- `memory_delivery_mode` — e.g. `inference_tool` (MRIL)
- `memory_tools` — tuple of exactly one `remember_*` tool entry (required when memory_required)
- `execution_intent` — description of execution plan
- `execution_inputs` — inputs for execution
- `execution_constraints` — constraint flags (planning_only, no_inference_in_meta, no_execution_in_meta, no_model_fields)
- `source_action_goal` — the echoed Global A `IntentDecision` (`episode_id, intent, confidence, reason`). Compat-named `source_action_goal`; it is **not** a goal/target.

**Validation (`meta_loop/pack/run.py:52-87`):**
- Banned keys check (recursive over all key paths): rejects packs containing model/inference keys (model, llm, engine, prompt, system_prompt, temperature, max_tokens, tokens, sampling, top_p, top_k)
- Constraint flags check: ensures the four required flags are each set to `True`
- Tool validation: `tool_reason` required when `tool` is set
- Memory validation (when `memory_required`): `memory_reason`, `memory_query`, and `memory_tools` required; `memory_tools` must contain **exactly one** `remember_*` tool

**Policy integration:** Reads `pressure_loop.policy.meta_pack_banned_keys()` for banned key list.

## Meta B Phase Chain

### 1. `consistency/` — Packet Consistency Check
Assembles `MetaBInput` and checks packet consistency (meta_loop/consistency/run.py).

**Input:** `episode`
**Output:** `MetaBInput` (dataclass with: episode_id, pressure, source_action_goal, execution_pack, auto_outcome)
**Behavior:** Folds episode data (pressure, global_, meta, auto) into a single input structure. Returns `None` if `episode.auto` missing. `source_action_goal` is read via the compat dual-read (`global_['intent_decision'] or global_['action_goal'] or execution_pack['source_action_goal']`) — it carries the `IntentDecision` echo, not a goal.

**Consistency check (run):**
- Compares stable fields across source_action_goal, execution_pack, and auto_outcome
- Checks episode_id, action, and tool consistency
- Returns `(consistency_dict, critical_mismatch_flag)`
- Critical mismatch = episode_id mismatch OR action mismatch OR tool mismatch

### 2. `disposition/` — Cycle Kind → Disposition
Maps AUTO cycle_kind to disposition and meta status (meta_loop/disposition/run.py).

**Input:** `MetaBInput`, `critical_mismatch_flag`
**Output:** `(disposition, status)` tuple
**Mapping (cycle_kind → disposition, status) — `meta_b_cycle_kind_map` default:**
- `success` → `completed`, `success`
- `partial` → `degraded`, `partial`
- `fail` / `failed` → `failed`, `failed`
- `skipped` → `skipped`, `skipped`
- `idle` → `idle`, `idle`
- (unknown/missing cycle_kind) → `invalid`, `invalid` (`meta_b_unknown_cycle_kind`)

Dispositions: `completed, degraded, failed, skipped, idle, invalid`. Meta statuses: `success, partial, failed, skipped, idle, invalid`.

**Critical override:** If critical_mismatch_flag is True, forces disposition=`invalid`, status=`invalid`.

### 3. `reflect/` — MetaReflectionPack Assembly
Builds `MetaReflectionPack` from consistency, disposition, and AUTO outcome (meta_loop/reflect/run.py).

**Input:** `MetaBInput`, `consistency_dict`, `disposition`, `status`
**Output:** `MetaReflectionPack` (dataclass)
**Only-if-executed soft gate (`meta_loop/reflect/run.py:49-61`):** Meta B reflects over execution **only if execution actually ran**. It computes `executed = bool(action and action != "IDLE" and status not in {idle, skipped, invalid})`. When **not** executed → `result = "none"` — it **never fabricates a failure** for a non-execution (IDLE/skipped). When executed → `result = "success"` if `status == "success"` else `"failure"`; the actual reason-it-failed travels in `failure_reason`. This success/failure verdict folds up to Global B.
**Fields:**
- `episode_id` — from input
- `consistency` — consistency check results
- `execution` — execution block: `executed` (bool), `result` (`success`/`failure`/`none`), `action`, `tool`, `status`, `disposition`, `reason`, `execution_intent`, `failure_kind`, `failure_reason`, `memory_status`, `warnings`
- `trace_summary` — trace presence flags (has_response_text, has_tool_result, has_image_artifact, runtime_ms)

There is no `goal_matches` field — the pack reflects execution against the intent, not a goal.

## Topic Resolution Plug-In (LEARN Branch)

When the Global A intent is `LEARN`, the Meta A tool phase invokes the Topic Resolution Engine before selecting a tool. See `docs/architecture/TOPIC_RESOLUTION_AND_MASTERY_GUIDED_LEARNING.md` (Sections 2 + 3).

**File:** `meta_loop/tool/run.py:72-100`

**Behavior:**
1. Calls `resolve_learning_topic(state)` from `cognition.topic_resolution.resolve_topic`
2. If `NO_TOPIC_AVAILABLE`:
   - `state.tool = None`
   - `state.execution_intent = "SKIP_NO_TOPIC"`
   - `state.tool_reason = "NO_TOPIC_AVAILABLE — no resolvable learning target; aborting LEARN"`
3. Pack phase (`meta_loop/pack/run.py:97-100`) detects `tool is None` + `execution_intent == "SKIP_NO_TOPIC"` and overrides `action = "IDLE"`
4. If resolved:
   - `state.tool = _learn_default_tool()` (default `web_fetch`)
   - `state.execution_inputs = {"query": _search_phrase(resolved.topic), "domain": resolved.domain, "topic_source": resolved.source}`
   - When `resolved.source == "trajectory_fold"`, additionally carries: `topic_fold=True`, `fold_question_id` (if present), `folded_question` (if present) — these travel in `execution_inputs` to the AUTO phase

This prevents generic LEARN queries from reaching AUTO and wasting inference cycles.

## Memory-First LEARN (3b)

When the LEARN intent resolves to a concrete topic, the Meta A **memory phase** forces a `remember_knowledge` recall on that topic before AUTO execution. See `docs/architecture/TOPIC_RESOLUTION_AND_MASTERY_GUIDED_LEARNING.md` (Section 3).

**File:** `meta_loop/memory/run.py:136-148`

**Behavior:**
1. Extracts `resolved_topic` from `state.execution_inputs.query` (the concrete topic set by the tool phase).
2. Calls `_apply_learn_knowledge_recall(state, resolved_topic)` (`memory/run.py:61-83`), which routes the recall to the domain's own lobe via `_learn_recall_tool_for_domain(domain)`: `remember_code` for a coding subcategory (lobe 8), else `remember_knowledge` (lobe 2; failure-soft default).
3. Sets `memory_required = True`, `memory_reason = 'Recall prior knowledge on the learning target: {topic}'`, and `memory_delivery_mode = 'inference_tool'`.
4. AUTO's MRIL tool dispatch will therefore recall from the domain's lobe (coding lobe 8 via `remember_code`, else `200_knowledge`) on the exact resolved topic before any web fetch or external tool runs.

This guarantees LEARN episodes start from prior knowledge rather than cold external search.

## Sequence / Handoffs

1. **Global A** (`global_loop.loop.run_global_a`) → produces an `IntentDecision` (no goal, no target), writes to `episode.global_['intent_decision']`
2. **Meta A** (`meta_loop.loop.run_meta_a`) → produces `ExecutionActionPack`, writes to `episode.meta['execution_action_pack']`
3. **Omega motor planning when enabled** → authors or rebinds the motor
   contract; **AUTO** then runs model/tool execution and produces
   `AutoOutcomePack`, written to `episode.auto`
4. **Meta B** (`meta_loop.loop.run_meta_b`) → produces `MetaReflectionPack` (execution only-if-executed), writes to `episode.meta['reflection']`, then hands pack UP to `global_loop.loop.run_global_b`
5. **Global B** (`global_loop.loop.run_global_b`) → produces `GlobalOutcomePack` carrying an `EpisodeOutcome` (intent × trajectory_alignment × divergence_score × execution_outcome — no goal_state), writes to `episode.global_['outcome']`

## Determinism

The meta loop is **fully deterministic** — no model inference, no random choices.
All mappings are 1:1 or template-based.

## Policy Integration

Meta phases read policy from `pressure_loop.policy`:
- `meta_intent_to_action()` — intent → action mapping overrides
- `meta_capability_tool_map()` — capability → tool mapping overrides
- `meta_learn_default_tool()` — default tool for LEARN intent
- `meta_memory_pattern(name)` — regex patterns for memory detection
- `meta_pack_banned_keys()` — banned model/inference keys for pack validation

These functions merge defaults from `config/meta.yaml` with runtime overrides from `policy/symphony.policy.yaml`.

## Current State / Legacy Findings

The live surface is the `loop.py` sequencer plus the intake, action, tool,
skill, memory, pack, consistency, disposition, and reflection phases. The
compatibility re-export modules remain narrower than the phase packages.

### Dead / unused artifacts

- `meta_loop/intake/run.py::gap_domain` — defined but **never called**. Superseded by `USE_SKILL` text/metadata handling in `action/run.py`.
- `meta_loop/meta_b.py::run_meta_b_global_b_front` — defined but has **no production callers**. Only mentioned in archived build-status docs.

### Stub / incomplete packages

- `meta_loop/skill/__init__.py` — docstring-only; unlike every other phase package, it does **not** re-export `run`, `foldback`, `select_skill_for_intent`, etc. All callers import `meta_loop.skill.run` / `meta_loop.skill.foldback` directly.

### Ineffective code

- `meta_loop/reflect/run.py:94-109` computes `neuromod_adjustments` only when `disposition` is `"explore"` or `"exploit"`, but `disposition/run.py` never returns those values (only `completed`, `degraded`, `failed`, `skipped`, `idle`, `invalid`). The branch never fires and `neuromod_adjustments` is always `{"dopamine": 0.0, "plasticity": 0.0}`.

## Compat Shims

- `meta_a.py` — Compat re-export shim for Slice 2 (logic moved to phase sub-packages + loop)
- `meta_b.py` — Compat re-export shim for Slice 4 (logic moved to phase sub-packages + loop)
