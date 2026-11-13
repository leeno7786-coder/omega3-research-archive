# Trajectory Decision Instrumentation Map (Phase 1 — Devin)

**Scope anchor:** commit `bf174405` (trajectory decision model)  
**Date:** 2026-06-12  
**Authority:** [`AGENTS.md`](../../AGENTS.md) + live code
**System state:** day-zero reset (pre-boot)  

---

## The three conventions (exact sinks)

| Surface | Function | Failure fork |
|---|---|---|
| Telemetry + failure log | `guardian.telemetry.writer.write_telemetry(system=, event=, phase="", status="", summary="", episode_id=None, **fields)` | `_is_failure(record)` -> `failures.jsonl` when `status in {fail, error, degraded, crash, timeout, declined}` OR `ok is False` (`failure_class in {"bad_request"}` and pre-boot substrate classes are exempt) — `writer.py:43-113` |
| HUD terminal feedback | `guardian.terminal_feedback.emit_data(system, phase, *, status="ok", summary="", episode_id=None, ok=None, **fields)` (`ok=False` auto-promotes status->"fail"), `emit_phase(loop, phase, *, status, summary, **fields)` | n/a (display surface) |
| Phase spans | `guardian.terminal_feedback.phase_span(loop, phase, **fields)` context manager — emits the phase fail on exception before propagation | VERIFY per-site that span-fail actually reaches `write_telemetry`/`failures.jsonl` |

---

## Surface 1 — `omega_loop/package/run.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 1a | `package/run.py:55` | `read_trajectory()` — load current setpoint. Fail-hard on corrupt store. | `temporal_store.py:146-158` emits `emit_data("pressure","read_trajectory",status="fail",reason="corrupt_record",ok=False)`; `loop.py:83` `phase_span("omega","package")` catches the re-raised `RuntimeError` and emits `status="fail"`. | None. Both the specific emit and the span reach `failures.jsonl` (`test_failure_log_batch1.py:154-170` proves this). Span summary = `"corrupt trajectory record in lobe 9: ..."` — usable. | — | Verified covered |
| 1b | `package/run.py:61-63` | Decision normalization: no current trajectory forces `decision="establish"` regardless of model output. | `package/run.py:119-134` `emit_data("omega","omega_outcome_pack",trajectory_decision=decision)` captures the **final** decision only. | Invisible that coercion occurred (model may have said "idle" or "abandon", but system overrode). | P1 | `emit_data("omega","trajectory_decision_normalized", episode_id=window_id, status="ok", summary="no current setpoint; forced establish", original_decision=trajectory_update.decision, normalized_decision=decision)` immediately after line 63 |
| 1c | `package/run.py:88-91` | `establish` branch: `write_trajectory(_new_trajectory().to_dict())`. | Same as above — only the summary omega_outcome_pack emit exists. | No per-write observation. WHICH branch executed is invisible. | P1 | `emit_data("omega","trajectory_write", episode_id=window_id, status="ok", action="establish", trajectory_id=new_trajectory.trajectory_id)` immediately after line 91 |
| 1d | `package/run.py:92-104` | `abandon` (or mislabeled-establish-with-current) branch: `supersede_trajectory(current,new)` then two writes — `record_type="trajectory_superseded"` + active `"trajectory"`. | Same — only summary emit. | **Zero observation** for the superseded history write. The old->new chain is invisible. | P0 | `emit_data("omega","trajectory_write", episode_id=window_id, status="ok", action="supersede", record_type="trajectory_superseded", old_trajectory_id=current.trajectory_id, new_trajectory_id=new.trajectory_id)` immediately after line 103, AND `emit_data("omega","trajectory_write", episode_id=window_id, status="ok", action="establish", trajectory_id=new.trajectory_id)` immediately after line 104 |
| 1e | `package/run.py:105-115` | `alter` branch: `modify_trajectory(...)` then `write_trajectory(altered.to_dict())`. | Same — only summary emit. | No per-write observation. | P1 | `emit_data("omega","trajectory_write", episode_id=window_id, status="ok", action="alter", trajectory_id=altered.trajectory_id)` immediately after line 115 |
| 1f | `package/run.py:116-118` | `idle` branch: `write_trajectory(current.to_dict())` — re-persist unchanged. | Same — only summary emit. | No per-write observation. | P1 | `emit_data("omega","trajectory_write", episode_id=window_id, status="ok", action="idle", trajectory_id=current.trajectory_id)` immediately after line 118 |
| 1g | `package/run.py:119-134` | Soft `emit_data("omega","omega_outcome_pack",...)` AFTER all writes. | This IS the summary observation surface. | None — it carries `trajectory_decision`, `trajectory_diff`, `trajectory_update`, `coherence`. It is failure-soft (wrapped in `try/except`). | — | Already adequate |

**Notes:**
- `write_trajectory()` itself can raise (disk / `write_memory` failure). If it does, the exception propagates out of `package_window`, out of `phase_span("omega","package")` (`loop.py:83`), which emits `status="fail"` and reaches `failures.jsonl`. No additional emitter needed for substrate I/O failure — the span covers it.
- The four branch emits (1c–1f) must be **failure-soft** (`try/except Exception: pass`) and must NOT change control flow.

---

## Surface 2 — `omega_loop/intake/run.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 2a | `intake/run.py:62` | `read_trajectory()` — live read for diffing. Fail-hard on corrupt store. | `temporal_store.py:146-158` emit_data on corrupt; `loop.py:52` `phase_span("omega","intake")` catches exception and emits `status="fail"`. | None. Reaches `failures.jsonl` with usable `reason="corrupt_record"` plus span context. | — | Verified covered |
| 2b | `intake/run.py:62-64` | Live-vs-snapshot divergence: `live_trajectory` non-empty and replaces frozen `trajectory_snapshot`. | None. | Silent when a crashed window is drained after a later trajectory write. An important system event with no visibility. | P1 | `emit_data("omega","trajectory_divergence", episode_id=window_id, status="ok", summary="live trajectory replaced frozen snapshot", has_snapshot=bool(getattr(window,"trajectory_snapshot",None)), live_empty=not bool(live_trajectory))` immediately after line 64 |

**Notes:**
- The `window_id` local is available at `intake/run.py:51`.

---

## Surface 3 — `omega_loop/reflect/run.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 3a | `reflect/run.py:84-87` | `raw_text is None` -> raise `RuntimeError("omega reflect: model produced no output...")`. | `loop.py:66-69` `phase_span("omega","reflect")` catches it and emits `status="fail"`, summary = exception text. | None. Span-fail reaches `failures.jsonl` (`status="fail"` in frozenset). Summary is usable. | — | Verified covered |
| 3b | `reflect/run.py:89-91` | `payload is None` -> raise `ReflectionParseError("omega reflect: model output not parseable as JSON")`. | `loop.py:80-81` `phase_span("omega","reflect")` catches it (exhausted-retries path) and emits `status="fail"`. | None. Span-fail reaches `failures.jsonl`. Summary is usable. | — | Verified covered |
| 3c | `reflect/run.py:97-101` | `trajectory_update_from_reflection(reflection,...)` — coercion of omitted/garbage decision to `"establish"` (no baseline) or `"idle"` (baseline present). | `reflect/run.py:104-116` `emit_data("omega","trajectory_update",...)` emits the diff + window_id, but **does not capture the coercion**. | Invisible when model mislabels/omits the decision. Cannot tell whether the model said "idle", "panic", or nothing. | P0 | Add to the existing `emit_data` block at lines 104-116: include `decision_original=raw_decision or "(omitted)"` and `decision_coerced=(decision != raw_decision)` fields, OR insert a separate `emit_data("omega","trajectory_decision_coerced", episode_id=omega_input.window_id, status="ok", summary=f"decision coerced from {raw_decision!r} to {decision!r}", original_decision=raw_decision or "(omitted)", coerced_decision=decision, has_current_trajectory=bool(omega_input.trajectory_snapshot))` immediately after line 101 |

**Notes:**
- `trajectory_update_from_reflection` is a pure function in `_omega_state.py:333-363`; the emit belongs at the reflect caller (as recommended above).
- `OmegaReflection.from_dict` (`_omega_state.py:76-87`) is maximally tolerant — no raise path — so no span-fail risk there.

---

## Surface 4 — `omega_loop/_omega_state.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 4a | `_omega_state.py:103-110` | `TrajectoryUpdate.__post_init__` — raises `ValueError` if `decision` not in `TRAJECTORY_DECISION_VALUES`. | Caller `phase_span` (`loop.py:66-81` for reflect, `loop.py:83` for package) catches `ValueError` and emits `status="fail"`. | None. Span-fail reaches `failures.jsonl`. Summary is the validation message — usable. | — | Verified covered |
| 4b | `_omega_state.py:333-363` | `trajectory_update_from_reflection(...)` — coercion logic. Pure function. | Caller `reflect/run.py:97-101` has the existing `emit_data("omega","trajectory_update")`, but does NOT emit the coercion. | Invisible coercion. Emit belongs at caller (see Surface 3c). | P0 | See Surface 3c recommendation |
| 4c | `_omega_state.py:176-244` | `compute_trajectory_diff(...)` — deterministic diff. `ESTABLISHING` when no baseline or empty window. | `package/run.py:119-134` captures the final `trajectory_diff`. | No dedicated observation of the diff computation itself. Arguably covered by the summary emit. | P3 | Judgment: low value. If added: `emit_data("omega","trajectory_diff_computed", episode_id=window_id, status="ok", trajectory_diff=diff, baseline_present=bool(trajectory_snapshot))` inside `build_omega_input_for_window` after line 70 |

---

## Surface 5 — `pressure_loop/temporal_store.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 5a | `temporal_store.py:145-161` | `read_trajectory()` corrupt JSON path: catches `JSONDecodeError`, emits `emit_data("pressure","read_trajectory",status="fail",reason="corrupt_record",ok=False)`, then re-raises `RuntimeError`. | The emit_data fires; plus caller `phase_span` catches the `RuntimeError`. | **Verified** — both records reach `failures.jsonl`: the specific emit has `reason="corrupt_record"`; the span has `error_type="RuntimeError"` and summary. Usable. | — | Verified covered |
| 5b | `temporal_store.py:182-213` | `write_trajectory(..., record_type="trajectory_superseded")` — history write for abandoned trajectories. | None. | **Zero observation.** The superseded record is written silently. | P0 | Caller recommendation in Surface 1d. Alternatively, add inside `write_trajectory` before `write_memory`: `emit_data("pressure","trajectory_persist", status="ok", record_type=record_type, cycle_id=env.cycle_id)` — but callers own decision observation, so prefer Surface 1d. |
| 5c | `temporal_store.py:182-213` | `write_trajectory(..., record_type="trajectory")` — active setpoint write. | None per-write; `package/run.py:119-134` covers the decision outcome. | Per-write success invisible. | P1 | Caller recommendation in Surface 1c–1f |

---

## Surface 6 — `deepsleep_loop/reseed/run.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 6a | `reseed/run.py:107-117` | Steering rule branch: explicit decision (`establish`/`alter`/`abandon`) vs `idle`-reinforce vs legacy-continuation fallback. | `reseed/run.py:228-241` `_emit_reseed("generate_reseed",...)` captures `trajectory_decision` and `trajectory_gap=steer`, but not **which rule fired**. | Cannot distinguish "steer because model said alter" from "steer because idle+DRIFTING" from "steer because legacy flag". | P1 | `_emit_reseed("trajectory_steer_rule", status="ok", rule=("explicit_decision" if decision in {"establish","alter","abandon"} else ("idle_reinforce" if decision=="idle" else "legacy_continuation")), trajectory_decision=decision, trajectory_diff=trajectory_diff)` immediately before line 118 |
| 6b | `reseed/run.py:119-127` | Weight-floor application: clamp to `[0,1]`, then force `0.5` if `<=0.001`. | None. | Invisible that a zero-weight was floored to 0.5. | P2 | `_emit_reseed("trajectory_weight_floor", status="ok", original_weight=weight_raw, floored_weight=0.5)` immediately inside the `if weight <= 0.001:` block (line 124) |
| 6c | `reseed/run.py:131-133` | Snapshot-fallback-for-targets: when `decision=="idle"` and model omitted targets, uses `ds_input.trajectory_snapshot` targets. | None. | Invisible that the SET trajectory was used because model omitted targets. | P2 | Add metadata to the `trajectory_gap` contributor in lines 134-155: `fallback_to_snapshot=(decision=="idle" and not trajectory_update.get("target_domain"))`, OR `_emit_reseed("trajectory_snapshot_fallback", status="ok", target_domain=..., target_capability=...)` immediately after line 133 |

**Notes:**
- All `_emit_reseed` calls are already failure-soft (`_emit_reseed` wraps `emit_data` in `try/except`). Any additional emitters inside `generate_reseed` should follow the same pattern.

---

## Surface 7 — `omega_loop/loop.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 7a | `loop.py:52-53` | `phase_span("omega","intake")` wrapping `build_omega_input_for_window()`. | Span fail on exception -> `emit_phase(status="fail")` -> `_route_telemetry` -> `write_telemetry(status="fail")` -> `_is_failure` -> `failures.jsonl`. | None. Verified by `test_phase_span_fail_on_exception_reraises` and `test_read_trajectory_corrupt_json_is_logged_before_crash`. On corrupt store, span summary = `"corrupt trajectory record in lobe 9: ..."` — usable. | — | Verified covered |
| 7b | `loop.py:59-64` | `phase_span("omega","reason")` wrapping `reason_phase.reason()`. | Same span-fail path. | None. | — | Verified covered |
| 7c | `loop.py:66-81` | `phase_span("omega","reflect")` wrapping `reflect_phase.reflect()`. | Same span-fail path. Covers `RuntimeError` (no output), `ReflectionParseError`, and `ValueError` (`TrajectoryUpdate` validation). | None. On `ReflectionParseError`, summary = `"omega reflect: model output not parseable as JSON"` — usable. On `TrajectoryUpdate` `ValueError`, summary = validation message — usable. | — | Verified covered |
| 7d | `loop.py:83-100` | `phase_span("omega","package")` wrapping `package_phase.package_window()`. | Same span-fail path. Covers corrupt store read, `write_trajectory` failure, and `TrajectoryUpdate` `ValueError`. | None. On corrupt store, same exception text as 7a. On `ValueError`, summary = `"TrajectoryUpdate.decision must be one of ..."` — usable. | — | Verified covered |

**Notes:**
- All four `phase_span` wrappers emit `status="fail"` on exception, which is in `_FAILURE_STATUSES`, so `_is_failure` returns `True` and the record forks to `failures.jsonl`.
- No trajectory-related exception can escape `run_omega_for_window` without a `failures.jsonl` entry.

---

## Surface 8 — `guardian/terminal_feedback.py` `_omega_epoch_from_event`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter |
|---|-----------|-------------|--------------------|-----|---|---------------------------|
| 8a | `terminal_feedback.py:165-213` | `_omega_epoch_from_event(event)` — projects `kind="data"` events into the HUD epoch ring. | `terminal_feedback.py:216-228` `_mirror_hud_live_feed` routes matching events to `RECENT_OMEGA_EPOCHS`. | None. End-to-end flow verified:  
 1. `package/run.py:119-134` `emit_data("omega","omega_outcome_pack", trajectory_decision=decision, ...)`  
 2. -> `_publish_event` (`terminal_feedback.py:320-337`)  
 3. -> `_mirror_hud_live_feed` (`terminal_feedback.py:216-228`)  
 4. -> `_omega_epoch_from_event` matches `loop="omega"`, `phase="omega_outcome_pack"` (line 170)  
 5. -> extracts `trajectory_decision` from `event.get("trajectory_decision")` (line 208) and `trajectory_diff` (line 207)  
 6. -> `_append_shared_state_ring(RECENT_OMEGA_EPOCHS, omega_record, 20)` (line 226). | — | Verified covered |

**Notes:**
- The reflect intermediate emit (`reflect/run.py:104-116`, phase=`"trajectory_update"`) does **not** match `_omega_epoch_from_event` (filters on `"omega_outcome_pack"`), which is correct — only the final pack should drive the epoch ring.
- Fallback to `update.get("decision")` at line 208-210 ensures the ring always has a decision value even if the top-level event field is missing.

---

## Surface 9 — `schema/omega_trajectory.py`

| # | file:line | What happens | Already covered by | Gap | P | Exact recommended emitter / judgment |
|---|-----------|-------------|--------------------|-----|---|--------------------------------------|
| 9a | `schema/omega_trajectory.py:209-269` | `make_trajectory(...)` — pure factory. | None. | Judgment: **NO instrumentation here.** Callers (`package/run.py:78-86`) own observation of what they create. | — | Callers own observation |
| 9b | `schema/omega_trajectory.py:272-283` | `modify_trajectory(old, **fields)` — pure `alter` helper. | None. | Judgment: **NO instrumentation here.** Callers (`package/run.py:107-115`) own observation of the decision and the write. | — | Callers own observation |
| 9c | `schema/omega_trajectory.py:286-301` | `supersede_trajectory(old, new)` — pure `abandon` helper. | None. | Judgment: **NO instrumentation here.** Callers (`package/run.py:97-104`) own observation of the supersede chain. | — | Callers own observation |
| 9d | `schema/omega_trajectory.py:304-318` | `complete_trajectory(...)` — pure completion helper. | None. | Not in current decision path. | — | N/A |
| 9e | `schema/omega_trajectory.py:321-334` | `decrement_trajectory(...)` — pure cycle-decrement helper. | None. | Not in current decision path. | — | N/A |

---

## Sweep: exception escapes without `failures.jsonl`

**Finding:** No trajectory-decision-path exception can escape to the boot loop without a `failures.jsonl` record.

- Every trajectory-related I/O or validation call inside `run_omega_for_window` is wrapped by one of the four `phase_span` wrappers (`intake`, `reason`, `reflect`, `package`).
- `phase_span` on exception emits `status="fail"` (`terminal_feedback.py:527-536`), which routes through `_route_telemetry` -> `write_telemetry` -> `_is_failure` (status in frozenset) -> `failures.jsonl`.
- `read_trajectory()` adds its OWN `emit_data("pressure","read_trajectory",status="fail",ok=False)` (`temporal_store.py:146-158`), which also forks to `failures.jsonl` via `_is_failure`.
- Internal soft-failure helpers (`_current_mastery_state`, `_open_questions_summary`, `emit_data` wrappers) catch their own exceptions and do not propagate.
- The only unwrapped code in `run_omega_for_window` is `_window_id` (string getattr), loop setup, and episode temporal extraction — none can raise trajectory-related exceptions.

---

## Sweep: status-vocabulary mistakes

**Finding:** No incorrect status strings found in the trajectory decision path.

All emitters in the mapped surfaces use exact strings from the frozenset:
- `"ok"`, `"fail"`, `"degraded"`, `"skip"`, `"enter"` — all valid.
- No `"failed"`, `"failure"`, `"timeout"`, or `"declined"` were found in trajectory-path emitter calls.

---

## Gaps summary (P0–P3)

| Priority | Count | Surfaces | Build directive for Cursor (Phase 2) |
|---|---|---|---|
| **P0** | 3 | 1d (supersede zero observation), 3c/4b (coercion invisible), 5b (superseded write) | **Mandatory.** Must land before first boot. |
| **P1** | 6 | 1b (normalization), 1c/1e/1f (per-write establish/alter/idle), 2b (divergence), 6a (per-rule steer) | **Mandatory.** Builds operator trust in the first window. |
| **P2** | 2 | 6b (weight floor), 6c (snapshot fallback) | Mechanical if time; skip if trivial. |
| **P3** | 1 | 4c (diff-computation observation) | Skip unless trivial. |

**Total gaps to close:** 12 recommendations (9 P0+P1 mandatory, 2 P2 optional, 1 P3 skip).

**Iron rules reminder for Phase 2:**
- Emitters are **failure-soft** (`try/except Exception: pass`).
- Emitters must **NOT** change control flow — cognition stays fail-hard, substrate stays failure-soft.
- No new emitter frameworks. Only the three sinks above.
- RED-first: tests assert the value AND the fail-mode, both directions.

---

*Map generated by Devin (Phase 1 — read-only audit). Filed against work-order `#3b604968`.*
