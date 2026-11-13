# Failure-Log MAP-MISS Sweep — Durable-Write + Continuity Silent Failures

**Work order:** `99c23bf5` (claude → devin) · **Predecessor:** 60-row `FAILURE_LOG_COVERAGE_MAP.md` (batches 1-3, gate 857)

## Coverage oracle applied

A path is **already covered** (excluded) if its failure branch fires any of:
1. `emit_data` / `emit_phase` / `emit_inference` / `emit_memory` with `status` in {`fail`,`error`,`degraded`,`crash`,`timeout`,`declined`} or `ok=False`
2. `emit_signal(..., status="degraded"|"fail"|...)`
3. Any wrapper calling `data_logger.emit_event(..., ok=False)` or `emit_data(..., ok=False)`
4. The failing call is **inside** a `phase_span(...)` and **raises** (phase_span emits `status="fail"` then re-raises)
5. `memory/encoding.py::_fail(...)` — every `_fail()` call emits `status="fail"`

A path is **uncovered** (listed) only if its failure branch hits **none** of the above.

---

## memory — promotion, journal, rollback, supersedes, entity_linker

| file:line | failure path (what durable/continuity op fails) | current behavior | WHY not already covered (cite the absent emit) | recommended emit (helper + status + reason + key fields) | priority |
|---|---|---|---|---|---|
| `memory/promotion.py:579-589` | `commit_promotion` `atomic_write` fails (schema/IO/traversal) | `log.error` + `journal_abort` + `return (False, None, None)` | `journal_abort` writes to the encoding journal, NOT to `failures.jsonl`; no `emit_data`/`emit_event`/`phase_span` fires on this branch | `emit_data("memory", "commit_promotion", status="fail", ok=False, reason="atomic_write_failed", staging_ref=..., error=str(exc))` | **P0** |
| `memory/promotion.py:753-760` | `commit_flagged` `atomic_write` fails | `log.error` + `journal_abort` + `return (False, None, None)` | Same as above — journal_abort is not the unified sink; no emit reaches `failures.jsonl` | `emit_data("memory", "commit_flagged", status="fail", ok=False, reason="atomic_write_failed", staging_ref=..., error=str(exc))` | **P0** |
| `memory/promotion.py:896-898` | `commit_quarantine` file write (`json.dump` + `os.replace`) fails | `log.error` + `return (False, None, None)` | Only `log.error`; no `emit_data` / `emit_event` / `phase_span` / `_fail()` | `emit_data("memory", "commit_quarantine", status="fail", ok=False, reason="quarantine_write_failed", staging_ref=..., error=str(exc))` | P1 |
| `memory/promotion.py:925-926` | `commit_quarantine` quarantine index update fails | `log.warning` only; `index_updated` stays `False` | Only `log.warning`; no emit of any kind | `emit_data("memory", "commit_quarantine", status="degraded", ok=False, reason="quarantine_index_update_failed", staging_ref=..., error=str(exc))` | P1 |
| `memory/encoding_journal.py:98-100` | `_append` OSError (journal open/step/close/abort) | `log.error` + `return False` | Caller (`journal_open`, `journal_step`, `journal_close`, `journal_abort`) ignores return value; no emit on journal-append loss | `emit_data("memory", "encoding_journal_append", status="fail", ok=False, reason="journal_append_failed", error=str(exc))` | P1 |
| `memory/encoding_journal.py:288-299` | `recover_unclosed` `rollback_fn` raises during boot recovery | `log.error` only; error appended to result list | Boot recovery is NOT inside a `phase_span`; `log.error` does not reach the unified sink | `emit_data("memory", "encoding_journal_recovery", status="fail", ok=False, reason="rollback_failed", memory_id=..., error=str(exc))` | P1 |
| `memory/supersedes.py:265-285` | `link_supersedes` new_meta `write_text` fails | `log.error` + in-memory revert + `return {"ok": False, ...}` | Caller (`memory/api.py:1021-1029`) logs error but does NOT emit; no helper emit fires on this branch | `emit_data("memory", "supersedes_link", status="fail", ok=False, reason="write_new_failed", new_id=..., error=str(exc))` | P1 |
| `memory/supersedes.py:293-339` | `link_supersedes` prior back-link `write_text` fails (after new_meta already written) | `log.error` + in-memory revert + disk revert attempt + `return {"ok": False, ...}` | Same caller gap; no emit reaches sink; half-formed chain is silent | `emit_data("memory", "supersedes_link", status="fail", ok=False, reason="write_prior_failed", prior_id=..., error=str(exc))` | P1 |

## memory / runtime — lesser durability degrade

| file:line | failure path | current behavior | WHY not already covered | recommended emit | priority |
|---|---|---|---|---|---|
| `memory/enrichment/entity_linker.py:195-208` | Back-link `write_text` on peer memory fails | `log.error` + in-memory revert + `back_link_persisted = False` | Caller (`memory/api.py:1041-1062`) catches linker exceptions but does NOT emit; no helper emit fires | `emit_data("memory", "entity_linker", status="degraded", ok=False, reason="back_link_write_failed", target_path=..., error=str(e))` | P2 |
| `memory/enrichment/entity_linker.py:224-238` | Forward-link `write_text` on new memory fails (after back-links succeeded) | `log.error` only; asymmetric edges on disk | Same caller gap; no emit | `emit_data("memory", "entity_linker", status="degraded", ok=False, reason="forward_link_write_failed", new_path=..., error=str(e))` | P2 |
| `memory/strength_index.py:562-565` | `_flush_singleton` `save(force=True)` fails at shutdown | `log.error` only | Shutdown hook is outside `phase_span`; no emit infrastructure guarantee at exit; but dirty records are lost | `emit_data("memory", "strength_index_flush", status="degraded", ok=False, reason="shutdown_flush_failed", error=str(exc))` | P2 |
| `runtime/face/recognition.py:176-184` | `persist_brain_anchor` raises (anchor_store `_persist` OSError) | Caught in `recognition.enroll`; `log.warning` + `return {"ok": False, ...}` | No emit on enrollment failure; `_persist` itself has no try/except + emit | `emit_data("runtime", "anchor_enroll", status="fail", ok=False, reason="anchor_store_persist_failed", name=..., error=str(exc))` | P2 |

---

## Excluded paths (oracle-confirmed coverage or out-of-scope)

| file:line | why excluded |
|---|---|
| `memory/promotion.py:181-184` | Already in 60-row map; now covered by batch implementation. |
| `memory/promotion.py:602-617` | Encoding failure → `rollback_canonical_write` or `restore_canonical_write`, both of which call `emit_data("memory", "rollback", ...)` at end. Covered. |
| `memory/promotion.py:620-624, 786-789` | Staging cleanup failures post-success. Pre-canonical; benign-empty. |
| `memory/promotion.py:987-988` | `rollback_promotion` OSError on staging removal. Pre-canonical; P2-level, excluded to keep sweep narrow. |
| `memory/encoding.py:193-195, 226-228` | Both hit `_fail()` which now emits `status="fail"`. Covered by oracle #5. |
| `memory/encoding.py:496-512` | `_identity_contradiction_precheck` import/read failures. Read-side guards; no write. |
| `memory/strength_index.py:172-173` | Listed in 60-row map; `save()` now emits `emit_data(..., ok=False)` on OSError. Covered. |
| `memory/consolidation.py:95-97` | Listed in 60-row map; `run_consolidation_once()` now emits on exception. Covered. |
| `memory/knowledge_router.py:100+` | Listed in 60-row map; `_load_index()` now emits on parse failure. Covered. |
| `memory/knowledge_router.py:121-127` | `_save_index` has no try/except; raises on failure. Loud, not silent. |
| `memory/engine/validate_write.py:222-254, 290-296` | `patch_index` / `unpatch_index` file writes have no try/except; raise on failure. Loud. |
| `memory/gates/base.py:310, 338-340` | `atomic_write` failure caught in `_write`; returns `None`. Caller `_ingest_impl` checks `if result` and emits `emit_data("memory", "ingest_error", ok=False, ...)` on else. Covered. |
| `memory/api.py:911-919, 1120-1129` | `write_memory` staging/direct-commit failures call `_emit_write_memory_data(...)`. Covered. |
| `pressure_loop/episode.py:385-402` | All three paths (accumulator reset, archive, reflection_window append) now have `emit_data` in their except blocks. Covered by batch 1-3. |
| `pressure_loop/reflection_window.py:169-212, 271-318, 364-416` | `save_current_trajectory`, `save_reflection_window`, `archive_reflection_window` all emit on success and failure. Covered. |
| `pressure_loop/open_questions.py:243-251` | `record_open_question` `_append` failure now emits explicitly. Covered. |
| `pressure_loop/temporal_store.py:31-70, 179-197` | Both call `write_memory` which emits via `_emit_write_memory_data`. Covered. |
| `runtime/face/anchor_store.py:69-71` | Listed in 60-row map; now emits on load failure. Covered. |
| `memory/encoding_journal.py:371-382` | `compact_journal` rewrite failure. Maintenance path; excluded for narrow scope. |
| `memory/queues.py:240-248` | `drain_jsonl` tail rewrite failure. Maintenance path; excluded for narrow scope. |

---

## Summary

**Genuine map-misses found: 11 rows** (down from a first-pass of ~20 after applying the oracle rigorously).

- **P0: 2** — `memory/promotion.py` `commit_promotion` and `commit_flagged` `atomic_write` failures. These are the exact seed example from the work order: a canonical memory durable write is lost and the only trace is a `log.error` line that never reaches `failures.jsonl`.
- **P1: 6** — `commit_quarantine` file + index writes, `encoding_journal._append` loss, boot-recovery rollback failure, and two `supersedes` chain-link writes. All are continuity-structure writes that silently degrade or lose state.
- **P2: 3** — `entity_linker` asymmetric-edge writes, `strength_index` shutdown flush, and `anchor_store` enrollment persist. Lesser durability degrade.

**False-positive check:** The original map's double-emit pattern (already-covered helpers) does **not** appear in any row above. Every listed path was verified to have **zero** of: `emit_data`/`emit_event`/`phase_span`-raise/`emit_signal`/`_fail()` on its failure branch. The oracle was applied caller-up as well — e.g. `atomic_write` failures inside `memory.gates.base._write` were excluded because `_ingest_impl` emits on the `None` return; `rollback_canonical_write` failures were excluded because the function itself emits at its tail.

**Next lane:** Cursor implements from this map; Claude verifies independently.