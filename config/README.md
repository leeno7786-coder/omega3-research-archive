# config — subsystem configuration

YAML parameter files consumed by the Omega 3.0 cognitive loop via `config.loader` or `pressure_loop/policy`.

| File | Subsystem | Purpose |
|------|-----------|---------|
| `auto.yaml` | auto_loop | Prompt templates, tool config, execution params |
| `boot.yaml` | boot / lifecycle | Component order, manifest paths |
| `cognition_chain.yaml` | cognition_chain | Executive cascade sequence |
| `curiosity.yaml` | curiosity | Bounded open-question concurrency |
| `deepsleep.yaml` | deepsleep | Consolidation, mastery, stores |
| `directives.yaml` | operator ingress | IMAGE_CREATE / DOCUMENT_CREATE |
| `dopamine.yaml` | neuromod | RPE weights |
| `skill_execution.yaml` | global/meta/auto | Phase 6 USE_SKILL routing, user skill trust, guard defaults |
| `global.yaml` | global_loop | Global A/B sequencer tunables. `intents` block is explicitly **NOT read at runtime** |
| `global_b.yaml` | global_loop | Global B assembly |
| `global_intent.yaml` | global_loop | **Governed mirror only** — opened_by → intent map. Live routing is hard-coded in `global_loop/intent/run.py`; not loaded at runtime |
| `journal.yaml` | omega / pressure | Episode journal JSONL |
| `memory.yaml` | memory | **Partially consumed**: `promotion`/`demotion`/`decay`/`contradiction` are live. `gates`/`layer_write_rules`/`recall`/`adversarial` blocks are self-labeled non-authoritative mirrors |
| `meta.yaml` | meta_loop | Meta A phases |
| `meta_b.yaml` | meta_loop | Meta B disposition |
| `omega.yaml` | omega_loop | Reason / reflect / package |
| `operator_chat.yaml` | pressure ingress | Chat adapter tunables |
| `pressure.yaml` | pressure_loop | Tick interval, thresholds, aggregation formula, ignition compare, refractory rules |
| `reflection_window.yaml` | pressure / omega | 10-episode window |
| `runtime.yaml` | runtime | Endpoints, sockets |
| `sensory.yaml` | sensory | Operator presence latch |
| `strength_index.yaml` | memory / neuromod | Recall robustness |
| `tools.yaml` | auto_loop | Tool registry |
| `tts.yaml` | runtime | Voice defaults |
| `guardian_response_classes.yaml` | guardian | Response class taxonomy |
| `knowledge.yaml` | pressure_loop/policy | Knowledge extractor artifact buffer tunables |
| `pressure_contributors.yaml` | pressure_loop | **Governed mirror only** — per-reader tunables, field→canonical-type map, retired contributors registry. Live contributor constants are in `pressure_loop/pressure_field.py`; not loaded at runtime |
| `web_sources.yaml` | tools | Web-fetch catalog |
| `document_templates/` | auto_loop | Shipped document skeletons + instructions for DOCUMENT_CREATE (`generic`, `letter`, `meeting_minutes`, `readme_spec`, `report`, `resume`, `summary`) |
| `loader.py` | config | Unified YAML loader (`load_subsystem_bundle`, cache, merge) |
| `schema_validate.py` | config | JSON Schema validation for governed bundles (`../schema/*.schema.yaml` — sibling of config/, not a subdirectory) |

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| YAML config files | 28 |
| Python helpers | 3 (`__init__.py`, `loader.py`, `schema_validate.py`) |
| Actively consumed YAML files | 26 |
| Governed mirror / not loaded at runtime | 2 (`global_intent.yaml`, `pressure_contributors.yaml`) |
| Stubs / placeholders | 0 |

**Governed mirrors (not runtime-consumed):**

- `global_intent.yaml` — kept as a human-readable/schema-checked reference, but `global_loop/intent/run.py` owns the live intent routing table.
- `pressure_contributors.yaml` — kept as a reference/retired-contributors registry, but live contributor logic and constants live in `pressure_loop/pressure_field.py`.

**Partially consumed configs:**

- `memory.yaml` — `enabled`, lifecycle sections, contradiction rules, and the
  `dynamic_y_lobes` onboarding flag are live. Gate permissions, recall routing,
  and adversarial limits are code-owned and are not duplicated in YAML.
- `global.yaml` — the `intents` block is explicitly **not read at runtime** (the file itself documents this).
- `auto.yaml` — rich prompt/model/routing metadata; keys are consumed by `pressure_loop/policy.py` and AUTO phases. (`ASK_OPERATOR_QUERY` previously lacked a model-profile arm; the arm now exists — `auto_loop/route/run.py:67-68` routes it to `TEXT_FAST`, matching `auto.yaml:175`.)

Historical loader targets that no longer have YAML bundles are not part of the
current configuration surface and should not be reintroduced.

**Single entry point:** Live production code consumes configs through `config.loader.load_subsystem_bundle(stem)`, `config.loader.load_<stem>_config()` helpers, or `pressure_loop/policy.py` private wrappers. Direct `open()` calls on `config/*.yaml` are rare and mostly limited to governance tests.

**Authority matrix:**

- Code in the top-level loop packages owns architecture, routing, and retired
  surfaces.
- A `config/<stem>.yaml` value is live only where a production reader consumes
  that field; otherwise it must not be present.
- `policy/symphony.policy.yaml` overlays only the sections explicitly merged by
  `pressure_loop/policy.py`; it is not a universal override.
- Other `policy/*.policy.yaml` files are governance contracts, except
  `shared_state.policy.yaml`, which is a live persistence policy.

**Phase 7 memory taxonomy:**

| Env var | Effect |
|---------|--------|
Taxonomy capture, compilation, and Meta A recall are always live. The only
remaining switch is `OMEGA_MEMORY_TAXONOMY_PRESSURE`, which enables the optional
advisory pressure contributor from the compiled graph (7M).

Authority boundary (Dewey vs taxonomy): `docs/architecture/MEMORY_AUTHORITY_BOUNDARY.md`.
Data root: `core/paths.py` → `MEMORY_TAXONOMY_ROOT` (`memory/taxonomy_store/`).

**Bundle consumers (live):** `core/boot_lifecycle.py` (boot), `cognition_chain.py` + `pressure_loop/policy.py` (cognition_chain), `core/skill_execution_config.py` (skill_execution), `sensory/operator_presence.py` (sensory), `pressure_loop/reflection_window.py`, `pressure_loop/operator_chat.py`, `runtime/tts.py`, `guardian/response_classes.py`, and `core/*_config.py` readers for memory, directives, and strength_index.

**Historical retirement note:** The former loader targets and the old Global A
`extract`/`goal`/`goal_state` configuration vocabulary are not current inputs.
The removed pressure contributors remain documented only in the governed
retirement registry.

**Permission policies** (separate layer): `policy/*.policy.yaml` under the root
`policy/` directory. `symphony.policy.yaml` is the cascade conductor policy;
`shared_state.policy.yaml` is the persistence contract.
