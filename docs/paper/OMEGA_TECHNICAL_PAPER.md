# Omega: A Pressure-Driven Metacognitive Cognitive Architecture for Persistent, Sovereign, Local Intelligence

**A technical white paper**
Noah Lee · Omega Project · Draft of 2026-07-21

---

## Abstract

Contemporary AI systems built on large language models (LLMs) are, architecturally, stateless functions: each response is computed from a prompt window and discarded. Continuity is simulated by re-injecting text, "memory" is retrieval over an external index, and capability is bought almost entirely with scale — larger models, more parameters, more compute, delivered as a cloud service the user does not own. This paper describes **Omega**, a cognitive architecture that inverts those assumptions. Omega treats **memory as the substrate and inference as a replaceable tissue**; it runs entirely on a single user's local hardware with no cloud dependency for cognition; and it is engineered so that *intelligence emerges from architecture rather than from model size*. The system is organized around a single **pressure loop** that ignites discrete cognitive **episodes**; each episode flows through a fixed, orchestrator-free **executive cascade**; batches of episodes are consolidated during a biologically inspired **DeepSleep** phase and reflected upon by a windowed **Omega** metacognitive layer that maintains a long-horizon **trajectory setpoint**. Memory is deterministic: writes and recalls are routed by grammar rather than by a machine-learned classifier, so the production memory path has no inference dependency and cannot silently drift. We report a **LongMemEval score of 69.2** achieved by a *local 4-billion-parameter model on a single integrated GPU*, with an error decomposition showing that 77% of remaining error is attributable to the answer model and only 23% to retrieval — evidence that the deterministic memory architecture, not the small model, is doing the heavy lifting. We situate Omega against agent frameworks, retrieval-augmented generation, and classical cognitive architectures, and argue that persistence, determinism, and self-governance — not parameter count — are the load-bearing properties of a system that must *survive, remember, and compound capability* on constrained, user-owned hardware.

---

## 1. Introduction

### 1.1 The problem with "intelligence as a service"

The dominant paradigm in applied AI is a hosted large model accessed over a network. This paradigm has three structural consequences that are rarely stated as design decisions because they are inherited rather than chosen:

1. **Statelessness.** The model has no memory of its own. What looks like continuity is the application re-serializing prior turns into the next prompt. Nothing is *learned* between sessions; nothing persists that the model itself owns.
2. **Cloud dependency.** Cognition — reasoning, recall, identity, governance — happens on someone else's servers. The user's continuity of self, their accumulated context, and their ability to operate at all are contingent on a remote service remaining available, affordable, and unchanged.
3. **Scale as the only lever.** Capability improvements come almost entirely from making the model bigger. Architecture is thin: a prompt, a vector store, a tool-calling loop. When the wrapper is thin, the model *is* the system, and the only way up is more parameters.

For many applications this is acceptable. But it forecloses an entire class of systems: a **personal, sovereign intelligence** that a single person owns outright, that persists and compounds what it learns, that continues to function offline, and that governs its own evolution without a remote operator. Building that system requires treating the LLM not as the intelligence but as one replaceable organ inside a larger cognitive architecture.

### 1.2 Thesis

Omega is built on a single load-bearing claim:

> **Intelligence emerges from architecture, not model size.** The model is inference tissue. The system is cognition. Memory is substrate. Policy is behavior. Verification is truth. Constraint creates architecture; architecture creates cognition; scale only amplifies architecture — it never replaces it.

From this follow the two design inversions that define the system:

- **Memory outranks inference.** All cognition begins with memory. When the model's output conflicts with verified memory, memory wins. The memory system is engineered to be *more reliable than the model*, because on constrained local hardware there is no larger model to fall back on when the small one confabulates.
- **Persistence over performance (Law Zero).** A temporarily brilliant system that cannot persist is a failure; a slower system that survives, improves, and compounds is intelligence. Recoverability, inspectability, reproducibility, and identity continuity are prioritized over speed, elegance, and impressive one-shot demonstrations.

### 1.3 Deployment stance: sovereign by construction

Omega is a **per-user, offline-first, portable, single-user-owned** product — an architectural fact that shapes every other decision, not a deployment afterthought:

- **Per-user.** One instance per user. No multi-tenancy, no shared cognition. Each instance has exactly one identity and one user it serves.
- **Offline-first.** All cognition runs locally. External tools (e.g., web fetch) are read-only conveniences for grounding, never load-bearing for reasoning, recall, identity, or governance. An offline instance remains fully functional for everything that does not literally require external data.
- **Portable.** Instance + memory + identity + constitutional rules + model weights live together on the user's machine. Backup is copying a directory; migration is copying it elsewhere. No licensing servers, no activation calls, no external state.
- **Single-user-owned.** No remote operator, no backdoor, no telemetry phoning home, no cloud-gated features. The user is the sole authority over what their instance does, remembers, and when it stops.

This stance is *why* the rest of the architecture reads the way it does. It is why a governance subsystem (Guardian) exists at all — in an offline, single-owner deployment there is no remote administrator, and the user themselves is the slow drift vector. It is why constitutional rules are append-only and why every persistent object carries a schema, provenance, and a decay policy: the user must be able to inspect, recover, and roll back without external tooling.

### 1.4 Contributions

This paper describes an implemented system (~564 first-party Python modules; ~35,000 lines in the memory subsystem alone; 636 test files defining 4,174 test functions). Its principal architectural contributions are:

1. **Pressure-driven episode ignition** in which cognition is triggered by weighted internal drives and operator input passing a competitive gate — including an *epistemic-emptiness* drive by which a knowledge-empty substrate experiences its own emptiness *as* pressure, so curiosity is endogenous rather than an implanted goal (§3.2).
2. **An orchestrator-free executive cascade** in which each cognitive episode is a single carrier object passed through a fixed sequence of phases, curing the race conditions and schema drift endemic to shared-state orchestrator loops (§3.3).
3. **Windowed trajectory-setpoint steering**, in which a metacognitive layer owns a persistent long-horizon setpoint and steers the system toward it by modulating future pressure, rather than rewriting declared goals to match observed behavior (§3.4).
4. **A biologically inspired DeepSleep consolidation phase** that promotes durable products of experience — mastery, identity traits, semantic facts, failure patterns, trajectory — from working experience into the long-term memory cortex, closing the learn-consolidate-recall loop (§3.5).
5. **Deterministic, two-organ memory** in which the filesystem is truth and a derived graph/matrix engine is an advisory recall index; write and recall routing is grammar-seeded rather than classifier-driven, so the production memory path has no inference dependency (§3.6).

We report evaluation results (§4), relate the design to prior work (§5), and discuss its limits honestly (§6).

---

## 2. Design principles

Omega is governed by an explicit written contract that is **bidirectional**: the builder must satisfy it, *and the running system enforces it on itself* through schema validation, policy gates, and governance checks. The design question for any feature is never "how do I as the developer respect this rule?" but "how does the system enforce this rule at runtime?" A rule that only the developer remembers is considered unimplemented.

**The Five Prime Directives.**

1. **Memory First.** No response, plan, reflection, or action occurs before memory routing determines the relevant context. Blind context-stuffing and full-memory walks are forbidden.
2. **Deterministic over Magical.** Every decision must be explainable, inspectable, and reproducible: logs, telemetry, causal traceability, validation before persistence, explicit failure states. Emergence is allowed; chaos is not.
3. **Modular Sovereignty.** Every subsystem is independently replaceable. *If replacing one model breaks the mind, the architecture is wrong.*
4. **Failure is Signal.** Failures are retained as learning signal, not suppressed. Contradiction retention and negative-reinforcement pathways are first-class.
5. **Governance before Autonomy.** No subsystem rewrites itself directly. The only sanctioned path is **propose → simulate → verify → commit**; never observe → rewrite.

**Capability delta — the only metric.** Omega does not measure progress by answer quality, verbosity, confidence, or "feeling alive." It measures one thing: *what can the system do now that it could not do before, and does that improvement create the capacity for future improvement?* A better answer is not capability delta; a newly synthesized retrieval policy that permanently improves future reasoning is. This is how a constrained system is supposed to beat a large one — by **compounding** architecture, not by renting scale.

---

## 3. System architecture

### 3.1 Overview and boot lifecycle

Omega boots through an ordered lifecycle (`boot.py` → `core/boot_lifecycle.py`): acquire a boot lock, restore substrate state from a durable snapshot, establish connectivity to the local inference runtime, warm the memory indices (coordinate index, strength index, graph compile-on-boot), spawn the operator interface (a local HUD), and finally start the **pressure loop**. Teardown reverses the order.

Two failure postures are deliberately different:

- **Substrate persistence is fail-soft.** If snapshot restore fails, boot continues on a degraded substrate rather than refusing to start.
- **Cognition is fail-hard.** A failure inside a cognitive phase halts the tick rather than continuing on a corrupted or half-written mental state. The operative principle is that *a mind running on poison must stop, not improvise* — silent cognitive corruption is worse than a crash you can inspect and recover from.

Critically, there is **no central orchestrator**. The system intentionally omits the orchestrator loop, tick-scheduler, and route-selector layers common to agent frameworks. Instead, *the pressure loop is the sole driver*: pressure ignites, the executive cascade runs, the episode closes.

### 3.2 The pressure loop and episode ignition

The pressure loop runs `gather → evaluate → (optionally) ignite` on a fixed interval (a preferred embodiment uses ~15 s). It is the only "forever loop" in the system and the only thing that can start an episode.

**Contributors.** Each tick gathers weighted *contributors* from a pressure-field registry — internal drives and operator input. Canonical contributors include curiosity gap, operator command, operator query, learning debt, execution gap, idle-probe aggregate, and DeepSleep-staged trajectory-gap reseeds. One contributor is foundational to the system's theory of motivation:

> **Epistemic emptiness.** A brand-new, no-knowledge substrate is designed to experience its own emptiness *as pressure* — the *want-to-understand is the engine, not an implanted goal.* The contributor reads the count of verified memory records; pressure is high when the substrate is empty and *decays as the system accumulates verified memory.* Drive precedes goals: the system is curious before it is told to be, and its curiosity is quantitatively grounded in how little it currently knows.

This is the architectural answer to a hard question in autonomous agents — *where does intrinsic motivation come from without hand-coding goals?* In Omega it comes from the gap between the empty substrate and a full one.

**Neuromodulation.** Before the gate, contributor weights are modulated by tonic neuromodulator levels — dopamine (vigor/spread), serotonin (patience, raising the ignition ceiling and margin), and stress. This couples the *threshold to act* to the system's internal state rather than to a fixed constant, and it is computed by an explicit reward-prediction-error formula, not narrated.

**The hybrid gate.** For autonomous competition, a dominant contributor must both reach an effective **ceiling** and exceed the runner-up by an effective **margin**. Summed partial pressure does not open an episode — many small urges do not add up to action; one urgency must dominate. This prevents the system from thrashing on diffuse low-grade pressure.

**Operator supremacy.** When any contributor is operator-sourced, the operator input is forced to dominance, bypassing autonomous margin competition. The user is always heard, immediately, regardless of what the system currently wants to do. Operator inputs arriving while an episode is already active are enqueued in a durable FIFO and consumed exactly one per cycle, guaranteeing one episode per message in arrival order.

### 3.3 The episode carrier and the executive cascade

At most **one active episode** exists at a time, and **closure precedes the next ignition**. This single invariant eliminates an entire class of concurrency bugs.

An episode is a **carrier object** (`CognitiveEpisode`): a structured record whose slots are written, in order, by the phases of the cascade. Rather than passing episode-local data through a shared mutable "last-result" bus — the design that produces schema drift and race conditions in orchestrator architectures — each phase writes into its own slot of the one converging truth object.

On ignition, the cognition chain (`cognition_chain.py`, ~3,150 lines) runs a **fixed-order cascade with no route fork**:

| Phase | Role | Inference? |
|-------|------|:----------:|
| **Global A** | Parse input into an `IntentDecision` (discrete intent label + confidence + reason). No open-ended goals. | No |
| **Meta A** | Deterministically produce an `ExecutionActionPack`: tool selection, memory operations, directives. | No |
| **AUTO** | Validate the pack, recall memory via the deterministic walker, build the prompt, dispatch tools and **model inference**, fold results into an outcome pack. | **Yes** |
| **Meta B** | Deterministic reflection on what execution actually did. | No |
| **Global B** | Package an `EpisodeOutcome` (intent + trajectory alignment + divergence + execution outcome) — *without* rewriting the goal to match behavior. | No |
| **Journal + close** | Per-episode DeepSleep journal (mastery/failure/semantic facets) then episode close. | No |

Only **two** of the phases run a language model: AUTO (execution) and, at the window level, Omega (reflection, §3.4). Everything else — intent classification, planning, reflection, outcome packaging, consolidation — is **deterministic code**. This is the concrete meaning of "the model is one organ": most of cognition is architecture, and the model is invoked only where genuine open-ended generation is required. Individual phase "fronts" can be disabled by configuration without changing the cascade order, satisfying Modular Sovereignty.

### 3.4 Windowed reflection and trajectory-setpoint steering

Per-episode cognition is myopic by design. Long-horizon coherence is the job of a separate metacognitive layer that runs *over batches of episodes*.

Completed episodes accumulate into a **reflection window** (preferred size N = 10). On the Nth episode the window **locks**, and the **Omega** layer runs — the second and only other model-running layer. Omega performs a 5W1H reflection over the locked window (what happened, why, who the system is becoming) and, crucially, updates a **trajectory setpoint**.

The trajectory model is the system's theory of *self-direction over time*, and it is deliberately built as a control-systems setpoint rather than a mutable goal:

- The trajectory is a **setpoint owned by Omega**; pressure is the **steering mechanism**. Omega does **not** rewrite the trajectory to match what the system has been doing. It emits *pressure feedback* to steer the system back toward the setpoint.
- Each window Omega emits a decision ∈ {**establish, idle, alter, abandon**}. `idle` re-persists the setpoint unchanged; `alter` shifts the target in place; `abandon` supersedes the old trajectory into a history record while preserving the old→new identity chain (no orphaned trajectories).
- The observed relationship between behavior and setpoint is reported as a purely *descriptive* diff — ESTABLISHING, FOLLOWING, PARTIAL_FOLLOWING, DRIFTING — which is telemetry, not a verdict, and never itself edits the goal.

This separation — *setpoint ownership distinct from local outcome packaging* — is what enables stable long-horizon steering. A system that edits its goals to match its drift will always appear "on track" and will never correct. Omega instead keeps a fixed target and raises learning pressure toward it.

### 3.5 DeepSleep: biological consolidation

After Omega reflects on a locked window, **DeepSleep** runs — a fully deterministic consolidation phase (no model inference, no randomness) that plays the role sleep plays in biological memory: it takes the transient products of waking experience and commits the durable ones to long-term storage, while letting the rest decay.

DeepSleep runs at two grains: a **truncated per-episode** chain after every episode close (mastery, failure, semantic, consolidate) so that window-level mastery statistics are accurate, and a **full window-level** chain (13 phases) after Omega. The window chain, in order, is: intake → mastery → identity → semantic → trajectory → failure → query-lifecycle → consolidate → projection → **consolidate-memory** → memory-integrity → reseed → closure. Salient phases:

- **Mastery (grounded, not incremented).** Domain mastery values are *set absolutely* from Omega's per-episode grounded estimate (what was recalled and produced versus what was targeted), not bumped by a free counter. An **Application Credit Law** requires at least one successful episode in the window before any mastery is credited — the system cannot claim competence it did not demonstrate. Mastery may rise *or fall*.
- **Identity.** Repeated behaviors and stable patterns observed by Omega increment identity *traits* — but under an **Identity Safety Law**, traits are communication-only: they are projected into prompts to shape voice, and can *never* directly produce actions. The system may come to describe itself differently; it cannot rewrite what it is permitted to do by narrating a new self.
- **Trajectory, failure, and open-query lifecycles** consolidate the window's steering target, its typed failure patterns (classified as tool / planning / response), and its outstanding questions, each into its own typed, schema-valid store.
- **The closed memory loop.** The `consolidate-memory` phase promotes durable products into the single memory cortex via atomic writes: journal → journal lobe, identity → identity lobe, mastery/semantic → knowledge lobe, resolved queries → reasoning lobe. AUTO recall later reads *the same cortex* DeepSleep writes into — one closed loop of write → index → read.
- **Reseed.** DeepSleep computes the *gaps* the window revealed — a mastery target not yet reached, an application skill lagging its knowledge, an unresolved question — and stages them as **pressure contributors for the next cycle.** This is how a completed sleep phase *causes the next waking episode*: consolidation does not just store the past, it seeds the future.

DeepSleep is **fail-hard**: a corrupt or unwritten continuity store halts the cascade rather than letting the mind continue on poisoned state, with the sole exception that a not-yet-existing store on first boot reads as empty (absence is not failure).

The biological framing here is a conceptual analogy, deliberately: consolidation, salience-weighted encoding, decay/forgetting, replay, and homeostatic reseeding are realized across DeepSleep, the neuromodulation subsystem, and the strength index — not as a single named module, but as a set of mechanisms that together give the system a genuine sleep-consolidate-wake rhythm.

### 3.6 Memory: the two-organ doctrine and deterministic routing

Memory is the substrate the whole architecture rests on, and it is built to a strict **two-organ doctrine**:

- **The filesystem is truth (FS = TRUTH).** Long-term memory is a structured cortex of schema-valid JSON records laid out across **12 "lobes"** — identity, personas, knowledge, reasoning, situational, tools, vision, coding, temporal, workflows, journal, and a development lobe — each addressed by a **3D "Dewey" coordinate** `(lobe, subdomain, depth)`. Truth is read directly from the filesystem, never from the recall organs.
- **The graph is recall (graph = RECALL).** A derived graph and a matrix "Dewey Mapping Engine" (**DME**) provide fast associative retrieval — but they are *advisory*. Every hit they produce is re-verified against the filesystem before it is trusted. A recall index that becomes authority is an error; the derived organ can be deleted and rebuilt from truth at any time.

The property that makes this reliable is **determinism on the production path.** Memory placement and recall scope are chosen by **grammar-seeded routing**, not by a machine-learned classifier:

- **Write-Y == Read-Y by construction.** The subdomain a fact is written to is computed by the same grammar (`subdomain_for(text, lobe)`) that computes where it will be recalled from — pronoun rules for identity/persona lobes, longest-keyword term-matching for knowledge/coding/workflow lobes. Because write and read use the *same deterministic function*, a fact cannot be filed where recall will never look.
- **Undecidable content is held, not guessed.** Static-lobe content the grammar cannot confidently route is sent to a reserved `_unsorted` coordinate with an `undecided` provenance flag, rather than being misfiled by a low-confidence guess.
- **No inference in the read path.** Machine-learned classifiers and embedding services are *offline audit tools only*; the production ingest/recall path fails loud if a classifier is invoked. This is what lets the memory system keep working when the model runtime is degraded or down — recall does not depend on inference at all.
- **Atomic, schema-validated writes.** Every write is `validate → temp file → fsync → atomic rename`, with the coordinate index updated atomically alongside the document. There is no freeform persistence: every stored object carries source, confidence, timestamp, validation status, and decay policy. A **strength index** models each memory as an engram with its own decay constant, so memories consolidate, strengthen with reinforcement, and fade without it — and contradictions are adjudicated during consolidation with confidence penalties and optional demotion.

The memory contract's cardinal rule follows from all of this: **memory always outranks inference.** When fluent model output conflicts with validated memory, the validated structure wins.

### 3.7 Guardian: governance without a remote administrator

Because Omega is single-user and offline, there is no external administrator to catch drift or reject a poisoned input. That role is filled by **Guardian**, an internal governance authority. Its scope is precise and, importantly, *narrow*:

- **Guardian gates external, untrusted ingress** — user chat and connector/workspace observations — and returns one of ALLOW / FLAG / QUARANTINE / REJECT. It is the immune boundary against outside poisoning of memory.
- **Cognitive self-writes are direct.** The system's own consolidated learning is written straight into memory; Guardian does *not* sit in the internal memory path (that would make cognition depend on a gate on every thought).
- **Guardian is the sole writer of shared runtime state and mind checkpoints**, giving those a single auditable authority and enabling recovery/rollback.

This division embodies Governance-before-Autonomy at exactly the right boundary: the outside world is untrusted and gated; the system's own verified cognition is trusted but auditable; and self-modification of policy still routes through propose → verify → commit rather than direct rewrite.

### 3.8 The model fleet and local runtime

Omega uses **many small specialist models**, orchestrated by the architecture, rather than one large generalist. The inference substrate is `llama.cpp` (GGUF quantized models) exposed through a local HTTP gateway (preferred port 11434), complemented by ONNX runtimes (CPU and, where available, an NPU) for embeddings, face processing, and the DME recall engine, and a ggml image-generation engine. A **capability broker** preflights the runtime before AUTO or Omega calls a model — returning *proceed*, *response-hold* (a preflight deferral, not a routing change), or *alternate-capability* — so that runtime health is checked in one place rather than duplicated inside every tool.

The fleet includes, among ~20 seats: a resident 4B cognition/chat model (Nemotron-3 Nano) as the always-on mind; a Gemma-4 E4B model used as the long-context answer seat for memory benchmarks; a proven 4B tool-caller (Unsloth Qwen3.5-4B, 262k context) driving the browser agent, coding, and voice; a SetFit chat-intent classifier; Whisper-v3-turbo for speech-to-text; Kokoro for text-to-speech; a FLUX image-generation model; a face-recognition pipeline; and a multimodal reasoning model for vision. Concurrency is capped (default 4 resident models) with idle eviction. No model exceeds consumer-hardware reach; the entire system is designed to run on a single user's machine, including on an integrated GPU.

---

## 4. Evaluation

### 4.1 Long-horizon memory: LongMemEval

The headline evaluation is **LongMemEval**, a benchmark for long-horizon conversational memory (temporal reasoning, multi-session recall, knowledge updates, preference retention, and abstention), judged by an external frontier model as grader.

Omega's memory architecture, with a **local Gemma-4 E4B (4-bit, 64k context) answer model running on a single AMD 860M integrated GPU**, scores:

| Metric | Score |
|---|---|
| **Overall (6-type average)** | **69.2** |
| single-session-user | 92.9 |
| single-session-assistant | 80.4 |
| knowledge-update | 71.8 |
| temporal-reasoning | 70.7 |
| multi-session | 54.1 |
| single-session-preference | 46.7 |
| abstention | 27 / 30 |

The result to weigh is not the absolute number but *what produces it*. There is **no frontier model in the answer seat** — a 4B local model does the reasoning. The score is carried by the deterministic memory architecture feeding that small model the right context.

This is confirmed by an **error decomposition** of the remaining failures: **77% are attributable to the answer layer (the 4B model) and only 23% to retrieval.** In other words, the deterministic memory system is *not the bottleneck* — retrieval misses are the minority of errors, and the ceiling is set by the size of the local answer model, exactly as the architectural thesis predicts. Swapping in a larger answer model (which Modular Sovereignty makes a configuration change, not a rewrite) would be expected to close most of the remaining gap without any change to the memory architecture.

*Number-hygiene note.* We deliberately do **not** claim figures that are sometimes associated with this project: a ~95% figure circulating publicly is an unrelated external party's task-averaged claim (its own disclosure is 93.2% raw); a ~97% figure is an internal stretch *target* from a design document, not a measured result; and an internal "100%" scorecard refers to a *different*, small, deterministic ingest/recall proof harness, not LongMemEval. The honest, reproducible number for the local-4B configuration is **69.2**, with the benchmark campaign paused by design to prioritize completing the build.

### 4.2 Determinism and reproducibility

Because the memory production path is classifier-free and the majority of the cascade is deterministic code, most of Omega's behavior is **reproducible by construction**: the same input routed through the same grammar produces the same memory placement and recall scope every time. This is validated by a large regression suite — **636 test files defining 4,174 test functions** — covering routing invariants (write-Y == read-Y), atomic-write integrity, cascade ordering, DeepSleep phase behavior, and governance boundaries. (We report suite *size* as a measure of engineering scope; a full pass-rate requires a live run and is not asserted here.)

### 4.3 Scale

The implemented system is substantial: ~564 first-party Python modules, with the memory subsystem alone at ~35,000 lines and the pressure/episode subsystem at ~19,000. The cognition cascade is a single ~3,150-line orchestrating module. This is reported to make one point: Omega is an *engineered system*, not a prompt-and-vector-store wrapper. The intelligence budget is spent on architecture.

---

## 5. Related work

**Agent frameworks and orchestrator loops.** Contemporary autonomous-agent frameworks typically employ a central orchestrator that polls a task queue, selects a route, and dispatches sub-agents through a shared mutable state bus. This conflates episode-local data with global guidance and produces race conditions and cross-layer schema drift. Omega removes the orchestrator entirely: a single pressure loop ignites, and each episode is a carrier object passed through a fixed cascade with no route fork. Guidance state that must persist across episodes (the trajectory setpoint) is an explicit carve-out, not a bus key.

**Retrieval-augmented generation (RAG).** RAG augments a stateless model with embedding-based retrieval over an external index — retrieval is a machine-learned similarity search, and placement/recall depend on an embedding model at inference time. Omega's memory is the inverse in three ways: the filesystem, not a vector store, is the source of truth; routing is deterministic grammar, not learned similarity, so the production path has *no* inference dependency; and memory is not a passive index but an active substrate with strength, decay, contradiction adjudication, and consolidation. Embedding-based recall exists in Omega (the DME organ) but is advisory and verified against truth, never authoritative.

**Classical cognitive architectures.** Symbolic cognitive architectures such as SOAR and ACT-R model cognition as production systems over declarative and procedural memory with sub-symbolic activation. Omega inherits their conviction that *architecture is the seat of cognition* and echoes ACT-R's activation/decay in its strength index, but differs in substrate: it integrates modern LLM inference as a bounded organ inside a deterministic scaffold, targets a single-user sovereign deployment, and adds a control-theoretic trajectory setpoint and a pressure-driven, neuromodulated ignition model rather than a fixed decision cycle.

**Memory-augmented and continual-learning LLMs.** Recent work adds episodic memory or scratchpads to LLMs, and studies sleep-like replay and consolidation for continual learning. Omega operationalizes these ideas as first-class subsystems: DeepSleep is an explicit consolidation phase that promotes typed products (mastery, identity, semantic, trajectory, failure) into long-term storage and *reseeds* future motivation, and neuromodulation modulates encoding strength per cycle. The distinguishing commitment is that these mechanisms are **deterministic, schema-validated, and inspectable**, not emergent side effects of a training procedure.

**Sovereignty and local-first AI.** Against the prevailing hosted-service model, Omega belongs to a small but growing local-first tradition. Its contribution to that tradition is to show that the local-first constraint is not merely a privacy preference but a *forcing function for better architecture*: because there is no cloud to fall back on, memory must be reliable, cognition must be deterministic, and governance must be internal — precisely the properties that make the system compound.

---

## 6. Discussion and limitations

We state the limits plainly, because an honest account is more persuasive than a polished one.

- **The answer-layer ceiling is real.** With a 4B local answer model, the reasoning quality on the hardest multi-session and preference tasks is bounded (§4.1). The architecture localizes this limit to a single replaceable organ, but it does not remove it; a larger local model is the expected path to close it.
- **Single-user by design, not by limitation of ambition.** Omega deliberately forgoes multi-tenancy. This is a positioning choice — sovereignty over scale — but it does mean the architecture is not, and is not trying to be, a hosted service.
- **The benchmark campaign is paused.** The 69.2 figure is the last measured result before the operator prioritized finishing the build over chasing benchmark points. It is honest and reproducible, but it is a snapshot, not a fully optimized ceiling.
- **A sovereign inference engine is aspirational.** A from-scratch C++ inference engine is described in the roadmap but is *not* the live substrate; today the runtime is `llama.cpp` plus ONNX. We flag this so the roadmap is not mistaken for the current system.
- **Documentation drift is a maintenance cost of a fast-moving architecture.** The system has been through at least one major architectural consolidation (a prior multi-loop "Symphony" design was retired in favor of the single-cascade design described here); some older design documents still describe the retired structure. The live code, the boot path, and the patent specification are the authoritative description; this paper is written to the as-built system.

None of these undercut the central claim. They sharpen it: the load-bearing properties are architectural, the one place scale still matters is quarantined to a swappable organ, and everything the system persists, it persists in an inspectable, recoverable form.

---

## 7. Conclusion

Omega is a wager that the field's default assumptions are backwards for an entire class of systems. If you want a disposable oracle, a large hosted model is the right tool. But if you want a **personal intelligence that a single person owns, that persists and compounds what it learns, that keeps working offline, and that governs its own evolution**, then statelessness, cloud dependency, and scale-as-the-only-lever are not features to inherit — they are the problem to solve.

Omega solves it by making **memory the substrate and inference a replaceable organ**; by driving cognition from **internal pressure** — including the pressure of its own emptiness — rather than from a polling orchestrator; by consolidating experience during a deterministic **sleep** phase that both stores the past and seeds the future; by steering itself over the long horizon with a **trajectory setpoint** it does not allow itself to fudge; and by routing memory **deterministically**, so the one thing a mind cannot afford to have fail — its memory — does not depend on the one thing that can. The result, demonstrated on a 4-billion-parameter model on a single integrated GPU, is that a small model inside the right architecture reasons over long horizons far better than its size would predict — because, as the contract puts it, **intelligence emerges from architecture, not model size.**

---

## Appendix A — Figure references

The system is documented with eight formal figures (see `docs/patent/drawings/`): (1) system overview; (2) pressure tick and ignition flow; (3) the executive cognition cascade; (4) the reflection-window lifecycle; (5) the trajectory-setpoint steering loop; (6) deterministic memory routing; (7) the episode-carrier data model; (8) the runtime capability broker.

## Appendix B — Implementation reference

| Function | Module |
|----------|--------|
| Boot lifecycle | `boot.py`, `core/boot_lifecycle.py` |
| Pressure tick / ignition | `pressure_loop/loop.py`, `pressure_loop/pressure.py`, `pressure_loop/pressure_field.py` |
| Episode store / carrier | `pressure_loop/episode.py` |
| Executive cascade | `cognition_chain.py` |
| Reflection window | `pressure_loop/reflection_window.py` |
| Omega reflection | `omega_loop/` |
| DeepSleep consolidation | `deepsleep_loop/loop.py` (+ per-phase sub-packages) |
| Memory cortex / routing | `memory/dewey_map.py`, `memory/engine/walker.py`, `auto_loop/mril_tools/subdomain_helpers.py` |
| Recall engine (DME) | `memory/dme/` |
| Governance | `guardian/decision.py`, `guardian/external_ingress.py`, `guardian/state_persistence.py` |
| Capability broker | `runtime/runtime_capability_broker.py` |
| Model fleet config | `F:\omega-runtime\config\omega3_models.json` |

---

*This paper describes the as-built Omega 3.0 system as of 2026-07-21. It is written to the live code and the provisional patent specification; where older design documents conflict, the live code governs.*
