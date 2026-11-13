# Pressure Loop

The **single pressure tick loop** of Omega 3.0. This subsystem owns ONLY pressure — gathering contributors, evaluating them, and handing off to the cognition chain when ignition occurs.

## Scope

The pressure loop is the ONE loop of the system. It:
- Runs at the configured tick cadence
- Gathers pressure contributors from the pressure field
- Evaluates contributors with neuromodulation (dopamine vigor, serotonin patience)
- Opens episodes when the dominant contributor reaches the ignition threshold
- Hands off to the cognition chain and pauses during episode execution
- Resumes ticking after the episode completes

## Current State / Legacy Findings

The current surface includes pressure gathering/evaluation, the durable
cognition queue, physical pipeline dispatch, episode storage, Omega level and
reseed state, operator/system ingress, and the reflection-window lane described
below. Some audit-only readers remain record-only by design.

### Live modules

The pressure loop is the live ignition and pressure substrate of Omega 3.0. The central trio is heavily wired into `boot.py` and the cognition cascade:

- `loop.py` — tick driver; called by `boot.py` and `core/boot_lifecycle.py`
- `pressure.py` — evaluation logic and reseed/suppress staging; called by `loop.py`, `operator_chat.py`, and `cognition_chain.py`
- `contributors.py` — canonical types; used by `global_loop/`, `omega_loop/motor/run.py`, `cognition_chain.py`, and many tests
- `pressure_field.py` — contributor reader registry; read by `pressure.py`
- `episode.py` — episode substrate; used by `runtime.py`, `cognition_chain.py`, `omega_loop/package/run.py`, and `hud/telemetry.py`
- `runtime.py` — shared runtime state; used by `boot.py`, `hud/server.py`, `hud/telemetry.py`, and `guardian/decision.py`
- `policy.py` — central policy loader; consumed by `cognition_chain.py`, `knowledge_extractor/store/run.py`, most loop packages, `core/cognition_pacing.py`, and many tests

Other active categories:

- **Operator ingress / chat / queue:** `operator_chat.py`, `cognition_queue.py`, `operator_pressure.py`, `operator_chat_responses.py`, `system_messages.py`
- **Proactive / Omega-authored feeds:** `proactive_chat_messages.py`, `proactive_lifecycle.py`, `proactive_result_renderer.py`
- **Pause / event / durable plumbing:** `pressure_loop_pause.py`, `system_event_queue.py`, `durable_write.py`
- **Reflection / journal / trajectory:** `reflection_window.py`, `episode_journal.py`, `temporal_store.py`, `open_questions.py`, `trajectory_influence_store.py`, `pressure_surface.py`
- **Omega control → pressure:** `omega_successor.py` (the one validated successor per episode), `learning_progress.py`, `signal_ledger.py`
- **Motor / build records:** `motor_loop_guard.py`, episode-SRAM `BuildObjective` / `ExecutionReport`, `emergent_pressure.py`, `learn_topic_choice.py`
- **Diagnostic-only memory seams (not gathered for ignition):** `taxonomy_pressure.py`, `recall_verdict_pressure.py`, `integrity_pressure.py`
- **Symphony / idle signals:** `idle_wake.py`, `symphony_signals.py`, `symphony_learning_state.py`, `symphony_probes.py`

### Dead / stale / inert findings

1. **Zeroed legacy pressure contributors in `pressure_field.py`**  
   The following field IDs are still registered but are explicitly removed from pressure via `pressure._REMOVED_FIELD_IDS` and always return `0.0`:
   - `neuromod_spike` — replaced by advisory tonic body state (`_NEUROMOD_TONIC`, `read_neuromod_tonic`, `refresh_neuromod_tonic`, `decay_neuromod_tonic`)
   - `guardian` — removed from pressure
   - `tool_creation_need` — removed
   - `evidence_terminal` — removed (was an ASK/HOLD terminal reader)

2. **`operator_priority_store.py` — implemented but inert**  
   `read_operator_priority` fires the `operator_priority` contributor, but `set_priority` has **no live HUD caller**. The operator-directed priority affordance exists in code but no UI path populates it.

3. **`cycle_cadence.py` — legacy compat surface**  
   `HybridScheduler.tick()` and the default signal providers are **symphony-silenced** on the live boot path (`boot.py` uses `pressure_loop.loop.run_pressure_tick_loop`). The module is still used only by `hud/telemetry.py` for scheduler caching and by `symphony_probes.py` / `pressure_field.py` via `_recent_jsonl_entries`. `persistence_threshold_provider` is a hardcoded zero-pressure stub.

4. **`signal_ledger.py` — record-only by design**  
   Step 8 writes `guardian/logs/signals.jsonl`, but nothing reads that ledger to make decisions. This is intentional (record-only audit substrate) but worth noting.

### Docs / reality discrepancies

- The README previously described `operator_priority` → `ASK_OPERATOR` as a live contributor, but the setter is not wired to the HUD.
- The README describes `cycle_cadence.py` as "pressure-modulated scheduling"; in production it is no longer the conductor and is primarily a telemetry-cached legacy surface.
- The "Modules gained from `core/` cleanup" section below lists several relocated modules; this section is accurate but the `cycle_cadence.py` summary overstates its runtime role.

## Modules

### `cognition_queue.py` — Durable Cognition Queue
One arrival-ordered durable queue converges operator, system, plugin, and
machine-minted work. It grants up to four process-local pipeline claims; a row
remains durable until its own acknowledgement boundary and survives restart.
Queue state is stored under `guardian/state/cognition_queue.json`.

### `system_messages.py` — Text App (System Queries)
The mind's outbound query channel to the operator (`pressure_loop/system_messages.py`).

- **Channel separation** — only `ASK_OPERATOR` episodes land here; `PROACTIVE_RESPONSE` never does.
- **Two-action baseline** — `respond` attaches the reply and feeds it back
  through the cognition queue as `operator_response` (→ `RESOLVE_QUERY`);
  `delete` is pressure-loss (record removed without system feedback). There is
  no ignore action.
- **Durable** — persists to `guardian/state/system_messages.json` (day-zero-wiped).
- **HUD surface** — badge count = `unread_count()`; panel renders via `GET /api/system-messages`.

### `proactive_chat_messages.py` — Autonomous Chat Updates
Proactive status narration while cognition runs autonomously (`pressure_loop/proactive_chat_messages.py`).

- **Channel separation** — only `PROACTIVE_RESPONSE` episodes land here; `ASK_OPERATOR` uses the text app; operator `RESPOND` uses `operator_chat_responses`.
- **HUD surface** — the right proactive pinboard polls `GET /api/chat/proactive` (medium clock + SSE `proactive_chat_appended`); the center feed remains operator conversation only.
- **Durable** — persists to `guardian/state/proactive_chat_messages.json` (day-zero-wiped).

### `proactive_lifecycle.py` — Ask → Policy → Dialogue Application

`ASK_OPERATOR` is policy acquisition, not an endpoint.  The append-only ledger at
`guardian/state/proactive_lifecycle.jsonl` binds the operator's exact answer to later
`LEARN`, dialogue application, `PRESENT_RESULT`, and foldback events.  A pending dialogue
application projects one ordinary `proactive_update` contributor, so Global A and Meta A
continue to emit `PROACTIVE_RESPONSE`; there is no proactive scheduler or new action.

`PRESENT_RESULT` is structured metadata inside that existing route.  Application completion
always presents.  Passing evidence may promote mastery through the normal Omega/DeepSleep
path; weak or blocked evidence is still presented, records corrective foldback, and continues
or asks according to the bound operator policy.  Presentation is recorded before foldback,
and result/foldback IDs make restart retries idempotent.

AUTO executes `PROACTIVE_RESPONSE` through the same dropdown-first chat execution limb as
operator dialogue; Omega never evaluates an empty synthetic transcript. Dispatch pressure is
below operator priority, FIFO across ready threads, and bounded to two attempts per application.
No-progress applications park in `continuing` with an operator-visible notice. A malformed
non-tail ledger is quarantined with loud telemetry, while pressure gathering and legacy
pre-ledger questions remain available instead of terminating the cognition tick or HUD request.

### `policy.py` — Policy Loaders (Rebuild Path)
Central policy loaders for pressure + cognition slice configuration (pressure_loop/policy.py).

**Purpose:** Canonical policy loaders for pressure + cognition slice configuration. Merges defaults from `config/*.yaml` with runtime overrides from `policy/symphony.policy.yaml`.

**Config sources (merged in order):**
1. `config/pressure.yaml` — pressure defaults
2. `config/global.yaml` — global defaults
3. `config/meta.yaml` — meta defaults
4. `policy/symphony.policy.yaml` — runtime overrides

**Key functions:**

**General:**
- `load_policy(policy_path)` — Loads and merges all config sources; returns dict with `pressure`, `global`, `meta` sections
- `symphony_mode_enabled()` — Checks if conductor_mode is 'symphony'
- `pressure_conductor_wired()` — Checks if pressure.conductor_wired is true
- `clear_policy_cache()` — Clears LRU caches for config reload

**Pressure configuration:**
- `pressure_tick_interval_seconds()` — Configured tick cadence
- `pressure_open_threshold()` — Ignition ceiling (default: 1.0)
- `pressure_winner_margin()` — Ignition margin (default: 0.05)
- `pressure_neuromod_hybrid_patience()` — Legacy compatibility loader; no longer moves ignition ceiling or margin
- `pressure_neuromod_weight_modulation()` — Legacy compatibility/telemetry loader; no longer mutates pressure weights

**Slice gates:**
- `slice1_global_a_front_enabled()` — Global A via global_loop (always true on ignite)
- `slice2_meta_a_front_enabled()` — Meta A via meta_loop
- `slice3_auto_front_enabled()` — AUTO via `auto_loop`
- `slice4_meta_b_global_b_front_enabled()` — Meta B + Global B via meta_loop/global_loop
- `slice5_omega_front_enabled()` — Omega via omega_loop

**Journal configuration:**
- `episode_journal_path()` — Relative path for the episode journal JSONL buffer (default: `pressure_loop/state/episode_journal.jsonl`)
- `episode_journal_window_default()` — Default N for `journal_window()` (default: 50)

**Global configuration:**
- Global B tunables load from `config/global_b.yaml` via `global_b_*` policy helpers

**Meta configuration:**
- `meta_intent_to_action()` — Intent → action mapping overrides
- `meta_capability_tool_map()` — Capability → tool mapping overrides
- `meta_learn_default_tool()` — Default tool for LEARN intent (default: web_fetch)
- `meta_memory_pattern(name)` — Regex patterns for memory detection (respond_prior, learn_prior)
- `meta_pack_banned_keys()` — Banned model/inference keys for pack validation

**Boot guard:**
- `assert_pressure_boot_allowed()` — Validates conductor_mode and conductor_wired before boot

### `loop.py` — Tick Driver
The single pressure tick loop + tick counter (pressure_loop/loop.py).

**Key functions:**
- `run_pressure_tick_loop(runtime)` — Main loop: each tick gathers contributors, evaluates, and on ignition hands off to `cognition_chain.ignite_episode` then pauses. Writes `_pressure_tick` and `_epoch` to shared state each tick.
- `run_pressure_tick_once(runtime)` — Single-tick variant for smoke tests / `--once` mode.

### `pressure.py` — Evaluation Logic
Gathers contributors and evaluates them to produce `EpisodeIgnition` (pressure_loop/pressure.py).

**Key functions:**
- `gather_contributors(shared_state)` — Reads authoritative operator ingress,
  merges reseed/suppression state, adds eligible Omega successors and explicit
  rollback events, and enforces ignition authority. Deterministic audits,
  mastery scans, practice minting, pending-build scans, and other background
  observations do not wake the system. Operator inputs remain in the durable
  cognition queue until `evaluate()` leases them.
- `evaluate(contributors, *, active_episode, ...)` — Returns `EpisodeIgnition` when a new episode should open, else `None`. Uses the raw pressure field for the hybrid ignition gate (ceiling + margin test), then carries the tonic neuromod state as advisory context for Meta, motor planning, execution foldback, and Omega.
- `stage_reseed(contributors)` — Appends additive reseed contributors to
  `_RESEED_STAGING` for the next pressure evaluation.
- `stage_reseed_suppress(contributor_type, weight)` — Accumulates suppression overlay in `_RESEED_SUPPRESS` (max-per-type, clamped 0–1, applied and cleared in `gather_contributors`).

**Neuromodulation — Advisory Tonic Body State (not a contributor):**
Neuromod is a **decaying learning, memory, mood, and deliberation amplifier**, not pressure and not an ignition authority. `neuromod_spike` remains removed from the pressure field.
- `_NEUROMOD_TONIC` (`pressure_field.py:401`): in-memory dict of `dopamine` / `serotonin` / `stress`. Thread-locked reads/writes.
- `refresh_neuromod_tonic(neuromod)` (`pressure_field.py:448`): replaces tonic from the closed-episode `neuromod` dict (called by `EpisodeStore.close()`).
- `decay_neuromod_tonic()` (`pressure_field.py:475`): decays tonic toward baseline on idle ticks using `pressure.yaml` `tonic_decay.decay_rate` (default 0.25).
- `read_neuromod_tonic()` (`pressure_field.py:439`): returns current tonic values; `evaluate()` snapshots these values into `EpisodeIgnition.neuromod_state` without altering pressure weights, thresholds, margins, or the winner.
- Dopamine may bias vigor and persistence during planning and reflection.
- Low serotonin may reduce assertiveness and favor verification or asking when cognition is already uncertain.
- Stress may bias caution and evidence requirements. None of these signals may change the goal, intent, operator request, factual evidence, or whether eligible pressure ignites.

**Ignition gate:** Top weight must reach effective ceiling AND beat runner-up by effective margin (pressure_loop/pressure.py).

**Reseed + suppression overlay:**
- `_RESEED_STAGING` (`pressure.py:58`): additive successor contributors; live DeepSleep closure stages only `source="omega"`, and gather rejects any non-Omega autonomous entry defensively.
- `_RESEED_SUPPRESS` (`pressure.py:59`): suppression map from Omega `pressure_feedback.contributors_to_lower`. Applied symmetrically after staging+queue merge; cleared after drain. Prevents additive-only reseed from being unable to reduce a contributor's weight. `weight = max(0.0, weight - suppress.get(type, 0))`.

### `contributors.py` — Canonical Types
Defines the canonical contributor, ignition, and action-goal types (pressure_loop/contributors.py).

**Types:**
- `Contributor(source, type, weight, metadata)` — A pressure contribution. Live ignition authority is operator, Omega, or explicit dev rollback; other sources are observational only.
- `EpisodeIgnition(episode_id, aggregate_pressure, opened_by, dominant_contributor, contributors, trajectory_snapshot, neuromod_state)` — The canonical definition of an episode, handed to the cognition chain. `neuromod_state` is advisory context, separate from pressure.
- `IntentDecision(episode_id, intent, confidence, reason)` — Global A's intent-only output (intent is one of 12 `ALLOWED_INTENTS`).

**ALLOWED_INTENTS (12):** `RESPOND`, `PROACTIVE_RESPONSE`, `RESOLVE_QUERY`, `ASK_OPERATOR`, `LEARN`, `USE_TOOL`, `IMAGE_CREATE`, `DOCUMENT_CREATE`, `SELF_INQUIRY`, `IDLE`, `USE_SKILL`, `PURSUE_OUTCOME` (`pressure_loop/contributors.py`; legacy `APPLY` canonicalizes to `USE_SKILL`). `PURSUE_OUTCOME` is reserved for a persisted Omega-authored emergent pressure and deterministically maps to `EXECUTE_MOTOR_PROGRAM`; it is not inferred from ordinary operator prose.

### `pressure_field.py` — Contributor Registry
The contributor reader registry (pressure_loop/pressure_field.py). This module aggregates pressure contributions from multiple sources into contributor scalars.

**Contributors (readers):**
- `curiosity_gap` — curiosity cycle pressure
- `operator_message` — operator chat/commands parsed by limbic ingress
- `proactive_update` — Omega reseed only (no pressure_field reader); maps to `PROACTIVE_RESPONSE` → right proactive pinboard
- `epistemic_emptiness` — endogenous pressure from low knowledge coverage
- `idle_probe_aggregate` — aggregated idle probe findings
- `learning_debt` — operator-directed mastery pressure drive
- (Removed: `guardian`, `tool_creation_need`, `evidence_terminal`, `neuromod_spike` — no longer pressure contributors. Neuromod is advisory tonic body state, not a contributor.)

**Key functions:**
- `build_registry()` — Returns dict of field_id → reader function
- `export_pressure_field_state()` / `import_pressure_field_state()` — Tick-state WAL seam for restart-safe accumulator and neuromod tonic snapshot (shutdown/restore)

### `episode_journal.py` — Episode Journal Substrate (Slice 5a)
Durable JSONL journal for completed episodes. Omega/DeepSleep read the **maintained window's `episode_bodies`** directly (Model C); the journal is still written for audit and for the `recent_outcome_population()` baseline (typed outcome counts read from each row's `outcome_axes`, WO-06), but is no longer the reflection input path (pressure_loop/episode_journal.py).

**Types:**
- `PriorEpisode` — Frozen dataclass with: episode_id, ignition_reason, intent, action, outcome, trajectory_update, omega_reflection
- `to_dict()` / `from_dict()` — Stable JSON serialization for JSONL lines

**Key functions:**
- `prior_episode_from_episode(ep)` — Defensive extractor from an in-flight episode-like object. Treats missing data as None; never crashes.
- `archive_episode(ep, path=None)` — Appends one `PriorEpisode` JSONL record (failure-soft). Called as an additive side-effect from `EpisodeStore.close()` (`pressure_loop/episode.py:343-350`). Uses `episode_journal_path()` for default path.
- `journal_window(n=None, path=None)` — Reads last N `PriorEpisode` records (oldest → newest). Failure-soft: returns `[]` on missing path, parse errors, or unparseable lines.
- `compact_episode_for_reflection(body)` — Compacts an enriched episode body for Omega reflection. Emits `operator_thread` when the episode answers a system query (carrying `system_query_id`, `question`, and `answer`) so the Q&A thread survives window compaction (`pressure_loop/episode_journal.py:196-209`).

**Config:**
- `config/journal.yaml` — `episode_journal.path` and `episode_journal.window_default`
- `pressure_loop/policy.py` — `_load_journal_config_yaml()` (cached loader), `episode_journal_path()`, `episode_journal_window_default()`

**Storage:**
- Default path: `pressure_loop/state/episode_journal.jsonl` (gitignored)
- Format: one JSON line per episode, sorted keys

## Inputs

- **Shared state signals:** Various SS keys (e.g., `LAST_CURIOSITY_CYCLE`, `LAST_INGRESS_PRESSURE`, `OUTSTANDING_USER_QUESTION`)
- **Cognition queue:** Async operator messages that arrive during an episode
  remain in the durable arrival-ordered FIFO (`pressure_loop/cognition_queue.py`)
  until their own pipeline claim reaches acknowledgement. A synchronous chat
  pin uses the same admission surface.
- **Neuromodulation:** Native weave — `cognition_chain._start_auto` computes RPE neuromod from AUTO outcome + the expectation `_neuromod_expectation` derives from `recent_outcome_population()` (the measured task success rate, or the stated 0.5 prior when no task was attempted, labelled as such in `episode.auto["neuromod_expectation"]`), writes to `episode.auto["neuromod"]`, and latches memory salience. On episode close, `EpisodeStore.close()` calls `refresh_neuromod_tonic()` to snapshot the episode's neuromod into `_NEUROMOD_TONIC`. `evaluate()` reads `read_neuromod_tonic()` as the primary neuromod gate; `decay_neuromod_tonic()` erodes it on idle ticks. The old `LAST_NEUROMOD_SPIKE` shared-state slot and the `neuromod_spike` pressure contributor are both deleted.
- **Omega pressure_feedback:** Omega window reflection emits `pressure_feedback` with `contributors_to_raise`, `contributors_to_lower`, `contributors_to_create`. These are parsed by DeepSleep reseed (`deepsleep_loop/reseed/run.py:127-163`) and staged into the pressure system as additive reseed contributors (`stage_reseed`) or suppression overlay (`stage_reseed_suppress`). This is the **metacognitive steering** path: Omega's reflection alters the next cycle's pressure scores.

## Output

- **EpisodeIgnition** — The canonical definition of the episode, handed to the cognition chain via `cognition_chain.ignite_episode` (pressure_loop/loop.py).

## Handoff / Cascade

The handoff and cognition cascade live **OUTSIDE** the pressure loop in `cognition_chain.py`. Pressure's job is to:
1. Evaluate contributors
2. Produce `EpisodeIgnition` when threshold is met
3. Call `ignite_episode(store, ignition)` to hand off
4. Pause until episode completes
5. Resume ticking

The cascade (Global A → Meta A → Omega motor when eligible → AUTO → Meta B →
Global B → Omega episode → DeepSleep) is triggered by the ignition handoff, not
driven by the pressure loop. A staged
ten-episode reflection window has its own pressure contributor and reflection
episode; it is separate from the ordinary episode cascade.

### Emergent motor pressure and loop limits

`emergent_pressure.py` persists Omega-authored pressure directly, with no Guardian hop. An initial eligible attempt may create the first successor only with `pressure_resolution.status="create"`, an empty resolution `pressure_id`, no originating pressure, and an empty `supersedes_pressure_id`; its episode, outcome, failed program, and evidence provenance must match the attempt. Existing pressure evolution stays strict: revise, split, supersede, satisfy, continue, and retire must name the exact origin.

The successor is written before it is staged into reseed, so the next `PURSUE_OUTCOME` ignition has durable provenance. The loop guard compares material pathway fingerprints and admits progress only from a positive structured check/delta with executor-owned tool, ledger, or workspace-diff identity. Prose, progress digests, failed checks, and empty failure labels cannot reset the loop. Two equivalent no-progress attempts invalidate the pathway; three distinct factual failures route to operator input unless new required capabilities make the next method materially different. These thresholds are governed by `config/cognition_chain.yaml`.

Infrastructure failures (`timeout`, `bridge_unavailable`, `runtime_unavailable`, and `rate_limited`) are transient pressure signals, not evidence that a cognitive pathway is bad. They may trigger a later retry but do not increment equivalent/distinct pathway-failure counts. Verified evidence is the only route to positive mastery; the 2026-07-11 live run recorded a verified application delta of `+0.05`, while all preceding unverified attempts remained at zero.

## Modules gained from core/ cleanup

The following modules were relocated from core/ → pressure_loop/ during the core cleanup:

- `cycle_cadence.py` — Legacy hybrid scheduler (`tick()`, `register_signal_provider()`). Still used by `hud/telemetry.py` for scheduler caching and by `symphony_probes.py` / `pressure_field.py` via `_recent_jsonl_entries`, but the live boot path no longer uses it as the conductor.
- operator_pressure.py — Operator pressure ingress helpers
- pressure_loop_pause.py — Pause state management (pressure_loop_paused)
- symphony_signals.py — Signal emission helpers
- symphony_learning_state.py — Learning state envelope for pressure field reads
- symphony_probes.py — Idle epistemic probes (`run_idle_probes()`)
- open_questions.py — Outstanding questions journal

## Observability

All pressure-loop operations emit via `guardian.terminal_feedback` (failure-soft, never halts cognition) with matching `guardian.data_logger.emit_event` JSONL durability where noted.

| Operation | Hook | Durable JSONL subsystem |
|-----------|------|------------------------|
| Tick / gather / evaluate / ignite | `phase_span` + `emit_data` | `omega.pressure.tick`, `omega.pressure.gather`, `omega.pressure.evaluate` |
| Advisory neuromod snapshot (legacy event name) | `emit_data` | `omega.pressure.neuromod_weights` |
| Reseed / suppress staging | `emit_data` | `omega.pressure.reseed_staging`, `omega.pressure.reseed_suppress` |
| Episode open / close | `emit_data` | `omega.pressure.episode_open`, `omega.pressure.episode_close` |
| Enriched episode body build | `emit_data` | `omega.pressure.enriched_episode` |
| Episode journal I/O (archive, journal_window, load/save) | `emit_data` | `omega.pressure.episode_journal.*` |
| Reflection window I/O (load/save/clear/archive/append/open) | `emit_data` | `omega.pressure.reflection_window.*` |
| Operator chat orchestration | `emit_phase` + `emit_data` | `omega.pressure.operator_chat` |
| Operator pressure I/O | `emit_data` | `omega.pressure.operator_directive`, `omega.pressure.operator_ingress` |
| Symphony ignition classify | `emit_data` | `omega.pressure.ignition_classify` |
| Symphony probes | `emit_data` | `omega.pressure.probe_*` |
| Symphony learning state | `emit_phase` + `emit_data` | `omega.pressure.learning_state` |
| Cycle cadence | `emit_signal` + audit | `omega.pressure.cycle_cadence` |
| Open questions | `emit_phase` | `omega.pressure.open_questions` |

> Failure-log coverage: `docs/architecture/FAILURE_LOG_COVERAGE.md`

## Pressure-Loop Closure (Omega `pressure_feedback`)

Omega's window reflection emits `pressure_feedback` with `contributors_to_raise`, `contributors_to_lower`, and `contributors_to_create`. DeepSleep reseed translates this into concrete pressure modifications for the **next** tick. See `docs/architecture/TOPIC_RESOLUTION_AND_MASTERY_GUIDED_LEARNING.md` (Sections 5 + 7).

**File:** `deepsleep_loop/reseed/run.py:205-270`

### Raise / Create
Omega feedback entries are converted to `Contributor(source="omega", type, weight, metadata={"origin": "omega_pressure_feedback"})` and staged via `stage_reseed()` (`pressure_loop/pressure.py:84-89`).

### Lower
Omega feedback entries are staged as suppression overlays via `stage_reseed_suppress(type, weight)` (`pressure_loop/pressure.py:91-99`). On the next `gather_contributors()`, the suppression is applied symmetrically: `weight = max(0.0, weight - suppress[type])`.

This closes the metacognitive loop: **Omega observes a 10-episode window and steers the next pressure cycle's intent.**

## Episode Mastery Delta (10th-Episode Fix)

`EpisodeStore.append_episode_mastery_delta(episode_id, delta)` (`pressure_loop/episode.py:729`) appends per-episode mastery delta annotations to the active `ReflectionWindow`. Called from `cognition_chain._maybe_run_per_episode_deepsleep()` (`cognition_chain.py:114-119`) after each per-episode consolidation. The delta lands in `window.episode_mastery_deltas`, which is carried into Omega intake as `mastery_changes` (`omega_loop/intake/run.py:60-68`). The 10th episode's delta is appended **before** the window locks, so it is included in the Omega input.

## Open Questions Persistence (3c.2b)

Open questions carry a `windows_survived` counter (`pressure_loop/open_questions.py:122`) that tracks how many locked reflection windows have elapsed while the question remains OPEN.

- **Stamping:** `record_open_question()` stamps `asked_window_id` from the active reflection window and initializes `windows_survived = 0` (`open_questions.py:239-249`).
- **Increment hook:** `increment_windows_survived_for_open_questions()` is
  called by the active window-reflection handoff before the collecting window
  is archived, so only still-OPEN questions that survive a completed window
  receive the bump.
- **Consumption:** `_pick_critical_open_question()` in the Topic Resolution Engine uses `windows_survived >= persistence_threshold` (default 3, overridable via `policy/symphony.policy.yaml`) as one leg of the criticality gate.

## Determinism

Pressure evaluation is **deterministic** — no model inference. The only stochastic element is the operator's async input timing.
