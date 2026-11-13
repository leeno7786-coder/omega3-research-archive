# Omega 3.0 — Doctrine V2 (post-REPAIR + SLICE-1)

**Status:** operator-locked 2026-04-30
**Supersedes:** the 13-layer cycle_orchestrator description in `docs/research/CLAUDE.md` and the 6-phase brain loop description in `docs/research/The contract..txt § Loop 1`.
**Inherits:** the contract's law-zero, prime directives, deployment model, memory contract, schema contract, emergence contract, and capability delta contract — all still binding.

This is the single source of truth for Omega 3.0's current architecture as of SLICE-1 of the REPAIR-V2 pass. When in doubt, this document wins over older descriptions.

---

## The architecture in one paragraph

Omega 3.0 runs as **two cognitive doors** (BrainLoop = outward-facing operator entry; Autonomous loop = inward-facing system initiator) calling the **same voice_box model** for inference + tool dispatch. **Three deterministic supporting loops** (Global, Meta, DeepSleep) feed and consume from the two doors via shared state and persistent stores. **One reasoning loop** (Omega) uses tiny_cognitive to evolve the system — creating new tools (writes them to skills lobe + config; voice_box uses them in subsequent cycles) and proposing policy changes (operator approves). The five background loops (Auto, Global, Meta, DeepSleep, Omega) run on a **7-minute epoch heartbeat** in strict order. The Orchestrator is the shared-state holder + cycle controller; loops register with it and read upstream loops' outputs through it.

---

## The two-model architecture

| Model | Runtime | Port | Role |
|---|---|---|---|
| `voice_box` (qwen3.5-2b) | Lemonade | 13305 | The mind's voice. Used by BOTH BrainLoop (outward) AND Autonomous loop (inward). Dispatches tools. |
| `tiny_cognitive` (qwen3-1.7b) | llama-server | 49234 | Omega's reasoning model. The ONLY model that writes to system config. Creates new tools. |
| `embedding_gemma` (300M Q4_1) | FastFlowLM | 52625 | L0 substrate. Always-resident. Powers similarity / drift / contradiction detection across the system. |
| `mxbai_relevance` / `bge_reranker` | ONNX (in-process, DirectML) | n/a | Cross-encoder rerankers used by memory.api for recall arbitration. |

Three "phase-eligible" model_kinds in the registry: `voice_box`, `mxbai_relevance`, `deterministic` (a stub for phases that don't invoke models — Guardian classify, posture rendering, memory writes, autonomy feedback recording). `tiny_cognitive` is bound at the Omega-loop level, not via the phase registry (Omega has its own sequencer).

---

## The six loops

### 1. BrainLoop — outward door

**Purpose:** Operator-initiated cognition. The only loop that reaches the HUD as primary surface.

**Trigger:** One of three doors:
- `core.brain_receiver.handle_user_input(text)` — operator typed something
- `core.brain_receiver.handle_system_signal(text, kind=...)` — internal trigger
- `core.brain_receiver.handle_autonomous_initiation(curiosity)` → routes to autonomous

**Sequencer:** `brain/brainloop.py` walks 5 phase IDs in order; the 5th (Verify) only fires when `intake.source == "autonomous"`:

| Phase ID | Doctrinal role | Always runs? |
|---|---|---|
| `brain.retrieve` | Perceive + Classify + Think-read | Yes |
| `brain.translate` | Act-prep (shape structured_response) | Yes |
| `brain.respond` | Act (voice_box invocation + tool dispatch + delivery) | Yes |
| `brain.verify` | Verify (autonomy feedback channel) | autonomous only |
| `brain.store` | Think-write (memory.api.write_memory) | Yes |

**Per-cycle audit:** Each phase emits a per-phase `layer_state` record via ExecutionEngine; the brain runner emits one per-loop `layer_state` summary at completion (REPAIR-6). Result dict surfaces in `shared_state["last_brain_response"]`.

### 2. Autonomous loop — inward door

**Purpose:** System-initiated cognition. The system's "internal HUD operator." Per the contract: "the initiator, because initiation is the proof of mind."

**Trigger:** Epoch tick (slot 1). Plus cadence-driven `sweep_and_initiate(limit=1)` between epoch ticks for curiosity-driven proactive cognition.

**Sequencer:** `autonomous/autonomous_loop/loop.py` walks 8 phases:

| Phase | Description |
|---|---|
| Phase 1 — Read Meta Orders | Drains Meta's drop file into OrderQueue; peeks next order |
| Phase 2 — Build Prompt | Assembles inference payload; injects tool schemas from skills lobe |
| Phase 3 — Inference | **Calls voice_box** (Lemonade port 13305); dispatches tool_calls; Turn 2 if tools returned |
| Phase 3.5 — Dopamine | Reward prediction error |
| Phase 3.6 — Plasticity | Three-factor encoding gate |
| Phase 3.7 — Strength Index | Memory robustness snapshot |
| Phase 3.8 — Stress | HPA-axis integrator |
| Phase 4 — Persist | Writes cycle record + telemetry event with all four neuromodulator signals |

**Voice_box is the inference path** — the autonomous loop fires voice_box DIRECTLY (not via BrainLoop). When the task is operator-relevant, voice_box's output streams to the HUD, providing real-time transparency. When the task is purely-internal, output flows back into shared_state for downstream loops.

### 3. Global loop — strategic reflection

**Purpose:** Long-range reflection on telemetry trends, failure patterns, knowledge mastery. Produces goals.

**Trigger:** Epoch tick (slot 2).

**Sequencer:** `metacognition/global_loop/loop.py` — 4 phases (intake → reflection → goal_formulation → emission). All deterministic; no model invocation.

**Output:** `goals.jsonl` + journal entries (lobe 6, tagged `deep_sleep_input` + `global_reflection`). Surfaces in `shared_state["last_global_observations"]` + `shared_state["last_global_goals"]`.

### 4. Meta loop — execution planner

**Purpose:** Translates Global's goals into immutable order packages for Autonomous to execute.

**Trigger:** Epoch tick (slot 3).

**Sequencer:** `metacognition/meta_loop/loop.py` — 4 phases (intake → synthesis → assembly → dispatch). All deterministic; no model invocation.

**Output:** `meta_orders.json` (Autonomous reads on next epoch). Surfaces in `shared_state["last_meta_orders"]`.

### 5. DeepSleep loop — memory consolidation

**Purpose:** Memory restructuring + consolidation only. Per the contract: "Only Deep Sleep restructures memory."

**Trigger:** Epoch tick (slot 4) + 120s settling window after.

**Sequencer:** `metacognition/deep_sleep/loop.py` — 4 phases. **Note (SLICE-12):** the existing `phase_2_synthesis/capability_synthesizer.py` (keyword-based tool stub generation) is misplaced and gets retired. DeepSleep's role is memory consolidation, NOT tool synthesis.

**Output:** Memory consolidation records via `core/deepsleep_engine.run_consolidation_pass()` (wired in SLICE-7). The retired SharedState projection is not part of the live wire; canonical filesystem and graph state are authoritative.

### 6. Omega loop — identity + evolution + tool creation

**Purpose:** The only inside-facing loop with its own reasoning model. Evolves the system.

**Trigger:** Epoch tick (slot 5, last).

**Sequencer:** `metacognition/omega/loop.py` — 4 phases:

| Phase | Model | Description |
|---|---|---|
| Phase 1 — Intake | none | Collects telemetry trends, journal, tool registry, deep_sleep audit |
| Phase 2 — Evaluate | **tiny_cognitive** | Identifies stagnation, failure modes lacking tools, capability gaps |
| Phase 3 — Propose | **tiny_cognitive** | Generates (a) tool definitions [executable code + schema] and (b) policy proposals [config diffs] |
| Phase 4 — Commit | none | Tools → Guardian approval → write to skills lobe + config. Policies → operator approval queue |

**Voice_box uses Omega-created tools** in subsequent cycles. The flow: Omega generates a tool → Guardian approves → tool surfaces in skills lobe → voice_box reads its schema in next inference → voice_box dispatches it when its prompt requires.

**Per the contract:** "Tools yes (with Guardian); policies no (operator only)." Omega's tool outputs go through Guardian; Omega's policy outputs go through the operator approval queue (`autonomous_policy_creation_forbidden`).

---

## The 7-minute epoch heartbeat

```
[CADENCE TICK] sweep_and_initiate fires BrainLoop if pressure-triggered (independent)
  ↓
[AUTO]      run_audited_loop("auto", run_auto, ctx)               60s
[GLOBAL]    run_audited_loop("global", run_global_loop, ctx)      60s
[META]      run_audited_loop("meta", run_meta_loop, ctx)          60s
[DEEPSLEEP] run_audited_loop("deepsleep", run_deep_sleep, ctx)    60s
[IDLE]      120s deep-sleep settling window
[OMEGA]     run_audited_loop("omega", run_omega_loop, ctx)        60s
                                                            ━━━━
                                                            420s = 7min
```

`boot.py` X = 60. Per `project_seven_minute_epoch_cadence.md`. The cadence scheduler (`core/cycle_cadence.py`) fires curiosity-driven BrainLoop cycles between epoch ticks based on pressure (contradiction spike, Guardian intervention, etc.) — independent of the epoch heartbeat.

**SLICE-3 will:** convert these direct `run_audited_loop` calls to `Orchestrator.register_loop` + `Orchestrator.run_autonomy_cycle`. Same heartbeat, different dispatch — enables shared-state threading per the doctrine.

---

## Shared state (the inter-loop wire)

`core/shared_state.py:SharedState` is the data passing surface between loops. The Orchestrator holds one instance. Every loop reads upstream loops' outputs through it and writes its own output for downstream consumers.

**Canonical keys (SLICE-2 will land the schema enforcement):**

| Key | Writer | Readers |
|---|---|---|
| `last_brain_response` | brain runner (REPAIR-4) | Autonomous, Global, Omega |
| `last_auto_cycle` | autonomous loop runner | Global, Omega |
| `last_global_observations` | global runner | Meta, Omega |
| `last_global_goals` | global runner | Meta |
| `last_meta_orders` | meta runner | Autonomous (next epoch) |
| `last_omega_evaluation` | omega runner | Meta, DeepSleep |
| `last_omega_proposals` | omega runner | Operator approval queue |

---

## What's RETIRED (do not reintroduce)

The following surfaces existed in pre-REPAIR Omega 3.0 and are now gone. Any reference in older code/docs is stale; do not regenerate.

| Retired | Slice | Replaced by |
|---|---|---|
| `core/cycle_orchestrator.py` (13-layer per-input walker) | REPAIR-3 | `core/orchestrator.py` (shared state + loop registration) |
| `core/cognitive_producers.py` (meta/global/omega/guardian/action layer producers) | REPAIR-3 | Each loop owns its own cognition + emits its own `layer_state` per cycle |
| `core/memory_resolution_producer.py` | REPAIR-3 | Memory recall lives inside `brain.retrieve`, not a separate producer |
| `brain.reason` phase | build_log/0031 | Reasoning lives in memory.encoding (L0 substrate) + meta/global/omega; no dedicated brain phase |
| `brain/cli.py` + `brain/web.py` | REPAIR-4 | HUD is a separate system; brain package has no entry-point shims |
| `tests/test_orchestrator.py` (legacy) | REPAIR-10 | `tests/test_orchestrator_sharedstate.py` |
| `tests/test_intra_layer_failure.py` | REPAIR-3 | Failure Flow Law preserved per loop (REPAIR-6); no per-layer cascade test |
| `metacognition/deep_sleep/phases/phase_2_synthesis/capability_synthesizer.py` (keyword tool stubs) | SLICE-12 | Omega Phase 3 (tiny_cognitive-driven tool generation) |
| `phase_registry.yaml::autonomous_loop:` section (auto.observe/reflect/etc.) | SLICE-13 | Autonomous loop has its own sequencer; doesn't use the engine's phase registry |
| `handlers/auto.py` (scaffold handlers for auto.* phases) | SLICE-13 | Same — registry section retired |
| boot.py `taskkill llama-server.exe` shutdown block | REPAIR-9 L1 | llama-server lifecycle is external; Omega's tiny_cognitive just connects |

---

## Live status (post-REPAIR-10 + F1-F4)

**Architectural state:**
- ✓ Orchestrator + SharedState (REPAIR-1)
- ✓ BrainLoop 4/5-phase (REPAIR-2)
- ✓ Wrong-abstraction modules retired (REPAIR-3)
- ✓ Boot wiring fixed (REPAIR-4)
- ✓ Entry-points routed through Orchestrator (REPAIR-5)
- ✓ Per-loop layer_state emission (REPAIR-6)
- ✓ approve_request commit-failure bug fixed (REPAIR-7)
- ✓ Reflection/deepsleep candidacy queues drained (REPAIR-8)
- ✓ Hygiene sweep — taskkill, snapshot migration, write_guard logging, mxbai kind→role (REPAIR-9)
- ✓ Cross-slice regression sweep — 247/247 REPAIR-owned + 150/150 adjacent (REPAIR-10)
- ✓ Deterministic model_kind for non-LLM phases (F1)
- ✓ Persona regex bug fixed (F2)
- ✓ DeepSleep `_detect_patterns` defensive (F3)
- ✓ test_memory_stress + test_autonomous_loop B1 reframed (F4)

**Phase 2 / SLICE work in progress (this doc names the targets):**
- Foundation: SLICE-1 (this doctrine lock) → SLICE-2 (SharedState keys) → SLICE-3 (loop registration)
- Per-loop: SLICE-4 (Autonomous endpoint to voice_box) → SLICE-5..9
- Cross-loop: SLICE-10 (curiosity feedback) → SLICE-11 (voice_box dispatch consolidation) → SLICE-12 (Omega tool creation)
- Hygiene: SLICE-13 (phase_registry retirement)
- Verification: SLICE-14 (E2E integration test) → SLICE-15 (capability delta metric) → SLICE-16 (final readiness sign-off)

**Target completion:** 2026-05-10 (Mother's Day investor showing).

---

## How to use this document

When designing or modifying any subsystem:

1. **Check this doctrine first.** If your change conflicts with anything here, either the doctrine evolves (operator-locked update + memory + this doc) or your change is wrong.
2. **Don't trust older docs uncritically.** `docs/research/CLAUDE.md` and `docs/research/The contract..txt` are foundational but partially superseded — this doc names what changed.
3. **Cite this doc in build logs.** Every Phase 2 build log should reference DOCTRINE_V2 + the relevant memory file(s) for the doctrine sections it depends on.
4. **Memory is canonical for live decisions.** This doc is the synthesis; the individual `project_*.md` memory files are the per-decision authoritative locks. When this doc and a memory disagree, the memory wins (it's the per-decision lock; this doc is just the integrated view).
