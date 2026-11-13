# Auto Loop

Pure sequencer for **AUTO** (ExecutionActionPack → AutoOutcomePack). The model-running execution layer between Meta A and Meta B.

## Architecture

### `loop.py` — Pure Sequencer
The main sequencer with two entry points (auto_loop/loop.py).

**Key functions:**
- `run_auto(episode, *, runtime_client, tool_invoker, memory)` — intake → validate → recall → promptpack → route → execute → fold. Writes `episode.auto` and returns `AutoOutcomePack`.
- `run_auto_front` — alias for `run_auto`.

### Phase Sub-Package Pattern
Each phase is a separate sub-package under `auto_loop/` with the standard pattern:
- `<phase>/run.py` — Contains the `run()` function for that phase
- `<phase>/__init__.py` — Re-exports the main exports (from .run import ...)

The loop imports each phase package and calls its main entry function (e.g. `intake.auto_input_from_episode`, `validate.validate_for_execution`, `recall.memory_envelope_before_execute`, `promptpack.build_prompt_pack`, `route.select_model_profile`, `execute.execute`, `fold._fold_episode_auto`). This modular design allows phases to be developed and tested independently.

### `mril_tools/` — Memory-Recall-in-Loop Sub-Package
Provides forced memory recall tooling used by the execute phase (auto_loop/mril_tools/).

**Key exports:**
- `memory_recall_required(pack)` — inspects the execution pack to determine whether a forced memory read is needed before generation; imported by both `loop.py` and `execute/run.py`
- `run_forced_memory_tools(pack, episode_id, memory)` — executes every forced-recall call, then applies one cross-tool finalization pass before returning `(results, MemoryEnvelope)`. The final pass query-scopes whole-lobe identity/persona grounding, recognizes organization-role questions (`owner`, `own`, `work`, `role`, and equivalent structured facets), excludes unrelated identity/code evidence, deduplicates by memory id, relation semantics, and normalized content, prefers qualified relations such as `best_friend` over legacy `friend` duplicates, and caps answer-facing evidence at eight candidates. The model prompt and HUD hit list therefore consume the same cleaned set.
- `candidate_hygiene.py` — deterministic final recall candidate relevance, relation-family routing (family and organization roles), qualified-relation preference, semantic deduplication, and bounded delivery. It does not alter provenance verdicts or re-gate admitted memory through Guardian.
- `response_plan.py` — binds every query-named subject to verified relation facets. Multi-entity comparisons keep all named targets in the primary/direct evidence set; deterministic code binds facts, while the model reasons over and articulates their relationship.
- `format.py` — retains legacy/shared recall formatting helpers used outside the dynamic answer lane. Recall is not sent through Guardian again; admitted strings are delimiter-escaped and `harden()`-fenced as attributed data when composed into model context.
- `scaffold/runtime.py` — invokes the ID-free `scaffold_composer` seat with the current turn plus cleaned verified recall, validates its v2 JSON grammar, fences recall as data, and renders the prompt consumed immediately by the response model. A model-emitted record id or malformed scaffold fails into a minimal host-grounded fallback; it never reconnects the retired response prompt pack.
- `registry.py`, `router.py`, `lobes.py`, `subdomain_helpers.py`, `remember.py` — internal recall routing, lobe mapping, and remember-call helpers

### `skill/` — Skill Dispatch Sub-Package
Mediates USE_SKILL execution on the existing chat brain (auto_loop/skill/).

**Key exports:**
- `dispatch_skill_pack(sp, *, confirmed, command_policy, workspace_root)` — loads the SKILL.md contract, guards, and runs a native tool-call transcript on the chat brain; the normalized compatibility result rides in `outputs["skill_result"]` for Meta B / Global B foldback
- `skill_dispatch_confirmed(source_action_goal)` — authorization gate (operator-confirmed or `OMEGA_AUTONOMOUS_SKILL_EXEC` on)
- `operator_workspace_root()`, `default_command_policy` — workspace jail helpers
- `skill_index.py` — keyword-match skill index (100+ SKILL.md files) used as context enrichment for skill selection

Skill execution has two explicit routes. `read_only` and `network_read` skills retain their exact declared observation tools and forbidden tools; tool-less review/planning skills remain tool-less in the live-chat executor. Their evidence-backed findings/report are the completed artifact, so a successful review closes through Meta B / Global B without requiring file changes or creating mastery continuation. All other safety classes use the `development` route and retain the full jailed development toolset, build/test evidence, resume, and project-rehydration behavior.

### Motor-program transport

For `EXECUTE_MOTOR_PROGRAM`, `motor_adapter.effective_motor_action()` resolves one stable transport lane without mutating the canonical pack: `USE_TOOL` or `USE_SKILL`. `motor_program_from_pack()` reparses the exact Omega-authored contract before execution. Building/testing capabilities are included in deterministic skill selection, while the dispatched objective remains Omega's original objective.

AUTO and the bounded executor have execution authority only. Direct-tool facts and normalized tool-call/ledger/workspace-diff records become `ExecutionEvidence`; verification results include executor provenance such as tool-call id, command, exit code, and result/stdout/diff digests. Raw top-level model claims (`files_changed`, `commands_run`, `tests_run`, `artifacts`, `evidence`, or completion prose) are ignored unless reconstructed from those factual records. Model-supplied intent, pathway, outcome, acceptance criteria, and executor identity are discarded. Positive completion/mastery cannot be self-attested: the evidence contract must be admitted by the executor-owned artifact/source/check lanes, and code/build programs require at least one successful factual check. Historical `qwen_*` schema names are compatibility identifiers only.

For repeated checks, the final matching executor call defines the current result while the full tool history remains available, so a valid TDD RED→GREEN sequence is admitted by its final GREEN state. Successful bridge payloads never derive a failure kind by scanning opaque ids or arbitrary raw text; only explicit failure fields are normalized. Real `429`/rate-limit failures normalize to `rate_limited`, which the motor loop guard treats as transient and therefore never uses to invalidate a pathway.

### Shared State Carrier
- `_auto_state.py` — Shared carriers (AutoInput, MemoryEnvelope, PromptPack, AutoOutcomePack) + constants (PROMPT_TYPES, MODEL_PROFILES, RESPONSE_MODES, lane→lobe mappings, scope aliases, profile max tokens).

## AUTO Phase Chain

### 1. `intake/` — Episode → AutoInput
Assembles `AutoInput` from episode (auto_loop/intake/run.py).

**Input:** `episode`
**Output:** `AutoInput` (dataclass with: episode_id, pressure, source_action_goal, execution_pack, trajectory_snapshot)
**Behavior:** Reads `episode.meta['execution_action_pack']`, folds pressure contributors and trajectory snapshot. Returns `None` if no execution_action_pack exists. Live motor programs are reparsed from independent copies and their program/material fingerprints are recomputed before admission. Any legacy `shadow: true` motor payload is removed from the AUTO transport and cannot execute.

### 2. `validate/` — ExecutionActionPack Validation
Validates the execution pack using Meta A's hard-ban rules (auto_loop/validate/run.py).

**Input:** `execution_pack` (from AutoInput)
**Output:** `(ok, failure_kind, failure_reason)` tuple
**Behavior:** Reuses `meta_loop.meta_a.validate_execution_action_pack` for hard-ban checks (rejects model/inference keys). Returns failure tuple if validation fails.

### 3. `recall/` — Memory Recall
Recalls memory via `memory.api.read_memory` with lane scope (auto_loop/recall/run.py).

**Input:** `execution_pack`, `episode_id`, optional `memory` callback
**Output:** `MemoryEnvelope` (dataclass with: memory_required, memory_status, memory_reason, memory_scope, items, error)
**Behavior:**
- If `memory_required=False` in pack, returns envelope with status="skipped"
- Maps semantic `memory_scope` lane names to allowed lobe ints via `scope_to_lobes()`
- Applies denied lobe rules (journal/dev denied unless explicitly in scope) via `denied_lobes_for_scope()`
- Calls `memory.api.read_memory` with QuerySpec (cycle_id, text, modality, task_scope, allowed_lobes, denied_lobes)
- Returns envelope with items (memory_id, source, timestamp, relevance, content) on success

### 4. `promptpack/` — PromptPack Construction

Deterministically constructs PromptPack from execution pack and memory (auto_loop/promptpack/run.py).

**Input:** `execution_pack`, `memory_envelope`, optional `pressure`
**Output:** `PromptPack` (dataclass with: prompt_type, system_prompt, developer_prompt, user_prompt, context_blocks, response_mode, tool_call_spec, image_spec)
**Prompt type mapping (action → prompt_type):**

- `RESPONSE` + explicit remember intake → `RESPONSE_ACK`
- `RESPONSE` + classified chat-intent `question` → `RESPONSE_QUESTION`
- `RESPONSE` + classified chat-intent `ack` / `freeform` → `RESPONSE_FREEFORM`
- `RESPONSE` + classified chat-intent `reasoning` → `RESPONSE_REASONING`
- `RESPONSE` + temporal recall shape → `RESPONSE_TEMPORAL`
- `RESPONSE` + memory_required (no classifier hit) → `RESPONSE_REASONING`
- `RESPONSE` + question-ending text (no classifier hit) → `RESPONSE_QUESTION`
- `RESPONSE` + acknowledge/confirm keywords (no classifier hit) → `RESPONSE_ACK`
- `RESPONSE` (fallback) → `RESPONSE_REASONING`
- `ASK_OPERATOR` → `ASK_OPERATOR_QUERY` (the system-generated query; own pack type, never `PROACTIVE_REASONING`)
- `PROACTIVE_RESPONSE` + memory_required → `PROACTIVE_REASONING`
- `PROACTIVE_RESPONSE` + status/notification keywords → `PROACTIVE_ACK`
- `PROACTIVE_RESPONSE` (fallback) → `PROACTIVE_REASONING`
- `USE_TOOL` → `TOOL_CALL_SYSTEM`
- `IMAGE_CREATION` → `IMAGE_SYSTEM`
- `DOCUMENT_CREATION` → `DOCUMENT_SYSTEM`
- `IDLE` → `IDLE_SYSTEM`

**Response prompt ownership:** `RESPONSE` and `PROACTIVE_RESPONSE` packs intentionally carry an empty `system_prompt`; deterministic ingress and chat-shape labels select the response mode, while the live chat executor supplies the answer context. `config/auto.yaml` prompt variants remain active for dedicated tool, skill, image, document, ask-operator, and idle execution paths. `RESPONSE_ACK` is reserved for deterministic memory-intake receipts.

**Context blocks:** memory_block (from envelope), tool_block (tool name/reason/params), constraints_block (reserved).

### 5. `route/` — Model Profile Selection

Selects model profile based on prompt type and memory enrichment (auto_loop/route/run.py).

**Input:** `prompt_type`, `memory_enriched`, `tool_call_is_direct`
**Output:** `model_profile` string or None
**Routing rules:**

- `RESPONSE_REASONING`, `PROACTIVE_REASONING` → `TEXT_REASONING`
- `DOCUMENT_SYSTEM` → `TEXT_REASONING`
- `TOOL_RESULT_SYNTHESIS` → `TEXT_REASONING`
- `RESPONSE_QUESTION`, `PROACTIVE_ACK`, `RESPONSE_ACK` + memory_enriched → `TEXT_REASONING`
- `RESPONSE_QUESTION`, `PROACTIVE_ACK`, `RESPONSE_ACK` + not memory_enriched → `TEXT_FAST`
- `TOOL_CALL_SYSTEM` + tool_call_is_direct → None (no model needed)
- `TOOL_CALL_SYSTEM` + not tool_call_is_direct → `TOOL_EXECUTOR`
- `IMAGE_SYSTEM` → `IMAGE_GENERATION`
- `IDLE_SYSTEM` → None
- `ASK_OPERATOR_QUERY` → `TEXT_FAST`
- Any unrecognized type → None

**Helper:** `tool_call_is_direct(execution_pack)` returns True when Meta-bound params fully specify the tool call (web_fetch with query, memory_recall, file_reader, ocr_tool, desktop_tool, image_tool).

The selected model profile is authoritative at execution. A memory-enriched `RESPONSE_QUESTION`
promoted to `TEXT_REASONING` uses the reasoning recall contract, enables the model's private thinking
channel, and receives the longer reasoning timeout; it cannot remain a think-off question merely
because the original prompt label was `RESPONSE_QUESTION`.

### 6. `execute/` — Model/Tool/Image Execution

Executes the action using model inference, tool invocation, or image generation (auto_loop/execute/run.py).

**Input:** `prompt_pack`, `model_profile`, `execution_pack`, `memory_envelope`, `episode_id`, optional `runtime_client`, optional `tool_invoker`
**Output:** `exec_result` dict (outputs, failure_kind, failure_reason, model_id, warnings)

**Execution paths:**

- `RESPONSE`, `PROACTIVE_RESPONSE` — every open-ended reply calls the local `scaffold_composer` immediately before the response seat. The composer receives the current turn plus only cleaned, verified, fenced recall; its validated ID-free scaffold determines answer structure, subject binding, gaps, and whether answer inference uses reasoning. The following response request hotswaps back to the answer seat. The old classifier-selected response system prompts are disconnected. A canned `RESPONSE_ACK` and deterministic receipts such as remember/vision acknowledgements bypass both models.
- `ASK_OPERATOR` — retains its dedicated query-generation path.
- `USE_TOOL` — resolves final args via `runtime.tool_dispatch.deterministic_args_from_order` (overlaying order-domain inputs) then invokes the tool via `tool_invoker` or the default schema-backed invoker; world-mutating raw tools are blocked at entry (fail-closed)
- `IMAGE_CREATION` — image generation via `runtime.socket.invoke_runtime_tool` with spec (prompt, width, height, steps, seed); artifact is persisted via `runtime.image_store`
- `DOCUMENT_CREATION` — model fills a template via `runtime.document_templates`, renders via `runtime.document_renderer`, persists via `runtime.document_store`
- `USE_SKILL` — dispatches a `SkillExecutionPack` to `auto_loop.skill.run.dispatch_skill_pack` (native chat-brain tool loop); the dispatch carries `execution_route=read_only|development`, and success is gated by the route-specific tool envelope plus factual evidence

### Browser perception and action

`BROWSER_ACTION` consumes Global A's semantic plan/current step and Meta A's
live screenshot/OCR receipt. AUTO validates the attachment digest and live
session/window binding, runs one visual tool decision, validates the selected
`browser_control` arguments, and executes one primitive. A visually satisfied
step still selects a non-mutating `snapshot`. Failed perception or inference
executes zero primitives. No tool-result inference or browser reply follows.

AUTO records bounded factual post-action observation and screenshot metadata,
updates the workflow receipt without advancing its semantic cursor, and stops.
The episode's screenshot attachment is cleared even on failed attempts.
Completion and any next episode belong to downstream cognition.
- `PLUGIN_OPERATION` — validates and executes a signed MCP plugin operation; plugin evidence is screened before foldback
- `IDLE` — no operation

**Runtime preflight:** Uses `runtime.runtime_capability_broker.preflight_runtime_operation` for CODING_MODEL and IMAGE capabilities.

**Failure handling:** Returns failure_kind/failure_reason on validation failure, memory failure, runtime unavailability, or tool failure.

**Helper:** `_build_failure_outcome(ai, *, failure_kind, failure_reason, memory_envelope)` constructs AutoOutcomePack for failure cases.

### 7. `fold/` — Episode Auto Fold + AutoOutcomePack Emit

Folds execution results into `episode.auto` dict and emits AutoOutcomePack (auto_loop/fold/run.py).

**Input:** `AutoInput`, status, cycle_kind, memory_envelope, model_profile, model_id, prompt_pack, exec_result, runtime_ms
**Output:** `AutoOutcomePack` (dataclass with: episode_id, source_action_goal, execution_pack, auto)

**Folded episode.auto structure:**

- `episode_id`, `action`, `status`, `failure_kind`, `failure_reason`
- `memory` block (memory_required, memory_status, memory_reason, memory_scope, items, error)
- `model_profile`, `model_id`
- `prompt_pack` block (prompt_type, response_mode, hashes of prompts/blocks)
- `outputs` block (response_text, tool_result, tool_result_summary, image_artifact)
- `telemetry` block (cycle_kind, runtime_ms, tool_runtime_ms, memory_runtime_ms, warnings)

**Hashing:** Prompt text fields are SHA256-hashed (first 16 chars) via `_hash_text()` to avoid bloating episode storage.

## Sequence / Handoffs

1. **Meta A** (`meta_loop.loop.run_meta_a`) → produces `ExecutionActionPack`, writes to `episode.meta['execution_action_pack']`
2. **AUTO** (`auto_loop.loop.run_auto`) → produces `AutoOutcomePack`, writes to `episode.auto`
3. **Meta B** (`meta_loop.loop.run_meta_b`) → consumes `episode.auto`, produces `MetaReflectionPack`, writes to `episode.meta['reflection']`

## Determinism

The AUTO loop is **semi-deterministic**:

- Intake, validate, recall, promptpack, route, and fold phases are fully deterministic
- Execute phase involves model inference (non-deterministic) but follows deterministic routing and tool synthesis patterns
- All mappings (intent→prompt_type, prompt_type→model_profile, scope→lobes) are 1:1

## Constants

**PROMPT_TYPES** (13): RESPONSE_REASONING, RESPONSE_QUESTION, RESPONSE_ACK, RESPONSE_FREEFORM, RESPONSE_TEMPORAL, PROACTIVE_REASONING, PROACTIVE_ACK, TOOL_CALL_SYSTEM, TOOL_RESULT_SYNTHESIS, SKILL_BUILD_SYSTEM, IMAGE_SYSTEM, DOCUMENT_SYSTEM, IDLE_SYSTEM

**MODEL_PROFILES** (4): TEXT_REASONING, TEXT_FAST, TOOL_EXECUTOR, IMAGE_GENERATION

**RESPONSE_MODES** (5): reasoning, question, ack, freeform, summary

**Lane→lobe mappings** (_LANE_LOBE): Maps memory lanes (IDENTITY, PERSONA, KNOWLEDGE, REASONING, SITUATIONAL, TOOLS, SKILLS, JOURNAL, VISION, CODING, TEMPORAL, WORKFLOWS, PROJECT, DEV) to integer lobe indices.

**Scope aliases** (_SCOPE_ALIASES): operator_history → (PERSONA, SITUATIONAL); conversation → (PERSONA, SITUATIONAL); trajectory → (TEMPORAL).

**Profile max tokens** (_PROFILE_MAX_TOKENS): TEXT_REASONING=2048, TEXT_FAST=1024, TOOL_EXECUTOR=512 (ceilings, not targets — raised 2026-07-12 after live truncation).

## Policy Integration

AUTO phases read policy from `pressure_loop.policy` (backed by `config/auto.yaml`, overlay in `policy/symphony.policy.yaml`):

### Policy Files

- `config/auto.yaml` — AUTO config tunables (prompt types, model profiles, route rules, execute parameters, recall limits, fold settings, failure kinds)
- `policy/auto.policy.yaml` — AUTO operational policy (behavioral contract, sequence definition, constraints, alignment)
- `schema/auto.schema.yaml` — AUTO canonical types (AutoInput, MemoryEnvelope, PromptPack, AutoOutcomePack, EpisodeAutoFold, enums)
- `tests/core/test_auto_policy_wiring.py` — policy wiring tests

### Phase-Specific Policy Reads

- **promptpack/run.py** — `auto_system_prompt(prompt_type)` for non-response execution routes; response labels and acknowledgement keywords remain deterministic routing metadata, not answer templates
- **route/run.py** — `auto_direct_tools_always()` / `auto_web_fetch_requires_query()` (direct-tool rules), `auto_memory_enriched_bumps_fast_to_reasoning()` (profile routing)
- **execute/run.py** — `auto_profile_max_tokens()` / `auto_generate_temperature()` / `auto_generate_top_p()` (generation parameters), `auto_tool_result_synth_max_chars()` (synthesis truncation)
- **recall/run.py** — `auto_memory_recall_limit()` (QuerySpec limit)
- **tool dispatch** — `auto_web_fetch_default_max_results()` via the runtime
  tool-dispatch surface

### Original-Preserving Defaults

All policy helpers preserve original hardcoded defaults when config values are missing (e.g., default_temperature=0.7, default_top_p=0.9, TEXT_REASONING max_tokens=1024, recall limit=10).

### Cross-Loop Policy

Meta pack validation still uses `meta_pack_banned_keys()` from Meta config (not AUTO config).

## Observability

All AUTO phases emit via `guardian.terminal_feedback` (failure-soft, never halts cognition) with matching `guardian.data_logger.emit_event` JSONL durability where noted.

| Operation | Hook | Durable JSONL subsystem |
|-----------|------|------------------------|
| run_auto sequencer | `phase_span` + `emit_data` | `omega.auto.run_auto` |
| Episode intake | `emit_data` | `omega.auto.intake` |
| Execution pack validation | `emit_data` | `omega.auto.validate` |
| Memory recall / MRIL recall | `emit_data` | `omega.auto.memory_envelope`, `omega.auto.mril_recall` |
| MRIL remember calls | `emit_memory` | `omega.memory.remember_*` |
| PromptPack construction | `emit_data` | `omega.auto.promptpack` |
| Model profile selection | `emit_data` | `omega.auto.model_profile` |
| _generate (all paths: MRIL hop-1/2, response, tool args, synth, image) | `emit_inference` | `omega.auto.inference` |
| IMAGE_CREATION persist + artifact assembly | `emit_data` | `omega.auto.image_artifact` |
| Tool invocation (runtime + local) | `emit_data` | `omega.auto.tool_invoke` |
| fold/run.py | `emit_data` | `omega.auto.fold` |

> Failure-log coverage: `docs/architecture/FAILURE_LOG_COVERAGE.md`

## Lanes (2026-09-15)

Global A's route sets the lane and the lane sets the tools (`brain_skill_executor.lane_tool_names`):
chat offers only web search/fetch and connected-app tools; the agent lane owns workspace,
file, command and job tools and runs **one tool batch per episode**; the browser lane runs
**one WebDriver primitive per episode**. Unfinished agent or browser work continues through an
Omega successor episode, never a durable job continuation (see
`docs/architecture/COGNITIVE_CASCADE_CURRENT.md`).

## Current State / Legacy Findings

As of the latest codebase audit, **every module under `auto_loop/` is actively used** by production code or tests. There are no fully unused sub-packages or stub phases.

| Metric | Count |
|---|---|
| Total Python files | 34 |
| Actively used modules | 34 |
| Unused modules | 0 |
| Stub/TODO modules | 0 |

### Legacy seams still present but not exercised

Two historical injection/call seams remain in source but are **no longer invoked** by the live cognition chain:

1. **`auto_loop.execute.run._preflight()`**
   - Still defined and exported via `auto_loop.execute` / `auto_loop.auto`, but **never called** in live code or tests.
   - Historical architecture notes describe it as an active preflight gate; the
     current execute phase relies on
     `runtime.runtime_capability_broker.preflight_runtime_operation` per
     capability instead.

2. **`apply_invoker` parameter**
   - Threaded through `run_auto()` → `execute()`, but `execute/run.py` **never reads or calls it**.
   - Superseded by `auto_loop.skill.run.dispatch_skill_pack` for `USE_SKILL` dispatch.

These are cleanup candidates: either remove them entirely or update downstream docs to mark them retired.

## Compat Shims

- `auto.py` — Compat re-export shim for Slice 3 (logic moved to phase sub-packages + loop). Re-exports all public types and functions from _auto_state and phase run modules.
