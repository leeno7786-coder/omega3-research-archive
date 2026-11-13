# Failure-Log Coverage Map

**Work order:** `e9cd2704` (claude → devin) · **Scope:** read-only MAP of silent failure paths across all systems.

---

## pressure_loop

| file:line | failure path (what malfunctions) | current behavior | cat | recommended emit | priority |
|---|---|---|---|---|---|
| `pressure_loop/episode.py:385-389` | `reset_pressure_accumulator()` fails inside bare `except: pass` | silent swallow — next episode opens on corrupted accumulator state | 1 | `emit_data("pressure", "episode_close", status="degraded", reason="accumulator_reset_failed", episode_id=..., ok=False, error=str(exc))` | **P0** |
| `pressure_loop/episode.py:393-398` | `archive_episode(ep)` fails inside `except: pass` | `log.exception()` only — no unified emit; episode lost from journal | 1 | `emit_data("pressure", "episode_close", status="degraded", reason="archive_failed", episode_id=..., ok=False, error=str(exc))` | P1 |
| `pressure_loop/episode.py:400-411` | `_append_episode_to_reflection_window()` fails inside `except: pass` | `log.exception()` only — no unified emit; Omega never sees episode | 1 | `emit_data("pressure", "episode_close", status="degraded", reason="reflection_window_append_failed", episode_id=..., ok=False, error=str(exc))` | P1 |
| `pressure_loop/pressure.py:403-408` | `shared_state.read(LAST_INGRESS_PRESSURE)` returns non-dict for operator message contributor | silent drop — falls through to generic contributor stub | 2 | `emit_data("pressure", "gather_contributors", status="degraded", reason="operator_message_malformed", field_id="operator_message", ok=False)` | P1 |
| `pressure_loop/temporal_store.py:129-145` | `read_trajectory()` raises `RuntimeError` on corrupt trajectory record | raise outside `phase_span` — crash cause never reaches unified log before process dies | 4 | Wrap in `phase_span` or emit before raise: `emit_data("pressure", "read_trajectory", status="fail", reason="corrupt_record", ok=False, error=str(exc))` | P1 |
| `pressure_loop/operator_pressure.py:252-297` | `read_operator_directive()` returns `None` on malformed timestamp (`ValueError`/`TypeError`) | silent degradation — treats as expired with no emit | 3 | `emit_data("pressure", "read_operator_directive", status="degraded", reason="malformed_timestamp", ok=False, error=str(exc))` | P2 |
| `pressure_loop/open_questions.py:243-251` | `_append(q.to_dict())` fails in `record_open_question()` | returns `None` — only logs warning, no emit | 2 | `emit_data("pressure", "record_open_question", status="fail", reason="append_failed", ok=False, error=str(exc))` | P2 |
| `pressure_loop/pressure.py:146-163` | `evaluate()` neuromod/spike extraction fails | returns zero dict silently; pressure metrics vanish | 1 | `emit_data("pressure", "evaluate", status="degraded", reason="neuromod_extraction_failed", ok=False, error=str(exc))` | P2 |

## global_loop / meta_loop / cognition_chain

| file:line | failure path (what malfunctions) | current behavior | cat | recommended emit | priority |
|---|---|---|---|---|---|
| `global_loop/ignition/run.py:10-15` | validation raises `ValueError` on missing ignition fields | raise outside `phase_span` — crash cause never reaches unified log | 4 | Emit before raise or wrap in `phase_span` at call site (`cognition_chain._start_global_a`) | **P0** |
| `global_loop/assemble/run.py:32-37` | `meta_b_input_from_episode(episode)` fails | bare `except: pass` — silently falls back to `None` | 1 | `emit_data("global", "assemble", status="degraded", reason="meta_b_input_failed", ok=False, error=str(exc))` | P1 |
| `global_loop/extract/run.py:13-18` | policy import/call fails | silent fallback to hardcoded 120 with no emit | 1 | `emit_data("global", "extract", status="degraded", reason="policy_load_failed", ok=False, error=str(exc))` | P2 |
| `meta_loop/consistency/run.py:93-150` | critical consistency mismatch detected | returns `disposition="invalid"` / `status="invalid"` downstream, but no explicit failure emit at detection site | 2 | `emit_data("meta", "consistency", status="fail", reason="critical_mismatch", ok=False)` | P2 |

## auto_loop / execute / tools

| file:line | failure path (what malfunctions) | current behavior | cat | recommended emit | priority |
|---|---|---|---|---|---|
| `auto_loop/execute/run.py:480-483` | tool `invoke()` returns `{"ok": False, ...}` but code never checks `ok` flag | silent success — marks `tool_status="success"` despite tool-reported failure | 5 | Check `inv.get("ok", True)`; on False: `emit_data("auto", "tool_invocation", status="fail", ok=False, error=inv.get("error"), tool=...)` | P1 |
| `auto_loop/execute/run.py:507-511` | tool `invoke()` raises exception | caught and stored in `outputs["tool_error"]` with `failure_kind="tool_failed"`, but **no emit** to unified log | 5 | `emit_data("auto", "tool_invocation", status="fail", ok=False, error=str(exc), tool=..., episode_id=...)` | P1 |
| `auto_loop/execute/run.py:311-313` | `run_forced_memory_tools()` raises during MRIL recall | returns `False` with `mril_recall_error` set, but no emit | 2 | `emit_data("auto", "mril_recall_failed", status="fail", ok=False, error=str(exc), episode_id=...)` | P1 |
| `auto_loop/execute/run.py:392-402` | `run_forced_memory_tools()` raises during hop-2 recall | returns failure dict with `failure_kind="memory_continuity_substrate_invalid"`, but no emit | 5 | `emit_data("auto", "mril_hop2_recall_failed", status="fail", ok=False, error=str(exc), episode_id=...)` | P1 |
| `runtime/socket.py:70-71` | preflight check declines operation | returns `None` silently; caller treats as "not a runtime tool" | 2 | `emit_data("runtime", "socket_preflight_declined", status="degraded", ok=False, reason=..., operation=...)` | P1 |
| `auto_loop/execute/run.py:588-595` | `persist_generated_image()` fails | degraded to in-memory artifact with only `warnings.append()`; no `status="degraded"` emit | 3 | `emit_data("auto", "image_persist_failed", status="degraded", ok=False, error=str(exc), episode_id=...)` | P2 |
| `auto_loop/execute/run.py:160-172` | `knowledge_extractor.extract()` fails | degraded to no artifact with only `warnings.append()`; no emit | 3 | `emit_data("auto", "knowledge_extract_failed", status="degraded", ok=False, error=str(exc), episode_id=...)` | P2 |
| `auto_loop/execute/run.py:505-506` | `_generate()` for tool-result synthesis returns `None`/falsy | tool still marked `"success"`; synthesis missing silently | 3 | `emit_data("auto", "tool_result_synthesis", status="degraded", ok=False, reason="synthesis_unavailable", episode_id=...)` | P2 |
| `schema/tools/web_fetch.py:381-382` | direct URL fetch (`requests.get`) fails | `except Exception: pass` — returns empty results silently | 1 | `emit_data("tools", "web_fetch_direct_fetch_failed", status="fail", ok=False, error=str(exc), query=...)` | P1 |
| `schema/tools/web_fetch.py:213-214` | `category_for_domain()` import/call fails | `except Exception: coarse = None` — falls back to domain keyword detection silently | 3 | `emit_data("tools", "web_fetch_category_resolution_failed", status="degraded", ok=False, error=str(exc), domain=...)` | P2 |
| `schema/tools/web_fetch.py:141-142` | `load_web_sources_config()` fails | `log.warning()` only — no emit; curated sources unavailable | 1 | `emit_data("tools", "web_sources_config_load_failed", status="degraded", ok=False, error=str(exc))` | P2 |

## omega_loop / deepsleep_loop

| file:line | failure path (what malfunctions) | current behavior | cat | recommended emit | priority |
|---|---|---|---|---|---|
| `deepsleep_loop/consolidate/run.py:60-61` | knowledge artifact consumption fails | `except Exception: pass` — artifact may be missing from journal without signal | 1 | `emit_data("deepsleep", "consolidate", status="degraded", reason="knowledge_artifact_failed", ok=False, error=str(exc))` | P2 |

## memory / runtime

| file:line | failure path (what malfunctions) | current behavior | cat | recommended emit | priority |
|---|---|---|---|---|---|
| `memory/encoding.py:193-195` | `semantic_state_engine` import fails in `encode_committed_memory()` | returns `_fail()` with `ok=False`, but no proactive emit | 2 | Emit inside `_fail()` helper: `emit_data("memory", "encoding_step_failed", ok=False, step="import", error=...)` | P1 |
| `memory/encoding.py:226-228` | `embed_persist` (re-write with embeddings) fails | returns `_fail()`, but no emit | 2 | Emit inside `_fail()` helper: `emit_data("memory", "encoding_embed_persist_failed", ok=False, error=...)` | P1 |
| `memory/gates/base.py:151-152,164-165` | signal detectors (`is_knowledge_signal`, `is_self_referential`) raise | bare `except: pass` — fallback to base classifier without emit | 1 | `emit_data("memory", "classify_override_failed", ok=False, override_type=..., error=str(exc))` | P2 |
| `memory/gates/base.py:194` | `_enrich()` decomposer fails | logs warning but no emit; write proceeds without enrichment | 1 | `emit_data("memory", "enrichment_failed", ok=False, lobe=..., error=str(exc))` | P2 |
| `memory/gates/base.py:231` | `_attach_embedding()` embed fails | logs `debug` only — write proceeds without vector | 1 | `emit_data("memory", "gate_embed_failed", ok=False, memory_id=..., error=str(exc))` | P2 |
| `memory/gates/base.py:293` | `StrengthIndex.register()` fails | logs `debug` only — memory written but NOT tracked in strength index | 1 | `emit_data("memory", "strength_register_failed", ok=False, memory_id=..., error=str(exc))` | P2 |
| `memory/gates/base.py:470-471` | `_ingest_impl()` intermediate failures (SI, enrichment) | emit only fires if `path is None`; partial failures silent | 2 | Emit on EVERY failure path, not just final `None` | P2 |
| `memory/gates/base.py:521-523` | `recall()` walker error | emits `recall_error` to `data_logger` only — NOT to `terminal_feedback` / unified log | 1 | `emit_data("memory", "walker_recall_failed", ok=False, error=str(exc))` in addition to `data_logger` | P2 |
| `memory/gates/base.py:568-569` | `recall_recent()` index load error | emits `recall_error` to `data_logger` only — NOT to unified log | 1 | `emit_data("memory", "index_load_failed", ok=False, error=str(exc))` | P2 |
| `memory/api.py:283-285` | `read_memory()` recall plan compilation fails | logs warning, sets `plan=None`, continues silently | 1 | `emit_data("memory", "recall_plan_compilation_failed", ok=False, error=str(exc))` | P2 |
| `memory/api.py:583-621` | `read_memory()` lane arbitration exception | logs warning, falls back to walker-only — no `status="degraded"` emit | 3 | `emit_data("memory", "lane_arbitration_degraded", status="degraded", ok=False, error=str(exc))` | P2 |
| `memory/api.py:692-695` | `read_memory()` walker arbitration shim fails | logs warning, sets `arbitration=None` — no emit | 3 | `emit_data("memory", "walker_arbitration_failed", status="degraded", ok=False, error=str(exc))` | P2 |
| `memory/strength_index.py:172-173` | `save()` OSError on write | logs `error` but no `emit_data` | 1 | `emit_data("memory", "strength_index_save_failed", ok=False, error=str(exc))` | P2 |
| `memory/consolidation.py:95-97` | `run_consolidation_once()` StrengthIndex flush fails | returns `"degraded"` status but NO emit for the exception | 3 | `emit_data("memory", "consolidation_strength_flush_failed", status="degraded", ok=False, error=str(exc))` | P3 |
| `memory/knowledge_router.py:100+` | `_load_index()` JSON decode / OSError | returns `{}` silently — Z-routing silently degrades | 2 | `emit_data("memory", "knowledge_z_index_load_failed", ok=False, error=str(exc))` | P3 |
| `memory/api.py:403-409` | `read_memory()` history attach per-candidate fails | `log.warning` only — skips history for that record silently | 1 | `emit_data("memory", "history_attach_failed", ok=False, memory_id=..., error=str(exc))` | P3 |
| `memory/api.py:427-439` | `read_memory()` history attach import/outer failure | `log.error` only — disables revision history without emit | 1 | `emit_data("memory", "history_attach_disabled", ok=False, error=str(exc))` | P3 |
| `memory/promotion.py:181-184` | `_emit_audit()` audit write fails | logs `debug` only — no unified emit | 1 | `emit_data("memory", "audit_write_failed", ok=False, error=str(exc))` | P3 |
| `runtime/model_router.py:172-184` | `_emit_audit()` audit write fails | logs `debug` only — no unified emit | 1 | `emit_data("runtime", "model_router_audit_write_failed", ok=False, error=str(exc))` | P3 |
| `runtime/face/anchor_store.py:69-71` | `_ensure_loaded()` file read failure | logs warning, returns `{}` silently | 2 | `emit_data("runtime", "anchor_store_load_failed", ok=False, error=str(exc))` | P3 |

## knowledge_extractor / sensory / autonomic / guardian / hud

| file:line | failure path (what malfunctions) | current behavior | cat | recommended emit | priority |
|---|---|---|---|---|---|
| `hud/telemetry.py:597-606` | charter/ledger probe (combined `_probe_charter`) fails | `except`: returns honest degraded dict AND emits | 2 | `emit_data("guardian", "charter_probe_failed", status="degraded", ok=False, error=str(exc))` ✅ DONE | P1 |
| `autonomic/cognitive_churn.py:648-649` | content fingerprints read fails | `except Exception: pass` — no telemetry attempted | 1 | `emit_data("autonomic", "churn_fingerprints", ok=False, error=str(exc)[:120])` | P3 |
| `sensory/operator_presence.py:468-470` | broker exception in presence detector | `except Exception: pass` — presence gate fails closed but no emit | 1 | `emit_data("sensory", "presence_broker_failed", status="degraded", ok=False, error=str(exc))` | P2 |
| `hud/telemetry.py:368` | journal clean check returns `True` on exception | silent false positive — reports clean when state unknown | 2 | `emit_data("hud", "journal_check", ok=False, error=str(exc)[:120])` | P2 |
| `hud/telemetry.py:863` | shared state read returns `None` on exception | silent degradation — runtime state unavailable | 2 | `emit_data("hud", "shared_state_read_failed", ok=False, error=str(exc))` | P2 |
| `hud/telemetry.py:882` | shared state key read returns `None` on exception | silent degradation — specific runtime state unknown | 2 | `emit_data("hud", "shared_state_key_read_failed", ok=False, error=str(exc))` | P2 |
| `hud/server.py:185` | URL path resolution returns empty string on exception | image URLs unresolvable, images fail to load silently | 2 | `emit_data("hud", "image_url_resolution_failed", ok=False, error=str(exc))` | P3 |
| `hud/server.py:2663` | strength index read returns `None` on exception | memory strength unknown, UI dims without explanation | 2 | `emit_data("hud", "strength_index_read_failed", ok=False, error=str(exc))` | P3 |
| `hud/server.py:2053` | vision upload base64 encode fails | face enrolled without stored thumbnail; now emits degraded | 1 | `emit_data("hud", "vision_upload_b64_failed", status="degraded", ok=False, error=str(exc)[:120])` ✅ DONE | P3 |
| `hud/server.py:3153` | `enrolled_operator_name()` returns `None` on exception | operator enrollment unknown silently | 2 | `emit_data("hud", "enrolled_operator_name_failed", ok=False, error=str(exc))` | P3 |
| `hud/server.py:3168` | heartbeat read returns empty string on exception | watcher heartbeat unknown — stale detection fails | 2 | `emit_data("hud", "heartbeat_read_failed", ok=False, error=str(exc))` | P3 |
| `hud/server.py:3183-3191` | `presence_is_stale()` raises in `/api/presence/state` | staleness check fails — operator never sees watcher-dead signal | 2 | `emit_data("hud", "presence_staleness_failed", ok=False, error=str(exc))` | P3 |
| `hud/telemetry.py:248` | presence read returns `"UNKNOWN"` on exception | presence state unknown silently | 2 | `emit_data("hud", "presence_read_failed", ok=False, error=str(exc))` | P3 |
| `hud/telemetry.py:294` | operator name read returns empty on exception | operator identity unknown silently | 2 | `emit_data("hud", "operator_name_read_failed", ok=False, error=str(exc))` | P3 |

---

## INTENTIONAL-SILENT (do NOT instrument)

These paths are intentionally silent by design. Instrumenting them would recurse, self-spam, or flag normal-empty conditions.

| file:line | what | reason |
|---|---|---|
| `guardian/telemetry/writer.py:130,169` | `_append_line()`, `write_telemetry()` try/except | telemetry plumbing — failure-soft by design; recursion risk |
| `guardian/terminal_feedback.py:147-148,179,218,267,290,311` | `_publish_event()`, `emit_phase()`, `emit_inference()`, `emit_data()`, `emit_memory()` try/except bodies | observability plumbing — self-spam risk if emit fails |
| `guardian/data_logger.py:88,97,115` | logging channel mirror / event write / telemetry mirror try/except | data_logger plumbing — failure-soft by design |
| `cognition_chain.py:61-62,77-78` | `set_episode_id()` cleanup try/except | telemetry state cleanup — non-critical |
| All `except Exception: pass` around `emit_*` calls in loops | e.g. `global_loop/loop.py:66-67`, `meta_loop/loop.py:76-77`, `auto_loop/loop.py:165-166`, `omega_loop/*/run.py` telemetry blocks, `deepsleep_loop/loop.py` telemetry blocks, `autonomic/*` telemetry blocks, `sensory/voice_io.py` telemetry blocks, `guardian/decision.py` telemetry blocks, `guardian/hmac_ledger.py` telemetry blocks, `guardian/write_guard.py` telemetry blocks | defensive emit wrappers — the operator directive says telemetry surface never halts cognition; a failed emit should not cascade |
| `pressure_loop/pressure.py:493-495` | `evaluate()` returns `None` when episode already active | benign expected branch — "episode_active" is normal, not malfunction |
| `pressure_loop/open_questions.py:328-337` | `attach_user_answer()` returns `None` when no open questions | benign expected branch — absence is normal |
| `pressure_loop/reflection_window.py:103-128` | `load_current_trajectory()` returns `{}` when file not found | benign expected branch — first boot has no trajectory yet |
| `runtime/client.py:232-247` | `_post()` returns `None` on connection/timeout/request error | spec-compliant failure contract — callers check `None` and emit |
| `memory/api.py:96-110` | `write_memory()` returns `None` on non-dict input | validation guard — malformed input, not operational failure |

---

## Summary

**Total actionable gaps mapped: 55 rows across 8 systems.**

- **By category:** 22 swallowed exceptions (cat 1), 19 failure returns (cat 2), 9 degraded fallbacks (cat 3), 1 silent crash cause (cat 4), 4 tool failures (cat 5).
- **By priority:** 2 P0 (continuity-critical: episode accumulator reset, ignition validation), 14 P1 (runtime/model/tool/core-loop), 23 P2 (memory/knowledge/sensory), 16 P3 (hud/peripheral).
- **Highest-impact gaps:** `episode.py:385-389` (accumulator reset swallowed, P0) and `global_loop/ignition/run.py:10-15` (validation raises outside `phase_span`, P0) — both can crash or corrupt the cognitive cascade without leaving a record in the unified failure log.
- **Pattern:** The codebase correctly wraps most real operations in `phase_span` or emits `ok=False` on failure, but **three systematic gaps** recur: (a) `except Exception: pass` around real I/O/state mutations inside helper functions, (b) `log.debug`/`log.warning` without a matching `emit_data`, and (c) return-path fallbacks (`None`, `{}`, `False`) that degrade silently rather than emitting `status="degraded"`.

**Next lane:** Cursor implements from this map; Claude verifies independently.
