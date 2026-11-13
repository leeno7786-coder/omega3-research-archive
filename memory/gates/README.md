# memory/gates — the 12 per-lobe write/recall gates

**What it is** — One gate class per Dewey lobe. Each gate scopes `ingest` /
`recall` / `recall_recent` to exactly one lobe (its `READABLE_LOBES` /
`WRITABLE_LOBES`), so no gate can touch another lobe's storage. This is the
direct write/recall primitive — lane discipline is enforced in code: each gate
owns one lobe and recall is filtered to permitted lobes. Cognitive systems
normally compose memory operations through [`memory/api.py`](../api.py);
Guardian arbitrates untrusted external ingress, not the mind's own writes.

## Key files / areas

| File | Role |
|------|------|
| [`__init__.py`](./__init__.py) | Re-exports the 12 gate classes under their CURRENT canonical names. No backwards-compat aliases — old `SkillsGate` / `PersonalGate` / `ContextGate` imports must move to `ToolsGate` / `PersonasGate` / `SituationalGate`. |
| [`base.py`](./base.py) | `MemoryGate` base class: `ingest` / `recall` / `recall_recent`, lobe-contract enforcement, Guardian raw-text check, per-lobe `DECOMPOSERS` enrichment, `StrengthIndex` register-on-ingest + touch-on-recall, advisory `resolve_undecidable` hook on `_unsorted` holds, telemetry via `guardian.terminal_feedback` (`emit_guardian_event` / `emit_data`), which mirrors to `guardian.data_logger.emit_event`. |
| [`lobe_gates.py`](./lobe_gates.py) | The 12 concrete gates + the canonical lobe-map docstring. |
| [`subdomain_override.py`](./subdomain_override.py) | `apply_subdomain_override` — Slice 2 of the ingest pipeline; resolves write-Y from the recall grammar (via `auto_loop.mril_tools.subdomain_helpers.subdomain_for`) so write-Y == recall-Y by construction. |
| [`undecided_hold.py`](./undecided_hold.py) | `route_undecided_subdomain` — Phase 5 hold routing; when grammar cannot decide Y on a static lobe, routes the record to `_unsorted` (Y=127) with `provenance.undecided=true`. Dynamic-Y lobes defer to the allocator instead. |

The 12 gates (class → lobe → `SOURCE_TAG`), all single-lobe (`READABLE == WRITABLE == [N]`):

| Gate | Lobe | Tag | Notes |
|------|------|-----|-------|
| `IdentityGate` | 0 identity | `lobe:identity` | constitution, personality, emergent_traits, evolution, guardian, self_inquiry, omega |
| `PersonasGate` | 1 personas | `lobe:personas` | OTHER people; `(1,0)`=operator, `(1,1)`=person_stub, per-person 3rd tier via `core.persona_router` |
| `KnowledgeGate` | 2 knowledge | `lobe:knowledge` | all knowledge subdomains + 3-tier domain routing |
| `ReasoningGate` | 3 reasoning | `lobe:reasoning` | goals, decisions, constraints, heuristics, alignment (no decomposer) |
| `SituationalGate` | 4 situational | `lobe:situational` | 5W1H + outcome + sentiment; system self-awareness (meta/auto/global events land here) |
| `ToolsGate` | 5 tools | `lobe:tools` | tools registry; `(5,0)` cognitive / `(5,1)` dev; `registry_scope` metadata picks Y deterministically |
| `JournalGate` | 6 journal | `lobe:journal` | meta, global_loop, omega, deep_sleep, observations |
| `VisionGate` | 7 vision | `lobe:vision` | images, face embeddings, scene descriptions, visual patterns |
| `CodingGate` | 8 coding | `lobe:coding` | coding knowledge keyed by language (Y) + concern (Z) |
| `TemporalGate` | 9 temporal | `lobe:temporal` | sessions, events, milestones, history (no decomposer) |
| `WorkflowsGate` | 10 workflows | `lobe:workflows` | operator-recorded sequences, detected patterns, tool-chain templates |
| `DevGate` | 11 dev | `lobe:dev` | dev artifacts, progress, context, vision (no decomposer, isolated from brain) |

## Entry points / how it's used

- **Write** — `gate.ingest(text, metadata)`: rejects if `WRITABLE_LOBES`
  empty → Guardian raw-text check → deterministic coord seed
  (`[lobe,0,0]`) → `apply_subdomain_override` (grammar Y) →
  `route_undecided_subdomain` (static lobe, no grammar → Y=127
  `_unsorted` + `provenance.undecided`; advisory `resolve_undecidable`
  no-op) → dynamic-Y allocator when
  flagged (lobes 2/8) → enrich → atomic write → `StrengthIndex.register`
  → entity-link if lobe in `ENTITY_BEARING_LOBES`. Returns the written
  path or `None` on rejection.
- **Recall** — `gate.recall(query, limit, domain)`: single-lobe gates force
  the walker to `READABLE_LOBES[0]` (deterministic whole-lobe fan-out,
  including `_unsorted` at Y=127). Results are filtered to permitted lobes;
  hits get `StrengthIndex.touch`. `gate.recall_recent(limit, lobe)` returns
  ingest-time-ordered records from a coord-index snapshot.
- **Lane discipline** — `READABLE_LOBES` / `WRITABLE_LOBES` are the lane.
  Every system-initiated response type has ONE home lobe and is read/written
  only through that lobe's gate. Cross-lobe reach exists only via entity
  links, not via a gate widening its lobe set.
- **Relation to `memory.api` + guardian** — Cognitive systems call
  [`memory/api.py`](../api.py), not gates. `_guardian_check` calls
  `memory.write_guard.guard_raw_text` (fail-open if Guardian absent).
  `ToolsGate` registrations are source-tag dispatched: guardian writes,
  voice_box / dev pipeline read.

## Depends on / depended-on-by

- **Depends on** — `memory.core.build_memory_dict`,
  `memory.engine.validate_write.atomic_write`, `memory.engine.walker`,
  `memory.routing.resolve_undecidable`, `memory.strength_index`,
  `memory.enrichment.*` (decomposers),
  `memory.dewey_map` (`ENTITY_BEARING_LOBES`, `SUBDOMAIN_MAP`),
  `memory.write_guard`, `guardian.terminal_feedback`, `guardian.data_logger`,
  `memory.gates.subdomain_override`, `memory.gates.undecided_hold`.
- **Depended-on-by** — [`memory/api.py`](../api.py) (the composition layer
  cognitive systems and Guardian use), `memory.enrichment.entity_linker`
  (post-write hook).

## Gotchas / doctrine

- **Lane discipline is structural, not advisory** — per the
  `project_lane_discipline_doctrine` / `project_guardian_sole_writer_doctrine`
  governance doctrines. A write whose
  proposed X != the gate's contracted lobe has its X overridden to the
  contract lobe (Y/Z kept), emitting `ingest_lobe_enforced`. This closed a
  confirmed pollution bug (journal/temporal records leaking into lobe 0).
- **Current lobe map (from `lobe_gates.py`, post 2026-05-06 restructure;
  authoritative — older READMEs were stale):** 0 identity · 1 personas
  (was `100_personal`) · 2 knowledge · 3 reasoning · 4 situational ·
  5 tools (was `500_skills`; `ToolsGate`, was `SkillsGate`; `(5,0)`
  cognitive + `(5,1)` dev) · 6 journal · 7 vision · 8 coding · 9 temporal ·
  10 workflows · 11 dev.
- **`JournalGate` (lobe 6) — DeepSleep is the sole writer.** No other layer
  routes here; meta/auto/global self-awareness records go to
  `SituationalGate` (lobe 4), durable history to `TemporalGate` (lobe 9).
- **No backwards-compat aliases** — `__init__.py` exports only canonical
  names. A system booted under a stale gate name is a lie; fix the import.
- **`ENTITY_BEARING_LOBES = {1, 4}`** (`memory.dewey_map`, canonical set).
  Only personas (1, "Bob is my dad") and situational (4, "Bob likes
  chicken") trigger the post-write entity linker; cross-lobe relationship
  chains depend on both arms.
- **Single-lobe is enforced.** `MemoryGate.__init_subclass__` rejects
  `lobe_gates.py` subclasses with more than one readable/writable lobe.
  Historical multi-lobe classifier routing is not part of the live ingest/recall path.
  Cross-lobe writes use explicit coords via `memory.api.write_memory`.
- **`ToolsGate.ingest` is overridden** — when `metadata['registry_scope']`
  (or `constraints.registry_scope`) is `cognitive`/`dev` it writes `[5,0]` or
  `[5,1]` deterministically; the manifest scope is authoritative.
- **Undecided hold (Phase 5)** — static lobes with no `subdomain_for`
  signal land at `(lobe, 127, Z)` `_unsorted` with `provenance.undecided=true`
  and confidence capped at 0.3 — never polluting real Y=0 categories.
  Lobes 2/8 (when dynamic-Y flagged) skip hold and use the allocator.
  Lobe 6 journal always grammar-decides to Y=0, so `_unsorted` is
  unreachable there. Tier-3 holds append to
  `guardian/state/routing_edge_catalog.jsonl` (Phase 7). DeepSleep
  re-resolution and LFM finetune (Phase 8) are data-driven on catalog size.
- **Entity links are deterministic (Phase 5)** — `entity_linker` keys on
  `linked_persons`, persona `domain`, and structured `entities` — not
  capitalized-word regex from raw text.
- **Guardian boundary** — external ingress uses Guardian policy; cognitive
  self-writes use the direct memory path. Routing and recall remain deterministic.
- **MRIL recall router** (`auto_loop/mril_tools/router.py`) — regex intents
  first; `how to` / `how do we do` → lobe **5** `remember_procedure` (tool
  skills); code/workflow marker disambiguation without that prefix → lobe **8**
  or **10** `remember_workflow`. Gate ingest symmetry is independent of MRIL
  tool lobe pick (Phase 7 harness `recall_lobe_accuracy` is informational).
- **Persona direct-write fallback** — when `write_memory` fails, `persona_router`
  writes `[1, 1, Z]` JSON directly; it is a failure-soft recovery path only.
