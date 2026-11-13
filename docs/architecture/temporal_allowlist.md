# Temporal Allowlist — Phase 2 §P2-S20

Direct `datetime.now()`, `datetime.utcnow()`, `time.time()`, and
`time.monotonic()` calls are FORBIDDEN on product cognitive paths
(per [`slices/PACKET_P2_S20.md`](slices/PACKET_P2_S20.md) RB#2 + RB#3).
Cognitive surfaces must call through
[`core.symphony_time`](../../core/symphony_time.py) — the single
temporal authority — so fake-time tests can pin every surface to one
reference frame.

This file enumerates the **named categories** of legitimate direct-
time calls that remain in the codebase. Every direct time call must
fit one of these categories, and the audit at
`tests/regression/test_symphony_p2_s20_temporal_authority.py` §3
checks that this allowlist file lists each category by name.

## Categories

### 1. Local timeout / duration measurement

`time.monotonic()` and `time.time()` used inside a single function
to measure a bounded local elapsed interval (HTTP request latency,
sandbox execution timeout, queue wait time, animation frame deltas).
These are LOCAL DURATION mechanics — they do not write a timestamp
into a cognitive payload that another surface will read back. The
reference frame is INTERNAL to the function, so fake-time injection
at the authority does not need to reach them.

Examples (non-exhaustive):
- `core/response_engine.py` — per-act + per-curiosity cooldown
  windows. **Migrated** to authority for fake-time consistency
  because these windows interact with episode timestamps.
- `brain/runtime_client.py` — HTTP retry backoff timing.
- `autonomous/autonomous_loop/metrics.py` — phase latency
  histograms.
- `core/model_lifecycle.py` — model load/unload duration logging.
- `core/model_router.py` — per-request scheduling deltas.
- `memory/consolidation.py` — DeepSleep consolidation step
  duration accounting (not the consolidation timestamp itself,
  which is on the authority).

### 2. Third-party adapter boundary

Calls inside an adapter that wraps a non-Symphony library and
needs to satisfy that library's time contract (e.g., a logger
sink that requires a `time.time()` epoch float, a third-party
queue that records its own enqueue timestamps).

Examples (non-exhaustive):
- `core/feedback.py` — terminal feedback emission timestamps
  consumed by the HUD subprocess (HUD owns its own time
  reference).
- `core/data_logger.py` — append-only debug log stream timestamps.
- `core/provenance.py` — schema provenance stamps that must use
  real wall-clock for audit traceability.

### 3. Test fixture

`datetime.now()` / `time.time()` calls inside `tests/` are
allowed without an entry. The audit only scans `core/`, `brain/`,
`autonomous/`, `metacognition/`, and `memory/` — not `tests/`.

### 4. Legacy compatibility pending removal

Direct time calls on surfaces that are marked for removal in a
Kimi removal packet but cannot be migrated mechanically without
breaking a legacy API contract. These must be referenced from a
removal-packet row (e.g., `REMOVE_P2_S7_LEGACY_TIMER_CHAIN`) so
the eventual removal closes them.

Examples (non-exhaustive):
- `core/continuous_timing.py` — legacy stage-timer vocabulary
  (per the file's own header note, retained as compatibility
  metadata until every caller migrates to
  `cue_desk_tick_summary`).

## Migrated cognitive surfaces (RB#2)

These surfaces were migrated to `core.symphony_time` during P2-S20.
The migrated set expanded in the post-Gemini-round-1 cure to cover
substrate-signal sites whose timestamps feed cognitive decisions
(pressure, curiosity, autonomic, operator pressure, conversation
state, open questions, layer state, brain receiver, autonomous
intake):

| File | Helper | Reads |
| --- | --- | --- |
| `core/cue_desk.py` | `_iso()` | `now_iso()` |
| `core/episode_store.py` | `_now_iso()` | `now_iso()` |
| `core/symphony_runner.py` | `_now_iso_for_episode()` | `now_iso()` |
| `core/episode_wal.py` | `_now_iso()` | `now_iso()` |
| `core/symphony_learning_state.py` | `_now_iso()` + `_seconds_since_iso()` | `now_iso()` |
| `core/episode_temporal.py` | `_default_capture()` | `now_iso()` + `now_wall()` |
| `core/response_engine.py` | `_can_dispatch()` + `_record_dispatch()` + `_is_stuck_active()` | `now_wall()` + `now_iso()` |
| `core/deepsleep_engine.py` | DeepSleep consolidation timestamp | `now_iso()` |
| `core/pressure_field.py` | `_now_iso_evidence()` + `read_operator_message` TTL | `now_iso()` |
| `core/curiosity_engine.py` | `_build_entry()` + `_no_signal_entry()` created_at | `now_iso()` |
| `core/autonomic_modulator.py` | `_now_iso()` + `_now_epoch()` | `now_iso()` + `now_wall()` |
| `core/operator_pressure.py` | `_now_iso()` + directive TTL now-anchor | `now_iso()` |
| `core/outstanding_question_state.py` | `_now_iso()` + `_age_seconds()` | `now_iso()` |
| `core/open_questions.py` | `_now_iso()` | `now_iso()` |
| `core/brain_receiver.py` | `_now_iso()` | `now_iso()` |
| `core/conversation_state.py` | `_now_iso()` + `_is_expired()` now-default | `now_iso()` |
| `core/curiosity_store.py` | `_now_iso()` | `now_iso()` |
| `core/curiosity_producer.py` | `_now_iso()` | `now_iso()` |
| `core/followup_spawner.py` | `_now_iso()` | `now_iso()` |
| `core/layer_state.py` | `_now_iso()` | `now_iso()` |
| `core/autonomic.py` | snapshot captured_at | `now_iso()` |
| `core/autonomous_engine.py` | autonomous BrainIntake timestamp | `now_iso()` |

Under `set_fake_time(iso='X', wall=W)`, every surface above records
`X` / `W` verbatim. The fake-time scenario at
`scripts/symphony_boot_cascade.py::_run_scenario_temporal_authority_convergence`
proves end-to-end pinning across episode open / pack / archive / WAL.

## Forbidden categories

- Cognitive timestamp drift — any new code that imports
  `datetime.now` / `time.time` directly on a cognitive surface
  named above must instead call through `core.symphony_time`.
- Clock-based action fabrication — wall-clock alone cannot create
  unbounded action (per RB#4). Pressure / refractory rules must
  gate every clock-derived "we should do X now" decision.
