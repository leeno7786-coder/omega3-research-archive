# memory — the persistent intelligence substrate (Dewey 3D cortex)

**What it is** — `memory/` is the only place Omega's state crosses a
session boundary. Every fact, event, persona, decision, and self-model
fragment lives here as a schema-valid JSON record at a deterministic 3D
coordinate `(X, Y, Z)`, indexed for retrieval and tracked for strength
(engram model). It is gate-routed and SourceLedger-backed. Fresh intake and
cognitive self-writes first enter episode SRAM; DeepSleep is the only
write-capable drain into canonical filesystem truth. Guardian arbitrates
untrusted external human-authored ingress, not the mind's own writes.

**Deterministic routing:** recall and ingest share **grammar routing** —
`route_memory_tool` + `subdomain_for` pick lobe/subdomain; gates apply the same
helper on write so **write-Y == read-Y** by construction. Undecidable free text
lands in per-lobe `_unsorted` (Y=127). Semantic perception is a candidate-only,
brain-resident Ettin retina that produces binary codes; no dense vector store or
runtime embedding/classification service is part of the live memory path. Per
doctrine: *memory outranks inference; persistence > performance; deterministic >
magical.*

---

## Key files / areas

| Path | Role |
|------|------|
| `api.py` | **The canonical adapter.** `read_memory` / `write_memory` / `promote_memory` / `rollback_memory` / `decay_tick` / `resolve_contradiction` / `memory_health`. Every cognitive loop crosses the cognition↔persistence boundary here. cycle_id required on every call. |
| `core.py` | Shared pipeline helpers: `build_memory_dict`, `safe_coord`, `make_id`, `now_iso`. Turns raw text+coord into a schema-valid dict. |
| `dewey_map.py` | **Single source of truth for the coordinate system.** `LOBE_MAP`, `SUBDOMAIN_MAP`, `KNOWLEDGE_DOMAINS`, `CODING_DOMAINS`, `ENTITY_BEARING_LOBES`, `SCRIPTED_LOBES`, and `resolve_path`. |
| `promotion.py` | User-chat-only `create_staging → simulate_promotion → commit_promotion → rollback_promotion`. Staging lives at `guardian/state/staging/`. |
| `encoding.py` | Post-commit L0 atomic encoding (self-vector, contradiction precheck, persistence anchor, reflection eligibility, and DeepSleep candidacy). Full pipeline or roll back. |
| `rollback.py` | Inverse of `atomic_write`; invoked by `encoding.py` when a post-commit step fails (no partial state). |
| `consolidation.py` | Low-priority background worker; target of `decay_tick()` (StrengthIndex flush, contradiction candidate drain, cooperative yield). |
| `contradiction_resolution.py` | Drains `deepsleep_candidates.jsonl`, groups `contradiction:canonical=…;evidence=…` reasons, adjudicates via `core/memory_contradiction.py`, applies loser confidence penalty + audit persist. |
| `demotion.py` | One-stage lifecycle demotion on contradiction penalty/strike or sustained below `demotion.confidence_floor` (`StrengthIndex.below_floor_cycles`). |
| `confidence_decay.py` | Lazy `provenance.confidence` decay on `read_memory` recall from governed `decay.*` rules + pressure tick interval. |
| `queues.py` | Append-only jsonl sidecars `reflection_eligible.jsonl` + `deepsleep_candidates.jsonl` under `guardian/state/queues/`. Producers at write time, drained by Reflection / DeepSleep / consolidation. |
| `strength_index.py` | Per-memory engram tracker (write_strength, touches, decay). Always via `ensure_strength_index()`; canonical path resolved by the module. |
| `supersedes.py` | Revision-chain management (`X — was Y`); detect_collision + link_supersedes, fired post-commit on entity-bearing lobes. |
| `knowledge_router.py` | Dynamic Z-cluster allocation under `200_knowledge/<subdomain>/` (mkdir-on-first-write, deterministic dirname). |
| `recall_*.py` | Posture-aware recall pipeline. **External production callers:** `recall_lanes.py`, `recall_verifier.py`. The rest (`recall_posture`, `recall_plan`, `recall_compiler`, `recall_arbitrator`, `recall_receipt`, `recall_aliases`, `answerability_index.py`) are wired internally through `memory/api.py`/`recall_compiler` and have no direct external callers. |
| `auto_loop/mril_tools/subdomain_helpers.py` | Deterministic per-lobe subdomain routing (`subdomain_for(text, lobe)`). Pronoun-based for identity/persona; word-boundary term-match for knowledge/coding/workflows. |
| `memory/gates/subdomain_override.py` | Slice 2 ingest override — `apply_subdomain_override` sets the deterministic helper Y over the gate-seeded default (Y=0) at the ingest convergence point. |
| `autonomic/symphony_neuromod.py` | Slice 3 salience compute + ambient latch — `compute_memory_salience`, `set_current_memory_salience`, `current_memory_salience`. |
| `evidence_set.py`, `self_vector.py`, `promotion.py` | Recall evidence merge; derived identity self-vector; user-chat promotion pipeline. |
| `answerability_index.py` | **Internal-only** — used by `recall_arbitrator` and `taxonomy/recall`, no dedicated tests, no external callers. |
| `coding_project.py` | Coding-lobe project helpers (used). |
| `engine/` | `validate_write.py` (schema + atomic write + index patch), `walker.py` + `walker_loader.py` (deterministic composite-score search + `walker_query` adapter), and `reindex.py` (rebuild coord_index from disk). The live engine has no learned classifier on its routing path. |
| `gates/` | `base.py` = `MemoryGate` (ingest/recall/recall_recent, permission checks, user-input raw-text guard, deterministic coordinate routing, enrichment, atomic write, strength wiring, post-write entity-link/supersedes hooks). `lobe_gates.py` = the 12 concrete per-lobe gates. |
| `enrichment/` | Per-lobe decomposers aggregated through `memory/enrichment/__init__.py`: `identity`, `personas`, `knowledge`, `situational`, `vision_enrichment`, `journal`, `coding`, `workflow`, plus `entity_linker`. Gates reference the package; a decomposer failure is not committed as a successful write. |
| `indexes/` | `coord_index.json` (master id → {coord, path, title, type, canonical, domain}; walker reads in-memory with mtime invalidation). Any older label-map artifact is not a live recall authority. |
| `tmp_writes/` | Engine atomic-write tmp dir (write → fsync → atomic rename; cleared per write). |
| `000_identity/ … 1100_dev/` | The 12 lobe directories — the persisted corpus. |

---

## Entry points / how it's used

Two surfaces:

- **`memory.api`** (canonical, cycle-bound) — every auto/global/
  DeepSleep/Omega phase that touches memory goes through this. Reads
  return a `RetrievalResult` (`candidates` + posture/receipt `meta`)
  and touch StrengthIndex on every hit (reconsolidation — diagnostic
  peeks must call `walker_query` directly). User-chat writes submit a
  `CanonicalEnvelope` with `propose=True` (Guardian-arbitrated); internal/
  mind writes are DIRECT (`propose=False, bypass_guardian=True`) — see below.
- **`memory.gates.lobe_gates`** (direct, per-lobe) — used by boot-time
  seeds, deterministic source-tagged ingest, and tests. One gate per
  lobe; each reads only its lobe and writes only its lobe.

**Guardian gates UNTRUSTED EXTERNAL HUMAN-AUTHORED ingress (user chat + connector/Workspace observations — see `guardian/external_ingress.py` and root `AGENTS.md`), never the mind's own writes.** Every internal/mind write —
episodic capture, situational/temporal records, learned knowledge,
identity/traits, graph projection, recall reconsolidation, consolidation —
is the mind writing to its own canonical memory and is therefore DIRECT
(`write_memory(propose=False, bypass_guardian=True)`),
never `propose=True` / `guardian.decide_request`. The Guardian path is
reserved for USER CHAT crossing into the system: `write_memory(propose=True)`
stages an envelope, then calls `guardian.decide_request`, the autonomous
immune authority that ALLOWs/REJECTs/FLAGs/QUARANTINEs that external input.
On ALLOW, `commit_promotion` runs the atomic encoding chain
(`encoding.encode_committed_memory`); any encoding-step failure triggers
`rollback.rollback_canonical_write`.

`resolve_contradiction` deliberately does NOT call Guardian — it
queues to the deepsleep_candidates jsonl and returns `resolved=False`;
consolidation (`run_consolidation_once`) drains those entries, runs
governed adjudication (`config/memory.yaml` → `core/memory_config.py`),
subtracts `loser_confidence_penalty` from the loser's
`provenance.confidence`, and appends an audit entry. Canonical losers
are penalty-immune. Lifecycle demotion fires when confidence drops below
`demotion_eligible_below`, when `demotion.contradiction_threshold` strikes
accumulate, or when confidence stays below `demotion.confidence_floor` for
`sustained_floor_cycles` pressure ticks (tracked on recall). Further
restructuring writes still
use the direct memory path. (Guardian guards the external boundary; the mind solves problems.)

`verify_promotion` enforces `promotion.observation_to_episodic.min_confidence`
from the same bundle (`promotion_confidence` check after guardian envelope).

---

## Depends on / depended-on-by

**Depends on:** `core.paths.OMEGA_ROOT` (all paths derive from here —
no env vars, no relative paths), `schema/memory_schema.json`
(the persisted-memory schema), `guardian` (external-ingress decisions and
telemetry), `memory.write_guard`, and `memory.retina` / `memory.ettin_encoder`
for candidate-only semantic perception.

**Depended-on-by:** `auto_loop` (recall via `memory.api.read_memory`),
`global_loop` (knowledge frontier reads), `deepsleep_loop` (queue drains,
contradiction adjudication, journal-only writes, consolidate_memory
promotion), `omega_loop` (identity + self-inquiry), `guardian`
(gates USER-CHAT ingress only — internal/mind writes are direct),
`pressure_loop` (episode journal reads for Omega window), `autonomic/`
(situational/temporal event recollection, strength_index).

---

## Current State / Legacy Findings

The current memory surface is the canonical API, the Dewey map, deterministic
gate routing, the walker/index, the recall verifier, StrengthIndex, DME, the
binary semantic retina, and the optional taxonomy compiler. Historical
classifier-training artifacts are not part of runtime routing.

### Internal-only / narrower surface than docs imply

- Most `recall_*.py` modules (`recall_posture`, `recall_plan`, `recall_compiler`, `recall_arbitrator`, `recall_receipt`, `recall_aliases`) are wired internally through `memory/api.py` → `recall_compiler`. Only `recall_lanes.py` and `recall_verifier.py` have external production callers.
- `memory/enrichment/*.py` individual decomposers have no external callers; gates reference the package via `memory/enrichment/__init__.py`.
- `memory/answerability_index.py` is internal-only (used by `recall_arbitrator` and `taxonomy/recall`).
- `memory/taxonomy/entities.py` and `memory/taxonomy/failure_topology.py` are used internally by the compiler but have no dedicated tests.

### Live hot path

The canonical production surface is `memory/api.py` (`read_memory` / `write_memory`), backed by `memory/dewey_map.py`, `memory/engine/walker.py`, `memory/engine/validate_write.py`, `memory/recall_verifier.py`, `memory/strength_index.py`, `memory/dme/adapter.py`, and `memory/taxonomy/compiler.py`.

---

## Deterministic two-lane routing (Slices 1–3)

### Recall lane

When `memory_required` is true, Meta B routes one or more
`remember_*` tools. The router (`auto_loop/mril_tools/router.py`)
calls `subdomain_for(query, lobe)` to pick a deterministic subdomain
Y based on the query text:

- **Identity (0)** — first/second/third-person pronouns + keyword
  triggers (`traits`, `open questions`, `rules`, `never`).
- **Persona (1)** — first-person → operator (`Y=0`); third-person →
  person_stub (`Y=1`).
- **Knowledge (2)**, **Coding (8)**, **Workflows (10)** — longest
  word-boundary keyword match against `SUBDOMAIN_MAP` names for that
  lobe.
- **Vision (7)** and all other lobes — helper returns `None`; recall
  widens to the full lobe (no subdomain constraint).

The tool spec carries `"subdomain": Y`. `remember.py` passes it as
`allowed_subdomains=[Y]` inside `QuerySpec`. In `walker_query`, this
translates to `force_lobe=lobe` + `allowed_subdomains=[Y]`. The walker seeds
candidates directly from `list_ids_in_subdomain(X, Y)`.

**Result:** MRIL recall is deterministic zero-shot — no model inference
in the read path for helper-routed lobes.

### Ingest lane (write-Y == read-Y)

At the gate ingest convergence point (`memory/gates/base.py` ~line 342),
after the gate seeds `coord=[lobe,0,0]` from its contract but before the
permission check, `apply_subdomain_override(text, coord, label_id)`
runs. If the deterministic helper returns a Y for this lobe differing
from the seeded default, the helper **wins** — coord and label_id
are rewritten. Telemetry event `ingest_subdomain_override` fires when
an actual override happens.

**Result:** the record lands at the same (X, Y) that recall will
search, making the write-read loop invariant by construction. Helper-`None`
static-lobe text lands in `_unsorted` (Y=127) via
`route_undecided_subdomain`; knowledge/coding use the dynamic-Y
`subdomain_allocator`.

### Salience lifecycle (Slice 3)

After AUTO completes, `cognition_chain._start_auto` calls
`autonomic/symphony_neuromod.compute_episode_neuromod(auto, baseline)` — a
pure RPE formula (signed dopamine, |RPE| plasticity, stress, serotonin)
— and writes the dict onto `episode.auto["neuromod"]`. It then calls
`set_current_memory_salience(neuromod)` — a **single ambient latch**
for the cycle. The old "project a spike record" / `LAST_NEUROMOD_SPIKE`
shared-state path is deleted.

`compute_memory_salience` converts:
- positive **dopamine** + **plasticity** → `scale` ≥ 1.0 (stronger
  initial encoding)
- **stress** → `tau_factor` < 1.0 (shorter retention)
- **serotonin** proxy (derived from negative dopamine) → `tau_factor`
  > 1.0 (longer retention)

Every gate write calls `StrengthIndex.register(mid, write_strength)`.
If no explicit `salience` is passed, `register` samples the ambient
latch at that instant: `write_strength *= scale`, and `encode_tau =
DECAY_TAU_SECONDS * tau_factor` is stored per-memory. `get_strength()`
reads the per-memory `encode_tau` at recall time.

**Result:** biological salience modulates encoding strength and decay
per cycle, without changing the deterministic routing logic.

---

## Episode SRAM and the DeepSleep boundary

`memory.episode_sram` is the intake and working-memory bucket for the active
episode. Operator turns, connector observations, execution reports, cognitive
self-writes, and other fresh memory all enter this bucket first. Recall does
not read it; recall sees only the canonical filesystem and its graph
projection as they existed before the episode.

DeepSleep owns the only write-capable drain. During its active
`EpisodeStore.close(..., before_finalize=...)` transaction it routes every
held envelope through ingestion, writes canonical filesystem truth, reconciles
contradictions, rebuilds the graph and DME projection from that filesystem,
runs maintenance, and only then finalizes the episode carrier. SRAM clears
after a successful drain.

The older reflection-window state may remain as passive historical data, but
it is not an input, trigger, consolidation path, or metacognition cadence.
Omega reasons once per episode.

---

## Gotchas / doctrine

**Coordinate system** — `(X, Y, Z)`: X = lobe (0–11), Y = subdomain,
Z = domain-specific (knowledge lobe = three-tier `lobe/subdomain/
domain`; otherwise positional). `dewey_map.resolve_path(coord,
domain=...)` is the deterministic coord→path map.

**Current lobe map (verified from `dewey_map.py`, 2026-05-18):**

| X | Directory | Gate | Routing | Purpose |
|---|-----------|------|---------|---------|
| 0 | `000_identity` | IdentityGate | deterministic helper (undecidable → `_unsorted` Y=127) | constitution, personality, emergent_traits, evolution, guardian, **self_inquiry** (0,5), **omega** (0,6 — Omega's open questions/curiosity) |
| 1 | `100_personas` | PersonasGate | deterministic helper (undecidable → `_unsorted` Y=127) | people the system knows. **(1,0)=operator** (the principal persona), **(1,1)=person_stub** (other people; `core.persona_router` allocates a canonical profile root and per-person tree before `memory.persona_ingest` attaches cited facts/relationships). *Canonical persona coord is `[1,1]`.* |
| 2 | `200_knowledge` | KnowledgeGate | deterministic helper + dynamic-Y allocator | ~56 fine-grained Y disciplines; Z = dynamic content cluster via `knowledge_router`. `KNOWLEDGE_DOMAINS` maps domains→(Y,dir). |
| 3 | `300_reasoning` | ReasoningGate | scripted | goals, decisions, constraints, heuristics, alignment (legacy; reasoning largely relocated to situational 5W1H) |
| 4 | `400_situational` | SituationalGate | deterministic helper (undecidable → `_unsorted` Y=127) | 5W1H + outcome + sentiment — SYSTEM self-awareness; every meta choice / auto attempt / global outcome lands here as full event recollection |
| 5 | `500_tools` | ToolsGate | scripted | (5,0)=cognitive, (5,1)=dev. Guardian writes (registration); dispatch reads. *Renamed from 500_skills.* |
| 6 | `600_journal` | JournalGate | scripted | **(6,0)=deepsleep only.** DeepSleep is the SOLE writer (enforced by `test_deepsleep_journal_ownership`). meta/global/omega subdomains were retired. |
| 7 | `700_vision` | VisionGate | scripted | images, face_embeddings, scene_descriptions, visual_patterns |
| 8 | `800_coding` | CodingGate | deterministic helper + dynamic-Y allocator | 50 flat Dewey subdomains (language × concern); `CODING_DOMAINS` + `coding_coord()` |
| 9 | `900_temporal` | TemporalGate | scripted | sessions, events, milestones, history (grounding links). **Slice 4 Model C done:** Omega's reflected read/write space — episodes written post-reflection `(9,1)`, trajectory baseline `(9,2)`. |
| 10 | `1000_workflows` | WorkflowsGate | deterministic helper (undecidable → `_unsorted` Y=127) | code_authoring/debugging/review/testing/refactoring, learn_concept, operator_recorded, tool_chain_template |
| 11 | `1100_dev` | DevGate | scripted | artifacts, progress, context, vision. **Isolated** — dev records never bleed into cognitive recall. |

Persona relationship leaves preserve the exact asserted relation in
`enrichment.entities`. Qualified relations (for example `best_friend`) also
carry their `base_relation` (`friend`) and modifiers (`["best"]`); the generic
base edge for the same subject/object is suppressed at ingest. Forward person
and reverse operator leaves share that relation fingerprint so final MRIL
aggregation can collapse them into one answer-facing fact while still keeping
both canonical navigation directions.

- **Helper-routed lobes (deterministic recall + ingest override):**
  0, 1, 2, 4, 8, 10.
  **`SCRIPTED_LOBES = {3, 5, 6, 7, 9, 11}`** — source-tag dispatch and no
  learned routing step. Adding to either set has propagation rules documented
  in `dewey_map.py`.
- **`ENTITY_BEARING_LOBES = frozenset({1, 4})`** — the canonical set
  for cross-lobe entity linking + supersedes chains + brain-side
  always-allow recall. **Never hardcode `{1, 4}`** — import the
  constant.

**Lane discipline** (`docs/contracts/00_cognitive_doctrine.md` and the
lane-discipline doctrine) — `QuerySpec.allowed_lobes` /
`denied_lobes` enforce that each query's recall stays in one lane.
`auto_loop/recall/run.py` sets default `denied_lobes=[6, 11]` (journal
and dev) unless the memory scope explicitly names those lanes;
deny takes precedence over allow. `via_link` records (entity-graph
edges) bypass `allowed` but still honor `denied`.

**Atomic write contract** — `engine/validate_write.py`: schema
validate against `schema/memory_schema.json` FIRST → path-
traversal blocked (resolved path must be under `MEMORY_ROOT`) → write
to `memory/tmp_writes/<id>.tmp` → fsync → atomic rename → atomic
`coord_index.json` patch under lock. A crash mid-write leaves the
prior corpus intact; failed validation rejects entirely (no partial
state).

**Historical retirement note / path doctrine** — The former `.omega_data/`
state root is not current. Staging = `guardian/state/staging/`; candidacy queues =
`guardian/state/queues/*.jsonl`; telemetry via `guardian.
terminal_feedback.emit_*` → `guardian/logs/`. All paths derive from
`core.paths.OMEGA_ROOT`; no env vars, no relative paths. The memory
schema is `schema/memory_schema.json` and MUST NOT be redefined
elsewhere.

**StrengthIndex is observational** — it informs ranking, never gates
writes. A low-strength memory is still recallable, just ranked lower.
Recall reinforces (reconsolidation); untouched memories decay toward a
floor, never fully forgotten. Since Slice 3, decay tau is per-memory
(`encode_tau`) sampled from the cycle's ambient salience at ingest.

**Extending the routing** — to add a deterministic subdomain:
1. Register `(X, Y) → dirname` in `dewey_map.SUBDOMAIN_MAP`.
2. For identity/persona: add a rule in `subdomain_helpers.py`.
3. For knowledge/coding/workflows: the term-match index is built
   automatically from `SUBDOMAIN_MAP`; no code change needed unless
   you want a custom matcher.
4. Ingest mirrors automatically via `apply_subdomain_override`.

## Observability

All memory operations emit via `guardian.terminal_feedback` (failure-soft, never halts cognition) with matching `guardian.data_logger.emit_event` JSONL durability where noted.

| Operation | Hook | Durable JSONL subsystem |
|-----------|------|------------------------|
| read_memory / write_memory | `emit_data` | `omega.memory.read`, `omega.memory.write` |
| encode_committed_memory | `emit_data` | `omega.memory.encoding` |
| commit_promotion | `emit_data` | `omega.memory.promotion.commit` |
| commit_flagged / commit_quarantine | `emit_data` | `omega.memory.promotion.flagged`, `omega.memory.promotion.quarantine` |
| Promotion pipeline (create_staging, simulate, verify) | `emit_data` | `omega.memory.promotion.*` |
| StrengthIndex mutations | `emit_memory` + `emit_data` | `omega.memory.strength` |
| Lane arbitration & reordering | `emit_data` | `omega.memory.lane_arbitration` |
| Semantic projection and candidate arbitration | `emit_signal` + `emit_data` | `omega.memory.semantic.*` |
| Walker / walker_loader | `emit_data` | `omega.memory.walker` |
| Semantic retina / DME projection | `emit_signal` + `emit_data` | `omega.memory.semantic.*` |
| Lobe gates | `emit_data` | `omega.memory.lobe_gate` |
| Encoding sub-steps (self-vector, contradiction, reflection queue, deepsleep queue) | `emit_data` | `omega.memory.encoding.*` |
| Queues (reflection + deepsleep) | `emit_data` | `omega.memory.queues` |
| Self_vector update | `emit_data` | `omega.memory.self_vector` |
| Dewey_map resolution | `emit_data` | `omega.memory.dewey` |
| Rollback operations | `emit_data` | `omega.memory.rollback` |
| promote_memory / resolve_contradiction | `emit_data` | `omega.memory.promote`, `omega.memory.contradiction` |
| memory_health | `emit_data` | `omega.memory.health` |
| consolidation (si.save) | `emit_data` | `omega.memory.consolidation` |

> Failure-log coverage: `docs/architecture/FAILURE_LOG_COVERAGE.md`

**Contracts** — binding governance lives in `docs/contracts/`:
`00_cognitive_doctrine.md`, `01_schema_contract.md`,
`02_deepsleep_governance.md`, `03_curiosity_development.md`,
`04_scoring_contract.md`, `06_response_system.md`.
(`DOCTRINE_V2.md` is phantom per the FINAL VERDICT cure law — do not
treat as authoritative.)
