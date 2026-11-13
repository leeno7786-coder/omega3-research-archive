# Minimal Boot Surface Mapping

**Work order:** `new-minimal-boot`  
**Date:** 2026-06-04  
**Purpose:** Map the exact minimal surface for a new boot that boots ONLY the built sliced chain (Pressure → Global A → Meta A → AUTO → Meta B → Global B) + neuro spikes + pressure contributors. Everything else dies / is added when its slice lands.

---

## 1. TICK LOOP: How boot.py run_continuous() drives ticks

**Location:** `boot.py:756-1090` (`run_continuous()`)

**Exact minimal loop:**
```
acquire boot_lock → boot() → loop:
  - check pressure_loop_pause gate (skip tick if paused)
  - increment _pressure_tick / _epoch counter
  - CueDesk.tick(shared_state)
  - sleep tick_interval_s
```

**Call path:**
- `boot.py:1053` → `_cue_desk.tick(_orchestrator.shared_state)`
- `core/cue_desk.py:591` → `CueDesk.tick()`
- Inside `tick()`:
  - `curiosity_substrate_tick()` (line 695) - probes curiosity store + idle probes
  - `_tick_slice1_front()` (line 774) when `slice1_global_a_front_enabled()` is true
  - Inside `_tick_slice1_front()`:
    - `gather_contributors()` (line 473) → `core/pressure.py:169`
    - `evaluate()` (line 481) → `core/pressure.py:216`
    - `episode_store.open()` (line 536)
    - `form_intent()` (line 557) → `core/global_a.py:111`
  - If slice flags are enabled, the sliced chain runs via `_tick_slice1_front` returning after Global A only (Slice 1 front). The full sliced chain (Slice 2-4) runs when the chain runner is invoked (see section 2).

**Where the sliced chain is invoked:**
The sliced chain is NOT currently invoked from the Slice 1 front. The Slice 1 front (`_tick_slice1_front`) stops at Global A → ActionGoal and returns. The full sliced chain entry is `run_symphony_through_deepsleep` in `core/symphony_runner.py`, which is the default chain runner for CueDesk when slice flags are enabled (see section 2).

---

## 2. SLICED CHAIN ENTRY: Which function runs the sliced movements

**Module + exact entry:** `core/symphony_runner.py:2845` → `run_symphony_through_deepsleep()`

**Is it the product full-chain orchestrator?** Yes. This is the S5 full-chain harness that runs GLOBAL_A → META_A → AUTO → META_B → GLOBAL_B → OMEGA → DEEPSLEEP. However, with the slice flags enabled (`slice1_global_a_front_enabled`, `slice2_meta_a_front_enabled`, `slice3_auto_front_enabled`, `slice4_meta_b_global_b_front_enabled`), it runs only the sliced chain (lines 2931-2938):

```python
if slice1_global_a_front_enabled():
    if slice2_meta_a_front_enabled():
        _append_slice2_meta_a_movement(trace, episode)
        if slice3_auto_front_enabled():
            _append_slice3_auto_movement(trace, episode)
            if slice4_meta_b_global_b_front_enabled():
                _append_slice4_meta_b_global_b_movement(trace, episode)
    return trace
```

**Does the live booted tick call it?**
- **Current production boot:** NO. The current boot uses CueDesk with the Slice 1 front only (`_tick_slice1_front`), which stops at Global A → ActionGoal. The full chain runner (`run_symphony_through_deepsleep`) is only used for WAL-restored episodes and when explicitly injected.
- **With slice flags enabled:** The chain runner would be called by CueDesk when an episode is opened and the chain runner is resolved (see `core/cue_desk.py:1182-1187` where it defaults to `run_symphony_through_deepsleep`).

**Current flow with slice flags:**
- Slice 1 front (`_tick_slice1_front`) runs independently and returns after Global A
- The full chain runner is NOT invoked in the normal tick path
- This is a **gap** - the new minimal boot should wire the sliced chain entry to run after Slice 1

---

## 3. TRANSITIVE-IMPORT TRAP: Does symphony_runner.py pull in legacy at TOP LEVEL?

**Top-level imports in `core/symphony_runner.py` (lines 47-67):**
```python
from core.shared_state import SharedState
from core.shared_state_keys import (
    LAST_GLOBAL_GOALS,
    LAST_GLOBAL_OBSERVATIONS,
    LAST_META_CYCLE,
    LAST_META_ORDERS,
    LAST_NEUROMOD_SPIKE,
    LAST_OMEGA_EVALUATION,
    LAST_OMEGA_PROPOSALS,
    LAST_OMEGA_TRAJECTORY,
)
from core.symphony_global_action import (
    build_episode_archive_snapshot,
    build_global_late_update,
)
from core.symphony_projection import (
    EPISODE_PROJECTION_CLEAR_SLOTS,
    clear_episode_projections,
)
from core.symphony_signals import SymphonySignal
from guardian.terminal_feedback import emit_phase, phase_span
```

**Analysis:**
- All top-level imports are **infrastructure modules** (shared_state, shared_state_keys, symphony_*, guardian.terminal_feedback)
- **NO legacy loop imports** at top level (no `autonomous_loop`, `meta_b_loop`, `global_loop`, `omega`, `deep_sleep`, `mint`)

**Lazy imports (inside functions):**
- `run_meta_b_movement` (line 263): `from metacognition.meta_b_loop import MetaLoopContext, run_meta_b_loop`
- `run_global_a_movement`: imports global loop modules lazily
- `run_omega_movement`: imports omega modules lazily
- `run_deepsleep_movement`: imports deepsleep modules lazily
- `mint_tool_from_curiosity` (line 2962): `from metacognition.omega.tool_synthesis.mint import mint_tool_from_curiosity`

**Verdict:** The new boot **CAN reuse the existing sliced entry cleanly** (`run_symphony_through_deepsleep` in `core/symphony_runner.py`). No thin orchestrator module is needed because:
1. Top-level imports are safe (infrastructure only)
2. Legacy loop imports are lazy (only load when those specific movements run)
3. With slice flags enabled, the legacy movements (OMEGA, DEEPSLEEP, mint) are skipped

**Recommendation:** Reuse `core/symphony_runner.py` as the sliced chain entry. It is already designed to be slice-gated and does not pull in legacy at import time.

---

## 4. MINIMAL INFRA INIT: Smallest set of subsystems boot() must initialize

**Required for sliced chain to run end-to-end:**

| Subsystem | Init call in boot() | Evidence | Does sliced chain need it? |
|-----------|---------------------|----------|---------------------------|
| **Episode store** | `Orchestrator()` (line 500) creates `episode_store` | `core/orchestrator.py` | **YES** - CueDesk opens episodes against it |
| **SharedState / Orchestrator** | `Orchestrator()` (line 500), `set_default_orchestrator()` (line 511) | `boot.py:497-511` | **YES** - CueDesk and chain runner read/write shared state |
| **Runtime client** | `get_runtime_client()` (line 314) | `boot.py:314-349` | **YES (lazy)** - AUTO needs it for model inference; lazy init is OK |
| **Memory API (embedding substrate)** | `set_embedding_substrate_state()` (line 657) | `boot.py:657-668` | **YES** - AUTO recall needs memory.api for embedding |
| **Tool dispatch + catalog + cognitive tool schemas** | None in boot() (persisted on disk) | `boot.py:378-390` comment | **YES** - AUTO USE_TOOL needs tool dispatch and schemas |
| **Symphony policy + slice flags** | `assert_symphony_boot_allowed()` (line 794) | `boot.py:794-795` | **YES** - Slice gates check these flags |
| **Pressure conductor** | `CueDesk(_orchestrator)` (line 946) | `boot.py:945-947` | **YES** - CueDesk IS the pressure conductor |
| **Memory system (StrengthIndex, gates, classifier)** | `init_strength_index()` (line 189), `KnowledgeGate()` (line 231), `get_classifier()` (line 229) | `boot.py:178-232` | **YES** - AUTO recall needs StrengthIndex and classifier |
| **Guardian (terminal feedback, identity charter)** | `install_terminal_feedback()` (line 42), `IdentityCharter()` (line 250), `get_guardian_node()` (line 275) | `boot.py:42-277` | **MAYBE** - Terminal feedback is for observability; identity charter is production-mode only |
| **Phase registry + execution engine** | `PhaseRegistry.instance()` (line 439), `ExecutionEngine()` (line 499) | `boot.py:437-512` | **NO** - Sliced chain uses direct movement runners, not phase registry |
| **BrainLoop runner** | `make_brain_runner()` (line 505) | `boot.py:498-505` | **NO** - Sliced chain uses AUTO (auto.py), not BrainLoop |

**Minimal init list for new boot:**
1. Episode store + SharedState + Orchestrator (set_default)
2. Memory system (StrengthIndex, gates, classifier)
3. Runtime client (lazy - can defer until first AUTO call)
4. Memory API (embedding substrate state)
5. Tool dispatch + catalog (schema files persist on disk, no init needed)
6. Symphony policy loader (to read slice flags)
7. CueDesk (pressure conductor)
8. Guardian terminal feedback (optional - for observability)

**NOT needed:**
- Phase registry
- Execution engine
- BrainLoop runner
- Legacy handler registration
- Dev tool registration
- HUD spawn
- Face warmup
- Substrate health probes
- Self-state hydration
- First-contact seeding
- Tick-WAL restore

---

## 5. CONTRIBUTORS + NEURO SPIKES: How pressure contributors are gathered and fed

**Gather path:**
1. `CueDesk.tick()` → `curiosity_substrate_tick()` (line 695) - probes curiosity store + idle probes
2. `_tick_slice1_front()` → `gather_contributors()` (line 473) → `core/pressure.py:169`
3. `gather_contributors()` reads from `pressure_field` readers (line 175-177):
   ```python
   readers = pf.build_registry()
   for field_id, reader in readers.items():
       if field_id in _REMOVED_FIELD_IDS:
           continue
       weight = reader(shared_state)
   ```
4. `evaluate()` applies the pressure-only ceiling/margin check and attaches advisory neuromod state to a successful ignition

**Contributor sources (per `core/pressure.py:34-42`):**
- `curiosity_gap` → autonomous
- `operator_intent` → operator
- `outstanding_question` → autonomous
- `operator_message` → operator (refined via ingress metadata)
- `epistemic_emptiness` → autonomous
- `idle_probe_aggregate` → autonomous
- `learning_debt` → autonomous

**Removed contributors (line 29-31):**
- `guardian` - removed this slice
- `tool_creation_need` - removed this slice
- `evidence_terminal` - not a pressure contributor (within-episode observation)

**Neuromod integration (corrected 2026-07-28):**
- `evaluate()` reads tonic dopamine/serotonin/stress/spike and attaches it to `EpisodeIgnition.neuromod_state`.
- Chemistry cannot change pressure weights, threshold, margin, or the winning contributor.
- The snapshot shapes downstream deliberation: low serotonin may lower assertiveness, stress may favor caution, and dopamine may bias vigor/persistence without changing the requested work.

**Autonomous contributors (LEARN/IDLE mapped to new spec):**
- `learning_debt` → maps to LEARN intent
- `curiosity_gap` → maps to LEARN intent
- `epistemic_emptiness` → maps to LEARN intent
- `idle_probe_aggregate` → maps to IDLE intent
- `outstanding_question` → maps to PROACTIVE_RESPONSE intent

**NEURO-SPIKE contributors (neuromodulator spike system):**
- Neuromod spikes are NOT pressure contributors themselves
- They MODULATE existing contributor weights and threshold
- Spike source: AUTO writes `episode.auto.neuromod` → `normalize_episode_auto_neuromod()` → `persist_neuromod_spike_projection()` → SharedState `last_neuromod_spike`
- Next tick reads `last_neuromod_spike` in `_neuromod_from_shared_state()` (pressure.py:102)

**What the new boot must wire:**
1. `pressure_field` readers must be registered (happens in `core/pressure_field.py` module load)
2. `curiosity_store` must be probed via `curiosity_substrate_tick()` (for curiosity_gap contributor)
3. `idle_probes` must run via `run_idle_probes()` (for idle_probe_aggregate contributor)
4. SharedState `last_neuromod_spike` slot must be readable (no init needed - just read)
5. Operator input path: operator messages queue in `_PENDING_QUEUE` (pressure.py:44) when they arrive during episode halt

---

## 6. DROP LIST: Everything boot.py / run_continuous() wires that the sliced chain does NOT need

**HUD spawn (lines 824-864):**
- `spawn_hud_in_thread()` - operator-facing UI, not needed for sliced chain
- Auto-open browser - not needed
- **Confirmation:** Sliced chain runs headless; HUD is optional observability

**Face warmup (lines 898-918):**
- `face_models_warmup_probe()` - face recognition models
- `warm_anchor_store_from_vision_lobe()` - vision lobe anchors
- **Confirmation:** Sliced chain does not use face recognition

**Substrate health probes (lines 866-921):**
- HTTP probes to omega-runtime, HUD - observability only
- **Confirmation:** Sliced chain does not depend on these probes

**First-contact surface (lines 923-943):**
- `first_contact_status()` - onboarding seeding
- **Confirmation:** Sliced chain does not depend on onboarding state

**Legacy handlers / phase registry (lines 430-480):**
- `import handlers` - legacy phase handler registration
- `PhaseRegistry.instance()` - legacy phase contract registry
- **Confirmation:** Sliced chain uses direct movement runners, not phase registry

**Execution engine (lines 495-499):**
- `ExecutionEngine()` - wraps phase invocations
- `HandlerRuntime()` - bridges engine to registry
- **Confirmation:** Sliced chain calls movement runners directly, not through engine

**BrainLoop runner (lines 498-505):**
- `make_brain_runner()` - registers brain loop
- **Confirmation:** Sliced chain uses AUTO (auto.py), not BrainLoop

**Dev tool registration (lines 396-417):**
- `register_dev_agent_tools()` - dev/coding tools
- `register_dev_tools()` - project tools
- **Confirmation:** Sliced chain uses cognitive tools only, not dev tools

**Tick-WAL restore (lines 577-627):**
- `restore_tick_state_into_substrate()` - restores tick counter from WAL
- **Confirmation:** Sliced chain can start with fresh tick counter; WAL restore is for crash recovery

**Boot question reset (lines 629-655):**
- `clear_pending_operator_questions()` - clears legacy question queue
- **Confirmation:** Sliced chain uses pressure contributors, not legacy question queue

**Self-state hydration (lines 700-730):**
- `hydrate_self_state()` - hydrates identity/trajectory snapshot
- **Confirmation:** Sliced chain reads trajectory from SharedState directly; hydration is for first-contact

**Boot lock (lines 781-789):**
- `acquire_boot_lock()` - single-instance guard
- **Confirmation:** Useful for production, but not strictly required for sliced chain operation

**Browser test mode (lines 949-977):**
- Browser extension test mode
- **Confirmation:** Not needed for sliced chain

**Pressure loop pause gate (lines 991-1034):**
- `is_pressure_loop_paused()` - dev pill pause
- **Confirmation:** Useful for dev mode, but not required for sliced chain operation

**Global operator outreach retry (lines 727-768):**
- `dispatch_pending_if_present()` - retries pending operator response
- **Confirmation:** Sliced chain does not use global operator outreach (Slice 5+ feature)

**Guardian production mode (lines 244-277):**
- `IdentityCharter.load()` / `verify()` - production-mode cryptographic chain
- **Confirmation:** Dev mode can skip this; production mode needs it

**Summary of drops:**
- All UI/observability (HUD, face warmup, health probes)
- All legacy infrastructure (phase registry, execution engine, BrainLoop)
- All dev tools (dev tool registration)
- All crash recovery (tick-WAL restore)
- All onboarding (first-contact, self-state hydration)
- All optional guards (boot lock, pause gate, browser test)
- All Slice 5+ features (global operator outreach)

---

## FINDING SUMMARY

**Minimal init list:**
1. Episode store + SharedState + Orchestrator (set_default)
2. Memory system (StrengthIndex, gates, classifier, coord index)
3. Runtime client (lazy - defer until first AUTO call)
4. Memory API (embedding substrate state)
5. Tool dispatch + catalog (schema files persist, no init needed)
6. Symphony policy loader (to read slice flags)
7. CueDesk (pressure conductor)
8. Guardian terminal feedback (optional - for observability)

**Chain entry + transitive-import verdict:**
- **Reuse existing entry:** `core/symphony_runner.py:run_symphony_through_deepsleep()`
- **Transitive-import verdict:** SAFE - top-level imports are infrastructure only; legacy loop imports are lazy
- **Thin orchestrator needed:** NO - existing symphony_runner is already slice-gated and safe

**Contributor/neuro-spike wiring:**
1. Wire `pressure_field` readers (module load auto-registers)
2. Wire `curiosity_substrate_tick()` for curiosity_gap + idle probes
3. Wire SharedState `last_neuromod_spike` read path (no init, just read)
4. Wire operator input queue (`_PENDING_QUEUE` in pressure.py)
5. Neuromod modulation is automatic in `evaluate()` via `_neuromod_from_shared_state()`

**Critical gap identified:**
The current Slice 1 front (`_tick_slice1_front`) stops at Global A → ActionGoal and does NOT invoke the sliced chain runner. The new minimal boot must wire the chain runner (`run_symphony_through_deepsleep`) to run after Slice 1 when slice flags are enabled. This is the missing link between the Slice 1 front and the Slice 2-4 movements.
