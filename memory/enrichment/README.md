# memory/enrichment — per-lobe decomposers that structure an envelope before write

**What it is** — The decomposer layer. Before a gate writes a memory, it runs
the lobe's `enrich(text, **kwargs)` function, which pattern-extracts structured
metadata (entities, facets, coords, kinds) and attaches it as the record's
`enrichment` block. Every decomposer is deterministic and pattern-based — no LLM.
Each returns `None` on no signal (absence of structure is data, not an error).
This package also exposes the post-write entity linker that stitches
cross-lobe edges.

**Key files / areas**

- [`__init__.py`](__init__.py) → declares `ENRICHMENT_MAP` (lobe id → enrich fn) and re-exports each `enrich_*` alias. The single source of the lobe↔enricher binding.
- [`identity.py`](identity.py) → lobe 0. Regex-detects self-referential statements about Omega (identity / purpose / creator). Returns `decomposer="identity"`, `matches`, `speaker`.
- [`personas.py`](personas.py) → lobe 1. Extracts names, relationships, preferences, location, age, occupation, email into an `entities` list. Also exports `personal_signal_strength(text)->float`. Note: the returned dict's internal `decomposer`/`kind` is `"personal"` (label predates the personas rename).
- [`knowledge.py`](knowledge.py) → lobe 2 (per `ENRICHMENT_MAP`). 5W1H-style WHO/WHEN/WHERE entity extraction. (The module docstring says "lobe 5" — stale; the live route is lobe 2.)
- [`situational.py`](situational.py) → lobe 4. Full event-recollection schema: picks one of 8 facets (5W1H + outcome + sentiment), trinary success, sentiment, failure reason, intensity, `linked_persons`/`linked_coding`. Accepts caller `**kwargs` hints. Depends on `memory.dewey_map.SITUATIONAL_FACETS`.
- [`journal.py`](journal.py) → lobe 6 (per `ENRICHMENT_MAP`). Stamps `decomposer="journal"`, `kind="inference_log"`, `logged_at` (UTC ISO). Always returns a dict. (Its docstring says "lobe 10" — stale; the live route is lobe 6.)
- [`vision_enrichment.py`](vision_enrichment.py) → lobe 7. Wraps image-description text (returns `None` if `len < 10`, truncates to 500 chars).
- [`coding.py`](coding.py) → lobe 8. Detects language + concern from cue regexes, resolves `domain_key` through `memory.dewey_map.coding_coord`, returns `coord`, `domain`, `known`.
- [`workflow.py`](workflow.py) → lobe 10. Detects workflow `kind` (operator_recorded / tool_chain_template / code_* / learn_concept), extracts numbered/bulleted steps. Depends on `memory.dewey_map.WORKFLOW_KINDS`.
- [`entity_linker.py`](entity_linker.py) → post-write hook `link_personal_memories(path, COORD_INDEX)`. Scans entity-bearing lobes, finds shared proper nouns, writes symmetric bidirectional `links`. Linkable lobes come from `memory.dewey_map.ENTITY_BEARING_LOBES`.

**Entry points / how it's used**

- Each gate in [`../gates/lobe_gates.py`](../gates/lobe_gates.py) sets a `DECOMPOSERS = {lobe: enrich_fn}` class attribute (imported from this package's `enrich_*` aliases). Only lobes 0, 1, 2, 4, 6, 7, 8, 10 carry a decomposer; others leave `DECOMPOSERS = {}`.
- `MemoryGate._enrich(text, lobe)` in [`../gates/base.py`](../gates/base.py) looks up `DECOMPOSERS.get(lobe)` and calls it; a decomposer failure is surfaced rather than committed as a successful enriched write.
- `MemoryGate.ingest()` calls `_enrich`, then `_write`, then runs `link_personal_memories` on entity-bearing writes. `memory/api.py` also invokes `link_personal_memories` on the persistence path.

**Depends on / depended-on-by**

- Depends on: `memory.dewey_map` (`SITUATIONAL_FACETS`, `coding_coord`, `CODING_DOMAINS`, `WORKFLOW_KINDS`, `ENTITY_BEARING_LOBES`); the standard library (`re`, `json`, `datetime`).
- Depended-on-by: `memory/gates/base.py` + `memory/gates/lobe_gates.py` (decomposer dispatch + override), `memory/api.py` (entity linking on write), and the test suites under `tests/memory/` and `tests/integration/`.
- For lobe 1, the gate (not this package) routes a person into a per-persona Y namespace via `core.persona_router`; the enricher only extracts the entities the router and linker consume.

**Gotchas / doctrine**

- `ENRICHMENT_MAP` is authoritative for which lobe an enricher serves. Two module docstrings are stale and disagree with it: `knowledge.py` says "lobe 5" (real: 2) and `journal.py` says "lobe 10" (real: 6). Trust the map, not the prose.
- `personas.py` is named for its canonical lobe-1 home per `project_guardian_sole_writer_doctrine` (2026-05-08, see [`__init__.py`](__init__.py)), but its emitted `decomposer`/`kind` value is still the legacy `"personal"` — that string is an internal record field, not a module name.
- A decomposer returning `None` is normal and means "no structured signal" — never treat it as failure; the write still proceeds without an `enrichment` block.
- `situational.py`, `coding.py`, and `workflow.py` accept caller `**kwargs` hints (e.g. `facet`, `success`, `language`, `kind`, `steps`); operator-driven writes can pre-pick a facet/kind rather than relying on cue detection.
- `entity_linker` deliberately skips superseded records and writes the back-edge first, only counting a link if the symmetric back-write persisted — asymmetric edges produce one-way recall holes (dad→Bob works, Bob→dad doesn't). Add lobes via `ENTITY_BEARING_LOBES` in `memory.dewey_map`, never inline here.
