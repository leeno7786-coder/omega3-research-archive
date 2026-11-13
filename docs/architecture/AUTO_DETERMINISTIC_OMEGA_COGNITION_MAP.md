# AUTO Deterministic + Omega Cognitive Layer — Relocation Map

> **Historical snapshot — superseded 2026-08-03.** The active system now runs
> Omega and DeepSleep on every episode. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md`.

> **Status:** LIVE-VALIDATED (active motor programs enabled after shadow and executor-proof gates)
> **Date:** 2026-06-14  
> **Branch:** main (`git pull origin main` — up to date)  
> **Work orders:** agent bus `#afc93b53` + addendum `#e865dc0a` (Claude → Cursor)  
> **Builds on:** [`LEARN_MASTERY_APPLICATION_LOOP_MAP.md`](LEARN_MASTERY_APPLICATION_LOOP_MAP.md)

### Verification methodology (codex-grade evidence discipline)

| Claim class | How verified |
|-------------|--------------|
| **Proven** | `rg invoke_runtime_generate` across `global_loop/`, `meta_loop/`, `auto_loop/` — zero hits outside `auto_loop/execute/run.py` (forward pass) and `omega_loop/reason/run.py` (window fold-back); corroborated by `scripts/audit_emit_coverage.py:125-134` (`INFERENCE_MARKERS` lists only those two cognition call sites). |
| **Proven** | All six `_generate` call sites read directly in `auto_loop/execute/run.py:390-796`; line numbers in §1 match live file. |
| **Proven** | `run_forced_memory_tools` → `invoke_remember_tool` → `memory.api.read_memory(QuerySpec)` at `auto_loop/mril_tools/remember.py:29-56` — no `invoke_runtime_generate`. |
| **Proven** | The current cascade declares `omega_motor` between `meta_a` and `auto`; per-episode Omega remains after Global B and before DeepSleep. |
| **Out of scope** | HUD operator chat (`hud/server.py` — separate `invoke_runtime_generate` for `/api/chat`); knowledge_extractor hierarchical classifier inference (`memory/engine/hierarchical_classifier.py`) — substrate sidecar, not cognition cascade forward pass. |

---

## Architecture target (operator decision)

**Forward pass:** Global A (intent) → Meta A (tool + params) → Auto (deterministic fill + execute, **zero inference**) → fold.

**Fold-back:** Meta B → Global B → **per-episode Omega** (one inference: comprehend, diff recall vs result, set mastery + pressure) → DeepSleep (deterministic consolidate, per episode).

### Live motor-program insertion (2026-07-11)

Outcome-seeking episodes now use this order:

`Pressure → Global A → Meta A → Omega Motor → AUTO → Meta B → Global B → Omega Episode → DeepSleep`

- Global A and Meta A remain deterministic. Meta A transports an `emergent_pressure` unchanged and selects only the stable execution lane: `USE_TOOL` for read/research capabilities or `USE_SKILL` for building, code execution, testing, and patching. Required motor capabilities participate in skill selection without changing Omega's outcome contract.
- Omega Motor is the only planner before execution. It validates a complete `MotorProgram`, emits `omega/motor_program`, and attaches the exact canonical program. AUTO and Qwen are limbs: they may exercise bounded local discretion, but normalized bridge output strips planner/authority fields and can only contribute executor facts.
- Executor provenance is reconstructed from successful bridge `tool_calls`, the bridge-owned ledger, and independently derived workspace diffs. Untrusted top-level `files_changed`, `commands_run`, `tests_run`, `artifacts`, `evidence`, and completion prose do not become factual evidence by assertion. Code/build completion additionally requires a successful provenance-bearing check.
- Persistence is evidence-first and Guardian-free: `motor_execution_evidence` → `motor_attempt_assessment` → pressure resolution/create → successor pressure staging → DeepSleep mastery → `pathway_outcome` projection. A first originless attempt uses the explicit `create` lifecycle; revision, split, and supersession continue to require the exact originating pressure.
- Positive knowledge mastery requires admitted factual knowledge evidence; positive application mastery requires admitted files/tests evidence. Only after that gate does pathway credit compute dopamine/plasticity/stress/serotonin/noradrenaline. Neuromodulation changes pathway credit and future selection, never mastery directly.
- Loop breaking is governed by `equivalent_no_progress_limit: 2` and `distinct_failure_limit: 3`. Two materially equivalent no-progress attempts invalidate a pathway. Three distinct evidence-bearing failures ask the operator unless a genuinely new required-capability path exists.
- Rollout was completed incrementally. Shadow programs were first authored and telemetered while preserving AUTO's prior bytes; a defensive `shadow: true` marker remains excluded from AUTO, Omega packets, episode/attempt memory, mastery/pathway credit, and dev-project persistence. After live shadow proof and active executor-proof validation, `config/cognition_chain.yaml` now ships `shadow_mode: false`.

### Live acceptance evidence (2026-07-11)

- Local mind: `gemma_e4b` with thinking enabled. Motor planning uses a dedicated 600-second timeout because system-prompt-heavy turns on the Radeon 860M took roughly 81–285 seconds in live runs; latency must not be classified as incapability.
- Execution limb: the live TypeScript bridge on `:8092`, OpenRouter, and `tencent/hy3:free`, jailed to `F:\omega-workspace`. Gemma authored the plan and evidence contract; HY3 received only the bounded execution dispatch.
- Shadow derivation episode `ed24c7fe-c01e-4108-aa03-a3187b960698` and shadow code episode `8bd2f91b-9aeb-49a5-bf84-87946a72531a` produced valid programs and were excluded from AUTO.
- Active validation exposed and closed three evidence-boundary defects: test receipts placed in the source-proof lane, historical TDD RED results overriding the final GREEN result, and an opaque successful tool-call id containing `429` being misclassified as rate limiting. A fourth diagnostic correctly fail-closed when the direct process omitted the workspace jail root.
- Final active episode `258ba8fc-8973-4aa1-90c6-64529fc85295` produced program `mp_258ba8fc-8973-4aa1-90c6-64529fc85295_02`. Its contract required only the exact `clamp.py` and `test_clamp.py` paths, required `pytest`, and required no source proof. HY3 created the files, executor-owned pytest evidence passed, `_evidence_contract_admitted(...)` returned true, and the independent attempt verdict was `verified`.
- Manual replay of the generated test file passed 7 tests. DeepSleep/pathway projection awarded application mastery `0.00 → 0.05` with dopamine `+0.5`; every preceding unverified live attempt retained zero mastery delta.
- Final regression proof passed the complete repository suite twice: `3932 passed, 1 skipped` without coverage and again with the configured coverage gate (`74%` aggregate). The focused motor suite passed `229` tests, and the import-order authority reproducer passed `96` tests.
- Detailed receipts are recorded in [`../validation/omega-emergent-motor-program-live-validation.md`](../validation/omega-emergent-motor-program-live-validation.md).

**Window:** every 10 episodes — existing windowed Omega reflection remains; this map notes what must change for the *additional* per-episode Omega pass.

---

## 1. Forward-pass model calls

`invoke_runtime_generate` appears in exactly two live cognition packages: `auto_loop/execute/run.py` and `omega_loop/reason/run.py`. **Global A and Meta A have no model calls** (Meta A enforces `no_inference_in_meta: True` at `meta_loop/action/run.py:46-50`).

### Global A — **none**

| Site | Classification |
|------|----------------|
| `global_loop/loop.py:60-101` (`run_global_a`) | Deterministic: `ignition_phase.run` → `intent_phase.run` → `IntentDecision`. No `_generate` / `invoke_runtime_generate`. |

### Meta A — **none**

| Site | Classification |
|------|----------------|
| `meta_loop/loop.py:38-78` (`run_meta_a`) | Deterministic: intake → action → tool → memory → pack. No inference. |
| `meta_loop/action/run.py:63-100` | Planning only; `no_inference_in_meta` constraint. |
| `meta_loop/tool/run.py:43-179` | Rule-based tool + `execution_inputs` binding. |
| `meta_loop/memory/run.py:84-138` | MRIL routing flags only; recall runs later in Auto execute. |

### Auto execute — all `invoke_runtime_generate` call sites

Inner wrapper: `auto_loop/execute/run.py:246-304` (`_generate` → `invoke_runtime_generate`).

| Line(s) | Operation | Trigger | Bucket | Relocation notes |
|---------|-----------|---------|--------|------------------|
| 390-397 | `auto_mril_tool` | `memory_delivery_mode == "inference_tool"` + RESPONSE paths | **RUBBER-STAMP DELETE** | Model echoes Meta-routed `remember_*` tool call; `validate_mril_hop1_tool_call` (`auto_loop/mril_tools/format.py:125-146`) checks exact query. Replace with forced `run_forced_memory_tools` only. |
| 447-453 | `auto_response` | MRIL hop-2 after forced recall | **RELOCATE → Omega** | Real response text generation with recalled knowledge injected. |
| 463-468 | `auto_response` | RESPONSE / PROACTIVE_RESPONSE / ASK_OPERATOR (single-hop MRIL) | **RELOCATE → Omega** | Operator-facing response text. |
| 491-505 | `auto_tool` | `USE_TOOL` + `model_profile == "TOOL_EXECUTOR"` + `not tool_call_is_direct` | **RUBBER-STAMP DELETE** | Model arg-gen for tool call; merged via `overlay_tool_call_args` (`runtime/tool_dispatch.py:76-84`). Operator belief: **no tool needs model-composed args** once Meta binds inputs — see §2. |
| 563-568 | `auto_synth` | After successful `USE_TOOL` | **RELOCATE → Omega** | `TOOL_RESULT_SYNTHESIS` — comprehension/summary of raw `tool_result`. Raw result should stay; synthesis moves to fold-back Omega. |
| 791-796 | `auto_document` | `DOCUMENT_CREATION` | **RELOCATE → Omega** | Model fills markdown from template + user/upload text before `render_document`. |
| 625-635 | `invoke_runtime_tool` (`image_tool`) | `IMAGE_CREATION` | **Not text LLM** | Socket image gen (`config/runtime.yaml:69-73`); stays in deterministic Auto unless policy moves image to specialized path. |

**Non-generate inference:** `IMAGE_CREATION` uses `invoke_runtime_tool` not `invoke_runtime_generate` (`auto_loop/execute/run.py:620-635`).

### Bucket summary (Auto forward pass)

| Bucket | Count | Action |
|--------|-------|--------|
| RUBBER-STAMP DELETE | 2 paths | MRIL hop-1 validate (`390-408`), TOOL_EXECUTOR arg-gen (`491-505`) |
| RELOCATE → per-episode Omega | 4 paths | Response (`447-468`), tool result synth (`563-568`), document fill (`791-796`) |
| Stay deterministic in Auto | Tool invoke, MRIL forced recall, image socket, IDLE | Execute raw tools; no `_generate` on happy path for USE_TOOL when `tool_call_is_direct` |

---

## 2. Tools inventory (Auto executable)

Tools are selected in Meta A (`meta_loop/tool/run.py`), executed via `_default_tool_invoker` (`auto_loop/execute/run.py:45-119`) → `invoke_runtime_backed_tool` (`runtime/runtime_tool_dispatch.py:28-107`) or schema `implementation_module` / `implementation_function`.

### Capability tools (USE_TOOL / LEARN → USE_TOOL action)

| Tool | Meta selection | Arg source today | Deterministic today? | Model arg-gen? |
|------|----------------|------------------|----------------------|----------------|
| `web_fetch` | Default LEARN (`meta_loop/tool/run.py:66-72`, `config/meta.yaml:92`); default USE_TOOL fallback (`meta_loop/tool/run.py:150-152`) | `execution_inputs.query`, `domain`, `topic_source`; `deterministic_args_from_order` adds `domain` (dewey category) + `max_results` (`runtime/tool_dispatch.py:87-113`) | **Yes** when `query` present in inputs (`auto_loop/route/run.py:17-20`, `config/auto.yaml:139-140`) | Only if `auto_web_fetch_requires_query` and query missing — then `TOOL_EXECUTOR` path |
| `file_reader` | Keyword match (`meta_loop/tool/run.py:83-86`) | `execution_inputs.query` | **Yes** — `auto_direct_tools_always` (`config/auto.yaml:134-138`, `pressure_loop/policy.py:1039-1046`) | No |
| `ocr_tool` | Keyword match (`meta_loop/tool/run.py:138-141`) | `execution_inputs.query` | **Yes** — direct_tools always | No |
| `desktop_tool` | Keyword match (`meta_loop/tool/run.py:142-148`) | `execution_inputs.query` | **Yes** — direct_tools always | No |
| `vision_reasoning_tool` | Vision keywords (`meta_loop/tool/run.py:87-108`) | `image_ref`, `question` | **Yes** — args fully specified by Meta | No |
| `face_tool` | Face keywords (`meta_loop/tool/run.py:109-137`) | `image_ref`, `operation` | **Yes** | No |
| `image_tool` | `IMAGE_CREATE` intent (`meta_loop/tool/run.py:155-162`) | `image_spec` / prompt via promptpack | **Yes** for IMAGE_CREATION action (socket, not LLM) | No |
| `document_tool` | `DOCUMENT_CREATE` intent (`meta_loop/tool/run.py:164-177`) | `document_spec` struct | **Partial** — spec deterministic; **body fill is model** (`execute/run.py:791-796`) | Document body only → relocate to Omega |
| `schema_injector` | Not in Meta tool map | Schema-only (`schema/tools/schema_injector.json`) | N/A | N/A |
| `read_operator_file` | Not in Meta tool map | Schema-only | N/A | N/A |

### Runtime-backed sockets (`config/runtime.yaml:68-90`)

`image_tool`, `vision_reasoning_tool`, `face_tool` — HTTP to omega-portable-lite; no LLM arg-gen.

### MRIL remember_* (substrate read, not Auto “tools” in USE_TOOL sense)

12 tools in `auto_loop/mril_tools/registry.py:23-36`. Invoked via `run_forced_memory_tools` / `invoke_remember_tool` → `memory.api.read_memory` (`auto_loop/mril_tools/remember.py:31-56`). **No model.**

### Operator verdict on model-composed args

**Mostly confirmed.** With `requires_query_in_inputs: true` for `web_fetch` (`config/auto.yaml:139-140`), LEARN and typical USE_TOOL paths supply `query` in Meta `execution_inputs`, so `tool_call_is_direct` is true and `TOOL_EXECUTOR` is skipped (`auto_loop/route/run.py:44-45`). Residual model arg-gen only when web_fetch runs without a bound query — policy/config edge, not the main LEARN path.

---

## 3. Fold plumbing (Auto → Meta B → Global B → Omega)

### Auto → episode fold

| Step | File:line | What survives |
|------|-----------|---------------|
| Execute | `auto_loop/execute/run.py:920-928` | `outputs` (raw `tool_result`, `response_text`, artifacts, `remembered_knowledge_tool_results`) |
| Fold | `auto_loop/fold/run.py:24-83` | `episode.auto` dict: `outputs.*`, `memory` envelope, `failure_*`, telemetry hashes |
| Loop write | `auto_loop/loop.py:151` | `episode.auto = folded` |

### Meta B intake (fold → reflection)

| Field | Source | File:line |
|-------|--------|-----------|
| `auto_outcome` | `episode.auto` + echo `execution_pack` | `meta_loop/consistency/run.py:30-78` |
| `execution_pack` | `episode.meta["execution_action_pack"]` | `meta_loop/consistency/run.py:45` |
| `source_action_goal` | `episode.global_["intent_decision"]` | `meta_loop/consistency/run.py:39-44` |
| Raw tool result | `auto_outcome.auto.outputs.tool_result` | `meta_loop/reflect/run.py:46-82` (`has_tool_result` in trace_summary) |
| Recall results | `outputs.remembered_knowledge_tool_results` | Folded in `auto_loop/fold/run.py:72-74` — **survives to Meta B** via `auto_outcome` |

Meta B reflect **does not copy** raw `tool_result` or recall blobs into `MetaReflectionPack` — only booleans in `trace_summary` (`meta_loop/reflect/run.py:79-84`). Full payloads remain on `episode.auto.outputs`.

### Global B package (Meta B → journal body)

| Artifact | File:line | Omega-readable? |
|----------|-----------|-----------------|
| `artifacts.tool_result` | `global_loop/package/run.py:100-105` | **Yes** — full raw result in episode outcome |
| `artifacts.tool_result_summary` | `global_loop/package/run.py:102` | Synth text (today from Auto; future from Omega) |
| `artifacts.response_text` | `global_loop/package/run.py:101` | Yes |
| `observations.memory_used` | `global_loop/package/run.py:97` | Status only |
| Recall items | Not in GlobalOutcomePack artifacts | Still on `episode.auto.outputs.remembered_knowledge_tool_results` until journal compaction |

### Where per-episode Omega would read recall vs result

**Today:** No per-episode Omega. **Insertion point:** immediately after `run_global_b(episode, reflection_dict)` at `cognition_chain.py:501`, **before** the `slice5_omega_front_enabled()` window gate at `cognition_chain.py:502-505`. Per-episode DeepSleep currently runs later via `_run_per_episode_terminus` (`cognition_chain.py:321-341`, triggered on episode close / tenth-episode pipeline).

**Inputs available on `episode` at that point:**

- Raw tool result: `episode.auto["outputs"]["tool_result"]` or `episode.global_["outcome"]["artifacts"]["tool_result"]`
- Recall: `episode.auto["outputs"]["remembered_knowledge_tool_results"]`
- Intent / action: `episode.global_["intent_decision"]`, `episode.meta["execution_action_pack"]`
- Meta B verdict: `episode.meta["reflection"]`

**Gap:** Journal compaction (`pressure_loop/episode_journal.py:compact_episode_for_reflection`) may elide fields — per-episode Omega should run **before** journal write or read live `episode` object, not compacted window bodies only.

---

## 4. Omega layer today

### Windowed reflection (live)

| Aspect | File:line | Detail |
|--------|-----------|--------|
| Trigger | `cognition_chain.py:502-504`, `502-521` | After Global B on **10th episode** when `store.is_tenth_episode_window_reflection()` |
| Cadence | `pressure_loop/reflection_window.py:15-32` (`WINDOW_SIZE` default 10), `core/cognition_chain_config.py:24-26` | Per **window**, not per episode |
| Sequencer | `omega_loop/loop.py:38-100` | intake → reason → reflect → package |
| Model call | `omega_loop/reason/run.py:79-92` | `operation="metacognitive_reasoning"`, thinking enabled |
| Prompt | `omega_loop/_prompt.py:20+` | Window-level JSON schema; no tool execution |
| Inputs | `omega_loop/intake/run.py:44-110` | 10 compact episode bodies, trajectory, mastery state, mastery deltas, open questions |
| Outputs | `omega_loop/package/run.py:27+` | `WindowReflection`, trajectory write (`write_trajectory`), pressure feedback for reseed |
| Post-Omega DeepSleep | `cognition_chain.py:385-394` | `run_deepsleep_for_window(..., skip_episode_level_phases=True)` |

### What must change for per-episode Omega cognition

| Need | Current | Change |
|------|---------|--------|
| Cadence | Window only (`omega_loop/loop.py`) | New `run_omega_for_episode(episode)` after Global B each episode |
| Prompt | Metacognitive window JSON (`omega_loop/_prompt.py`) | Action-specific prompt: intent, tool, raw result, recall envelope, mastery gap |
| Inputs | `OmegaInput` from locked window | Build from single `episode` + `episode.auto` + `episode.meta["reflection"]` |
| Outputs | Trajectory + window reflection | Mastery deltas, pressure params, optional episode-level reflection record — **must not double-write** mastery file (see §5) |
| Inference moved from Auto | Response, synth, document fill | Relocate call sites from §1 into new episode Omega reason phase |

Windowed Omega **stays** at episode 10 for trajectory/setpoint; per-episode Omega is **additive** fold-back cognition.

---

## 5. Mastery write ownership

### Authoritative counter file (`knowledge_mastery.json`)

| Site | File:line | What |
|------|-----------|------|
| **Only writer** | `deepsleep_loop/mastery/run.py:169` | `upsert_mastery(record)` inside `_apply_mastery_for_episode` |
| Store API | `deepsleep_loop/mastery_store.py:73-90` | `upsert_mastery` read-modify-write JSON |
| Invoked from | `deepsleep_loop/loop.py:192`, `338` | `mastery_phase.update_mastery(ds_input)` per-episode and window paths |
| Increment logic | `deepsleep_loop/mastery/run.py:149-154` | LEARN → `knowledge_mastery += inc`; USE_TOOL → `application_mastery += inc` (free counter — see LEARN map) |

### Secondary mastery **memory** writes (not the JSON store)

| Site | File:line | What |
|------|-----------|------|
| Narrative envelopes | `deepsleep_loop/consolidate_memory/run.py:126-181` | `_mastery_envelopes` → `memory.api.write_memory` type `mastery_progress` |
| Trigger | `deepsleep_loop/loop.py:226-231`, `406-413` | `consolidate_to_memory` after consolidate phase |

### Readers (not writers)

| Site | File:line |
|------|-----------|
| Omega window intake | `omega_loop/intake/run.py:18-31` (`load_mastery`) |
| Topic resolution gap | `cognition/topic_resolution/resolve_topic.py:200-226` |
| DeepSleep reseed | `deepsleep_loop/reseed/run.py:21-49` |
| Projection HUD | `deepsleep_loop/projection/run.py:42-43` |
| Learning state envelope | `pressure_loop/symphony_learning_state.py` (reads JSON) |

### Target registration (seed → gap: target > 0, actual ≈ 0)

| Mechanism | File:line | Behavior |
|-----------|-----------|----------|
| Default target on first write | `deepsleep_loop/mastery/run.py:78-95`, `144`, `166` | `target_mastery` from `action_goal.metadata.target_mastery` or `deepsleep_mastery_default_target()` (0.70) |
| First record creation | `deepsleep_loop/mastery/run.py:137-169` | Only on **successful** LEARN/USE_TOOL episode; knowledge jumps to `+0.10` on first LEARN — no durable `actual=0` state after first success |
| Gap-based LEARN topic | `cognition/topic_resolution/resolve_topic.py:200-226` | `_resolve_mastery_gap`: existing records where `target - knowledge_mastery >= 0.5` |
| Learning-debt topic | `cognition/topic_resolution/resolve_topic.py:248-267` | `top_weak` from learning_state envelope — domains with low mastery in composed snapshot |
| Pressure reseed | `deepsleep_loop/reseed/run.py:21-49` | `mastery_gap` contributors from records where `knowledge < target` |
| Contributor domain hint | `pressure_loop/pressure_field.py:1408-1515` | Scalar `learning_debt` weight; `primary_learning_debt_domain` (`autonomic/cognitive_churn.py:660-681`) picks one domain for churn |

**Per-episode Omega mastery ownership:** Omega should **compute** mastery intent; **single write** remains `update_mastery` / `upsert_mastery` in DeepSleep (or Omega emits `mastery_effects` consumed by consolidate without a second `upsert` path).

---

## 6. DeepSleep cadence

| Path | When | `consolidate_to_memory` | File:line |
|------|------|-------------------------|-----------|
| **Per-episode** | After each episode close (when `per_episode_deepsleep_close` + slice6) | **Yes** | `cognition_chain.py:330-341` → `deepsleep_loop/loop.py:155-303` → `consolidate_to_memory` at `226-231` |
| **Per-window** | After windowed Omega on episode 10 | **Yes** (skip episode-level mastery/failure/semantic if per-episode already ran) | `cognition_chain.py:385-394` → `deepsleep_loop/loop.py:121-152`, `333-425` → `406-413` |

`consolidate_to_memory` implementation: `deepsleep_loop/consolidate_memory/run.py:277+`.

**Verdict:** `consolidate_to_memory` runs **per-episode** (on close) **and** again at window boundary (with `skip_episode_level_phases=True` on live path to avoid duplicate mastery/failure/semantic).

---

## 7. `learning_debt` shape

| Layer | Shape | File:line |
|-------|-------|-----------|
| Pressure contributor | **Scalar** `float` weight ∈ [0, 0.50] | `pressure_loop/pressure_field.py:1408-1515` (`read_learning_debt`) |
| Contributor id | String type `"learning_debt"` | `pressure_loop/pressure_field.py:120`, `1576` |
| **Not** per-(lobe, subdomain) | Weight is global; details dict has `domains_count`, flags | `pressure_loop/pressure_field.py:1499-1509` |
| Episode envelope | `learning_debt_flags` bools + `mastery.top_weak` list (top 3 domains) | `pressure_loop/symphony_learning_state.py:55-62`, `156+` |
| Topic binding | Weakest domain name from `top_weak` | `cognition/topic_resolution/resolve_topic.py:253-267` |
| Churn scaling | `primary_learning_debt_domain(envelope)` → single domain string | `autonomic/cognitive_churn.py:660-681` |

---

## 8. Recall path (`run_forced_memory_tools`)

### Substrate read — no model

| Step | File:line |
|------|-----------|
| Entry | `auto_loop/mril_tools/run.py:16-79` |
| Signature | `run_forced_memory_tools(...) -> Tuple[List[Dict[str, Any]], MemoryEnvelope]` (`run.py:16-21`) |
| Per-tool | `auto_loop/mril_tools/remember.py:11-56` → `memory.api.read_memory(QuerySpec)` |
| Return shape | Each result dict: `kind`, `tool`, `memory_type`, `lane`, `lobe`, `evidence[]`, `confidence` (`remember.py:105-152`) |
| Registry | `auto_loop/mril_tools/registry.py:23-36` (12 `remember_*` tools) |

### Invocation sites

| Site | File:line | When |
|------|-----------|------|
| Auto execute (single-hop) | `auto_loop/execute/run.py:318-322` | `memory_recall_required` && `delivery_mode != "inference_tool"` — before response/tool |
| Auto execute (inference_tool hop) | `auto_loop/execute/run.py:412-416` | After hop-1 validation — forced recall replaces model-driven recall |
| Tests / scripts | `tests/core/test_auto_memory.py:81`, `tests/cognition/test_topic_resolution_3b.py:157`, `scripts/test_mril_single_hop_live.py:51` | — |

### Meta A gates recall

| Intent | File:line |
|--------|-----------|
| LEARN | `meta_loop/memory/run.py:100-107` — `remember_knowledge` on resolved topic |
| RESPOND (prior refs) | `meta_loop/memory/run.py:90-95` |
| USE_TOOL | `meta_loop/memory/run.py:114-136` |

**Note:** LEARN sets `memory_required=True` but execute still uses MRIL hop-1 model call when `memory_delivery_mode == "inference_tool"` (`meta_loop/memory/run.py:50`) — rubber-stamp path in §1.

---

## Relocation checklist (implementation preview)

1. **Delete** MRIL hop-1 `_generate` + `validate_mril_hop1_tool_call`; always `run_forced_memory_tools` for `memory_required`.
2. **Delete** `TOOL_EXECUTOR` `_generate` when Meta binds full args; enforce `tool_call_is_direct` for all tools.
3. **Remove** `auto_synth`, response `_generate`, document `_generate` from forward Auto; return raw outputs only.
4. **Add** `run_omega_for_episode` after `run_global_b` in `cognition_chain.py` with action-specific prompt consuming `tool_result` + `remembered_knowledge_tool_results`.
5. **Single mastery write** — Omega proposes; DeepSleep `update_mastery` persists (or Omega writes once into journal `mastery_effects` consumed by consolidate).
6. **Keep** windowed `run_omega_for_window` at episode 10 unchanged for trajectory/setpoint.

---

## Cross-reference

[`LEARN_MASTERY_APPLICATION_LOOP_MAP.md`](LEARN_MASTERY_APPLICATION_LOOP_MAP.md) documents why relocation matters: mastery counters are not grounded, LEARN recall-first is wired in Meta but undermined by inference_tool hop-1, and execution does not apply recalled knowledge to application_mastery.
