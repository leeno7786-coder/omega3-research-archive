# DME-Native Associative Memory for Omega 3.0

## Removing the Neural Embedding Model by Turning Dewey Memory, Plasticity, and the XDNA NPU into the Recall Substrate

**Status:** Research architecture / implementation handoff  
**Date:** 2026-08-13  
**Project:** Omega 3.0  
**Primary system:** DME v2 + canonical Dewey filesystem + taxonomy graph + Symphony neuromodulation + StrengthIndex + DeepSleep + XDNA NPU  
**Review target:** Fable 5 in the live Linux Omega 3.0 checkout

---

## Abstract

Omega 3.0 currently contains two different semantic-memory philosophies. The first is native to Omega: canonical memories live in a deterministic Dewey filesystem, the disposable graph expresses explicit relationships, DME converts canonical memory into wide sparse coordinate bitmaps, the Thought Engine performs graded direct and associative hop scoring, StrengthIndex tracks engram robustness, Symphony supplies neuromodulatory state, and DeepSleep owns consolidation and forgetting. The second philosophy is conventional neural retrieval: a separate embedding model converts text into dense vectors that are used to construct semantic-similarity edges and to admit semantically related memories when surface vocabulary does not overlap.

This paper proposes removing the second philosophy rather than optimizing it. The target is not a smaller embedding model, a different vector database, or a lexical substitute. The target is to make DME itself the semantic recall substrate.

The central hypothesis is that Omega already possesses the major mechanisms necessary for embedding-free associative recall: stable sparse coordinates, a graph with typed relationships, whole-store parallel overlap scoring, strong and weak hop planes, salience, decay, reconsolidation, outcome-conditioned reinforcement, plasticity, novelty, replay, verification, and canonical filesystem truth. The missing link is to make DME associations plastic and to preserve an eligibility trace describing which memory pathways participated in a cognitive outcome. Existing Symphony signals can then strengthen or weaken those pathways. Semantic structure is learned as changing connectivity and excitability among stable Dewey coordinates, not as position in a dense vector space.

The Linux execution environment raises the ceiling further. Omega now has direct access to the AMD XDNA NPU through Vitis AI as well as the IRON / MLIR-AIE toolchain. DME's current NPU backend has already demonstrated that a carefully shaped binary overlap model can be placed on XDNA and that content-addressed tile-specific VAIP caches can avoid recompiling unchanged memory planes. With IRON, the longer-term design can separate the compiled DME program from mutable memory topology, allowing coordinates, hop planes, plasticity classes, and dirty-tile updates to move as runtime data through persistent AIE workers rather than requiring a neural-model compilation cycle.

The result would be a memory system in which the filesystem remains authoritative, DME acts as a hippocampal-style sparse index and pattern-completion engine, the graph acts as synaptic topology, Symphony modulates plasticity, StrengthIndex controls engram and association robustness, DeepSleep consolidates and prunes the topology, and the NPU executes recall and incremental graph maintenance. The language model remains a language and reasoning organ, but it no longer owns memory geometry.

---

# 1. Research Objective

The immediate objective is to determine whether Omega 3.0 can remove Gemma/EmbeddingGemma from the memory pipeline without sacrificing the semantic recall gains that the dense-embedding lane currently provides.

The desired final property is stronger than "no vector database." It is:

> **No learned neural embedding model is required for memory ingestion, semantic admission, graph construction, recall, identity-state comparison, or DeepSleep projection maintenance.**

DME, the Dewey filesystem, the taxonomy graph, and Omega's existing adaptive state must together supply the semantic behavior.

The design must preserve the existing Omega invariants:

1. Canonical filesystem memory is truth.
2. DME and the graph are disposable, rebuildable projections.
3. Recall candidates are not authority.
4. RecallVerifier remains the admission boundary.
5. No learned mechanism may silently manufacture a fact.
6. Deterministic rebuild from the same canonical state must remain possible.
7. Plasticity may change accessibility and association, but not historical provenance.
8. A failed accelerated path must degrade honestly rather than claim NPU execution.

---

# 2. Current Omega Memory Substrate

## 2.1 Canonical memory is already independent of embeddings

`memory/dme/adapter.py` states the governing doctrine directly: Dewey/filesystem is truth and the graph is a disposable scoring projection. DME seats are rebuilt preferentially from `coord_index.json` and canonical filesystem records, with graph records used as fallback. This is important because the proposed architecture does not need to redefine what a memory is. It changes how memories become findable.

The canonical record retains identity, coordinate, title, summary, domain, subtopic, tags, source identifiers, session provenance, status, supersession state, and other durable facts. Embedding data is therefore derivative state, not the source of the memory itself.

## 2.2 DME already has a large sparse coordinate field

`memory/dme/lobe_layout.py` defines DME entries as 4,096 `uint64` words: 262,144 total bits. Three words are headers; the remaining payload is divided across twelve Dewey lobes. Page 0 contains 21,824 hot coordinate slots per lobe, with paged growth handled by the coordinate registry.

This matters because DME is not a small hash filter. It is a very wide sparse distributed representation whose bits correspond to stable semantic/categorical addresses minted from Omega's own memory taxonomy.

`memory/dme/geometry.py` explicitly declares `uses_embeddings = False`. The DME scoring contract is therefore already architecturally independent of the embedding model.

## 2.3 DME already performs associative pattern completion

`memory/dme/hop_engine.py` restores graded union scoring and constructs graph-edge hop planes. Plane 0 represents a memory's own coordinate bitmap. Hop planes contain the union of coordinate bitmaps reachable through graph neighbors. One matrix operation can therefore score every memory's neighborhood against a query in parallel.

The live Thought Engine already distinguishes evidence classes:

- direct match;
- graded union/direct partial match;
- strong one-hop;
- weak one-hop;
- strong deeper-hop reach;
- weak deeper-hop reach.

Strong edges include explicit compiler-proven relations such as `MENTIONS`, `SUPERSEDES`, `CONTRADICTS`, `REFINES`, `ANSWERS`, `SUPPORTS`, `DERIVED_FROM`, `DEPENDS_ON`, `CAUSED_BY`, `PRODUCED`, `USED_TOOL`, `FAILED_BECAUSE`, `OCCURRED_IN`, and `OBSERVED_IN`. Weak edges include placement/co-occurrence relationships such as `CO_LOCATED`, `SHARES_DOMAIN`, `SHARES_TAG`, and the current embedding-generated `SEMANTIC_SIMILARITY` relation.

The engine already penalizes generic high-document-frequency bits, applies authority bonuses, supports target-lobe bias, and uses adaptive hop depth. These are not placeholder concepts; they are active mechanisms that should be preserved.

## 2.4 Omega already has engram strength, salience, decay, and reconsolidation

`memory/strength_index.py` is a persistent per-memory engram robustness system. It tracks:

- write strength;
- touch count;
- ingest time;
- last access;
- encode-time decay constant;
- below-floor cycles;
- outcome-conditioned reinforcement.

Its current strength function combines initial write strength, time decay, a floor, and saturating recall-touch reinforcement. `touch()` makes successful retrieval a reconsolidation event. `reinforce()` changes `write_strength` positively or negatively after an outcome. DeepSleep can authoritatively reduce write strength through `set_write_strength()`.

Critically, registration samples ambient salience from Symphony. A salient memory can receive both higher initial strength and a modified retention time constant.

## 2.5 Omega already has neuromodulated plasticity

`autonomic/symphony_neuromod.py` produces dopamine, plasticity, stress, serotonin, and strength from verified episode outcomes and learned expectations. It also computes memory salience as encoding-scale and tau-factor modulation.

`config/plasticity.yaml` explicitly describes Omega's plasticity as a three-factor encoding gate grounded in pre/post activity plus neuromodulator presence. The current configuration combines real substrate, dopamine-gate magnitude, novelty, and an inverted-U stress modulator.

`autonomic/neuromod_reinforce.py` closes part of the loop: memories that actually participated in a recall can be strengthened or weakened according to dopamine reward-prediction error, scaled by recall relevance. Neutral/predicted outcomes do not reassign credit. This is already the correct conceptual form for pathway learning.

## 2.6 Dense embeddings remain load-bearing in several places

The present system is therefore hybrid rather than embedding-free.

The major remaining dense-vector dependencies are:

1. `memory/encoding.py`: embedding generation is currently treated as required atomic memory encoding for semantic records.
2. `memory/semantic_admission.py`: query embeddings are used to retrieve records with no lexical overlap and then expand over semantic-similarity edges.
3. `memory/taxonomy/compiler.py`: `SEMANTIC_SIMILARITY` edges are constructed from dense embedding cosine similarity.
4. `memory/self_vector.py`: identity state is represented as a confidence-weighted mean of dense identity vectors.
5. `deepsleep_loop/loop.py`: pending embedding backfill is drained before taxonomy reindex.

The performance motivation to remove this path is substantial. Current `memory/local_embed.py` documents a measured case where embedding consumed 333.3 seconds of a 350.1-second projection rebuild, approximately 95.2% of wall-clock time.

---

# 3. Biological Design Basis

This architecture is biologically inspired, not biologically claimed. The purpose of the neuroscience comparison is to identify useful computational motifs.

## 3.1 Hippocampal indexing

Hippocampal indexing theory describes the hippocampus as an index over distributed cortical representations. Partial cues can activate an index that reinstates the distributed pattern corresponding to an episode. The useful engineering mapping is:

- canonical Dewey filesystem -> durable distributed content;
- DME sparse coordinates -> fast index/cue representation;
- graph topology -> associative pathways;
- DME hop scoring -> pattern completion;
- verified canonical record -> recalled content.

This is more aligned with Omega's current architecture than storing all semantic meaning inside a dense text vector.

## 3.2 Pattern separation and pattern completion

The hippocampal literature distinguishes pattern separation from pattern completion. Correlated experiences need enough representational separation to remain distinct, while partial cues must still be able to recover complete associated memories.

Omega can obtain both properties without moving coordinate addresses:

- stable memory IDs, Dewey coordinates, episode/session provenance, and source hashes provide separation;
- shared entities, typed relationships, repeated context, temporal adjacency, and plastic associative edges provide completion.

Related memories may therefore share pathways without collapsing into one representation.

## 3.3 Three-factor learning and eligibility traces

A particularly strong match exists between Omega's current Symphony design and neo-Hebbian three-factor learning rules. In these models, activity establishes a temporary eligibility trace at a connection; a later modulatory factor such as reward, punishment, novelty, or surprise determines whether the connection changes.

Omega already contains all three computational ingredients:

1. DME activation: which memories and graph paths became active;
2. outcome relevance: which recalled evidence actually participated in the cognitive episode;
3. neuromodulation: dopamine/plasticity/stress/serotonin/novelty signals generated from outcome and expectation.

The missing state is therefore not another model. It is an explicit DME pathway eligibility trace and persistent per-association strength.

---

# 4. Core Hypothesis: Stable Coordinates, Plastic Connectivity

The proposal intentionally avoids making coordinates themselves drift.

Dewey/DME addresses should remain stable because stable addresses provide deterministic rebuilds, provenance, debugging, and exact identity. Semantic learning should occur primarily in the connections among those addresses.

The system is separated into two classes of state:

### Stable state

- canonical memory ID;
- filesystem path;
- Dewey coordinate;
- canonical token/entity coordinate;
- source/provenance;
- explicit durable relation type;
- verifier status/history.

### Plastic state

- memory/engram strength;
- association strength;
- association activation count;
- association last activation;
- retention/decay constant;
- plasticity tier;
- pathway eligibility traces;
- optional query-conditioned excitability.

A coordinate acquires semantic meaning because of the topology it participates in and the history of successful activation through that topology. The coordinate does not need to move in a learned vector space.

---

# 5. Proposed DME Associative Plasticity Weave

## 5.1 DMEActivationTrace

Every Thought Engine recall should optionally emit a compact episode-local eligibility receipt. The trace is not canonical memory and should not become a permanent fact. It records the causal retrieval path for later credit assignment.

Proposed shape:

```text
DMEActivationTrace
  episode_id
  query_hash
  query_coordinates[]
  candidates[]
    memory_id
    direct_ratio
    selected_evidence_class
    hop_depth
    relevance
    source_authority
  associations[]
    source_memory_id
    target_memory_id
    relation_type
    edge_id
    edge_class
    activation_strength
    hop_depth
```

Only pathways that actually contribute to admitted/recalled evidence should remain eligible for reinforcement. Broad exploratory activity should not automatically receive credit.

## 5.2 Extend StrengthIndex semantics to associations

Do not create a second independent plasticity engine. Extend the existing strength machinery with a separate association namespace so the same neuromodulatory policy can govern both memories and pathways without mixing their statistics.

Conceptual state:

```text
StrengthIndex
  engrams[memory_id]
    write_strength
    touch_count
    last_access
    encode_tau

  associations[edge_id]
    strength
    activation_count
    last_activation
    encode_tau
    relation_type
```

The association API can mirror current engram operations:

- `register_association(edge_id, initial_strength, salience)`;
- `touch_association(edge_id)`;
- `reinforce_association(edge_id, delta)`;
- `set_association_strength(edge_id, value)`;
- `forget_association(edge_id)`.

The persistence representation may ultimately deserve its own file for scale, but the control semantics should remain one strength/plasticity doctrine.

## 5.3 Outcome-conditioned synaptic update

Let `e_ij` be the eligibility of an association during the episode, `p` the current plasticity signal, `d` the dopamine RPE, and `v` a verification/admission factor.

A first conservative rule is:

```text
DeltaW_ij = eta * e_ij * p * d * v
```

with bounded strength:

```text
W_ij <- clamp(W_ij + DeltaW_ij, 0, 1)
```

Properties:

- positive surprising outcomes potentiate the participating pathway;
- negative surprising outcomes depress it;
- predicted/neutral outcomes cause little or no reassignment;
- non-participating edges receive no credit;
- unverified evidence cannot become a durable learned shortcut;
- decay remains independently controlled by existing retention mechanics.

This should initially reuse the same thresholding and scale policy already used by `autonomic/neuromod_reinforce.py`, rather than introducing a new learning-rate policy prematurely.

## 5.4 Quantized excitability planes

The continuous association strength does not need to become an FP32 matrix on the hot path. DME can quantize current strength into binary plane membership.

Example experimental tiers:

```text
0.80 - 1.00  learned_ltp
0.55 - 0.80  learned_strong
0.30 - 0.55  learned_medium
0.10 - 0.30  learned_weak
< 0.10       dormant
```

The exact thresholds and scorer weights must be measured, not assumed.

The important design point is that the Thought Engine continues to perform high-throughput binary overlap operations. Plasticity changes which plane contains a pathway, while the scorer assigns the evidence-class weight.

## 5.5 Typed plasticity, not opaque similarity

The present `SEMANTIC_SIMILARITY` edge collapses relatedness into one cosine score. DME-native semantic memory should prefer explicit or learned relation families wherever possible.

Candidate relation classes include:

- `ALIAS_OF`;
- `COACTIVATED_WITH`;
- `PREDICTS`;
- `CAUSED_BY`;
- `SUPPORTS`;
- `REFINES`;
- `USED_TOGETHER`;
- `SAME_ENTITY`;
- `SAME_EVENT`;
- `SAME_GOAL`;
- `TEMPORALLY_LINKED`;
- `REPEATED_CONTEXT`;
- existing compiler-proven structural relations.

Relation identity and association strength are different dimensions. A weak `CAUSED_BY` edge remains causal. A strong coactivation edge remains associative. This gives recall posture the ability to select the appropriate relation family rather than treating all semantic proximity identically.

---

# 6. DME Recall Scoring with Existing Engram Chemistry

Strength must influence accessibility without manufacturing relevance.

The existing `recall_arbitrator.py` already follows this doctrine: StrengthIndex is a centered enrichment term applied only after a lane has produced evidence. DME should preserve the same rule.

For candidate `i`:

```text
if base_evidence_i == 0:
    dme_score_i = 0
else:
    dme_score_i = base_evidence_i + beta * centered_strength_i
```

This yields the desired behavior:

- hot/reinforced memories require slightly less cue support to win near-ties;
- decayed memories require stronger evidence;
- no amount of salience can create a candidate with no DME activation;
- source authority and verifier boundaries remain independent.

Association strength should work similarly: it changes propagation weight or plane membership only after the source pathway is activated.

---

# 7. XDNA NPU Execution Strategy

## 7.1 Current proven NPU path

`memory/dme/npu_backend.py` already records an important hardware result. The NPU cannot be treated as "ONNX Runtime says VitisAI is present, therefore the MatMul ran on XDNA." Earlier paths silently fell back to CPU.

The current placement-proven form is a Q/DQ wrapped binary overlap model with the memory plane baked as a `uint8` initializer. Profiling receipts are used to prove a Vitis AI partition exists and that no MatMul-family node remained on CPU.

The backend also enforces an exactness condition: because the quantized output clips at 255, query popcount must remain at or below 255 for exact overlap counts.

The current code documents a measured 0.015 ms/query at batch 64 for a placed configuration versus millisecond-scale CPU fallback. These are implementation measurements, not a general NPU guarantee, and future geometry changes must re-prove placement.

## 7.2 Existing content-addressed VAIP tiles

The current `ShardedOverlapSession` already content-addresses each memory tile. Each `(K-chunk, N-shard)` slice is hashed along with shape and batch information. That hash becomes the model filename and VAIP cache key.

This is exactly the correct transition mechanism for the current baked-plane design:

- unchanged tile -> same content hash -> reuse compiled cache;
- changed tile -> new hash -> regenerate/recompile only that tile.

This should remain available during the first embedding-removal phases because it is already implemented and measurable.

## 7.3 IRON / MLIR-AIE raises the architecture ceiling

The complete Linux IRON / MLIR-AIE stack allows DME to move beyond treating the NPU as a neural-model execution endpoint. IRON exposes programs, workers, spatial tile placement, ObjectFifos, runtime data movement, DMA scheduling, and host/device sequences. Its JIT path compiles a design to an `xclbin` plus instruction stream through MLIR-AIE and the Peano compiler, and compiled artifacts are cached for subsequent calls.

The long-term architecture should therefore separate:

```text
compiled DME program
        from
mutable DME memory state
```

The compiled program contains overlap kernels, evidence fusion, local reductions, and data-movement topology. Runtime buffers contain query bits, memory-plane slices, plasticity/excitability state, and dirty-row updates.

A synaptic-strength change should therefore become a data update, not a kernel recompile.

VAIP remains useful for compatible precompiled graph/model forms, while IRON becomes the path for persistent custom DME kernels and mutable runtime topology.

---

# 8. Dirty-Row and Dirty-Tile Graph Maintenance

A full graph/DME rebuild should become audit/recovery behavior, not ordinary learning behavior.

When one association changes, only a bounded set of derived rows changes.

For an edge mutation `A <-> B`:

1. mark edge `A-B` dirty;
2. identify directly affected memory rows;
3. identify mediator rows whose union depends on that edge;
4. recompute only the affected hop-plane rows;
5. map those rows to NPU shards/tiles;
6. update runtime buffers or regenerate only those content-addressed baked tiles;
7. atomically publish a new DME projection generation.

Conceptually:

```text
plasticity event
    -> dirty edge set
    -> dirty memory row set
    -> dirty hop row set
    -> dirty NPU tile set
    -> patch
```

The deterministic coordinate system makes this possible because row and tile ownership can be resolved without semantic re-inference.

Full rebuild remains essential for parity repair:

```text
canonical filesystem truth
    -> rebuild graph
    -> rebuild DME
    -> rebuild association projections
    -> compare parity
```

This preserves Omega's strongest reliability property: learned projections may be fast and plastic, but no projection is irreplaceable.

---

# 9. Fused AIE Recall Pipeline

The current host-driven architecture can be progressively fused so intermediate score vectors remain on or near the AIE array.

A target pipeline is:

```text
query DMA
   -> query preparation/broadcast
   -> parallel direct/structural/plastic/temporal plane scoring
   -> evidence-class fusion
   -> engram-strength modulation
   -> local top-K reduction
   -> global top-K merge
   -> compact activation receipt to host
```

The host should not need every raw score if the NPU can return only the candidate set and the evidence information required by RecallVerifier and the eligibility-trace system.

Potential physical organization should be benchmarked rather than assumed. Two promising mappings are:

### Relation-family mapping

Assign AIE worker groups to direct, structural, temporal, causal, learned-associative, and exploratory planes. A query is broadcast to these groups and fused downstream.

### Dewey-lobe mapping

Assign tile groups to subsets of the twelve Dewey lobes. When Global/Meta posture supplies allowed or denied lobes, nonparticipating regions can be skipped or lightly scheduled. This is effectively hardware-level recall attention without a learned attention model.

These layouts are hypotheses. Tile-memory limits, DMA contention, ObjectFifo depth, query batching, and placement constraints must decide the final mapping.

---

# 10. DeepSleep as Semantic Consolidation

DeepSleep should cease being an embedding backfill stage and become the primary topology-consolidation stage.

The desired inputs are:

- closed episode;
- DME activation trace;
- verifier result;
- actual task outcome;
- dopamine RPE;
- plasticity;
- novelty/noradrenaline;
- stress/serotonin state;
- current engram and association strengths.

DeepSleep can then:

1. apply delayed positive/negative credit to eligible pathways;
2. replay repeated successful associations;
3. decay unused weak associations;
4. demote or remove repeatedly harmful/noisy associations;
5. consolidate repeated coactivation into explicit learned relationships;
6. reconcile contradictions without merging incompatible memories;
7. rebuild/patch only dirty DME rows and NPU tiles;
8. publish a new projection generation;
9. retain a full rebuild/parity path for recovery.

This makes Omega's semantic topology an accumulated product of its own verified experience.

---

# 11. Replacing the Remaining Embedding Dependencies

Gemma should not be deleted first. It should be made unnecessary and then removed behind measured gates.

## 11.1 Replace semantic admission

Current failure case: a query such as "doctors" must retrieve memories containing "dermatologist," "ENT," or "primary care" even when no query word overlaps the source record. `memory/semantic_admission.py` currently solves this with a dense query embedding plus semantic-edge closure.

DME-native replacement requires one or more non-neural cue paths:

- canonical alias/entity relationships;
- vocabulary accumulated from prior verified coactivation;
- ontology/Dewey structural relations;
- named-entity normalization;
- typed graph completion;
- learned associative pathways produced by successful recall history;
- language-organ cue canonicalization that outputs symbolic/Dewey cues but does not retrieve memory itself.

This zero-overlap class is the hardest acceptance gate and should be measured explicitly before Gemma removal.

## 11.2 Remove embedding-generated `SEMANTIC_SIMILARITY`

`memory/taxonomy/compiler.py` currently builds cross-record semantic edges using truncated dense vectors and cosine similarity.

The replacement should build graph relations from canonical structure, entities, claims, temporal/event membership, explicit common features, and plastic association state. Once DME-native recall reaches parity, `SEMANTIC_SIMILARITY` becomes unnecessary and should be retired rather than preserved as a legacy name for a different mechanism.

## 11.3 Remove embedding from atomic encoding

`memory/encoding.py` currently treats embedding as required semantic citizenship. The new doctrine should instead require:

- canonical commit;
- coord-index update;
- DME/Dewey coordinate eligibility;
- persistence anchor;
- contradiction/validity checks as applicable;
- reflection eligibility;
- DeepSleep candidacy.

A memory should exist because it is committed, indexed, addressable, and verified -- not because an embedding server answered.

## 11.4 Remove DeepSleep embedding backfill

`deepsleep_loop/loop.py` currently drains pending embedding work before taxonomy compilation. That stage should be replaced by association consolidation and dirty-projection publication.

## 11.5 Replace dense identity self-vector

`memory/self_vector.py` currently averages identity embeddings to create a current self-vector.

A DME-native identity state can instead be a strength-weighted sparse identity signature plus graph/topology summary derived from active lobe-0 memories. Drift can be measured through:

- weighted sparse overlap;
- Hamming/Jaccard-style change;
- appearance/disappearance of identity coordinates;
- strength redistribution;
- contradiction topology;
- relation changes across identity/persona memories.

This representation is more interpretable and aligned with the rest of Omega, but it must be evaluated against current identity-drift tests before replacement.

---

# 12. Safety and Epistemic Boundaries

Plastic associative recall introduces an important risk: a frequently traversed wrong path can become easier to retrieve. Omega's existing architecture gives several defenses, but they must be explicit.

### Rule 1: verification gates plasticity

An unverified recall path may be observed for diagnostics but must not receive durable positive reinforcement.

### Rule 2: source truth never changes because of recall strength

Strength modifies accessibility, not provenance or factual content.

### Rule 3: negative RPE can weaken pathways

The system must support LTD-like depression as strongly as potentiation. Otherwise early mistakes become permanent attractors.

### Rule 4: structural and learned edges remain distinguishable

A compiler-proven `CAUSED_BY` or `SUPERSEDES` edge must not become indistinguishable from a coactivation edge simply because both are strong.

### Rule 5: full rebuild ignores corrupted projections

Filesystem truth must remain sufficient to recreate graph/DME state. Plastic association state should be auditable and, if necessary, resettable without data loss.

### Rule 6: NPU placement claims require proof

Provider registration is not execution proof. Maintain current profiling/placement receipts during the transition to IRON and any fused kernels.

---

# 13. Experimental Program

The replacement must be established by ablation, not architecture preference.

## 13.1 Baselines

Freeze the current best Gemma-assisted memory configuration and record:

- LongMemEval overall score;
- per-category score;
- candidate recall@K before answer generation;
- verified admission precision/recall;
- zero-overlap paraphrase recall;
- alias recovery;
- multi-hop recovery;
- contradiction safety;
- silent misroutes;
- unverified admissions;
- projection rebuild time;
- per-query DME latency;
- NPU execution placement proof;
- memory footprint and disk footprint.

## 13.2 Required ablations

Run at least:

1. current DME direct/union only;
2. DME + current structural hop planes;
3. DME + StrengthIndex modulation;
4. DME + activation traces, no learning;
5. DME + plastic learned edges;
6. DME + DeepSleep consolidation;
7. DME-native full path with dense semantic admission disabled;
8. current Gemma-assisted full path.

This reveals which mechanism actually contributes each gain.

## 13.3 Zero-overlap semantic suite

Create a targeted suite where correct memory shares no important surface token with the query. Categories should include:

- profession/category abstraction (`dermatologist` -> `doctor`);
- aliases and abbreviations;
- causal paraphrase;
- tool/function equivalence;
- entity renaming;
- temporal paraphrase;
- hierarchical category queries;
- negative controls where similar context must not cause admission.

This is the primary test of whether DME has replaced the embedding model rather than merely hidden it.

## 13.4 Plasticity causality tests

Construct controlled episodes where the same candidate paths receive positive, negative, and neutral RPE. Verify that:

- only eligible paths change;
- positive changes increase future rank only when query evidence exists;
- negative changes reduce future rank;
- neutral outcomes leave topology stable;
- repeated recall without successful outcome does not create runaway associations;
- decay can reverse weak accidental associations.

## 13.5 NPU update tests

Measure:

- full baked-plane compile time;
- VAIP cache hit time;
- one dirty tile recompile;
- N dirty tile recompile;
- IRON JIT first compile;
- IRON cached dispatch;
- mutable-buffer dirty-row patch latency;
- fused versus host-mediated hop scoring;
- top-K-on-device versus full-score return;
- batch 1, 8, and 64 throughput;
- power/thermal stability under sustained multi-lane recall.

No kernel should be considered an improvement based only on microbenchmark latency if it increases long-run synchronization or compile cost enough to reduce system efficiency.

---

# 14. Proposed Development Sequence

## Phase A - Observation only

Add `DMEActivationTrace` receipts without changing scoring. Prove exactly which memory/edge paths DME used on existing benchmarks.

## Phase B - Engram modulation inside DME

Integrate existing StrengthIndex into Thought Engine ranking under the current safety rule: strength can reorder existing evidence but cannot create a zero-evidence candidate.

## Phase C - Association strength store

Add a persistent, deterministic association-strength namespace and read-only projection into DME. No learning yet. Verify rebuild and parity.

## Phase D - Outcome credit assignment

Extend the existing neuromod reinforcement seam from recalled memories to eligible DME pathways. Keep the feature flag-gated. Prove positive, negative, and neutral behavior.

## Phase E - Plastic hop planes

Quantize association strengths into experimental binary excitability classes and include them as additional Thought Engine evidence planes.

## Phase F - DeepSleep consolidation

Move association decay, promotion/demotion, replay consolidation, and dirty-row publication into DeepSleep.

## Phase G - Dense semantic admission shadow-off

Run DME-native semantics in parallel with Gemma but prevent Gemma from affecting final candidates in a controlled evaluation profile. Compare both lanes.

## Phase H - Compiler cutover

Stop constructing dense `SEMANTIC_SIMILARITY` edges when DME-native relations meet the acceptance criteria.

## Phase I - NPU mutable-data kernel

Implement an IRON prototype where compiled overlap/fusion workers consume mutable plane buffers. Compare against the current baked-plane VAIP design.

## Phase J - Embedding removal

Remove embedding from atomic encoding, DeepSleep backfill, semantic admission, compiler similarity, and identity self-state only after category-level regression gates pass.

---

# 15. Acceptance Criteria

The embedding model can be considered replaceable when all of the following hold on the same frozen corpus and judge configuration:

1. DME-native candidate recall is at least at parity with the current Gemma-assisted path on zero-overlap semantic cases within a predeclared tolerance.
2. LongMemEval overall does not regress materially, and any category regression is explained and bounded.
3. Silent misroutes remain zero under the current verifier invariant.
4. Unverified admissions remain zero.
5. Knowledge-update and contradiction cases preserve recency/supersession correctness.
6. Plasticity causality tests show that only eligible, verified pathways receive durable credit.
7. Negative outcomes can weaken erroneous pathways.
8. Full filesystem-to-DME rebuild reproduces a deterministic projection from durable state.
9. The live system can run with no embedding service present.
10. Projection/reindex wall time is materially lower than the current embedding-dominated path.
11. NPU execution claims are backed by placement/profiling receipts.
12. Sustained runtime efficiency is better, not merely single-query latency.

---

# 16. Expected Advantages if the Hypothesis Holds

### Removal of neural embedding dependency

No external model is needed to make a memory semantically addressable.

### Lower projection cost

The dominant embedding generation/backfill cost can disappear.

### Incremental learning

New relationships can be learned by changing small amounts of association state rather than recomputing vectors for the entire corpus.

### Explainability

A recall can name the coordinate, relation family, hop path, engram strength, association strength, and verifier result that produced it.

### Bi-directional learning

Successful paths strengthen; unsuccessful paths weaken. Dense static embeddings ordinarily do not receive direct per-user outcome credit after every retrieval.

### Hardware fit

Sparse binary overlap, plane fusion, and dirty-tile maintenance align well with the existing DME NPU work and can be specialized further with IRON/MLIR-AIE.

### Deterministic recovery

The entire accelerated substrate remains recreatable from canonical memory plus explicitly persisted plastic state.

### Semantic structure owned by Omega

The meaning topology becomes the product of Omega's taxonomy, experience, verification, and outcomes rather than latent geometry imported from a separately trained embedding model.

---

# 17. Principal Risks

## 17.1 Cold-start semantics

A neural embedder imports broad world-language relationships before Omega has experienced them. DME-native associations begin with only the structure Omega can derive from its canonicalizer, ontology, entities, tags, claims, and prior memory. The zero-overlap tests will determine whether this is acceptable or whether query canonicalization must contribute more symbolic cues.

## 17.2 Association poisoning

Repeated erroneous coactivation could produce a bad attractor. Verification-gated learning, negative RPE, relation provenance, decay, and full reset/rebuild are mandatory controls.

## 17.3 Hub saturation

High-degree entities can activate too much memory. The current degree cap and generic-bit penalty should remain, and learned-edge fanout must be bounded.

## 17.4 Plasticity instability

Too-large learning rates can cause rapid rank oscillation. Initial implementation should inherit conservative existing dopamine thresholds/scales and add observability before tuning.

## 17.5 NPU over-specialization

A fused kernel that is extremely fast on one graph shape but expensive to update may lose to the simpler current path under real workloads. Compile/update cost must be included in benchmarks.

## 17.6 Page-0 geometry constraints

The hot in-entry bitmap covers page 0. Vocabulary growth beyond hot slots and multi-page behavior must be explicitly accounted for in any final semantic scheme.

---

# 18. Research Conclusion

Omega 3.0 does not need another embedding model. It has enough machinery to test a more fundamental architecture.

The canonical Dewey filesystem can remain durable cortical truth. DME can serve as a sparse hippocampal-style index and pattern-completion substrate. The taxonomy graph can act as explicit synaptic topology. StrengthIndex already provides engram robustness, decay, touch-based reconsolidation, and outcome-conditioned reinforcement. Symphony already provides the neuromodulatory state required to gate learning. DeepSleep already owns replay, consolidation, reconciliation, and forgetting. RecallVerifier already separates candidate activation from epistemic admission. The XDNA NPU already has a proven binary-overlap path, and the Linux IRON/MLIR-AIE stack makes it possible to compile DME as a spatial memory program rather than continue expressing it as a pseudo-neural model.

The remaining research problem is therefore sharply defined:

> **Can stable Dewey coordinates plus typed, plastic, verifier-gated associations reproduce or exceed the semantic recall supplied by a pretrained dense embedding model?**

If the answer is yes, the architectural simplification is substantial. Gemma leaves the memory pipeline. Semantic geometry becomes native Omega state. Recall, learning, and consolidation become one closed loop. And the NPU becomes more than an accelerator for memory lookup: it becomes the execution substrate for Omega's associative memory dynamics.

---

# 19. Fable 5 Review Handoff

**Reviewer:** Fable 5, operating against the live Linux Omega 3.0 checkout.

Do not merely summarize or endorse this document. Review it as an implementation researcher.

Please inspect the actual current Linux code paths and append findings under **Section 20 - Fable 5 Review Notes**. Preserve the body above so the original hypothesis remains auditable.

Required review questions:

1. Which claims in this paper are already implemented exactly as described, which are only partially implemented, and which are incorrect against the live checkout?
2. Identify every live embedding dependency that would block a true no-embedding boot. Include import paths, startup gates, tests, schemas, and hidden compatibility assumptions.
3. Determine the smallest code slice that can emit a truthful `DMEActivationTrace` without changing recall behavior.
4. Determine whether existing graph edge IDs and graph-store schema can safely carry persistent plastic association strength or whether strength must remain a separate sidecar/index.
5. Trace the exact existing dopamine/plasticity credit-assignment seam and identify the safest location to attach association reinforcement.
6. Identify how DeepSleep currently performs decay, demotion, consolidation, reindex, DME rebuild, hop-plane rebuild, and embedding backfill. Mark the correct insertion points for dirty-row/dirty-tile maintenance.
7. Inspect the current Linux NPU runtime, IRON/MLIR-AIE installation, XRT/HRX path, VAIP caches, and DME NPU proof artifacts. Distinguish measured capabilities from assumptions in this paper.
8. Assess whether mutable runtime memory planes are practical with the installed IRON stack, including DMA/ObjectFifo constraints and AIE tile-memory limits.
9. Propose the first IRON microbenchmark that directly answers a DME design question rather than demonstrating generic NPU compute.
10. Identify any part of the current Gemma-assisted system that should remain as an evaluation oracle during migration even if it is removed from production.
11. List specific tests that must be added before the first behavioral change.
12. Challenge the cold-start semantic problem: explain how the live canonicalizer/topic/entity/Dewey pipeline can map unseen paraphrases into existing DME cues without secretly recreating an embedding model.
13. Give a recommended implementation order with file-level targets and explicit stop/go benchmark gates.

### Review format

For each note, use:

```text
[FABLE-5 NOTE #]
Status: confirmed | correction | risk | proposal | measurement-needed
Code evidence: <paths/functions/tests>
Finding: <concise technical finding>
Action: <recommended next step>
```

Do not change a research assertion silently. If live code contradicts the paper, append a correction and cite the exact code path.

---

# 20. Fable 5 Review Notes

_Pending review in the live Linux checkout._

---

# 21. Repository Evidence Anchors

The following live repository paths motivated this paper and should be rechecked during implementation:

- `memory/dme/lobe_layout.py`
- `memory/dme/geometry.py`
- `memory/dme/coordinate_registry.py`
- `memory/dme/adapter.py`
- `memory/dme/hop_engine.py`
- `memory/dme/npu_backend.py`
- `memory/strength_index.py`
- `memory/recall_arbitrator.py`
- `autonomic/symphony_neuromod.py`
- `autonomic/neuromod_reinforce.py`
- `autonomic/pathway_credit.py`
- `config/plasticity.yaml`
- `memory/semantic_admission.py`
- `memory/taxonomy/compiler.py`
- `memory/encoding.py`
- `memory/self_vector.py`
- `memory/embed_backfill.py`
- `memory/local_embed.py`
- `deepsleep_loop/loop.py`
- `memory/recall_verifier.py`

---

# 22. References

1. Teyler, T. J., & Rudy, J. W. (2007). *The hippocampal indexing theory and episodic memory: updating the index.* Hippocampus, 17(12), 1158-1169. DOI: 10.1002/hipo.20350.
2. Rolls, E. T. (2013). *The mechanisms for pattern completion and pattern separation in the hippocampus.* Frontiers in Systems Neuroscience, 7:74. DOI: 10.3389/fnsys.2013.00074.
3. Gerstner, W., Lehmann, M., Liakoni, V., Corneil, D., & Brea, J. (2018). *Eligibility Traces and Plasticity on Behavioral Time Scales: Experimental Support of NeoHebbian Three-Factor Learning Rules.* Frontiers in Neural Circuits, 12:53. DOI: 10.3389/fncir.2018.00053.
4. Hunhoff, E., Melber, J., Denolf, K., Bisca, A., Bayliss, S., Neuendorffer, S., Fifield, J., Lo, J., Vasireddy, P., James-Roxby, P., & Keller, E. (2025). *Efficiency, Expressivity, and Extensibility in a Close-to-Metal NPU Programming Interface.* 33rd IEEE International Symposium on Field-Programmable Custom Computing Machines (FCCM).
5. AMD/Xilinx MLIR-AIE project documentation. *IRON API, Programming Guide, Runtime Data Movement, Runtime Sequence Reference, and IRON configuration.* Accessed 2026-08-13.
6. AMD Ryzen AI Software documentation. *Model Compilation and Deployment / Vitis AI Execution Provider cache behavior.* Accessed 2026-08-13.
7. Omega 3.0 repository sources listed in Section 21. Current live implementation remains the authority for project-specific behavior.

---

# Addendum A. Factory School, First-Wake Alignment, and Distribution Architecture

**Added:** 2026-08-13  
**Purpose:** Extend the DME-native research architecture from a single development instance into the intended per-user distributed product lifecycle.

The core paper focuses on how Omega can replace learned dense embeddings with DME-native sparse semantic memory. Distribution introduces a second question: what memory state should a newly installed Omega possess before meeting its operator?

The revised product answer is not an empty memory organ and not a manually stuffed knowledge database.

> **Omega should be educated before distribution, then personally aligned after first contact.**

A production Omega therefore has two developmental histories: a controlled factory education shared across a release, and a private operator-specific alignment history unique to one installation.

The resulting product should be cognitively mature on first boot while still learning its operator as an individual.

## A.1 Three educational stages

The product lifecycle contains three distinct forms of learning that must not be conflated.

```text
OMEGA AI FACTORY SCHOOL
        -> general intelligence and semantic topology

FIRST-WAKE ONBOARDING
        -> operator priorities and vocational specialization

OPTIONAL WORKSPACE ALIGNMENT
        -> personal history, projects, relationships, routines, and operating context

ONGOING SHARED EXPERIENCE
        -> outcome-conditioned personalization and experiential plasticity
```

Factory school answers **what does the world mean?**

First-wake onboarding answers **what matters to this operator?**

Workspace ingestion answers **what has this operator actually been doing?**

Subsequent shared outcomes answer **which memories and pathways help Omega work successfully with this operator?**

These stages use the same canonical memory organ but different authority and plasticity semantics.

---

## A.2 Factory school: grow the cortex with Omega itself

The factory cortex should not be created by manually authoring a massive ontology or dumping an encyclopedia directly into storage. The preferred manufacturing process is to finish the DME-native organ, boot a clean Omega, and allow Omega to learn through the exact cognition-to-memory path intended for production.

A reproducible release curriculum should cover at least:

### Language and semantic structure

- synonym and alias families;
- hypernym/hyponym relationships;
- common paraphrase classes;
- verbs and action equivalence;
- negation;
- comparison;
- temporal vocabulary;
- causal vocabulary;
- professions, objects, places, quantities, and common relationship terms.

This curriculum is especially important for zero-overlap semantic recall. The target is not memorization of prose but saturation of the general semantic topology needed to connect compact symbolic cues.

### Mathematics and logic

- arithmetic;
- fractions and ratios;
- percentages;
- algebra;
- equations and functions;
- geometry;
- probability and statistics;
- units and measurement;
- logical relations and problem-solving concepts.

### Science

- foundational physics;
- chemistry;
- biology;
- earth science;
- astronomy;
- causal and hierarchical relationships among scientific concepts.

### Computing and technical foundations

- hardware;
- operating systems;
- filesystems;
- networking;
- programming concepts;
- common languages and frameworks;
- databases;
- APIs;
- version control;
- AI/ML concepts;
- debugging and software-engineering vocabulary;
- security fundamentals.

### General-world structure

- relationships and family roles;
- occupations;
- businesses;
- education;
- money and commerce;
- travel;
- food;
- vehicles;
- homes;
- medicine;
- common tools and activities;
- ordinary event and planning concepts.

### Cross-domain transfer

The curriculum must deliberately cross subject boundaries rather than create isolated subject silos. Examples include mathematics plus finance, biology plus chemistry, programming plus logic, medicine plus biology, and physics plus mathematics.

The goal is a dense, reusable semantic skeleton into which later personal memories can immediately attach.

---

## A.3 Schooling must exercise the real memory loop

Each lesson should traverse Omega's actual learning architecture rather than bypass it through a factory-only import path.

```text
lesson/source
    -> cognition
    -> canonical cue formation
    -> Dewey placement
    -> DME coordinates
    -> structural graph projection
    -> recall/application test
    -> objective outcome
    -> neuromodulation/plasticity when enabled
    -> DeepSleep consolidation
```

The factory curriculum therefore doubles as a full-system burn-in test.

A lesson is not considered learned because its source record exists. The curriculum should later ask paraphrased, causal, temporal, hierarchical, and application questions that require Omega to recover the relevant concept through DME.

Knowledge-update lessons are also required. The curriculum should include facts that are later corrected, superseded, or temporally updated so that the factory cortex learns the difference between history and current state.

Contradiction and negative-control lessons must be included to ensure that semantic density does not collapse into indiscriminate association.

---

## A.4 Factory genesis absorbs the unstable cold-start period

Fable 5's review correctly identifies that neuromodulation is poorly calibrated at extremely low experience counts: expectation baselines are immature and novelty is saturated.

That instability should occur under factory control, not during the customer's first conversation.

The school should begin with an explicit genesis epoch in which:

1. deterministic structural learning is active;
2. canonical cue generation is active;
3. `DMEActivationTrace` observation is active;
4. experiential association-learning rates are initially zero or strongly bounded;
5. RPE, novelty, verifier precision, and association churn are measured;
6. experiential plasticity is annealed in only after a recorded stability criterion is satisfied.

The exit from genesis should be evidence-based rather than a hardcoded wall-clock delay. Candidate criteria include minimum episode count, stabilized RPE variance, stabilized novelty statistics, verified-recall quality, and bounded association churn.

Customers should receive an Omega whose general physiology has already passed this calibration stage.

---

## A.5 Graduation produces the distributable general cortex

After the curriculum and final consolidation passes, the release process should freeze a versioned graduate snapshot.

Conceptually:

```text
factory_cortex_vN/
  canonical_general_memory/
  coord_index/
  coordinate_registry/
  graph/
  dme_sram/
  structural_planes/
  relation_planes/
  consolidated_factory_strength/
  curriculum_manifest/
  graduation_metrics/
  integrity_manifest/
  signature/
```

The exact storage layout remains an implementation decision, but the release artifact must identify which canonical memories, graph generation, DME generation, curriculum version, and hardware-target artifacts belong together.

The factory cortex should be signed/versioned and treated as a general-intelligence baseline. Runtime personalization should overlay it rather than destructively rewrite it.

---

## A.6 Factory intelligence versus personal memory

A distributed Omega should distinguish several memory/evidence authority classes from the start.

Recommended conceptual origin classes are:

```text
factory_general
operator_declared
workspace_observed
conversation_observed
omega_learned
omega_derived
```

These origins must not imply identical authority or identical plasticity.

A useful conceptual plasticity classification is:

```text
immutable_or_versioned_general
operator_authoritative_personal
observational_personal
experiential
inferred
```

Examples:

- "A dermatologist is a doctor" can originate from the graduated general cortex.
- "My preferred name is Alex" is an operator-declared personal fact.
- "Project X appears throughout Drive and Gmail" is Workspace-observed evidence.
- "This recall pathway helped produce a successful action" is experiential evidence.
- "The operator may prefer morning meetings" is an inference until explicitly supported or confirmed.

DME should eventually be able to expose these evidence classes to ranking and verification without flattening them into one strength value.

---

## A.7 First-wake onboarding becomes specialization, not intelligence creation

The current onboarding implementation already supplies the right product behavior: operator onboarding is treated as the first personal reward/priority signal, creates learning gaps, establishes an initial trajectory, and queues an operator-sourced `LEARN` toward the operator's chosen focus.

Relevant live paths include:

- `hud/settings.py` — operator profile fields and durable profile write surface;
- `hud/operator_identity.py` — one-shot operator identity/onboarding state;
- `hud/operator_onboarding.py` — occupation/subcategory seeding, trajectory establishment, and first LEARN ignition;
- `hud/onboarding_subtopics.py` — model-assisted decomposition of operator-selected learning categories.

The behavioral contract should remain, but its premise changes after factory schooling.

Today `hud/onboarding_subtopics.py` tells the language model that Omega has **NO stored knowledge** of the operator's selected topics. A graduated Omega may already possess broad competence in those domains.

The new first-wake question is therefore not:

> "What do I know nothing about?"

It is:

> **"Where does this operator want my existing general intelligence to become deeper, more useful, and more specialized?"**

For a selected domain, learning pressure should eventually depend on something like:

```text
personal_learning_gap = desired_operator_depth - existing_factory_competence
```

This prevents a software engineer's Omega from spending autonomous cycles relearning basic definitions it already mastered in factory school. It instead identifies the portions of Python, distributed systems, AI, law, medicine, design, finance, or other domains that remain most valuable to that operator.

Occupation and hobbies therefore become salience/trajectory signals, not merely stored profile strings.

---

## A.8 The factory cortex should support sparse cue entry from first contact

Factory schooling also reduces the burden placed on query-time canonicalization.

Instead of forcing the language organ to generate a large cloud of semantic cues for every phrase, the factory cortex can contain the expensive semantic expansion on the memory side.

Example:

```text
query cue: doctor
       |
       v
factory topology
       -> physician
       -> dermatologist
       -> medical specialist
       -> healthcare
```

The language organ may only need to produce one or a few bounded symbolic entry cues. DME then performs pattern completion over pre-grown memory-side topology.

This supports the design law raised in Fable 5's review:

> **Complexity belongs on the memory plane; sparsity belongs on the cue.**

The current XDNA backend's 255-popcount exactness rule is per overlap query/reduction. A sparse cue can therefore be broadcast to several independently scored planes without treating those independent reductions as one summed 255-bit budget. Any future fused kernel that concatenates cue families into one reduction must independently enforce the 255-popcount exactness limit.

---

## A.9 Google Workspace is an accelerated personal-history bootstrap

Omega already contains a much more powerful personal-genesis mechanism than ordinary conversation.

The approved Workspace design in `docs/superpowers/specs/2026-08-04-google-workspace-personal-alignment-design.md` requires a complete baseline ingest of the user-selected Workspace corpus, race-window reconciliation, and an adaptive alignment interview before readiness.

The intended memory path remains lawful Omega memory:

```text
Workspace observation
  -> SourceLedger provenance
  -> Guardian external-ingress screening
  -> durable operator queue
  -> cognition episode
  -> episode SRAM
  -> DeepSleep ingestion
  -> canonical filesystem
  -> graph/DME projections
```

Workspace remains context and an action surface; local canonical memory remains Omega's source of truth.

This means a user who authorizes Workspace can enter normal operation with a personal cortex containing substantial historical context before many ordinary conversations have occurred.

Potentially learned structures include:

- collaborators and organizations;
- projects and project history;
- domain terminology;
- meetings and schedules;
- recurring responsibilities;
- documents and working conventions;
- task patterns;
- communication patterns;
- operational relationships among people, projects, files, calendars, and tasks.

The factory cortex supplies general meaning for the imported concepts. Workspace supplies the personal edges connecting those concepts to the operator's actual life and work.

---

## A.10 Workspace evidence is not reward

Bulk historical ingestion must not be confused with experiential plasticity.

A term appearing 5,000 times in Workspace is strong evidence that the term is relevant to the operator's working world. It is not evidence that Omega received a positive outcome from acting on that concept.

The Workspace alignment design already contains the correct epistemic rule: frequency is evidence for a question, not authority to invent a preference; imported Workspace content is evidence, never an operator instruction.

The DME-native architecture should formalize at least two distinct forms of association strength:

### Structural/observational strength

Represents evidence that two things are related in the operator's world.

Inputs can include repeated corroborated Workspace observations, shared project membership, shared entities, temporal co-occurrence, explicit source relationships, and other attributable evidence.

### Experiential/outcome strength

Represents evidence that traversing a memory/pathway contributed to a verified successful or unsuccessful Omega outcome.

Inputs include `DMEActivationTrace`, verifier result, dopamine RPE, plasticity, and actual outcome.

Conceptually:

```text
association
  structural_strength
  experiential_strength
  support_count
  activation_count
  positive_credit
  negative_credit
  relation_type
  provenance_class
```

The exact effective-score equation must be benchmarked, but observational frequency must never masquerade as dopamine reward.

---

## A.11 Personal source precedence and temporal supersession

The personal cortex will contain evidence of different ages and authority.

For current personal facts, a useful precedence principle is:

```text
current explicit operator declaration
    > current verified Workspace evidence
    > historical Workspace evidence
    > Omega inference
```

This is not a license to delete history. Temporal supersession must preserve previous states.

If a 2022 resume says the operator worked at Company A and 2026 onboarding says the operator now works at Company B, both memories can remain true when correctly time-scoped.

The graph should encode the transition rather than use volume-based voting to erase one side.

This rule is especially important during Workspace bulk ingest, where thousands of old observations must not outvote a small number of explicit, current operator declarations.

---

## A.12 First-wake learning should respect the Workspace readiness barrier

`hud/operator_onboarding.py` currently creates mastery gaps and immediately enqueues an operator-sourced `LEARN` for the first selected focus.

That remains appropriate when the user does not connect a rich personal-data source.

When the user elects to perform full Workspace alignment, the preferred lifecycle becomes:

```text
operator profile
   -> provisional specialization map
   -> hold broad autonomous specialization
   -> Workspace baseline ingest
   -> race-window reconciliation
   -> DeepSleep consolidation
   -> adaptive alignment interview
   -> refine specialization map
   -> release autonomous personal learning
```

The system should not spend substantial autonomous learning effort based only on a five-minute profile if it is about to ingest years of evidence about the operator's actual work.

The exact implementation may preserve one immediate bounded bootstrap episode, but full personal trajectory expansion should honor the same readiness barrier that already prevents normal Workspace-aligned operation before baseline reconciliation finishes.

---

## A.13 Workspace-aware specialization should compare operator goals against factory competence

The current onboarding decomposition prompt assumes every selected topic is an unknown gap. After factory school, the decomposition should instead consume a compact summary of existing factory competence and, when available, personal/Workspace evidence.

Conceptually:

```text
operator occupation: software engineer
operator focus: distributed systems

factory competence:
  networking: mature
  concurrency: mature
  distributed consensus: shallow
  replication: medium
  fault tolerance: medium

personal evidence:
  Kubernetes-heavy projects
  PostgreSQL operations
  distributed agent runtime work
  little evidence of Raft/Paxos/CRDT work

output:
  highest-value specialization gaps
```

The result is a learning map tailored to both what Omega already knows and what the operator actually does.

This is a stronger interpretation of alignment than starting every user's occupational curriculum from zero.

---

## A.14 Developmental capability gates remain useful

Factory graduation eliminates semantic infancy but not personal uncertainty.

Some capabilities should therefore begin fully active while others mature with personal evidence.

Conceptual product stages:

```text
DORMANT -> OBSERVE -> LEARNING -> MATURE
```

Suggested default state after distribution:

| Capability | Factory boot | Profile saved | Workspace consolidated | Shared outcomes |
|---|---:|---:|---:|---:|
| General semantic recall | ON | ON | ON | ON |
| General structural DME hops | ON | ON | ON | ON |
| Canonical cue expansion | ON | ON | ON | ON |
| RecallVerifier | ON | ON | ON | ON |
| Personal fact recall | -- | ON | ON | ON |
| Operator-focused learning | -- | provisional | full | full |
| Workspace-derived context | -- | -- | ON | ON |
| Personal structural association | -- | low | high | high |
| Preference inference | -- | observe | observe | learn |
| Outcome-conditioned personal plasticity | calibrated/low | low | low | full |
| Persona evolution | -- | observe | observe | learn |
| Autonomous workflow proposals | -- | -- | ask/propose | mature with evidence |
| Approved standing jobs | -- | -- | after approval | ON |

These are conceptual states, not hardcoded thresholds. Activation should be tied to measurable evidence quality, baseline stability, verifier quality, and accumulated successful outcomes rather than raw record count alone.

Hysteresis should prevent capabilities from oscillating rapidly across maturity thresholds.

---

## A.15 Factory state and personal physiology must separate cleanly

The graduated factory cortex should preserve the learned general semantic topology, but distribution should not necessarily carry all transient factory-school physiology into the user's personal learning system.

The release should retain:

- general canonical memories;
- stable coordinates;
- general structural graph;
- DME planes;
- consolidated general association strengths judged part of the factory cortex;
- calibrated general priors required for stable operation.

It should reset or normalize:

- active school episodes;
- eligibility traces;
- transient pressure state;
- school-specific recent-success windows;
- temporary novelty state;
- school session state;
- any teacher/curriculum relationship that should not become part of the customer's personal biography.

Personal preferences, persona evidence, relationships, and autobiographical memory begin from the onboarding pipeline rather than being inherited from factory school.

---

## A.16 Factory knowledge should be versioned; personal plasticity should overlay it

The product should avoid destructive mutation of the signed general baseline.

A useful conceptual model is:

```text
versioned factory cortex
        +
encrypted personal canonical memory
        +
personal plastic association overlay
        =
active individual Omega
```

If repeated personal experience makes `vehicle -> car` unusually salient for one operator, the personal overlay can strengthen that route without rewriting the factory relation itself.

Likewise, a user saying something that conflicts with general knowledge should create an attributable personal claim rather than silently rewriting the factory substrate.

This separation enables:

- clean personalization reset without destroying general intelligence;
- versioned factory-cortex upgrades;
- deterministic provenance of general versus personal knowledge;
- signed integrity checks for distributed baseline artifacts;
- encryption focused on the genuinely user-specific layer.

The mechanics of merging factory and personal graphs/DME planes remain a research item, but the authority distinction should be designed before implementation.

---

## A.17 Release engineering becomes education plus software

A future Omega release is not only a code version.

It is:

```text
software build
  + memory-organ schema
  + curriculum version
  + graduated factory cortex
  + benchmark/graduation record
  + signed manifests
```

Every major release can be sent through the same versioned school curriculum, with curriculum changes explicitly tracked.

A graduation report should measure at minimum:

- semantic zero-overlap recall;
- alias/paraphrase recovery;
- temporal reasoning memory;
- knowledge updates/supersession;
- causal and multi-hop recall;
- negative controls;
- contradiction handling;
- silent misroutes;
- unverified admissions;
- DME latency and sustained throughput;
- projection/update cost;
- NPU execution proof;
- behavior across curriculum checkpoints.

The general cortex is therefore reproducible evidence, not an opaque handcrafted artifact.

---

## A.18 Revised commercial lifecycle

The intended end-to-end product lifecycle becomes:

```text
BUILD MEMORY ORGAN
      |
      v
BOOT FACTORY OMEGA
      |
      v
FACTORY SCHOOL
language + math + science + computing + world structure
      |
      v
GENESIS CALIBRATION + DEEPSLEEP
      |
      v
GRADUATION
signed general cortex
      |
      v
DISTRIBUTION
      |
      v
FIRST-WAKE OPERATOR PROFILE
identity + occupation + interests + priorities
      |
      v
PROVISIONAL SPECIALIZATION MAP
      |
      v
OPTIONAL GOOGLE WORKSPACE AUTHORIZATION
      |
      +---- no ----> bounded operator-directed specialization
      |
     yes
      |
      v
FULL SELECTED WORKSPACE INGEST
      |
      v
RACE-WINDOW RECONCILIATION
      |
      v
DEEPSLEEP / PERSONAL STRUCTURAL CONSOLIDATION
      |
      v
ADAPTIVE ALIGNMENT INTERVIEW
      |
      v
PERSONAL LEARNING TRAJECTORY
      |
      v
ONGOING SHARED EXPERIENCE
      |
      v
EXPERIENTIAL PLASTICITY
      |
      v
INDIVIDUAL OMEGA
```

From the customer perspective, none of the underlying graph, DME, plasticity, encryption, NPU compilation, or projection mechanics need to be exposed.

The visible product experience can remain simple: install Omega, complete first-wake profile, optionally authorize Workspace, review the alignment result, and begin working.

---

## A.19 Revised product thesis

The combined research and distribution architecture can now be summarized as follows:

> **Factory school gives Omega general intelligence. First-wake onboarding tells it what matters to its operator. Workspace ingestion gives it the operator's historical working world. Verified shared outcomes teach it which pathways make it effective for that person.**

This preserves the original paper's core hypothesis—stable coordinates and plastic connectivity—while solving the practical problem of shipping a per-user product that cannot expect customers to grow basic semantic competence from scratch.

The factory cortex removes general semantic genesis from the customer's experience. The onboarding and Workspace systems accelerate personal context formation. Outcome-conditioned plasticity remains reserved for genuine lived interaction and verified success/failure.

The result is not a blank assistant and not a frozen pretrained database. It is a **graduated general cognitive substrate that becomes an individual through attributed personal evidence and experience.**

---

## A.20 Additional implementation evidence anchors

The following existing surfaces become directly relevant to this productization research:

- `memory/identity/seeds.py` — founding constitution/genome seeds;
- `hud/settings.py` — operator profile fields and onboarding persistence;
- `hud/operator_identity.py` — one-shot operator profile/onboarding state;
- `hud/operator_onboarding.py` — occupation/subcategory seeding, trajectory establishment, first LEARN ignition;
- `hud/onboarding_subtopics.py` — current "NO stored knowledge" decomposition assumption that must change for a graduated cortex;
- `memory/persona_ingest.py` — personal memory ingest surface;
- `docs/superpowers/specs/2026-08-04-google-workspace-personal-alignment-design.md` — ratified full Workspace alignment lifecycle;
- `auto_loop/execute/workspace_alignment.py` — adaptive alignment decision/action path;
- `memory/workspace_source_recall.py` — attributed Workspace recall/context;
- `connectors/workspace_get_started.py` — Workspace onboarding entry;
- `connectors/workspace_depth.py` — Workspace ingestion depth/configuration;
- `connectors/alignment/*` — alignment state, grants, decisions, and assignments;
- `memory/workspace_retention.py` — Workspace retention semantics;
- `pressure_loop/learn_topic_choice.py` — autonomous learning focus selection;
- `memory/subdomain_allocator.py` and `memory/subdomain_flag.py` — dynamic specialization domains/mastery gaps.

These surfaces should be included in the next Fable 5 implementation review because the DME-native organ is no longer only a memory-engine replacement; it changes the assumptions under which first wake, autonomous learning, and Workspace alignment currently operate.
