# Auto Loop — As-Built Map

> **Historical.** Slice-3 as-built map (2026-06-07). It is not authority for the
> live cascade. See [`COGNITIVE_CASCADE_CURRENT.md`](COGNITIVE_CASCADE_CURRENT.md).
>
> Live entry (as of 2026-09-19): `cognition_chain._start_auto` at
> `cognition_chain.py:3101` calls `run_auto_front`. Sequencer: `auto_loop/loop.py:173`
> `run_auto` → `auto_loop/loop.py:195` `_run_auto`; `run_auto_front = run_auto` at `loop.py:369`.

> **Status:** LANDED + live-proven (Slice 3, commits `cb329b46`, `88f32a63` on `main`).
> **Authority:** [`SLICE_3_AUTO_PACKETS.md`](SLICE_3_AUTO_PACKETS.md), [`AUTO_INTEGRATION_SURFACE.md`](AUTO_INTEGRATION_SURFACE.md), [`AUTO_BUILD_AUDIT.md`](AUTO_BUILD_AUDIT.md).
> **Mapper:** Devin · **Date:** 2026-06-07

**See Also:**
- [`README.md`](README.md) — Architecture directory index
- [`COGNITION_CHAIN_AND_PROMPTS_MAP.md`](COGNITION_CHAIN_AND_PROMPTS_MAP.md) — Cognition chain structure
- [`LIVE_CODEBASE_MAP.md`](LIVE_CODEBASE_MAP.md) — Live codebase mapping
- [`PRESSURE_LOOP_CLOSURE_MAP.md`](PRESSURE_LOOP_CLOSURE_MAP.md) — Pressure loop closure
- [AGENTS.md](../../AGENTS.md) — governing cognition cycle

---

## 0. What this file is

The **auto_loop** is the **model-running execution layer** between Meta A and Meta B in the cognition cascade. It is the **only model-running layer** between Meta A and Meta B. Omega also runs model inference, but after Global B on every episode (and separately for windowed reflection) — not only window-triggered. AUTO performs: validate → recall → promptpack → route → execute → fold → emit.

---

## 1. Purpose

AUTO transforms the `ExecutionActionPack` (from Meta A) into an `AutoOutcomePack` via one model inference pass (for RESPONSE/PROACTIVE_RESPONSE/TOOL_RESULT_SYNTHESIS) or direct tool invocation (for direct USE_TOOL / IDLE / IMAGE_CREATION). It is **semi-deterministic**: intake, validate, recall, promptpack, route, and fold are fully deterministic; execute involves model inference.

---

## 2. Structure

```
auto_loop/
├── loop.py              # Pure sequencer (run_auto, run_auto_front)
├── auto.py              # Compat re-export shim (Slice 3)
├── _auto_state.py       # Shared carriers + constants
├── intake/              # Episode → AutoInput
├── validate/            # ExecutionActionPack validation (Meta A hard-ban reuse)
├── recall/              # Pre-execute memory envelope
├── promptpack/          # Deterministic PromptPack construction
├── route/               # Model profile selection
├── execute/             # Model/tool/image execution
├── fold/                # Episode.auto fold + AutoOutcomePack emit
└── mril_tools/          # MRIL v2 deterministic memory routing
    ├── registry.py      # 12 remember_* tools → exact lobe mapping
    ├── remember.py      # Thin wrappers over memory.api.read_memory
    ├── router.py        # Query-type router → forced tool sequence
    ├── run.py           # Run forced MRIL tools (fail-hard)
    ├── subdomain_helpers.py  # Deterministic per-lobe subdomain routing
    ├── format.py        # Format remember_* outputs for inference
    └── lobes.py         # Lane/lobe scope helpers
```

---

## 3. Control + data flow

### Entry: `run_auto_front` → `run_auto`

`cognition_chain.py:3101` (`_start_auto`) → `auto_loop/auto.py` re-export → `auto_loop/loop.py:369` (`run_auto_front = run_auto`) → `:173` (`run_auto`) → `:195` (`_run_auto`)

```python
_run_auto(episode, *, runtime_client, tool_invoker, memory):
  ai = intake_phase.auto_input_from_episode(episode)   # AutoInput or None
  if ai is None: return None                           # loop.py:205-206
  lane_entry = open_lane_entry(episode, ai.execution_pack)
  ok, failure_kind, failure_reason = validate_phase.validate_for_execution(...)
  if not ok: return _build_failure_outcome(...)
  memory_envelope = recall_phase.memory_envelope_before_execute(...)
  prompt_pack = promptpack_phase.build_prompt_pack(...)
  model_profile = route_phase.select_model_profile(...)
  exec_result = execute(...)                           # Model/tool/image execution
  folded = fold_phase._fold_episode_auto(...)
  episode.auto = folded                                # loop.py:365
  return auto_outcome_from_fold(ai, folded)
```

---

## 4. Key seams

| Concern | File:Line | What |
|---------|-----------|------|
| **Intake returns None** | `auto_loop/intake/run.py:16` / `loop.py:205-206` | No `execution_action_pack` in episode → returns None → `_start_auto` raises RuntimeError |
| **Validate hard-ban reuse** | `auto_loop/validate/run.py:10-18` | Reuses `meta_loop.pack.validate_execution_action_pack` |
| **MRIL v2 deterministic routing** | `auto_loop/mril_tools/registry.py:23-36` | 12 `remember_*` tools, each maps to exactly one lobe |
| **Subdomain routing** | `auto_loop/mril_tools/subdomain_helpers.py:533` | `subdomain_for(text, lobe)` — pronoun (0,1), term-match (2,8,10), widened (others) |
| **Query router** | `auto_loop/mril_tools/router.py:219` | `route_memory_tool` / `route_memory_tools` — regex then default `remember_knowledge` |
| **Forced recall** | `auto_loop/mril_tools/run.py:31` | `run_forced_memory_tools` — fail-hard on remember_* read errors |
| **Fail-hard memory** | `auto_loop/execute/run.py:1788` | Memory read failures return `memory_continuity_substrate_invalid` |
| **Image creation** | `auto_loop/execute/run.py:1969` | `IMAGE_CREATION` records `runtime_unavailable` when the image socket declines |
| **Status/cycle_kind mapping** | `auto_loop/loop.py:28` | `failure_kind` → failed/fail; IDLE with no failure → idle/idle; else success/success |
| **Prompt type routing** | `auto_loop/promptpack/run.py:78` | `build_prompt_pack` — action + memory_required + heuristics |
| **Model profile routing** | `auto_loop/route/run.py:43` | `select_model_profile` — prompt_type + memory_enriched + tool_direct |
| **Episode.auto fold** | `auto_loop/fold/run.py:265` / `loop.py:365` | `_fold_episode_auto` hashes prompt texts; sequencer writes `episode.auto` |
| **Neuromod hook** | `cognition_chain.py:3132` | After AUTO, `_start_auto` computes neuromod and `set_current_memory_salience()` |

---

## 5. Consumes / Produces

### Consumes

| Source | Type | File:Line |
|--------|------|-----------|
| Meta A | `ExecutionActionPack` | `_start_auto` `cognition_chain.py:3101` via `episode.meta` |
| Pressure Loop | Pressure snapshot + trajectory | `auto_loop/intake/run.py:11` `auto_input_from_episode` |
| Memory API | Recall results | `auto_loop/mril_tools/remember.py:248` `invoke_remember_tool` |
| Runtime Client | Model inference | `auto_loop/execute/run.py:1445` `execute` |
| Tool Invoker | Tool execution | `auto_loop/execute/run.py:1445` `execute` (invoker / schema tools) |
| Policy | System prompts, keywords, limits | `auto_loop/promptpack/run.py:78` `build_prompt_pack` |

### Produces

| Destination | Type | File:Line |
|-------------|------|-----------|
| Episode Store | `episode.auto` dict | `auto_loop/loop.py:365` |
| Meta B | `AutoOutcomePack` | `_start_auto` returns into `_start_meta_b_global_b` `cognition_chain.py:3597` |
| Guardian Telemetry | Telemetry events | `auto_loop/fold/run.py:382` `_emit_fold_summary` |
| Neuromod | `episode.auto` dict | `cognition_chain.py:3132` |

---

## 6. Fail-fast classification

### Fail-fast (halt the cascade)

| Operation | File:Line | Condition |
|-----------|-----------|-----------|
| Intake returns None | `auto_loop/loop.py:205-206` | No `execution_action_pack` — cascade raises in `_start_auto` |
| Memory read fails | `auto_loop/execute/run.py:1788` | `run_forced_memory_tools` error → `memory_continuity_substrate_invalid` |
| Lane-entry refusal | `auto_loop/loop.py:237` | `LaneEntryRefused` — dependent action does not execute |

### Fail-soft (record and continue)

| Operation | File:Line | Condition |
|-----------|-----------|-----------|
| Runtime unavailable | `auto_loop/execute/run.py:3031` / `:3089` | Response generation preflight declined |
| Image generation fails | `auto_loop/execute/run.py:1969` | Image tool returns None → `runtime_unavailable` |
| Telemetry emit fails | `auto_loop/fold/run.py:382` | Exception in `_emit_fold_summary` `emit_data` |

---

## 7. Invariants

| # | Invariant | Enforced at | File:Line |
|---|-----------|-------------|-----------|
| 1 | Forced remember_* tools run as listed | `run_forced_memory_tools()` | `auto_loop/mril_tools/run.py:31` |
| 2 | Exact lobe per tool | `invoke_remember_tool()` | `auto_loop/mril_tools/remember.py:248` |
| 3 | Write-Y == Read-Y by construction | `apply_subdomain_override()` | `memory/gates/base.py:388` |
| 4 | Validation reuses Meta A hard-ban | `validate_for_execution()` | `auto_loop/validate/run.py:10` |
| 5 | Missing AUTO pack halts the cascade | `_start_auto` | `cognition_chain.py:3115` |

---

## 8. Related docs

- `docs/_archive/architecture/SLICE_3_AUTO_PACKETS.md` — Build spec
- `docs/architecture/AUTO_INTEGRATION_SURFACE.md` — Seam map
- `docs/_archive/architecture/AUTO_BUILD_AUDIT.md` — Verification
- `docs/architecture/COGNITION_CHAIN_MAP.md` — Cascade orchestration
- `docs/architecture/META_LOOP_MAP.md` — Meta A handoff source / Meta B handoff target
- `docs/architecture/PRESSURE_LOOP_MAP.md` — Pressure ignition

### Supersedes

This map supersedes any pre-Slice-3 auto_loop maps that did not document:
- MRIL v2 deterministic memory routing (12 `remember_*` tools, subdomain helpers)
- The fold phase (episode.auto structure, hashing)
- The compat shim pattern (`auto.py`)
- The fail-hard remediation

---

*Read-only audit. No code modified.*
