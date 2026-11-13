# memory/indexes — runtime lookup tables for the Dewey memory substrate

**What it is** — The home of the runtime coordinate index:
`coord_index.json` (id → on-disk record locator, rebuilt/patched from the
corpus). The whole directory is gitignored — its contents are generated
runtime/build artifacts, never source-of-truth in the repo. This README
documents what the directory is for and how the index is built; the files
themselves are transient.

**Key files / areas**

- `memory/indexes/coord_index.json` → `{ memory_id: {id, coord, path, title,
  type, domain, subtopic, canonical, summary, tags, status, superseded_by,
  private, quarantined, text_prefix, recall_proj} }`. The id→record locator
  plus a recall projection so taxonomy recall can filter/score without
  opening every canonical JSON. Empty `{}` at zero-day; grows as records
  land. Atomic-written (tmp + `fsync` + `replace`) by every writer.
- `memory/indexes/label_map.json` → a historical/offline routing artifact when
  present. It is not read by the live deterministic recall path.
- `memory/indexes/coord_index.tmp` → transient atomic-write staging file
  (created and renamed away by `reindex.build_index`); not normally present.

**Entry points / how it's used**

- **coord_index — full rebuild**: [`memory/engine/reindex.py`](../engine/reindex.py)
  `build_index()` walks `memory/**/*.json`, schema-validates each against
  [`schema/memory_schema.json`](../../schema/memory_schema.json)
  (Draft7), skips `indexes/`+`schema/` dirs and id/coord-less files, and
  atomic-writes the whole index. CLI/repair entrypoint (`.\.venv-brain\Scripts\python.exe -m memory.engine.reindex`) and the rebuild step in the reset script
  ([`scripts/day_zero_reset.py`](../../scripts/day_zero_reset.py)) — not
  on the hot path.
- **coord_index — incremental patch**: [`memory/engine/validate_write.py`](../engine/validate_write.py)
  `atomic_write` → `_patch_index_unsafe` adds one entry per record write;
  `unpatch_index` removes one on rollback. Both write the index atomically
  under `_INDEX_LOCK`. This is the hot path; `reindex` only rebuilds on drift.
- **coord_index — read side**: [`memory/engine/walker.py`](../engine/walker.py)
  holds the process-shared `COORD_INDEX` dict. `load_coord_index()`
  mtime-checks `coord_index.json` and does a diff-based in-place reload (never
  a rebind — ~25 callers hold the reference). `boot.py` calls
  `load_coord_index()` then `ensure_strength_index()` during startup so the
  StrengthIndex singleton is warmed against the loaded corpus.
- **Historical label-map tooling:** older offline routing/training tools may
  read `label_map.json`; that artifact is not a runtime memory authority.

**Depends on / depended-on-by**

- **Built from**: the `memory/**/*.json` corpus +
  [`schema/memory_schema.json`](../../schema/memory_schema.json) →
  `coord_index.json`.
- **Built by**: `memory/engine/reindex.py`, `memory/engine/validate_write.py`,
  `scripts/day_zero_reset.py` (coord_index wipe/rebuild).
- **Read by**: `memory/engine/walker.py` and its callers, `memory/api.py`
  (`memory_health()` reports `coord_index_path`/`coord_index_exists`), and
  boot/reindex tooling.

**Gotchas / doctrine**

- **Generated, not committed.** The directory is gitignored. Do not hand-edit
  these files or treat their on-disk contents as authoritative; the canonical
  sources are the corpus and schema. A fresh clone has no runtime index until
  boot or reindex tooling runs.
- **coord_index drift is loud, not silent.** A `_patch_index_unsafe` failure
  inside `atomic_write` raises so the caller (`memory.rollback`) unlinks the
  orphan record; the index never silently desyncs from the corpus. `reindex`
  is the wholesale repair when drift is detected; a corrupted index file is
  logged and rebuilt rather than silently wiped.
- **Reload is in-place + mtime-gated.** `COORD_INDEX` is one object for the
  process lifetime; reload applies removals-then-adds under a lock. Iterate via
  `walker.coord_index_snapshot()`, never raw `COORD_INDEX.items()`. On
  mtime-coarse filesystems same-tick double writes may be missed; reload
  converges on the next call (see `memory/engine/walker.py` AUDIT-5 notes and
  `project_omega_mantra_deterministic_home` in MEMORY).
- See [`memory/README.md`](../README.md) for the deterministic route → write →
  retrieve machinery that produces and consumes the coordinate index.
