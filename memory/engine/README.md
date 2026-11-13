# memory/engine — Dewey route → atomic-write → indexed retrieval

**What it is** — The mechanical substrate under `memory/`: deterministic
grammar routing picks coordinates; the walker reads/scores/ranks records;
`validate_write` atomically persists JSON + coord-index patch. No ML classifier
is used on the hot path.

## Key files / areas

- `validate_write.py` → schema-validate, `final_path_for_meta` (three-tier
  routing: X=2 knowledge uses `knowledge_z_index` sidecar for Z-dirname,
  X=1 persona uses `domain` field as third tier, all other lobes are flat),
  `atomic_write`, `patch_index` / `unpatch_index` (rollback inverse).
- `walker.py` → deterministic retrieval (`_grammar_seed`, `_tokenize`,
  `composite_score`, link expansion). `walker_query()` adapts `QuerySpec`.
  `COORD_INDEX` is a module-level live dict; `load_coord_index()` reloads
  via mtime-checked diff-swap (AUDIT-5). `coord_index_snapshot()` returns a
  lock-safe shallow copy for iteration. Stale index entries self-heal on
  `FileNotFoundError` via `_evict_stale_index_entry`. `composite_score`
  includes a `StrengthIndex` component and a query-independent floor: records
  with zero geometric + link + token + domain relevance score 0 regardless of
  provenance/strength.
- `walker_loader.py` → `WalkerClient` / `load_walker()`. `WalkerClient`
  resolves an explicit coord to neighborhood candidates or falls back to
  grammar seed + lobe fan-out when no coord is supplied.
- `reindex.py` → rebuild `memory/indexes/coord_index.json` from disk via
  `build_index()`; `extract_minimal()` is the canonical single entry-schema
  source shared with `validate_write._patch_index_unsafe`.
- The labeled routing corpus lives in `memory/routing/training_data.py` and is
  consumed by `memory/routing/corpus.py` for deterministic harness tests.

## Entry points / how it's used

- **Write path** (`memory/gates/base.py`): single-lobe gate calls
  `apply_subdomain_override` (`memory/gates/subdomain_override.py`), which
  resolves Y via `subdomain_for` (`auto_loop.mril_tools.subdomain_helpers`);
  undecided texts land in `_unsorted` via `route_undecided_subdomain`
  (`memory/gates/undecided_hold.py`); then `atomic_write`.
- **Read path** (`memory/api.py`): `walker_query` / gate `recall` →
  `walker(..., force_lobe=X)` whole-lobe or pinned subdomain fan-out.

Deterministic disambiguation for coding vs workflows lives in
`memory/routing/code_workflow_disambiguation.py` (used by MRIL router).
