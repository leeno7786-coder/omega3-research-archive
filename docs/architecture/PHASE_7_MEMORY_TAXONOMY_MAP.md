# Phase 7A — Memory Surface Inventory & Taxonomy Integration Map

**Branch:** `memory-taxonomy` · **Worktree:** `F:\omega-memtax` · **Status:** 7A complete (inventory only, **no behavior change**).

This map inventories every existing memory surface so the Phase 7 deterministic memory
taxonomy layer (7B→7R) attaches to real shapes, never replaces load-bearing systems, and
honors the build doctrine:

```
Filesystem = immutable truth substrate   (append-only event ledger — canonical)
Graph      = deterministic cognitive projection   (compiled FROM the ledger — disposable)
Indexes    = deterministic recall accelerators
```

---

## 0. Two hard constraints that reshape the literal spec

### 0.1 Guardian is OUT of the memory surface (operator directive, 2026-06-21 — "period")

**Guardian's only tie is user-facing (operator/UI) input.** The Phase 7 taxonomy substrate
(ledger, graph, compiler, recall, capture, adapters, pressure-signals) is **autonomous
memory** and is built **Guardian-free**:

- No `guardian.decision.decide_request` / staging / `promote_memory` anywhere in `memory/taxonomy/`.
- No imports of `guardian.*` from the taxonomy layer.
- Phase 7 does **not** write through `memory/api.py:write_memory` (that path is Guardian-mediated
  for the user-facing `propose=True` case). It writes its **own** append-only ledger directly.

This is consistent with the existing autonomous path: `deepsleep_loop/consolidate_memory/run.py`
already calls `write_memory(..., propose=False, bypass_guardian=True)`. Phase 7 goes further —
**zero Guardian**. (See `feedback_guardian_scope`.)

### 0.2 Data root → `data/memory_taxonomy/` (a deliberate Guardian-free third tree)

`core/paths.py:6-20` enforces a sole-writer doctrine: every write today lands under exactly two
trees — `memory/` (Guardian-written) and `guardian/` (Guardian's own). **Both are
Guardian-associated**, so neither is acceptable for a Guardian-free substrate (constraint 0.1).
Therefore Phase 7 uses the spec's literal `data/memory_taxonomy/` as an **explicit, documented
exception** to the two-tree doctrine.

- Add a constant to `core/paths.py` (e.g. `MEMORY_TAXONOMY_ROOT = OMEGA_ROOT / "data" / "memory_taxonomy"`)
  so the path stays relocatable; derive all 7D ledger / 7H SQLite paths from it.
- Add `data/` to `.gitignore` (substrate is local, not version-controlled).
- Reuse the bare durability primitives `pressure_loop/durable_write.py`
  (`append_jsonl_line` = append+flush+`os.fsync`; `write_text_atomic` = tmp+`os.replace`) —
  these are Guardian-free fsync helpers, not a Guardian path.

---

## 1. The 12 mandated 7A questions — answered

| # | Question | Answer (exact location) |
|---|----------|-------------------------|
| 1 | Where are episode events stored? | Canonical continuity: **`pressure_loop/state/episode_journal.jsonl`** (append-only, `pressure_loop/episode_journal.py:archive_episode:367` via `durable_write` fsync). DeepSleep journal: `deepsleep_loop/state/journal.jsonl` (`journal_store.py:append_journal_entry:29`). No taxonomy/graph store exists — 7D adds it. |
| 2 | Where is skill-execution evidence folded? | `deepsleep_loop/skill_consolidation.py:consolidate_skill_episode:234`, driven from `cognition_chain.py:_run_skill_foldback:787` (`fold_skill_result` → `consolidate_skill_episode`); result stored on `episode.meta["skill_consolidation"]` (`cognition_chain.py:812`). |
| 3 | Where is capability reinforcement written? | Computed in `memory/capability_catalog.py:reinforce_capability:137` / `weaken_capability:184`; **persisted** by `memory/strength_index.py:StrengthIndex.reinforce:293` → `guardian/state/strength_index.json` (`write_strength` per lobe-5 leaf `5.0Y.qwenskill-<slug>`). Δ=0.05. |
| 4 | Where are failures recorded? | `deepsleep_loop/failure_store.py:upsert_failure:53` → `deepsleep_loop/state/failure.json`, keyed `"{kind}::{context}"`. Shape: frozen `FailurePattern(kind, context, domain_id, frequency, last_episode)` (`_deepsleep_state.py:249`). Two writers: deepsleep failure phase (`failure/run.py:149`) + skill path (`skill_consolidation.py:93`). |
| 5 | Where is recall performed? | A full deterministic recall engine **already exists**: `memory/api.py:read_memory:270` → `recall_compiler` → `recall_lanes` (16 lanes) → `recall_arbitrator` (scored, tie-broken by `memory_id`) → `recall_receipt`. In-cascade recall runs in AUTO (`auto_loop/mril_tools/`, `auto_loop/execute/run.py:350`), **after** Meta-A skill selection. No pre-selection recall hook exists (7N must add it). |
| 6 | Which writes are canonical? | The per-lobe JSON under `memory/<lobe>/<subdomain>/<id>.json` + index `memory/indexes/coord_index.json`, written via `memory/engine/validate_write.py:atomic_write:154` (reached by `write_memory` and `gate.ingest`). |
| 7 | Which writes are telemetry only? | `guardian/terminal_feedback.py:emit_data` (in-RAM ring only), `guardian/telemetry/writer.py:write_telemetry` (`guardian/telemetry/*.jsonl`), `guardian/data_logger.py:emit_event` (`guardian/logs/*.jsonl`). Observability, not truth, not fsynced. |
| 8 | Which data is append-only? | `pressure_loop/state/episode_journal.jsonl`, `deepsleep_loop/state/journal.jsonl`, `guardian/state/encoding_journal.jsonl` (fsynced WAL, the best reference idiom), `memory/queues.py` sidecars, `guardian/state/routing_edge_catalog.jsonl`, `guardian/ledger/chain.jsonl` (HMAC, sacred). |
| 9 | Which data is mutable state? | `guardian/state/strength_index.json` (per-id rewrite), canonical memory JSON (overwrite-by-id, supersedes chains), `memory/indexes/coord_index.json`, `deepsleep_loop/state/failure.json` (frequency table). |
| 10 | Which surfaces should emit taxonomy events? | The 5 cascade fold points (§3) read once at **episode close** (7F). Richest consolidation emitters: `consolidate_to_memory` (run.py:308), `consolidate_skill_episode` (skill_consolidation.py:234), `consolidate_failures` (failure/run.py:149). |
| 11 | Which systems must NOT be replaced? | §5. |
| 12 | What does the ledger consume first? | The closed `CognitiveEpisode` + its `OmegaEpisodeInput` packet (`omega_loop/episode/packet.py:139`), plus the per-category consolidation outputs and the `SkillConsolidationRecord` for USE_SKILL episodes. |

---

## 2. Surface inventory by classification

**Canonical truth (do not replace, do not route Phase 7 writes through):**
`memory/api.py` (`write_memory`/`read_memory`/`CanonicalEnvelope`/`QuerySpec`) · `memory/core.py:build_memory_dict` ·
`memory/engine/validate_write.py:atomic_write` + `memory/indexes/coord_index.json` ·
`memory/gates/*` (12 lobe gates; `SituationalGate`=lobe-4 episode store, `ToolsGate`/`WorkflowsGate`/`CodingGate`=skill stores) ·
`memory/capability_catalog.py` + `memory/strength_index.py` (capability/plasticity) ·
`pressure_loop/state/episode_journal.jsonl` (`pressure_loop/episode_journal.py`).

**Read/recall stack (compose with, do not fork — 7J/7N):**
`memory/recall_compiler.py` · `recall_lanes.py` (16 lanes, 12 Dewey lobes) · `recall_arbitrator.py`
(deterministic, tie-break `memory_id` asc) · `recall_posture.py` · `recall_aliases.py` ·
`recall_receipt.py` · `answerability_index.py` · `memory/gates/base.py:recall` · `auto_loop/mril_tools/*`.

**Append-only ledgers (reference idioms for 7D; do not overload):**
`memory/encoding_journal.py` (**best WAL reference** — open/step/close/abort + `recover_unclosed` + `compact_journal`, fsynced) ·
`pressure_loop/durable_write.py` (**reuse** `append_jsonl_line` / `write_text_atomic`) ·
`deepsleep_loop/journal_store.py` · `memory/queues.py` · `guardian/hmac_ledger.py` (tamper-evident chain reference for 7I).

**Telemetry only (may subscribe as an optional live mirror; never authoritative):**
`guardian/terminal_feedback.py` (`emit_data`, `phase_span`, `subscribe`) · `guardian/telemetry/writer.py` · `guardian/data_logger.py`.

**Schema definitions (house style for 7B — §4):** `schema/*.py`.

---

## 3. Episode field-path map — the adapter source map (7E) + capture source (7F)

The `CognitiveEpisode` (`pressure_loop/episode.py:125`) is the **single mutable truth object**:
opened at ignition, every movement writes onto the **same** instance held in `EpisodeStore._active`.
Adapters read these exact paths (`global_` carries trailing underscore in Python; JSON key is `global`):

| Cascade phase | Episode path | Key fields |
|---------------|--------------|-----------|
| Pressure ignition | `episode.pressure` (from `EpisodeIgnition`, `contributors.py:56`) | `aggregate`, `opened_by`, `contributors[].{id,weight,detail}`, `trajectory_snapshot` |
| Global A intent | `episode.global_["intent_decision"]` (`global_loop/loop.py:83`) | `intent` (∈ `ALLOWED_INTENTS` incl. `USE_SKILL`), `confidence`, `reason` |
| Meta A action | `episode.meta["execution_action_pack"]` (`meta_loop/loop.py:64`) | `action`, `tool`, `execution_inputs`, `memory_required` |
| Meta A skill selection | `episode.meta["skill_execution_pack"]` (`meta_loop/skill/run.py:283`) | `SkillExecutionPack` → `SelectedSkillDecision`: `skill_name`, `skill_id`, `execution_kind`, `allowed_tools`, `forbidden_tools`, `limits`, `dispatch_id` |
| AUTO execution | `episode.auto["status"]`, `["failure_kind"]`, `["neuromod"]` (`auto_loop/fold/run.py:24`) | execution status + telemetry |
| qwen skill **result** | `episode.auto["outputs"]["skill_result"]` (`QwenSkillExecutionResult`, `schema/qwen_skill_result.py:33`) | `status`, `tool_calls[]`, `files_changed[]`, `commands_run[]`, `tests_run[]`, **`diff_summary`** (dict `{git_diff_present, git_diff}` — *not* `diff`), `failure_class`, `guard_status`, `evidence`, `unresolved_blockers`, `dispatch_id`, `execution_kind` |
| (also) | `episode.auto["outputs"]["skill_dispatch" / "skill_status" / "skill_objective" / "skill_guard_status"]` | dispatch echo |
| Meta B (live) | `episode.meta["reflection"]` | consistency / disposition / status |
| Meta B skill | `episode.meta["skill_reflection"]` (`MetaBSkillOutcome`, `meta_b_skill_outcome.py:50`) | `outcome_kind` (9 kinds), `failure_class`, `safety_blocker`, `satisfied_dispatch`, `unresolved_blockers`, `guard_status` |
| Global B (live) | `episode.global_["outcome"]` (`GlobalOutcomePack`) + `["omega_packet"]` | `summary`, `grounded`, `followups`, `execution_outcome`, `response_text` |
| Global B skill | `episode.global_["skill_outcome"]` (`GlobalBSkillUpdate`, `global_b_skill_update.py:32`) | `intent_satisfied`, `objective_complete`, `more_work_needed`, `continuation_signals`, `continuation_pressure`, `close_episode`, `ask_operator` |
| Skill consolidation | `episode.meta["skill_consolidation"]` (`cognition_chain.py:812`) | `{record (SkillConsolidationRecord), applied, summary (deepsleep_skill_summary)}` |
| Per-episode Omega | `episode.global_["omega_result"]` (`cognition_chain.py:444`) | cognized result |
| Continuation (self-drive) | staged contributor via `cognition_chain.py:_stage_skill_continuation:751` | `continuation_depth`, `prior_episode`, `root_objective` (depth cap 6) |

**Deterministic per-episode digest (preferred 7F/7G source):**
`omega_loop/episode/packet.py:build_omega_episode_input:139` → `OmegaEpisodeInput`
(`episode_id, intent, action, tool, domain, operator_text, outcome, failure_reason, raw_tool_result,
recall, applied{status,summary,has_diff,files_touched,error}, mastery_agenda`) — assembled with **no
model call**, already attached at `episode.global_["omega_packet"]`. `OmegaEpisodeResult.knowledge_to_ingest`
(`tuple[KnowledgeProduct{domain,statement,confidence}]`) + `mastery_effects` are structured claim material for 7G.

---

## 4. Schema house style — the contract 7B must follow

From `schema/README_dataclasses.md` + the 9 skill-bridge schemas (newer idiom):

- **Pure stdlib `@dataclass(frozen=True)`** — *no pydantic, no attrs, no plain-dict*. Leaf-level
  (stdlib + dataclasses only, **no upward imports** into brain/guardian/omega/memory).
- Module-level version constant `<NAME>_SCHEMA_VERSION = "omega.<snake_name>.v1"`; field
  `schema_version: str = <CONST>` (the **last** `to_dict` key). Field `type: str = "<snake_name>"` discriminator.
- **Enums = module-level `frozenset` constants** (never `enum.Enum`); reject in `validate()` via
  `not in` / set-difference. The older core schemas use `typing.Literal` — 7B follows the **newer** frozenset idiom.
- **`validate(self) -> list[str]`** (returns errors, `[]`==valid, **never raises**); membership + cross-field checks.
- Hand-written **`to_dict(self) -> dict[str, Any]`** (type-first, `schema_version`-last; tuples→`list`, dicts→`dict`).
- Immutable defaults on a frozen dataclass: `tuple = ()` and `dict = field(default_factory=dict)`.
- **Fail-hard builders** define a typed `class <X>Error(ValueError)`, call `.validate()`, `raise <X>Error("; ".join(errors))`.
  Fail-closed variant (`normalize_qwen_result`) emits a status-tagged record instead of raising.
- Deterministic id: `json.dumps([...], sort_keys=True, ensure_ascii=False)` → `sha256[:N]`
  (`skill_execution_pack.py:_dispatch_id`; `failure_record.py:failure_fingerprint` normalizes components first).

> **DECISION (7B location):** the spec places schemas at `memory/taxonomy/schemas.py`. The
> `schema/` doctrine ("every schema defined once, here") is about **engine-registered I/O contracts**
> checked via `isinstance(obj, entry.input_schema)`. The taxonomy records are a **sidecar projection
> layer**, not cognition-engine I/O — precedented by package-local frozen dataclasses already living
> outside `schema/` (`deepsleep_loop/_deepsleep_state.py`, `omega_loop/episode/types.py`,
> `auto_loop/_auto_state.py`). 7B therefore keeps the spec's `memory/taxonomy/schemas.py` location
> **but adopts the `schema/` house style exactly** (frozen dataclass + frozenset enums + `validate()`
> + `to_dict()` + `_SCHEMA_VERSION`). These are **new** types (no redefinition → no registry collision).
> The taxonomy enum frozensets become the single source of truth that 7E/7F import.

---

## 5. What Phase 7 must NOT replace

1. `memory/api.py` (`write_memory`/`read_memory`/`CanonicalEnvelope`/`QuerySpec`) — *"MUST NOT be redefined elsewhere"*. Phase 7 sits alongside; does **not** write through it.
2. `memory/strength_index.py` + `memory/capability_catalog.py` — capability/plasticity store. 7L/7M **read** `write_strength`, never fork it.
3. `memory/engine/validate_write.py` + `coord_index.json` + per-lobe `memory/<lobe>/...` — the canonical substrate (source of truth).
4. The deterministic recall engine (`read_memory` + compiler/lanes/arbitrator/posture/receipt/answerability) — 7J composes/extends, never duplicates. Align to the 12-lobe / 16-lane taxonomy.
5. `pressure_loop/episode_journal.py` + `episode_journal.jsonl` — per-episode continuity (fail-hard, load-bearing). 7F hooks **alongside** `archive_episode`.
6. `deepsleep_loop/skill_consolidation.py` + `failure_store.py` (`FailurePattern`) — live capability/failure machinery. 7K/7L **consume** these (outcome_kind/failure_class/frequency), never re-classify.
7. `deepsleep_loop/journal_store.py` + `consolidate_memory/run.py:consolidate_to_memory` — canonical consolidation. Taxonomy events attach beside the canonical write.
8. Guardian (`guardian/*`) — Guardian's domain is user-facing input **only** (§0.1). Phase 7 never imports it.
9. `schema/*` existing contracts (incl. `memory_object.py` + `memory_schema.json`, the `SKILL_CLASSES`/`EXECUTION_KINDS`/`SAFETY_CLASSES` frozensets) — 7B adds alongside; reuses skill enums by import.
10. `core/paths.py` sole-writer doctrine — Phase 7 adds the `MEMORY_TAXONOMY_ROOT` constant and stays declarative; the `data/` tree is the **one documented exception** (§0.2).

---

## 6. Integration seams (where 7D–7N attach)

- **7D ledger:** new `data/memory_taxonomy/events/YYYY/MM/DD/events.jsonl`; reuse `pressure_loop.durable_write.append_jsonl_line` + `write_text_atomic`; mirror `memory/encoding_journal.py` recovery idiom. **Guardian-free, direct append.**
- **7E adapters:** read the §3 episode paths → `MemoryEvent`s. Pure functions, no mutation, no graph write. Skill evidence must preserve `skill_name/skill_id/execution_kind/dispatch_id/status/tool_calls/files_changed/tests_run/diff_summary/failure_class/guard_status`.
- **7F capture hook:** at episode close — `cognition_chain.py` close path (drives `pressure_loop/episode_journal.py:archive_episode:367`) / `EpisodeStore.close:326`. Read the fully-folded episode (or `enriched_episode_from_episode` / `OmegaEpisodeInput`) **once**, append the event chain to the 7D ledger. Failure-soft; never strands the episode. **No `write_memory`, no Guardian.**
- **7N recall-before-selection (most important wiring):** insert in `meta_loop/skill/run.py:attach_skill_execution_pack` **between** objective resolution (`:254`) and `select_skill_for_intent` (`:264`) — run taxonomy `recall(objective)`, feed prior failure/workflow signals into selection, attach `recall_packet` hash/evidence to the pack. Deterministic; degraded-safe on recall failure.
- **7K failure topology:** consume `MetaBSkillOutcome.outcome_kind` + `FailurePattern` frequency table; compile into graph edges (`FAILED_BECAUSE`/`WEAKENS`/`REOPENS`/`CREATED_PRESSURE`).
- **7L skill/workflow memory:** consume `SkillConsolidationRecord` + `_workflow_memory_candidate` (`skill_consolidation.py:166`); read `capability_strengths_for_catalog` for reliability context.
- **7J recall + 7M pressure:** compose with `read_memory` (project results into the graph, or add a taxonomy lane/posture); pressure-signals read graph + `strength_index.aggregate`, emit **candidates only** (no action selection, no episode open).

---

## 7. Acceptance (7A) — met

- ✅ Map names exact files/functions (with line refs).
- ✅ No code behavior changes (this commit adds one doc only).
- ✅ Current tests remain green (markdown-only change).
- ✅ Identifies existing canonical memory writes (§2, §5).
- ✅ Identifies integration seams for taxonomy events (§3, §6).
- ✅ Two doctrine corrections locked (Guardian-free §0.1; data root §0.2).
