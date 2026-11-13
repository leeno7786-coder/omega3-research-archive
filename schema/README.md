# schema — per-subsystem JSON Schema contracts

`schema/` is the canonical home for engine-registered dataclasses and YAML/JSON
wire contracts used by cognition, execution, memory, runtime, and policy
loaders. Package-local records and dictionary-shaped outcomes remain owned by
their producing packages. A file is a runtime validator only when a live loader
or persistence boundary names it.

## Live Contract Areas

- `emergent_pressure.py`, `motor_program.py`, and `execution_evidence.py` define
  pressure, motor, and executor evidence shapes used by the cognition cascade.
- `execution_skill.py`, `selected_skill.py`, `skill_execution_pack.py`,
  `global_b_skill_update.py`, `meta_b_skill_outcome.py`, and
  `skill_consolidation_record.py` define the bounded skill path.
- `omega_trajectory.py`, `pathway_outcome.py`, `proactive_lifecycle.py`, and
  `curiosity_engine.py` define trajectory, credit, proactive, and curiosity
  records.
- `routing_policy.py`, `tool_spec.py`, `failure_record.py`, and
  `provenance.py` define execution guards, tool registration, failure, and
  provenance vocabulary.
- Live episode timing is produced by `core/episode_temporal.py` and
  `core/symphony_time.py`. The former session-only phase-clock schemas were
  removed because no runtime consumer loaded them.
- `qwen_skill_dispatch.py` and `qwen_skill_result.py` are current skill wire
  contracts with historical filenames. They do not imply a separate model
  fleet or execution lane.

## Wire Schemas

The YAML contracts cover the loop/config surfaces, including `auto`,
`deepsleep`, `global`, `global_b`, `global_intent`, `journal`, `knowledge`,
`meta`, `meta_b`, `omega`, `pressure`, `pressure_contributors`, `runtime`,
`skill_execution`, `tools`, and `web_sources`.

Infrastructure contracts include `boot`, `cognition_chain`,
`reflection_window`, `operator_chat`, `sensory`, `tts`, `directives`,
`memory`, `curiosity`, and `guardian`. `memory_schema.json` is the persisted
memory-object validator. SharedState persistence is validated by
`core.shared_state_keys.KEY_REGISTRY` and its typed write boundary.

`tools/web_fetch.json` is the canonical on-disk tool schema and round-trips
through `tool_spec.load_spec_from_schema_file()`. `tools/generated/` is only an
empty package marker at present, not an active generated-tool registry.

The MCP package, tool-catalog, and resource-catalog JSON files are structural
release schemas. Their cross-field trust, digest, uniqueness, and secret-path
rules are enforced by `mcp_platform.package_contracts`; the plugin manifest's
cross-object rules are enforced by `plugin_platform.contracts`.

## Historical Boundaries

- The former goal contract is not a current schema producer. Global A emits
  `IntentDecision` from `pressure_loop.contributors`; Global B emits the
  `EpisodeOutcome` dictionary outside this package.
- Historical classifier-era and duplicate dataclass contracts are not live
  routing authorities. Current memory routing is deterministic and current
  semantic perception is the binary Ettin retina candidate lane.
- `ToolSpec.to_openai_tool()` and `parse_tool_calls_from_text()` remain
  compatibility helpers without current production callers.

## See Also

- [`README_dataclasses.md`](README_dataclasses.md)
- `AGENTS.md` and the package-local contract tests
