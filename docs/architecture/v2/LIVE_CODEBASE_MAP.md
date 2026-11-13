# LIVE CODEBASE MAP -- Omega 3.0

> **Historical snapshot — superseded 2026-08-03.** This map predates the
> one-episode cognition cycle and universal SRAM lane. See `AGENTS.md` and
> `docs/BASELINE_2026-08-03.md` for current authority and evidence.

**Generated:** 2026-06-10 from live codebase scan of `F:\Omega3.0`
**Scope:** Entire system -- every active module, data structure, config, and integration point
**Status:** main branch

---

## 1. EXECUTIVE SUMMARY

Omega 3.0 is a **pressure-driven cognition cascade** -- a single-threaded, synchronous execution pipeline that runs inside a continuous pressure tick loop. There is no central orchestrator, no route fork, and no outlet layer. Every pressure ignition produces exactly one episode, which carries data through a deterministic cascade:

```
Pressure Tick (every 15s)
  |
  v
gather_contributors() -> evaluate() -> EpisodeIgnition
  |
  v
cognition_chain.ignite_episode()  [synchronous, blocks pressure loop]
  |
  +-- Global A (intent/target extraction)
  +-- Meta A (execution planning, NO model inference)
  +-- AUTO (model inference + tool execution)
  +-- Meta B (reflection, NO model inference)
  +-- Global B (outcome packaging)
  +-- Per-Episode DeepSleep (mastery/failure/semantic consolidation)
  +-- Episode Close (archive + window append)
  |
  v
Window Reflection (every 10 episodes)
  +-- Omega (metacognitive reasoning over 10-episode window)
  +-- Window DeepSleep (identity/trajectory/query/reseed)
  +-- Archive & Reset Window
```

**Key invariant:** One active episode at a time. Pressure queues contributors while an episode is open. The cascade is fail-hard -- any phase failure halts the tick.

---

## 2. PROJECT STRUCTURE

```
F:\Omega3.0
|
|-- boot.py                     # Entry point: boot() -> pressure tick loop
|-- cognition_chain.py          # Cascade orchestrator
|-- pytest.ini                  # pytest config (1044 tests, coverage, 300s timeout)
|-- requirements.txt            # Core deps
|-- requirements-dev.txt        # Dev deps
|
|-- auto_loop/                  # Slice 3: Execution engine
|-- autonomic/                  # Neuromodulation (dopamine, plasticity, stress, serotonin)
|-- browser_agent_extension/    # Browser automation
|-- cognition/                  # Topic resolution + curiosity engine
|-- config/                     # YAML configs
|-- core/                       # Shared infrastructure (boot_lock, shared_state, paths)
|-- deepsleep_loop/             # Slice 6: Consolidation engine
|-- docs/                       # Architecture documentation (120+ files)
|-- global_loop/                # Slice 1 + 4b: Intent + outcome
|-- guardian/                   # Observability + decision authority
|-- hud/                        # Operator web interface (FastAPI, port 13310)
|-- knowledge_extractor/        # Knowledge artifact pipeline
|-- memory/                     # Gate-based indexed cortex
|-- meta_loop/                  # Slice 2 + 4a: Planning + reflection
|-- omega_loop/                 # Slice 5: Window reflection
|-- policy/                     # Policy evaluation
|-- pressure_loop/              # The heartbeat (15s ticks)
|-- runtime/                    # Model server HTTP client bridge
|-- scripts/                    # Operational scripts (shutdown, reset, smoke)
|-- sensory/                    # Presence + voice I/O
|-- skills/                     # Agent skills
|-- tests/                      # Test suite (~1044 tests)
+-- .venv-brain/                # Python virtual environment
```

---

## 3. BOOT SEQUENCE

**File:** `boot.py` (255 lines)

```
run_continuous()
  |
  +-- acquire boot_lock (kill duplicate boot.py processes)
  +-- assert_pressure_boot_allowed() (policy check)
  +-- boot()
  |     +-- Create PressureRuntime (SharedState + EpisodeStore)
  |     +-- set_default_runtime(runtime)
  |     +-- Health probe: runtime client /health
  |     +-- Memory warmup (coord_index, strength_index)
  |     +-- Guardian response_classes reload
  |     +-- Terminal console install
  |     +-- Emit readiness summary
  +-- ensure_cognition_runtime()  [FAIL-HARD: model must be up]
  +-- spawn_hud_in_thread() -> http://127.0.0.1:13310
  +-- run_pressure_tick_loop(runtime)  [INFINITE LOOP]
  |     +-- 15s ticks until KeyboardInterrupt
  +-- shutdown_hud() [finally]
```

**Key:** Boot is failure-soft for substrate (warnings logged, continues) but fail-hard for cognition (model unavailable -> crash).

---

## 4. PRESSURE LOOP -- THE HEARTBEAT

**File:** `pressure_loop/loop.py` (127 lines)

### 4.1 Tick Loop

```python
run_pressure_tick_loop(runtime)
  while True:
    read pause_state (fail-fast if unreadable)
    if paused: sleep and continue
    increment tick counter
    write tick + epoch to SharedState
    _run_pressure_tick_body(runtime)
    sleep 15 seconds
```

### 4.2 Tick Body

```python
_run_pressure_tick_body(runtime)
  gather_contributors(ss, episode_store)
    -> List[Contributor]
  evaluate(contributors, active_episode, shared_state, episode_store)
    -> EpisodeIgnition | None
  if ignition:
    ignite_episode(store, ignition)  [BLOCKS until cascade completes]
  else:
    decay_neuromod_tonic()  [failure-soft]
```

### 4.3 Contributor Gathering

**File:** `pressure_loop/pressure.py`

**Contributor Types:** `curiosity_gap`, `operator_command`, `operator_query`, `outstanding_question`, `epistemic_emptiness`, `idle_probe_aggregate`, `learning_debt`

**Queueing:** Only operator inputs queue during episode halt. Autonomous drives never queue.

### 4.4 Evaluation (Hybrid Ignition Gate)

```
Top weight >= ceiling(0.10) AND (top - second) >= margin(0.05)
  Yes -> EpisodeIgnition (new UUID, winner's type, contributors, trajectory)
  No  -> None (suppress reason: below_ceiling / ambiguous_winner)
```

**Neuromod formula:**
- `dopamine = rpe` (reward prediction error)
- `plasticity = |rpe|` (learning rate)
- `stress = 0.6*max(0,-outcome) + 0.4*max(0,-rpe) + 0.3*overrun`
- `serotonin = baseline - 0.5*stress` (patience)

---

## 5. COGNITION CASCADE

**File:** `cognition_chain.py` (277 lines)

### 5.1 Full Cascade Flow

```python
_ignite_episode_cascade(store, ignition)
  _maybe_run_window_reflection(store)  [BEFORE open -- if window locked]

  # Capture context
  temporal_context = capture_temporal_context(phase="pressure")
  learning_state = build_episode_learning_state()

  # Open episode
  episode = store.open(
    opened_by=ignition.opened_by,
    pressure=pressure_snapshot,
    temporal=temporal_context,
    learning_state=learning_state,
  )  [FAIL-HARD if None]

  # Run cascade
  try:
    _start_global_a(store, episode, ignition)
      -> run_global_a(episode, ignition) -> ActionGoal
    if slice2_meta_a_front_enabled():
      _start_meta_a(store, episode)
        -> run_meta_a(episode) -> ExecutionActionPack
    if slice3_auto_front_enabled():
      _start_auto(store, episode)
        -> run_auto_front(episode) -> AutoOutcomePack
        -> compute_episode_neuromod() -> episode.auto["neuromod"]
    if slice4_meta_b_global_b_front_enabled():
      _start_meta_b_global_b(store, episode)
        -> run_meta_b(episode) -> MetaReflectionPack
        -> run_global_b(episode, reflection) -> GlobalOutcomePack
    cascade_status = "completed"
  except Exception:
    cascade_status = "failed"
    raise

  finally:
    pack = _run_per_episode_journal_write(store, episode, cascade_status)
      -> run_deepsleep_for_episode(store, ep_id, body)
    store.close(cascade_status)
    _apply_per_episode_mastery_delta(store, pack)
    _maybe_run_window_reflection(store)  [AFTER close]
```

### 5.2 Slice Gates (from `core/policy/symphony.yaml`)

All slices enabled in production:
- `slice1_global_a_front: true`
- `slice2_meta_a_front: true`
- `slice3_auto_front: true`
- `slice4_meta_b_global_b_front: true`
- `slice5_omega_front: true`
- `slice6_deepsleep_front: true`

---

## 6. THE FIVE COGNITION LOOPS

### 6.1 GLOBAL_LOOP (Slice 1 + 4b)

**Files:** `global_loop/loop.py`, `global_loop/*/run.py`

**Global A:** `run_global_a(episode, ignition) -> ActionGoal`
```
ignition -> intent -> extract -> goal
```
- **intent:** Map `opened_by` -> one of 9 ALLOWED_INTENTS
- **extract:** Extract target/topic (max 120 chars)
- **goal:** Assemble ActionGoal with confidence

**Output:** `episode.global_["action_goal"] = {intent, target, confidence, reason}`

**Global B:** `run_global_b(episode, meta_reflection) -> GlobalOutcomePack`
```
assemble -> goal_state -> ground -> summarize -> followups -> package
```

**Output:** `episode.global_["outcome"] = {goal_state, grounded, summary, followups, ...}`

---

### 6.2 META_LOOP (Slice 2 + 4a)

**Files:** `meta_loop/loop.py`, `meta_loop/*/run.py`

**Meta A:** `run_meta_a(episode) -> ExecutionActionPack`
```
intake -> action -> tool -> memory -> pack
```
- **action:** Map intent -> META_ACTION (RESPONSE, USE_TOOL, IMAGE_CREATION, etc.)
- **tool:** Select tool (web_fetch, ocr_tool, etc.)
- **memory:** memory_required, scope, query, tools
- **pack:** Validate + assemble

**Key:** Meta A is deterministic -- NO model inference. Planning only.

**Meta B:** `run_meta_b(episode) -> calls Global B`
```
consistency -> disposition -> reflect -> [Global B]
```

**Key:** Meta B is deterministic -- NO model inference.

---

### 6.3 AUTO_LOOP (Slice 3)

**Files:** `auto_loop/loop.py`, `auto_loop/*/run.py`

**Entry:** `run_auto_front(episode) -> AutoOutcomePack`
```
intake -> validate -> recall -> promptpack -> route -> execute -> fold
```

- **recall:** MRIL tools (remember, recall, query) -- fetch knowledge before execution
- **route:** Select model profile (TEXT_REASONING, TEXT_FAST, TOOL_EXECUTOR, IMAGE_GENERATION)
- **execute:** Model inference + tool calls + image generation

**Output:** `episode.auto = {status, cycle_kind, outputs, telemetry, memory, ...}`

**Key:** This is where ALL model inference in the cascade happens (along with Omega).

---

### 6.4 OMEGA_LOOP (Slice 5)

**Files:** `omega_loop/loop.py`, `omega_loop/*/run.py`

**Entry:** `run_omega_for_window(window) -> OmegaOutcomePack`

**Trigger:** Window locks at 10 episodes. Runs BEFORE next episode opens.

```
intake -> reason -> reflect -> package
```

- **intake:** Build OmegaInput from 10 episode bodies + trajectory snapshot + mastery_changes
- **reason:** METACOGNITIVE_REASONING model pass (4096 tokens, temperature 1.0)
- **reflect:** Parse JSON, validate, extract reflection + trajectory update

**Output:** `OmegaOutcomePack` with reflection, trajectory_update, trajectory_diff, temporal, learning_state.

**Retry logic:** 3 attempts total (2 retries).

---

### 6.5 DEEPSLEEP_LOOP (Slice 6)

**Files:** `deepsleep_loop/loop.py`, `deepsleep_loop/*/run.py`

#### Per-Episode DeepSleep

**Entry:** `run_deepsleep_for_episode(store, episode_id, body)`
```
intake -> mastery -> failure -> semantic -> consolidate -> consolidate_memory
```

- **mastery:** knowledge_increment: 0.10, default_target: 0.70
- **failure:** Consolidate failure patterns
- **semantic:** Promote semantic knowledge

#### Window DeepSleep

**Entry:** `run_deepsleep_for_window(store, window, omega_outcome)`
```
intake -> mastery -> identity -> semantic -> trajectory -> failure
  -> query_lifecycle -> consolidate -> projection -> consolidate_memory
  -> reseed -> closure
```

- **identity:** Update identity traits
- **trajectory:** Maintain trajectory records
- **query_lifecycle:** Process open questions
- **projection:** Future capability forecasting
- **reseed:** Generate pressure contributors from gaps (mastery_gap, application_gap, outstanding_question)
- **closure:** Stage reseed for next ignition

**Output:** `DeepSleepOutcomePack` with updated_identity, updated_mastery, pressure_seed, projection_state.

---

## 7. EPISODE LIFECYCLE & DATA STRUCTURES

### 7.1 CognitiveEpisode

**File:** `pressure_loop/episode.py`

```python
@dataclass
class CognitiveEpisode:
    episode_id: str           # UUID
    opened_at: str            # ISO8601
    closed_at: Optional[str]  # ISO8601
    state: str                # "open" -> "complete"
    pressure: Dict[str, Any]  # {aggregate, opened_by, contributors, trajectory_snapshot}
    opened_by: str            # Dominant contributor type
    meta: Dict[str, Any]      # {execution_action_pack, reflection}
    auto: Dict[str, Any]      # {status, cycle_kind, outputs, telemetry, neuromod}
    global_: Dict[str, Any]   # {action_goal, outcome}
    omega: Dict[str, Any]     # {trajectory_update, reflection}
    sensory: List[Dict]         # [{ts, source, kind, payload}]
    brain_turns: List[Dict]     # [{cycle_id, ingress_class, outcome_hint, action_kind}]
    temporal: Dict[str, Any]    # {captured_at_iso, wall_clock_seconds, tick_count, phase, cycle_id, epoch_id, source}
    learning_state: Dict[str, Any]  # {captured_at_iso, source, strength_index, mastery, recall_confidence, learning_debt_flags}
    cascade_status: str       # "completed" | "failed"
    archived_at: Optional[str]
```

### 7.2 EpisodeStore

**Thread safety:** Single `threading.Lock` around active-episode reference.

**Key Methods:**
- `open(pressure, opened_by, temporal, learning_state) -> CognitiveEpisode`
- `close(cascade_status) -> CognitiveEpisode` -- archives JSONL, appends to reflection window
- `current() -> CognitiveEpisode | None`
- `append_episode_mastery_delta(episode_id, delta) -> None`
- `get_locked_window() -> ReflectionWindow | None`
- `consume_window_ready() -> ReflectionWindow | None`
- `archive_and_reset_window(window, omega_outcome) -> None`

### 7.3 ReflectionWindow

**File:** `pressure_loop/reflection_window.py`

```python
@dataclass
class ReflectionWindow:
    window_id: str
    started_at_episode: str
    trajectory_snapshot: Dict[str, Any]  # Immutable at window open
    episodes: List[str] = []            # episode_ids
    episode_bodies: List[Dict] = []      # Enriched episode bodies
    cascade_status_by_episode: Dict[str, str] = {}
    episode_mastery_deltas: List[Dict] = []
    status: str = "collecting"           # "collecting" -> "locked"
```

**WINDOW_SIZE = 10**

**Lifecycle:**
1. `open_reflection_window(started_at_episode)` -> Create new window
2. `append_episode_to_window(window, episode_id, cascade_status, body)` -> Append, lock at 10
3. `save_reflection_window(window)` -> Persist to `guardian/state/reflection_window.json`

---

## 8. MEMORY SYSTEM

**Files:** `memory/api.py`, `memory/promotion.py`, `memory/semantic_state_engine.py`, `memory/strength_index.py`, `memory/engine/walker.py`, `memory/encoding.py`, `memory/gates/base.py`

### 8.1 Memory Architecture

```
Memory Write Flow:
  write_memory(envelope)
    -> create_staging(envelope)  [guardian/state/staging/<ref>.json]
    -> verify_promotion(staging_ref)  [Guardian checks]
    -> commit_promotion(staging_ref, approver_id)
      -> atomic_write(memory_dict)  [memory/tmp_writes/*.tmp -> fsync -> rename]
      -> patch_index()  [update coord_index.json]
      -> encode_committed_memory()
        -> embed()  [EmbeddingGemma-300M, 768 dims]
        -> self-vector update (identity lobe)
        -> contradiction precheck
        -> StrengthIndex.register()
        -> enqueue_reflection_eligible()
        -> enqueue_deepsleep_candidate()
```

### 8.2 Dewey 3D Coordinate System

```
X = Lobe (0-11):
  0: Identity      1: Personas     2: Knowledge
  3: Reasoning     4: Situational  5: Tools
  6: Journal       7: Vision       8: Coding
  9: Temporal     10: Workflows   11: Dev

Y = Subdomain (chapter within lobe)
Z = Leaf (individual memory record)
```

**Memory ID format:** `X.Y.Z.hex` (e.g., `2.24.eaa7ed25`)

### 8.3 Key Memory Components

| Component | File | Purpose |
|-----------|------|---------|
| **API** | `memory/api.py` | read_memory(), write_memory(), promote_memory() |
| **Promotion** | `memory/promotion.py` | Staging -> verification -> commit chain |
| **Write Guard** | `memory/write_guard.py` | Content policy (adversarial detection) |
| **Semantic Engine** | `memory/semantic_state_engine.py` | L0 embedding (EmbeddingGemma-300M) |
| **Strength Index** | `memory/strength_index.py` | Per-memory robustness tracking |
| **Walker** | `memory/engine/walker.py` | Filesystem retrieval (classify -> neighbors -> expand) |
| **Encoding** | `memory/encoding.py` | Post-commit 8-step pipeline |
| **Gates** | `memory/gates/base.py` | Per-lobe access control |

### 8.4 StrengthIndex

- **Path:** `guardian/state/strength_index.json`
- **Decay:** 24h half-time, floor at 30% of write_strength
- **Touch boost:** Max 0.40 additive, saturates at 25 touches
- **Save threshold:** Dirty count >= 25

### 8.5 SemanticStateEngine

- **Model:** EmbeddingGemma-300M (768 dims)
- **Contradiction threshold:** cosine < 0.30
- **Drift threshold:** cosine distance > 0.50
- **Failure:** Returns None when FLM unavailable

---

## 9. AUTONOMIC/NEUROMOD SYSTEM

**File:** `autonomic/symphony_neuromod.py`

### 9.1 Formulas

```
baseline_clamped = clamp(baseline_success_rate, 0, 1)
expected = 2 * baseline_clamped - 1
rpe = clamp((outcome - expected) / 2, -1, 1)

dopamine = rpe
plasticity = |rpe|
stress = 0.6 * max(0, -outcome) + 0.4 * max(0, -rpe) + 0.3 * overrun
serotonin = baseline_clamped - 0.5 * stress
strength = |rpe|
```

### 9.2 Memory Salience

```
scale = 1.0 + ENCODE_DOPAMINE_GAIN * reward + ENCODE_PLASTICITY_GAIN * plasticity
tau_factor = 1.0 - STRESS_TAU_PULL * stress + SEROTONIN_TAU_PUSH * serotonin
```

- Latched by `set_current_memory_salience(neuromod)` after each AUTO cycle
- Sampled by memory writes within the cycle

---

## 10. HUD & RUNTIME

### 10.1 HUD (Operator Surface)

**File:** `hud/server.py` -- FastAPI app factory, port 13310

**Key Routes:**
- `GET /` -> omega_interface.html (self-contained, no external assets)
- `GET /healthz` -> liveness probe
- `GET /api/status` -> 10 status pills (voice, identity, memory, l0, embed, classify, face, etc.)
- `GET /api/boot/state` -> boot banner
- `GET /api/panel/{topic}` -> detail panels (brain, memory, omega, meta, global, autoloop, deepsleep, settings)
- `GET /api/telemetry/snapshot` -> unified telemetry
- `GET/POST /api/settings/*` -> theme/voice/profile persistence

**Spawner:** `hud/spawner.py` -- daemon thread, restart-on-crash (5 restarts, exponential backoff)

### 10.2 Runtime (Model Server Bridge)

**File:** `runtime/client.py` -- Singleton HTTP client to omega-portable-lite (:11434)

**Capabilities:** embed, classify, generate, transcribe, synthesise, face_identify, document_extract, image_gen, vision

**Timeout mapping:**
- health: 2s, classify: 5s, embed: 5s, generate: 60s, transcribe: 30s, image_gen: 120s

**Capability Broker:** `runtime/runtime_capability_broker.py`
- 11 capabilities: VOICE, IMAGE, STT, TTS, FACE, DOCUMENT_PARSING, CODING_MODEL, etc.
- States: AVAILABLE, DEGRADED, DOWN
- Routing: PROCEED, RESPONSE_HOLD, ALTERNATE_CAPABILITY
- **Doctrine:** Every runtime operation MUST call `preflight_runtime_operation()` first

### 10.3 Guardian Decision System

**File:** `guardian/decision.py`

**Four States:**
- **ALLOW** -> commit to canonical memory, normal response
- **FLAG** -> flagged memory, bounded response posture
- **QUARANTINE** -> isolated storage, defensive posture
- **REJECT** -> no persistence, security event, hard boundary

**Response Modes:**
- `accept_normal`, `bounded_emotional_distance`, `identity_boundary_enforcement`, `security_block`

---

## 11. CONFIGURATION SYSTEM

### 11.1 Loading Architecture

**Priority:** `core/policy/symphony.yaml` > `config/*.yaml` > code defaults

**Three loaders:**
1. `config.loader` -- unified YAML loader with caching + schema validation
2. `pressure_loop.policy` -- direct YAML reads (lru_cache) for pressure/cognition
3. Direct reads -- `runtime_policy.py`, `response_classes.py` bypass loader

### 11.2 Key Config Files

| Config | Controls | Primary Readers |
|--------|----------|-----------------|
| `config/pressure.yaml` | Tick interval, thresholds, neuromod | pressure_loop/policy.py |
| `config/global.yaml` | Global A/B intent/outcome | global_loop/*/run.py |
| `config/meta.yaml` | Meta A/B action/reflection | meta_loop/*/run.py |
| `config/auto.yaml` | AUTO promptpack/route/execute | auto_loop/*/run.py |
| `config/omega.yaml` | Omega reason/reflect/package | omega_loop/*/run.py |
| `config/deepsleep.yaml` | DeepSleep mastery/identity/trajectory | deepsleep_loop/*/run.py |
| `config/runtime.yaml` | Model endpoints (localhost:11434) | runtime/client.py |
| `config/tools.yaml` | Tool registry | auto_loop/tool/run.py |
| `core/policy/symphony.yaml` | **AUTHORITATIVE** slice gates | pressure_loop/policy.py, boot.py |

### 11.3 Slice Gates (symphony.yaml)

All enabled in production:
```yaml
pressure:
  slice1_global_a_front: true
  slice2_meta_a_front: true
  slice3_auto_front: true
  slice4_meta_b_global_b_front: true
  slice5_omega_front: true
  slice6_deepsleep_front: true
```

---

## 12. TEST INFRASTRUCTURE

### 12.1 Dual-Runner Model

**Plain-Script (DEFAULT):**
- File IS the test -- module-level checks, top-to-bottom
- Ends with `sys.exit(0 if FAIL == 0 else 1)`
- Run: `python tests/<system>/test_x.py`

**pytest-Style:**
- `def test_*` functions or `class Test*`
- Collected by pytest; conftest AST-parses to avoid importing plain-scripts

**Aggregate Gate:** `tests/run_full_matrix.py`
- Discovers all `test_*.py` + `stress_*.py`
- Runs each in clean subprocess
- Reports pass/fail by exit code

### 12.2 pytest Configuration

**pytest.ini:**
- 1044 collected tests
- Coverage: core, pressure_loop, global_loop, meta_loop, auto_loop, omega_loop, deepsleep_loop, memory, runtime, guardian, knowledge_extractor, autonomic, hud
- Timeout: 300s per test
- Maxfail: 5 (fail-fast for regression)

### 12.3 Test Categories

| Category | Location | Coverage |
|----------|----------|----------|
| Core | `tests/core/` | Boot, episode, cognition chain, temporal, failure logs |
| Pressure | `tests/pressure_loop/` | Tick, evaluate, contributors, policy |
| Global | `tests/global_loop/` | Intent, extract, goal, outcome |
| Meta | `tests/meta_loop/` | Action, tool, memory, pack, reflection |
| AUTO | `tests/auto_loop/` | Execution, promptpack, routing, fold |
| Omega | `tests/omega_loop/` | Input, reason, reflect, package |
| DeepSleep | `tests/deepsleep_loop/` | Consolidation, mastery, identity, semantic |
| Memory | `tests/memory/` | Recall, strength, encoding, gates |
| Guardian | `tests/guardian/` | Terminal console, approval queue |
| Runtime | `tests/runtime/` | STT format, client timeout |
| HUD | `tests/hud/` | Settings, emitters |
| Cognition | `tests/cognition/` | Topic resolution (3a, 3b, 3c1, 3c2) |
| Autonomic | `tests/autonomic/` | Neuromod, cadence, strength |
| Regression | `tests/regression/` | Hardening series (slice acceptance, repair sweeps) |

### 12.4 conftest Fixtures

**tests/conftest.py (autouse):**
1. `_reset_neuromod_tonic()` -- clear tonic state between tests
2. `_reset_memory_salience_latch()` -- clear salience global between tests
3. `_isolate_telemetry_paths()` (session) -- redirect state to temp dir

**tests/core/conftest.py:**
- `_isolate_memory_root()` -- hermetic memory store per test

---

## 13. KEY ARCHITECTURAL INVARIANTS

1. **One active episode at a time** -- Pressure queues contributors until episode closes
2. **Fail-hard cognition** -- Any cascade phase failure halts the tick (operator directive)
3. **No route fork** -- Every ignition runs Global A -> Meta A -> AUTO -> Meta B -> Global B (always)
4. **No outlet layer** -- No response_hold, no ignition routing, no short-circuit close
5. **Episode is carrier** -- All data flows through episode slots; no SharedState cascade bus for episode-local data
6. **Synchronous execution** -- Cascade runs to completion before pressure resumes
7. **Window-triggered Omega** -- Omega + window DeepSleep run only when 10 episodes are closed
8. **Deterministic Meta A/B** -- NO model inference in Meta A or Meta B; planning only
9. **Model-running layers** -- Only AUTO and Omega run inference
10. **Guardian sole-writer** -- All writes under `memory/` and `guardian/` trees
11. **Atomic encoding** -- All 8 encoding steps complete or rollback fully
12. **Single-instance boot** -- One live substrate per state tree; fresh boot kills prior process

---

## 14. PERSISTENCE PATHS

| Path | Purpose | Writer |
|------|---------|--------|
| `memory/` | Cognitive substrate (JSON files, 3-tier) | guardian (atomic_write) |
| `memory/indexes/coord_index.json` | Memory ID -> coord/path map | atomic_write |
| `guardian/state/strength_index.json` | Per-memory robustness | StrengthIndex |
| `guardian/state/staging/` | Pre-promotion envelopes | promotion.create_staging |
| `guardian/state/encoding_journal.jsonl` | Post-commit encoding audit | encoding |
| `guardian/state/queues/*.jsonl` | Reflection + deepsleep drains | queues |
| `guardian/state/boot.lock` | Single-instance lock | boot_lock |
| `guardian/state/reflection_window.json` | Active reflection window | EpisodeStore |
| `guardian/state/face_anchors.json` | Face embeddings (512-dim) | anchor_store |
| `guardian/state/hud/*.json` | HUD settings | hud/settings |
| `guardian/logs/*.jsonl` | Telemetry + audit logs | emit_* functions |

---

## 15. EXTERNAL DEPENDENCIES

**Model Server:** omega-portable-lite at `http://127.0.0.1:11434`
- embedding: gemma_e4b (768 dims)
- generation: gemma_e4b
- classify: setfit_* models
- STT: whisper_small_en
- TTS: jarvis_piper
- face: face_det_10g, face_w600k_r50
- image: sd_turbo
- vision: cosmos_reason2b

**Python:** 3.13 (via .venv-brain)

**Key Packages:** fastapi, uvicorn, numpy, opencv-python, requests, pydantic, beautifulsoup4, playwright, sounddevice, pypdf, reportlab, pytest

---

*This map is a living document. If the codebase changes, this map must be updated.*
