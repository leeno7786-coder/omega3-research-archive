# Failure-Log Coverage — Authoritative Doc

**Status:** COMPLETE (B1 `3204eaaf` / B2 `b4956928` / B3 `5da7739a` / B4 `e23bbe46`; sink `c9fa47ba`; gate 869, 71 paths).  
**Scope:** Every silent-failure path across all subsystems now emits a failure-status record that reaches `guardian/logs/failures.jsonl`.  
**Source maps:** `FAILURE_LOG_COVERAGE_MAP.md` (60 rows) + `FAILURE_LOG_MAPMISS_SWEEP.md` (11 rows).

---

## 1. MECHANISM

### 1.1 Sink — `guardian/telemetry/writer.py::write_telemetry`

`write_telemetry` is the single fork point. Every structured telemetry record is written to the firehose (`guardian/telemetry/firehose.jsonl`) and to a per-system file (`guardian/telemetry/{system}.jsonl`). If the record carries a failure status or `ok=False`, it is **also** appended to the bounded failure log:

```python
_FAILURE_STATUSES = {"fail", "error", "degraded", "crash", "timeout", "declined"}

def _is_failure(record: dict[str, Any]) -> bool:
    if str(record.get("status") or "").strip().lower() in _FAILURE_STATUSES:
        return True
    return record.get("ok") is False
```

- **Only failures write** — a healthy system leaves `failures.jsonl` empty (or near-empty).
- **Thread-safe** — per-file `threading.Lock`, lazy-created per path.
- **Failure-soft** — `write_telemetry` catches its own exceptions and logs a warning; a failed telemetry write never halts cognition (`writer.py:168`).
- **Path:** `guardian/logs/failures.jsonl` (relative to `OMEGA_ROOT`).

### 1.2 Status-aware `data_logger` mirror

`guardian/data_logger.py::emit_event` writes subsystem events to `guardian/logs/{subsystem}.jsonl` and **also** mirrors to `write_telemetry` (`data_logger.py:100-115`). The mirror is now status-aware:

```python
write_telemetry(
    system=system,
    phase=event_name,
    event="data",
    status=("fail" if data.get("ok") is False else str(data.get("status") or "ok")),
    ok=data.get("ok"),
    ...
)
```

Any subsystem event passing `ok=False` or a failure `status` automatically reaches `failures.jsonl` through this mirror. This covers pre-existing helpers such as `memory/gates/base.py::_emit(..., ok=False)`, `memory/promotion.py::_emit_promotion(..., ok=False)`, and `runtime/model_router.py` audit signals.

### 1.3 `phase_span` auto-fail-on-raise

`guardian/terminal_feedback.py::phase_span` is a context manager that emits `status="enter"` on entry and `status="ok"`/`"skip"` on normal exit. If the wrapped block **raises any exception**, it emits `status="fail"` with `error_type`, `summary=str(exc)`, and `latency_ms`, then re-raises (`terminal_feedback.py:341-352`).

This means any crash-causing call site already inside a `phase_span` automatically reaches the failure log **before** the process dies. Crash-cause rows in the map (e.g. `global_loop/ignition/run.py`, `pressure_loop/temporal_store.py`) were wrapped at their call sites rather than at the raise point.

### 1.4 Test isolation — `tests/conftest.py`

`tests/conftest.py` provides a **session-scoped** fixture `_isolate_telemetry_paths` that reassigns the module globals `writer.TELEMETRY_DIR`, `writer.FAILURE_LOG`, and `data_logger._LOG_DIR` to a temporary directory (`conftest.py:92-101`).

- Tests exercise failure paths constantly; without isolation they would fill the real `failures.jsonl` with synthetic failures.
- Every test asserting on `writer.FAILURE_LOG` reads from the temp file, guaranteeing a clean signal.
- The temp tree is `shutil.rmtree`-cleaned on session end.

---

## 2. COVERAGE — 71 covered paths

Organized by subsystem. Each row cites `file:line`, the batch that wired it, and the emit helper used.

### 2.1 pressure_loop (8 paths)

| # | file:line | Batch | What malfunctions | Emit helper | Status |
|---|---|---|---|---|---|
| 1 | `pressure_loop/episode.py:385-389` | B1 | `reset_pressure_accumulator()` swallowed exception | `emit_data` | degraded |
| 2 | `pressure_loop/episode.py:393-398` | B1 | `archive_episode()` swallowed exception | `emit_data` | degraded |
| 3 | `pressure_loop/episode.py:400-411` | B1 | `_append_episode_to_reflection_window()` swallowed exception | `emit_data` | degraded |
| 4 | `pressure_loop/pressure.py:403-408` | B1 | `LAST_INGRESS_PRESSURE` non-dict for operator message | `emit_data` | degraded |
| 5 | `pressure_loop/temporal_store.py:129-145` | B1 | `read_trajectory()` raises on corrupt JSON (crash cause) | `phase_span` at call site | fail |
| 6 | `pressure_loop/operator_pressure.py:252-297` | B2 | `read_operator_directive()` malformed timestamp | `emit_data` | degraded |
| 7 | `pressure_loop/open_questions.py:243-251` | B2 | `_append()` fails in `record_open_question()` | `emit_data` | fail |
| 8 | `pressure_loop/pressure.py:146-163` | B2 | `evaluate()` neuromod/spike extraction fails | `emit_data` | degraded |

### 2.2 global_loop / meta_loop / cognition_chain (4 paths)

| # | file:line | Batch | What malfunctions | Emit helper | Status |
|---|---|---|---|---|---|
| 9 | `global_loop/ignition/run.py:10-15` | B1 | Validation raises `ValueError` on missing ignition fields | `phase_span` at call site | fail |
| 10 | `global_loop/assemble/run.py:32-37` | B2 | `meta_b_input_from_episode()` fails | `emit_data` | degraded |
| 11 | `global_loop/extract/run.py:13-18` | B2 | Policy import/call fails | `emit_data` | degraded |
| 12 | `meta_loop/consistency/run.py:93-150` | B2 | Critical consistency mismatch detected | `emit_data` | fail |

### 2.3 auto_loop / execute / tools (12 paths)

| # | file:line | Batch | What malfunctions | Emit helper | Status |
|---|---|---|---|---|---|
| 13 | `auto_loop/execute/run.py:480-483` | B1 | Tool `invoke()` returns `ok=False` but marked success | `emit_data` | fail |
| 14 | `auto_loop/execute/run.py:507-511` | B1 | Tool `invoke()` raises exception | `emit_data` | fail |
| 15 | `auto_loop/execute/run.py:311-313` | B1 | `run_forced_memory_tools()` raises during MRIL recall | `emit_data` | fail |
| 16 | `auto_loop/execute/run.py:392-402` | B1 | `run_forced_memory_tools()` raises during hop-2 recall | `emit_data` | fail |
| 17 | `auto_loop/execute/run.py:588-595` | B2 | `persist_generated_image()` fails | `emit_data` | degraded |
| 18 | `auto_loop/execute/run.py:160-172` | B2 | `knowledge_extractor.extract()` fails | `emit_data` | degraded |
| 19 | `auto_loop/execute/run.py:505-506` | B2 | `_generate()` for tool-result synthesis returns falsy | `emit_data` | degraded |
| 20 | `schema/tools/web_fetch.py:381-382` | B1 | Direct URL fetch (`requests.get`) fails | `emit_data` | fail |
| 21 | `schema/tools/web_fetch.py:213-214` | B2 | `category_for_domain()` import/call fails | `emit_data` | degraded |
| 22 | `schema/tools/web_fetch.py:141-142` | B2 | `load_web_sources_config()` fails | `emit_data` | degraded |

### 2.4 omega_loop / deepsleep_loop (1 path)

| # | file:line | Batch | What malfunctions | Emit helper | Status |
|---|---|---|---|---|---|
| 23 | `deepsleep_loop/consolidate/run.py:60-61` | B2 | Knowledge artifact consumption fails | `emit_data` | degraded |

### 2.5 memory / runtime (28 paths — 17 original map + 11 map-miss)

#### Original map rows (17)

| # | file:line | Batch | What malfunctions | Emit helper | Status |
|---|---|---|---|---|---|
| 24 | `memory/gates/base.py:151-152,164-165` | B2 | Signal detectors (`is_knowledge_signal`, `is_self_referential`) raise | `emit_data` | fail |
| 25 | `memory/gates/base.py:194` | B2 | `_enrich()` decomposer fails | `emit_data` | fail |
| 26 | `memory/gates/base.py:231` | B2 | `_attach_embedding()` embed fails | `emit_data` | fail |
| 27 | `memory/gates/base.py:293` | B2 | `StrengthIndex.register()` fails | `emit_data` | fail |
| 28 | `memory/gates/base.py:470-471` | B2 | `_ingest_impl()` intermediate failures (SI, enrichment) | `emit_data` | fail |
| 29 | `memory/gates/base.py:521-523` | B2 | `recall()` walker error | `emit_data` | fail |
| 30 | `memory/gates/base.py:568-569` | B2 | `recall_recent()` index load error | `emit_data` | fail |
| 31 | `memory/api.py:283-285` | B2 | `read_memory()` recall plan compilation fails | `emit_data` | fail |
| 32 | `memory/api.py:583-621` | B2 | `read_memory()` lane arbitration exception | `emit_data` | degraded |
| 33 | `memory/api.py:692-695` | B2 | `read_memory()` walker arbitration shim fails | `emit_data` | degraded |
| 34 | `memory/strength_index.py:172-173` | B2 | `save()` OSError on write | `emit_data` | fail |
| 35 | `memory/consolidation.py:95-97` | B3 | `run_consolidation_once()` StrengthIndex flush fails | `emit_data` | degraded |
| 36 | `memory/knowledge_router.py:100+` | B3 | `_load_index()` JSON decode / OSError | `emit_data` | fail |
| 37 | `memory/api.py:403-409` | B3 | `read_memory()` history attach per-candidate fails | `emit_data` | fail |
| 38 | `memory/api.py:427-439` | B3 | `read_memory()` history attach import/outer failure | `emit_data` | fail |
| 39 | `memory/promotion.py:181-184` | B3 | `_emit_audit()` audit write fails | `emit_data` | fail |
| 40 | `runtime/face/anchor_store.py:69-71` | B3 | `_ensure_loaded()` file read failure | `emit_data` | fail |
| 41 | `runtime/model_router.py:172-184` | B3 | `_emit_audit()` audit write fails | `emit_data` | fail |

#### Map-miss sweep rows (11) — Batch 4

| # | file:line | Batch | What malfunctions | Emit helper | Status |
|---|---|---|---|---|---|
| 42 | `memory/promotion.py:579-589` | B4 | `commit_promotion` `atomic_write` fails | `emit_data` | fail |
| 43 | `memory/promotion.py:753-760` | B4 | `commit_flagged` `atomic_write` fails | `emit_data` | fail |
| 44 | `memory/promotion.py:896-898` | B4 | `commit_quarantine` file write fails | `emit_data` | fail |
| 45 | `memory/promotion.py:925-926` | B4 | `commit_quarantine` quarantine index update fails | `emit_data` | degraded |
| 46 | `memory/encoding_journal.py:98-100` | B4 | `_append` OSError (journal open/step/close/abort) | `emit_data` | fail |
| 47 | `memory/encoding_journal.py:288-299` | B4 | `recover_unclosed` `rollback_fn` raises during boot recovery | `emit_data` | fail |
| 48 | `memory/supersedes.py:265-285` | B4 | `link_supersedes` new_meta `write_text` fails | `emit_data` | fail |
| 49 | `memory/supersedes.py:293-339` | B4 | `link_supersedes` prior back-link `write_text` fails | `emit_data` | fail |
| 50 | `memory/enrichment/entity_linker.py:195-208` | B4 | Back-link `write_text` on peer memory fails | `emit_data` | degraded |
| 51 | `memory/enrichment/entity_linker.py:224-238` | B4 | Forward-link `write_text` on new memory fails | `emit_data` | degraded |
| 52 | `memory/strength_index.py:562-565` | B4 | `_flush_singleton` `save(force=True)` fails at shutdown | `emit_data` | degraded |
| 53 | `runtime/face/recognition.py:176-184` | B4 | `persist_brain_anchor` raises (anchor_store `_persist` OSError) | `emit_data` | fail |

### 2.6 knowledge_extractor / sensory / autonomic / guardian / hud (14 paths)

| # | file:line | Batch | What malfunctions | Emit helper | Status |
|---|---|---|---|---|---|
| 54 | `hud/telemetry.py:597-606` | B1 | charter/ledger probe (combined `_probe_charter`) fails | `emit_data` (`charter_probe_failed`) | degraded |
| 55 | `autonomic/cognitive_churn.py:648-649` | B3 | Content fingerprints read fails | `emit_data` | fail |
| 56 | `sensory/operator_presence.py:468-470` | B3 | Broker exception in presence detector | `emit_data` | degraded |
| 57 | `hud/telemetry.py:368` | B3 | Journal clean check returns `True` on exception | `emit_data` | fail |
| 58 | `hud/telemetry.py:863` | B3 | Shared state read returns `None` on exception | `emit_data` | fail |
| 59 | `hud/telemetry.py:882` | B3 | Shared state key read returns `None` on exception | `emit_data` | fail |
| 60 | `hud/server.py:185` | B3 | URL path resolution returns empty string on exception | `emit_data` | fail |
| 61 | `hud/server.py:2663` | B3 | Strength index read returns `None` on exception | `emit_data` | fail |
| 62 | `hud/server.py:2053` | B3 | Vision upload base64 encode fails (face stored without thumbnail) | `emit_data` (`vision_upload_b64_failed`) | degraded |
| 63 | `hud/server.py:3153` | B3 | `enrolled_operator_name()` returns `None` on exception | `emit_data` | fail |
| 64 | `hud/server.py:3168` | B3 | Heartbeat read returns empty string on exception | `emit_data` | fail |
| 65 | `hud/server.py:3183-3191` | B3 | `presence_is_stale()` raises in `/api/presence/state` | `emit_data` (`presence_staleness_failed`) | fail |
| 66 | `hud/telemetry.py:248` | B3 | Presence read returns `"UNKNOWN"` on exception | `emit_data` | fail |
| 67 | `hud/telemetry.py:294` | B3 | Operator name read returns empty on exception | `emit_data` | fail |

### 2.7 Pre-existing component helpers (already covered before batches)

These helpers emit on **every** failure path that calls them; no batch code was needed:

| Helper | Where | Mechanism |
|---|---|---|
| `_fail()` | `memory/encoding.py:405-431` | `emit_data("memory", "encode_committed_memory", status="fail", ok=False, ...)` |
| `_emit(ok=False)` | `memory/gates/base.py` | `data_logger.emit_event(..., ok=False)` → mirror → sink |
| `_emit_promotion(..., ok=False)` | `memory/promotion.py` | `data_logger.emit_event(..., ok=False)` → mirror → sink |
| `_emit_failure` → `emit_signal` | `runtime/model_router.py` | `emit_signal(..., status="degraded")` → `_route_telemetry` → sink |
| `phase_span` raise | Any call site wrapped | Auto `status="fail"` on exception, then re-raise |

---

## 3. KNOWN CHARACTERISTIC: dual-route DOUBLE-EMIT

Some failure paths write **two correlated records** to `failures.jsonl` for a single fault — one via `terminal_feedback.emit_*` and one via the `data_logger` mirror. This is **accepted by design**: completeness > cleanliness. The mirror guarantees that `data_logger`-only paths (pre-existing helper calls) reach the sink, but when a batch emit is added at the same site, both routes fire.

### Rows with confirmed double-emit

| Row(s) | file:line | Why two records appear |
|---|---|---|
| 29 | `memory/gates/base.py:521-523` (`walker_recall_failed`) | Batch emit added `emit_data(..., ok=False)`; existing `data_logger.emit_event(..., ok=False)` mirror also forks to sink. |
| 30 | `memory/gates/base.py:568-569` (`index_load_failed`) | Same pattern: batch emit + existing `data_logger` mirror with `ok=False`. |
| 39 | `memory/promotion.py:181-184` (`audit_write_failed`) | `_emit_audit` calls `data_logger.emit_event(..., ok=False)`; batch added `emit_data` in the same `except` block. |
| 41 | `runtime/model_router.py:172-184` (`model_router_audit_write_failed`) | Same: `_emit_audit` data_logger mirror + batch `emit_data`. |

**Remote-monitor grouping:** a downstream monitor should group by `(episode_id, ts, reason)` or `(run_id, phase, error)` to collapse correlated duplicates into one actionable alert. The double-emit does NOT unbound the log — it only affects these rare failure branches.

---

## 4. DIST-DEFERRED

The following telemetry artifacts are **not** part of the failure-log contract but are noted for cleanup at distribution/packaging time:

| Artifact | Size | Action |
|---|---|---|
| `guardian/telemetry/firehose.jsonl` | ~86 MB | Remove at packaging — it logs everything; the failure log is the bounded replacement. |
| `guardian/telemetry/*.jsonl` (per-system files) | ~162 MB total | Remove at packaging — or rotate aggressively. These are diagnostic, not operational. |

The failure log itself stays small because it is **failure-only**. On a healthy run it should be empty or single-digit bytes. Do NOT include the firehose in any remote-monitor pull.

---

## 5. REMOTE-MONITOR CONTRACT

### 5.1 What the monitor pulls

A remote health monitor watches **one file**:

```text
guardian/logs/failures.jsonl
```

### 5.2 Semantics

- **Bounded:** writes ONLY on failure. On a healthy system the file is empty.
- **A healthy full-system run leaving `failures.jsonl` empty IS the pre-dist smoke signal.**
- **Non-empty** means at least one subsystem reported a malfunction during that run. The monitor should:
  1. Group by `episode_id` + `ts` + `reason` to deduplicate double-emits.
  2. Surface the most recent `status=fail` / `status=crash` rows first.
  3. Treat `status=degraded` as lower urgency — the system continued, but a fallback path was taken.

### 5.3 Schema of each line

```json
{
  "ts": "2026-06-08T12:34:56.789012+00:00",
  "run_id": "uuid-or-no-run",
  "episode_id": "ep-123",
  "system": "pressure",
  "phase": "episode_close",
  "event": "data",
  "status": "degraded",
  "summary": "accumulator_reset_failed",
  "reason": "accumulator_reset_failed",
  "ok": false,
  "error": "RuntimeError: induced reset failure"
}
```

Required fields: `ts`, `run_id`, `episode_id`, `system`, `phase`, `event`, `status`.  
Optional but common: `summary`, `reason`, `ok`, `error`, `latency_ms`, `memory_id`, `tool`, `candidate_count`.

---

## 6. INTENTIONAL-SILENT (reference)

These paths remain deliberately silent; do NOT instrument them:

| file:line | Reason |
|---|---|
| `guardian/telemetry/writer.py:130,169` | Telemetry plumbing — failure-soft by design; recursion risk |
| `guardian/terminal_feedback.py:147-148,179,218,267,290,311` | Observability plumbing — self-spam risk if emit fails |
| `guardian/data_logger.py:88,97,115` | Data_logger plumbing — failure-soft by design |
| All `except Exception: pass` around existing `emit_*` calls in loops | Defensive emit wrappers — operator directive: telemetry never halts cognition |
| Benign-empty branches | Normal-empty is NOT malfunction (e.g. no open questions, episode already active, first-boot no trajectory) |

Full list: see `FAILURE_LOG_COVERAGE_MAP.md` §INTENTIONAL-SILENT.

---

## 7. VERIFICATION

- **Unit tests:** `tests/core/test_failure_log_batch1.py` (16 rows), `tests/core/test_failure_log_batch2.py` (27 rows), `tests/core/test_failure_log_batch3.py` (15 rows), `tests/core/test_failure_log_batch4.py` (11 rows) — each asserts that the induced failure reaches `writer.FAILURE_LOG` filtered by `reason`/`phase`.
- **Gate:** `.venv-brain\Scripts\python.exe -m pytest tests/core/test_failure_log_batch*.py -o addopts="" -p no:cacheprovider -q` — all green.
- **Static:** every `emit_data` / `emit_phase` / `emit_inference` / `emit_memory` import resolves; no unbound names.
- **Live:** induced failures during boot-smoke confirm records appear in the temp-isolated log.
