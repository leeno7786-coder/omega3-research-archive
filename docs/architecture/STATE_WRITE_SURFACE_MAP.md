# State Write Surface Map — historical day-zero reset inventory

> **Historical snapshot (2026-06-09).** This generated workorder inventory is
> not the current reset authority. It predates the SetFit retirement and other
> state-surface migrations. Verify paths against `scripts/day_zero_reset.py`,
> `core/paths.py`, and the current package READMEs before using it.

**Generated:** 2026-06-09 by Devin (read-only static analysis)  
**Authority:** `docs/_archive/architecture/STATE_WRITE_SURFACE_MAP_WORKORDER.md`  
**Goal:** give Cursor a concrete spec to build a correct `day_zero_reset.py`.

---

## 1. INVENTORY

Category legend:
- **ACCUM** — accumulated cognition state (must be cleared on day-zero)
- **STRUCT** — structural scaffolding / build artifacts (preserve)
- **SACRED** — never touched (ledger, credentials, model weights)
- **PROC** — process-only runtime lock (not a state wipe target)

GAP legend:
- **MISS** — day_zero_reset never touches this path
- **STALE** — day_zero_reset clears a legacy path, nothing reads it
- **NOOP** — day_zero_reset calls a function whose module is deleted → no-op
- **OK** — cleared at the canonical path the cognition uses

| # | Canonical path | Writer (file:line) | Reader (file:line) | Category | day_zero disposition | GAP? |
|---|----------------|----------------------|--------------------|----------|----------------------|------|
| 1 | `memory/000_identity/*.json` … `memory/1100_dev/*.json` | `memory.engine.validate_write.atomic_write` (`validate_write.py:153`) | `memory.engine.walker` (`walker.py:24`), `memory.api.read_memory` (`api.py:51`) | ACCUM | Cleared by `wipe_corpus()` | OK |
| 2 | `memory/indexes/coord_index.json` | `memory.engine.validate_write._patch_index_unsafe` (`validate_write.py:217`), `memory.engine.reindex` (`reindex.py:19`) | `memory.engine.walker` (`walker.py:25`), `memory.api.memory_health` (`api.py:1387`) | ACCUM | Reset to `{}` by `reset_coord_index()` | OK |
| 3 | `memory/indexes/label_map.json` | Build-time only (`scripts/setfit_dataset/build_datasets.py:48` reads; `memory.engine.train_setfit.py:18` reads) | `memory.engine.train_setfit` (`train_setfit.py:18`), `tests/memory/test_memory_comprehensive.py:103` | STRUCT | Not touched | OK (preserve STRUCT) |
| 4 | `memory/tmp_writes/*.tmp` | `memory.engine.validate_write.atomic_write` (`validate_write.py:168`) | None (temp only) | ACCUM | Cleared by `wipe_tmp_writes()` | OK |
| 5 | `guardian/state/strength_index.json` | `memory.strength_index.StrengthIndex.save` (`strength_index.py:157`) | `memory.strength_index.StrengthIndex._load` (`strength_index.py:124`), `memory.api.memory_health` | ACCUM | Reset to empty by `reset_strength_index()` | OK |
| 6 | `guardian/state/knowledge_z_index.json` | `memory.knowledge_router._save_index` (`knowledge_router.py:121`) | `memory.knowledge_router._load_index` (`knowledge_router.py:94`) | ACCUM | Reset to empty by `reset_knowledge_z_index()` | OK |
| 7 | `guardian/state/boot.lock` | `core.boot_lock._write_lock` (`boot_lock.py:132`) | `core.boot_lock._read_lock` (`boot_lock.py:105`) | PROC | Not touched | OK (process-only, must NOT wipe) |
| 8 | `deepsleep_loop/state/mastery.json` | `deepsleep_loop.mastery_store.upsert_mastery` (`mastery_store.py:88`) | `deepsleep_loop.mastery_store.load_mastery` (`mastery_store.py:59`), `cognition.topic_resolution.resolve_topic` (`resolve_topic.py:196`), `omega_loop.intake.run` | ACCUM | **Missed entirely** | **MISS** |
| 9 | `deepsleep_loop/state/trajectory.json` | `deepsleep_loop.trajectory_store.save_trajectory` (`trajectory_store.py:66`) | `deepsleep_loop.trajectory_store.load_trajectory` (`trajectory_store.py:41`) | ACCUM | **Missed entirely** | **MISS** |
| 10 | `deepsleep_loop/state/failure.json` | `deepsleep_loop.failure_store.upsert_failure` (`failure_store.py:68`) | `deepsleep_loop.failure_store.load_failures` (`failure_store.py:40`), `cognition.topic_resolution.resolve_topic` (`resolve_topic.py:345`) | ACCUM | **Missed entirely** | **MISS** |
| 11 | `deepsleep_loop/state/identity.json` | `deepsleep_loop.identity_store.upsert_trait` (`identity_store.py:72`) | `deepsleep_loop.identity_store.load_identity` (`identity_store.py:43`) | ACCUM | **Missed entirely** | **MISS** |
| 12 | `deepsleep_loop/state/semantic.json` | `deepsleep_loop.semantic_store.upsert_semantic` (`semantic_store.py:70`) | `deepsleep_loop.semantic_store.load_semantic` (`semantic_store.py:40`) | ACCUM | **Missed entirely** | **MISS** |
| 13 | `deepsleep_loop/state/projection.json` | `deepsleep_loop.projection_store.write_projection` (`projection_store.py:52`) | `deepsleep_loop.projection_store.read_projection` (`projection_store.py:37`) | ACCUM | **Missed entirely** | **MISS** |
| 14 | `deepsleep_loop/state/open_queries.json` | `deepsleep_loop.query_store.upsert_query` (`query_store.py:71`) | `deepsleep_loop.query_store.load_queries` (`query_store.py:44`), `open_queries` | ACCUM | **Missed entirely** | **MISS** |
| 15 | `deepsleep_loop/state/journal.jsonl` | `deepsleep_loop.journal_store.append_journal_entry` (`journal_store.py:38`) | `deepsleep_loop.journal_store.read_journal` (`journal_store.py:59`) | ACCUM | **Missed entirely** | **MISS** |
| 16 | `guardian/state/reflection_window.json` | `pressure_loop.reflection_window.save_reflection_window` (`reflection_window.py:180-186` inferred from load/save symmetry) | `pressure_loop.reflection_window.load_reflection_window` (`reflection_window.py:223`) | ACCUM | Removed by `wipe_v2_state_residue()` | OK |
| 17 | `guardian/state/current_trajectory.json` | `pressure_loop.reflection_window.save_current_trajectory` (`reflection_window.py:185`) | `pressure_loop.reflection_window.load_current_trajectory` (`reflection_window.py:137`) | ACCUM (legacy) | Removed by `wipe_v2_state_residue()` | OK |
| 18 | `guardian/state/save_state.json` | **Module deleted** — `guardian.state_persistence` no longer exists | **Module deleted** | ACCUM (legacy) | `reset_guardian_save_state()` imports deleted module → **no-op** | **NOOP** |
| 19 | `guardian/state/order_queue.json` | Unknown (legacy) | Unknown (legacy) | ACCUM (legacy) | Reset to `[]` by `reset_order_queue()` | OK |
| 20 | `guardian/state/face_anchors.json` | `runtime.face.anchor_store.AnchorStore._persist` (`anchor_store.py:85`) | `runtime.face.anchor_store.AnchorStore._ensure_loaded` (`anchor_store.py:66`) | ACCUM | Emptied to `{}` by `reset_guardian_state_transients()` | OK |
| 21 | `guardian/state/persona_index.json` | Unknown (legacy) | Unknown (legacy) | ACCUM (legacy) | Emptied to `{}` by `reset_guardian_state_transients()` | OK |
| 22 | `guardian/state/knowledge_mastery.json` | `pressure_loop.symphony_learning_state` (`symphony_learning_state.py:502`) | `autonomic.cognitive_churn._load_mastery_scores` (`cognitive_churn.py:519`) | ACCUM (legacy) | Zeroed by `reset_knowledge_mastery()` | **STALE** — cognition reads `deepsleep_loop/state/mastery.json` (via `deepsleep_mastery_path()`), NOT this path |
| 23 | `guardian/state/omega_open_questions.jsonl` | `pressure_loop.open_questions._append` (`open_questions.py:158`) | `pressure_loop.open_questions._load_all` (`open_questions.py:176`) | ACCUM | Removed by `reset_guardian_state_transients()` | OK |
| 24 | `guardian/state/global_operator_outreach.json` | **Module deleted** — `core.global_operator_outreach.py` no longer exists | None | ACCUM (legacy) | Removed by `reset_guardian_state_transients()` | **STALE** — live outreach is fields inside `omega_open_questions.jsonl` (OpenQuestion dataclass) |
| 25 | `guardian/state/global_operator_outreach.jsonl` | **Module deleted** | None | ACCUM (legacy) | Removed by `reset_guardian_state_transients()` | **STALE** |
| 26 | `guardian/state/autonomous_outcomes.jsonl` | **Replaced by SharedState** — schema says "replaces autonomous_outcomes.jsonl" (`shared_state.schema.json:53`) | `autonomic.cognitive_churn._read_recent_outcomes` (`cognitive_churn.py:447`) | ACCUM (legacy) | Removed by `reset_guardian_state_transients()` | **STALE** — writer is dead; reader is legacy orphan |
| 27 | `guardian/state/encoding_journal.jsonl` | `memory.encoding_journal._append` (`encoding_journal.py:93`) | `memory.encoding_journal.find_unclosed_entries` (`encoding_journal.py:205`) | ACCUM | Removed by `reset_guardian_state_transients()` | OK |
| 28 | `guardian/state/boot_self_state_snapshot.json` | Unknown (legacy) | Unknown (legacy) | ACCUM (legacy) | Removed by `reset_guardian_state_transients()` | OK |
| 29 | `guardian/state/goals.jsonl` | Unknown (legacy — WAL projection retired per S7) | Unknown (legacy) | ACCUM (legacy) | Removed by `reset_guardian_state_transients()` | OK |
| 30 | `guardian/state/identity/current_self_vector.json` | `memory.self_vector._atomic_write_json` (`self_vector.py:85`) | `memory.self_vector.read_current_self_vector` (`self_vector.py:235`) | ACCUM | Cleared (subdir emptied) by `wipe_v2_state_residue()` | OK |
| 31 | `guardian/state/identity/self_vector_history.jsonl` | `memory.self_vector.update_self_vector` (`self_vector.py:129`) | `memory.self_vector._read_history_tail` (`self_vector.py:212`) | ACCUM | Cleared (subdir emptied) by `wipe_v2_state_residue()` | OK |
| 32 | `guardian/state/queues/reflection_eligible.jsonl` | `memory.queues._append_jsonl` (`queues.py:89`) | `memory.queues.drain_jsonl` (`queues.py:234`) | ACCUM | Cleared (subdir emptied) by `wipe_v2_state_residue()` | OK |
| 33 | `guardian/state/queues/deepsleep_candidates.jsonl` | `memory.queues._append_jsonl` (`queues.py:89`) | `memory.queues.drain_jsonl` (`queues.py:234`) | ACCUM | Cleared (subdir emptied) by `wipe_v2_state_residue()` | OK |
| 34 | `guardian/state/quarantine/*.json` + `quarantine_index.json` | `memory.promotion._quarantine_memory` (`promotion.py:904`) | `memory.promotion` reads quarantine | ACCUM | Cleared (subdir emptied) by `wipe_v2_state_residue()` | OK |
| 35 | `guardian/state/uploads/**/*` | `runtime.document_upload._resolve_upload_path` (`document_upload.py:33`) | `runtime.document_upload` | ACCUM | Cleared (subdir emptied) by `wipe_v2_state_residue()` | OK |
| 36 | `guardian/state/omega_cli.pid` | Unknown (omega_cli_ts?) | Unknown | PROC | Removed by `wipe_v2_state_residue()` | OK |
| 37 | `guardian/state/hud/theme_settings.json` | `hud.settings._write_hud_theme_settings` (`settings.py:231`) | `hud.settings.load_hud_theme_settings` (`settings.py:220`) | ACCUM | **Missed entirely** | **MISS** |
| 38 | `guardian/state/hud/voice_settings.json` | `hud.settings._write_voice_settings` (`settings.py:437`) | `hud.settings.load_voice_settings` (`settings.py:410`) | ACCUM | **Missed entirely** | **MISS** |
| 39 | `guardian/state/documents/**/*` | `runtime.document_store` (`document_store.py:56`) | `runtime.document_store` (`document_store.py:74`) | ACCUM | **Missed entirely** | **MISS** |
| 40 | `guardian/state/images/**/*` | `runtime.image_store` (`image_store.py:65`) | `runtime.image_store` (`image_store.py:83`) | ACCUM | **Missed entirely** | **MISS** |
| 41 | `guardian/state/coding_snapshots/**/*` | `memory.coding_project` (`coding_project.py:229`) | `memory.coding_project` | ACCUM | **Missed entirely** | **MISS** |
| 42 | `pressure_loop/state/episode_journal.jsonl` | `pressure_loop.episode_journal` (`episode_journal.py:294`) | `pressure_loop.episode_journal.journal_window` | ACCUM | **Missed entirely** | **MISS** |
| 43 | `guardian/logs/*.jsonl` (subsystem logs) | `guardian.data_logger.emit_event` (`data_logger.py:94`), `guardian.decision` (`decision.py:259`), `guardian.telemetry.writer` (`writer.py:167` for failures) | `guardian.data_logger.read_events` (`data_logger.py:139`) | ACCUM | Cleared by `wipe_runtime_jsonl()` + `wipe_logs()` | OK |
| 44 | `guardian/logs/failures.jsonl` | `guardian.telemetry.writer` (`writer.py:167`) | None (failure sink) | ACCUM | Cleared by `wipe_logs()` | OK |
| 45 | `guardian/telemetry/*.jsonl` | `guardian.telemetry.writer._append_line` (`writer.py:127`) | HUD telemetry | ACCUM | Cleared by `wipe_telemetry_firehose()` | OK |
| 46 | `guardian/telemetry/*.log` (incl. terminal.log) | `guardian.telemetry.writer` (firehose), `guardian.terminal_console._install_file_handler` (`terminal_console.py:218`) | HUD / operator | ACCUM | Cleared by `wipe_telemetry_firehose()` | OK |
| 47 | `guardian/ledger/chain.jsonl` | `guardian.hmac_ledger` (`hmac_ledger.py:287`) | `guardian.hmac_ledger` | SACRED | Never touched | OK |
| 48 | `guardian/ledger/key.bin` | `guardian.hmac_ledger` (`hmac_ledger.py:91`) | `guardian.hmac_ledger` | SACRED | Never touched | OK |
| 49 | `guardian/ledger/.activated` | `guardian.production_mode` (`production_mode.py:44`) | `guardian.production_mode` | SACRED | Never touched | OK |
| 50 | `guardian/state/credential_ledger.enc` | Unknown | Unknown | SACRED | Never touched | OK |
| 51 | `runtime/models/**/*` (GGUF / ONNX / SetFit) | `memory.engine.train_setfit` (`train_setfit.py:655`), external portable-lite | portable-lite runtime | SACRED | Never touched | OK |
| 52 | `.omega_data/**/*` | Legacy (retired) | None | ACCUM (legacy) | Removed by `wipe_legacy_omega_data_tree()` | OK |
| 53 | `guardian/state/staging/staging-*.json` | `memory.promotion` (`promotion.py:178`) | `memory.promotion` (`promotion.py:1036`) | ACCUM | Cleared by `wipe_staging()` | OK |
| 54 | `guardian/logs/model_router_audit.jsonl` | `runtime.model_router._emit_audit` (`model_router.py:179`) | None | ACCUM | Cleared by `wipe_runtime_jsonl()` (in RUNTIME_JSONL list) | OK |
| 55 | `guardian/logs/cycle_cadence_audit.jsonl` | `pressure_loop.cycle_cadence` (`cycle_cadence.py:169`) | None | ACCUM | Cleared by `wipe_logs()` (generic `guardian/logs/*` wipe) | OK |

---

## 2. DAY-ZERO GAP SUMMARY

**Total gaps found: 14**

### A. The big miss — entire `deepsleep_loop/state/` directory (8 files)
The current `day_zero_reset.py` does **not** touch `deepsleep_loop/state/` at all. This directory contains ~1.35 MB of live cognition state that survives resets.

| Canonical path | What goes stale if missed |
|----------------|---------------------------|
| `deepsleep_loop/state/mastery.json` | Mastery scores (knowledge/application per domain) — Omega + topic resolution read this |
| `deepsleep_loop/state/trajectory.json` | Trajectory records (target_domain + capability) — continuity structure |
| `deepsleep_loop/state/failure.json` | Failure patterns — topic resolution reads for execution-blockage detection |
| `deepsleep_loop/state/identity.json` | Learned identity traits — DeepSleep consolidation output |
| `deepsleep_loop/state/semantic.json` | Promoted semantic knowledge — DeepSleep consolidation output |
| `deepsleep_loop/state/projection.json` | Self-view projection state — DeepSleep self-model |
| `deepsleep_loop/state/open_queries.json` | Open questions — DeepSleep query tracking |
| `deepsleep_loop/state/journal.jsonl` | DeepSleep continuity journal |

**Correct action:** add a `wipe_deepsleep_state()` function that deletes every `.json` / `.jsonl` under `deepsleep_loop/state/` (or enumerate the 8 canonical paths above). The default paths come from `pressure_loop.policy.deepsleep_*_path()` functions; in practice they all resolve under `deepsleep_loop/state/`.

### B. Stale-path clear — `guardian/state/knowledge_mastery.json` (LEGACY)
- `reset_knowledge_mastery()` zeroes `guardian/state/knowledge_mastery.json`
- **Nothing in the live path reads this file.**
- The live mastery store is `deepsleep_loop/state/mastery.json` (via `deepsleep_mastery_path()`)
- **Correct action:** remove `reset_knowledge_mastery()` from day_zero; add mastery to the deepsleep wipe instead.

### C. NOOP clear — `guardian/state/save_state.json`
- `reset_guardian_save_state()` imports from `guardian.state_persistence` which **no longer exists on disk**
- The function catches `Exception` and returns `False`, so the reset prints "absent (no-op)"
- **The file may actually exist on disk** (from prior boots) but is never deleted because the importer fails before the deletion runs.
- **Correct action:** inline the deletion (unlink if exists) instead of importing a deleted module.

### D. Live state missed by day_zero
| Path | Writer | Why it matters |
|------|--------|----------------|
| `guardian/state/hud/theme_settings.json` | `hud/settings.py:231` | Operator HUD appearance — survives reset |
| `guardian/state/hud/voice_settings.json` | `hud/settings.py:437` | Operator voice/TTS config — survives reset |
| `guardian/state/documents/**/*` | `runtime/document_store.py:56` | Uploaded documents — survives reset |
| `guardian/state/images/**/*` | `runtime/image_store.py:65` | Stored images — survives reset |
| `guardian/state/coding_snapshots/**/*` | `memory/coding_project.py:229` | Coding project snapshots — survives reset |
| `pressure_loop/state/episode_journal.jsonl` | `pressure_loop/episode_journal.py:294` | Episode journal buffer — survives reset |

**Correct action:** add wipe functions for each of the above (or add their directories to an existing generic wipe).

### E. Legacy files still cleared (harmless but dead)
These files are deleted by `reset_guardian_state_transients()` but their writers no longer exist:
- `global_operator_outreach.json` / `.jsonl` — `core/global_operator_outreach.py` was deleted; live outreach is in `omega_open_questions.jsonl`
- `autonomous_outcomes.jsonl` — replaced by SharedState `last_auto_cycle`; reader `autonomic.cognitive_churn` still tries to read it, but writer is dead
- `goals.jsonl` — WAL-projection retired per S7 audit
- `boot_self_state_snapshot.json` — unknown writer, likely legacy

These are **not functional gaps** (clearing a dead file is harmless), but they clutter the reset script. A future cleanup could drop them once the last readers are removed.

---

## 3. PRESERVE LIST (SACRED + STRUCTURAL — NEVER WIPE)

| Path | Why preserved |
|------|---------------|
| `guardian/ledger/chain.jsonl` | HMAC chain — birthed once per instance |
| `guardian/ledger/key.bin` | HMAC seed — never leaves the machine |
| `guardian/ledger/.activated` | Production sentinel — once set, permanent |
| `guardian/state/credential_ledger.enc` | Operational secrets vault |
| `runtime/models/**/*` | Model weights (GGUF / ONNX / SetFit) — multi-GB substrate |
| `memory/indexes/label_map.json` | SetFit classifier label vocabulary — build artifact derived from `dewey_map` |
| `config/**/*` | Static policy/schema/config files — not runtime data |
| `schema/**/*` | JSON schemas — not runtime data |
| `guardian/state/boot.lock` | Process boot lock — must survive to detect double-boot; removed by `shutdown.py` or atexit only |

---

## 4. QUICK REFERENCE — path accessor functions

All canonical paths should be resolved through these accessors (do not hardcode strings):

| Accessor | Module | Default resolved path |
|----------|--------|----------------------|
| `OMEGA_ROOT` | `core.paths` | repo root |
| `MEMORY_ROOT` | `core.paths` | `memory/` |
| `GUARDIAN_STATE_ROOT` | `core.paths` | `guardian/state/` |
| `GUARDIAN_LOGS_ROOT` | `core.paths` | `guardian/logs/` |
| `STAGING_ROOT` | `core.paths` | `guardian/state/staging/` |
| `STRENGTH_INDEX_PATH` | `memory.strength_index` | `guardian/state/strength_index.json` |
| `reflection_window_path()` | `pressure_loop.reflection_window` | `guardian/state/reflection_window.json` |
| `current_trajectory_path()` | `pressure_loop.reflection_window` | `guardian/state/current_trajectory.json` |
| `episode_journal_path()` | `pressure_loop.policy` | `pressure_loop/state/episode_journal.jsonl` |
| `deepsleep_mastery_path()` | `pressure_loop.policy` | `deepsleep_loop/state/mastery.json` |
| `deepsleep_trajectory_path()` | `pressure_loop.policy` | `deepsleep_loop/state/trajectory.json` |
| `deepsleep_failure_path()` | `pressure_loop.policy` | `deepsleep_loop/state/failure.json` |
| `deepsleep_identity_path()` | `pressure_loop.policy` | `deepsleep_loop/state/identity.json` |
| `deepsleep_semantic_path()` | `pressure_loop.policy` | `deepsleep_loop/state/semantic.json` |
| `deepsleep_projection_path()` | `pressure_loop.policy` | `deepsleep_loop/state/projection.json` |
| `deepsleep_query_path()` | `pressure_loop.policy` | `deepsleep_loop/state/open_queries.json` |
| `deepsleep_journal_path()` | `pressure_loop.policy` | `deepsleep_loop/state/journal.jsonl` |

---

## 5. RECOMMENDED RESET SCRIPT CHANGES (for Cursor)

1. **Add `wipe_deepsleep_state()`** — delete all `.json` / `.jsonl` under `deepsleep_loop/state/` (or enumerate the 8 canonical files). This is the highest-impact fix.
2. **Remove `reset_knowledge_mastery()`** — it clears a legacy path no reader uses.
3. **Fix `reset_guardian_save_state()`** — inline the `save_state.json` unlink; do not import the deleted `guardian.state_persistence` module.
4. **Add HUD settings wipe** — `guardian/state/hud/theme_settings.json` + `voice_settings.json`.
5. **Add runtime document/image wipe** — `guardian/state/documents/` + `guardian/state/images/`.
6. **Add coding snapshots wipe** — `guardian/state/coding_snapshots/`.
7. **Add episode journal wipe** — `pressure_loop/state/episode_journal.jsonl`.
8. **(Optional cleanup)** Remove dead legacy files from `reset_guardian_state_transients()` once their last readers are retired: `global_operator_outreach.json`, `autonomous_outcomes.jsonl`, `goals.jsonl`, `boot_self_state_snapshot.json`.
