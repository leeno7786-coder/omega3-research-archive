# schema — canonical engine contracts

**What it is** — This directory owns the engine-registered wire contracts and
the persisted memory validator. Most frozen dataclasses used by the cognition
cascade live here, while package-local runtime records and dictionary-shaped
outcomes remain owned by their producing packages. YAML/JSON files here are
runtime validators only when a live loader or persistence boundary names them.

**Historical relocation note:** This directory was moved to top-level `schema/`
during the core cleanup. The current source of truth is this directory.

## Key files / areas

| File / area | What it is |
|---|---|
| `tool_spec.py` | `ToolSpec`, `ToolParameter`, `SafetyClass`, `ToolOriginType` — the canonical tool-registration contract. Carries `implementation_source: str = ""` (generated module source for synthesized tools; `""` = points at an existing module via `implementation_module`/`implementation_function`). Live helper: `load_spec_from_schema_file()`. `ToolSpec.to_openai_tool()` and `parse_tool_calls_from_text()` are retained but currently unused in production. |
| `tools/web_fetch.json` | The one canonical on-disk tool schema. Round-trips with `load_spec_from_schema_file()`; the reference example for the shape. |
| `tools/web_fetch.py` | The `web_fetch` tool I/O dataclass + implementation. |
| `tools/__init__.py` | Marks `tools/` as a package. |
| `tools/generated/` | Reserved package location for synthesized tools; currently only an empty package marker is present. `tool_spec.implementation_source` carries generated module text when that contract is used. |
| `memory_schema.json` | Memory-object shape (id/coord/type/title/summary/provenance/flags/audit required) validated before persist. |
| Historical goal contract | The pre-v2 goal object is not a current schema producer. Global A emits `IntentDecision`; Global B emits `EpisodeOutcome` (see the two rows below). |
| `IntentDecision` (live; **not** in `schema/`) | The v2 Global→Meta object — `IntentDecision(episode_id, intent, confidence, reason)`, defined in `pressure_loop/contributors.py`. Global A parses an **intent** (one of `ALLOWED_INTENTS`) from pressure + episode context — **no goal, no target** — and writes it to `episode.global_["intent_decision"]` (`global_loop/loop.py`). `global_loop/assemble/run.py` dual-reads `intent_decision or action_goal` as an intentional **compat alias** so pre-v2 fixtures / locked windows still parse; `action_goal` is not a live producer. |
| `EpisodeOutcome` (live; dict shape, **not** a `schema/` dataclass) | What Global B emits to Omega — `intent × trajectory_alignment × divergence_score × execution_outcome` (no `goal_state`), assembled in `global_loop/package/run.py` and landed at `episode.global_["outcome"]["outcome"]` (`global_loop/loop.py`). Replaces the retired goal-reflection / `goal_state` phase. |
| `failure_record.py` | `FailureRecord` — exception promoted to first-class data. |
| `omega_trajectory.py` | Omega-layer trajectory record. |
| `provenance.py` | Origin stamp carried by tool_spec + failure_record. |
| `curiosity_engine.py` | Curiosity-engine entry + literals (HUD curiosity store + server). |
| `execution_skill.py` | `ExecutionSkill` + frontmatter validator — canonical bounded-box contract for SKILL.md (Phase 6B). Declares `skill_class`, `intents`, `execution_kinds`, `domains`, allowed/forbidden tools, safety class, hard limits, evidence, completion criteria, structured output schema. Cross-field safety rules live here (read-only must not write, destructive must confirm, `self_modification` denied by default). The config wire is `skill_execution.schema.yaml`. |
| `selected_skill.py` | `SelectedSkillDecision` — Meta A's chosen `ExecutionSkill` (Phase 6F). Carries allowed/forbidden tools, limits, evidence requirements, completion criteria, output schema, and structured `failure_reason` on no-match. Meta A selects only; it never executes tools or writes prompts. |
| `skill_execution_pack.py` | `SkillExecutionPack` — Meta → Auto hand-off contract (Phase 6G). Carries selected skill, tool permissions, safety envelope, hard limits, evidence requirements, and an exact echo of the source action goal. Deliberately carries no prompt text. |
| `qwen_skill_dispatch.py` | Skill dispatch wire contract; the filename is historical. AUTO pushes the selected SKILL.md body through this contract, and the executor does not discover skills independently. |
| `qwen_skill_result.py` | Skill execution result wire contract; the filename is historical. Fail-closed normalization distinguishes malformed results, invalid dispatches, and unknown outcomes. |
| `meta_b_skill_outcome.py` | `MetaBSkillOutcome` — Meta B's classification of a bounded skill-execution result into a `skill_*` outcome kind. Flag-gated by `OMEGA_USE_SKILL_ROUTING`. |
| `global_b_skill_update.py` | `GlobalBSkillUpdate` — Global B's integration of a skill outcome (Phase 6K). Records whether the result satisfied Global intent, whether the objective is complete, blockers, continuation signals, and whether to create continuation pressure. |
| `skill_consolidation_record.py` | `SkillConsolidationRecord` + `CapabilitySignal` — Omega/DeepSleep consolidation of a skill episode (Phase 6L). Records full execution provenance (selected skill, what it did, outcome, capability reinforcement/weakening signal). |
| `routing_policy.py` | Routing policy guard (Phase 6M). Enforces `Global USE_SKILL → Meta selected skill → Auto bounded dispatch → executor tools`; Omega's deterministic substrate remains the allowed exception. |

## Entry points / how it's used

- **Registered phase contracts import from here.** The codebase is schema-first
  where a phase I/O registry names a contract: `from schema.<name> import
  <Schema>`. Package-local records remain owned by their runtime package.
- **`tool_spec.ToolSpec`** is the registration contract. `tool_policy` resolves `implementation_module` + `implementation_function` to a real callable; synthesized tools instead carry `implementation_source` (generated module text) and `origin_type="synthesized"`, and must clear an extra verification step before registration.
- **JSON validators** are consumed at runtime: `memory_schema.json` before memory persist. SharedState persistence is validated by `core.shared_state_keys.KEY_REGISTRY` and its typed write boundary.
- **`tools/web_fetch.json`** is loaded via `load_spec_from_schema_file()`; new on-disk tool schemas follow the same round-trip shape.

## Depends on / depended-on-by

- **Depends on:** almost nothing — `tool_spec.py` pulls `schema.provenance`; otherwise the package is leaf-level by design (stdlib + dataclasses only).
- **Depended-on-by:** `auto_loop`, `global_loop`, `meta_loop`, `omega_loop`, `guardian`, `memory`, `runtime`, and policy/config readers. This package is the bottom of the import graph; those consumers conform to these shapes.

## Gotchas / doctrine

- **Schemas live ONLY here.** A new field → edit the canonical file. A new schema → add it here (and to `PHASE_IO_SCHEMAS` if it's phase I/O). No parallel schema definitions, no alias zoo. This is the anti-stale-doc, anti-drift rule.
- **Historical retirement note (2026-07-04):** unused dataclass mirrors and
  duplicate wire schemas were removed. The remaining autonomic YAML schemas are
  live contracts; do not recreate parallel Python mirrors.
- **Historical goal note:** the old goal layer is not a live producer;
  `action_goal` / `goal_state` survive only as compatibility-shaped reads.
- **Canonical references:** `AGENTS.md` is the governing architecture contract;
  package READMEs and current tests are the implementation references. Dated
  documents under `docs/contracts/` are historical unless explicitly marked
  current.
- **See also:** `schema/README.md` — YAML/JSON wire schema contracts.
